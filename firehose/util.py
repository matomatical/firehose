import collections
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import types

import requests
import tqdm


# firehose config
CONFIG_PATH = "config.toml"

# arXiv's OAI-PMH endpoint, shared by `harvest` and `classes`.
OAI_API_URL = "https://oaipmh.arxiv.org/oai"


# # # 
# Config loading utilities


def _anchor_path(path: str, base: str) -> str:
    """Expand a leading ~ and, if the result is still relative, resolve it
    against `base`."""
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    return path


def load_config(path: str = CONFIG_PATH) -> dict:
    """Parse the TOML config file.

    Relative `[paths]` values (data, downloads) are anchored to the config
    file's own directory, so firehose reads/writes the same data no matter
    which directory it is invoked from. (The `--data-dir`/`--download-dir` CLI
    overrides are left as given, i.e. relative to the current directory, since
    those are typed per-invocation in a shell.)
    """
    with open(path, "rb") as f:
        config = tomllib.load(f)
    base = os.path.dirname(os.path.abspath(path))
    paths = config.get("paths", {})
    for key in ("data", "downloads"):
        if key in paths:
            paths[key] = _anchor_path(paths[key], base)
    return config


def data_paths(
    config: dict,
    *,
    data_dir: str | None = None,
) -> types.SimpleNamespace:
    """
    Paths for data, with optional override. Ensures the data directory exists
    (it is gitignored, so it's absent on a fresh clone) so the first harvest or
    scan can write into it.
    """
    data_dir = os.path.expanduser(data_dir or config["paths"]["data"])
    os.makedirs(data_dir, exist_ok=True)
    return types.SimpleNamespace(
        data_dir=data_dir,
        cache=os.path.join(data_dir, "arxiv.txt"),
        readlog=os.path.join(data_dir, "readlog.txt"),
        scanlog=os.path.join(data_dir, "scanlog.jsonl"),
    )


# # #
# File parsing utilities
#
# arxiv.txt (the paper cache) and readlog.txt (the seen-index) are plain text,
# one paper per logical entry, sorted by date. To keep them small while staying
# greppable and hand-editable, entries sharing a date are grouped under a
# single date header rather than repeating the date on every line:
#
#     2026-03-04:    <- date header: every bare id below it has this date
#     2603.00012
#     2603.00077
#     2025-08-12:
#     2508.00002
#
# save_cache and readlog's live append (append_readlog, driven by
# sample.Readlog) both emit this grouped form.


def load_cache(
    path: str,
) -> tuple[dict[str, datetime.date], datetime.date]:
    """
    Load the {id: date} paper cache plus the "latest datestamp" watermark from
    the first line.
    """
    with open(path, 'r') as f:
        # 1st line has form "latest datestamp: DATESTAMP"
        latest_date = to_date(next(f).strip().split(": ")[-1])
        # subsequent lines are papers (see the format note above)
        lines = f.read().splitlines()
    cache = {}
    for xid, date in _parse_dated_lines(tqdm.tqdm(lines, ncols=80)):
        cache[xid] = date
    return cache, latest_date


def load_readlog(
    path: str,
) -> tuple[dict[str, datetime.date], datetime.date | None]:
    """
    Load the seen-index as a {id: date} dict, plus the date of its last entry
    (None if empty). That date seeds the live appender's open group, so
    resuming a same-day session continues that group without re-reading the
    file.
    """
    readlog = {}
    last_date = None
    with open(path, 'r') as f:
        for xid, date in _parse_dated_lines(f):
            readlog[xid] = date
            last_date = date
    return readlog, last_date


def _parse_dated_lines(lines):
    """
    Yield (id, date) per entry from an iterable of lines. Each entry is a bare
    "<id>" dated by the nearest "<YYYY-MM-DD>:" header above it; each header
    date is constructed once and shared across the ids beneath it.
    """
    current_date = None
    for line in lines:
        line = line.rstrip("\n")
        if line.endswith(":"):
            current_date = to_date(line[:-1])
        else:
            yield line, current_date


# # # 
# File writing utilities


def save_cache(
    path: str,
    latest_date: datetime.date,
    cache: dict[str, datetime.date],
):
    """
    Write the {id: date} paper cache to disk: the "latest datestamp" watermark
    on the first line, then the bare ids sorted by date and grouped.
    """
    sorted_cache = sorted((date, xid) for xid, date in cache.items())
    with open(path, 'w') as f:
        f.write(f"latest datestamp: {to_datestamp(latest_date)}\n")
        _write_grouped(f, tqdm.tqdm(sorted_cache, ncols=80))


def _write_grouped(f, dated_ids):
    """
    Write (date, id) pairs, sorted by date, in grouped form: a "<date>:" header
    whenever the date changes, then each id on its own line.
    """
    current_date = None
    for date, xid in dated_ids:
        if date != current_date:
            f.write(f"{to_datestamp(date)}:\n")
            current_date = date
        f.write(f"{xid}\n")


def append_readlog(
    path: str,
    xid: str,
    date: datetime.date,
    open_date: datetime.date | None,
) -> datetime.date:
    """
    Append `xid` to the seen-index in grouped form, writing a "<date>:" header
    first iff `open_date` (the date currently governing the end of the file)
    differs from `date`. Returns `date` as the new open_date to thread into the
    next call; seed the first call with the readlog's last date from
    load_readlog (None for a fresh file).
    """
    with open(path, 'a') as f:
        if open_date != date:
            f.write(f"{to_datestamp(date)}:\n")
        f.write(f"{xid}\n")
    return date


# # # 
# Event logging utilities


def log_event(path: str, event: dict) -> None:
    """
    Append one event as a JSON line to the scan log at `path`, stamped with the
    current local time under the key "t".
    """
    record = {"t": datetime.datetime.now().isoformat(), **event}
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def load_scanlog(path: str) -> list[dict]:
    """
    Read every event from the scan log: the JSON object on each non-blank line,
    in file (chronological) order. The inverse of log_event. Returns [] if the
    log does not exist yet (no scans recorded).
    """
    if not os.path.exists(path):
        return []
    events = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


# # # 
# Date utilities


def to_date(datestamp: str) -> datetime.date:
    # faster method, taking advantage of fixed format
    return datetime.date(*map(int, datestamp.split('-')))
    # robust method, alternative previously tried.
    # return datetime.datetime.strptime(datestamp, '%Y-%m-%d').date()


def to_datestamp(date: datetime.date) -> str:
    return date.strftime('%Y-%m-%d')


# # # 
# ArXiv paper handling utilities


def to_name(result) -> str:
    """
    `result` is from the arXiv API, it has field:

    * .authors: a list of authors with str .name fields
    * .published.year: the year of submission
    * .title: string title

    This method combines these into a string name for the paper.
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
        ncols=80,
    )
    # open file (the caller ensures the path does not already exist)
    with open(path, 'wb') as file:
        # stream the data into the file
        for data in response.iter_content(chunk_size=1024):
            size = file.write(data)
            bar.update(size)
    bar.close()


# # # 
# Platform-independent utilities


def copy_to_clipboard(text: str) -> bool:
    """
    Copy `text` to the system clipboard using the platform-appropriate tool.

    Returns True if the text was handed off to a clipboard tool, or False if no
    usable clipboard is available (e.g. a headless Linux session). Never raises
    when a clipboard tool is missing.
    """
    if sys.platform == "darwin":
        argv = ["pbcopy"]
    elif sys.platform.startswith("linux"):
        # only attempt if there is a display to own the X/Wayland selection
        if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
            return False
        if shutil.which("wl-copy"):
            argv = ["wl-copy"]
        elif shutil.which("xclip"):
            argv = ["xclip", "-selection", "clipboard"]
        elif shutil.which("xsel"):
            argv = ["xsel", "--clipboard", "--input"]
        else:
            return False
    else:
        return False
    try:
        with subprocess.Popen(argv, stdin=subprocess.PIPE) as proc:
            proc.communicate(input=text.encode())
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def open_url(url: str) -> bool:
    """
    Open `url` with the platform's default handler (browser / opener).

    Returns True if an opener was launched, or False otherwise (in which case
    the caller may want to print the URL instead). Never raises.
    """
    if sys.platform == "darwin":
        opener = "open"
    elif sys.platform.startswith("linux"):
        opener = "xdg-open" if shutil.which("xdg-open") else None
    else:
        opener = None
    if opener is None:
        return False
    try:
        subprocess.Popen(
            [opener, url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


