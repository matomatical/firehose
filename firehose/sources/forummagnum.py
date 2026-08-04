"""
The ForumMagnum source adapter (see firehose.sources for the interface it
implements). LessWrong and the EA Forum run the same ForumMagnum software,
so one adapter class covers both, instantiated per site.

Posts are fetched from the site's GraphQL "views" API in monthly
`postedAt` windows; each fetch re-pulls a trailing month so karma, comment
counts, and edits refresh for recent posts (idempotent upserts absorb the
overlap). Documents are post metadata plus a plaintext excerpt — the post
body is not mirrored; grabbing a post fetches its full `htmlBody` at that
moment instead. The mirror shards by posted month, and the index
carries tag slugs plus "~"-prefixed pseudo-tags for the site's own flags
(so e.g. Alignment Forum membership is a query-time filter, and the "~"
keeps the flags clear of the sites' user-created tag slugs).

Caveats this adapter works around or accepts:

* The API silently truncates any query at 5000 results, so a full window
  is an error (abort rather than mirror a hole) — monthly windows run
  ~7x below the cap at current peak volume.
* There is no upstream deletion signal: a deleted post simply stops
  appearing, and its mirrored copy stays until removed by hand.
* There is no schema stability guarantee: the query below asks for
  exactly what it needs, and any GraphQL error is fatal rather than
  worked around.
* The sites' GraphQL layers differ: the EA Forum's silently ignores
  view terms bound through GraphQL variables (returning the newest
  posts whatever window was asked for), so the terms are inlined as
  literals — which both sites honour — and every returned post is
  checked against the requested window, making a server that ignores
  the terms a loud failure instead of a corrupt mirror.
"""

import datetime
import html
import json
import os
import time

import requests

from firehose import ids
from firehose import index
from firehose import util
from firehose.paper import Paper, to_name
from firehose.sources import Record


# Politeness: at most one request per second, identified with contact info
# (the sites publish no rate limits and endorse archival API use).
MIN_REQUEST_INTERVAL = 1.0
USER_AGENT = "firehose (research reading tool; matthew@timaeus.co)"

# The API truncates every query at this many results, silently: a window
# reaching it must abort, never record a watermark past it.
WINDOW_CAP = 5000

# Each fetch restarts this far behind the watermark, refreshing karma /
# comment counts / edits on recent posts.
TRAILING_DAYS = 30

# Where a fresh mirror's harvest begins: before the earliest backdated
# import (pre-LessWrong essays carry postedAt dates from 2002 onward).
EARLIEST = datetime.date(2002, 1, 1)

# Pauses before each successive attempt at one window's request; HTTP-layer
# failures retry, GraphQL errors (schema drift) fail immediately.
REQUEST_RETRY_WAITS = (0.0, 5.0, 25.0)

# Connect and read timeouts per request, in seconds.
REQUEST_TIMEOUT = (10, 120)

# %-formatted (never GraphQL variables: see the module docstring) with
# ISO date strings and an integer limit.
_POSTS_QUERY_TEMPLATE = """
{
  posts(input: {terms: {
    view: "new", after: "%(after)s", before: "%(before)s", limit: %(limit)d
  }}) {
    results {
      _id
      title
      slug
      pageUrl
      postedAt
      modifiedAt
      baseScore
      voteCount
      commentCount
      wordCount
      curatedDate
      frontpageDate
      question
      isEvent
      af
      tags { slug }
      user { displayName }
      coauthors { displayName }
      contents { plaintextDescription }
    }
  }
}
"""


class ForumMagnumAdapter:

    def __init__(self, source: str, graphql_url: str):
        self.source = source
        self.graphql_url = graphql_url

    def earliest_watermark(self) -> datetime.date:
        return EARLIEST

    def fetch(self, watermark: datetime.date):
        """
        Yield one batch of Records per monthly window, from a month before
        the watermark (the trailing re-pull) up to the present. Windows
        overlap by a day at each boundary, so no post is lost to the API's
        inclusive/exclusive convention; upserts absorb the duplicates.
        """
        start = watermark - datetime.timedelta(days=TRAILING_DAYS)
        month = start.replace(day=1)
        today = _utc_today()
        last_request_time = 0.0
        while month <= today:
            next_month = _next_month(month)
            wait = last_request_time + MIN_REQUEST_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            last_request_time = time.time()
            results = self._window(
                after=month - datetime.timedelta(days=1),
                before=next_month,
            )
            batch = []
            for raw in results:
                try:
                    batch.append(self._parse_post(raw))
                except Exception as e:
                    batch.append(e)
            if batch:
                yield batch
            month = next_month

    def _graphql(self, query: str) -> dict:
        """POST one GraphQL query and return its "data", retrying transport
        failures; GraphQL errors (schema drift) are fatal immediately."""
        for attempt, pause in enumerate(REQUEST_RETRY_WAITS, start=1):
            time.sleep(pause)
            try:
                response = requests.post(
                    self.graphql_url,
                    json={"query": query},
                    headers={"User-Agent": USER_AGENT},
                    timeout=REQUEST_TIMEOUT,
                )
                response.raise_for_status()
                break
            except requests.RequestException:
                if attempt == len(REQUEST_RETRY_WAITS):
                    raise
        body = response.json()
        if "errors" in body:
            raise RuntimeError(
                f"GraphQL errors from {self.graphql_url}: {body['errors']!r}"
            )
        return body["data"]

    def _window(self, after: datetime.date, before: datetime.date):
        """One window's posts. A full window hit the truncation cap, and a
        post dated outside the window means the server ignored the terms;
        both are errors."""
        data = self._graphql(_POSTS_QUERY_TEMPLATE % {
            "after": after.isoformat(),
            "before": before.isoformat(),
            "limit": WINDOW_CAP,
        })
        results = data["posts"]["results"]
        if len(results) >= WINDOW_CAP:
            raise RuntimeError(
                f"window {after}..{before} returned {len(results)} posts: "
                f"at the silent truncation cap, so results are incomplete; "
                f"narrow the window"
            )
        for raw in results:
            posted_at = raw.get("postedAt")
            if posted_at is None:
                continue   # malformed post: the parse reports it instead
            if not (after <= _posted_date(posted_at) <= before):
                raise RuntimeError(
                    f"window {after}..{before} returned a post dated "
                    f"{posted_at}: the server ignored the window terms"
                )
        return results

    def _parse_post(self, raw: dict) -> Record:
        """Normalise one GraphQL post into a Record. Field order is fixed
        here (and nowhere else) so that serialising a document is
        deterministic; absent optional fields are omitted, not null."""
        posted_at = raw["postedAt"]
        authors = []
        user = raw.get("user")
        if user and user.get("displayName"):
            authors.append(_collapse(user["displayName"]))
        for coauthor in raw.get("coauthors") or ():
            if coauthor.get("displayName"):
                authors.append(_collapse(coauthor["displayName"]))
        tags = []
        for tag in raw.get("tags") or ():
            slug = tag.get("slug")
            if slug and slug not in tags:
                tags.append(slug)
        for flag, pseudo_tag in (
            ("af", "~af"),
            ("curatedDate", "~curated"),
            ("frontpageDate", "~frontpage"),
            ("isEvent", "~event"),
            ("question", "~question"),
        ):
            if raw.get(flag):
                tags.append(pseudo_tag)
        contents = raw.get("contents") or {}
        doc = {
            "id": raw["_id"],
            "source": self.source,
            "title": _collapse(raw.get("title") or ""),
            "slug": raw.get("slug"),
            "page_url": raw["pageUrl"],
            "posted_at": posted_at,
            "modified_at": raw.get("modifiedAt"),
            "authors": authors,
            "karma": raw.get("baseScore"),
            "vote_count": raw.get("voteCount"),
            "comment_count": raw.get("commentCount"),
            "word_count": raw.get("wordCount"),
            "tags": tags,
            "excerpt": contents.get("plaintextDescription") or "",
        }
        doc = {key: value for key, value in doc.items() if value is not None}
        return Record(
            id=doc["id"],
            datestamp=_posted_date(posted_at),
            doc=doc,
        )

    def shard(self, local_id: str, date: datetime.date | None) -> str | None:
        """The posted-month ("YYYY-MM") shard, from the entry date — the
        ids are opaque random strings, so a document with no known entry
        is not locatable (None)."""
        if date is None:
            return None
        return f"{date.year:04d}-{date.month:02d}"

    def subscription(self, section: dict):
        """Everything: the whole site is subscribed, and narrower cuts
        (e.g. Alignment Forum only) are query-time filters over the
        pseudo-tags."""
        return lambda entry: True

    def entry(self, doc: dict) -> index.Entry:
        """A post's index entry: posted date and tags (slugs plus
        pseudo-tags)."""
        return index.Entry(
            date=_posted_date(doc["posted_at"]),
            categories=tuple(doc.get("tags", ())),
        )

    def datestamp(self, doc: dict) -> datetime.date:
        """The posted date: the axis the watermark advances along (edits
        are caught by the trailing re-pull, not the watermark)."""
        return _posted_date(doc["posted_at"])

    def to_paper(self, doc: dict) -> Paper:
        posted = _instant(doc.get("posted_at"))
        modified = _instant(doc.get("modified_at"))
        authors = list(doc.get("authors", ()))
        title = doc.get("title", "")
        numbers = [
            f"{doc[key]} {unit}"
            for key, unit in (
                ("karma", "karma"),
                ("comment_count", "comments"),
                ("word_count", "words"),
            )
            if key in doc
        ]
        return Paper(
            id=ids.join(self.source, doc["id"]),
            # the id fields are arXiv-flavoured; the bare local id stands
            # in until the download path learns per-source filenames
            xidv=doc["id"],
            name=to_name(
                authors=authors,
                year=posted.year if posted else None,
                title=title,
            ),
            entry_id=doc["page_url"],
            title=title,
            authors=authors,
            categories=list(doc.get("tags", ())),
            summary=doc.get("excerpt", ""),
            published=posted,
            updated=modified or posted,
            comment=", ".join(numbers) if numbers else None,
            doc=doc,
        )

    def filename(self, paper: Paper) -> str:
        """HTML filename: '<Author+Year Title> [<source>_<id>].html'."""
        local_id = ids.local(paper.id)
        return util.to_filename(
            paper.name, f"{self.source}_{local_id}", ".html",
        )

    def grab(self, paper: Paper, path: str) -> str:
        """Fetch the post's full text now — the body is never mirrored, so
        grabbing queries it at this moment, like fetching an arXiv PDF —
        and file it as a self-contained HTML page."""
        local_id = ids.local(paper.id)
        data = self._graphql(_GRAB_QUERY_TEMPLATE % {
            "id": json.dumps(local_id),
        })
        result = data["post"]["result"]
        if result is None:
            raise RuntimeError(f"post {local_id} no longer exists upstream")
        page = _grabbed_page(
            title=result.get("title") or paper.title,
            url=paper.entry_id,
            html_body=result.get("htmlBody") or "",
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
        return f"downloaded ★ ({os.path.getsize(path) / 1024:.0f} KiB)"


_GRAB_QUERY_TEMPLATE = """
{
  post(input: {selector: {_id: %(id)s}}) {
    result {
      title
      htmlBody
    }
  }
}
"""


def _grabbed_page(title: str, url: str, html_body: str) -> str:
    """Wrap a post's htmlBody fragment as a minimal self-contained HTML
    page: charset, title, and a link back to the post above the body."""
    return (
        "<!doctype html>\n"
        "<html>\n"
        '<head><meta charset="utf-8"><title>'
        + html.escape(title)
        + "</title></head>\n"
        "<body>\n"
        f'<p><a href="{html.escape(url, quote=True)}">{html.escape(url)}</a></p>\n'
        f"<h1>{html.escape(title)}</h1>\n"
        f"{html_body}\n"
        "</body>\n"
        "</html>\n"
    )


def _collapse(text: str) -> str:
    """Collapse internal whitespace runs (titles and names arrive with
    stray newlines and doubled spaces) and strip the ends. The excerpt is
    exempt: its paragraph breaks are meaningful."""
    return " ".join(text.split())


def _utc_today() -> datetime.date:
    """The current UTC date (the sites' postedAt instants are UTC)."""
    return datetime.datetime.now(datetime.timezone.utc).date()


def _next_month(month: datetime.date) -> datetime.date:
    if month.month == 12:
        return datetime.date(month.year + 1, 1, 1)
    return datetime.date(month.year, month.month + 1, 1)


def _posted_date(posted_at: str) -> datetime.date:
    return datetime.date.fromisoformat(posted_at[:10])


def _instant(instant: str | None) -> datetime.datetime | None:
    return datetime.datetime.fromisoformat(instant) if instant else None


ADAPTERS = {
    "lw": ForumMagnumAdapter(
        source="lw",
        graphql_url="https://www.lesswrong.com/graphql",
    ),
    "eaf": ForumMagnumAdapter(
        source="eaf",
        graphql_url="https://forum.effectivealtruism.org/graphql",
    ),
}
