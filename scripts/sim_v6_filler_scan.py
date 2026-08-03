"""
v6 idea, filler-scan variant (2026-08-03): the user's real goal is fuller
capital utilization -- multiply total compounded gain by keeping idle
capital working, not necessarily finding a "perfect" inverse. Corrects two
things from the earlier passes:
  (1) strict zero-overlap only (no forced early exit) -- a filler trade is
      only counted if its entire real entry-to-exit span fits inside one of
      the primary's idle windows. Giving up a few trades is fine; the goal
      is to never distort a node's exit behavior away from what its own
      backtest says it does.
  (2) scored on TOTAL COMPOUNDED GAIN, not alpha vs SPY -- the ask is "does
      running the filler alongside the primary multiply total capital
      growth", not "does the filler alone beat the market."
  (3) widened candidate pool -- not restricted to a matched leveraged
      inverse. Any ticker with its own decent v5-backtested node is a fair
      filler candidate, scanned the same way sim_v6_parking_vehicle_sweep.py
      scanned the whole universe for a static-hold vehicle, but here each
      candidate is actively traded on its own real signal rules.

Candidate pool: the 12 tickers with any real v5 backtest_cache data today
(AGQ, DUST, GDXD, GDXU, HIBL, NUGT, SOXL, TQQQ, USD, UVIX, YANG, ZSL) --
everything else in config.json's broader universe has no v5 data yet and
would need a resweep first (see docs/backlog_cache.md).

Usage:
  .venv/bin/python scripts/sim_v6_filler_scan.py --primary GDXU
  .venv/bin/python scripts/sim_v6_filler_scan.py  # all 3 ready primaries
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

import strategies
from backtester import prep_inputs, run_backtest_dispatch
from scripts.export_trades import load_hourly
from scripts.sim_v6_parking_vehicle_sweep import extract_gap_windows
from scripts.campaign_comparison_table import best_node, safe_best, best_any, fmt_node

DB_PATH = "cache/research/trading_universe.db"
STRATEGY = "TrailingExitZScoreBreakout"
FIXED_SLS = [1, 2, 3]
ENTRY_TIMING = "open_check"

PRIMARIES = ["GDXU", "NUGT", "AGQ"]
CANDIDATE_POOL = ["AGQ", "DUST", "GDXD", "GDXU", "HIBL", "NUGT", "SOXL",
                  "TQQQ", "USD", "UVIX", "YANG", "ZSL"]


def get_candidate_trades(ticker, node):
    strategy_class = getattr(strategies, STRATEGY)
    _, fourth_axis_col = strategies.resolve_axis_columns(STRATEGY)
    trail_pct_pct = node["tpct"] if fourth_axis_col == "trail_pct" else 0.0

    df_h = load_hourly(ticker)
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    strat = strategy_class(window=node["window"], z_score_threshold=node["z"])
    ind = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_h, ind)

    return run_backtest_dispatch(
        strategy_class, df_h, ind, ticker,
        take_profit=node["tp"], sl_raw=node["axis"], max_hours_to_hold=node["hold"],
        z_score_threshold=node["z"], fixed_sl=node["fixed_sl"],
        trail_pct_pct=trail_pct_pct, entry_timing=ENTRY_TIMING, prep=p,
    )


def trade_return(t):
    return (t["Exit Price"] / t["Entry Price"]) - 1


def score_filler(primary, candidate, windows_df, con):
    if candidate == primary:
        return None
    nodes = [best_node(con, "v5", candidate, STRATEGY, fsl, ENTRY_TIMING) for fsl in FIXED_SLS]
    nodes = [n for n in nodes if n]
    node = safe_best(nodes) or best_any(nodes)
    if node is None:
        return None
    trades = get_candidate_trades(candidate, node)

    fits = [t for t in trades
            for _, w in windows_df.iterrows()
            if t["Entry Time"] >= w["start"] and t["Exit Time"] <= w["end"]]
    if not fits:
        return {"candidate": candidate, "node": fmt_node(node), "safe": node["safe"],
                "own_trades": len(trades), "filler_trades": 0,
                "idle_windows_used": 0, "compounded_gain_pct": 0.0}

    rets = [trade_return(t) for t in fits]
    compounded = 1.0
    for r in rets:
        compounded *= (1 + r)
    wins = sum(1 for r in rets if r > 0)
    windows_used = len(set((t["Entry Time"], t["Exit Time"]) for t in fits))
    return {
        "candidate": candidate, "node": fmt_node(node), "safe": node["safe"],
        "own_trades": len(trades), "filler_trades": len(fits),
        "idle_windows_used": windows_used,
        "compounded_gain_pct": (compounded - 1) * 100,
        "win_rate_pct": wins / len(rets) * 100,
    }


def run_primary(primary, con):
    print(f"\n=== {primary}: idle-window filler scan ===")
    windows_df = pd.DataFrame(extract_gap_windows(primary))
    if windows_df.empty:
        print(f"  no idle windows for {primary}, skipping")
        return []
    print(f"  {len(windows_df)} real idle windows")

    rows = []
    for candidate in CANDIDATE_POOL:
        r = score_filler(primary, candidate, windows_df, con)
        if r:
            r["primary"] = primary
            rows.append(r)
            print(f"  {candidate}: {r['filler_trades']} filler trades / {r['own_trades']} own "
                  f"({r['idle_windows_used']}/{len(windows_df)} windows used), "
                  f"compounded_gain={r['compounded_gain_pct']:.1f}%")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--primary", choices=PRIMARIES, default=None)
    args = ap.parse_args()
    con = sqlite3.connect(DB_PATH)

    all_rows = []
    for primary in ([args.primary] if args.primary else PRIMARIES):
        all_rows.extend(run_primary(primary, con))

    out = pd.DataFrame(all_rows)
    out = out.sort_values(["primary", "compounded_gain_pct"], ascending=[True, False])
    print("\n=== Summary (best filler per primary, by total compounded gain) ===")
    for primary in out["primary"].unique():
        sub = out[out["primary"] == primary].head(3)
        print(f"\n{primary}:")
        print(sub[["candidate", "filler_trades", "idle_windows_used", "compounded_gain_pct",
                    "win_rate_pct", "safe"]].to_string(index=False))

    out_path = Path("output") / "v6_filler_scan_results.csv"
    out.to_csv(out_path, index=False)
    print(f"\n-> {out_path}")


if __name__ == "__main__":
    main()
