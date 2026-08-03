"""
v6 idea, overlap-timing check (2026-08-03): before optimizing anything, test
the user's core assumption directly -- does a primary v5 node's real open-
position window ever coincide with its matched inverse's own real open-
position window (using the inverse's own best available v5 node)? The
user's hypothesis is that a buy signal on one side implies price is far from
the inverse's own trigger, so they shouldn't fire at the same time -- this
script measures how often that's actually true, not just assumed.

For each pair, pulls both tickers' own real trade lists (entry/exit
intervals) via run_backtest_dispatch against their respective best node
configs (primary: real watchlist_id=65 node; inverse: best cliff-safe/
best-any v5 node, same source as sim_v6_inverse_secondary_trade.py), then
reports what fraction of each side's trades overlap in time with an open
position on the other side.

Usage:
  .venv/bin/python scripts/sim_v6_inverse_overlap_check.py
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

import strategies
from backtester import prep_inputs, run_backtest_dispatch
from scripts.export_trades import load_hourly
from scripts.campaign_comparison_table import best_node, safe_best, best_any, fmt_node

DB_PATH = "cache/research/trading_universe.db"
LIVE_DB = Path(__file__).resolve().parent.parent / "cache" / "live" / "trading_live.db"

PAIRS = [
    ("GDXU", "GDXD"),
    ("NUGT", "DUST"),
    ("AGQ", "ZSL"),
]
STRATEGY = "TrailingExitZScoreBreakout"
FIXED_SLS = [1, 2, 3]
ENTRY_TIMING = "open_check"


def get_primary_node(ticker, watchlist_id=65):
    conn = sqlite3.connect(LIVE_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM watch_list WHERE ticker=? AND watchlist_id=?", (ticker, watchlist_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def run_trades_from_node(ticker, node, strategy_name, entry_timing):
    strategy_class = getattr(strategies, strategy_name)
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    sl_axis_real_col = "trail_sell_pct" if sl_axis_col == "trail_pct" else sl_axis_col

    df_h = load_hourly(ticker)
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])

    if "fixed_sl" in node and "axis" in node:  # best_node()-shaped (inverse side)
        window, z = node["window"], node["z"]
        strat = strategy_class(window=window, z_score_threshold=z)
        ind = strat.generate_daily_indicators(df_daily)
        p = prep_inputs(df_h, ind)
        trail_pct_pct = node["tpct"] if fourth_axis_col == "trail_pct" else 0.0
        return run_backtest_dispatch(
            strategy_class, df_h, ind, ticker,
            take_profit=node["tp"], sl_raw=node["axis"], max_hours_to_hold=node["hold"],
            z_score_threshold=z, fixed_sl=node["fixed_sl"],
            trail_pct_pct=trail_pct_pct, entry_timing=entry_timing, prep=p,
        )
    else:  # watch_list row shape (primary side)
        sl_raw = node[sl_axis_real_col]
        trail_pct_pct = node["trail_sell_pct"] if fourth_axis_col == "trail_pct" else 0.0
        take_profit = node.get("arm_sell_pct") or 0.0
        strat = strategy_class(window=node["window"], z_score_threshold=node["z_score_threshold"])
        ind = strat.generate_daily_indicators(df_daily)
        p = prep_inputs(df_h, ind)
        return run_backtest_dispatch(
            strategy_class, df_h, ind, ticker,
            take_profit=take_profit, sl_raw=sl_raw, max_hours_to_hold=node["max_hold_hours"],
            z_score_threshold=node["z_score_threshold"], fixed_sl=node["fixed_sl"],
            trail_pct_pct=trail_pct_pct, entry_timing=node.get("entry_timing", "close"), prep=p,
        )


def intervals_overlap(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def overlap_stats(trades_a, trades_b):
    """For each trade in A, does it overlap ANY trade in B?"""
    n_overlap = 0
    for ta in trades_a:
        for tb in trades_b:
            if intervals_overlap(ta["Entry Time"], ta["Exit Time"], tb["Entry Time"], tb["Exit Time"]):
                n_overlap += 1
                break
    return n_overlap, len(trades_a)


def main():
    con = sqlite3.connect(DB_PATH)
    rows = []

    for primary, inverse in PAIRS:
        print(f"\n=== {primary} <-> {inverse} ===")
        p_node = get_primary_node(primary)
        if p_node is None:
            print(f"  no watchlist_id=65 node for {primary}, skipping")
            continue
        primary_strategy = p_node["strategy"]
        primary_trades = run_trades_from_node(primary, p_node, primary_strategy, p_node.get("entry_timing", "close"))
        print(f"  {primary} ({primary_strategy}): {len(primary_trades)} real trades")

        nodes = [best_node(con, "v5", inverse, STRATEGY, fsl, ENTRY_TIMING) for fsl in FIXED_SLS]
        nodes = [n for n in nodes if n]
        i_node = safe_best(nodes) or best_any(nodes)
        if i_node is None:
            print(f"  {inverse}: no v5 node, skipping")
            continue
        print(f"  {inverse} node: {fmt_node(i_node)} (cliff-safe={i_node['safe']})")
        inverse_trades = run_trades_from_node(inverse, i_node, STRATEGY, ENTRY_TIMING)
        print(f"  {inverse} ({STRATEGY}): {len(inverse_trades)} real trades")

        p_overlap, p_total = overlap_stats(primary_trades, inverse_trades)
        i_overlap, i_total = overlap_stats(inverse_trades, primary_trades)

        print(f"  {primary} trades that overlap an open {inverse} position: {p_overlap}/{p_total} "
              f"({p_overlap/p_total*100:.1f}%)" if p_total else "  no primary trades")
        print(f"  {inverse} trades that overlap an open {primary} position: {i_overlap}/{i_total} "
              f"({i_overlap/i_total*100:.1f}%)" if i_total else "  no inverse trades")

        rows.append({
            "primary": primary, "inverse": inverse,
            "primary_trades": p_total, "primary_overlap_pct": p_overlap / p_total * 100 if p_total else None,
            "inverse_trades": i_total, "inverse_overlap_pct": i_overlap / i_total * 100 if i_total else None,
        })

    out = pd.DataFrame(rows)
    print("\n=== Summary ===")
    print(out.to_string(index=False))
    out_path = Path("output") / "v6_inverse_overlap_results.csv"
    out.to_csv(out_path, index=False)
    print(f"\n-> {out_path}")
    print("\nNote: inverse trades use its own best (often non-cliff-safe) v5 node -- this measures "
          "TIMING overlap only, not whether the inverse's trades are individually profitable "
          "(see sim_v6_inverse_secondary_trade.py for that, and its caveat about weak inverse nodes).")


if __name__ == "__main__":
    main()
