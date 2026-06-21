"""
Smoke test for firehose.vis. The visualisation entry points read the cache and
readlog off disk (no network), so one can be driven end-to-end against a tmp
data dir. This guards the path-resolution wiring (config -> data_paths ->
load_cache) -- the class of regression a util rename/removal silently introduces
in an entry point that the pure-helper unit tests never call.
"""
import datetime

from firehose import util, vis


def test_all_submitted_years_runs_against_tmp_cache(tmp_path, capsys):
    (tmp_path / "config.toml").write_text('[paths]\ndata = "unused"\n')
    cache = {
        "oai:arXiv.org:2501.00001": datetime.date(2025, 1, 1),
        "oai:arXiv.org:2601.00002": datetime.date(2026, 1, 2),
    }
    util.save_cache(str(tmp_path / "arxiv.txt"), datetime.date(2026, 1, 2), cache)

    vis.all_submitted_years(
        config_path=str(tmp_path / "config.toml"),
        data_dir=str(tmp_path),
    )

    out = capsys.readouterr().out
    assert "2025 (1 papers)" in out
    assert "2026 (1 papers)" in out
