import collections
import datetime
import re

import requests
import tqdm


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
    with open(path, 'r') as f:
        # 1st line has form "latest datestamp: DATESTAMP"
        latest_date = to_date(next(f).strip().split(": ")[-1])
        # subsequent lines are papers
        lines = f.read().splitlines()
    # process these
    for line in tqdm.tqdm(lines):
        xid, datestamp = line.split()
        cache[xid] = to_date(datestamp)
    # optionally add prefixes
    if not strip_prefix:
        cache = {"oai:arXiv.org:" + x: d for x, d in cache.items()}
    return cache, latest_date


def save_cache(
    path: str,
    latest_date: datetime.date,
    cache: dict[str, datetime.date],
    has_prefix: bool = False,
):
    sorted_cache = sorted([(date, xid) for xid, date in cache.items()])
    with open(path, 'w') as f:
        f.write(f"latest datestamp: {to_datestamp(latest_date)}\n")
        for date, xid in tqdm.tqdm(sorted_cache):
            f.write("{} {}\n".format(
                xid[len("oai:arXiv.org:"):],
                to_datestamp(date),
            ))


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
    # robust method
    # return datetime.datetime.strptime(datestamp, '%Y-%m-%d').date()
    # faster method, taking advantage of fixed format
    return datetime.date(*map(int, datestamp.split('-')))


def to_datestamp(date: datetime.date) -> str:
    return date.strftime('%Y-%m-%d')


def to_name(result) -> str:
    """
    `result` is from the arXiv API, it has field:

    * .authors: a list of authors with str .name fields
    * .published.year: the year of submission
    * .title: string title

    This method combines these into a string name for the paper in my preferred
    format.
    """
    # author
    # 1:  LastName
    # 2:  LastName1+LastName2
    # >2: LastName1+
    authors = [a.name.split()[-1] for a in result.authors]
    if len(authors) > 2:
        authors[1:] = [""]
    author_str = "+".join(authors)

    # year is just year
    year_str = str(result.published.year)

    # title is just title
    title_str = result.title

    # combine
    return f"{author_str}{year_str} {title_str}"


def to_filename(name: str, xidv: str) -> str:
    return re.sub(r"[^\w ?+,'()\[\]\-]", "_", f"{name} [{xidv}]") + ".pdf"


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


