"""Rebuild real trade sequences for already-promoted candidate_nodes rows and persist
them into backtest_winner_trades -- the "targeted lookback" tool for the schema-v2
design's core claim: nothing needs to be captured inline during a sweep, because
regenerating a single winner's trades on demand is cheap (a handful of seconds, not the
Phase1/Phase2 cost). Built 2026-08-27 specifically to backfill SOXL fixed_sl=3's 9
winners, whose trades were captured in-memory during scripts/bench_phase1_phase2_
inmemory.py's run but never persisted (no backtest_winner_trades table existed yet at
the time) -- lost when that process exited. Reusable for any (ticker, strategy,
version, fixed_sl) scope already in candidate_nodes, not just that one backfill.

Usage: .venv/bin/python scripts/rebuild_winner_trades.py --ticker SOXL \\
    --strategy TrailingBothZScoreBreakout --version bench-inmemory-v6-massive-w2021-08-23_2026-08-21 \\
    --fixed-sl 3
"""
import argparse
import os
import re
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from run_optimization_sweep import DB_PATH, _load_node_inputs_ground_truth
from backtester import run_backtest_ground_truth
from node_key import node_key
import strategies
from phase4_candidate_nodes_resolver import _stop_loss_and_tpct_from_row


def _window_dates_from_version(version):
    """Inlined from scripts/candidate_summary_report.py's own helper (same regex,
    same 'last match' rule for double-suffixed versions) -- NOT imported directly
    because that module pulls in a currently-broken dependency chain
    (verify_fill_resolution_accuracy -> verify_trailing_buy_resolution.replay_five_min,
    a pre-existing bug unrelated to this script)."""
    if not version:
        return None, None
    matches = re.findall(r"-w(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})", version)
    if not matches:
        return None, None
    return matches[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--strategy", required=True, choices=[
        "TrailingBothZScoreBreakout", "TrailingExitZScoreBreakout"])
    ap.add_argument("--version", required=True)
    ap.add_argument("--fixed-sl", dest="fixed_sl", type=float, required=True)
    args = ap.parse_args()

    start_date, end_date = _window_dates_from_version(args.version)
    if start_date is None:
        raise SystemExit(f"--version={args.version!r} has no -w{{start}}_{{end}} suffix -- "
                          f"this tool requires a windowed version so the real backtest "
                          f"window is unambiguous, not guessed.")
    data_source = "massive" if "-massive" in args.version else "yahoo"
    entry_timing = "open_check"

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT window, z, arm_pct, trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing
            FROM candidate_nodes
            WHERE ticker=? AND strategy=? AND version=? AND fixed_sl=?
        """, (args.ticker, args.strategy, args.version, args.fixed_sl)).fetchall()

    if not rows:
        raise SystemExit(f"No candidate_nodes rows for ticker={args.ticker} strategy={args.strategy} "
                          f"version={args.version} fixed_sl={args.fixed_sl} -- nothing to rebuild.")
    print(f"Found {len(rows)} candidate_nodes rows to rebuild trades for.")

    strategy_class = getattr(strategies, args.strategy)
    is_both = args.strategy == 'TrailingBothZScoreBreakout'

    buffer = []
    t0 = time.time()
    for window, z, arm_pct, trail_buy_pct, trail_sell_pct, max_hold_hours, row_entry_timing in rows:
        inputs = _load_node_inputs_ground_truth(args.ticker, strategy_class, args.strategy,
                                                  window, z, start_date, end_date,
                                                  data_source=data_source)
        _, df_daily_processed, minute_df, df_hourly_windowed, prep, mprep = inputs
        if is_both:
            trail_buy_pct_arg, trail_sell_pct_arg, arm_pct_arg = trail_buy_pct, trail_sell_pct, arm_pct
        else:
            trail_buy_pct_arg, trail_sell_pct_arg, arm_pct_arg = 0.0, trail_sell_pct, arm_pct
        trades = run_backtest_ground_truth(
            df_hourly_windowed, df_daily_processed, args.ticker, minute_df,
            fixed_sl=args.fixed_sl, arm_pct=arm_pct_arg, trail_buy_pct=trail_buy_pct_arg,
            trail_sell_pct=trail_sell_pct_arg, max_hours_to_hold=max_hold_hours,
            z_score_threshold=z, is_both=is_both,
            open_check_entry_timing=(row_entry_timing == 'open_check'),
            same_bar_reentry=True, prep=prep, mprep=mprep, need_times=True,
        )
        # node_key() takes (take_profit, stop_loss, trail_sell_pct) in the STRATEGY-
        # NEUTRAL axis meaning build_params_dict/resolve_axis_columns expect -- NOT the
        # raw candidate_nodes storage columns (arm_pct, trail_buy_pct, trail_sell_pct)
        # unpacked here. Passing the raw columns straight through is correct BY
        # COINCIDENCE for TrailingBoth (its sl_axis mapping is an identity, trail_buy_pct
        # IS stop_loss) but WRONG for TrailingExit (resolve_axis_columns returns
        # ('trail_pct', None) there, so the real stop_loss lives in trail_sell_pct, not
        # trail_buy_pct -- candidate_nodes always stores trail_buy_pct=0.0 for a
        # TrailingExit row, see build_candidate_report_ground_truth's own is_both branch
        # -- an unfixed call here would silently bake stop_loss=0.0 into every
        # TrailingExit node_key, both making those rows permanently invisible to
        # run_optimization_sweep.get_cached_trades' read-back (dead cache -- a real
        # regression this script's dead cache would otherwise cause, found 2026-08-29
        # paired-review MEDIUM finding against the trades-cache read-back task) AND
        # colliding multiple distinct TrailingExit candidates onto ONE node_key under
        # backtest_winner_trades' UNIQUE(node_key, version, trade_idx) constraint,
        # interleaving different candidates' trades into a single stored list. Use the
        # same inverse mapping phase4_candidate_nodes_resolver.py's own forward mapping
        # (and build_candidate_report_ground_truth's is_both branch) already establish,
        # not a third independent derivation.
        sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(args.strategy)
        real_stop_loss, real_tpct = _stop_loss_and_tpct_from_row(
            sl_axis_col, fourth_axis_col, trail_buy_pct, trail_sell_pct)
        nk = node_key(args.strategy, args.ticker, args.fixed_sl, window, z, max_hold_hours,
                       arm_pct, real_stop_loss, real_tpct, row_entry_timing,
                       strategies.resolve_axis_columns)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
        for i, t in enumerate(trades):
            buffer.append((nk, args.version, args.ticker, args.strategy, args.fixed_sl, i,
                           str(t['Entry Time']), t['Entry Price'], str(t['Exit Time']),
                           t['Exit Price'], t['exit_reason'], t['Return'], int(t['armed']),
                           str(t['Arm Time']) if t['armed'] else None, t['Arm Price'], now_iso))
    t1 = time.time()
    print(f"Regenerated {len(buffer):,} trade rows across {len(rows)} candidates in {t1 - t0:.2f}s")

    with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_winner_trades (
                node_key TEXT, version TEXT, ticker TEXT, strategy TEXT, fixed_sl REAL,
                trade_idx INTEGER, entry_time TEXT, entry_price REAL, exit_time TEXT,
                exit_price REAL, exit_reason TEXT, return_pct REAL, armed INTEGER,
                arm_time TEXT, arm_price REAL, created_at TEXT,
                UNIQUE(node_key, version, trade_idx)
            )""")
        before = conn.execute(
            "SELECT COUNT(*) FROM backtest_winner_trades WHERE version=? AND ticker=? AND strategy=?",
            (args.version, args.ticker, args.strategy)).fetchone()[0]
        conn.executemany("""
            INSERT OR IGNORE INTO backtest_winner_trades
                (node_key, version, ticker, strategy, fixed_sl, trade_idx, entry_time,
                 entry_price, exit_time, exit_price, exit_reason, return_pct, armed,
                 arm_time, arm_price, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, buffer)
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) FROM backtest_winner_trades WHERE version=? AND ticker=? AND strategy=?",
            (args.version, args.ticker, args.strategy)).fetchone()[0]
    print(f"Written to backtest_winner_trades: {after - before} new rows "
          f"({len(buffer) - (after - before)} already existed).")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
