"""Runs the real drought-overlay AND margin add-on-at-arm backtests
(scripts/drought_overlay_test.py's get_trades_and_bars/find_drought_windows/
simulate_overlay, and scripts/stacked_model/add_on.py's generate_addon_trades
-- all imported directly, not reimplemented) against a ticker's best
backtest_cache node instead of a real watch_list row.

Why this exists: both overlays normally source nodes via
scripts/drought_detection_test.py::load_nodes, which reads watch_list --
so neither can run on a fresh liquidity-screen candidate that hasn't gone
through the checklist/promotion process yet (see docs/watchlist_candidate_checklist.md).
This shim builds the identical node dict shape from a raw backtest_cache
winning row (scripts/locate_best_node.py::node_dict) so the same, already-
proven overlay logic can run on a candidate immediately after its core sweep,
with zero watch_list/live-trading footprint -- pure read-only backtest.
Put-hedge and skim-and-reserve are NOT covered here (skim needs a real
equity-tracking node; put-hedge needs live options-chain data neither of
which apply to a raw candidate pre-promotion).

Each ticker's node is registered once in candidate_nodes (deduped by full
param tuple, scripts/locate_best_node.py::get_or_create_candidate_node) --
the interim id to key against before any real wl_id exists (raised
2026-08-07: "no WL id yet so can't use that"). candidate_overlay_results
rows reference candidate_node_id instead of repeating the node's params.

Usage:
  .venv/bin/python scripts/run_overlay_shim.py TICKER [TICKER ...] [--version v5] [--confirm-days 10]

GT-kernel mode (2026-08-23, run_for_node_ground_truth()): --kernel gt runs an arbitrary
hand-specified node (not a backtest_cache lookup, no resolve_version()) through the v6
ground-truth kernel's own drought/add-on functions (backtester.run_backtest_ground_truth +
simulate_drought_overlay_ground_truth + apply_addon_overlay_ground_truth) instead:
  .venv/bin/python scripts/run_overlay_shim.py TICKER --kernel gt \\
      --strategy TrailingBothZScoreBreakout --window 20 --z 1.5 --sl 2 --arm 29 \\
      --tb 1.0 --ts 4.0 --hold 126 [--entry-timing open_check] \\
      [--start-date 2023-07-24 --end-date 2026-08-21] [--data-source yahoo]
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from scripts.locate_best_node import (
    node_dict, node_from_candidate_id, DB_PATH, ensure_candidate_nodes_table,
    get_or_create_candidate_node, resolve_version,
)
from scripts.drought_overlay_test import get_trades_and_bars, find_drought_windows, simulate_overlay
from scripts.stacked_model.add_on import generate_addon_trades


def ensure_table(conn):
    """Lazily self-managed, same pattern as db_cache.py's data_mutation_log --
    not part of the core schema init_idempotent_db() owns. Candidate-only:
    rows here are read-only backtest smoke-test results against a constructed
    node, never a real watch_list-backed run (those go through the real
    drought/addon overlay tables once a candidate is actually promoted)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidate_overlay_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_timestamp TEXT NOT NULL,
            mechanism TEXT NOT NULL,
            ticker TEXT NOT NULL,
            candidate_node_id INTEGER NOT NULL REFERENCES candidate_nodes(id),
            confirm_days INTEGER,
            entry_time TEXT NOT NULL,
            exit_time TEXT NOT NULL,
            exit_reason TEXT NOT NULL,
            ret REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_candidate_overlay_ticker
        ON candidate_overlay_results(ticker, mechanism, run_timestamp)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_candidate_overlay_node
        ON candidate_overlay_results(candidate_node_id)
    """)
    conn.commit()


def run_drought(node, candidate_node_id, ticker, trades, df_h, confirm_days):
    rows = []
    for entry_i, gap_end in find_drought_windows(trades, df_h, confirm_days):
        result = simulate_overlay(df_h, entry_i, gap_end, node["fixed_sl"], node["arm_pct"],
                                   node["trail_sell_pct"])
        rows.append({
            "mechanism": "drought", "ticker": ticker, "candidate_node_id": candidate_node_id,
            "confirm_days": confirm_days,
            "entry_time": str(df_h.index[entry_i + 1]), "exit_time": str(df_h.index[result["exit_i"]]),
            "exit_reason": result["exit_reason"], "ret": result["ret"],
        })
    return rows


def run_addon(node, candidate_node_id, ticker, trades, df_h):
    rows = []
    for t in generate_addon_trades(trades, df_h):
        rows.append({
            "mechanism": "addon", "ticker": ticker, "candidate_node_id": candidate_node_id,
            "confirm_days": None,
            "entry_time": str(df_h.index[t["entry_i"]]), "exit_time": str(df_h.index[t["exit_i"]]),
            "exit_reason": t["exit_reason"], "ret": t["ret"],
        })
    return rows


def run_for_node(conn, ticker, node, confirm_days, mechanisms=("drought", "addon")):
    """Runs the overlay backtest(s) against an ARBITRARY, already-built node
    dict (not necessarily the ticker's single auto-picked best) -- added
    2026-08-08 (later) so candidate_summary_report.py can compute overlay
    results on demand for all 3 candidate-selection types (safe/unsafe/
    possible), not just whichever one happens to already be registered.
    `mechanisms` lets a caller compute only the missing one(s) instead of
    re-running (and re-inserting) a mechanism that's already in the DB."""
    try:
        trades, df_h = get_trades_and_bars(node)
    except Exception as e:
        print(f"{ticker}: failed ({e})")
        return []
    if len(trades) < 2:
        print(f"{ticker}: too few real trades ({len(trades)}) to evaluate overlays")
        return []

    candidate_node_id = get_or_create_candidate_node(conn, node)
    rows = []
    if "drought" in mechanisms:
        rows += run_drought(node, candidate_node_id, ticker, trades, df_h, confirm_days)
    if "addon" in mechanisms:
        rows += run_addon(node, candidate_node_id, ticker, trades, df_h)
    return rows


def run_for_node_ground_truth(ticker, node, start_date, end_date, data_source="yahoo",
                               confirm_days_grid=None, vol_gate_grid=None):
    """GT-native equivalent of run_for_node() above, for an ARBITRARY node (not limited
    to Phase2.5-GT shortlisted candidates) -- added 2026-08-23 alongside the GT-kernel
    drought/add-on work (backtester.simulate_drought_overlay_ground_truth,
    backtester.apply_addon_overlay_ground_truth, both built for run_optimization_sweep.py's
    per-candidate Phase2.5-GT report and generic w.r.t. its shortlist pipeline). This just
    wires the same two functions up for a hand-specified node instead.

    Deliberately does NOT go through locate_best_node.resolve_version() -- that function
    hard-errors on any ticker with real kernel_version='ground_truth_v6' backtest_cache rows
    specifically to stop a caller from silently falling back to stale legacy v5/v5.1 data
    (see its 2026-08-23 guard). This path never queries backtest_cache by version at all --
    `node`'s params are supplied directly by the caller (CLI args here) and fed straight to
    run_backtest_ground_truth, so there is no version string to resolve and nothing for that
    guard to catch or need to route around.

    Drought is gated on strategies.uses_arm_trail_exit(node['strategy']) (generalized
    2026-09-02 -- simulate_drought_overlay_ground_truth's own docstring no longer claims a
    TrailingBoth-only restriction; the real gate is a capability flag on whichever strategy
    actually implements the fixed-SL-then-arm-then-trail check_exit the overlay's own
    position-management reuses, currently True for TrailingBoth/TrailingExit) -- this
    wrapper skips the drought call (printing why) for any strategy that flag is False for,
    rather than calling it incorrectly. Add-on has no such restriction (matches run_addon()'s
    own unconditional call for both strategies above).

    same_bar_reentry=True (2026-08-23, paired-review CONFIRMED HIGH finding): matches every
    real GT reference path this wrapper claims to mirror -- run_optimization_sweep.py's own
    per-candidate drought/add-on report (build_candidate_report_ground_truth, ~line 2974) and
    GT Phase1 (run_ground_truth_phase1.py) both pass True. run_optimization_sweep.py:3085-3092
    explicitly documents that the drought core list was fixed to True this month specifically
    to stop it disagreeing with "the old post-hoc drought script's" same False choice this
    wrapper had first drafted -- reusing False here would have silently regressed that exact
    fix and made this tool's own core-trade count/compounded figures disagree with the
    candidate report for the identical node.

    addon_compounded_pct (2026-08-23, paired-review CONFIRMED HIGH finding): computed over
    ALL of apply_addon_overlay_ground_truth(trades) (armed trades carry the blended return,
    unarmed trades keep their own core Return unchanged), exactly like the canonical consumer
    (run_optimization_sweep.py:~2567) -- NOT over the armed-only subset. Filtering to
    addon_applied==True first (an earlier draft's bug) silently drops every SL/unarmed-TIME
    loss from the compounding, systematically inflating the number. The armed-only subset
    (`addon_trades` in the returned dict) is still used for the n=/mean_ret/win_rate
    descriptive stats -- those are legitimately about the add-on leg's own trades -- just not
    for the compounded total, which must reflect the whole strategy.

    return_below_floor handling (2026-08-23, paired-review CONFIRMED HIGH finding):
    apply_addon_overlay_ground_truth's own docstring and run_optimization_sweep.py:2576-2580
    establish a hard convention -- if ANY trade breaches the -100% floor, addon compounded
    stats must NOT be aggregated (a single such trade can flip prod(1+r)'s sign into a
    meaningless number with no exception raised). addon_compounded_pct is forced to None
    (with addon_below_floor_count still reported honestly) whenever that happens, matching
    that convention instead of silently printing a poisoned number.

    Returns None (after printing why) if the ticker has no hourly data for data_source, the
    windowed frame is empty, or fewer than 2 real core GT trades result. Otherwise a dict:
    {ticker, n_core_trades, addon_trades (list of dicts, addon_applied==True only -- for the
    descriptive stats below), addon_mean_ret, addon_win_rate, addon_compounded_pct (over the
    FULL trade list, or None if addon_below_floor_count > 0), addon_below_floor_count, drought
    (simulate_drought_overlay_ground_truth's own result dict, or None if strategies.
    uses_arm_trail_exit(node['strategy']) is False)}."""
    import pandas as pd
    import run_optimization_sweep as ros
    import strategies as strategies_mod
    from backtester import (
        run_backtest_ground_truth, apply_addon_overlay_ground_truth,
        simulate_drought_overlay_ground_truth,
    )

    strategy_class = getattr(strategies_mod, node["strategy"])
    is_both = node["strategy"] == "TrailingBothZScoreBreakout"
    supports_drought = strategies_mod.uses_arm_trail_exit(node["strategy"])

    inputs = ros._load_node_inputs_ground_truth(
        ticker, strategy_class, node["strategy"], int(node["window"]), float(node["z"]),
        start_date, end_date, data_source=data_source)
    if inputs is None:
        print(f"{ticker}: no hourly data available for data_source={data_source}")
        return None
    _, df_daily_processed, minute_df, df_hourly_windowed, prep, mprep, _actual_fill_res = inputs
    if df_hourly_windowed.empty:
        print(f"{ticker}: empty windowed hourly frame for {start_date}..{end_date}")
        return None

    trades = run_backtest_ground_truth(
        df_hourly_windowed, df_daily_processed, ticker, minute_df,
        fixed_sl=float(node["fixed_sl"]), arm_pct=float(node["arm_pct"]),
        trail_buy_pct=float(node["trail_buy_pct"]), trail_sell_pct=float(node["trail_sell_pct"]),
        max_hours_to_hold=int(node["max_hold_hours"]), z_score_threshold=float(node["z"]),
        is_both=is_both, open_check_entry_timing=(node["entry_timing"] == "open_check"),
        same_bar_reentry=True, prep=prep, mprep=mprep, need_times=True,
    )
    if len(trades) < 2:
        print(f"{ticker}: too few real GT core trades ({len(trades)}) to evaluate overlays")
        return None

    result = {"ticker": ticker, "n_core_trades": len(trades)}

    all_addon_trades = apply_addon_overlay_ground_truth(trades)
    addon_trades = [t for t in all_addon_trades if t["addon_applied"]]
    result["addon_trades"] = addon_trades
    below_floor_count = int(sum(t["return_below_floor"] for t in all_addon_trades))
    result["addon_below_floor_count"] = below_floor_count
    if addon_trades:
        armed_rets = pd.Series([t["Return"] for t in addon_trades])
        result["addon_mean_ret"] = float(armed_rets.mean())
        result["addon_win_rate"] = float((armed_rets > 0).mean())
        if below_floor_count > 0:
            # A floor breach can flip prod(1+r)'s sign into a meaningless number with no
            # exception raised -- never aggregate through it (see docstring above).
            result["addon_compounded_pct"] = None
        else:
            all_rets = pd.Series([t["Return"] for t in all_addon_trades])
            result["addon_compounded_pct"] = float((all_rets + 1).prod() - 1) * 100
    else:
        result["addon_mean_ret"] = result["addon_win_rate"] = result["addon_compounded_pct"] = None

    if supports_drought:
        result["drought"] = simulate_drought_overlay_ground_truth(
            trades, df_hourly_windowed, ticker, fixed_sl=float(node["fixed_sl"]),
            arm_pct=float(node["arm_pct"]), trail_sell_pct=float(node["trail_sell_pct"]),
            confirm_days_grid=confirm_days_grid, vol_gate_grid=vol_gate_grid)
    else:
        result["drought"] = None
        print(f"{ticker}: strategy={node['strategy']} does not support the drought overlay's "
              f"arm-then-trail exit shape (strategies.uses_arm_trail_exit=False) -- skipping")

    return result


def run_for_ticker(conn, ticker, version, confirm_days):
    resolved = version or resolve_version(conn, ticker)
    node = node_dict(conn, ticker, resolved)
    if node is None:
        print(f"{ticker}: no backtest_cache data, skipping")
        return []
    return run_for_node(conn, ticker, node, confirm_days)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--version", default=None,
                     help="force a single version. Default: auto-resolve per ticker "
                          "(v5.1 when the ticker has it, else v5), matching "
                          "candidate_full_review.py's convention.")
    ap.add_argument("--confirm-days", type=int, default=10)
    ap.add_argument("--node-id", type=int, default=None,
                     help="run against this exact candidate_nodes id instead of "
                          "re-deriving 'best' -- use when you need to match a specific "
                          "row a report already displays (best_row()'s own selection "
                          "can legitimately disagree with candidate_full_review.py's "
                          "per-candidate-type node picks). Mutually exclusive with "
                          "passing tickers.")
    ap.add_argument("--out", default=None)
    ap.add_argument("--kernel", choices=["legacy", "gt"], default="legacy",
                     help="legacy (default): existing backtest_cache-based path "
                          "(drought_overlay_test/stacked_model.add_on, "
                          "resolve_version()-scoped). gt: v6 ground-truth kernel path "
                          "(run_backtest_ground_truth + simulate_drought_overlay_ground_"
                          "truth + apply_addon_overlay_ground_truth) for an arbitrary "
                          "hand-specified node -- no backtest_cache lookup, no "
                          "resolve_version(). Requires exactly one ticker plus "
                          "--strategy/--window/--z/--sl/--arm/--tb/--ts/--hold.")
    ap.add_argument("--strategy", default=None, help="--kernel gt only")
    ap.add_argument("--window", type=int, default=None, help="--kernel gt only")
    ap.add_argument("--z", type=float, default=None, help="--kernel gt only")
    ap.add_argument("--sl", type=float, default=None, help="fixed_sl, --kernel gt only")
    ap.add_argument("--arm", type=float, default=None, help="arm_pct, --kernel gt only")
    ap.add_argument("--tb", type=float, default=None, help="trail_buy_pct, --kernel gt only")
    ap.add_argument("--ts", type=float, default=None, help="trail_sell_pct, --kernel gt only")
    ap.add_argument("--hold", type=int, default=None, help="max_hold_hours, --kernel gt only")
    ap.add_argument("--entry-timing", default="open_check", choices=["close", "open_check"],
                     help="--kernel gt only")
    ap.add_argument("--start-date", default=None, help="--kernel gt only")
    ap.add_argument("--end-date", default=None, help="--kernel gt only")
    ap.add_argument("--data-source", default="yahoo", choices=["yahoo", "massive"],
                     help="--kernel gt only")
    args = ap.parse_args()

    if args.kernel == "gt":
        if len(args.tickers) != 1:
            print("--kernel gt requires exactly one ticker positional arg")
            sys.exit(1)
        required = {"strategy": args.strategy, "window": args.window, "z": args.z,
                    "sl": args.sl, "arm": args.arm, "tb": args.tb, "ts": args.ts,
                    "hold": args.hold}
        missing = [k for k, v in required.items() if v is None]
        if missing:
            print(f"--kernel gt requires: {', '.join('--' + m for m in missing)}")
            sys.exit(1)
        ticker = args.tickers[0]
        node = {
            "strategy": args.strategy, "window": args.window, "z": args.z,
            "fixed_sl": args.sl, "arm_pct": args.arm, "trail_buy_pct": args.tb,
            "trail_sell_pct": args.ts, "max_hold_hours": args.hold,
            "entry_timing": args.entry_timing,
        }
        result = run_for_node_ground_truth(ticker, node, args.start_date, args.end_date,
                                            data_source=args.data_source)
        if result is None:
            return
        print(f"\n{ticker}: {result['n_core_trades']} core GT trades")
        d = result["drought"]
        if d is not None:
            drought_str = "n/a" if d['drought_compounded_pct'] is None else f"{d['drought_compounded_pct']:+.1f}%"
            combined_str = "n/a" if d['combined_compounded_pct'] is None else f"{d['combined_compounded_pct']:+.1f}%"
            print(f"drought: confirm_days={d['best_confirm_days']} vol_gate={d['best_vol_gate']} "
                  f"windows={d['n_drought_windows']} simulated={d['n_drought_simulated']} "
                  f"core={d['core_compounded_pct']:+.1f}% "
                  f"drought={drought_str} combined={combined_str}")
        if result["addon_trades"]:
            if result["addon_compounded_pct"] is None:
                compounded_str = f"n/a (return_below_floor breach x{result['addon_below_floor_count']} -- not aggregated)"
            else:
                compounded_str = f"{result['addon_compounded_pct']:.1f}%"
            print(f"addon: n={len(result['addon_trades'])} "
                  f"mean_ret={result['addon_mean_ret']*100:.2f}% "
                  f"win_rate={result['addon_win_rate']:.3f} "
                  f"compounded={compounded_str} "
                  f"below_floor={result['addon_below_floor_count']}")
        else:
            print("addon: no armed core trades -- nothing to evaluate")
        if args.out:
            pd.DataFrame(result["addon_trades"]).to_csv(args.out, index=False)
            print(f"\nWrote {args.out}")
        return

    conn = sqlite3.connect(DB_PATH)
    ensure_candidate_nodes_table(conn)
    ensure_table(conn)
    all_rows = []
    if args.node_id is not None:
        node = node_from_candidate_id(conn, args.node_id)
        if node is None:
            print(f"no candidate_nodes row with id={args.node_id}")
            sys.exit(1)
        all_rows += run_for_node(conn, node["ticker"], node, args.confirm_days)
    else:
        for t in args.tickers:
            all_rows += run_for_ticker(conn, t, args.version, args.confirm_days)

    if not all_rows:
        print("No drought/addon overlay trades found for any requested ticker.")
        return

    run_ts = datetime.now().isoformat(timespec="seconds")
    conn.executemany("""
        INSERT INTO candidate_overlay_results
            (run_timestamp, mechanism, ticker, candidate_node_id,
             confirm_days, entry_time, exit_time, exit_reason, ret)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [(run_ts, r["mechanism"], r["ticker"], r["candidate_node_id"],
           r["confirm_days"], r["entry_time"], r["exit_time"], r["exit_reason"], r["ret"])
          for r in all_rows])
    conn.commit()
    print(f"\nWrote {len(all_rows)} rows to candidate_overlay_results (run_timestamp={run_ts})")

    df = pd.DataFrame(all_rows)
    pd.set_option("display.width", 160)
    for mech, sub in df.groupby("mechanism"):
        print(f"\n--- {mech} pooled (n={len(sub)} trades across {sub['ticker'].nunique()} tickers) ---")
        print(f"mean_ret={sub['ret'].mean()*100:.2f}%  median_ret={sub['ret'].median()*100:.2f}%  "
              f"win_rate={(sub['ret'] > 0).mean():.3f}  "
              f"compounded={(np.prod(1 + sub['ret']) - 1)*100:.1f}%")

        print(f"--- {mech} per-ticker ---")
        per_ticker = sub.groupby("ticker").agg(
            n=("ret", "size"), mean_ret=("ret", "mean"),
            win_rate=("ret", lambda s: (s > 0).mean()),
            compounded=("ret", lambda s: float(np.prod(1 + s) - 1)),
        ).round(4)
        print(per_ticker.to_string())

    if args.out:
        df.to_csv(args.out, index=False)
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
