"""Out-of-sample validation for the drought overlay's own risk parameters -- now a real
5-axis search (confirm_days, vol_gate, sl, arm, trail), for SOXL/AGQ/KORU. Per the
2026-08-07 conversation: picking the best node/config per ticker is fine, but the
winner has to survive a genuine out-of-sample check before being trusted, which the
earlier per-ticker 5-axis sweep (docs/research_log.md's 2026-08-05/06 entries) never
did -- this closes that gap for real, including the vol-gate axis this time (the first
version of this script only searched confirm_days/sl/arm/trail).

Method: split each ticker's full real drought-window history chronologically at the
midpoint of its data range. Grid-search (same VOL_GATE_GRID/SL_GRID/ARM_GRID/TRAIL_GRID/
CONFIRM_DAYS_GRID and cliff_safety_5axis as scripts/drought_overlay_sweep.py's
per-ticker mode, reused directly -- not re-invented) + cliff-safety-select the best
config using ONLY the fit half. Apply that exact winning config to the test half
(windows it never influenced) and compare against the plain default (confirm_days=10,
node's own core sl/arm/trail, no gate -- what scripts/stacked_model/drought.py uses when
no override is passed) on the same test half. If the tuned config doesn't beat the plain
default out-of-sample, that's real evidence it was fit to noise.

Real caveat, stated up front: KORU and AGQ have far fewer drought windows than SOXL
(9-13 total across the whole history), so cutting each in half leaves very few windows
per side, and the vol-gate axis further thins whichever half it's applied to -- a clean
answer isn't guaranteed given the sample size, and a small-n result here should be read
as "still inconclusive," not "resolved," either way.

Usage: .venv/bin/python scripts/drought_out_of_sample_check.py [--tickers SOXL AGQ KORU]
"""
import argparse
import sys
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.drought_detection_test import load_nodes
from scripts.drought_overlay_test import get_trades_and_bars, find_drought_windows, simulate_overlay
from scripts.drought_overlay_sweep import (
    SL_GRID, ARM_GRID, TRAIL_GRID, CONFIRM_DAYS_GRID, VOL_GATE_GRID,
    cliff_safety_5axis, get_ivol_series, _entry_vol_pctile,
)

DEFAULT_TICKERS = ["SOXL", "AGQ", "KORU"]
MIN_TRADES = 5  # a "safe" verdict on fewer trades than this isn't meaningful


def _compounded(rets):
    return float(np.prod([1 + r for r in rets]) - 1) if rets else None


def _gate_windows(windows, df_h, ivol_series, vol_gate):
    if vol_gate is None:
        return windows
    gated = []
    for entry_i, gap_end in windows:
        entry_time = df_h.index[entry_i + 1] if entry_i + 1 < len(df_h) else df_h.index[entry_i]
        pctile = _entry_vol_pctile(entry_time, ivol_series)
        if pctile is not None and pctile < vol_gate:
            gated.append((entry_i, gap_end))
    return gated


def split_windows(trades, df_h, confirm_days, midpoint):
    windows = find_drought_windows(trades, df_h, confirm_days)
    fit = [w for w in windows if df_h.index[w[0]] < midpoint]
    test = [w for w in windows if df_h.index[w[0]] >= midpoint]
    return fit, test


def grid_search_fit_only(trades, df_h, ivol_series, midpoint):
    """5-axis grid-search + cliff-safety-select using ONLY fit-half windows."""
    fit_cells = {}  # (confirm_days, vol_gate, sl, arm, trail) -> [rets] (fit half only)
    windows_by_cd = {}
    for confirm_days in CONFIRM_DAYS_GRID:
        fit_w_all, test_w_all = split_windows(trades, df_h, confirm_days, midpoint)
        windows_by_cd[confirm_days] = (fit_w_all, test_w_all)
        if not fit_w_all:
            continue
        for vol_gate in VOL_GATE_GRID:
            fit_w = _gate_windows(fit_w_all, df_h, ivol_series, vol_gate)
            if not fit_w:
                continue
            for sl, arm, trail in product(SL_GRID, ARM_GRID, TRAIL_GRID):
                rets = [simulate_overlay(df_h, ei, ge, sl, arm, trail)["ret"] for ei, ge in fit_w]
                fit_cells[(confirm_days, vol_gate, sl, arm, trail)] = rets

    if not fit_cells:
        return None, windows_by_cd

    scored = []
    for key, rets in fit_cells.items():
        if len(rets) < MIN_TRADES:
            continue  # too few trades for "robust neighbor profile" to mean anything
        comp = _compounded(rets)
        # min_n=MIN_TRADES: a neighbor cell with fewer trades than the "safe verdict"
        # bar itself can't single-handedly decide a candidate's worst_neighbor -- see
        # cliff_safety_5axis's docstring for the real case this closes.
        worst, n_neighbors = cliff_safety_5axis(fit_cells, key, min_n=MIN_TRADES)
        scored.append((key, len(rets), comp, worst, n_neighbors))
    if not scored:
        return None, windows_by_cd

    # SAFETY-FIRST selection (2026-08-07, per the user's explicit call: "I don't need
    # the best node, I need a safe one") -- rank by the WORST NEIGHBOR value, not the
    # cell's own return. Picking by own-return-among-safe-candidates still cherry-picks
    # within that subset; picking by worst-neighbor directly rewards robustness itself,
    # not performance. Requires neighbor coverage (n_neighbors > 0) so a cell with no
    # comparable neighbors (edge of the grid, thin fit-half data) can't win by default.
    checked = [s for s in scored if s[4] > 0 and not np.isnan(s[3])]
    if not checked:
        return None, windows_by_cd
    best = max(checked, key=lambda s: s[3])
    return {"key": best[0], "fit_n": best[1], "fit_compounded": best[2],
            "fit_worst_neighbor": best[3], "fit_n_neighbors": best[4],
            "fit_safe": best[3] >= 0}, windows_by_cd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=DEFAULT_TICKERS)
    args = ap.parse_args()

    rows = []
    for ticker in args.tickers:
        node = load_nodes(65, [ticker])[0]
        trades, df_h = get_trades_and_bars(node)
        ivol_series = get_ivol_series(ticker)
        midpoint = df_h.index[0] + (df_h.index[-1] - df_h.index[0]) / 2

        winner, windows_by_cd = grid_search_fit_only(trades, df_h, ivol_series, midpoint)
        if winner is None:
            print(f"{ticker}: no fit-half drought windows at all, skipped")
            continue

        cd, vg, sl, arm, trail = winner["key"]
        _, tuned_test_w_all = windows_by_cd[cd]
        tuned_test_w = _gate_windows(tuned_test_w_all, df_h, ivol_series, vg)
        tuned_test_rets = [simulate_overlay(df_h, ei, ge, sl, arm, trail)["ret"] for ei, ge in tuned_test_w]
        tuned_test_comp = _compounded(tuned_test_rets)

        default_cd = 10
        default_sl, default_arm, default_trail = node["fixed_sl"], node["arm_pct"], node["trail_sell_pct"]
        default_fit_w, default_test_w = windows_by_cd[default_cd]
        default_fit_comp = _compounded(
            [simulate_overlay(df_h, ei, ge, default_sl, default_arm, default_trail)["ret"]
             for ei, ge in default_fit_w])
        default_test_comp = _compounded(
            [simulate_overlay(df_h, ei, ge, default_sl, default_arm, default_trail)["ret"]
             for ei, ge in default_test_w])

        beats_default = (tuned_test_comp is not None and default_test_comp is not None
                          and tuned_test_comp > default_test_comp)
        rows.append({
            "ticker": ticker, "midpoint": str(midpoint.date()),
            "tuned_confirm_days": cd, "tuned_vol_gate": vg, "tuned_sl": sl,
            "tuned_arm": arm, "tuned_trail": trail,
            "tuned_fit_n": winner["fit_n"], "tuned_fit_compounded_pct": winner["fit_compounded"] * 100,
            "fit_cliff_safe": winner["fit_safe"],
            "tuned_test_n": len(tuned_test_w), "tuned_test_compounded_pct":
                None if tuned_test_comp is None else tuned_test_comp * 100,
            "default_fit_n": len(default_fit_w), "default_fit_compounded_pct":
                None if default_fit_comp is None else default_fit_comp * 100,
            "default_test_n": len(default_test_w), "default_test_compounded_pct":
                None if default_test_comp is None else default_test_comp * 100,
            "tuned_beats_default_oos": beats_default,
        })

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 240)
    print(df.round(2).to_string(index=False))
    print("\nVerdict: for each ticker, does the fit-half-chosen 5-axis config (now including\n"
          "the vol gate) still beat the plain default on held-out (test-half) windows it never\n"
          "influenced? A 'False' or a tiny test_n means treat the tuned config as unresolved/\n"
          "overfit-risk, not adopted.")


if __name__ == "__main__":
    main()
