"""
Tests for firehose.store: the pure candidate filter (filter_unread) and
selection window (select_papers), and LocalStore end to end on a tmp data
directory — selection with metadata, event recording and the derived
seen-set, the log-tailing refresh, and the pre-shaped reading-state
queries.
"""

import datetime
import json
import random

from conftest import make_data_dir, make_doc, make_store
from firehose.store import filter_unread, select_papers


def _d(day: int) -> datetime.date:
    """A date in May 2025, parameterised by day-of-month."""
    return datetime.date(2025, 5, day)


def _cache(n: int) -> dict[str, datetime.date]:
    """arxiv:p1 .. arxiv:pn in cache order, dated by index."""
    return {f"arxiv:p{i}": _d(i) for i in range(1, n + 1)}


def _unread(n: int) -> list[tuple[str, datetime.date]]:
    return filter_unread(_cache(n), read=set())


# -- filter_unread: the candidate set -------------------------------------------

def test_filter_unread_excludes_read():
    out = filter_unread(_cache(5), read={"arxiv:p4", "arxiv:p5"})
    assert [pid for pid, _ in out] == ["arxiv:p1", "arxiv:p2", "arxiv:p3"]


def test_filter_unread_narrows_to_source():
    cache = {
        "arxiv:p1": _d(1), "lw:q1": _d(2), "arxiv:p2": _d(3),
    }
    assert [pid for pid, _ in filter_unread(cache, set(), source="lw")] == (
        ["lw:q1"]
    )
    assert [pid for pid, _ in filter_unread(cache, set(), source="arxiv")] == (
        ["arxiv:p1", "arxiv:p2"]
    )


def test_filter_unread_applies_per_source_cutoffs():
    cache = {
        "arxiv:older": datetime.date(2024, 1, 1),   # dropped
        "arxiv:at": datetime.date(2025, 4, 15),     # == cutoff: dropped
        "arxiv:new": datetime.date(2025, 4, 16),    # kept (strictly after)
        "lw:old": datetime.date(2010, 1, 1),        # no lw cutoff: kept
    }
    cutoffs = {"arxiv": datetime.date(2025, 4, 15)}
    assert {pid for pid, _ in filter_unread(cache, set(), cutoffs=cutoffs)} == {
        "arxiv:new", "lw:old",
    }
    # no cutoffs mapping: the full backlog
    assert len(filter_unread(cache, set())) == 4


def test_filter_unread_source_and_cutoff_combine():
    cache = {
        "arxiv:old": datetime.date(2024, 1, 1),
        "arxiv:new": datetime.date(2025, 5, 1),
        "lw:new": datetime.date(2025, 5, 2),
    }
    out = filter_unread(
        cache,
        set(),
        cutoffs={"arxiv": datetime.date(2025, 1, 1)},
        source="arxiv",
    )
    assert [pid for pid, _ in out] == ["arxiv:new"]


# -- select_papers: the selection window ----------------------------------------

def test_select_papers_default_takes_last_n_newest_first():
    out = select_papers(_unread(5), n=2)
    # last two in cache order are p4, p5; returned reversed (newest first)
    assert [pid for pid, _ in out] == ["arxiv:p5", "arxiv:p4"]


def test_select_papers_backwards_takes_first_n_in_order():
    out = select_papers(_unread(5), n=2, backwards=True)
    assert [pid for pid, _ in out] == ["arxiv:p1", "arxiv:p2"]


def test_select_papers_offset_narrows_window_before_selecting():
    # offset=3 -> last three candidates [p3,p4,p5]; backwards then takes first two
    out = select_papers(_unread(5), n=2, offset=3, backwards=True)
    assert [pid for pid, _ in out] == ["arxiv:p3", "arxiv:p4"]


def test_select_papers_randomise_is_deterministic_with_seeded_rng():
    out1 = select_papers(_unread(5), n=3, randomise=True, rng=random.Random(0))
    out2 = select_papers(_unread(5), n=3, randomise=True, rng=random.Random(0))
    assert len(out1) == 3
    assert {pid for pid, _ in out1} <= {f"arxiv:p{i}" for i in range(1, 6)}
    assert out1 == out2   # same seed -> same draw


def test_select_papers_randomise_returns_short_remaining_backlog():
    out = select_papers(
        _unread(3), n=100, randomise=True, rng=random.Random(0),
    )

    assert len(out) == 3
    assert {pid for pid, _ in out} == {f"arxiv:p{i}" for i in range(1, 4)}


def test_select_papers_randomise_empty_backlog_is_empty():
    assert select_papers([], n=100, randomise=True) == []


def test_select_papers_n_zero_or_negative_returns_empty():
    # Guards the unread[-0:] == unread[:] trap: without the n<=0 short-circuit the
    # default branch would return *all* candidates for n=0 (and slice oddly for n<0).
    assert select_papers(_unread(3), n=0) == []
    assert select_papers(_unread(3), n=-1) == []


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
            {"t": "2026-01-03T10:00:00", "type": "view", "id": "arxiv:2601.00001"},
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
            {"t": "2026-01-03", "type": "read-import", "id": "arxiv:2601.00002"},
        ],
    )
    store = make_store(tmp_path)

    papers = store.select_papers(10)

    assert [p.xid for p in papers] == ["2601.00001"]


def test_select_papers_drops_ids_missing_from_mirror(tmp_path):
    # an id in the index whose document was deleted upstream is skipped
    from firehose import mirror
    from firehose import sources

    make_data_dir(tmp_path, [make_doc("2601.00001"), make_doc("2601.00002")])
    updater = mirror.Updater(str(tmp_path / "mirror" / "arxiv"))
    updater.delete("2601.00001", sources.adapter("arxiv").shard("2601.00001"))
    updater.flush()
    store = make_store(tmp_path)

    papers = store.select_papers(10)

    assert [p.xid for p in papers] == ["2601.00002"]


def test_get_paper_ignores_subscription(tmp_path):
    make_data_dir(tmp_path, [make_doc("2601.00001", categories=("math.NT",))])
    store = make_store(tmp_path, subscribed={"cs.LG"})

    assert store.get_paper("arxiv:2601.00001").title == "Title 2601.00001"
    assert store.get_paper("arxiv:2601.99999") is None


# -- LocalStore: event recording and the derived seen-set ------------------------

def test_record_events_appends_and_marks_seen(tmp_path):
    make_data_dir(tmp_path, [make_doc("2601.00001"), make_doc("2601.00002")])
    store = make_store(tmp_path)

    store.record_events([{"type": "start", "n": 1}])
    store.record_events([{"type": "view", "id": "arxiv:2601.00001"}])

    # events land on disk, stamped
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in lines]
    assert [e["type"] for e in events] == ["start", "view"]
    assert all("t" in e for e in events)
    # and the seen-set reflects the view immediately
    assert store.read_ids() == {"arxiv:2601.00001"}
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
            {"t": "2026-01-03T10:00:05", "type": "view", "id": "arxiv:2601.00001"}
        ) + "\n")

    assert store.read_ids() == set()      # not seen yet...
    store.refresh_events()
    assert store.read_ids() == {"arxiv:2601.00001"}
    assert len(store.scan_events()) == 2


def test_refresh_events_does_not_refold_own_recordings(tmp_path):
    # record_events folds as it writes, so a later refresh (e.g. the one in
    # status) must not read the store's own appends back as news
    make_data_dir(tmp_path, [make_doc("2601.00001")])
    store = make_store(tmp_path)

    store.record_events([{"type": "view", "id": "arxiv:2601.00001"}])
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
            {"t": "2026-01-03T10:00:00", "type": "view", "id": "arxiv:2601.00001"},
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
    assert status["watermarks"] == {"arxiv": "2026-01-02"}
    assert status["subscribed_papers"] == 2
    assert status["seen_papers"] == 1
    assert status["events"] == 1
    assert status["last_event"]["id"] == "arxiv:2601.00001"
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
            {"t": "2026-01-03T10:00:00", "type": "view", "id": "arxiv:2601.00001"}
        ) + "\n")

    assert store.status()["events"] == 1


def test_status_on_empty_data_dir(tmp_path):
    make_data_dir(tmp_path, [])
    store = make_store(tmp_path)

    status = store.status()

    assert status["watermarks"] == {"arxiv": None}
    assert status["subscribed_papers"] == 0
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
            {"t": "2026-01-03", "type": "read-import", "id": "arxiv:2601.00001"},
            {"t": "2026-01-04T09:00:00", "type": "view", "id": "arxiv:2601.00002"},
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
    assert store.subscribed_ids() == ["arxiv:2601.00001", "arxiv:2601.00002"]
    assert store.read_ids() == {"arxiv:2601.00001", "arxiv:2601.00002"}


def test_unread_dates_respects_source_cutoff(tmp_path):
    make_data_dir(tmp_path, [
        make_doc("2401.00001", date="2024-01-01"),
        make_doc("2601.00002", date="2026-01-02"),
    ])
    store = make_store(
        tmp_path, modern_cutoff=datetime.date(2025, 4, 15),
    )

    assert len(store.unread_dates(modern=False)) == 2
    assert store.unread_dates() == [datetime.date(2026, 1, 2)]


# -- LocalStore: multiple sources -------------------------------------------------

class _FakeAdapter:
    """A minimal second source ("fake"), single-sharded, subscribing to
    everything: exercises the multi-source paths before a real second
    adapter exists."""

    source = "fake"

    def shard(self, local_id, date=None):
        return "all"

    def subscription(self, section):
        return lambda entry: True

    def to_paper(self, doc):
        from firehose.paper import Paper
        return Paper(
            id=f"fake:{doc['id']}",
            xidv=doc["id"],
            name=f"Fake {doc['id']}",
            entry_id=f"https://fake.example/{doc['id']}",
            title=doc["title"],
            authors=[],
            categories=[],
            summary="",
            published=None,
            updated=None,
            comment=None,
            doc=doc,
        )


def _fake_source(tmp_path, docs: dict[str, datetime.date], monkeypatch):
    """Install the fake adapter and write its index and mirror files."""
    from firehose import index, mirror, sources

    real_adapter = sources.adapter
    monkeypatch.setattr(
        sources,
        "adapter",
        lambda name: _FakeAdapter() if name == "fake" else real_adapter(name),
    )
    (tmp_path / "index").mkdir(exist_ok=True)
    index.save_index(
        path=str(tmp_path / "index" / "fake.txt"),
        watermark=max(docs.values()),
        entries={
            local_id: index.Entry(date=date, categories=())
            for local_id, date in docs.items()
        },
    )
    mirror.save_shard(
        str(tmp_path / "mirror" / "fake"),
        "all",
        {
            local_id: {"id": local_id, "title": f"T {local_id}", "source": "fake"}
            for local_id in docs
        },
    )


def _two_source_store(tmp_path, fake_section=None):
    from firehose import util
    from firehose.store import LocalStore

    paths = util.data_paths({"paths": {"data": str(tmp_path)}})
    return LocalStore(paths, sources_config={
        "arxiv": {"categories": ["cs:cs:LG"]},
        "fake": fake_section or {},
    })


def test_multi_source_dates_merge_sorted(tmp_path, monkeypatch):
    make_data_dir(tmp_path, [
        make_doc("2601.00001", date="2026-01-01"),
        make_doc("2601.00003", date="2026-01-03"),
    ])
    _fake_source(tmp_path, {
        "q2": datetime.date(2026, 1, 2),
        "q4": datetime.date(2026, 1, 4),
    }, monkeypatch)
    store = _two_source_store(tmp_path)

    # merged and interleaved by (date, id) across sources
    assert store.subscribed_ids() == [
        "arxiv:2601.00001", "fake:q2", "arxiv:2601.00003", "fake:q4",
    ]
    # newest-first selection mixes sources and builds each source's Paper
    papers = store.select_papers(10)
    assert [p.id for p in papers] == [
        "fake:q4", "arxiv:2601.00003", "fake:q2", "arxiv:2601.00001",
    ]
    assert papers[0].title == "T q4"
    assert papers[1].title == "Title 2601.00003"


def test_reading_state_queries_narrow_by_source(tmp_path, monkeypatch):
    make_data_dir(
        tmp_path,
        [make_doc("2601.00001", date="2026-01-01")],
        events=[
            {"t": "2026-01-05T10:00:00", "type": "view", "id": "arxiv:2601.00001"},
            {"t": "2026-01-06T10:00:00", "type": "view", "id": "fake:q2"},
        ],
    )
    _fake_source(tmp_path, {
        "q2": datetime.date(2026, 1, 2),
        "q4": datetime.date(2026, 1, 4),
    }, monkeypatch)
    store = _two_source_store(tmp_path)

    assert store.subscribed_ids(source="fake") == ["fake:q2", "fake:q4"]
    assert store.submitted_dates(source="fake") == [
        datetime.date(2026, 1, 2), datetime.date(2026, 1, 4),
    ]
    assert store.read_dates(source="fake") == [datetime.date(2026, 1, 6)]
    assert store.read_submit_dates(source="fake") == [datetime.date(2026, 1, 2)]
    # and unfiltered queries still cover everything
    assert len(store.subscribed_ids()) == 3
    assert len(store.read_dates()) == 2


def test_multi_source_source_narrowing_and_cutoffs(tmp_path, monkeypatch):
    make_data_dir(tmp_path, [make_doc("2601.00001", date="2026-01-01")])
    _fake_source(tmp_path, {
        "old": datetime.date(2025, 1, 1),
        "new": datetime.date(2026, 1, 2),
    }, monkeypatch)
    store = _two_source_store(
        tmp_path, fake_section={"modern_cutoff": datetime.date(2025, 6, 1)},
    )

    assert [p.id for p in store.select_papers(10, source="fake")] == [
        "fake:new",   # fake:old is behind fake's cutoff
    ]
    assert [p.id for p in store.select_papers(10, source="fake", modern=False)] == [
        "fake:new", "fake:old",
    ]
    # the fake cutoff does not touch the other source
    assert len(store.select_papers(10)) == 2
    assert store.unread_dates(source="fake") == [datetime.date(2026, 1, 2)]
    # get_paper dispatches on the id's source
    assert store.get_paper("fake:new").title == "T new"


def test_multi_source_tolerates_unharvested_source(tmp_path, monkeypatch):
    from firehose import sources

    make_data_dir(tmp_path, [make_doc("2601.00001", date="2026-01-01")])
    real_adapter = sources.adapter
    monkeypatch.setattr(
        sources,
        "adapter",
        lambda name: _FakeAdapter() if name == "fake" else real_adapter(name),
    )
    store = _two_source_store(tmp_path)   # "fake" configured, never harvested

    assert store.subscribed_ids() == ["arxiv:2601.00001"]
    assert store.status()["watermarks"] == {
        "arxiv": "2026-01-01", "fake": None,
    }
