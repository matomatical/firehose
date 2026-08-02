"""
Parsing arXiv OAI-PMH records (arXivRaw metadata format) into firehose's
per-paper JSON documents.

A document is a plain dict, JSON-serialisable, with fields taken verbatim
from arXivRaw plus two conventions:

* `categories` is split into a list (primary category first, then
  cross-lists); `versions` is a list of objects with ISO-8601 UTC dates.
* `oai_datestamp` (added, from the OAI envelope) records when arXiv last
  touched the record; the harvest watermark is the maximum of these.

Absent optional fields are omitted, not null. Field order is fixed here (and
nowhere else) so that serialising a document is deterministic: byte-comparing
files detects real changes. Scalar fields have internal whitespace collapsed
(arXivRaw hard-wraps long values), except the abstract, which is preserved
verbatim apart from surrounding whitespace so paragraph breaks survive.
"""

import datetime
import email.utils
from dataclasses import dataclass


# OAI record identifiers carry this prefix (e.g. "oai:arXiv.org:2603.04402").
# Strip it at parse time so documents and the index deal in bare ids.
OAI_ID_PREFIX = "oai:arXiv.org:"

# Known arXivRaw scalar fields, in document order. Unknown fields the format
# may grow are kept too: they sort after these, before `versions`.
_FIELD_ORDER = (
    "id",
    "title",
    "authors",
    "categories",
    "abstract",
    "comments",
    "license",
    "doi",
    "journal-ref",
    "report-no",
    "msc-class",
    "acm-class",
    "submitter",
    "proxy",
)


@dataclass
class ParsedRecord:
    """One OAI record, parsed: `doc` is None iff the record is deleted
    upstream (paper withdrawn from arXiv's OAI feed)."""
    xid: str
    datestamp: datetime.date
    doc: dict | None


def parse_record(record) -> ParsedRecord:
    """
    Parse an OAI `<record>` XML element (lxml or stdlib ElementTree, any
    namespace prefixes) holding arXivRaw metadata into a ParsedRecord.
    """
    header = _child(record, "header")
    identifier = _text(_child(header, "identifier"))
    xid = identifier.removeprefix(OAI_ID_PREFIX)
    datestamp = datetime.date.fromisoformat(_text(_child(header, "datestamp")))
    if header.get("status") == "deleted":
        return ParsedRecord(xid=xid, datestamp=datestamp, doc=None)

    metadata = _child(record, "metadata")
    raw = next(child for child in metadata if isinstance(child.tag, str))

    fields = {}
    versions = []
    for child in raw:
        if not isinstance(child.tag, str):
            continue  # XML comments / processing instructions
        tag = _local(child.tag)
        if tag == "version":
            versions.append({
                "version": child.get("version"),
                "date": _version_date(child),
                "size": _grandchild_text(child, "size"),
                "source_type": _grandchild_text(child, "source_type"),
            })
        elif tag == "abstract":
            fields[tag] = _text(child)
        else:
            fields[tag] = " ".join(_text(child).split())

    doc = {"id": fields.pop("id", xid)}
    for key in _FIELD_ORDER[1:]:
        if key in fields:
            doc[key] = fields.pop(key)
    for key in sorted(fields):
        doc[key] = fields[key]
    if "categories" in doc:
        doc["categories"] = doc["categories"].split()
    doc["versions"] = versions
    doc["oai_datestamp"] = datestamp.isoformat()
    return ParsedRecord(xid=xid, datestamp=datestamp, doc=doc)


def submitted_date(doc) -> datetime.date:
    """The paper's submission date: the date of its first version."""
    versions = doc.get("versions", [])
    if not versions or not versions[0].get("date"):
        raise ValueError(f"no v1 date for {doc.get('id')!r}")
    return datetime.date.fromisoformat(versions[0]["date"][:10])


# # #
# XML helpers, namespace-agnostic (match on local tag names, so they work on
# lxml and stdlib elements alike and don't hardcode namespace URIs)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element, name: str):
    for child in element:
        if isinstance(child.tag, str) and _local(child.tag) == name:
            return child
    raise ValueError(f"no <{name}> in <{_local(element.tag)}>")


def _text(element) -> str:
    return (element.text or "").strip()


def _grandchild_text(element, name: str) -> str | None:
    for child in element:
        if isinstance(child.tag, str) and _local(child.tag) == name:
            return _text(child) or None
    return None


def _version_date(version_element) -> str | None:
    """A version's submission instant, RFC 822 in the feed ("Mon, 12 Jun 2017
    17:57:34 GMT"), normalised to ISO 8601 UTC; None when absent."""
    raw = _grandchild_text(version_element, "date")
    if raw is None:
        return None
    parsed = email.utils.parsedate_to_datetime(raw)
    return parsed.astimezone(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
