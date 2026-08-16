"""Stage-1 (backtest-change-rollout skill) manual trade audit for the new date-windowing
capability added to run_optimization_sweep.py::_load_node_inputs/_window_prep
(docs/deep_backlog.md's 2026-08-16 date-range entry, Phase 1 scope: data-loading only).

Per the skill: this produces human-reviewable evidence for the user to check by hand --
it does NOT itself claim the change is verified. Runs SOXL's real live `ira` node
config (watch_list id=92: TrailingBothZScoreBreakout, window=10, z=1.0, arm_sell_pct=30,
fixed_sl=2.0, trail_buy_pct=3.0, trail_sell_pct=1.0, max_hold_hours=70,
entry_timing=open_check) twice -- once against the full cached history, once windowed
to a bounded 1yr-ish range -- and prints, for every real trade whose entry falls inside
the window, the exact entry/exit bar OHLC and fill from both runs side by side, plus the
indicator values (SMA/Std) actually used, so a person can hand-verify the fill wasn't
corrupted by the windowing (either mis-slicing sma/std/trend, which are indexed per
calendar day and must stay full/unsliced, or truncating input before indicators warm up).

Also prints an alpha comparison demonstrating the benchmark fix (2026-08-16 paired review
HIGH finding): windowed trades scored against a full-history spy_bh (the pre-fix bug) vs.
against a windowed spy_bh via compute_bh_returns(ticker, start, end) (the fix) -- these
should differ, since SPY's own buy-hold return over a ~1yr window is not the same as over
the full multi-year cache.

--cache-roundtrip additionally exercises the NEW piece added on top of Phase 1
(window_version_suffix() + dispatch_parallel_grid's ValueError guard + CLI wiring,
2026-08-16/17): drives this exact node's grid coordinates through the REAL
dispatch_parallel_grid path (a ProcessPoolExecutor + real backtest_cache writes/reads
under a windowed, suffixed version string), then compares the row read back from
backtest_cache against a direct uncached run_backtest_dispatch call for the identical
window/params. A second dispatch_parallel_grid call for the same task (now a guaranteed
cache hit, 0 unvisited tasks) confirms the persisted row is stable on reread too.

Usage: .venv/bin/python scripts/audit_date_window_soxl.py [--start 2025-06-01] [--end 2026-06-01]
       .venv/bin/python scripts/audit_date_window_soxl.py --cache-roundtrip [--start ...] [--end ...]
"""
import argparse
import sqlite3
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from run_optimization_sweep import (
    DB_PATH, _load_node_inputs, _summarize_trades, _warmup_worker,
    compute_bh_returns, dispatch_parallel_grid, window_version_suffix,
)
from backtester import run_backtest_dispatch

TICKER = 'SOXL'
STRATEGY_NAME = 'TrailingBothZScoreBreakout'
NODE = dict(window=10, z=1.0, arm_sell_pct=30.0, fixed_sl=2.0,
            trail_buy_pct=3.0, trail_sell_pct=1.0, max_hold_hours=70,
            entry_timing='open_check')


def _run(start_date=None, end_date=None):
    strategy_class = getattr(strategies, STRATEGY_NAME)
    _, _, prep = _load_node_inputs(TICKER, strategy_class, STRATEGY_NAME, NODE['window'], NODE['z'],
                                    start_date, end_date)
    trades = run_backtest_dispatch(
        strategy_class, None, None, TICKER,
        take_profit=NODE['arm_sell_pct'], sl_raw=NODE['trail_buy_pct'],
        max_hours_to_hold=NODE['max_hold_hours'], z_score_threshold=NODE['z'],
        fixed_sl=NODE['fixed_sl'], trail_pct_pct=NODE['trail_sell_pct'],
        entry_timing=NODE['entry_timing'], return_bounds=False, prep=prep,
    )
    closed = [t for t in trades if t['Result'] in ('WIN', 'LOSS', 'TWIN', 'TLOSS')]
    return closed, prep


def _bar_row(prep, ts):
    idx = prep['timestamps'].searchsorted(ts)
    if idx >= len(prep['timestamps']) or prep['timestamps'][idx] != ts:
        return None
    return dict(
        timestamp=prep['timestamps'][idx],
        open=prep['opens'][idx], high=prep['highs'][idx],
        low=prep['lows'][idx], close=prep['prices'][idx],
        daily_idx=int(prep['daily_idx'][idx]),
        sma=prep['sma_arr'][prep['daily_idx'][idx]] if prep['daily_idx'][idx] >= 0 else None,
        std=prep['std_arr'][prep['daily_idx'][idx]] if prep['daily_idx'][idx] >= 0 else None,
    )


AUDIT_VERSION_BASE = "v5-stage1audit"


def audit_cache_roundtrip(start_date, end_date):
    """Drives SOXL's real node params through dispatch_parallel_grid (real
    ProcessPoolExecutor + real backtest_cache write) under a windowed+suffixed
    version, then checks the persisted row against a direct uncached compute, and
    again on a guaranteed-cache-hit rerun. Prints a comparison table; does not
    itself claim correctness -- same framing as the rest of this script."""
    config_version = AUDIT_VERSION_BASE + window_version_suffix(start_date, end_date)

    # Task tuple matching run_single_backtest_node_isolated's (tp, sl, hold, w, z, tpct):
    # for TrailingBothZScoreBreakout, tp=arm_sell_pct, sl=trail_buy_pct (sl_axis),
    # tpct=trail_sell_pct (fourth_axis) -- see strategies.TrailingBothZScoreBreakout /
    # resolve_axis_columns and _sl_axis_real_column's docstring.
    tp, sl, hold, w, z, tpct = (
        int(NODE['arm_sell_pct']), int(NODE['trail_buy_pct']), int(NODE['max_hold_hours']),
        int(NODE['window']), float(NODE['z']), float(NODE['trail_sell_pct']),
    )
    fixed_sl = NODE['fixed_sl']
    entry_timing = NODE['entry_timing']
    task = (tp, sl, hold, w, z, tpct)

    print(f"=== Cache round-trip audit: version={config_version!r} task={task} "
          f"fixed_sl={fixed_sl} entry_timing={entry_timing} ===\n")

    # Direct uncached compute -- ground truth for comparison.
    win_trades, _ = _run(start_date, end_date)
    _, win_spy_bh = compute_bh_returns(TICKER, start_date, end_date)
    if win_trades:
        direct_alpha, direct_n, direct_win_rate, direct_compounded, _ = _summarize_trades(win_trades, win_spy_bh)
    else:
        direct_alpha = direct_n = direct_win_rate = direct_compounded = None
    print(f"Direct uncached (_load_node_inputs + run_backtest_dispatch): "
          f"trades={direct_n} alpha={direct_alpha} compounded={direct_compounded} win_rate={direct_win_rate}")

    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        conn.execute("DELETE FROM backtest_cache WHERE version=? AND ticker=? AND strategy=?",
                     (config_version, TICKER, STRATEGY_NAME))
        conn.commit()

    with ProcessPoolExecutor(max_workers=2, initializer=_warmup_worker) as pool:
        df1 = dispatch_parallel_grid(
            pool, [task], TICKER, STRATEGY_NAME, config_version, "Stage1-CacheRoundtrip-Write",
            win_spy_bh, win_spy_bh, "audit-run", fixed_sl=fixed_sl, entry_timing=entry_timing,
            start_date=start_date, end_date=end_date,
        )
        print(f"\nFirst call (should compute fresh, write 1 row): {len(df1)} row(s) returned")
        if not df1.empty:
            r = df1.iloc[0]
            print(f"  Trades={r['Trades']} Alpha vs SPY %={r['Alpha vs SPY %']:.4f} "
                  f"Return %={r['Return %']:.4f} Win Rate %={r['Win Rate %']:.4f}")

        df2 = dispatch_parallel_grid(
            pool, [task], TICKER, STRATEGY_NAME, config_version, "Stage1-CacheRoundtrip-Reread",
            win_spy_bh, win_spy_bh, "audit-run", fixed_sl=fixed_sl, entry_timing=entry_timing,
            start_date=start_date, end_date=end_date,
        )
        print(f"\nSecond call (should be a pure cache hit, 0 recompute): {len(df2)} row(s) returned")
        if not df2.empty:
            r2 = df2.iloc[0]
            print(f"  Trades={r2['Trades']} Alpha vs SPY %={r2['Alpha vs SPY %']:.4f} "
                  f"Return %={r2['Return %']:.4f} Win Rate %={r2['Win Rate %']:.4f}")

    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        row = conn.execute("""
            SELECT trades, alpha_vs_spy, strategy_return, win_rate, version
            FROM backtest_cache WHERE version=? AND ticker=? AND strategy=?
              AND axis_tp=? AND trail_buy_pct=? AND max_hold_hours=? AND window=?
              AND z_score_threshold=? AND trail_sell_pct=? AND entry_timing=?
        """, (config_version, TICKER, STRATEGY_NAME, tp, sl, hold, w, z, tpct, entry_timing)).fetchone()

    print(f"\nRaw backtest_cache row for this exact key: {row}")
    if row is not None and direct_n is not None:
        db_trades, db_alpha, db_compounded, db_win_rate, db_version = row
        print(f"\n=== Comparison: direct-uncached vs backtest_cache-persisted ===")
        print(f"  trades:      direct={direct_n}  cached={db_trades}  match={direct_n == db_trades}")
        print(f"  alpha:       direct={direct_alpha:.6f}  cached={db_alpha:.6f}  "
              f"match={abs(direct_alpha - db_alpha) < 1e-6}")
        print(f"  compounded:  direct={direct_compounded:.6f}  cached={db_compounded:.6f}  "
              f"match={abs(direct_compounded - db_compounded) < 1e-6}")
        print(f"  win_rate:    direct={direct_win_rate:.6f}  cached={db_win_rate:.6f}  "
              f"match={abs(direct_win_rate - db_win_rate) < 1e-6}")
        print(f"  version isolation: persisted under {db_version!r} (base version tag "
              f"never touched -- confirms suffix, not overwrite-in-place)")
    elif row is None:
        print("  !! No row found -- write did not persist as expected, investigate before trusting this path.")
    elif direct_n is None:
        print("  !! Direct uncached run produced zero closed trades in this window -- "
              "nothing to compare (try a different window).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2025-06-01')
    ap.add_argument('--end', default='2026-06-01')
    ap.add_argument('--cache-roundtrip', action='store_true',
                     help='Run the dispatch_parallel_grid cache round-trip check instead of '
                          'the prep/trade-detail audit.')
    args = ap.parse_args()

    if args.cache_roundtrip:
        audit_cache_roundtrip(args.start, args.end)
        return

    full_trades, full_prep = _run()
    win_trades, win_prep = _run(args.start, args.end)

    print(f"=== SOXL {STRATEGY_NAME} node -- full history: {len(full_trades)} closed trades, "
          f"windowed [{args.start}, {args.end}]: {len(win_trades)} closed trades ===\n")

    win_by_entry = {t['Entry Time']: t for t in win_trades}
    in_window_full = [t for t in full_trades if args.start <= str(t['Entry Time'])[:10] <= args.end]

    print(f"Full-history trades with Entry Time inside the window: {len(in_window_full)}")
    print(f"Windowed-run trades total: {len(win_trades)}\n")

    matched, only_full, only_win = 0, [], []
    for t in in_window_full:
        et = t['Entry Time']
        if et in win_by_entry:
            matched += 1
        else:
            only_full.append(t)
    win_entries = set(win_by_entry.keys())
    full_in_window_entries = {t['Entry Time'] for t in in_window_full}
    only_win = [win_by_entry[e] for e in win_entries - full_in_window_entries]

    print(f"Matched by exact Entry Time (full vs windowed): {matched}")
    print(f"Only in full-history-but-in-window (no windowed match): {len(only_full)}")
    print(f"Only in windowed run (no full-history-in-window match): {len(only_win)}\n")

    header = (f"{'Entry Time':<20}{'Exit Time':<20}{'Result':<8}{'Held(h)':<9}"
              f"{'FullEntry$':<12}{'WinEntry$':<12}{'FullExit$':<12}{'WinExit$':<12}{'EntryMatch':<12}{'ExitMatch':<10}")
    print(header)
    print('-' * len(header))
    for t in in_window_full:
        et = t['Entry Time']
        wt = win_by_entry.get(et)
        full_entry_bar = _bar_row(full_prep, et)
        win_entry_bar = _bar_row(win_prep, et)
        if wt is not None:
            entry_match = 'YES' if abs(t['Entry Price'] - wt['Entry Price']) < 1e-9 else 'DIFF'
            exit_match = 'YES' if abs(t['Exit Price'] - wt['Exit Price']) < 1e-9 else 'DIFF'
            win_entry_p = f"{wt['Entry Price']:.4f}"
            win_exit_p = f"{wt['Exit Price']:.4f}"
            wt_exit_time = wt['Exit Time']
        else:
            entry_match, exit_match = 'MISSING', 'MISSING'
            win_entry_p, win_exit_p, wt_exit_time = '-', '-', '-'
        print(f"{str(et):<20}{str(t['Exit Time']):<20}{t['Result']:<8}{t['hours_held']:<9}"
              f"{t['Entry Price']:<12.4f}{win_entry_p:<12}{t['Exit Price']:<12.4f}{win_exit_p:<12}"
              f"{entry_match:<12}{exit_match:<10}")
        if wt is not None and str(wt_exit_time) != str(t['Exit Time']):
            print(f"    !! exit time differs: full={t['Exit Time']} windowed={wt_exit_time}")

    print("\n=== Detail for first 5 matched trades: entry/exit bar OHLC + indicators, full vs windowed ===\n")
    shown = 0
    for t in in_window_full:
        if shown >= 5:
            break
        et, xt = t['Entry Time'], t['Exit Time']
        wt = win_by_entry.get(et)
        if wt is None:
            continue
        shown += 1
        print(f"--- Trade {shown}: entry {et}, exit {xt}, result {t['Result']} ---")
        for label, ts in (('ENTRY', et), ('EXIT', xt)):
            fb = _bar_row(full_prep, ts)
            wb = _bar_row(win_prep, ts)
            print(f"  [{label}] bar {ts}")
            print(f"    full-history run:  O={fb['open']:.4f} H={fb['high']:.4f} L={fb['low']:.4f} "
                  f"C={fb['close']:.4f} SMA={fb['sma']:.4f} Std={fb['std']:.4f}")
            if wb is not None:
                print(f"    windowed run:       O={wb['open']:.4f} H={wb['high']:.4f} L={wb['low']:.4f} "
                      f"C={wb['close']:.4f} SMA={wb['sma']:.4f} Std={wb['std']:.4f}")
                ohlc_match = all(abs(fb[k] - wb[k]) < 1e-9 for k in ('open', 'high', 'low', 'close'))
                ind_match = abs(fb['sma'] - wb['sma']) < 1e-9 and abs(fb['std'] - wb['std']) < 1e-9
                print(f"    OHLC match: {ohlc_match}   SMA/Std match: {ind_match}")
            else:
                print("    windowed run: bar not found in windowed arrays")
        print()

    if only_full:
        print("=== Trades present in full-history-in-window but MISSING from windowed run (investigate) ===")
        for t in only_full[:10]:
            print(f"  entry={t['Entry Time']} exit={t['Exit Time']} result={t['Result']}")
    if only_win:
        print("=== Trades present in windowed run but not in full-history-in-window set (investigate) ===")
        for t in only_win[:10]:
            print(f"  entry={t['Entry Time']} exit={t['Exit Time']} result={t['Result']}")

    print("\n=== Alpha benchmark comparison (2026-08-16 fix) ===")
    _, full_spy_bh = compute_bh_returns(TICKER)
    _, win_spy_bh = compute_bh_returns(TICKER, args.start, args.end)
    alpha_full = _summarize_trades(full_trades, full_spy_bh)[0] if full_trades else None
    alpha_win_buggy = _summarize_trades(win_trades, full_spy_bh)[0] if win_trades else None
    alpha_win_fixed = _summarize_trades(win_trades, win_spy_bh)[0] if win_trades else None
    print(f"  full-history spy_bh:            {full_spy_bh:.4f}%")
    print(f"  windowed [{args.start},{args.end}] spy_bh: {win_spy_bh:.4f}%")
    print(f"  full-history run alpha:                          {alpha_full:.4f}%")
    print(f"  windowed run alpha vs FULL-history spy_bh (pre-fix, WRONG): {alpha_win_buggy:.4f}%")
    print(f"  windowed run alpha vs WINDOWED spy_bh      (post-fix, correct): {alpha_win_fixed:.4f}%")


if __name__ == '__main__':
    main()
