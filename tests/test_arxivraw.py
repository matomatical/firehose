"""Tests for parsing OAI arXivRaw records into per-paper documents."""

import datetime

import pytest
from lxml import etree

from firehose import arxivraw


def _record(xid, datestamp, metadata=None, deleted=False):
    status = ' status="deleted"' if deleted else ""
    metadata_block = (
        f"<metadata>{metadata}</metadata>" if metadata is not None else ""
    )
    return etree.fromstring(
        f'<record xmlns="http://www.openarchives.org/OAI/2.0/">'
        f"<header{status}>"
        f"<identifier>oai:arXiv.org:{xid}</identifier>"
        f"<datestamp>{datestamp}</datestamp>"
        f"<setSpec>cs</setSpec>"
        f"</header>"
        f"{metadata_block}"
        f"</record>"
    )


RAW_NS = 'xmlns="http://arxiv.org/OAI/arXivRaw/"'

MODERN_METADATA = f"""
<!-- a comment the parser must skip -->
<arXivRaw {RAW_NS}>
  <id>1706.03762</id>
  <submitter>Llion Jones</submitter>
  <version version="v1">
    <date>Mon, 12 Jun 2017 17:57:34 GMT</date>
    <size>1102kb</size>
    <source_type>D</source_type>
  </version>
  <version version="v2">
    <date>Mon, 19 Jun 2017 16:49:45 GMT</date>
    <size>1124kb</size>
    <source_type>D</source_type>
  </version>
  <title>Attention Is
     All You Need</title>
  <authors>Ashish Vaswani, Noam Shazeer</authors>
  <categories>cs.CL cs.LG</categories>
  <comments>15 pages, 5 figures</comments>
  <license>http://arxiv.org/licenses/nonexclusive-distrib/1.0/</license>
  <doi>10.5555/12345</doi>
  <shiny-new-field>future-proofing</shiny-new-field>
  <abstract>  First paragraph
of the abstract.

Second paragraph.  </abstract>
</arXivRaw>
"""


def test_parse_modern_record():
    parsed = arxivraw.parse_record(
        _record("1706.03762", "2023-08-03", MODERN_METADATA)
    )
    assert parsed.xid == "1706.03762"
    assert parsed.datestamp == datetime.date(2023, 8, 3)
    doc = parsed.doc
    # canonical key order: known fields, then unknown, then versions and
    # the datestamp
    assert list(doc.keys()) == [
        "id", "title", "authors", "categories", "abstract", "comments",
        "license", "doi", "submitter", "shiny-new-field", "versions",
        "oai_datestamp",
    ]
    # scalar whitespace collapsed; abstract paragraphs preserved
    assert doc["title"] == "Attention Is All You Need"
    assert doc["abstract"] == "First paragraph\nof the abstract.\n\nSecond paragraph."
    assert doc["categories"] == ["cs.CL", "cs.LG"]
    assert doc["versions"] == [
        {
            "version": "v1",
            "date": "2017-06-12T17:57:34Z",
            "size": "1102kb",
            "source_type": "D",
        },
        {
            "version": "v2",
            "date": "2017-06-19T16:49:45Z",
            "size": "1124kb",
            "source_type": "D",
        },
    ]
    assert doc["oai_datestamp"] == "2023-08-03"
    assert arxivraw.submitted_date(doc) == datetime.date(2017, 6, 12)


OLD_STYLE_METADATA = f"""
<arXivRaw {RAW_NS}>
  <id>math/0211159</id>
  <submitter>Grisha Perelman</submitter>
  <version version="v1">
    <date>Mon, 11 Nov 2002 16:11:49 GMT</date>
    <size>33kb</size>
  </version>
  <title>The entropy formula for the Ricci flow</title>
  <authors>Grisha Perelman</authors>
  <categories>math.DG</categories>
  <msc-class>53C</msc-class>
  <abstract>We present a monotonic expression for the Ricci flow.</abstract>
</arXivRaw>
"""


def test_parse_old_style_record_omits_absent_fields():
    parsed = arxivraw.parse_record(
        _record("math/0211159", "2005-09-17", OLD_STYLE_METADATA)
    )
    doc = parsed.doc
    assert doc["id"] == "math/0211159"
    assert "license" not in doc
    assert "comments" not in doc
    assert doc["msc-class"] == "53C"
    assert doc["versions"][0]["source_type"] is None
    assert arxivraw.submitted_date(doc) == datetime.date(2002, 11, 11)


def test_parse_deleted_record():
    parsed = arxivraw.parse_record(
        _record("1234.56789", "2026-01-01", deleted=True)
    )
    assert parsed.xid == "1234.56789"
    assert parsed.datestamp == datetime.date(2026, 1, 1)
    assert parsed.doc is None


DATELESS_METADATA = f"""
<arXivRaw {RAW_NS}>
  <id>2600.00001</id>
  <version version="v1"><size>1kb</size></version>
  <title>t</title>
  <authors>a</authors>
  <categories>cs.AI</categories>
  <abstract>x</abstract>
</arXivRaw>
"""


def test_versions_without_dates_parse_but_have_no_submitted_date():
    parsed = arxivraw.parse_record(
        _record("2600.00001", "2026-01-01", DATELESS_METADATA)
    )
    assert parsed.doc["versions"][0]["date"] is None
    with pytest.raises(ValueError):
        arxivraw.submitted_date(parsed.doc)
