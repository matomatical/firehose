Firehose
========

Install with `pip install -e .`.

Harvest
-------

`firehose-harvest` creates a file, `arxiv.txt`, which is a list of all article
IDs on arXiv within a prespecified list of classes, along with their submission
datestamps. The first few lines of mine (as of today) indicate the format:

```
number of papers: 749208
latest datestamp: 2025-08-13
oai:arXiv.org:cs/9301111 1990-01-01
oai:arXiv.org:cs/9301113 1991-08-01
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
(There seems to be some persistent crash around 2006, not sure why, try
restarting from a few days earlier or something? Can't remember.)

Subsequent runs are very fast. You can run it every morning and it only needs a
few seconds to connect and download the new paper IDs.

Sample
------

`firehose` displays the latest 100 unread papers.

TODO: Document.


Read log
--------

`firehose-readlog` displays various visualisations showing progress.

TODO: Document.
