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

# GT_TRADES_KERNEL_VERSION (2026-08-29, staleness-invalidation fix, paired-review LOW
# finding -- round 2: this literal used to be duplicated as a module-level constant in
# bench_phase1_phase2_inmemory.py AND a function-local in run_optimization_sweep.py,
# with nothing forcing them to move together). Single source of truth now, imported by
# every real writer/reader of backtest_winner_trades' kernel_version column: bench_
# phase1_phase2_inmemory.py (writer), run_optimization_sweep.py's build_candidate_
# report_ground_truth (reader), scripts/rebuild_winner_trades.py (writer), scripts/
# phase5_second_level_overlay_check.py (reader, via candidate_verification_store.
# get_cached_trades). Reuses the SAME 'ground_truth_v6' literal backtest_cache's own
# kernel_version column already uses (run_optimization_sweep.py, e.g.
# discover_all_gt_scopes) -- not a new naming scheme. Bump this the next time
# backtester.run_backtest_ground_truth's real trade-generation logic changes
# materially (the same cadence docs/backtest-change-rollout's own skill already
# tracks) -- every consumer importing this constant picks up the bump automatically,
# no risk of one call site being bumped and another left stale.
GT_TRADES_KERNEL_VERSION = "ground_truth_v6"


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
