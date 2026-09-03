"""Read-only: how many real (capital-at-stake) positions are open SIMULTANEOUSLY at
once, over a lookback window -- docs/backlog_cache.md, raised 2026-08-27 ("no check on
HOW MANY concurrent real positions are open at once"). Not about positions interacting
(margin/correlation, a separate design question, see docs/backlog_cache.md's "pooled/
shared capital" entry) -- just a real count: at any given moment, how many real nodes
had simultaneous open positions, with no existing check surfacing that number.

Queries trade_log (every closed real trade, real entry_time/exit_time interval) +
open_positions (currently-open real positions, treated as an interval running from
entry_time to "now") directly -- read-only, no writes anywhere, no signals_db.py change
(that's a gated live-daemon module; this is a standalone query script over its own
tables, not a change to how positions are tracked).

"Real (capital-at-stake)" = is_dry_run_sim=0 on both tables -- the existing, already-
real distinction this schema carries (a dry_run_sim row is a simulation, never real
capital). No additional notional-size filter is applied by default (this answers "how
many real positions," not "how many real positions above some size") -- see --min-notional
if a size floor is ever wanted.

Method: classic max-concurrent-intervals sweep-line. Every real trade/open-position
becomes one (start, end) interval; a +1 event at start, -1 event at end; sorted
chronologically (end-events processed before start-events at an identical timestamp, so
a trade closing at the exact moment another opens doesn't count as 2 concurrent) with a
running sum tracked throughout -- the running sum's own maximum IS the real historical
peak concurrency, and its full time series is what --histogram summarizes into a
distribution (not just the single peak number).

Usage:
  .venv/bin/python scripts/concurrent_positions_check.py
  .venv/bin/python scripts/concurrent_positions_check.py --days 90
  .venv/bin/python scripts/concurrent_positions_check.py --account soxl_ira
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

LIVE_DB_PATH = "cache/live/trading_live.db"


def _fetch_intervals(conn, since, account=None):
    """Returns [(start_iso, end_iso_or_None, ticker, account, notional)] for every real
    (is_dry_run_sim=0) trade_log row (closed, real end) and open_positions row (still
    open, end=None -- caller resolves to "now"), scoped to `since` (only intervals that
    could possibly still be open/relevant at or after this cutoff -- a closed trade that
    exited before `since` can never contribute to the lookback window's concurrency)."""
    acct_sql = " AND account=?" if account else ""
    acct_params = [account] if account else []

    closed = conn.execute(f"""
        SELECT entry_time, exit_time, ticker, account,
               COALESCE(shares, 0) * COALESCE(entry_price, 0) AS notional
        FROM trade_log
        WHERE is_dry_run_sim=0 AND entry_time IS NOT NULL AND exit_time IS NOT NULL
              AND exit_time >= ?{acct_sql}
    """, [since] + acct_params).fetchall()

    open_now = conn.execute(f"""
        SELECT entry_time, NULL, ticker, account,
               COALESCE(shares, 0) * COALESCE(entry_price, 0) AS notional
        FROM open_positions
        WHERE is_dry_run_sim=0 AND entry_time IS NOT NULL{acct_sql}
    """, acct_params).fetchall()

    return closed + open_now


def max_concurrent(intervals, now_iso):
    """Sweep-line: returns (peak_count, peak_time, timeline) where timeline is the full
    chronological (time, running_count) series -- the histogram/percentile summary below
    is built from this, not just the single peak number."""
    events = []  # (time, delta, order) -- order breaks ties: ends (-1) before starts (+1)
    # Parse to real datetime objects immediately, never sort/compare raw strings:
    # open_positions/trade_log store 'YYYY-MM-DD HH:MM:SS' (space separator), while
    # `now_iso` (this function's own synthetic "still open" end-time) is Python's
    # datetime.isoformat() default ('T' separator) -- a real, confirmed risk found
    # 2026-09-03 before shipping this: string comparison of two SAME-DAY timestamps in
    # different separator styles resolves on the separator byte itself (' ' < 'T' in
    # ASCII), silently misordering events that share a calendar date. Parsing removes
    # the risk entirely rather than papering over it with a format-normalization step
    # that could just as easily be forgotten at the next call site.
    now_dt = datetime.fromisoformat(now_iso)
    for start, end, ticker, account, notional in intervals:
        end_dt = datetime.fromisoformat(end) if end else now_dt
        events.append((datetime.fromisoformat(start), +1, 1))
        events.append((end_dt, -1, 0))
    events.sort(key=lambda e: (e[0], e[2]))

    count = 0
    peak = 0
    peak_time = None
    timeline = []
    for t, delta, _ in events:
        count += delta
        timeline.append((t, count))
        if count > peak:
            peak = count
            peak_time = t
    return peak, peak_time, timeline


def summarize_timeline(timeline):
    """Time-WEIGHTED distribution of concurrency levels (not just how often the count
    *changes* to each level -- a level that persists for days should count for more than
    one that flickers for a second), since the goal is 'how much real time is spent at N
    concurrent positions,' not 'how many transition events land on N.' `timeline` entries
    are already real datetime objects (see max_concurrent) -- no re-parsing needed here."""
    if len(timeline) < 2:
        return {}
    weighted = {}
    for i in range(len(timeline) - 1):
        t0, count = timeline[i]
        t1, _ = timeline[i + 1]
        dt = (t1 - t0).total_seconds()
        weighted[count] = weighted.get(count, 0.0) + dt
    total = sum(weighted.values())
    return {k: v / total for k, v in weighted.items()} if total > 0 else {}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=365,
                     help="lookback window in days (default 365). A closed trade whose "
                          "exit_time is before this cutoff can't affect the window's "
                          "concurrency and is excluded up front.")
    ap.add_argument("--account", default=None,
                     help="scope to one real account (e.g. soxl_ira) -- default: all "
                          "real accounts pooled together (the real question: how many "
                          "simultaneous positions exist across the whole real portfolio, "
                          "not per-account, since account segregation doesn't change "
                          "whether TWO tickers both need attention/capital at once).")
    ap.add_argument("--db", default=LIVE_DB_PATH)
    args = ap.parse_args()

    now = datetime.utcnow()
    # strftime, NOT .isoformat() -- open_positions/trade_log store 'YYYY-MM-DD HH:MM:SS'
    # (space separator); .isoformat() defaults to 'T', which would silently break the
    # SQL string-comparison filter below for a same-calendar-day cutoff (see
    # max_concurrent's own docstring for the identical risk this avoids downstream).
    since = (now - timedelta(days=args.days)).strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(args.db)
    intervals = _fetch_intervals(conn, since, account=args.account)
    conn.close()

    if not intervals:
        print(f"No real (is_dry_run_sim=0) positions found in the last {args.days} days"
              f"{f' for account={args.account}' if args.account else ''}.")
        return

    peak, peak_time, timeline = max_concurrent(intervals, now.isoformat())
    dist = summarize_timeline(timeline)

    print(f"Concurrent real position check -- last {args.days} days"
          f"{f', account={args.account}' if args.account else ' (all real accounts pooled)'}")
    print(f"  {len(intervals)} real position(s) (closed + currently open) considered")
    print(f"  PEAK concurrent: {peak} (first reached {peak_time})")
    print(f"\n  Time-weighted distribution (% of real time spent at N concurrent):")
    for n in sorted(dist):
        pct = dist[n] * 100
        bar = "#" * int(pct / 2)
        print(f"    {n:2d} concurrent: {pct:5.1f}%  {bar}")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
