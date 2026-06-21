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


# firehose reads its settings from a TOML config (config.toml at the top level
# by default; override with --config-path). config.toml is configuration, not
# data, so it stays at the top level rather than under the data directory.
# Path precedence is:  CLI argument  >  [paths] in the config  >  default below.
CONFIG_PATH = "config.toml"
DEFAULT_DATA_DIR = "data"
DEFAULT_DOWNLOAD_DIR = "~/storage/library/readings"

# arXiv's OAI-PMH endpoint, shared by `harvest` and `classes`.
OAI_API_URL = "https://oaipmh.arxiv.org/oai"


def load_config(path: str) -> dict:
    """Parse the TOML config file."""
    with open(path, "rb") as f:
        return tomllib.load(f)


def subscribed_classes(config: dict) -> set[str]:
    """Subscribed arXiv setSpecs: the [arxiv].categories list. Commented-out
    entries (the available-but-not-followed catalog) are TOML comments, so the
    parser drops them automatically.
    """
    return set(config["arxiv"]["categories"])


def resolve_paths(
    config: dict,
    *,
    data_dir: str | None = None,
    download_dir: str | None = None,
) -> types.SimpleNamespace:
    """Resolve the data-file and download paths, with optional per-run
    overrides. Precedence for each: the explicit argument > [paths] in the
    config > the built-in default.
    """
    paths = config.get("paths", {})
    data_dir = os.path.expanduser(data_dir or paths.get("data", DEFAULT_DATA_DIR))
    download_dir = os.path.expanduser(
        download_dir or paths.get("downloads", DEFAULT_DOWNLOAD_DIR)
    )
    return types.SimpleNamespace(
        data_dir=data_dir,
        cache=os.path.join(data_dir, "arxiv.txt"),
        readlog=os.path.join(data_dir, "readlog.txt"),
        savelog=os.path.join(data_dir, "savelog.txt"),
        scanlog=os.path.join(data_dir, "scanlog.jsonl"),
        downloads=download_dir,
    )


def paths(
    config_path: str = CONFIG_PATH,
    *,
    data_dir: str | None = None,
    download_dir: str | None = None,
) -> types.SimpleNamespace:
    """Convenience: load the config and resolve paths in one call."""
    return resolve_paths(
        load_config(config_path),
        data_dir=data_dir,
        download_dir=download_dir,
    )


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
    for line in tqdm.tqdm(lines, ncols=80):
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
):
    """Write the paper cache to disk.

    Keys are expected to carry the OAI prefix ("oai:arXiv.org:"), which is
    stripped on the way out, so ids are stored bare and sorted by (date, id).
    load_cache re-adds the prefix by default (strip_prefix=False).
    """
    sorted_cache = sorted([(date, xid) for xid, date in cache.items()])
    with open(path, 'w') as f:
        f.write(f"latest datestamp: {to_datestamp(latest_date)}\n")
        for date, xid in tqdm.tqdm(sorted_cache, ncols=80):
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
        ncols=80,
    )
    # open file (the caller ensures the path does not already exist)
    with open(path, 'wb') as file:
        # stream the data into the file
        for data in response.iter_content(chunk_size=1024):
            size = file.write(data)
            bar.update(size)
    bar.close()


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


def log_event(path: str, event: dict) -> None:
    """
    Append one event as a JSON line to the scan log at `path`, stamped with the
    current local time under the key "t". Each call is a self-contained append,
    so the log is written in real time and survives a crash mid-session.
    """
    record = {"t": datetime.datetime.now().isoformat(), **event}
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


