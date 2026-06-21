import textwrap

from firehose import util


def test_load_my_classes_reads_uncommented_categories(tmp_path):
    """Subscribed = uncommented entries in [arxiv].categories. Commented-out
    lines (the available-but-not-followed catalog) and inline `# name` notes are
    TOML comments, so they are ignored."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(textwrap.dedent("""
        [arxiv]
        categories = [
            "cs:cs:AI",      # Artificial Intelligence
          # "cs:cs:AR",      # Hardware Architecture  (not followed)
            "cs:cs:LG",
            "stat:stat:ML",
        ]
    """))
    assert util.load_my_classes(str(cfg)) == {
        "cs:cs:AI",
        "cs:cs:LG",
        "stat:stat:ML",
    }
