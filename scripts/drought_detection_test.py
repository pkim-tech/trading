"""
Research script: tests whether a low-latency, twice-daily volatility read (computed at the
existing 9:30/14:30 signal-check checkpoints -- no new infrastructure) can give useful advance
warning of a "drought" (an extended stretch with no trade signals, the mechanism identified
behind SOXL's weak 2026 -- docs/research_log.md's 2026-08-04 late entries) and, separately, warn
when the drought is ending in time to exit an overlay position before normal trading resumes.

Deliberately NOT a retune of the z-threshold (that would be single-episode curve-fitting -- see
conversation). This tests a detection signal pooled across EVERY historical drought on the
watchlist, not just the one known SOXL case, to avoid the same trap.

Method: at every 9:30/14:30 bar in each ticker's real history, computes trailing 5-trading-day
realized volatility (std of hourly log returns, ~35 bars) and its EXPANDING-window percentile
rank against that ticker's own history up to and including that checkpoint (no lookahead -- only
uses what would have been knowable at that exact moment). Identifies droughts (>= MIN_DROUGHT_DAYS
consecutive trading days with zero real trade signals). For each drought: (a) lead time -- how
many trading days before the drought started did the vol percentile first drop below
QUIET_THRESHOLD and stay there; (b) recovery lag -- how many trading days before the drought's
actual end (next real trade signal) did the percentile cross back above RECOVERY_THRESHOLD
(negative = the percentile signal recovered AFTER trading had already resumed, i.e. a lagging,
not leading, indicator).

Usage: .venv/bin/python scripts/drought_detection_test.py [--tickers ...] [--watchlist-id 65]
       [--min-drought-days 10] [--csv]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtester import prep_inputs, OPEN
import strategies
from scripts.export_trades import (
    load_hourly,
    simulate_trail_both_annotated,
    simulate_trail_exit_chaos,
)

LIVE_DB = Path("cache/live/trading_live.db")
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
TARGET_H0, TARGET_H1 = 9, 14
VOL_LOOKBACK_BARS = 35  # ~5 trading days at ~7 bars/day
QUIET_THRESHOLD = 0.20
RECOVERY_THRESHOLD = 0.50


def load_nodes(watchlist_id, tickers=None):
    con = sqlite3.connect(LIVE_DB)
    q = """
        SELECT MIN(id), ticker, strategy, window, z_score_threshold, arm_sell_pct, take_profit,
               fixed_sl, trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing
        FROM watch_list WHERE watchlist_id=? AND mode='research'
    """
    params = [watchlist_id]
    if tickers:
        q += f" AND ticker IN ({','.join('?' * len(tickers))})"
        params += tickers
    q += " GROUP BY ticker"
    rows = con.execute(q, params).fetchall()
    con.close()
    cols = ["id", "ticker", "strategy", "window", "z", "arm_sell_pct", "take_profit", "fixed_sl",
            "trail_buy_pct", "trail_sell_pct", "max_hold_hours", "entry_timing"]
    nodes = [dict(zip(cols, r)) for r in rows]
    for n in nodes:
        n["arm_pct"] = n["arm_sell_pct"] if n["strategy"] == "TrailingBothZScoreBreakout" else n["take_profit"]
    return nodes


def build_indicators(strategy_name, df_daily, window):
    strat_cls = getattr(strategies, strategy_name)
    return strat_cls(window=window).generate_daily_indicators(df_daily)


def get_signal_days(node):
    """Real trade signal days (deduped to calendar days) for drought detection."""
    df_h = load_hourly(node["ticker"])
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    ind = build_indicators(node["strategy"], df_daily, node["window"])
    p = prep_inputs(df_h, ind)
    open_check = node["entry_timing"] == "open_check"

    if node["strategy"] == "TrailingBothZScoreBreakout":
        trades = simulate_trail_both_annotated(
            p, node["arm_pct"] / 100.0, node["fixed_sl"] / 100.0, node["max_hold_hours"],
            node["trail_buy_pct"] / 100.0, node["trail_sell_pct"] / 100.0,
            TARGET_H0, TARGET_H1, node["z"], open_check=open_check,
        )
    elif node["strategy"] == "TrailingExitZScoreBreakout":
        rng = np.random.default_rng(0)
        trades = simulate_trail_exit_chaos(
            p, node["arm_pct"] / 100.0, node["fixed_sl"] / 100.0, node["max_hold_hours"],
            node["trail_sell_pct"] / 100.0, TARGET_H0, TARGET_H1, node["z"],
            rng, "drop", 0.0, "drop", 0.0, open_check=open_check,
        )
    else:
        raise ValueError(f"unhandled strategy {node['strategy']}")

    signal_days = sorted({
        df_h.index[t["signal_i"]].normalize()
        for t in trades if t["signal_i"] is not None and t["result"] != OPEN
    })
    return signal_days, df_h


def checkpoint_series(df_h):
    """Builds the checkpoint-level (9:30/14:30 bars only) trailing-vol + expanding-percentile
    series, matching what's actually computable live at those two daily checks."""
    hours = df_h.index.hour
    mask = (hours == TARGET_H0) | (hours == TARGET_H1)
    checkpoints = df_h.index[mask]

    log_ret = np.diff(np.log(df_h["Close"].values))
    log_ret = np.insert(log_ret, 0, np.nan)
    ret_series = pd.Series(log_ret, index=df_h.index)

    rows = []
    all_vols_so_far = []
    for ts in checkpoints:
        loc = df_h.index.get_loc(ts)
        if isinstance(loc, slice):
            loc = loc.stop - 1
        if loc < VOL_LOOKBACK_BARS:
            continue
        window_rets = ret_series.iloc[loc - VOL_LOOKBACK_BARS + 1: loc + 1]
        if window_rets.isna().any():
            continue
        vol = float(window_rets.std())
        all_vols_so_far.append(vol)
        if len(all_vols_so_far) < 30:  # need enough history for a meaningful percentile
            continue
        pctile = float((np.array(all_vols_so_far[:-1]) < vol).mean())
        rows.append({"time": ts, "day": ts.normalize(), "vol": vol, "pctile": pctile})
    return pd.DataFrame(rows)


def find_droughts(signal_days, all_trading_days, min_drought_days):
    signal_days_set = set(signal_days)
    droughts = []
    gap_start = None
    for i, day in enumerate(all_trading_days):
        if day in signal_days_set:
            if gap_start is not None:
                gap_len = i - gap_start
                if gap_len >= min_drought_days:
                    droughts.append((all_trading_days[gap_start], day, gap_len))
                gap_start = None
        else:
            if gap_start is None:
                gap_start = i
    return droughts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=None)
    parser.add_argument("--watchlist-id", type=int, default=65)
    parser.add_argument("--min-drought-days", type=int, default=10)
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()

    nodes = load_nodes(args.watchlist_id, args.tickers)
    drought_rows = []
    for node in nodes:
        try:
            signal_days, df_h = get_signal_days(node)
        except Exception as e:
            print(f"{node['ticker']}: failed ({e})")
            continue

        cp = checkpoint_series(df_h)
        if cp.empty:
            continue
        daily_last = cp.groupby("day").last().reset_index()
        all_days = list(daily_last["day"])
        droughts = find_droughts(signal_days, all_days, args.min_drought_days)

        day_to_idx = {d: i for i, d in enumerate(all_days)}
        for start, end, gap_len in droughts:
            start_idx = day_to_idx[start]
            # lead time: walk backward from drought start to find how many days earlier
            # pctile first dropped below QUIET_THRESHOLD and never rose above it again
            # before the drought started.
            lead_days = 0
            i = start_idx - 1
            while i >= 0 and daily_last["pctile"].iloc[i] < QUIET_THRESHOLD:
                lead_days += 1
                i -= 1

            end_idx = day_to_idx[end]
            # recovery lag: walk backward from drought end to find how many days earlier
            # pctile first crossed back above RECOVERY_THRESHOLD and stayed there.
            recovery_lead_days = 0
            i = end_idx - 1
            while i >= start_idx and daily_last["pctile"].iloc[i] >= RECOVERY_THRESHOLD:
                recovery_lead_days += 1
                i -= 1
            # if recovery threshold was never crossed before the drought ended, it's a lag
            recovered_in_time = recovery_lead_days > 0

            drought_rows.append({
                "ticker": node["ticker"], "start": str(start.date()), "end": str(end.date()),
                "gap_trading_days": gap_len, "detection_lead_days": lead_days,
                "recovery_lead_days": recovery_lead_days if recovered_in_time else 0,
                "recovered_before_end": recovered_in_time,
            })

    df = pd.DataFrame(drought_rows)
    if df.empty:
        print("No droughts found.")
        return
    pd.set_option("display.width", 160)
    pd.set_option("display.max_rows", None)

    if args.csv:
        OUTPUT_DIR.mkdir(exist_ok=True)
        df.to_csv(OUTPUT_DIR / "drought_detection_test.csv", index=False)
        print(f"Wrote {OUTPUT_DIR / 'drought_detection_test.csv'}\n")

    print(f"--- All droughts found (>= {args.min_drought_days} trading days with zero signals) ---")
    print(df.to_string(index=False))

    print(f"\n--- Summary (n={len(df)} droughts across {df['ticker'].nunique()} tickers) ---")
    print(f"Detection lead time (days quiet-flag was already on before drought started): "
          f"mean={df['detection_lead_days'].mean():.1f}  median={df['detection_lead_days'].median():.1f}")
    print(f"Droughts with ANY detection lead (>0 days warning): "
          f"{(df['detection_lead_days'] > 0).sum()}/{len(df)}")
    print(f"Droughts where recovery signal fired before the drought actually ended: "
          f"{df['recovered_before_end'].sum()}/{len(df)}")
    recovered = df[df["recovered_before_end"]]
    if len(recovered):
        print(f"  Among those: mean recovery lead time = {recovered['recovery_lead_days'].mean():.1f} days")


if __name__ == "__main__":
    main()
