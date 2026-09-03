"""Same-day/recent-days minute-data cache (Task #11, 2026-09-01) -- built after a real
design pivot mid-task: the original plan (nightly incremental refresh directly into the
canonical massive_hourly_derived/massive_minute_derived archive) was abandoned once a
real reproducibility risk was confirmed directly in code (bench_phase1_phase2_inmemory.py's
own comment: "a promotion mid-run is not a case this pipeline defends against anywhere
else either") -- a nightly automated active_builds promotion could land mid-campaign and
silently make a multi-hour sweep non-reproducible. The stable canonical archive's refresh
cadence must stay coarse/manual, not automated.

This script exists for a DIFFERENT, narrower need: same-day live-vs-kernel verification
(evening_status.py / paper_vs_backtest_reconcile.get_trades_and_bars_since_ground_truth)
wants FRESH data for the last few real trading days, not a stable backtest snapshot. Real
trigger, same night: UGL's massive_hourly_derived was 10 days stale, causing a false
PHANTOM report.

Design: a genuinely SEPARATE, disposable sqlite db (cache/research/massive_minute_recent.db
-- deliberately NOT trading_universe.db, for full decoupling: zero lock contention with a
real sweep campaign hitting that db concurrently, and "disposable" is then literal --
this file can be deleted and rebuilt from scratch at any time with zero data loss, since
it never holds anything the canonical archive doesn't also eventually get via the existing
manual/ad-hoc refresh path). Holds only a rolling RECENT_DAYS-day window per ticker --
each run fully REPLACES that ticker's rows (delete + reinsert), no incremental merge
complexity needed (unlike the abandoned canonical-refresh design) since this cache never
claims to be authoritative history, only "good enough for the last few days."

Reuses fetch_massive_minute_data.fetch_ticker (the same real API-calling primitive the
canonical-refresh path and scripts/fetch_massive_minute_incremental.py both use) and
scripts.fetch_massive_minute_incremental._rows_to_df for the raw-rows-to-DataFrame
transform -- no new Massive-fetch codepath.

KNOWN, ACCEPTED LIMITATION (documented explicitly per paired design discussion, not
silently absorbed): this cache is only SPLIT-adjusted (Massive's aggs `adjusted=true`
flag), NOT dividend-adjusted, unlike massive_hourly_derived/massive_minute_derived (which
apply Massive's own /stocks/v1/dividends endpoint on top). Over a RECENT_DAYS-day window
this is very likely negligible for these leveraged ETF tickers (no dividend event is
expected to land inside almost any given few-day window), but it is a real, uncorrected
discrepancy versus the canonical dividend-adjusted series -- acceptable for same-day
verification's purpose (does today's real trade match what the kernel WOULD have done),
not acceptable if this cache were ever repurposed as a backtest data source (it must not
be -- see the reproducibility-risk finding above for why that boundary matters).

Usage:
    .venv/bin/python scripts/refresh_recent_minute_cache.py
        # default: every real capital-at-stake ticker (same selection as
        # signals_invariants.check_massive_hourly_derived_freshness, via
        # signals_db.get_watchlist + signals_helpers.has_capital_at_stake)
    .venv/bin/python scripts/refresh_recent_minute_cache.py --tickers UGL AGQ
    .venv/bin/python scripts/refresh_recent_minute_cache.py --recent-days 10

Read side: scripts.refresh_recent_minute_cache.load_recent_cache(ticker) -- returns a
tz-naive OHLC DataFrame (empty if nothing cached for that ticker), consumed by
paper_vs_backtest_reconcile.get_trades_and_bars_since_ground_truth to extend the
canonical minute_df/df_h tail before its staleness check, WITHOUT ever writing to
canonical."""
import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.fetch_massive_minute_data import fetch_ticker
from scripts.fetch_massive_minute_incremental import _rows_to_df

RECENT_DAYS = 7
DB_PATH = Path(__file__).resolve().parent.parent / "cache" / "research" / "massive_minute_recent.db"


def ensure_table(db_path=None):
    db_path = db_path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS minute_bars_recent (
                ticker TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                Open REAL NOT NULL,
                High REAL NOT NULL,
                Low REAL NOT NULL,
                Close REAL NOT NULL,
                Volume REAL,
                refreshed_at TEXT NOT NULL,
                PRIMARY KEY (ticker, timestamp)
            )
        """)


def _canonical_hourly_max_date(ticker):
    """Real cutoff of massive_hourly_derived's active build for this ticker, or None if
    no build exists yet. Same source (db_cache.get_massive_hourly_ohlcv) the real splice
    consumer (paper_vs_backtest_reconcile.get_trades_and_bars_since_ground_truth) reads,
    so 'does this ticker's start reach canonical' is answered with the exact same data
    the splice will actually see -- not a separately-derived approximation that could
    itself drift out of sync with the real canonical cutoff."""
    import db_cache
    try:
        df_h = db_cache.get_massive_hourly_ohlcv(ticker)
    except ValueError:
        # get_massive_hourly_ohlcv raises (not an empty frame) when no build exists yet
        # for this ticker -- a real "no canonical data at all" case, same as empty here.
        return None
    return None if df_h.empty else df_h.index.max().date()


def refresh_ticker(ticker, recent_days=RECENT_DAYS, db_path=None):
    """start reaches back far enough to directly abut canonical massive_hourly_derived's
    own cutoff (whatever that is for THIS ticker), not just a fixed recent_days window
    from today -- found 2026-09-02: canonical only refreshes manually (no cron), so once
    a ticker's build drifts more than recent_days behind, the fixed window leaves a real
    gap of missing trading days between canonical's end and this cache's start that
    neither source covers. That gap silently corrupts every rolling SMA/Std computed
    across it (a whole missing window of daily closes), which can shift a GT kernel
    replay's z-score band enough to miss a real, independently-confirmed signal breach
    (traced live on AGQ/HIBL/NUGT/UGL, all missing real trading days 08-24/08-25 between
    canonical's 08-21 cutoff and this cache's then-7-day-window 08-26 start). Still capped
    at recent_days as the default/minimum -- this only ever widens the window to close a
    real gap, never narrows the ordinary case below the existing default."""
    db_path = db_path or DB_PATH
    end = date.today()
    start = end - timedelta(days=recent_days)
    canonical_max = _canonical_hourly_max_date(ticker)
    if canonical_max is not None and canonical_max + timedelta(days=1) < start:
        start = canonical_max + timedelta(days=1)
    rows = fetch_ticker(ticker, start, end)
    if not rows:
        print(f"  {ticker}: no data returned, leaving prior cache (if any) untouched")
        return 0
    df = _rows_to_df(rows)
    df["timestamp"] = df["timestamp"].dt.tz_localize(None)  # match get_massive_minute_ohlcv's
                                                              # tz-naive index convention

    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM minute_bars_recent WHERE ticker = ?", (ticker,))
        refreshed_at = pd.Timestamp.now(tz="America/New_York").isoformat()
        conn.executemany(
            "INSERT INTO minute_bars_recent (ticker, timestamp, Open, High, Low, Close, Volume, refreshed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(ticker, ts.isoformat(), o, h, l, c, v, refreshed_at)
             for ts, o, h, l, c, v in zip(df["timestamp"], df["Open"], df["High"],
                                           df["Low"], df["Close"], df["Volume"])])
        conn.commit()
    print(f"  {ticker}: cached {len(df):,} rows ({df['timestamp'].min()} -> {df['timestamp'].max()})")
    return len(df)


def load_recent_cache(ticker, db_path=None):
    """Read side, consumed by paper_vs_backtest_reconcile.py. Returns an empty
    (correctly-shaped) DataFrame if this ticker has never been cached -- callers
    should treat that the same as 'no recent-cache data available', not an error."""
    db_path = db_path or DB_PATH
    if not Path(db_path).exists():
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"],
                             index=pd.DatetimeIndex([], name="timestamp"))
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(
            "SELECT timestamp, Open, High, Low, Close FROM minute_bars_recent "
            "WHERE ticker = ? ORDER BY timestamp", conn, params=(ticker,))
    if df.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"],
                             index=pd.DatetimeIndex([], name="timestamp"))
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.set_index("timestamp")[["Open", "High", "Low", "Close"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=None,
                     help="default: every real capital-at-stake ticker "
                          "(signals_db.get_watchlist + has_capital_at_stake)")
    ap.add_argument("--recent-days", type=int, default=RECENT_DAYS)
    args = ap.parse_args()

    if args.tickers:
        tickers = args.tickers
    else:
        import signals_db as db
        import signals_helpers as helpers
        tickers = sorted({n["ticker"] for n in db.get_watchlist()
                           if helpers.has_capital_at_stake(n)})

    ensure_table()
    print(f"Recent-minute-cache refresh: {len(tickers)} ticker(s), last {args.recent_days} days: {tickers}")
    total = 0
    for ticker in tickers:
        print(f"=== {ticker} ===")
        total += refresh_ticker(ticker, recent_days=args.recent_days)
    print(f"\nDone: {total:,} total rows cached across {len(tickers)} ticker(s) in {DB_PATH}")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
