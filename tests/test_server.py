"""
Tests for the HTTP boundary: a RemoteStore driving the FastAPI app from
`firehose.server` (in-process via TestClient, which is an httpx.Client)
against a real LocalStore on a tmp data directory. Exercises the full
client -> wire -> server -> store contract both ways.
"""

import datetime
import json

import httpx
from fastapi.testclient import TestClient

from conftest import make_data_dir, make_doc, make_store
from firehose.server import create_app
from firehose.store import RemoteStore


def make_remote(tmp_path) -> RemoteStore:
    app = create_app(make_store(tmp_path))
    return RemoteStore(
        "http://testserver",
        client=TestClient(app),
        spool_path=str(tmp_path / "unsent-events.jsonl"),
    )


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

    assert remote.get_paper("arxiv:2601.00001").title == "Title 2601.00001"
    # old-style ids carry a slash through the URL path
    assert remote.get_paper("arxiv:math/0211159").xid == "math/0211159"
    assert remote.get_paper("arxiv:2601.99999") is None


def test_record_events_round_trip(tmp_path):
    make_data_dir(tmp_path, [make_doc("2601.00001"), make_doc("2601.00002")])
    remote = make_remote(tmp_path)

    remote.record_events([{"type": "start", "n": 1}])
    remote.record_events([{"type": "view", "id": "arxiv:2601.00001"}])
    remote.close()   # recording is asynchronous; close settles delivery

    # the view lands in the server's seen-set and on its disk, stamped
    # with the client's timestamp
    assert remote.read_ids() == {"arxiv:2601.00001"}
    assert [p.xid for p in remote.select_papers(10)] == ["2601.00002"]
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
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
            {"t": "2026-01-03", "type": "read-import", "id": "arxiv:2601.00001"},
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
    assert remote.subscribed_ids() == ["arxiv:2601.00001", "arxiv:2601.00002"]
    assert remote.scan_events() == [
        {"t": "2026-01-03", "type": "read-import", "id": "arxiv:2601.00001"},
    ]
    remote.refresh_events()   # a no-op, but part of the interface


def test_event_order_survives_asynchronous_delivery(tmp_path):
    make_data_dir(tmp_path, [make_doc("2601.00001"), make_doc("2601.00002")])
    remote = make_remote(tmp_path)

    remote.record_events([{"type": "start", "n": 2}])
    remote.record_events([{"type": "view", "id": "arxiv:2601.00001"}])
    remote.record_events([{"type": "view", "id": "arxiv:2601.00002"}])
    remote.record_events([{"type": "end"}])
    remote.close()

    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert [e.get("id", e["type"]) for e in events] == [
        "start", "arxiv:2601.00001", "arxiv:2601.00002", "end",
    ]


def _failing_remote(tmp_path, handler, retry_waits) -> RemoteStore:
    """A RemoteStore whose transport is `handler`, spooling into tmp_path."""
    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://testserver",
    )
    return RemoteStore(
        "http://testserver",
        client=client,
        spool_path=str(tmp_path / "unsent-events.jsonl"),
        retry_waits=retry_waits,
    )


def test_event_posting_retries_transient_failures(tmp_path, capsys):
    posts = []

    def handler(request):
        posts.append(request)
        if len(posts) == 1:
            raise httpx.ConnectError("transient blip")
        return httpx.Response(200, json={"recorded": 1})

    remote = _failing_remote(tmp_path, handler, retry_waits=(0.0, 0.0))
    remote.record_events([{"type": "view", "id": "arxiv:2601.00001"}])
    remote.close()

    assert len(posts) == 2   # first attempt failed, retry delivered
    assert not (tmp_path / "unsent-events.jsonl").exists()
    assert "warning" not in capsys.readouterr().out


def test_undeliverable_events_spool_with_a_notice(tmp_path, capsys):
    def handler(request):
        raise httpx.ConnectError("server down")

    remote = _failing_remote(tmp_path, handler, retry_waits=(0.0,))
    remote.record_events([{"type": "start", "n": 1}])
    remote.record_events([{"type": "view", "id": "arxiv:2601.00001"}])
    remote.close()

    spool_path = tmp_path / "unsent-events.jsonl"
    events = [
        json.loads(line) for line in spool_path.read_text().splitlines()
    ]
    assert [e["type"] for e in events] == ["start", "view"]
    assert all("t" in e for e in events)   # stamped at recording time
    out = capsys.readouterr().out
    assert "2 events were not delivered" in out
    assert str(spool_path) in out


def test_status_round_trip(tmp_path):
    make_data_dir(
        tmp_path,
        [make_doc("2601.00001", date="2026-01-01")],
        events=[
            {"t": "2026-01-03T10:00:00", "type": "view", "id": "arxiv:2601.00001"},
        ],
    )
    remote = make_remote(tmp_path)

    status = remote.status()

    assert status["url"] == "http://testserver"
    assert "server_started" in status
    assert status["watermark"] == "2026-01-01"
    assert status["subscribed_papers"] == 1
    assert status["seen_papers"] == 1
    assert status["events"] == 1
    assert status["harvests"] == []   # no harvest log on this data dir


def test_notice_if_slow_prints_only_when_slow(capsys):
    import time

    from firehose.store import _notice_if_slow

    with _notice_if_slow("waiting...", delay=0.01):
        time.sleep(0.05)
    assert "waiting..." in capsys.readouterr().out

    with _notice_if_slow("waiting...", delay=0.5):
        pass
    time.sleep(0.05)   # were the timer leaked, it would fire soon after
    assert capsys.readouterr().out == ""
