import sys

from sickle import Sickle

from firehose import util


def classes():
    """
    Print arXiv's full category catalog (OAI setSpecs and names), sorted.

    arXiv's taxonomy is effectively frozen, so config.toml ships a static copy.
    Run this on the rare occasion you want to look up a setSpec or check whether
    arXiv has added a category, then edit config.toml by hand.
    """
    sickle = Sickle(util.OAI_API_URL)
    # dict dedupes the duplicate setSpecs arXiv's ListSets returns (e.g. gr-qc)
    catalog = sorted({s.setSpec: s.setName for s in sickle.ListSets()}.items())
    width = max((len(spec) for spec, _ in catalog), default=0)
    for spec, name in catalog:
        print(f"{spec:{width}}  # {name}")
    print(f"{len(catalog)} categories", file=sys.stderr)

