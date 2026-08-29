"""Phase5 (new, 2026-08-28) -- second-level PRECISION check on Phase4's overlay-
adjusted CAGR, mirroring Phase3's own core-trades-only precision check
(scripts/phase3_second_level_check.py) but for the add-on/drought OVERLAY legs
Phase3 deliberately doesn't touch. Runs AFTER Phase4 (GT overlay report), against
that scope's real finalist candidates.

Real head start reused directly, not reimplemented: scripts/
sim_1s_vs_1m_groundtruth_overlays.py already runs the existing, already-validated
GT overlay functions (backtester.apply_addon_overlay_ground_truth,
backtester.simulate_drought_overlay_ground_truth) against BOTH a 1-minute and a
1-second trade list for a single node (its own --overlays flag). This module is
an ORCHESTRATION wrapper: for every real GT Phase4 finalist in a scope (from
run_optimization_sweep.derive_phase25_candidates_ground_truth -- the exact same
function GT Phase4's own report calls, so the finalist set here is identical to
what Phase4 already reported, not re-derived independently), converts the
candidate into the node dict simulate()/report_overlays expect (same axis-column
mapping run_optimization_sweep.build_candidate_report_ground_truth itself uses:
for TrailingBothZScoreBreakout, arm_pct=cand['take_profit'],
trail_buy_pct=cand['stop_loss'], trail_sell_pct=cand['tpct']; for
TrailingExitZScoreBreakout, arm_pct=cand['take_profit'],
trail_sell_pct=cand['stop_loss'], trail_buy_pct=0.0 -- see
run_optimization_sweep.build_candidate_report_ground_truth's own is_both branch),
runs the overlay CAGR at both granularities, and reports the delta -- same
reporting shape/thresholds as Phase3 (mean/median/worst/best delta across the
finalist population, not just a single hand-picked node).

Data (hourly + 1-second, resampled once to 1-minute) is loaded ONCE per ticker,
matching Phase3's own pattern -- not once per candidate, which would repeat the
"slow step" (loading the multi-GB 1-second file) N times for no reason.

Per-candidate checks run in a ProcessPoolExecutor (this project's existing
sweep-worker-pool convention, run_optimization_sweep.py) -- the loaded
DataFrames are set as module-level globals BEFORE the pool is created, so
Linux's default `fork` start method gives every worker copy-on-write access to
the already-loaded data with no per-worker reload (measured: reloading the full
1-second series alone costs ~456s; naive per-worker reloading would have made
parallelizing net-negative).

Scope decision (2026-08-28 design discussion, not written up elsewhere before
this script -- see this file's own docstring as the record, and docs/design.md's
matching entry): run against every real Phase4 finalist if the measured
per-candidate cost is cheap enough (comparable to Phase3's own per-node cost);
otherwise fall back to the top few winners and report the real measured cost so
a human can judge. The FIRST candidate of each scope is always run standalone
(to get a real, uncontended timing measurement) before the scope-wide decision
is made and the (possibly-truncated) remainder dispatched to the pool.
--limit lets a caller force the fallback explicitly.

Usage:
  .venv/bin/python scripts/phase5_second_level_overlay_check.py --ticker SOXL \\
      --version v6-massive-w2021-08-23_2026-08-21
  .venv/bin/python scripts/phase5_second_level_overlay_check.py --ticker SOXL \\
      --strategy TrailingBothZScoreBreakout --fixed-sl 1 --version <version>
  .venv/bin/python scripts/phase5_second_level_overlay_check.py --ticker SOXL \\
      --version <version> --limit 3   # top 3 finalists per scope only
"""
import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import pandas as pd

from run_optimization_sweep import DB_PATH, derive_phase25_candidates_ground_truth
from prune_backtest_cache_ground_truth import discover_all_gt_scopes, _hp_for_strategy
from sim_1s_vs_1m_groundtruth_overlays import (
    load_hourly, load_seconds, resample_seconds_to_minutes, simulate, cagr,
)
from backtester import apply_addon_overlay_ground_truth, simulate_drought_overlay_ground_truth

# Above this measured per-candidate wall-clock cost (seconds), fall back to
# top-few-only instead of the full finalist set, absent an explicit --limit
# override. Chosen as "same order of magnitude as Phase3's own per-node cost"
# per the 2026-08-28 design discussion's own framing ("if it computes fast
# then sure do it to all the finalists") -- Phase3's own per-node cost (one
# 1m + one 1s simulate() call, no overlays) is the reference point; this adds
# two more overlay passes on top of that, so some multiple of that base cost
# is expected and acceptable, not a sign of trouble.
_CHEAP_ENOUGH_SECS_PER_CANDIDATE = 15.0
_FALLBACK_TOP_N = 3

# Set once in main() before the ProcessPoolExecutor is created -- fork (the
# Linux default multiprocessing start method) gives every worker copy-on-write
# access to these without a per-worker reload. Never reassigned inside a
# worker process.
_DFH = None
_DF_1M = None
_DF_1S = None


def node_from_candidate(ticker, strategy_name, entry_timing, fixed_sl, cand):
    """Same axis-column mapping run_optimization_sweep.build_candidate_report_
    ground_truth uses internally (its own is_both branch, right before its
    run_backtest_ground_truth call) -- reused here verbatim, not re-derived,
    so a Phase5 node is guaranteed to mean the same real (arm_pct/trail_buy_pct/
    trail_sell_pct) triple Phase4's own report already computed core/addon/
    drought CAGR from."""
    is_both = strategy_name == "TrailingBothZScoreBreakout"
    if is_both:
        trail_buy_pct, trail_sell_pct = float(cand["stop_loss"]), float(cand["tpct"])
    else:
        trail_buy_pct, trail_sell_pct = 0.0, float(cand["stop_loss"])
    arm_pct = float(cand["take_profit"])
    return dict(
        ticker=ticker, strategy=strategy_name, window=cand["window"],
        z_score_threshold=cand["z_score_threshold"], fixed_sl=fixed_sl,
        arm_pct=arm_pct, arm_sell_pct=arm_pct, take_profit=None,
        trail_buy_pct=trail_buy_pct, trail_sell_pct=trail_sell_pct,
        max_hold_hours=cand["max_hold_hours"], entry_timing=entry_timing,
    )


def overlay_cagrs(trades, ticker, dfh, node, years):
    """Runs the SAME existing, already-validated GT overlay functions
    scripts/sim_1s_vs_1m_groundtruth_overlays.py's report_overlays() calls
    (apply_addon_overlay_ground_truth / simulate_drought_overlay_ground_truth),
    unmodified -- this differs from report_overlays only in RETURNING the
    computed CAGR numbers (for a delta table) instead of only printing them."""
    gt_trades = [t.to_gt_dict(ticker) for t in trades]
    if not gt_trades:
        return None, None, None
    addon_trades = apply_addon_overlay_ground_truth(gt_trades)
    core_bal = addon_bal = 1.0
    for t in addon_trades:
        core_bal *= (1 + t["Return_core"])
        addon_bal *= (1 + t["Return"])
    core_cagr = cagr(core_bal, years, start_bal=1.0)
    addon_cagr = cagr(addon_bal, years, start_bal=1.0)

    drought_cagr = None
    if node["strategy"] == "TrailingBothZScoreBreakout":
        drought = simulate_drought_overlay_ground_truth(
            gt_trades, dfh, ticker, fixed_sl=node["fixed_sl"], arm_pct=node["arm_pct"],
            trail_sell_pct=node["trail_sell_pct"])
        if drought is not None and drought.get("combined_compounded_pct") is not None:
            drought_cagr = cagr(1.0 + drought["combined_compounded_pct"] / 100.0, years, start_bal=1.0)
    return core_cagr, addon_cagr, drought_cagr


def _check_candidate_core(node, dfh, df_1m, df_1s, start, end, years):
    """The actual compute -- shared by the standalone first-candidate timing
    call (main process) and the pool worker below (forked process, reading
    the module-level _DFH/_DF_1M/_DF_1S globals instead of taking them as
    arguments, so they're never re-pickled/re-sent per task)."""
    t0 = time.monotonic()
    trades_1m = simulate(node, dfh, df_1m, start, end, same_bar_reentry=True)
    trades_1s = simulate(node, dfh, df_1s, start, end, same_bar_reentry=True)
    core_1m, addon_1m, drought_1m = overlay_cagrs(trades_1m, node["ticker"], dfh, node, years)
    core_1s, addon_1s, drought_1s = overlay_cagrs(trades_1s, node["ticker"], dfh, node, years)
    elapsed = time.monotonic() - t0
    return {
        "n_trades_1m": len(trades_1m), "n_trades_1s": len(trades_1s),
        "core_cagr_1m": core_1m, "core_cagr_1s": core_1s,
        "addon_cagr_1m": addon_1m, "addon_cagr_1s": addon_1s,
        "drought_cagr_1m": drought_1m, "drought_cagr_1s": drought_1s,
        "addon_delta_pp": None if (addon_1m is None or addon_1s is None) else (addon_1m - addon_1s) * 100,
        "drought_delta_pp": None if (drought_1m is None or drought_1s is None) else (drought_1m - drought_1s) * 100,
        "elapsed_secs": elapsed,
    }


def _pool_worker(cand_idx, node, start, end, years):
    """Runs in a forked worker process -- reads _DFH/_DF_1M/_DF_1S as module-
    level globals (inherited via fork's copy-on-write at pool-creation time,
    set in main() before the ProcessPoolExecutor is created), not as function
    arguments, so the ~450MB+ DataFrames are never pickled/sent over IPC."""
    row = _check_candidate_core(node, _DFH, _DF_1M, _DF_1S, start, end, years)
    row["candidate"] = cand_idx
    row["node"] = node
    return row


def _print_row(row, node):
    print(f"  candidate {row['candidate']}: TP={node['arm_pct']} SL={node['trail_buy_pct']} "
          f"tpct={node['trail_sell_pct']} hold={node['max_hold_hours']}h -- "
          f"core 1m/1s={_fmt(row['core_cagr_1m'])}/{_fmt(row['core_cagr_1s'])}  "
          f"addon 1m/1s={_fmt(row['addon_cagr_1m'])}/{_fmt(row['addon_cagr_1s'])} "
          f"(delta {_fmt_pp(row['addon_delta_pp'])})  "
          f"drought 1m/1s={_fmt(row['drought_cagr_1m'])}/{_fmt(row['drought_cagr_1s'])} "
          f"(delta {_fmt_pp(row['drought_delta_pp'])})  [{row['elapsed_secs']:.1f}s]")


def _fmt(x):
    return "N/A" if x is None else f"{x:.2%}"


def _fmt_pp(x):
    return "N/A" if x is None else f"{x:+.2f}pp"


def run_scope(ticker, strategy_name, version, entry_timing, fixed_sl, dfh, df_1m, df_1s,
              start, end, years, pool, limit=None):
    hp = _hp_for_strategy(strategy_name)
    candidates = derive_phase25_candidates_ground_truth(
        ticker, strategy_name, version, hp, fixed_sl=fixed_sl, entry_timing=entry_timing)
    if not candidates:
        print("  no Phase4 candidates for this scope -- skipping.")
        return []
    if limit is not None:
        candidates = candidates[:limit]

    # First candidate always runs standalone (main process, not the pool) --
    # gives a real, uncontended timing measurement to decide the rest of this
    # scope's scope, per this file's own docstring. Only meaningful when the
    # caller didn't already force --limit.
    first_node = node_from_candidate(ticker, strategy_name, entry_timing, fixed_sl, candidates[0])
    first_row = _check_candidate_core(first_node, dfh, df_1m, df_1s, start, end, years)
    first_row["candidate"] = 1
    _print_row(first_row, first_node)
    rows = [dict(first_row, ticker=ticker, strategy=strategy_name, fixed_sl=fixed_sl)]

    remaining = candidates[1:]
    if limit is None and len(candidates) > _FALLBACK_TOP_N:
        if first_row["elapsed_secs"] > _CHEAP_ENOUGH_SECS_PER_CANDIDATE:
            print(f"  first candidate took {first_row['elapsed_secs']:.1f}s (> "
                  f"{_CHEAP_ENOUGH_SECS_PER_CANDIDATE:.0f}s cheap-enough threshold) -- "
                  f"falling back to top {_FALLBACK_TOP_N} finalists only for this scope.")
            remaining = remaining[:_FALLBACK_TOP_N - 1]
        else:
            print(f"  first candidate took {first_row['elapsed_secs']:.1f}s (<= "
                  f"{_CHEAP_ENOUGH_SECS_PER_CANDIDATE:.0f}s) -- running all "
                  f"{len(candidates)} finalists for this scope.")

    if remaining:
        futures = {}
        for i, cand in enumerate(remaining, start=2):
            node = node_from_candidate(ticker, strategy_name, entry_timing, fixed_sl, cand)
            fut = pool.submit(_pool_worker, i, node, start, end, years)
            futures[fut] = node
        # Collect in submission order (not completion order) so scope output
        # reads in the same TP/SL-descending order Phase4's own report does,
        # even though the underlying work ran in parallel.
        results_by_idx = {}
        for fut in as_completed(futures):
            row = fut.result()
            results_by_idx[row["candidate"]] = (row, futures[fut])
        for i in sorted(results_by_idx):
            row, node = results_by_idx[i]
            _print_row(row, node)
            row.pop("node", None)
            rows.append(dict(row, ticker=ticker, strategy=strategy_name, fixed_sl=fixed_sl))

    return rows


def main():
    global _DFH, _DF_1M, _DF_1S
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--strategy", default=None, help="omit to check every real GT strategy scope")
    ap.add_argument("--entry-timing", default=None, help="omit to check every real GT entry_timing scope")
    ap.add_argument("--fixed-sl", type=float, default=None, help="omit to check every real GT fixed_sl scope")
    ap.add_argument("--data-source", choices=["yahoo", "massive"], default="massive")
    ap.add_argument("--limit", type=int, default=None,
                     help="force top-N finalists per scope instead of the auto cost-based decision")
    ap.add_argument("--workers", type=int, default=6,
                     help="ProcessPoolExecutor worker count (default 6, half of a 12-core box, "
                          "leaves headroom -- this is off-hours ad hoc research compute, not a "
                          "live-daemon-adjacent job, see long-job-launch skill's cap-to-headroom rule)")
    args = ap.parse_args()

    with sqlite3.connect(DB_PATH) as conn:
        all_scopes = discover_all_gt_scopes(conn)
    scopes = [s for s in all_scopes
              if s[0] == args.ticker and s[2] == args.version
              and (args.strategy is None or s[1] == args.strategy)
              and (args.entry_timing is None or s[3] == args.entry_timing)
              and (args.fixed_sl is None or s[4] == args.fixed_sl)]
    if not scopes:
        raise SystemExit(f"No real GT scopes found for ticker={args.ticker} version={args.version} "
                          f"strategy={args.strategy} entry_timing={args.entry_timing} fixed_sl={args.fixed_sl}")
    print(f"Found {len(scopes)} real GT scope(s) to check.")

    t_load = time.monotonic()
    print(f"Loading {args.ticker} hourly data...")
    _DFH = load_hourly(args.ticker, data_source=args.data_source)
    print(f"Loading {args.ticker} 1-second data (this is the slow step)...")
    _DF_1S = load_seconds(args.ticker)
    print(f"  {len(_DF_1S):,} 1-second rows, {_DF_1S.index.min()} -> {_DF_1S.index.max()}")
    _DF_1M = resample_seconds_to_minutes(_DF_1S)
    print(f"  {len(_DF_1M):,} 1-minute rows (resampled from the 1s series above)")
    load_secs = time.monotonic() - t_load
    print(f"Data load: {load_secs:.1f}s")

    start = "2021-08-23"
    end = "2026-08-21"
    bars_for_years = _DFH.loc[start:end + " 23:59:59"]
    years = (bars_for_years.index.max() - bars_for_years.index.min()).total_seconds() / (365.25 * 86400)

    t_run = time.monotonic()
    all_rows = []
    # Pool created AFTER _DFH/_DF_1M/_DF_1S are populated, so fork (Linux
    # default) gives every worker copy-on-write access with no reload.
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for ticker, strategy_name, version, entry_timing, fixed_sl in scopes:
            print(f"\n{'#' * 80}\n{ticker} / {strategy_name} / {version} / "
                  f"entry_timing={entry_timing} / fixed_sl={fixed_sl}\n{'#' * 80}")
            rows = run_scope(ticker, strategy_name, version, entry_timing, fixed_sl,
                              _DFH, _DF_1M, _DF_1S, start, end, years, pool, limit=args.limit)
            all_rows.extend(rows)
    run_secs = time.monotonic() - t_run

    if not all_rows:
        print("\nNo candidates checked -- nothing to summarize.")
        return

    df_out = pd.DataFrame(all_rows)
    out_path = os.path.join(ROOT, "output", f"phase5_second_level_overlay_check_{args.ticker}.csv")
    df_out.to_csv(out_path, index=False)

    print(f"\n=== Phase5 summary: {len(df_out)} candidate(s) checked across {len(scopes)} scope(s) ===")
    for label in ("addon_delta_pp", "drought_delta_pp"):
        s = df_out[label].dropna()
        if s.empty:
            print(f"  {label}: no candidates had both 1m/1s values -- N/A")
            continue
        print(f"  {label}: mean={s.mean():+.2f}pp median={s.median():+.2f}pp "
              f"worst={s.max():+.2f}pp best={s.min():+.2f}pp "
              f"({(s > 0).sum()}/{len(s)} show 1m OPTIMISM)")
    print(f"\nData load: {load_secs:.1f}s | Candidate checks: {run_secs:.1f}s total ({args.workers} workers), "
          f"{run_secs / len(df_out):.1f}s/candidate average (wall-clock, not CPU-time) | "
          f"Total: {load_secs + run_secs:.1f}s")
    print(f"Full results: {out_path}")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
