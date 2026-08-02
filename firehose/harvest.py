import collections
import itertools
import os
import time

from sickle import Sickle
from sickle.oaiexceptions import NoRecordsMatch
import tqdm

from firehose import arxivraw
from firehose import index
from firehose import mirror as mirror_store
from firehose import util
from firehose import vis


MAX_RPS = 1/3
BATCH_SIZE = 3_500
# BATCH_SIZE = 20_000 # for headers only

# OAI-PMH record identifiers carry this prefix (e.g. "oai:arXiv.org:2603.04402").
# Strip it at ingest so the cache and the rest of firehose deal in bare ids.
OAI_ID_PREFIX = "oai:arXiv.org:"


def harvest(
    expected_total: int | None = None,
    num_batches: int | None = None,
    config_path: str = util.CONFIG_PATH,
    data_dir: str | None = None,
):
    """
    Download new arXiv ids in selected classes.
    """
    # load subscribed classes and resolve data paths from config
    config = util.load_config(config_path)
    my_classes = set(config["arxiv"]["categories"])
    paths = util.data_paths(config, data_dir=data_dir)
    cache_path = paths.cache

    # configure client
    sickle = Sickle(util.OAI_API_URL)

    # identifying archive
    print("identifying archive...")
    last_request_time = time.time()
    identify = sickle.Identify()

    # load previous headers
    if os.path.exists(cache_path):
        print("loading papers from disk...")
        cache, latest_date = util.load_cache(path=cache_path)
        print(f"loaded {len(cache)} papers")
        print(f"* latest date: {latest_date}")
    else:
        cache = {}
        print("no previous paper cache detected.")
        latest_date = util.to_date(identify.earliestDatestamp)
        print(f"* latest date: {latest_date}")

    # query all record headers from that date
    print(f"querying all papers updated since {latest_date}...")
    new_records = sickle.ListRecords(
        metadataPrefix='oai_dc',
        **{'from': util.to_datestamp(latest_date)},
    )

    # work through the query
    if expected_total is None:
        total = None
    else:
        total = expected_total - len(cache)
    bar = tqdm.tqdm(
        total=total,
        ncols=80,
        unit=" papers",
        unit_scale=1,
    )
    try:
        for _ in itertools.count() if num_batches is None else range(num_batches):
            # rate limit
            next_request_time = last_request_time + 1/MAX_RPS + 0.5
            wait_time = next_request_time - time.time()
            if wait_time > 0:
                bar.write(f"waiting {wait_time} seconds...")
                time.sleep(wait_time)

            # load a batch of papers
            bar.write("loading a batch of papers...")
            batch = []
            last_request_time = time.time()
            for t, record in zip(range(BATCH_SIZE), new_records):
                batch.append(record)

            # update progress bar
            bar.update(len(batch))
            bar.write(f"loaded {len(batch)} papers:")

            # save the new article ids to memory
            new_dates = []
            num_new_papers = 0
            num_got_papers = 0
            num_skipped_papers = 0
            num_deleted_records = 0
            num_removed_papers = 0
            for record in batch:
                xid = record.header.identifier.removeprefix(OAI_ID_PREFIX)
                # Deleted OAI records have a header (including their update
                # datestamp) but no metadata. Process them before touching
                # record.metadata, and remove any now-unavailable cached id.
                update_date = util.to_date(record.header.datestamp)
                if record.deleted:
                    num_deleted_records += 1
                    if cache.pop(xid, None) is not None:
                        num_removed_papers += 1
                    latest_date = update_date
                    continue

                submit_date = util.to_date(record.metadata['date'][0])
                classes = set(record.header.setSpecs)
                if not (classes & my_classes):
                    num_skipped_papers += 1
                    latest_date = update_date
                    continue
                if xid not in cache:
                    num_new_papers += 1
                    new_dates.append(submit_date)
                    cache[xid] = submit_date
                else:
                    num_got_papers += 1
                latest_date = update_date
            # print the new article statistics
            bar.write(f"* got papers:      {num_got_papers}")
            bar.write(f"* new papers:      {num_new_papers}")
            bar.write(f"* skipped papers:  {num_skipped_papers}")
            bar.write(f"* deleted records: {num_deleted_records}")
            bar.write(f"* removed papers:  {num_removed_papers}")
            bar.write("* new paper dates:")
            bar.write(str(vis.vis_dates(dates=new_dates, print_counts=False)))
            bar.write(f"* new latest update date: {latest_date}")

            if len(batch) < BATCH_SIZE:
                break
    except KeyboardInterrupt:
        print("\nexiting query early.")
    except Exception as e:
        print("exiting query due to another error:", e)
        raise
    finally:
        # Preserve all successfully processed records even when the query is
        # interrupted or fails. Unexpected errors then continue propagating, so
        # callers and shell scripts are not told that a partial harvest succeeded.
        bar.close()
        print("saving papers to disk...")
        util.ensure_data_dir(paths)
        util.save_cache(
            path=cache_path,
            latest_date=latest_date,
            cache=cache,
        )

    print("done.")


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
    last_request_time = time.time()
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
                    status = _apply(parsed, entries, paths.mirror)
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

            # checkpoint the index so an interrupted run resumes from near
            # where it stopped rather than from the last completed run
            if batch_number % checkpoint_batches == 0:
                bar.write("checkpoint: saving index...")
                index.save_index(
                    path=paths.index, watermark=watermark, entries=entries,
                )

            if len(batch) < BATCH_SIZE:
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
        print("saving index...")
        index.save_index(
            path=paths.index, watermark=watermark, entries=entries,
        )
        print(f"saved {len(entries)} entries; watermark {watermark}")

    print("done.")


def _apply(
    parsed: arxivraw.ParsedRecord,
    entries: dict[str, index.Entry],
    mirror_dir: str,
) -> str:
    """
    Apply one parsed OAI record to the mirror and the in-memory index
    entries. Returns "new", "updated", or "unchanged" for live records
    (per the mirror write), or "deleted" / "deleted-absent" for records
    deleted upstream (per whether there was a paper to remove).
    """
    if parsed.doc is None:
        existed = mirror_store.delete_paper(mirror_dir, parsed.xid)
        entries.pop(parsed.xid, None)
        return "deleted" if existed else "deleted-absent"
    status = mirror_store.write_paper(mirror_dir, parsed.doc)
    entries[parsed.xid] = index.Entry(
        date=arxivraw.submitted_date(parsed.doc),
        categories=tuple(parsed.doc.get("categories", ())),
    )
    return status
