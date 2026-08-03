"""
Unit tests for the pure data-shaping core (firehose.stats): reading-state
shapes and the scan-time analytics, all plain-data functions — no terminal,
no filesystem, no network.
"""
import datetime

from firehose import stats


# -- unread selection ----------------------------------------------------------

def test_select_unread_dates_drops_read_and_pre_cutoff():
    cache = {
        "old":    datetime.date(2025, 1, 1),   # <= cutoff, dropped
        "read":   datetime.date(2026, 1, 2),   # already read, dropped
        "unread": datetime.date(2026, 1, 3),   # kept
    }
    dates = stats.select_unread_dates(
        cache, read={"read"}, cutoff=datetime.date(2025, 6, 1),
    )
    assert dates == [datetime.date(2026, 1, 3)]


def test_select_unread_dates_no_cutoff_keeps_full_backlog():
    cache = {
        "old":    datetime.date(2025, 1, 1),
        "unread": datetime.date(2026, 1, 3),
    }
    dates = stats.select_unread_dates(cache, read=set(), cutoff=None)
    assert sorted(dates) == [datetime.date(2025, 1, 1), datetime.date(2026, 1, 3)]


# -- read papers resolved to submission dates -----------------------------------

def test_read_submit_dates_resolves_and_drops_missing():
    readlog = {
        "a": datetime.date(2026, 6, 1),   # read date (ignored here)
        "gone": datetime.date(2026, 6, 1),  # not in cache: dropped
        "b": datetime.date(2026, 6, 2),
    }
    cache = {
        "a": datetime.date(2025, 1, 1),
        "b": datetime.date(2025, 2, 2),
        "unread": datetime.date(2025, 3, 3),
    }
    assert stats.read_submit_dates(readlog, cache) == [
        datetime.date(2025, 1, 1), datetime.date(2025, 2, 2),
    ]


# -- heatmap intensity normalisation ---------------------------------------------

def test_normalise_date_counts_scales_busiest_day_to_one():
    d1, d2 = datetime.date(2026, 1, 1), datetime.date(2026, 1, 2)
    assert stats.normalise_date_counts({d1: 1, d2: 4}) == {d1: 0.25, d2: 1.0}


def test_normalise_date_counts_empty():
    assert stats.normalise_date_counts({}) == {}


def test_normalise_date_counts_proportions_keyed_by_totals():
    d1, d2, d3 = (datetime.date(2026, 1, day) for day in (1, 2, 3))
    norm = stats.normalise_date_counts(
        {d1: 2, d3: 5},                      # d3 absent from totals: dropped
        total_counts={d1: 4, d2: 8},         # d2 has no count: 0.0
    )
    assert norm == {d1: 0.5, d2: 0.0}


# -- batched read proportions ----------------------------------------------------

def test_batch_read_proportions_including_short_final_batch():
    all_xids = ["a", "b", "c", "d", "e"]
    proportions = stats.batch_read_proportions(
        all_xids, read={"a", "b", "e"}, batch_size=2,
    )
    assert proportions == [1.0, 0.0, 1.0]   # [a b] [c d] [e]


def test_batch_read_proportions_nothing_read():
    assert stats.batch_read_proportions(["a", "b"], read=set(), batch_size=10) == [0.0]


# -- scan-time analytics core --------------------------------------------------

def _ev(t, type, **rest):
    """A scan event at "2026-06-22T<t>" (t is a "HH:MM:SS" suffix)."""
    return {"t": f"2026-06-22T{t}", "type": type, **rest}


def test_split_sessions_groups_by_start_end():
    events = [
        _ev("11:00:00", "start", n=1), _ev("11:00:05", "view", xid="a"),
        _ev("11:00:10", "end"),
        _ev("12:00:00", "start", n=1), _ev("12:00:03", "view", xid="b"),
        _ev("12:00:09", "end"),
    ]
    sessions = stats.split_sessions(events)
    assert len(sessions) == 2
    assert [e["type"] for e in sessions[0]] == ["start", "view", "end"]


def test_split_sessions_handles_missing_end_then_new_start():
    # a crash (start with no end) still closes when the next start arrives, and
    # a trailing in-progress run (no end yet) is returned too.
    events = [
        _ev("11:00:00", "start", n=1), _ev("11:00:05", "view", xid="a"),
        _ev("12:00:00", "start", n=1), _ev("12:00:03", "view", xid="b"),
    ]
    sessions = stats.split_sessions(events)
    assert len(sessions) == 2
    assert [e["type"] for e in sessions[1]] == ["start", "view"]


def test_session_active_seconds_sums_gaps():
    # gaps: 5 (start->view) + 22 (view->view) + 5 (view->end) = 32
    session = [
        _ev("11:00:00", "start", n=2), _ev("11:00:05", "view", xid="a"),
        _ev("11:00:27", "view", xid="b"), _ev("11:00:32", "end"),
    ]
    assert stats.session_active_seconds(session) == 32.0


def test_session_active_seconds_excludes_paused_span():
    # the 100s pause->resume gap is dropped; the rest (5 + 5 + 3 = 13) counts.
    session = [
        _ev("11:00:00", "start", n=1), _ev("11:00:05", "view", xid="a"),
        _ev("11:00:10", "pause"), _ev("11:01:50", "resume"),
        _ev("11:01:53", "end"),
    ]
    assert stats.session_active_seconds(session) == 13.0


def test_summarise_scan_time_distinct_papers_and_totals():
    # one session, a re-viewed paper ("a" twice via back/forward) counts once.
    events = [
        _ev("11:00:00", "start", n=2), _ev("11:00:04", "view", xid="a"),
        _ev("11:00:10", "view", xid="b"), _ev("11:00:14", "view", xid="a"),
        _ev("11:00:20", "end"),
    ]
    summary = stats.summarise_scan_time(events)
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
    summary = stats.summarise_scan_time(events)
    assert [d.date for d in summary.days] == [
        datetime.date(2026, 6, 22), datetime.date(2026, 6, 23),
    ]
    assert summary.sessions == 2 and summary.papers == 2


def test_summarise_scan_time_empty():
    summary = stats.summarise_scan_time([])
    assert summary.days == [] and summary.sessions == 0
    assert summary.seconds == 0.0 and summary.seconds_per_paper == 0.0


def test_summarise_scan_time_excludes_read_imports():
    # a block of imported reading history (day-resolution timestamps,
    # preceding any real session) contributes no sessions, papers, or time.
    events = [
        {"t": "2025-04-23", "type": "read-import", "xid": "old-a"},
        {"t": "2025-04-24", "type": "read-import", "xid": "old-b"},
        _ev("11:00:00", "start", n=1),
        _ev("11:00:04", "view", xid="a"),
        _ev("11:00:10", "end"),
    ]
    summary = stats.summarise_scan_time(events)
    assert summary.sessions == 1 and summary.papers == 1
    assert summary.seconds == 10.0
    assert [d.date for d in summary.days] == [datetime.date(2026, 6, 22)]
