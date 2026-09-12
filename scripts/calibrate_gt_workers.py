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

Sustained-load mode (--sustained, added 2026-09-11, research(3) peer review): the
single-cell test above only captures ONE call's peak PSS per worker, a few seconds --
it can't see whether a per-worker module-level cache (_SECOND_DF_CACHE, _HOURLY_DF_
CACHE_GT) or numba JIT state grows across many candidates processed sequentially by
the SAME long-lived worker, the way a real run_gt_mode campaign actually runs (each
outer-pool worker resimulates many scopes/candidates over tens of minutes, not one).
--sustained has each worker process N real distinct candidate_nodes rows for the
ticker in a loop, self-reporting its own process's PSS after every candidate, so
growth across a worker's lifetime is visible even when a single call's peak looks flat.

Usage:
  .venv/bin/python scripts/calibrate_gt_workers.py --ticker AGQ --try 8 10
  .venv/bin/python scripts/calibrate_gt_workers.py --ticker SOXL --try 8 6 4 2
  .venv/bin/python scripts/calibrate_gt_workers.py --ticker SOXL --try 8 --sustained --candidates-per-worker 15
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


def _candidate_rows_from_db(ticker, limit=20):
    """Real distinct candidate_nodes rows for `ticker` -- for --sustained mode, so each
    worker resimulates genuinely different coordinates across its loop (not the same
    cell N times, which a per-coordinate cache could trivially short-circuit)."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.execute("""
            SELECT strategy, window, z, fixed_sl, arm_pct, trail_buy_pct, trail_sell_pct,
                   max_hold_hours, entry_timing
            FROM candidate_nodes WHERE ticker=? ORDER BY id DESC LIMIT ?
        """, (ticker, limit))
        return cur.fetchall()
    finally:
        conn.close()


def _cell_args_from_row(ticker, row, spy_bh, data_source):
    strategy, w, z, fixed_sl, arm_pct, trail_buy_pct, trail_sell_pct, hold, entry_timing = row
    is_both = strategy == "TrailingBothZScoreBreakout"
    if is_both:
        tp, sl, tpct = arm_pct, trail_buy_pct, trail_sell_pct
    else:
        tp, sl, tpct = arm_pct, trail_sell_pct, trail_sell_pct
    return (ticker, strategy, tp, sl, hold, w, z, fixed_sl, tpct, entry_timing, None, None,
            spy_bh, None, data_source, True, "second")


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


def _force_kill_children(proc):
    """pool.shutdown(wait=False, cancel_futures=True) only cancels futures that haven't
    STARTED yet -- it does NOT terminate already-running worker processes, which keep
    executing (and keep growing memory) until they finish on their own. Confirmed
    2026-09-11: a SOXL sustained-load would_oom trip took 27s+ to actually resolve this
    way, during which real system `used` climbed to 21GB/23GB with swap active -- a real
    near-miss on this shared machine, not a hypothetical one. Must SIGKILL every child
    directly for an OOM-risk abort to actually mean "stop growing memory now"."""
    try:
        children = proc.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    for child in children:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            continue
    psutil.wait_procs(children, timeout=10)


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
                    _force_kill_children(this_proc)
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


def _run_cell_loop_entrypoint(args_list):
    """Runs inside ONE long-lived worker process, same as a real run_gt_mode outer-pool
    worker resimulating many scopes/candidates sequentially over its lifetime. Self-
    reports this process's own PSS (no children -- this process itself is the leaf, no
    fork double-counting to worry about) after every candidate, so a per-worker cache/
    JIT-state growth trend is visible even when a single call's peak looks flat."""
    import psutil as _psutil
    from run_optimization_sweep import _run_addon_cliff_cell_isolated
    self_proc = _psutil.Process()
    trace = []
    t0 = time.monotonic()
    for i, cell_args in enumerate(args_list):
        try:
            _run_addon_cliff_cell_isolated(cell_args)
        except Exception:
            pass
        trace.append({"i": i, "pss_bytes": self_proc.memory_full_info().pss,
                       "elapsed": round(time.monotonic() - t0, 1)})
    return trace


def run_sustained_attempt(ticker, workers, candidates_per_worker):
    """Each of `workers` long-lived worker processes resimulates `candidates_per_worker`
    real, distinct candidate_nodes coordinates in a sequential loop -- the fidelity gap
    research(3) flagged in the single-cell test above. Returns per-worker PSS traces
    plus the same external tree-PSS OOM watchdog as run_one_attempt."""
    total_mem = psutil.virtual_memory().total
    headroom_bytes = int(total_mem * MEMORY_HEADROOM_FRACTION)
    this_proc = psutil.Process()
    baseline_other_used = psutil.virtual_memory().used - _proc_tree_rss(this_proc)

    cfg = CANDIDATES[ticker]
    print(f"[{ticker} workers={workers} sustained] preloading...", flush=True)
    _preload(ticker)
    import run_optimization_sweep as ros
    _, spy_bh = ros.compute_bh_returns(ticker, data_source=cfg["data_source"])

    rows = _candidate_rows_from_db(ticker, limit=max(candidates_per_worker, 20))
    if not rows:
        raise ValueError(f"no candidate_nodes rows found for {ticker}")
    n_total = workers * candidates_per_worker
    cycled_rows = [rows[i % len(rows)] for i in range(n_total)]
    all_args = [_cell_args_from_row(ticker, r, spy_bh, cfg["data_source"]) for r in cycled_rows]
    worker_chunks = [all_args[w * candidates_per_worker:(w + 1) * candidates_per_worker]
                     for w in range(workers)]

    print(f"[{ticker} workers={workers} sustained] dispatching {workers} worker(s), "
          f"{candidates_per_worker} candidates each...", flush=True)
    start = time.monotonic()
    peak_rss = 0
    outcome = "ok"
    error_text = None
    worker_traces = []
    ctx = multiprocessing.get_context("fork")
    try:
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            futures = [pool.submit(_run_cell_loop_entrypoint, chunk) for chunk in worker_chunks]
            while True:
                done = all(f.done() for f in futures)
                tree_rss = _proc_tree_rss(this_proc)
                peak_rss = max(peak_rss, tree_rss)
                projected_used = baseline_other_used + tree_rss
                if projected_used >= total_mem - headroom_bytes:
                    print(f"[{ticker} workers={workers} sustained] WOULD HAVE OOM'D -- "
                          f"tree_rss={tree_rss/1e9:.2f}GB other_used="
                          f"{baseline_other_used/1e9:.2f}GB total={total_mem/1e9:.2f}GB "
                          f"headroom={headroom_bytes/1e9:.2f}GB. Killing early.", flush=True)
                    for f in futures:
                        f.cancel()
                    _force_kill_children(this_proc)
                    pool.shutdown(wait=False, cancel_futures=True)
                    outcome = "would_oom"
                    break
                if done:
                    break
                time.sleep(POLL_INTERVAL_SECONDS)
            if outcome == "ok":
                for f in as_completed(futures):
                    try:
                        worker_traces.append(f.result())
                    except Exception as e:
                        outcome = "error"
                        error_text = repr(e)
    except Exception as e:
        outcome = "error"
        error_text = repr(e)

    elapsed = time.monotonic() - start
    growth_per_worker = []
    if worker_traces:
        for trace in worker_traces:
            if len(trace) >= 2:
                first_pss = trace[0]["pss_bytes"]
                last_pss = trace[-1]["pss_bytes"]
                growth_per_worker.append(round((last_pss - first_pss) / 1e9, 3))
        print(f"[{ticker} workers={workers} sustained] per-worker PSS growth "
              f"(last - first candidate, GB): {growth_per_worker}", flush=True)

    result = {
        "ticker": ticker, "workers": workers, "outcome": outcome, "mode": "sustained",
        "candidates_per_worker": candidates_per_worker, "peak_rss_bytes": peak_rss,
        "elapsed_seconds": round(elapsed, 1), "growth_per_worker_gb": growth_per_worker,
    }
    if error_text:
        result["error"] = error_text
    print(f"[{ticker} workers={workers} sustained] outcome={outcome} "
          f"peak_rss={peak_rss/1e9:.2f}GB elapsed={elapsed:.1f}s", flush=True)
    return result


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
    single_cell = [r for r in results if r.get("mode") != "sustained"]
    sustained = [r for r in results if r.get("mode") == "sustained"]

    by_ticker = {}
    for r in single_cell:
        by_ticker.setdefault(r["ticker"], []).append(r)

    print("-- single-cell (one call per worker) --")
    print(f"{'ticker':<8}{'active_rows':>14}{'safe_max_workers':>18}{'peak_rss_gb':>14}")
    for ticker, attempts in by_ticker.items():
        rows = real_active_row_count(ticker)
        ok_attempts = [a for a in attempts if a["outcome"] == "ok"]
        safe_max = max((a["workers"] for a in ok_attempts), default=None)
        peak_at_safe = next((a["peak_rss_bytes"] for a in ok_attempts
                             if a["workers"] == safe_max), 0)
        print(f"{ticker:<8}{rows:>14}{str(safe_max):>18}{peak_at_safe/1e9:>13.2f}G")

    if sustained:
        print("\n-- sustained (many candidates/worker, per-worker PSS growth) --")
        print(f"{'ticker':<8}{'workers':>8}{'cands/worker':>13}{'outcome':>12}"
              f"{'peak_rss_gb':>13}  growth_per_worker_gb")
        for r in sustained:
            print(f"{r['ticker']:<8}{r['workers']:>8}{r.get('candidates_per_worker', ''):>13}"
                  f"{r['outcome']:>12}{r['peak_rss_bytes']/1e9:>12.2f}G  "
                  f"{r.get('growth_per_worker_gb', [])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", choices=sorted(CANDIDATES), help="ticker to calibrate")
    ap.add_argument("--try", dest="workers_values", type=int, nargs="+",
                     help="--workers values to attempt, in order")
    ap.add_argument("--report", action="store_true", help="print results table and exit")
    ap.add_argument("--sustained", action="store_true",
                     help="test sustained per-worker load (many candidates/worker) instead "
                          "of one isolated cell -- see module docstring")
    ap.add_argument("--candidates-per-worker", type=int, default=15,
                     help="--sustained only: real candidates each worker processes sequentially")
    args = ap.parse_args()

    if args.report:
        print_report()
        return

    if not args.ticker or not args.workers_values:
        ap.error("--ticker and --try are required unless --report")

    rows = real_active_row_count(args.ticker)
    print(f"{args.ticker}: {rows} active-build rows in massive_second_derived")

    for workers in args.workers_values:
        if args.sustained:
            result = run_sustained_attempt(args.ticker, workers, args.candidates_per_worker)
        else:
            result = run_one_attempt(args.ticker, workers)
        result["row_count"] = rows
        save_result(result)
        if result["outcome"] != "ok":
            print(f"[{args.ticker}] stopping sweep at workers={workers} "
                  f"(outcome={result['outcome']})")
            break


if __name__ == "__main__":
    main()
