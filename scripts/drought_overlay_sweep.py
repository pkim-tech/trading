"""
Real grid search over the drought-overlay's risk-management parameters, following this
project's established sweep discipline (docs/design.md's "SOXL drought-overlay parameter
sweep" section) -- fixed_sl%, arm_pct%, trail_sell_pct%, and confirm_days as 4 INDEPENDENT
axes, not the coarse lockstep manual sweep (`docs/research_log.md`'s 2026-08-04 very late
entry) that moved SL/trail together and never isolated which axis actually mattered.

Reuses drought_overlay_test.py's real building blocks directly (get_trades_and_bars,
find_drought_windows, simulate_overlay) rather than reimplementing any of them -- the
entry/exit mechanics are the same TrailingBothZScoreBreakout arm-then-trail state machine
already proven out there; this script only adds the grid loop, pooling, and cliff-safety
screen around it.

Scope, decided 2026-08-05: the 10 v5 watchlist tickers only (AGQ/DPST/GDXU/HIBL/KORU/NUGT/
SOXL/UDOW/USD/YANG) -- NOT the "big 6" broad-index comparison group (SPY/SSO/UPRO/QQQ/QLD/
TQQQ) from the 2026-08-04 research finding, since those aren't on any watchlist and would
need a separate backtest_cache lookup path. That comparison is a follow-up, not built here.

Cliff-safety, decided 2026-08-05: grid-neighbor robustness (this project's standard
island/cliff-safety convention -- does a candidate cell's immediate neighbors along each
axis hold up, not just the best cell in isolation), NOT the MIN(possible, pessimistic,
certain) intrabar-fill-ambiguity resolution the core backtester kernel uses -- that would be
a much bigger addition to simulate_overlay itself (currently single-resolution) and wasn't
what this sweep needed to answer the real question raised by the manual sweep (SOXL's result
being 2-of-13-trades fragile is a small-sample/parameter-instability problem, exactly what
grid-neighbor robustness catches).

confirm_days is swept as a real 4th axis (decided 2026-08-05, not held constant) -- droughts/
entry points depend on confirm_days, so windows are recomputed once per (ticker, confirm_days)
pair via find_drought_windows and reused across every (sl, arm, trail) combo at that
confirm_days, rather than recomputed per grid cell.

Usage: .venv/bin/python scripts/drought_overlay_sweep.py [--tickers ...] [--watchlist-id 65]
       [--top-n 15] [--csv]
"""
import argparse
import sys
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.drought_detection_test import load_nodes
from scripts.drought_overlay_test import find_drought_windows, get_trades_and_bars, simulate_overlay

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

SL_GRID = [1, 2, 3, 5, 7, 10, 15, 18]
ARM_GRID = [5, 10, 15, 20, 30, 40]
TRAIL_GRID = [1, 2, 3, 5, 7, 10, 15]
CONFIRM_DAYS_GRID = [5, 10, 15, 20]


def _compounded(rets):
    return float(np.prod([1 + r for r in rets]) - 1) if rets else float("nan")


def run_ticker_sweep(node):
    """Returns {(confirm_days, sl, arm, trail): [rets]} for one ticker -- windows are
    computed once per confirm_days and reused across every (sl, arm, trail) combo at that
    confirm_days, since window-finding doesn't depend on the exit-risk parameters at all."""
    trades, df_h = get_trades_and_bars(node)
    if len(trades) < 2:
        return {}

    cells = {}
    for confirm_days in CONFIRM_DAYS_GRID:
        windows = find_drought_windows(trades, df_h, confirm_days)
        if not windows:
            continue
        for sl, arm, trail in product(SL_GRID, ARM_GRID, TRAIL_GRID):
            rets = [simulate_overlay(df_h, entry_i, gap_end, sl, arm, trail)["ret"]
                    for entry_i, gap_end in windows]
            cells[(confirm_days, sl, arm, trail)] = rets
    return cells


def pool_cells(per_ticker_cells):
    """per_ticker_cells: {ticker: {(cd, sl, arm, trail): [rets]}} -> pooled
    {(cd, sl, arm, trail): [rets across all tickers]}."""
    pooled = {}
    for cells in per_ticker_cells.values():
        for key, rets in cells.items():
            pooled.setdefault(key, []).extend(rets)
    return pooled


def cliff_safety(pooled, key):
    """Grid-neighbor robustness: the worst pooled compounded return among this cell's
    immediate neighbors (one step in each of the 4 axes, using the real grid value lists --
    not a fixed +/-1 offset, so edge cells and uneven grid spacing are both handled
    correctly). A neighbor missing from `pooled` (e.g. zero drought windows at that
    confirm_days) is skipped, not treated as a failure -- absence of data isn't evidence of
    a cliff. Returns (worst_neighbor_compounded, n_neighbors_checked); NaN/0 if no
    neighbors exist at all (shouldn't happen for an interior cell)."""
    cd, sl, arm, trail = key
    grids = {0: CONFIRM_DAYS_GRID, 1: SL_GRID, 2: ARM_GRID, 3: TRAIL_GRID}
    neighbor_comps = []
    for axis, grid in grids.items():
        idx = grid.index(key[axis])
        for step in (-1, 1):
            n_idx = idx + step
            if not (0 <= n_idx < len(grid)):
                continue
            n_key = list(key)
            n_key[axis] = grid[n_idx]
            n_key = tuple(n_key)
            if n_key in pooled and pooled[n_key]:
                neighbor_comps.append(_compounded(pooled[n_key]))
    if not neighbor_comps:
        return float("nan"), 0
    return min(neighbor_comps), len(neighbor_comps)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=None)
    parser.add_argument("--watchlist-id", type=int, default=65)
    parser.add_argument("--top-n", type=int, default=15)
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()

    nodes = load_nodes(args.watchlist_id, args.tickers)
    per_ticker_cells = {}
    for node in nodes:
        try:
            cells = run_ticker_sweep(node)
        except Exception as e:
            print(f"{node['ticker']}: failed ({e})")
            continue
        if cells:
            per_ticker_cells[node["ticker"]] = cells
        print(f"  {node['ticker']}: {len(cells)} cells computed")

    if not per_ticker_cells:
        print("No droughts found for any ticker.")
        return

    pooled = pool_cells(per_ticker_cells)

    rows = []
    for key, rets in pooled.items():
        cd, sl, arm, trail = key
        worst_neighbor, n_neighbors = cliff_safety(pooled, key)
        comp = _compounded(rets)
        rows.append({
            "confirm_days": cd, "fixed_sl": sl, "arm_pct": arm, "trail_sell_pct": trail,
            "n": len(rets), "win_rate": (np.array(rets) > 0).mean() if rets else float("nan"),
            "compounded_pct": comp * 100 if rets else float("nan"),
            "worst_neighbor_compounded_pct": worst_neighbor * 100 if not np.isnan(worst_neighbor) else float("nan"),
            "n_neighbors": n_neighbors,
            "cliff_safe": bool(not np.isnan(worst_neighbor) and worst_neighbor > 0),
        })
    df = pd.DataFrame(rows).sort_values("compounded_pct", ascending=False)
    pd.set_option("display.width", 200)

    if args.csv:
        OUTPUT_DIR.mkdir(exist_ok=True)
        df.to_csv(OUTPUT_DIR / "drought_overlay_sweep_pooled.csv", index=False)
        per_ticker_rows = []
        for ticker, cells in per_ticker_cells.items():
            for key, rets in cells.items():
                cd, sl, arm, trail = key
                per_ticker_rows.append({
                    "ticker": ticker, "confirm_days": cd, "fixed_sl": sl, "arm_pct": arm,
                    "trail_sell_pct": trail, "n": len(rets),
                    "win_rate": (np.array(rets) > 0).mean() if rets else float("nan"),
                    "compounded_pct": _compounded(rets) * 100 if rets else float("nan"),
                })
        pd.DataFrame(per_ticker_rows).to_csv(OUTPUT_DIR / "drought_overlay_sweep_per_ticker.csv", index=False)
        print(f"\nWrote {OUTPUT_DIR / 'drought_overlay_sweep_pooled.csv'} and "
              f"{OUTPUT_DIR / 'drought_overlay_sweep_per_ticker.csv'}")

    print(f"\n--- Top {args.top_n} pooled cells by compounded return "
          f"({len(pooled)} total cells, {sum(len(v) for v in pooled.values())} total trade-observations) ---")
    print(df.head(args.top_n).round(2).to_string(index=False))

    cliff_safe_df = df[df["cliff_safe"]]
    print(f"\n--- Top {args.top_n} CLIFF-SAFE pooled cells (worst neighbor also net positive) ---")
    if cliff_safe_df.empty:
        print("None -- no pooled cell has an entirely net-positive neighborhood.")
    else:
        print(cliff_safe_df.head(args.top_n).round(2).to_string(index=False))

    # SOXL in isolation for the best overall pooled cell, per the design doc's explicit ask
    # (SOXL's result reported both in isolation and pooled, since it was the one ticker with
    # a fragile-but-positive result in the earlier manual sweep).
    if not df.empty and "SOXL" in per_ticker_cells:
        best = df.iloc[0]
        key = (int(best["confirm_days"]), best["fixed_sl"], best["arm_pct"], best["trail_sell_pct"])
        soxl_rets = per_ticker_cells["SOXL"].get(key, [])
        print(f"\n--- SOXL in isolation at the best pooled cell {key} ---")
        if soxl_rets:
            print(f"n={len(soxl_rets)}  win_rate={(np.array(soxl_rets) > 0).mean():.3f}  "
                  f"compounded={_compounded(soxl_rets)*100:.1f}%")
        else:
            print("SOXL has zero drought windows at this cell's confirm_days.")


if __name__ == "__main__":
    main()
