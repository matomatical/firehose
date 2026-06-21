"""
Tests for firehose.sample's helpers: the pure _key_to_command (keys -> Scanner
commands) and select_papers (which papers to scan, given the cache + readlog),
plus the session-state classes Readlog and Downloads (plain-file + mocked I/O,
no terminal or network). (The Scanner side of the input layer, commands ->
effects, is covered in test_scanner.py.)
"""

import datetime
import random

import readchar

from firehose import util
from firehose.sample import Downloads, Readlog, _key_to_command, select_papers


def test_key_to_command_letters():
    assert _key_to_command("q") == "quit"
    assert _key_to_command("o") == "open"
    assert _key_to_command("s") == "save"
    assert _key_to_command("d") == "download"
    assert _key_to_command("x") == "remove"


def test_key_to_command_special_keys():
    assert _key_to_command(readchar.key.ESC) == "quit"
    assert _key_to_command(readchar.key.LEFT) == "back"
    assert _key_to_command(readchar.key.RIGHT) == "forward"
    assert _key_to_command(readchar.key.SPACE) == "pause"
    assert _key_to_command(readchar.key.UP) == "open"
    assert _key_to_command(readchar.key.DOWN) == "down"


def test_key_to_command_unknown_is_none():
    assert _key_to_command("z") is None
    assert _key_to_command("1") is None


# -- select_papers: filtering + ordering ---------------------------------------

def _d(day: int) -> datetime.date:
    """A date in May 2025 (after the modern cutoff), parameterised by day-of-month."""
    return datetime.date(2025, 5, day)


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


# -- session-state classes: Readlog / Downloads --------------------------------

def test_readlog_appends_grouped_across_dates(tmp_path):
    path = str(tmp_path / "readlog.txt")
    d1, d2 = datetime.date(2026, 6, 20), datetime.date(2026, 6, 21)
    rl = Readlog(path)
    rl.log("a", d1)
    rl.log("b", d1)   # same day -> no new header
    rl.log("c", d2)   # new day -> header
    assert open(path).read() == "2026-06-20:\na\nb\n2026-06-21:\nc\n"


def test_readlog_resume_continues_open_group(tmp_path):
    # a fresh Readlog on an existing file seeds its open group from the last
    # header, so a same-day resume continues it instead of duplicating the header
    path = str(tmp_path / "readlog.txt")
    d = datetime.date(2026, 6, 21)
    Readlog(path).log("a", d)
    Readlog(path).log("b", d)          # new instance, same day
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
