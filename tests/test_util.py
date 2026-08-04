"""
Tests for firehose.util: the data-file I/O (cache + readlog), the pure
date/name/filename formatting, and the event-log writer. All pure or
plain-file I/O, so no terminal, network, or clipboard is needed.
"""

import datetime
import json
import os
import types

import pytest

from firehose import util


# -- data path resolution ------------------------------------------------------

# data_paths joins the data-dir filenames onto a data dir, which comes from an
# explicit override else [paths].data in the config. (test_config.py's
# resolve_paths tests went with that helper; these cover its replacement, so the
# path wiring keeps a test after the rename.)

def test_data_paths_from_config():
    p = util.data_paths({"paths": {"data": "mydata"}})
    assert p.data_dir == "mydata"
    assert p.index("arxiv") == os.path.join("mydata", "index", "arxiv.txt")
    assert p.mirror("arxiv") == os.path.join("mydata", "mirror", "arxiv")
    assert p.events == os.path.join("mydata", "events.jsonl")


def test_data_paths_override_wins():
    p = util.data_paths({"paths": {"data": "mydata"}}, data_dir="other")
    assert p.data_dir == "other"
    assert p.index("arxiv") == os.path.join("other", "index", "arxiv.txt")


def test_data_paths_expands_user():
    p = util.data_paths({"paths": {"data": "~/d"}})
    assert p.data_dir == os.path.expanduser("~/d")
    q = util.data_paths({"paths": {"data": "ignored"}}, data_dir="~/e")
    assert q.data_dir == os.path.expanduser("~/e")


# load_config anchors relative [paths] values to the config file's own
# directory (not the CWD), so firehose reads/writes the same data wherever it
# is invoked from; ~ and absolute paths are left as the user meant them.

def test_load_config_anchors_relative_paths_to_config_dir(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[paths]\ndata = "data"\ndownloads = "dl"\n')
    config = util.load_config(str(cfg))
    assert config["paths"]["data"] == str(tmp_path / "data")
    assert config["paths"]["downloads"] == str(tmp_path / "dl")


def test_load_config_leaves_absolute_and_user_paths(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[paths]\ndata = "/var/data"\ndownloads = "~/dl"\n')
    config = util.load_config(str(cfg))
    assert config["paths"]["data"] == "/var/data"
    assert config["paths"]["downloads"] == os.path.expanduser("~/dl")


def test_load_config_tolerates_missing_paths_keys(tmp_path):
    # test_vis smoke configs omit downloads; a bare config must not KeyError.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[paths]\ndata = "data"\n')
    config = util.load_config(str(cfg))
    assert config["paths"]["data"] == str(tmp_path / "data")
    assert "downloads" not in config["paths"]


# -- readlog (retired format, kept readable for the one-off import) ------------

def test_load_readlog_missing_file_is_empty(tmp_path):
    path = tmp_path / "readlog.txt"

    assert util.load_readlog(str(path)) == ({}, None)
    assert not path.exists()  # loading remains a read-only operation


def test_load_readlog(tmp_path):
    path = tmp_path / "readlog.txt"
    path.write_text("2025-04-23:\n2504.15284\n2025-04-24:\n2504.15286\n")
    readlog, last_date = util.load_readlog(str(path))
    assert readlog == {
        "2504.15284": datetime.date(2025, 4, 23),
        "2504.15286": datetime.date(2025, 4, 24),
    }
    assert last_date == datetime.date(2025, 4, 24)


def test_load_readlog_duplicate_id_keeps_last(tmp_path):
    # readlog is a dict keyed by id; a repeated id takes the later date.
    path = tmp_path / "readlog.txt"
    path.write_text("2025-04-23:\n2504.15284\n2025-05-01:\n2504.15284\n")
    assert util.load_readlog(str(path))[0] == {"2504.15284": datetime.date(2025, 5, 1)}


def test_load_readlog_reads_grouped(tmp_path):
    # the grouped form: a "<date>:" header dates the bare ids beneath it. An id
    # appearing under a later group still takes the later date (dict keeps last).
    path = tmp_path / "readlog.txt"
    path.write_text(
        "2025-04-23:\n"
        "2504.15284\n"
        "2504.15286\n"
        "2025-05-01:\n"
        "2504.15284\n"
    )
    readlog, last_date = util.load_readlog(str(path))
    assert readlog == {
        "2504.15284": datetime.date(2025, 5, 1),
        "2504.15286": datetime.date(2025, 4, 23),
    }
    assert last_date == datetime.date(2025, 5, 1)   # date of the final entry


# -- date helpers --------------------------------------------------------------

def test_to_date_parses_iso():
    assert util.to_date("2025-08-13") == datetime.date(2025, 8, 13)


def test_date_datestamp_round_trip():
    d = datetime.date(2026, 1, 9)
    assert util.to_date(util.to_datestamp(d)) == d
    assert util.to_datestamp(d) == "2026-01-09"  # zero-padded, fixed width




def test_to_filename_sanitizes_dot_in_modern_id():
    # '.' is not in the allowed character set, so it becomes '_'
    assert util.to_filename("Smith+Lee2026 Deep Nets", "2508.09137v1", ".pdf") \
        == "Smith+Lee2026 Deep Nets [2508_09137v1].pdf"


def test_to_filename_sanitizes_slash_in_old_style_id():
    assert util.to_filename("Author1996 Survey", "cs/9605103v1", ".pdf") \
        == "Author1996 Survey [cs_9605103v1].pdf"


def test_to_filename_sanitizes_colon_in_title():
    assert util.to_filename("Smith2026 Title: A Study", "2508.09137v1", ".html") \
        == "Smith2026 Title_ A Study [2508_09137v1].html"


# -- clipboard -----------------------------------------------------------------

class _FakeClipboardProcess:
    def __init__(self, returncode):
        self.returncode = returncode
        self.input = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def communicate(self, input):
        self.input = input


@pytest.mark.parametrize(("returncode", "copied"), [(0, True), (1, False)])
def test_copy_to_clipboard_checks_process_exit_status(
    monkeypatch, returncode, copied,
):
    process = _FakeClipboardProcess(returncode)
    monkeypatch.setattr(util.sys, "platform", "darwin")
    monkeypatch.setattr(util.subprocess, "Popen", lambda *args, **kwargs: process)

    assert util.copy_to_clipboard("a title") is copied
    assert process.input == b"a title"


# -- event-log writer ------------------------------------------------------

def test_log_event_appends_json_lines_with_timestamp(tmp_path):
    path = str(tmp_path / "events.jsonl")
    util.log_event(path, {"type": "view", "id": "arxiv:2508.00001"})
    util.log_event(path, {"type": "save", "id": "arxiv:2508.00001"})

    records = [json.loads(line) for line in open(path)]
    assert len(records) == 2
    assert records[0]["type"] == "view" and records[0]["id"] == "arxiv:2508.00001"
    assert records[1]["type"] == "save"
    # every record is timestamped with an ISO-8601 "t" the analytics side parses back
    for r in records:
        datetime.datetime.fromisoformat(r["t"])


def test_log_event_creates_missing_parent_dir(tmp_path):
    path = str(tmp_path / "nested" / "dir" / "events.jsonl")
    util.log_event(path, {"type": "start", "n": 3})
    assert os.path.exists(path)
    assert json.loads(open(path).read())["n"] == 3


# -- event-log reader ------------------------------------------------------------

def test_load_events_round_trips_log_event(tmp_path):
    # load_events is the inverse of log_event: it yields the written events
    # (with the stamped "t") in chronological order.
    path = str(tmp_path / "events.jsonl")
    util.log_event(path, {"type": "start", "n": 2})
    util.log_event(path, {"type": "view", "id": "arxiv:2508.00001"})
    util.log_event(path, {"type": "end"})

    events = util.load_events(path)
    assert [e["type"] for e in events] == ["start", "view", "end"]
    assert events[0]["n"] == 2 and events[1]["id"] == "arxiv:2508.00001"
    assert all("t" in e for e in events)


def test_load_events_skips_blank_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"t": "2026-06-22T11:00:00", "type": "view", "id": "arxiv:a"}\n\n')
    assert len(util.load_events(str(path))) == 1


def test_load_events_missing_file_is_empty(tmp_path):
    # no scans recorded yet -> [], not a crash (the file is created on first scan)
    assert util.load_events(str(tmp_path / "absent.jsonl")) == []
