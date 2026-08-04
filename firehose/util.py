import collections
import dataclasses
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib


# firehose config
CONFIG_PATH = "config.toml"


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


@dataclasses.dataclass(frozen=True)
class DataPaths:
    """
    Where each data file lives under the data directory. The cross-source
    files (the event log and its relatives, the harvest log) are fixed
    paths; the mirror and index are per-source, keyed by source name
    ("arxiv"), so each source keeps its own shard directory and watermark.
    """

    data_dir: str

    def mirror(self, source: str) -> str:
        """The source's mirror directory (its metadata shards)."""
        return os.path.join(self.data_dir, "mirror", source)

    def index(self, source: str) -> str:
        """The source's index file (entries plus harvest watermark)."""
        return os.path.join(self.data_dir, "index", f"{source}.txt")

    @property
    def events(self) -> str:
        return os.path.join(self.data_dir, "events.jsonl")

    @property
    def harvests(self) -> str:
        return os.path.join(self.data_dir, "harvests.jsonl")

    @property
    def unsent(self) -> str:
        return os.path.join(self.data_dir, "unsent-events.jsonl")

    @property
    def readlog(self) -> str:  # retired format
        return os.path.join(self.data_dir, "readlog.txt")


def data_paths(
    config: dict,
    *,
    data_dir: str | None = None,
) -> DataPaths:
    """
    Compute the data file paths, with an optional data-dir override; ~ is
    expanded. Pure: it does not touch the filesystem. Writers (harvest, sample)
    call ensure_data_dir first, since the data dir is gitignored and so absent
    on a fresh clone; readers (vis) don't need it to exist.
    """
    return DataPaths(
        data_dir=os.path.expanduser(data_dir or config["paths"]["data"]),
    )


def ensure_data_dir(paths: DataPaths) -> None:
    """Create the data directory (from data_paths) if absent, so a writer can
    file into it. Separated from data_paths so path computation stays pure."""
    os.makedirs(paths.data_dir, exist_ok=True)


# # #
# File parsing utilities


def load_readlog(
    path: str,
) -> tuple[dict[str, datetime.date], datetime.date | None]:
    """
    Load a retired-format readlog.txt as a {id: date} dict, plus the date of
    its last entry (None if empty, or when the file does not exist).

    The format is plain text with entries grouped under date headers:

        2026-03-04:    <- date header: every bare id below it has this date
        2603.00012
        2603.00077

    The live client no longer writes this file — reading history lives in
    the event log — but this parser remains so the one-off import of a
    readlog into the event log can be re-run against a straggler copy
    (e.g. from a machine that scanned on an older client).
    """
    if not os.path.exists(path):
        return {}, None
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
# Event logging utilities


def log_event(path: str, event: dict) -> int:
    """
    Append one event as a JSON line to the event log at `path`. An event
    without a "t" timestamp is stamped with the current local time; one that
    already carries a "t" (recorded earlier, written now) keeps it. Returns
    the size of the log after the append, for readers that tail the file.
    """
    record = {"t": datetime.datetime.now().isoformat(), **event}
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
        return f.tell()


def load_events(path: str) -> list[dict]:
    """
    Read every event from the event log: the JSON object on each non-blank line,
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
# Download filename utilities


def to_filename(name: str, tag: str, extension: str) -> str:
    """A safe download filename, '<name> [<tag>]<extension>': characters
    outside a conservative whitelist are replaced with underscores (the
    extension is appended verbatim)."""
    return re.sub(r"[^\w ?+,'()\[\]\-]", "_", f"{name} [{tag}]") + extension


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
        return proc.returncode == 0
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
