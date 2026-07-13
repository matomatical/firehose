"""
Smoke test for firehose.vis. The visualisation entry points read the cache and
readlog off disk (no network), so one can be driven end-to-end against a tmp
data dir. This guards the path-resolution wiring (config -> data_paths ->
load_cache) -- the class of regression a util rename/removal silently introduces
in an entry point that the pure-helper unit tests never call.

Plus unit tests for the scan-time analytics core (split_sessions /
session_active_seconds / summarise_scan_time / render_scan_time), which are
pure functions over a flat scanlog event list -- no terminal or network.
"""
import datetime

from firehose import util, vis


def test_all_submitted_years_runs_against_tmp_cache(tmp_path, capsys):
    (tmp_path / "config.toml").write_text('[paths]\ndata = "unused"\n')
    cache = {
        "2501.00001": datetime.date(2025, 1, 1),
        "2601.00002": datetime.date(2026, 1, 2),
    }
    util.save_cache(str(tmp_path / "arxiv.txt"), datetime.date(2026, 1, 2), cache)

    vis.all_submitted_years(
        config_path=str(tmp_path / "config.toml"),
        data_dir=str(tmp_path),
    )

    out = capsys.readouterr().out
    assert "2025 (1 papers)" in out
    assert "2026 (1 papers)" in out


# -- unread selection ----------------------------------------------------------

def test_select_unread_dates_drops_read_and_pre_cutoff():
    cache = {
        "old":    datetime.date(2025, 1, 1),   # <= cutoff, dropped
        "read":   datetime.date(2026, 1, 2),   # already read, dropped
        "unread": datetime.date(2026, 1, 3),   # kept
    }
    dates = vis.select_unread_dates(
        cache, read={"read"}, cutoff=datetime.date(2025, 6, 1),
    )
    assert dates == [datetime.date(2026, 1, 3)]


def test_select_unread_dates_no_cutoff_keeps_full_backlog():
    cache = {
        "old":    datetime.date(2025, 1, 1),
        "unread": datetime.date(2026, 1, 3),
    }
    dates = vis.select_unread_dates(cache, read=set(), cutoff=None)
    assert sorted(dates) == [datetime.date(2025, 1, 1), datetime.date(2026, 1, 3)]


# -- scan-time analytics core --------------------------------------------------

def _ev(t, type, **rest):
    """A scanlog event at "2026-06-22T<t>" (t is a "HH:MM:SS" suffix)."""
    return {"t": f"2026-06-22T{t}", "type": type, **rest}


def test_split_sessions_groups_by_start_end():
    events = [
        _ev("11:00:00", "start", n=1), _ev("11:00:05", "view", xid="a"),
        _ev("11:00:10", "end"),
        _ev("12:00:00", "start", n=1), _ev("12:00:03", "view", xid="b"),
        _ev("12:00:09", "end"),
    ]
    sessions = vis.split_sessions(events)
    assert len(sessions) == 2
    assert [e["type"] for e in sessions[0]] == ["start", "view", "end"]


def test_split_sessions_handles_missing_end_then_new_start():
    # a crash (start with no end) still closes when the next start arrives, and
    # a trailing in-progress run (no end yet) is returned too.
    events = [
        _ev("11:00:00", "start", n=1), _ev("11:00:05", "view", xid="a"),
        _ev("12:00:00", "start", n=1), _ev("12:00:03", "view", xid="b"),
    ]
    sessions = vis.split_sessions(events)
    assert len(sessions) == 2
    assert [e["type"] for e in sessions[1]] == ["start", "view"]


def test_session_active_seconds_sums_gaps():
    # gaps: 5 (start->view) + 22 (view->view) + 5 (view->end) = 32
    session = [
        _ev("11:00:00", "start", n=2), _ev("11:00:05", "view", xid="a"),
        _ev("11:00:27", "view", xid="b"), _ev("11:00:32", "end"),
    ]
    assert vis.session_active_seconds(session) == 32.0


def test_session_active_seconds_excludes_paused_span():
    # the 100s pause->resume gap is dropped; the rest (5 + 5 + 3 = 13) counts.
    session = [
        _ev("11:00:00", "start", n=1), _ev("11:00:05", "view", xid="a"),
        _ev("11:00:10", "pause"), _ev("11:01:50", "resume"),
        _ev("11:01:53", "end"),
    ]
    assert vis.session_active_seconds(session) == 13.0


def test_summarise_scan_time_distinct_papers_and_totals():
    # one session, a re-viewed paper ("a" twice via back/forward) counts once.
    events = [
        _ev("11:00:00", "start", n=2), _ev("11:00:04", "view", xid="a"),
        _ev("11:00:10", "view", xid="b"), _ev("11:00:14", "view", xid="a"),
        _ev("11:00:20", "end"),
    ]
    summary = vis.summarise_scan_time(events)
    assert summary.sessions == 1
    assert summary.papers == 2           # distinct: {a, b}
    assert summary.seconds == 20.0       # 4 + 6 + 4 + 6
    assert summary.seconds_per_paper == 10.0
    assert len(summary.days) == 1
    assert summary.days[0].date == datetime.date(2026, 6, 22)


def test_summarise_scan_time_buckets_by_session_start_day():
    events = [
        {"t": "2026-06-22T11:00:00", "type": "start", "n": 1},
        {"t": "2026-06-22T11:00:06", "type": "view", "xid": "a"},
        {"t": "2026-06-22T11:00:10", "type": "end"},
        {"t": "2026-06-23T09:00:00", "type": "start", "n": 1},
        {"t": "2026-06-23T09:00:04", "type": "view", "xid": "b"},
        {"t": "2026-06-23T09:00:10", "type": "end"},
    ]
    summary = vis.summarise_scan_time(events)
    assert [d.date for d in summary.days] == [
        datetime.date(2026, 6, 22), datetime.date(2026, 6, 23),
    ]
    assert summary.sessions == 2 and summary.papers == 2


def test_summarise_scan_time_empty():
    summary = vis.summarise_scan_time([])
    assert summary.days == [] and summary.sessions == 0
    assert summary.seconds == 0.0 and summary.seconds_per_paper == 0.0


def test_render_scan_time_has_totals_row():
    events = [
        _ev("11:00:00", "start", n=1), _ev("11:00:04", "view", xid="a"),
        _ev("11:00:10", "end"),
    ]
    out = vis.render_scan_time(vis.summarise_scan_time(events))
    assert "2026-06-22" in out
    total = next(line for line in out.splitlines() if line.startswith("TOTAL"))
    # sessions and papers columns both read 1
    assert total.split() == ["TOTAL", "1", "1", "0:00:10", "10.00s"]


def test_scan_time_legend_names_both_ends():
    # magenta end is always 0:00:00; cyan end is the busiest day (625s -> 0:10:25)
    label = str(vis._scan_time_legend(625.0)).splitlines()[0]
    assert label == "time spent: (magenta = 0:00:00, cyan = 0:10:25)"


def test_scan_time_entry_point_runs_against_tmp_scanlog(tmp_path, capsys):
    # end-to-end through the shell (heatmap off, so no terminal needed): guards
    # the config -> data_paths -> load_scanlog wiring, like the years smoke test.
    (tmp_path / "config.toml").write_text('[paths]\ndata = "unused"\n')
    util.log_event(str(tmp_path / "scanlog.jsonl"), {"type": "start", "n": 1})
    util.log_event(str(tmp_path / "scanlog.jsonl"), {"type": "view", "xid": "a"})
    util.log_event(str(tmp_path / "scanlog.jsonl"), {"type": "end"})

    vis.scan_time(
        config_path=str(tmp_path / "config.toml"),
        data_dir=str(tmp_path),
        heatmap=False,
    )
    out = capsys.readouterr().out
    total = next(line for line in out.splitlines() if line.startswith("TOTAL"))
    assert total.split()[:3] == ["TOTAL", "1", "1"]  # sessions, papers


def test_scan_time_entry_point_no_scans(tmp_path, capsys):
    (tmp_path / "config.toml").write_text('[paths]\ndata = "unused"\n')
    vis.scan_time(
        config_path=str(tmp_path / "config.toml"),
        data_dir=str(tmp_path),
        heatmap=False,
    )
    assert "no scans recorded yet." in capsys.readouterr().out
