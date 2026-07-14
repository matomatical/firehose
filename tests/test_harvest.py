"""Network-free tests for the OAI harvest boundary."""

import datetime
import types

import pytest

from firehose import harvest as harvest_module
from firehose import util


def _record(
    xid,
    *,
    updated,
    submitted=None,
    deleted=False,
    categories=("cs:cs:AI",),
):
    record = types.SimpleNamespace(
        deleted=deleted,
        header=types.SimpleNamespace(
            identifier=harvest_module.OAI_ID_PREFIX + xid,
            datestamp=updated,
            setSpecs=list(categories),
        ),
    )
    if not deleted:
        record.metadata = {"date": [submitted]}
    return record


def _configure_harvest(monkeypatch, tmp_path, records):
    """Point harvest at a temporary cache and a deterministic fake OAI server."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[paths]\ndata = "data"\n'
        '[arxiv]\ncategories = ["cs:cs:AI"]\n'
    )

    class FakeSickle:
        def __init__(self, endpoint):
            assert endpoint == util.OAI_API_URL

        def Identify(self):
            return types.SimpleNamespace(earliestDatestamp="2005-09-16")

        def ListRecords(self, **kwargs):
            assert kwargs["metadataPrefix"] == "oai_dc"
            return iter(records)

    monkeypatch.setattr(harvest_module, "Sickle", FakeSickle)
    monkeypatch.setattr(harvest_module, "BATCH_SIZE", len(records) + 1)
    monkeypatch.setattr(harvest_module.time, "sleep", lambda _: None)
    return config_path, tmp_path / "data" / "arxiv.txt"


def test_harvest_removes_deleted_record_and_advances_checkpoint(
    tmp_path, monkeypatch,
):
    deleted = "2606.00001"
    kept = "2606.00002"
    new = "2606.00003"
    config_path, cache_path = _configure_harvest(monkeypatch, tmp_path, [
        _record(deleted, updated="2026-06-02", deleted=True),
        _record(new, updated="2026-06-03", submitted="2026-06-01"),
        _record(
            "2606.00004",
            updated="2026-06-04",
            submitted="2026-06-02",
            categories=("math:math:AG",),
        ),
    ])
    cache_path.parent.mkdir()
    util.save_cache(str(cache_path), datetime.date(2026, 6, 1), {
        deleted: datetime.date(2026, 5, 31),
        kept: datetime.date(2026, 6, 1),
    })

    harvest_module.harvest(config_path=str(config_path))

    cache, latest = util.load_cache(str(cache_path))
    assert cache == {
        kept: datetime.date(2026, 6, 1),
        new: datetime.date(2026, 6, 1),
    }
    assert latest == datetime.date(2026, 6, 4)


def test_harvest_checkpoints_then_reraises_unexpected_error(
    tmp_path, monkeypatch,
):
    good = "2606.00001"
    malformed = _record(
        "2606.00002", updated="2026-06-03", submitted="2026-06-02",
    )
    malformed.metadata = {}
    config_path, cache_path = _configure_harvest(monkeypatch, tmp_path, [
        _record(good, updated="2026-06-02", submitted="2026-06-01"),
        malformed,
    ])

    with pytest.raises(KeyError, match="date"):
        harvest_module.harvest(config_path=str(config_path))

    # The successfully processed prefix is durable, but the checkpoint stays at
    # its last successful record so the malformed record is not skipped.
    cache, latest = util.load_cache(str(cache_path))
    assert cache == {good: datetime.date(2026, 6, 1)}
    assert latest == datetime.date(2026, 6, 2)
