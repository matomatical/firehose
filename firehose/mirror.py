"""
A document-metadata mirror: gzipped JSON-lines archives on disk, one per
shard.

Layout: `<mirror_dir>/<shard>.jsonl.gz`, one line per document, sorted by
id. Which shard holds which id is the caller's rule — each source adapter
names a shard for each of its ids (a pure function of the id and its
index-entry date, at a granularity to suit the source's volume) — so this
module never computes shards itself: writers pass the shard with each
upsert or deletion, and readers pass a `shard_fn` resolving each id they
ask for.
One line per document keeps the archives line-tool friendly
(`zgrep <id> <shard>.jsonl.gz` returns a whole document; `zcat | jq`
pretty-prints), and the fixed serialisation (key order pinned by the
parser, sorted ids, zeroed gzip timestamp) makes an archive's bytes a pure
function of its documents, so unchanged shards are byte-identical across
rewrites.

Whole shards are the unit of I/O: reading one document decompresses its
shard (~a quarter second for the largest), and writing goes through an
`Updater` that buffers upserts and deletions in memory and rewrites each
touched shard atomically (write a sibling temp file, then rename over) on
flush. Files are deliberately not fsynced — a crash can lose the most
recent flush, but the harvest watermark only advances when the index is
saved after it, so the next run re-fetches and re-applies anything lost.
"""

import gzip
import json
import os
import tempfile
from collections.abc import Callable, Iterator


def shard_path(mirror_dir: str, shard: str) -> str:
    return os.path.join(mirror_dir, shard + ".jsonl.gz")


def dumps_doc(doc: dict) -> str:
    """Serialise a document exactly as it is stored (one line). Deterministic
    given the document's key order (fixed by the parser), so byte equality
    of serialisations means document equality."""
    return json.dumps(doc, ensure_ascii=False) + "\n"


def load_shard(mirror_dir: str, shard: str) -> dict[str, dict]:
    """Load one shard's documents as {id: doc}; {} if the shard has none."""
    try:
        f = gzip.open(shard_path(mirror_dir, shard), "rt", encoding="utf-8")
    except FileNotFoundError:
        return {}
    with f:
        docs = {}
        for line in f:
            if line.strip():
                doc = json.loads(line)
                docs[doc["id"]] = doc
        return docs


def save_shard(mirror_dir: str, shard: str, docs: dict[str, dict]) -> None:
    """
    Write one shard's archive: every doc on its own line, sorted by id,
    with a zeroed gzip timestamp (so equal documents give equal bytes).
    The temp-write-and-rename replace is atomic; an empty `docs` removes
    the archive instead.
    """
    path = shard_path(mirror_dir, shard)
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
            prefix=f".firehose-{shard}-",
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


def read_paper(mirror_dir: str, xid: str, shard: str) -> dict | None:
    """Load one document from its shard, or None if it is not there."""
    return load_shard(mirror_dir, shard).get(xid)


def read_papers(
    mirror_dir: str,
    xids: list[str],
    shard_fn: Callable[[str], str],
) -> dict[str, dict]:
    """
    Load many documents as {id: doc}, decompressing each needed shard
    only once. Ids not in the mirror are silently absent from the result.
    """
    by_shard: dict[str, list[str]] = {}
    for xid in xids:
        by_shard.setdefault(shard_fn(xid), []).append(xid)
    found = {}
    for shard, shard_xids in by_shard.items():
        docs = load_shard(mirror_dir, shard)
        for xid in shard_xids:
            if xid in docs:
                found[xid] = docs[xid]
    return found


def shards(mirror_dir: str) -> list[str]:
    """Every shard with an archive, sorted."""
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
    """Yield every document in the mirror, in (shard, id) order."""
    for shard in shards(mirror_dir):
        docs = load_shard(mirror_dir, shard)
        for xid in sorted(docs):
            yield docs[xid]


class Updater:
    """
    Buffered writes to the mirror: upserts and deletions accumulate in
    memory (loading each touched shard on first touch) and land on disk
    when `flush` rewrites the dirty shards. Call `flush` before saving
    the index that describes the writes, and expect memory to hold every
    shard touched since the last flush.
    """

    def __init__(self, mirror_dir: str):
        self._mirror_dir = mirror_dir
        self._shards: dict[str, dict[str, dict]] = {}
        self._dirty: set[str] = set()

    def _shard(self, shard: str) -> dict[str, dict]:
        if shard not in self._shards:
            self._shards[shard] = load_shard(self._mirror_dir, shard)
        return self._shards[shard]

    def upsert(self, doc: dict, shard: str) -> str:
        """
        Add or replace one document in `shard`. Returns "new" (id not in
        the shard), "updated" (replaced a different document), or
        "unchanged" (identical document; nothing to write).
        """
        xid = doc["id"]
        docs = self._shard(shard)
        existing = docs.get(xid)
        if existing == doc:
            return "unchanged"
        docs[xid] = doc
        self._dirty.add(shard)
        return "updated" if existing is not None else "new"

    def delete(self, xid: str, shard: str) -> bool:
        """Remove one document from `shard` (the record was deleted
        upstream, or its date moved it to a different shard). Returns
        whether it was present."""
        docs = self._shard(shard)
        if xid not in docs:
            return False
        del docs[xid]
        self._dirty.add(shard)
        return True

    def flush(self) -> None:
        """Rewrite every dirty shard's archive, then drop the in-memory
        buffer (so long runs don't accumulate the whole mirror)."""
        for shard in sorted(self._dirty):
            save_shard(self._mirror_dir, shard, self._shards[shard])
        self._shards.clear()
        self._dirty.clear()
