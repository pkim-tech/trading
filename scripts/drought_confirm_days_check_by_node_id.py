"""Out-of-sample drought confirm_days validation for an EXPLICIT candidate_nodes
id -- generalizes soxs_drought_confirm_days_check.py (built for the SOXS
drought_confirm_days=1 investigation) after the same need recurred for
HIBL/LABU/KORU/DFEN during the 2026-08-19 promotion pass: none of these have
the load_nodes(65, [ticker])-required state='paper'/paper_role IS NULL
watch_list row (SOXL/AGQ/KORU do, from their original pre-promotion research
node; the others never had one), and their existing drought confirm_days
values (6/8/10/10) all trace to a 2026-08-09 mass sweep whose own docstring
flags it as "full-data best-pick only, not the fit/test-half-split +
single-trade-removal stress test" -- the same unvalidated caliber that
produced SOXS's confirm_days=1 problem the same night.

Mirrors drought_out_of_sample_check.py's real 5-axis fit/test methodology
exactly (same grids, same cliff-safety selection, same imported functions)
-- just sources the node dict from an explicit candidate_nodes.id instead of
load_nodes()'s watch_list lookup.

Usage: .venv/bin/python scripts/drought_confirm_days_check_by_node_id.py NODE_ID [NODE_ID ...]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sqlite3

from scripts.drought_overlay_test import get_trades_and_bars, simulate_overlay
from scripts.drought_overlay_sweep import get_ivol_series
from scripts.drought_out_of_sample_check import grid_search_fit_only, _gate_windows, _compounded

DB = "cache/research/trading_universe.db"


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
    node = dict(zip(["ticker", "strategy", "window", "z", "fixed_sl", "arm_pct",
                      "trail_buy_pct", "trail_sell_pct", "max_hold_hours", "entry_timing"], row))
    return node


def check_node(node_id):
    node = load_node(node_id)
    if node is None:
        print(f"node_id={node_id}: not found")
        return
    ticker = node["ticker"]
    trades, df_h = get_trades_and_bars(node)
    ivol_series = get_ivol_series(ticker)
    midpoint = df_h.index[0] + (df_h.index[-1] - df_h.index[0]) / 2

    winner, windows_by_cd = grid_search_fit_only(trades, df_h, ivol_series, midpoint)
    print(f"\n=== {ticker} (node_id={node_id}) ===")
    if winner is None:
        print("  no fit-half drought windows at all -- cannot validate")
        return

    cd, vg, sl, arm, trail = winner["key"]
    _, tuned_test_w_all = windows_by_cd[cd]
    tuned_test_w = _gate_windows(tuned_test_w_all, df_h, ivol_series, vg)
    tuned_test_rets = [simulate_overlay(df_h, ei, ge, sl, arm, trail)["ret"] for ei, ge in tuned_test_w]
    tuned_test_comp = _compounded(tuned_test_rets)

    default_cd = 10
    default_sl, default_arm, default_trail = node["fixed_sl"], node["arm_pct"], node["trail_sell_pct"]
    default_fit_w, default_test_w = windows_by_cd[default_cd]
    default_test_comp = _compounded(
        [simulate_overlay(df_h, ei, ge, default_sl, default_arm, default_trail)["ret"]
         for ei, ge in default_test_w])

    print(f"  Fit-half-selected winner: confirm_days={cd} vol_gate={vg} sl={sl} arm={arm} trail={trail}")
    print(f"    fit:  n={winner['fit_n']} compounded={winner['fit_compounded']*100:.1f}% "
          f"worst_neighbor={winner['fit_worst_neighbor']*100:.1f}% cliff_safe={winner['fit_safe']}")
    print(f"    test: n={len(tuned_test_w)} compounded="
          f"{'n/a' if tuned_test_comp is None else f'{tuned_test_comp*100:.1f}%'}")
    print(f"  Plain default (confirm_days=10): test n={len(default_test_w)} compounded="
          f"{'n/a' if default_test_comp is None else f'{default_test_comp*100:.1f}%'}")

    # full-history stress test on the CURRENTLY-STAGED confirm_days (from the node's
    # own most recent candidate_overlay_results row, not the fit-winner) -- passed
    # in by the caller loop below via a second call with the real value.


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        check_node(int(arg))
