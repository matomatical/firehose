"""
The `firehose sample` command: select a batch of unseen papers from the
store and present their abstracts to scan, recording views / saves /
downloads.

* `sample()` is the entry point and describes the end-to-end pipeline at a
  high level, selecting papers and then presenting them in sequence.
* The sequence presentation is driven by a functional core `Scanner` state
  machine taking commands and issuing effects, plus a pure render function
  `render_frame`.
* Side-effects are carried out by each effect's `run()` method, acting on a
  `Session` bundle: the store (which records events) and stateful managers
  (`Downloads`, `Stopwatch`).
"""

import datetime
import os
import shutil
import textwrap
import time
from dataclasses import dataclass

import matthewplotlib as mp
import readchar

from firehose import ids
from firehose import sources
from firehose import util
from firehose import vis
from firehose.store import make_store


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
    source: str | None = None,
    # config
    config_path: str = util.CONFIG_PATH,
    data_dir: str | None = None,
    download_dir: str | None = None,
):
    """
    Select and present abstracts for a batch of unseen papers.

    With --no-modern, include each source's backlog (papers dated on or
    before its configured cutoff); with --source, narrow to one source.
    With --no-query, stop after previewing the selection's calendar.
    """
    config = util.load_config(config_path)
    download_dir = download_dir or config["paths"]["downloads"]
    store = make_store(config, data_dir=data_dir)

    print("selecting papers to scan...")
    papers = store.select_papers(
        n,
        backwards=backwards,
        randomise=randomise,
        offset=offset,
        modern=modern,
        source=source,
    )
    print(f"selected {len(papers)} papers to scan")

    print("visualising on calendar...")
    toread_dates = [p.published.date() for p in papers if p.published]
    print(vis.vis_dates(toread_dates))

    if not query:
        print("exiting.")
        return

    if len(papers) == 0:
        print("no papers to show.")
        return

    print("press q to cancel or anything else to start.")
    if readchar.readkey() == "q":
        return

    # start scanning loop!
    sc = Scanner(papers)
    session = Session(
        store=store,
        downloads=Downloads(download_dir),
        stopwatch=Stopwatch(),
    )
    try:
        run_effects(sc, sc.start(), session)
        while not sc.done:
            # measure the terminal each frame so a mid-scan resize is
            # respected; shutil (not os) falls back to 80x24 off a TTY
            # instead of raising.
            rows = shutil.get_terminal_size().lines
            print(render_frame(sc, session.stopwatch.elapsed(), rows=rows))
            command = KEY_TO_COMMAND.get(readchar.readkey())
            if command is None:
                continue
            run_effects(sc, sc.feed(command), session)
    finally:
        # settle event delivery (in remote mode, recording is asynchronous)
        # even when the scan loop dies mid-session
        store.close()
    print("done!")


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
    def id(self):
        """The current paper's namespaced id (what events record)."""
        return self.current.id

    @property
    def state(self):
        return self.states[self.index]

    def _arrive(self):
        # effects emitted when landing on the current paper
        self.expanded = False   # each new paper starts collapsed
        if self.index > self.nseen:
            self.nseen = self.index
        return [Log({"type": "view", "id": self.id})]

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
            Log({"type": "save", "id": self.id}),
            Clip(f"- ? {self.current.name}\n"),
        ]

    def _download(self):
        self.states[self.index] = "downloaded"
        self.message = "downloading..."
        return [
            # Commit the external effect before recording/copying success. If
            # the download raises, the remaining effects are never run.
            Download(self.current),
            Log({"type": "download", "id": self.id}),
            Clip(f"- {self.current.name}\n"),
        ]

    def clipboard_finished(self, copied: bool):
        """Add clipboard delivery feedback to the current action message."""
        detail = "copied to clipboard" if copied else "clipboard not available"
        self.message += f" ({detail})"

    def download_finished(self, completed_progress: str):
        """Replace the in-progress message with the completed download bar."""
        self.message = completed_progress

    def _remove(self):
        was = self.states[self.index]
        self.states[self.index] = "none"
        self.message = "removed"
        effects = [Log({"type": "remove", "id": self.id})]
        if was == "downloaded":
            effects.append(DeleteDownload(self.id))
        return effects

    def _already(self):
        self.message = f"already {self.state}"
        return []

    def _nothing(self):
        self.message = "nothing to remove"
        return []


# # # 
# Render scanning state


GLYPHS = {"none": " ", "saved": "☆", "downloaded": "★"}


def _datestamp(instant) -> str:
    """The date of a paper's published/updated instant, for the header
    line (scanning doesn't need the time of day); "?" when the feed
    carried no date."""
    if instant is None:
        return "?"
    if isinstance(instant, datetime.datetime):
        return instant.date().isoformat()
    return str(instant)


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
    authors = ', '.join(str(a) for a in p.authors)
    seen = scanner.nseen + 1
    average = elapsed / seen if seen > 0 else 0.0
    glyph = GLYPHS[scanner.state]

    # id + categories, wrapped on visible width: fill the *plain* text (so the
    # SGR codes never inflate the measured width), counting the id via
    # initial_indent, then wrap the category portion in a single italic pair —
    # italic persists across the internal newlines, as the authors block does.
    id_prefix = f"{p.entry_id} "
    wrapped_cats = textwrap.fill(
        ', '.join(str(c) for c in p.categories),
        width=80,
        initial_indent=id_prefix,
        subsequent_indent="",
    )                                       # "" when there are no categories
    cats_line = id_prefix + "\033[3m" + wrapped_cats[len(id_prefix):] + "\033[0m"

    # header: the scanning essentials, kept whenever the frame is clipped
    header = [
        f"[{scanner.index + 1} / {scanner.n}] "
        f"{mp.progress((scanner.index + 1) / scanner.n, width=60)} {glyph}",
        f"{datetime.timedelta(seconds=int(elapsed))} ({average:.2f} seconds/paper)"
        + (" — PAUSED (space to resume)" if scanner.paused else ""),
        cats_line,
        f"published: {_datestamp(p.published)} updated: {_datestamp(p.updated)}",
        "\033[1m" + textwrap.fill(p.title, width=80) + "\033[0m",
        "\033[2m" + textwrap.fill(authors, width=80) + "\033[0m",
    ]
    body = [*header, textwrap.fill(p.summary, width=80), ""]
    if p.comment is not None:
        body.append(textwrap.fill(f"comment: {p.comment}", width=80))
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
        main += ["", textwrap.fill(f"comment: {p.comment}", width=80)]
    main_rows = "\n".join(main).split("\n")
    keep = max(0, budget - len(tail))
    return prefix + "\n".join(main_rows[:keep] + tail)


# # # 
# System state managers


@dataclass
class Session:
    """The store and stateful per-session managers the effects act on,
    bundled so each effect's run() takes a single context argument."""
    store: object
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


class Downloads:
    """
    Tracks the files grabbed during a scan session so a later undo can
    remove them, keyed by namespaced paper id. Each paper's source adapter
    names its file and fetches its content; this manager owns the
    filesystem policy — files land in <download_dir>/<YYYY-MM>/,
    de-duplicated with a "(duplicate)" suffix.
    """

    def __init__(self, download_dir):
        self.download_dir = os.path.expanduser(download_dir)
        self._paths = {}

    def download(self, paper):
        adapter = sources.adapter(ids.source(paper.id))
        dirpath = os.path.join(
            self.download_dir, datetime.date.today().strftime("%Y-%m"),
        )
        stem, extension = os.path.splitext(adapter.filename(paper))
        path = os.path.join(dirpath, stem + extension)
        os.makedirs(dirpath, exist_ok=True)
        while os.path.exists(path):
            stem = f"{stem} (duplicate)"
            path = os.path.join(dirpath, stem + extension)
        message = adapter.grab(paper, path)
        self._paths[paper.id] = path
        return message

    def delete(self, paper_id):
        path = self._paths.pop(paper_id, None)
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
    """Record this event in the store (stamped with a time on write)."""
    event: dict

    def run(self, session):
        session.store.record_events([self.event])


@dataclass
class Clip:
    """Copy this text to the system clipboard."""
    text: str

    def run(self, session):
        return util.copy_to_clipboard(self.text)


@dataclass
class Open:
    """Open this URL with the platform browser/opener."""
    url: str

    def run(self, session):
        if not util.open_url(self.url):
            print(f"no opener available; url: {self.url}")


@dataclass
class Download:
    """Grab this paper's full content through its source's adapter."""
    paper: object

    def run(self, session):
        return session.downloads.download(self.paper)


@dataclass
class DeleteDownload:
    """Delete the file previously grabbed for this paper, if any."""
    paper_id: str

    def run(self, session):
        session.downloads.delete(self.paper_id)


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


def run_effects(scanner: Scanner, effects, session: Session):
    """Run effects and feed externally observed outcomes back to the scanner."""
    for effect in effects:
        result = effect.run(session)
        if isinstance(effect, Download):
            scanner.download_finished(result)
        elif isinstance(effect, Clip):
            scanner.clipboard_finished(result)
