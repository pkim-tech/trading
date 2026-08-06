"""Thin wrapper around drought_overlay_test.py's already-validated drought-overlay
mechanics (get_trades_and_bars/find_drought_windows/simulate_overlay), returning
drought trades normalized to the canonical schema (trade_schema.from_drought_overlay)
so v5_stacked_backtest.py can combine them with core/add-on/put-hedge trades using one
common trade-list shape.

Reuses, does not reimplement -- the drought mechanics themselves are already tested and
validated (docs/research_log.md's 2026-08-05/06 drought-overlay entries); the only real
new work here is the schema normalization.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts.drought_overlay_test import get_trades_and_bars, find_drought_windows, simulate_overlay
from scripts.drought_overlay_sweep import get_ivol_series, _entry_vol_pctile
from scripts.stacked_model.trade_schema import from_drought_overlay


def _apply_vol_gate(windows, df_h, ticker, vol_gate):
    if vol_gate is None:
        return windows
    ivol_series = get_ivol_series(ticker)
    gated = []
    for entry_i, gap_end in windows:
        entry_time = df_h.index[entry_i + 1] if entry_i + 1 < len(df_h) else df_h.index[entry_i]
        pctile = _entry_vol_pctile(entry_time, ivol_series)
        if pctile is not None and pctile < vol_gate:
            gated.append((entry_i, gap_end))
    return gated

CONFIRM_DAYS_STEP = 5
SL_STEP = 1
ARM_STEP = 2
TRAIL_STEP = 1


def _cell_compounded(trades, df_h, confirm_days, sl_pct, arm_pct, trail_pct):
    windows = find_drought_windows(trades, df_h, confirm_days)
    if not windows:
        return None
    rets = [simulate_overlay(df_h, ei, ge, sl_pct, arm_pct, trail_pct)["ret"] for ei, ge in windows]
    return float(np.prod([1 + r for r in rets]) - 1), len(rets)


def check_cliff_safety(node, confirm_days=10, sl_pct=None, arm_pct=None, trail_pct=None):
    """Robustness check for the ONE FIXED drought config generate_drought_trades() uses
    by default (the node's own core risk params + confirm_days=10) -- perturbs each axis
    by one small step (CONFIRM_DAYS_STEP/SL_STEP/ARM_STEP/TRAIL_STEP; NOT a re-search or
    grid optimization) and checks whether every neighbor is still profitable, the same
    "worst_neighbor >= 0" convention three_layer_summary.py uses for its own (much
    larger) grid search.

    Built for a real gap found in conversation 2026-08-07: the drought overlay's own
    parameters were never actually checked for robustness the way core's parameters
    were. NOT wired into generate_drought_trades()/the pipeline automatically -- this
    is a manual validation tool, run once per candidate config (SOXL's
    confirm_days=3/vol_gate=0.4 was validated this way; the result is hardcoded as
    scripts/v5_stacked_backtest.py's DROUGHT_VALIDATED_CONFIG, not re-derived by
    calling this function at runtime). Re-run this directly before trusting any NEW
    drought candidate config -- caught 2026-08-08 after this docstring previously
    (incorrectly) implied the gap was closed automatically."""
    trades, df_h = get_trades_and_bars(node)
    sl_pct = sl_pct if sl_pct is not None else node["fixed_sl"]
    arm_pct = arm_pct if arm_pct is not None else node["arm_pct"]
    trail_pct = trail_pct if trail_pct is not None else node["trail_sell_pct"]

    base = _cell_compounded(trades, df_h, confirm_days, sl_pct, arm_pct, trail_pct)
    if base is None:
        return {"safe": False, "base_compounded": None, "worst_neighbor": None, "n_checked": 0}
    base_comp, _ = base

    axis_steps = [("confirm_days", CONFIRM_DAYS_STEP), ("sl_pct", SL_STEP),
                  ("arm_pct", ARM_STEP), ("trail_pct", TRAIL_STEP)]
    base_params = {"confirm_days": confirm_days, "sl_pct": sl_pct, "arm_pct": arm_pct, "trail_pct": trail_pct}

    neighbors = []
    for axis, step in axis_steps:
        for direction in (-1, 1):
            params = dict(base_params)
            params[axis] = params[axis] + direction * step
            if params[axis] <= 0:
                continue
            cell = _cell_compounded(trades, df_h, params["confirm_days"], params["sl_pct"],
                                     params["arm_pct"], params["trail_pct"])
            if cell is not None:
                neighbors.append(cell[0])

    if not neighbors:
        return {"safe": False, "base_compounded": base_comp, "worst_neighbor": None, "n_checked": 0}
    worst = min(neighbors)
    return {"safe": worst >= 0, "base_compounded": base_comp, "worst_neighbor": worst, "n_checked": len(neighbors)}


def generate_drought_trades(node, confirm_days=10, sl_pct=None, arm_pct=None, trail_pct=None,
                             vol_gate=None):
    """Real drought-overlay trades for one node, normalized to the canonical schema.
    Defaults (sl_pct/arm_pct/trail_pct=None) reuse the node's OWN core-strategy risk
    params, matching drought_overlay_test.py's own default behavior -- deliberately not
    re-optimized here, per this session's "core config stays frozen, don't reopen the
    search" decision applied equally to the overlay's own defaults.

    vol_gate: optional percentile threshold (0-1), reusing
    drought_overlay_sweep.get_ivol_series/_entry_vol_pctile exactly (the same
    2026-08-05/06 per-ticker intraday-vol entry gate, not reimplemented) -- skips a
    drought window's entry if the entry-time intraday-vol percentile against the
    ticker's own historical distribution is >= vol_gate. None (default) = no gate."""
    trades, df_h = get_trades_and_bars(node)
    sl_pct = sl_pct if sl_pct is not None else node["fixed_sl"]
    arm_pct = arm_pct if arm_pct is not None else node["arm_pct"]
    trail_pct = trail_pct if trail_pct is not None else node["trail_sell_pct"]

    windows = find_drought_windows(trades, df_h, confirm_days)
    windows = _apply_vol_gate(windows, df_h, node["ticker"], vol_gate)

    opens, closes = df_h["Open"].values, df_h["Close"].values
    drought_trades = []
    for entry_i, gap_end in windows:
        entry_bar = entry_i + 1 if entry_i + 1 < len(opens) else entry_i
        entry_price = opens[entry_bar] if entry_i + 1 < len(opens) else closes[entry_i]
        result = simulate_overlay(df_h, entry_i, gap_end, sl_pct, arm_pct, trail_pct)
        drought_trades.append(from_drought_overlay(result, entry_bar, entry_price, df_h))
    return drought_trades, df_h
