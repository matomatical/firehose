"""
The Paper object: everything the scanner displays or files about one paper.

Constructed from a mirror document (`from_mirror_doc`); the fields mirror
what the scan frame renders (title, authors, abstract, categories, dates,
comment) plus derived identifiers: `xidv` (id with version suffix), and
`name`, a human-readable "Surname+Year Title" string used to file PDFs.
"""

import datetime
from dataclasses import dataclass


@dataclass
class Paper:
    xidv: str         # arxiv id with version, e.g. "2601.00001v1"
    name: str         # "Author+Year Title", for PDF filenames
    entry_id: str     # abstract page URL, e.g. "http://arxiv.org/abs/..."
    title: str
    authors: list
    categories: list
    summary: str
    published: object
    updated: object
    comment: object

    @property
    def xid(self) -> str:
        """ArXiv id without version, e.g. "2601.00001"."""
        # TODO: might break for old ids?
        return self.xidv.split('v')[0]

    @classmethod
    def from_mirror_doc(cls, doc: dict) -> "Paper":
        """
        Build a Paper from a mirror document (see arxivraw): versions carry
        the published (v1) and updated (latest) instants, `authors` is one
        display string split heuristically into individual names, and
        `abstract`/`comments` map to `summary`/`comment`.
        """
        versions = doc["versions"]
        version = versions[-1].get("version") or "v1"
        xidv = f"{doc['id']}{version}"
        published = _version_datetime(versions[0])
        updated = _version_datetime(versions[-1])
        authors = split_authors(doc.get("authors", ""))
        return cls(
            xidv=xidv,
            name=to_name(
                authors=authors,
                year=published.year if published else None,
                title=doc.get("title", ""),
            ),
            entry_id=f"http://arxiv.org/abs/{xidv}",
            title=doc.get("title", ""),
            authors=authors,
            categories=list(doc.get("categories", ())),
            summary=doc.get("abstract", ""),
            published=published,
            updated=updated,
            comment=doc.get("comments"),
        )


def split_authors(authors: str) -> list[str]:
    """
    Split an authors display string ("A. One, B. Two and C. Three") into
    individual names. Heuristic: commas separate authors, an "and" splits
    the final pair; affiliations or collaboration names pass through as
    given.
    """
    names = []
    for chunk in authors.split(","):
        for name in chunk.split(" and "):
            name = name.strip()
            if name:
                names.append(name)
    return names


def to_name(authors: list[str], year: int | None, title: str) -> str:
    """
    A paper's human-readable name: "Surname+Year Title" (two surnames for a
    pair of authors, "Surname+" for more than two).
    """
    surnames = [name.split()[-1] for name in authors if name.split()]
    if len(surnames) > 2:
        surnames[1:] = [""]
    author_str = "+".join(surnames)
    year_str = str(year) if year is not None else "????"
    return f"{author_str}{year_str} {title}"


def _version_datetime(version: dict) -> datetime.datetime | None:
    """A version's submission instant as a datetime, or None if the feed
    carried no date for it."""
    if version.get("date"):
        return datetime.datetime.fromisoformat(version["date"])
    return None
