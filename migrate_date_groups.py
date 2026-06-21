"""One-off migration: rewrite arxiv.txt + readlog.txt from the flat
"<id> <YYYY-MM-DD>" form into the compact grouped "<date>:"-header form.

Safe to run anywhere a firehose data dir lives (e.g. the laptop after deploy):

    python migrate_date_groups.py [DATA_DIR]

Idempotent (re-running on an already-grouped file is a no-op) and lossless: every
(id, date) pair is preserved -- including an id the readlog saw on more than one
date. Each file is rewritten via a temp file that is reparsed and verified before
an atomic rename, so an interrupted run never corrupts the original.
"""
import collections
import os
import sys

from firehose import util


def _size(path: str) -> str:
    return f"{os.path.getsize(path) / 1e6:.2f} MB"


def migrate_arxiv(path: str) -> int:
    """Rewrite the paper cache grouped. Ids are unique here, so the {id: date}
    dict is lossless; verify the reparse matches before replacing."""
    cache, latest = util.load_cache(path)            # prefixed keys
    tmp = path + ".tmp"
    util.save_cache(tmp, latest, cache)
    check, check_latest = util.load_cache(tmp)
    assert check == cache and check_latest == latest, "arxiv.txt round-trip mismatch"
    os.replace(tmp, path)
    return len(cache)


def migrate_readlog(path: str) -> int:
    """Rewrite the seen-index grouped, preserving the full multiset of
    (id, date) pairs (an id may legitimately appear under several dates)."""
    with open(path) as f:
        pairs = [(d, xid) for xid, d in util._parse_dated_lines(f)]
    before = collections.Counter(pairs)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        util._write_grouped(f, sorted(pairs))
    with open(tmp) as f:
        after = collections.Counter((d, xid) for xid, d in util._parse_dated_lines(f))
    assert after == before, "readlog.txt pair multiset changed"
    os.replace(tmp, path)
    return len(pairs)


def main() -> None:
    data_dir = sys.argv[1] if len(sys.argv) > 1 else None
    paths = util.paths(data_dir=data_dir)
    for label, path, fn in [
        ("arxiv.txt", paths.cache, migrate_arxiv),
        ("readlog.txt", paths.readlog, migrate_readlog),
    ]:
        if not os.path.exists(path):
            print(f"{label}: not found at {path}, skipping")
            continue
        before = _size(path)
        n = fn(path)
        print(f"{label}: {n:,} entries, {before} -> {_size(path)}")


if __name__ == "__main__":
    main()
