"""Tests for the per-paper metadata mirror."""

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
    assert mirror.paper_path("m", "2003.14184") == "m/2003/2003.14184.json"
    assert (
        mirror.paper_path("m", "math.GT/0309136")
        == "m/0309/math.GT_0309136.json"
    )


def test_write_read_delete_roundtrip(tmp_path):
    mirror_dir = str(tmp_path)
    doc = _doc("2003.14184", title="Ünïcode Title")

    assert mirror.write_paper(mirror_dir, doc) == "new"
    assert mirror.read_paper(mirror_dir, doc["id"]) == doc
    # unicode stored raw, not escaped; file ends with a newline
    blob = open(mirror.paper_path(mirror_dir, doc["id"]), encoding="utf-8").read()
    assert "Ünïcode" in blob
    assert blob.endswith("}\n")

    assert mirror.write_paper(mirror_dir, doc) == "unchanged"
    doc["title"] = "A New Title"
    assert mirror.write_paper(mirror_dir, doc) == "updated"
    assert mirror.read_paper(mirror_dir, doc["id"])["title"] == "A New Title"

    assert mirror.delete_paper(mirror_dir, doc["id"]) is True
    assert mirror.delete_paper(mirror_dir, doc["id"]) is False
    assert mirror.read_paper(mirror_dir, doc["id"]) is None


def test_iter_and_count_papers_sorted_with_no_temp_leftovers(tmp_path):
    mirror_dir = str(tmp_path)
    xids = ["2003.14184", "math/0211159", "2003.00001"]
    for xid in xids:
        mirror.write_paper(mirror_dir, _doc(xid))

    assert mirror.count_papers(mirror_dir) == 3
    assert [d["id"] for d in mirror.iter_papers(mirror_dir)] == [
        "math/0211159", "2003.00001", "2003.14184",
    ]
    # atomic writes leave no temporary files behind
    for dirpath, _, filenames in os.walk(mirror_dir):
        for filename in filenames:
            assert filename.endswith(".json"), filename


def test_serialisation_is_deterministic():
    assert mirror.dumps_doc(_doc("2003.14184")) == mirror.dumps_doc(
        _doc("2003.14184")
    )
    parsed = json.loads(mirror.dumps_doc(_doc("2003.14184")))
    assert parsed == _doc("2003.14184")
