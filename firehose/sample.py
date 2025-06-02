import datetime
import gzip
import os
import time
import re

import arxiv
import tqdm
import tyro
import readchar
import matthewplotlib as mp

from firehose import util


def sample(
    n: int = 500,
    query: bool = True,
    backwards: bool = False,
    cache_path: str = "arxiv.txt",
    readlog_path: str = "rdlog.txt",
):
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

    # sampling papers
    print("sampling new papers up to the budget...")
    if backwards:
        toread = unread[:n]
    else:
        toread = unread[-n:]
        toread = toread[::-1]
    print(f"sampled {len(toread)} papers")

    print("visualising on calendar...")
    toread_dates = [date for xid, date in toread]
    print(util.vis_dates(toread_dates))

    if not query:
        print("exiting...")
        return

    # run the query
    print("querying the API to get metadata for these papers...")
    client = arxiv.Client()
    toread_xids = [xid for xid, _ in toread]
    results = []
    bar = tqdm.tqdm(
        range(0, len(toread_xids), 100),
        unit_scale=100,
        unit="paper",
    )
    for cursor in bar:
        search = arxiv.Search(
            id_list=toread_xids[cursor:cursor+100],
            max_results=100,
        )
        results.extend(client.results(search))
        time.sleep(3.5)

    print("query complete. press q to cancel or anything else to start.")
    k = readchar.readkey()
    done = (k == "q")

    # setup for printing
    today = datetime.date.today().strftime('%Y-%m-%d')
    def log(xid):
        with open('rdlog.txt', 'a') as f:
            f.write(f'{xid} {today}\n')
        print(f'logged {xid} {today}')

    start_time = None
    last_time = None
    now = None
    for i, result in enumerate(results, 1):
        if done:
            break

        # timing information
        if now is not None:
            last_time = now
        now = time.time()
        if start_time is None:
            start_time = now

        # display result
        print('\033[2J\033[H', end="") # clear screen
        print(mp.progress(i/len(toread_xids), width=80))
        print(f"[{i} / {len(toread_xids)}]", end=" ")
        if last_time is None:
            print(f"(timing start)")
        else:
            print(f"{now-start_time:.1f} ({now-last_time:+.1f}) [{(now-start_time)/(i-1):.2f}/paper]")
        print(
            result.entry_id,
            ', '.join(["\033[3m"+str(c)+"\033[0m" for c in result.categories]),
        )
        print('published:', result.published, 'updated:', result.updated)
        print("\033[1m"+result.title+"\033[0m")
        print(', '.join(["\033[2m"+str(a)+"\033[0m" for a in result.authors]))
        print(result.summary)
        print()
        if result.comment is not None:
            print('comment:', result.comment)
        print()

        # log result
        assert result.entry_id.startswith("http://arxiv.org/abs/")
        xidv = result.entry_id[len("http://arxiv.org/abs/"):]
        xid, _v = xidv.split('v')
        log(xid)
        
        # interact with user
        key = readchar.readkey()
        if key == "q":
            done = True
        elif key == "d":
            print("downloading...")
            # construct filename
            authors = [a.name.split()[-1] for a in result.authors]
            if len(authors) > 2:
                authors[1:] = [""]
            filename="{}{} {}.pdf".format(
                "+".join(authors),
                result.published.year,
                re.sub(r"[^\w ?'\-]", "_", result.title),
            )
            dirpath = os.path.join(os.path.expanduser('~'), "Downloads")
            # check filename
            while os.path.exists(os.path.join(dirpath, filename)):
                filename += " (1)"
            # download filename
            result.download_pdf(dirpath=dirpath, filename=filename)
        elif key == "o":
            # open the current link and proceed
            os.system(f"open '{result.entry_id}'")
        else:
            # ANYTHING ELSE, JUST PROCEED
            pass

    print("done!")


def cli():
    tyro.cli(sample)
