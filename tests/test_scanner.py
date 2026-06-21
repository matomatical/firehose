"""
Tests for the pure scanning logic (firehose.scanner) and a single end-to-end
test of the shell loop (firehose.sample._run_session) with I/O mocked.

The Scanner has no I/O, clock, or randomness, so these run in milliseconds with
no terminal, network, or clipboard.
"""

import json

import pytest

from firehose import scanner as scn
from firehose.scanner import (
    Scanner, Paper, Log, Clip, Open, Readlog, Download, DeletePDF, render_frame,
)


def mkpaper(i: int) -> Paper:
    xid = f"2601.{i:05d}"
    return Paper(
        xid=xid,
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


# -- Scanner: arrival / session -------------------------------------------------

def test_start_emits_start_then_arrival():
    sc = Scanner(papers(2))
    fx = sc.start()
    assert fx == [
        Log({"type": "start", "n": 2}),
        Readlog(sc.xid),
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


# -- Scanner: pause -------------------------------------------------------------

def test_pause_gates_actions_and_resume():
    sc = Scanner(papers(1)); sc.start()
    fx = sc.feed("pause")
    assert sc.paused and fx == [Log({"type": "pause"})]
    fx = sc.feed("save")            # gated while paused
    assert fx == [] and sc.states[0] == "none"
    fx = sc.feed("pause")           # space resumes
    assert not sc.paused and fx == [Log({"type": "resume"})]


def test_quit_works_while_paused():
    sc = Scanner(papers(1)); sc.start()
    sc.feed("pause")
    fx = sc.feed("quit")
    assert sc.done and fx == [Log({"type": "end"})]


# -- Scanner: navigation --------------------------------------------------------

def test_forward_arrives_and_logs_new_paper():
    sc = Scanner(papers(2)); sc.start()
    fx = sc.feed("forward")
    assert sc.index == 1
    assert Readlog(sc.xid) in fx
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
    assert not any(isinstance(e, Readlog) for e in fx)


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
    assert "Title 1" in render_frame(sc, "t")
    assert "☆" not in render_frame(sc, "t") and "★" not in render_frame(sc, "t")
    sc.feed("save")
    assert "☆" in render_frame(sc, "t")
    sc.feed("down")  # -> downloaded
    assert "★" in render_frame(sc, "t")


# -- shell integration (sample._run_session with I/O mocked) -------------------

def test_run_session_integration(tmp_path, monkeypatch):
    from firehose import sample as S
    from firehose import util

    scanlog = tmp_path / "scanlog.jsonl"
    readlog = tmp_path / "readlog.txt"
    dl = tmp_path / "dl"

    monkeypatch.setattr(util, "copy_to_clipboard", lambda text: False)
    monkeypatch.setattr(util, "open_url", lambda url: False)
    monkeypatch.setattr(util, "download_paper",
                        lambda paper_id, path: open(path, "w").write("PDF"))

    keys = iter(["s", "x", "d", "x", "q"])  # save, remove, download, remove, quit
    monkeypatch.setattr(S.readchar, "readkey", lambda: next(keys))

    S._run_session(
        papers(1),
        scanlog_path=str(scanlog),
        readlog_path=str(readlog),
        download_dir=str(dl),
    )

    events = [json.loads(line)["type"] for line in scanlog.open()]
    assert events == ["start", "view", "save", "remove", "download", "remove", "end"]
    assert list(dl.rglob("*.pdf")) == []          # PDF downloaded then deleted
    assert list(util.load_readlog(str(readlog))) == ["2601.00001"]  # logged once
