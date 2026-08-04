"""
Smoke test for firehose.vis. The visualisation entry points query the store
off disk (no network), so one can be driven end-to-end against a tmp data
dir. This guards the path-resolution wiring (config -> data_paths -> store)
-- the class of regression a util rename/removal silently introduces in an
entry point that the pure-helper unit tests never call.

Plus unit tests for the renderers (render_scan_time, _scan_time_legend,
render_status). The data-shaping these render is covered in test_stats.py.
"""
import datetime
import json

from conftest import make_data_dir, make_doc
from firehose import stats, vis


def _config(tmp_path) -> str:
    (tmp_path / "config.toml").write_text(
        '[paths]\ndata = "unused"\n'
        '[scan]\nmodern_cutoff = 2025-04-15\n'
        '[sources.arxiv]\ncategories = ["cs:cs:LG"]\n'
    )
    return str(tmp_path / "config.toml")


def test_all_submitted_years_runs_against_tmp_store(tmp_path, capsys):
    config_path = _config(tmp_path)
    make_data_dir(tmp_path, [
        make_doc("2501.00001", date="2025-01-01"),
        make_doc("2601.00002", date="2026-01-02"),
    ])

    vis.all_submitted_years(
        config_path=config_path,
        data_dir=str(tmp_path),
    )

    out = capsys.readouterr().out
    assert "2025 (1 papers)" in out
    assert "2026 (1 papers)" in out


def test_render_scan_time_has_totals_row():
    events = [
        {"t": "2026-06-22T11:00:00", "type": "start", "n": 1},
        {"t": "2026-06-22T11:00:04", "type": "view", "xid": "a"},
        {"t": "2026-06-22T11:00:10", "type": "end"},
    ]
    out = vis.render_scan_time(stats.summarise_scan_time(events))
    assert "2026-06-22" in out
    total = next(line for line in out.splitlines() if line.startswith("TOTAL"))
    # sessions and papers columns both read 1
    assert total.split() == ["TOTAL", "1", "1", "0:00:10", "10.00s"]


def test_scan_time_legend_names_both_ends():
    # magenta end is always 0:00:00; cyan end is the busiest day (625s -> 0:10:25)
    label = str(vis._scan_time_legend(625.0)).splitlines()[0]
    assert label == "time spent: (magenta = 0:00:00, cyan = 0:10:25)"


def test_scan_time_entry_point_runs_against_tmp_events(tmp_path, capsys):
    # end-to-end through the shell (heatmap off, so no terminal needed): guards
    # the config -> data_paths -> store wiring, like the years smoke test.
    config_path = _config(tmp_path)
    with open(tmp_path / "events.jsonl", "w") as f:
        for event in [
            {"t": "2026-06-22T11:00:00", "type": "start", "n": 1},
            {"t": "2026-06-22T11:00:04", "type": "view", "xid": "a"},
            {"t": "2026-06-22T11:00:10", "type": "end"},
        ]:
            f.write(json.dumps(event) + "\n")

    vis.scan_time(
        config_path=config_path,
        data_dir=str(tmp_path),
        heatmap=False,
    )
    out = capsys.readouterr().out
    total = next(line for line in out.splitlines() if line.startswith("TOTAL"))
    assert total.split()[:3] == ["TOTAL", "1", "1"]  # sessions, papers


def test_scan_time_entry_point_no_scans(tmp_path, capsys):
    config_path = _config(tmp_path)
    vis.scan_time(
        config_path=config_path,
        data_dir=str(tmp_path),
        heatmap=False,
    )
    assert "no scans recorded yet." in capsys.readouterr().out


def test_scan_time_notes_untimed_imported_reads(tmp_path, capsys):
    config_path = _config(tmp_path)
    with open(tmp_path / "events.jsonl", "w") as f:
        for event in [
            {"t": "2025-04-23", "type": "read-import", "xid": "old"},
            {"t": "2026-06-22T11:00:00", "type": "start", "n": 1},
            {"t": "2026-06-22T11:00:04", "type": "view", "xid": "a"},
            {"t": "2026-06-22T11:00:10", "type": "end"},
        ]:
            f.write(json.dumps(event) + "\n")

    vis.scan_time(
        config_path=config_path,
        data_dir=str(tmp_path),
        heatmap=False,
    )
    assert "skipping 1 papers without timing data" in capsys.readouterr().out


def test_render_status_remote_snapshot():
    out = vis.render_status({
        "url": "http://nook:8377",
        "server_started": "2026-08-04T04:07:12.123456",
        "data_dir": "/srv/firehose/data",
        "watermark": "2026-08-04",
        "subscribed_papers": 910000,
        "seen_papers": 133200,
        "events": 133842,
        "last_event": {
            "t": "2026-08-04T09:12:33.500000",
            "type": "view",
            "xid": "2508.01234",
        },
        "harvests": [
            {
                "t": "2026-08-03T04:06:48.900000",
                "t_start": "2026-08-03T04:00:01.000000",
                "counts": {"new": 900, "updated": 3},
                "watermark": "2026-08-03", "papers": 3130000,
                "completed": False,
            },
            {
                "t": "2026-08-04T04:07:12.900000",
                "t_start": "2026-08-04T04:00:01.000000",
                "counts": {"new": 1204, "updated": 33},
                "watermark": "2026-08-04", "papers": 3130412,
                "completed": True,
            },
        ],
    })
    assert "server: http://nook:8377 (running since 2026-08-04T04:07:12)" in out
    assert "mirror: watermark 2026-08-04, 3,130,412 papers" in out
    assert "subscribed: 910,000 papers, 133,200 seen" in out
    assert "events: 133,842, last view 2508.01234 at 2026-08-04T09:12:33" in out
    assert (
        "* 2026-08-04T04:00:01 .. 2026-08-04T04:07:12:"
        " new: 1204, updated: 33"
    ) in out
    # the interrupted run is marked
    assert "new: 900, updated: 3 [partial]" in out


def test_render_status_empty_local_data_dir():
    out = vis.render_status({
        "data_dir": "/tmp/fresh",
        "watermark": None,
        "subscribed_papers": None,
        "seen_papers": 0,
        "events": 0,
        "last_event": None,
        "harvests": [],
    })
    assert "data: /tmp/fresh" in out
    assert "mirror: no index" in out
    assert "events: 0" in out
    assert "recent harvests: none recorded" in out


def test_status_entry_point_runs_against_tmp_store(tmp_path, capsys):
    config_path = _config(tmp_path)
    make_data_dir(tmp_path, [make_doc("2601.00001", date="2026-01-01")])

    vis.status(config_path=config_path, data_dir=str(tmp_path))

    out = capsys.readouterr().out
    assert f"data: {tmp_path}" in out
    assert "mirror: watermark 2026-01-01" in out
    assert "subscribed: 1 papers, 0 seen" in out
    assert "recent harvests: none recorded" in out


def test_unread_entry_point_runs_against_tmp_data(tmp_path, capsys):
    # drives the full store + stats + calendar-render path off disk
    config_path = _config(tmp_path)
    make_data_dir(
        tmp_path,
        [
            make_doc("2501.00001", date="2025-01-01"),   # pre-cutoff: dropped
            make_doc("2601.00002", date="2026-01-02"),
            make_doc("2601.00003", date="2026-01-02"),
        ],
        events=[
            {"t": "2026-01-03T10:00:00", "type": "view", "xid": "2601.00003"},
        ],
    )

    vis.unread(
        config_path=config_path,
        data_dir=str(tmp_path),
    )

    assert "found 1 unread papers" in capsys.readouterr().out
