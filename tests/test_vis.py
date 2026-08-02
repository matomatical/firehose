"""
Smoke test for firehose.vis. The visualisation entry points read the cache and
readlog off disk (no network), so one can be driven end-to-end against a tmp
data dir. This guards the path-resolution wiring (config -> data_paths ->
load_cache) -- the class of regression a util rename/removal silently introduces
in an entry point that the pure-helper unit tests never call.

Plus unit tests for the renderers (render_scan_time, _scan_time_legend). The
data-shaping these render is covered in test_stats.py.
"""
import datetime

from firehose import stats, util, vis


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


def test_unread_entry_point_runs_against_tmp_data(tmp_path, capsys):
    # drives the full cache + readlog + stats + calendar-render path off disk
    (tmp_path / "config.toml").write_text(
        '[paths]\ndata = "unused"\n[scan]\nmodern_cutoff = 2025-04-15\n'
    )
    util.save_cache(str(tmp_path / "arxiv.txt"), datetime.date(2026, 1, 2), {
        "2501.00001": datetime.date(2025, 1, 1),   # pre-cutoff: dropped
        "2601.00002": datetime.date(2026, 1, 2),
        "2601.00003": datetime.date(2026, 1, 2),
    })
    util.append_readlog(
        str(tmp_path / "readlog.txt"),
        "2601.00003", datetime.date(2026, 1, 3), None,
    )

    vis.unread(
        config_path=str(tmp_path / "config.toml"),
        data_dir=str(tmp_path),
    )

    assert "found 1 unread papers" in capsys.readouterr().out
