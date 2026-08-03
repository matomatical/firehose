"""
Tests for firehose.sample: the pure functional core (the Scanner state machine
and render_frame), the key bindings (KEY_TO_COMMAND), and the effects run
end-to-end against a store and the session managers (Downloads/Stopwatch) on
plain files with mocked I/O. No terminal, network, or clipboard.
"""

import json
import re
import textwrap
import types

import pytest
import readchar

from firehose import util
from firehose.paper import Paper
from firehose.sample import (
    Scanner, Log, Clip, Open, Download, DeletePDF,
    PauseTimer, ResumeTimer, render_frame, KEY_TO_COMMAND, TRUNCATED_NOTICE,
    Session, Downloads, Stopwatch, run_effects,
)
from firehose.store import LocalStore


def mksession(tmp_path) -> Session:
    """A Session over a real LocalStore writing into tmp_path (the store's
    index and mirror are never touched by the effect tests)."""
    paths = types.SimpleNamespace(
        events=str(tmp_path / "events.jsonl"),
        index=str(tmp_path / "index.txt"),
        mirror=str(tmp_path / "metadata"),
    )
    return Session(
        store=LocalStore(paths, subscribed=set()),
        downloads=Downloads(str(tmp_path / "dl")),
        stopwatch=Stopwatch(),
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


# -- Scanner: arrival / session ------------------------------------------------

def test_start_emits_start_then_arrival():
    sc = Scanner(papers(2))
    fx = sc.start()
    assert fx == [
        Log({"type": "start", "n": 2}),
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
        Download(sc.xid, sc.current.xidv, sc.current.name),
        Log({"type": "download", "xid": sc.xid}),
        Clip(f"- {sc.current.name}\n"),
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
    assert Log({"type": "view", "xid": sc.xid}) in fx
    assert sc.nseen == 1


def test_forward_past_end_ends_session():
    sc = Scanner(papers(1)); sc.start()
    fx = sc.feed("forward")
    assert sc.done and fx == [Log({"type": "end"})]


def test_back_at_start_is_noop():
    sc = Scanner(papers(2)); sc.start()
    assert sc.feed("back") == [] and sc.index == 0


def test_revisit_logs_view_without_advancing_frontier():
    sc = Scanner(papers(2)); sc.start()    # arrive p0
    sc.feed("forward")                      # arrive p1
    fx = sc.feed("back")                    # back to p0: a view, nseen stays
    assert fx == [Log({"type": "view", "xid": sc.xid})]
    assert sc.nseen == 1


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


def test_render_wraps_long_comment():
    # a long comment must be wrapped like the abstract, so it occupies its true
    # number of display rows and the frame's line count stays honest (otherwise
    # a long one-line comment undercounts and throws off truncation).
    p = mkpaper(1)
    p.comment = "This is a lengthy comment. " * 10
    sc = Scanner([p]); sc.start()
    frame = render_frame(sc, 0.0, rows=None)
    wrapped = textwrap.fill(f"comment: {p.comment}", width=80)
    assert "\n" in wrapped              # the fixture is genuinely multi-line
    assert wrapped in frame             # emitted wrapped, verbatim
    assert f"comment: {p.comment}" not in frame  # never emitted as one line


def _strip_ansi(s: str) -> str:
    return re.sub(r"\033\[[0-9;]*[A-Za-z]", "", s)


def test_render_wraps_many_categories_on_visible_width():
    # the id + categories line must wrap on visible width (control codes not
    # counted), and the id must sit outside the italic (categories only).
    p = mkpaper(1)
    p.categories = ["cs.LG", "cs.AI", "cs.CL", "stat.ML", "math.OC",
                    "cs.NE", "cs.CV", "eess.SP", "cs.RO"]
    sc = Scanner([p]); sc.start()
    frame = render_frame(sc, 0.0, rows=None)
    id_lines = [ln for ln in frame.split("\n") if p.entry_id in ln]
    # the id anchors the first physical line; the categories spill onto more
    assert len(id_lines) == 1 and id_lines[0].startswith(p.entry_id)
    assert "\033[3m" in id_lines[0] and not id_lines[0].startswith("\033[3m")
    # every physical line stays within 80 columns once styling is stripped
    assert all(len(_strip_ansi(ln)) <= 80 for ln in frame.split("\n"))
    # more than one physical row is devoted to the category run
    cat_rows = [ln for ln in frame.split("\n")
                if any(c in _strip_ansi(ln) for c in ("eess.SP", "cs.RO"))]
    assert cat_rows and all(p.entry_id not in ln for ln in cat_rows)


def test_render_truncation_accounts_for_wrapped_comment():
    # with a comment long enough to overflow, the frame must still clip to the
    # row budget rather than trusting a bogus single-line count.
    p = mkpaper(1)
    p.comment = "Accepted at Some Conference 2026. " * 8
    sc = Scanner([p]); sc.start()
    frame = render_frame(sc, 0.0, rows=12)
    assert _rows(frame) <= 11


@pytest.mark.parametrize(
    ("copied", "detail"),
    [(True, "copied to clipboard"), (False, "clipboard not available")],
)
def test_save_message_reports_clipboard_outcome(tmp_path, monkeypatch, copied, detail):
    monkeypatch.setattr(util, "copy_to_clipboard", lambda text: copied)
    sc = Scanner(papers(1)); sc.start()
    session = mksession(tmp_path)

    run_effects(sc, sc.feed("save"), session)

    assert sc.state == "saved"
    assert sc.message == f"saved ☆ ({detail})"


def test_download_message_keeps_complete_progress_bar(tmp_path, monkeypatch):
    completed_progress = "downloaded ★: 100%|████| 4/4 bytes"

    def download(paper_id, path):
        open(path, "w").write("PDF")
        return completed_progress

    monkeypatch.setattr(
        util, "download_paper", download,
    )
    monkeypatch.setattr(util, "copy_to_clipboard", lambda text: False)
    sc = Scanner(papers(1)); sc.start()
    session = mksession(tmp_path)

    effects = sc.feed("download")
    assert sc.message == "downloading..."
    run_effects(sc, effects, session)

    assert sc.message == f"{completed_progress} (clipboard not available)"


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
    # method with I/O mocked -- exercises Scanner + effects + the store and
    # Session managers (Downloads/Stopwatch) end to end, including the
    # pause/resume timer effects. (The key-read half is covered by the
    # KEY_TO_COMMAND tests.)
    monkeypatch.setattr(util, "copy_to_clipboard", lambda text: False)
    monkeypatch.setattr(util, "open_url", lambda url: False)
    def download(paper_id, path):
        open(path, "w").write("PDF")
        return "downloaded ★: 100%|████|"

    monkeypatch.setattr(util, "download_paper", download)

    sc = Scanner(papers(1))
    session = mksession(tmp_path)

    def run(effects):
        run_effects(sc, effects, session)

    run(sc.start())
    for command in ["save", "remove", "pause", "pause", "download", "remove", "quit"]:
        run(sc.feed(command))

    events = [
        json.loads(line)["type"]
        for line in (tmp_path / "events.jsonl").open()
    ]
    assert events == [
        "start", "view", "save", "remove", "pause", "resume",
        "download", "remove", "end",
    ]
    assert list((tmp_path / "dl").rglob("*.pdf")) == []  # downloaded then deleted
    assert session.store.read_ids() == {"2601.00001"}    # the view marked it seen


def test_failed_download_is_not_logged_or_copied(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    copied = []

    def fail_download(paper_id, path):
        raise RuntimeError("offline")

    monkeypatch.setattr(util, "download_paper", fail_download)
    monkeypatch.setattr(util, "copy_to_clipboard", lambda text: copied.append(text))

    sc = Scanner(papers(1))
    session = mksession(tmp_path)
    run_effects(sc, sc.start(), session)

    with pytest.raises(RuntimeError, match="offline"):
        run_effects(sc, sc.feed("download"), session)

    events = [json.loads(line)["type"] for line in events_path.open()]
    assert events == ["start", "view"]
    assert copied == []
    assert list((tmp_path / "dl").rglob("*.pdf")) == []


# -- session sinks: Downloads ---------------------------------------------------

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
