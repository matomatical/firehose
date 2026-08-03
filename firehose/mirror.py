"""
The paper-metadata mirror: one gzipped JSON-lines archive per submission
month, on disk.

Layout: `<mirror_dir>/<YYMM>.jsonl.gz`, one line per paper, sorted by id.
YYMM comes from the id itself: the part before the dot for modern ids
("2003.14184" -> 2003), the first four digits after the slash for pre-2007
ids ("math/0211159" -> 0211). One line per paper keeps the archives
line-tool friendly (`zgrep <id> <month>.jsonl.gz` returns a whole document;
`zcat | jq` pretty-prints), and the fixed serialisation (key order pinned
by the parser, sorted ids, zeroed gzip timestamp) makes an archive's bytes
a pure function of its documents, so unchanged months are byte-identical
across rewrites.

Whole months are the unit of I/O: reading one paper decompresses its month
(~a quarter second for the largest), and writing goes through an `Updater`
that buffers upserts and deletions in memory and rewrites each touched
month atomically (write a sibling temp file, then rename over) on flush.
Files are deliberately not fsynced — a crash can lose the most recent
flush, but the harvest watermark only advances when the index is saved
after it, so the next run re-fetches and re-applies anything lost.
"""

import gzip
import json
import os
import tempfile
from collections.abc import Iterator


def shard(xid: str) -> str:
    """The YYMM month name for a paper id."""
    if "/" in xid:
        return xid.split("/", 1)[1][:4]
    return xid.split(".", 1)[0]


def month_path(mirror_dir: str, yymm: str) -> str:
    return os.path.join(mirror_dir, yymm + ".jsonl.gz")


def dumps_doc(doc: dict) -> str:
    """Serialise a document exactly as it is stored (one line). Deterministic
    given the document's key order (fixed by the parser), so byte equality
    of serialisations means document equality."""
    return json.dumps(doc, ensure_ascii=False) + "\n"


def load_month(mirror_dir: str, yymm: str) -> dict[str, dict]:
    """Load one month's papers as {id: doc}; {} if the month has none."""
    try:
        f = gzip.open(month_path(mirror_dir, yymm), "rt", encoding="utf-8")
    except FileNotFoundError:
        return {}
    with f:
        docs = {}
        for line in f:
            if line.strip():
                doc = json.loads(line)
                docs[doc["id"]] = doc
        return docs


def save_month(mirror_dir: str, yymm: str, docs: dict[str, dict]) -> None:
    """
    Write one month's archive: every doc on its own line, sorted by id,
    with a zeroed gzip timestamp (so equal documents give equal bytes).
    The temp-write-and-rename replace is atomic; an empty `docs` removes
    the archive instead.
    """
    path = month_path(mirror_dir, yymm)
    if not docs:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(mirror_dir, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=mirror_dir,
            prefix=f".firehose-{yymm}-",
            suffix=".tmp",
            delete=False,
        ) as raw:
            temp_path = raw.name
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as f:
                for xid in sorted(docs):
                    f.write(dumps_doc(docs[xid]).encode("utf-8"))
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass


def read_paper(mirror_dir: str, xid: str) -> dict | None:
    """Load one paper's document, or None if it is not in the mirror."""
    return load_month(mirror_dir, shard(xid)).get(xid)


def read_papers(mirror_dir: str, xids: list[str]) -> dict[str, dict]:
    """
    Load many papers' documents as {id: doc}, decompressing each needed
    month only once. Ids not in the mirror are silently absent from the
    result.
    """
    by_month: dict[str, list[str]] = {}
    for xid in xids:
        by_month.setdefault(shard(xid), []).append(xid)
    found = {}
    for yymm, month_xids in by_month.items():
        docs = load_month(mirror_dir, yymm)
        for xid in month_xids:
            if xid in docs:
                found[xid] = docs[xid]
    return found


def months(mirror_dir: str) -> list[str]:
    """Every month with an archive, sorted."""
    try:
        filenames = os.listdir(mirror_dir)
    except FileNotFoundError:
        return []
    return sorted(
        name.removesuffix(".jsonl.gz")
        for name in filenames
        if name.endswith(".jsonl.gz")
    )


def iter_papers(mirror_dir: str) -> Iterator[dict]:
    """Yield every document in the mirror, in (month, id) order."""
    for yymm in months(mirror_dir):
        docs = load_month(mirror_dir, yymm)
        for xid in sorted(docs):
            yield docs[xid]


class Updater:
    """
    Buffered writes to the mirror: upserts and deletions accumulate in
    memory (loading each touched month on first touch) and land on disk
    when `flush` rewrites the dirty months. Call `flush` before saving
    the index that describes the writes, and expect memory to hold every
    month touched since the last flush.
    """

    def __init__(self, mirror_dir: str):
        self._mirror_dir = mirror_dir
        self._months: dict[str, dict[str, dict]] = {}
        self._dirty: set[str] = set()

    def _month(self, yymm: str) -> dict[str, dict]:
        if yymm not in self._months:
            self._months[yymm] = load_month(self._mirror_dir, yymm)
        return self._months[yymm]

    def upsert(self, doc: dict) -> str:
        """
        Add or replace one paper's document. Returns "new" (id not in its
        month), "updated" (replaced a different document), or "unchanged"
        (identical document; nothing to write).
        """
        xid = doc["id"]
        docs = self._month(shard(xid))
        existing = docs.get(xid)
        if existing == doc:
            return "unchanged"
        docs[xid] = doc
        self._dirty.add(shard(xid))
        return "updated" if existing is not None else "new"

    def delete(self, xid: str) -> bool:
        """Remove one paper (the record was deleted upstream). Returns
        whether it was present."""
        docs = self._month(shard(xid))
        if xid not in docs:
            return False
        del docs[xid]
        self._dirty.add(shard(xid))
        return True

    def flush(self) -> None:
        """Rewrite every dirty month's archive, then drop the in-memory
        buffer (so long runs don't accumulate the whole mirror)."""
        for yymm in sorted(self._dirty):
            save_month(self._mirror_dir, yymm, self._months[yymm])
        self._months.clear()
        self._dirty.clear()
