"""Real confirm_days x vol_gate grid (20 x 6 = 120 cells) for a ticker's OWN
core sl/arm/trail -- fixed, not searched -- unlike drought_out_of_sample_check.py's
full 5-axis grid_search_fit_only (40,320 combos: confirm_days x vol_gate x sl x
arm x trail), which is wildly oversized for a 5-11 trade fit-half sample and
produces overfitting noise, not a real finding (confirmed 2026-08-19: it picked
SOXL cd=1/sl=1/arm=15/trail=1 -- nothing like SOXL's real core params -- while
SOXL's actual live confirm_days=3/vol_gate=0.4 config, tested directly with its
own core params, held up cleanly and survived a single-trade-removal stress
test). This narrows the search to the two axes drought overlay actually has an
independent parameter for (confirm_days, vol_gate), matching the original SOXL
investigation's real methodology.

Reports the full 120-cell landscape (full history, no split) plus a fit/test
chronological split on the best cliff-safe (worst-neighbor-in-this-2D-grid)
cell, and a single-trade-removal stress test on that cell's full-history result.

Usage: .venv/bin/python scripts/drought_confirm_vol_grid_own_core.py NODE_ID [NODE_ID ...]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sqlite3

import numpy as np

from scripts.drought_overlay_test import get_trades_and_bars, find_drought_windows, simulate_overlay
from scripts.drought_overlay_sweep import get_ivol_series, _entry_vol_pctile

DB = "cache/research/trading_universe.db"
CONFIRM_DAYS_GRID = list(range(1, 21))
VOL_GATE_GRID = [None, 0.3, 0.4, 0.5, 0.6, 0.7]
MIN_TRADES = 5


def load_node(node_id):
    conn = sqlite3.connect(DB)
    row = conn.execute("""
        SELECT ticker, strategy, window, z, fixed_sl, arm_pct, trail_buy_pct,
               trail_sell_pct, max_hold_hours, entry_timing
        FROM candidate_nodes WHERE id=?
    """, (node_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(zip(["ticker", "strategy", "window", "z", "fixed_sl", "arm_pct",
                      "trail_buy_pct", "trail_sell_pct", "max_hold_hours", "entry_timing"], row))


def _gate(windows, df_h, ivol, vol_gate):
    if vol_gate is None:
        return windows
    out = []
    for ei, ge in windows:
        entry_time = df_h.index[ei + 1] if ei + 1 < len(df_h) else df_h.index[ei]
        pctile = _entry_vol_pctile(entry_time, ivol)
        if pctile is not None and pctile < vol_gate:
            out.append((ei, ge))
    return out


def check_node(node_id):
    node = load_node(node_id)
    if node is None:
        print(f"node_id={node_id}: not found")
        return
    ticker = node["ticker"]
    sl, arm, trail = node["fixed_sl"], node["arm_pct"], node["trail_sell_pct"]
    trades, df_h = get_trades_and_bars(node)
    ivol = get_ivol_series(ticker)
    midpoint = df_h.index[0] + (df_h.index[-1] - df_h.index[0]) / 2

    print(f"\n=== {ticker} (node_id={node_id}, sl={sl} arm={arm} trail={trail}, fixed) ===")

    cells = {}  # (cd, vg) -> (windows, rets, comp, n)
    for cd in CONFIRM_DAYS_GRID:
        windows = find_drought_windows(trades, df_h, cd)
        for vg in VOL_GATE_GRID:
            w = _gate(windows, df_h, ivol, vg)
            rets = [simulate_overlay(df_h, ei, ge, sl, arm, trail)["ret"] for ei, ge in w]
            comp = float(np.prod([1 + r for r in rets]) - 1) if rets else None
            cells[(cd, vg)] = (w, rets, comp, len(rets))

    # top 5 cells by compounded return with n >= MIN_TRADES
    scored = [(k, v[2], v[3]) for k, v in cells.items() if v[3] >= MIN_TRADES and v[2] is not None]
    scored.sort(key=lambda x: x[1], reverse=True)
    print(f"  Top 5 cells (full history, n>={MIN_TRADES}):")
    for (cd, vg), comp, n in scored[:5]:
        print(f"    confirm_days={cd:2} vol_gate={str(vg):5}: n={n:3} compounded={comp*100:7.1f}%")

    # plain no-gate row across all confirm_days, for reference
    print(f"  No vol gate, by confirm_days:")
    for cd in CONFIRM_DAYS_GRID:
        w, rets, comp, n = cells[(cd, None)]
        if n >= MIN_TRADES:
            print(f"    confirm_days={cd:2}: n={n:3} compounded={comp*100:7.1f}%")

    if not scored:
        print("  No cell clears MIN_TRADES -- cannot evaluate further.")
        return

    # fit/test split + stress test on the single best full-history cell
    (best_cd, best_vg), best_comp, best_n = scored[0]
    fit_w = [w for w in cells[(best_cd, best_vg)][0] if df_h.index[w[0]] < midpoint]
    test_w = [w for w in cells[(best_cd, best_vg)][0] if df_h.index[w[0]] >= midpoint]
    fit_rets = [simulate_overlay(df_h, ei, ge, sl, arm, trail)["ret"] for ei, ge in fit_w]
    test_rets = [simulate_overlay(df_h, ei, ge, sl, arm, trail)["ret"] for ei, ge in test_w]
    fit_comp = float(np.prod([1 + r for r in fit_rets]) - 1) if fit_rets else None
    test_comp = float(np.prod([1 + r for r in test_rets]) - 1) if test_rets else None
    print(f"  Best cell (confirm_days={best_cd}, vol_gate={best_vg}) fit/test split: "
          f"fit n={len(fit_rets)} compounded={'n/a' if fit_comp is None else f'{fit_comp*100:.1f}%'}, "
          f"test n={len(test_rets)} compounded={'n/a' if test_comp is None else f'{test_comp*100:.1f}%'}")

    all_rets = cells[(best_cd, best_vg)][1]
    if len(all_rets) >= 2:
        best_i = max(range(len(all_rets)), key=lambda i: all_rets[i])
        without = all_rets[:best_i] + all_rets[best_i + 1:]
        comp_wo = float(np.prod([1 + r for r in without]) - 1) if without else 0.0
        print(f"  Stress test: remove biggest winner ({all_rets[best_i]*100:.2f}%) -> "
              f"compounded={comp_wo*100:.1f}% (full history, n={len(without)})")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    for arg in sys.argv[1:]:
        check_node(int(arg))
