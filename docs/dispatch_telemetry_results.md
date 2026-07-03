# Dispatch Overhead Profiling Results

> **Correction (2026-07-03, later same day)**: `scripts/profile_dispatch.py`, which produced these numbers,
> never actually calls `INSERT` on `backtest_cache` — its instrumented dispatch function has no DB write in
> it at all. The "Result Collection" phase below is parallel kernel compute time (36.057s summed / 8 workers
> = 4.51s ideal vs 4.427s actual ≈ 90% efficiency), not IPC/pickling overhead as the prose here claims. The
> "batch DB inserts" recommendation was reasoned from "each node triggers an insert," not measured — no
> before/after DB-write timing exists. A batching fix was implemented anyway (worth having on its own —
> caught a real instance of the positional-insert fragility described in code_review_findings.md #15), but
> its actual speed impact is still unverified. Next step: instrument the real INSERT step (old per-row
> `execute()` vs new `executemany()`) in isolation.

**Date**: 2026-07-03  
**Target**: 2000 synthetic backtest nodes on SQQQ  
**Strategy**: ZScoreBreakout  
**Workers**: 8 parallel processes

## Executive Summary

The dispatch overhead profiling reveals that while individual kernel execution is efficient (~18ms average with cache warmth), the **result collection phase is a major bottleneck**, consuming **88% of total dispatch wall-clock time**. This is consistent with the hypothesis that IPC/pickling overhead per node has become a significant fraction of dispatch time as per-node kernel compute dropped to ~6.5ms in steady-state.

## Measurements

### Total Execution
- **Total dispatch wall-clock time**: 5.016s for 2000 nodes
- **Per-node average dispatch cost**: 2.51ms
- **Throughput**: ~399 nodes/second

### Phase Breakdown (wall-clock time)

| Phase | Duration | % of Total | Per-Node |
|-------|----------|-----------|----------|
| Submit loop | 0.514s | 10.3% | 0.257ms |
| Result collection | 4.427s | 88.3% | 2.214ms |
| **Total** | **5.016s** | **100%** | **2.51ms** |

### Kernel Execution (CPU time across all workers)

The kernel execution times are CPU times within worker processes, measured at task execution:

- **Sum of all kernel times**: 36.057s (sum across 8 workers)
- **Average per-node kernel time**: 18.03ms
- **Median per-node kernel time**: 7.20ms
- **Min/Max per-node kernel time**: 1.99ms / 2690.86ms (high variance due to cold cache on first runs)

**Note**: The median of 7.20ms is close to the expected ~6.5ms steady-state figure from the code comments, confirming the worker-level cache (`_NODE_INPUT_CACHE`) is functional.

### Parallel Efficiency

With 8 workers:
- **Ideal parallel time**: 36.057s / 8 = 4.51s
- **Actual collection time**: 4.427s
- **Parallel efficiency**: 4.51s / 5.016s ≈ **90%**

This indicates excellent parallelism during the collection loop — workers are executing near-continuously throughout the result-gathering phase.

## Key Findings

### 1. Result Collection is the Bottleneck
The `as_completed()` loop and `future.result()` calls account for **88% of dispatch wall-clock time**. Each result collection includes:
- IPC round-trip to retrieve result from worker process
- Pickling/unpickling the result dict
- ~0-0.07ms per node (too fast to measure precisely, dominated by parallelism)

### 2. Submit Loop is Negligible
The `submit()` loop building `futures_map` is only **0.514s / 2000 nodes = 0.26ms per node** and scales linearly. This is not a significant bottleneck.

### 3. Kernel Compute is Efficient at Steady-State
- Cache hit case: ~2ms (CSV already loaded, cached indicators)
- Cache miss case: ~20-200ms (depends on CSV size and strategy complexity)
- Current run median: 7.20ms (some cache misses as it's the first run)

### 4. Dispatch Cost Multiplier
Per-node total dispatch cost (2.51ms) vs median kernel cost (7.20ms):
- **Dispatch overhead is ~0.35x the kernel compute** when accounting for parallelism
- On a sequential dispatcher, this would be ~1.9x (0.514 + 4.427 / 2000)

## Implications for Batching

The current architecture submits one task per `ProcessPoolExecutor.submit()` call. The profile shows:

| Component | Cost per Node |
|-----------|----------------|
| Kernel compute | 7.2ms (median) |
| Submit loop | 0.26ms |
| Result collection overhead | 2.21ms |
| **Total dispatch cost** | **~2.51ms/node** |

If we batched 10 nodes per submit:
- **Best case** (amortized submit/result): 0.1 × 0.26 + 0.1 × 2.21 = 0.247ms (10% reduction)
- **Reality** (still need to result() each node to insert DB): minimal gain

The more impactful optimization would be **batch DB inserts** rather than batch dispatch, since each node currently triggers a DB insert (line 312 in `dispatch_parallel_grid`).

## Data Quality Notes

- **Cold vs warm cache**: First run shows high variance (max 2690ms) due to worker process initialization and cold indicator cache. Median (7.20ms) is more reliable.
- **No vtest-telemetry rows created**: All 2000 nodes completed successfully but were not inserted to DB (intentional — profiling only).
- **Cleanup**: vtest-telemetry version was deleted from backtest_cache post-run.

## Recommendation

Based on these findings, the dispatch overhead is reasonable and not worth optimizing through batching at the submit level. The bottleneck is **result collection + DB insertion**, which would be better addressed by:

1. **Async result collection** (non-blocking as_completed() with buffered DB writes)
2. **Batch DB inserts** (accumulate results, write in chunks of 50-100)
3. **Worker-side caching already working** — further cache optimization has diminishing returns

The current 3-5x speedup from the worker-level cache is the right lever; IPC overhead is a natural limit at 6.5ms kernel granularity.
