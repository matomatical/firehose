import datetime
import os
import random
import time

import arxiv
import tqdm
import readchar

from firehose import util
from firehose import vis
from firehose import scanner as scn


def sample(
    n: int = 100,
    /,
    query: bool = True,
    backwards: bool = False,
    randomise: bool = False,
    offset: int | None = None,
    modern: bool = True,
    query_batch_size: int = 100,
    query_wait_time: float = 3.5,
    cache_path: str = "arxiv.txt",
    readlog_path: str = "rdlog.txt",
    scanlog_path: str = "scanlog.jsonl",
    download_dir: str = "~/storage/library/readings",
):
    """
    Download and present abstracts for a batch of papers.
    """
    # load cached headers with overlapping classes
    print("loading papers from disk...")
    cache, _ = util.load_cache(
        path=cache_path,
        strip_prefix=True,
    )
    print(f"loaded {len(cache)} papers")

    # load read papers from read log
    print("checking which have already been read...")
    readlog = util.load_readlog(path=readlog_path)
    read = set(readlog)
    print(f"loaded {len(read)} already-read papers")

    # filtering the lists
    print("removing these from the list...")
    unread = [(xid, date) for xid, date in cache.items() if xid not in read]
    print(f"remaining {len(unread)} papers to scan")

    # filtering the list for date
    if modern:
        print("removing old papers from the list...")
        cutoff = datetime.date(2025, 4, 15)
        unread = [(xid, date) for xid, date in unread if date > cutoff]
        print(f"remaining {len(unread)} papers to scan")

    # sampling papers
    print("sampling new papers up to the budget...")
    if offset is not None:
        unread = unread[-offset:]
    if backwards:
        toread = unread[:n]
    elif randomise:
        toread = random.sample(unread, n)
    else:
        toread = unread[-n:]
        toread = toread[::-1]
    print(f"sampled {len(toread)} papers")

    print("visualising on calendar...")
    toread_dates = [date for xid, date in toread]
    print(vis.vis_dates(toread_dates))

    if not query:
        print("exiting...")
        return

    # run the query
    print("querying the API to get metadata for these papers...")
    client = arxiv.Client(num_retries=0)
    toread_xids = [xid for xid, _ in toread]
    results = []
    bar = tqdm.tqdm(
        total=len(toread_xids),
        unit="paper",
        ncols=80,
    )
    for cursor in range(0, len(toread_xids), query_batch_size):
        search = arxiv.Search(
            id_list=toread_xids[cursor:cursor+query_batch_size],
            max_results=query_batch_size,
        )
        try:
            new_results = list(client.results(search))
        except arxiv.HTTPError as e:
            print(e)
            raise e
        results.extend(new_results)
        bar.update(len(new_results))
        if cursor+query_batch_size < len(toread_xids):
            time.sleep(query_wait_time)
    bar.close()

    print("reordering results")
    results_by_xid = {
        r.entry_id[len("http://arxiv.org/abs/"):].split('v')[0]: r
        for r in results
    }
    results_sorted = [
        results_by_xid[xid]
        for xid in toread_xids
        if xid in results_by_xid
    ]

    if len(results_sorted) == 0:
        print("no papers to show.")
        return
    papers = [_paper_from_result(r) for r in results_sorted]

    print("query complete. press q to cancel or anything else to start.")
    if readchar.readkey() == "q":
        return
    _run_session(
        papers,
        scanlog_path=scanlog_path,
        readlog_path=readlog_path,
        download_dir=download_dir,
    )
    print("done!")


def _paper_from_result(r) -> scn.Paper:
    """Build a lightweight Paper (decoupled from the arxiv result) for scanning."""
    xidv = r.entry_id[len("http://arxiv.org/abs/"):]
    return scn.Paper(
        xid=xidv.split('v')[0],
        xidv=xidv,
        name=util.to_name(r),
        entry_id=r.entry_id,
        title=r.title,
        authors=[str(a) for a in r.authors],
        categories=[str(c) for c in r.categories],
        summary=r.summary,
        published=r.published,
        updated=r.updated,
        comment=r.comment,
    )


def _key_to_command(key) -> str | None:
    """Map a raw keypress to a semantic Scanner command (None = ignore)."""
    if key == "q" or key == readchar.key.ESC:
        return "quit"
    if key == readchar.key.LEFT:
        return "back"
    if key == readchar.key.RIGHT:
        return "forward"
    if key == readchar.key.SPACE:
        return "pause"
    if key == "o" or key == readchar.key.UP:
        return "open"
    if key == "s":
        return "save"
    if key == "d":
        return "download"
    if key == readchar.key.DOWN:
        return "down"
    if key == "x":
        return "remove"
    return None


class Stopwatch:
    """Wall-clock stopwatch that can be paused; drives the live dwell average."""

    def __init__(self):
        self._accum = 0.0
        self._segment_start = time.time()
        self._paused = False

    def elapsed(self) -> float:
        if self._paused:
            return self._accum
        return self._accum + (time.time() - self._segment_start)

    def set_paused(self, paused: bool):
        if paused and not self._paused:
            self._accum += time.time() - self._segment_start
            self._paused = True
        elif not paused and self._paused:
            self._segment_start = time.time()
            self._paused = False


def _timing_line(stopwatch: Stopwatch, nseen: int, paused: bool) -> str:
    total = stopwatch.elapsed()
    seen = nseen + 1
    average = total / seen if seen > 0 else 0.0
    line = f"{datetime.timedelta(seconds=int(total))} ({average:.2f} seconds/paper)"
    if paused:
        line += "   — PAUSED (space to resume)"
    return line


def _execute(effect, *, scanlog_path, readlog_path, download_dir, pdf_paths):
    """Carry out one declarative effect emitted by the Scanner."""
    if isinstance(effect, scn.Log):
        util.log_event(scanlog_path, effect.event)

    elif isinstance(effect, scn.Clip):
        util.copy_to_clipboard(effect.text)

    elif isinstance(effect, scn.Open):
        if not util.open_url(effect.url):
            print(f"no opener available; url: {effect.url}")

    elif isinstance(effect, scn.Readlog):
        with open(readlog_path, 'a') as f:
            f.write(f"{effect.xid} {datetime.date.today().strftime('%Y-%m-%d')}\n")

    elif isinstance(effect, scn.Download):
        dirpath = os.path.join(
            os.path.expanduser(download_dir),
            datetime.date.today().strftime('%Y-%m'),
        )
        filename = util.to_filename(effect.name, effect.xidv)
        path = os.path.join(dirpath, filename)
        os.makedirs(dirpath, exist_ok=True)
        while os.path.exists(path):
            filename = f"{filename[:-4]} (duplicate).pdf"
            path = os.path.join(dirpath, filename)
        util.download_paper(paper_id=effect.xid, path=path)
        pdf_paths[effect.xid] = path

    elif isinstance(effect, scn.DeletePDF):
        path = pdf_paths.pop(effect.xid, None)
        if path and os.path.exists(path):
            os.remove(path)


def _run_session(papers, *, scanlog_path, readlog_path, download_dir):
    """Drive the interactive scan: render, read a key, run the Scanner's effects."""
    sc = scn.Scanner(papers)
    pdf_paths = {}

    def run(effects):
        for effect in effects:
            _execute(
                effect,
                scanlog_path=scanlog_path,
                readlog_path=readlog_path,
                download_dir=download_dir,
                pdf_paths=pdf_paths,
            )

    run(sc.start())
    stopwatch = Stopwatch()
    while not sc.done:
        print(scn.render_frame(sc, _timing_line(stopwatch, sc.nseen, sc.paused)))
        command = _key_to_command(readchar.readkey())
        if command is None:
            continue
        run(sc.feed(command))
        stopwatch.set_paused(sc.paused)


def nsample(
    n: int = 100000,
    /,
    backwards: bool = False,
    randomise: bool = False,
    offset: int | None = None,
    modern: bool = True,
    query_batch_size: int = 100,
    query_wait_time: float = 3.5,
    cache_path: str = "arxiv.txt",
    readlog_path: str = "rdlog.txt",
):
    """
    Run 'sample' without downloading (sample --no-query).
    """
    sample(
        n,
        query=False,
        backwards=backwards,
        randomise=randomise,
        offset=offset,
        modern=modern,
        query_batch_size=query_batch_size,
        query_wait_time=query_wait_time,
        cache_path=cache_path,
        readlog_path=readlog_path,
    )
