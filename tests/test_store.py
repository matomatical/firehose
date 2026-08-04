"""
Tests for firehose.store: the pure selection window (select_papers) and
LocalStore end to end on a tmp data directory — selection with metadata,
event recording and the derived seen-set, the log-tailing refresh, and the
pre-shaped reading-state queries.
"""

import datetime
import json
import random

from conftest import make_data_dir, make_doc, make_store
from firehose.store import select_papers


def _d(day: int) -> datetime.date:
    """A date in May 2025 (after the modern cutoff), parameterised by day-of-month."""
    return datetime.date(2025, 5, day)


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


def test_select_papers_randomise_returns_short_remaining_backlog():
    cache = {f"p{i}": _d(i) for i in range(1, 4)}

    out = select_papers(
        cache, set(), n=100, randomise=True, rng=random.Random(0),
    )

    assert len(out) == 3
    assert {xid for xid, _ in out} == {"p1", "p2", "p3"}


def test_select_papers_randomise_empty_backlog_is_empty():
    assert select_papers({}, set(), n=100, randomise=True) == []


def test_select_papers_n_zero_or_negative_returns_empty():
    # Guards the unread[-0:] == unread[:] trap: without the n<=0 short-circuit the
    # default branch would return *all* candidates for n=0 (and slice oddly for n<0).
    cache = {f"p{i}": _d(i) for i in range(1, 4)}
    assert select_papers(cache, set(), n=0) == []
    assert select_papers(cache, set(), n=-1) == []


# -- LocalStore: selection with metadata ----------------------------------------

def test_select_papers_returns_full_metadata(tmp_path):
    make_data_dir(tmp_path, [
        make_doc("2601.00001", date="2026-01-01"),
        make_doc("2601.00002", date="2026-01-02"),
    ])
    store = make_store(tmp_path)

    papers = store.select_papers(10)

    assert [p.xid for p in papers] == ["2601.00002", "2601.00001"]
    assert papers[0].title == "Title 2601.00002"
    assert papers[0].summary == "A summary."
    assert papers[0].published.date() == datetime.date(2026, 1, 2)


def test_select_papers_filters_to_subscribed_categories(tmp_path):
    make_data_dir(tmp_path, [
        make_doc("2601.00001", categories=("cs.LG",)),
        make_doc("2601.00002", categories=("math.NT",)),
        make_doc("2601.00003", categories=("math.NT", "cs.LG")),  # cross-list
    ])
    store = make_store(tmp_path, subscribed={"cs.LG"})

    papers = store.select_papers(10)

    assert {p.xid for p in papers} == {"2601.00001", "2601.00003"}


def test_select_papers_excludes_seen(tmp_path):
    make_data_dir(
        tmp_path,
        [make_doc("2601.00001"), make_doc("2601.00002")],
        events=[
            {"t": "2026-01-03T10:00:00", "type": "view", "xid": "2601.00001"},
        ],
    )
    store = make_store(tmp_path)

    papers = store.select_papers(10)

    assert [p.xid for p in papers] == ["2601.00002"]


def test_select_papers_read_imports_mark_seen(tmp_path):
    make_data_dir(
        tmp_path,
        [make_doc("2601.00001"), make_doc("2601.00002")],
        events=[
            {"t": "2026-01-03", "type": "read-import", "xid": "2601.00002"},
        ],
    )
    store = make_store(tmp_path)

    papers = store.select_papers(10)

    assert [p.xid for p in papers] == ["2601.00001"]


def test_select_papers_drops_ids_missing_from_mirror(tmp_path):
    # an id in the index whose document was deleted upstream is skipped
    from firehose import mirror

    make_data_dir(tmp_path, [make_doc("2601.00001"), make_doc("2601.00002")])
    updater = mirror.Updater(str(tmp_path / "metadata"))
    updater.delete("2601.00001")
    updater.flush()
    store = make_store(tmp_path)

    papers = store.select_papers(10)

    assert [p.xid for p in papers] == ["2601.00002"]


def test_get_paper_ignores_subscription(tmp_path):
    make_data_dir(tmp_path, [make_doc("2601.00001", categories=("math.NT",))])
    store = make_store(tmp_path, subscribed={"cs.LG"})

    assert store.get_paper("2601.00001").title == "Title 2601.00001"
    assert store.get_paper("2601.99999") is None


# -- LocalStore: event recording and the derived seen-set ------------------------

def test_record_events_appends_and_marks_seen(tmp_path):
    make_data_dir(tmp_path, [make_doc("2601.00001"), make_doc("2601.00002")])
    store = make_store(tmp_path)

    store.record_events([{"type": "start", "n": 1}])
    store.record_events([{"type": "view", "xid": "2601.00001"}])

    # events land on disk, stamped
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in lines]
    assert [e["type"] for e in events] == ["start", "view"]
    assert all("t" in e for e in events)
    # and the seen-set reflects the view immediately
    assert store.read_ids() == {"2601.00001"}
    assert [p.xid for p in store.select_papers(10)] == ["2601.00002"]


def test_refresh_events_tails_the_log(tmp_path):
    make_data_dir(
        tmp_path,
        [make_doc("2601.00001")],
        events=[{"t": "2026-01-03T10:00:00", "type": "start", "n": 1}],
    )
    store = make_store(tmp_path)
    assert store.read_ids() == set()

    # another writer appends to the log after the store loaded it
    with open(tmp_path / "events.jsonl", "a") as f:
        f.write(json.dumps(
            {"t": "2026-01-03T10:00:05", "type": "view", "xid": "2601.00001"}
        ) + "\n")

    assert store.read_ids() == set()      # not seen yet...
    store.refresh_events()
    assert store.read_ids() == {"2601.00001"}
    assert len(store.scan_events()) == 2


def test_refresh_events_does_not_refold_own_recordings(tmp_path):
    # record_events folds as it writes, so a later refresh (e.g. the one in
    # status) must not read the store's own appends back as news
    make_data_dir(tmp_path, [make_doc("2601.00001")])
    store = make_store(tmp_path)

    store.record_events([{"type": "view", "xid": "2601.00001"}])
    store.refresh_events()

    assert len(store.scan_events()) == 1


def test_store_tolerates_missing_event_log(tmp_path):
    make_data_dir(tmp_path, [make_doc("2601.00001")])
    store = make_store(tmp_path)
    assert store.scan_events() == []
    assert store.read_ids() == set()


# -- LocalStore: status snapshot --------------------------------------------------

def test_status_reports_store_state(tmp_path):
    make_data_dir(
        tmp_path,
        [
            make_doc("2601.00001", date="2026-01-01"),
            make_doc("2601.00002", date="2026-01-02"),
        ],
        events=[
            {"t": "2026-01-03T10:00:00", "type": "view", "xid": "2601.00001"},
        ],
    )
    harvest_records = [
        {
            "t": "2026-01-02T04:05:00", "t_start": "2026-01-02T04:00:00",
            "counts": {"new": 2}, "watermark": "2026-01-02", "papers": 2,
            "completed": True,
        },
    ]
    with open(tmp_path / "harvests.jsonl", "w") as f:
        for record in harvest_records:
            f.write(json.dumps(record) + "\n")
    store = make_store(tmp_path)

    status = store.status()

    assert status["data_dir"] == str(tmp_path)
    assert status["watermark"] == "2026-01-02"
    assert status["subscribed_papers"] == 2
    assert status["seen_papers"] == 1
    assert status["events"] == 1
    assert status["last_event"]["xid"] == "2601.00001"
    assert status["harvests"] == harvest_records
    assert json.dumps(status)   # JSON-clean, as served over HTTP
    store.close()               # part of the interface; a no-op locally


def test_status_rereads_the_logs(tmp_path):
    make_data_dir(tmp_path, [make_doc("2601.00001")])
    store = make_store(tmp_path)
    assert store.status()["events"] == 0

    # another writer appends after the store loaded the log
    with open(tmp_path / "events.jsonl", "a") as f:
        f.write(json.dumps(
            {"t": "2026-01-03T10:00:00", "type": "view", "xid": "2601.00001"}
        ) + "\n")

    assert store.status()["events"] == 1


def test_status_on_empty_data_dir(tmp_path):
    make_data_dir(tmp_path, [])
    store = make_store(tmp_path)

    status = store.status()

    assert status["watermark"] is None
    assert status["subscribed_papers"] is None
    assert status["seen_papers"] == 0
    assert status["events"] == 0
    assert status["last_event"] is None
    assert status["harvests"] == []


# -- LocalStore: reading-state queries -------------------------------------------

def test_reading_state_queries(tmp_path):
    make_data_dir(
        tmp_path,
        [
            make_doc("2601.00001", date="2026-01-01"),
            make_doc("2601.00002", date="2026-01-02"),
            make_doc("9901.00001", date="1999-01-01", categories=("math.NT",)),
        ],
        events=[
            {"t": "2026-01-03", "type": "read-import", "xid": "2601.00001"},
            {"t": "2026-01-04T09:00:00", "type": "view", "xid": "2601.00002"},
        ],
    )
    store = make_store(tmp_path, subscribed={"cs.LG"})

    assert store.submitted_dates() == [
        datetime.date(2026, 1, 1), datetime.date(2026, 1, 2),
    ]
    assert store.unread_dates() == []     # both subscribed papers are seen
    assert store.read_dates() == [
        datetime.date(2026, 1, 3), datetime.date(2026, 1, 4),
    ]
    assert store.read_submit_dates() == [
        datetime.date(2026, 1, 1), datetime.date(2026, 1, 2),
    ]
    assert store.subscribed_ids() == ["2601.00001", "2601.00002"]
    assert store.read_ids() == {"2601.00001", "2601.00002"}


def test_unread_dates_respects_cutoff(tmp_path):
    make_data_dir(tmp_path, [
        make_doc("2401.00001", date="2024-01-01"),
        make_doc("2601.00002", date="2026-01-02"),
    ])
    store = make_store(tmp_path)

    assert len(store.unread_dates()) == 2
    assert store.unread_dates(cutoff=datetime.date(2025, 4, 15)) == [
        datetime.date(2026, 1, 2),
    ]
