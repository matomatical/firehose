"""
The paper-metadata mirror: one JSON document per arXiv paper, on disk.

Layout: `<mirror_dir>/<YYMM>/<id>.json`, sharded by submission month so no
directory grows past a few tens of thousands of entries. YYMM comes from the
id itself: the part before the dot for modern ids ("2003.14184" -> 2003/),
the first four digits after the slash for pre-2007 ids ("math/0211159" ->
0211/). Slashes in old ids are replaced by underscores in filenames
("math/0211159" -> math_0211159.json).

Writes are atomic (write a sibling temp file, then rename over) and
idempotent: writing an identical document reports "unchanged" and leaves the
file untouched. Files are deliberately not fsynced — a crash can lose the
most recent writes, but the harvest watermark only advances when the index is
saved, so the next run re-fetches and re-writes anything lost.
"""

import json
import os
import tempfile
from collections.abc import Iterator


def shard(xid: str) -> str:
    """The YYMM shard directory name for a paper id."""
    if "/" in xid:
        return xid.split("/", 1)[1][:4]
    return xid.split(".", 1)[0]


def paper_path(mirror_dir: str, xid: str) -> str:
    return os.path.join(mirror_dir, shard(xid), xid.replace("/", "_") + ".json")


def dumps_doc(doc: dict) -> str:
    """Serialise a document exactly as it is stored on disk. Deterministic
    given the document's key order (fixed by the parser), so byte equality
    of serialisations means document equality."""
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def write_paper(mirror_dir: str, doc: dict) -> str:
    """
    Upsert one paper's document. Returns "new" (no file existed), "updated"
    (file replaced with different content), or "unchanged" (identical
    content; nothing written).
    """
    path = paper_path(mirror_dir, doc["id"])
    blob = dumps_doc(doc).encode("utf-8")
    try:
        with open(path, "rb") as f:
            if f.read() == blob:
                return "unchanged"
        existed = True
    except FileNotFoundError:
        existed = False

    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=parent,
            prefix=".firehose-paper-",
            suffix=".tmp",
            delete=False,
        ) as f:
            temp_path = f.name
            f.write(blob)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass
    return "updated" if existed else "new"


def read_paper(mirror_dir: str, xid: str) -> dict | None:
    """Load one paper's document, or None if it is not in the mirror."""
    try:
        with open(paper_path(mirror_dir, xid), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def delete_paper(mirror_dir: str, xid: str) -> bool:
    """Remove one paper (the record was deleted upstream). Returns whether
    it was present."""
    try:
        os.remove(paper_path(mirror_dir, xid))
        return True
    except FileNotFoundError:
        return False


def iter_papers(mirror_dir: str) -> Iterator[dict]:
    """Yield every document in the mirror, in sorted path order."""
    for path in _paper_paths(mirror_dir):
        with open(path, encoding="utf-8") as f:
            yield json.load(f)


def count_papers(mirror_dir: str) -> int:
    """The number of documents in the mirror (a directory walk, no parsing)."""
    return sum(1 for _ in _paper_paths(mirror_dir))


def _paper_paths(mirror_dir: str) -> Iterator[str]:
    for shard_name in sorted(os.listdir(mirror_dir)):
        shard_dir = os.path.join(mirror_dir, shard_name)
        if not os.path.isdir(shard_dir):
            continue
        for filename in sorted(os.listdir(shard_dir)):
            if filename.endswith(".json"):
                yield os.path.join(shard_dir, filename)
