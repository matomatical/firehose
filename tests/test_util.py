"""
Tests for firehose.util: the data-file I/O (cache + readlog), the pure
date/name/filename formatting, and the scanlog event writer. All pure or
plain-file I/O, so no terminal, network, or clipboard is needed.
"""

import datetime
import json
import os
import types

from firehose import util


# -- cache save/load round-trip + on-disk format -------------------------------

# The cache (data/arxiv.txt) is firehose's master paper index, so a save/load
# regression is the highest-consequence bug in the repo. These tests pin the
# exact on-disk format and the prefix handling.
#
# NB: save_cache writes ids with the OAI prefix ("oai:arXiv.org:") stripped, and
# load_cache(strip_prefix=False) re-adds it. save_cache assumes its input keys
# carry that prefix (harvest always holds prefixed keys), which is the usage
# exercised here.

def _prefixed(xid: str) -> str:
    return "oai:arXiv.org:" + xid


def test_save_cache_writes_expected_on_disk_format(tmp_path):
    path = str(tmp_path / "arxiv.txt")
    cache = {
        _prefixed("2508.00002"): datetime.date(2025, 8, 12),
        _prefixed("cs/9301111"): datetime.date(1990, 1, 1),
    }
    util.save_cache(path, datetime.date(2026, 3, 5), cache)

    lines = open(path).read().splitlines()
    # one header line, then "<bare-id> <YYYY-MM-DD>" sorted by (date, id), prefix
    # stripped. This is the real format of data/arxiv.txt (the README's extra
    # "number of papers:" header line is stale: the code neither writes nor reads it).
    assert lines == [
        "latest datestamp: 2026-03-05",
        "cs/9301111 1990-01-01",
        "2508.00002 2025-08-12",
    ]


def test_cache_round_trip_preserves_entries_and_latest_date(tmp_path):
    path = str(tmp_path / "arxiv.txt")
    cache = {
        _prefixed("2508.00002"): datetime.date(2025, 8, 12),
        _prefixed("cs/9301111"): datetime.date(1990, 1, 1),
    }
    latest = datetime.date(2026, 3, 5)
    util.save_cache(path, latest, cache)

    # default load re-adds the prefix -> identity round-trip
    loaded, loaded_latest = util.load_cache(path)
    assert loaded == cache
    assert loaded_latest == latest

    # strip_prefix=True yields the bare ids that sample/vis consume
    bare, _ = util.load_cache(path, strip_prefix=True)
    assert bare == {
        "2508.00002": datetime.date(2025, 8, 12),
        "cs/9301111": datetime.date(1990, 1, 1),
    }


def test_cache_round_trip_empty(tmp_path):
    path = str(tmp_path / "arxiv.txt")
    latest = datetime.date(2026, 3, 5)
    util.save_cache(path, latest, {})
    assert open(path).read() == "latest datestamp: 2026-03-05\n"
    loaded, loaded_latest = util.load_cache(path)
    assert loaded == {}
    assert loaded_latest == latest


# -- readlog -------------------------------------------------------------------

def test_load_readlog(tmp_path):
    path = tmp_path / "readlog.txt"
    path.write_text("2504.15284 2025-04-23\n2504.15286 2025-04-24\n")
    assert util.load_readlog(str(path)) == {
        "2504.15284": datetime.date(2025, 4, 23),
        "2504.15286": datetime.date(2025, 4, 24),
    }


def test_load_readlog_duplicate_id_keeps_last(tmp_path):
    # readlog is a dict keyed by id; a repeated id takes the later date.
    path = tmp_path / "readlog.txt"
    path.write_text("2504.15284 2025-04-23\n2504.15284 2025-05-01\n")
    assert util.load_readlog(str(path)) == {"2504.15284": datetime.date(2025, 5, 1)}


# -- date helpers --------------------------------------------------------------

def test_to_date_parses_iso():
    assert util.to_date("2025-08-13") == datetime.date(2025, 8, 13)


def test_date_datestamp_round_trip():
    d = datetime.date(2026, 1, 9)
    assert util.to_date(util.to_datestamp(d)) == d
    assert util.to_datestamp(d) == "2026-01-09"  # zero-padded, fixed width


# -- name / filename formatting ------------------------------------------------

def _result(author_names, year, title):
    """A duck-typed stand-in for an arxiv result (just what to_name reads)."""
    return types.SimpleNamespace(
        authors=[types.SimpleNamespace(name=n) for n in author_names],
        published=types.SimpleNamespace(year=year),
        title=title,
    )


def test_to_name_single_author():
    assert util.to_name(_result(["Jane Smith"], 2026, "Deep Nets")) == "Smith2026 Deep Nets"


def test_to_name_two_authors_joined():
    assert util.to_name(
        _result(["Jane Smith", "Bo Lee"], 2026, "Deep Nets")
    ) == "Smith+Lee2026 Deep Nets"


def test_to_name_three_or_more_authors_truncates():
    # >2 authors: first surname + "+", the rest dropped
    assert util.to_name(
        _result(["Jane Smith", "Bo Lee", "Al Wu"], 2026, "Deep Nets")
    ) == "Smith+2026 Deep Nets"


def test_to_name_uses_last_whitespace_token_as_surname():
    assert util.to_name(_result(["Jane van der Berg"], 2026, "X")) == "Berg2026 X"


def test_to_filename_sanitizes_dot_in_modern_id():
    # '.' is not in the allowed character set, so it becomes '_'
    assert util.to_filename("Smith+Lee2026 Deep Nets", "2508.09137v1") \
        == "Smith+Lee2026 Deep Nets [2508_09137v1].pdf"


def test_to_filename_sanitizes_slash_in_old_style_id():
    assert util.to_filename("Author1996 Survey", "cs/9605103v1") \
        == "Author1996 Survey [cs_9605103v1].pdf"


def test_to_filename_sanitizes_colon_in_title():
    assert util.to_filename("Smith2026 Title: A Study", "2508.09137v1") \
        == "Smith2026 Title_ A Study [2508_09137v1].pdf"


# -- scanlog event writer ------------------------------------------------------

def test_log_event_appends_json_lines_with_timestamp(tmp_path):
    path = str(tmp_path / "scanlog.jsonl")
    util.log_event(path, {"type": "view", "xid": "2508.00001"})
    util.log_event(path, {"type": "save", "xid": "2508.00001"})

    records = [json.loads(line) for line in open(path)]
    assert len(records) == 2
    assert records[0]["type"] == "view" and records[0]["xid"] == "2508.00001"
    assert records[1]["type"] == "save"
    # every record is timestamped with an ISO-8601 "t" the analytics side parses back
    for r in records:
        datetime.datetime.fromisoformat(r["t"])


def test_log_event_creates_missing_parent_dir(tmp_path):
    path = str(tmp_path / "nested" / "dir" / "scanlog.jsonl")
    util.log_event(path, {"type": "start", "n": 3})
    assert os.path.exists(path)
    assert json.loads(open(path).read())["n"] == 3
