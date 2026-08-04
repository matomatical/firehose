"""
The storage boundary: every read and write of firehose's data files goes
through a Store, so the commands above it (scanning, visualisation) never
touch the files directly.

A Store answers a small set of queries — select papers to scan (with full
metadata), fetch one paper, record scan events, report a status snapshot,
and serve the pre-shaped reading-state data the visualisation commands
consume — and hides where the answers come from. There are two implementations, chosen by `make_store`
from the config: `LocalStore` answers from the data directory in-process —
the mirror (per-paper metadata), the index (id, submission date, categories
per paper, loaded into memory), and the event log (the append-only record
of scanning, from which the seen-set is derived) — and `RemoteStore`
answers over HTTP from a firehose server that runs a LocalStore on its own
machine's data.

Subscription is a query-time filter: each source's index covers everything
the source mirrors, and a LocalStore is constructed with the config's
[sources] sections, whose adapters decide which index entries the
selection and reading-state queries range over (`get_paper` is
deliberately unrestricted). Each section may also set a `modern_cutoff`
date: papers dated on or before it are dropped from selection unless a
query asks for the full backlog (`modern=False`). In remote mode the
server's subscriptions and cutoffs apply.
"""

import contextlib
import datetime
import json
import os
import queue
import random
import threading
import time

import httpx

from firehose import ids
from firehose import index
from firehose import mirror
from firehose import sources
from firehose import stats
from firehose import util
from firehose.paper import Paper


def make_store(config: dict, data_dir: str | None = None):
    """
    The store the config asks for: a RemoteStore on the [server] section's
    `url` when one is set, else a LocalStore on the local data directory.
    An explicit `data_dir` override always means the local files at that
    path.
    """
    url = config.get("server", {}).get("url")
    if url and data_dir is None:
        return RemoteStore(url, spool_path=util.data_paths(config).unsent)
    paths = util.data_paths(config, data_dir=data_dir)
    return LocalStore(paths, sources_config=config["sources"])


def filter_unread(
    cache: dict[str, datetime.date],
    read: set[str],
    *,
    cutoffs: dict[str, datetime.date] | None = None,
    source: str | None = None,
) -> list[tuple[str, datetime.date]]:
    """
    The unread candidates from the cache, in cache order: (id, date) pairs
    whose id is not in `read`, narrowed to one source when `source` is
    given, and (when `cutoffs` is given) dropping each paper dated on or
    before its own source's cutoff — sources absent from the mapping keep
    everything. Pure.
    """
    unread = [
        (paper_id, date)
        for paper_id, date in cache.items()
        if paper_id not in read
    ]
    if source is not None:
        unread = [
            (paper_id, date) for paper_id, date in unread
            if ids.source(paper_id) == source
        ]
    if cutoffs:
        unread = [
            (paper_id, date) for paper_id, date in unread
            if (cutoff := cutoffs.get(ids.source(paper_id))) is None
            or date > cutoff
        ]
    return unread


def select_papers(
    unread: list[tuple[str, datetime.date]],
    *,
    n: int,
    backwards: bool = False,
    randomise: bool = False,
    offset: int | None = None,
    rng=random,
) -> list[tuple[str, datetime.date]]:
    """
    Choose which (id, date) papers to scan from the unread candidates
    (see filter_unread): a window of size `n`:

      * default:        the last `n` candidates, reversed (newest first);
      * backwards=True:  the first `n` candidates, in order (oldest first);
      * randomise=True:  up to `n` candidates drawn at random via `rng`.

    `offset`, when given, first narrows to the last `offset` candidates (paging
    back through older unread papers); `n <= 0` selects nothing. Pure: no I/O,
    clock, or global RNG — pass a seeded `rng` for deterministic sampling in
    tests.
    """
    if n <= 0:
        return []
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

    Construction takes the config's [sources] sections and loads the event
    log; the per-source indexes (each narrowed to its adapter's
    subscription) load on first use. Queries then run against these
    in-memory structures, and `record_events` keeps them in step as it
    appends to the log on disk.
    """

    def __init__(self, paths, sources_config: dict[str, dict]):
        self._paths = paths
        self._sources = {
            name: (sources.adapter(name), section)
            for name, section in sources_config.items()
        }
        self._cutoffs = {
            name: section["modern_cutoff"]
            for name, (_adapter, section) in self._sources.items()
            if "modern_cutoff" in section
        }
        self._lazy_dates: dict[str, datetime.date] | None = None
        self._events: list[dict] = []
        self._seen: dict[str, datetime.date] = {}   # id -> first-seen date
        self._events_offset = 0                     # bytes of the log consumed
        self.refresh_events()
        print(f"loaded {len(self._events)} events "
              f"({len(self._seen)} papers seen)")

    @property
    def _dates(self) -> dict[str, datetime.date]:
        """The subscribed view of the indexes: {namespaced id: submission
        date}, sorted by (date, id) across sources. Loaded on first use
        (queries that only touch the event log never pay for it); the full
        indexes are not retained. A configured source with no index yet
        (never harvested) contributes nothing, with a notice."""
        if self._lazy_dates is None:
            print("loading indexes...")
            merged = []
            for name, (adapter, section) in self._sources.items():
                try:
                    entries, _ = index.load_index(self._paths.index(name))
                except FileNotFoundError:
                    print(f"* {name}: no index yet (run `firehose mirror`)")
                    continue
                subscribed = adapter.subscription(section)
                before = len(merged)
                merged.extend(
                    (ids.join(name, xid), entry.date)
                    for xid, entry in entries.items()
                    if subscribed(entry)
                )
                print(f"* {name}: subscribed to {len(merged) - before} "
                      f"of {len(entries)} papers")
            merged.sort(key=lambda pair: (pair[1], pair[0]))
            self._lazy_dates = dict(merged)
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
        modern: bool = True,
        source: str | None = None,
        rng=random,
    ) -> list[Paper]:
        """
        Choose up to `n` unseen subscribed papers (see the module-level
        `select_papers` for the window semantics; `modern` applies each
        source's cutoff, `source` narrows to one source) and return them
        with full metadata, decompressing each selected shard once. A
        selected id missing from its mirror (deleted upstream since the
        index was built) is silently dropped.
        """
        selected = select_papers(
            filter_unread(
                self._dates,
                set(self._seen),
                cutoffs=self._cutoffs if modern else None,
                source=source,
            ),
            n=n,
            backwards=backwards,
            randomise=randomise,
            offset=offset,
            rng=rng,
        )
        by_source: dict[str, list[str]] = {}
        for paper_id, _date in selected:
            name, local_id = ids.split(paper_id)
            by_source.setdefault(name, []).append(local_id)
        papers = {}
        for name, local_ids in by_source.items():
            adapter, _section = self._sources[name]
            docs = mirror.read_papers(
                self._paths.mirror(name), local_ids, shard_fn=adapter.shard,
            )
            for local_id, doc in docs.items():
                papers[ids.join(name, local_id)] = adapter.to_paper(doc)
        return [
            papers[paper_id]
            for paper_id, _date in selected
            if paper_id in papers
        ]

    def get_paper(self, paper_id: str) -> Paper | None:
        """One paper's metadata by namespaced id, any category; None if
        not mirrored."""
        source_name, local_id = ids.split(paper_id)
        adapter = sources.adapter(source_name)
        doc = mirror.read_paper(
            self._paths.mirror(source_name), local_id, shard_fn=adapter.shard,
        )
        return adapter.to_paper(doc) if doc is not None else None

    # # #
    # Events

    def record_events(self, events: list[dict]) -> None:
        """
        Append events to the log and fold any view events into the
        in-memory seen-set. An event arriving with a "t" timestamp (e.g.
        stamped by a remote client) keeps it; bare events are stamped with
        the current time.
        """
        for event in events:
            if "t" not in event:
                event = {"t": datetime.datetime.now().isoformat(), **event}
            # advance the tail offset past our own append: it is already
            # folded, so refresh_events must not read it back as news
            self._events_offset = util.log_event(self._paths.events, event)
            self._fold_event(
                event, datetime.date.fromisoformat(event["t"][:10]),
            )

    def refresh_events(self) -> None:
        """
        Fold in events appended to the log by someone other than this store
        (an earlier session, or a concurrent one being watched live). Only
        the log's new bytes are read.
        """
        try:
            size = os.path.getsize(self._paths.events)
        except FileNotFoundError:
            return
        if size <= self._events_offset:
            return
        with open(self._paths.events, encoding="utf-8") as f:
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
            self._seen.setdefault(event["id"], date)

    def close(self) -> None:
        """Nothing to settle: record_events writes synchronously."""
        pass

    # # #
    # Status

    def status(self) -> dict:
        """
        A JSON-clean snapshot of this store's data: the most recent harvest
        records (the tail of the harvest log the `mirror` command appends
        to), each source's index watermark (None for a source not yet
        harvested) and the subscribed paper count, and the event log's
        size, seen-count, and last event. The logs are (re)read at call
        time, so a long-running process reports harvests and events that
        landed after it started.
        """
        self.refresh_events()
        watermarks = {}
        for name in self._sources:
            try:
                watermarks[name] = index.load_watermark(
                    self._paths.index(name),
                ).isoformat()
            except FileNotFoundError:
                watermarks[name] = None
        return {
            "data_dir": self._paths.data_dir,
            "watermarks": watermarks,
            "subscribed_papers": len(self._dates),
            "seen_papers": len(self._seen),
            "events": len(self._events),
            "last_event": self._events[-1] if self._events else None,
            "harvests": util.load_events(self._paths.harvests)[-10:],
        }

    # # #
    # Reading-state queries (pre-shaped for the visualisation commands)

    def submitted_dates(self) -> list[datetime.date]:
        """Submission dates of every subscribed paper, in (date, id) order."""
        return list(self._dates.values())

    def unread_dates(
        self, modern: bool = True, source: str | None = None,
    ) -> list[datetime.date]:
        """Submission dates of the unseen subscribed papers (`modern`
        applies each source's cutoff, `source` narrows to one source)."""
        return [
            date for _paper_id, date in filter_unread(
                self._dates,
                set(self._seen),
                cutoffs=self._cutoffs if modern else None,
                source=source,
            )
        ]

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


# Pauses before each successive attempt to post an event batch: the first
# try is immediate, and a batch that exhausts every attempt is spooled.
EVENT_RETRY_WAITS = (0.0, 1.0, 5.0)

# How long `close` waits for the background sender to drain the event queue
# before giving up and spooling what remains.
EVENT_CLOSE_TIMEOUT = 60.0

# Queued behind the last event by `close` to tell the sender to finish up.
_CLOSE_SENTINEL = object()


class RemoteStore:
    """
    A Store over HTTP: a thin client of a firehose server (`firehose
    serve`), one request per query. Queries are always as fresh as the
    server, so `refresh_events` has nothing to do.

    Event posting is asynchronous: `record_events` stamps timestamps with
    this machine's clock and queues the events for a background sender
    thread, so the caller (the scanning TUI) never waits on the network.
    Call `close` when recording is finished — it drains the queue, and
    events that could not be delivered (server down, retries exhausted)
    are appended to the spool file at `spool_path` for later replay
    rather than lost.
    """

    def __init__(
        self,
        url: str,
        client: httpx.Client | None = None,
        *,
        spool_path: str = "unsent-events.jsonl",
        retry_waits: tuple[float, ...] = EVENT_RETRY_WAITS,
    ):
        self._url = url
        self._client = client or httpx.Client(
            base_url=url, timeout=30.0, follow_redirects=True,
        )
        self._spool_path = spool_path
        self._retry_waits = retry_waits
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._sender: threading.Thread | None = None
        self._failed: list[dict] = []   # batches whose retries ran out

    def _get(self, path: str, **params):
        with _notice_if_slow(f"waiting on the server ({self._url}{path})..."):
            response = self._client.get(
                path,
                params={
                    key: value for key, value in params.items()
                    if value is not None
                },
            )
        response.raise_for_status()
        return response.json()

    # # #
    # Selection and metadata

    def select_papers(
        self,
        n: int,
        *,
        backwards: bool = False,
        randomise: bool = False,
        offset: int | None = None,
        modern: bool = True,
        source: str | None = None,
        rng=random,
    ) -> list[Paper]:
        """
        Choose up to `n` unseen subscribed papers with full metadata; the
        selection itself runs on the server, under the server's
        subscriptions and cutoffs (`rng` only draws the seed a randomised
        order is requested under).
        """
        docs = self._get(
            "/papers",
            n=n,
            backwards=backwards,
            randomise=randomise,
            seed=rng.getrandbits(63) if randomise else None,
            offset=offset,
            modern=modern,
            source=source,
        )
        return [_doc_to_paper(doc) for doc in docs]

    def get_paper(self, paper_id: str) -> Paper | None:
        """One paper's metadata by namespaced id, any category; None if
        not mirrored."""
        with _notice_if_slow(
            f"waiting on the server ({self._url}/papers/{paper_id})..."
        ):
            response = self._client.get(f"/papers/{paper_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return _doc_to_paper(response.json())

    # # #
    # Events

    def record_events(self, events: list[dict]) -> None:
        """
        Stamp events with this machine's clock (the moment of the action,
        which the dwell analytics depend on) and queue them for the
        background sender; returns immediately. Delivery is settled by
        `close`.
        """
        now = datetime.datetime.now().isoformat()
        for event in events:
            if "t" not in event:
                event = {"t": now, **event}
            self._queue.put(event)
        if self._sender is None:
            self._sender = threading.Thread(target=self._send_loop, daemon=True)
            self._sender.start()

    def _send_loop(self) -> None:
        """
        Deliver queued events until the close sentinel arrives. Each pass
        posts everything currently queued as one batch, so events that
        accumulate while a request is in flight coalesce into a single
        request (and the one-at-a-time passes preserve event order). A
        batch whose retries run out is parked for `close` to spool, and
        delivery continues with the next batch.
        """
        while True:
            item = self._queue.get()
            closing = item is _CLOSE_SENTINEL
            batch = [] if closing else [item]
            while not closing:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is _CLOSE_SENTINEL:
                    closing = True
                else:
                    batch.append(item)
            if batch:
                self._post_events(batch)
            if closing:
                return

    def _post_events(self, batch: list[dict]) -> None:
        """Post one batch, retrying on any HTTP-layer failure; a batch that
        exhausts every attempt lands in the failed list instead."""
        for wait in self._retry_waits:
            time.sleep(wait)
            try:
                response = self._client.post("/events", json=batch)
                response.raise_for_status()
                return
            except httpx.HTTPError:
                continue
        self._failed.extend(batch)

    def close(self) -> None:
        """
        Settle event delivery: wait (bounded) for the sender to drain the
        queue, then append anything undelivered to the spool file, with a
        printed notice. Safe to call when nothing was recorded, and a
        later record_events starts a fresh sender.
        """
        if self._sender is None:
            return
        self._queue.put(_CLOSE_SENTINEL)
        with _notice_if_slow("delivering remaining events to the server..."):
            self._sender.join(timeout=EVENT_CLOSE_TIMEOUT)
        if self._sender.is_alive():
            print(
                "warning: gave up waiting on the server; "
                "an in-flight batch of events may be lost"
            )
        self._sender = None
        # spool the given-up batches, plus anything the sender never took
        # (it timed out above, or died on an unexpected error)
        undelivered = self._failed
        self._failed = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not _CLOSE_SENTINEL:
                undelivered.append(item)
        if undelivered:
            for event in undelivered:
                util.log_event(self._spool_path, event)
            print(
                f"warning: {len(undelivered)} events were not delivered to "
                f"the server; saved to {self._spool_path} for later replay"
            )

    def refresh_events(self) -> None:
        pass

    # # #
    # Status

    def status(self) -> dict:
        """The server's status snapshot, plus the URL it answered on."""
        return {"url": self._url, **self._get("/status")}

    # # #
    # Reading-state queries (pre-shaped for the visualisation commands)

    def submitted_dates(self) -> list[datetime.date]:
        return _dates(self._get("/stats/submitted-dates"))

    def unread_dates(
        self, modern: bool = True, source: str | None = None,
    ) -> list[datetime.date]:
        return _dates(self._get(
            "/stats/unread-dates", modern=modern, source=source,
        ))

    def read_dates(self) -> list[datetime.date]:
        return _dates(self._get("/stats/read-dates"))

    def read_submit_dates(self) -> list[datetime.date]:
        return _dates(self._get("/stats/read-submit-dates"))

    def subscribed_ids(self) -> list[str]:
        return self._get("/stats/subscribed-ids")

    def read_ids(self) -> set[str]:
        return set(self._get("/stats/read-ids"))

    def scan_events(self) -> list[dict]:
        return self._get("/stats/scan-events")


def _doc_to_paper(doc: dict) -> Paper:
    """A Paper from a document that travelled over the wire: the doc's
    "source" field names the adapter that owns it (absent on arXiv
    documents, which predate the field)."""
    return sources.adapter(doc.get("source", "arxiv")).to_paper(doc)


def _dates(datestamps: list[str]) -> list[datetime.date]:
    return [datetime.date.fromisoformat(d) for d in datestamps]


@contextlib.contextmanager
def _notice_if_slow(message: str, delay: float = 1.0):
    """
    Print `message` if the wrapped block is still running after `delay`
    seconds — so a slow or hung server is visible rather than a silent
    stall. A block that finishes promptly prints nothing. Applied where a
    request blocks a caller who owns the terminal (the read queries, the
    final drain in `close`) — never on the background event sender, which
    must stay silent while the scanning TUI owns the screen.
    """
    timer = threading.Timer(delay, print, args=(message,))
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
