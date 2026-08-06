"""Margin add-on-at-arm: generalizes three_layer_summary.addon_layer's arm-to-exit
re-slicing into a standalone function returning an actual add-on TRADE LIST (not just
aggregate stats), so v5_stacked_backtest.py can combine it with other overlays' equity
curves rather than re-deriving trades from a summary dict.

Applies to any canonical trade list with a real arm_i (core trades from
drought_overlay_test.get_trades_and_bars have one; drought-overlay trades currently
don't -- trade_schema.py's from_drought_overlay leaves arm_i=None, so add-on only fires
on core trades today, per its "None means not applicable" contract).

Margin cost: uses the already-validated flat ~0.04% average-cost figure from the prior
AGQ margin add-on validation (real cushion, cliff-safe; margin interest was confirmed
negligible at typical ~33h hold times) rather than modeling a full day-count interest
schedule -- that precision isn't warranted given the prior finding.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from backtester import WIN, LOSS

MARGIN_COST_FLAT_PCT = 0.04  # validated negligible-but-real average cost, see docstring


def generate_addon_trades(trades, df_h):
    """One add-on trade per armed core trade (arm_i is not None): re-slices the SAME
    position's own arm-to-exit P&L (buying more at the arm bar's Close, exiting
    alongside the original position), matching three_layer_summary.addon_layer's
    validated logic, minus a flat margin-cost haircut."""
    closes = df_h["Close"].values
    out = []
    for t in trades:
        if t["arm_i"] is None:
            continue
        arm_i = t["arm_i"]
        entry_p = closes[arm_i]
        exit_p = t["exit_p"]
        held_days = (df_h.index[t["exit_i"]] - df_h.index[arm_i]).total_seconds() / 86400
        raw_ret = exit_p / entry_p - 1.0 - MARGIN_COST_FLAT_PCT / 100.0

        # combine_sequential's downstream cumprod() multiplies this trade's (1+ret)
        # in right after its parent core trade's (1+r_core) -- a plain cumprod of
        # raw_ret there computes (1+r_core)*(1+raw_ret), which implicitly sizes the
        # add-on off the core position's FINAL (exit-time) value. It was actually
        # sized off the core position's ARM-time value (weight = entry_p / core's own
        # entry_p), which is smaller whenever price keeps moving favorably after arm
        # -- confirmed 2026-08-08 to overstate combined return by up to 43% on real
        # AGQ trades. `ret` below is the compounding-equivalent value that makes a
        # plain cumprod([core_trade, addon_trade]) reproduce the true concurrent-
        # capital total exactly: 1 + r_core + weight*raw_ret. raw_ret (the add-on
        # capital's own real return) is preserved separately for win/loss
        # classification and reporting.
        r_core = t["exit_p"] / t["entry_p"] - 1.0
        weight = entry_p / t["entry_p"]
        compound_scale = weight / (1.0 + r_core)
        ret = compound_scale * raw_ret

        out.append({
            "signal_i": t.get("signal_i"), "entry_i": arm_i, "arm_i": None, "exit_i": t["exit_i"],
            "entry_p": entry_p, "exit_p": exit_p, "held": t["exit_i"] - arm_i,
            "held_days": held_days, "result": WIN if raw_ret > 0 else LOSS, "ret": ret,
            "raw_ret": raw_ret, "compound_scale": compound_scale, "exit_reason": "ADDON",
        })
    return out
