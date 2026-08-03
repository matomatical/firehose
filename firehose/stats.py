"""
Pure data-shaping behind the visualisation commands.

Everything here maps plain data structures (the {id: date} cache and readlog
dicts, scanlog event lists) to plain data structures (date lists, histograms,
proportions, summary records). No I/O, no clock, no rendering dependencies —
so it is unit-testable without a terminal, and callers on either side of a
process boundary can compute or consume these shapes.
"""

import collections
import datetime
from dataclasses import dataclass


# # #
# Reading-state shapes (from the paper cache and the readlog)


def select_unread_dates(
    cache: dict[str, datetime.date],
    read: set[str],
    cutoff: datetime.date | None = None,
) -> list[datetime.date]:
    """
    Submission dates of the unread papers in the cache: those whose id is
    not in `read` and (when a `cutoff` is given) dated after it. Order
    follows the cache.
    """
    return [
        date for xid, date in cache.items()
        if xid not in read and (cutoff is None or date > cutoff)
    ]


def read_submit_dates(
    readlog: dict[str, datetime.date],
    cache: dict[str, datetime.date],
) -> list[datetime.date]:
    """
    Submission dates of the papers that have been read: each read id is
    resolved through the cache, and read ids the cache no longer holds
    (e.g. withdrawn papers) are silently dropped. Order follows the readlog.
    """
    return [cache[xid] for xid in readlog if xid in cache]


def normalise_date_counts(
    counts: dict[datetime.date, int],
    total_counts: dict[datetime.date, int] | None = None,
) -> dict[datetime.date, float]:
    """
    Map per-date counts to intensities in [0, 1] for heatmap tinting.

    Without `total_counts`, counts are scaled so the busiest date is 1.0.
    With it, each date's intensity is the proportion count/total for that
    date, and the result is keyed by `total_counts` (dates with no count
    get 0.0; dates absent from `total_counts` are dropped).
    """
    if total_counts is None:
        if not counts:
            return {}
        max_count = max(counts.values())
        return {date: count / max_count for date, count in counts.items()}
    return {
        date: counts.get(date, 0) / total
        for date, total in total_counts.items()
    }


def batch_read_proportions(
    all_xids: list[str],
    read: set[str],
    batch_size: int,
) -> list[float]:
    """
    The proportion of read papers in each consecutive `batch_size`-sized
    batch of `all_xids` (the final batch may be smaller). Duplicate ids
    within a batch collapse before counting.
    """
    proportions = []
    for i in range(0, len(all_xids), batch_size):
        batch = set(all_xids[i:i + batch_size])
        proportions.append(len(batch & read) / len(batch))
    return proportions


# # #
# Scan-time analytics (from the scanlog event stream)
#
# These reduce a flat, chronological list of scanlog events (dicts with an
# ISO timestamp under "t" and a "type") into per-day and total dwell figures.
# Time is wall-clock between consecutive events, paused spans excluded, and
# "papers" counts the distinct ids seen — matching the seconds/paper shown
# live during a scan.


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
    mid-session) defensively closes the previous one, and a trailing run with
    no "end" yet (a session in progress) is still returned.
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
    "resume") is dropped. Only explicit pauses stop the clock.
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
    (sorted by date, each session attributed to the day it began) and the
    grand totals. Distinct papers and active seconds are summed across
    sessions, so a paper re-viewed in a later session counts once per
    session (as the live seconds/paper does).

    "read-import" events (reading history imported from before the event
    log existed, with day-resolution timestamps) are not scan-time events
    and are excluded up front.
    """
    events = [e for e in events if e.get("type") != "read-import"]
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
