import collections

import tyro
import tqdm
import matthewplotlib as mp

from firehose import util
from firehose import sample


READLOG_PATH = "rdlog.txt"
CACHE_PATH = "arxiv.txt"


def reading_dates(
    readlog_path: str = READLOG_PATH,
    save_as: str | None = None,
):
    print("loading read log...")
    readlog = util.load_readlog(path=readlog_path)
    dates = list(readlog.values())
    print(f"loaded {len(dates)} already-read papers")

    print("printing calendar...")
    vis = util.vis_dates(dates)
    print(vis)

    if save_as:
        print(f"saving calendar to {save_as}...")
        vis.saveimg(save_as)


def submitted_dates(
    readlog_path: str = READLOG_PATH,
    cache_path: str = CACHE_PATH,
    save_as: str | None = None,
):
    print("loading read log...")
    readlog = util.load_readlog(path=readlog_path)
    xids = set(readlog.keys())
    print(f"loaded {len(xids)} already-read papers")

    print("loading their submitted dates from paper cache...")
    cache, _ = util.load_cache(path=cache_path, strip_prefix=True)
    dates = [ cache[xid] for xid in xids if xid in cache ]
    print(f"resolved {len(dates)} read papers")

    print("printing calendar...")
    vis = util.vis_dates(dates)
    print(vis)

    if save_as:
        print(f"saving calendar to {save_as}...")
        vis.saveimg(save_as)


def all_submitted_dates(
    cache_path: str = CACHE_PATH,
    save_as: str | None = None,
):
    print("loading all submit dates from paper cache...")
    cache, _ = util.load_cache(path=cache_path)
    print(f"loaded {len(cache)} papers")
    dates = list(cache.values())

    print("printing calendar...")
    vis = util.vis_dates(dates)
    print(vis)

    if save_as:
        print(f"saving calendar to {save_as}...")
        vis.saveimg(save_as)


def all_submitted_years(
    cache_path: str = CACHE_PATH,
):
    print("loading all submit dates from paper cache...")
    cache, _ = util.load_cache(path=cache_path)
    print(f"loaded {len(cache)} papers")
    
    years = collections.Counter([date.year for date in cache.values()])

    print("printing calendar...")
    for year, count in sorted(years.items()):
        print(f"- {year} ({count} papers)")


def all_submitted_months(
    cache_path: str = CACHE_PATH,
):
    print("loading all submit dates from paper cache...")
    cache, _ = util.load_cache(path=cache_path)
    print(f"loaded {len(cache)} papers")
    
    year_months = collections.Counter([
        (date.year, date.month) for date in cache.values()
    ])

    print("printing calendar...")
    for (year, month), count in sorted(year_months.items()):
        print(f"- {year}.{month} ({count} papers)")


def proportion_dates(
    readlog_path: str = READLOG_PATH,
    cache_path: str = CACHE_PATH,
    save_as: str | None = None,
):
    print("loading all submit dates from paper cache...")
    cache, _ = util.load_cache(path=cache_path, strip_prefix=True)
    print(f"catalogued {len(cache)} papers")

    print("loading read log...")
    readlog = util.load_readlog(path=readlog_path)
    readlog_submit_dates = [ cache[xid] for xid in readlog if xid in cache ]

    print("printing calendar...")
    vis = util.vis_dates(
        dates=list(readlog_submit_dates),
        all_dates=list(cache.values()),
    )
    print(vis)

    if save_as:
        print(f"saving calendar to {save_as}...")
        vis.saveimg(save_as)


def proportion_papers(
    readlog_path: str = READLOG_PATH,
    cache_path: str = CACHE_PATH,
    batch_size: int = 100,
    save_as: str | None = None,
):
    print("loading all submitted ids from paper cache...")
    cache, _ = util.load_cache(path=cache_path, strip_prefix=True)
    all_xids = list(cache.keys())
    print(f"found {len(all_xids)} papers")

    print("loading read log")
    readlog = util.load_readlog(path=readlog_path)
    read_xids = list(readlog.keys())
    print(f"found {len(read_xids)} read papers")

    print("printing visualisation...")
    vis = util.vis_all(
        all_xids=all_xids,
        read_xids=read_xids,
        batch_size=batch_size,
    )
    print(vis)

    if save_as:
        print(f"saving visualisation to {save_as}...")
        vis.saveimg(save_as)


def hilbert(
    readlog_path: str = READLOG_PATH,
    cache_path: str = CACHE_PATH,
    batch_size: int = 100,
    save_as: str | None = None,
):
    print("loading all submitted ids from paper cache...")
    cache, _ = util.load_cache(path=cache_path, strip_prefix=True)
    all_xids = list(cache.keys())
    print(f"found {len(all_xids)} papers")

    print("loading read log")
    readlog = util.load_readlog(path=readlog_path)
    read_xids = set(readlog.keys())
    print(f"found {len(read_xids)} read papers")

    print("computing read vector...")
    read_vec = [xid in read_xids for xid in all_xids]

    print("printing visualisation...")
    vis = mp.hilbert(
        data=read_vec,
        dotcolor=(0,1,1),
        bgcolor=(0.1,0,0.1),
    )
    print(vis)

    if save_as:
        print(f"saving visualisation to {save_as}...")
        vis.saveimg(save_as)


def cli():
    tyro.extras.subcommand_cli_from_dict({
        'read-date': reading_dates,
        'submit-date': submitted_dates,
        'all': all_submitted_dates,
        'proportion': proportion_dates,
        'years': all_submitted_years,
        'months': all_submitted_months,
        'linear': proportion_papers,
        'hilbert': hilbert,
    })
