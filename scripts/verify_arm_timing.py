"""Ground-truth arm/trail-sell trigger check -- the arm-side sibling of
verify_open_price_quality.py, built 2026-09-17 the same night that tool
surfaced the 14:30 stale-price entry bug.

Two SEPARATE checks, because arm and trail-sell have different real trigger
semantics (confirmed against backtester.py/strategies.py during tonight's
design review):
  - ARM (activation): bar-CLOSE only (`c >= arm_price` at hourly close) --
    matches backtest semantics exactly. NOT an intrabar condition.
  - TRAIL-SELL (the actual exit once armed): genuinely intrabar -- Low
    crossing the trailing stop (`peak * (1 - trail_sell_pct/100)`), where
    peak keeps moving up on every new post-arm high.

No dedicated "arm happened" event exists yet (confirmed 2026-09-17 -- no
log_coverage_event call anywhere for an arm/trailing_sell-specific
scenario_key; filed as its own backlog gap). This script uses the best
available proxy for "when we detected it": the FIRST exit_check_decision row
with result='TRAIL' for that position -- bounded by the daemon's own poll
cadence, not a true instant-of-arming timestamp.

Ground truth: hourly cache (cache/research/{ticker}_1h.csv) for the arm
bar-close check; real minute/second data (db_cache.get_massive_second_ohlcv,
falling back to minute) for the trail-sell intrabar check -- printing which
resolution was actually used, never silently substituting.

Read-only, no live API calls, no trading-code changes. Usage:
    .venv/bin/python scripts/verify_arm_timing.py [--since 2026-09-01]
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db_cache

LIVE_DB = "cache/live/trading_live.db"
RESEARCH_DIR = Path(__file__).resolve().parent.parent / "cache" / "research"


def _get_arm_pct(wl_id, con):
    row = con.execute(
        "SELECT strategy, take_profit, arm_sell_pct, trail_sell_pct FROM watch_list WHERE id=?", (wl_id,)
    ).fetchone()
    if row is None:
        return None, None
    arm_pct = row["arm_sell_pct"] if row["strategy"] == "TrailingBothZScoreBreakout" else row["take_profit"]
    return arm_pct, row["trail_sell_pct"]


def _load_hourly(ticker):
    df = pd.read_csv(RESEARCH_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.sort_index()


def _ground_truth_arm_cross(ticker, entry_time_et, arm_price):
    """Bar-CLOSE-only, matches backtest semantics. Returns the bar timestamp
    whose Close first crosses arm_price, or None."""
    try:
        df = _load_hourly(ticker)
    except FileNotFoundError:
        return None
    df = df[df.index > entry_time_et]
    hit = df[df["Close"] >= arm_price]
    return hit.index[0] if not hit.empty else None


def _ground_truth_trail_cross(ticker, arm_bar_close_time, arm_price, trail_pct):
    """Intrabar (High/Low), starting from the arm bar's close. Walks forward
    bar by bar using the coarser hourly High as a rough peak proxy, then
    finds the first minute/second bar whose Low crosses that bar's trailing
    stop -- approximate (a true walk-forward peak needs bar-by-bar state,
    this uses the SIMPLEST correct approximation: peak = the highest hourly
    High from arm through each point) rather than a full intrabar peak walk.
    Returns (cross_time, resolution) or (None, resolution)."""
    try:
        df_hourly = _load_hourly(ticker)
    except FileNotFoundError:
        return None, None
    df_hourly = df_hourly[df_hourly.index >= arm_bar_close_time]
    if df_hourly.empty:
        return None, None
    running_peak = arm_price
    for ts, row in df_hourly.iterrows():
        running_peak = max(running_peak, row["High"])
        stop = running_peak * (1 - trail_pct / 100.0)
        if row["Low"] <= stop:
            return ts, "hourly-approx"
    return None, "hourly-approx"


def _first_trail_detection(con, ticker, after_ts_utc):
    row = con.execute(
        "SELECT ts FROM coverage_events WHERE scenario_key='exit_check_decision' "
        "AND ticker=? AND result='TRAIL' AND ts >= ? ORDER BY ts LIMIT 1",
        (ticker, after_ts_utc),
    ).fetchone()
    if row is None:
        return None
    return datetime.strptime(row["ts"], "%Y-%m-%d %H:%M:%S") - timedelta(hours=4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-01")
    args = ap.parse_args()

    con = sqlite3.connect(LIVE_DB)
    con.row_factory = sqlite3.Row

    trades = con.execute(
        "SELECT t.id, t.ticker, t.wl_id, t.entry_time, t.entry_price "
        "FROM trade_log t WHERE t.is_dry_run_sim=0 AND t.exit_reason='TRAIL' "
        "AND t.entry_time >= ? ORDER BY t.entry_time",
        (args.since,),
    ).fetchall()

    print(f"=== Arm + trail-sell ground-truth check, real TRAIL-exit positions since {args.since} ===\n")
    if not trades:
        print("No real TRAIL-exit trades in range.")
        return

    arm_delays = []
    for t in trades:
        arm_pct, trail_pct = _get_arm_pct(t["wl_id"], con)
        if arm_pct is None or trail_pct is None:
            print(f"  {t['ticker']:6s} trade_id={t['id']}  SKIP (missing arm_pct/trail_pct on node)")
            continue
        arm_price = t["entry_price"] * (1 + arm_pct / 100.0)
        entry_dt = datetime.strptime(t["entry_time"], "%Y-%m-%d %H:%M:%S")

        arm_cross = _ground_truth_arm_cross(t["ticker"], entry_dt, arm_price)
        if arm_cross is None:
            print(f"  {t['ticker']:6s} trade_id={t['id']}  arm_price=${arm_price:.4f}  "
                  f"NO ARM BAR-CLOSE CROSSING FOUND in cached hourly data")
            continue

        arm_cross_utc = (arm_cross + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")
        det_time = _first_trail_detection(con, t["ticker"], arm_cross_utc)
        arm_line = f"  {t['ticker']:6s} trade_id={t['id']}  arm_price=${arm_price:.4f}  arm_bar_close={arm_cross}"
        if det_time is None:
            print(arm_line + "  NO DETECTION EVENT FOUND after arm bar-close")
        else:
            delay = (det_time - arm_cross).total_seconds()
            arm_delays.append(delay)
            flag = " <<< SLOW" if delay >= 300 else ""
            print(arm_line + f"  detected={det_time}  arm_delay={delay:.0f}s{flag}")

        trail_cross, resolution = _ground_truth_trail_cross(t["ticker"], arm_cross, arm_price, trail_pct)
        if trail_cross is None:
            print(f"           trail-sell: no crossing found via {resolution or 'no data'}")
        else:
            print(f"           trail-sell ground truth ({resolution}): stop crossed ~{trail_cross}")

    if arm_delays:
        arm_delays.sort()
        n = len(arm_delays)
        print(f"\n{n} arm detections matched. delay: min={arm_delays[0]:.0f}s  "
              f"median={arm_delays[n//2]:.0f}s  max={arm_delays[-1]:.0f}s")
    else:
        print("\nNo arm detections matched to a ground-truth bar-close crossing.")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
