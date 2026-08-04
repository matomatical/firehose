import collections
import datetime
import itertools
import os
import time

import tqdm

from firehose import arxivraw
from firehose import index
from firehose import mirror as mirror_store
from firehose import util


MAX_RPS = 1/3
BATCH_SIZE = 3_500

# The OAI client library is imported on first use rather than here: this
# module is imported by the CLI on every command, and only `mirror` (a
# server-side job) actually harvests.
Sickle = None
NoRecordsMatch = None


def _load_sickle():
    global Sickle, NoRecordsMatch
    if Sickle is None:
        from sickle import Sickle as sickle_client
        from sickle.oaiexceptions import NoRecordsMatch as no_records_match
        Sickle = sickle_client
        NoRecordsMatch = no_records_match


def mirror(
    expected_total: int | None = None,
    num_batches: int | None = None,
    config_path: str = util.CONFIG_PATH,
    data_dir: str | None = None,
    checkpoint_batches: int = 25,
):
    """
    Download new and updated arXiv records (all categories) into the
    metadata mirror, maintaining the derived index and its watermark.
    """
    config = util.load_config(config_path)
    paths = util.data_paths(config, data_dir=data_dir)
    util.ensure_data_dir(paths)
    os.makedirs(paths.mirror, exist_ok=True)
    _load_sickle()
    run_start = datetime.datetime.now().isoformat()

    # configure client; retries ride out transient 503s (the server sets
    # Retry-After) so long unattended runs survive them. The generous read
    # timeout is for deep ListRecords pages, which the server can take tens
    # of seconds to prepare; timeouts still get through it (they abort the
    # run, the index checkpoint keeps the watermark, and a rerun resumes).
    sickle = Sickle(util.OAI_API_URL, max_retries=10, timeout=180)

    # load the index, or start a fresh mirror from the dawn of the archive
    if os.path.exists(paths.index):
        print("loading index...")
        entries, watermark = index.load_index(paths.index)
        print(f"loaded {len(entries)} papers")
        print(f"* watermark: {watermark}")
    else:
        entries = {}
        watermark = util.to_date(sickle.Identify().earliestDatestamp)
        print("no index detected: starting a fresh mirror.")
        print(f"* watermark: {watermark} (archive's earliest datestamp)")

    # query all records updated since the watermark (inclusive, so the
    # watermark day is re-fetched: upserts make the overlap harmless)
    print(f"querying all records updated since {watermark}...")
    try:
        records = sickle.ListRecords(
            metadataPrefix="arXivRaw",
            **{"from": util.to_datestamp(watermark)},
        )
    except NoRecordsMatch:
        print("no new records.")
        _record_harvest(
            paths,
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
    updater = mirror_store.Updater(paths.mirror)
    last_request_time = time.time()
    completed = False   # reached the end of the query (vs interrupted/partial)
    try:
        for batch_number in (
            itertools.count(1) if num_batches is None
            else range(1, num_batches + 1)
        ):
            # rate limit
            next_request_time = last_request_time + 1/MAX_RPS + 0.5
            wait_time = next_request_time - time.time()
            if wait_time > 0:
                time.sleep(wait_time)

            # load a batch of records
            batch = []
            last_request_time = time.time()
            for _, record in zip(range(BATCH_SIZE), records):
                batch.append(record)
            bar.update(len(batch))

            # apply the batch to the mirror and the in-memory index
            counts = collections.Counter()
            new_dates = []
            for record in batch:
                try:
                    parsed = arxivraw.parse_record(record.xml)
                    status = _apply(parsed, entries, updater)
                    watermark = max(watermark, parsed.datestamp)
                except Exception as e:
                    counts["error"] += 1
                    bar.write(f"! error on record: {e!r}")
                    continue
                counts[status] += 1
                if status == "new":
                    new_dates.append(entries[parsed.xid].date)
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
                    path=paths.index, watermark=watermark, entries=entries,
                )

            if len(batch) < BATCH_SIZE:
                completed = True
                break
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
            path=paths.index, watermark=watermark, entries=entries,
        )
        print(f"saved {len(entries)} entries; watermark {watermark}")
        _record_harvest(
            paths,
            t_start=run_start,
            counts=dict(totals),
            watermark=watermark,
            papers=len(entries),
            completed=completed,
        )

    print("done.")


def _record_harvest(
    paths,
    *,
    t_start: str,
    counts: dict[str, int],
    watermark,
    papers: int,
    completed: bool,
) -> None:
    """
    Append one run's record to the harvest log (harvests.jsonl beside the
    other data files): what the run applied ("counts", empty when there was
    nothing new), the watermark it reached, the resulting mirror size
    ("papers"), and whether it saw the query through to the end
    ("completed" is False for interrupted and batch-limited runs). The
    record's "t" stamp is the write time, i.e. when the run ended; "t_start"
    is when it began. Written only after the index is saved, so the log
    never describes state that didn't land on disk.
    """
    util.log_event(paths.harvests, {
        "t_start": t_start,
        "counts": counts,
        "watermark": util.to_datestamp(watermark),
        "papers": papers,
        "completed": completed,
    })


def _apply(
    parsed: arxivraw.ParsedRecord,
    entries: dict[str, index.Entry],
    updater: mirror_store.Updater,
) -> str:
    """
    Apply one parsed OAI record to the mirror updater and the in-memory
    index entries. Returns "new", "updated", or "unchanged" for live
    records (per the mirror upsert), or "deleted" / "deleted-absent" for
    records deleted upstream (per whether there was a paper to remove).
    """
    if parsed.doc is None:
        existed = updater.delete(parsed.xid)
        entries.pop(parsed.xid, None)
        return "deleted" if existed else "deleted-absent"
    status = updater.upsert(parsed.doc)
    entries[parsed.xid] = index.Entry(
        date=arxivraw.submitted_date(parsed.doc),
        categories=tuple(parsed.doc.get("categories", ())),
    )
    return status
