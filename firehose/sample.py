import datetime
import gzip
import os
import random
import time
import textwrap

import arxiv
import tqdm
import readchar
import matthewplotlib as mp

from firehose import util
from firehose import vis


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

    total = len(results_sorted)
    if total == 0:
        print("no papers to show.")
        return

    print("query complete. press q to cancel or anything else to start.")
    k = readchar.readkey()
    done = (k == "q")
    if not done:
        util.log_event(scanlog_path, {"type": "start", "n": total})

    # ---- navigation loop ----
    # Each paper has a state: none -> saved (☆) -> downloaded (★). The keys move
    # along this state machine and every action is logged to the scan log as a
    # timestamped event. Dwell time is NOT stored; it is derived offline from the
    # event timestamps (the running average below is an in-memory convenience).
    times = [0.0] * total          # active (pause-excluded) seconds per paper
    states = ["none"] * total      # per-paper state: none / saved / downloaded
    pdf_paths = [None] * total     # PDF path recorded on download, for remove
    glyphs = {"none": " ", "saved": "☆", "downloaded": "★"}

    nseen = -1                     # highest index reached (so index 0 counts)
    index = 0
    arrived = True                 # just landed on `index`: emit view + reset timer
    paused = False
    pause_start = 0.0
    view_start = 0.0
    view_paused = 0.0
    message = ""

    def commit_time():
        # add this visit's active (un-paused) time to the current paper's tally
        times[index] += (time.time() - view_start) - view_paused

    def do_save():
        states[index] = "saved"
        util.log_event(scanlog_path, {"type": "save", "xid": xid})
        ok = util.copy_to_clipboard(f"- ? {util.to_name(result)}\n")
        return "saved ☆  (copied '- ? ...')" if ok else "saved ☆  (no clipboard)"

    def do_download():
        paper_name = util.to_name(result)
        ok = util.copy_to_clipboard(f"- {paper_name}\n")
        dirpath = os.path.join(
            os.path.expanduser(download_dir),
            datetime.date.today().strftime('%Y-%m'),
        )
        filename = util.to_filename(paper_name, xidv)
        path = os.path.join(dirpath, filename)
        os.makedirs(dirpath, exist_ok=True)
        while os.path.exists(path):
            filename = f"{filename[:-4]} (duplicate).pdf"
            path = os.path.join(dirpath, filename)
        util.download_paper(paper_id=xid, path=path)
        states[index] = "downloaded"
        pdf_paths[index] = path
        util.log_event(scanlog_path, {"type": "download", "xid": xid})
        return "downloaded ★  (copied '- ...')" if ok else "downloaded ★  (no clipboard)"

    while not done:
        # select paper
        result = results_sorted[index]
        xidv = result.entry_id[len("http://arxiv.org/abs/"):]
        xid = xidv.split('v')[0]

        # on fresh arrival: record the read, emit a view event, start the timer
        if arrived:
            if index > nseen:
                nseen = index
                with open(readlog_path, 'a') as f:
                    f.write(f"{xid} {datetime.date.today().strftime('%Y-%m-%d')}\n")
            util.log_event(scanlog_path, {"type": "view", "xid": xid})
            view_start = time.time()
            view_paused = 0.0
            arrived = False

        # ---- render ----
        print('\033[2J\033[H', end="")

        # state line: position, progress, status glyph (glyph last to dodge any
        # ambiguous-width rendering of the star)
        print(
            f"[{index+1} / {total}]",
            mp.progress((index+1)/total, width=60),
            glyphs[states[index]],
        )

        # timing line (in-memory running average; excludes paused time)
        paused_now = view_paused + (time.time() - pause_start if paused else 0.0)
        total_active = sum(times) + (time.time() - view_start) - paused_now
        average = total_active / (nseen + 1)
        status = f"{datetime.timedelta(seconds=int(total_active))} ({average:.2f} seconds/paper)"
        if paused:
            status += "   — PAUSED (space to resume)"
        print(status)

        # display paper
        print(
            result.entry_id,
            ', '.join(["\033[3m"+str(c)+"\033[0m" for c in result.categories]),
        )
        print('published:', result.published, 'updated:', result.updated)
        print(
            "\033[1m",
            textwrap.fill(result.title, width=80),
            "\033[0m",
            sep="",
        )
        print(
            "\033[2m",
            textwrap.fill(', '.join(map(str, result.authors)), width=80),
            "\033[0m",
            sep="",
        )
        print(textwrap.fill(result.summary, width=80))
        print()
        if result.comment is not None:
            print('comment:', result.comment)
        print()
        if message:
            print(message)

        # ---- input ----
        key = readchar.readkey()

        # while paused, only space (resume) and q (quit) respond
        if paused:
            if key == readchar.key.SPACE:
                view_paused += time.time() - pause_start
                paused = False
                util.log_event(scanlog_path, {"type": "resume"})
                message = ""
            elif key == "q" or key == readchar.key.ESC:
                view_paused += time.time() - pause_start
                paused = False
                commit_time()
                util.log_event(scanlog_path, {"type": "end"})
                done = True
            else:
                message = "paused — press space to resume"
            continue

        if key == readchar.key.SPACE:
            paused = True
            pause_start = time.time()
            util.log_event(scanlog_path, {"type": "pause"})
            message = ""

        elif key == "q" or key == readchar.key.ESC:
            commit_time()
            util.log_event(scanlog_path, {"type": "end"})
            done = True

        elif key == readchar.key.LEFT:
            if index > 0:
                commit_time()
                index -= 1
                arrived = True
            message = ""

        elif key == readchar.key.RIGHT:
            commit_time()
            if index + 1 == total:
                util.log_event(scanlog_path, {"type": "end"})
                done = True
            else:
                index += 1
                arrived = True
            message = ""

        elif key == "o" or key == readchar.key.UP:
            if util.open_url(result.entry_id):
                message = "opened."
            else:
                message = f"no opener available; url: {result.entry_id}"

        elif key == "s":
            if states[index] == "none":
                message = do_save()
            else:
                message = f"already {states[index]}."

        elif key == readchar.key.DOWN:
            # progressive: none -> saved, saved -> downloaded
            if states[index] == "none":
                message = do_save()
            elif states[index] == "saved":
                message = do_download()
            else:
                message = "already downloaded ★."

        elif key == "d":
            if states[index] != "downloaded":
                message = do_download()
            else:
                message = "already downloaded ★."

        elif key == "x":
            if states[index] != "none":
                p = pdf_paths[index]
                if p and os.path.exists(p):
                    os.remove(p)
                pdf_paths[index] = None
                states[index] = "none"
                util.log_event(scanlog_path, {"type": "remove", "xid": xid})
                message = "removed."
            else:
                message = "nothing to remove."

    print("done!")


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
