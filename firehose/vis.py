import calendar
import collections
import datetime
import time
import typing

import matthewplotlib as mp

from firehose import util


READLOG_PATH = "rdlog.txt"
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
    vis = vis_dates(dates)
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


def reading_calendar(
    mode: typing.Literal[
        "read-date",
        "submit-date",
        "proportion",
    ] = "read-date",
    readlog_path: str = READLOG_PATH,
    cache_path: str = CACHE_PATH,
    save_as: str | None = None,
):
    print("loading read log...")
    readlog = util.load_readlog(path=readlog_path)
    print(f"loaded {len(readlog)} already-read papers")

    if mode == "submit-date" or mode == "proportion":
        print("loading their submitted dates from paper cache...")
        cache, _ = util.load_cache(path=cache_path, strip_prefix=True)
        print(f"resolved {len(cache)} read papers")
    
    print("printing calendar...")
    if mode == "read-date":
        read_dates = list(readlog.values())
        vis = vis_dates(read_dates)
    
    elif mode == "submit-date":
        submit_dates = [ cache[xid] for xid in readlog if xid in cache ]
        vis = vis_dates(submit_dates)

    elif mode == "proportion":
        submit_dates = [ cache[xid] for xid in readlog if xid in cache ]
        all_dates = list(cache.values())
        vis = vis_dates(
            dates=submit_dates,
            all_dates=all_dates,
        )

    print(vis)
        
    if save_as:
        print(f"saving calendar to {save_as}...")
        vis.saveimg(save_as)


def linear(
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
    vis = vis_all(
        all_xids=all_xids,
        read_xids=read_xids,
        batch_size=batch_size,
    )
    print(vis)

    if save_as:
        print(f"saving visualisation to {save_as}...")
        vis.saveimg(save_as)


def hilbert(
    live: bool = False,
    size: int | None = None,
    readlog_path: str = READLOG_PATH,
    cache_path: str = CACHE_PATH,
):
    print("loading all submitted ids from paper cache...")
    cache, _ = util.load_cache(path=cache_path, strip_prefix=True)
    all_xids = {xid: i for i, xid in enumerate(cache.keys())}
    print(f"found {len(all_xids)} papers")

    print("computing read vector...")
    read_vec = [False] * len(all_xids)
    rendered = False
    
    print("starting read loop...")
    with open(readlog_path, 'r') as f:
        while True:
            # read titles added so far
            new_titles = False
            for line in f:
                new_titles = True
                xid, _ = line.strip().split()
                if xid in all_xids:
                    read_vec[all_xids[xid]] = True

            # if there are new titles, redraw plot
            if new_titles:
                show_vec = read_vec if size is None else read_vec[-4**size:]
                vis = mp.hilbert(
                    data=show_vec,
                    color=(0.0, 1.0, 1.0),
                )
                if not rendered: # first time
                    print(vis)
                    rendered = True
                else: # subsequent
                    print(f"\x1b[{vis.height}A{vis}")
            
            # otherwise wait until next poll
            elif live:
                time.sleep(3)

            # or break
            else:
                break


def vis_dates(
    dates: list[datetime.date],
    all_dates: None | list[datetime.date] = None,
    print_counts: bool = True,
) -> mp.plot:
    """
    Adapted from matthewplotlib calendar heatmap example.
    """
    datelines = []
    # count dates
    counts = collections.Counter(dates)
    if print_counts:
        for datestamp, count in sorted(counts.items()):
            datelines.append(mp.text(f"{datestamp} {count}  "))

    if len(counts) == 0:
        return mp.text("(no dates)")

    # normalise counts
    if all_dates is None:
        max_count = max(counts.values())
        norm_data = {date: count/max_count for date, count in counts.items()}
    else:
        total_counts = collections.Counter(all_dates)
        norm_data = {
            date: counts.get(date, 0) / total_counts[date]
            for date in total_counts.keys()
        }

    start_date = min(norm_data.keys())
    end_date = max(norm_data.keys())
    year = start_date.year
    month = start_date.month
    month_plots = []
    while datetime.date(year, month, 1) <= end_date:
        # collect month
        title = mp.text(f"{calendar.month_name[month]:<9s} {year:4d}")
        daynames = mp.text("M T W t F S s ")
        week_plots = []
        for week in calendar.monthcalendar(year, month):
            day_plots = []
            for day in week:
                if day == 0:
                    day_plots.append(mp.text("  "))
                    continue
                date = datetime.date(year, month, day)
                if date not in norm_data:
                    day_plots.append(mp.text("▘ ", fgcolor=(0,0,0)))
                    continue
                day_plots.append(mp.text(
                    "▟█",
                    fgcolor=mp.cyber(norm_data[date]),
                    bgcolor=(0,0,0),
                ))
            week_plots.append(mp.hstack(*day_plots))
        month_plots.append(
            mp.vstack(title, daynames, *week_plots)
            + mp.blank(2,2),
        )
        
        # increment month
        month += 1
        if month == 13:
            year += 1
            month = 1

    calendar_plot = mp.wrap(*month_plots)
    if print_counts:
        if len(datelines) > 50:
            counts_plot = mp.wrap(
                *datelines,
                transpose=True,
            )
        else:
            counts_plot = mp.vstack(*datelines)
        return counts_plot / calendar_plot
    else:
        return calendar_plot


def vis_all(
    all_xids: list[str],
    read_xids: list[str],
    batch_size: int,
) -> mp.plot:
    # batch and count proportions
    read_xids = set(read_xids)
    proportions = []
    for i in range(0, len(all_xids), batch_size):
        batch = set(all_xids[i:i+batch_size])
        batch_read = batch & read_xids
        proportions.append(len(batch_read)/len(batch))

    # statistics
    num_batches = len(proportions)
    batches_complete = sum(p == 1 for p in proportions)
    total_progress = len(read_xids) / len(all_xids)
    
    # generate plots
    plot = mp.vstack(
        mp.wrap(*[
            mp.text("▟█", fgcolor=mp.cyber(p), bgcolor=(0,0,0))
            for p in proportions
        ]),
        mp.text(
            f"completed {batches_complete} "
            f"out of {num_batches} batches "
            f"of {batch_size} papers"
        ),
        mp.text("total progress: ")
        + mp.text(
            f"{total_progress:.3%}",
            fgcolor=mp.cyber(total_progress),
        )
    )
    return plot


