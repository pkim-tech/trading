"""node_key(): sha256(strategy + canonical params) -- stable node identity independent
of any table row id, per docs/plans/backtest_schema_v2_phase_tables.md's spec.
Standalone for now (not yet in strategies.py) since the schema-v2 design is still
being validated via scripts/bench_phase1_phase2_inmemory.py, not settled/production.

params_dict only includes axes the strategy actually declares (via
strategies.resolve_axis_columns) -- an unused axis contributes NO key material,
not a 0/default value, so two different strategies' overlapping column names never
collide and a strategy with fewer real axes never gets a phantom 4th value baked in.
"""
import hashlib
import json


def build_params_dict(strategy_name, ticker, fixed_sl, window, z_score_threshold, max_hold_hours,
                       take_profit, stop_loss, trail_sell_pct, entry_timing, resolve_axis_columns):
    sl_axis_col, fourth_axis_col = resolve_axis_columns(strategy_name)
    params = {
        "ticker": ticker, "fixed_sl": float(fixed_sl), "window": int(window),
        "z_score_threshold": float(z_score_threshold), "max_hold_hours": int(max_hold_hours),
        "entry_timing": entry_timing,
    }
    if strategy_name == 'TrailingBothZScoreBreakout':
        params["arm_pct"] = float(take_profit)
    else:
        params["take_profit"] = float(take_profit)
    params[sl_axis_col] = float(stop_loss)
    if fourth_axis_col:
        params[fourth_axis_col] = float(trail_sell_pct)
    return params


def node_key(strategy_name, ticker, fixed_sl, window, z_score_threshold, max_hold_hours,
             take_profit, stop_loss, trail_sell_pct, entry_timing, resolve_axis_columns):
    params = build_params_dict(strategy_name, ticker, fixed_sl, window, z_score_threshold,
                                max_hold_hours, take_profit, stop_loss, trail_sell_pct,
                                entry_timing, resolve_axis_columns)
    canonical = json.dumps(params, sort_keys=True)
    return hashlib.sha256(f"{strategy_name}|{canonical}".encode()).hexdigest()
