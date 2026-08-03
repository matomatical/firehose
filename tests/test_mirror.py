"""Tests for the month-archive metadata mirror."""

import gzip
import json
import os

from firehose import mirror


def _doc(xid, title="A Title"):
    return {
        "id": xid,
        "title": title,
        "categories": ["cs.AI"],
        "versions": [
            {
                "version": "v1",
                "date": "2020-03-28T03:22:52Z",
                "size": "33kb",
                "source_type": "D",
            },
        ],
        "oai_datestamp": "2026-07-28",
    }


def test_shard_and_paths():
    assert mirror.shard("2003.14184") == "2003"
    assert mirror.shard("math/0211159") == "0211"
    assert mirror.shard("math.GT/0309136") == "0309"
    assert mirror.month_path("m", "2003") == "m/2003.jsonl.gz"


def test_save_load_month_roundtrip(tmp_path):
    mirror_dir = str(tmp_path)
    docs = {
        "2003.14184": _doc("2003.14184", title="Ünïcode Title"),
        "2003.00001": _doc("2003.00001"),
    }
    mirror.save_month(mirror_dir, "2003", docs)

    assert mirror.load_month(mirror_dir, "2003") == docs
    assert mirror.load_month(mirror_dir, "1999") == {}
    assert mirror.read_paper(mirror_dir, "2003.14184")["title"] == (
        "Ünïcode Title"
    )
    assert mirror.read_paper(mirror_dir, "2003.99999") is None

    # one line per doc, sorted by id, unicode stored raw (not escaped)
    with gzip.open(mirror.month_path(mirror_dir, "2003"), "rt") as f:
        lines = f.read().splitlines()
    assert [json.loads(line)["id"] for line in lines] == [
        "2003.00001", "2003.14184",
    ]
    assert "Ünïcode" in lines[1]


def test_save_month_bytes_are_deterministic(tmp_path):
    # equal documents -> byte-identical archives, however arrived at (the
    # gzip timestamp is zeroed and lines are sorted by id)
    docs = {xid: _doc(xid) for xid in ["2003.14184", "2003.00001"]}
    mirror.save_month(str(tmp_path), "2003", docs)
    first = open(mirror.month_path(str(tmp_path), "2003"), "rb").read()
    mirror.save_month(str(tmp_path), "2003", dict(reversed(docs.items())))
    second = open(mirror.month_path(str(tmp_path), "2003"), "rb").read()
    assert first == second


def test_save_month_empty_removes_archive(tmp_path):
    mirror_dir = str(tmp_path)
    mirror.save_month(mirror_dir, "2003", {"2003.00001": _doc("2003.00001")})
    assert mirror.months(mirror_dir) == ["2003"]
    mirror.save_month(mirror_dir, "2003", {})
    assert mirror.months(mirror_dir) == []
    mirror.save_month(mirror_dir, "2003", {})   # already absent: no error


def test_read_papers_groups_by_month(tmp_path):
    mirror_dir = str(tmp_path)
    xids = ["2003.14184", "math/0211159", "2003.00001"]
    for xid in xids:
        updater = mirror.Updater(mirror_dir)
        updater.upsert(_doc(xid))
        updater.flush()

    found = mirror.read_papers(mirror_dir, xids + ["2003.99999"])
    assert set(found) == set(xids)
    assert found["math/0211159"]["id"] == "math/0211159"


def test_iter_papers_sorted_with_no_temp_leftovers(tmp_path):
    mirror_dir = str(tmp_path)
    updater = mirror.Updater(mirror_dir)
    for xid in ["2003.14184", "math/0211159", "2003.00001"]:
        updater.upsert(_doc(xid))
    updater.flush()

    assert [d["id"] for d in mirror.iter_papers(mirror_dir)] == [
        "math/0211159", "2003.00001", "2003.14184",
    ]
    # atomic writes leave no temporary files behind
    assert all(
        name.endswith(".jsonl.gz") for name in os.listdir(mirror_dir)
    )


def test_updater_upsert_delete_statuses(tmp_path):
    mirror_dir = str(tmp_path)
    doc = _doc("2003.14184")

    updater = mirror.Updater(mirror_dir)
    assert updater.upsert(doc) == "new"
    assert updater.upsert(doc) == "unchanged"
    revised = _doc("2003.14184", title="A New Title")
    assert updater.upsert(revised) == "updated"
    updater.flush()
    assert mirror.read_paper(mirror_dir, "2003.14184")["title"] == (
        "A New Title"
    )

    # a fresh updater sees the flushed state
    updater = mirror.Updater(mirror_dir)
    assert updater.upsert(revised) == "unchanged"
    assert updater.delete("2003.14184") is True
    assert updater.delete("2003.14184") is False
    updater.flush()
    assert mirror.read_paper(mirror_dir, "2003.14184") is None
    assert mirror.months(mirror_dir) == []   # emptied month archive removed


def test_updater_flush_only_rewrites_dirty_months(tmp_path):
    mirror_dir = str(tmp_path)
    mirror.save_month(mirror_dir, "2003", {"2003.00001": _doc("2003.00001")})
    untouched = os.stat(mirror.month_path(mirror_dir, "2003")).st_mtime_ns

    updater = mirror.Updater(mirror_dir)
    assert updater.upsert(_doc("2003.00001")) == "unchanged"   # loads, no dirt
    updater.upsert(_doc("2004.00001"))
    updater.flush()

    assert os.stat(mirror.month_path(mirror_dir, "2003")).st_mtime_ns == (
        untouched
    )
    assert mirror.months(mirror_dir) == ["2003", "2004"]


def test_serialisation_is_deterministic():
    assert mirror.dumps_doc(_doc("2003.14184")) == mirror.dumps_doc(
        _doc("2003.14184")
    )
    parsed = json.loads(mirror.dumps_doc(_doc("2003.14184")))
    assert parsed == _doc("2003.14184")
