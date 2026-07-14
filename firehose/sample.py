"""
The `firehose sample` command: download a batch of arXiv abstracts and present
them to scan, recording views / saves / downloads.

* `sample()` is the entry point and describes the end-to-end pipeline at a high
  level, loading papers and then presenting them in sequence.
* The sequence presentation is driven by a functional core `Scanner` state
  machine taking commands and issuing effects, plus a pure render function
  `render_frame`.
* Side-effects are carried out by each effect's `run()` method, acting on a
  `Session` bundle of stateful managers (`Scanlog`, `Readlog`, `Downloads`,
  `Stopwatch`).
"""

import datetime
import os
import random
import shutil
import textwrap
import time
from dataclasses import dataclass

import arxiv
import matthewplotlib as mp
import readchar
import tqdm

from firehose import util
from firehose import vis


# semantic scan commands keyed by raw keypress (readchar key constants are
# escape-sequence strings; an unmapped key -> None via .get, and is ignored)
KEY_TO_COMMAND = {
    # quit
    "q": "quit",
    readchar.key.ESC: "quit",
    # navigation
    readchar.key.LEFT: "back",
    readchar.key.RIGHT: "forward",
    # toggle timer
    readchar.key.SPACE: "pause",
    # interact with a paper
    "o": "open",
    readchar.key.UP: "open",
    readchar.key.DOWN: "down", # first save, then download
    "s": "save",
    "d": "download",
    "x": "remove",
    # expand a truncated frame to see the full abstract (toggle)
    "e": "expand",
}


# # # 
# Entry-point


def sample(
    n: int = 100,
    /,
    query: bool = True,
    # paper selection
    backwards: bool = False,
    randomise: bool = False,
    offset: int | None = None,
    modern: bool = True,
    # arxiv api interaction
    query_batch_size: int = 100,
    query_wait_time: float = 3.5,
    # config
    config_path: str = util.CONFIG_PATH,
    data_dir: str | None = None,
    download_dir: str | None = None,
):
    """
    Download and present abstracts for a batch of papers.
    """
    config = util.load_config(config_path)
    paths = util.data_paths(config, data_dir=data_dir)
    util.ensure_data_dir(paths)  # readlog/scanlog get written during the scan
    download_dir = download_dir or config["paths"]["downloads"]

    # load cached headers with overlapping classes
    print("loading papers from disk...")
    cache, _ = util.load_cache(path=paths.cache)
    print(f"loaded {len(cache)} papers")

    # load read papers from read log
    print("checking which have already been read...")
    readlog, last_read_date = util.load_readlog(path=paths.readlog)
    read = set(readlog)
    print(f"loaded {len(read)} already-read papers")

    # select which papers to scan
    print("selecting papers to scan...")
    toread = select_papers(
        cache,
        read,
        n=n,
        backwards=backwards,
        randomise=randomise,
        offset=offset,
        cutoff=config["scan"]["modern_cutoff"] if modern else None,
    )
    print(f"selected {len(toread)} papers to scan")

    print("visualising on calendar...")
    toread_dates = [date for xid, date in toread]
    print(vis.vis_dates(toread_dates))

    if not query:
        print("exiting.")
        return

    # run the query
    print("querying the API to get metadata for these papers...")
    client = arxiv.Client(num_retries=0)
    toread_xids = [xid for xid, _ in toread]
    results = []
    bar = tqdm.tqdm(
        total=len(toread_xids),
        unit="paper",
        ncols=80,
    )
    for cursor in range(0, len(toread_xids), query_batch_size):
        search = arxiv.Search(
            id_list=toread_xids[cursor:cursor+query_batch_size],
            max_results=query_batch_size,
        )
        try:
            new_results = list(client.results(search))
        except arxiv.HTTPError as e:
            print(e)
            raise e
        results.extend(new_results)
        bar.update(len(new_results))
        if cursor+query_batch_size < len(toread_xids):
            time.sleep(query_wait_time)
    bar.close()

    print("reordering results")
    results_by_xid = {
        r.entry_id[len("http://arxiv.org/abs/"):].split('v')[0]: r
        for r in results
    }
    results_sorted = [
        results_by_xid[xid]
        for xid in toread_xids
        if xid in results_by_xid
    ]

    if len(results_sorted) == 0:
        print("no papers to show.")
        return

    print("query complete. press q to cancel or anything else to start.")
    if readchar.readkey() == "q":
        return

    # start scanning loop!
    papers = [Paper.from_arxiv_result(r) for r in results_sorted]
    sc = Scanner(papers)
    session = Session(
        scanlog=Scanlog(paths.scanlog),
        readlog=Readlog(paths.readlog, last_read_date),
        downloads=Downloads(download_dir),
        stopwatch=Stopwatch(),
    )
    for effect in sc.start():
        effect.run(session)
    while not sc.done:
        # measure the terminal each frame so a mid-scan resize is respected;
        # shutil (not os) falls back to 80x24 off a TTY instead of raising.
        rows = shutil.get_terminal_size().lines
        print(render_frame(sc, session.stopwatch.elapsed(), rows=rows))
        command = KEY_TO_COMMAND.get(readchar.readkey())
        if command is None:
            continue
        for effect in sc.feed(command):
            effect.run(session)
    print("done!")


# # # 
# Paper selection algorithm


def select_papers(
    cache: dict[str, datetime.date],
    read: set[str],
    *,
    n: int,
    backwards: bool = False,
    randomise: bool = False,
    offset: int | None = None,
    cutoff: datetime.date | None = None,
    rng=random,
) -> list[tuple[str, datetime.date]]:
    """
    Choose which (xid, date) papers to scan from the cache.

    Drops already-read ids, then (when a `cutoff` is given) papers dated on or
    before `cutoff`, then takes a window of size `n`:

      * default:        the last `n` candidates, reversed (newest first);
      * backwards=True:  the first `n` candidates, in cache order (oldest first);
      * randomise=True:  up to `n` candidates drawn at random via `rng`.

    `offset`, when given, first narrows to the last `offset` candidates (paging
    back through older unread papers); `n <= 0` selects nothing. Pure: no I/O,
    clock, or global RNG — pass a seeded `rng` for deterministic sampling in
    tests.
    """
    if n <= 0:
        return []
    unread = [(xid, date) for xid, date in cache.items() if xid not in read]
    if cutoff is not None:
        unread = [(xid, date) for xid, date in unread if date > cutoff]
    if offset is not None:
        unread = unread[-offset:]
    if backwards:
        return unread[:n]
    if randomise:
        return rng.sample(unread, min(n, len(unread)))
    return unread[-n:][::-1]


# # # 
# Pure scanning loop state machine


class Scanner:
    """
    Tracks scanning state (position, per-paper save/download state, pause) and
    maps semantic commands to effects. No I/O, no clock, no randomness.

    Per-paper state advances none -> saved (☆) -> downloaded (★); `remove`
    returns it to none. Commands: back, forward, open, save, download, down
    (progressive save-then-download), remove, pause, quit.
    """

    def __init__(self, papers):
        self.papers = list(papers)
        self.n = len(self.papers)
        self.index = 0
        self.states = ["none"] * self.n     # per paper: none | saved | downloaded
        self.nseen = -1                     # highest index reached so far
        self.paused = False
        self.done = False
        self.expanded = False               # show full frame (past screen edge)
        self.message = ""

    @property
    def current(self):
        return self.papers[self.index]

    @property
    def xid(self):
        return self.current.xid

    @property
    def state(self):
        return self.states[self.index]

    def _arrive(self):
        # effects emitted when landing on the current paper
        self.expanded = False   # each new paper starts collapsed
        effects = []
        if self.index > self.nseen:
            self.nseen = self.index
            effects.append(MarkRead(self.xid))
        effects.append(Log({"type": "view", "xid": self.xid}))
        return effects

    def start(self):
        """Begin a session: a start event plus the first paper's arrival."""
        return [Log({"type": "start", "n": self.n})] + self._arrive()

    def feed(self, command):
        """Apply a semantic command and return the effects the shell must run."""
        self.message = ""

        # expand/collapse is a view-only toggle: it works whether running or
        # paused, touches no timer, and emits no effects (the loop re-renders).
        if command == "expand":
            self.expanded = not self.expanded
            return []

        # while paused, only resume and quit respond
        if self.paused:
            if command == "pause":
                self.paused = False
                return [Log({"type": "resume"}), ResumeTimer()]
            if command == "quit":
                self.done = True
                return [Log({"type": "end"})]
            self.message = "paused — press space to resume"
            return []

        if command == "pause":
            self.paused = True
            return [Log({"type": "pause"}), PauseTimer()]

        if command == "quit":
            self.done = True
            return [Log({"type": "end"})]

        if command == "back":
            if self.index > 0:
                self.index -= 1
                return self._arrive()
            return []

        if command == "forward":
            if self.index + 1 == self.n:
                self.done = True
                return [Log({"type": "end"})]
            self.index += 1
            return self._arrive()

        if command == "open":
            return [Open(self.current.entry_id)]

        if command == "save":
            return self._save() if self.state == "none" else self._already()

        if command == "download":
            if self.state != "downloaded":
                return self._download()
            else:
                return self._already()

        if command == "down":
            # progressive: none -> saved, saved -> downloaded
            if self.state == "none":
                return self._save()
            if self.state == "saved":
                return self._download()
            return self._already()

        if command == "remove":
            return self._remove() if self.state != "none" else self._nothing()

        return []  # unknown command: ignored

    # action helpers
    def _save(self):
        self.states[self.index] = "saved"
        self.message = "saved ☆"
        return [
            Log({"type": "save", "xid": self.xid}),
            Clip(f"- ? {self.current.name}\n"),
        ]

    def _download(self):
        self.states[self.index] = "downloaded"
        self.message = "downloaded ★"
        return [
            # Commit the external effect before recording/copying success. If
            # the download raises, the remaining effects are never run.
            Download(self.xid, self.current.xidv, self.current.name),
            Log({"type": "download", "xid": self.xid}),
            Clip(f"- {self.current.name}\n"),
        ]

    def _remove(self):
        was = self.states[self.index]
        self.states[self.index] = "none"
        self.message = "removed"
        effects = [Log({"type": "remove", "xid": self.xid})]
        if was == "downloaded":
            effects.append(DeletePDF(self.xid))
        return effects

    def _already(self):
        self.message = f"already {self.state}"
        return []

    def _nothing(self):
        self.message = "nothing to remove"
        return []


# # # 
# Paper object


@dataclass
class Paper:
    xidv: str         # arxiv id with version, e.g. "2601.00001v1"
    name: str         # util.to_name(result): "Author+Year Title"
    entry_id: str
    title: str
    authors: list
    categories: list
    summary: str
    published: object
    updated: object
    comment: object

    @property
    def xid(self) -> str:
        """ArXiv id without version, e.g. "2601.00001"."""
        # TODO: might break for old ids?
        return self.xidv.split('v')[0]

    @classmethod
    def from_arxiv_result(cls, r) -> "Paper":
        """
        Build a Paper from an arxiv API result object.
        """
        xidv = r.entry_id[len("http://arxiv.org/abs/"):]
        return cls(
            xidv=xidv,
            name=util.to_name(r),
            entry_id=r.entry_id,
            title=r.title,
            authors=[str(a) for a in r.authors],
            categories=[str(c) for c in r.categories],
            summary=r.summary,
            published=r.published,
            updated=r.updated,
            comment=r.comment,
        )


# # # 
# Render scanning state


GLYPHS = {"none": " ", "saved": "☆", "downloaded": "★"}


TRUNCATED_NOTICE = "\033[2m[Truncated... press 'e' to expand]\033[0m"


def render_frame(scanner, elapsed: float, *, rows: int | None = None):
    """
    Build the full terminal frame for the scanner's current paper.

    The frame is anchored to the top of the terminal: it is clipped to `rows`
    display lines so the trailing newline print() adds never lands on the
    bottom row (which would scroll the frame, dragging the header/title/authors
    off the top and leaving only the abstract tail on screen). When the frame
    would overflow, the abstract tail is dropped and a self-documenting notice
    is shown on the last line; pressing 'e' sets `scanner.expanded`, which
    renders the full frame instead (letting it scroll, so the whole abstract is
    reachable via the terminal's own scrollback). `rows=None` disables clipping
    (used by tests / non-interactive callers).
    """
    p = scanner.current
    cats = ', '.join("\033[3m" + str(c) + "\033[0m" for c in p.categories)
    authors = ', '.join(str(a) for a in p.authors)
    seen = scanner.nseen + 1
    average = elapsed / seen if seen > 0 else 0.0
    glyph = GLYPHS[scanner.state]

    # header: the scanning essentials, kept whenever the frame is clipped
    header = [
        f"[{scanner.index + 1} / {scanner.n}] "
        f"{mp.progress((scanner.index + 1) / scanner.n, width=60)} {glyph}",
        f"{datetime.timedelta(seconds=int(elapsed))} ({average:.2f} seconds/paper)"
        + (" — PAUSED (space to resume)" if scanner.paused else ""),
        f"{p.entry_id} {cats}",
        f"published: {p.published} updated: {p.updated}",
        "\033[1m" + textwrap.fill(p.title, width=80) + "\033[0m",
        "\033[2m" + textwrap.fill(authors, width=80) + "\033[0m",
    ]
    body = [*header, textwrap.fill(p.summary, width=80), ""]
    if p.comment is not None:
        body.append(f"comment: {p.comment}")
    body.append("")
    if scanner.message:
        body.append(scanner.message)

    prefix = "\033[2J\033[H"
    full = "\n".join(body)

    # keep one row spare so print()'s trailing newline can't trigger a scroll
    budget = None if rows is None else max(1, rows - 1)
    n_lines = full.count("\n") + 1
    if scanner.expanded or budget is None or n_lines <= budget:
        return prefix + full

    # overflow + collapsed: keep the top, drop the abstract tail, and end with
    # a notice (plus any transient message) on the bottom rows.
    tail = ([scanner.message] if scanner.message else []) + [TRUNCATED_NOTICE]
    main = [*header, textwrap.fill(p.summary, width=80)]
    if p.comment is not None:
        main += ["", f"comment: {p.comment}"]
    main_rows = "\n".join(main).split("\n")
    keep = max(0, budget - len(tail))
    return prefix + "\n".join(main_rows[:keep] + tail)


# # # 
# System state managers


@dataclass
class Session:
    """The stateful per-session managers the effects act on, bundled so each
    effect's run() takes a single context argument."""
    scanlog: "Scanlog"
    readlog: "Readlog"
    downloads: "Downloads"
    stopwatch: "Stopwatch"


class Stopwatch:
    """
    Wall-clock stopwatch that can be paused; drives the live dwell average.
    """

    def __init__(self):
        self._accum = 0.0
        self._segment_start = time.time()
        self._paused = False

    def elapsed(self) -> float:
        if self._paused:
            return self._accum
        return self._accum + (time.time() - self._segment_start)

    def set_paused(self, paused: bool):
        if paused and not self._paused:
            self._accum += time.time() - self._segment_start
            self._paused = True
        elif not paused and self._paused:
            self._segment_start = time.time()
            self._paused = False


class Scanlog:
    """
    The scan event log for a session: appends each event as a JSON line to
    scanlog.jsonl (util.log_event stamps it with the time on write).
    """

    def __init__(self, path):
        self.path = path

    def log(self, event):
        util.log_event(self.path, event)


class Readlog:
    """
    The seen-index for a scan session: appends each viewed id in grouped form
    (a "<date>:" header only when the day changes). Seeded with the readlog's
    last date (from load_readlog) so a same-day resume continues that group.
    """

    def __init__(self, path, open_date=None):
        self.path = path
        self._open_date = open_date

    def log(self, xid, date):
        self._open_date = util.append_readlog(self.path, xid, date, self._open_date)


class Downloads:
    """
    Tracks the PDFs grabbed during a scan session so a later undo can remove
    them. Files land in <download_dir>/<YYYY-MM>/ with names from
    util.to_filename, de-duplicated with a "(duplicate)" suffix.
    """

    def __init__(self, download_dir):
        self.download_dir = os.path.expanduser(download_dir)
        self._paths = {}

    def download(self, xid, name, xidv):
        dirpath = os.path.join(self.download_dir, datetime.date.today().strftime("%Y-%m"))
        filename = util.to_filename(name, xidv)
        path = os.path.join(dirpath, filename)
        os.makedirs(dirpath, exist_ok=True)
        while os.path.exists(path):
            filename = f"{filename[:-4]} (duplicate).pdf"
            path = os.path.join(dirpath, filename)
        util.download_paper(paper_id=xid, path=path)
        self._paths[xid] = path

    def delete(self, xid):
        path = self._paths.pop(xid, None)
        if path and os.path.exists(path):
            os.remove(path)


# # #
# Declarative effect objects
#
# The Scanner emits these inert data objects; the shell runs each effect's
# run(session) method, which performs the side effect via the Session's stateful
# managers (each effect uses the parts it needs).


@dataclass
class Log:
    """Append this event to the scan log (stamped with a time on write)."""
    event: dict

    def run(self, session):
        session.scanlog.log(self.event)


@dataclass
class Clip:
    """Copy this text to the system clipboard."""
    text: str

    def run(self, session):
        util.copy_to_clipboard(self.text)


@dataclass
class Open:
    """Open this URL with the platform browser/opener."""
    url: str

    def run(self, session):
        if not util.open_url(self.url):
            print(f"no opener available; url: {self.url}")


@dataclass
class MarkRead:
    """Append this paper id to the read log (readlog.txt)."""
    xid: str

    def run(self, session):
        session.readlog.log(self.xid, datetime.date.today())


@dataclass
class Download:
    """Download this paper's PDF."""
    xid: str
    xidv: str
    name: str

    def run(self, session):
        session.downloads.download(self.xid, self.name, self.xidv)


@dataclass
class DeletePDF:
    """Delete the PDF previously downloaded for this paper, if any."""
    xid: str

    def run(self, session):
        session.downloads.delete(self.xid)


@dataclass
class PauseTimer:
    """Pause the dwell timer (space while running)."""

    def run(self, session):
        session.stopwatch.set_paused(True)


@dataclass
class ResumeTimer:
    """Resume the dwell timer (space while paused)."""

    def run(self, session):
        session.stopwatch.set_paused(False)
