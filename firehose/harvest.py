"""
The `firehose mirror` command: a source-generic harvest runner. For each
source, the runner asks the source's adapter for everything upstream has
touched since the index's watermark and applies it to the source's mirror
and index, checkpointing along the way so an interrupted run resumes from
near where it stopped. Everything source-specific — the upstream API, the
normalisation, politeness — lives behind the adapter's `fetch`.
"""

import collections
import datetime
import itertools
import os

import tqdm

from firehose import index
from firehose import mirror as mirror_store
from firehose import sources
from firehose import util


def mirror(
    expected_total: int | None = None,
    num_batches: int | None = None,
    config_path: str = util.CONFIG_PATH,
    data_dir: str | None = None,
    checkpoint_batches: int = 25,
):
    """
    Download new and updated records into each source's metadata mirror,
    maintaining the derived indexes and their watermarks. (arXiv is the
    sole source so far.)
    """
    config = util.load_config(config_path)
    paths = util.data_paths(config, data_dir=data_dir)
    _harvest(
        sources.adapter("arxiv"),
        paths,
        expected_total=expected_total,
        num_batches=num_batches,
        checkpoint_batches=checkpoint_batches,
    )
    print("done.")


def _harvest(
    adapter,
    paths: util.DataPaths,
    *,
    expected_total: int | None,
    num_batches: int | None,
    checkpoint_batches: int,
) -> None:
    """Harvest one source: fetch records since its index's watermark and
    apply them to its mirror and index."""
    mirror_dir = paths.mirror(adapter.source)
    index_path = paths.index(adapter.source)
    util.ensure_data_dir(paths)
    os.makedirs(mirror_dir, exist_ok=True)
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    run_start = datetime.datetime.now().isoformat()

    # load the index, or start a fresh mirror from the source's beginning
    if os.path.exists(index_path):
        print("loading index...")
        entries, watermark = index.load_index(index_path)
        print(f"loaded {len(entries)} papers")
        print(f"* watermark: {watermark}")
    else:
        entries = {}
        watermark = adapter.earliest_watermark()
        print("no index detected: starting a fresh mirror.")
        print(f"* watermark: {watermark} (the source's earliest datestamp)")

    # query all records updated since the watermark; the batch pulled
    # before the loop's machinery exists keeps the nothing-new run cheap
    # (no index rewrite)
    print(f"querying all records updated since {watermark}...")
    batches = adapter.fetch(watermark)
    first_batch = next(batches, None)
    if first_batch is None:
        print("no new records.")
        _record_harvest(
            paths,
            source=adapter.source,
            t_start=run_start,
            counts={},
            watermark=watermark,
            papers=len(entries),
            completed=True,
        )
        return

    # work through the query
    if expected_total is None:
        total = None
    else:
        total = max(0, expected_total - len(entries))
    bar = tqdm.tqdm(
        total=total,
        ncols=80,
        unit=" papers",
        unit_scale=1,
        disable=None,
    )
    totals = collections.Counter()
    updater = mirror_store.Updater(mirror_dir, shard_fn=adapter.shard)
    completed = False   # reached the end of the query (vs interrupted/partial)
    try:
        for batch_number in (
            itertools.count(1) if num_batches is None
            else range(1, num_batches + 1)
        ):
            batch = first_batch if batch_number == 1 else next(batches, None)
            if batch is None:
                completed = True
                break
            bar.update(len(batch))

            # apply the batch to the mirror and the in-memory index
            counts = collections.Counter()
            new_dates = []
            for record in batch:
                if isinstance(record, Exception):
                    counts["error"] += 1
                    bar.write(f"! error on record: {record!r}")
                    continue
                status = _apply(record, entries, updater, adapter)
                watermark = max(watermark, record.datestamp)
                counts[status] += 1
                if status == "new":
                    new_dates.append(entries[record.id].date)
            totals.update(counts)

            # report the batch
            bar.write(f"batch {batch_number}: {len(batch)} records")
            bar.write("* " + ", ".join(
                f"{key}: {count}" for key, count in sorted(counts.items())
            ))
            if new_dates:
                bar.write(
                    f"* new papers submitted"
                    f" {min(new_dates)} .. {max(new_dates)}"
                )
            bar.write(f"* watermark: {watermark}")

            # checkpoint so an interrupted run resumes from near where it
            # stopped rather than from the last completed run. The mirror
            # flushes first: the index (whose save advances the watermark)
            # must never describe papers the mirror doesn't hold.
            if batch_number % checkpoint_batches == 0:
                bar.write("checkpoint: flushing mirror and saving index...")
                updater.flush()
                index.save_index(
                    path=index_path, watermark=watermark, entries=entries,
                )
    except KeyboardInterrupt:
        print("\nexiting query early.")
    except Exception as e:
        print("exiting query due to another error:", e)
        raise
    finally:
        # Preserve all successfully processed records even when the query is
        # interrupted or fails. Unexpected errors then continue propagating,
        # so callers and shell scripts are not told a partial run succeeded.
        bar.close()
        print("totals: " + ", ".join(
            f"{key}: {count}" for key, count in sorted(totals.items())
        ))
        print("flushing mirror and saving index...")
        updater.flush()
        index.save_index(
            path=index_path, watermark=watermark, entries=entries,
        )
        print(f"saved {len(entries)} entries; watermark {watermark}")
        _record_harvest(
            paths,
            source=adapter.source,
            t_start=run_start,
            counts=dict(totals),
            watermark=watermark,
            papers=len(entries),
            completed=completed,
        )


def _record_harvest(
    paths: util.DataPaths,
    *,
    source: str,
    t_start: str,
    counts: dict[str, int],
    watermark,
    papers: int,
    completed: bool,
) -> None:
    """
    Append one run's record to the harvest log (harvests.jsonl beside the
    other data files): the source it harvested, what the run applied
    ("counts", empty when there was nothing new), the watermark it
    reached, the resulting mirror size ("papers"), and whether it saw the
    query through to the end ("completed" is False for interrupted and
    batch-limited runs). The record's "t" stamp is the write time, i.e.
    when the run ended; "t_start" is when it began. Written only after
    the index is saved, so the log never describes state that didn't land
    on disk.
    """
    util.log_event(paths.harvests, {
        "source": source,
        "t_start": t_start,
        "counts": counts,
        "watermark": util.to_datestamp(watermark),
        "papers": papers,
        "completed": completed,
    })


def _apply(
    record: sources.Record,
    entries: dict[str, index.Entry],
    updater: mirror_store.Updater,
    adapter,
) -> str:
    """
    Apply one fetched record to the mirror updater and the in-memory
    index entries. Returns "new", "updated", or "unchanged" for live
    records (per the mirror upsert), or "deleted" / "deleted-absent" for
    records deleted upstream (per whether there was a document to remove).
    """
    if record.doc is None:
        existed = updater.delete(record.id)
        entries.pop(record.id, None)
        return "deleted" if existed else "deleted-absent"
    status = updater.upsert(record.doc)
    entries[record.id] = adapter.entry(record.doc)
    return status
