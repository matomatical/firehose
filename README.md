Firehose
========

Install with `pip install -e .`.

Harvest
-------

`firehose-harvest` creates a file, `arxiv.txt.gz`, which is a list of all
article IDs on arXiv together with their submission datestamps and their
classes. The first three lines of mine (as of today) give an indication of the
format:

```
number of papers: 2740498
oai:arXiv.org:adap-org/9710003 1997-10-21 physics:nlin:AO
oai:arXiv.org:adap-org/9711001 1997-11-04 physics:nlin:AO q-bio:q-bio
```

Unlike some other scrapers, this tool uses arXiv's
  [Open Archives Initiative (OAI)](https://info.arxiv.org/help/oa/index.html)
API to gather the article metadata. This is faster than using the standard web
API, at 3.5k articles per query, but it still takes a few hours the first time
you run it to catch up on arXiv's massive backlog of articles.

The long-running harvest will potentially crash. If it crashes in a way I have
experienced, or with a keyboard interrupt, it will attempt to save its progress
so far and can just be restarted. But maybe I missed some exceptions, in which
case, sorry, add them yourself or try again.

Subsequent runs are very fast. You can run it every morning and it only needs a
few seconds to connect and download the new paper IDs.


Sample
------

TODO: Document.
