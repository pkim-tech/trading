#!/usr/bin/env python3
"""
Instrument dispatch_parallel_grid to measure per-node overhead:
- Kernel execution time (backtest + cache lookup)
- Submit loop time (futures_map building)
- Result collection time (future.result() calls)
- IPC/pickling overhead

Uses synthetic tasks on SOXL with vtest-telemetry version.
Cleans up test data after measurement.
"""

import sys
import os
import time
import sqlite3
import json
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
import numpy as np
import pandas as pd

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from run_optimization_sweep import (
    DB_PATH, CACHE_DIR, init_idempotent_db,
    run_single_backtest_node_isolated, compute_bh_returns
)
import strategies


def run_single_backtest_node_timed(args):
    """
    Wrapper around run_single_backtest_node_isolated that measures kernel time.
    Returns (result_dict, kernel_elapsed_ms).
    """
    t_start = time.perf_counter()
    result = run_single_backtest_node_isolated(args)
    t_end = time.perf_counter()
    kernel_ms = (t_end - t_start) * 1000
    return (result, kernel_ms)


def dispatch_parallel_grid_instrumented(shared_pool, tasks, ticker, strategy_name, config_version, spy_bh, asset_bh):
    """
    Identical to dispatch_parallel_grid but with timing instrumentation on:
    1. Kernel time (inside run_single_backtest_node_isolated)
    2. Submit loop time (building futures_map)
    3. Result collection time (future.result() calls)
    """
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    cursor = conn.cursor()
    matrix_results = []
    unvisited_tasks = []

    # Prefetch cached results (same as original)
    cached_map = {}
    cursor.execute("""
        SELECT window, max_hold_hours, take_profit, stop_loss, z_score_threshold,
               trades, win_rate, strategy_return, alpha_vs_spy
        FROM backtest_cache
        WHERE strategy=? AND version=? AND ticker=?
    """, (strategy_name, config_version, ticker))
    for r in cursor.fetchall():
        if r[4] is None:
            continue
        cached_map[(int(r[2]), int(r[3]), int(r[1]), int(r[0]), float(r[4]))] = (r[5], r[6], r[7], r[8])

    # Filter to unvisited only
    for t in tasks:
        tp, sl, hold_hours, w, z_thresh = t
        cached_row = cached_map.get((int(tp), int(sl), int(hold_hours), int(w), float(z_thresh)))
        if not cached_row:
            unvisited_tasks.append(t)

    print(f"\n[PROFILE] {len(unvisited_tasks)} unvisited tasks, {len(tasks) - len(unvisited_tasks)} cached")

    if not unvisited_tasks:
        conn.close()
        return {
            "total_nodes": 0,
            "submit_time": 0,
            "collect_time": 0,
            "result_times": [],
            "kernel_times": [],
        }

    # Instrument: Submit loop time
    print("[PROFILE] Building futures_map...", end=" ", flush=True)
    t_submit_start = time.perf_counter()

    futures_map = {
        shared_pool.submit(
            run_single_backtest_node_timed,
            (ticker, strategy_name, config_version, int(tp), int(sl), hold, w, spy_bh, z, 0)
        ): task
        for task in unvisited_tasks
        for tp, sl, hold, w, z in [task]
    }

    t_submit_end = time.perf_counter()
    submit_time = t_submit_end - t_submit_start
    print(f"done ({submit_time:.3f}s)")

    # Instrument: Result collection time
    print("[PROFILE] Collecting results...", end=" ", flush=True)
    t_collect_start = time.perf_counter()

    result_times = []
    kernel_times = []
    for future in as_completed(futures_map):
        t_result_start = time.perf_counter()
        res, kernel_ms = future.result()
        t_result_end = time.perf_counter()

        result_time = (t_result_end - t_result_start) * 1000
        result_times.append(result_time)
        kernel_times.append(kernel_ms)

    t_collect_end = time.perf_counter()
    collect_time = t_collect_end - t_collect_start
    print(f"done ({collect_time:.3f}s)")

    conn.close()

    return {
        "total_nodes": len(unvisited_tasks),
        "submit_time": submit_time,
        "collect_time": collect_time,
        "result_times": result_times,
        "kernel_times": kernel_times,
    }


def generate_synthetic_tasks(n_nodes=3000, z_thresholds=[2.0], windows=[10, 20]):
    """Generate synthetic task list covering parameter space."""
    tp_range = list(range(3, 31, 3))  # 3 to 30 by 3
    sl_range = list(range(3, 31, 3))
    hold_range = list(range(7, 134, 14))  # 7 to 133 by 14

    tasks = []
    for z in z_thresholds:
        for w in windows:
            for tp in tp_range:
                for sl in sl_range:
                    for hold in hold_range:
                        tasks.append((int(tp), int(sl), int(hold), int(w), float(z)))
                        if len(tasks) >= n_nodes:
                            break
                    if len(tasks) >= n_nodes:
                        break
                if len(tasks) >= n_nodes:
                    break
            if len(tasks) >= n_nodes:
                break
        if len(tasks) >= n_nodes:
            break

    return tasks[:n_nodes]


def main():
    init_idempotent_db()

    ticker = "SQQQ"
    strategy_name = "ZScoreBreakout"
    config_version = "vtest-telemetry"

    print(f"\n{'='*70}")
    print(f"Dispatch Overhead Profiling: {ticker} / {strategy_name}")
    print(f"{'='*70}")

    # Ensure data is available
    cache_path = CACHE_DIR / f"{ticker}_1h.csv"
    if not cache_path.exists():
        print(f"ERROR: {cache_path} not found. Run data collection first.")
        sys.exit(1)

    print(f"[SETUP] Data cache: {cache_path}")

    # Get B&H returns for context
    asset_bh, spy_bh = compute_bh_returns(ticker)
    if asset_bh is None:
        print(f"ERROR: Could not compute B&H returns for {ticker}")
        sys.exit(1)
    print(f"[SETUP] {ticker} B&H: {asset_bh:+.1f}%, SPY B&H: {spy_bh:+.1f}%")

    # Generate synthetic tasks (~3000-4000 nodes)
    tasks = generate_synthetic_tasks(n_nodes=3500, z_thresholds=[2.0], windows=[10, 20])
    print(f"[SETUP] Generated {len(tasks)} synthetic tasks")

    # Run dispatch with instrumentation
    print(f"\n[PROFILE] Starting dispatch with {len(tasks)} tasks (max_workers=8)...")
    t_dispatch_total_start = time.perf_counter()

    with ProcessPoolExecutor(max_workers=8) as pool:
        results = dispatch_parallel_grid_instrumented(
            pool, tasks, ticker, strategy_name, config_version, spy_bh, asset_bh
        )

    t_dispatch_total_end = time.perf_counter()
    total_dispatch_time = t_dispatch_total_end - t_dispatch_total_start

    # Report findings
    print(f"\n{'='*70}")
    print("PROFILING RESULTS")
    print(f"{'='*70}")

    total_nodes = results["total_nodes"]
    submit_time = results["submit_time"]
    collect_time = results["collect_time"]
    result_times = results["result_times"]
    kernel_times = results["kernel_times"]

    if total_nodes == 0:
        print("No unvisited nodes to profile (all cached)")
        # Still cleanup
        print(f"\n[CLEANUP] Deleting vtest-telemetry rows from backtest_cache...", end=" ", flush=True)
        conn = sqlite3.connect(DB_PATH, timeout=60.0)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM backtest_cache WHERE version=?", (config_version,))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        print(f"deleted {deleted} rows")
        print(f"\n{'='*70}\n")
        return

    avg_result_time = np.mean(result_times) if result_times else 0
    median_result_time = np.median(result_times) if result_times else 0
    max_result_time = np.max(result_times) if result_times else 0

    avg_kernel_time = np.mean(kernel_times) if kernel_times else 0
    median_kernel_time = np.median(kernel_times) if kernel_times else 0
    max_kernel_time = np.max(kernel_times) if kernel_times else 0
    total_kernel_time = np.sum(kernel_times) / 1000.0  # convert ms to s

    # IPC overhead = time for submit + result collection
    ipc_overhead = submit_time + collect_time

    print(f"\nTotal nodes dispatched:      {total_nodes}")
    print(f"Total dispatch time:         {total_dispatch_time:.3f}s")
    print(f"\nTime breakdown:")
    print(f"  Kernel execution:          {total_kernel_time:.3f}s ({total_kernel_time/total_dispatch_time*100:.1f}%)")
    print(f"  Submit loop:               {submit_time:.3f}s ({submit_time/total_dispatch_time*100:.1f}%)")
    print(f"  Result collection:         {collect_time:.3f}s ({collect_time/total_dispatch_time*100:.1f}%)")
    print(f"  Unaccounted overhead:      {total_dispatch_time - total_kernel_time - submit_time - collect_time:.3f}s ({(total_dispatch_time - total_kernel_time - submit_time - collect_time)/total_dispatch_time*100:.1f}%)")

    print(f"\nKernel execution (backtest + cache):")
    print(f"  Average:                   {avg_kernel_time:.2f}ms")
    print(f"  Median:                    {median_kernel_time:.2f}ms")
    print(f"  Min:                       {np.min(kernel_times):.2f}ms")
    print(f"  Max:                       {max_kernel_time:.2f}ms")

    print(f"\nResult collection overhead (future.result() + IPC):")
    print(f"  Average:                   {avg_result_time:.2f}ms")
    print(f"  Median:                    {median_result_time:.2f}ms")
    print(f"  Min:                       {np.min(result_times) if result_times else 0:.2f}ms")
    print(f"  Max:                       {max_result_time:.2f}ms")

    print(f"\nPer-node metrics:")
    print(f"  Avg total dispatch cost:   {total_dispatch_time / total_nodes * 1000:.2f}ms")
    print(f"  Avg compute cost:          {avg_kernel_time:.2f}ms")
    print(f"  Avg IPC+dispatch cost:     {avg_result_time:.2f}ms")

    print(f"\nOverhead analysis:")
    ipc_fraction = ipc_overhead / total_dispatch_time
    compute_fraction = total_kernel_time / total_dispatch_time
    other_fraction = 1.0 - ipc_fraction - compute_fraction
    print(f"  Compute:                   {compute_fraction*100:.1f}% ({total_kernel_time:.3f}s)")
    print(f"  IPC/dispatch (submit+collect): {ipc_fraction*100:.1f}% ({ipc_overhead:.3f}s)")
    print(f"  Other overhead:            {other_fraction*100:.1f}%")

    speedup_vs_dispatch_cost = (total_dispatch_time / total_nodes) / (avg_kernel_time / 1000.0)
    print(f"\nDispatch overhead multiplier: {speedup_vs_dispatch_cost:.1f}x (per-node dispatch cost vs kernel cost)")

    # Cleanup: delete vtest-telemetry rows
    print(f"\n[CLEANUP] Deleting vtest-telemetry rows from backtest_cache...", end=" ", flush=True)
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM backtest_cache WHERE version=?", (config_version,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    print(f"deleted {deleted} rows")

    print(f"\n{'='*70}\n")


if __name__ == "__main__":
    main()
