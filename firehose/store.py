"""
The storage boundary: every read and write of firehose's data files goes
through a Store, so the commands above it (scanning, visualisation) never
touch the files directly.

A Store answers a small set of queries — select papers to scan (with full
metadata), fetch one paper, record scan events, and serve the pre-shaped
reading-state data the visualisation commands consume — and hides where the
answers come from. `LocalStore` answers from the data directory in-process:
the mirror (per-paper metadata), the index (id, submission date, categories
per paper, loaded into memory), and the event log (the append-only record
of scanning, from which the seen-set is derived).

Category subscription is a query-time filter: the index covers all of
arXiv, and a Store instance is constructed with the set of subscribed
category names that selection and reading-state queries range over
(`get_paper` is deliberately unrestricted).
"""

import datetime
import json
import os
import random

from firehose import index
from firehose import mirror
from firehose import stats
from firehose import util
from firehose.paper import Paper


def select_papers(
    cache: dict[str, datetime.date],
    read: set[str],
    *,
    n: int,
    backwards: bool = False,
    randomise: bool = False,
    offset: int | None = None,
    cutoff: datetime.date | None = None,
    rng=random,
) -> list[tuple[str, datetime.date]]:
    """
    Choose which (xid, date) papers to scan from the cache.

    Drops already-read ids, then (when a `cutoff` is given) papers dated on or
    before `cutoff`, then takes a window of size `n`:

      * default:        the last `n` candidates, reversed (newest first);
      * backwards=True:  the first `n` candidates, in cache order (oldest first);
      * randomise=True:  up to `n` candidates drawn at random via `rng`.

    `offset`, when given, first narrows to the last `offset` candidates (paging
    back through older unread papers); `n <= 0` selects nothing. Pure: no I/O,
    clock, or global RNG — pass a seeded `rng` for deterministic sampling in
    tests.
    """
    if n <= 0:
        return []
    unread = [(xid, date) for xid, date in cache.items() if xid not in read]
    if cutoff is not None:
        unread = [(xid, date) for xid, date in unread if date > cutoff]
    if offset is not None:
        unread = unread[-offset:]
    if backwards:
        return unread[:n]
    if randomise:
        return rng.sample(unread, min(n, len(unread)))
    return unread[-n:][::-1]


class LocalStore:
    """
    A Store over the data directory, in-process.

    Construction loads the index (narrowed to the subscribed categories) and
    the event log; queries then run against these in-memory structures, and
    `record_events` keeps them in step as it appends to the log on disk.
    """

    def __init__(self, paths, subscribed: set[str]):
        self._paths = paths
        self._subscribed = subscribed
        self._lazy_dates: dict[str, datetime.date] | None = None
        self._events: list[dict] = []
        self._seen: dict[str, datetime.date] = {}   # xid -> first-seen date
        self._events_offset = 0                     # bytes of the log consumed
        self.refresh_events()
        print(f"loaded {len(self._events)} events "
              f"({len(self._seen)} papers seen)")

    @property
    def _dates(self) -> dict[str, datetime.date]:
        """The subscribed view of the index: {xid: submission date}, in the
        index's (date, id) order. Loaded on first use (queries that only
        touch the event log never pay for it); the full index is not
        retained."""
        if self._lazy_dates is None:
            print("loading index...")
            entries, _ = index.load_index(self._paths.index)
            self._lazy_dates = {
                xid: entry.date
                for xid, entry in entries.items()
                if set(entry.categories) & self._subscribed
            }
            print(f"indexed {len(self._lazy_dates)} papers "
                  f"in {len(self._subscribed)} subscribed categories")
        return self._lazy_dates

    # # #
    # Selection and metadata

    def select_papers(
        self,
        n: int,
        *,
        backwards: bool = False,
        randomise: bool = False,
        offset: int | None = None,
        cutoff: datetime.date | None = None,
        rng=random,
    ) -> list[Paper]:
        """
        Choose up to `n` unseen subscribed papers (see the module-level
        `select_papers` for the window semantics) and return them with full
        metadata, decompressing each selected month once. A selected id
        missing from the mirror (deleted upstream since the index was
        built) is silently dropped.
        """
        selected = select_papers(
            self._dates,
            set(self._seen),
            n=n,
            backwards=backwards,
            randomise=randomise,
            offset=offset,
            cutoff=cutoff,
            rng=rng,
        )
        docs = mirror.read_papers(
            self._paths.mirror, [xid for xid, _date in selected],
        )
        return [
            Paper.from_mirror_doc(docs[xid])
            for xid, _date in selected
            if xid in docs
        ]

    def get_paper(self, xid: str) -> Paper | None:
        """One paper's metadata, any category; None if not mirrored."""
        doc = mirror.read_paper(self._paths.mirror, xid)
        return Paper.from_mirror_doc(doc) if doc is not None else None

    # # #
    # Events

    def record_events(self, events: list[dict]) -> None:
        """
        Append events to the log (each stamped with the current time on
        write) and fold any view events into the in-memory seen-set.
        """
        for event in events:
            util.log_event(self._paths.scanlog, event)
            self._fold_event(event, datetime.date.today())

    def refresh_events(self) -> None:
        """
        Fold in events appended to the log by someone other than this store
        (an earlier session, or a concurrent one being watched live). Only
        the log's new bytes are read.
        """
        try:
            size = os.path.getsize(self._paths.scanlog)
        except FileNotFoundError:
            return
        if size <= self._events_offset:
            return
        with open(self._paths.scanlog, encoding="utf-8") as f:
            f.seek(self._events_offset)
            for line in f:
                if line.strip():
                    event = json.loads(line)
                    self._fold_event(
                        event,
                        datetime.date.fromisoformat(event["t"][:10]),
                    )
            self._events_offset = f.tell()

    def _fold_event(self, event: dict, date: datetime.date) -> None:
        self._events.append(event)
        if event.get("type") in ("view", "read-import"):
            self._seen.setdefault(event["xid"], date)

    # # #
    # Reading-state queries (pre-shaped for the visualisation commands)

    def submitted_dates(self) -> list[datetime.date]:
        """Submission dates of every subscribed paper, in (date, id) order."""
        return list(self._dates.values())

    def unread_dates(
        self, cutoff: datetime.date | None = None,
    ) -> list[datetime.date]:
        """Submission dates of the unseen subscribed papers."""
        return stats.select_unread_dates(
            self._dates, set(self._seen), cutoff=cutoff,
        )

    def read_dates(self) -> list[datetime.date]:
        """Each seen paper's first-seen date, in first-seen order."""
        return list(self._seen.values())

    def read_submit_dates(self) -> list[datetime.date]:
        """Submission dates of the seen papers (ids that have since left
        the subscribed view are dropped)."""
        return stats.read_submit_dates(self._seen, self._dates)

    def subscribed_ids(self) -> list[str]:
        """Every subscribed paper's id, in (date, id) order."""
        return list(self._dates)

    def read_ids(self) -> set[str]:
        """The seen-set."""
        return set(self._seen)

    def scan_events(self) -> list[dict]:
        """The full event log, in file (chronological) order."""
        return list(self._events)
