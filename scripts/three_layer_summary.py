"""
Three-layer (core v5 strategy + drought-overlay/trough + arm-triggered add-on) total-return
summary across the full 18-ticker v4 watchlist (watchlist_id=57) plus the "big 6" broad-index
comparison group (SPY/SSO/UPRO/QQQ/QLD/TQQQ) -- built 2026-08-06 directly from the AGQ deep
dive earlier this session (see docs/research_log.md's 2026-08-06 entries).

Per user's explicit call: total (raw) return is the headline number everywhere; alpha_vs_spy
is used ONLY to select/screen which v5-swept node represents each ticker (same cliff-safe-
else-best-any convention as scripts/drought_overlay_sweep.py's load_big6_nodes/_best_cell),
never reported as the primary figure.

Real gap found while building this (2026-08-06): several tickers' LIVE watch_list node runs
TrailingBothZScoreBreakout, but that strategy was NEVER actually resWept under the v5
corrected kernel for those tickers -- only TrailingExitZScoreBreakout has v5 data (confirmed
for GDXU/HIBL/SOXL/USD). Rather than reporting N/A for those, this script selects the best
AVAILABLE v5-swept node per ticker (across both strategies x fixed_sl in {1,2,3}), matching
campaign_comparison_table.py's own convention -- so the reported node may differ from what's
actually running live for these 4 tickers. Flagged per-row (live_strategy_mismatch column).

Usage: .venv/bin/python scripts/three_layer_summary.py [--csv]
"""
import argparse
import sqlite3
import sys
from itertools import product
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.drought_detection_test import load_nodes
from scripts.drought_overlay_test import get_trades_and_bars, find_drought_windows, simulate_overlay

UNIVERSE_DB = Path(__file__).resolve().parent.parent / "cache" / "research" / "trading_universe.db"
LIVE_DB = Path(__file__).resolve().parent.parent / "cache" / "live" / "trading_live.db"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

V4_TICKERS = ['AGQ', 'DPST', 'DUST', 'GDXD', 'GDXU', 'HIBL', 'KORU', 'LABU', 'NAIL',
              'NUGT', 'RETL', 'SOXL', 'TQQQ', 'UDOW', 'USD', 'UVIX', 'YANG', 'ZSL']
BIG6_TICKERS = ["SPY", "SSO", "UPRO", "QQQ", "QLD", "TQQQ"]
STRATEGIES = ["TrailingBothZScoreBreakout", "TrailingExitZScoreBreakout"]
FIXED_SLS = [1, 2, 3]
ENTRY_TIMING = "open_check"
CLIFF_RADIUS = 2

ROBUST_ALPHA_SQL = ("MIN(alpha_vs_spy, COALESCE(alpha_vs_spy_pessimistic, alpha_vs_spy), "
                     "COALESCE(alpha_vs_spy_certain, alpha_vs_spy))")
ROBUST_RETURN_SQL = ("MIN(strategy_return, COALESCE(strategy_return_pessimistic, strategy_return), "
                      "COALESCE(strategy_return_certain, strategy_return))")

SL_GRID = [1, 2, 3, 5, 7, 10, 15, 18]
ARM_GRID = [5, 10, 15, 20, 30, 40]
TRAIL_GRID = [1, 2, 3, 5, 7, 10, 15]
CONFIRM_DAYS_GRID = [5, 10, 15, 20]


def get_live_strategy(ticker):
    try:
        conn = sqlite3.connect(LIVE_DB)
        row = conn.execute("SELECT strategy FROM watch_list WHERE ticker=? AND watchlist_id IN (57,65) "
                            "ORDER BY watchlist_id DESC LIMIT 1", (ticker,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def best_v5_node(conn, ticker):
    """Best cliff-safe-else-best-any v5/open_check node across both strategies x
    fixed_sl in {1,2,3}, screened by robust ALPHA but reporting robust TOTAL RETURN."""
    arm_cols = {"TrailingExitZScoreBreakout": "take_profit", "TrailingBothZScoreBreakout": "arm_sell_pct"}
    candidates = []
    for strategy in STRATEGIES:
        arm_col = arm_cols[strategy]
        for fixed_sl in FIXED_SLS:
            row = conn.execute(f"""
                SELECT window, z_score_threshold, {arm_col}, trail_buy_pct, trail_sell_pct,
                       max_hold_hours, {ROBUST_ALPHA_SQL} AS ralpha, {ROBUST_RETURN_SQL} AS rreturn,
                       trades, win_rate
                FROM backtest_cache
                WHERE version='v5' AND ticker=? AND strategy=? AND entry_timing=? AND stop_loss=? AND trades>0
                ORDER BY ralpha DESC LIMIT 1
            """, (ticker, strategy, ENTRY_TIMING, fixed_sl)).fetchone()
            if not row:
                continue
            (window, z, arm, trail_buy_pct, trail_sell_pct, max_hold_hours,
             ralpha, rreturn, trades, win_rate) = row
            r = CLIFF_RADIUS
            # trail_buy_pct MUST be held fixed here, not left unfiltered -- a TrailingBoth
            # neighbor search without this compares against wildly different trail_buy_pct
            # configs instead of real neighbors of the candidate's own value (the same bug
            # found+fixed 2026-08-07 in an ad hoc query and in top_safe_nodes.py; this
            # function had it too and went unnoticed through several real lookups the same
            # session, including a false "not safe" verdict on UCO -- see
            # docs/cliff_safety_query_checklist.md).
            worst = conn.execute(f"""
                SELECT MIN({ROBUST_ALPHA_SQL}) FROM backtest_cache
                WHERE version='v5' AND ticker=? AND strategy=? AND entry_timing=? AND stop_loss=?
                  AND window=? AND z_score_threshold=? AND trail_buy_pct IS ?
                  AND {arm_col} BETWEEN ? AND ? AND trail_sell_pct BETWEEN ? AND ?
                  AND max_hold_hours BETWEEN ? AND ? AND trades>0
            """, (ticker, strategy, ENTRY_TIMING, fixed_sl, window, z, trail_buy_pct,
                  arm - r, arm + r, trail_sell_pct - r, trail_sell_pct + r,
                  max_hold_hours - 7, max_hold_hours + 7)).fetchone()[0]
            # An empty neighbor set (MIN() over zero rows) is SQL NULL/Python None --
            # this must NOT read as "safe" (0.0 >= 0 was true before this fix). The
            # whole point of a cliff-safety check is that a wrong "safe" verdict is
            # expensive (docs/cliff_safety_query_checklist.md) -- no-neighbor-coverage
            # is exactly the case where the check has said nothing at all, not the case
            # where it's vouched for the config. Kept explicit rather than relying on
            # this query never actually returning an empty set today.
            worst_neighbor = float(worst) if worst is not None else float("nan")
            candidates.append({
                "ticker": ticker, "strategy": strategy, "window": window, "z": z,
                "arm_pct": arm, "fixed_sl": fixed_sl, "trail_buy_pct": trail_buy_pct,
                "trail_sell_pct": trail_sell_pct, "max_hold_hours": max_hold_hours,
                "entry_timing": ENTRY_TIMING, "ralpha": ralpha, "rreturn": rreturn,
                "worst_neighbor": worst_neighbor, "safe": worst is not None and worst_neighbor >= 0,
                "trades": trades, "win_rate": win_rate,
            })
    if not candidates:
        return None
    safe = [c for c in candidates if c["safe"]]
    return max(safe, key=lambda c: c["ralpha"]) if safe else max(candidates, key=lambda c: c["ralpha"])


def cliff_safety(cells, key):
    cd, sl, arm, trail = key
    grids = {0: CONFIRM_DAYS_GRID, 1: SL_GRID, 2: ARM_GRID, 3: TRAIL_GRID}
    neighbor_comps = []
    for axis, grid in grids.items():
        idx = grid.index(key[axis])
        for step in (-1, 1):
            n_idx = idx + step
            if not (0 <= n_idx < len(grid)):
                continue
            n_key = list(key); n_key[axis] = grid[n_idx]; n_key = tuple(n_key)
            if n_key in cells and cells[n_key]:
                comp = np.prod([1 + r for r in cells[n_key]]) - 1
                neighbor_comps.append(comp)
    return (min(neighbor_comps), len(neighbor_comps)) if neighbor_comps else (float("nan"), 0)


def overlay_layer(node):
    try:
        trades, df_h = get_trades_and_bars(node)
    except Exception:
        return None
    cells = {}
    for confirm_days in CONFIRM_DAYS_GRID:
        windows = find_drought_windows(trades, df_h, confirm_days)
        if not windows:
            continue
        for sl, arm, trail in product(SL_GRID, ARM_GRID, TRAIL_GRID):
            rets = [simulate_overlay(df_h, entry_i, gap_end, sl, arm, trail)["ret"]
                    for entry_i, gap_end in windows]
            cells[(confirm_days, sl, arm, trail)] = rets
    if not cells:
        return None, trades, df_h
    scored = []
    for key, rets in cells.items():
        comp = np.prod([1 + r for r in rets]) - 1
        worst, _ = cliff_safety(cells, key)
        scored.append((key, len(rets), comp, worst))
    safe = [s for s in scored if not np.isnan(s[3]) and s[3] > 0]
    best = max(safe, key=lambda s: s[2]) if safe else max(scored, key=lambda s: s[2])
    return {"n": best[1], "compounded_pct": best[2] * 100, "safe": bool(safe)}, trades, df_h


def addon_layer(trades, df_h):
    armed = [t for t in trades if t["arm_i"] is not None]
    if not armed:
        return None
    closes = df_h["Close"].values
    added_rets = np.array([(t["exit_p"] / closes[t["arm_i"]] - 1) for t in armed])
    comp = (np.prod(1 + added_rets) - 1) * 100
    return {"n": len(armed), "win_rate": float(np.mean(added_rets > 0) * 100), "compounded_pct": comp}


def load_big6_v5_nodes(conn):
    """Big-6 nodes shaped like load_nodes() output for overlay_layer()/get_trades_and_bars()."""
    nodes = []
    for ticker in BIG6_TICKERS:
        best = best_v5_node(conn, ticker)
        if best is None:
            continue
        nodes.append({
            "ticker": ticker, "strategy": best["strategy"], "window": best["window"], "z": best["z"],
            "arm_pct": best["arm_pct"], "fixed_sl": best["fixed_sl"], "trail_buy_pct": best["trail_buy_pct"] or 0.0,
            "trail_sell_pct": best["trail_sell_pct"], "max_hold_hours": best["max_hold_hours"],
            "entry_timing": ENTRY_TIMING,
        })
    return nodes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(UNIVERSE_DB)
    all_tickers = V4_TICKERS + BIG6_TICKERS

    liq = dict(conn.execute(
        f"SELECT symbol, avg_vol_10d*last_price*0.01 FROM tickers WHERE symbol IN "
        f"({','.join('?' * len(all_tickers))})", all_tickers))

    rows = []
    for ticker in all_tickers:
        best = best_v5_node(conn, ticker)
        if best is None:
            print(f"{ticker}: no v5/open_check backtest_cache data, skipped")
            continue
        node = {
            "ticker": ticker, "strategy": best["strategy"], "window": best["window"], "z": best["z"],
            "arm_pct": best["arm_pct"], "fixed_sl": best["fixed_sl"], "trail_buy_pct": best["trail_buy_pct"] or 0.0,
            "trail_sell_pct": best["trail_sell_pct"], "max_hold_hours": best["max_hold_hours"],
            "entry_timing": ENTRY_TIMING,
        }
        ov_result = overlay_layer(node)
        if ov_result is None:
            print(f"{ticker}: no hourly cache / drought windows, skipped")
            continue
        overlay, trades, df_h = ov_result
        addon = addon_layer(trades, df_h) if overlay else None

        live_strategy = get_live_strategy(ticker)
        mismatch = live_strategy is not None and live_strategy != best["strategy"]

        rows.append({
            "ticker": ticker, "strategy": best["strategy"], "sl": best["fixed_sl"],
            "core_trades": best["trades"], "core_total_return_pct": best["rreturn"],
            "core_safe": best["safe"], "liq_1pct_dollars": liq.get(ticker),
            "overlay_n": overlay["n"] if overlay else 0,
            "overlay_pct": overlay["compounded_pct"] if overlay else float("nan"),
            "overlay_safe": overlay["safe"] if overlay else False,
            "addon_n": addon["n"] if addon else 0,
            "addon_win_pct": addon["win_rate"] if addon else float("nan"),
            "addon_pct": addon["compounded_pct"] if addon else float("nan"),
            "live_strategy_mismatch": mismatch,
        })
        print(f"{ticker}: done")

    import pandas as pd
    df = pd.DataFrame(rows).sort_values("core_total_return_pct", ascending=False)
    pd.set_option("display.width", 240)
    print("\n" + df.round(1).to_string(index=False))

    if args.csv:
        OUTPUT_DIR.mkdir(exist_ok=True)
        df.to_csv(OUTPUT_DIR / "three_layer_summary.csv", index=False)
        print(f"\nWrote {OUTPUT_DIR / 'three_layer_summary.csv'}")


if __name__ == "__main__":
    main()
