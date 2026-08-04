"""Tests for the source-adapter registry and the arXiv adapter's
per-document mappings and PDF grabbing (its fetch loop is exercised via
test_harvest)."""

import datetime

import pytest
import requests

from conftest import make_doc
from firehose import index
from firehose import sources
from firehose.sources import arxiv as arxiv_module


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
    # the filename sanitiser replaces the ids' dots
    assert adapter.filename(paper) == (
        "Author+Boauthor2026 Title 2601_00001 [2601_00001v1].pdf"
    )


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
def test_download_pdf_tolerates_unknown_content_length(
    tmp_path, monkeypatch, headers,
):
    response = _FakeDownloadResponse(headers=headers, chunks=[b"%PDF", b" body"])
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(requests, "get", get)
    path = tmp_path / "paper.pdf"

    completed_progress = arxiv_module.download_pdf("2607.00001", str(path))

    assert path.read_bytes() == b"%PDF body"
    assert completed_progress.startswith("downloaded ★:")
    assert "100%" in completed_progress
    assert not list(tmp_path.glob(".firehose-*.part"))
    assert response.closed
    assert calls == [(
        "https://arxiv.org/pdf/2607.00001.pdf",
        {"stream": True, "timeout": arxiv_module.DOWNLOAD_TIMEOUT},
    )]


def test_download_pdf_rejects_http_error_without_creating_file(
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
        arxiv_module.download_pdf("missing", str(path))

    assert not path.exists()
    assert not list(tmp_path.glob(".firehose-*.part"))
    assert response.closed


def test_download_pdf_cleans_partial_and_preserves_destination(
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
        arxiv_module.download_pdf("2607.00001", str(path))

    assert path.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".firehose-*.part"))
    assert response.closed


def test_download_pdf_labels_live_progress(tmp_path, monkeypatch):
    response = _FakeDownloadResponse(
        headers={"content-length": "4"},
        chunks=[b"%PDF"],
    )
    progress_kwargs = {}

    class FakeBar:
        def __init__(self, **kwargs):
            progress_kwargs.update(kwargs)
            self.total = kwargs["total"]
            self.n = 0
            self.desc = kwargs["desc"]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def update(self, amount):
            self.n += amount

        def __str__(self):
            return f"{self.desc}: 100%|bar| {self.n}/{self.total}"

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response)
    monkeypatch.setattr(arxiv_module.tqdm, "tqdm", FakeBar)

    completed_progress = arxiv_module.download_pdf(
        "2607.00001", str(tmp_path / "paper.pdf")
    )

    assert progress_kwargs["desc"] == "downloading..."
    assert progress_kwargs["total"] == 4
    assert completed_progress == "downloaded ★: 100%|bar| 4/4"
