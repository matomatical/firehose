import collections

import matthewplotlib as mp

from firehose import util


CACHE_PATH = "arxiv.txt"


def all_submitted_dates(
    cache_path: str = CACHE_PATH,
    save_as: str | None = None,
):
    print("loading all submit dates from paper cache...")
    cache, _ = util.load_cache(path=cache_path)
    print(f"loaded {len(cache)} papers")

    print("printing calendar...")
    dates = list(cache.values())
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





