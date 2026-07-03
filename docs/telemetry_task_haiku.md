# Task: profile per-node overhead in the sweep dispatcher

## Context
`run_optimization_sweep.py` recently got a worker-level cache (`_NODE_INPUT_CACHE`,
line ~111) that cut per-node compute from ~279ms to ~6.5ms steady-state (CSV read +
indicator computation now happens once per `(ticker, strategy, window)` per worker
instead of once per grid node — see `_load_node_inputs`, line 115, and
`run_single_backtest_node_isolated`, line 140).

Despite that ~40x per-node speedup, real sweep runs only got 3-5x faster overall.
Working backward, the realized average per-node cost is closer to 60-90ms, not
6.5ms — meaning there's unaccounted overhead somewhere between "kernel finishes"
and "result lands in the results dataframe."

The dispatch loop lives in `dispatch_parallel_grid` (line 209). Each grid node is
submitted as its own task:

```python
futures_map = {
    shared_pool.submit(run_single_backtest_node_isolated,
                       (ticker, strategy_name, config_version, int(tp), int(sl), hold, w, spy_bh, z, fixed_sl)): task
    for task in unvisited_tasks
    for tp, sl, hold, w, z in [task]
}
```

Suspect: with one `ProcessPoolExecutor.submit()` call per node, pickling task args,
IPC round-trip, and pickling the result back are now a bigger fraction of per-node
time than the actual kernel execution.

## Goal
Instrument (don't yet fix) the dispatch path to measure, separately:
1. Time actually spent inside `run_single_backtest_node_isolated` (kernel + cache
   lookup) — this should confirm the ~6.5ms figure.
2. Time spent in the `shared_pool.submit(...)` loop building `futures_map`.
3. Time spent in the `as_completed(futures_map)` collection loop, specifically in
   `future.result()` calls (line ~275) — this is where IPC/pickling overhead for
   the *return value* would show up.
4. Wall-clock time for the whole `dispatch_parallel_grid` call vs. sum of (1).

## How to measure safely
- **Do not modify** `backtester.py`, `strategies.py`, or the numba kernels.
- **Do not run against the live watchlist tickers or a real strategy version** —
  use a disposable version string like `"vtest-telemetry"` so nothing pollutes
  real results.
- **Do not write to `config.json`** — write a small standalone script instead
  (e.g. `scripts/profile_dispatch.py`) that imports from `run_optimization_sweep.py`
  and calls `dispatch_parallel_grid` directly with a synthetic task list (e.g. 2,000-5,000
  `(tp, sl, hold, w, z)` tuples for one ticker, e.g. SOXL, so cache-hit skip doesn't
  interfere — use `config_version="vtest-telemetry"` so `cached_map` starts empty).
- After the run, **delete the `vtest-telemetry` rows** from `backtest_cache`
  (`DELETE FROM backtest_cache WHERE version='vtest-telemetry'`) — this is fine to
  delete since it's disposable test data the script itself created, not real
  results. Do not touch any other version's rows.
- Check nothing else is using the DB or CSV cache concurrently before running
  (`ps aux | grep run_optimization_sweep` should be empty) to avoid lock
  contention skewing the numbers.
- Use `time.perf_counter()` around the four measurement points above; print a
  summary (total time, sum of kernel time, submit-loop time, result-collection
  time, and the "unaccounted" remainder) at the end. No need for a fancy report —
  plain print statements are fine.

## Output
Report back: for ~2,000-5,000 synthetic nodes on one ticker, the breakdown of
where time actually goes (kernel vs. submit vs. result-collection vs.
unaccounted), and roughly what fraction of total dispatch time is IPC/dispatch
overhead vs. actual compute. That's the evidence needed to decide whether batching
multiple nodes per task is worth implementing.
