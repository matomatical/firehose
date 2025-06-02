import sys

from sickle import Sickle
import tyro


API_URL = "https://oaipmh.arxiv.org/oai"


def classes():
    sickle = Sickle(API_URL)

    # existing configured sets
    with open('classes.txt') as f:
        configured_sets = { line.split(maxsplit=1)[0] for line in f }

    # sets according to API
    api_listed_sets = { s.setSpec: s.setName for s in sickle.ListSets() }

    # new sets
    new_sets = set(api_listed_sets) - configured_sets

    # output
    print(len(new_sets), "new classes", file=sys.stderr)
    for set_id in new_sets:
        set_name = api_listed_sets[set_id]
        print(f"{set_id:32s} # {set_name}")

    sys.exit(len(new_sets))


def cli():
    tyro.cli(classes)
