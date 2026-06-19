"""
Pure scanning logic for `firehose sample`, separated from I/O so it can be
tested without a terminal, network, clipboard, or clock.

`Scanner` turns a semantic command (see firehose.sample._key_to_command) into a
state change plus a list of declarative *effects*. The imperative shell in
firehose.sample executes those effects (logging, clipboard, download, ...) and
owns the wall clock. `render_frame` builds a screen from a Scanner; it formats
only, performing no I/O.
"""

import textwrap
from dataclasses import dataclass

import matthewplotlib as mp


# -- declarative effects -----------------------------------------------------
# Returned by the Scanner and executed by the shell. Plain dataclasses, so tests
# can assert on them by equality without mocking anything.

@dataclass
class Log:
    """Append this event to the scan log (the shell stamps it with a time)."""
    event: dict

@dataclass
class Clip:
    """Copy this text to the system clipboard."""
    text: str

@dataclass
class Open:
    """Open this URL with the platform browser/opener."""
    url: str

@dataclass
class Readlog:
    """Append this paper id to the read log (rdlog.txt)."""
    xid: str

@dataclass
class Download:
    """Download this paper's PDF."""
    xid: str
    xidv: str
    name: str

@dataclass
class DeletePDF:
    """Delete the PDF previously downloaded for this paper, if any."""
    xid: str


# -- a paper to scan ---------------------------------------------------------

@dataclass
class Paper:
    xid: str          # arxiv id without version, e.g. "2601.00001"
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


GLYPHS = {"none": " ", "saved": "☆", "downloaded": "★"}


# -- the scanning state machine (pure) ---------------------------------------

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

    @property
    def glyph(self):
        return GLYPHS[self.states[self.index]]

    def _arrive(self):
        # effects emitted when landing on the current paper
        effects = []
        if self.index > self.nseen:
            self.nseen = self.index
            effects.append(Readlog(self.xid))
        effects.append(Log({"type": "view", "xid": self.xid}))
        return effects

    def start(self):
        """Begin a session: a start event plus the first paper's arrival."""
        return [Log({"type": "start", "n": self.n})] + self._arrive()

    def feed(self, command):
        """Apply a semantic command and return the effects the shell must run."""
        self.message = ""

        # while paused, only resume and quit respond
        if self.paused:
            if command == "pause":
                self.paused = False
                return [Log({"type": "resume"})]
            if command == "quit":
                self.done = True
                return [Log({"type": "end"})]
            self.message = "paused — press space to resume"
            return []

        if command == "pause":
            self.paused = True
            return [Log({"type": "pause"})]

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
            return self._download() if self.state != "downloaded" else self._already()

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

    # -- action helpers --
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
            Log({"type": "download", "xid": self.xid}),
            Clip(f"- {self.current.name}\n"),
            Download(self.xid, self.current.xidv, self.current.name),
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


# -- rendering (formatting only, no I/O) -------------------------------------

def render_frame(scanner, timing_line):
    """Build the full terminal frame for the scanner's current paper."""
    p = scanner.current
    cats = ', '.join("\033[3m" + str(c) + "\033[0m" for c in p.categories)
    authors = ', '.join(str(a) for a in p.authors)
    lines = [
        '\033[2J\033[H',
        f"[{scanner.index + 1} / {scanner.n}] "
        f"{mp.progress((scanner.index + 1) / scanner.n, width=60)} {scanner.glyph}",
        timing_line,
        f"{p.entry_id} {cats}",
        f"published: {p.published} updated: {p.updated}",
        "\033[1m" + textwrap.fill(p.title, width=80) + "\033[0m",
        "\033[2m" + textwrap.fill(authors, width=80) + "\033[0m",
        textwrap.fill(p.summary, width=80),
        "",
    ]
    if p.comment is not None:
        lines.append(f"comment: {p.comment}")
    lines.append("")
    if scanner.message:
        lines.append(scanner.message)
    return "\n".join(lines)
