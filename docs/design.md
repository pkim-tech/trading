# Design Document — Trading Alpha Engine

## Architecture Overview

Three discrete layers, each independently runnable:

1. **Data Collection** — daemon fetches and caches hourly OHLCV data
2. **Parameter Optimization** — brute force search for robust alpha islands
3. **Active Signals** — apply optimized params to current market state, surface entries/exits (planned)

---

## Layer 1 — Data Collection

- `data_collector.py` polls every 5 minutes, calls `data_manager.py` for incremental updates
- Data stored as `cache/{ticker}_1h.csv`, SPY always included as benchmark
- Incremental backfill with deduplication — overlapping buffer handles weekends/holidays
- Ticker universe defined in `tickers.json` — plain JSON array, read at startup
- Cron job runs `data_collector.py --once` daily at 6:30 AM via `scripts/run_data_collector.sh`, logs to `logs/data_collector_daily.log` (runs before 7 AM morning report so bands are fresh)

---

## Layer 2 — Parameter Optimization

### Strategy
Z-score mean reversion: buy when price deviates significantly below the rolling SMA, exit at take profit, stop loss, or max hold time.

Strategy variants:
- `ZScoreBreakout` — pure z-score entry, close-based fill (v1.5/v1.6)
- `TrendFilteredZScore` — z-score with 50d SMA trend filter overlay
- `LimitOrderZScoreBreakout` — limit entry at `lower_band` (fill on `Low <= lower_band` intrabar); intrabar stop loss checks `Low <= stop_price`; TP checks `Close >= tp_price` at bar close (v1.7)
- `TrailingExitZScoreBreakout` — close-based entry (v1.5 style); once `Close >= tp_price`, switches to trailing mode: tracks `peak = max(High)`, exits when `Low <= peak × (1 - trail_pct)`. Replaces SL once trailing is active (v1.8, experimental)
- `LimitOrderTrailingExit` — subclasses `LimitOrderZScoreBreakout`, keeps its intrabar `Low <= lower_band` entry (fill at `lower_band`), swaps the fixed TP/SL exit for `TrailingExitZScoreBreakout`'s trailing-stop exit. Built 2026-07-04 to test whether v1.7/v2.7's weak returns (see `docs/backlog.md`) come from the entry or the fixed-TP exit — the entry noise (any wick counts, not just a confirmed close) is unfixable without becoming a different strategy (would collapse into `TrailingBuyZScoreBreakout`'s bounce-confirmation or `TrendFilteredZScore`'s regime filter), so this isolates the exit side only (v2.11)
- `LimitExitZScoreBreakout` — bar-close confirmed entry (like `ZScoreBreakout`); SL is a fixed intrabar floor, but TP is modeled as a resting limit order — fills intrabar the moment `High >= tp_price`, at `tp_price`, instead of waiting for bar-close confirmation. Built 2026-07-04 as the "Close entry + Limit exit" combo from the watchlist-repick shorthand (see `docs/backlog.md`); live-parity wiring intentionally deferred, backfill-only for now (v2.12)

### Grid axis meaning by strategy — v1.x/v2.x only, see "v3.x reparameterization" below for the fix

**This table describes v1.x/v2.x data only.** As of 2026-07-05, v3.x fixes the overload
described here: `backtest_cache.stop_loss` always means real stop-loss, and
`trail_buy_pct`/`trail_pct` are real named columns — see the "v3.x reparameterization"
section below. v1.x/v2.x rows are untouched and still follow the table below exactly as
written; this section stays for interpreting that historical data.

The sweep grid always has exactly 3 free axes — `take_profit`, `stop_loss`, `hold_time` — plus `z_score_threshold`/`window` as separate loop dimensions. For strategies that need an extra parameter, that parameter is stuffed into the `stop_loss` ("sl") column instead of getting real grid space — the column's *name* stays `stop_loss` everywhere (DB schema, CLI, dispatch code) but its *meaning* changes per strategy. This has caused real confusion in conversation more than once — check this table before assuming what a strategy's `sl` value represents:

| Strategy | `tp` axis means | `sl` axis means | Real floor SL | Exit trail % |
|---|---|---|---|---|
| `ZScoreBreakout` (v1.5/2.5/2.6) | real take-profit | real stop-loss | — (sl axis is real) | — |
| `LimitOrderZScoreBreakout` (v1.7/2.7) | real take-profit | real stop-loss | — (sl axis is real) | — |
| `TrailingExitZScoreBreakout` (v1.8/2.8/v2.18) | TP-activation threshold | **trail_pct** (exit trailing %) | `config.execution.fixed_stop_loss` (static) | swept via sl axis |
| `LimitOrderTrailingExit` (v2.11) | TP-activation threshold | **trail_pct** (exit trailing %) | `config.execution.fixed_stop_loss` (static) | swept via sl axis |
| `LimitExitZScoreBreakout` (v2.12) | real take-profit (limit-order fill) | real stop-loss | — (sl axis is real) | — |
| `TrailingBuyZScoreBreakout` (v1.9/2.9) | real take-profit | **trail_buy_pct** (entry bounce %) | `config.execution.fixed_stop_loss` (static) | — (no trailing exit) |
| `TrailingBothZScoreBreakout` (v1.10/2.10, v2.13/14/15/16/17) | TP-activation threshold | **trail_buy_pct** (entry bounce %) | `config.execution.fixed_stop_loss` (static) | `config.execution.trail_pct` (static per-run, **not** swept — sl axis is already taken by trail_buy_pct) |

Key gotchas:
- `TrailingBothZScoreBreakout` needs *two* extra parameters (`trail_buy_pct` for entry, `trail_pct` for exit) but only has *one* free slot (`sl`). `trail_buy_pct` wins that slot; `trail_pct` is hardcoded per backfill run via `config.execution.trail_pct` (default 3%, read by `run_optimization_sweep.py`'s `_config_trail_pct()`). Testing trail_pct at other values means running the *entire 53-ticker backfill again* with a different constant — v2.13=1%, v2.14=2%, v2.15=3%, v2.16=4%, v2.17=5% (v2.10 stays as-is, the original untouched run at trail_pct=3% with the plain coarse sl-grid) — it can never be a real grid axis without a schema change + rewriting the phase1/2/3 mesh generation to handle a 4th dimension. v2.13-17 all use a `sl` grid extended to include 1,2,4,5 alongside the normal coarse 3-30% points (`scripts/run_v2_backfill_sweep.sh`'s `COMBINED` list), so `trail_buy_pct` gets guaranteed low-end coverage on every ticker too, not just the ones whose coarse=3% point happened to earn island/full-mesh refinement in v2.10.
- Only tickers that pass **Checkpoint 2** (cliff-free AND alpha≥200% AND liquidity≥$50k) get Phase 2 island refinement + Phase 3 full mesh (which tests `sl` 1-30 completely). Everything else only has the 10 coarse grid points. So "we already have sl=1-5 data for some tickers" only reflects which tickers looked good on the coarse pass, not a deliberate test of that range — a ticker whose true edge sits at sl=2 but whose sl=3 coarse point looked mediocre would never get refined down to sl=2 at all.
- Confirmed real (non-fluke) example: SOXL's best v2.10 node sits at `trail_buy_pct`=13-14% (30+ trades, 36-48% win rate) — nowhere near the 1-5% range, and found via full mesh since SOXL passed Checkpoint 2. Don't assume the 1-5% range is "where the edge is" without ticker-specific evidence; UVIX's apparent 1-5% cliff patterns are contaminated by many `trades=1` fluke rows in the cache and shouldn't be used as supporting evidence for anything.

### v3.x reparameterization (2026-07-05) — real named columns, trail_pct is now a real swept axis

`backtest_cache` was migrated (schema rebuilt in place, `run_optimization_sweep.py::init_idempotent_db`,
verified 60,364,303 rows carried over unchanged) to add real `trail_buy_pct`/`trail_pct` columns.
Going forward (v3.x onward): `stop_loss` **always** means real stop-loss; `trail_buy_pct`
(entry bounce %) and `trail_pct` (exit trailing %) are their own columns, populated only
for the strategies that use them (0 otherwise). The PK now includes both new columns.
v1.x/v2.x rows are untouched — they keep the old overloaded meaning described in the
table above, with `trail_buy_pct`/`trail_pct` = 0 (not populated) on those rows.

`trail_pct` is now a genuine 4th swept grid axis for `TrailingBothZScoreBreakout`
(`hyperparameters.trail_pcts` in config.json, e.g. `[1,2,3,4,5]`) — this replaces the old
v2.13-v2.17 pattern of one full 53-ticker backfill per trail_pct value with a single v3.x
run. `run_backtest_dispatch()` (`backtester.py`) is the single source of truth
for kernel dispatch, shared by the sweep engine, Node Inspector, and Portfolio (previously
each had their own, out-of-sync `issubclass` chain — Node Inspector/Portfolio only ever
dispatched to `run_backtest_v17`-or-`run_backtest`, silently wrong for all 4 trailing
strategies before this fix).

`watch_list`/`open_positions` also gained a real `trail_buy_pct` column (`active_signals.py`).
`add_node()` accepts optional `trail_buy_pct`/`trail_pct` kwargs for v3.x callers; omitting
both falls back to the old stop_loss-reinterpretation logic for legacy v1.x/v2.x nodes.

**Axis schema consolidation (2026-07-05)**: `sl_axis`/`fourth_axis`/`uses_fixed_sl` are now
class attributes on each strategy in `strategies.py` (on `BaseStrategy`, overridden per
subclass) — the single source of truth, replacing 3 independently-maintained
`_resolve_axis_columns()` copies (`active_signals.py`, `run_optimization_sweep.py`,
`pages/0_Top_Pivot.py`) and 2 separate `uses_fixed_sl` `issubclass` chains. Module-level
helpers `strategies.resolve_axis_columns(name)`/`strategies.uses_fixed_sl(name)` wrap the
class attributes for callers that only have the strategy name string. New
`strategies.validate_axis_values(strategy, trail_buy_pct, trail_pct)` warns (doesn't raise)
when a caller passes a value for an axis the strategy doesn't use (e.g. `trail_buy_pct` on a
bar-close `ZScoreBreakout` node), or omits one it requires — wired into `add_node()`'s
explicit v3.x-value path. Built after finding this exact duplication was the root cause of
the `trail_buy_pct`/`trail_pct` mis-mapping bug fixed earlier the same day (see
`docs/backlog.md`).

Full design/rationale: `/home/pkim/.claude/plans/ancient-giggling-kettle.md`.
Backfill script: `scripts/run_v3_backfill_sweep.sh`, one version per run
(`./scripts/run_v3_backfill_sweep.sh v3.21`), or no arg to run every included version in
sequence. `--validate` runs a 4-ticker sanity check first.

### Version Changelog

Canonical version→strategy→grid record. Update this table whenever a new version is
added to a backfill script — the version number alone doesn't tell you what ran.

| Version | Strategy | Tickers | tp/sl grid | trail_pct | Notes |
|---|---|---|---|---|---|
| v1.5/v1.6 | `ZScoreBreakout` | watchlist ad hoc | coarse 3-30 | — | Original, pre-bias-fix |
| v1.7 | `LimitOrderZScoreBreakout` | watchlist ad hoc | coarse 3-30 | — | Pre-bias-fix |
| v1.8 | `TrailingExitZScoreBreakout` | watchlist ad hoc | coarse 3-30 | static, `config.execution.trail_pct` | Pre-bias-fix, experimental |
| v1.9 | `TrailingBuyZScoreBreakout` | watchlist ad hoc | coarse 3-30 | — | Pre-bias-fix |
| v1.10 | `TrailingBothZScoreBreakout` | watchlist ad hoc | coarse 3-30 | static 3% | Pre-bias-fix |
| v2.4 | `TrendFilteredZScore` | 53-ticker universe | coarse 3-30 | — | Bias-fix reindex; weak results, closed out |
| v2.5/v2.6 | `ZScoreBreakout` | 53-ticker universe | coarse 3-30 | — | Bias-fix reindex |
| v2.7 | `LimitOrderZScoreBreakout` | 53-ticker universe | coarse 3-30 | — | Bias-fix reindex |
| v2.8 | `TrailingExitZScoreBreakout` | 53-ticker universe | coarse 3-30 | static, `config.execution.trail_pct` | Bias-fix reindex |
| v2.9 | `TrailingBuyZScoreBreakout` | 53-ticker universe | coarse 3-30 | — | Bias-fix reindex |
| v2.10 | `TrailingBothZScoreBreakout` | 53-ticker universe | coarse 3-30 | static 3% | Bias-fix reindex, original untouched run |
| v2.11 | `LimitOrderTrailingExit` | 53-ticker universe | coarse 3-30 | static, `config.execution.trail_pct` | New in v2.x, no v1.x precursor |
| v2.12 | `LimitExitZScoreBreakout` | 53-ticker universe | coarse 3-30 | — | New in v2.x, backfill-only |
| v2.13-17 | `TrailingBothZScoreBreakout` | 53-ticker universe | combined (adds 1,2,4,5) | static, one full run per value: 1%/2%/3%/4%/5% | Superseded by v3.21-27 |
| v2.18 | `TrailingExitZScoreBreakout` | 53-ticker universe | combined (adds 1,2,4,5) | static 3% | Superseded by v3.18 |
| v3.5/v3.6 | `ZScoreBreakout` | Sweep 3 (11 tickers) | combined (adds 1,2,4,5) | — | Real trail_buy_pct/trail_pct columns (n/a here) |
| v3.9 | `TrailingBuyZScoreBreakout` | Sweep 3 (11 tickers) | combined | — | |
| v3.18 | `TrailingExitZScoreBreakout` | Sweep 3 (11 tickers) | combined | real `trail_pct` column (swept via sl axis) | Replaces v2.18 |
| v3.21-27 | `TrailingBothZScoreBreakout` | Sweep 3 (11 tickers) | combined | real `trail_pct` column, one value per version: 1-7% | Replaces v2.10 + v2.13-17; `trail_pct` still not a free grid axis (sl slot taken by `trail_buy_pct`), so still one run per value — see "Grid axis meaning" above |

v3.4/v3.7/v3.8/v3.10/v3.11/v3.12/v3.13-17/v3.19-20 are deliberately skipped (TrendFiltered
and limit-order-family strategies not carried into v3.x; v3.8 coarse-grid TrailingExit was
redundant with v3.18's combined grid; v3.10 was a dropped "all trail_pct values in one
run" design, see `scripts/run_v3_backfill_sweep.sh` header). v3.28+ reserved for future
trailing-stop strategy variants (none defined yet).

Switched to the combined grid everywhere in v3.x (rather than coarse-by-default) after
confirming multiple current watchlist winners sit at the 1/2/4/5 low-end points — see
git history 2026-07-05 for the query.

### Optimization Approach

The optimizer searches for **winning islands** — regions of the (take profit, stop loss, hold time) parameter space where many neighboring nodes all produce positive alpha vs SPY. A single isolated peak is fragile; a broad plateau is robust.

**Evolution of the search approach:**
1. Smart grid search with generational refinement around alpha peaks
2. Fine-mesh adjustment around top performers — abandoned due to floating point precision issues on parameter adjustments
3. Full brute force — all nodes in the space, cached in SQLite. ~18k nodes per ticker, runs overnight. More reliable and gives a complete topology view.

### Key Components
- `run_optimization_sweep.py` — orchestrates the sweep, manages worker pool, writes progress to `active_phase_grid.json` (planned nodes) and `current_test.json` (live telemetry)
- `backtester.py` — single node evaluation. Kernels: `_simulate` (close-based, v1.5/v1.6), `_simulate_limit` (limit entry + intrabar SL, v1.7), `_simulate_trail` (close entry + trailing exit, v1.8), `_simulate_trail_buy`/`_simulate_trail_both` (bounce-confirmation entry, v1.9/v1.10), `_simulate_limit_trail` (limit entry + trailing exit, v2.11), `_simulate_close_limitexit` (close entry + limit-order TP exit, v2.12, added 2026-07-04). Corresponding wrappers: `run_backtest`, `run_backtest_v17`, `run_backtest_v18`, `run_backtest_v19`, `run_backtest_v110`, `run_backtest_v211`, `run_backtest_v212`. Sweep engine and Node Inspector dispatch to the correct wrapper based on strategy class (subclass checks — order-sensitive where one strategy subclasses another, e.g. `LimitOrderTrailingExit` must be checked before its parent `LimitOrderZScoreBreakout`). `prep_inputs` (line 16) maps each hourly bar to the *previous* day's SMA/std row (`i - 1`, fixed 2026-07-03) — previously mapped to that bar's own calendar day, letting every kernel variant see a same-day close that wasn't knowable intraday (see `docs/backlog.md` "Look-ahead bias..."). Single fix point shared by all kernel variants and every page that reuses them. `run_optimization_sweep.py`'s `_config_trail_pct()` (added 2026-07-04) reads `config.execution.trail_pct` for `TrailingBothZScoreBreakout`'s exit-side trail % — see "Grid axis meaning by strategy" above for why this can't be a real grid axis.
- `strategies.py` — strategy class definitions. `check_signal(ctx)` and `check_exit(ctx)` take a context dict (not individual args) — per-class implementations that mirror each backtest kernel's exact logic (bar-close vs continuous per exit reason). `z_score_threshold` stored in `self.params`. The sweep and Node Inspector both pass it to `run_backtest` explicitly.
- `scripts/verify_live_parity.py` — replays `active_signals.py`'s real `compute_buy_signal`/`check_sell_condition` (via a throwaway per-run SQLite DB) bar-by-bar against the Numba backtest kernels for a given ticker/node; diffs trade-by-trade and reports first divergence. Validates the live *orchestration* layer, not just `strategies.py` (see `docs/adr/0001-live-parity-sim-vs-backtest.md`). Since the `prep_inputs` look-ahead bias fix (2026-07-03), the plain `ZScoreBreakout` case reports a clean MATCH. The `LimitOrderZScoreBreakout` "mismatch" turned out to be a bug in this harness, not the kernel or live code — `replay()` was checking the entry signal against bar Close instead of Low (fixed 2026-07-04); production `active_signals.py` actually polls continuously all day for limit-entry nodes (`notify_limit_fill`, 5-min cadence, not gated by the signal-window check), so the kernel's Low-based assumption was the accurate one all along. Now also covers `LimitOrderTrailingExit` (v2.11). One remaining, unrelated, low-priority WIN/TWIN labeling discrepancy on the v1.8 case (not yet root-caused, cosmetic — entry/exit price/timing match).
- `scripts/run_v2_backfill_sweep.sh` — bias-corrected reindex wrapper, one major version up from v1.x (v2.4-v2.11; v2.11 has no v1.x precursor, see `LimitOrderTrailingExit` above). Scope: 53-ticker liquid/non-crypto/index-only/non-dupe list. Optional ticker-override arg for sanity checks (e.g. `./scripts/run_v2_backfill_sweep.sh v2.5 AGQ`) still goes through the version→strategy `patch_config` guard, so a manual override can't silently mismatch strategy and version tag.
- `pages/1_Spatial_Topology.py` — 4D Plotly scatter of parameter space, shows planned nodes in blue and completed nodes colored by alpha
- `pages/2_Node_Inspector.py` — re-runs backtest for a selected node, shows trade ledger and quarterly breakdown; Hurst/ADF analysis is opt-in (checkbox), lazy-loaded on demand
- `pages/4_Portfolio.py` — portfolio backtester with two node sources: (1) watchlist toggle, (2) DB research nodes (filter by version/alpha/trades/z). Gantt timeline + SPY/TQQQ overlay + concurrent positions panel. Hurst/ADF overlay removed (not actionable).
- `cache/trading_universe.db` — SQLite cache, nodes never re-evaluated once computed
- `config.json` — single source of truth for runtime config. `app.py` reads/writes directly — DB copy removed.

### Performance
- `ProcessPoolExecutor` with up to 10 workers (configurable via `execution.max_workers`)
- Phase 2 runs `execution.max_generations` times (default 1), re-centering island mesh on refined peaks each generation
- SQLite WAL mode for concurrent writes
- L3 cache optimization identified as next performance improvement (suggested by Gemini)
- Sweep auto-runs `refresh_dropdown_cache()` + `refresh_pivot_cache()` once on true completion (not between generations). `run_optimization_sweep.py --skip-cache-refresh` (added 2026-07-03) skips this — used by `run_v2_backfill_sweep.sh`'s no-arg (all-versions) path, which defers to a single combined refresh after all 7 versions finish instead of once per version (each refresh takes 2-4 min; not worth paying 7x when nobody's watching the Streamlit pages mid-run). Single-version/ticker-override invocations still refresh normally.
- `sweep_runs` DB table — one row per sweep execution: version, timestamps, status, strategies, tickers, phase_reached, config_json snapshot, log_file. `start_sweep_run`/`update_sweep_run` in `run_optimization_sweep.py` wire this automatically.
- `identify_island_candidates` scoped to `allowed_tickers` (current run's tickers) — prevents silently dropping candidates whose B&H data wasn't cached for the current run
- Cron job runs sweep daily at 4:15am
- `backtest_cache.fixed_sl` column (v1.8+) — the swept `stop_loss` column holds trail_pct/trail_buy_pct for those strategies, not the real fixed SL; cache-hit lookups key on `fixed_sl` too so re-running with a different `execution.fixed_stop_loss` recomputes instead of silently reusing stale results
- `dispatch_parallel_grid` batches `backtest_cache` writes via `executemany()` with an explicit column list instead of one positional `execute()` per node — benchmarked 2026-07-03: a 50-row batch (original value) was 28% *slower* than per-row inserts, because it committed more often (every 50 rows vs the old every-100); the `executemany()` call itself isn't the cost, commit frequency is. Bumped `batch_size` to 5000 (2026-07-03, later session) — negligible recompute-on-crash cost at measured ~399 nodes/sec throughput (~12s), negligible transaction-hold time (~7ms benchmarked for 2000 rows), and no live writer (`active_signals.py`) contends for the DB during an offline/unattended run. Real bottleneck is compute, not DB/IPC (profiler re-run confirms prior session's "88% result collection overhead" was a parallel-kernel-compute measurement artifact, not real overhead).
- `ProcessPoolExecutor` initializer (`_warmup_worker`) pays each Numba kernel's one-time JIT compile cost (~600ms cold) at worker startup instead of on a random real grid node mid-sweep — all 5 kernels (`_simulate`, `_simulate_limit`, `_simulate_trail`, `_simulate_trail_buy`, `_simulate_trail_both`) warmed with tiny dummy arrays
- `backtest_cache` indexes (`init_idempotent_db`): `idx_bc_version_window`, `idx_bc_version_ticker_strategy`, `idx_bc_version_return`, `idx_bc_ticker` — all verified in-use via `EXPLAIN QUERY PLAN` against real page queries (2026-07-03). Two indexes dropped as dead weight (pure insert-time cost, no query benefit): `idx_bc_version_ticker` (strict prefix of `idx_bc_version_ticker_strategy`, planner never chose it) and `idx_bc_version_ticker_z_return` (no query in the codebase matches its `(version, ticker, z_score_threshold, strategy_return DESC)` shape — see `docs/backlog.md` Low Priority for the exact `CREATE INDEX` to restore if ever needed). Matters more now that Phase 3's full mesh (108k inserts/ticker) is ~9x Phase 1's coarse volume.

---

## Layer 3 — Active Signals

`active_signals.py` — polls price data, fires BUY/SELL alerts to console and Slack. Fetches fresh data for all watched tickers at the start of each poll cycle — no separate data collector process needed.

- **Multi-watchlist**: `watchlists` DB table (id, name, is_active). One list is designated active — that's what the signal loop monitors. Same node can exist in multiple lists (UNIQUE constraint is scoped per list).
- **Node mode**: `watch_list.mode` — `live` fires full Slack BUY alerts; `research` logs signal to console only (no Slack, no position tracking).
- `watch_list` DB table — nodes selected for monitoring, scoped to a watchlist
- `open_positions` DB table — tracks entries pending exit; `trail_state` TEXT column stores per-position trailing-stop state (peak price, activated flag) as JSON. `trail_pct`/`fixed_sl` columns (also on `watch_list`) hold the real trailing % and fixed stop-loss % for v1.8/v1.9/v1.10 nodes — the swept `stop_loss` column on those strategies actually holds trail_pct/trail_buy_pct, not the real SL, so `check_sell_condition` reads the real values from these columns instead. `signal_time` (not `entry_time`, which is real-time fill time) is the bar the TIME-exit hold count is measured from, matching backtest kernel semantics (counts hourly bars in cached data, not wall-clock hours)
- Entry/exit logic delegated to strategy classes in `strategies.py` — no signal logic in `active_signals.py`
- **Slack Socket Mode** — bot token + app token; BUY/SELL messages have interactive Executed/Skipped buttons, price entry modal, chart image upload
- **BUY message** — shows market price, share count at $50k notional, and max notional / max shares at 1% of avg daily vol (liquidity ceiling from `tickers` table)
- **Morning report** — fires at startup and daily at 7 AM ET; dark-theme chart (30 trading days lookback, positional x-axis, right-side y-axis, both ±2σ generic band and node-specific z-threshold trigger line). Leading line: `{emoji} *TICKER* — BUY (bar-close/limit) — version — trigger $X`. Chart attached only when within 5% of trigger.
- **Current price** — uses `yfinance history(period='1d', interval='1m', prepost=True)` to capture pre/post-market; falls back to cached hourly close on failure
- Signal indicators use prior closed day's SMA/Std (not today's intraday close) — matches live trading semantics
- `--ticker TICKER` flag to filter the poll loop to specific tickers
- No brokerage integration — manual execution
- `scripts/live_test.py` — synthetic TEST ticker for end-to-end Socket Mode testing

### Winners Page

`pages/3_Winners.py` — Streamlit leaderboard of top nodes per ticker per z_score_threshold for a selected version.

- Filters: version, ticker, strategy, z_score_threshold multiselect, min trades, min alpha, beat asset B&H toggle, top N per ticker per threshold
- Groups by `(ticker, z_score_threshold)` — allows direct comparison of z=2.0 vs z=2.5 vs z=3.0 best nodes side by side
- Dismiss per `(ticker, strategy, version)` — persisted to `cache/dismissed_tickers.json`
- Click row → Watch / Dismiss / Open in Node Inspector actions
- Open in Node Inspector passes all params (window, TP, SL, hold, z_score_threshold) via session state — dropdowns auto-select on arrival
- Sidebar watchlist picker — create/delete/set-active named lists; active list drives signal loop
- Watch list table at bottom with inline label editing, mode toggle (live/research), and remove-by-uncheck

### Sweep Status Page

`pages/5_Sweep_Status.py` — per-ticker sweep progress for a selected version. Shows nodes cached vs expected, SUCCESS vs NO_TRADES counts, last data date, ASCII progress bar. Auto-refreshes every 30s. Useful for monitoring long-running sweeps and diagnosing gaps.

### Strategy Page

`pages/6_Strategy.py` — renders `docs/strategy.md` in the app. Living reference for signal logic, edge cases, and trading rules.

### Hurst Filter Page

`pages/7_Hurst_Filter.py` — sweeps Hurst cutoff across all qualifying watchlist nodes. Compares MR (mean-reverting, H<cutoff) vs MO (momentum, H≥cutoff) entry filters. Result: not actionable — see `docs/research.md`.

### ADF Filter Page

`pages/8_ADF_Filter.py` — same structure for ADF p-value filter. Non-stationary (p≥cutoff) vs stationary entries. See `docs/research.md`.

### Shared Modules

- `hurst.py` — `_hurst_vectorized` + `ROLLING_WINDOW=200`. Imported by Node Inspector and `active_signals.py`.

### Screener Page

`pages/4_Screener.py` — filter the full ticker universe before deciding what to sweep.

- Reads from `tickers` table in `cache/trading_universe.db`
- Filters: symbol/name search, AUM, dollar volume liquidity (investment × multiplier), leverage (2x/3x), inverse toggle, single-stock underlier toggle, has-data toggle, underlying index search, performance
- Columns: stock_underlier, index_underlier, leverage, inverse, has_data, price, dollar vol, AUM, performance, signals
- "Add to config.json" button adds selected tickers to `target_tickers` for the next sweep

### Open Positions Page

`pages/10_Open_Positions.py` — live view of manually entered positions tracked in `open_positions` DB table.

- Reads from `open_positions` in `cache/trading_universe.db`
- Fetches current price via `yfinance fast_info.last_price` at page load
- Shows: signal price, entry price, drift % (entry vs signal), current price, unrealized P&L%, TP price, SL price, hours held, hours remaining until time-exit, entry time
- TP = entry_price × (1 + tp%), SL = entry_price × (1 - sl%) — display only, Schwab stop is set separately at lower_band × (1 - (sl%+1%))
- Manual refresh button; no auto-refresh

### Ticker Universe Table

`tickers` table in `cache/trading_universe.db` — populated by `scripts/import_tickers.py` from screener CSV exports.

- Key derived columns: `leverage` (parsed from description), `inverse` (from fund type/description), `has_data` (cache CSV exists), `stock_underlier` / `index_underlier` (classified from underlying index + description)
- Re-run `python scripts/import_tickers.py <file.csv>` to replace with a new screener export

See `docs/strategy_architecture.md` for the target node/strategy data model (deferred until second strategy is added).

---

## Future — Live Trading Engine

If a brokerage API key is added (e.g. Alpaca, IBKR), Layer 3 can be extended to:
- Submit orders automatically on signal trigger
- Track open positions via broker API (not manual state)
- Handle fills, partial fills, and slippage reporting
- End-of-day reconciliation against broker blotter
