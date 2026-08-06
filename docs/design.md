# Design Document — Trading Alpha Engine

## Architecture Overview

Three discrete layers, each independently runnable:

1. **Data Collection** — daemon fetches and caches hourly OHLCV data
2. **Parameter Optimization** — brute force search for robust alpha islands
3. **Active Signals** — apply optimized params to current market state, surface entries/exits (planned)

---

## Layer 1 — Data Collection

- `data_collector.py` polls every 5 minutes, calls `data_manager.py` for incremental updates
- Data stored as `cache/research/{ticker}_1h.csv`, SPY always included as benchmark
- Incremental backfill with deduplication — overlapping buffer handles weekends/holidays
- Ticker universe defined in `tickers.json` — plain JSON array, read at startup
- Cron job runs `data_collector.py --once` daily at 6:30 AM via `scripts/run_data_collector.sh`, logs to `logs/data_collector_daily.log` (runs before 7 AM morning report so bands are fresh)

**Addendum (2026-07-14)**: `cache/` split into three buckets that were previously all flat in one folder: `cache/live/` (`trading_live.db` + backups, `trading_sim.db`, the daemon heartbeat — the real trade record), `cache/research/` (`trading_universe.db` + backups, all ticker `_1h.csv`, `watchlist_sweep.db` — regenerable), and `output/` (`*_trades.xlsx`, `live_backups/` hourly DB snapshots, one-off migration artifacts — not cache at all). See `CLAUDE.md`'s Runtime Artifacts section for the current layout. Crontab backup jobs updated to match.

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

**Addendum (2026-07-07)**: `trail_pct` renamed to `trail_sell_pct` for symmetry with `trail_buy_pct`. For `TrailingBothZScoreBreakout` specifically, `take_profit` was also split out — it never actually took profit for this strategy, it armed the trailing-sell mechanism, so that value now lives in a new `arm_sell_pct` column instead (`take_profit` is `NULL` on `TrailingBothZScoreBreakout` rows, real take-profit % everywhere else it's used). Done DB-side + `active_signals.py` this session; `run_optimization_sweep.py`/Streamlit pages/scripts still read the old names — see `docs/backlog_cache.md`.

**Addendum 2 (2026-07-07, later same session)**: `run_optimization_sweep.py` fixed to match — was fully broken (`no such column: trail_pct`) since the DB-side rename above had already landed. Renamed all internal SQL to `trail_sell_pct`, and added a new `axis_tp` column (`backtest_cache`, write-time-computed in Python as the raw swept 'tp' grid value regardless of strategy — `take_profit` for everything except `TrailingBothZScoreBreakout`, `arm_sell_pct` for that one). `axis_tp` is what the primary key and every internal island/cliff-box/candidate-selection query (`dispatch_parallel_grid`, `run_phase2_island`, `run_phase25_cliff_box`, `identify_full_mesh_candidates`) key off now — SQLite's composite PK can't dedupe on `take_profit` once it's NULL for TrailingBoth rows (`NULL` never equals `NULL`), so raw `take_profit` was unusable there. Backed up `trading_universe.db` first (`cache/trading_universe_pre_axis_tp.db.bak`); the table-rebuild migration for the live 75.6M-row table was still running as of session end — **check `cache/axis_tp_migration.log` next session and confirm it finished + row count matches before trusting any fresh sweep run.** Streamlit pages/other scripts still read the old names — see `docs/backlog_cache.md`.

Also added `watch_list.cached_avg_vol_10d` (`active_signals.py`) — `_build_buy_blocks`'s `tickers.avg_vol_10d` lookup (position-sizing cap) now wraps the research-DB query in try/except and falls back to this cached-on-success value, since that lookup had no busy-timeout and could crash the daemon if it collided with a research-DB migration/lock. Verified both paths via `scripts/test_avg_vol_fallback.py`.

**Addendum 3 (2026-07-07, same session, after wrap)**: the `axis_tp` migration got killed mid-script (user asked to bump `cache_size`, decided 12GB was too aggressive for a 15GB-RAM box and risky as a permanent default given `dispatch_parallel_grid` opens one connection per `ProcessPoolExecutor` worker — up to 10 — so killed it instead). Discovered `cursor.executescript()` does **not** wrap `CREATE`/`INSERT`/`DROP`/`RENAME` in one transaction — each auto-commits separately — so the kill landed after `DROP TABLE backtest_cache` had already committed, leaving only `backtest_cache_new` (missing just the final `RENAME`). A stray leftover process from an earlier ad hoc DB check was also still holding the file open (`fuser`) — killed. Found the WAL had grown to ~32GB unchecked from the `kill -9`.

While investigating, found a **serious host-level issue**: `df -h` inside WSL reports 742GB free, but the actual Windows `C:` drive has only ~1.95GB free (`ext4.vhdx`, 324.8GB on the host, is a sparse file — WSL's own filesystem free-space number is meaningless once the host can't grow it further). Same failure class as the WSL crash earlier this session. Paused everything non-essential; user is restarting Windows/WSL after next session close to reclaim host space — **don't trust `df -h` for headroom decisions until that restart is confirmed.**

Wrote `scripts/recover_migration_wal.py` to checkpoint the WAL safely and verify `backtest_cache_new`'s integrity before touching anything — confirmed **complete**: 86,213,203 rows, exactly matching the pre-migration backup, all `TrailingBothZScoreBreakout`/`axis_tp` invariants correct. The `INSERT...SELECT` had fully committed before the kill; only the final `RENAME` was missing (and the WAL was already checkpoint-empty by the time the script ran — the 32GB file size was stale). Ran `scripts/finish_axis_tp_rename.py` (rename + rebuild the 4 indexes on 86M rows) — **still running as of this session wrap, confirm completion next session** (check `ps aux | grep finish_axis_tp`, then verify `backtest_cache` row count = 86,213,203 and all 4 indexes exist).

**Addendum 4 (2026-07-07, next session)**: `axis_tp` migration confirmed complete — 86,213,203 rows, 4 indexes rebuilt (already committed as `ae44410`). Ran the planned sanity test: fresh `TrailingBothZScoreBreakout` backfill for an existing AGQ node vs. the pre-migration cached row. Numbers didn't match at first (fresh: 48 trades/368% return vs. cached: 47 trades/323%) — turned out to be expected data drift (2 extra trading days appended by the daily collector since the row was cached 2026-07-05), not corruption. Confirmed migration correctness properly instead via a direct row-for-row diff between the pre-migration backup and the live table (exact match) plus `axis_tp`/`arm_sell_pct` consistency spot-check. Dropped the 4 stale duplicate tables (`open_positions`/`trade_log`/`watch_list`/`watchlists`) from `trading_universe.db` — orphaned since the live/research DB split, confirmed nothing reads them from that file (all real reads target `trading_live.db`), backed up first to `cache/stale_tables_backup_20260707.sql`.

Propagated the `take_profit`→`axis_tp` / `trail_pct`→`trail_sell_pct` fix to `pages/0_Top_Pivot.py` (3 queries, including a real bug: the watchlist-pivot join compared `b.take_profit = w.take_profit`, which is `NULL = NULL` — always false — for 6 of 8 live `TrailingBothZScoreBreakout` tickers, silently breaking that section) and `db_cache.py` (`CLIFF_GRID_SQL` + `refresh_best_nodes_cache()`, the latter reproduced as a real nightly-cron crash via `TypeError: int() argument ... not 'NoneType'`, fix applied but not yet re-verified end-to-end). Remaining files (`Node_Inspector.py`, `Winners.py`, `Portfolio.py`, `Open_Positions.py`, `export_cliff_safety.py`, `verify_live_parity.py`, `fill_trail_pct_gaps.py`) not yet touched — same pattern applies. Note: `cache/watchlist_sweep.db` is a separate, never-migrated snapshot DB where `trail_pct`/`take_profit` are still the correct column names — don't rename those.

Added a nullable `account` column to `watch_list` (`trading_live.db`) and populated it for watchlist 7 per the user's stated real-money allocations (brokerage: AGQ/TQQQ/GDXU; sep: EDC; ira: SOXL/KORU/HIBL/YANG/DPST/NUGT). Chosen as the lower-risk additive option over a separate `accounts` table — user said they might still switch to a table later once P&L tracking needs grow. See `docs/backlog_cache.md` "Live trading behaviors" for the larger unstarted P&L/compounding/Slack-redesign scope this connects to.

**Addendum (2026-07-13)**: extended `account` to `open_positions`/`trade_log` (migration in `ensure_tables()`, backed up first). `open_position()`/`log_trade_entry()` now capture `node.get('account')` at execution time rather than relying on `watch_list.account`'s current value — needed because that value can change later (e.g. LABU ira→roth) and would otherwise mis-attribute historical trades. `pages/4_Portfolio.py` gained an "Account Performance (live)" section (realized win-rate/compounded-return from `trade_log`, unrealized $ P&L from `open_positions`, both grouped by account); `pages/10_Open_Positions.py` shows the column. Pre-migration open positions (AGQ/HIBL/EDC/SOXL) show as `unknown` — no historical backfill possible.

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

**Index added 2026-07-07**: `idx_bc_ticker_strategy_version ON backtest_cache(ticker, strategy, version)` —
none of the pre-existing indexes had `ticker` paired with `strategy`/`version`, so any
`ticker IN (...) AND strategy=? AND version LIKE '...'` filter (a common shape for
watchlist-scoped exploration) fell back to scanning most of the table. Added to both
`cache/trading_universe.db` and the `cache/watchlist_sweep.db` sandbox (see `docs/backlog.md`
"Watchlist-scoped trade-cache sandbox").

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
| v3.28-50 | `TrailingBothZScoreBreakout` | Sweep 3 (11 tickers), or `ALL53` | combined | real `trail_pct` column, one value per version: 8-30% (`version = trail_pct% + 20`, e.g. v3.29=9%, v3.50=30%) | Sparse-then-fill extension (2026-07-05 evening) — v3.18/NUGT/SOXL/TQQQ showed `TrailingExitZScoreBreakout` doing much better at wide trail_pct (9-24%) than `TrailingBoth`'s tested 1-7% range; every single-percent slot 8-30% is wired in `scripts/run_v3_backfill_sweep.sh` so no further script edits are needed to run any of them. `scripts/fill_trail_pct_gaps.py` recommends which neighboring single-percent versions to run next based on each ticker's best value so far. `ALL53` is a ticker-arg shorthand for the full 53-ticker universe (same list as `run_v2_backfill_sweep.sh`). |

v3.4/v3.7/v3.8/v3.10/v3.11/v3.12/v3.13-17/v3.19-20 are deliberately skipped (TrendFiltered
and limit-order-family strategies not carried into v3.x; v3.8 coarse-grid TrailingExit was
redundant with v3.18's combined grid; v3.10 was a dropped "all trail_pct values in one
run" design, see `scripts/run_v3_backfill_sweep.sh` header). v3.28+ reserved for future
trailing-stop strategy variants (none defined yet).

Switched to the combined grid everywhere in v3.x (rather than coarse-by-default) after
confirming multiple current watchlist winners sit at the 1/2/4/5 low-end points — see
git history 2026-07-05 for the query.

### v4 (2026-07-14/15) — fill-optimism resolution bounds + entry_timing + phase tagging

`_simulate_trail_both` now computes three parallel trailing-buy bounce-fill
resolutions per node instead of one, since none of hourly OHLC proves the true
intrabar path: **possible** (existing/unchanged, Low-before-High assumption),
**pessimistic** (new, mirror-image High-before-Low assumption), **certain** (new,
only resolves a fill when provable regardless of ordering). Verified via
`scripts/verify_v4_fill_bounds.py`: `possible` is byte-for-byte unchanged from
pre-v4 (exact match against historical v3.x rows once re-run against the
same-dated truncated price history). **Important correction (found 2026-07-15,
KORU)**: `pessimistic` is *not* a rigorous aggregate lower bound on `possible`
despite being provably same-bar-or-later/same-or-worse-price *per fill event* —
once it defers past the bar where `possible` already fired, the two trade
sequences diverge independently, and continued deferral can let `pessimistic`'s
running_low fall further before its own eventual fill, occasionally producing a
*better* aggregate result (same mechanism that lets `certain` beat `possible`).
None of the three is a mathematically guaranteed bound on the others in
aggregate — only per-fill-event trigger-price comparisons have proven orderings.
`ROBUST_ALPHA_SQL` = `MIN(possible, pessimistic, certain)` is still used for
island/cliff-safety ranking as the best available conservative heuristic, just
not a provable floor.

`entry_timing` (`close`/`open_check`) and `stop_loss` (for `uses_fixed_sl`
strategies) are campaign-level constants — like the pre-2026-07-05 `trail_pct`
pattern, not real grid axes (3-axis island cap, see below) — but unlike that
pattern, every v4 campaign shares one version string (`v4`); the real
`stop_loss`/`entry_timing` columns disambiguate campaigns instead
(`run_optimization_sweep.py::_campaign_scope_sql`). New `phase` column
(plain data, in-place `ALTER TABLE ADD COLUMN`, no PK rebuild) tags each row
with whichever phase (`Phase1-Coarse`/`Phase2-Island`/`Phase2.5-CliffBox`/
`Phase3-Full`) first computed it — the caching layer means a node keeps the
label of the *first* phase that reached it even if a later phase's mesh would
have also covered it, which is exactly what's needed to measure whether Phase 3
(originally meant as a fallback for when island search can't find a good node,
not a routine step) ever actually finds something the cheaper phases missed.
New `sl_sweep_summary` rollup table, one row per completed campaign.

**Phase-3 value-add answered (2026-07-15)**: across all 30 tagged SOXL+KORU SL-sweep
campaigns, Phase 3 (full mesh) never held the best `MIN(possible,pessimistic,certain)`
alpha node — Phase1 (coarse) or Phase2 (island) always did, Phase2.5 (cliff-box) won a
few. Island/cliff-safety selection (Checkpoint 2) was independently confirmed to only
ever read Phase1+2+2.5 data — Phase 3 was never part of that calculation to begin with.
Added `--max-phase {1,2,2.5,3}` (default `3`, unchanged pipeline behavior) to
`run_optimization_sweep.py` so future campaigns can skip Phase 3 outright
(`run_phase3_full` also now logs a `Phase3 best=... (pre-Phase3 best=...,
IMPROVED/no improvement)` line for live confirmation on any run that does still include
it). New `generation` column (nullable, `Phase2-Island` rows only) records which
island-search generation (1-indexed, `config.execution.max_generations`) first computed
a row, to similarly test whether the generation loop's extra passes earn their cost —
not yet analyzed. Also found and fixed: `phase` was never created by
`init_idempotent_db()` (only existed because it was added by hand against the live DB
in session 10) — a fresh DB would have failed on `INSERT ... phase`; now a proper
`ALTER TABLE ADD COLUMN`, alongside `generation`.

**New `same_day_block` kernel param (2026-07-16)**: `_simulate_trail_both` gained an
optional `same_day_block=False` argument (threaded through `run_backtest_v110`) that
mirrors `schwab_safety`'s real cash-account same-day-re-buy rule — a fresh signal is
ignored (not dropped forever, naturally re-checked on the next eligible target-hour bar)
on any day matching that resolution's own most recent exit day, tracked independently
per possible/pessimistic/certain. Default-off, fully backward compatible. Single-ticker
testing (not yet a real campaign axis/schema column) showed this materially reshapes
which nodes look best once same-day contention is priced in — some tickers (HIBL, DPST,
LABU) are structurally robust to it, others (YANG, GDXD, GDXU, KORU) lose most of their
unconstrained-baseline alpha. See `docs/backlog_cache.md` for the full writeup and
next-step plan (formalizing it as a real per-campaign axis is still undecided).

**Live/backtest sizing-formula gap found (2026-07-16)**: the live trailing-buy sizing
formula (`signals_blocks.py` — `shares = target_notional // price`, using the
*signal-time* price) and the backtest's compounding formula
(`run_optimization_sweep.py::_summarize_trades` — `((Return+1).prod()-1)`) both assume
an exact dollar notional can be deployed on every trade. Neither can, in different ways:
live because a trailing buy's real fill price isn't known until after the order is
sized, backtest because it never models share-count rounding or a sizing-price/fill-
price mismatch at all. Real KORU/AGQ trade reconstruction showed the practical impact is
smaller than the theoretical worst case (a few percent divergence over 31/37 trades, not
runaway compounding), because overshoot and undershoot trades roughly offset in
practice — but this is unverified for the rest of the watchlist. See
`docs/backlog_cache.md` for the full mechanism, real numbers, and the agreed fix plan
(conservative live sizing formula + backtest compounding rewrite, both still unimplemented).

**Emerging finding, not yet conclusive (2026-07-15)**: across both tickers tested,
`entry_timing='open_check'` won every single tested campaign (17/17, SOXL 10/10 + KORU
7/7), and `robust_alpha` showed a real declining trend as `stop_loss` loosened (SOXL
3% ≈ 2.5x better than 30%). Directly contradicts the current live config (15% flat SL,
close-only entry) — worth confirming across the rest of the watchlist before acting on
it (see `docs/backlog_cache.md`).

**Gap-through-trigger fix (2026-07-19)**: found while scoping the trailing-buy
idle-capital re-sizing item — real overnight/intraday gaps exceed a node's
`trail_buy_pct` on **19-44% of trading days** across the active v4 watchlist
(mean upward gap 1.6-4.4% vs. a typical 1% trigger). `_simulate_trail_both`
(all three resolutions) and `_simulate_trail_buy` always filled a trailing-buy
entry at the theoretical `running_low × (1 + trail_buy_pct)` trigger price,
even when the bar's own `Open` had already proven the trigger was blown
through (`certain`'s `op >= buy_trigger_prior` branch detected this and then
still used the stale price) — a distinct fill-optimism source from the
already-fixed Low/High-ordering one, closer in spirit to the deferred-SL gap
bug found and fixed 2026-07-17 (see the `simulate_trail_both_deferred_sell`
entry above) but on the entry side instead of the exit side. **Fixed**: all
three resolutions in `_simulate_trail_both`, plus `_simulate_trail_buy` (which
gained a new `opens` parameter it didn't previously take), now fill at the
real `Open` whenever it has already crossed the trigger confirmed through the
prior bar, before falling through to the existing Low/High logic. Verified via
a new synthetic-gap unit test (`tests/test_TrailingBuyZScoreBreakout.py`,
confirmed to fail pre-fix at the stale price and pass post-fix at the real
Open) and byte-identical parity between the fixed numba kernel and the fixed
`export_trades.simulate_trail_both_annotated` mirror on real SOXL/KORU/AGQ
data. **Not yet backfilled**: this changes `possible`/`pessimistic`/
`certain`/robust-alpha for every trailing-buy/-both node on file — needs the
same resweep treatment as the original fill-optimism fix, not yet run.

**Gap policy simulation (2026-07-19)**: new `export_trades.simulate_trail_both_gap_policy`
(configurable `skip_threshold` — `None` reproduces the fixed kernel's default
"always resize and enter at the real Open"; a float skips the entry attempt
entirely when the gap overshoots the trigger by more than that fraction) and
driver `scripts/sim_gap_policy.py` (sweeps `{None, 3%, 5%, 10%, 15%}` per
ticker, `output/gap_policy_summary.csv`), built to decide the live-side policy
empirically rather than by guessing (matches the `sim_chaos_monkey.py`/
`sim_delayed_sell.py` pattern). **Result across the full 18-ticker v4
watchlist**: gap-through trades have consistently low win rates (8.5%-41.8%,
most 10-30%) confirming they're genuinely worse setups, but skipping them
moves total compounded return only modestly and the direction is
ticker-specific/inconsistent — no universal threshold shows a clear
consistent edge, and no ticker shows a dramatic blowup avoided by skipping.
Leaning toward shipping "always resize and enter" (simplest, matches the
corrected kernel default) for the live-side fix (Part 3, not yet built:
`schwab_client.cancel_order`, `pending_buys.order_id`, a daily pre-open
`check_gap_risk()` — see `docs/backlog_cache.md`), pending final confirmation.

### Optimization Approach

The optimizer searches for **winning islands** — regions of the (take profit, stop loss, hold time) parameter space where many neighboring nodes all produce positive alpha vs SPY. A single isolated peak is fragile; a broad plateau is robust.

**Evolution of the search approach:**
1. Smart grid search with generational refinement around alpha peaks
2. Fine-mesh adjustment around top performers — abandoned due to floating point precision issues on parameter adjustments
3. Full brute force — all nodes in the space, cached in SQLite. ~18k nodes per ticker, runs overnight. More reliable and gives a complete topology view.

### Key Components
- `run_optimization_sweep.py` — orchestrates the sweep, manages worker pool, writes progress to `active_phase_grid.json` (planned nodes) and `current_test.json` (live telemetry)
- `backtester.py` — single node evaluation. Kernels: `_simulate` (close-based, v1.5/v1.6), `_simulate_limit` (limit entry + intrabar SL, v1.7), `_simulate_trail` (close entry + trailing exit, v1.8), `_simulate_trail_buy`/`_simulate_trail_both` (bounce-confirmation entry, v1.9/v1.10), `_simulate_limit_trail` (limit entry + trailing exit, v2.11), `_simulate_close_limitexit` (close entry + limit-order TP exit, v2.12, added 2026-07-04). Corresponding wrappers: `run_backtest`, `run_backtest_v17`, `run_backtest_v18`, `run_backtest_v19`, `run_backtest_v110`, `run_backtest_v211`, `run_backtest_v212`. Sweep engine and Node Inspector dispatch to the correct wrapper based on strategy class (subclass checks — order-sensitive where one strategy subclasses another, e.g. `LimitOrderTrailingExit` must be checked before its parent `LimitOrderZScoreBreakout`). `prep_inputs` (line 16) maps each hourly bar to the *previous* day's SMA/std row (`i - 1`, fixed 2026-07-03) — previously mapped to that bar's own calendar day, letting every kernel variant see a same-day close that wasn't knowable intraday (see `docs/backlog.md` "Look-ahead bias..."). Single fix point shared by all kernel variants and every page that reuses them. `run_optimization_sweep.py`'s `_config_trail_pct()` (added 2026-07-04) reads `config.execution.trail_pct` for `TrailingBothZScoreBreakout`'s exit-side trail % — see "Grid axis meaning by strategy" above for why this can't be a real grid axis.
- `strategies.py` — strategy class definitions. `check_signal(ctx)` and `check_exit(ctx)` take a context dict (not individual args) — per-class implementations that mirror each backtest kernel's exact logic (bar-close vs continuous per exit reason). `z_score_threshold` stored in `self.params`. The sweep and Node Inspector both pass it to `run_backtest` explicitly. **`check_exit` gained an Open-first gap check on SL/trailing-stop (2026-07-20)** for `TrailingBothZScoreBreakout`/`TrailingExitZScoreBreakout`/`LimitOrderTrailingExit` — mirrors the backtest kernel's 2026-07-20 exit-side gap-through-trigger fix, which had only been applied to `backtester.py`, not this live/paper-facing mirror. `ctx` now carries an `'open'` key (falls back to `current_price` if unavailable, same pattern as `low`/`high`); `signals_compute.check_sell_condition` threads it through as a new `open_price` param, and both call sites (`active_signals.py`, `paper_trading.py`) extract the real bar Open at bar-close. Verified via full bar-by-bar parity against real historical trades (151/151 SOXL, 128/129 AGQ matched the corrected kernel exactly).
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
- `backtest_cache.win_twin_rate` column (added 2026-07-05): `win_rate` only counts `Result=='WIN'` exactly, excluding profitable `TIME`-exit trades (`TWIN`) — found while investigating a KORU node whose 21% win_rate looked alarming next to another node's 71%, but turned out to yield about the same alpha; per-trade breakdown showed 71% of its trades were actually profitable, just via `TWIN`. `win_twin_rate = (WIN+TWIN)/trades` is the real profitable-trade rate, computed alongside `win_rate` in `run_single_backtest_node_isolated`/`dispatch_parallel_grid` and shown in `pages/0_Top_Pivot.py`'s Cliff Safety table. Old rows keep `win_twin_rate=0` (not recomputed retroactively).
- `dispatch_parallel_grid` batches `backtest_cache` writes via `executemany()` with an explicit column list instead of one positional `execute()` per node — benchmarked 2026-07-03: a 50-row batch (original value) was 28% *slower* than per-row inserts, because it committed more often (every 50 rows vs the old every-100); the `executemany()` call itself isn't the cost, commit frequency is. Bumped `batch_size` to 5000 (2026-07-03, later session) — negligible recompute-on-crash cost at measured ~399 nodes/sec throughput (~12s), negligible transaction-hold time (~7ms benchmarked for 2000 rows), and no live writer (`active_signals.py`) contends for the DB during an offline/unattended run. Real bottleneck is compute, not DB/IPC (profiler re-run confirms prior session's "88% result collection overhead" was a parallel-kernel-compute measurement artifact, not real overhead).
- `ProcessPoolExecutor` initializer (`_warmup_worker`) pays each Numba kernel's one-time JIT compile cost (~600ms cold) at worker startup instead of on a random real grid node mid-sweep — all 5 kernels (`_simulate`, `_simulate_limit`, `_simulate_trail`, `_simulate_trail_buy`, `_simulate_trail_both`) warmed with tiny dummy arrays
- `backtest_cache` indexes (`init_idempotent_db`): `idx_bc_version_window`, `idx_bc_version_ticker_strategy`, `idx_bc_version_return`, `idx_bc_ticker` — all verified in-use via `EXPLAIN QUERY PLAN` against real page queries (2026-07-03). Two indexes dropped as dead weight (pure insert-time cost, no query benefit): `idx_bc_version_ticker` (strict prefix of `idx_bc_version_ticker_strategy`, planner never chose it) and `idx_bc_version_ticker_z_return` (no query in the codebase matches its `(version, ticker, z_score_threshold, strategy_return DESC)` shape — see `docs/backlog.md` Low Priority for the exact `CREATE INDEX` to restore if ever needed). Matters more now that Phase 3's full mesh (108k inserts/ticker) is ~9x Phase 1's coarse volume.

---

## Layer 3 — Active Signals

`active_signals.py` — polls price data, fires BUY/SELL alerts to console and Slack. Fetches fresh data for all watched tickers at the start of each poll cycle — no separate data collector process needed.

- **Module split (2026-07-13/14)**: the file was 2739 lines with no internal boundaries; split into `signals_config.py` (paths, Slack tokens, `bolt_app` singleton, `SIM_MODE`/`INTERACTIVE`), `signals_db.py` (all DB CRUD), `signals_compute.py` (`_load_cache`, `compute_buy_signal` + indicator cache, `check_sell_condition`), and `signals_notify.py` (charts, Slack blocks, `notify_*`, reminder loops, Bolt handlers, reference report). `active_signals.py` itself is now just `run_loop` + CLI dispatch, re-exporting every public/underscore name from the four submodules so existing `from active_signals import X` / `import active_signals as a; a.X` call sites (12 scripts/pages, all test files) keep working unchanged. Gotcha worth knowing if this file gets touched again: `DB_PATH`/`SLACK_CHANNEL_ID` are mutable module globals owned by `signals_config.py` — every submodule reads them via `cfg.DB_PATH` attribute access, never `from signals_config import DB_PATH`, since the latter freezes a stale copy at import time and breaks both test monkeypatching and `_resolve_channel_id()`'s runtime mutation. Verified via the full test suite (40/40), `py_compile` on every dependent file, live CLI smoke tests (`list`/`positions`), `scripts/watchlist_status.py`, and both `verify_trailing_buy_resolution.py`/`verify_trailing_sell_resolution.py` regression checks — all clean. Not yet cut over to the live daemon (queued behind the restart already pending for other reasons, see `docs/backlog_cache.md`).

- **DB split (2026-07-07)**: `watchlists`/`watch_list`/`open_positions`/`trade_log` now live in `cache/trading_live.db` (small, hot tables the daemon reads/writes every poll), separate from `cache/trading_universe.db` (`backtest_cache`/`hurst_cache`/`tickers`/`kv_cache` — the large research-side tables, including the sweep engine's own cache-of-`backtest_cache`-queries). Reason: heavy research maintenance (REINDEX/VACUUM/sweeps) on the 146M+-row `backtest_cache` was locking out live daemon reads. `active_signals.py`'s `DB_PATH` points at `trading_live.db`; `RESEARCH_DB_PATH` covers the one `hurst_cache` lookup it still makes. Any code that joins live + research data (e.g. `pages/0_Top_Pivot.py`'s Watchlist pivot) uses `ATTACH DATABASE` across the two files.

- **Multi-watchlist**: `watchlists` DB table (id, name, is_active). One list is designated active — that's what the signal loop monitors (`active_signals.py` re-queries `get_watchlist()`, the active one, fresh every loop iteration — a `set_active_watchlist` swap takes effect on the daemon's next poll, no restart needed). Same node can exist in multiple lists (UNIQUE constraint is scoped per list). **Watchlist versioning, 2026-07-18**: same pattern used for the earlier watchlist 7→9 supersession — when a watchlist's scope needs to change materially, clone the nodes you want to keep into a new watchlist and `set_active_watchlist` rather than mutating/deleting the old one, which stays around inactive as an archive. Active watchlist is now **id=57 ("Live v4")**, holding all 19 tickers from the 2026-07-18 walk-forward screen (12 original watchlist + 7 non-watchlist candidates), all `research` mode. Watchlist 9 ("Sweep v3 - Full", the prior active list) is untouched but inactive, preserving the 10 v3.x-only nodes' full config.
  - **`account` joined the dedup key 2026-07-26**: neither `add_node`'s Python-level check-then-skip nor the table's SQL `UNIQUE` constraint included `account` — two nodes with identical ticker/strategy/params but different accounts (a real, intentional case: the same strategy paper-trading in one account, live in another) were silently deduped as if they were the same node. Found live adding a second real DPST node alongside its existing research node. Fixed via a full table-rebuild migration in `ensure_tables()` (detected by checking whether `account` already appears inside the live `sqlite_master.sql`'s `UNIQUE(...)` clause, so it only runs once per DB). Same-account identical-param nodes are still correctly deduped — only cross-account pairing was the gap.
- **Node mode**: `watch_list.mode` — `live` fires full Slack BUY alerts; `research` logs signal to console only (no Slack, no position tracking) — **except** for tickers in `schwab_safety.AUTOMATION_ENABLED_TICKERS`, which route to the paper-trading simulation (`paper_trading.start_paper_buy`, see below) instead of the silent research print.
- **Paper trading for `AUTOMATION_ENABLED_TICKERS` (built 2026-07-18)**: `dry_run=True` (the state of every real Schwab account today) only posts a Slack "[DRY RUN] would place..." message and produces zero simulated fill/P&L — no way to see what the automation engine would have actually done. `paper_trading.py` fills that gap with a simulation that never calls `schwab_client`/`schwab_safety` at all (independent of `dry_run`), reusing the real signal/exit logic so it stays faithful:
  - `start_paper_buy(node, sig)` — called from `_scan_buy_signals` on a BUY for a research-mode, automation-enabled ticker running a trailing-buy strategy. Inserts a `paper_pending_buys` row (`running_low` seeded at signal price), deduped against an existing paper position/pending row.
  - `update_paper_buys()` — called every poll, unconditionally (mirrors `check_auto_fills`'s "not gated to a window" reasoning — a real trailing buy can fill any time). Tracks `running_low = min(running_low, current_price)`; once `current_price >= running_low * (1 + trail_buy_pct/100)`, sizes `shares = int(starting_notional // current_price)` — fully deployed at the real discovered fill price (the "correct" sizing approach identified in the trailing-buy sizing backlog item, since paper trading learns the true fill price directly rather than guessing ahead of it the way the live worst-case formula must) — and opens a `paper_positions` row.
  - `check_paper_sells(last_seen_bar, paper_sell_alerted, load_cache)` — mirrors the real `open_positions` exit-check block in `run_loop` exactly (same `at_bar_close` detection, shares the `last_seen_bar` dict since a ticker is never simultaneously `live` and `research`), calling `signals_compute.check_sell_condition(..., paper=True)` — the same exit state machine used for real positions, just writing to `paper_positions`/`paper_trade_log`.
  - **`watch_list.paper_alert_verbose` (2026-07-26)**: all 5 `_post_message` calls above (buy signal, market-buy fill, trailing-buy fill, trailing-sell-armed, sell) are gated on this per-node flag, default 0/suppressed — paper trading is mostly used for troubleshooting, not routine review, so its Slack output is noise by default. Set to 1 per ticker when actually weighing a go-live decision on it. `check_paper_sells` looks the node up via `db.get_watch_list_node_by_id(pos['wl_id'])` per position per poll to read the flag. No effect on real/dry_run alerts, which are unconditional.
  - `signals_db.py` gained `paper_positions`/`paper_trade_log` (schema-identical mirrors of `open_positions`/`trade_log`) and a lighter `paper_pending_buys` (no reminder machinery — a paper fill is auto-detected every poll, never confirmed by a human click, unlike real `pending_buys`). Existing CRUD (`get_open_positions`, `open_position`, `close_position`, `log_trade_entry`, `log_trade_exit`, `update_position_trail_state`) took a `paper=False` param rather than being duplicated — same columns either way, just a different table name (`_pos_tables(paper)` helper).
  - **Deliberately not** routed through the real `_attempt_automated_buy`/`_attempt_automated_sell`/`notify_buy_signal` path (the backlog's original framing) — that would write real `pending_buys` rows that nothing ever marks Filled (no human clicks the button for a research ticker, auto-fill-detection is opt-in/off), which would sit forever and make `check_buy_reminders` nag indefinitely. Full separation keeps real live-trading state untouched by research-mode paper activity.
  - **Known limitation**: fills are sampled at `POLL_SECS` cadence, not tick-perfect against a real broker's continuously-live `TRAILING_STOP` price — close enough to score signal-catching reliability and get directionally-real fill data, not a tick-perfect replay.
  - `scripts/paper_trading_status.py` — prints current pending/open/closed paper state, matching the `scripts/open_positions_status.py` convention.
- **`watch_list_audit` table (2026-07-18)**: append-only log of every `create_watchlist`/`delete_watchlist`/`set_active_watchlist`/`add_node`/`remove_node`/`set_node_mode`/`label_node` call (`signals_db._log_audit`, `get_watchlist_audit(limit=200)` to read). Built after `watchlists.id` (an `AUTOINCREMENT` column) jumped to 57 with no way to reconstruct why — 47 prior watchlists had been created-then-deleted via the Streamlit UI's Create/Delete buttons over the project's history, legitimate usage but genuinely unexplainable after the fact since nothing was logging it. **A second, real instance of this same symptom (id jumped 57→65 for one genuine new watchlist) was root-caused and fixed 2026-07-20**: `create_watchlist` used `INSERT OR IGNORE` keyed on the UNIQUE `name` column — reproduced directly that SQLite silently burns an `AUTOINCREMENT` id on a name conflict even though no row is written and no error is raised, so any duplicate-name call (UI retry, re-run script) wastes an id with zero audit trace. Fixed to check for an existing name first and only `INSERT` when genuinely new; duplicate calls now return the existing id with zero ids consumed, verified against a fresh test DB.
- **`watch_list.annotation` column (2026-07-18)**: freeform human-readable "why" a node is in its current state (e.g. "walk-forward clean, promoted 2026-07-18"), distinct from `label` (short display tag) and `watch_list_audit` (mechanical what-changed log). Setter: `signals_db.annotate_node(watch_id, text)`, also writes to the audit log.
- **Fixed 2026-07-18, `add_node`'s `fixed_sl` computation**: for any `strategies.uses_fixed_sl` strategy, `add_node` used to always compute `fixed_sl` from `config.json`'s global `execution.fixed_stop_loss` (currently 15%), ignoring whatever real per-node SL value the caller actually wanted — no override parameter existed. Silently produced `fixed_sl=15.0` on 19 v4 (SL=1%) nodes inserted 2026-07-18; worked around that session by inserting via direct SQL instead of `add_node`. Now takes a `fixed_sl_override=None` param — when set, used instead of `_config_fixed_stop_loss()`; `None` (the default) preserves the old behavior for legacy callers.
- **`AUTOMATION_ENABLED_TICKERS` moved to `.env` (2026-07-19)**: was a hardcoded Python set in `schwab_safety.py` (git-tracked, self-documenting via commits); moved to `SCHWAB_AUTOMATION_TICKERS` in `.env` (gitignored) — same rationale as `SCHWAB_ACCOUNT_<NAME>`/`NICKNAMES` already living there: it's deployment-specific config, not something that belongs in shared code, and if this codebase is ever handed to someone else they should pick their own tickers rather than inherit this deployment's. Since `.env` changes no longer show up in `git log`, `schwab_safety.sync_automation_scope()` (called once at `run_loop` startup, not at import time — a bare `import schwab_safety` from tests/scripts must never write to the live DB) compares the current env-derived set against `cache/live/schwab_automation_scope.json` and logs any diff to `watch_list_audit` (`action='automation_scope_change'`) — the only remaining record of when/why the scope changed.
- **Automation scope widened to all 18 v4 tickers (2026-07-19)**: `SCHWAB_AUTOMATION_TICKERS` expanded from GDXD-only to every v4 research-mode ticker on watchlist 57 (EDC deliberately excluded, see below), each set to `starting_notional=5000` (previously the flat $50k default) — all still `research` mode, so this only widens who gets `paper_trading.py`'s simulation, not who gets a real/dry-run order attempt.
- **Two real gaps found while widening scope, not yet fixed**:
  1. **`paper_trading.start_paper_buy`'s dedup is ticker-only** (`db.get_paper_pending_buy(ticker)` / `db.get_open_position(ticker, paper=True)`), unlike the real `open_position()`'s `(ticker, window)` dedup (`signals_db.py:737-738`, itself not `node.id`-based either — two nodes sharing a `window` value would still collide there too). Fine while every automation-enabled ticker has exactly one node; would need to become `(ticker, window)`-aware before a ticker could ever run two nodes (e.g. a v3.x and a v4 node) through paper trading simultaneously.
  2. **SELL-side automation is ticker-gated only, not mode-gated.** `notify_trailing_activated` (`signals_notify.py:309-321`, called unconditionally from the real `open_positions` exit-check loop) calls `_attempt_automated_sell(pos, current_price)` with no check on the position's node `mode` — only `_attempt_automated_sell`'s own `ticker not in AUTOMATION_ENABLED_TICKERS` gate. So a `research`-mode ticker with a real open position (as EDC had) would still have its real exit routed through the automated-sell attempt if added to the automation scope — asymmetric with the BUY side, which `_scan_buy_signals` only reaches via `_attempt_automated_buy` when `mode=='live'`. This is the deeper reason EDC's node was removed rather than added to the widened scope (see below), not just avoiding paper-trading noise.
- **EDC's v3.27 node removed from `watch_list` (2026-07-19)**, not just left out of the automation scope — user is tracking the real open position's unwind manually via spreadsheet going forward. Confirmed removing the `watch_list` row doesn't affect the real position at all: `check_sell_condition`/`notify_sell_signal` key off `open_positions` directly, and every exit parameter (`stop_loss=15`, `arm_sell_pct=20`, `trail_sell_pct=7`, `max_hold_hours=112`) was already copied onto the `open_positions` row at entry time (2026-07-16) by `open_position()` — SELL alerts keep firing at those levels regardless of `watch_list` state. EDC just stops being polled for new BUY signals. Watchlist 57 is now 18 nodes, all v4.
- `watch_list` DB table — nodes selected for monitoring, scoped to a watchlist
- `open_positions` DB table — tracks entries pending exit; `trail_state` TEXT column stores per-position trailing-stop state (peak price, activated flag) as JSON. `trail_pct`/`fixed_sl` columns (also on `watch_list`) hold the real trailing % and fixed stop-loss % for v1.8/v1.9/v1.10 nodes — the swept `stop_loss` column on those strategies actually holds trail_pct/trail_buy_pct, not the real SL, so `check_sell_condition` reads the real values from these columns instead. `signal_time` (not `entry_time`, which is real-time fill time) is the bar the TIME-exit hold count is measured from, matching backtest kernel semantics (counts hourly bars in cached data, not wall-clock hours). `shares` column (added 2026-07-08, both `open_positions` and `trade_log`) — nullable, populated via `open_position(..., shares=...)`; needed for real notional/P&L tracking since position sizing isn't always a flat $50k once compounding is in play. Existing rows aren't backfilled unless done manually.
- `pending_buys` DB table (added 2026-07-09, three-state flow added 2026-07-10) — mirrors `trail_state` for the entry side: a trailing-buy order has no `open_positions` row yet to hang state off of, so this table tracks ticker/node/signal price+time/reminder bookkeeping. Three-state lifecycle, since a placed trailing-buy order still can't be detected as filled live (unlike the sell side's `order_placed`, which needs no further confirmation once placed): **(1) signal fires** → row created, `order_placed=0`; **(2) "Trailing Buy Order Placed"** confirmed → `order_placed=1`, still no `open_positions` row (no fill yet); **(3) "Filled"** confirmed (real price, via a modal) → `open_position()` actually runs, row cleared. `check_buy_reminders()` nags every `BUY_REMINDER_MINUTES` (15) throughout *both* pre-placed and placed-but-unfilled phases (only resolution — Filled or Skipped — stops it, unlike the sell-side trailing-order reminder which stops nagging once `order_placed=True`), using the same supersede pattern as `check_trailing_reminders`. `_trailing_buy_status()` approximates whether the bounce-off-low trigger has actually been met yet (mirrors the backtest's `_simulate_trail_both` running-low logic against cached hourly bars) to pick reminder wording/urgency — not a live implementation of the real state machine (still tracked as a gap below), just informs the nag.
- `notify_buy_signal`/`_build_buy_blocks` branch on `_is_trailing_buy(node)` — trailing-buy nodes get "Trailing Buy Order Placed"/"Skipped" buttons (no price asked, since fill price isn't known at alert time); non-trailing (market/limit) nodes keep the original "Executed"-with-price-modal flow, since those fill immediately and a price is knowable right away.
- **Buy-check loop guards against already-open positions** (fixed 2026-07-08) — `run_loop` builds `open_position_keys` from `get_open_positions()` each iteration and skips `notify_buy_signal` for any ticker+window already held, printing `[skip]` instead. This existed as a gap since the loop was first written (2026-06-30) and was never exercised until a 2026-07-08 selloff pushed already-held tickers back below trigger, firing spurious re-BUY alerts for KORU/HIBL/SOXL.
- **Heartbeat**: `run_loop` writes current time to `cache/active_signals_heartbeat.txt` every iteration. `scripts/check_heartbeat.py` posts a Slack alert (independent of the daemon's own `bolt_app`/socket) if that file goes stale — meant to catch the daemon going silent (e.g. host sleep/suspend) without relying on the daemon itself to notice its own death. **Dropped 2026-07-13, not built**: explored wiring a Windows Task Scheduler job to invoke it on a 15-min repeat, but for the failure modes it would catch (sleep/network/power), the user has no way to act on the alert remotely — root cause (sleep during market hours) fixed directly via a Windows power-plan change instead. `check_heartbeat.py` itself still works standalone if ever revisited (a 2026-07-13 fix made it also alert on its own unhandled crash, not just the two expected stale/missing paths) — see `docs/backlog_cache.md`.
- **Live/backtest parity gap, `TrailingBothZScoreBreakout` — resolved 2026-07-13, no broker fills needed**: `scripts/verify_live_parity.py` still deliberately excludes this strategy from comparison (see its own docstring) — live has no implementation of the trailing-buy "wait for bounce" entry state machine; it just detects "z-score crossed trigger" and hands off bounce-timing to a broker-side trailing-buy order, so there's no live code to replay against the kernel. Instead of waiting on real broker fill data, built `scripts/verify_trailing_buy_resolution.py`/`verify_trailing_sell_resolution.py`: re-detect the real bounce-entry/trailing-exit using yfinance 5-min bars (a proxy for continuous broker-side tracking) and diff against the hourly-bar kernel's prediction for the same signal, across the whole watchlist. Result: entries 130/130 matched (mean +0.19% price diff, SOXL the real outlier at +1.81% — `trail_buy_pct=1%` far tighter than its own ~3.65% median intra-hour swing); exits 21/21 matched (mean -0.17%, already at parity). Both accept `--tickers AGQ,SOXL` for a cheap subset check, wired into `docs/pre_commit_checklist.md` as a regression control whenever `active_signals.py`/`strategies.py`/`backtester.py` changes. **Not fully closed**: this validates the *price* assumption, not the broker's own trailing-buy order mechanics (no real-fill-time-vs-signal-time verification yet).
- **`compute_buy_signal` indicator caching (2026-07-13)**: previously recomputed the full rolling SMA/Std history from scratch via `generate_daily_indicators()` on every 5-min poll, per node — real, repeated wasted work (the backtest kernel already computes `sma_arr`/`std_arr` once via `prep_inputs`). Fixed with a module-level `_indicator_cache` keyed by `(ticker, strategy, window)` → `(cache_key, indicators_df)`, where `cache_key` is `(len(df_daily_prior), last_date)` — invalidates automatically the moment the underlying daily data actually advances (new day's close appended), can't serve stale indicators. Verified: second call reuses the identical cached DataFrame object, signal/sma/std match exactly.
- Entry/exit logic delegated to strategy classes in `strategies.py` — no signal logic in `active_signals.py`
- **Slack Socket Mode** — bot token + app token; BUY/SELL messages have interactive Executed/Skipped buttons, price entry modal, chart image upload
- **Reminder functions decoupled from `INTERACTIVE`** (fixed 2026-07-10) — `check_buy_reminders`/`check_trailing_reminders` previously hard-gated on `if not INTERACTIVE: return` and called `bolt_app.client.chat_postMessage` directly instead of `_post_message`, so they silently never fired in SIM_MODE *or* in any real non-Socket-Mode (webhook-only) production deployment — a genuine gap, not just a testability issue, since the whole point of these functions is nagging when something's stalled. Now always run and post through `_post_message` (buttons still only render when `INTERACTIVE=True`, gated inside `_pending_buy_blocks`/`_trailing_order_blocks` themselves).
- **Exit-pending reminder (4r), finished 2026-07-11** — `check_exit_reminders(open_positions)` (mirrors `check_trailing_reminders`'s supersede-not-edit-in-place pattern, `EXIT_REMINDER_MINUTES=15` flat cadence, no plausibility gating needed — see phase-4 note below) polls `trail_state.exit_pending` and nags via `_exit_pending_blocks` (reuses the original `sell_exited`/`sell_skipped` action_ids rather than inventing new ones) until Exited/Skipped resolves it. Wired into `run_loop` alongside `check_trailing_reminders`/`check_buy_reminders`. Also fixed while touching this: `notify_sell_signal`'s non-interactive console fallback hardcoded `exit_reason='MANUAL'` regardless of the real reason (TP/SL/TIME/TRAIL) — now passes `exit_reason=reason` (the button path already did this correctly; only the typed-price console fallback had the bug).
- **Buy-fill reminder gating + per-phase counters, 2026-07-11** — two fixes to `check_buy_reminders`/`_pending_buy_blocks`/`mark_pending_buy_placed`, both from live user feedback while walkthrough-testing the reminder messages in Slack: (1) `mark_pending_buy_placed()` now resets `reminder_count`/`last_reminder_at` when flipping `order_placed`, so the fill-confirmation phase gets its own reminder numbering (#1, #2, ...) instead of continuing the placement phase's count — sharing one counter across two different questions ("is it placed?" vs "did it fill?") read as a lie about how many times the user had actually been asked about the fill. (2) `check_buy_reminders` now skips (without touching `last_reminder_at`, so it rechecks cheaply every poll rather than waiting out a full stale interval) nagging the fill-confirmation phase while `_trailing_buy_status()` reports `met is False` — flat 15-min nagging regardless of whether a fill is even plausible yet was pure noise, especially for wide-`trail_buy_pct` tickers like KORU (12%). `met=None` (unknown, e.g. stale/missing cache) still nags, erring toward not silently dropping a real stalled fill. **Deliberately not applied to the arm reminder (`check_trailing_reminders`) or the exit reminder (`check_exit_reminders`)** — both only ever fire after `check_sell_condition` has already confirmed a real price-based trigger (arm threshold crossed / sell condition met), so there's no "is this plausible yet" guessing problem the way there is for the buy side's un-implemented broker-side bounce state machine.
- **Same bug found in `_trailing_buy_status()` while building the above**: returned `(False, None)` instead of `(None, None)` when no cached bars existed since the signal fired (e.g. weekend/stale cache) — `_pending_buy_blocks`'s `elif met is False` branch then formatted `None` as `{trigger:.2f}` and crashed. `False` claimed "confirmed not met yet" when the real state was "no data, unknown" — fixed to return `(None, None)`, which `_pending_buy_blocks` already handled gracefully via its existing unknown-status branch.
- **BUY message** — shows market price, share count at $50k notional, and max notional / max shares at 1% of avg daily vol (liquidity ceiling from `tickers` table)
- **Reference report** (`send_reference_report`, renamed from `send_startup_report` 2026-07-09) — fires at startup/restart and at fixed daily times (7:00 AM, 9:20 AM, 3:20 PM ET as of 2026-07-10, was 9:20/15:20 only), reading off `build_reference_table` (the single computation shared with `_send_window_alert` and `scripts/reference_table.py`). Renders one mrkdwn prose block per ticker (mobile-readable; the old wide code-block table is now CLI-only) split into Open Positions / Buy Candidates sections; dark-theme chart attached only for buy candidates within 5% of trigger. `_send_window_alert` (fires inside the 10:25/15:25 signal windows) reuses the same row data but only shows tickers within 5% of their trigger, not the full watchlist. `_ticker_block`'s SL display shows `cancelled (trail order live)` instead of a stale price once a held position's trailing-sell order is confirmed placed (`trail_state.order_placed=True`) — the broker only allows one resting sell-all order, so the fixed catastrophic stop is genuinely replaced once the trailing order goes in, matching the backtest kernel exactly (`_simulate_trail_both` never rechecks the fixed `stop_price` once `trailing=True`). Also shows `Z Trigger`/`Last Sale $` (compounds next-buy notional off the prior trade's proceeds, `_last_sale_recovery`) alongside the existing trigger/proximity/arm/trail% fields. **"Reconfirm limit order" reminder block removed 2026-07-12**: it prompted pre-staging a limit order for any buy candidate within 5% of trigger, but that's stale wording left over from the pre-`TrailingBothZScoreBreakout` era — none of the 11 live watchlist tickers use a staged-then-edited limit order anymore, and pre-staging didn't actually save time anyway (share count still needs recalculating off the live price at signal time, and buying power caps how many shares can safely be staged in advance). Live experiment now: place the trailing-buy order cold from the BUY alert itself, no pre-staging step. **Manual open/close buttons + on-demand resend, added 2026-07-12** — guards against a misclick (e.g. tapping "Skipped" after a real fill/exit actually happened at the broker) leaving the DB out of sync with reality. Every `_ticker_block` row now carries an `INTERACTIVE`-gated action button: flat tickers get "Manually Open `{ticker}`" (opens a modal asking Price + Shares, prefilled with a suggested share count from `_last_sale_recovery`/current price but fully editable — needed because a real fill's actual share count can differ from what auto-sizing would compute), held tickers get "Manually Close `{ticker}`" (modal asks Price only, calls `close_position(..., exit_reason='MANUAL')`). The modal's Confirm/Cancel doubles as the confirmation step the user asked for — no separate "are you sure" needed. `_ticker_block` now returns a **list** of blocks (section + optional actions block) instead of a single block; all three call sites (`_send_window_alert`, `send_reference_report`'s held/flat loops) updated to flatten via `+=` instead of `.append()`/list-comprehension. Also added a "🔄 Resend Report" button at the top of `send_reference_report`'s blocks (`resend_ref_table` action) that posts a brand new report on demand rather than editing the clicked one in place, so old reports (and their now-stale manual buttons) remain as a historical record. Verified end-to-end 2026-07-12 by running a standalone Socket Mode listener (`bolt_app` + `SocketModeHandler`, no polling loop) against the live DB while the real daemon was confirmed stopped — real Manually-Close-AGQ and Manually-Open-KORU clicks both worked correctly (DB backed up first; test rows manually reverted afterward). One caveat found during testing: `_last_sale_recovery`'s "next buy" notional reads off the most recent *closed* trade regardless of how recently/how it was closed — a test-only manual close briefly poisoned AGQ's displayed next-buy notional to ~$74k until the test row was cleaned up. Not a bug, just something to keep in mind if a manual close is ever left in place longer than intended.
- **Phase emoji (`_phase_emoji`, redesigned 2026-07-11 from the single-ball 2026-07-10 prototype)** — a 4-bubble lifecycle strip, one per ticker, leading `_ticker_block`'s line and the CLI table's `Phase` column: **① Signal** (grey=idle → yellow=buy signal fired, no order yet → green=order placed), **② Filled** (grey → yellow=order placed, awaiting fill → green=filled/holding), **③ Armed** (grey=holding, not armed → yellow=armed, trailing-sell order not yet resting → green=trailing-sell order confirmed placed), **④ Sold** (grey → yellow=SELL signal fired, exit not yet confirmed; green is theoretical only — the row disappears from the table once actually closed). Replaced the original single-ball design after the user found it unreadable in practice ("I didn't understand it") — the key insight driving the redesign was that filled and armed are genuinely distinct states (a position can be held without being armed yet), which a single ball couldn't represent. The original single-ball's standalone `_proximity_emoji` companion ball (adjacent, same-colored, made rows read as an undifferentiated 5-ball blur) was dropped from `_ticker_block` entirely per user call — not actionable pre-bar-close anyway, and the phase strip already covers state while proximity % is in the text body. Verified via `scripts/test_phase_emoji.py` (rewritten for the 4-bubble format, all 7 state combinations) plus an interactive step-by-step Slack walkthrough with the user (dummy-`action_id` button previews per ticket layout convention, see below) exercising every transition end-to-end on an isolated sim DB.
- **Broker-stop tracking + phase-specific trigger labels (2026-07-14, AGQ live incident)**: `open_positions.broker_stop_price` column added — a plain fact about the real broker stop order price (set once, independent of any alert state), distinct from `trail_state.exit_pending` (an ephemeral snapshot of the algo's own SL firing, cleared once resolved). `set_broker_stop_price(ticker, price)` in `signals_db.py`. When set, SL alerts (`_build_sell_blocks`/`_exit_pending_blocks`) now say "protected by broker stop @ $X, no action needed" instead of implying urgency the broker order already covers. Also fixed `build_reference_table`'s pre-entry (`pos is None`) branch: it fetched `pending_buys` but never used it to pick the trigger price, always showing the stale initial z-cross trigger even once a trailing-buy order was active and the real number to watch had moved — now checks `pending_buys` and shows the bounce-above-running-low trigger (via `_trailing_buy_status`) when applicable. Root incident: AGQ's algo SL (entry × 0.85 = $63.58) fired correctly 2026-07-13 15:29:42 (daemon was running, not a bug), user skipped the Slack confirmation, and it sat unresolved while the real broker stop was 1% wider ($62.83, the `stop_loss+1%` buffer) — discussion concluded the buffer's original premise (protect against noise before a "real" signal) doesn't hold, since the algo's own SL check is already an unconfirmed intrabar low breach (`strategies.py`'s `ctx['low'] <= stop_price`), mechanically identical to a real stop order. **Convention going forward: broker stops should be set at the algo's exact `fixed_sl` price, no padding** (see `docs/backlog_cache.md`). Also added `Trigger Label` to `build_reference_table` rows / `_ticker_block` — the previously-generic "trig" is now phase-specific (`z-cross`, `tb-bounce`, `arm`, `trail-sell`), and pre-entry `Arm $`/`SL $` dollar previews were dropped from the Slack text entirely (config %s alone are shown; showing speculative dollar projections before any fill was assessed as "theatre" — noise dressed as information). `scripts/watchlist_status.py` got the equivalent `Phase`/trigger fix for the CLI view.
- **Duplicate-position guard now surfaces to Slack, "Missed It" button added, held-row layout trimmed (2026-07-14)**: `open_position()` (`signals_db.py`) now returns `True`/`False` instead of silently returning `None` on the duplicate-ticker path — every caller (`handle_trail_buy_filled`, `handle_entry_price`, `handle_manual_open_price`, the terminal fallback in `notify_signal_and_wait`) checks the return and posts an honest "ALREADY OPEN, ignored" warning (via new `_existing_position_note()` helper, backed by new `db.get_open_position(ticker)`) instead of a false "Filled"/"Executed" success message. Root incident: KORU was manually filled via "Manual Open" (price+shares modal), but `handle_manual_open_price` never called `db.clear_pending_buy()`, so the stale `pending_buys` row kept nagging; the user then tapped "Filled" on that stale reminder, which computed a second auto-sized share count and *reported* success even though `open_position()`'s existing duplicate guard silently no-opped the write — leaving the user unsure what was actually live. Both root causes fixed: `handle_manual_open_price` now clears the pending-buy row, and the silent no-op now reports honestly. Also added a third button, **"Missed It"**, alongside Filled/Cancelled in the fill-confirmation phase (`_pending_buy_blocks`, `handle_trail_buy_order_placed`) — distinct from Cancelled (which implies the broker order itself was pulled): Missed It is for when `_trailing_buy_status()`'s bounce check reports `met=True` because the trigger was hit on a bar before the real broker order was actually resting (order placed a few minutes late), so the order may still be live at the broker but isn't worth continuing to nag about. Separately, `_ticker_block`'s held-position row was trimmed per user feedback: entry price (`$X` or `$X x N shares`) now shown next to the account tag; `next buy ~$Xk` (only relevant pre-entry) dropped from held rows; the non-held row's `z-trig` label renamed to `z1` and moved to the front of its line to match the held row's leading-`z` convention; and the held row's `arm`/`ts` config-% line is dropped entirely once a position is already armed (`trail_state.trailing=True`) — both are already baked into the trigger price shown above at that point, so the config %s are dead info once live.
- **Sim-DB button-preview convention reaffirmed 2026-07-11**: user asked about making SIM buttons real/interactive with a SIM-aware branch in the live daemon's handlers; explicitly decided against it — that would add a production/test routing branch into the code path that manages real trades (risk: a bug there could write test data into live tables or vice versa). Dummy `action_id`s (e.g. `dummy_preview_0`) stay the standard for visually previewing button layout in Slack without live-daemon interaction risk.
- **`_post_message` SIM_MODE marker** (fixed 2026-07-10) — previously only rewrote `"header"`-type blocks with the `🧪 SIM` prefix, so any message built from `"section"` blocks (most of them — BUY/SELL alerts, reminders) shipped with no visible SIM tag in the rendered body at all, only in the fallback notification text Slack doesn't show when `blocks` is present. Now prepends/appends dedicated `"context"` marker blocks (`🧪 SIM MODE: <scenario>` / `🧪 SIM MODE END`, scenario from optional `SIM_SCENARIO` env var) regardless of block composition.
- **Current price** — uses `yfinance history(period='1d', interval='1m', prepost=True)` to capture pre/post-market; falls back to cached hourly close on failure
- Signal indicators use prior closed day's SMA/Std (not today's intraday close) — matches live trading semantics
- `--ticker TICKER` flag to filter the poll loop to specific tickers
- Manual execution remains the norm; `schwab_client`/`schwab_safety` provide an opt-in automated path (`AUTOMATION_ENABLED_TICKERS`, every account `dry_run=True` today) rather than full brokerage integration.
- `scripts/live_test.py` — synthetic TEST ticker for end-to-end Socket Mode testing
- **Part 3 — trailing-buy budget adherence (built 2026-07-21)**: closes two backlog items (idle-capital re-sizing, gap-through-trigger fill optimism) with padded sizing + an overnight gap guard + a post-fill top-up. Full design at `docs/research_log.md`'s 2026-07-21 entry (design phase) and this entry (implementation).
  - **Same-day sizing pad**: `buy_order_sizing(node, sig, pad_pct=1.0)` (`signals_helpers.py`) now sizes off `trail_buy_pct + pad_pct` instead of `trail_buy_pct` alone — covers ordinary same-day slippage between signal detection and the order actually going live (real same-day fill rate 40.6-100% across watchlist-65's 4 TB tickers, the majority case, not the exception).
  - **Pre-open gap guard** (`signals_notify.check_gap_resize`, fired once daily from `active_signals.run_loop` at `_GAP_CHECK_WINDOW=(9,15,9,29)`, before `Session.NORMAL` orders execute at 9:30): for each still-pending, order-placed trailing buy, checks whether the real overnight gap has already cleared the bounce trigger (`schwab_client.get_current_price` — Schwab's own `get_quote` extended-session price primary, yfinance `fast_info.last_price` fallback; confirmed Schwab's feed is genuinely real-time where yfinance's pre-market feed can run tens of minutes stale). If cleared, cancels the resting order (`schwab_client.cancel_order`) and replaces it with a plain `MARKET` order (mirrors the backtest kernel's own `entry_price=op` gap-fill behavior — no dedicated `MARKET_ON_OPEN` type exists in `schwab-py`, a `Session.NORMAL` market order queued pre-open achieves the same) sized off the live quote with a flat 5% pad, then polls for its own fill and reconciles immediately rather than waiting for the next `check_auto_fills` cycle. If the trigger hasn't cleared, no action — the resting order's original sizing is still a valid bound (`running_low` is non-increasing).
  - **`schwab_safety.check_order`/`approve_and_record` gained `is_gap_correction=False`** — bypasses only the BUY signal-window time gate (the gap-check window is deliberately outside `_SIGNAL_WINDOWS`/`_OPEN_CHECK_WINDOWS`); every other guard (kill switch, caps, duplicate-order, `_has_open_order`) still applies.
  - **Post-fill top-up**: `signals_notify._reconcile_buy_fill(ticker, fill_price, filled_shares)` is now the single entry point for a detected BUY fill, shared by `check_auto_fills` (slow poll) and the new `drain_fill_queue` (fast path, below) — it clears the `pending_buys` row first (the existing dedup marker, so whichever path notices a fill first "wins" and the other is a no-op), opens the position, then calls `_reconcile_fill(node, fill_price, filled_shares)`, which tops up under-spent notional with a market buy (`signals_db.top_up_position`, blends `entry_price` by share-weighted average) or notifies-only on a rare overspend (no corrective sell — large-gap overspend is already prevented by the gap guard above).
  - **Real-order async-confirmation (2026-07-24, first `dry_run=False` day, `schwab_client.py`)**: every real
    placement/cancel now polls the real status (`_confirm_order_status`, 4 attempts/0.5s) instead of trusting
    the initial HTTP response — confirmed live 2026-07-23 night that placement/cancellation are asynchronous
    (an HTTP 201/200 doesn't mean the order actually resolved that way yet). A confirmed
    REJECTED/CANCELED/EXPIRED raises `schwab_client.OrderRejected` (caught by every real placement call site,
    falls back to manual instead of treating a dead-on-arrival order as placed); `cancel_order` now returns
    `(response, confirmed_status)`, and both callers (`check_gap_resize`, `_attempt_automated_sell`) abort
    rather than proceed (a replacement order / a new trailing-sell) unless the cancel is confirmed
    `CANCELED` — closes a real double-order/oversell risk found via Opus review.
  - **`schwab_stream.py` (new)** wraps `schwab.streaming.StreamClient` for account-activity fill events, run via `run_stream_forever()` as a daemon thread started from `run_loop` (reconnects with capped exponential backoff on any disconnect, Slack-alerts, never gives up). Pushes parsed fill events onto a module-level `queue.Queue`; `signals_notify.drain_fill_queue()` (called every `run_loop` iteration, cheap/non-blocking) drains it and calls `_reconcile_buy_fill` from the main thread (avoids cross-thread sqlite access). Deliberately just a latency improvement — `check_auto_fills`'s existing poll keeps running unconditionally as the always-on fallback if the stream degrades or never comes up. Field parsing is unverified against a real fill event (same caveat as `schwab_client.get_filled_order` before its first real fill) — confirm before trusting beyond "worst case, the slow poll catches it a few minutes later."
  - **`pending_buys.order_id`** (new nullable column) holds the real broker order id (`schwab.utils.Utils.extract_order_id`, captured by `_place_equity_order`/`_place_trailing_order`, both now returning `(response, order_id)`) — needed by `check_gap_resize` to cancel a still-resting order. `_PENDING_BUY_NODE_KEYS` also gained `starting_notional` (previously only on the paper-trading variant) — the post-fill top-up needs the real target notional, which the pre-fix subset didn't carry.
  - Verified: full `pytest tests/` (107 passed, was 98 — 9 new tests across `tests/test_part3_gap_resize.py`/`tests/test_schwab_stream.py`), `verify_trailing_buy_resolution.py`/`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` (clean, consistent with prior drift), `pending_buys.order_id` migration confirmed additive-only against a `trading_live.db` backup (row count unchanged, new column present).
  - **SOXL's watchlist-65 node decision, 2026-07-21**: stays `TrailingBothZScoreBreakout` (not swapped to `TrailingExitZScoreBreakout`) — the operational-ease motivation for that swap is exactly what this Part 3 automation resolves directly, so there's no reason to give up TB's extra alpha (1212.1% vs TE's 947.0% best alpha).
  - **Not yet done**: no real order has been tested end-to-end (every account is still `dry_run=True`); a real Schwab account-activity fill event hasn't confirmed `schwab_stream.py`'s payload parsing.
- **Part 4 — Entry Trigger/Fill/SL-Placement/Arm-latency automation for `TrailingExitZScoreBreakout` (built 2026-07-21)**: automates the 6 watchlist-65 `TrailingExitZScoreBreakout` tickers' (AGQ, DPST, KORU, NUGT, UDOW, YANG) previously fully-manual BUY flow. Full design at `/home/pkim/.claude/plans/replicated-gliding-quasar.md` (not git-tracked).
  - **Pinned single-shot scheduling** (`active_signals.py`): `_PINNED_BAR_TIMES` (one per hourly bar boundary, `:30:02`, +2s buffer) replaces ambient `POLL_SECS`-cadence detection for entry (`_scan_pinned_entry`, the 4 real signal-reaction moments) and exit-arm latency (`_scan_pinned_exit_arm`, all 7, open positions on automation-enabled tickers only). `_sleep_until_next_cycle` wakes the main loop early right before the next pinned target instead of free-running past it. `_scan_buy_signals` gained a `price_overrides` param so ambient and pinned checks share one alert code path.
  - **`schwab_client.get_session_open_price()`** (new): reads Schwab's real `quote.openPrice` field (confirmed present live) — the session's fixed opening print, matching the backtest kernel's literal bar Open exactly (eliminates the ambient-poll detection drift, measured 1.78% avg at the 9:30 open in the prior session's research). Retries 3x/2s, falls back to `get_current_price()` (`is_true_open=False`).
  - **`buy_order_sizing` gained `market_pad_pct`** (`signals_helpers.py`, default `DEFAULT_MARKET_ENTRY_PAD_PCT=1.0`, provisional placeholder — not yet derived from real fill-time-only slippage data, follow-up flagged) for the non-trailing (plain market-buy) sizing branch, distinct from the trailing-buy branch's `pad_pct`.
  - **`_attempt_automated_market_buy`/`notify_buy_signal`'s new branch** (`signals_notify.py`): `market_buy_eligible = (not trailing_buy) and (ticker in AUTOMATION_ENABLED_TICKERS)` places a real (or dry_run) plain market order via `schwab_client.place_equity_buy`. `db.add_pending_buy` fires whenever `trailing_buy or market_buy_eligible`, not gated on `auto_placed` — a blocked/failed automated placement still falls back to the existing manual reminder flow instead of silently dropping the signal.
  - **SL Placement (real gap, not previously automated for any ticker)**: `schwab_client.place_stop_loss()` (new, `OrderType.STOP` + `set_stop_price`, same `OrderBuilder` pattern as the trailing orders) places a genuine resting STOP. `signals_notify._sync_confirm_and_protect` runs a synchronous fast-confirm poll (5×2s) immediately after an automated market buy, and on a hit calls `_reconcile_buy_fill` — opens/tops-up/protects the position in one place (idempotent/dedup-safe, same clear-`pending_buys`-first marker the poll/websocket paths already use), so there's no separate provisional-then-resize step. On timeout, fires an urgent Slack alert (`🚨 ... position may be temporarily UNPROTECTED`) instead of silently deferring — ~70-80% of this strategy's trades exit via SL, the primary defense mechanism, not a rare backstop. New `open_positions.sl_order_id` column (nullable); `_attempt_automated_sell` now cancels it (`schwab_client.cancel_order`) before placing the trailing-sell order on arm, avoiding two live sell orders for the same shares.
  - **Three real bugs found and fixed during a phase-by-phase scenario review, 2026-07-21 (continuing session)**: (1) `_place_stop_loss_for_position` computed `stop_price` off the real **fill** price, not the **trigger/signal** price — the backtest kernel's `stop_price = entry_price * (1 - sl%)` uses `entry_price` = the trigger itself (op/cp), zero fill slippage modeled, so anchoring to fill price let market-order slippage silently loosen the live stop relative to the backtest's for that trade (a fill better than the trigger produces a looser stop, meaning a gap the backtest would have exited through could leave the live position open through a larger drawdown than modeled). Fixed to anchor `stop_price` off `signal_price` (now threaded through `_reconcile_buy_fill`/`_place_stop_loss_for_position`), matching the backtest's formula exactly regardless of fill slippage. (2) `_reconcile_buy_fill`'s `place_sl` was only ever passed `True` by the synchronous fast-confirm path (`_sync_confirm_and_protect`) — if that path timed out, the async fallback paths (`check_auto_fills`, `drain_fill_queue`, `check_gap_resize`'s fill poll) all called `_reconcile_buy_fill` with the default `place_sl=False`, so a position could sit permanently unprotected despite the timeout alert's own claim that the fallback would eventually cover it. Fixed by having `_reconcile_buy_fill` determine `place_sl` itself (market-buy + automation-scope ticker), so the SL genuinely gets placed regardless of which path detects the fill. (3) `_reconcile_fill`'s post-fill top-up (Part 3) never actually placed a broker order at all — it only wrote `open_positions.shares`/`entry_price` via `db.top_up_position`, despite the docstring claiming "tops up the position with a market buy." The account never held the extra shares while every downstream sell order (SL, trailing-sell) sized off the inflated share count — a real oversell/short-sell risk. Fixed to call `schwab_client.place_equity_buy` for the top-up quantity first (dry_run/`SafetyViolation`-aware, DB only updated on success); `is_gap_correction` threaded through so a top-up following a gap-correction fill (which fires outside `_SIGNAL_WINDOWS`/`_OPEN_CHECK_WINDOWS`) isn't wrongly blocked by the signal-window gate.
  - **`schwab_safety`'s duplicate-order guard tightened to match on quantity too, 2026-07-21**: fixing (3) above exposed that the guard (`account+ticker+side` within `DUPLICATE_ORDER_WINDOW_SECS=60`) was never actually exercised by the top-up before (no broker call existed) — once it was, the top-up (firing seconds after the primary buy, same ticker/account/side) was getting blocked as a false "duplicate." Rather than add another bypass flag (the `is_gap_correction` pattern, now used twice), tightened the guard's fingerprint to also require the quantity to match within `DUPLICATE_ORDER_QUANTITY_TOLERANCE_PCT=5.0` — a real resubmission bug/retry has (near-)identical quantity, while a legitimately distinct order (top-up, much smaller) doesn't, so no bypass is needed at all. `recent_orders` entries now record `quantity`. **Known remaining gap, not fixed**: `approve_and_record` records the order into `recent_orders` *before* the real `place_order` call happens (`schwab_client._place_equity_order`) — if that broker call then fails/times out/is rejected, the guard still thinks it succeeded, so a legitimate retry within the window is wrongly blocked. The more correct fix (raised in conversation, not built) is to query Schwab's real order book (`get_orders_for_account`, already used by `get_filled_order`) for a genuine WORKING/FILLED match instead of trusting a local pre-flight-recorded heuristic — bigger structural change to the safety layer, scoped as its own follow-up rather than bolted on this session.
  - Verified (all three fixes + guard change): full `pytest tests/` (137 passed, was 131 — new tests in `tests/test_part4_entry_trigger.py`, `tests/test_part3_gap_resize.py`, `tests/test_schwab_safety.py`), `verify_trailing_buy_resolution.py`/`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` (clean, consistent with prior documented drift).
  - **`schwab_safety._OPEN_CHECK_WINDOWS` widened from `(9,31,...)`/`(14,31,...)` to `(9,30,...)`/`(14,30,...)`** — found while implementing: the BUY signal-window time gate started one minute *after* the new pinned entry checks fire (`:30:02`), which would have wrongly blocked every pinned-check automated order.
  - **Real bug found and fixed via Deliverable 1** (`signals_compute.compute_buy_signal`): `prev_close` was computed from the unsliced `df_daily['Close'].iloc[-1]` instead of `df_daily_prior` (already correctly sliced to `< as_of`/today, the same frame the indicators use) — harmless in live use only because `df_daily`'s last row usually *is* the same as `df_daily_prior`'s, but a real latent bug (today's partial resampled bar could leak in as "prev_close", silently wrong for the corporate-action guard and `Overnight %`) and a hard failure for any historical `as_of` replay (`scripts/verify_live_parity.py`, `scripts/live_sim.py`, and the new Deliverable 1 script below all use `as_of` — every one of them was silently getting a wrong/future `prev_close`). Fixed by switching to `df_daily_prior['Close']`.
  - **Deliverable 1 — `scripts/verify_pinned_entry_vs_backtest.py`** (new, offline): for each of the 6 tickers' real node, runs the real backtest kernel to get every trade's Entry Time/Entry Price, determines Open-vs-Close branch by comparing against the bar's real Open/Close, replays `compute_buy_signal(node, as_of=..., price_override=...)` and asserts it reproduces the same BUY decision. First real run (post-`prev_close` fix): 5/6 tickers 100% clean (DPST 126/126, KORU 77/77, NUGT 34/34, UDOW 72/72, YANG 60/60); AGQ 128/129, the one mismatch a real ~2:1 stock-split day correctly caught by the corporate-action discontinuity guard (expected divergence — the backtest kernel has no such guard, live correctly does) not a defect.
  - **Deliverable 2 — `open_price_quality_log` table + `scripts/verify_open_price_quality.py`** (new): `_scan_pinned_entry` logs every `get_session_open_price` fetch (ticker, target time, price, `is_true_open`); the follow-up script joins it against the real cached Open/Close the next day. Needs real trading days to populate — can't be backfilled, not yet run.
  - **`paper_trading.start_paper_market_buy`** (new): fixes a real gap found live — `start_paper_buy` previously no-op'd for any non-trailing-buy node, so `TrailingExitZScoreBreakout` produced zero paper-trading activity. Sizes via the same `buy_order_sizing` real code path, opens the paper position directly (no bounce-fill phase to simulate, unlike a trailing buy).
  - **`signals_handlers.py:193` drive-by fix**: replaced hardcoded `shares = int(50_000 // exec_price)` with `_last_sale_recovery`-based sizing, matching the pattern already used elsewhere in the same file.
  - Verified: full `pytest tests/` (131 passed, was 107 — 24 new in `tests/test_part4_entry_trigger.py`). Deliverable 1 run above. Not yet done: no real order tested end-to-end (`dry_run=True` everywhere); Deliverable 2 needs real trading-day data; `dry_run=False` flip pending both.
- **Cash-balance check + daemon fault-tolerance built, 2026-07-21**: new `docs/automation_principles.md` (engineering-principles doc for this whole module, reviewed whenever `active_signals.py`/`signals_*.py` are touched, per `CLAUDE.md`) and `docs/live_test_coverage.md` (standing ledger of which live scenarios are still unverified against the real daemon) written first, then two backlog items resolved against them:
  - **`schwab_client.get_account_balance(account)`** (new) + a BUY-only check in `schwab_safety.check_order`: requires `cash_available >= notional + CASH_SAFETY_BUFFER` (`CASH_SAFETY_BUFFER=200`, a small flat per-order cushion for fees/price-tick overage — deliberately not a restatement of the user's own much larger ~$1,000 cash-reserve habit per account, per `automation_principles.md` #7a), fails closed on any balance-fetch error. Separately (non-blocking): posts a Slack warning if `cash_available < CASH_RESERVE_WATERMARK=1_000` on any BUY check, so the user knows to top up cash before the reserve runs out — this doesn't gate the order. Confirmed by an independent Opus review to sit at the correct chokepoint (every BUY path, including the top-up and gap-correction replacement, routes through it) and to be genuinely fail-closed. The review's one real finding — Schwab doesn't reserve buying power for a resting order, so two different live tickers sharing one account could each pass the cash check against the same undecremented balance — was fixed the same session: `schwab_safety._has_open_buy_order_in_account` blocks a second concurrent BUY into an account with any other ticker's resting BUY, reusing the same `_open_orders()` fetch the pre-existing same-ticker guard already made (refactored to share one API call instead of two, `_has_open_order` now takes the pre-fetched list). Two smaller findings logged to backlog, not fixed: the balance fetch runs while `approve_and_record`'s cross-account file lock is held (latency/liveness concern, not correctness), and `cashAvailableForTrading`'s exact field semantics remain unverified against a real account response.
  - **`active_signals._guarded(section, fn, *args, **kwargs)`** (new): wraps every previously-unguarded `run_loop` section (reference report, gap-resize, window alerts, pinned scans, per-position exit-check — now itself per-position isolated via `_check_position_exit` — paper-sell checks, reminders, auto-fill checks, fill-queue drain, paper-buy updates, per-node limit-fill loop — similarly isolated via `_check_limit_fill` — both buy-signal scans), catching and logging any exception and posting a 15-min-cooldown-rate-limited Slack alert rather than swallowing it silently. The whole loop body is additionally wrapped in one outer try/except as a last-resort net for anything that slips through an individual guard. `tests/test_run_loop_fault_tolerance.py` (6 new tests) covers `_guarded` directly; no test yet runs a full `run_loop` iteration with an injected failure end-to-end.
  - Verified: full `pytest tests/` (151 passed, was 137). Neither change has been observed live yet (see `docs/live_test_coverage.md`); `dry_run=True` everywhere, unchanged.
- **Opus-review follow-ups + live-state reconciliation built, 2026-07-21/22**: closed out the two smaller findings left open from the cash-balance-check review above, plus the SELL-side mode-gating gap and the live-state reconciliation idea, both from `docs/deep_backlog.md`.
  - **`schwab_client._get_client()` now bounds every Schwab HTTP call to a 10s timeout** (`_CLIENT_TIMEOUT_SECS`, via `client.set_timeout()`) instead of schwab-py's 30s default — the balance-fetch/order-book calls run inside `schwab_safety`'s cross-account file lock, so an unbounded stall there previously risked stalling order processing for every account, not just the one being checked. Simpler than restructuring lock ordering (`automation_principles.md` #7a). New `tests/test_schwab_client.py`.
  - **Test-hygiene sweep**: confirmed every test file importing `schwab_safety`/`schwab_client` already mocks `_open_orders`/`get_filled_order` — no stragglers hitting the real Schwab API. No code change needed.
  - **SELL-side automated-order attempt is now mode-gated, not just ticker-gated** (`signals_notify._attempt_automated_sell`): looks up the position's own `(ticker, window)` node from `db.get_watchlist()` and only proceeds if it finds `mode=='live'`, falling back to manual (not KeyError) if no matching node exists at all. Mirrors the BUY side's existing `mode=='live'` gate (`automation_principles.md` #7) — closes the exact gap that forced EDC's node removal instead of a scope addition on 2026-07-19. 2 new tests in `tests/test_schwab_automation.py`.
  - **Live-state reconciliation check built** (`signals_notify.check_live_state_reconciliation`, called every `run_loop` poll cycle via `_guarded`, automation-scope tickers only): new `schwab_client.get_real_position(account, ticker)` (`Client.get_account(fields=[Account.Fields.POSITIONS])`, field names unverified against a real response, same caveat pattern as `get_account_balance`) compares the broker's real share count against `open_positions.shares`, and whether the expected resting protective order (SL pre-arm, trailing-sell post-arm) actually exists at the broker via the existing `schwab_safety._open_orders` order-book fetch. Posts a text-only proposed-remediation Slack alert on any mismatch (e.g. "correct `open_positions.shares` to N" / "place a stop-loss order now") — deliberately detection-only, no execution path; broker treated as ground truth (`automation_principles.md` #1). Scoped down from the original design's clickable-approve-button idea after confirming with the user: a real execution path would need its own dedicated safety review, kept as a possible v2. Alerts rate-limited 15min per (position, mismatch-kind). 8 new tests (`tests/test_live_state_reconciliation.py`).
  - Verified: full `pytest tests/` (164 passed, was 153). `verify_trailing_buy_resolution.py`/`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` clean. None of these four changes has been observed live yet (see `docs/live_test_coverage.md`); `dry_run=True` everywhere, unchanged.
- **Full-stack Opus review + duplicate-order-guard broker-truth fix + a critical duplicate-sell bug, 2026-07-22**: a scoped independent Opus review of the whole automation surface (`schwab_client.py`, `schwab_safety.py`, `active_signals.py`, `signals_notify.py`, `signals_compute.py`, `signals_db.py`, `paper_trading.py`), asked to focus on seams between features individually reviewed in prior sessions rather than re-litigating each in isolation.
  - **Duplicate-order guard now confirms against Schwab's real order book for real accounts** (closes the "bigger structural change" item flagged 2026-07-21): `check_order`'s duplicate check still uses the local `recent_orders` record as the initial candidate signal, but for a non-`dry_run` account it now cross-checks a new `_broker_confirms_order` (against a new unfiltered `_all_orders`, `_open_orders`'s sibling) before blocking — a failed/rejected/errored prior attempt no longer wrongly blocks a legitimate retry. Dry-run accounts keep the old pure-local behavior (nothing real to verify against). 2 new tests.
  - **CRITICAL: trailing-arm state clobber caused re-arming and a second live trailing-sell order for the same shares.** `check_sell_condition` persists the newly-armed state (`trailing=True, peak=P`) to the DB, but `notify_trailing_activated` was merging its reminder fields onto the **stale pre-arm** `pos['trail_state']` the caller passed in, silently overwriting the real armed state right after arming — the next bar re-armed the position and (since a TrailingBoth position entered via trailing-buy has no `sl_order_id` to cancel first) placed a duplicate trailing-sell order, an oversell risk if both filled. Fixed via new `signals_db.get_position_by_id(position_id)` (fresh single-row lookup by PK) — `notify_trailing_activated` now re-reads before merging. 1 new test.
  - **SELL-side had no resting-order duplicate guard at all** (only BUY did) — the structural gap that let the bug above actually stack two live orders. New `schwab_safety._has_open_sell_order`, wired into `check_order` as a same-ticker-only SELL check (unlike the BUY guard, not account-wide — an unrelated resting BUY must not block closing a position). 2 new tests.
  - **Manual-fix Slack alert when `_attempt_automated_sell` cancels a resting SL but the trailing-sell placement then fails**: per explicit user direction, no auto-recovery/reordering (no safe alternative exists) — just a 🚨 UNPROTECTED alert with the SL price to manually re-place. 1 new test.
  - **Poll-loop/Slack-handler race on `open_position`/`close_position`**: both did a SELECT-then-act with no lock spanning both statements; the poll loop thread and the Socket Mode Slack handler thread (same process) could each pass their own existence check before either committed. Fixed with a plain `threading.Lock()` (`signals_db._position_lock`) — single-process/multi-thread daemon, not the cross-process concern `schwab_safety`'s file lock guards. `close_position` now returns `True`/`False` (was implicit `None`) and is a safe no-op if the row's already gone, instead of silently re-writing `trade_log`'s exit fields with a racing second call's values. 3 new tests.
  - **Not fixed, explicit user call**: kill switch / per-ticker pause also blocking protective sell orders, not just new entries — understood behavior (turning the algo off means accepting the exposure), not treated as a bug.
  - Verified: full `pytest tests/` (172 passed, was 164). `verify_trailing_buy_resolution.py`/`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` clean. Nothing here has been observed live (see `docs/live_test_coverage.md`); `dry_run=True` everywhere, unchanged.
- **Order-submission retry + mobile-readable UNPROTECTED alerts, 2026-07-22 (continuing session)**:
  - **`schwab_client._submit_order_with_retry(account_hash, order)`** (new): retries only the actual broker `place_order` call (up to `_ORDER_SUBMIT_RETRY_ATTEMPTS=3`, `_ORDER_SUBMIT_RETRY_INTERVAL_SECS=2` apart) on a generic exception (timeout, connection error, transient 5xx) — deliberately does *not* retry `schwab_safety.approve_and_record()` itself, which must run exactly once per attempt (it increments the daily/burst-cap counters and the duplicate-order fingerprint; retrying it would make one real attempt look like several against those caps). A `SafetyViolation` already happens before this helper is ever reached, so it's structurally never what gets retried — retrying a deliberate policy block (kill switch, cash, paused ticker) would just re-check unchanged state for zero chance of success while delaying the fallback alert. Wired into all three real placement call sites: `_place_equity_order` (BUY/SELL market, covers the post-fill top-up too), `_place_trailing_order` (trailing buy/sell), `place_stop_loss`. 3 new tests in `tests/test_schwab_client.py`.
  - **UNPROTECTED alerts reformatted for mobile** (new standing convention, see `CLAUDE.md`): all three (the trailing-sell-placement-fails-after-SL-cancel alert in `_attempt_automated_sell`, and the two stop-loss-placement-failure alerts in `_place_stop_loss_for_position`) now lead with `🚨 *{ticker}* ({account}) UNPROTECTED — place stop-loss SELL {qty} @ ~${price}` on line 1 — the actionable fact (ticker, account, exact action) survives a truncated mobile notification preview — with the technical reason (the underlying exception) pushed to a parenthetical second line. Previously buried ticker/account/price inside one long run-on sentence with no account at all. 1 test assertion updated.
  - **Discussed and deliberately not built yet, backlogged**: a max-cumulative-BUY-notional-per-ticker-per-day ceiling (multiplier on `watch_list.starting_notional`), as a backstop distinct from every existing guard — walked through why none of `notional_cap`/`HARD_ORDER_CEILING` (per-order only), `daily_order_cap` (per-account order *count*, not per-ticker notional), the resting-order guard (no protection against repeated *market* buys, which fill almost instantly), or the duplicate-order guard (60s window / 5% quantity tolerance only) actually bounds total same-day BUY exposure to one ticker, and why the cash-balance check doesn't either (bounds total account capital, not intended trade size — an account can hold more cash than one ticker's `starting_notional`, especially with compounding). Design converged on: normally `1.1x starting_notional`; if a same-day sell already closed the position, base it on `~1.0-1.05x` the most-recent sell's notional instead (this strategy always exits fully, so "most recent" is unambiguous) rather than stacking both allowances. **Not built** — user wants to think about it further before committing to a number. See `docs/backlog_cache.md`.
- **Stale-cache race fixed + new observability primitives, 2026-07-22**: found via a real HIBL paper trade entering and SL'ing 31 seconds apart (full root-cause in `docs/research_log.md`'s 2026-07-22 entry). `signals_compute._current_price()` had no staleness check on its cached-CSV read, so a poll landing between market open and that ticker's first same-day refresh could hand back yesterday's close as if it were live — the mechanism behind the false entry/exit. Fixed: returns `(None, None)` if the cache predates today past market open; all 6 call sites (2 real, non-paper) already handled `None`. Independent Opus review confirmed the fix and flagged one residual gap (not yet fixed): a genuine live-day data-refresh failure now silently suppresses a real position's intrabar exit check with only a trace log, no Slack alert. Also built, prompted by the same investigation: `signals_db.slack_message_log` (full text + mode of every real `_post_message` call — previously no persistent record existed at all) and a shared `signals_helpers.log_poll()` helper writing `[poll]`-prefixed lines to the existing `VERBOSE_LOG_PATH`, wired into every price/bar-consuming decision point (`_current_price`, `_check_position_exit`, `_scan_pinned_exit_arm`, `_scan_pinned_entry`, `_check_limit_fill`, both `paper_trading.py` poll functions). Full suite: 181 passed. See `docs/backlog_cache.md` for the still-open canary-node and `live_sim.py`-harness follow-ups from the same investigation.

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

- Reads from `open_positions` in `cache/trading_live.db` (moved from `trading_universe.db` in the 2026-07-07 DB split, see Layer 3 above)
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

**Addendum (2026-07-14)**: Schwab module skeleton added — `schwab_auth.py` (OAuth via the
`schwab-py` library's `easy_client`, token cached at `cache/live/schwab_token.json`; the 7-day
refresh-token expiry means unattended operation still needs a human to redo browser login
roughly weekly, no way around this today), `schwab_client.py` (account-nickname→hash resolution
from env vars, `place_equity_buy`/`place_equity_sell`), `schwab_safety.py` (the gate every order
must pass through first: per-account allowlist/notional-cap/daily-order-cap/dry-run flag, a hard
global order ceiling, and a global kill switch). All accounts start `dry_run=True` with
placeholder caps — real numbers and the first interactive OAuth login are still pending. No
order has been placed against a real account yet.

**Addendum (2026-07-15)**: First real OAuth login completed (IRA account only, matched to
`SCHWAB_ACCOUNT_IRA` by masked suffix, never a full account number in `.env`) — connectivity,
account-hash resolution, and dry-run order calls verified end-to-end against the real API.
`schwab_client.py` gained `place_trailing_buy`/`place_trailing_sell` (real `TRAILING_STOP`
broker orders via the generic `OrderBuilder`, since `equity_orders` has no convenience wrapper
for it) — mirrors the manual workflow's actual entry/exit mechanics (`docs/CLAUDE.md`'s
`TrailingBothZScoreBreakout` notes) rather than polling for the bounce/pullback ourselves.
`schwab_safety.py` gained real guardrails beyond the 2026-07-14 skeleton: a ticker allowlist +
account-consistency check (both sourced live from `watch_list`, not cached), a duplicate-order
window, a same-day-re-buy block (real cash-account good-faith-violation risk — same-day-*sell*
is deliberately not blocked, a soft employer preference not a broker rule), a BUY-only
signal-window time gate (mirrors `active_signals._in_buy_window`; SELL isn't gated since
`check_sell_condition` runs continuously, not just in the two windows), and
`AUTOMATION_ENABLED_TICKERS = {"KORU"}` — automation is scoped to one ticker for now (SOXL was
considered but has an open manually-entered position; automation shouldn't grab control
mid-position). The kill switch now persists to `cache/live/schwab_kill_switch.json` (survives a
daemon restart, unlike a bare env var) with Slack "Stop Engine"/"Start Engine" buttons wired into
the reference report. Still not wired into `active_signals.py` at all — every call this session
was direct/manual, dry-run only.

**Addendum (2026-07-15b) — corporate-action detection**: `signals_helpers.detect_price_discontinuity`
matches the reference/current price ratio against known round-number split factors (2, 3, 5,
10, 20, ... and inverses) within a tolerance, rather than a bare magnitude threshold — a 3x
leveraged ETF can plausibly crash >66% in one real extreme day, so magnitude alone can't
distinguish a real crash from a split; a real move landing within tolerance of a clean ratio by
coincidence is vanishingly unlikely. Wired into both `compute_buy_signal` (freezes new-signal
generation on a stale `prev_close`) and `check_sell_condition` (freezes SL/arm checks on a stale
`entry_price` — the exact false-SL mechanism KORU's split exposed live). The buy-side freeze
self-heals via `data_manager.py`'s matching merge-guard (same round-number match, rescales the
whole local CSV cache before merging in fresh data). The sell-side freeze needs a human: one
Slack alert per detection (not one per poll — tracked in `cache/live/corporate_action_alerts.json`)
with a proposed correction and an "Apply Correction" button; applying it directly fixes
`entry_price`, which is what clears the freeze (there's no separate frozen-flag to toggle —
the discontinuity check just stops matching once the data's back in scale).

**Addendum (2026-07-17) — GDXD promotion, `entry_timing='open_check'` goes live-actionable, real settlement finding, still not wired into `active_signals.py`**: `signals_blocks._build_buy_blocks` fixed to size trailing-buy orders conservatively (`shares = target_notional // (price × (1 + trail_buy_pct))`) instead of off the signal-time price — the real fill price is unbounded in both directions relative to signal price, so the old formula could spend more than budgeted. `watch_list` gained `entry_timing` (default `'close'`) and `starting_notional` (default `50000`, backfilled) columns — `_last_sale_recovery(ticker, starting_notional)` now requires the caller to pass this explicitly (raises if both trade history and `starting_notional` are missing) instead of a hidden flat-$50k fallback.

`entry_timing='open_check'` (previously backtest-only, no live equivalent, see the still-open backlog item elsewhere in this file) is now live-actionable: `active_signals._OPEN_CHECK_WINDOWS = [(9,31,9,40),(14,31,14,40)]`, an earlier poll near each relevant bar's Open, alongside the existing close-window checks. Signal-scanning refactored into `_scan_buy_signals()`, shared by both windows — an `open_check` node only gets evaluated in the early window; a `close` node only in the existing windows; the pre-existing `buy_alerted` dedup (keyed without a time/window component) is what stops an open_check node's early BUY from re-firing at the later close check, no new state needed. GDXD promoted into the real live `watch_list` as the first `entry_timing='open_check'` node and the first ticker with a non-flat `fixed_sl` (1%, vs. everyone else's unswept global 15% default) and non-$50k `starting_notional` ($5k) — a deliberate, checklist-reviewed, user-confirmed small pilot (`docs/watchlist_candidate_checklist.md` gained checks 9/10 for same-day-block sensitivity, found to gut ~93% of this node's robust alpha — accepted given the small size).

`schwab_safety.AUTOMATION_ENABLED_TICKERS` swapped `{"KORU"}` → `{"GDXD"}` (KORU flat, no handoff risk either way); `_SIGNAL_WINDOWS` widened to include `_OPEN_CHECK_WINDOWS` so the automated BUY gate doesn't reject GDXD's real early-window orders. New per-ticker automation pause/resume (`ticker_automation_enabled`/`pause_ticker_automation`/`resume_ticker_automation`, persisted to `cache/live/schwab_ticker_automation.json`, mirrors the kill-switch pattern) with Slack buttons on the reference report, shown only for tickers in `AUTOMATION_ENABLED_TICKERS`.

**Real settlement-behavior finding**: live-tested directly against the real IRA account (confirmed $271,662.09 settled cash via `client.get_account()` first — already above both `HARD_ORDER_CEILING=$100k` and the IRA's own $75k cap). User placed a real $200k `TRAILING_STOP` buy order, then separately a real large limit order, directly in Schwab's UI: **buying power was unaffected by either** — Schwab does not reserve/check buying power for a resting order at placement time, only (presumably) at actual fill. This is the opposite of the working hypothesis going in ("a cash account may already provide a hard backstop") — no such backstop exists at placement time; `schwab_safety`'s own per-order caps are the only protection today. New guard added in response: `schwab_safety._has_open_order()` queries Schwab's real live order book (not local state) and `check_order` now refuses a second concurrent BUY for a ticker that already has one outstanding — SELL is never blocked by this, same asymmetry as the same-day-re-buy guardrail. Aggregate-across-tickers exposure (multiple different tickers' resting orders collectively exceeding real cash) is not yet guarded against — flagged as a real gap for whenever `AUTOMATION_ENABLED_TICKERS` widens beyond one ticker.

**Still not wired into `active_signals.py` at all** — every order this session (including the settlement test) was placed directly by the user in Schwab's UI or via a one-off script, not through the daemon's actual signal loop. `schwab_client`/`schwab_safety` are fully built and gated but nothing in `run_loop` calls them on a real BUY/SELL signal; GDXD today alerts through the exact same manual Slack workflow as every other ticker. That wiring — where in the loop it plugs in, what triggers a real vs. dry-run call — is unscoped, not just unbuilt.

Also added `scripts/sim_delayed_sell.py` + `export_trades.simulate_trail_both_deferred_sell`: a repeatable, per-ticker/per-node bar-by-bar replay (pure-Python mirror of `_simulate_trail_both`, reuses the real entry logic unchanged) quantifying the cost of intentionally deferring a same-day exit to the next calendar day — the mirror image of the existing `same_day_block` kernel feature (which defers the entry side instead). Caveat: this pure-Python mirror only supports `entry_timing='close'`, no `open_check` branch, so results for an `open_check` node like GDXD are indicative, not exact. Found and fixed a real bug while building it: naive list-position pairing between the baseline and deferred trade sequences produces nonsense multi-month "drift" once a real deferral shifts the timeline — fixed by matching trades on entry bar index instead, which is only valid up to first divergence (reported explicitly).

**Addendum (2026-07-17b) — `schwab_client` finally wired into `active_signals.py`'s real BUY/SELL loop**: closes the "still not wired" gap from the addendum above. Scope: only the trailing-buy/trailing-sell path (`TrailingBothZScoreBreakout`, the only strategy any `AUTOMATION_ENABLED_TICKERS` ticker uses) — plain market-order automation is untouched, no live ticker needs it.
- **Automated placement**: `signals_notify.notify_buy_signal`/`notify_trailing_activated` now call `schwab_client.place_trailing_buy`/`place_trailing_sell` directly for a pilot-scope ticker, instead of waiting on the "Trailing Buy Order Placed"/"Order Placed" Slack buttons. A `SafetyViolation` (paused, outside signal window, kill switch, etc.) or any unexpected exception falls back to the existing manual button flow unchanged — `schwab_client` already Slack-posts the BLOCKED/`[DRY RUN]` message either way. `signals_blocks._build_buy_blocks` gained an `auto_placed` flag: when true it renders the Filled/Missed It/Cancelled button set directly (skipping the now-redundant "order placed" step). Sizing logic extracted into `signals_helpers.buy_order_sizing` so blocks and automated placement share one formula.
- **Fill detection is a separate, opt-in capability** (`signals_notify.check_auto_fills`, polled every `run_loop` iteration, not gated to market hours since a GTC order can fill any time): a per-ticker toggle, `schwab_safety.auto_fill_detection_enabled` (persisted to `cache/live/schwab_auto_fill_detection.json`, **defaults off** — opposite polarity from the placement-automation toggle, which defaults on within pilot scope), with Slack enable/disable buttons on the reference report. When off, a placed order still needs the human Filled/Exited click, same as before. When on, `schwab_client.get_filled_order(account, ticker, side)` polls Schwab's live order book for the most recent `FILLED` order matching ticker+side and auto-calls `open_position`/`close_position` with the real fill price/qty — field parsing (`orderActivityCollection`/`executionLegs`) is best-effort against Schwab's documented schema, not yet confirmed against a real fill, which is why this stays opt-in rather than replacing the manual step outright. **Node-scoped as of 2026-07-26** (`schwab_safety.node_auto_fill_detection_enabled`, AND-gated with the ticker-level flag): the ticker-only key above was a real gap the 2026-07-25/26 wl_id refactor missed — enabling it from one node's Slack row could otherwise have silently applied to a different node sharing the same ticker in another account. The node-level layer defaults closed (including unresolvable node identity), the inverse fail-direction from the pause-mechanism's node-level layer, since this flag grants trust rather than restricting it. See `docs/deep_backlog.md`'s 2026-07-26 entry.
- Every account is still `dry_run=True`, so automated placement is exercisable for real right now with zero live-order risk — this doubles as the pre-cutover sanity test. New tests in `tests/test_schwab_automation.py` (9 cases: automated placement happy path + three fallback scenarios, fill-detection toggle default-off no-op, and both buy/sell auto-fill recording paths with `get_filled_order` monkeypatched).
- **Not done**: `dry_run` has not been flipped to `False` for any account, and the auto-fill-detection toggle has not been turned on for GDXD — both deliberately deferred to a later, separate decision once the dry-run placement path is watched live through a real signal window.

**Addendum (2026-07-17c) — consolidated candidate-checklist tooling; deferred-exit gap-risk bug found and fixed; same-day-block backlog item resolved as unnecessary**: new `scripts/candidate_checklist_report.py` runs checks 1/2/3/6/7/8 from `docs/watchlist_candidate_checklist.md` for a ticker list in one shot, importing the existing check scripts' functions directly (`check_stock_splits.check_ticker`, `verify_trailing_buy/sell_resolution`'s signal/replay functions) rather than duplicating logic — replaces a session's worth of scattered one-off queries with one committed, rerunnable script; writes a CSV. Both `verify_trailing_buy_resolution.py`/`verify_trailing_sell_resolution.py` gained a `--adhoc` mode (ticker+params passed directly, bypassing the live `watch_list` DB query entirely) so candidate tickers not yet promoted can be checked without touching the live table.

Real finding while investigating GDXD's same-day-buy→sell economics: `export_trades.simulate_trail_both_deferred_sell`'s SL branch pinned every deferred stop-loss exit to the *nominal* `stop_price`, regardless of how far price actually moved while deferred — a real optimism bug (a naive first pass showed deferring same-day exits *improving* GDXD's return, +22,402%→+25,793%, which didn't survive scrutiny). Fixed: a deferred SL exit now fills at the worse of `stop_price` or the resolving day's `Open` (modeling real overnight gap-through risk, the actual cost that deferring itself creates). Corrected result: +22,402%→+10,473%, ~47% of edge retained — a real cost, not a free win. Also added `open_check` support to `simulate_trail_both_annotated`/`simulate_trail_both_deferred_sell` (mirrors `backtester._simulate_trail_both`'s Open-before-Close check, same bar, no synthetic bar) since GDXD's real live node uses it, not `close`.

Separately, discovered the live `watch_list` still runs old v3.x params (`fixed_sl=15%`, close-timing) for every ticker except GDXD — the whole v4 SL-sweep's findings (`stop_loss=1%` best everywhere checked) were never promoted beyond GDXD's pilot. Not yet acted on; checklist screening of the v4 winning nodes for the other 11 tickers is in progress (`docs/backlog_cache.md`).

The `same_day_block`-direction backlog item (this file didn't cover it in detail) is **resolved as unnecessary**, not implemented: FINRA eliminated the classic PDT $25k/4-trades-in-5-days rule entirely effective 2026-06-04 (Regulatory Notice 26-10, confirmed via live lookup), and no explicit employer rule exists either — see `docs/backlog_cache.md` for the full resolution chain. `schwab_safety.py`'s existing `same_day_block` (blocks same-day re-buy after an exit, the original cash-account-era logic) is untouched.

**Addendum (2026-07-18) — train/test split + walk-forward out-of-sample validation, resolving the long-deferred train/test backlog item**: new `scripts/train_test_split_check.py` (single 70/30 chronological split per ticker) and `scripts/walk_forward_check.py` (generalizes to N=5 equal calendar windows, evaluated independently). Both reuse the existing backtest kernel/trade list rather than re-sweeping — exploits that the strategy's SMA/std indicators are already backward-looking, so slicing one full-history backtest's trade list chronologically is numerically equivalent to running separate backtests on split date ranges. `train_test_split_check.py` gained a `period_spy_bh(start, end)` helper (slices SPY's own cached CSV to an explicit date range) after finding the original version reused `compute_bh_returns()`'s single full-history SPY benchmark for both halves, understating/distorting split-specific alpha — `walk_forward_check.py` imports and reuses the fixed helper for its own per-fold benchmarks. Full 19-ticker run (12 live watchlist + 7 screened candidates, all against v4 SL=1%/open_check nodes): the single 70/30 split proved unreliable in isolation (KORU showed an apparent 518% out-of-sample improvement traced to one outlier trade); the 5-fold walk-forward gave a materially more trustworthy picture — 14/19 tickers had zero negative-alpha folds across all 5 out-of-time windows. Added as checklist check 13 (`docs/watchlist_candidate_checklist.md`). Full writeup in `docs/research_log.md`'s 2026-07-18 entry.

**Addendum (2026-07-27 night) — order-swap mechanism moved from cancel+place to atomic `replace_order`; automated exits extended to TP/SL/TIME; exact-order fill confirmation replaces fuzzy matching everywhere**: three related fixes from a real incident (a live GDXU position corrupted by a stale fuzzy fill-match, sat unprotected ~12h).
- **`schwab_client.get_filled_order` gains `order_id`-scoped exact lookup.** Previously (see the 2026-07-17c-era description above) it always did a fuzzy "most recent FILLED order matching ticker+side" search — a real hazard if the actual just-placed order hasn't filled yet by the time a poll gives up, since it'll happily return an unrelated stale fill instead of `None`. Every real call site (`check_gap_resize`, `check_auto_fills`'s BUY and SELL branches, `drain_fill_queue`, `_sync_confirm_and_protect`) now threads through the specific `order_id` it placed; the fuzzy fallback only remains for genuinely order-id-less callers.
- **Automated exits extended beyond TRAIL.** Previously only the trailing-stop arm event (`notify_trailing_activated`/`_attempt_automated_sell`) placed a real order automatically — TP/SL/TIME exits always waited on a manual Slack tap, with no automated path at all, unlike the strategy's own assumption of an exact bar-close exit fill. New `_attempt_automated_exit_sell` places a real market sell for these three reasons (same automation-scope/`mode=='live'` guards as the TRAIL path).
- **Auto-close on confirmed own-order fill, no manual tap required.** New `check_own_sell_fills`, called unconditionally every poll cycle (not gated behind the existing opt-in `auto_fill_detection_enabled` toggle — that toggle is for detecting a fill on an order we didn't place; this only ever confirms our own known `order_id`, so there's no ambiguity to gate on). `notify_sell_signal` also attempts an immediate bounded confirm-and-close right after placing/reusing the order, skipping the manual alert entirely when it succeeds.
- **Cancel+place replaced with atomic `replace_order`.** The two-step pattern used by `check_gap_resize`, `_attempt_automated_sell`, and `_attempt_automated_exit_sell` (cancel a resting order, then place a new one) left a real window where a confirmed cancel could be followed by a failed/blocked new placement, leaving nothing resting at the broker in between. New `schwab_client.replace_equity_order_with_market`/`replace_order_with_trailing_sell` call schwab-py's `replace_order` (cancel-old + create-new as one broker call) instead. `cancel_order` is now dead code in this codebase (no remaining caller), kept for a genuine cancel-with-no-replacement case. **Known accepted residual risk** (not fixed, user's explicit call): the retry wrapper around `replace_order` can still fire a second `replace_order` against an order_id the first (client-side-failed but broker-side-succeeded) attempt already replaced — see `docs/backlog_cache.md`'s 2026-07-27 entry for the full mechanism and why it was accepted rather than fixed same session.
- Two independent Sonnet review rounds (per-user-preference, not Opus) on the real diff, all CONFIRMED findings fixed same session. Full suite 302 passed, harness 7/7, invariants clean.

Separately, same session: 6 canary proof-of-life nodes (`IVV`/`QQQ`/`IWM`/`DIA`/`VOO`/`XLF`, `mode='live'`/`account='ira'`) were accidentally deleted during an unrelated live-watchlist scope-narrowing cleanup, then restored via a corrected `scripts/add_canary_nodes.py` (ticker `SPY`→`IVV`, since `SPY` is now a real `soxl_test` live node and paper/canary dedup is ticker-only) with `scenario_expectations.node_id` relinked to the new node ids. New `scripts/audit_live_test_candidates.py` (reports real broker+DB state and a scenario-fit verdict for a set of candidate tickers) and `scripts/stage_live_test_order.py` (the repeatable direct-broker-bypass tool for staging a real order outside a signal window, replacing ad-hoc one-off queries/orders) — see `docs/live_test_coverage.md`'s new runbook section.

**Addendum (2026-07-30) — daily coverage report split into a genuine "start of day" (readiness) vs "end of day" (outcome) pair, per user's explicit correction that a 7am report showing yesterday's trade-outcome results doesn't answer "is this ready to go right now."**
- **New `'informational'` `expected_frequency` tier** (`signals_db.scenario_expectations`, alongside `'daily'`/`'occasional'`/`'regression-only'`) — checked and printed every day like `'daily'`, but a miss never mints a `coverage_deviations` ticket. Applied to `canary_pinned_entry`(IWM)/`canary_time_exit`(XLF)/`reconciliation_mismatch`, whose trigger conditions are trade-conditional, not guaranteed-daily like the other 4 canary designs — these were manufacturing a real ticket on every day the condition simply didn't fire. `scripts/coverage_check.py::run_check` now queries `['daily','informational']` and tags informational misses `ticket_eligible=False`; `signals_notify.send_coverage_report` respects that flag in its unexplained-list/status-glyph logic.
- **6 new scenario_expectations rows** for the `ira` inverse-mirror nodes added 2026-07-29 (SPXU/QID/TWM/SDOW/FAZ/JNUG) that never got their own rows despite mirroring the original 6 canary designs — same `scenario_key` as their counterpart, disambiguated by `ticker`.
- **New `signals_invariants.check_staged_config_matches_expected()`** — DB-only, diffs every `staged_test_config` row's `expected_config` against the real live `watch_list` node's current values (config-drift, not outcome). `scripts/seed_baseline_config.py` seeds a `baseline_config` row for every `mode='live'` node lacking one (skips already-staged nodes, so a rerun can't launder real drift into a new "expected" baseline). New `signals_invariants.print_all_live_node_state()` prints a per-node ✓/✗ table for both real-money tiers: `soxl_ira` (SH/RETL/GDXU/DPST/SPY) and `ira` (the 13 canary/mirror nodes).
- **`active_signals.py` scheduling rework**: the outcome check (`send_coverage_report`, "did yesterday's designed trade actually happen") moved off the 7am slot into the existing 16:05 EOD slot, now checking `today` instead of `_previous_trading_day(now)`. The 7am slot is readiness-only (`signals_invariants.run_all()` + `print_all_live_node_state()`), both also now called unconditionally at daemon startup (a real gap found mid-session: the live-node-state report wasn't in the startup path, so it silently never ran on a same-day restart after 7am, which is the norm here). The EOD slot runs both halves — outcome check and a re-run of the readiness checks — per the user's explicit framing that the night session is both "how did our tests do" and "are we staged for tomorrow."
- **`signals_notify.build_reference_table`'s console print fix**: previously showed only `Version`, which can't distinguish a `mode='live'` node from a `mode='research'` node sharing one `version` (e.g. DPST has both, both `version='v5'`) — found live when the user couldn't tell how many real live nodes existed from the log. Now shows `Version (Mode/Account)`.
- Full suite 339 passed (was 321). Harness 7/7.
- **Session-wrap Opus review of the real diff found 1 HIGH + 5 lower findings, all fixed except one accepted no-op**:
  1. HIGH — the EOD block's two Slack-posting pieces (outcome coverage report, EOD invariants alert) were gated by `eod_report_alerted`, which is deliberately un-pre-seeded on the premise that the block is "read-only, idempotent, no Slack" — no longer true once these two were added, so every daemon restart after 16:05 (this project restarts constantly) would re-post both to Slack. Fixed: split into a separate `eod_slack_alerted` set, pre-seeded like `reference_alerted`/`gap_check_alerted`, plus an unconditional startup catch-up call mirroring the old 7am pattern. `eod_report_alerted` itself stays un-pre-seeded, now correctly scoped to only the genuinely idempotent log-only pieces (`build_phased_monitors_report`/`print_all_live_node_state`).
  2. MEDIUM-HIGH — `pages/14_Coverage.py`'s dashboard rendered an `'informational'` scenario's miss as a false-green "✓ met", since it inferred status purely from "no `coverage_deviations` row exists" — true for a `'daily'` miss (which does record one) but not for `'informational'` (which by design never does). Fixed: a distinct neutral status for informational rows instead of falling through to the "met" branch.
  3. MEDIUM — the new EOD outcome check's trading-day gate was weekday-only (`weekday() >= 5`), same as the pre-existing 7am version — a weekday market holiday would still mint false permanent tickets for all `'daily'` rows (now 8, doubled by the mirror-pair seed). Fixed: widened to a full NYSE-calendar check (`pandas_market_calendars`, mirroring `active_signals._is_trading_day`) in both `scripts/coverage_check.py::run_check` and `signals_notify.send_coverage_report`.
  4. MEDIUM — `check_staged_config_matches_expected`/`staged_config_status` silently `continue`d past `actual is None`, treating a field gone missing/null as a non-issue rather than the real drift it is (unlike the sibling `check_open_position_config_matches_live_node`, where that skip is legitimate). Fixed via a new shared `_config_field_mismatch` helper that reports it.
  5. LOW-MEDIUM — the same helper also closes a latent crash risk: a non-numeric `expected_config` value would have raised `ValueError` and taken down the rest of `run_all()` (no per-check isolation) — now caught and reported as its own mismatch instead.
  6. LOW — tolerance was `> 0.01` against real staged values as small as `0.1` (`arm_sell_pct`/`trail_sell_pct`/`fixed_sl` on several canary nodes), which could hide a 10% parameter drift; tightened to `1e-9` (matching the sibling check) as part of the same helper.
  Accepted as a self-healing non-issue, not fixed: an exit recorded after 16:05 now mints a same-day ticket that the old 7am-next-day timing would have caught cleanly — heals itself via `clear_deviation_if_resolved` on the next real check, which (per finding 1's fix) now actually happens reliably.

**Addendum (2026-08-01) — the EOD slot now also runs a full nightly review/plan cycle, and `daily_plan` is a new table.**
`signals_notify.build_eod_scenario_review(check_date)` runs alongside the existing outcome-check
piece in the same 16:05 slot (both the live-loop trigger and the startup catch-up path, same
`eod_slack_alerted` pre-seed guard as the pieces above so a restart after 16:05 doesn't double-post).
Posts one Slack message: a readiness headline (`verified/total (%)` pulled live from
`scripts/coverage_registry.py`'s per-branch status), the canary check (reuses `coverage_check.py`),
real live + paper activity today (closed trades and still-open positions, diffed against the prior
day's `daily_plan` row when one exists), then `build_tomorrow_plan(check_date)`'s output appended
inline. `daily_plan` (new table, `signals_db.py`) holds one row per `(plan_date, category, ticker)`
— `category='canary'` rows are a same-day copy of the relevant `scenario_expectations` row (frozen
snapshot, not a live reference, so a plan stays accurate even if `scenario_expectations` changes
later); `category='live'`/`'paper'` rows are derived from whatever position is open at plan-build
time (`_position_trigger_summary`: real `fixed_sl`/`arm_sell_pct`/`trail_sell_pct`/`max_hold_hours`
on the position, not the node — a position carries its own entry-time snapshot of these, not a live
node reference). Deliberately doesn't predict new entries — a position that hasn't opened yet gets no
plan row, since a mean-reversion signal isn't predictable a day ahead. Built directly in response to
the user's actual question ("how close are we to trading material money" / "what critical code paths
haven't been tested") — the readiness headline is the direct answer, recomputed fresh every EOD run
so it's never stale by more than one trading day.

## Test Fixtures & Coverage-Proof Techniques (reference, added 2026-08-01)

Several distinct testing/coverage systems exist in this codebase, built at different times for
different real gaps. They are deliberately **not unified into one system** — each answers a genuinely
different question, and past sessions have specifically avoided collapsing them (e.g. the Trade-Flow
Grid and the canary system were kept separate on purpose, see the `docs/backlog_cache.md`/`deep_backlog.md`
2026-07-27 entries). This section is a map of what exists and what each one is actually for, so that
doesn't need re-deriving in conversation.

| System | Answers | Scope | Key files |
|---|---|---|---|
| **Trade-Flow Accountability Grid** | "Has this real logic branch ever fired, live/dry-run/paper, ever?" — an all-time evidence log. | Project-wide (32-41+ rows, one per real trade-flow branch) | `scripts/coverage_registry.py`, `pages/14_Coverage.py` |
| **Canary/scenario system** | "Did today's expected behavior actually happen?" — daily operational monitoring, ticket-model (unexplained deviation = actionable). | Project-wide, day-scoped | `signals_db.scenario_expectations`/`coverage_deviations`, `scripts/coverage_check.py` |
| **`offline_proof_for()`** | "Is there ANY test proof this branch works, live or not?" — greps `tests/test_*.py` fresh every run for a real assertion vs. a passing mention. The connective tissue between the Grid and the techniques below. | Per Grid row | `scripts/coverage_registry.py::offline_proof_for` |
| **`fake_broker` fixture** | Drives real (non-dry_run) order-placement code against a stateful, evolving fake order book — not a per-call mock. Built because per-function mocking let real sequence bugs (place → hours pass → a later guard misreads the still-resting order) hide behind green tests. | Integration-style tests for any real broker-mutating code path | `tests/fake_broker.py`, `tests/test_fake_broker_*.py` |
| **Truth tables** | Systematic enumeration of one function's full state space in a single parametrized test, instead of scattered hand-named examples that may not cover every cell. | One function at a time (built for `check_entry_abandon`'s 12-cell `(account, order_placed, order_id)` space) | `tests/test_entry_abandon_truth_table.py` |
| **Hypothesis property tests** | Asserts an invariant holds across a *searched* space too large to hand-enumerate (used when a truth table's cell count would explode — `update_paper_buys`' 5-axis space vs. `check_entry_abandon`'s 3-axis one). | One function/invariant at a time | `tests/test_paper_trading_properties.py` |
| **Mutation testing** | "Would the test suite actually catch this *specific, real* historical bug if reintroduced?" — reverts one real fix at a time, confirms the paired test fails, then restores it. Answers a different question than coverage (which only proves a line executed, not that a wrong value would be caught). | One function/bug at a time (built for `check_entry_abandon`'s 5 historical bugs) | `scripts/mutation_test_entry_abandon.py` |
| **Staged real-order test** | "Does this actually work against the real broker, when the natural signal timing is too unreliable/slow to wait for?" — a deliberate, forced real order, not a wait-and-observe. Answers what nothing else in this table can: fake_broker/hypothesis/truth-tables prove our own code's logic; canaries/the Grid prove what's *already* happened; this is the only technique that manufactures a real event on demand. Real capital risk, so it's the most tightly protocoled entry here — see below. | One scenario/account at a time, ad hoc scripts (e.g. `scripts/live_sanity_check.py`) | `docs/deep_backlog.md`'s 2026-07-23 test-day entry; protocol below |

**When to reach for which**: coverage_events + the Grid for "is this proven in the real system at all";
canaries for "is today's expected behavior actually happening"; fake_broker for any new test that
exercises real order-placement sequencing; a truth table when a function's state space is small and
enumerable; hypothesis when it isn't; mutation testing when a past real bug needs a regression test
whose bite is itself verified, not assumed; a staged real-order test only when none of the above can
answer the question and waiting for a natural occurrence is impractically slow/unreliable (e.g. a
sizing-dependent code path that real trade notional rarely triggers by chance).

### Staged real-order test protocol (added 2026-08-02)
Established after the 2026-07-23 sanity-check day and reused for the `post_fill_topup` live
re-confirmation — the same shape each time, now written down instead of re-derived:

1. **Pick a flat node/ticker** (no open real position) so the test can't collide with capital
   already at risk. Confirm via `open_positions` immediately before staging, not from memory.
2. **Choose bypass vs. production path deliberately.** Bypass (`schwab_client` direct, like
   `live_sanity_check.py`) only proves Schwab's own behavior (order rejections, mechanics) — use
   it for broker-behavior questions. Route through the real production path
   (`schwab_safety.check_order` → `signals_notify`/`signals_handlers`) when the question is
   whether *our* code does the right thing — a bypass test can't answer that.
3. **Size deliberately to force the target condition**, not hope a natural signal produces it —
   e.g. for a sizing-dependent gate, pick a size guaranteed to cross the threshold rather than
   waiting on real notional to happen to land there.
4. **Typed per-ticker confirmation, one-shot, never loops/retries** — same guard as
   `live_sanity_check.py`, prevents a staged test from repeat-firing into a real duplicate order.
5. **Tag the result as staged, not organic**, in whatever log captures it (e.g. a note in
   `coverage_events.detail`) — a forced pass proves less than an organic one and must stay
   distinguishable later, the same way `live_sanity_check.py`'s events were correctable-but-labeled
   rather than silently blended into real signal-driven history.
6. **Clean up after**: close any position opened, revert any temporary config change, confirm real
   account state matches expectations before walking away.
7. **Document the real result** — a `deep_backlog.md` entry always; `research_log.md` too if it's
   a genuine finding, not just a fix confirmation.
8. **The user always places the real order**, same convention as sweep campaigns and daemon
   restarts — this session scopes/designs the test, never executes it.

## Two-track paper trading (built 2026-08-05 — real reconcile-and-resync engine, not the original design)

**Renamed, 2026-08-05**: "Account A/B" below was never a literal second broker account (paper
trading has none) — implemented as two `watch_list` nodes per ticker, same `account` label,
distinguished by the new `paper_role` column. Renamed to avoid the misleading "account" framing:
**live-track** (`paper_role` NULL — the existing free-running paper node, live-tick pricing) and
**daily-track** (`paper_role='daily_sync'` — the new sibling, hourly-close pricing, nightly
reconciled). "B-track"/"backtest_sync" appeared briefly in this session's first draft and are gone
from the code; only pre-existing 2026-08-04 history (`research_log.md`/`conversation_summary.md`)
still uses "Account A/B", left untouched per their append-only convention.

**Corrected mid-build, 2026-08-05**: the first version of the nightly job (`reset_backtest_sync_nodes`)
just force-closed every daily-track position at 16:05 ET regardless of state — user caught this as
wrong before it ever ran for real: every v5 node's `max_hold_hours` is 21-140 (all multi-day), so a
fixed nightly close would prevent daily-track from ever completing a real multi-day trade, making the
whole comparison vacuous. Replaced with a real reconcile-and-resync engine (below) before any
daily-track node was created against the live DB.

**Built**: (1) `signals_compute.compute_buy_signal` — a `node.get('paper_role') == 'daily_sync'` node
always prices its signal check off the last closed hourly bar's `Close`, overriding both the live
yfinance tick and any caller-supplied `price_override` (e.g. the pinned real-broker-price path), with
the same staleness guard `_current_price` already has (`_STALE_PRICE_MAX_AGE`, added after the
contextual Opus review flagged its absence). (2) `paper_trading.reconcile_daily_track_nodes()` — nightly,
wired into `active_signals.py`'s existing 16:05 ET EOD slot (log-only, no Slack, idempotent). For
each daily-track node: get its actual paper state and a fresh backtest replay's implied current state
(last raw trade if still `OPEN`, else flat) via `paper_trading._backtest_replay_for_node`. A match
requires the SAME bar (`signal_i`), not just "both in a position today". On a mismatch, the only shape
that's mechanistically explainable by price today is backtest-open/daily-track-flat — daily-track's
Close-only check is a strict subset of the real kernel's open_check logic (Open first, Close
fallback); a counterfactual re-check (would Close alone have fired at the backtest's signal bar?)
distinguishes "yes, explained by the Open-vs-Close gap → resync daily-track to the backtest's implied
entry" from "no, unexplained → halt via `db.set_daily_sync_halted`, surfaced by
`signals_invariants.check_daily_sync_halted_nodes`". Every check (match/synced/halted) logs a full
diagnostic snapshot (`daily_track_reconciliation_log` table, `db.log_daily_track_reconciliation`) —
user's explicit call ("logs would be terrible to query") — not just a verdict. (3)
`scripts/add_daily_track_paper_nodes.py` — clones each v5 live-track node's real, current config
(filtered on `version=='v5'` specifically, not just ticker+mode — GDXU's `soxl_test` pilot node was
briefly mis-included by the first draft, caught by both paired Opus reviews) into a daily-track
sibling. `add_node` gained a `paper_role` param, included in both the Python-level dedup check and a
`watch_list` schema rebuild adding `paper_role` to the real UNIQUE constraint (mirrors the 2026-07-26
`account` fix — without it a daily-track node collides with its live-track sibling on insert, since
every other field is identical by design). `paper_trade_log`/`trade_log` gained `wl_id` (previously
absent from both — without it, live-track and daily-track trades were indistinguishable in history,
the paired reviews' top finding) so `log_trade_entry` can attribute every row to its real node. Full
test coverage: `tests/test_daily_track_paper.py`.

**Fixed post-review, same session (a fresh paired Opus review round found these against the reconcile
engine above)**: (1) the bar-match compared `actual['signal_time']` against the backtest's `signal_i` —
but a real trailing-buy paper fill stores `signal_time=fill_time` (wall-clock, the deliberate
2026-07-31 hold-budget-origin fix), so it could never resolve to a bar index and every organic
TrailingBoth entry would halt on its first night. Fixed via a new nullable `signal_bar_time` column
(`open_positions`/`paper_positions`/`trade_log`/`paper_trade_log`, `open_position`/`log_trade_entry`
gained the param) populated only by `paper_trading.update_paper_buys`' bounce-fill completion, from
the pending buy's own bar-aligned `signal_time`. (2) A resting paper pending buy (bounce-fill wait
phase) was invisible to the reconcile (only `open_positions` was checked), causing a false halt on a
pure fill-timing race — now checked first and skipped for the night (`action='pending_skip'`, no
verdict) rather than judged. (3) The resync branch could reopen a position daily-track had already
legitimately closed (exits aren't price-source-isolated, so "flat" doesn't always mean "missed it") —
now checks `db.get_trade_log_for_wl_id` for a prior closed trade against the same signal bar before
resyncing. (4) The `daily_sync` price path could pick up yfinance's still-forming current-hour bar
(never trimmed by `data_manager.fetch_live_data_smart`), defeating the whole premise during the back
half of a signal window — now drops the last row when its hour matches the current wall-clock hour, for
live calls only. (5) The staleness guard applied unconditionally, breaking every historical-replay
caller (`as_of`/`df_hourly_override` supplied) — now gated to live calls only. (6) Resync's synthetic
position had `shares=None` and `signal_price=entry_p` (manufacturing a fake zero entry_drift) — now
sized like a real bounce-fill and priced at the bar's actual Close. (7) No dedup on
`daily_track_reconciliation_log` per (wl_id, check_date) — a restart after 16:05 would double-log; now
a no-op if already reconciled today. Full regression coverage for all 7:
`tests/test_daily_track_paper.py`.

**Exit-side coverage added, same session** (user pushed back on the entry-only scope: "i feel like we
want to have exit side covered?"). Initial instinct was to force daily-track's exit checks to bar-close
only (matching the backtest's once-per-bar resolution exactly) — user pushed back on that too ("i think
exits on papertrading should be real and reconcile should figure out that it was a time price issue"),
which is the better design: `check_paper_sells` stays fully unchanged (real, reactive, mid-bar live-tick
fallback intact for every node including daily-track), and the reconcile is what resolves a mid-bar exit
after the fact. New `_bar_containing(df_h, ts)` (last cached bar at-or-before wall-clock `ts` — a
different lookup than entries' `_bar_index_for`, which needs an exact match since a trailing-buy signal
bar can lag its fill by many bars; an exit's wall-clock time, by contrast, always falls inside the same
bar its trigger action occurred in) resolves which bar a real exit landed in, compared against the
backtest replay's own `exit_i` for the same trade — same bar is a match regardless of the exact
intrabar price (the same "explained by price resolution" framing as the entry side's Open-vs-Close
case), different bars halt (no known price-source explanation once entries already match).
`reconcile_daily_track_nodes` restructured: daily-track's actual state is now 'open' / 'closed' / 'flat'
(previously just open/flat) — for 'open'/'closed', the backtest's trade for the SAME entry signal bar is
found by searching the full trade history, not just the tail, so an already-resolved trade on both sides
gets its exit compared too. daily-track closing early while the backtest replay still shows the trade
open is logged only, never resynced (user's explicit call, same session — reopening would fabricate a
duplicate of a trade that already really happened). New action values: `exit_diverged_no_action`
(daily-track closed, backtest still open — log only) alongside the existing `match`/`synced`/`halted`/
`pending_skip`/`skipped_already_closed`. `daily_track_reconciliation_log` gained `actual_exit_time`/
`backtest_exit_time` columns (queryable, not just prose in `detail`). 3 new regression tests (exit match,
exit-bar mismatch, daily-track-still-open-but-backtest-exited) in `tests/test_daily_track_paper.py` (17
total).

**Corrected after a second paired Opus review round, same session** — both reviews independently found
the exit-side layer's first draft had 2 real bugs, plus the user pushed the design further:

1. **`'closed'` state permanently shadowed `'flat'`** (contextual review, CONFIRMED) — the first version
   anchored the actual/backtest comparison on daily-track's own most recent closed trade, with no recency
   check. Once a node had ANY trade history, the entry-miss/resync path (the core feature, reviewed twice
   already) became permanently unreachable, and a genuinely NEW missed signal at a later bar would
   silently log `'match'` against the old, unrelated trade — actively hiding the exact divergence this
   design exists to catch. **Fixed by re-anchoring the whole comparison on `bt_ref` (the backtest's own
   most recent trade, `trades[-1]`, open or closed) instead of daily-track's history** — daily-track's
   state is now determined by searching for ITS OWN record (open position or closed trade) matching
   `bt_ref`'s specific signal bar, so old, unrelated history can never mask a new miss. New
   `actual_state='open_different_trade'` for the (rare) case where daily-track holds something matching
   neither the backtest's reference trade nor any resolvable flat/closed state — halts, unexplained.
   Regression test: `test_reconcile_catches_new_missed_entry_after_old_closed_trade`.
2. **Bar-close exits systematically off-by-one** (independent review, CONFIRMED, reproduced) —
   `check_paper_sells` records `exit_time=datetime.now()` (wall-clock poll time). For an `at_bar_close`
   exit specifically, that wall-clock moment always falls chronologically inside the NEXT bar's window
   (bar N ends exactly when bar N+1 begins, and the poll detecting the close fires shortly after), so
   `_bar_containing(exit_time)` misattributed every clean bar-close exit to the wrong bar — halting
   daily-track nodes on precisely their most backtest-comparable exits. **Fixed with the same pattern as
   entries' `signal_bar_time`**: new nullable `exit_bar_time` column (`trade_log`/`paper_trade_log`,
   `close_position`/`log_trade_exit` gained the param), populated by `check_paper_sells` with the real
   graded bar (`last_bar_ts`) on the `at_bar_close` branch, left `None` on the mid-bar reactive branch
   (where wall-clock `exit_time` genuinely does fall inside the trigger bar, and `_bar_containing` is
   correct). Reconcile prefers `exit_bar_time` (exact match) when present, falls back to
   `_bar_containing` on wall-clock `exit_time` otherwise. Regression test:
   `test_reconcile_exit_bar_close_uses_exit_bar_time_not_wallclock`.
3. **User pushed the "open, backtest closed" halt further** — asked directly why the blind halt
   couldn't also try to explain itself by price, the same discipline as entries. Confirmed valid (both
   reviews flagged the blind halt as likely to fire on the single most common exit divergence: the
   backtest's intrabar Low breaching a stop daily-track's `POLL_SECS`-cadence live-tick sampling never
   saw). **Added a symmetric exit-side counterfactual**: new `_close_alone_would_breach_sl` — at the
   backtest's exit bar, would the bar's Close ALONE (not the intrabar Low) also breach the fixed-SL
   trigger? If not, wick-only, explained — resync by FORCE-CLOSING daily-track's position at the
   backtest's implied exit (the same "sync back to the correct state for next day" principle as the
   entry side, applied symmetrically). If Close alone would also breach (a real, persistent gap, not
   just a wick) or the exit was trailing-armed (no reconstructable fixed threshold without peak-tracking
   state not available from the trade dict alone — an acknowledged, undone limitation), unexplained —
   halt. Regression tests: `test_reconcile_halts_when_daily_track_still_open_but_backtest_trail_exited`,
   `test_reconcile_resyncs_wick_only_sl_exit`.
4. **A `numpy.bool_` identity-comparison bug** (`explained is False` never matches a numpy array
   comparison result, a different object from the Python singleton) was found and fixed live while
   testing piece 3 above, before it ever reached review — compare by value (`breach is not None and not
   breach`) instead.

`tests/test_daily_track_paper.py` had 20 tests total at this point, including the two entry-miss
resync tests, three original exit-side tests, and five new tests covering pieces 1-3 above plus a
strengthened non-last-bar exit-match test (the independent review flagged the original as too weak to
have caught the off-by-one itself, since it happened to use the frame's last row, which `searchsorted`
trivially clamps to regardless of the bug).

## Final redesign, same session: reconcile is pure observation, no resync, no halt

After landing the reconcile-and-resync engine above, the user re-examined the whole premise mid-review:
"i'm not sure i agree with halts - we can just review everything after hours?" then, after discussion,
"no i think i didn't want to auto sync" and "in fact letting it run might show up more interesting
behaviors." The reasoning: auto-resyncing (opening/closing positions whenever a divergence is proven
"explained") erases exactly the natural drift that's most interesting to study over a long comparison
window, and halting on unexplained divergence would quiet nodes down whenever something is actually
happening -- the opposite of what a multi-week comparison is for. Final framing, the user's words:
**"reconcile as one action (how far are we) vs sync (prepare for next day)"** -- two conceptually
distinct operations. Reconcile (this session's scope) is now pure classification/logging; a "sync"
action that would actually realign daily-track's state is a separate, not-yet-built concept for later.

**What changed**: every `db.open_position`/`db.close_position`/`db.set_daily_sync_halted` call was
removed from `reconcile_daily_track_nodes` — it now only computes and logs. Action labels were renamed
from imperative (`synced`/`halted`) to descriptive (`entry_miss_explained`/`entry_miss_unexplained`,
`exit_wick_explained`/`exit_wick_unexplained`, `exit_bar_mismatch`, `exit_early`, `ambiguous_position`,
`no_backtest_data`, `match`, `pending_skip`) so the log reads as a classification, not a claim that
something was done. `daily_sync_halted_at`'s schema, `set_daily_sync_halted`, the gate in
`start_paper_buy`/`start_paper_market_buy`, and `signals_invariants.check_daily_sync_halted_nodes` were
all left in place (harmless, inert — nothing sets the flag automatically anymore) as scaffolding for
whenever a real "sync" tool is built.

**The trailing-stop wick counterfactual (the earlier "fix #1" ask) was built as part of this same
pass**, purely for richer classification, not action: new `_close_alone_would_breach_trail` reconstructs
the peak the same way the kernel does (`backtester.py`/`scripts/export_trades.py`'s
`simulate_trail_both_annotated`: peak starts at the arm bar's Close, then `max(peak, High)` each
subsequent bar) but using only Close, from `arm_i` to `exit_i` — if that alone would also trigger an
exit, not explained; if not, wick-only, explained. A TIME-forced-while-armed exit (`held >=
max_hold_hours`) is checked first and always unexplained (a bar-count question, not a price one — no
counterfactual applies). 6 new tests cover the trail-wick-explained, trail-wick-unexplained, and
TIME-forced-while-armed cases, alongside the SL wick case's own explained/unexplained pair.

`tests/test_daily_track_paper.py` was rewritten (not patched) to match: every test that previously
asserted a position was opened/closed now asserts it was left UNCHANGED by the call. 24 tests total,
covering the full classification matrix (both entry and exit sides, explained and unexplained, plus the
pending/ambiguous/no-data edge states) with zero state-mutation assertions anywhere in the file.

**Not built**: piece 4 (`scripts/paper_vs_backtest_reconcile.py`'s own rebuild to use `wl_id`-scoped
queries and report reconciliation history) and the kernel's "waiting"/bounce-fill internal state (still
invisible to the entry-side comparison — a lower-severity gap flagged by the contextual review, not yet
addressed). No daily-track node has been created against the real live DB yet — `add_daily_track_paper_nodes.py`
is built and tested but not yet run for real.

## Final paired review of the pure-observation design, same session — 1 real bug found at two depths, plus a genuine shared-utility fix

Both reviews independently confirmed the three core claims (zero state mutation in `reconcile_daily_track_nodes`,
the `daily_sync_halted_at` gate is genuinely inert — `set_daily_sync_halted` has no production caller anywhere
in the codebase, only tests — and the trailing-stop peak reconstruction is faithful to the kernel) and re-verified
the full suite (533 passed). Findings:

1. **[MEDIUM-HIGH, confirmed by both, at two depths] The `arm_i is None` discriminator misclassified
   TIME-forced exits as SL wick-explained.** The contextual review caught the surface bug: `time_forced`
   required `arm_i is not None`, so an UNARMED TIME exit (the kernel's TWIN/TLOSS result for a trade that
   never armed) fell through to the SL counterfactual, found Close nowhere near the SL threshold, and logged
   `exit_wick_explained` with an actively false "SL breach was wick-only" detail — silently hiding a real
   bar-count divergence. **Fixed**: dropped `arm_i is not None` from the condition — `held >= max_hold_hours`
   alone is a sufficient and correct TIME-forced signal regardless of arm state (the kernel's TWIN/TLOSS
   result implies it by construction). The independent review went deeper and found the root cause was worse
   than the surface symptom: `scripts/export_trades.py::simulate_trail_exit_chaos` (the kernel mirror for
   `TrailingExitZScoreBreakout`) hardcoded `arm_i=None` in **every** trade dict it ever emitted, at all 5
   `trades.append` sites, regardless of whether the trade genuinely armed (crossed take-profit into the
   trailing-stop phase) — even though the real strategy (`strategies.py::TrailingExitZScoreBreakout`) has
   the identical arm-then-trail state machine `TrailingBothZScoreBreakout` does. This meant every genuine
   trailing-stop exit for this entire strategy family was misrouted to the SL counterfactual (wrong
   threshold, not just a wrong label). **Fixed at the source**: added real `arm_bar` tracking to
   `simulate_trail_exit_chaos` (mirroring the pattern already used in the other three `simulate_trail_*`
   mirrors in the same file) — armed exits now correctly report `arm_i`, unarmed SL/TIME exits correctly
   keep `arm_i=None`. Verified no other consumer of this function reads `arm_i` (13 scripts call it; none
   reference the field), so this is purely additive. New `tests/test_export_trades_arm_tracking.py` (4
   tests, deterministic constructed price paths, not random) proves genuine trailing exits, genuine SL
   exits, unarmed TIME exits, and still-open armed trades all report the correct `arm_i` — the 2nd and 4th
   would fail without the fix.
2. **[LOW, confirmed] `no_backtest_data` was documented but never emitted** — the entry-miss branch's
   "counterfactual literally can't be computed" case (no prior-day indicator row, or zero-Std day) fell
   through to `entry_miss_unexplained` with an affirmative "would have fired on Close alone too" detail the
   code never actually evaluated. Fixed: emits `no_backtest_data`/`explained_by_price=None` instead.
3. **[LOW, confirmed] Silent replay failures** — a permanently-broken node (missing CSV, unhandled
   strategy) only printed and skipped, invisible in the log every night. Fixed: logs `action='replay_failed'`.
4. **[LOW, confirmed] Stale docstrings/comments still describing the abandoned resync-and-halt design** —
   `signals_db.py`'s `paper_role`/`daily_sync_halted_at` migration comments, the `daily_track_reconciliation_log`
   comment listing `'match'/'synced'/'halted'` as the action vocabulary, `get_trade_log_for_wl_id`'s
   "pre-resync check" docstring, and `add_daily_track_paper_nodes.py`'s "nightly reconcile-and-resync"
   line — all rewritten to describe the actual pure-observation behavior.
5. **[LOW, test honesty, confirmed] Four "position unchanged" assertions only checked `is not None`**,
   not the actual field values — would have passed even if reconcile mutated `entry_price`/`shares`.
   Strengthened to assert the exact unchanged `entry_price`.
6. **[MEDIUM, contextual judgment, resolved as "working as intended"]** The independent review flagged
   that `open_check` daily-track nodes structurally can never fire an Open-leg entry live (the pinned
   9:31-9:40 window's staleness guard rejects the check since the day's own bar hasn't closed/cached yet),
   making every Open-leg `entry_miss_explained` a foregone conclusion rather than new information. On
   reflection this is the CORRECT, intended consequence of isolating price-source timing as the one
   variable under test — a Close-only node structurally cannot participate in an Open-leg entry, ever, by
   construction, and that's exactly the point. Clarified in `reconcile_daily_track_nodes`' docstring rather
   than "fixed" (a pinned-window exemption would reintroduce Open-vs-Close as a live variable, defeating
   the isolation).

Full suite after this round: still 533 passed (the `arm_i` fix and its 4 new tests are additive; the
`test_ticker_cycle`→`analyze_ticker_cycle` rename earlier this session cleared what used to be 1
pre-existing unrelated collection error, so this is genuinely 0 failures/errors, not 533-of-534).
`tests/test_daily_track_paper.py` is now 30 tests.

## Two-account paper trading (original design, added 2026-08-04 very late)

**Motivation**: a long investigation (`docs/research_log.md`'s 2026-08-04 very-late entry) found
paper trading's real trade sequence diverging from a backtest replay for several tickers. Two real,
distinct, non-bug causes were confirmed: (1) paper and a full-history backtest replay are two
independently-sequenced single-position simulations, never synchronized at a common starting
boundary — one small early divergence anywhere upstream cascades into a completely different trade
sequence downstream, making "trades in calendar window X" comparisons invalid once any drift has
occurred; (2) paper's real-time signal check (`signals_compute.compute_buy_signal`) uses a live
1-minute yfinance tick for its current-price comparison, while the backtest uses the fixed hourly-bar
Close — same SMA/Std indicator math (verified identical), different price input. Neither is a bug,
but they mean today's single paper-trading track can't cleanly answer both "does real execution hold
up" and "does live code correctly mirror the backtest kernel" at once.

**Design**: run two paper-tracking nodes per ticker instead of one, distinguished by role, not by a
literal separate broker account (paper trading never touches `schwab_client`/real accounts at all --
`account` is just a label field for it). Confirmed safe to co-locate under the same `account` value:
paper trading's dedup/position lookup is already `wl_id`-scoped end to end (`start_paper_buy`'s
`get_open_position_by_wl_id`, `_scan_buy_signals`'s `already_held` check) -- the one `(ticker, window)`
fallback in `active_signals.py` only fires for legacy pre-wl_id-migration rows with a NULL `wl_id`,
never for two newly-created nodes. This also happens to be the first real test of
`buy_fill_reconciles_correct_node` (coverage grid: "no live proof -- no two live nodes currently
share a ticker"), safely, on the paper side.

- **Account A (unmodified)**: today's existing paper-trading node/behavior. Never resets, accumulates
  real trade history under real market conditions and real intraminute price ticks. This is the "does
  the strategy actually hold up" signal -- divergence from backtest here is expected and is the point,
  not a defect to chase.
- **Account B (new)**: a second node per ticker that (a) uses the backtest's hourly-bar-close price
  for its signal check instead of a live tick (needs a new mode in `paper_trading.py`/
  `signals_compute.compute_buy_signal`), and (b) periodically resets to the backtest's real state
  (needs a reset/resync mechanism, not yet designed -- open question: reset on what cadence, and does
  "reset" mean re-seeding `paper_positions` to match a fresh backtest replay's current state, or
  literally closing/reopening to force alignment). Any divergence remaining in Account B is then
  provably attributable to price-availability timing alone, since the starting-boundary and
  price-source variables are both controlled for.

**Implementation pieces, not yet built or scoped in detail**:
1. `paper_trading.py`/`signals_compute.compute_buy_signal`: an hourly-bar-close price mode, gated per
   node (e.g. a new `watch_list` column or a role tag), for Account B's signal check.
2. The Account B reset/resync mechanism itself -- cadence and exact semantics still open.
3. A second `watch_list` node per ticker for Account B, `mode='research'`, same strategy/config as
   Account A's node, tagged distinctly (role/label TBD) so reporting can tell them apart.
4. `scripts/paper_vs_backtest_reconcile.py` needs a real rebuild before it's trustworthy for either
   account: per-ticker, determine the correct synchronized starting boundary from real current state
   (flat -> sync at today; holding a position -> sync in-trade at the real entry price/time) --
   confirmed tonight this is a state-query/preparation concern, not reconciliation logic itself, and
   should probably live in the still-not-yet-built shared "real current state" function discussed
   below, not be re-derived inside the reconciliation script.

**Related, still-open from the same investigation**: the broader "multiple scripts each reimplement
their own view of position/pending-buy state" architecture problem (`status_check.py` missing
`pending_buys` entirely, `coverage_check.py`'s `get_pending_buys_for_ticker_on_date` date-scoping bug)
-- both found the same night, logged in `docs/backlog_cache.md`. A shared, canonical state function
would also directly serve Account B's boundary-sync logic above, so these two backlog items should
probably be scoped together, not solved twice.

## SOXL drought-overlay parameter sweep (built 2026-08-05 — real gap found, not yet resolved)

**Built**: `scripts/drought_overlay_sweep.py` implements the design below exactly (4 independent
axes -- fixed_sl%, arm_pct%, trail_sell_pct%, confirm_days -- 1344 cells, pooled across the 10 v5
watchlist tickers, grid-neighbor cliff-safety). Runs in ~9 seconds. Full result:
`docs/research_log.md`'s 2026-08-05 (late) entry. **Real gap found, not resolved**: the best pooled
cell is cliff-safe (grid-neighbor robustness holds) but is dominated by a single ticker's small
sample (KORU, n=7, contributing the large majority of the pooled +448.8% figure) -- the same failure
shape as the original SOXL-only 2-trade fragility this sweep was built to screen out, just not the
axis grid-neighbor cliff-safety actually checks. Needs a concentration screen (max single-ticker
share of pooled compounded return, or a minimum-net-positive-tickers-at-n>=X requirement) before any
pooled cell can be trusted. Put-hedge design below stays blocked pending that fix.

### Original design (2026-08-04 very late, superseded by the "Built" note above)

**Motivation**: `scripts/drought_overlay_test.py` (2026-08-04 very late) tested the drought-overlay
exit mechanics (fixed SL + arm-then-trail) reusing the core mean-reversion node's own tuned values
(fixed_sl=2%, arm_pct=30%, trail_sell_pct=1% for SOXL) -- values tuned for a completely different
entry context (a z-band dip), not "buy into a presumed-stable rally." A coarse manual sweep (moving
SL/trail together, 1% to 18%) showed wider is directionally better (pooled compounded went from
-76.9% to +4.3%) but was never a real grid search -- SL/arm/trail were moved in lockstep, not
explored as independent axes, and SOXL's one positive result was fragile (2 of 13 trades driving the
whole outcome).

**What a real sweep needs, following this project's established sweep discipline** (same shape as
`.claude/skills/backtest-change-rollout/SKILL.md`, not a from-scratch method):
1. **Independent axes**: fixed_sl%, arm_pct% (the trailing-stop arm threshold), trail_sell_pct%,
   searched as a real 3D grid, not moved together. Confirm-days (how many no-signal days before the
   overlay enters) is a plausible 4th axis, currently fixed at 10 -- worth including or explicitly
   deciding to hold constant with a stated reason.
2. **Cliff-safety check**: per this project's standard convention (`MIN(possible, pessimistic,
   certain)`-style worst-neighbor robustness, not just best-cell alpha) -- the earlier finding that
   SOXL's result was 2-trades-fragile is exactly the failure mode cliff-safety screening exists to
   catch, and this parameter space hasn't been screened that way at all yet.
3. **Larger sample before trusting any winner**: SOXL had only 13 historical drought-overlay
   opportunities in the tested window. A real sweep should pool across all 10 v5 tickers (or at least
   the tickers where the broad-index-vs-sector-commodity gap-risk split from tonight favors the
   overlay, see the research log) for enough trades to distinguish a real edge from 2-trade luck, with
   SOXL's own result reported both in isolation and pooled.
4. **Reuses the real exit state machine already built tonight** (`simulate_overlay` in
   `drought_overlay_test.py`, itself just `TrailingBothZScoreBreakout`'s own arm-then-trail mechanic
   applied to a drought-confirmed entry) -- the sweep just needs to grid-call it with varying
   fixed_sl/arm_pct/trail_sell_pct instead of the node's own fixed values, not a new simulator.

## Put-hedge option-selection methodology (planned, added 2026-08-04 very late — not yet built)

**Motivation**: tonight's rough Black-Scholes approximation (assumed IV from realized vol, since
yfinance has no historical options data) gave directional 2-week ATM/10%-OTM premium estimates for
several tickers, and established the real mechanism reason to prefer puts over stops (a stop can be
gapped through, a put's payoff is capped/defined regardless) plus a real structural tailwind (implied
vol tends to be cheap exactly when the drought-detector would fire, since it's a low-realized-vol
period). None of this amounts to an actual selection rule yet.

**Open questions a real methodology needs to answer**:
1. **Strike selection**: ATM (full protection, expensive) vs. OTM (cheaper, but leaves an uncovered
   gap between entry and strike). The real historical tail losses found tonight (AGQ -48% single day,
   KORU -33.64% gap-blowout) need to be checked against candidate strikes directly -- an OTM strike
   picked for cost reasons alone could still leave the worst historical gap partially uncovered.
2. **Expiry selection**: tonight's overlay hold-time distribution (median 2 days, mean 5.2, max 33.8)
   is right-skewed -- a single fixed expiry either overpays for the typical short hold or leaves the
   rare long hold unprotected past expiry. Needs a real rule (e.g. tied to `max_hold_hours`, or a
   rolling/laddered approach), not a single assumed value.
3. **Real IV, not a realized-vol proxy**: `yfinance` does expose *live* option chains
   (`yf.Ticker(t).option_chain(date)`) -- a real methodology should price against actual current
   quotes when deciding whether to hedge right now, even though it can't backtest historical option
   prices (no historical chain data exists anywhere accessible to this project).
4. **Real cost-benefit rule**: tonight's comparison was worst-case tail loss vs. one premium figure,
   eyeballed -- a real rule needs expected value (probability-weighted tail-loss reduction across all
   historical drought-overlay trades, not just the worst one) against premium paid on *every* entry,
   whether or not that entry would have hit the tail case.
5. **Sizing**: contract count needs to match the overlay's real share count per entry, not a round
   number.

Depends on the SOXL sweep above landing on real SL/arm/trail parameters first, since the put's strike
distance and expiry should be chosen relative to the overlay's actual (swept, not guessed) risk
profile, not the ad hoc parameters tested tonight.

## Shared canonical position-state function (built 2026-08-05)

**Motivation**: `docs/backlog_cache.md`'s 2026-08-04 finding — multiple scripts (`status_check.py`/
`audit_live_test_candidates.py`, `coverage_check.py`, `watchlist_status.py`, `paper_trading_status.py`)
each reimplemented their own ad hoc view of overlapping live state (open positions, pending buys,
node config), so the same bug class (a resting pending buy invisible to "flat" classification) got
fixed in one script (`audit_one`, 2026-08-05 earlier) without the identical gap in `watchlist_status.py`
being touched at all. Prioritized first in the 2026-08-04 very-late session's explicit build order
(shared state function -> SOXL sweep -> two-account paper trading), though built last since paper
trading landed out of order.

**Built**: `signals_db.get_real_position_state(wl_id)` — node-scoped (not ticker-scoped, since ticker
alone is ambiguous once 2+ nodes share a ticker, the same root cause behind the earlier wl_id
refactor). Returns `{node, real_position, paper_position, pending_buy, paper_pending_buy, status}`,
where `status` is one of `'flat' | 'pending_entry' | 'holding' | 'holding_paper'` (real position takes
precedence over paper when both happen to be present).

**Scope, decided 2026-08-05**: swapped into `audit_one`/`status_check.py` (near drop-in — already
node-scoped and paper-aware, just consolidates its own real/paper/pending assembly into one call) and
`watchlist_status.py` (moderate rework — replaced a ticker-keyed `{ticker: pending}` dict with a
per-node call, fixing its latent ambiguity bug as a side effect). `coverage_check.py` (inherently
date-scoped/historical, not "current state") and `paper_trading_status.py` (an intentional unscoped
table-dump view) were deliberately left unconverted — poor fit for a "what's this node's state right
now" function, per design-time scoping discussion.

**Node resolution left out of scope, deliberately**: `audit_live_test_candidates._resolve_live_node`
(ticker -> node, preferring `mode='live'` on ambiguity) stays where it is, not folded into
`signals_db.py` — `get_real_position_state` takes `wl_id`, so the caller is still responsible for
picking the right node first. Same session, `_resolve_live_node`'s SQL gained `AND paper_role IS
NULL` — the new daily-track clones (paper_role='daily_sync', same ticker/mode='research' as their
live-track sibling) made every v5 ticker resolve ambiguously without it; a daily-track node is never
the right thing to audit via a bare ticker lookup, only ever addressed by its specific wl_id.

**Side fix, same session**: `add_node`'s tax-advantaged-account guard (`TAX_ADVANTAGED_EXCLUDED_
TICKERS = {"USO", "AGQ"}`, K-1/UBTI real-money risk) and the matching `signals_invariants.check_tax_
advantaged_excluded_tickers` daily check were both narrowed to only fire for `mode == 'live'` nodes —
found live when AGQ's daily-track clone (paper-only, zero real capital) was blocked by a guard whose
entire purpose is preventing real tax exposure. User's explicit call: "override agq into the IRA
account - we won't put it to live testing there this is paper trade only" — fix the guard itself to
be mode-aware, not route around it per-script.

## Live automation design: drought overlay, margin add-on-at-arm, put-hedge (2026-08-07, design only, not built)

**Context**: the 2026-08-07 v5-stacked research session (see `docs/research_log.md`'s entry of the
same date) validated three overlay mechanisms as real, backtested edges worth eventually running with
real capital: the drought overlay (buy the underlying during a confirmed low-vol no-signal gap,
manage with the core strategy's own SL/arm/trail state machine), margin add-on-at-arm (borrow 100% of
the current position's size when a core position arms, doubling exposure), and put-hedge (a
protective put purchased alongside any of the above, sized to the position, rolled/exited with it).
This section is the architecture and staged rollout plan for turning those into real, generic,
parameterized `watch_list` features — not yet implemented.

**Explicit correction from the framing session had drifted into**: these are NOT ticker-specific
code paths. Exactly like every other strategy parameter in this system (`fixed_sl`, `arm_pct`,
`trail_sell_pct`, etc.), drought/add-on/put-hedge are config toggles + parameters on a `watch_list`
node — any ticker's node can turn any of them on with its own parameter values. The backtest
research happened to validate specific (ticker, parameter) combinations first (SOXL drought
confirm_days=3/vol_gate=0.4, AGQ add-on), but the feature itself must be built generically, the same
way the rest of this project already works.

### Paper/live-vs-backtest reconciliation (real gap, found 2026-08-07 during design review -- missing from the first draft of this section)

**The question that exposed the gap**: the backtest validated these mechanisms against a FIXED
historical window (2023-2026). Live/paper execution runs forward on NEW data the backtest never
saw, using real-time-computed inputs (vol_pctile against an accumulating history, confirm_days
counted against live signal gaps) that will diverge from the specific historical instances already
tested. Nothing in the first draft of this design said how anyone would know whether live execution
was actually doing what the backtest said it would.

**Answer: reuse the existing daily-track pattern, don't invent a new one.** Core strategy already
solved this exact problem (`paper_trading.reconcile_daily_track_nodes`, `docs/design.md`'s 2026-08-05
"Two-track paper trading" sections) -- a nightly job replays the real backtest kernel against
whatever data actually happened that day, compares against what live/paper's real state machine
actually did, classifies the divergence (`entry_miss_explained`/`unexplained`,
`exit_bar_mismatch`, etc.), and writes one diagnostic row per node per night. Deliberately
**pure observation** -- never auto-corrects, never auto-halts, per the same reasoning that applied to
core: auto-resyncing erases exactly the signal this comparison exists to produce.

The real, useful fact for these three new mechanisms: **the backtest kernel to replay against
already exists**, built the same session this design doc's own gap was found in --
`scripts/stacked_model/drought.py::generate_drought_trades`,
`scripts/stacked_model/add_on.py::generate_addon_trades`, and
`scripts/stacked_model/put_hedge.py::apply_hedge` ARE the equivalent of `_simulate_trail_both` for
these mechanisms. A `reconcile_overlay_nodes` job (new, mirrors `reconcile_daily_track_nodes`'s
shape) would: for each node with `drought_overlay_enabled`/`addon_enabled`/`put_hedge_enabled` set,
call the matching backtest function against real data through today, compare its implied
entry/exit/P&L to what the live/paper position rows actually recorded, log one diagnostic row/night.
This is now explicitly item 3.5 in the staged pre-work checklist below -- it has to exist before any
staged real-order testing (item 6), the same way daily-track reconciliation was built before trusting
core's own live-vs-backtest parity.

**Second real gap, caught in the same design-review conversation**: the daily-track/live-track split
itself needs to exist for these three mechanisms too, not just the reconciliation job -- same reason
it was originally built for core (`docs/design.md`'s 2026-08-05 "Two-track paper trading" sections):
the backtest kernel prices everything off the last closed hourly bar's Close, but real (live-tick)
execution prices will genuinely differ, and without a daily-track variant the reconciliation job
above can't tell "the overlay logic is actually wrong" apart from "this is just normal price-source
noise between a discrete backtest assumption and continuous live pricing." **Scoped to paper trading,
same as core's existing daily-track nodes** (`paper_role='daily_sync'`) -- not a real-capital
concept, a validation-fidelity tool that runs before real capital is ever involved. Concretely: each
of the three mechanisms' paper-trading nodes (item 3 below) needs its own daily-track clone
(`paper_role='daily_sync'`, prices off last-closed-hourly-bar Close, exactly mirroring
`compute_buy_signal`'s existing `daily_sync` branch) alongside a normal live-tick-priced paper node --
`reconcile_overlay_nodes` then reconciles the DAILY-TRACK node against the backtest kernel (clean,
no price-source confound -- proves the logic), while a live-track-vs-daily-track comparison
separately quantifies how much live-tick pricing costs/differs from the idealized assumption (an
execution-quality question, not a correctness one). Folded into item 3's scope below, not a separate
checklist item -- the paper-trading extension was always going to need this shape, it just wasn't
stated explicitly until now.

### Parameterization model (proposed, not yet schema-migrated)

New `watch_list` columns (or a linked per-node config table, TBD at implementation time):
- `drought_overlay_enabled` (bool), `drought_confirm_days` (int), `drought_vol_gate` (float, nullable
  = no gate) -- mirrors `scripts/stacked_model/drought.py`'s `generate_drought_trades` signature
  directly, so the backtest and live code paths share the same parameter names.
- `addon_enabled` (bool) -- no separate parameters; add-on always borrows 100% of the current
  position's size at the moment core's own trailing-arm condition fires (per the real math validated
  2026-08-07: this sizing rule is what makes the backtest's multiplicative accounting exact rather
  than an approximation -- a different sizing rule would need new backtest work first, not just a
  new live parameter).
- `put_hedge_enabled` (bool), `put_hedge_otm_pct` (float) -- applies to whichever of core/drought/
  add-on legs are open at the time, each leg gets its OWN put (per the 2026-08-07 corrected
  reasoning: add-on is a separate share block, needs its own hedge, not a shared one with core).

### Real execution mechanics needed (not yet built, real gaps to close)

**Proposed schema** (extends `open_positions` with a discriminator + linkage, adds one new table for
options -- a genuinely different asset class, not a variant of an equity position):

```sql
-- open_positions gains:
ALTER TABLE open_positions ADD COLUMN position_source TEXT DEFAULT 'core';
  -- 'core' | 'drought_overlay' | 'addon_leg'
ALTER TABLE open_positions ADD COLUMN parent_position_id INTEGER;
  -- addon_leg rows point to the core position.id they're attached to; NULL for core/drought_overlay
ALTER TABLE open_positions ADD COLUMN drought_confirm_days INTEGER;
ALTER TABLE open_positions ADD COLUMN drought_vol_gate REAL;
  -- both NULL except for position_source='drought_overlay' rows -- records what config produced
  -- this entry, same reasoning as staged_test_config's baseline-drift protection

-- new table, options are not equity shares:
CREATE TABLE option_positions (
    id INTEGER PRIMARY KEY,
    wl_id INTEGER NOT NULL,
    linked_position_id INTEGER NOT NULL REFERENCES open_positions(id),
    ticker TEXT, strike REAL, expiration TEXT, contract_type TEXT DEFAULT 'put', quantity INTEGER,
    entry_price REAL, entry_time TEXT,
    status TEXT DEFAULT 'open',  -- 'open' | 'rolled' | 'closed'
    exit_price REAL, exit_time TEXT,
    roll_from_id INTEGER REFERENCES option_positions(id)  -- NULL unless this is a post-roll contract
);
```

**State machines** (each mechanism's real transitions, not yet coded):

1. **Drought overlay**: `WATCHING` (node's core position is flat, no signal recently) →
   `CONFIRMING` (tracking elapsed no-signal days against `drought_confirm_days`, mirrors
   `find_drought_windows`'s real logic) → `VOL_CHECK` (once confirmed, evaluate `drought_vol_gate`
   against the live vol_pctile reading, same `_entry_vol_pctile` function the backtest already uses)
   → `ENTERED` (real buy placed, `position_source='drought_overlay'` row created) → `ARMED` (price
   crossed the node's own `arm_pct`, same mechanic as core) → `EXITED` via SL/TRAIL (identical to
   core's own exit checks) OR **HANDOFF** (core's own real signal fires while this position is still
   open -- the one genuinely new cross-position dependency: the drought-overlay poll must check the
   SAME node's core signal state every cycle, not just its own SL/TRAIL levels).
2. **Add-on**: `WATCHING` (linked core position open, not yet armed) → `TRIGGERED` (core's
   `trail_state['trailing']` flips True -- the exact same event `notify_trailing_activated` already
   detects for core's own arm notification, so add-on hooks the same real event, doesn't invent a new
   detection path) → `BORROW_BUY` (place a real margin buy for 100% of the core position's current
   market value -- `shares = floor(core_position.shares * core_position.current_price /
   addon_entry_price)`, real order) → `LINKED` (`addon_leg` row's `parent_position_id` set) →
   `EXITED` (fires on the SAME trigger as the parent core position -- SL/TRAIL/TIME -- both rows close
   in the same real transaction, never independently).
3. **Put-hedge**: on any qualifying equity position's real OPEN event (core entry, drought_overlay
   entry, addon_leg creation) → if `put_hedge_enabled`: `BUY_TO_OPEN` (strike = entry_price *
   (1 - put_hedge_otm_pct/100), nearest real expiration >= some minimum days-to-cover, mirrors
   `get_roll_contract`'s real selection logic) → `HOLDING` (poll days-to-expiration) → if under a roll
   threshold AND the linked equity position is still open: `ROLL` (sell current contract, buy a fresh
   one, `roll_from_id` links them) → on the linked equity position's real CLOSE event: `SELL_TO_CLOSE`
   (realize final hedge P&L). Real ordering question not yet resolved: does the put close BEFORE or
   AFTER the linked equity sells, to avoid a moment of either unhedged exposure or double-selling risk
   -- needs a real decision at implementation time, not assumed either way here.

**New functions needed** (proposed names, matching this project's existing `signals_db.py`/
`signals_notify.py`/`schwab_client.py` conventions -- not yet written):
- `signals_db.open_drought_overlay_position(wl_id, entry_price, confirm_days, vol_gate, ...)`
- `signals_notify.check_drought_overlay_entry(node)` -- called from the same poll loop as core's own
  signal check, per-node
- `signals_notify.check_drought_overlay_exit(position)` -- SL/TRAIL/HANDOFF, HANDOFF specifically
  needs to read the SAME node's core signal state
- `signals_db.open_addon_leg(parent_position_id, borrow_shares, entry_price)`
- `signals_notify.check_addon_trigger(position)` -- hooked onto the same event
  `notify_trailing_activated` already fires on
- `schwab_client.place_option_buy_to_open(account, ticker, strike, expiration, quantity)` -- entirely
  new, no options order-placement exists in this codebase today
- `schwab_client.place_option_sell_to_close(account, option_position_id)`
- `signals_notify.check_put_hedge_roll(option_position)`

**Truth-table dimensions to enumerate** (item 5 below) -- the real combinatorial surface, not yet
spot-checked let alone exhaustively covered:
- core state: `{flat, open, armed}`
- drought_overlay state per node: `{n/a (core not flat), watching, confirming, entered, armed,
  exited}` -- by construction drought should never be `entered` while core is `open`/`armed`
  simultaneously on the same node, but that's an assumption needing an explicit guard + test, not
  just a comment
- addon_leg state: `{none, open}` -- only reachable when core state is `armed`
- put_hedge state, independently per linked position (core/drought_overlay/addon_leg can each carry
  their own put): `{none, open, rolling, closed}`
Real edge cases the enumeration needs to specifically resolve: a core position's real entry signal
firing on the exact same poll cycle a drought-overlay HANDOFF check would also fire (which wins, and
does the drought-overlay's sell need to complete before core's own buy is allowed to place, given the
same-ticker double-buy guard already in `check_order`); a put-hedge roll needing to happen on the
exact same bar its linked equity position exits; an addon_leg's SL trigger vs. its parent core
position's own SL trigger firing on the same bar (should be identical by construction since they
share the same exit rule, but needs a real test proving they never desync).

### Staged pre-work checklist, in order (per the user's own explicit list, 2026-08-07)

This project's own established convention (see `docs/design.md`'s "Test Fixtures & Coverage-Proof
Techniques" table) is that live-capital-touching changes go through several DISTINCT, deliberately
non-unified proof layers before real money is at risk. For a change this size (three new mechanisms,
one entirely new asset class), all of the following are real prerequisites, not optional polish:

1. **Design the real DB schema** for drought-overlay positions, add-on legs, and option-contract
   tracking (extends the bullet points above into an actual migration) -- do this FIRST, since
   everything else builds on top of real tables existing.
2. **`tests/fake_broker.py` extension**: add options order-placement support (buy-to-open puts,
   contract expiration/exercise/assignment simulation) -- currently equity-orders-only. A real,
   nontrivial extension, not a small add.
3. **Paper-trading extension**: `paper_trading.py` needs to simulate all three new mechanisms using
   the SAME state machine real code will use, so paper-trading coverage exists BEFORE live orders do
   (matches this project's standing practice of paper-testing every mechanism before it goes live).
   **Each mechanism needs BOTH a live-track (live-tick priced) and daily-track
   (`paper_role='daily_sync'`, last-closed-hourly-bar-Close priced) paper node**, same reason and same
   shape as core's existing daily-track split -- without it, item 3.5's reconciliation can't separate
   genuine logic bugs from ordinary price-source noise.
3.5. **Paper/live-vs-backtest reconciliation** (found missing from the first draft of this design,
   see the section above) -- a `reconcile_overlay_nodes` nightly job, mirroring
   `paper_trading.reconcile_daily_track_nodes`'s pure-observation pattern exactly, replaying
   `drought.py`/`add_on.py`/`put_hedge.py`'s real backtest functions against each day's actual data
   and comparing to what live/paper actually recorded. Must exist before item 6 (staged real-order
   testing), the same way core's own daily-track reconciliation was built and trusted before any of
   core's live-vs-backtest parity claims were.
4. **Trade-Flow Accountability Grid** (`scripts/coverage_registry.py`): new scenario rows for every
   new control point (drought entry placement, drought HANDOFF exit, add-on trigger detection, add-on
   order placement, put purchase, put roll, put exercise/sale) -- the grid can't answer "is this
   proven live" for a mechanism it doesn't know exists yet.
5. **Truth-table permutation coverage**: the real state-space here is genuinely bigger than anything
   this system has modeled before -- core+drought+add-on+put-hedge can all be simultaneously "on" for
   one node, and their real-world interactions (core arms while a drought-overlay position is also
   open on the same ticker? a put-hedge contract expires mid-hold on any of the three position types?)
   need explicit enumeration, not just spot-checking. This is exactly the kind of combinatorial
   surface the existing truth-table testing technique exists for.
6. **Staged real-order live testing**, matching the existing "Staged real-order test protocol"
   pattern already used for `post_fill_topup`/`market_buy_placement` (small real notional, organic
   real signals, no forced/faked triggers) -- applied fresh to each of the three new order types
   (drought entry, add-on entry, put purchase), one at a time.
7. **Edge-cases-on-edge-cases hardening pass**: this project's own history (the 2026-07-31
   exit/arm/entry audit found 9 real bugs, a same-day follow-up found 8 more, an independent review of
   THAT found 8 more) strongly suggests a dedicated audit pass will be needed once the above is built
   and staged-tested, not a one-and-done implementation.

**Not started this session** -- this is the plan to pick up fresh in a future session, given the real
size of the undertaking (a new asset class, three new position-tracking concepts, and the full
multi-layer proof process this project requires before real capital is at risk).

**This 8-item staged checklist was extracted into a standalone, reusable document**,
`docs/new_mechanism_promotion_standard.md`, since items 3.5 and the daily-track-split part of item 3
were both established patterns that should have been applied automatically on the first draft of
this design and instead needed direct prompting to catch -- the standalone doc exists so the *next*
new mechanism doesn't repeat that gap. Check it first for anything future, this section stays as the
concrete worked example.

### Built 2026-08-09 -- drought overlay + margin add-on-at-arm executed through live daemon wiring; put-hedge and staged real-order testing still open

Executed the staged checklist above end to end for drought overlay and margin add-on-at-arm (put-hedge
explicitly excluded, user's call at session start) -- items 1 (schema), 3 (paper-trading, both live-track
and daily-track), 3.5 (nightly reconcile), 4 (Accountability Grid), 5 (truth-table tests), and the daemon
wiring itself. Item 2 (`fake_broker.py` options support) was never needed since dropping put-hedge means
no new asset class is involved -- everything here is equity-only, using the existing paper-order
machinery. Full detail, including the real bugs found (1 CRITICAL, 4 HIGH, several MEDIUM/LOW, across
three paired Opus review rounds) and fixed: `docs/deep_backlog.md`'s 2026-08-09 entry.

**One real schema decision reverses this section's original sketch**: margin add-on legs do NOT get a
`position_source='addon_leg'` value on `open_positions`/`trade_log` as drafted above -- a cold Opus
review found that would break `get_open_position`/`top_up_position`/`set_broker_stop_price`/
`get_held_tickers`/`schwab_safety.check_order`'s double-buy guard (all assume at most one row per
ticker/wl_id, which an add-on leg sharing its parent's wl_id would violate). Add-on legs live in their own
dedicated `addon_legs`/`paper_addon_legs` tables instead, linked to their parent via `parent_trade_log_id`
(a permanent `trade_log.id`, not `parent_position_id`, which is deleted when the parent closes).
Drought-overlay is unaffected by this -- it genuinely does use `position_source='drought_overlay'` on the
shared tables, safely, because `open_position()`'s existing wl_id-keyed dedup already makes core and
drought mutually exclusive for one node (drought only ever opens while core is flat).

**Real-money blast radius**: everything built is paper-only. No path from any new function reaches
`schwab_client`/`schwab_safety`, and the real live DB (`trading_live.db`) was never migrated this
session -- only copies. The schema lands automatically the next time anything calls `ensure_tables()`
against it (the next daemon restart), and every new config flag defaults to 0/NULL, so this is a genuine
no-op for the current real watchlist until a node is deliberately opted in.

**Not built**: put-hedge (scope exclusion), staged real-order testing (needs organic real signals over
time), the edge-case hardening pass this project's own history suggests will be needed once staged
testing starts producing real data.

### Real-order execution edit plan (drought entry + add-on entry), 2026-08-1x — planned, then implemented (see build-status addendum at the end of this section)

Opus planning pass (research-only, no code written) covering everything needed to move drought-overlay
entry and margin add-on-at-arm from paper-only to real order placement. Full plan saved verbatim below.
Skim-and-reserve confirmed out of scope (alert-only, never places an automated order, needs no real-order
code). All 17 staged paper nodes (ids 167-183) untouched by this plan.

**Requires user sign-off on 6 decisions (D1-D6) before any implementation starts** — see Part 1 below.

**Most consequential finding**: the existing `check_order` double-buy guard is not one gate but effectively
three for an add-on order — the existing-position guard, `_has_open_order` (side-agnostic, and a core
position's own resting protective SELL is *always* present at the exact moment add-on triggers, so this
blocks 100% of real attempts unless specifically fixed), and the signal-window gate. A naive `is_protective`
exemption would also incorrectly inherit the daily-order-cap bypass. The plan's answer: a new, narrow
`is_addon_leg` flag with 5 verified preconditions (margin account, armed core position, no existing leg,
exact share-count match) plus a same-side-only `_has_open_buy_order_for_ticker` replacement for the
side-agnostic check — preserves the real 2026-07-24 double-buy protection while unblocking the one shape
guaranteed to occur on every add-on trigger.

Second key finding: `soxl_ira` is the only account that is both live (`dry_run=False`) and margin-typed —
every other account is either dry-run or cash-typed and structurally cannot margin-borrow or (for drought's
same-day HANDOFF re-entry) legally same-day-rebuy. Both mechanisms are only real-money-coherent on
`soxl_ira` today.

Full plan: `docs/plans/real_order_execution_drought_addon.md`.

**Build-status addendum, 2026-08-1x (later same day)** — Phases 1-9 of the plan implemented end-to-end
(schema, `schwab_safety`/`schwab_client` `is_addon_leg` exemption, real drought entry, real drought HANDOFF,
real add-on entry, real add-on lockstep exit, Accountability Grid rows, 4 new fake_broker scenario test
files, reconciliation extended to `mode='live'`). **Nothing flipped live** — every mechanism still defaults
off (0/NULL) for every real node; no agent step sets `mode='live'`, flips `dry_run=False`, or fires a real
order, per the plan's own Part 12 constraint.

**Paired Opus review (independent-cold + contextual) run and resolved same session.** Both reviews
independently converged on the same root defect — a genuine CRITICAL — and each found one additional
CRITICAL/HIGH the other missed; all confirmed findings were fixed and re-verified with real fake_broker
tests (not just re-read), including two more latent bugs the reviews themselves didn't catch, found only by
building a test that faithfully reproduced the real call-site ordering:

- **[CRITICAL, both reviews independently]** The lockstep leg-exit SELL was structurally unplaceable 100% of
  the time. Every one of the 7 real call sites runs `db.close_position()` (which DELETEs the parent's
  `open_positions` row) BEFORE `close_addon_leg_real_if_open()`, so `check_order`'s SELL-branch preconditions
  — both the original `is_addon_leg` exemption (parent-position-scoped) and the older fail-closed
  no-position guard — always saw no parent position on file and refused the leg's own real exit order. Fixed
  architecturally, not by reordering the 7 call sites: the `is_addon_leg` SELL exemption (and its position-
  size bound) now resolves the open leg via `get_open_addon_leg_by_wl_id(_node_id)` — node-scoped, valid
  whether or not the parent row still exists — instead of `get_open_addon_leg_by_parent(pos['id'])`.
- **[CRITICAL, cold review]** Once the leg's own D3 protective stop was resting, the PARENT core position's
  own ordinary exit placement (a real SL/TIME/TP trigger) was blocked by `_has_open_sell_order` seeing the
  leg's stop as a duplicate resting SELL. Fixed: `_has_open_sell_order` gained a plural `exclude_order_ids`
  param, and `check_order`'s SELL branch now always excludes a known open leg's `sl_order_id` (not just when
  `is_addon_leg=True`) — the parent's own exit must never be blocked by its own leg's separate, wanted order.
- **Two more real bugs in the SAME surface, found only by test-driven verification** (neither review caught
  these — a direct read of the diff doesn't surface them, only exercising the real call order does):
  `check_order`'s SELL-side duplicate-order-*fingerprint* check (a second, independent guard from the
  resting-order check above, keyed on account/ticker/side/quantity within a 60s window) also fired in BOTH
  directions — the leg's own stop placement looked like a fingerprint-dup of the parent's just-placed arm-time
  SELL, and the parent's later real exit looked like a fingerprint-dup of the leg's own recently-placed stop
  — since leg and core shares are identical by construction, every SELL pair for this ticker/account looks
  like a duplicate to a check with no concept of "which leg." Fixed by skipping this fingerprint check for
  SELL whenever the node has ANY open addon leg, not just when the current call is the leg's own placement.
- **[HIGH, contextual review]** No dry-run fill synthesis for the add-on leg — a `dry_run=True` account (the
  plan's own mandatory `brokerage`-account rehearsal prerequisite) never produces a real fill/order-id, so
  the leg was written `entry_status='placed'`/`entry_order_id=None` and could never confirm, permanently
  stranding it; the mirror gap existed on the exit side too. Fixed: both `check_addon_trigger_real` and
  `close_addon_leg_real_if_open` now detect a genuine dry-run no-op (an order call that returned `(None,
  None)` without raising — the only way that happens after `approve_and_record` already ran every real
  guard) and synthesize the fill/close immediately, mirroring `update_dry_run_buys`'s existing convention.
- **[HIGH, cold review]** A leg whose entry fill wasn't confirmed within the ~10s synchronous poll window
  never got its D3 stop placed at all, and was left permanently unprotected. Fixed:
  `check_addon_leg_reconciliation`'s late-fill-confirmation branch now also calls
  `_place_stop_loss_for_addon_leg`.
- **[HIGH, cold review]** `set_addon_leg_exit_order_id` existed but was called nowhere — an exit SELL
  unconfirmed within `close_addon_leg_real_if_open`'s own short poll window had zero further tracking: a
  real order live at the broker with no local record, leg stuck open forever. Fixed: the order id is now
  persisted, and `check_addon_leg_reconciliation` gained a third check that polls and closes it on a later
  cycle.
- **[MEDIUM, cold review]** `_place_stop_loss_for_addon_leg` recorded a fabricated `broker_stop_price` for a
  dry-run leg (order_id None). Fixed to only record it on a genuine placement, mirroring the existing
  `_place_stop_loss_for_position` guard against the same class of false claim.
- **[MEDIUM, contextual review]** The manual "Filled" Slack modal's suggested-share prefill
  (`handle_trail_buy_filled`) wasn't drought-aware, unlike the dispatch fix already applied to
  `handle_entry_price`'s equivalent prefill. Fixed to check the pending row's `position_source` the same way.

**Not fixed this session** (lower severity, explicitly deferred rather than silently dropped): the
orphaned-leg reconciliation alert has no throttle (could repeat every poll in a stuck scenario — same
failure shape as the 2026-08-02 TRAIL-reminder spam, now a narrower window given the fixes above); the
leg's own stop firing independently of the parent is undetected (D3's own plan language already accepts
this as a known, low-probability gap); the full `test_fake_broker_overlay_truth_table_scenario.py` file from
Part 9's list was not written (the four scenario files that were written directly drove and proved the real
production code paths, including finding the bugs above — judged sufficient given the mechanism is still
gated off for every real node).

D1-D6 resolved this session:
- **D1** (exemption shape): adopted the plan's own recommendation — `is_addon_leg`, not `is_protective`.
- **D2** (cash/margin basis): adopted the plan's recommendation — new `schwab_client.get_account_buying_power`
  + `ADDON_BUYING_POWER_HEADROOM_MULT=2.0`, gated to the add-on path only.
- **D3** (add-on leg's own stop): adopted recommendation (b) — anchored to the PARENT's entry_price/stop_pct.
  Required an unplanned extension: the leg's stop SELL collides with `check_order`'s existing resting-SELL
  duplicate guard (the parent's own protective SELL is always already resting), so a matching SELL-side
  `is_addon_leg` exemption (verified against an actual open leg for the parent) was added alongside the
  BUY-side one the plan described.
- **D4** (drought sizing basis): resolved by reading `scripts/stacked_model/drought.py::generate_drought_trades`
  directly — it's pnl_pct-only (sizing-agnostic), so paper's `starting_notional // price` convention carries
  over to real unchanged, matching every other real sizing path (`buy_order_sizing`).
- **D5** (combined core+addon exposure vs. `soxl_ira`'s $3,000 cap): deliberately NOT a new invented number —
  implemented as `core_notional + addon_notional <= account.notional_cap`, reusing the existing cap as a
  combined-exposure ceiling. Conservative by construction (can only ever be MORE restrictive than no check at
  all); flagged for the user to revisit if a real add-on candidate's core leg alone already consumes most of
  the cap.
- **D6** (first staged-test node): explicitly left for Part 12, user-executed only — not a code decision.

New tests: `tests/test_fake_broker_addon_entry_scenario.py` (6), `tests/test_fake_broker_addon_lockstep_exit_scenario.py`
(7), `tests/test_fake_broker_drought_entry_scenario.py` (4), `tests/test_fake_broker_drought_handoff_scenario.py`
(5) — all real production code paths driven directly against `tests/fake_broker.py` (new `set_buying_power`
support added), not mocked. Building these caught real bugs before/after the review round: the add-on entry
happy-path test initially failed because `check_order`'s D5 combined-exposure default correctly rejected an
oversized test fixture (test data fixed, not the guard); `check_addon_leg_reconciliation`'s timeout-abandon
path called `set_addon_leg_entry_filled(entry_status='abandoned')` instead of `close_addon_leg`, leaving the
row's `status` column stuck at `'open'` forever (would have permanently blocked any future add-on for that
parent) — fixed to call `close_addon_leg` like every other abandon/cancel branch. Post-review, two dedicated
tests reproducing the REAL call-site ordering (`test_lockstep_close_succeeds_after_parent_position_already_
deleted`, `test_parents_own_exit_still_works_once_the_leg_has_its_own_resting_stop`) are what actually
surfaced the two additional fingerprint-check bugs above — the reviews' own findings alone would not have
caught them, only driving the real code did.

Full suite after all fixes: 589/590 passed (`tests/test_schwab_automation.py::test_automated_exit_sell_
replace_updates_stale_sl_order_id` fails on `webbrowser.Error: could not locate runnable browser` --
confirmed pre-existing/environmental, reproduces identically in isolation with zero relation to this diff,
independently confirmed by the contextual review agent against the pre-diff tree too). `scripts/live_sim_
harness.py`: 7/7. `signals_invariants.py`: clean against the real live DB. `scripts/check_backlog_cache_
lean.py`: clean.

`signals_invariants.py` also gained a new `check_addon_drought_live_nodes_have_coherent_account_type` check
(any `addon_enabled`/`drought_overlay_enabled` `mode='live'` node must sit on a real margin account).

