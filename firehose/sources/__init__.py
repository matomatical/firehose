"""
Source adapters: the per-source code behind the source-generic machinery.

A source is a feed of documents (arXiv is the first). Its adapter owns
everything specific to that source — how to talk to the upstream API, how
its documents are normalised, sharded, indexed, and displayed — behind a
small interface the generic harvest runner, store, and index rebuild all
work against:

* `source` — the source's name, which keys its mirror directory and index
  file.
* `earliest_watermark()` — where a fresh mirror's harvest begins.
* `fetch(watermark)` — an iterator of record batches: everything the
  upstream feed has touched since the watermark, normalised into
  `Record`s. A record that fails to parse yields the raised Exception in
  its place, so the runner can count it and continue. Politeness (rate
  limiting, batch sizing) lives inside; a batch smaller than the source's
  full batch size is the last.
* `shard(id, date)` — which mirror archive holds this id, given the id's
  index-entry date (each source shards at a granularity to suit its
  volume). The rule is a pure function of (id, date); a source whose ids
  encode their own dates ignores `date` and accepts None, while a source
  with date-based shards and opaque ids returns None for an unknown date
  (the document is not locatable without its entry).
* `subscription(section)` — the subscribed-entry predicate for the
  source's config section: which index entries the reading queries range
  over (arXiv: category overlap; a broad source may subscribe to
  everything).
* `entry(doc)` — a mirrored document's index entry.
* `datestamp(doc)` — when the upstream feed last touched the document
  (the axis the harvest watermark advances along).
* `to_paper(doc)` — the document→display mapping the scanner renders.
* `filename(paper)` — the filename (with extension) a grabbed copy of
  the paper files under.
* `grab(paper, path)` — fetch the paper's full content (PDF, post body)
  from upstream and write it to `path`; returns the status message the
  scanner shows. Raises on failure without creating `path`'s file, or
  leaves any partial file to its own cleanup.
"""

import dataclasses
import datetime


@dataclasses.dataclass
class Record:
    """One fetched record, normalised: `doc` is None iff the record was
    deleted upstream (so the mirrored copy, if any, must be removed)."""
    id: str                    # the source's own id for the document
    datestamp: datetime.date   # when the feed last touched it
    doc: dict | None


def adapter(source: str):
    """The adapter for `source`: a shared, stateless instance. (Imported
    lazily so importing this package stays light.)"""
    if source == "arxiv":
        from firehose.sources import arxiv
        return arxiv.ADAPTER
    if source in ("lw", "eaf"):
        from firehose.sources import forummagnum
        return forummagnum.ADAPTERS[source]
    raise ValueError(f"unknown source: {source}")
