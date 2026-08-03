"""
v6 idea, secondary-trade variant (2026-08-03): correction to the earlier
sim_v6_inverse_parking.py, which held the inverse ticker statically for the
entire idle window (and got wrecked by leveraged-ETF decay on the rare
month-plus gaps). The user's actual idea: don't hold the inverse -- trade it
on its OWN real z-score signal rules, same as any other node, and only ever
during the primary's idle windows (so it never competes with the primary for
capital). This only counts an inverse trade as a "secondary trade" if its
entire entry-to-exit span fits inside one idle window -- a trade that would
still be open when the primary re-enters is excluded (would need explicit
overlap-handling, e.g. forced exit, not modeled here).

Uses each inverse ticker's own best cliff-safe (fallback best-any) v5 node
config (TrailingExitZScoreBreakout, entry_timing=open_check, fixed_sl in
{1,2,3} -- the only combo currently swept for GDXD/DUST/ZSL), run through the
same run_backtest_dispatch the sweep engine itself uses, to get a real trade
list -- not just a buy-and-hold return.

Usage:
  .venv/bin/python scripts/sim_v6_inverse_secondary_trade.py
"""
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
PAIRS = [
    ("GDXU", "GDXD"),
    ("NUGT", "DUST"),
    ("AGQ", "ZSL"),
]
STRATEGY = "TrailingExitZScoreBreakout"
FIXED_SLS = [1, 2, 3]
ENTRY_TIMING = "open_check"


def get_inverse_trades(ticker, node):
    strategy_class = getattr(strategies, STRATEGY)
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(STRATEGY)
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
    entry_p, exit_p = t["Entry Price"], t["Exit Price"]
    return (exit_p / entry_p) - 1


def main():
    con = sqlite3.connect(DB_PATH)
    all_rows = []

    for source, inverse in PAIRS:
        print(f"\n=== {source} idle windows -> secondary-trade {inverse}? ===")
        windows = pd.DataFrame(extract_gap_windows(source))
        if windows.empty:
            print(f"  no idle windows for {source}, skipping")
            continue
        print(f"  {len(windows)} real idle windows")

        nodes = [best_node(con, "v5", inverse, STRATEGY, fsl, ENTRY_TIMING) for fsl in FIXED_SLS]
        nodes = [n for n in nodes if n]
        node = safe_best(nodes) or best_any(nodes)
        if node is None:
            print(f"  {inverse}: no v5 node found, skipping")
            continue
        node["fixed_sl"] = node["fixed_sl"]  # already set by best_node
        print(f"  {inverse} node: {fmt_node(node)} (cliff-safe={node['safe']})")

        inverse_trades = get_inverse_trades(inverse, node)
        print(f"  {inverse}'s own full backtest: {len(inverse_trades)} trades")

        secondary = []
        for _, w in windows.iterrows():
            fits = [
                t for t in inverse_trades
                if t["Entry Time"] >= w["start"] and t["Exit Time"] <= w["end"]
            ]
            if fits:
                secondary.extend(fits)

        n_windows_used = len(set(
            (t["Entry Time"], t["Exit Time"]) for t in secondary
        ))
        print(f"  secondary trades fired: {len(secondary)} (inside {len(windows)} idle windows)")

        if not secondary:
            all_rows.append({
                "source": source, "inverse": inverse, "idle_windows": len(windows),
                "secondary_trades": 0, "compounded_return_pct": 0.0,
                "mean_trade_return_pct": None, "win_rate_pct": None,
            })
            continue

        rets = [trade_return(t) for t in secondary]
        compounded = 1.0
        for r in rets:
            compounded *= (1 + r)
        wins = sum(1 for r in rets if r > 0)
        all_rows.append({
            "source": source, "inverse": inverse, "idle_windows": len(windows),
            "secondary_trades": len(secondary),
            "compounded_return_pct": (compounded - 1) * 100,
            "mean_trade_return_pct": (sum(rets) / len(rets)) * 100,
            "win_rate_pct": wins / len(rets) * 100,
        })

    out = pd.DataFrame(all_rows)
    print("\n=== Summary ===")
    print(out.to_string(index=False))
    out_path = Path("output") / "v6_inverse_secondary_trade_results.csv"
    out.to_csv(out_path, index=False)
    print(f"\n-> {out_path}")


if __name__ == "__main__":
    main()
