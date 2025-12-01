import itertools
import os
import time

from sickle import Sickle
import tqdm

from firehose import util
from firehose import vis


API_URL = "https://oaipmh.arxiv.org/oai"
MAX_RPS = 1/3
BATCH_SIZE = 3_500
# BATCH_SIZE = 20_000 # for headers only
CACHE_PATH = "arxiv.txt"
CLASSES_PATH = "classes.txt"


def harvest(
    expected_total: int | None = None,
    num_batches: int | None = None,
    classes_path: str = CLASSES_PATH,
    cache_path: str = CACHE_PATH,
):
    """
    Download new arXiv ids in selected classes.
    """
    # load classes
    my_classes = util.load_my_classes(path=classes_path)

    # configure client
    sickle = Sickle(API_URL)

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
        dynamic_ncols=True,
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
            for record in batch:
                xid = record.header.identifier
                submit_date = util.to_date(record.metadata['date'][0])
                _updated_date = util.to_date(record.metadata['date'][-1])
                latest_date = util.to_date(record.header.datestamp)
                classes = set(record.header.setSpecs)
                if not (classes & my_classes):
                    num_skipped_papers += 1
                    continue
                if xid not in cache:
                    num_new_papers += 1
                    new_dates.append(submit_date)
                    cache[xid] = submit_date
                else:
                    num_got_papers += 1
            # print the new article statistics
            bar.write(f"* got papers:     {num_got_papers}")
            bar.write(f"* new papers:     {num_new_papers}")
            bar.write(f"* skipped papers: {num_skipped_papers}")
            bar.write("* new paper dates:")
            bar.write(str(vis.vis_dates(dates=new_dates, print_counts=False)))
            bar.write(f"* new latest update date: {latest_date}")
            
            if len(batch) < BATCH_SIZE:
                break
    except KeyboardInterrupt as e:
        print("\nexiting query early.")
        pass
    except Exception as e:
        print("exiting query due to another error:", e)
        pass
    bar.close()
        
    print("saving papers to disk...")
    util.save_cache(
        path=cache_path,
        latest_date=latest_date,
        cache=cache,
        has_prefix=True,
    )

    print("done.")


