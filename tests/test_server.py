"""
Tests for the HTTP boundary: a RemoteStore driving the FastAPI app from
`firehose.server` (in-process via TestClient, which is an httpx.Client)
against a real LocalStore on a tmp data directory. Exercises the full
client -> wire -> server -> store contract both ways.
"""

import datetime
import json

from fastapi.testclient import TestClient

from conftest import make_data_dir, make_doc, make_store
from firehose.server import create_app
from firehose.store import RemoteStore


def make_remote(tmp_path) -> RemoteStore:
    app = create_app(make_store(tmp_path))
    return RemoteStore("http://testserver", client=TestClient(app))


def test_select_papers_round_trip(tmp_path):
    make_data_dir(tmp_path, [
        make_doc("2601.00001", date="2026-01-01"),
        make_doc("2601.00002", date="2026-01-02"),
        make_doc("2601.00003", date="2026-01-03", categories=("math.NT",)),
    ])
    remote = make_remote(tmp_path)

    papers = remote.select_papers(10)

    # unsubscribed category filtered server-side; newest first; full metadata
    assert [p.xid for p in papers] == ["2601.00002", "2601.00001"]
    assert papers[0].title == "Title 2601.00002"
    assert papers[0].summary == "A summary."
    assert papers[0].published.date() == datetime.date(2026, 1, 2)
    assert papers[0].name.startswith("Author+Boauthor2026")


def test_select_papers_selection_options_forwarded(tmp_path):
    make_data_dir(tmp_path, [
        make_doc(f"2601.0000{i}", date=f"2026-01-0{i}") for i in range(1, 6)
    ])
    remote = make_remote(tmp_path)

    assert [p.xid for p in remote.select_papers(2, backwards=True)] == [
        "2601.00001", "2601.00002",
    ]
    assert [
        p.xid
        for p in remote.select_papers(10, cutoff=datetime.date(2026, 1, 3))
    ] == ["2601.00005", "2601.00004"]

    # a seeded draw is deterministic across requests
    import random
    one = remote.select_papers(3, randomise=True, rng=random.Random(7))
    two = remote.select_papers(3, randomise=True, rng=random.Random(7))
    assert [p.xid for p in one] == [p.xid for p in two]


def test_get_paper_round_trip(tmp_path):
    make_data_dir(tmp_path, [
        make_doc("2601.00001"),
        make_doc("math/0211159", date="2002-11-11"),
    ])
    remote = make_remote(tmp_path)

    assert remote.get_paper("2601.00001").title == "Title 2601.00001"
    # old-style ids carry a slash through the URL path
    assert remote.get_paper("math/0211159").xid == "math/0211159"
    assert remote.get_paper("2601.99999") is None


def test_record_events_round_trip(tmp_path):
    make_data_dir(tmp_path, [make_doc("2601.00001"), make_doc("2601.00002")])
    remote = make_remote(tmp_path)

    remote.record_events([{"type": "start", "n": 1}])
    remote.record_events([{"type": "view", "xid": "2601.00001"}])

    # the view lands in the server's seen-set and on its disk, stamped
    # with the client's timestamp
    assert remote.read_ids() == {"2601.00001"}
    assert [p.xid for p in remote.select_papers(10)] == ["2601.00002"]
    events = [
        json.loads(line)
        for line in (tmp_path / "scanlog.jsonl").read_text().splitlines()
    ]
    assert [e["type"] for e in events] == ["start", "view"]
    assert all("t" in e for e in events)


def test_reading_state_queries_round_trip(tmp_path):
    make_data_dir(
        tmp_path,
        [
            make_doc("2601.00001", date="2026-01-01"),
            make_doc("2601.00002", date="2026-01-02"),
        ],
        events=[
            {"t": "2026-01-03", "type": "read-import", "xid": "2601.00001"},
        ],
    )
    remote = make_remote(tmp_path)

    assert remote.submitted_dates() == [
        datetime.date(2026, 1, 1), datetime.date(2026, 1, 2),
    ]
    assert remote.unread_dates() == [datetime.date(2026, 1, 2)]
    assert remote.unread_dates(cutoff=datetime.date(2026, 1, 2)) == []
    assert remote.read_dates() == [datetime.date(2026, 1, 3)]
    assert remote.read_submit_dates() == [datetime.date(2026, 1, 1)]
    assert remote.subscribed_ids() == ["2601.00001", "2601.00002"]
    assert remote.scan_events() == [
        {"t": "2026-01-03", "type": "read-import", "xid": "2601.00001"},
    ]
    remote.refresh_events()   # a no-op, but part of the interface
