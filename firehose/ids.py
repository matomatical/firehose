"""
Namespaced document ids: "<source>:<source-local id>", e.g.
"arxiv:2601.00001".

Wherever documents from different sources can mix — the event log, the
in-memory reading state, everything a store serves — an id carries its
source as a prefix. Per-source files (a source's mirror shards and its
index) store the source-local id alone, since their location already
names the source; the prefix is attached at load time and stripped again
at the source's own boundary (upstream URLs, downloads).

Source names contain no ":"; source-local ids may (the first ":" splits).
"""


def join(source: str, local_id: str) -> str:
    """The namespaced id for `local_id` from `source`."""
    return f"{source}:{local_id}"


def split(namespaced_id: str) -> tuple[str, str]:
    """A namespaced id's (source, source-local id) parts."""
    source, _, local_id = namespaced_id.partition(":")
    if not local_id:
        raise ValueError(f"id has no source prefix: {namespaced_id!r}")
    return source, local_id


def source(namespaced_id: str) -> str:
    """The source name of a namespaced id."""
    return split(namespaced_id)[0]


def local(namespaced_id: str) -> str:
    """The source-local part of a namespaced id."""
    return split(namespaced_id)[1]
