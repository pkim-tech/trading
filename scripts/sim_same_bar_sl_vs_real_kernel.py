"""Validates tonight's same-bar-SL finding against the REAL production kernel
(backtester.run_backtest_v110, return_bounds=True) directly, not the hand-rolled Python
mirror (scripts/export_trades.py::simulate_trail_both_annotated, the 'possible'-only
resolution) used everywhere else tonight -- see docs/research_log.md's 2026-08-21 entries.

Raised directly: is the "remove the fill-bar SL check" fix target well-defined only
relative to 'possible', or does it hold for 'pessimistic'/'certain' too (the resolutions
that actually matter for robust_alpha = MIN(possible, pessimistic, certain), the bound
real node promotion is based on)? Confirmed by reading backtester.py directly (2026-08-21):
all three resolutions use an identical single-branch-per-bar state machine -- a fill inside
the `elif waiting:` branch is never exit-checked on that same bar, only starting the next
bar's `if in_trade:` branch, for possible AND pessimistic AND certain alike. So the fix
target (no fill-bar SL check) is resolution-independent -- this script confirms that
empirically too, by comparing the SAME live-mimic (current, unfixed, same-bar-SL-checking
behavior) against all three REAL kernel outputs, not just one hand-rolled mirror.

Usage:
    .venv/bin/python scripts/sim_same_bar_sl_vs_real_kernel.py [--tickers T ...]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from backtester import prep_inputs, run_backtest_v110
from scripts.export_trades import load_hourly
from scripts.sim_5min_whipsaw import NODES
from scripts.sim_same_bar_sl_characterization import simulate_same_bar_sl, _summarize

TB_NODES = [n for n in NODES if n['strategy'] == 'TrailingBothZScoreBreakout']


def _trade_summary(trades):
    """trades here are backtester._build_trades dicts (real kernel), not the
    scripts/export_trades.py dict shape -- different key names, so a separate
    summarizer from sim_same_bar_sl_characterization.py's _summarize."""
    compounded = 1.0
    for t in trades:
        compounded *= (1.0 + t['Return'])
    return dict(n=len(trades), compounded_pct=round((compounded - 1.0) * 100, 2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', nargs='*', default=None)
    args = ap.parse_args()
    nodes = TB_NODES if not args.tickers else [n for n in TB_NODES if n['ticker'] in args.tickers]

    rows = []
    for node in nodes:
        ticker = node['ticker']
        take_profit = node['arm_pct'] / 100.0
        fixed_sl = node['fixed_sl'] / 100.0
        trail_buy_pct = node['trail_buy_pct'] / 100.0
        trail_pct = node['trail_sell_pct'] / 100.0
        max_hold_hours = node['max_hold_hours']

        df_h = load_hourly(ticker)
        df_daily = df_h.resample("D").last().dropna(subset=["Close"])
        strat = strategies.TrailingBothZScoreBreakout(window=node['window'], z_score_threshold=node['z'])
        ind = strat.generate_daily_indicators(df_daily)
        p = prep_inputs(df_h, ind)

        # REAL production kernel, all 3 resolutions, direct call -- not a hand-rolled mirror.
        trades_possible, trades_pessimistic, trades_certain = run_backtest_v110(
            df_h, ind, ticker, take_profit=take_profit, stop_loss=fixed_sl,
            max_hours_to_hold=max_hold_hours, z_score_threshold=node['z'],
            trail_buy_pct=trail_buy_pct, trail_pct=trail_pct, entry_timing='open_check',
            return_bounds=True, prep=p)

        # Live-mimic: CURRENT (unfixed) behavior -- same-bar SL check fires on every
        # fill, carried and fresh alike (this is what live actually does today).
        live_mimic, _, _ = simulate_same_bar_sl(
            p, take_profit, fixed_sl, trail_buy_pct, trail_pct, max_hold_hours, 9, 14, node['z'],
            apply_to='all', gate_rederivation=True)

        sp, spess, sc = _trade_summary(trades_possible), _trade_summary(trades_pessimistic), _trade_summary(trades_certain)
        sm = _summarize(live_mimic)

        rows.append(dict(ticker=ticker,
                          possible_n=sp['n'], possible_pct=sp['compounded_pct'],
                          pessimistic_n=spess['n'], pessimistic_pct=spess['compounded_pct'],
                          certain_n=sc['n'], certain_pct=sc['compounded_pct'],
                          live_mimic_n=sm['n'], live_mimic_pct=sm['compounded_pct']))

    print(f"\n{'ticker':6} {'possible_n':>10} {'possible_%':>11} {'pessim_n':>9} {'pessim_%':>10} "
          f"{'certain_n':>9} {'certain_%':>10} {'live_mimic_n':>13} {'live_mimic_%':>13}")
    for r in rows:
        print(f"{r['ticker']:6} {r['possible_n']:>10} {r['possible_pct']:>11} {r['pessimistic_n']:>9} "
              f"{r['pessimistic_pct']:>10} {r['certain_n']:>9} {r['certain_pct']:>10} "
              f"{r['live_mimic_n']:>13} {r['live_mimic_pct']:>13}")

    print("\npossible/pessimistic/certain = REAL production kernel (backtester.run_backtest_v110), all 3 resolutions.")
    print("live_mimic = current (unfixed) live behavior: same-bar SL check fires on every fill.")
    print("If live_mimic is worse than ALL THREE real-kernel resolutions, the same-bar-SL divergence")
    print("is real regardless of which resolution is used as the reference, not an artifact of 'possible'.")


if __name__ == '__main__':
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
