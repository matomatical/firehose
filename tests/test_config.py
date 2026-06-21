import datetime
import os
import textwrap

from firehose import util


def _write(tmp_path, body):
    cfg = tmp_path / "config.toml"
    cfg.write_text(textwrap.dedent(body))
    return str(cfg)


def test_subscribed_classes_ignores_commented_entries(tmp_path):
    """Subscribed = uncommented entries in [arxiv].categories. Commented-out
    lines (the available-but-not-followed catalog) and inline `# name` notes are
    TOML comments, so they are dropped by the parser."""
    path = _write(tmp_path, """
        [arxiv]
        categories = [
            "cs:cs:AI",      # Artificial Intelligence
          # "cs:cs:AR",      # Hardware Architecture  (not followed)
            "cs:cs:LG",
            "stat:stat:ML",
        ]
    """)
    assert util.subscribed_classes(util.load_config(path)) == {
        "cs:cs:AI",
        "cs:cs:LG",
        "stat:stat:ML",
    }


def test_resolve_paths_from_config(tmp_path):
    path = _write(tmp_path, """
        [arxiv]
        categories = []
        [paths]
        data = "mydata"
        downloads = "/pdfs"
    """)
    p = util.resolve_paths(util.load_config(path))
    assert p.data_dir == "mydata"
    assert p.cache == os.path.join("mydata", "arxiv.txt")
    assert p.readlog == os.path.join("mydata", "readlog.txt")
    assert p.scanlog == os.path.join("mydata", "scanlog.jsonl")
    assert p.downloads == "/pdfs"


def test_resolve_paths_cli_override_wins(tmp_path):
    path = _write(tmp_path, """
        [arxiv]
        categories = []
        [paths]
        data = "mydata"
        downloads = "/pdfs"
    """)
    p = util.resolve_paths(
        util.load_config(path), data_dir="other", download_dir="/elsewhere"
    )
    assert p.data_dir == "other"
    assert p.downloads == "/elsewhere"


def test_resolve_paths_defaults_when_no_paths_section(tmp_path):
    path = _write(tmp_path, """
        [arxiv]
        categories = []
    """)
    p = util.resolve_paths(util.load_config(path))
    assert p.data_dir == util.DEFAULT_DATA_DIR
    assert p.downloads == os.path.expanduser(util.DEFAULT_DOWNLOAD_DIR)


def test_modern_cutoff_from_config(tmp_path):
    # a bare YYYY-MM-DD in TOML parses straight to a datetime.date
    path = _write(tmp_path, """
        [arxiv]
        categories = []
        [scan]
        modern_cutoff = 2025-04-15
    """)
    assert util.modern_cutoff(util.load_config(path)) == datetime.date(2025, 4, 15)


def test_modern_cutoff_defaults_when_absent(tmp_path):
    path = _write(tmp_path, """
        [arxiv]
        categories = []
    """)
    assert util.modern_cutoff(util.load_config(path)) == util.DEFAULT_MODERN_CUTOFF
