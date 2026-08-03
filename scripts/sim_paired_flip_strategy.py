"""
Paired-capital strategy, aggressive-flip variant (2026-08-03) -- NOT v5, a
distinct strategy paradigm per the user's explicit call. Compares three ways
of using a primary ticker's idle capital on a matched filler/inverse ticker:

  1. skip-only    -- filler only trades on its own natural signal, and only
                     counted if the whole trade fits inside an idle window
                     (scripts/sim_v6_inverse_secondary_trade.py's method).
  2. forced-exit  -- filler trades on its own natural signal any time
                     primary is idle, but gets force-closed the instant
                     primary re-enters (scripts/sim_constrained_inverse_pair.py).
  3. aggressive-flip -- the moment primary's position closes, immediately
                     open a filler position right then (regardless of the
                     filler's own independent z-score state at that instant
                     -- "flip the order as if you had the other side"),
                     which then runs under the filler's own SL/TP/trailing/
                     hold rules until either it exits naturally or primary's
                     next entry forces it closed. If the flip position closes
                     naturally before primary re-enters, normal idle-window
                     signal scanning resumes for any remaining idle time.

All three use the SAME filler node config so the comparison isolates the
capital-usage rule, not a parameter difference. Reports total compounded
gain for the primary alone vs. each variant's joint result.

Usage:
  .venv/bin/python scripts/sim_paired_flip_strategy.py --primary AGQ --filler ZSL
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import strategies
from backtester import prep_inputs, run_backtest_dispatch, WIN, LOSS, TWIN, TLOSS, OPEN
from scripts.export_trades import load_hourly
from scripts.campaign_comparison_table import best_node, safe_best, best_any, fmt_node
from scripts.sim_v6_joint_capital import get_watchlist_node, trades_from_live_node, windows_from_trades, compounded

DB_PATH = "cache/research/trading_universe.db"
STRATEGY = "TrailingExitZScoreBreakout"
FIXED_SLS = [1, 2, 3]
ENTRY_TIMING = "open_check"
FLIP = 6


def get_own_trades_and_config(ticker, con):
    node = get_watchlist_node(ticker)
    if node is not None:
        trades, label, kind = trades_from_live_node(ticker, node)
        return trades, label, kind, None  # no reusable (window,z,sl,tp,hold) config for flip sim
    nodes = [best_node(con, "v5", ticker, STRATEGY, fsl, ENTRY_TIMING) for fsl in FIXED_SLS]
    nodes = [n for n in nodes if n]
    node = safe_best(nodes) or best_any(nodes)
    if node is None:
        return None, None, None, None

    df_h = load_hourly(ticker)
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    strat = strategies.TrailingExitZScoreBreakout(window=node["window"], z_score_threshold=node["z"])
    ind = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_h, ind)
    trades = run_backtest_dispatch(
        strategies.TrailingExitZScoreBreakout, df_h, ind, ticker,
        take_profit=node["tp"], sl_raw=node["axis"], max_hours_to_hold=node["hold"],
        z_score_threshold=node["z"], fixed_sl=node["fixed_sl"], trail_pct_pct=0.0,
        entry_timing=ENTRY_TIMING, prep=p)
    return trades, f"{STRATEGY} {fmt_node(node)}", ("cliff-safe" if node["safe"] else "best-any"), node


def skip_only_gain(primary_trades, filler_trades):
    windows = windows_from_trades(primary_trades)
    fits = [t for t in filler_trades
            for _, w in windows.iterrows()
            if t["Entry Time"] >= w["start"] and t["Exit Time"] <= w["end"]]
    merged = sorted(primary_trades + fits, key=lambda t: t["Entry Time"])
    rets = [(t["Exit Price"] / t["Entry Price"]) - 1 for t in merged]
    return compounded(rets), len(fits)


def forced_exit_gain(primary_trades, filler_ticker, filler_node):
    """Reruns filler's own kernel bar-by-bar with busy-gated entries + forced
    exit on primary re-entry (mirrors sim_constrained_inverse_pair.py)."""
    df_h = load_hourly(filler_ticker)
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    strat = strategies.TrailingExitZScoreBreakout(window=filler_node["window"], z_score_threshold=filler_node["z"])
    ind = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_h, ind)

    ts_index = pd.Index(p["timestamps"])
    n = len(ts_index)
    busy = np.zeros(n, dtype=bool)
    force_exit = np.zeros(n, dtype=bool)
    for t in primary_trades:
        mask = (ts_index >= t["Entry Time"]) & (ts_index <= t["Exit Time"])
        busy |= mask
        pos = ts_index.searchsorted(t["Entry Time"])
        if pos < n and ts_index[pos] == t["Entry Time"]:
            force_exit[pos] = True

    filler_trades = _simulate_gated(
        p, take_profit=filler_node["tp"] / 100.0, stop_loss=filler_node["axis"] / 100.0,
        max_hours_to_hold=filler_node["hold"], trail_pct=filler_node["axis"] / 100.0,
        z_thresh=filler_node["z"], busy=busy, force_exit=force_exit, flip_entry=None)

    merged = sorted(primary_trades + filler_trades, key=lambda t: t.get("entry_ts") or t["Entry Time"])
    rets = [t["Return"] if "Return" in t else (t["Exit Price"] / t["Entry Price"]) - 1 for t in merged]
    return compounded(rets), len(filler_trades)


def aggressive_flip_gain(primary_trades, filler_ticker, filler_node):
    """Same as forced_exit_gain, but ALSO opens a filler position the instant
    primary exits, regardless of filler's own signal state at that bar."""
    df_h = load_hourly(filler_ticker)
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    strat = strategies.TrailingExitZScoreBreakout(window=filler_node["window"], z_score_threshold=filler_node["z"])
    ind = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_h, ind)

    ts_index = pd.Index(p["timestamps"])
    n = len(ts_index)
    busy = np.zeros(n, dtype=bool)
    force_exit = np.zeros(n, dtype=bool)
    flip_entry = np.zeros(n, dtype=bool)
    for t in primary_trades:
        mask = (ts_index >= t["Entry Time"]) & (ts_index <= t["Exit Time"])
        busy |= mask
        entry_pos = ts_index.searchsorted(t["Entry Time"])
        if entry_pos < n and ts_index[entry_pos] == t["Entry Time"]:
            force_exit[entry_pos] = True
        exit_pos = ts_index.searchsorted(t["Exit Time"])
        # flip fires the bar AFTER primary's exit bar (capital free from next bar)
        if exit_pos + 1 < n:
            flip_entry[exit_pos + 1] = True

    filler_trades = _simulate_gated(
        p, take_profit=filler_node["tp"] / 100.0, stop_loss=filler_node["axis"] / 100.0,
        max_hours_to_hold=filler_node["hold"], trail_pct=filler_node["axis"] / 100.0,
        z_thresh=filler_node["z"], busy=busy, force_exit=force_exit, flip_entry=flip_entry)

    merged = sorted(primary_trades + filler_trades, key=lambda t: t.get("entry_ts") or t["Entry Time"])
    rets = [t["Return"] if "Return" in t else (t["Exit Price"] / t["Entry Price"]) - 1 for t in merged]
    n_flips = sum(1 for t in filler_trades if t.get("Result") == FLIP)
    return compounded(rets), len(filler_trades), n_flips


def _simulate_gated(p, take_profit, stop_loss, max_hours_to_hold, trail_pct, z_thresh,
                     busy, force_exit, flip_entry):
    prices, opens, highs, lows = p["prices"], p["opens"], p["highs"], p["lows"]
    hours, daily_idx = p["hours"], p["daily_idx"]
    sma_arr, std_arr = p["sma_arr"], p["std_arr"]
    trend_arr, has_trend = p["trend_arr"], p["has_trend"]
    timestamps = p["timestamps"]
    n = len(prices)

    trades = []
    in_trade = trailing = False
    entry_price = stop_price = tp_price = peak = 0.0
    entry_bar = held = 0
    entered_via_flip = False

    for i in range(n):
        cp, op, high, low = prices[i], opens[i], highs[i], lows[i]

        if in_trade:
            held += 1
            if force_exit[i]:
                exit_px = op
                pc = (exit_px - entry_price) / entry_price
                trades.append({"entry_ts": timestamps[entry_bar], "Entry Time": timestamps[entry_bar],
                                "Exit Time": timestamps[i], "Entry Price": entry_price,
                                "Exit Price": exit_px, "Result": FLIP if entered_via_flip else LOSS,
                                "Return": pc})
                in_trade = trailing = entered_via_flip = False
                continue
            if trailing:
                trail_stop_gap = peak * (1.0 - trail_pct)
                if op <= trail_stop_gap:
                    pc = (op - entry_price) / entry_price
                    trades.append({"entry_ts": timestamps[entry_bar], "Entry Time": timestamps[entry_bar],
                                    "Exit Time": timestamps[i], "Entry Price": entry_price,
                                    "Exit Price": op, "Result": WIN if pc > 0 else LOSS, "Return": pc})
                    in_trade = trailing = entered_via_flip = False
                    continue
                if high > peak:
                    peak = high
                trail_stop = peak * (1.0 - trail_pct)
                if low <= trail_stop or held >= max_hours_to_hold:
                    exit_px = trail_stop if low <= trail_stop else cp
                    pc = (exit_px - entry_price) / entry_price
                    trades.append({"entry_ts": timestamps[entry_bar], "Entry Time": timestamps[entry_bar],
                                    "Exit Time": timestamps[i], "Entry Price": entry_price,
                                    "Exit Price": exit_px, "Result": WIN if pc > 0 else LOSS, "Return": pc})
                    in_trade = trailing = entered_via_flip = False
                continue
            if op <= stop_price:
                pc = (op - entry_price) / entry_price
                trades.append({"entry_ts": timestamps[entry_bar], "Entry Time": timestamps[entry_bar],
                                "Exit Time": timestamps[i], "Entry Price": entry_price,
                                "Exit Price": op, "Result": LOSS, "Return": pc})
                in_trade = entered_via_flip = False
                continue
            if low <= stop_price:
                pc = (stop_price - entry_price) / entry_price
                trades.append({"entry_ts": timestamps[entry_bar], "Entry Time": timestamps[entry_bar],
                                "Exit Time": timestamps[i], "Entry Price": entry_price,
                                "Exit Price": stop_price, "Result": LOSS, "Return": pc})
                in_trade = entered_via_flip = False
                continue
            if cp >= tp_price:
                trailing, peak = True, cp
                continue
            if held >= max_hours_to_hold:
                pc = (cp - entry_price) / entry_price
                trades.append({"entry_ts": timestamps[entry_bar], "Entry Time": timestamps[entry_bar],
                                "Exit Time": timestamps[i], "Entry Price": entry_price,
                                "Exit Price": cp, "Result": TWIN if pc > 0 else TLOSS, "Return": pc})
                in_trade = entered_via_flip = False
            continue

        # not in trade
        if flip_entry is not None and flip_entry[i] and not busy[i]:
            in_trade, trailing, entered_via_flip = True, False, True
            entry_price = op
            tp_price, stop_price = op * (1 + take_profit), op * (1 - stop_loss)
            entry_bar, held = i, 0
            continue

        if busy[i]:
            continue

        h = hours[i]
        if h != 9 and h != 14:
            continue
        di = daily_idx[i]
        if di < 0:
            continue
        sma, std = sma_arr[di], std_arr[di]
        if std == 0.0:
            continue
        lower_band = sma - std * z_thresh
        signal_open = (op <= lower_band) and (op > trend_arr[di]) if has_trend else op <= lower_band
        if signal_open:
            in_trade, trailing, entered_via_flip = True, False, False
            entry_price = op
            tp_price, stop_price = op * (1 + take_profit), op * (1 - stop_loss)
            entry_bar, held = i, 0

    return trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--primary", required=True)
    ap.add_argument("--filler", required=True)
    args = ap.parse_args()
    con = sqlite3.connect(DB_PATH)

    primary_trades, p_label, p_kind, _ = get_own_trades_and_config(args.primary, con)
    print(f"{args.primary}: {len(primary_trades)} real trades ({p_label})")
    p_rets = [(t["Exit Price"] / t["Entry Price"]) - 1 for t in primary_trades]
    primary_alone = compounded(p_rets)
    print(f"  primary alone: {primary_alone:.1f}%")

    filler_trades, f_label, f_kind, f_node = get_own_trades_and_config(args.filler, con)
    print(f"{args.filler}: {len(filler_trades)} real trades ({f_label}, {f_kind})")
    if f_node is None:
        print("  filler has a live node (not a best_node config) -- flip/forced-exit variants "
              "need a reusable (window,z,sl,tp,hold) config, skipping those, skip-only only")
        joint, n_fit = skip_only_gain(primary_trades, filler_trades)
        print(f"  1. skip-only:       joint={joint:.1f}%  mult={((1+joint/100)/(1+primary_alone/100)):.2f}x  ({n_fit} filler trades)")
        return

    joint1, n_fit1 = skip_only_gain(primary_trades, filler_trades)
    joint2, n_fit2 = forced_exit_gain(primary_trades, args.filler, f_node)
    joint3, n_fit3, n_flips = aggressive_flip_gain(primary_trades, args.filler, f_node)

    print(f"\n  1. skip-only:       joint={joint1:7.1f}%  mult={(1+joint1/100)/(1+primary_alone/100):.2f}x  ({n_fit1} filler trades)")
    print(f"  2. forced-exit:     joint={joint2:7.1f}%  mult={(1+joint2/100)/(1+primary_alone/100):.2f}x  ({n_fit2} filler trades)")
    print(f"  3. aggressive-flip: joint={joint3:7.1f}%  mult={(1+joint3/100)/(1+primary_alone/100):.2f}x  ({n_fit3} filler trades, {n_flips} via flip)")


if __name__ == "__main__":
    main()
