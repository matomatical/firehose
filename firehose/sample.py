import datetime
import gzip
import os
import random
import subprocess
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
    query_batch_size: int = 100,
    query_wait_time: float = 3.5,
    cache_path: str = "arxiv.txt",
    readlog_path: str = "rdlog.txt",
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

    # sampling papers
    print("sampling new papers up to the budget...")
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
    client = arxiv.Client()
    toread_xids = [xid for xid, _ in toread]
    results = []
    bar = tqdm.tqdm(
        total=len(toread_xids),
        unit="paper",
    )
    for cursor in range(0, len(toread_xids), query_batch_size):
        search = arxiv.Search(
            id_list=toread_xids[cursor:cursor+query_batch_size],
            max_results=query_batch_size,
        )
        new_results = list(client.results(search))
        results.extend(new_results)
        bar.update(len(new_results))
        if cursor+query_batch_size < len(toread_xids):
            time.sleep(query_wait_time)
    bar.close()

    print("query complete. press q to cancel or anything else to start.")
    k = readchar.readkey()
    done = (k == "q")

    # results navigation loop
    times = [0.] * len(results)
    nseen = 0
    index = 0
    first_write = True
    while not done:
        # select paper
        result = results[index]
        start_time = time.time()
        assert result.entry_id.startswith("http://arxiv.org/abs/")
        xidv = result.entry_id[len("http://arxiv.org/abs/"):]
        xid, _v = xidv.split('v')
        if index > nseen:
            nseen = index
        if index == nseen:
            with open('rdlog.txt', 'a') as f:
                f.write(f"{xid} {datetime.date.today().strftime('%Y-%m-%d')}\n")

        # clear screen
        print('\033[2J\033[H', end="")
        
        # display state and timing statistics
        print(
            f"[{index+1} / {len(results)}]",
            mp.progress((index+1)/len(results), width=60),
        )
        if nseen == 0:
            total_td = 0
            average = 0
        else:
            total = sum(times[:nseen])
            total_td = datetime.timedelta(seconds=int(total))
            average = total / nseen
        print(f"{total_td} ({average:.2f} seconds/paper)")

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
        print(result.summary)
        print()
        if result.comment is not None:
            print('comment:', result.comment)
        print()
        
        old_index = index
        while True:
            key = readchar.readkey()
            if key == "q" or key == readchar.key.ESC:
                done = True
                break
            
            elif key == readchar.key.LEFT:
                index = max(0, index - 1)
                break
            
            elif key == readchar.key.RIGHT or key == readchar.key.SPACE:
                index = index + 1
                if index == len(results):
                    done = True
                break
            
            elif key == "o" or key == readchar.key.UP:
                # open the current link and proceed
                print(f"opening '{result.entry_id}'...")
                os.system(f"open '{result.entry_id}'")
                print(f"opened.")
            
            elif key == "d" or key == readchar.key.DOWN:
                paper_name = util.to_name(result)

                print("copying title to clipboard...")
                with subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE) as pb:
                    pb.communicate(input=f"- {paper_name}\n".encode())

                # print("adding to reading list...")
                # readinglist = os.path.join(
                #     os.path.expanduser('~'),
                #     "readings",
                #     "downloads.md",
                # )
                # with open(readinglist, 'a') as r:
                #     if first_write:
                #         r.write("\nfirehose {}\n\n".format(
                #             datetime.date.today().strftime('%Y.%m.%d'),
                #         ))
                #         first_write = False
                #     r.write(f"- {paper_name}\n")

                print("downloading...")
                dirpath = os.path.join(
                    os.path.expanduser('~'),
                    "storage",
                    "library",
                    "readings",
                    datetime.date.today().strftime('%Y-%m'),
                )
                filename = util.to_filename(paper_name)
                path = os.path.join(dirpath, filename)
                os.makedirs(dirpath, exist_ok=True)
                while os.path.exists(path):
                    filename = f"{filename[:-4]} (duplicate).pdf"
                    path = os.path.join(dirpath, filename)
                util.download_paper(paper_id=xid, path=path)
                print("downloaded.")
        
        finish_time = time.time()
        spent_time = finish_time - start_time
        times[old_index] += spent_time

    print("done!")


def nsample(
    n: int = 100,
    /,
    backwards: bool = False,
    randomise: bool = False,
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
        query_batch_size=query_batch_size,
        query_wait_time=query_wait_time,
        cache_path=cache_path,
        readlog_path=readlog_path,
    )
