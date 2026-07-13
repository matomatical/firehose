import calendar
import collections
import datetime
import time
import typing
from dataclasses import dataclass

import matthewplotlib as mp

from firehose import util


def all_submitted_dates(
    config_path: str = util.CONFIG_PATH,
    data_dir: str | None = None,
    save_as: str | None = None,
):
    config = util.load_config(config_path)
    print("loading all submit dates from paper cache...")
    cache, _ = util.load_cache(path=util.data_paths(config, data_dir=data_dir).cache)
    print(f"loaded {len(cache)} papers")

    print("printing calendar...")
    dates = list(cache.values())
    vis = vis_dates(dates)
    print(vis)

    if save_as:
        print(f"saving calendar to {save_as}...")
        vis.saveimg(save_as)


def unread(
    modern: bool = True,
    config_path: str = util.CONFIG_PATH,
    data_dir: str | None = None,
    save_as: str | None = None,
):
    """
    Show unread papers by submission date on a calendar heatmap.

    Loads the paper cache and the read log, drops papers already seen and (with
    --modern, the default) those on or before the modern cutoff, then renders the
    rest by submission date. This is the calendar `sample` prints as its dry run,
    without any API query or download. Pass --no-modern to include the full
    backlog, --save-as to write the calendar to an image.
    """
    config = util.load_config(config_path)
    paths = util.data_paths(config, data_dir=data_dir)

    print("loading papers from disk...")
    cache, _ = util.load_cache(path=paths.cache)
    print(f"loaded {len(cache)} papers")

    print("checking which have already been read...")
    readlog, _ = util.load_readlog(path=paths.readlog)
    read = set(readlog)
    print(f"loaded {len(read)} already-read papers")

    cutoff = config["scan"]["modern_cutoff"] if modern else None
    unread_dates = select_unread_dates(cache, read, cutoff=cutoff)
    print(f"found {len(unread_dates)} unread papers")

    print("printing calendar...")
    vis = vis_dates(unread_dates)
    print(vis)

    if save_as:
        print(f"saving calendar to {save_as}...")
        vis.saveimg(save_as)


def all_submitted_years(
    config_path: str = util.CONFIG_PATH,
    data_dir: str | None = None,
):
    config = util.load_config(config_path)
    print("loading all submit dates from paper cache...")
    cache, _ = util.load_cache(path=util.data_paths(config, data_dir=data_dir).cache)
    print(f"loaded {len(cache)} papers")
    
    years = collections.Counter([date.year for date in cache.values()])

    print("printing calendar...")
    for year, count in sorted(years.items()):
        print(f"- {year} ({count} papers)")


def all_submitted_months(
    config_path: str = util.CONFIG_PATH,
    data_dir: str | None = None,
):
    config = util.load_config(config_path)
    print("loading all submit dates from paper cache...")
    cache, _ = util.load_cache(path=util.data_paths(config, data_dir=data_dir).cache)
    print(f"loaded {len(cache)} papers")
    
    year_months = collections.Counter([
        (date.year, date.month) for date in cache.values()
    ])

    print("printing calendar...")
    for (year, month), count in sorted(year_months.items()):
        print(f"- {year}.{month} ({count} papers)")


def reading_calendar(
    mode: typing.Literal[
        "read-date",
        "submit-date",
        "proportion",
    ] = "read-date",
    config_path: str = util.CONFIG_PATH,
    data_dir: str | None = None,
    save_as: str | None = None,
):
    config = util.load_config(config_path)
    paths = util.data_paths(config, data_dir=data_dir)
    print("loading read log...")
    readlog, _ = util.load_readlog(path=paths.readlog)
    print(f"loaded {len(readlog)} already-read papers")

    if mode == "submit-date" or mode == "proportion":
        print("loading their submitted dates from paper cache...")
        cache, _ = util.load_cache(path=paths.cache)
        print(f"resolved {len(cache)} read papers")
    
    print("printing calendar...")
    if mode == "read-date":
        read_dates = list(readlog.values())
        vis = vis_dates(read_dates)
    
    elif mode == "submit-date":
        submit_dates = [ cache[xid] for xid in readlog if xid in cache ]
        vis = vis_dates(submit_dates)

    elif mode == "proportion":
        submit_dates = [ cache[xid] for xid in readlog if xid in cache ]
        all_dates = list(cache.values())
        vis = vis_dates(
            dates=submit_dates,
            all_dates=all_dates,
        )

    print(vis)
        
    if save_as:
        print(f"saving calendar to {save_as}...")
        vis.saveimg(save_as)


def linear(
    config_path: str = util.CONFIG_PATH,
    data_dir: str | None = None,
    batch_size: int = 100,
    save_as: str | None = None,
):
    config = util.load_config(config_path)
    paths = util.data_paths(config, data_dir=data_dir)
    print("loading all submitted ids from paper cache...")
    cache, _ = util.load_cache(path=paths.cache)
    all_xids = list(cache.keys())
    print(f"found {len(all_xids)} papers")

    print("loading read log")
    readlog, _ = util.load_readlog(path=paths.readlog)
    read_xids = list(readlog.keys())
    print(f"found {len(read_xids)} read papers")

    print("printing visualisation...")
    vis = vis_all(
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
    size: int | None = None,
    config_path: str = util.CONFIG_PATH,
    data_dir: str | None = None,
):
    config = util.load_config(config_path)
    paths = util.data_paths(config, data_dir=data_dir)
    print("loading all submitted ids from paper cache...")
    cache, _ = util.load_cache(path=paths.cache)
    all_xids = {xid: i for i, xid in enumerate(cache.keys())}
    print(f"found {len(all_xids)} papers")

    print("computing read vector...")
    read_vec = [False] * len(all_xids)
    rendered = False
    
    print("starting read loop...")
    with open(paths.readlog, 'r') as f:
        while True:
            # read titles added so far
            new_titles = False
            for line in f:
                line = line.strip()
                # skip blanks and "<date>:" group headers; every other line is a
                # bare paper id (see the data-format note in util.py).
                if not line or line.endswith(":"):
                    continue
                new_titles = True
                xid = line
                if xid in all_xids:
                    read_vec[all_xids[xid]] = True

            # if there are new titles, redraw plot
            if new_titles:
                show_vec = read_vec if size is None else read_vec[-4**size:]
                vis = mp.hilbert(
                    data=show_vec,
                    color=(0.0, 1.0, 1.0),
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


def scan_time(
    config_path: str = util.CONFIG_PATH,
    data_dir: str | None = None,
    heatmap: bool = True,
    save_as: str | None = None,
):
    """
    Report time spent scanning abstracts: per day, in total, and per paper.

    Derives dwell from the scan log (data/scanlog.jsonl), the per-session
    start/view/.../end event stream. A session's active time is the wall-clock
    between its events minus any spans you paused (mirroring the live sample
    timer); "per paper" divides by distinct papers seen, matching sample's
    on-screen seconds/paper. With --heatmap (default), also draws a calendar
    tinted by each day's scanning time.
    """
    config = util.load_config(config_path)
    paths = util.data_paths(config, data_dir=data_dir)
    print("loading scan log...")
    events = util.load_scanlog(path=paths.scanlog)
    print(f"loaded {len(events)} events")

    summary = summarise_scan_time(events)
    if not summary.days:
        print("no scans recorded yet.")
        return

    print(render_scan_time(summary))

    if heatmap:
        max_seconds = max(day.seconds for day in summary.days)
        norm_data = {
            day.date: (day.seconds / max_seconds if max_seconds else 0.0)
            for day in summary.days
        }
        vis = _vis_month_grid(norm_data)
        print(vis)
        if save_as:
            print(f"saving heatmap to {save_as}...")
            vis.saveimg(save_as)


def _vis_month_grid(norm_data: dict[datetime.date, float]) -> mp.plot:
    """
    Render the month-by-month calendar heatmap for a {date: intensity} map,
    where each intensity in [0, 1] picks a colour from the `cyber` map. Spans
    every month from the earliest to the latest dated day; days with no entry
    are drawn as a dim marker. Assumes `norm_data` is non-empty.
    """
    start_date = min(norm_data.keys())
    end_date = max(norm_data.keys())
    year = start_date.year
    month = start_date.month
    month_plots = []
    while datetime.date(year, month, 1) <= end_date:
        # collect month
        title = mp.text(f"{calendar.month_name[month]:<9s} {year:4d}")
        daynames = mp.text("M T W t F S s ")
        week_plots = []
        for week in calendar.monthcalendar(year, month):
            day_plots = []
            for day in week:
                if day == 0:
                    day_plots.append(mp.text("  "))
                    continue
                date = datetime.date(year, month, day)
                if date not in norm_data:
                    day_plots.append(mp.text("▘ ", fgcolor=(0,0,0)))
                    continue
                day_plots.append(mp.text(
                    "▟█",
                    fgcolor=mp.cyber(norm_data[date]),
                    bgcolor=(0,0,0),
                ))
            week_plots.append(mp.hstack(*day_plots))
        month_plots.append(
            mp.vstack(title, daynames, *week_plots)
            + mp.blank(2,2),
        )

        # increment month
        month += 1
        if month == 13:
            year += 1
            month = 1

    return mp.wrap(*month_plots)


def select_unread_dates(
    cache: dict[str, datetime.date],
    read: set[str],
    cutoff: datetime.date | None = None,
) -> list[datetime.date]:
    """
    Submission dates of the unread papers in the cache: those whose id is not in
    `read` and (when a `cutoff` is given) dated after it. Pure — the data-shaping
    behind the `unread` command, mirroring sample.select_papers' filter without
    its windowing (no n / backwards / randomise / offset). Order follows cache.
    """
    return [
        date for xid, date in cache.items()
        if xid not in read and (cutoff is None or date > cutoff)
    ]


def vis_dates(
    dates: list[datetime.date],
    all_dates: None | list[datetime.date] = None,
    print_counts: bool = True,
) -> mp.plot:
    """
    Adapted from matthewplotlib calendar heatmap example.
    """
    datelines = []
    # count dates
    counts = collections.Counter(dates)
    if print_counts:
        for datestamp, count in sorted(counts.items()):
            datelines.append(mp.text(f"{datestamp} {count}  "))

    if len(counts) == 0:
        return mp.text("(no dates)")

    # normalise counts
    if all_dates is None:
        max_count = max(counts.values())
        norm_data = {date: count/max_count for date, count in counts.items()}
    else:
        total_counts = collections.Counter(all_dates)
        norm_data = {
            date: counts.get(date, 0) / total_counts[date]
            for date in total_counts.keys()
        }

    calendar_plot = _vis_month_grid(norm_data)
    if print_counts:
        if len(datelines) > 50:
            counts_plot = mp.wrap(
                *datelines,
                transpose=True,
            )
        else:
            counts_plot = mp.vstack(*datelines)
        return counts_plot / calendar_plot
    else:
        return calendar_plot


def vis_all(
    all_xids: list[str],
    read_xids: list[str],
    batch_size: int,
) -> mp.plot:
    # batch and count proportions
    read_xids = set(read_xids)
    proportions = []
    for i in range(0, len(all_xids), batch_size):
        batch = set(all_xids[i:i+batch_size])
        batch_read = batch & read_xids
        proportions.append(len(batch_read)/len(batch))

    # statistics
    num_batches = len(proportions)
    batches_complete = sum(p == 1 for p in proportions)
    total_progress = len(read_xids) / len(all_xids)
    
    # generate plots
    plot = mp.vstack(
        mp.wrap(*[
            mp.text("▟█", fgcolor=mp.cyber(p), bgcolor=(0,0,0))
            for p in proportions
        ]),
        mp.text(
            f"completed {batches_complete} "
            f"out of {num_batches} batches "
            f"of {batch_size} papers"
        ),
        mp.text("total progress: ")
        + mp.text(
            f"{total_progress:.3%}",
            fgcolor=mp.cyber(total_progress),
        )
    )
    return plot


# # #
# Scan-time analytics (pure core)
#
# `scan_time` shells around these. They reduce a flat scanlog event list (from
# util.load_scanlog) into per-day and total dwell figures, with no I/O. The
# model mirrors sample.Stopwatch / sample's on-screen seconds/paper: time is
# wall-clock between consecutive events, paused spans excluded, and "papers"
# counts the distinct ids seen.


@dataclass
class DayStats:
    """One day's scanning: sessions run, distinct papers seen, active seconds."""
    date: datetime.date
    sessions: int
    papers: int
    seconds: float

    @property
    def seconds_per_paper(self) -> float:
        return self.seconds / self.papers if self.papers else 0.0


@dataclass
class ScanTimeSummary:
    """Per-day breakdown plus the grand totals across every session."""
    days: list[DayStats]
    sessions: int
    papers: int
    seconds: float

    @property
    def seconds_per_paper(self) -> float:
        return self.seconds / self.papers if self.papers else 0.0


def split_sessions(events: list[dict]) -> list[list[dict]]:
    """
    Group a flat event list into sessions. A session opens on a "start" event
    and runs to its "end"; a fresh "start" with no intervening "end" (a crash
    mid-session) defensively closes the previous one, and a trailing run with no
    "end" yet (a session in progress) is still returned.
    """
    sessions = []
    current = []
    for event in events:
        if event.get("type") == "start" and current:
            sessions.append(current)
            current = []
        current.append(event)
        if event.get("type") == "end":
            sessions.append(current)
            current = []
    if current:
        sessions.append(current)
    return sessions


def session_active_seconds(events: list[dict]) -> float:
    """
    Active wall-clock seconds in one session: the gaps between consecutive
    events summed, but a gap that opens on a "pause" event (idle until the
    "resume") is dropped. This matches sample.Stopwatch, which only stops the
    clock for explicit pauses.
    """
    total = 0.0
    paused = False
    for before, after in zip(events, events[1:]):
        if before.get("type") == "pause":
            paused = True
        elif before.get("type") == "resume":
            paused = False
        if not paused:
            t0 = datetime.datetime.fromisoformat(before["t"])
            t1 = datetime.datetime.fromisoformat(after["t"])
            total += (t1 - t0).total_seconds()
    return total


def summarise_scan_time(events: list[dict]) -> ScanTimeSummary:
    """
    Reduce a flat scanlog event list to a ScanTimeSummary: per-day DayStats
    (sorted by date, each session attributed to the day it began) and the grand
    totals. Distinct papers and active seconds are summed across sessions, so a
    paper re-viewed in a later session counts once per session (as the live
    seconds/paper does).
    """
    by_day: dict[datetime.date, DayStats] = {}
    total_sessions = 0
    for session in split_sessions(events):
        if not session:
            continue
        total_sessions += 1
        day = datetime.datetime.fromisoformat(session[0]["t"]).date()
        papers = len({
            e["xid"] for e in session if e.get("type") == "view"
        })
        seconds = session_active_seconds(session)
        stats = by_day.get(day)
        if stats is None:
            stats = by_day[day] = DayStats(day, 0, 0, 0.0)
        stats.sessions += 1
        stats.papers += papers
        stats.seconds += seconds
    days = [by_day[day] for day in sorted(by_day)]
    return ScanTimeSummary(
        days=days,
        sessions=total_sessions,
        papers=sum(d.papers for d in days),
        seconds=sum(d.seconds for d in days),
    )


def _fmt_hms(seconds: float) -> str:
    """Whole-second H:MM:SS, e.g. 625.4 -> '0:10:25'."""
    return str(datetime.timedelta(seconds=round(seconds)))


def _scan_time_row(label: str, papers: int, seconds: float, per_paper: float) -> str:
    return f"{label:<10} {papers:>7} {_fmt_hms(seconds):>9} {per_paper:>8.2f}s"


def render_scan_time(summary: ScanTimeSummary) -> str:
    """Format a ScanTimeSummary as a plain-text table with a totals row."""
    header = f"{'date':<10} {'papers':>7} {'time':>9} {'s/paper':>9}"
    lines = [header]
    for day in summary.days:
        lines.append(_scan_time_row(
            day.date.isoformat(), day.papers, day.seconds, day.seconds_per_paper,
        ))
    lines.append("─" * len(header))
    lines.append(_scan_time_row(
        "TOTAL", summary.papers, summary.seconds, summary.seconds_per_paper,
    ))
    lines.append("")
    lines.append(
        f"{summary.sessions} sessions, {summary.papers} papers, "
        f"{_fmt_hms(summary.seconds)} total, "
        f"{summary.seconds_per_paper:.2f}s per paper"
    )
    return "\n".join(lines)


