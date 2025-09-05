import time
import typing

import matthewplotlib as mp

from firehose import util


READLOG_PATH = "rdlog.txt"
CACHE_PATH = "arxiv.txt"


def calendar(
    mode: typing.Literal[
        "read-date",
        "submit-date",
        "proportion",
    ] = "read-date",
    readlog_path: str = READLOG_PATH,
    cache_path: str = CACHE_PATH,
    save_as: str | None = None,
):
    print("loading read log...")
    readlog = util.load_readlog(path=readlog_path)
    print(f"loaded {len(readlog)} already-read papers")

    if mode == "submit-date" or mode == "proportion":
        print("loading their submitted dates from paper cache...")
        cache, _ = util.load_cache(path=cache_path, strip_prefix=True)
        print(f"resolved {len(cache)} read papers")
    
    print("printing calendar...")
    if mode == "read-date":
        read_dates = list(readlog.values())
        vis = util.vis_dates(read_dates)
    
    elif mode == "submit-date":
        submit_dates = [ cache[xid] for xid in readlog if xid in cache ]
        vis = util.vis_dates(submit_dates)

    elif mode == "proportion":
        submit_dates = [ cache[xid] for xid in readlog if xid in cache ]
        all_dates = list(cache.values())
        vis = util.vis_dates(
            dates=submit_dates,
            all_dates=all_dates,
        )

    print(vis)
        
    if save_as:
        print(f"saving calendar to {save_as}...")
        vis.saveimg(save_as)


def linear(
    readlog_path: str = READLOG_PATH,
    cache_path: str = CACHE_PATH,
    batch_size: int = 100,
    save_as: str | None = None,
):
    print("loading all submitted ids from paper cache...")
    cache, _ = util.load_cache(path=cache_path, strip_prefix=True)
    all_xids = list(cache.keys())
    print(f"found {len(all_xids)} papers")

    print("loading read log")
    readlog = util.load_readlog(path=readlog_path)
    read_xids = list(readlog.keys())
    print(f"found {len(read_xids)} read papers")

    print("printing visualisation...")
    vis = util.vis_all(
        all_xids=all_xids,
        read_xids=read_xids,
        batch_size=batch_size,
    )
    print(vis)

    if save_as:
        print(f"saving visualisation to {save_as}...")
        vis.saveimg(save_as)


def hilbert(
    live: bool = False,
    readlog_path: str = READLOG_PATH,
    cache_path: str = CACHE_PATH,
):
    print("loading all submitted ids from paper cache...")
    cache, _ = util.load_cache(path=cache_path, strip_prefix=True)
    all_xids = {xid: i for i, xid in enumerate(cache.keys())}
    print(f"found {len(all_xids)} papers")

    print("computing read vector...")
    read_vec = [False] * len(all_xids)
    rendered = False
    
    print("starting read loop...")
    with open(readlog_path, 'r') as f:
        while True:
            # read titles added so far
            new_titles = False
            for line in f:
                new_titles = True
                xid, _ = line.strip().split()
                if xid in all_xids:
                    read_vec[all_xids[xid]] = True

            # if there are new titles, redraw plot
            if new_titles:
                vis = mp.hilbert(
                    data=read_vec,
                    dotcolor=(0,1,1),
                    bgcolor=(0.2,0,0.2),
                )
                if not rendered: # first time
                    print(vis)
                    rendered = True
                else: # subsequent
                    print(f"\x1b[{vis.height}A{vis}")
            
            # otherwise wait until next poll
            elif live:
                time.sleep(3)

            # or break
            else:
                break


