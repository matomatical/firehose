"""
Shared builders for store-backed tests: a tiny on-disk data directory (mirror,
index, event log, config) that LocalStore and the entry points can be driven
against end to end.
"""

import datetime
import json

from firehose import index
from firehose import mirror
from firehose import util


def make_doc(
    xid: str,
    date: str = "2026-01-02",
    categories: tuple[str, ...] = ("cs.LG",),
    **fields,
) -> dict:
    """A minimal mirror document for paper `xid`, submitted on `date`."""
    doc = {
        "id": xid,
        "title": f"Title {xid}",
        "authors": "Ada Author, Bo Boauthor",
        "categories": list(categories),
        "abstract": "A summary.",
        "versions": [{
            "version": "v1",
            "date": f"{date}T00:00:00+00:00",
            "size": "1kb",
            "source_type": "D",
        }],
        "oai_datestamp": date,
    }
    doc.update(fields)
    return doc


def make_data_dir(data_dir, docs: list[dict], events: list[dict] = ()) -> None:
    """
    Populate `data_dir` (a pathlib tmp dir) as a firehose data directory:
    each doc goes into the mirror and the index, each event (complete with
    its "t" timestamp) onto the event log.
    """
    entries = {}
    updater = mirror.Updater(str(data_dir / "metadata"))
    for doc in docs:
        updater.upsert(doc)
        entries[doc["id"]] = index.Entry(
            date=datetime.date.fromisoformat(doc["oai_datestamp"]),
            categories=tuple(doc["categories"]),
        )
    updater.flush()
    if entries:
        index.save_index(
            path=str(data_dir / "index.txt"),
            watermark=max(entry.date for entry in entries.values()),
            entries=entries,
        )
    if events:
        with open(data_dir / "events.jsonl", "w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")


def make_store(data_dir, subscribed: set[str] = frozenset({"cs.LG"})):
    """A LocalStore over `data_dir` (populate it with make_data_dir first)."""
    from firehose.store import LocalStore

    config = {"paths": {"data": str(data_dir)}}
    paths = util.data_paths(config)
    return LocalStore(paths, subscribed=set(subscribed))
