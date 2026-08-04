"""
The `firehose serve` command: expose a LocalStore over HTTP so clients on
other machines can scan and visualise against this machine's data.

The API mirrors the Store interface one-to-one (a RemoteStore is a thin
client of it): selection with full metadata, single-paper lookup, event
ingest, a status snapshot, and the pre-shaped reading-state queries. Papers travel as mirror
documents (the client rebuilds Paper objects); dates travel as ISO strings.
Event timestamps are client-generated: ingest preserves any "t" already on
an event and stamps only bare ones.

There is no authentication: bind the server to an interface that is itself
the trust boundary (e.g. a tailnet address), never a public one.
"""

import datetime
import random

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware

from firehose import util
from firehose.store import LocalStore


def create_app(store: LocalStore) -> FastAPI:
    """The FastAPI app serving `store`."""
    app = FastAPI(title="firehose")
    # date lists and id lists dominate the payloads and compress ~20x
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.get("/papers")
    def papers(
        n: int,
        backwards: bool = False,
        randomise: bool = False,
        seed: int | None = None,
        offset: int | None = None,
        cutoff: datetime.date | None = None,
    ) -> list[dict]:
        rng = random.Random(seed) if seed is not None else random
        selected = store.select_papers(
            n,
            backwards=backwards,
            randomise=randomise,
            offset=offset,
            cutoff=cutoff,
            rng=rng,
        )
        return [p.doc for p in selected]

    @app.get("/papers/{paper_id:path}")
    def paper(paper_id: str) -> dict:
        found = store.get_paper(paper_id)
        if found is None:
            raise HTTPException(status_code=404, detail=f"no paper {paper_id}")
        return found.doc

    @app.post("/events")
    async def events(request: Request) -> dict:
        batch = await request.json()
        store.record_events(batch)
        return {"recorded": len(batch)}

    # the app's creation time: with the index loaded once at startup, this
    # tells a client how stale the in-memory view could be
    started = datetime.datetime.now().isoformat()

    @app.get("/status")
    def status() -> dict:
        return {"server_started": started, **store.status()}

    @app.get("/stats/submitted-dates")
    def submitted_dates() -> list[str]:
        return [d.isoformat() for d in store.submitted_dates()]

    @app.get("/stats/unread-dates")
    def unread_dates(cutoff: datetime.date | None = None) -> list[str]:
        return [d.isoformat() for d in store.unread_dates(cutoff=cutoff)]

    @app.get("/stats/read-dates")
    def read_dates() -> list[str]:
        return [d.isoformat() for d in store.read_dates()]

    @app.get("/stats/read-submit-dates")
    def read_submit_dates() -> list[str]:
        return [d.isoformat() for d in store.read_submit_dates()]

    @app.get("/stats/subscribed-ids")
    def subscribed_ids() -> list[str]:
        return store.subscribed_ids()

    @app.get("/stats/read-ids")
    def read_ids() -> list[str]:
        return sorted(store.read_ids())

    @app.get("/stats/scan-events")
    def scan_events() -> list[dict]:
        return store.scan_events()

    return app


def serve(
    host: str | None = None,
    port: int | None = None,
    config_path: str = util.CONFIG_PATH,
    data_dir: str | None = None,
):
    """
    Serve this machine's data over HTTP for remote firehose clients.

    Host and port come from the config's [server] section (`listen_host`,
    `listen_port`) unless overridden. The index loads once at startup;
    restart the server after a `mirror` run so it sees new papers.
    """
    config = util.load_config(config_path)
    server_config = config.get("server", {})
    host = host or server_config.get("listen_host", "127.0.0.1")
    port = port or server_config.get("listen_port", 8377)

    paths = util.data_paths(config, data_dir=data_dir)
    store = LocalStore(paths, subscribed=util.subscribed_categories(config))
    store.submitted_dates()   # touch the index so it loads now, not mid-request

    uvicorn.run(create_app(store), host=host, port=port, log_level="info")
