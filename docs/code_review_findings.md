# Code Review Findings — 2026-07-03

Full-codebase review: gaps and optimizations, prioritized for fixing. Each item has file:line, the problem, why it matters, and a suggested fix.

**Update (same session):** items #9, #10, #14, #19, #22 and the logging half of #16 were fixed directly — see "Fixed in review session" at the bottom. Remaining items stand.

**Also discovered:** `tests/test_ZScoreBreakout.py` fails 2/11 — it still asserts the pre-7/2 theoretical TP/SL exit prices (`entry*1.10` / `entry*0.95`), but `check_exit` was intentionally changed to return the actual bar close / stop price. Update the test expectations to match the kernel-parity semantics.

**Ground rules for the fixing session:**
- Never `DELETE` from `backtest_cache` to fix a query mismatch — fix the query; versioning exists for coexistence.
- Scope edits tightly; verify each diff immediately after applying.
- A sweep may be running (`logs/sweep_new_tickers.log`) — check before touching `run_optimization_sweep.py`, `backtester.py`, `strategies.py`, or `config.json`. If it's running, do the P2/UI items first.
- The live `active_signals.py` process won't pick up code changes until restarted — note in the summary if any P0 fix requires a restart.
- Items already in `docs/backlog.md` (half-day sessions, Phase 2.5 (w,z) scope, cliff-check improvements, process-restart consistency) are intentionally not repeated here.

---

## P0 — Live-trading correctness

### 1. TIME exit uses wall-clock hours live; backtest counts trading bars — FIXED (unreviewed, see bottom)
`active_signals.py:525-526` — `check_sell_condition` computes `hours_held = (now - entry_time).total_seconds() / 3600` (wall clock). Every backtest kernel counts `held += 1` per *hourly trading bar* (`backtester.py`), and the parity script counts bar indexes (`scripts/verify_live_parity.py:61`, `hours_held = i - entry_bar_idx`) — which is why parity passed while live is wrong.

Impact: a hold=133h node means ~19 trading days in backtest, but live the TIME exit fires after 133 wall-clock hours ≈ 5.5 calendar days — roughly 3.5× early. Affects every open position. Note the code already has the correct concept in `_add_trading_hours` (`active_signals.py:1126`) used for the Slack deadline display — display and exit check currently disagree with each other.

Fix: compute hours held by counting hourly bars in the cached price data with timestamp > entry_time (mirrors kernel semantics and handles holidays for free), or invert `_add_trading_hours`. Apply in `check_sell_condition`; also the morning-report "held Nh" display (`active_signals.py:1247`) uses wall clock.

### 2. v1.8/v1.9/v1.10 nodes cannot round-trip from sweep DB to live monitor — FIXED (unreviewed, see bottom)
Two related gaps:

a) **`fixed_stop_loss` is not stored in `backtest_cache`.** For v1.8 the swept `stop_loss` column actually holds `trail_pct`; for v1.9/v1.10 it holds `trail_buy_pct` (`run_optimization_sweep.py:136-156`); the real SL is `config.execution.fixed_stop_loss`, which appears nowhere in the DB row. Re-running the same version with a different `fixed_stop_loss` silently returns stale cached rows (primary-key collision) — wrong results with no error. Fix: add a `fixed_sl` column (default 0), include it when writing rows for the trailing strategies, and include it in cache-hit lookups. Do not touch existing rows.

b) **Live monitor misinterprets those node params.** `active_signals.py:527` builds the strategy with `trail_pct=pos.get('trail_pct', 0.03)` — but neither `watch_list` nor `open_positions` has a `trail_pct` column, so it's always 0.03, and `pos['stop_loss']` (which is trail_pct/trail_buy_pct for v1.8+) is used as the fixed SL in `check_exit`. Adding a v1.8+ node from the pivot to the watchlist would trade the wrong parameters. Same phantom column in `notify_trailing_activated` (`active_signals.py:1108`). Fix: add `trail_pct` and `fixed_sl` columns to `watch_list`/`open_positions` (follow the existing ALTER-if-missing pattern in `ensure_tables`), populate on add, and pass through `check_sell_condition`.

### 3. v1.9/v1.10 have no live execution path
`strategies.py` has `check_signal`/`check_exit` for `TrailingBuyZScoreBreakout`/`TrailingBothZScoreBreakout`, but the live loop (`active_signals.py:run_loop`) has no trailing-entry state machine — no "signal fired, now track running low, alert when price bounces trail_buy_pct% above it" phase. A BUY alert would fire at the z-signal and tell the user to buy immediately, which is not the backtested entry. Also `_STRATEGY_LABELS` (`active_signals.py:1170-1175`) is missing both classes, so the morning report shows raw class names with no action text.

Fix (if sweep results favor these versions): add a `pending_entries` state (ticker, signal time, running low, wait bars) persisted like `open_positions`, checked each poll; alert on bounce trigger; expire after max_hold_hours wait bars, matching `_simulate_trail_buy`. At minimum, add the `_STRATEGY_LABELS` entries and a guard that refuses to add v1.9/v1.10 nodes to a live watchlist until support exists.

### 4. Signal-window "algo alive" alert requires an exact-minute hit — FIXED (unreviewed, see bottom)
`active_signals.py:1371-1375` — fires only when `now.minute == wm` (exactly :25). With `POLL_SECS=300` the loop lands on arbitrary minutes, so the 10:25/15:25 alert usually never fires. Fix: fire when `now` is anywhere inside the window and the (day, label) key hasn't fired yet — the dedupe set already exists.

### 5. `sell_alerted` is never cleared — FIXED (unreviewed, see bottom)
`active_signals.py:1329,1408` — once a SELL alert fires for a position id, it's suppressed forever (until process restart), including when the user clicks "Skipped (position kept open)". A skipped TP alert means no further exit alerts of any kind for that position — including a later SL. Fix: clear the id on a new bar (or daily, like `buy_alerted`), or remove it in the `sell_skipped` handler via a shared structure.

### 6. app.py config form silently corrupts config.json — FIXED (unreviewed, see bottom)
`app.py:151-172` — the save handler rebuilds the config dict from scratch: drops `z_score_thresholds` from hyperparameters, drops `max_workers` and `fixed_stop_loss` from execution. And the strategy multiselect (`app.py:129-133`) only offers 2 of the 6 strategy classes, so a v1.7+ config loaded into the form loses its strategy on save. One "Lock Configuration" click from the UI breaks the next sweep run. Fix: merge form fields into the loaded config instead of rebuilding; derive the multiselect options from the `strategies` module; add a z_thresholds input.

---

## P1 — Sweep correctness & performance

### 7. Phase 1 skip-check can wrongly skip the coarse scan
`run_optimization_sweep.py:338-349` — counts cached rows matching (strategy, version, ticker, z IN grid, window IN grid) but does **not** restrict tp/sl/hold to the coarse-grid values. Phase-2 island-mesh rows (fine tp/sl values, same w/z) inflate the count, so `cached >= expected` can pass while coarse nodes are missing — subsequent island detection then runs on an incomplete grid. Fix: add `take_profit IN (...) AND stop_loss IN (...) AND max_hold_hours IN (...)` to the count query.

### 8. Numba kernels have no MAX_TRADES bounds guard
`backtester.py:13` — all 5 kernels write `entry_i[count] = ...` unchecked. In nopython mode an overflow past 5000 trades is a silent out-of-bounds write (corrupt results or crash), not an IndexError. A hold=7h, z=1.0 config on 2 years of hourly data gets close. Fix: in each kernel, `if count >= MAX_TRADES: break` (one line, five places).

### 9. Every sweep node re-reads the CSV and recomputes indicators
`run_optimization_sweep.py:109-134` — `run_single_backtest_node_isolated` parses `{ticker}_1h.csv`, resamples daily, and recomputes rolling SMA/Std per node. Phase 3 is ~18,000 nodes per ticker → ~18,000 parses of the same file per worker-share. Workers are long-lived (shared `ProcessPoolExecutor`), so a module-level memo cache works:
- cache hourly df per ticker (one entry — tickers are processed sequentially, so a 1-entry cache is enough and bounds memory),
- cache `generate_daily_indicators` output per (ticker, strategy, window) — z doesn't affect indicators,
- hoist the per-call timestamp prep in `backtester.run_backtest*` (`.strftime` over ~13k timestamps + daily_idx dict) into the same cached layer; it's identical for every node of a (ticker, strategy, window).

This is the single biggest sweep speedup available — likely the difference between hours and tens of minutes on dispatch-bound phases.

### 10. Per-task cache-check SELECT in dispatch
`run_optimization_sweep.py:194-211` — one SELECT per task; Phase 3 issues ~18k queries per ticker before dispatching. Fix: one query loading all (w, hold, tp, sl, z) → row for the (strategy, version, ticker), then dict lookups.

### 11. `compute_buy_signal` runs up to 3× per node per poll, each with a network call
`active_signals.py` — called from `_send_window_alert` (1149), limit-fill detection (1427), and the in-window scan (1438). Each call spins a fresh `ThreadPoolExecutor` and hits yfinance for a 1m history. Fix: memoize per ticker per loop iteration (fetch the live price once per ticker per cycle and reuse); nodes on the same ticker share it.

### 12. Unused heavy imports in the sweep module
`run_optimization_sweep.py:13-14` — matplotlib + seaborn only feed the commented-out heatmap block; every worker process pays those imports at spawn. Remove (keep the commented block's imports noted in the comment if it may return).

### 13. Daily-resample logic differs subtly across the codebase
`active_signals.py:420` uses `.resample('D').last().dropna()` (drops a day if *any* column is NaN); `run_optimization_sweep.py:129` uses `.dropna(subset=[close_col])`; `data_manager.py` has three more copies. If any bar ever has a NaN in a non-close column, live indicators diverge from backtest indicators for that day — a quiet parity leak. Fix: one shared `load_hourly_and_daily(ticker)` helper (e.g. in `data_manager.py`) used by the sweep worker, active_signals, and verify_live_parity, with the `dropna(subset=[close])` semantics the backtest uses.

---

## P2 — Code quality / UI / hygiene

### 14. backtester.py: 5× duplicated prep code
`run_backtest` / `_v17` / `_v18` / `_v19` / `_v110` each repeat ~20 identical lines of array prep and ~12 of trade-dict building. Extract `_prep_arrays(...)` and `_trades_from_arrays(...)`. (Coordinate with #9 — do them together.)

### 15. `INSERT OR REPLACE INTO backtest_cache VALUES (?×15)` without column list
`run_optimization_sweep.py:277` — positional insert breaks silently if the table gains a column (it already did once — z_score_threshold was ALTERed in). Name the columns.

### 16. Worker errors are swallowed invisibly
`run_single_backtest_node_isolated` returns `status: ERROR/SIM_ERROR/UNKNOWN_STRAT` with no exception text, and `dispatch_parallel_grid` ignores those statuses entirely — failed nodes just vanish (not cached, not logged, retried forever on every rerun). Fix: include `repr(e)` in the returned dict and log a warning per failed node (or one aggregated count per ticker/phase).

### 17. Stale window text
`active_signals.py:1457` — "next: 10:25 or 14:55 ET" should be 15:25.

### 18. Unknown CLI command silently starts the live loop
`active_signals.py:1543` — `if cmd in ('run',) or cmd not in _CMDS:` means a typo (`postions`) launches the full monitor. Print usage and exit for unknown commands.

### 19. Top Pivot: duplicated section header
`pages/0_Top_Pivot.py:243` and `:386` both render `st.subheader("Universe — Best Alpha by Strategy")` — the first (with the max-alpha input between them) makes the page show the title twice. Keep one.

### 20. Top Pivot: watchlist pivot hardcodes `watchlist_id = 1`
`pages/0_Top_Pivot.py:429` — should join on the active watchlist (`watchlists.is_active=1`) like `active_signals.get_active_watchlist_id()`.

### 21. Top Pivot: `load_pivot_from_db(version, min_trades)` ignores min_trades
`pages/0_Top_Pivot.py:66-88` — the SQL doesn't use it (filtering happens later in `_build_pivot`), but it's part of the `@st.cache_data` key, fragmenting the cache per min_trades value. Drop the param.

### 22. Top Pivot: `iterrows` in the cliff-safe grid build
`pages/0_Top_Pivot.py:326-328` — builds the (tp, sl)→alpha lookup with `iterrows` over what can be millions of grid rows; this is the probable cause of the unconfirmed slow load noted at last session close. Replace with `dict(zip(zip(grp.take_profit.astype(int), grp.stop_loss.astype(int)), grp.alpha_vs_spy))` — order-of-magnitude faster.

### 23. Hardcoded checkpoint constants
`run_optimization_sweep.py:502-503` (alpha ≥ 200, liquidity ≥ $50k) and `:692` (n_index=25, n_stock=5) — move to `config.execution` with the current values as defaults.

### 24. Two pages share the `4_` prefix
`pages/4_Portfolio.py` and `pages/4_Screener.py` — Streamlit orders by filename; renumber Screener.

### 25. Root-directory clutter
Untracked one-offs and artifacts in repo root: `Results (7).csv`, `Results (8).csv`, `results.csv`, `config.json.bak`, `docs/.operational_limits.md.swp` (stale vim swap), `test_report.py` (3-line manual runner), `test_pipeline.py`, `run_smst_full.py`, `open_fill_analysis.py`, `hurst_filter_sweep.py`. Suggest: move still-useful one-offs into `scripts/`, delete the swap file and CSV exports (confirm with user before deleting data files), and gitignore `*.bak` / `Results*.csv` / `results.csv`.

### 26. `identify_full_mesh_candidates` name-shadow risk in db_cache
`db_cache.py:48` — local variable `strategies` shadows the commonly imported module name; harmless here but rename to `strats` for grep-ability. Also `refresh_pivot_cache` (`db_cache.py:74-80`) groups by `trades` inside the GROUP BY, producing multiple rows per (ticker, window, z) — `_build_pivot` re-aggregates with max so results are correct, but the grouping looks accidental; add a comment or simplify.

---

## Suggested fix order
1. ~~P0 #1, #2, #4, #5~~ (fixed 2026-07-03, see "Fixed in follow-up session" below) — P0 #17 not yet done; active_signals.py needs a restart once these land
2. ~~P0 #6~~ (fixed 2026-07-03, see below)
3. P1 #7, #8 (sweep correctness — wait for the running sweep to finish)
4. P1 #11, #13, remaining P2s opportunistically
5. P0 #3 only if/when v1.9/v1.10 win the sweep comparison

**Next session**: review each fix below one at a time (not yet independently reviewed — implemented and self-verified only), plus the real DB-insert-only performance test noted in `dispatch_telemetry_results.md`'s correction.

---

## Fixed in review session (2026-07-03)

- **#9 worker caching**: `_load_node_inputs` + `_NODE_INPUT_CACHE` in `run_optimization_sweep.py` — per-worker memo of CSV + indicators + prepared kernel arrays, keyed (ticker, strategy, window). 279ms → 6.5ms per node steady-state (~40×). Verified payload-identical to pre-refactor on AGQ nodes; all 5 strategy dispatch paths exercised; `verify_live_parity.py` ALL MATCH.
- **#14 prep dedup**: `backtester.prep_inputs()` + `_build_trades()`; all 5 `run_backtest*` take optional `prep=`. High/Low fall back to Close for close-only frames (synthetic test data).
- **#10 dispatch batching**: one SELECT per (strategy, version, ticker) into `cached_map` instead of per-task queries. Verified cached path returns identical rows to computed path.
- **#16 (logging half)**: worker returns `error: repr(e)`; dispatch logs first failed node + per-phase failure counts. (Statuses still not persisted — failed nodes are retried each run; acceptable.)
- **#19**: duplicate "Universe — Best Alpha by Strategy" subheader removed.
- **#22 + cliff-safe slowness**: root cause was two full scans (candidates 69s + 3M-row/712MB grid load 88s) plus `iterrows`. Replaced with one `GROUP BY` scan collapsing holds to `MAX(alpha)` (`db_cache.CLIFF_GRID_SQL`), walk-down in pandas, and a sweep-end kv cache (`refresh_cliff_grid_cache`, wired into the sweep's final refresh) so the page loads from `kv_cache` instantly. Semantics change (deliberate): a neighbor's alpha is now its best-over-holds value instead of an arbitrary hold's row (the old dict build silently kept whichever hold was read last); candidates are now the top-100 *distinct* (tp, sl) nodes rather than top-100 raw rows (which were mostly hold-duplicates).
- matplotlib/seaborn imports removed from `run_optimization_sweep.py` (only fed the commented-out heatmap block).

---

## Fixed in follow-up session (2026-07-03, continued) — NOT YET REVIEWED

Implemented and self-verified (unit-level smoke tests + one real backfill run against live data) this session, but not yet walked through by the user one fix at a time — that's next session's first task.

- **#1 TIME exit**: `check_sell_condition` now counts cached hourly bars with `timestamp > signal_time` (`_bars_held`, `active_signals.py`) instead of wall-clock `(now - entry_time)`. Verified against real AGQ data: a 133-hold node correctly fires TIME exit at exactly bar 133, not ~38 bars early. All wall-clock "held Nh" displays (morning report Slack + console, `cmd_positions`) switched to the same bar-count so they no longer disagree with the actual exit check. Note: `verify_live_parity.py` passing before this fix didn't catch it — that script computes `hours_held` itself via bar-index arithmetic and calls `strategies.py` directly, never exercising `active_signals.py`'s `check_sell_condition`.
- **#2 fixed_sl/trail_pct round-trip**: `backtest_cache` gets a `fixed_sl` column (default 0); `dispatch_parallel_grid`'s cache-hit key now includes it for v1.8/v1.9/v1.10 (issubclass check against `TrailingExitZScoreBreakout`/`TrailingBuyZScoreBreakout`) so re-running with a different `execution.fixed_stop_loss` recomputes instead of silently reusing stale rows — verified with a real cache-hit/miss test (same fixed_sl → hit; different → miss + recompute; non-trailing strategy → unaffected). `watch_list`/`open_positions` get `trail_pct`/`fixed_sl` columns, populated in `add_node()`/`open_position()`, including through the Slack BUY-button JSON round-trip (was silently dropping them — `app.py`-style bug, same root cause as #6). `check_sell_condition` now reads real fixed SL and trail % from these columns instead of misreading the swept `stop_loss` column as both.
- **#4 signal-window exact-minute bug**: reused the existing `_SIGNAL_WINDOWS` tuple instead of a separate hardcoded `[(10,25,"10:25"),(15,25,"15:25")]` list; fires anywhere in `[start, end]` per window, not just the exact opening minute.
- **#5 sell_alerted never cleared**: dedup key changed from `position_id` to `(position_id, bar_ts)` — self-clears on the next bar instead of needing an explicit clear, so a skipped TP alert no longer permanently suppresses a later SL alert on the same position.
- **#6 app.py config corruption**: save handler now merges into `dict(db_config)` instead of rebuilding from scratch — `max_workers`/`fixed_stop_loss`/any future key survive a save. Strategy multiselect now derived from `strategies.BaseStrategy` subclasses via `inspect` instead of a hardcoded 2-item list (the real config's `LimitOrderZScoreBreakout` wasn't in the old list — this was an active bug, not hypothetical). Added a `z_score_thresholds` text input so it round-trips instead of being silently dropped. Verified: replaying the real `config.json` through the new merge logic with unchanged form values reproduces it byte-for-byte.
- **#10/#15 (Haiku, background agent)**: `dispatch_parallel_grid`'s per-node `INSERT OR REPLACE` batched into `executemany()` chunks of 50 with an explicit column list (was positional `VALUES (?×15)`, finding #15). Caught a real live instance of #15 during testing: once `fixed_sl` was added as the 16th column, the *old* positional-insert code silently failed every single insert (`table backtest_cache has 16 columns but 15 values were supplied`, swallowed by the per-node exception handler) — computed all 12,000 nodes but persisted zero rows. New code confirmed writing correctly against the same table.
- **Correction to `dispatch_telemetry_results.md`**: the profiling script (`scripts/profile_dispatch.py`) that produced the "88% of time is Result Collection" finding never actually touches `backtest_cache` — no `INSERT` anywhere in its instrumented path. Its own numbers show "Result Collection" ≈ parallel kernel compute time (~90% efficiency: 4.51s ideal vs 4.427s actual), not IPC/pickling overhead as its prose claimed. The recommendation to batch DB inserts was reasoned from "each node currently triggers a DB insert," not measured. The batching fix above is still correct/worth having independent of this (real #15 bug demonstrated above), but there's no real before/after evidence it improved sweep speed — next session should instrument the actual INSERT step (old per-row `execute()` vs new `executemany()`) in isolation to get real numbers.
