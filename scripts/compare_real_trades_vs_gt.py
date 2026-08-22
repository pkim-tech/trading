"""Trade-by-trade comparison: real broker-executed trades vs BOTH ground-truth
implementations (production v6 kernel `backtester.run_backtest_ground_truth`, and the
original independent prototype `scripts/sim_minute_groundtruth_independent.simulate`),
for the 12 real capital-at-stake tickers. Built 2026-08-22 for the HIGH backlog item
(SOXL/HIBL negative-CAGR finding needing trade-level re-verification before any live
watchlist action).

Two independently-implemented matching methodologies, per the same "don't trust one
script" pattern used the night this backlog item was raised:
  (A) reuses match_trades() from scripts/verify_real_trades_vs_kernel.py (optimal
      bipartite matching via scipy linear_sum_assignment on |delta-hours|).
  (B) a from-scratch, independent greedy tolerance match (nearest entry_time within
      tolerance, no shared code with (A)) -- a bug in (A)'s matching logic can't
      silently produce a false agreement (B) would also miss.

Matches on the real FILL time (trade_log.entry_time) against each GT source's own
entry_time (also a fill time, post-WAIT-resolution for TrailingBoth) -- NOT
signal_time, unlike verify_real_trades_vs_kernel.py, because that tool's target kernel
(run_backtest_dispatch) returns an "Entry Time" that is actually the SIGNAL bar, while
both GT sources here resolve Entry Time to the real fill. Confirmed by reading each
kernel's own trade-construction code before writing this, not assumed.

Read-only / compute-only -- no production kernel file modified, no live watchlist state
touched, no sweep campaign launched (each ticker's own single live config is run once
through each kernel, not a grid).
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from backtester import run_backtest_ground_truth, prep_inputs, prep_minute_inputs
from scripts.sim_minute_groundtruth_independent import (
    load_hourly, load_minutes, simulate as prototype_simulate,
)

LIVE_DB = Path(__file__).resolve().parent.parent / "cache" / "live" / "trading_live.db"
TICKERS = ["AGQ", "DFEN", "DPST", "GDXU", "HIBL", "JNUG", "KORU", "LABU",
           "NUGT", "SOXL", "UGL", "WEBL"]


def get_live_config(ticker):
    con = sqlite3.connect(LIVE_DB)
    con.row_factory = sqlite3.Row
    row = con.execute(
        """SELECT id, ticker, account, strategy, window, take_profit, stop_loss,
                  max_hold_hours, z_score_threshold, trail_sell_pct, fixed_sl,
                  trail_buy_pct, arm_sell_pct, entry_timing, starting_notional
           FROM watch_list WHERE ticker=? AND state='live' AND archived_at IS NULL""",
        (ticker,),
    ).fetchone()
    con.close()
    if not row:
        return None
    d = dict(row)
    d["is_both"] = d["strategy"] == "TrailingBothZScoreBreakout"
    d["arm_pct"] = d["arm_sell_pct"] if d["is_both"] else d["take_profit"]
    return d


def get_real_trades(ticker):
    con = sqlite3.connect(LIVE_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT id, entry_time, entry_price, exit_time, exit_price, exit_reason, pnl_pct
           FROM trade_log
           WHERE ticker=? AND is_dry_run_sim=0 AND position_source='core' 
           ORDER BY entry_time""",
        (ticker,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def run_production_kernel(cfg, ticker, start, end):
    cache_path = Path(__file__).resolve().parent.parent / "cache" / "research" / f"{ticker}_1h.csv"
    df_hourly_raw = pd.read_csv(cache_path, index_col=0, parse_dates=True)
    df_hourly_raw.index = pd.to_datetime(df_hourly_raw.index).tz_localize(None)
    df_hourly_raw = df_hourly_raw.sort_index()
    close_col = "Adj Close" if "Adj Close" in df_hourly_raw.columns else "Close"
    df_daily = df_hourly_raw.resample("D").last().dropna(subset=[close_col])
    strat_cls = getattr(strategies, cfg["strategy"])
    strat_instance = strat_cls(window=cfg["window"], z_score_threshold=cfg["z_score_threshold"])
    df_daily_processed = strat_instance.generate_daily_indicators(df_daily)
    minute_df = load_minutes(ticker)

    lo = pd.Timestamp(start) - pd.Timedelta(days=5)
    hi = pd.Timestamp(end) + pd.Timedelta(days=5)
    df_hourly_windowed = df_hourly_raw.loc[lo:hi]
    if df_hourly_windowed.empty:
        return None, "no hourly data in window"

    trades = run_backtest_ground_truth(
        df_hourly_windowed, df_daily_processed, ticker, minute_df,
        fixed_sl=float(cfg["fixed_sl"]), arm_pct=float(cfg["arm_pct"]),
        trail_buy_pct=float(cfg["trail_buy_pct"]), trail_sell_pct=float(cfg["trail_sell_pct"]),
        max_hours_to_hold=int(cfg["max_hold_hours"]), z_score_threshold=float(cfg["z_score_threshold"]),
        is_both=cfg["is_both"], open_check_entry_timing=(cfg["entry_timing"] == "open_check"),
        same_bar_reentry=True, need_times=True,
    )
    norm = [{"entry_time": t["Entry Time"], "entry_price": t["Entry Price"],
             "exit_time": t["Exit Time"], "exit_price": t["Exit Price"],
             "exit_reason": t["exit_reason"], "ret": t["Return"]} for t in trades]
    return norm, None


def run_prototype_kernel(cfg, ticker, start, end):
    dfh = load_hourly(ticker)
    mdf = load_minutes(ticker)
    node = {"ticker": ticker, "strategy": cfg["strategy"], "window": cfg["window"],
            "z_score_threshold": cfg["z_score_threshold"], "entry_timing": cfg["entry_timing"],
            "fixed_sl": cfg["fixed_sl"], "arm_pct": cfg["arm_pct"],
            "trail_buy_pct": cfg["trail_buy_pct"], "trail_sell_pct": cfg["trail_sell_pct"],
            "max_hold_hours": cfg["max_hold_hours"]}
    try:
        trades = prototype_simulate(node, dfh, mdf, start, end, intrabar="kernel",
                                     backstop=False, same_bar_reentry=True)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    norm = [{"entry_time": t.entry_time, "entry_price": t.entry_price,
             "exit_time": t.exit_time, "exit_price": t.exit_price,
             "exit_reason": t.reason, "ret": t.ret} for t in trades]
    return norm, None


def match_A(real_trades, other_trades, tolerance_hours=6.0):
    """Reused verify_real_trades_vs_kernel.py's optimal bipartite matching logic
    (imported would require its CLI module-level DB path constant; reimplemented
    verbatim here since it's a small, pure function -- same algorithm, not a patch)."""
    if not real_trades or not other_trades:
        return [], list(real_trades), list(other_trades)
    n, m = len(real_trades), len(other_trades)
    BIG = 1e6
    cost = np.full((n, m), BIG)
    for i, rt in enumerate(real_trades):
        for j, bt in enumerate(other_trades):
            if bt["entry_time"] is None or rt["entry_time"] is None:
                continue
            delta = abs((pd.Timestamp(rt["entry_time"]) - pd.Timestamp(bt["entry_time"])).total_seconds()) / 3600.0
            if delta <= tolerance_hours:
                cost[i, j] = delta
    row_ind, col_ind = linear_sum_assignment(cost)
    matched, used_r, used_b = [], set(), set()
    for i, j in zip(row_ind, col_ind):
        if cost[i, j] < BIG:
            matched.append((real_trades[i], other_trades[j], cost[i, j]))
            used_r.add(i); used_b.add(j)
    unmatched_r = [rt for i, rt in enumerate(real_trades) if i not in used_r]
    unmatched_b = [bt for j, bt in enumerate(other_trades) if j not in used_b]
    return matched, unmatched_r, unmatched_b


def match_B(real_trades, other_trades, tolerance_hours=6.0):
    """Independent from-scratch matcher -- no shared code with match_A. Greedy:
    each real trade claims its nearest not-yet-claimed candidate within tolerance,
    processed in chronological order (not optimal-assignment, deliberately a
    different algorithm class so a bug in (A) can't also be present in (B))."""
    remaining = list(range(len(other_trades)))
    matched, unmatched_r = [], []
    for rt in sorted(real_trades, key=lambda x: x["entry_time"] or pd.Timestamp.min):
        if rt["entry_time"] is None:
            unmatched_r.append(rt); continue
        best_j, best_delta = None, None
        for j in remaining:
            bt = other_trades[j]
            if bt["entry_time"] is None:
                continue
            delta = abs((pd.Timestamp(rt["entry_time"]) - pd.Timestamp(bt["entry_time"])).total_seconds()) / 3600.0
            if delta <= tolerance_hours and (best_delta is None or delta < best_delta):
                best_j, best_delta = j, delta
        if best_j is not None:
            matched.append((rt, other_trades[best_j], best_delta))
            remaining.remove(best_j)
        else:
            unmatched_r.append(rt)
    unmatched_b = [other_trades[j] for j in remaining]
    return matched, unmatched_r, unmatched_b


def main():
    for ticker in TICKERS:
        print(f"\n{'='*90}\n{ticker}\n{'='*90}")
        cfg = get_live_config(ticker)
        if not cfg:
            print("  SKIP: no live watch_list row found")
            continue
        real = get_real_trades(ticker)
        closed_real = [r for r in real if r["exit_time"] is not None]
        print(f"  live config: strategy={cfg['strategy']} window={cfg['window']} z={cfg['z_score_threshold']} "
              f"fixed_sl={cfg['fixed_sl']} arm_pct={cfg['arm_pct']} trail_buy={cfg['trail_buy_pct']} "
              f"trail_sell={cfg['trail_sell_pct']} hold={cfg['max_hold_hours']}h entry_timing={cfg['entry_timing']}")
        print(f"  real trades: {len(real)} total ({len(closed_real)} closed, {len(real)-len(closed_real)} still open)")
        if not real:
            print("  STRUCTURALLY NOT COMPARABLE: zero real trades exist for this node -- nothing to match against.")
            continue

        start = min(r["entry_time"] for r in real)
        end = max((r["exit_time"] or r["entry_time"]) for r in real)
        print(f"  window: {start} .. {end}")

        prod, prod_err = run_production_kernel(cfg, ticker, start, end)
        proto, proto_err = run_prototype_kernel(cfg, ticker, start, end)
        if prod_err:
            print(f"  PRODUCTION KERNEL ERROR: {prod_err}")
        if proto_err:
            print(f"  PROTOTYPE ERROR: {proto_err}")
        if prod is not None:
            print(f"  production-v6 trades in window: {len(prod)}")
        if proto is not None:
            print(f"  prototype trades in window: {len(proto)}")

        real_norm = [{"entry_time": r["entry_time"], "exit_reason": r["exit_reason"]} for r in real]

        for label, other in (("real vs PRODUCTION-v6", prod), ("real vs PROTOTYPE", proto)):
            if other is None:
                continue
            mA, urA, ubA = match_A(real_norm, other)
            mB, urB, ubB = match_B(real_norm, other)
            print(f"  -- {label} --")
            print(f"     methodology A (optimal assignment): {len(mA)} matched, {len(urA)} real unmatched, {len(ubA)} kernel-only")
            print(f"     methodology B (greedy independent):  {len(mB)} matched, {len(urB)} real unmatched, {len(ubB)} kernel-only")
            if len(mA) != len(mB) or {id(x[0]) for x in mA} != {id(x[0]) for x in mB}:
                print("     *** (A) vs (B) DISAGREE on which real trades matched ***")
            for rt, bt, delta in mA:
                exit_match = "OK" if rt["exit_reason"] == bt["exit_reason"] else f"MISMATCH(real={rt['exit_reason']},kernel={bt['exit_reason']})"
                print(f"       matched: real_entry={rt['entry_time']} kernel_entry={bt['entry_time']} "
                      f"delta={delta:.2f}h exit_reason={exit_match}")
            for rt in urA:
                print(f"       UNMATCHED REAL TRADE: entry={rt['entry_time']} exit_reason={rt['exit_reason']}")

        if prod is not None and proto is not None:
            mA, urA, ubA = match_A([{"entry_time": t["entry_time"], "exit_reason": t["exit_reason"]} for t in prod], proto)
            print(f"  -- PRODUCTION-v6 vs PROTOTYPE (re-validating parity under real config/window) --")
            print(f"     {len(mA)} matched, {len(urA)} production-only, {len(ubA)} prototype-only")


if __name__ == "__main__":
    main()
