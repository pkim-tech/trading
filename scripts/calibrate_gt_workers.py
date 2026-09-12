"""Empirically calibrates a safe --workers ceiling for run_addon_cliff_safety_ground_
truth's real ProcessPoolExecutor (candidate_summary_report.py's run_gt_mode --kernel gt
path), tiered by a ticker's real (active-build) massive_second_derived row count.

Built after --workers 8 OOM-killed a real run_gt_mode scope on SOXL (2026-09-11) -- a
per-worker transient-array memory-scaling issue in the per-candidate GT resim, distinct
from coder4's preload-before-fork fix (_run_addon_cliff_cell_isolated's own preload,
validated separately on AGQ). This script does NOT touch run_optimization_sweep.py or
candidate_summary_report.py -- it imports and calls their real, already-preloaded,
already-picklable per-cell worker (_run_addon_cliff_cell_isolated) directly, in a small
isolated ProcessPoolExecutor, instead of running the whole (much slower, tens-of-
minutes-per-datapoint) report pipeline.

Row-count note (2026-09-11): massive_second_derived is multi-vintage (build_id, see
active_builds) -- a bare `GROUP BY ticker` over the whole table sums every historical
build, not just the active one. Real active-build counts are far smaller than that
naive count suggested (SOXL: 22.2M active vs 88.7M summed-across-builds). Always join
against active_builds when reporting a ticker's real row count.

Preloads the ticker's real hourly + second-resolution dataframes ONCE in this (parent)
process before creating the pool, exactly as run_addon_cliff_safety_ground_truth's own
preload-before-fork step does, so forked workers inherit them via copy-on-write instead
of each loading an independent copy. For each --workers value, submits N copies of the
SAME real candidate coordinate concurrently and watches summed worker RSS via psutil
(same watchdog rationale as before: kill this one attempt early -- before a real OOM --
if projected memory use would exceed headroom, log "would have OOM'd", move on).

Usage:
  .venv/bin/python scripts/calibrate_gt_workers.py --ticker AGQ --try 8 10
  .venv/bin/python scripts/calibrate_gt_workers.py --ticker SOXL --try 8 6 4 2
  .venv/bin/python scripts/calibrate_gt_workers.py --report
"""
import argparse
import json
import multiprocessing
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import psutil

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
DB_PATH = _ROOT / "cache" / "research" / "trading_universe.db"
RESULTS_PATH = _ROOT / "output" / "calibrate_gt_workers_results.json"

# Fraction of total system memory left as headroom -- this is a shared dev machine.
MEMORY_HEADROOM_FRACTION = 0.15
POLL_INTERVAL_SECONDS = 0.5

# One real candidate config per tier ticker (candidate_nodes, queried 2026-09-11 --
# any real row is fine here, doesn't need to be a good/finalist candidate, just
# realistic params so the resim does real work). tp/sl/tpct follow strategies.
# resolve_axis_columns's convention (see run_single_backtest_node_ground_truth_
# isolated's own docstring): TrailingBoth sweeps trail_buy_pct(=sl)/arm(=tp)/
# trail_sell_pct(=tpct); TrailingExit sweeps trail_sell_pct(=sl)/arm(=tp), tpct unused.
CANDIDATES = {
    "AGQ": dict(strategy="TrailingBothZScoreBreakout", fixed_sl=4.0, w=15, z=0.5,
                hold=77, tp=27.0, sl=4.0, tpct=2.0, entry_timing="open_check",
                data_source="massive"),
    "TNA": dict(strategy="TrailingExitZScoreBreakout", fixed_sl=3.0, w=20, z=1.0,
                hold=35, tp=23.0, sl=1.0, tpct=1.0, entry_timing="open_check",
                data_source="massive"),
    "SOXL": dict(strategy="TrailingExitZScoreBreakout", fixed_sl=8.0, w=5, z=1.5,
                 hold=35, tp=27.0, sl=3.0, tpct=3.0, entry_timing="open_check",
                 data_source="massive"),
}


def real_active_row_count(ticker):
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.execute("""
            SELECT COUNT(*) FROM massive_second_derived m
            JOIN active_builds ab ON ab.ticker = m.ticker AND ab.table_name = 'second'
                                  AND ab.build_id = m.build_id
            WHERE m.ticker = ?
        """, (ticker,))
        return cur.fetchone()[0]
    finally:
        conn.close()


def _build_cell_args(ticker):
    cfg = CANDIDATES[ticker]
    import run_optimization_sweep as ros
    _, spy_bh = ros.compute_bh_returns(ticker, data_source=cfg["data_source"])
    return (ticker, cfg["strategy"], cfg["tp"], cfg["sl"], cfg["hold"], cfg["w"], cfg["z"],
            cfg["fixed_sl"], cfg["tpct"], cfg["entry_timing"], None, None, spy_bh, None,
            cfg["data_source"], True, "second")


def _preload(ticker):
    """Preload-before-fork, same call shape run_addon_cliff_safety_ground_truth uses."""
    import run_optimization_sweep as ros
    cfg = CANDIDATES[ticker]
    ros._load_hourly_df_ground_truth(ticker, data_source=cfg["data_source"])
    ros._load_second_df(ticker, data_source=cfg["data_source"])


def _proc_tree_rss(proc):
    """Summed PSS (proportional set size), not raw RSS. After the preload-before-fork
    step, every worker inherits the same large second-resolution dataframe via
    copy-on-write -- naive RSS summed across N forked children counts those shared
    pages N times over, wildly overstating real physical memory use (confirmed
    2026-09-11: an 8-worker SOXL attempt reported ~19.5GB tree RSS at t=0.1s, before
    any worker could have done real per-candidate divergent work -- matches 8x a
    single ~2.4GB preloaded frame almost exactly, not a real OOM risk). PSS divides
    each shared page's cost across the processes sharing it, so summing PSS across
    the tree gives the real total physical footprint without double-counting."""
    try:
        procs = [proc] + proc.children(recursive=True)
    except psutil.NoSuchProcess:
        return 0
    total = 0
    for p in procs:
        try:
            total += p.memory_full_info().pss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return total


def run_one_attempt(ticker, workers):
    """Submits `workers` copies of the same real cell to a fresh ProcessPoolExecutor,
    watching summed RSS of this process + its pool workers under the psutil watchdog.
    Returns a dict: {ticker, workers, outcome: 'ok'|'would_oom'|'error', peak_rss_bytes,
    elapsed_seconds}."""
    total_mem = psutil.virtual_memory().total
    headroom_bytes = int(total_mem * MEMORY_HEADROOM_FRACTION)
    this_proc = psutil.Process()
    baseline_other_used = psutil.virtual_memory().used - _proc_tree_rss(this_proc)

    print(f"[{ticker} workers={workers}] preloading...", flush=True)
    _preload(ticker)
    cell_args = _build_cell_args(ticker)

    print(f"[{ticker} workers={workers}] dispatching {workers} concurrent cell(s)...",
          flush=True)
    start = time.monotonic()
    peak_rss = 0
    outcome = "ok"
    error_text = None
    ctx = multiprocessing.get_context("fork")
    try:
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            futures = [pool.submit(_run_cell_isolated_entrypoint, cell_args)
                       for _ in range(workers)]
            while True:
                done = all(f.done() for f in futures)
                tree_rss = _proc_tree_rss(this_proc)
                peak_rss = max(peak_rss, tree_rss)
                projected_used = baseline_other_used + tree_rss
                if projected_used >= total_mem - headroom_bytes:
                    print(f"[{ticker} workers={workers}] WOULD HAVE OOM'D -- "
                          f"tree_rss={tree_rss/1e9:.2f}GB other_used="
                          f"{baseline_other_used/1e9:.2f}GB total={total_mem/1e9:.2f}GB "
                          f"headroom={headroom_bytes/1e9:.2f}GB. Killing early.", flush=True)
                    for f in futures:
                        f.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    outcome = "would_oom"
                    break
                if done:
                    break
                time.sleep(POLL_INTERVAL_SECONDS)
            if outcome == "ok":
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception as e:
                        outcome = "error"
                        error_text = repr(e)
    except Exception as e:
        outcome = "error"
        error_text = repr(e)

    elapsed = time.monotonic() - start
    result = {
        "ticker": ticker, "workers": workers, "outcome": outcome,
        "peak_rss_bytes": peak_rss, "elapsed_seconds": round(elapsed, 1),
    }
    if error_text:
        result["error"] = error_text
    print(f"[{ticker} workers={workers}] outcome={outcome} "
          f"peak_rss={peak_rss/1e9:.2f}GB elapsed={elapsed:.1f}s", flush=True)
    return result


def _run_cell_isolated_entrypoint(args):
    from run_optimization_sweep import _run_addon_cliff_cell_isolated
    return _run_addon_cliff_cell_isolated(args)


def load_results():
    if RESULTS_PATH.exists():
        return json.loads(RESULTS_PATH.read_text())
    return []


def save_result(result):
    results = load_results()
    results.append(result)
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(results, indent=2))


def print_report():
    results = load_results()
    if not results:
        print("No results yet.")
        return
    by_ticker = {}
    for r in results:
        by_ticker.setdefault(r["ticker"], []).append(r)

    print(f"{'ticker':<8}{'active_rows':>14}{'safe_max_workers':>18}{'peak_rss_gb':>14}")
    for ticker, attempts in by_ticker.items():
        rows = real_active_row_count(ticker)
        ok_attempts = [a for a in attempts if a["outcome"] == "ok"]
        safe_max = max((a["workers"] for a in ok_attempts), default=None)
        peak_at_safe = next((a["peak_rss_bytes"] for a in ok_attempts
                             if a["workers"] == safe_max), 0)
        print(f"{ticker:<8}{rows:>14}{str(safe_max):>18}{peak_at_safe/1e9:>13.2f}G")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", choices=sorted(CANDIDATES), help="ticker to calibrate")
    ap.add_argument("--try", dest="workers_values", type=int, nargs="+",
                     help="--workers values to attempt, in order")
    ap.add_argument("--report", action="store_true", help="print results table and exit")
    args = ap.parse_args()

    if args.report:
        print_report()
        return

    if not args.ticker or not args.workers_values:
        ap.error("--ticker and --try are required unless --report")

    rows = real_active_row_count(args.ticker)
    print(f"{args.ticker}: {rows} active-build rows in massive_second_derived")

    for workers in args.workers_values:
        result = run_one_attempt(args.ticker, workers)
        result["row_count"] = rows
        save_result(result)
        if result["outcome"] != "ok":
            print(f"[{args.ticker}] stopping sweep at workers={workers} "
                  f"(outcome={result['outcome']})")
            break


if __name__ == "__main__":
    main()
