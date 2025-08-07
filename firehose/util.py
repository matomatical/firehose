import calendar
import collections
import datetime

import requests
import matthewplotlib as mp
import tqdm


def vis_dates(
    dates: list[datetime.date],
    all_dates: None | list[datetime.date] = None,
    print_counts: bool = True,
) -> mp.plot:
    """
    Adapted from matthewplotlib calendar heatmap example.
    """
    # count dates
    counts = collections.Counter(dates)
    if print_counts:
        for datestamp, count in sorted(counts.items()):
            print(datestamp, count)

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
                    day_plots.append(mp.text("▘ ", color=(0,0,0)))
                    continue
                day_plots.append(mp.text(
                    "▟█",
                    color=mp.cool(1-norm_data[date]),
                    bgcolor=(0,0,0),
                ))
            week_plots.append(mp.hstack(*day_plots))
        month_plots.append(
            mp.vstack(title, daynames, *week_plots)
            | mp.blank(2,2),
        )
        
        # increment month
        month += 1
        if month == 13:
            year += 1
            month = 1

    plot = mp.wrap(*month_plots)
    return plot


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
    return (
        mp.wrap(*[
            mp.text("▟█", color=mp.cool(1-p), bgcolor=(0,0,0))
            for p in proportions
        ])
        ^ mp.text(
            f"completed {batches_complete} "
            f"out of {num_batches} batches "
            f"of {batch_size} papers"
        )
        ^ (
            mp.text("total progress: ")
            | mp.text(f"{total_progress:.3%}", color=mp.cool(1-total_progress))
        )
    )


def load_my_classes(
    path: str,
) -> set[str]:
    my_classes = set()
    with open(path) as f:
        for line in f:
            class_, star, _ = line.split(maxsplit=2)
            if star == "*":
                my_classes.add(class_)
    return my_classes


def load_cache(
    path: str,
    strip_prefix: bool = False,
) -> tuple[dict[str, datetime.date], datetime.date]:
    cache = {}
    with open(path, 'rt') as f:
        # 1st line has form "number of papers: NUM_PAPERS"
        num_papers = int(next(f).strip().split(": ")[-1])
        # 2nd line has form "latest datestamp: DATESTAMP"
        latest_date = to_date(next(f).strip().split(": ")[-1])
        # subsequent lines
        for line in tqdm.tqdm(f, total=num_papers):
            xid, datestamp = line.strip().split()
            if strip_prefix:
                xid = xid[len("oai:arXiv.org:"):]
            cache[xid] = to_date(datestamp)
    return cache, latest_date


def load_readlog(
    path: str,
) -> dict[str, datetime.date]:
    readlog = {}
    with open(path, 'r') as f:
        for line in f:
            xid, datestamp = line.strip().split()
            readlog[xid] = to_date(datestamp)
    return readlog


def to_date(datestamp: str) -> datetime.date:
    return datetime.datetime.strptime(datestamp, '%Y-%m-%d').date()


def to_datestamp(date: datetime.date) -> str:
    return date.strftime('%Y-%m-%d')


def download_paper(paper_id: str, path: str):
    # get download iterator
    url = f"https://arxiv.org/pdf/{paper_id}.pdf"
    response = requests.get(url, stream=True)
    total = int(response.headers.get('content-length', 0))
    bar = tqdm.tqdm(
        desc="Download",
        total=int(response.headers.get('content-length', None)),
        unit='iB',
        unit_scale=True,
        unit_divisor=1024,
    )
    # open file (TODO: CHECK IT DOES NOT EXIST?)
    with open(path, 'wb') as file:
        # stream the data into the file
        for data in response.iter_content(chunk_size=1024):
            size = file.write(data)
            bar.update(size)
    bar.close()

