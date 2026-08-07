"""How sensitive are drought/add-on overlay results to EACH real axis of a
candidate's winning node, independently -- not just the one trail_buy_pct
spot-check done in conversation 2026-08-07 (docs/research_log.md's entry).

Follows docs/cliff_safety_query_checklist.md explicitly: for each axis
checked, every OTHER real column is held fixed via an exact-equality filter
(including entry_timing and the redundant stop_loss/fixed_sl pair, which are
always numerically equal but stored as separate columns) -- only the one
axis under test is allowed to vary, one grid step at a time, using the real
neighboring values that exist in backtest_cache for this exact ticker/
strategy/config (not an assumed step size).

For each axis x each real neighbor step, computes drought-window Jaccard
overlap (entry-time set vs the winner's) and add-on trade-count/return
stability, so a genuinely fragile axis (like trail_buy_pct, already found)
can be told apart from a robust one.

Usage:
  .venv/bin/python scripts/candidate_node_sensitivity.py TICKER [--version v5]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.locate_best_node import DB_PATH
from scripts.drought_overlay_test import get_trades_and_bars, find_drought_windows
from scripts.stacked_model.add_on import generate_addon_trades

ROBUST_ALPHA_SQL = ("MIN(alpha_vs_spy, COALESCE(alpha_vs_spy_pessimistic, alpha_vs_spy), "
                     "COALESCE(alpha_vs_spy_certain, alpha_vs_spy))")

# Every real backtest_cache column that describes a node's config, per the
# cliff-safety checklist's item 1 -- written out explicitly, not recalled
# from memory. TP_COL is resolved per-strategy (arm_sell_pct for TrailingBoth,
# take_profit for TrailingExit -- the other stays NULL and isn't a real axis
# for that strategy, so it's excluded from the varied-axis list but still
# held fixed via IS in the WHERE clause).
FIXED_COLS = ["window", "z_score_threshold", "fixed_sl", "stop_loss", "entry_timing",
              "max_hold_hours", "trail_buy_pct", "trail_sell_pct", "arm_sell_pct", "take_profit"]


def get_winner(conn, ticker, version, strategy):
    row = conn.execute(f"""
        SELECT window, z_score_threshold, fixed_sl, stop_loss, entry_timing, max_hold_hours,
               trail_buy_pct, trail_sell_pct, arm_sell_pct, take_profit,
               {ROBUST_ALPHA_SQL} AS robust_alpha, trades
        FROM backtest_cache
        WHERE ticker=? AND version=? AND strategy=? AND trades>0
        ORDER BY robust_alpha DESC LIMIT 1
    """, (ticker, version, strategy)).fetchone()
    if row is None:
        return None
    cols = FIXED_COLS + ["robust_alpha", "trades"]
    return dict(zip(cols, row))


def make_node(ticker, strategy, cfg):
    node = dict(ticker=ticker, strategy=strategy, window=cfg["window"], z=cfg["z_score_threshold"],
                fixed_sl=cfg["fixed_sl"], entry_timing=cfg["entry_timing"],
                max_hold_hours=cfg["max_hold_hours"], trail_buy_pct=cfg["trail_buy_pct"],
                trail_sell_pct=cfg["trail_sell_pct"], arm_sell_pct=cfg["arm_sell_pct"],
                take_profit=cfg["take_profit"])
    node["arm_pct"] = cfg["arm_sell_pct"] if strategy == "TrailingBothZScoreBreakout" else cfg["take_profit"]
    return node


def where_fixed(conn_params, cfg, exclude_axis):
    """Builds (clause, params) holding every FIXED_COLS entry except exclude_axis
    at cfg's exact value -- IS instead of = so a NULL-valued column (the
    strategy's inapplicable TP/arm column) still matches correctly."""
    clauses, params = [], []
    for col in FIXED_COLS:
        if col in (exclude_axis, "stop_loss" if exclude_axis == "fixed_sl" else None):
            continue
        if col == "fixed_sl" and exclude_axis == "stop_loss":
            continue
        val = cfg[col]
        if val is None:
            clauses.append(f"{col} IS NULL")
        else:
            clauses.append(f"{col} = ?")
            params.append(val)
    return " AND ".join(clauses), params


def neighbor_values(conn, ticker, version, strategy, cfg, axis):
    clause, params = where_fixed(conn, cfg, axis)
    rows = conn.execute(f"""
        SELECT DISTINCT {axis} FROM backtest_cache
        WHERE ticker=? AND version=? AND strategy=? AND trades>0 AND {clause}
    """, [ticker, version, strategy] + params).fetchall()
    vals = sorted(set(r[0] for r in rows if r[0] is not None))
    if cfg[axis] not in vals:
        return []
    idx = vals.index(cfg[axis])
    neighbors = []
    if idx > 0:
        neighbors.append(vals[idx - 1])
    if idx < len(vals) - 1:
        neighbors.append(vals[idx + 1])
    return neighbors


def fetch_exact(conn, ticker, version, strategy, cfg, axis, new_val):
    clause, params = where_fixed(conn, cfg, axis)
    row = conn.execute(f"""
        SELECT window, z_score_threshold, fixed_sl, stop_loss, entry_timing, max_hold_hours,
               trail_buy_pct, trail_sell_pct, arm_sell_pct, take_profit
        FROM backtest_cache
        WHERE ticker=? AND version=? AND strategy=? AND trades>0 AND {clause} AND {axis} = ?
        LIMIT 1
    """, [ticker, version, strategy] + params + [new_val]).fetchone()
    if row is None:
        return None
    return dict(zip(FIXED_COLS, row))


def overlap_stats(ticker, node_a, node_b, confirm_days=10):
    trades_a, df_a = get_trades_and_bars(node_a)
    trades_b, df_b = get_trades_and_bars(node_b)
    wa = find_drought_windows(trades_a, df_a, confirm_days)
    wb = find_drought_windows(trades_b, df_b, confirm_days)
    ea = set(str(df_a.index[e + 1]) for e, g in wa)
    eb = set(str(df_b.index[e + 1]) for e, g in wb)
    jaccard = len(ea & eb) / len(ea | eb) if (ea or eb) else float("nan")

    addon_a = generate_addon_trades(trades_a, df_a)
    addon_b = generate_addon_trades(trades_b, df_b)
    return {
        "core_trades_a": len(trades_a), "core_trades_b": len(trades_b),
        "drought_a": len(wa), "drought_b": len(wb), "drought_jaccard": jaccard,
        "addon_a": len(addon_a), "addon_b": len(addon_b),
    }


def run_ticker(conn, ticker, version):
    strategies = [r[0] for r in conn.execute(
        "SELECT DISTINCT strategy FROM backtest_cache WHERE ticker=? AND version=? AND trades>0",
        (ticker, version)).fetchall()]

    for strategy in strategies:
        winner = get_winner(conn, ticker, version, strategy)
        if winner is None:
            continue
        winner_node = make_node(ticker, strategy, winner)
        try:
            winner_trades, winner_df = get_trades_and_bars(winner_node)
        except Exception as e:
            print(f"{ticker}/{strategy}: winner failed ({e}), skipping")
            continue
        if len(winner_trades) < 2:
            print(f"{ticker}/{strategy}: too few winner trades, skipping")
            continue

        print(f"\n=== {ticker} / {strategy} — winner: w{winner['window']} z{winner['z_score_threshold']} "
              f"sl{winner['fixed_sl']} hold{winner['max_hold_hours']} "
              f"tb{winner['trail_buy_pct']} ts{winner['trail_sell_pct']} "
              f"arm{winner['arm_sell_pct']} tp{winner['take_profit']} "
              f"alpha={winner['robust_alpha']:.1f}% trades={winner['trades']} ===")

        varied_axes = ["window", "z_score_threshold", "fixed_sl", "max_hold_hours", "trail_sell_pct"]
        if strategy == "TrailingBothZScoreBreakout":
            varied_axes.append("trail_buy_pct")
            tp_axis = "arm_sell_pct"
        else:
            tp_axis = "take_profit"
        varied_axes.append(tp_axis)

        for axis in varied_axes:
            neighbors = neighbor_values(conn, ticker, version, strategy, winner, axis)
            for nv in neighbors:
                ncfg = fetch_exact(conn, ticker, version, strategy, winner, axis, nv)
                if ncfg is None:
                    continue
                neighbor_node = make_node(ticker, strategy, ncfg)
                try:
                    stats = overlap_stats(ticker, winner_node, neighbor_node)
                except Exception as e:
                    print(f"  {axis}={nv}: failed ({e})")
                    continue
                print(f"  {axis}: {winner[axis]} -> {nv}  |  "
                      f"core_trades {stats['core_trades_a']}->{stats['core_trades_b']}  "
                      f"drought_windows {stats['drought_a']}->{stats['drought_b']} "
                      f"(jaccard={stats['drought_jaccard']:.2f})  "
                      f"addon_trades {stats['addon_a']}->{stats['addon_b']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--version", default="v5")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    for t in args.tickers:
        run_ticker(conn, t, args.version)


if __name__ == "__main__":
    main()
