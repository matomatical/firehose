"""Tests for the paper index (plain-text format, load/save, rebuild)."""

import datetime

import pytest

from firehose import index
from firehose import mirror


def _entries():
    return {
        "0705.0001": index.Entry(
            date=datetime.date(2007, 5, 21),
            categories=("cs.AI", "cs.LG"),
        ),
        "math/0211159": index.Entry(
            date=datetime.date(2002, 11, 11),
            categories=("math.DG",),
        ),
        "0705.0002": index.Entry(
            date=datetime.date(2007, 5, 21),
            categories=(),
        ),
    }


def test_save_load_roundtrip(tmp_path):
    path = str(tmp_path / "index.txt")
    watermark = datetime.date(2026, 8, 1)
    index.save_index(path=path, watermark=watermark, entries=_entries())
    loaded, loaded_watermark = index.load_index(path)
    assert loaded == _entries()
    assert loaded_watermark == watermark


def test_file_format_is_grouped_and_sorted(tmp_path):
    path = str(tmp_path / "index.txt")
    index.save_index(
        path=path,
        watermark=datetime.date(2026, 8, 1),
        entries=_entries(),
    )
    assert open(path).read() == (
        "latest datestamp: 2026-08-01\n"
        "2002-11-11:\n"
        "math/0211159 math.DG\n"
        "2007-05-21:\n"
        "0705.0001 cs.AI cs.LG\n"
        "0705.0002\n"
    )


def _write_config(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[paths]\ndata = "data"\n')
    return str(config_path)


def test_rebuild_index_from_mirror(tmp_path):
    config_path = _write_config(tmp_path)
    mirror_dir = str(tmp_path / "data" / "metadata")
    updater = mirror.Updater(mirror_dir)
    updater.upsert({
        "id": "2003.14184",
        "categories": ["math.PR", "cs.NA"],
        "versions": [{"version": "v1", "date": "2020-03-28T03:22:52Z"}],
        "oai_datestamp": "2026-07-28",
    })
    updater.upsert({
        "id": "math/0211159",
        "categories": ["math.DG"],
        "versions": [{"version": "v1", "date": "2002-11-11T16:11:49Z"}],
        "oai_datestamp": "2005-09-17",
    })
    updater.flush()

    index.rebuild_index(config_path=config_path)

    entries, watermark = index.load_index(str(tmp_path / "data" / "index.txt"))
    assert watermark == datetime.date(2026, 7, 28)
    assert entries == {
        "2003.14184": index.Entry(
            date=datetime.date(2020, 3, 28),
            categories=("math.PR", "cs.NA"),
        ),
        "math/0211159": index.Entry(
            date=datetime.date(2002, 11, 11),
            categories=("math.DG",),
        ),
    }


def test_rebuild_index_refuses_missing_or_empty_mirror(tmp_path):
    config_path = _write_config(tmp_path)
    with pytest.raises(SystemExit):
        index.rebuild_index(config_path=config_path)
    (tmp_path / "data" / "metadata").mkdir(parents=True)
    with pytest.raises(SystemExit):
        index.rebuild_index(config_path=config_path)
