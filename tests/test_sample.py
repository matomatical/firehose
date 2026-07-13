"""
Tests for firehose.sample: the pure functional core (the Scanner state machine
and render_frame), the pure helpers (KEY_TO_COMMAND, select_papers), and the
session-state sinks (Scanlog/Readlog/Downloads) on plain files with mocked I/O.
No terminal, network, or clipboard.
"""

import datetime
import json
import random

import readchar

from firehose import util
from firehose.sample import (
    Scanner, Paper, Log, Clip, Open, MarkRead, Download, DeletePDF,
    PauseTimer, ResumeTimer, render_frame, KEY_TO_COMMAND, TRUNCATED_NOTICE,
    Session, Scanlog, Readlog, Downloads, Stopwatch, select_papers,
)


def mkpaper(i: int) -> Paper:
    xid = f"2601.{i:05d}"
    return Paper(
        xidv=xid + "v1",
        name=f"Author{i}2026 Title {i}",
        entry_id=f"http://arxiv.org/abs/{xid}v1",
        title=f"Title {i}",
        authors=["Ada Author", "Bo Boauthor"],
        categories=["cs.LG", "cs.AI"],
        summary="A summary.",
        published="2026-01-01",
        updated="2026-01-01",
        comment=None,
    )


def papers(n: int) -> list:
    return [mkpaper(i) for i in range(1, n + 1)]


def _d(day: int) -> datetime.date:
    """A date in May 2025 (after the modern cutoff), parameterised by day-of-month."""
    return datetime.date(2025, 5, day)


# -- key bindings --------------------------------------------------------------

def test_key_to_command_letters():
    assert KEY_TO_COMMAND.get("q") == "quit"
    assert KEY_TO_COMMAND.get("o") == "open"
    assert KEY_TO_COMMAND.get("s") == "save"
    assert KEY_TO_COMMAND.get("d") == "download"
    assert KEY_TO_COMMAND.get("x") == "remove"


def test_key_to_command_special_keys():
    assert KEY_TO_COMMAND.get(readchar.key.ESC) == "quit"
    assert KEY_TO_COMMAND.get(readchar.key.LEFT) == "back"
    assert KEY_TO_COMMAND.get(readchar.key.RIGHT) == "forward"
    assert KEY_TO_COMMAND.get(readchar.key.SPACE) == "pause"
    assert KEY_TO_COMMAND.get(readchar.key.UP) == "open"
    assert KEY_TO_COMMAND.get(readchar.key.DOWN) == "down"


def test_key_to_command_unknown_is_none():
    assert KEY_TO_COMMAND.get("z") is None
    assert KEY_TO_COMMAND.get("1") is None


# -- select_papers: filtering + ordering ---------------------------------------

def test_select_papers_default_takes_last_n_newest_first():
    cache = {f"p{i}": _d(i) for i in range(1, 6)}   # p1..p5 in cache order
    out = select_papers(cache, read=set(), n=2)
    # last two in cache order are p4, p5; returned reversed (newest first)
    assert [xid for xid, _ in out] == ["p5", "p4"]


def test_select_papers_backwards_takes_first_n_in_order():
    cache = {f"p{i}": _d(i) for i in range(1, 6)}
    out = select_papers(cache, read=set(), n=2, backwards=True)
    assert [xid for xid, _ in out] == ["p1", "p2"]


def test_select_papers_excludes_read():
    cache = {f"p{i}": _d(i) for i in range(1, 6)}
    out = select_papers(cache, read={"p4", "p5"}, n=2)
    # candidates are p1,p2,p3; last two reversed -> p3, p2
    assert [xid for xid, _ in out] == ["p3", "p2"]


def test_select_papers_modern_filters_on_or_before_cutoff():
    cache = {
        "older": datetime.date(2024, 1, 1),     # dropped
        "cutoff": datetime.date(2025, 4, 15),   # == cutoff, dropped (kept iff strictly after)
        "new1": datetime.date(2025, 4, 16),     # kept
        "new2": datetime.date(2025, 5, 1),      # kept
    }
    # with cutoff
    papers_with_cutoff = select_papers(
        cache,
        set(),
        n=10,
        cutoff=datetime.date(2025, 4, 15),
    )
    assert {xid for xid, _ in papers_with_cutoff} == {"new1", "new2"}
    # without cutoff
    papers_without_cutoff = select_papers(
        cache,
        set(),
        n=10,
        cutoff=None,
    )
    assert {xid for xid, _ in papers_without_cutoff} == {
        "older", "cutoff", "new1", "new2",
    }


def test_select_papers_offset_narrows_window_before_selecting():
    cache = {f"p{i}": _d(i) for i in range(1, 6)}
    # offset=3 -> last three candidates [p3,p4,p5]; backwards then takes first two
    out = select_papers(cache, set(), n=2, offset=3, backwards=True)
    assert [xid for xid, _ in out] == ["p3", "p4"]


def test_select_papers_randomise_is_deterministic_with_seeded_rng():
    cache = {f"p{i}": _d(i) for i in range(1, 6)}
    out1 = select_papers(cache, set(), n=3, randomise=True, rng=random.Random(0))
    out2 = select_papers(cache, set(), n=3, randomise=True, rng=random.Random(0))
    assert len(out1) == 3
    assert {xid for xid, _ in out1} <= {f"p{i}" for i in range(1, 6)}
    assert out1 == out2   # same seed -> same draw


def test_select_papers_n_zero_or_negative_returns_empty():
    # Guards the unread[-0:] == unread[:] trap: without the n<=0 short-circuit the
    # default branch would return *all* candidates for n=0 (and slice oddly for n<0).
    cache = {f"p{i}": _d(i) for i in range(1, 4)}
    assert select_papers(cache, set(), n=0) == []
    assert select_papers(cache, set(), n=-1) == []


# -- Scanner: arrival / session ------------------------------------------------

def test_start_emits_start_then_arrival():
    sc = Scanner(papers(2))
    fx = sc.start()
    assert fx == [
        Log({"type": "start", "n": 2}),
        MarkRead(sc.xid),
        Log({"type": "view", "xid": sc.xid}),
    ]
    assert sc.nseen == 0


# -- Scanner: save / download / remove state machine ---------------------------

def test_save_then_remove_no_pdf():
    sc = Scanner(papers(1)); sc.start()
    fx = sc.feed("save")
    assert sc.states[0] == "saved"
    assert fx == [Log({"type": "save", "xid": sc.xid}), Clip(f"- ? {sc.current.name}\n")]
    fx = sc.feed("remove")
    assert sc.states[0] == "none"
    assert fx == [Log({"type": "remove", "xid": sc.xid})]  # no DeletePDF: only saved


def test_download_then_remove_deletes_pdf():
    sc = Scanner(papers(1)); sc.start()
    fx = sc.feed("download")
    assert sc.states[0] == "downloaded"
    assert fx == [
        Log({"type": "download", "xid": sc.xid}),
        Clip(f"- {sc.current.name}\n"),
        Download(sc.xid, sc.current.xidv, sc.current.name),
    ]
    fx = sc.feed("remove")
    assert sc.states[0] == "none"
    assert Log({"type": "remove", "xid": sc.xid}) in fx
    assert DeletePDF(sc.xid) in fx


def test_down_is_progressive():
    sc = Scanner(papers(1)); sc.start()
    sc.feed("down")
    assert sc.states[0] == "saved"
    sc.feed("down")
    assert sc.states[0] == "downloaded"
    assert sc.feed("down") == []  # already downloaded


def test_save_when_saved_is_noop():
    sc = Scanner(papers(1)); sc.start()
    sc.feed("save")
    assert sc.feed("save") == []
    assert "already" in sc.message


def test_remove_when_none_is_noop():
    sc = Scanner(papers(1)); sc.start()
    assert sc.feed("remove") == []


def test_open_does_not_change_state():
    sc = Scanner(papers(1)); sc.start()
    fx = sc.feed("open")
    assert fx == [Open(sc.current.entry_id)]
    assert sc.states[0] == "none"


# -- Scanner: pause ------------------------------------------------------------

def test_pause_gates_actions_and_resume():
    sc = Scanner(papers(1)); sc.start()
    fx = sc.feed("pause")
    assert sc.paused and fx == [Log({"type": "pause"}), PauseTimer()]
    fx = sc.feed("save")            # gated while paused
    assert fx == [] and sc.states[0] == "none"
    fx = sc.feed("pause")           # space resumes
    assert not sc.paused and fx == [Log({"type": "resume"}), ResumeTimer()]


def test_quit_works_while_paused():
    sc = Scanner(papers(1)); sc.start()
    sc.feed("pause")
    fx = sc.feed("quit")
    assert sc.done and fx == [Log({"type": "end"})]


# -- Scanner: navigation -------------------------------------------------------

def test_forward_arrives_and_logs_new_paper():
    sc = Scanner(papers(2)); sc.start()
    fx = sc.feed("forward")
    assert sc.index == 1
    assert MarkRead(sc.xid) in fx
    assert Log({"type": "view", "xid": sc.xid}) in fx
    assert sc.nseen == 1


def test_forward_past_end_ends_session():
    sc = Scanner(papers(1)); sc.start()
    fx = sc.feed("forward")
    assert sc.done and fx == [Log({"type": "end"})]


def test_back_at_start_is_noop():
    sc = Scanner(papers(2)); sc.start()
    assert sc.feed("back") == [] and sc.index == 0


def test_revisit_does_not_relog_readlog():
    sc = Scanner(papers(2)); sc.start()    # arrive p0 (readlog)
    sc.feed("forward")                      # arrive p1 (readlog)
    fx = sc.feed("back")                    # back to p0: view but NOT readlog
    assert Log({"type": "view", "xid": sc.xid}) in fx
    assert not any(isinstance(e, MarkRead) for e in fx)


def test_quit_ends():
    sc = Scanner(papers(1)); sc.start()
    fx = sc.feed("quit")
    assert sc.done and fx == [Log({"type": "end"})]


def test_unknown_command_ignored():
    sc = Scanner(papers(1)); sc.start()
    assert sc.feed("frobnicate") == []


# -- render --------------------------------------------------------------------

def test_render_shows_state_glyph():
    sc = Scanner(papers(1)); sc.start()
    assert "Title 1" in render_frame(sc, 0.0)
    assert "☆" not in render_frame(sc, 0.0) and "★" not in render_frame(sc, 0.0)
    sc.feed("save")
    assert "☆" in render_frame(sc, 0.0)
    sc.feed("down")  # -> downloaded
    assert "★" in render_frame(sc, 0.0)


def _longpaper():
    p = mkpaper(1)
    p.summary = ("Lorem ipsum dolor sit amet. " * 40).strip()
    return p


def _rows(frame: str) -> int:
    # display rows the frame occupies (the \033[2J\033[H prefix has no newline)
    return frame.count("\n") + 1


def test_render_no_clip_when_rows_none():
    sc = Scanner([_longpaper()]); sc.start()
    frame = render_frame(sc, 0.0, rows=None)
    assert TRUNCATED_NOTICE not in frame
    assert "ipsum" in frame  # full abstract present


def test_render_truncates_when_frame_overflows():
    sc = Scanner([_longpaper()]); sc.start()
    frame = render_frame(sc, 0.0, rows=12)
    # clipped to at most rows-1 lines so print()'s newline can't scroll it
    assert _rows(frame) <= 11
    assert frame.rstrip().endswith(TRUNCATED_NOTICE)
    assert "Title 1" in frame  # header/title kept


def test_render_no_truncation_when_it_fits():
    sc = Scanner(papers(1)); sc.start()  # short "A summary."
    frame = render_frame(sc, 0.0, rows=40)
    assert TRUNCATED_NOTICE not in frame


def test_render_expanded_shows_full_frame_even_if_overflowing():
    sc = Scanner([_longpaper()]); sc.start()
    sc.feed("expand")
    frame = render_frame(sc, 0.0, rows=12)
    assert TRUNCATED_NOTICE not in frame
    assert "ipsum" in frame  # whole abstract emitted (it may scroll)


def test_render_truncated_keeps_action_message():
    sc = Scanner([_longpaper()]); sc.start()
    sc.feed("save")  # sets message "saved ☆"
    frame = render_frame(sc, 0.0, rows=12)
    assert TRUNCATED_NOTICE in frame
    assert "saved" in frame  # feedback survives clipping


# -- expand command ------------------------------------------------------------

def test_expand_toggles_and_emits_no_effects():
    sc = Scanner(papers(1)); sc.start()
    assert sc.expanded is False
    assert sc.feed("expand") == []
    assert sc.expanded is True
    assert sc.feed("expand") == []
    assert sc.expanded is False


def test_expand_works_while_paused():
    sc = Scanner(papers(1)); sc.start()
    sc.feed("pause")
    sc.feed("expand")
    assert sc.expanded is True and sc.paused is True


def test_navigation_resets_expanded():
    sc = Scanner(papers(2)); sc.start()
    sc.feed("expand")
    assert sc.expanded is True
    sc.feed("forward")
    assert sc.expanded is False


# -- effects: end-to-end through the session sinks (I/O mocked) ----------------

def test_effects_run_end_to_end(tmp_path, monkeypatch):
    # Drive the Scanner directly and run each emitted effect via its run(session)
    # method with I/O mocked -- exercises Scanner + effects + the Session managers
    # (Scanlog/Readlog/Downloads/Stopwatch) end to end, including the pause/resume
    # timer effects. (The key-read half is covered by the KEY_TO_COMMAND tests.)
    scanlog_path = tmp_path / "scanlog.jsonl"
    readlog_path = tmp_path / "readlog.txt"
    dl = tmp_path / "dl"

    monkeypatch.setattr(util, "copy_to_clipboard", lambda text: False)
    monkeypatch.setattr(util, "open_url", lambda url: False)
    monkeypatch.setattr(util, "download_paper",
                        lambda paper_id, path: open(path, "w").write("PDF"))

    sc = Scanner(papers(1))
    session = Session(
        scanlog=Scanlog(str(scanlog_path)),
        readlog=Readlog(str(readlog_path)),
        downloads=Downloads(str(dl)),
        stopwatch=Stopwatch(),
    )

    def run(effects):
        for effect in effects:
            effect.run(session)

    run(sc.start())
    for command in ["save", "remove", "pause", "pause", "download", "remove", "quit"]:
        run(sc.feed(command))

    events = [json.loads(line)["type"] for line in scanlog_path.open()]
    assert events == [
        "start", "view", "save", "remove", "pause", "resume",
        "download", "remove", "end",
    ]
    assert list(dl.rglob("*.pdf")) == []          # PDF downloaded then deleted
    assert list(util.load_readlog(str(readlog_path))[0]) == ["2601.00001"]  # logged once


# -- session sinks: Readlog / Downloads ----------------------------------------

def test_readlog_appends_grouped_across_dates(tmp_path):
    path = str(tmp_path / "readlog.txt")
    d1, d2 = datetime.date(2026, 6, 20), datetime.date(2026, 6, 21)
    rl = Readlog(path)
    rl.log("a", d1)
    rl.log("b", d1)   # same day -> no new header
    rl.log("c", d2)   # new day -> header
    assert open(path).read() == "2026-06-20:\na\nb\n2026-06-21:\nc\n"


def test_readlog_resume_continues_open_group(tmp_path):
    # a Readlog seeded with the readlog's last date (from load_readlog) continues
    # that day's group on resume instead of duplicating the header
    path = str(tmp_path / "readlog.txt")
    d = datetime.date(2026, 6, 21)
    Readlog(path).log("a", d)
    _, open_date = util.load_readlog(path)
    Readlog(path, open_date).log("b", d)   # resumed session, seeded from the file
    assert open(path).read() == "2026-06-21:\na\nb\n"


def _stub_downloader(monkeypatch):
    monkeypatch.setattr(
        util, "download_paper", lambda paper_id, path: open(path, "w").write("PDF")
    )


def test_downloads_dedups_on_filename_collision(tmp_path, monkeypatch):
    _stub_downloader(monkeypatch)
    dl = Downloads(str(tmp_path))
    dl.download("2601.1", "Smith2026 A", "2601.1v1")
    dl.download("2601.1", "Smith2026 A", "2601.1v1")   # identical -> "(duplicate)"
    pdfs = [p.name for p in tmp_path.rglob("*.pdf")]
    assert len(pdfs) == 2 and any("(duplicate)" in n for n in pdfs)


def test_downloads_delete_removes_tracked_file(tmp_path, monkeypatch):
    _stub_downloader(monkeypatch)
    dl = Downloads(str(tmp_path))
    dl.download("2601.1", "Smith2026 A", "2601.1v1")
    assert list(tmp_path.rglob("*.pdf"))               # downloaded
    dl.delete("2601.1")
    assert list(tmp_path.rglob("*.pdf")) == []         # removed
    dl.delete("2601.1")                                # unknown id -> no error
