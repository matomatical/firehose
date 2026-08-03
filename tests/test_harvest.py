"""Network-free tests for the metadata-mirror harvest (`firehose mirror`)."""

import datetime
import types

from lxml import etree

from firehose import arxivraw
from firehose import harvest as harvest_module
from firehose import index
from firehose import mirror as mirror_store
from firehose import util


def _raw_record(xid, *, datestamp, submitted=None, deleted=False, title="T"):
    """A fake sickle record carrying a real arXivRaw XML element."""
    if deleted:
        metadata = ""
        status = ' status="deleted"'
    else:
        metadata = (
            '<metadata><arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/">'
            f"<id>{xid}</id>"
            f'<version version="v1"><date>{submitted}</date></version>'
            f"<title>{title}</title>"
            "<authors>A. Author</authors>"
            "<categories>cs.AI math.ST</categories>"
            "<abstract>An abstract.</abstract>"
            "</arXivRaw></metadata>"
        )
        status = ""
    element = etree.fromstring(
        '<record xmlns="http://www.openarchives.org/OAI/2.0/">'
        f"<header{status}>"
        f"<identifier>oai:arXiv.org:{xid}</identifier>"
        f"<datestamp>{datestamp}</datestamp>"
        "</header>"
        f"{metadata}"
        "</record>"
    )
    return types.SimpleNamespace(xml=element)


def test_apply_upserts_and_deletes(tmp_path):
    mirror_dir = str(tmp_path)
    entries = {}
    updater = mirror_store.Updater(mirror_dir)
    live = arxivraw.parse_record(_raw_record(
        "2606.00001",
        datestamp="2026-06-02",
        submitted="Mon, 01 Jun 2026 10:00:00 GMT",
    ).xml)

    assert harvest_module._apply(live, entries, updater) == "new"
    assert entries["2606.00001"] == index.Entry(
        date=datetime.date(2026, 6, 1),
        categories=("cs.AI", "math.ST"),
    )
    assert harvest_module._apply(live, entries, updater) == "unchanged"

    revised = arxivraw.parse_record(_raw_record(
        "2606.00001",
        datestamp="2026-06-03",
        submitted="Mon, 01 Jun 2026 10:00:00 GMT",
        title="Revised",
    ).xml)
    assert harvest_module._apply(revised, entries, updater) == "updated"

    deleted = arxivraw.parse_record(_raw_record(
        "2606.00001", datestamp="2026-06-04", deleted=True,
    ).xml)
    assert harvest_module._apply(deleted, entries, updater) == "deleted"
    assert "2606.00001" not in entries
    assert harvest_module._apply(deleted, entries, updater) == (
        "deleted-absent"
    )
    updater.flush()
    assert mirror_store.read_paper(mirror_dir, "2606.00001") is None


def _configure_mirror(monkeypatch, tmp_path, records, expect_identify):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[paths]\ndata = "data"\n')

    class FakeSickle:
        def __init__(self, endpoint, **kwargs):
            assert endpoint == util.OAI_API_URL

        def Identify(self):
            assert expect_identify, "Identify called despite existing index"
            return types.SimpleNamespace(earliestDatestamp="2005-09-16")

        def ListRecords(self, **kwargs):
            assert kwargs["metadataPrefix"] == "arXivRaw"
            return iter(records)

    monkeypatch.setattr(harvest_module, "Sickle", FakeSickle)
    monkeypatch.setattr(harvest_module, "BATCH_SIZE", len(records) + 1)
    monkeypatch.setattr(harvest_module.time, "sleep", lambda _: None)
    return str(config_path), tmp_path / "data"


def test_mirror_end_to_end_then_resumes_from_watermark(tmp_path, monkeypatch):
    config_path, data_dir = _configure_mirror(monkeypatch, tmp_path, [
        _raw_record(
            "2606.00001",
            datestamp="2026-06-02",
            submitted="Mon, 01 Jun 2026 10:00:00 GMT",
        ),
        _raw_record("2606.99999", datestamp="2026-06-03", deleted=True),
    ], expect_identify=True)

    harvest_module.mirror(config_path=config_path)

    doc = mirror_store.read_paper(str(data_dir / "metadata"), "2606.00001")
    assert doc["title"] == "T"
    entries, watermark = index.load_index(str(data_dir / "index.txt"))
    assert watermark == datetime.date(2026, 6, 3)
    assert set(entries) == {"2606.00001"}

    # a second run loads the saved index (Identify would assert) and
    # continues from the watermark
    config_path, data_dir = _configure_mirror(monkeypatch, tmp_path, [
        _raw_record(
            "2606.00002",
            datestamp="2026-06-04",
            submitted="Tue, 02 Jun 2026 10:00:00 GMT",
        ),
    ], expect_identify=False)
    harvest_module.mirror(config_path=config_path)
    entries, watermark = index.load_index(str(data_dir / "index.txt"))
    assert set(entries) == {"2606.00001", "2606.00002"}
    assert watermark == datetime.date(2026, 6, 4)


def test_mirror_survives_malformed_record(tmp_path, monkeypatch):
    bad = _raw_record(
        "2606.00013",
        datestamp="2026-06-02",
        submitted="Mon, 01 Jun 2026 10:00:00 GMT",
    )
    bad.xml = etree.fromstring("<record><header></header></record>")
    config_path, data_dir = _configure_mirror(monkeypatch, tmp_path, [
        bad,
        _raw_record(
            "2606.00014",
            datestamp="2026-06-03",
            submitted="Mon, 01 Jun 2026 11:00:00 GMT",
        ),
    ], expect_identify=True)

    harvest_module.mirror(config_path=config_path)

    entries, watermark = index.load_index(str(data_dir / "index.txt"))
    assert set(entries) == {"2606.00014"}
    assert watermark == datetime.date(2026, 6, 3)
