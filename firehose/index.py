"""
The paper index: every mirrored paper's id, submission date, and categories,
in one plain-text file, loaded into memory for querying.

The index is derived data: `rebuild_index` regenerates it from the mirror by
a full scan, so it can always be reconstructed after a crash or by-hand
surgery on the mirror. Format (an evolution of the grouped date format):

    latest datestamp: 2026-08-01     <- watermark: newest oai_datestamp seen
    2007-05-23:                      <- date header: submission date of the
    0705.1234 cs.AI cs.LG               ids below; each line is an id then
    math/0703999 math.DG                its categories, primary first
    2007-05-24:
    ...

Entries are sorted by (date, id) and grouped under one header per date, so
files stay compact, greppable, and diffable.
"""

import datetime
import os
import tempfile
from typing import NamedTuple

import tqdm

from firehose import mirror
from firehose import util


class Entry(NamedTuple):
    date: datetime.date
    categories: tuple[str, ...]


def _parse_watermark(line: str) -> datetime.date:
    """The watermark date from the index's first line."""
    return datetime.date.fromisoformat(line.strip().split(": ")[-1])


def load_watermark(path: str) -> datetime.date:
    """Read just the watermark from the index, without loading the entries."""
    with open(path, encoding="utf-8") as f:
        return _parse_watermark(next(f))


def load_index(path: str) -> tuple[dict[str, Entry], datetime.date]:
    """Load the {id: Entry} index plus the watermark from the first line."""
    with open(path, encoding="utf-8") as f:
        watermark = _parse_watermark(next(f))
        lines = f.read().splitlines()
    entries = {}
    current_date = None
    for line in tqdm.tqdm(lines, ncols=80, disable=None):
        if line.endswith(":"):
            current_date = datetime.date.fromisoformat(line[:-1])
        else:
            xid, *categories = line.split()
            entries[xid] = Entry(date=current_date, categories=tuple(categories))
    return entries, watermark


def save_index(
    path: str,
    watermark: datetime.date,
    entries: dict[str, Entry],
) -> None:
    """
    Write the index to disk: watermark line, then entries sorted by
    (date, id) under grouped date headers. Written to a sibling temporary
    file and atomically renamed over the previous index, which survives
    intact if serialisation is interrupted.
    """
    ordered = sorted(
        entries.items(), key=lambda item: (item[1].date, item[0])
    )
    parent = os.path.dirname(os.path.abspath(path))
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=".firehose-index-",
            suffix=".tmp",
            delete=False,
        ) as f:
            temp_path = f.name
            f.write(f"latest datestamp: {watermark.isoformat()}\n")
            current_date = None
            for xid, entry in tqdm.tqdm(ordered, ncols=80, disable=None):
                if entry.date != current_date:
                    f.write(f"{entry.date.isoformat()}:\n")
                    current_date = entry.date
                f.write(" ".join((xid, *entry.categories)) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass


def rebuild_index(
    config_path: str = util.CONFIG_PATH,
    data_dir: str | None = None,
):
    """
    Regenerate the index from the metadata mirror by a full scan.
    """
    from firehose import arxivraw

    config = util.load_config(config_path)
    paths = util.data_paths(config, data_dir=data_dir)
    mirror_dir = paths.mirror("arxiv")
    index_path = paths.index("arxiv")
    if not os.path.isdir(mirror_dir):
        raise SystemExit(f"no mirror at {mirror_dir}; run `firehose mirror`")

    print("rebuilding index from the mirror...")
    entries = {}
    watermark = None
    documents = mirror.iter_papers(mirror_dir)
    for doc in tqdm.tqdm(documents, ncols=80, disable=None):
        entries[doc["id"]] = Entry(
            date=arxivraw.submitted_date(doc),
            categories=tuple(doc.get("categories", ())),
        )
        datestamp = datetime.date.fromisoformat(doc["oai_datestamp"])
        if watermark is None or datestamp > watermark:
            watermark = datestamp
    if watermark is None:
        raise SystemExit(f"mirror at {mirror_dir} is empty")

    print("saving index...")
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    save_index(path=index_path, watermark=watermark, entries=entries)
    print(f"saved {len(entries)} entries; watermark {watermark}")
