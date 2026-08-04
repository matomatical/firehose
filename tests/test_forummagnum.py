"""Network-free tests for the ForumMagnum (LW / EAF) adapter: canned
GraphQL responses drive the fetch loop, and the per-document mappings run
on canned posts."""

import datetime
import re
import types

import pytest

from firehose import harvest as harvest_module
from firehose import index
from firehose import mirror as mirror_store
from firehose import sources
from firehose.sources import forummagnum

LW = sources.adapter("lw")


def _raw_post(_id="abc123Xyz", **overrides):
    """One post as the GraphQL API returns it."""
    post = {
        "_id": _id,
        "title": "A Post",
        "slug": "a-post",
        "pageUrl": f"https://www.lesswrong.com/posts/{_id}/a-post",
        "postedAt": "2026-07-15T12:30:00.123Z",
        "modifiedAt": "2026-07-16T01:00:00.000Z",
        "baseScore": 42,
        "voteCount": 17,
        "commentCount": 5,
        "wordCount": 1234,
        "curatedDate": None,
        "frontpageDate": "2026-07-15T13:00:00.000Z",
        "question": False,
        "isEvent": False,
        "af": True,
        "tags": [{"slug": "ai"}, {"slug": "interpretability"}],
        "user": {"displayName": "Alice Author"},
        "coauthors": [{"displayName": "Bob Both"}],
        "contents": {"plaintextDescription": "An excerpt."},
    }
    post.update(overrides)
    return post


def _install_api(monkeypatch, respond, today="2026-08-04"):
    """Patch the GraphQL transport: `respond(variables)` supplies each
    window's results (or a full body via a "data"/"errors" key). Returns
    the list of window variables requested, appended to live."""
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        assert "matthew" in headers["User-Agent"]
        # terms must be inlined: EAF silently ignores variable-bound terms
        assert "variables" not in json
        query = json["query"]
        variables = {
            "after": re.search(r'after: "([^"]*)"', query).group(1),
            "before": re.search(r'before: "([^"]*)"', query).group(1),
            "limit": int(re.search(r"limit: (\d+)", query).group(1)),
        }
        calls.append(variables)
        body = respond(variables)
        if not isinstance(body, dict):
            body = {"data": {"posts": {"results": body}}}
        return types.SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: body,
        )

    monkeypatch.setattr(forummagnum.requests, "post", fake_post)
    monkeypatch.setattr(forummagnum.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        forummagnum, "_utc_today",
        lambda: datetime.date.fromisoformat(today),
    )
    return calls


# -- the fetch loop ---------------------------------------------------------------


def test_fetch_windows_trail_the_watermark_by_a_month(monkeypatch):
    posts = {   # keyed by each window's `after` bound
        "2026-04-30": [_raw_post("may1", postedAt="2026-05-02T00:00:00.000Z")],
        "2026-06-30": [_raw_post("jul1")],
    }
    calls = _install_api(
        monkeypatch,
        lambda variables: posts.get(variables["after"], []),
    )

    batches = list(LW.fetch(datetime.date(2026, 6, 20)))

    # 2026-06-20 − 30 days lands in May: four windows, each padded a day
    # behind its month so boundary posts cannot fall between windows
    assert [(c["after"], c["before"]) for c in calls] == [
        ("2026-04-30", "2026-06-01"),
        ("2026-05-31", "2026-07-01"),
        ("2026-06-30", "2026-08-01"),
        ("2026-07-31", "2026-09-01"),
    ]
    assert all(c["limit"] == forummagnum.WINDOW_CAP for c in calls)
    # empty windows yield no batch; results arrive as parsed Records
    assert [[r.id for r in batch] for batch in batches] == [["may1"], ["jul1"]]
    assert batches[0][0].datestamp == datetime.date(2026, 5, 2)


def test_fetch_aborts_on_the_silent_truncation_cap(monkeypatch):
    _install_api(
        monkeypatch,
        lambda variables: [
            _raw_post(f"p{n}") for n in range(forummagnum.WINDOW_CAP)
        ],
    )
    with pytest.raises(RuntimeError, match="truncation"):
        next(LW.fetch(datetime.date(2026, 8, 1)))


def test_fetch_aborts_when_the_server_ignores_the_window(monkeypatch):
    # the EA Forum failure mode: window terms dropped, newest posts
    # returned for every window
    _install_api(
        monkeypatch,
        lambda variables: [
            _raw_post("new1", postedAt="2026-08-04T12:00:00.000Z"),
        ],
    )
    with pytest.raises(RuntimeError, match="ignored the window"):
        next(LW.fetch(datetime.date(2026, 5, 10)))


def test_fetch_fails_loudly_on_graphql_errors(monkeypatch):
    _install_api(
        monkeypatch,
        lambda variables: {"errors": [{"message": "Unknown field"}]},
    )
    with pytest.raises(RuntimeError, match="GraphQL errors"):
        next(LW.fetch(datetime.date(2026, 8, 1)))


def test_fetch_yields_exceptions_for_malformed_posts(monkeypatch):
    _install_api(
        monkeypatch,
        lambda variables: (
            [{"_id": "missing-everything"}, _raw_post("good1")]
            if variables["before"] == "2026-08-01" else []
        ),
    )
    (batch,) = list(LW.fetch(datetime.date(2026, 7, 20)))
    assert isinstance(batch[0], Exception)
    assert batch[1].id == "good1"


# -- document normalisation -------------------------------------------------------


def test_parse_post_normalises_the_document():
    record = LW._parse_post(_raw_post())

    assert record.id == "abc123Xyz"
    assert record.datestamp == datetime.date(2026, 7, 15)
    # fixed key order pins the serialisation
    assert list(record.doc) == [
        "id", "source", "title", "slug", "page_url", "posted_at",
        "modified_at", "authors", "karma", "vote_count", "comment_count",
        "word_count", "tags", "excerpt",
    ]
    assert record.doc["source"] == "lw"
    assert record.doc["authors"] == ["Alice Author", "Bob Both"]
    assert record.doc["karma"] == 42
    # real slugs first, then the "~"-prefixed flags that apply
    assert record.doc["tags"] == ["ai", "interpretability", "~af", "~frontpage"]
    assert record.doc["excerpt"] == "An excerpt."


def test_parse_post_collapses_whitespace_in_title_and_names():
    record = LW._parse_post(_raw_post(
        title="A Title:\nBroken  Across   Lines ",
        user={"displayName": "Alice\nAuthor"},
    ))
    assert record.doc["title"] == "A Title: Broken Across Lines"
    assert record.doc["authors"][0] == "Alice Author"


def test_parse_post_omits_absent_fields():
    record = LW._parse_post(_raw_post(
        modifiedAt=None,
        user=None,
        coauthors=None,
        tags=[],
        af=False,
        frontpageDate=None,
        question=True,
        contents=None,
    ))
    assert "modified_at" not in record.doc
    assert record.doc["authors"] == []
    assert record.doc["tags"] == ["~question"]
    assert record.doc["excerpt"] == ""


# -- the shard rule and index mappings ----------------------------------------------


def test_shard_is_the_posted_month():
    date = datetime.date(2026, 7, 15)
    assert LW.shard("abc123Xyz", date) == "2026-07"
    assert LW.shard("qt/abc123Xyz", date) == "2026-07"
    # opaque ids are not locatable without their entry date
    assert LW.shard("abc123Xyz", None) is None


def test_entry_and_datestamp_follow_the_posted_date():
    doc = LW._parse_post(_raw_post()).doc
    assert LW.entry(doc) == index.Entry(
        date=datetime.date(2026, 7, 15),
        categories=("ai", "interpretability", "~af", "~frontpage"),
    )
    assert LW.datestamp(doc) == datetime.date(2026, 7, 15)
    assert LW.subscription({})(LW.entry(doc)) is True


def test_to_paper_display_mapping():
    doc = LW._parse_post(_raw_post()).doc
    paper = LW.to_paper(doc)

    assert paper.id == "lw:abc123Xyz"
    assert paper.title == "A Post"
    assert paper.authors == ["Alice Author", "Bob Both"]
    assert paper.name == "Author+Both2026 A Post"
    assert paper.entry_id == "https://www.lesswrong.com/posts/abc123Xyz/a-post"
    assert paper.categories == ["ai", "interpretability", "~af", "~frontpage"]
    assert paper.summary == "An excerpt."
    assert paper.comment == "42 karma, 5 comments, 1234 words"
    assert paper.published == datetime.datetime(
        2026, 7, 15, 12, 30, 0, 123000, tzinfo=datetime.timezone.utc,
    )
    assert paper.doc is doc


# -- integration with the harvest runner ---------------------------------------------


def test_apply_moves_a_document_whose_posted_date_changed(tmp_path):
    mirror_dir = str(tmp_path)
    entries = {}
    updater = mirror_store.Updater(mirror_dir)

    july = LW._parse_post(_raw_post())
    assert harvest_module._apply(july, entries, updater, LW) == "new"

    august = LW._parse_post(_raw_post(postedAt="2026-08-01T09:00:00.000Z"))
    harvest_module._apply(august, entries, updater, LW)
    updater.flush()

    # the July copy is gone; the document lives in its new month's shard
    assert mirror_store.read_paper(mirror_dir, "abc123Xyz", "2026-07") is None
    assert mirror_store.read_paper(
        mirror_dir, "abc123Xyz", "2026-08",
    )["posted_at"] == "2026-08-01T09:00:00.000Z"
    assert entries["abc123Xyz"].date == datetime.date(2026, 8, 1)


# -- grab (on-demand full text) -----------------------------------------------------


def test_filename_carries_source_and_id():
    paper = LW.to_paper(LW._parse_post(_raw_post()).doc)
    assert LW.filename(paper) == (
        "Author+Both2026 A Post [lw_abc123Xyz].html"
    )


def test_grab_files_a_self_contained_page(tmp_path, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        assert '"abc123Xyz"' in json["query"]
        return types.SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": {"post": {"result": {
                "title": "A Post <escaped>",
                "htmlBody": "<p>Full text.</p>",
            }}}},
        )

    monkeypatch.setattr(forummagnum.requests, "post", fake_post)
    monkeypatch.setattr(forummagnum.time, "sleep", lambda _: None)
    paper = LW.to_paper(LW._parse_post(_raw_post()).doc)
    path = str(tmp_path / "grabbed.html")

    message = LW.grab(paper, path)

    page = open(path, encoding="utf-8").read()
    assert "<p>Full text.</p>" in page                      # body verbatim
    assert "A Post &lt;escaped&gt;" in page                 # title escaped
    assert paper.entry_id in page                           # link back
    assert message.startswith("downloaded ★")


def test_grab_of_a_vanished_post_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(
        forummagnum.requests, "post",
        lambda url, json=None, headers=None, timeout=None: types.SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": {"post": {"result": None}}},
        ),
    )
    monkeypatch.setattr(forummagnum.time, "sleep", lambda _: None)
    paper = LW.to_paper(LW._parse_post(_raw_post()).doc)
    path = str(tmp_path / "grabbed.html")

    with pytest.raises(RuntimeError, match="no longer exists"):
        LW.grab(paper, path)
    assert not (tmp_path / "grabbed.html").exists()


def test_adapter_registry_covers_both_sites():
    assert sources.adapter("lw").graphql_url.startswith(
        "https://www.lesswrong.com"
    )
    assert sources.adapter("eaf").graphql_url.startswith(
        "https://forum.effectivealtruism.org"
    )
    assert sources.adapter("lw").source == "lw"
    assert sources.adapter("eaf").source == "eaf"
