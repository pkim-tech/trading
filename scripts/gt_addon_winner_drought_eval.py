"""Informational-only: what would the drought overlay do to the SINGLE overall Phase2.5-GT
winning node's own trade list? Built 2026-08-22 alongside the GT add-on-overlay work
(backtester.apply_addon_overlay_ground_truth, run_optimization_sweep.run_addon_cliff_safety_
ground_truth) -- NOT a safety-scoped evaluation (user's explicit call: drought does not need
its own cliff-safety pass right now, "we'll end up doing a second sweep on the winner nodes"
later). This is a quick number for awareness only.

Reuses scripts/drought_overlay_test.py's real drought mechanism verbatim --
find_drought_windows (confirm_days-gated gap detection between consecutive real signals) and
simulate_overlay (core-strategy-style fixed-SL/trailing-stop exit, bar-index based, strategy-
agnostic) -- neither function depends on the LEGACY hourly kernel's trade format, only on
(df_h, signal_i, exit_i) bar indices, so adapting them to the GT kernel's own trade list is a
pure format conversion: GT trades carry real Timestamps (Entry Time/Exit Time), converted here
to positions in the SAME df_hourly_windowed index the GT trades were resolved against via
index.get_loc (exact match expected -- GT always resolves entry/exit to a real index label).

Usage: .venv/bin/python scripts/gt_addon_winner_drought_eval.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_optimization_sweep as ros
from scripts.drought_overlay_test import find_drought_windows, simulate_overlay

CONFIRM_DAYS = 3  # validated default per docs/deep_backlog.md (GDXU's stress-tested config)


def trades_to_bar_indices(trades, df_hourly_windowed):
    idx = df_hourly_windowed.index
    out = []
    for t in trades:
        try:
            signal_i = idx.get_loc(t['Entry Time'])
            exit_i = idx.get_loc(t['Exit Time'])
        except KeyError:
            continue  # entry/exit fell outside df_hourly_windowed's own index -- skip
        out.append({'signal_i': signal_i, 'exit_i': exit_i})
    return out


def main():
    ticker = "SOXL"
    strategy_name = "TrailingBothZScoreBreakout"
    config_version = "v6-w2023-07-24_2026-08-21"
    start_date, end_date = "2023-07-24", "2026-08-21"
    fixed_sl, entry_timing = 2, "open_check"

    hp = {
        'take_profits': [1, 2, 3, 4, 5, 6, 9, 12, 15, 18, 21, 24, 27, 30],
        'stop_losses': [1, 2, 3, 4, 5, 6, 9, 12, 15, 18, 21, 24, 27, 30],
        'trail_pcts': [1, 2, 3, 4, 5, 6, 7],
        'hold_time_caps': list(range(7, 141, 7)),
        'windows': [10, 20],
        'z_score_thresholds': [1.0, 1.5, 2.0],
    }

    candidates = ros.derive_phase25_candidates_ground_truth(
        ticker, strategy_name, config_version, hp, fixed_sl=fixed_sl, entry_timing=entry_timing)
    if not candidates:
        raise SystemExit("No Phase2.5-GT candidates found -- nothing to evaluate.")
    winner = max(candidates, key=lambda c: c['robust_alpha'])
    print(f"Overall winner: TP={winner['take_profit']} SL={winner['stop_loss']} "
          f"hold={winner['max_hold_hours']}h w={winner['window']} z={winner['z_score_threshold']} "
          f"tpct={winner['tpct']} core_cagr={winner['cagr']:.1f}%")

    strategy_class = ros.strategies.TrailingBothZScoreBreakout
    inputs = ros._load_node_inputs_ground_truth(
        ticker, strategy_class, strategy_name, winner['window'], winner['z_score_threshold'],
        start_date, end_date, data_source="yahoo")
    _, df_daily_processed, minute_df, df_hourly_windowed, prep, mprep = inputs

    trades = ros.run_backtest_ground_truth(
        df_hourly_windowed, df_daily_processed, ticker, minute_df,
        fixed_sl=fixed_sl, arm_pct=winner['take_profit'], trail_buy_pct=winner['stop_loss'],
        trail_sell_pct=winner['tpct'], max_hours_to_hold=winner['max_hold_hours'],
        z_score_threshold=winner['z_score_threshold'], is_both=True,
        open_check_entry_timing=(entry_timing == 'open_check'), same_bar_reentry=False,
        prep=prep, mprep=mprep, need_times=True,
    )
    print(f"Core GT trades: {len(trades)}")

    bar_trades = trades_to_bar_indices(trades, df_hourly_windowed)
    windows = find_drought_windows(bar_trades, df_hourly_windowed, CONFIRM_DAYS)
    print(f"Drought windows found (confirm_days={CONFIRM_DAYS}): {len(windows)}")

    drought_rets = []
    for entry_i, backstop_i in windows:
        res = simulate_overlay(df_hourly_windowed, entry_i, backstop_i,
                                fixed_sl_pct=fixed_sl, arm_pct=winner['take_profit'],
                                trail_sell_pct=winner['tpct'])
        if res is not None:
            drought_rets.append(res['ret'])

    core_compounded = ((1 + pd.Series([t['Return'] for t in trades])).prod() - 1) * 100 \
        if drought_rets or trades else 0.0
    print(f"Drought-eligible windows actually simulated: {len(drought_rets)}")
    if drought_rets:
        drought_compounded = ((1 + pd.Series(drought_rets)).prod() - 1) * 100
        combined_compounded = ((1 + pd.Series([t['Return'] for t in trades] + drought_rets)).prod() - 1) * 100
        print(f"Core-only compounded return: {core_compounded:+.1f}%")
        print(f"Drought-only compounded return (extra trades): {drought_compounded:+.1f}%")
        print(f"Core+drought combined compounded return: {combined_compounded:+.1f}%")
    else:
        print("No drought windows produced a simulated trade -- nothing to add.")


if __name__ == "__main__":
    main()
