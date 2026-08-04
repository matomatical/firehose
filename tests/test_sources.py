"""Tests for the source-adapter registry and the arXiv adapter's
per-document mappings (its fetch loop is exercised via test_harvest)."""

import datetime

import pytest

from conftest import make_doc
from firehose import index
from firehose import sources


def test_adapter_registry():
    assert sources.adapter("arxiv").source == "arxiv"
    with pytest.raises(ValueError):
        sources.adapter("gopherspace")


def test_arxiv_shard_rule():
    shard = sources.adapter("arxiv").shard
    assert shard("2003.14184") == "2003"
    assert shard("math/0211159") == "0211"
    assert shard("math.GT/0309136") == "0309"


def test_arxiv_document_mappings():
    adapter = sources.adapter("arxiv")
    doc = make_doc("2601.00001", date="2026-01-02", categories=("cs.LG",))

    assert adapter.entry(doc) == index.Entry(
        date=datetime.date(2026, 1, 2), categories=("cs.LG",),
    )
    assert adapter.datestamp(doc) == datetime.date(2026, 1, 2)
    paper = adapter.to_paper(doc)
    assert paper.xid == "2601.00001"
    assert paper.title == "Title 2601.00001"
