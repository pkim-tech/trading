"""
v6 idea, joint-capital variant (2026-08-03): the real question is total
compounded capital growth with BOTH legs running on one shared pool, not the
filler's isolated return. Merges the primary's own real trades with the
filler's strictly-non-overlapping trades (entry/exit both inside one of the
primary's idle windows -- zero-overlap only, no forced exits, per the user's
explicit call: giving up a few filler trades is fine, distorting either
node's own backtested exit behavior is not) into one chronological trade
sequence and compounds it as a single capital curve. Reports the multiplier
vs. the primary running alone.

Also tests BOTH role assignments for a pair (X-primary/Y-filler and
Y-primary/X-filler) since there's no a priori reason the "obvious" primary
(the one with more/bigger real trades) produces the better joint outcome --
per the user's own note.

"Primary" role for a ticker uses its real watchlist_id=65 live node params if
it has one (GDXU/NUGT/AGQ/HIBL/SOXL/USD/YANG do); otherwise falls back to its
best v5 backtest_cache node (GDXD/DUST/ZSL/TQQQ/UVIX don't have a live node).
Either way this is just "that ticker's own real trade list" -- the role
label only matters for whose idle windows gate the other's entries.

Usage:
  .venv/bin/python scripts/sim_v6_joint_capital.py --pairs GDXU:GDXD NUGT:DUST AGQ:ZSL
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
from scripts.campaign_comparison_table import best_node, safe_best, best_any, fmt_node

LIVE_DB = Path(__file__).resolve().parent.parent / "cache" / "live" / "trading_live.db"
DB_PATH = "cache/research/trading_universe.db"
STRATEGY = "TrailingExitZScoreBreakout"
FIXED_SLS = [1, 2, 3]
ENTRY_TIMING = "open_check"


def get_watchlist_node(ticker, watchlist_id=65):
    conn = sqlite3.connect(LIVE_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM watch_list WHERE ticker=? AND watchlist_id=?", (ticker, watchlist_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def trades_from_live_node(ticker, node):
    strategy_class = getattr(strategies, node["strategy"])
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(node["strategy"])
    sl_axis_real_col = "trail_sell_pct" if sl_axis_col == "trail_pct" else sl_axis_col
    sl_raw = node[sl_axis_real_col]
    trail_pct_pct = node["trail_sell_pct"] if fourth_axis_col == "trail_pct" else 0.0
    take_profit = node.get("arm_sell_pct") or 0.0

    df_h = load_hourly(ticker)
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    strat = strategy_class(window=node["window"], z_score_threshold=node["z_score_threshold"])
    ind = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_h, ind)

    return run_backtest_dispatch(
        strategy_class, df_h, ind, ticker,
        take_profit=take_profit, sl_raw=sl_raw, max_hours_to_hold=node["max_hold_hours"],
        z_score_threshold=node["z_score_threshold"], fixed_sl=node["fixed_sl"],
        trail_pct_pct=trail_pct_pct, entry_timing=node.get("entry_timing", "close"), prep=p,
    ), node["strategy"], "live-node"


def trades_from_best_v5_node(ticker, con):
    nodes = [best_node(con, "v5", ticker, STRATEGY, fsl, ENTRY_TIMING) for fsl in FIXED_SLS]
    nodes = [n for n in nodes if n]
    node = safe_best(nodes) or best_any(nodes)
    if node is None:
        return None, None, None
    _, fourth_axis_col = strategies.resolve_axis_columns(STRATEGY)
    trail_pct_pct = node["tpct"] if fourth_axis_col == "trail_pct" else 0.0

    df_h = load_hourly(ticker)
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    strat = strategies.TrailingExitZScoreBreakout(window=node["window"], z_score_threshold=node["z"])
    ind = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_h, ind)

    trades = run_backtest_dispatch(
        strategies.TrailingExitZScoreBreakout, df_h, ind, ticker,
        take_profit=node["tp"], sl_raw=node["axis"], max_hours_to_hold=node["hold"],
        z_score_threshold=node["z"], fixed_sl=node["fixed_sl"],
        trail_pct_pct=trail_pct_pct, entry_timing=ENTRY_TIMING, prep=p,
    )
    return trades, f"{STRATEGY} {fmt_node(node)}", ("cliff-safe" if node["safe"] else "best-any")


def get_own_trades(ticker, con):
    node = get_watchlist_node(ticker)
    if node is not None:
        return trades_from_live_node(ticker, node)
    return trades_from_best_v5_node(ticker, con)


def windows_from_trades(trades):
    windows = []
    prev_exit = None
    for t in trades:
        if prev_exit is not None and prev_exit < t["Entry Time"]:
            windows.append({"start": prev_exit, "end": t["Entry Time"]})
        prev_exit = t["Exit Time"]
    return pd.DataFrame(windows)


def compounded(rets):
    c = 1.0
    for r in rets:
        c *= (1 + r)
    return (c - 1) * 100


def joint_run(primary, filler, con):
    p_trades, p_label, p_kind = get_own_trades(primary, con)
    if not p_trades:
        return None
    windows = windows_from_trades(p_trades)
    f_trades, f_label, f_kind = get_own_trades(filler, con)
    if not f_trades:
        return None

    fits = [t for t in f_trades
            for _, w in windows.iterrows()
            if t["Entry Time"] >= w["start"] and t["Exit Time"] <= w["end"]]

    p_rets = [(t["Exit Price"] / t["Entry Price"]) - 1 for t in p_trades]
    f_rets = [(t["Exit Price"] / t["Entry Price"]) - 1 for t in fits]

    merged = sorted(p_trades + fits, key=lambda t: t["Entry Time"])
    merged_rets = [(t["Exit Price"] / t["Entry Price"]) - 1 for t in merged]

    primary_alone = compounded(p_rets)
    joint = compounded(merged_rets)

    return {
        "primary": primary, "filler": filler,
        "primary_label": p_label, "primary_kind": p_kind,
        "filler_label": f_label, "filler_kind": f_kind,
        "primary_trades": len(p_trades), "primary_alone_gain_pct": primary_alone,
        "filler_trades_fit": len(fits), "filler_only_gain_pct": compounded(f_rets),
        "idle_windows": len(windows),
        "joint_gain_pct": joint,
        "multiplier_vs_primary_alone": (1 + joint / 100) / (1 + primary_alone / 100) if primary_alone != -100 else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", nargs="+", default=["GDXU:GDXD", "NUGT:DUST", "AGQ:ZSL"])
    args = ap.parse_args()
    con = sqlite3.connect(DB_PATH)

    rows = []
    for pair in args.pairs:
        a, b = pair.split(":")
        for primary, filler in [(a, b), (b, a)]:
            print(f"\n=== primary={primary} / filler={filler} ===")
            r = joint_run(primary, filler, con)
            if r is None:
                print("  insufficient data, skipping")
                continue
            print(f"  primary trades: {r['primary_trades']} ({r['primary_kind']}) "
                  f"-> alone: {r['primary_alone_gain_pct']:.1f}%")
            print(f"  filler trades fit into {r['idle_windows']} idle windows: "
                  f"{r['filler_trades_fit']} ({r['filler_kind']}) "
                  f"-> filler-only: {r['filler_only_gain_pct']:.1f}%")
            print(f"  JOINT compounded gain: {r['joint_gain_pct']:.1f}%  "
                  f"(multiplier vs primary alone: {r['multiplier_vs_primary_alone']:.2f}x)")
            rows.append(r)

    out = pd.DataFrame(rows)
    print("\n=== Summary ===")
    print(out[["primary", "filler", "primary_trades", "primary_alone_gain_pct",
                "filler_trades_fit", "joint_gain_pct", "multiplier_vs_primary_alone"]]
          .to_string(index=False))
    out_path = Path("output") / "v6_joint_capital_results.csv"
    out.to_csv(out_path, index=False)
    print(f"\n-> {out_path}")


if __name__ == "__main__":
    main()
