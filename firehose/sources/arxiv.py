"""
The arXiv source adapter (see firehose.sources for the interface it
implements): fetching via arXiv's OAI-PMH feed in the arXivRaw metadata
format, a mirror sharded by submission month, and the arXiv
document→display mapping.
"""

import datetime
import time

from firehose import ids
from firehose import index
from firehose import util
from firehose.paper import Paper
from firehose.sources import Record
from firehose.sources import arxivraw


# arXiv's OAI-PMH endpoint, shared by the adapter and `firehose classes`.
OAI_API_URL = "https://oaipmh.arxiv.org/oai"

# Politeness: at most one record batch is pulled per 1/MAX_RPS seconds.
MAX_RPS = 1/3
BATCH_SIZE = 3_500

# The OAI client library is imported on first use rather than here: this
# module is imported by the CLI on every command, and only harvesting
# (a server-side job) actually needs it.
Sickle = None
NoRecordsMatch = None


def _load_sickle():
    global Sickle, NoRecordsMatch
    if Sickle is None:
        from sickle import Sickle as sickle_client
        from sickle.oaiexceptions import NoRecordsMatch as no_records_match
        Sickle = sickle_client
        NoRecordsMatch = no_records_match


def setspec_to_category(setspec: str) -> str:
    """Translate an OAI setSpec (the form categories take in the config) to
    a category name: "cs:cs:AI" -> "cs.AI", "physics:hep-th" -> "hep-th"."""
    return ".".join(setspec.split(":")[1:])


def _client():
    """An OAI client; retries ride out transient 503s (the server sets
    Retry-After) so long unattended runs survive them. The generous read
    timeout is for deep ListRecords pages, which the server can take tens
    of seconds to prepare; timeouts still get through it (they abort the
    run, the index checkpoint keeps the watermark, and a rerun resumes)."""
    _load_sickle()
    return Sickle(OAI_API_URL, max_retries=10, timeout=180)


class ArxivAdapter:

    source = "arxiv"

    def earliest_watermark(self) -> datetime.date:
        """The archive's earliest OAI datestamp."""
        return util.to_date(_client().Identify().earliestDatestamp)

    def fetch(self, watermark: datetime.date):
        """
        Yield batches of Records for everything arXiv touched since the
        watermark (inclusive, so the watermark day is re-fetched: upserts
        make the overlap harmless). All categories are fetched — the
        mirror stores all of arXiv, and subscription filters at query
        time.
        """
        client = _client()
        try:
            records = client.ListRecords(
                metadataPrefix="arXivRaw",
                **{"from": util.to_datestamp(watermark)},
            )
        except NoRecordsMatch:
            return
        last_request_time = time.time()
        while True:
            # rate limit before pulling each batch
            next_request_time = last_request_time + 1/MAX_RPS + 0.5
            wait_time = next_request_time - time.time()
            if wait_time > 0:
                time.sleep(wait_time)
            last_request_time = time.time()
            batch = []
            for _, record in zip(range(BATCH_SIZE), records):
                try:
                    parsed = arxivraw.parse_record(record.xml)
                    batch.append(Record(
                        id=parsed.xid,
                        datestamp=parsed.datestamp,
                        doc=parsed.doc,
                    ))
                except Exception as e:
                    batch.append(e)
            if batch:
                yield batch
            if len(batch) < BATCH_SIZE:
                return

    def shard(self, xid: str, date: datetime.date | None = None) -> str:
        """The submission-month (YYMM) shard name for a paper id: the
        part before the dot for modern ids ("2003.14184" -> "2003"), the
        first four digits after the slash for pre-2007 ids
        ("math/0211159" -> "0211"). The id encodes its own month, so the
        entry date is unused."""
        if "/" in xid:
            return xid.split("/", 1)[1][:4]
        return xid.split(".", 1)[0]

    def subscription(self, section: dict):
        """The subscribed-entry predicate from the [sources.arxiv] config
        section: entries sharing a category with the section's
        `categories` list (OAI setSpecs)."""
        subscribed = {setspec_to_category(s) for s in section["categories"]}
        return lambda entry: bool(set(entry.categories) & subscribed)

    def entry(self, doc: dict) -> index.Entry:
        """A paper's index entry: submission date and categories."""
        return index.Entry(
            date=arxivraw.submitted_date(doc),
            categories=tuple(doc.get("categories", ())),
        )

    def datestamp(self, doc: dict) -> datetime.date:
        """When arXiv last touched the record (its OAI datestamp)."""
        return datetime.date.fromisoformat(doc["oai_datestamp"])

    def to_paper(self, doc: dict) -> Paper:
        return Paper.from_mirror_doc(
            doc, paper_id=ids.join(self.source, doc["id"]),
        )


ADAPTER = ArxivAdapter()
