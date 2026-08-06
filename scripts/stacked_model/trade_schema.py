"""Canonical trade-record schema for the v5-stacked overlay framework
(docs/deep_backlog.md's 2026-08-06/07 v5-stacked design entry).

Three incompatible trade-record shapes already exist in this codebase:

1. Hourly kernel mirror (scripts/export_trades.py's simulate_trail_both_annotated /
   simulate_trail_exit_chaos, consumed directly by drought_overlay_test.get_trades_and_bars):
   {signal_i, signal_z, entry_i, arm_i, exit_i, entry_p, exit_p, held, result, ret}.
   `held` is a raw hourly-bar count. This is the CANONICAL shape -- most existing
   tooling already consumes it, so core trades need no adapter at all.
2. Drought-overlay (scripts/drought_overlay_test.py::simulate_overlay): {exit_i,
   exit_reason, ret} only -- no entry_p/exit_p/held/result.
3. Daily crash-stress (scripts/sim_bear_market_stress.py::run_strategy_daily):
   day-indexed, different field names (entry_price/exit_price/held_days/return/reason).

Duration-dependent overlays (put_hedge.py) need a real elapsed-time figure regardless
of source granularity, but `held` means different things across these three shapes (bar
count vs. day count). Every canonical trade dict here also carries `held_days` (real
calendar days, computed from actual timestamps, not inferred from a bars/day constant)
-- overlays needing duration should always read held_days, never `held` alone.
"""
from backtester import WIN, LOSS


def attach_held_days(trades, df_h):
    """Adds a real held_days field to hourly bar-indexed trades (format #1, straight out
    of get_trades_and_bars) by looking up actual elapsed calendar time between entry_i
    and exit_i in df_h's real timestamp index -- not a bars-per-day approximation."""
    out = []
    for t in trades:
        span = (df_h.index[t["exit_i"]] - df_h.index[t["entry_i"]]).total_seconds() / 86400
        out.append({**t, "held_days": span})
    return out


def from_drought_overlay(overlay_result, entry_i, entry_price, df_h):
    """Normalizes drought_overlay_test.simulate_overlay()'s {exit_i, exit_reason, ret}
    into the canonical shape. No signal_i/arm_i concept for a drought entry (it's
    confirmed by elapsed no-signal time, not a z-score breakout) -- both left None;
    downstream code (e.g. add_on.py) must treat None as "not applicable", not "zero"."""
    exit_i = overlay_result["exit_i"]
    ret = overlay_result["ret"]
    exit_p = entry_price * (1 + ret)
    held_days = (df_h.index[exit_i] - df_h.index[entry_i]).total_seconds() / 86400
    return {
        "signal_i": None, "entry_i": entry_i, "arm_i": None, "exit_i": exit_i,
        "entry_p": entry_price, "exit_p": exit_p, "held": exit_i - entry_i,
        "held_days": held_days, "result": WIN if ret > 0 else LOSS, "ret": ret,
        "exit_reason": overlay_result["exit_reason"],
    }


def from_daily_stress(daily_trade):
    """Normalizes sim_bear_market_stress.run_strategy_daily()'s day-indexed
    {entry_day, exit_day, entry_price, exit_price, held_days, reason, return} into the
    canonical shape. Lossy: no real signal_i/arm_i (the daily approximation doesn't
    track them), and entry_i/exit_i here are trading-day positions in the synthetic
    daily series, NOT real hourly bar indices -- never mix with format #1 indices."""
    ret = daily_trade["return"]
    return {
        "signal_i": None, "entry_i": daily_trade["entry_day"], "arm_i": None,
        "exit_i": daily_trade["exit_day"], "entry_p": daily_trade["entry_price"],
        "exit_p": daily_trade["exit_price"], "held": daily_trade["held_days"],
        "held_days": float(daily_trade["held_days"]),
        "result": WIN if ret > 0 else LOSS, "ret": ret,
        "exit_reason": daily_trade["reason"],
    }
