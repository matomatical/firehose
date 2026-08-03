import calendar
import collections
import datetime
import time
import typing

import matthewplotlib as mp

from firehose import stats
from firehose import util
from firehose.store import LocalStore


def _store(config: dict, data_dir: str | None) -> LocalStore:
    """The store the visualisation commands query."""
    paths = util.data_paths(config, data_dir=data_dir)
    return LocalStore(paths, subscribed=util.subscribed_categories(config))


def all_submitted_dates(
    config_path: str = util.CONFIG_PATH,
    data_dir: str | None = None,
    save_as: str | None = None,
):
    config = util.load_config(config_path)
    store = _store(config, data_dir)

    print("printing calendar...")
    dates = store.submitted_dates()
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

    Drops papers already seen and (with --modern, the default) those on or
    before the modern cutoff, then renders the rest by submission date. This
    is the calendar `sample` prints as its dry run, without any download.
    Pass --no-modern to include the full backlog, --save-as to write the
    calendar to an image.
    """
    config = util.load_config(config_path)
    store = _store(config, data_dir)

    cutoff = config["scan"]["modern_cutoff"] if modern else None
    unread_dates = store.unread_dates(cutoff=cutoff)
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
    store = _store(config, data_dir)

    years = collections.Counter([
        date.year for date in store.submitted_dates()
    ])

    print("printing calendar...")
    for year, count in sorted(years.items()):
        print(f"- {year} ({count} papers)")


def all_submitted_months(
    config_path: str = util.CONFIG_PATH,
    data_dir: str | None = None,
):
    config = util.load_config(config_path)
    store = _store(config, data_dir)

    year_months = collections.Counter([
        (date.year, date.month) for date in store.submitted_dates()
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
    store = _store(config, data_dir)

    print("printing calendar...")
    if mode == "read-date":
        vis = vis_dates(store.read_dates())

    elif mode == "submit-date":
        vis = vis_dates(store.read_submit_dates())

    elif mode == "proportion":
        vis = vis_dates(
            dates=store.read_submit_dates(),
            all_dates=store.submitted_dates(),
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
    store = _store(config, data_dir)

    print("printing visualisation...")
    vis = vis_all(
        all_xids=store.subscribed_ids(),
        read_xids=list(store.read_ids()),
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
    store = _store(config, data_dir)
    all_xids = {xid: i for i, xid in enumerate(store.subscribed_ids())}

    print("starting read loop...")
    read_vec = [False] * len(all_xids)
    drawn = 0    # read papers reflected in the plot so far
    rendered = False
    while True:
        # fold in reads recorded since the last poll
        read = store.read_ids()
        if len(read) > drawn or not rendered:
            for xid in read:
                if xid in all_xids:
                    read_vec[all_xids[xid]] = True
            drawn = len(read)
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

        # wait until the next poll, or finish
        if not live:
            break
        time.sleep(3)
        store.refresh_events()


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
    store = _store(config, data_dir)
    events = store.scan_events()
    untimed = sum(1 for e in events if e.get("type") == "read-import")
    if untimed:
        print(f"skipping {untimed} papers without timing data")

    summary = stats.summarise_scan_time(events)
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
        print()
        print(_scan_time_legend(max_seconds))
        print()
        vis = _vis_month_grid(norm_data)
        print(vis)
        if save_as:
            print(f"saving heatmap to {save_as}...")
            vis.saveimg(save_as)


def _scan_time_legend(max_seconds: float) -> mp.plot:
    """
    Colour key for the calendar heatmap: the `cyber` gradient tints each day
    from magenta (no scanning) to cyan (the busiest day), matching
    _vis_month_grid. The gradient bar sits under a label naming both ends.
    """
    label = f"time spent: (magenta = {_fmt_hms(0)}, cyan = {_fmt_hms(max_seconds)})"
    width = len(label)
    row = [i / (width - 1) for i in range(width)]
    bar = mp.image([row, row], colormap=mp.cyber)  # 2 rows -> 1 char tall
    return mp.vstack(mp.text(label), bar)


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

    norm_data = stats.normalise_date_counts(
        counts,
        total_counts=None if all_dates is None else collections.Counter(all_dates),
    )
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
    proportions = stats.batch_read_proportions(
        all_xids, read_xids, batch_size,
    )

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
# Scan-time rendering (the analytics core lives in stats.py)


def _fmt_hms(seconds: float) -> str:
    """Whole-second H:MM:SS, e.g. 625.4 -> '0:10:25'."""
    return str(datetime.timedelta(seconds=round(seconds)))


def _scan_time_row(
    label: str, sessions: int, papers: int, seconds: float, per_paper: float,
) -> str:
    return f"{label:<10} {sessions:>8} {papers:>7} {_fmt_hms(seconds):>9} {per_paper:>8.2f}s"


def render_scan_time(summary: stats.ScanTimeSummary) -> str:
    """Format a ScanTimeSummary as a plain-text table with a totals row."""
    header = f"{'date':<10} {'sessions':>8} {'papers':>7} {'time':>9} {'s/paper':>9}"
    lines = [header]
    for day in summary.days:
        lines.append(_scan_time_row(
            day.date.isoformat(), day.sessions, day.papers,
            day.seconds, day.seconds_per_paper,
        ))
    lines.append("─" * len(header))
    lines.append(_scan_time_row(
        "TOTAL", summary.sessions, summary.papers,
        summary.seconds, summary.seconds_per_paper,
    ))
    return "\n".join(lines)


