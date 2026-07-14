import itertools
import os
import time

from sickle import Sickle
import tqdm

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
