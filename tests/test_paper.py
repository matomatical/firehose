"""
Tests for firehose.paper: building Papers from mirror documents, the authors
display-string split, and the "Surname+Year Title" naming.
"""

import datetime

from conftest import make_doc
from firehose.paper import Paper, split_authors, to_name


def test_from_mirror_doc_maps_fields():
    doc = make_doc(
        "2601.00001",
        date="2026-01-02",
        categories=("cs.LG", "cs.AI"),
        comments="10 pages, 3 figures",
    )
    p = Paper.from_mirror_doc(doc)

    assert p.xid == "2601.00001"
    assert p.xidv == "2601.00001v1"
    assert p.entry_id == "http://arxiv.org/abs/2601.00001v1"
    assert p.title == "Title 2601.00001"
    assert p.authors == ["Ada Author", "Bo Boauthor"]
    assert p.categories == ["cs.LG", "cs.AI"]
    assert p.summary == "A summary."
    assert p.comment == "10 pages, 3 figures"
    assert p.published.date() == datetime.date(2026, 1, 2)
    assert p.name == "Author+Boauthor2026 Title 2601.00001"


def test_from_mirror_doc_versions():
    doc = make_doc("2601.00001", versions=[
        {"version": "v1", "date": "2026-01-02T00:00:00+00:00",
         "size": None, "source_type": None},
        {"version": "v3", "date": "2026-02-10T12:00:00+00:00",
         "size": None, "source_type": None},
    ])
    p = Paper.from_mirror_doc(doc)

    assert p.xidv == "2601.00001v3"          # latest version
    assert p.published.date() == datetime.date(2026, 1, 2)
    assert p.updated.date() == datetime.date(2026, 2, 10)


def test_from_mirror_doc_tolerates_missing_version_date():
    doc = make_doc("2601.00001", versions=[
        {"version": "v1", "date": None, "size": None, "source_type": None},
    ])
    p = Paper.from_mirror_doc(doc)

    assert p.published is None
    assert p.name.startswith("Author+Boauthor???? ")


def test_split_authors_commas_and_and():
    assert split_authors("Ada Author") == ["Ada Author"]
    assert split_authors("Ada Author and Bo Boauthor") == [
        "Ada Author", "Bo Boauthor",
    ]
    assert split_authors("Ada Author, Bo Boauthor and Cy Coauthor") == [
        "Ada Author", "Bo Boauthor", "Cy Coauthor",
    ]
    assert split_authors("Ada Author, Bo Boauthor, and Cy Coauthor") == [
        "Ada Author", "Bo Boauthor", "Cy Coauthor",
    ]
    assert split_authors("") == []


def test_to_name_author_forms():
    # 1: Surname; 2: Surname1+Surname2; >2: Surname1+
    assert to_name(["Ada Author"], 2026, "T") == "Author2026 T"
    assert to_name(["Ada Author", "Bo Boauthor"], 2026, "T") == (
        "Author+Boauthor2026 T"
    )
    assert to_name(
        ["Ada Author", "Bo Boauthor", "Cy Coauthor"], 2026, "T"
    ) == "Author+2026 T"


def test_to_name_uses_last_whitespace_token_as_surname():
    assert to_name(["Jane van der Berg"], 2026, "X") == "Berg2026 X"
