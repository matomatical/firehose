"""
Tests for firehose.util: the data-file I/O (cache + readlog), the pure
date/name/filename formatting, and the scanlog event writer. All pure or
plain-file I/O, so no terminal, network, or clipboard is needed.
"""

import datetime
import json
import os
import types

import pytest
import requests

from firehose import util


# -- data path resolution ------------------------------------------------------

# data_paths joins the data-dir filenames onto a data dir, which comes from an
# explicit override else [paths].data in the config. (test_config.py's
# resolve_paths tests went with that helper; these cover its replacement, so the
# path wiring keeps a test after the rename.)

def test_data_paths_from_config():
    p = util.data_paths({"paths": {"data": "mydata"}})
    assert p.data_dir == "mydata"
    assert p.cache == os.path.join("mydata", "arxiv.txt")
    assert p.readlog == os.path.join("mydata", "readlog.txt")
    assert p.scanlog == os.path.join("mydata", "scanlog.jsonl")


def test_data_paths_override_wins():
    p = util.data_paths({"paths": {"data": "mydata"}}, data_dir="other")
    assert p.data_dir == "other"
    assert p.cache == os.path.join("other", "arxiv.txt")


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


# -- cache save/load round-trip + on-disk format -------------------------------

# The cache (data/arxiv.txt) is firehose's master paper index, so a save/load
# regression is the highest-consequence bug in the repo. These tests pin the
# exact on-disk format. Ids are stored and loaded bare; the OAI prefix is a
# harvest-only concern and never reaches save_cache/load_cache.

def test_save_cache_writes_expected_on_disk_format(tmp_path):
    path = str(tmp_path / "arxiv.txt")
    cache = {
        "2508.00002": datetime.date(2025, 8, 12),
        "2508.00001": datetime.date(2025, 8, 12),  # shares a date
        "cs/9301111": datetime.date(1990, 1, 1),
    }
    util.save_cache(path, datetime.date(2026, 3, 5), cache)

    lines = open(path).read().splitlines()
    # the "latest datestamp" header, then ids grouped under a "<date>:" header,
    # sorted by (date, id). Ids sharing a date sit under one header rather than
    # repeating the date on every line. (The README's extra "number of papers:"
    # line is stale: the code neither writes nor reads it.)
    assert lines == [
        "latest datestamp: 2026-03-05",
        "1990-01-01:",
        "cs/9301111",
        "2025-08-12:",
        "2508.00001",
        "2508.00002",
    ]
    assert not list(tmp_path.glob(".firehose-cache-*.tmp"))


def test_save_cache_interruption_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "arxiv.txt"
    previous = "latest datestamp: 2026-03-04\n2026-03-04:\n2603.00001\n"
    path.write_text(previous)

    def fail_mid_write(f, dated_ids):
        f.write("partial replacement\n")
        raise RuntimeError("interrupted")

    monkeypatch.setattr(util, "_write_grouped", fail_mid_write)

    with pytest.raises(RuntimeError, match="interrupted"):
        util.save_cache(
            str(path),
            datetime.date(2026, 3, 5),
            {"2603.00002": datetime.date(2026, 3, 5)},
        )

    assert path.read_text() == previous
    assert not list(tmp_path.glob(".firehose-cache-*.tmp"))


def test_cache_round_trip_preserves_entries_and_latest_date(tmp_path):
    path = str(tmp_path / "arxiv.txt")
    cache = {
        "2508.00002": datetime.date(2025, 8, 12),
        "2508.00001": datetime.date(2025, 8, 12),  # exercise a group
        "cs/9301111": datetime.date(1990, 1, 1),
    }
    latest = datetime.date(2026, 3, 5)
    util.save_cache(path, latest, cache)

    loaded, loaded_latest = util.load_cache(path)
    assert loaded == cache               # ids round-trip bare, identity
    assert loaded_latest == latest


def test_load_cache_reads_grouped(tmp_path):
    # the loader reads bare ids dated by the "<date>:" header above them; the
    # first line is always the "latest datestamp" watermark.
    path = str(tmp_path / "arxiv.txt")
    open(path, "w").write(
        "latest datestamp: 2026-03-05\n"
        "1990-01-01:\n"
        "cs/9301111\n"
        "2025-08-12:\n"          # date header -> the two bare ids below share it
        "2508.00001\n"
        "2508.00002\n"
    )
    cache, latest = util.load_cache(path)
    assert latest == datetime.date(2026, 3, 5)
    assert cache == {
        "cs/9301111": datetime.date(1990, 1, 1),
        "2508.00001": datetime.date(2025, 8, 12),
        "2508.00002": datetime.date(2025, 8, 12),
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
    assert last_date == datetime.date(2025, 4, 24)   # seeds the live appender


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


# -- grouped readlog appender (live scan path) ---------------------------------

def test_append_readlog_writes_grouped_and_threads_open_date(tmp_path):
    path = str(tmp_path / "readlog.txt")
    d1, d2 = datetime.date(2026, 6, 20), datetime.date(2026, 6, 21)
    open_date = None                                          # fresh file
    open_date = util.append_readlog(path, "a", d1, open_date)  # header d1 + a
    open_date = util.append_readlog(path, "b", d1, open_date)  # just b (same day)
    open_date = util.append_readlog(path, "c", d2, open_date)  # header d2 + c
    assert open_date == d2
    assert open(path).read() == "2026-06-20:\na\nb\n2026-06-21:\nc\n"
    assert util.load_readlog(path)[0] == {"a": d1, "b": d1, "c": d2}


def test_append_readlog_resumes_same_day_group(tmp_path):
    # seeding open_date from load_readlog lets a second same-day session
    # continue the day's group instead of writing a duplicate header
    path = str(tmp_path / "readlog.txt")
    d = datetime.date(2026, 6, 21)
    util.append_readlog(path, "a", d, None)                    # header d + a
    _, open_date = util.load_readlog(path)                     # = d, as on resume
    util.append_readlog(path, "b", d, open_date)               # just b
    assert open(path).read() == "2026-06-21:\na\nb\n"


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


# -- PDF download --------------------------------------------------------------

class _FakeDownloadResponse:
    def __init__(self, *, headers=None, chunks=(), status_error=None):
        self.headers = headers or {}
        self.chunks = chunks
        self.status_error = status_error
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def raise_for_status(self):
        if self.status_error is not None:
            raise self.status_error

    def iter_content(self, chunk_size):
        assert chunk_size == 64 * 1024
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk


@pytest.mark.parametrize("headers", [{}, {"content-length": "not-a-number"}])
def test_download_paper_tolerates_unknown_content_length(
    tmp_path, monkeypatch, headers,
):
    response = _FakeDownloadResponse(headers=headers, chunks=[b"%PDF", b" body"])
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(requests, "get", get)
    path = tmp_path / "paper.pdf"

    util.download_paper("2607.00001", str(path))

    assert path.read_bytes() == b"%PDF body"
    assert not list(tmp_path.glob(".firehose-*.part"))
    assert response.closed
    assert calls == [(
        "https://arxiv.org/pdf/2607.00001.pdf",
        {"stream": True, "timeout": util.DOWNLOAD_TIMEOUT},
    )]


def test_download_paper_rejects_http_error_without_creating_file(
    tmp_path, monkeypatch,
):
    response = _FakeDownloadResponse(
        headers={"content-length": "3"},
        chunks=[b"ERR"],
        status_error=requests.HTTPError("404 Not Found"),
    )
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response)
    path = tmp_path / "paper.pdf"

    with pytest.raises(requests.HTTPError, match="404"):
        util.download_paper("missing", str(path))

    assert not path.exists()
    assert not list(tmp_path.glob(".firehose-*.part"))
    assert response.closed


def test_download_paper_cleans_partial_and_preserves_destination(
    tmp_path, monkeypatch,
):
    response = _FakeDownloadResponse(chunks=[
        b"partial",
        requests.ConnectionError("connection lost"),
    ])
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response)
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"existing")

    with pytest.raises(requests.ConnectionError, match="connection lost"):
        util.download_paper("2607.00001", str(path))

    assert path.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".firehose-*.part"))
    assert response.closed


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


# -- scanlog reader ------------------------------------------------------------

def test_load_scanlog_round_trips_log_event(tmp_path):
    # load_scanlog is the inverse of log_event: it yields the written events
    # (with the stamped "t") in chronological order.
    path = str(tmp_path / "scanlog.jsonl")
    util.log_event(path, {"type": "start", "n": 2})
    util.log_event(path, {"type": "view", "xid": "2508.00001"})
    util.log_event(path, {"type": "end"})

    events = util.load_scanlog(path)
    assert [e["type"] for e in events] == ["start", "view", "end"]
    assert events[0]["n"] == 2 and events[1]["xid"] == "2508.00001"
    assert all("t" in e for e in events)


def test_load_scanlog_skips_blank_lines(tmp_path):
    path = tmp_path / "scanlog.jsonl"
    path.write_text('{"t": "2026-06-22T11:00:00", "type": "view", "xid": "a"}\n\n')
    assert len(util.load_scanlog(str(path))) == 1


def test_load_scanlog_missing_file_is_empty(tmp_path):
    # no scans recorded yet -> [], not a crash (the file is created on first scan)
    assert util.load_scanlog(str(tmp_path / "absent.jsonl")) == []
