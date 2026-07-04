# Session Cache

Handover notes between Claude sessions. Append a new entry on session close. Most recent first.

---

## 2026-07-03 (evening) — v2.x backfill launched, live-parity spot-checks on AGQ, cache-refresh deferred

### What we did
- **Launched the v2.x bias-corrected backfill** (`scripts/run_v2_backfill_sweep.sh`, no-arg full run) — in progress at session end, ~9hr estimated (measured ~84s/ticker × 53 tickers × 7 strategies), safe to run unattended offline (no network calls, confirmed last session).
- **Clarified the version↔z-threshold mapping is incidental, not designed**: `patch_config` in `run_v2_backfill_sweep.sh` never touches `z_score_thresholds` — every version just inherits whatever's in `config.json` at run time. v1.5 being z=2.0-only was because `config.json` happened to have a single z value when that sweep ran, not a per-version rule the script enforces. v2.5/v2.6 will end up identical (both all-z) once both complete.
- **Live-parity spot-checked AGQ mid-run** using `scripts/verify_live_parity.py`'s `compare()`/`kernel_trades()` called ad hoc (no need to hardcode every node into the script's `__main__` block):
  - Live watchlist node (w=10 z=2.0 tp=19 sl=8 hold=133h): v1.5 (pre-fix, cached) 372.8% return/18 trades vs. v2.5 (post-fix) 106.1% return/31 trades — confirms the bias-fix magnitude at the individual-node level, matching last session's aggregate estimate. Live-parity MATCH (31/31 trades) on the corrected kernel.
  - Established that **grid-max (best-of-sweep) comparisons across versions are not apples-to-apples** — each version re-maximizes over ~10k+ combos, so a different node can win each time (e.g. v1.6's best-of-grid alpha 871%→668% same-node-family comparison looked like a modest drop, but the *same exact node* run through the fixed kernel actually dropped 934%→365%, a much bigger haircut — the grid-max shift was partly just re-optimization noise, not the true bias-removal effect). Only same-node comparisons cleanly isolate what the fix actually did.
  - Best AGQ v2.5 candidate found so far (z unrestricted, since the sweep doesn't restrict it): w=10 z=1.0 tp=19 sl=11 hold=140h — 668% alpha, 42 trades, 45.2% win rate, live-parity MATCH (42/42). Materially different from the current live watchlist entry (z=1.0 vs 2.0, SL=11 vs 8, hold=140 vs 133h) — a real candidate for watchlist review once the full sweep confirms it's not a single-ticker fluke.
- **Added `--skip-cache-refresh` to `run_optimization_sweep.py`** — `refresh_dropdown_cache`/`refresh_pivot_cache`/`refresh_cliff_grid_cache` take 2-4 min each; `run_v2_backfill_sweep.sh`'s no-arg path was paying that once per version (7x, ~15-30 min total) for a Streamlit page nobody's watching mid-sweep. Now deferred to one combined refresh after all 7 versions finish. Single-version/ticker-override invocations (sanity checks) still refresh normally — the skip flag is only set for the full 7-version loop (`DEFER_CACHE_REFRESH=1`).
- **Added `scripts/post_sweep_report.py`** — run manually after the backfill completes (no polling; user will run it themselves when the sweep's done). Reports live-parity + fresh-kernel stats for every `watch_list` node (16 nodes across `ZScoreBreakout`/`LimitOrderZScoreBreakout`, not just the 4-ticker live watchlist) plus each one's best-alpha v2.x replacement candidate, written to `docs/post_sweep_report.md`. Not yet run against a completed backfill.

### Key decisions
- Same-node comparison is the only valid way to measure the bias fix's effect; best-of-grid comparisons are confounded by re-optimization over a large combo space and should not be used to judge whether the fix "helped" or "hurt" a given z-threshold or strategy.
- Cache-refresh timing doesn't matter functionally (Top Pivot page falls back to a live query if the cache is stale/missing) — deferring it is a pure time-savings, no correctness tradeoff.

### Next Session
1. Run `.venv/bin/python scripts/post_sweep_report.py` once the v2.x backfill finishes — generates `docs/post_sweep_report.md`.
2. Review the AGQ w=10 z=1.0 tp=19 sl=11 hold=140h candidate (and equivalents for EDC/FAS/HIBL once their v2.x data lands) as potential watchlist swaps — confirm not overfit to a narrow window before promoting.
3. `config.json.bak` is a live runtime artifact from the in-progress sweep (created/restored by its `trap`) — leave it alone until the sweep exits.

---

## 2026-07-03 (later still) — Look-ahead bias fixed, v2.x backfill prepared for offline run

### What we did
- **Fixed the look-ahead bias** discovered last session: one-line change in `backtester.py:24` (`prep_inputs`) — `daily_lookup` now maps each hourly bar to the *previous* day's indicator row (`i - 1`) instead of its own day's row, mirroring `active_signals.compute_buy_signal`'s `today` cutoff. Single fix point shared by every kernel variant (`run_backtest`/`_v17`/`_v18`/`_v19`/`_v110`) and every page that reuses `prep_inputs`. Verified via `scripts/verify_live_parity.py`: plain `ZScoreBreakout` (SOXL) now reports a clean MATCH (30/30 trades) where it used to mismatch on bias alone. Remaining mismatches are the pre-existing documented `LimitOrderZScoreBreakout` intrabar-low-proxy issue, plus one new minor WIN/TWIN result-code labeling discrepancy on the v1.8 case (identical entry/exit price/timing, not yet root-caused, low priority).
- **Versioning decision for the corrected reindex**: new major version namespace v2.x, keeping the *same* version↔strategy mapping as v1.x (v2.4=`TrendFilteredZScore`, v2.5/v2.6=`ZScoreBreakout`, v2.7=`LimitOrderZScoreBreakout`, v2.8=`TrailingExitZScoreBreakout`, v2.9=`TrailingBuyZScoreBreakout`, v2.10=`TrailingBothZScoreBreakout`) — not a flat single "v2.0" tag (considered, rejected — version stays tied to strategy per user). v1.x data is left untouched (`INSERT OR REPLACE` would silently destroy same-tag data, confirmed via `backtest_cache`'s composite PK).
- **Scope**: narrowed the backfill to a curated 53-ticker list — liquid (≥$50k max notional at 1% of 10d avg $ volume, reusing `pages/11_Universe_Scan.py`'s existing formula), non-crypto, index-underlier only (`tickers.stock_underlier IS NULL`, excludes single-stock leveraged ETPs), non-Direxion-dupe (`dupe_direxion IS NULL`). Built and verified against the real `tickers` table schema rather than guessed.
- **New wrapper**: `scripts/run_v2_backfill_sweep.sh`, mirrors `scripts/run_new_tickers_sweep.sh`'s structure (config-patch-per-version + `trap`-protected `config.json` restore). Added an optional ticker-override arg (`./scripts/run_v2_backfill_sweep.sh v2.5 AGQ`) so single-ticker sanity checks still go through the version→strategy `patch_config` guard instead of a hand-rolled command — added specifically because a hand-rolled command is what caused the incident below.
- **Perf**: `dispatch_parallel_grid`'s insert `batch_size` bumped 50→5000 (`run_optimization_sweep.py:315`) — last session's benchmark found the 50-row batch was 28% *slower* than the old per-row inserts (commit frequency, not `executemany()` itself, is the cost). At 5000, recompute-on-crash is ~12s (measured ~399 nodes/sec throughput) and transaction hold time ~7ms (benchmarked) — safe with no concurrent DB writer during the offline run (`active_signals.py` won't run over the long weekend, markets closed).
- **Index audit**: found 3 extra `backtest_cache` indexes existing in the live DB but never declared in `init_idempotent_db` (pure historical accident, no record of who/why). Verified via `EXPLAIN QUERY PLAN` against real GUI page queries: `idx_bc_ticker` and `idx_bc_version_return` are genuinely used (Winners page) and now declared idempotently; `idx_bc_version_ticker_z_return` matched no real query and was dropped (exact `CREATE INDEX` preserved in `docs/backlog.md` to restore if ever needed). Also dropped `idx_bc_version_ticker` — a strict prefix of `idx_bc_version_ticker_strategy`, confirmed via query plan that the planner never chose it; pure insert-time overhead, more costly now given Phase 3's full-mesh insert volume is ~9x Phase 1's coarse.
- **Data prep for offline run**: refreshed all 53 backfill tickers + SPY via `fetch_live_data_smart` (all succeeded, fresh through 2026-07-02). Read-only completeness check across the full `tickers.json` universe (1515 symbols): 1397 fresh, 75 missing, 43 stale — none of the missing/stale ones are in the 53-ticker backfill scope. Confirmed `run_optimization_sweep.py` makes zero network calls (grepped imports) — safe to run fully offline/unattended.
- **Incident**: hand-rolled a hard-coded `run_optimization_sweep.py --version v2.5 --tickers AGQ` command as a manual sanity-check step; the user's own run of that exact command (before `config.json` was patched) wrote 108k rows tagged `v2.5` under the wrong strategy (`LimitOrderZScoreBreakout` instead of `ZScoreBreakout`). A follow-up bash call I issued (intended to patch config + rerun correctly) was rejected by the user but appears to have executed anyway before the rejection registered, writing a second 108k-row batch under the correct strategy but without authorization. Both batches (216k rows total) were identified and deleted after user confirmation. Root cause of the original mismatch: nothing in `run_optimization_sweep.py` enforces the version→strategy mapping — it's purely a shell-script convention (`scripts/run_new_tickers_sweep.sh`'s case statement), and bypassing the script with a manual command has no guard rail. Fixed by adding the ticker-override arg to `scripts/run_v2_backfill_sweep.sh` (above) instead of ever hand-rolling the command again.

### State at close
- Bias fix, index changes, batch_size change, and `scripts/run_v2_backfill_sweep.sh` committed. `config.json` restored to its pre-session committed state (`LimitOrderZScoreBreakout`) — the ad-hoc sanity-check patches were not meant to persist.
- `v2.5/AGQ` cleaned of both erroneous batches — currently empty, ready for a real sanity-check run via the script.
- Full v2.x backfill (all 7 versions, 53 tickers, `./scripts/run_v2_backfill_sweep.sh` with no args) not yet run — user plans to run it during an extended offline (no-internet) period starting now.
- v1.8's WIN/TWIN labeling discrepancy (parity harness) is unresolved and low priority — noted in backlog, not blocking.

### Next
1. Run `./scripts/run_v2_backfill_sweep.sh` (no args) for the full v2.x backfill during the offline period.
2. Once back online: review v2.x results against v1.x for the same tickers/strategies — expect alpha to come down (like the AGQ/EDC/FAS/HIBL replay comparison from last session) but stay positive if the edge is real.
3. Revisit the v1.8 WIN/TWIN labeling discrepancy in `verify_live_parity.py`'s `compare()` output — low priority, not a PnL bug.
4. Decide whether the live watchlist (still v1.5, pre-bias-fix) should be re-pointed at v2.x nodes once backfill completes.
5. `active_signals.py` still needs a restart (carried over from prior sessions) — not urgent, markets closed for the holiday weekend anyway.

---

## 2026-07-03 — ADR 0001 implemented; discovered look-ahead bias in every backtest kernel

### What we did
- **Implemented ADR 0001** (`docs/adr/0001-live-parity-sim-vs-backtest.md`): `active_signals.compute_buy_signal` now takes optional `as_of`/`price_override`/`df_hourly_override`/`df_daily_override` (all default `None` = unchanged live behavior). `scripts/verify_live_parity.py` rewritten — `replay()` now calls the real `active_signals.compute_buy_signal`/`check_sell_condition` through a throwaway per-run SQLite DB (needed since `check_sell_condition` persists `trail_state` via a real DB write), instead of reimplementing its own decision logic. `check_sell_condition` needed no changes (already injectable). `kernel_trades()` extended with `run_backtest_v19`/`run_backtest_v110` branches for v1.9/v1.10 wiring, but v1.9/v1.10 were **not** added to `compare()` — audit found `active_signals.py` has zero live entry logic for the "wait for bounce" trailing-buy state machine (P0 #3, already known), so comparing them would just restate that gap rather than test derived-input correctness. Test-first: harness is ready for when P0 #3 lands.
- **Immediately surfaced a major, unplanned finding**: switching the parity test to call real `compute_buy_signal` made every test case mismatch, including plain `ZScoreBreakout` with no other known gaps. Traced to a genuine look-ahead bias in the kernel, confirmed by direct code trace (not inference): `run_optimization_sweep.py:135-137` builds daily SMA/std including that day's own closing price; `backtester.py:16-30` (`prep_inputs`) maps each hourly bar to its own calendar day's indicator row — so a 9:30am/2:30pm intraday check uses a same-day close that doesn't exist yet at that hour. Structural, not strategy-specific — every strategy in `strategies.py` shares the same `generate_daily_indicators`/`daily_idx` plumbing. Exit side (`check_exit`) unaffected (no sma/std references there). Only `active_signals.py`'s three live functions (`compute_buy_signal`, `_chart_buy`, `_chart_sell`) correctly exclude "today."
- **Mapped full blast radius**: grepped every `generate_daily_indicators`/`resample('D')` call site — one root cause, not several. Every trade-simulating page/script (`pages/2_Node_Inspector.py`, `pages/4_Portfolio.py`, `pages/7_Hurst_Filter.py`, `pages/8_ADF_Filter.py`, `pages/9_Entry_Delay.py`, `hurst_filter_sweep.py`, `open_fill_analysis.py`) reuses `backtester.run_backtest`/`run_backtest_v17` — same kernel, same bug — rather than an independent reimplementation. Other bias categories checked and ruled out: trailing-stop/peak tracking, `_bars_held`, entry/TP/SL fill prices all use only already-realized bar data.
- **Quantified impact** on the live watchlist (AGQ/EDC/FAS/HIBL, all v1.5 ZScoreBreakout w=10) using the new harness directly: alpha stays positive for all four after removing the bias, but was overstated ~3x (EDC, HIBL) to 7x+ (AGQ, FAS) — kernel alpha 308-665% vs corrected-replay alpha 40-202%. Trade counts also diverge substantially (e.g. AGQ 18→31). Also means the sweep's *relative ranking* across all tickers is suspect, not just these four's magnitude.
- **Why didn't the earlier full-codebase review catch this?** `docs/code_review_findings.md` scoped itself to inter-implementation consistency (does `active_signals.py` match `strategies.py`/`backtester.py`); its reference tool (the *old* `verify_live_parity.py`) reimplemented the kernel's same-day-inclusive convention rather than calling live's `today` cutoff, so it was structurally blind to this class of bug. The code is also unremarkable in isolation (idiomatic pandas rolling) — the bug is purely in temporal alignment, which only a "what's actually knowable live" comparison (i.e. this session's ADR 0001 rewrite) could reveal.
- **Backlog additions**: full write-up of the bias (mechanism, blast radius, quantified impact, review-gap explanation) as a new High Priority item in `docs/backlog.md`. Also added a Low-Priority research idea (user's insight): the bias mechanically makes entry *harder* on days with a large move (same-day close pulls SMA down and inflates STD, pushing `lower_band` further away) — matches measured direction (fewer kernel trades, not more). Open question: does a same-day realized-intraday-vol-gated variant (no future info) preserve any of that effect, or does it evaporate once done honestly (mean-reversion caveat: sigma should come back down post-spike if genuinely mean-reverting).
- Updated `docs/design.md`'s description of `scripts/verify_live_parity.py` to reflect what it actually does now.

### State at close
- ADR 0001 code changes done, verified via direct runs (both the generic 4-ticker test set and the 4 real watchlist nodes).
- `active_signals.py` live process deliberately **not** restarted this session — user explicitly said no need, long weekend, no reason to.
- The look-ahead bias fix itself (excluding same-day close in the sweep path, mirroring `compute_buy_signal`) is **not implemented** — flagged as a substantial rerun requiring its own scoping session, not a quick patch.

### Next
1. Decide scope/timing for the look-ahead bias fix + sweep rerun (see backlog High Priority item) — this could reshuffle which tickers/nodes are worth trading at all, not just the current watchlist's four.
2. Consider the same-day realized-vol-gated research idea (backlog, Low Priority/Ideas) as a way to test whether the bias encodes any real signal or is pure noise-selection.
3. Manually test v1.8 fixed_sl/trail_pct round-trip (carried over from prior session, still open, current watchlist is all v1.5 so never exercises that path).
4. Restart `active_signals.py` whenever convenient (not urgent — no code changes since last restart affect it beyond what's already been running, aside from ADR 0001's `compute_buy_signal` signature change, which is backward-compatible with no-arg calls).

---

## 2026-07-03 — P0 fixes reviewed & accepted, numba warmup, log split, ADR 0001 (parity test redesign)

### What we did
- **Reviewed all 5 P0 live-trading fixes** (6216f59) one at a time with the user — TIME exit bar-counting, fixed_sl/trail_pct round-trip, signal-window alert, sell_alerted dedup, app.py config save. No correctness issues found. Added backlog item: manually test fixed_sl/trail_pct round-trip against a real v1.8 position before promoting it live (current watchlist is all v1.5, never exercises that code path; v1.8 had many sweep winners so it's a near-term promotion candidate).
- **Perf retest (Haiku background agent)**: re-ran `scripts/profile_dispatch.py` and a new isolated DB-insert benchmark (old per-row `execute()` vs new batched `executemany()`, 12k synthetic rows). Batching is actually 28% *slower* (more frequent commits: every 50 rows vs old every 100) — not a speed win, kept only for the correctness fix (silent insert failures once `fixed_sl` became the 16th column). Confirmed prior session's "88% result collection overhead" was a measurement artifact (kernel time summed across 8 parallel workers, divided by wall-clock — double-counts); real workload is compute-bound, not IPC/DB-bound.
- **Numba worker warmup**: added `_warmup_worker()` initializer to `run_optimization_sweep.py`'s `ProcessPoolExecutor` — pays each of the 5 kernels' one-time JIT compile cost (~600ms cold, confirmed) at worker startup instead of on a random real grid node mid-sweep.
- **active_signals.py log split**: `logs/active_signals.log` (human-readable, tees console) + `logs/active_signals_verbose.log` (per-ticker `fetch_live_data_smart` chatter, previously discarded entirely). Verified stdout only shows the concise line, verbose chatter never touches console.
- **SIGNAL_POLL_SECS** tightened to 30s in `.env` (untracked) — `fetch_live_data_smart` only hits Yahoo once/ticker/hour regardless of poll frequency (guard clause), so no added API load; makes the P0 #4 window-alert land reliably early instead of relying on luck. Inline override (`SIGNAL_POLL_SECS=90 python active_signals.py`) documented in readme for quieter manual/foreground runs.
- Cleaned up root-dir clutter: `Results (7/8).csv`, `results.csv`, `config.json.bak` (byte-identical to config.json), `.operational_limits.md.swp`, `test_report.py` (throwaway 3-line smoke script).
- **Feature-wrapped and committed** (74ec60f): numba warmup, log split, backlog item, doc updates (design.md, readme.md).
- **Design discussion → ADR 0001** (`docs/adr/0001-live-parity-sim-vs-backtest.md`, new `docs/adr/` dir, lightweight Context/Decision/Consequences format): clarified `active_signals.py` is not a third implementation of trading rules — it delegates to `strategies.py`, adding only DB/Slack orchestration and derived-input computation. Audited that derived-input layer for the same drift risk that caused the P0 #1 bug (two independent computations of `hours_held`, one wrong): found `real_sl_pct`/`trail_pct` selection for fixed_sl strategies has **zero** test coverage (`verify_live_parity.py`'s `kernel_trades()` doesn't even branch on v1.9/v1.10), and `compute_buy_signal`'s "today" date-cutoff + intrabar-low proxy + live-price-fallback are all untested buy-side risks. Decided: extend `verify_live_parity.py` (not a new script) so its `replay()` calls `active_signals.py`'s real `compute_buy_signal`/`check_sell_condition` instead of calling `strategies.py` directly — both entry and exit sides together (a buy-side bug means there's nothing to feed the exit side, so partial coverage was explicitly rejected). Also decided `kernel_trades()` must keep recomputing fresh from `backtester.py` rather than reading `backtest_cache` — the DB only stores aggregates (not a real trade-by-trade ledger) and can go stale relative to current kernel code, the same staleness class P0 #2 just fixed elsewhere.

### State at close
- All P0 fixes accepted; `active_signals.py` still needs a restart to pick up all of this session's changes (never done this session).
- ADR 0001 written but **not yet implemented** — user explicitly wants implementation done in a fresh context/session, not this one.
- ADR file (`docs/adr/0001-live-parity-sim-vs-backtest.md`) is new/untracked as of this close — per `session close` semantics only `conversation_summary.md` gets committed, so this file needs a separate commit next session (or as part of implementing ADR 0001).

### Next
1. Implement ADR 0001: refactor `compute_buy_signal` for injectable `as_of`/`price_override`/`df_hourly_override`/`df_daily_override` (all default `None` = unchanged live behavior); swap `verify_live_parity.py`'s `replay()` to call the real `active_signals.compute_buy_signal`/`check_sell_condition`; add a throwaway SQLite DB per test run (needed for `check_sell_condition`'s internal `update_position_trail_state` write); add v1.8/v1.9/v1.10 test cases to `compare()` and extend `kernel_trades()` with `run_backtest_v19`/`run_backtest_v110` branches (currently absent).
2. Do NOT re-litigate the ADR 0001 design — it was deliberately deferred to a fresh context specifically to implement cleanly, not to redesign.
3. Commit `docs/adr/0001-live-parity-sim-vs-backtest.md` (currently untracked).
4. Restart `active_signals.py` to pick up this session's changes (numba warmup doesn't apply to it, but log split + poll cadence do).
5. Manually test v1.8 fixed_sl/trail_pct round-trip (backlog item) before promoting it live.

---

## 2026-07-01 — Multi-watchlist, live/research modes, MULL corporate action investigation

### What we did

- **Multi-watchlist support** (committed `6d4ab1c`): `watchlists` table with named profiles; one is_active drives signal loop. `watch_list` gains `watchlist_id` + `mode` (live/research). Migration runs automatically on next `ensure_tables()` call — existing 12 nodes moved to 'main'. Active signals suppresses Slack for `mode='research'` nodes (console-only).
- **Winners page**: sidebar watchlist picker, create/delete/set-active controls, Mode selectbox in data editor.
- **Portfolio page**: sidebar watchlist picker.
- **Watchlist backup**: `cache/watchlist_backup_20260701.json` — 12-node snapshot before trimming.
- **MULL corporate action investigation**: MULL had a 25:1 forward split on 2026-06-26. yfinance daily and cache are correctly split-adjusted (÷25); the "97% drop" on Google Finance is Google showing unadjusted prices. yfinance hourly has a bug where it returns 25x higher prices for pre-split dates vs daily — cache is on the correct scale and internally consistent. Backtesting results valid.

### Current State

- Watchlist: 12 nodes in 'main' (all mode=live). User was trimming to 7 — not yet done.
- run_optimization_sweep.py has uncommitted changes from prior session (Phase 2.5, island check fix).
- `docs/session_cache_addendum.md` still untracked — contains prior session notes, can be deleted or committed.

### Next Session

1. Trim watchlist to 7 nodes (use Winners page → uncheck Watch, or sidebar picker)
2. Decide between AGQ v1.5 (w=10 z=2.0 TP=19 SL=8 hold=133h) vs v1.6 (w=20 z=1.0 TP=28 SL=11 hold=140h)
3. Mark some nodes as 'research' vs 'live' once list is trimmed
4. Commit run_optimization_sweep.py (Phase 2.5 + island check fix)
5. Fix Phase 2.5 to sweep all 3 island centers × all (w,z) combos (backlog item added)
6. Review MULL/VRTL/NBIZ single-stock nodes before trading

---

## 2026-07-01 (addendum) — v1.6 Sweep Execution + Cliff Analysis

### What we did

- **v1.6 sweep completed overnight**: 358 tickers coarse, 30 island mesh (top 25 index + 5 other), cliff check at checkpoint 2. Only 1 ticker (WULX) survived cliff check for full mesh. No index-underlier tickers passed. WULX full mesh done, heatmap at `logs/topology_WULX_ZScoreBreakout.png`.
- **SMST full mesh**: Launched separately (`run_smst_full.py`, PID 102868, log: `logs/smst_full.log`). SMST had best_alpha +2188% but worst_neighbor -97.7% (cliff) — full mesh running to see the complete topology.
- **v1.5 cliff check**: Ran against full 1-30 data. 25/340 tickers safe. Top safe: VRTL (+721%), WULX (+515%), CIFG (+458%), GEVX (+284%), CRDU (+261%). Watchlist tickers (AGQ/EDC/FAS/HIBL) not in safe list.
- **Cliff check design note**: `CLIFF_RADIUS=2` = ±2 integer steps in TP and SL = 5×5=25-node box. `AND trades > 0` excludes NO_TRADES nodes from cliff detection — may miss edges where signal disappears.
- **Sweep run registry**: Discussed — `sweep_runs` DB table to snapshot config + notes per run. Not yet built.
- **Backlog updated**: v1.6 marked done, sweep registry + cliff improvements + Universe Scan pending items added.

### Key Findings

- Most high-alpha tickers are single-node spikes, not plateaus — only 7% pass cliff check on v1.5 full data
- WULX and VRTL are the strongest cliff-safe candidates from v1.5
- Cliff check brutality is a feature: protects against overfitting to lucky single nodes
- v1.6 coarse+island is fast enough (2.5h for 358 tickers at 10 workers) to run regularly

### Next Session

1. Check SMST full mesh result (`tail logs/smst_full.log`) — see if its topology is wide or spiky
2. Run full mesh on v1.5 cliff-safe tickers (VRTL, CIFG, GEVX, CRDU, HUTG) at z=[1.0, 1.5]
3. Build sweep run registry (`sweep_runs` DB table + sweep engine integration)
4. Update Universe Scan safety score to worst-neighbor min + color coding
5. Commit all session changes

---

## 2026-07-01 — v1.6 Sweep Design + Universe Scan Page

### What we did

- **Feature wrap**: committed Open Positions (signal price + drift %), Portfolio research nodes, Node Inspector Hurst opt-in, Top Pivot z-threshold expansion, sweep cache fix.
- **v1.6 sweep design**: Validated coarse-odd approach against v1.5 full data. Step-3 grid [3,6,...,30] recovers true peaks with Δ=0% in 10/10 test cases at ~28% of full node count.
- **Three-phase sweep architecture** designed:
  - **Phase 1 — Coarse** (all 358 tickers): TP/SL [3,6,...,30], all hold/window/z → 2,864,000 nodes
  - **Phase 2 — Island mesh** (top 25 index + top 5 non-index by coarse alpha): 3 islands ± 4 radius per ticker → 343,200 net new nodes
  - **Phase 3 — Full mesh** (top 5 index + top 5 non-index): complete 1-30 TP/SL grid → 525,600 net new nodes
  - **Total: 3,732,800 nodes** — fits in 4,320,000 capacity (200/s × 6h) with 14% headroom
- **Alpha gate**: rank-based — if not in top 50 by coarse alpha, skip island mesh. Top 5+5 for full mesh.
- **Cliff detection gate** (CRITICAL): Phase 3 selection is NOT just top-5 by coarse alpha. After Phase 2, compute worst-neighbor alpha from island mesh data. Filter out tickers where worst_neighbor < 0 (cliff risk). Top 5 index + 5 non-index selected from cliff-free survivors only.
- **Universe Scan page** (`pages/11_Universe_Scan.py`): built — shows coarse scan results for all tickers, liquidity (max notional = avg_vol × price × 1%), underlier type (index/stock), flags (LOW_LIQ / TOP_IDX / TOP_STK / REFINE), safety score (positive-neighbor count), full universe table with toggle.
- **config.json → v1.6**: 358 tickers (full v1.5 universe), z=[1.0, 1.5], TP/SL coarse [3,6,...,30], max_workers=10, max_generations=0.
- **run_optimization_sweep.py**: max_workers now reads from config instead of hardcoded 6.

### Key Decisions

- Coarse step-3 (not step-2/odds) — 11% of nodes vs 25%, same recovery quality
- 25 index + 5 non-index for island mesh; 5+5 for full mesh — fits budget
- Phase 3 requires cliff check from Phase 2 — sweep must checkpoint between phases, not run linearly
- z=2.0 NOT in v1.6 config — already fully swept in v1.5

### Next Session

1. **Build multi-phase sweep logic** in `run_optimization_sweep.py`:
   - Phase 1: coarse scan, save to DB as v1.6
   - Checkpoint: rank by coarse alpha, identify top 30 (25 index + 5 non-index) from `tickers` table underlier classification
   - Phase 2: island mesh for top 30
   - Checkpoint: compute worst-neighbor safety score, filter cliff tickers, pick top 10 survivors
   - Phase 3: full mesh for top 10
2. **Update Universe Scan page** safety score to use worst-neighbor (min neighbor alpha) rather than count, and color-code (green plateau / red cliff)
3. **Fine mesh trigger button** in Universe Scan page (top 25 only)
4. Commit `pages/11_Universe_Scan.py`, `config.json`, `run_optimization_sweep.py`

---

## 2026-07-01 — z=1.0/1.5 Sweep, Portfolio Rework, Coarse Grid Validation

### What we did

- **Hurst/ADF cleanup**: Removed pre-warm from Node Inspector; Hurst analysis now opt-in behind checkbox. Removed dead Hurst/ADF columns from Portfolio (were always NaN). Backlog updated.
- **Backlog cleanup**: Removed completed items, removed "full leveraged universe" item (already have 300+), consolidated Hurst/ADF into single done item.
- **Open Positions page**: Added `Signal $` and `Drift %` columns (entry price vs signal price).
- **v1.5.1 sweep (z=1.0/1.5, watchlist tickers)**: Ran full fine grid (TP/SL 1-30, w=10/20) for AGQ/EDC/FAS/HIBL. Fixed cache check bug (was counting all z-thresholds together). Renamed z=1.0/1.5 rows to v1.5.1. Accidentally deleted w=30 rows — lesson: fix the query, not the data. Cache check now also scopes by window.
- **Portfolio rework**: Supports mix-and-match node selection. Watchlist toggle + Research expander (version picker, filters, top nodes table, multiselect). Nodes from any version can be combined in one Gantt view.
- **Exit window analysis**: Exits can fire at any bar close, not just signal windows. Quick model showed restricted exits costs 0-22% compounded return. Not worth operationalizing.
- **Coarse grid validation (v1.5)**: Even-number grid (2,4,6,...,30) finds islands reliably. Every-3 missed EDC entirely (TP=17 sits between 15 and 18). Decision: even numbers for discovery, full fine grid for confirmed winners only.
- **Versioning convention**: 1.x = strategy family, 1.x.y = run variation. v1.5 = z=2.0 fine grid. v1.5.1 = z=1.0/1.5 fine grid (watchlist 4).
- **Z=1.0/1.5 findings**: HIBL z=1.0 w=20 TP=8 SL=6 strong (75 trades, 60% win rate, 629% alpha). AGQ z=1.0 dangerous (sustained downtrend). Cherry-picking watchlist tickers for z=1.0 isn't fair — need to sweep broader universe.
- **Top Pivot fix**: Added z=1.0/1.5 to `Z_THRESHOLDS`. Requires Streamlit cache clear after DB refresh.

### Key Decisions

- Even-number coarse grid is the standard for discovery sweeps going forward
- w=30 in coarse pass only; w=10/20 for fine grid
- Non-single-stock universe ≈ 78 tickers (filter criteria to rediscover next session)
- Capital deployment strategy: explore z=1.0/1.5 on broader universe when z=2.0 signals are quiet

### Next Session

1. Rediscover the ~78-ticker filter criteria
2. Set up even-number coarse grid sweep for non-single-stock universe at z=1.0/1.5
3. Validate coarse vs fine for v1.5.1 once sweep completes
4. Check sweep status (was still running: HIBL z=1.5 in progress)
5. Refresh Streamlit cache to see v1.5.1 in UI

---

## 2026-06-30 (session 3) — Cache/Index Sweep, DB Pruning, Top Pivot Navigation

### What we did

- **Top Pivot overhaul**: Replaced selectbox + buttons with HTML table (`st.html`) where each backtest cell (w=10/20 z=2.0) is a clickable link directly to Node Inspector. Links encode full node params (ticker, version, window, z, TP, SL, hold) via URL query params. Node Inspector reads `st.query_params` as fallback when session state is empty.
- **`_load_dropdown_opts` cache fix**: Was missing `@st.cache_data` — hit DB on every rerun. Added `ttl=86400`.
- **Double decorator bug**: Node Inspector had two stacked `@st.cache_data` decorators on `_load_dropdown_opts` and `run_cached_backtest`. Fixed to single decorator each.
- **Cache sweep**: Added `@st.cache_data` to 9 uncached DB/file functions across 6 pages (Spatial Topology `load_dropdown_options`, Winners `load_versions`/`load_ticker_strategy_options`/`load_results`, Portfolio `load_watchlist`/`load_hourly`, Sweep Status `load_versions`/`get_data_date`, Open Positions `load_positions`).
- **TTL sweep**: Changed all version-keyed backtest data functions to `ttl=86400` across all pages. Price/computation functions left at shorter TTLs.
- **DB indexes**: Added `(version, ticker)`, `(version, window)`, `(version, ticker, strategy)` indexes on `backtest_cache`. Applied to live DB.
- **`load_best_nodes` KV persistence**: Added `refresh_best_nodes_cache()` to `db_cache.py`. `load_best_nodes` now checks KV store first — survives Streamlit restarts without re-running window function query. Populated for all versions.
- **DB pruning**: Deleted z=2.5, z=3.0, w=30 rows + all of v1.2/v1.3/v1.4. 76M → 13M rows, 21GB → 3.5GB after vacuum. Archive saved at `cache/trading_universe_archive_20260630.db`.
- **WINDOWS/Z_THRESHOLDS**: Updated to `[10, 20]` and `[2.0]` in Top Pivot constants and pivot query.
- **Strategy discussion**: Decided `LimitOrderZScoreBreakout` will be a separate class (not inherited from ZScoreBreakout) — entry price is fundamental to P&L chain. Will share band/signal calculation via utility. Gets its own v1.6 sweep.

### Key Findings

- `on_select="rerun"` for `st.dataframe` does not work in this environment — clicking cells does nothing. Workaround: HTML table with `<a href>` links using `target="_top"`.
- `@st.cache_data` is in-memory only — clears on Streamlit restart. Version-keyed backtest data should also be persisted in KV store (SQLite) for restart resilience.
- Pruning z=2.5/3.0 and w=30 removed 83% of rows — confirmed those params almost never yield good signals.

### Current State

- DB: v1.5 only, 13M rows, 3.5GB. Archive at `cache/trading_universe_archive_20260630.db`.
- Watchlist: AGQ w=10 TP=19 SL=8, EDC w=10 TP=17 SL=17, FAS w=10 TP=25 SL=10, HIBL w=10 TP=29 SL=21

### Next Session

1. Design and implement `LimitOrderZScoreBreakout` strategy + v1.6 sweep
2. Revisit Top Pivot sort/filter (HTML table has JS sort on column headers; may want more)
3. Apply DB indexes when app is idle (was locked during session)

---

## 2026-06-30 (session 2) — Morning Report, Sweep Fixes, Open Positions Page

### What we did

- **Node Inspector commit**: Watchlist table now shows Return%, Alpha%, Asset B&H, SPY B&H, B&H Mult, Trades, Win% inline. Height auto-sizes. (`pages/2_Node_Inspector.py`)
- **Open Positions page** (`pages/10_Open_Positions.py`): Reads `open_positions` DB table, fetches current price via yfinance, shows entry/current price, unrealized P&L%, TP/SL prices, hours held/remaining.
- **Sweep skip optimization**: `run_master_evolutionary_suite` now does a single COUNT query before CSV reads to skip fully-cached tickers — eliminated ~15 min waste at sweep start.
- **max_workers**: Changed 6→10→6 (10 caused no improvement due to SQLite WAL issue; back to 6 to leave cores for active_signals).
- **WAL incident**: Stale process (PID 256511, python3 -c inline SQLite query from prior session) held a read lock for 592 hours, blocking WAL autocheckpoint. WAL grew to 30GB. Fixed by killing stale process + db_cache.py (also stuck for 3h). WAL flushed on next connection. Root cause: correlated subquery ran without index before idx_bc_version_ticker_z_return existed.
- **config.json**: Stripped ~200 completed tickers. Only incomplete tickers remain (~83 → now fewer after sweep continued).
- **Data collector cron**: Moved from 8 AM to 6:30 AM so daily bars are fresh before 7 AM morning report.
- **Morning report — daily 7 AM**: `send_startup_report` now fires daily at 7 AM ET via poll loop (tracks `last_morning_report_date`). No restart needed.
- **Morning report — overnight change**: Shows `now $X.XX (+Y.Y% O/N)  close $Z.ZZ` using `yfinance history(prepost=True)` for current price and `df_daily.iloc[-1]` for prev close (no date filter — picks up most recent completed session).
- **Morning report — data date**: Shows `data MM/DD` = last daily bar date used for bands so you can confirm freshness after 6:30 AM cron.
- **BUY message**: Added `max $Xk / Y shares @ 1% vol` from `avg_vol_10d` in tickers table (liquidity ceiling).

### Key Findings

- `fast_info.last_price` = regular session close only — misses pre/post-market. Use `history(prepost=True)` instead.
- `fast_info.previous_close` is inconsistent post-market (sometimes returns today's close, sometimes yesterday's). Use `df_daily.iloc[-1]` instead.
- `df_daily[df_daily.index < today]` excludes today's bar because resample index = midnight. Use `df_daily.iloc[-1]` for prev_close (no filter).
- WAL autocheckpoint works correctly but is blocked by any long-lived reader. Kill stale python3 -c processes after sessions.
- SQLite correlated subquery without index is O(n²) — will hang indefinitely on 20M+ row table. Index exists now (`idx_bc_version_ticker_z_return`) but needs a covering index on `(version, ticker, strategy_return DESC)` for that specific pattern.

### Current State

- Watchlist: AGQ, EDC, FAS, HIBL — no open positions
- FAS at +2.2% from trigger (🔶) — set alarm for 10:28 and 15:28 tomorrow
- Sweep running on remaining ~83 tickers; active_signals.py running (PID 269226)
- Morning report will auto-fire at 7 AM ET

### Next Session

1. Verify 7 AM morning report fires correctly with fresh bands (check `data` date = 06/30 after 6:30 AM cron)
2. Add covering index `(version, ticker, strategy_return DESC)` to prevent O(n²) query hangs
3. Run db_cache.py after sweep completes
4. Check sweep completion status

---

## 2026-06-30 (addendum) — Live Execution Design, Slack Redesign, Watchlist Trim

### What we did

- **Open-fill analysis**: Ran across all 17 watchlist tickers. Open-fill (9:30 bar open as entry) is consistently worse than 10:30 close — selection bias (bars only selected when close <= lower_band). Conclusion: market order at 10:30 matches backtest entry best.
- **Real-time price**: `compute_buy_signal` now uses `yfinance fast_info.last_price` instead of last cached hourly close. Fallback to cache on failure.
- **Signal time-gating**: Buy and sell signals only evaluated in windows 10:25–10:40 AM and 15:25–15:40 PM ET, matching backtest `target_hours=(9,14)` (9:30 bar close at 10:30, 14:30 bar close at 15:30). Outside windows, loop idles.
- **Execution workflow documented** (`docs/operational_limits.md`): Stage limit order pre-market at absurd price, edit to market at 10:30/15:30 when Slack fires. No overnight limit orders at lower_band (open-fill analysis showed this is worse).
- **Startup report redesigned**: Block Kit with 🔶/🟡/⚪ proximity emoji, sorted by % to trigger, open positions section with P&L, reconfirm reminder for hot tickers (< 5% away).
- **BUY message redesigned**: Two-line action card — `🟢 FAS — BUY — Market — $148.12 — 337 shares (~$50k)` / `🔴 FAS — SELL ALL — Stop Loss — $128.12 (-11% from trigger)`. Stop loss at lower_band × (1 - (SL% + 1%)) — 1% buffer over backtest SL for intraday noise protection. Intrabar false trigger rate confirmed very low (0.0–0.3% of bars).
- **SELL messages redesigned**: TP → cancel stop loss, sell market. SL → check account, should have auto-filled. TIME → change stop loss to market close order.
- **Portfolio page**: Ticker multiselect to toggle tickers on/off, full watchlist expander, TQQQ normalized price overlay alongside SPY, Hurst/ADF computation commented out for speed.
- **pages/9_Entry_Delay.py**: Entry delay analysis across all watchlist tickers.
- **open_fill_analysis.py**: Standalone open-fill vs backtest return script.
- **Watchlist trimmed** to AGQ, EDC, FAS, HIBL (top 4 by alpha/island quality). Others remain in DB.
- **Alternative trading windows** added to backlog.

### Key Findings

- Open-fill is always worse than 10:30 close — not a bug, just selection bias
- Intrabar SL false trigger rate: 0.0–0.3% across all 4 tickers — tight Schwab stop is fine
- Portfolio peak concurrent positions: up to 14 (all correlated — same macro event). Trimmed to 4 tickers to manage.
- 🔶 in morning report = set phone alarm for 10:28 and 15:28

### Current State

- Watchlist: AGQ w=10 TP=19 SL=8, EDC w=10 TP=17 SL=17, FAS w=10 TP=25 SL=10, HIBL w=10 TP=29 SL=21
- FAS at +2.9% from trigger — 🔶 tomorrow morning
- No open positions

### Next Session

1. Build Streamlit open positions page
2. Commit `pages/2_Node_Inspector.py` changes (not staged this session)
3. Check sweep status
## 2026-06-29 (addendum 2) — Watchlist Expansion, DB Indexes, Entry Delay Analysis

### What we did

- **docs/research.md**: Created. Captured Hurst/ADF filter findings and sweep parameter conclusions (was left in session_cache by predecessor).
- **Watchlist expanded to 17 tickers**: Added KORU, HIBL, SOXL, TQQQ, NAIL (top 5 by return), then corrected via Top Pivot download — KORU/SOXL/NAIL don't beat B&H (B&H mult < 1.0x, filtered by Top Pivot). Left them on watchlist anyway (user curious about signals). Added URTY, DUSL, TNA, DRN, OILU, CURE, MIDU from Top Pivot list (user removed TQQQ, GDXU, JNUG from download).
- **Watchlist versions**: Updated all v1.4 → v1.5.
- **DB index added**: `idx_bc_version_ticker_z_return` on `(version, ticker, z_score_threshold, strategy_return DESC)`. Took 220s to build on 45M rows.
- **PK fix in Node Inspector**: Watchlist metrics query now includes `strategy='ZScoreBreakout'` to hit PK instead of falling back to `idx_bc_ticker` scan.
- **Node Inspector watchlist table**: Now shows Return%, Alpha%, Asset B&H, SPY B&H, B&H Mult, Trades, Win% inline. Height auto-sizes to row count. Metrics cached via `load_watchlist_metrics()`.
- **active_signals.py startup report**: `send_startup_report()` fires at startup, posts Slack table with current price, buy trigger (lower_band), z-score, TP price, SL price per ticker.
- **pages/9_Entry_Delay.py**: New page. For each watchlist node, runs backtest then replays each trade with entry delayed 1-4 hours. Shows compounded return and missed trade count per delay. Finding: delayed entry is consistently terrible — strategy selects against fast mean-reversions.
- **Limit order analysis (AGQ)**: 9 of 18 AGQ trades fire at 9:30 bar. Open fill (using 9:30 open as entry) gives 311% vs 372% backtest — about 62% lower compounded return. Limit order fills between open and close (at lower_band), so real performance is between 311-372%. Interesting stat to run across all tickers.
- **backlog**: Added v1.6 open-price entry model. Removed FAS watchlist removal item (user decided to keep). SPY/VIX filter added as next research direction.
- **docs/design.md**: Updated with pages 7/8, shared hurst.py module, max_workers=6, sweep auto-cache, cron.

### Key Findings

- Delayed entry is bad: selecting for trades that didn't bounce fast = selecting losers
- Earlier entry (limit order at open) is better than waiting for 10:30 close
- But open fill still ~17% worse than backtested return for AGQ (compounded)
- Limit order at lower_band placed night before is valid execution approach
- 9:30 bar open is typically 1-3% above the 10:30 close (entry price in backtest)
- Most 9:30 trades gap through the limit price — fill at open, not exactly at lower_band

### Next Session

1. Run open-fill analysis across all 17 watchlist tickers (AGQ showed -62% compounded vs backtest — is this typical?)
2. Commit pending changes (active_signals.py, Node Inspector, pages/9, docs/)
3. Check sweep status — 30 U-Z tickers remaining
4. Run db_cache.py after sweep completes

---

## 2026-06-29 (addendum) — Hurst/ADF Research, Node Inspector Perf, Sweep Config

### What we did

- **Hurst bug fixed**: `np.var` → `np.mean(x**2)` in `_hurst_vectorized`. Was stripping trend component, making everything look mean-reverting. Values now correctly go above 0.5 during trending periods.
- **hurst.py**: Extracted `_hurst_vectorized` + `ROLLING_WINDOW=200` to shared module. Node Inspector imports from there. `active_signals.py` ADF window also updated to 200.
- **test_hurst.py**: Synthetic fBm sanity checks — random walk ≈0.5, trending >0.55, mean-reverting <0.45. All pass.
- **Node Inspector perf**: Fixed 7-13s load time caused by uncached `DISTINCT ticker` query on 45M rows. Added `_load_dropdown_opts()` with `@st.cache_data`. Added `@st.cache_data` to `run_cached_backtest`. Pre-warm limited to watchlist-only (was 314 tickers). Added `get_kv` import. Suppressed trades now show grey vrects.
- **Top Pivot**: Added "Exclude index" toggle (98 tickers tagged in `index_underlier`).
- **db_cache.py**: `DB_PATH` now uses `__file__`-relative path so cron job works from any directory.
- **run_optimization_sweep.py**: Auto-runs `refresh_dropdown_cache()` + `refresh_pivot_cache()` on sweep completion. `max_workers=6`. Cron job added at 4:15am daily.
- **config.json**: Expanded to 357 tickers for next sweep. Sweep ran to ~328/357 before kill (stopped at NFLU). Remaining: USAX→ZSL (30 tickers, all U-Z).
- **pages/7_Hurst_Filter.py**: Sweep Hurst filter (MR vs MO) across all qualifying nodes. Result: MO (momentum, H≥cutoff) helps 43/87 nodes vs MR 31/87. Weak signal.
- **pages/8_ADF_Filter.py**: Same for ADF p-value. Non-stationary filter (p≥cutoff) showed benefit on AGQ, DPST, EDC, FAS but not LABU. Fixed-cutoff test on FAS showed cherry-picking — all fixed cutoffs worse than base.

### Key Findings
- Hurst/ADF as entry filters: not actionable. Lag problem — can't detect regime change in time. At-entry regime is backward-looking and doesn't predict trade outcome reliably.
- Slight lean toward momentum entries (H≥0.5, non-stationary) but sample sizes too small (18-24 trades) to be confident.
- w10 z3.0 maxes at 4 trades over 2 years — too rare to trade. z2.0 is the real edge.
- w30 has no qualifying nodes for non-single-stock — likely trend drift kills mean reversion at that timescale.
- SVXY (inverse VIX): 93% return, 3.6× B&H, 18 trades at w20 z2.5 — marginal, too volatile.

### Current State
- Watchlist: AGQ, DPST, EDC, FAS, LABU (all v1.4 — needs update to v1.5)
- active_signals.py running
- 30 tickers remaining in v1.5 sweep (U-Z)
- Uncommitted changes staged, commit pending

### Next Session
1. Commit pending changes
2. Update watchlist versions v1.4 → v1.5
3. Run remaining 30 tickers: `.venv/bin/python3 run_optimization_sweep.py 2>&1 | tee -a logs/sweep_v15_full.log`
4. Run `db_cache.py` after sweep
5. SPY trend / VIX level as entry filter — next research direction

---

## 2026-06-29 — Portfolio Page, Pivot Cache, Signal Improvements

### What we did

- **Portfolio page** (`pages/4_Portfolio.py`): Gantt chart of all watchlist node trades on a shared x-axis with SPY price overlay and concurrent-positions step chart. Hurst + ADF sliders filter trades by regime at entry time — lets you see if regime filtering improves per-trade avg return. Summary metrics bar (trades, win rate, avg return, avg win, avg loss, avg hold, max concurrent) + per-node table with unfiltered vs filtered columns side-by-side.
- **Pivot cache** (`db_cache.py` `refresh_pivot_cache()`): Pre-aggregates Top Pivot data per (ticker, window, z, trades) into `kv_cache`. Page load now hits a key lookup instead of scanning 49M rows. Fallback to SQL if cache miss. Run `.venv/bin/python3 db_cache.py` after each sweep to refresh both dropdown and pivot caches.
- **Top Pivot cache integration** (`pages/0_Top_Pivot.py`): `load_pivot()` checks `kv_cache` first; `min_trades` filter applied in pandas on cached cell data.
- **z_score_threshold bug fixed** (`active_signals.py` line 311): `compute_buy_signal()` was hardcoding `2.0` in strategy constructor and `lower_band` calculation. Now uses `node['z_score_threshold']`.
- **Hurst + ADF in BUY signal**: At signal time, pulls latest Hurst from `hurst_cache` and computes ADF fresh on last 420 hourly bars. Both shown in console print and Slack `_fields_block`.
- **Removed `docs/handover.md`**: Was stale and duplicating DB state. `go` now reads last ~60 lines of `session_cache.md`. `session close` appends here only.
- **venv fix**: All python commands need `.venv/bin/python3`, not bare `python3`.

### Key Decisions

- Portfolio Hurst/ADF sliders show regime filter effect vs baseline — useful for deciding whether to use regime filter in live trading
- ADF computed fresh at signal time (fast enough for one ticker); no dedicated cache needed yet
- Hurst/ADF screener columns: will hook into data download pipeline rather than single scalar (regime-dependent); pending design decision on aggregation
- Pivot cache stores per-(ticker, window, z, trades) granularity so any min_trades value can be filtered in Python

### Current State

- Watch list: AGQ w=10 TP=19 SL=8 hold=133h, DPST w=10 TP=21 SL=12 hold=126h, EDC w=10 TP=17 SL=17 hold=112h, FAS w=10 TP=25 SL=10 hold=133h, LABU w=20 TP=21 SL=18 hold=84h
- Sweep v1.5: z=2.0 done, z=2.5 done, z=3.0 ~50% (33 tickers missing)
- No open positions. Ready to go live tomorrow with `active_signals.py`.

### Next Session

1. Start live signal monitoring: `.venv/bin/python3 active_signals.py`
2. Restart z=3.0 sweep: `.venv/bin/python3 run_optimization_sweep.py 2>&1 | tee -a logs/sweep_v15_full.log`
3. Run `.venv/bin/python3 db_cache.py` after sweep completes
4. Hurst/ADF screener column design: hook into data download, decide on aggregation approach
5. Position sizing in Slack BUY signal (data already in `tickers` table)

---

## 2026-06-28 (session 3) — Performance, Top Pivot, Config Cleanup, Hurst 60d

### What we did

- **Hurst vectorized**: Replaced Python loop in `rolling_hurst` with `sliding_window_view` + batch `lstsq`. Now computes every bar (step=1) instead of every 12. Off-by-one fixed (`[:-1]` on windows). ~10-50× faster on first load.
- **Hurst caching**: `hurst_cache` DB table stores full rolling series for watchlist tickers, persists across sessions. Non-watchlist tickers use `st.session_state` only. Staleness check vs CSV max timestamp. Pre-warm on Node Inspector load: watchlist from DB, >200% return tickers from session_state.
- **Hurst window changed to 60d** (was 30d). `ROLLING_WINDOW = 60 * 7 = 420`. Old 30d cached rows cleared and recomputed for all 5 watchlist tickers.
- **Node Inspector `@st.fragment`**: Slider + chart + metrics wrapped in fragment. Only the fragment reruns on slider drag — no data loading, backtest, or Hurst recomputation. H_at_entry pre-computed outside fragment (depends only on h_series, not cutoff).
- **Hurst filter finding**: Only 1 watchlist ticker showed improvement from the filter. Hurst may be better as a screener (ticker quality gate) than a per-trade filter. 60d window recomputed to recheck.
- **Winners perf**: Added `idx_bc_version_return` index on `(version, strategy_return)`. WAL mode enabled. Dropdown options moved to `kv_cache` DB table (persistent across server restarts). `load_results` TTL kept at 60s (filtered differently each call).
- **Spatial Topology perf**: `load_dropdown_options` replaced — now reads from `kv_cache` instead of `SELECT DISTINCT version, ticker, strategy FROM backtest_cache` (full 48M row scan). `load_slice` TTL raised to 3600s.
- **`db_cache.py`**: New module with `get_kv`/`set_kv` (JSON key-value in `kv_cache` table) and `refresh_dropdown_cache()`. Run as script to populate. Covers versions, tickers, strategies for Winners + Topology. Cache populated.
- **Top Pivot page** (`0_Top_Pivot.py`): New page. Pivot of best return per (ticker, window=10/20/30, z=2.0/2.5/3.0) = 9 cells + max + alpha + bh_mult. SQL-side GROUP BY (not Python). Filters: min trades, min return, min alpha, min B&H mult, exclude single-stock toggle. Editable "Underlier" text column — type stock symbol to mark as single-stock, saves to `tickers.stock_underlier`. Row selection → "View in Winners" (pre-filters ticker) or "Open in Node Inspector".
- **Single-stock filtering**: `tickers.stock_underlier` column used as quality gate. 233 of 357 original config tickers flagged as single-stock (mostly single-stock leveraged ETFs like NVDL, TSLL, AMDL etc).
- **config.json rebuilt**: 71 tickers ordered: watchlist (5) → top returners non-single-stock (42) → everything else non-inverse non-single-stock (24). Inverse/bear ETFs removed (34 flagged in `tickers.inverse=1`). VOO removed. Crypto (BITU, BITX, BTCL, ETHU, ETHT) kept — Bitcoin did well in sweep.
- **Backlog updated**: Two-phase UX rethink added (discovery vs optimization, node-centric vs ticker-centric).

### Key Decisions
- Single-stock leveraged ETFs excluded from sweep for now — optimize non-single-stock first.
- Inverse/bear ETFs excluded — strategy is long-only mean reversion.
- Crypto kept — historical results were reasonable.
- Hurst as per-trade filter has limited value for most watchlist tickers. More useful as screener column.
- `kv_cache` pattern for expensive dropdown queries; `hurst_cache` pattern for expensive per-ticker time series.

### Pending
- Hurst filter reassessment with 60d window — re-check which watchlist ticker improved
- FAS watchlist removal still pending decision
- LABU SL=9 vs SL=18 — wait for more z=2.5/3.0 data
- `refresh_dropdown_cache()` not yet scheduled (manual for now)

---

## 2026-06-28 (session 2) — DB PK Fix, Node Inspector Rebuild, Winners Fixes

### What we did

- **backtest_cache PK bug found and fixed**: `z_score_threshold` was missing from the PRIMARY KEY. `INSERT OR REPLACE` for z=2.5 silently overwrote z=2.0 rows (same PK). Sweep was generating 162k total nodes but finding only 54k cached (1 z-threshold worth) every restart. Root cause: table predates the column — `CREATE TABLE IF NOT EXISTS` never re-ran to update PK; `ALTER TABLE ADD COLUMN` can't change PKs.
- **DB migration**: Killed sweep + Streamlit. Rebuilt `backtest_cache` with correct PK (includes `z_score_threshold`). Copied all existing data. Replaced all v1.5 z=2.0 rows with v1.4 z=2.0 data (valid: the hardcoded z=2.0 bug means v1.4 z=2.0 = correct v1.5 z=2.0). Result: 17.9M z=2.0 rows, 36.6k z=2.5 (AGQ partial), 9.0M z=3.0 (141 tickers complete).
- **watch_list schema**: Added `z_score_threshold REAL DEFAULT 2.0` column. `ensure_tables()` now auto-migrates. `add_node()` accepts and stores it. Winners page passes it when adding to watchlist. Watchlist display now shows Z Thresh column.
- **Winners page**: Fixed Return/Alpha/etc columns sorting as strings — replaced string-formatting lambdas with `st.column_config.NumberColumn(format=...)` so underlying values stay numeric.
- **Node Inspector full rebuild**: Watchlist at top (clickable to pre-fill params). Price chart with Bollinger bands at z=2.0/2.5/3.0. Trade entry/exit markers + win/loss shading. Rolling Hurst (30d) subplot. Rolling ADF p-value (checkbox-gated). Hurst filter slider with suppressed trade markers and side-by-side metrics. All heavy computation cached by ticker+params.
- **Alembic discussion**: Decided not to adopt — SQLite + single-dev, additive-only schema changes, git history is sufficient audit trail.
- **Sweep restarted** by user after migration. Resume point: AGQ ~94k unvisited (down from 108k pre-fix).

### Key Decisions
- Node Inspector = optimization/validation view. Spatial Topology = island finding. Portfolio view (future) = tradability / capital requirements.
- Hurst stays as dynamic post-filter (slider), not a sweep dimension — they answer different questions.
- ADF gated behind checkbox (slow on first load, fast after cache warms).

### Current State
- Sweep running (user's terminal), resuming z=2.5 pass across all tickers
- Streamlit running PID 222878
- DB: PK correct, z=2.0 complete (v1.4-filled), z=2.5 in progress, z=3.0 done for 141 tickers

---

## 2026-06-28 — Backtester Bug Fix, Hurst Analysis, Overnight Sweep

### What we did
- **Critical bug fixed**: `backtester.py` Numba kernel had `sma - std * 2.0` hardcoded — `z_score_threshold` was stored in the DB tag but never affected the simulation. All v1.5 z=2.5 and z=3.0 nodes were identical to z=2.0 results. Fixed `_simulate` to accept `z_thresh` parameter; `run_backtest` now accepts `z_score_threshold=2.0`; sweep and Node Inspector both pass it through. Verified: same (w=20, TP=28, SL=9, hold=140) node gives 21 trades at z=2.0, 5 at z=2.5, 1 at z=3.0 for LABU.
- **DB cleanup**: Deleted 108k corrupt rows for AGQ z=2.5/3.0 and LABU z=3.0 (v1.5). Those nodes ran with the hardcoded 2.0 threshold regardless of tag.
- **Spatial Topology fix**: `load_slice` now includes `z_score_threshold` column. Dropdown selector appears when multiple thresholds exist — previously all thresholds were blended into one 3D scatter causing duplicate coordinate points and apparent "same profile" across thresholds.
- **Winners page**: Changed `groupby('ticker')` → `groupby(['ticker', 'z_score_threshold'])`. Now shows top N per ticker per threshold side by side for direct comparison.
- **LABU z=2.0 fix**: v1.5 only had 18k/72k rows (partial copy). Copied missing 54k rows from v1.4. LABU now shows in Winners at 210% alpha, 271% return, 2.54× B&H (w=20, TP=21, SL=18, hold=84h) — note SL=18 differs from watchlist param SL=9.
- **FAS sweeps**: z=3.0 — zero positive alpha across 54k nodes. z=2.5 — max 85.4% alpha, 1.43× B&H (only 13 trades). v1.4 559% return node was correct but FAS is structurally momentum.
- **Hurst exponent computed** for all watchlist tickers (R/S method on daily prices). All show H>0.5: AGQ=0.663, DPST=0.654, EDC=0.594, FAS=0.574, LABU=0.522 (6mo). LABU is the only one showing any mean-reversion at 1yr (H=0.454). ADF tests on FAS all non-stationary (p>0.35).
- **Hurst discussion**: Strategy is not pure mean reversion — it captures "recovery from extreme short-term dip in a trending asset." H>0.5 on price level is consistent with snap-back working. Short hold times avoid volatility decay, not momentum.
- **Overnight sweep started**: tmux `sweep_v15_full`, z=[2.0, 2.5, 3.0] for 357 tickers ordered by watchlist first (AGQ, DPST, EDC, FAS, LABU, CRMX) then descending v1.4 max alpha. Currently on DPST. z=2.0 nodes are cached for most tickers so overhead is minimal — mainly catches missed tickers like FAS.
- **statsmodels installed** (for ADF test).
- **v1.6 grid discussion**: Coarse grid (every-3 integers: [3,6,9,...,30] = 6k nodes vs 54k) proposed for v1.6 to validate island consistency. Decided to keep sequential integers for v1.5 and evaluate after results.

### Key Decisions
- FAS should be removed from watchlist: Hurst, ADF, z=3.0 sweep all point to momentum. The v1.4 559% return node is real but untrustworthy structurally.
- LABU SL=9 (original watchlist param) may be wrong — best v1.5 node at z=2.0 has SL=18. Worth revisiting once z=2.5/3.0 data is in.
- The "same return profile" bug was purely the hardcoded 2.0 in the Numba kernel — not a data copy issue.

### Current State
- Overnight sweep running: tmux `sweep_v15_full`, currently on DPST (~50% through), z=[2.0, 2.5, 3.0]
- DB: ~35.8M rows total. v1.5 clean z=2.0 data for all tickers except FAS (being swept now). z=2.5/3.0 for AGQ partially done, FAS z=2.5 done (54k), FAS z=3.0 done (54k, zero positive alpha)
- LABU v1.5 z=2.0: 72k rows complete, shows in Winners
- Watchlist: AGQ, DPST, EDC, FAS (candidate for removal), LABU, CRMX
- No open positions

### Next Session Should
- Check sweep completion — compare z=2.0 vs z=2.5 vs z=3.0 best nodes per watchlist ticker in Winners (now grouped by threshold)
- Decide whether to remove FAS from watchlist based on full v1.5 results
- Revisit LABU watchlist params — SL=18 at z=2.0 beats SL=9; check if z=2.5/3.0 changes this
- Run Hurst + ADF as batch computation across all 357 tickers → add to screener columns (backlog: high priority)
- Plan v1.6 coarse grid sweep design before building

---

## 2026-06-28 — v1.5 Winners/Status UI, Config Finalized, DB Copy

### What we did (append)
- **Winners page**: `z_score_threshold` added as display column, filter multiselect, and passed through session state to Node Inspector and Topology jumps. Min trades floored at 1 (was 0 — caused 17M row load and crash).
- **Sweep Status**: `expected_per_ticker` now multiplies by `z_score_thresholds` count; version-aware so v1.4 still shows 54k and v1.5 shows correct count.
- **v1.4 → v1.5 copy**: All 17.7M v1.4 rows copied into v1.5 with `z_score_threshold=2.0`. Overnight run now only needs to sweep 2.5 and 3.0 thresholds (108k nodes/ticker instead of 162k).
- **config.json finalized**: watchlist tickers (AGQ, DPST, EDC, FAS, LABU, CRMX) at top; `z_score_thresholds: [2.0, 2.5, 3.0]`; 357 tickers total.
- **CRMX added**: 6th in queue for v1.5 overnight sweep. No watchlist params yet — need v1.5 results first.
- **LABU v1.5 sweep**: running in tmux `sweep_v15` (z=3.0 threshold, 54k nodes complete).

### Next Session Should (updated)
- Check LABU v1.5 results — compare z=2.5 and z=3.0 nodes vs z=2.0 (v1.4). Is SL sensitivity improved at higher threshold?
- Start overnight full v1.5 sweep: `tmux new-session -d -s sweep_v15_full ".venv/bin/python run_optimization_sweep.py 2>&1 | tee logs/sweep_v15_full.log"`
- Add CRMX to watchlist once v1.5 results are in
- Position sizing in Slack BUY message (high priority backlog)

---

## 2026-06-28 — v1.5 Sweep, DB Memory Fixes, Risk Discussion, Watchlist Updates

### What we did
- **Risk discussion (Gemini follow-up)** — reviewed mean reversion risks: regime detection, bull market bias, leveraged ETF volatility decay, correlation risk. Concluded long-only + time exit + SL is structurally sound; key gap is regime detection (Hurst filter).
- **Backlog additions** — Half-Life of Reversion, ADF test, Hurst filter (6-month rolling window, DFA method), regime transition stress test (synthetic), H threshold slider (real ticker), rolling 1-year window re-sweep convention.
- **v1.5 sweep** — added `z_score_threshold` as a sweep parameter (2.5, 3.0 in addition to implicit 2.0 from v1.4). Motivated by LABU having wide SL nodes — hypothesis: higher threshold = deeper dips = tighter SL viable. Changes: `strategies.py`, `run_optimization_sweep.py`, `config.json`, `backtest_cache` schema (new column, ALTER TABLE migration).
- **DB memory fix** — all three main pages (Spatial Topology, Node Inspector, Winners) were loading 18M rows on startup (~6GB). Fixed: targeted queries per (version, ticker, strategy), numeric filters pushed into SQL for Winners, watchlist stats scoped to watchlist tickers. Added DB indexes on `version` and `(version, ticker)`.
- **Spatial Topology** — "View in Topology" button added to Winners page; session state jump lands on correct ticker/strategy/version.
- **Node Inspector** — z_score_threshold dropdown added; rewritten to use targeted slice query.
- **Watchlist** — added DPST (w=10, TP=21, SL=12, hold=126h) and FAS (w=10, TP=25, SL=10, hold=133h). AGQ and EDC already on list. LABU already on list. FAS noted as low win rate (22%) — intentional nerve test with smaller position.
- **v1.4 sweep** — was 90% complete at session start; completed during session (323→357 tickers).
- **v1.5 sweep** — started for watchlist tickers only (AGQ, DPST, EDC, FAS, LABU) in tmux session `sweep_v15`. ~108k nodes per ticker.

### Key Decisions
- v1.5 uses same DB, new version tag — threshold is just another parameter, not a new strategy
- FAS spatial grid is all-green (island warning) — likely momentum, not mean reversion. Added anyway for small position nerve test.
- QCML and CRWL are single-stock underliers (QCOM, CRDO) — excluded from watchlist consideration
- Bitcoin ETFs tested poorly (max 42% return) — not worth pursuing
- Hurst/ADF/Half-life are screener metrics (per-ticker, offline), not sweep parameters

### Current State
- v1.4: 357/357 tickers complete
- v1.5: running in tmux `sweep_v15` for watchlist tickers; config.json restored to full 357 tickers for overnight full run
- Watchlist: AGQ, EDC, DPST, FAS, LABU
- No open positions
- Streamlit running (PID ~5733)

### Next Session Should
- Check v1.5 watchlist sweep results — compare 2.5 and 3.0 threshold nodes vs 2.0 for LABU specifically (SL sensitivity)
- Start overnight v1.5 full sweep: `tmux new-session -d -s sweep_v15_full ".venv/bin/python run_optimization_sweep.py 2>&1 | tee logs/sweep_v15_full.log"`
- Review Winners page under v1.5 once data is available
- Consider position sizing in Slack BUY message (still high priority backlog)

---

## 2026-06-27 — Sweep Universe Expansion, Bug Fixes, Strategy Docs

### What we did
- **Results (8).csv imported** — 682 leveraged ETPs with Underlying Index + Total Assets; 673 have price data
- **508 new tickers fetched** — merged into tickers.json (now 1515); 506 CSVs downloaded, 2 failed (AGATF, DEE)
- **Timezone cleanup** — new yfinance (1.4.1) returns tz-aware timestamps; fixed 380 CSVs by stripping tz offset, fixed `data_manager.py` and `run_optimization_sweep.py` to strip tz on load
- **357 tickers loaded into config.json** — leveraged ETPs, has data, $1M liquidity floor (avg_vol_10d × last_price), includes inverse
- **app.py strategy name fix** — multiselect options updated from `ZScore_Original` → `ZScoreBreakout`; default fallback also fixed
- **NO_TRADES caching fix** — nodes returning no trades were never written to DB, causing them to rerun on every sweep restart. Now cached with trades=0 — massive speedup for thin tickers
- **Heatmap duplicate fix** — `pivot()` replaced with `groupby().mean().unstack()` to handle duplicate TP/SL cells
- **Trade log** — new `trade_log` table in DB; `log_trade_entry` on BUY execution, `log_trade_exit` on SELL; `open_positions` gets `trade_log_id` FK; auto-migrated via ALTER TABLE guard on startup
- **`pages/5_Sweep_Status.py`** — new page: per-ticker progress (nodes cached vs expected, % complete, ASCII bar), SUCCESS vs NO_TRADES counts, data freshness (last date in CSV), version filter, auto-refresh 30s
- **`pages/6_Strategy.py`** — renders `docs/strategy.md` in app
- **`docs/strategy.md`** — strategy reference: signal logic, params, live trading assumptions, edge cases
- **`docs/operational_limits.md`** — Phase 1 trading rules: risk-first principle, position limits, execution rules, travel policy, no early exits
- **Winners page filters** — added Min return % (default 100%), Min B&H multiplier (default 2x), B&H Mult column
- **Sweep running in tmux** — ~1.4M nodes/hr, ETA ~10h from session start (~10 tickers done at session close)

### Key Decisions
- NO_TRADES nodes are now cached but excluded from Winners/results — holes in grid are swept, not missed
- $1M liquidity floor = `avg_vol_10d × last_price ≥ $1M` — same as `$50k × 20`, 357 tickers pass
- Sweep runs via `tmux new -s sweep` to survive terminal drops
- Phase 2 (automated exits) closer than expected — Schwab API likely target; exits are deterministic and low-risk to automate

### Backlog additions
- Position sizing in Slack BUY message (high priority)
- Chaos monkey / floor alpha: worst-case entry/exit delay (1d and 2wk), missed TIME_EXIT, drop top N trades
- Portfolio backtest page: concurrent positions over time, capital utilization
- Automated exits (Phase 2): Schwab API, TP/SL/TIME submitted as market orders
- Broader ticker universe: results.csv (999 rows, mixed) for non-leveraged expansion

### Current State
- Sweep running in tmux, ~12% complete, ETA ~10h
- 3 tickers on watch list: AGQ, EDC, LABU
- Trade log built, not yet tested end-to-end (needs a real trade)
- DB has v1.4 data for 40 tickers

### Next Session Should
- Check sweep completion, review Winners page with new filters
- Pick next watchlist candidates from v1.4 results (filter: >100% return, >2x B&H mult, beat SPY)
- Build position sizing into Slack BUY message (`avg_vol_10d × last_price` from screener DB)
- Discuss chaos monkey sweep design before building

---

## 2026-06-27 — Screener, Ticker Universe, Data Collection

### What we did
- **Winners → Node Inspector fix** — now passes window/TP/SL/hold in session state so all dropdowns auto-select on arrival
- **Node Inspector strategy bug fix** — removed stale `strategy_mapping` dict, replaced with `getattr(strategies, name)` — was causing `NoneType is not callable` error
- **Data refresh wiring** — `active_signals.py` now fetches fresh price data for all watched tickers at the start of each poll cycle; no longer needs `data_collector.py` running alongside
- **`tickers.json`** — created as single source of truth for data collection universe; `data_collector.py` now reads from it instead of hardcoded list
- **Daily cron** — `scripts/run_data_collector.sh` runs `data_collector.py --once` at 8 AM daily, logs to `logs/data_collector_daily.log`
- **Full history pull** — ran `data_collector.py --once` for all ~1000 tickers in tickers.json; 926 CSVs cached, 74 failed (delisted/no data)
- **`tickers` DB table** — `scripts/import_tickers.py` imports screener CSV into `cache/trading_universe.db` with derived columns: `leverage` (parsed from description), `inverse`, `has_data` (CSV exists), `stock_underlier` / `index_underlier` (classified from underlying index + description), `last_price`, `total_assets`, performance columns
- **`pages/4_Screener.py`** — filter leveraged ETF universe before deciding what to sweep; filters: AUM, dollar-volume liquidity (investment × multiplier), leverage (2x/3x), inverse, single-stock underlier, has-data, underlying index search, performance; "Add to config.json" button populates `target_tickers`
- **Screener CSV exploration** — imported `results.csv` (mixed leveraged/non-leveraged), then `Results (7).csv` (682 leveraged-only but missing Underlying Index + Total Assets columns)

### Key Decisions
- `tickers.json` = data collection universe (all candidates); `config.json` target_tickers = sweep candidates (curated subset)
- Dollar-volume liquidity filter: `avg_vol_10d × last_price ≥ investment × multiplier` (default $50k × 20 = $1M)
- `leveraged_etp` field from screener is reliable — 1x inverse ETFs correctly marked "No"
- `stock_underlier` / `index_underlier` split is cleaner than a boolean flag; crypto/commodity/currency leave both NULL
- Single-stock underlier detection uses company suffixes on `underlying_index` + description patterns ("2X Long TSLA Daily", "ADRhedged") for "No Underlying Index" cases

### Current State
- 926 tickers with hourly price history cached
- Screener page working; current import (Results 7.csv) missing Underlying Index + Total Assets — underlier classification is description-only and incomplete
- No open positions

### Next Session Should
- **Re-export screener** with Underlying Index + Total Assets columns, re-run `python scripts/import_tickers.py <new_file.csv>`
- **Use Screener to select sweep candidates** — filter to 2x/3x, exclude single-stock, apply liquidity filter, add to config.json
- **Run sweep on leveraged universe** — ~130 tickers with data at 2x/3x; at 20 min/ticker ~45 hours with current grid. Consider coarsening TP/SL to every 2% (→ ~3 days)
- **Trade log** — new DB table for executed trades (signal price, exec price, exit price, drift), triggered from Socket Mode modal submissions

---

## 2026-06-27 — Socket Mode, Winners Page, Live Test

### What we did
- **Slack Socket Mode** — upgraded from webhook to `slack_bolt` + bot token + app token. BUY/SELL messages now have interactive Executed/Skipped/Exited buttons. Clicking opens a price entry modal; submission writes to `open_positions` and updates the original message. Falls back to webhook if bot tokens not set.
- **Chart upload** — `matplotlib` chart generated on each signal (price, SMA, ±2σ bands, signal marker for BUY; entry/TP/SL lines for SELL), uploaded via `files_upload_v2`. Channel ID resolved at startup via `_resolve_channel_id()`.
- **`compute_buy_signal` fix** — now excludes today's intraday close from the daily rolling window. Prior version made BUY signals mathematically impossible (lowering price also lowered the lower band). Fix: `df_daily[df_daily.index < today]`.
- **`--ticker` filter** — `python active_signals.py run --ticker TEST` limits poll loop to specific tickers without removing others from watch list.
- **Single-line poll summary** — one line per poll cycle instead of one line per node.
- **`scripts/live_test.py`** — synthetic TEST ticker driver: `setup` (writes CSV + adds to watch list), `sell` (pumps price above TP), `status`, `cleanup`. Verified full BUY→SELL→close flow via Slack.
- **`pages/3_Winners.py`** — leaderboard of top nodes per ticker. Filters: version, ticker, strategy, min trades, min alpha, beat asset B&H toggle, top N. Dismiss per `(ticker, strategy, version)` persisted to `cache/dismissed_tickers.json`. Click row → Watch / Dismiss / Open in Node Inspector. Watch list table at bottom with inline label editing, remove by uncheck, and backtest stats joined from DB. Last price + daily volume columns from cache CSVs.
- **Deleted `v_perf_test`** — 18,090 rows removed from `backtest_cache`.
- **`.env` cleanup** — removed stale `Code snippet` header and unused `OPENAI_API_KEY`, `FINNHUB_API_KEY`, `SMA_PERIOD`, `Z_SCORE_THRESHOLD` lines.

### Key Decisions
- Socket Mode runs in a daemon thread; poll loop continues immediately after posting signal (non-blocking)
- Dismiss scope: `(ticker, strategy, version)` — new version resets dismissals, different strategy on same ticker stays visible
- `strategy_return` and `alpha_vs_spy` stored as percentage points (e.g. 729.2 = 729.2%) — do not multiply by 100 in display
- `compute_buy_signal` uses prior closed day's indicators — matches live trading semantics (today's daily bar hasn't closed yet)

### Current State
- Layer 3: Socket Mode live and tested end-to-end. AGQ on watch list (ID=1).
- Winners page: working. Dismiss file at `cache/dismissed_tickers.json`.
- No open positions.

### Next Session Should
- **Winners → Node Inspector jump**: window/TP/SL/hold dropdowns in Node Inspector need auto-selection when navigating from Winners (session state currently only passes ticker/strategy/version)
- **Data refresh wiring**: call `fetch_live_data_smart(ticker)` inside poll loop before checking signals
- **Trade log**: new DB table for executed trades — signal price, exec price, exit price, drift. Triggered from Socket Mode modal submissions.

---

## 2026-06-26 — Layer 3 Active Signals + Strategy Architecture

### What we did
- **Built `active_signals.py`** — poll loop, BUY/SELL signal detection, Slack Block Kit notifications, open position tracking, execution prompt. CLI commands: `run`, `list`, `add`, `remove`, `positions`
- **Refactored `strategies.py`** — added `check_exit` to `BaseStrategy`; `active_signals` now delegates all entry/exit logic to the strategy class, no signal math duplicated
- **Renamed strategy DB values** to match class names (`ZScore_Original` → `ZScoreBreakout`, `ZScore_TrendFiltered` → `TrendFilteredZScore`). `run_optimization_sweep.py` now uses `getattr(strategies, name)` — no hardcoded map
- **Added `tests/`** — `test_ZScoreBreakout.py` (11 cases), `test_TrendFilteredZScore.py` (4 cases), shared helpers in `conftest.py`. All passing.
- **Wrote `docs/strategy_architecture.md`** — target data model where node = `(strategy, params as JSON)`, ticker is a param not a field, strategy declares its own parameter schema. Migration deferred until second strategy added.
- **Added AGQ to watch list** — `v1.4 AGQ top 20w 140h` (window=20, TP=28, SL=9, hold=140h)
- **`python-dotenv`** added; `.env` file needed for `SLACK_WEBHOOK_URL`

### Key Decisions
- Strategy class is the single source of truth for entry/exit logic — `active_signals.py` knows nothing about signal math
- Node identity is `(strategy, params as JSON)` — ticker is a param, not a first-class field. Hardcoded columns in `backtest_cache` are acceptable until a second strategy is added.
- Exit conditions (TP/SL/TIME) live on `BaseStrategy.check_exit` — subclasses can override for custom exit rules
- No unit tests by choice — sanity checking via running known nodes through the backtester is more meaningful
- `active_signals.py` requires `data_collector.py` running simultaneously — documented in readme.md

### Current State
- Layer 3: `active_signals.py` built and tested. Watch list has AGQ. No open positions.
- Slack webhook works (incoming webhook). Interactive buttons (Socket Mode) planned but not built.
- `active_signals.py` has no data refresh — reads stale cache unless `data_collector.py` is running
- DB strategy names now match class names; v1.2/v1.3 rows still have old names but those are dirty data anyway

### Next Session Should
- **Slack Socket Mode** — upgrade from webhook to bot token + app token; enable interactive buttons (Executed/Skipped) with modal for execution price entry and chart image upload
- **Chart image in Slack** — price history + SMA/bands + signal marker, generated on signal fire and uploaded via bot token
- **Live simulation test** — inject synthetic BUY data for TEST ticker, fire real Slack message, confirm trade, pump TP/SL price, confirm exit
- **Winners page** — Streamlit leaderboard showing top nodes per ticker for current version, with "Add to Watch List" button (imports `add_node` from `active_signals.py`)
- **Data refresh wiring** — call `fetch_live_data_smart(ticker)` inside the poll loop before checking signals

---

## 2026-06-26 — Performance & UI Session

### What we did
- **Numba optimization**: `backtester.py` rewritten with `@njit` kernel — 4s → 11ms per node (~360x). Full 18k node sweep now takes ~5 minutes on 10 workers vs overnight
- **SQLite batch commits**: writes now batch every 100 nodes — eliminates write contention with 10 workers
- **Fixed TIME_EXIT bug**: filter in `run_optimization_sweep.py` now correctly includes `TWIN`/`TLOSS` — this was causing thousands of nodes to go missing from DB
- **Fixed `max_hold_days` → `max_hold_hours`** in `active_phase_grid.json` output
- **Restored blue dots** (planned nodes) in Spatial Topology page, filtered against already-completed nodes
- **Topology UI fixes**: alpha filter defaults to absolute floor, leaderboard respects 4th-dimension slice, version picker is now first and cascades to ticker/strategy
- **Config simplified**: removed dual DB/file config — `config.json` is now single source of truth. Fixed generations `min_value=0`
- **Deleted legacy files**: `trading_engine.py`, `visualize_results.py`, `plot_3d_growth.py`, old `strategy_optimizer.py`
- **Renamed**: `strategy_optimizer.py` → `backtester.py`, `run_backtest_simulation` → `run_backtest`
- **tqdm throttled**: postfix updates every 2 seconds, display intervals 15-30s
- **Nightly DB backup cron** set up at 2am, keeps last 7 backups
- **Verified**: new backtester output matches DB on spot-check (mismatches are expected — DB was populated with buggy TIME_EXIT code)

### Current State
- Layer 1 (data collection): working
- Layer 2 (optimization): working, fast. Currently running sweep with new version tag to get clean data
- Layer 3 (live trading): not started
- DB has mixed versions — v1.2/v1.3 computed with old buggy code, new version being computed now with fixed code
- `requirements.txt` needs `numba` added

### Key Decisions Made
- Brute force full sweep is now fast enough that generations are redundant — left in code as escape hatch for future larger grids
- `config.json` single source of truth — DB copy of config removed
- numpy/Numba over GPU — dataset too small to benefit from GPU

### Next Session Should
- Add `numba` to `requirements.txt`
- Build Layer 3 live trading engine (`live_trading.py`)
- Build trade chart Streamlit page (price/bands/markers, launchable from Node Inspector)
- Review sweep results once current run completes — pick parameter sets for live trading

---

## 2026-06-25 — Repo Setup & Documentation

### What we did
- Set up SSH key (ed25519) and connected to GitHub
- Created repo `pkim-tech/trading`, renamed branch from `master` to `main`
- Cleaned up project structure: moved legacy files to `output/`, set up `.gitignore` and `.claudeignore`
- Created `~/.claude/CLAUDE.md` (global prefs) and `trading/CLAUDE.md` (project context)
- Rewrote `readme.md` with three-layer architecture
- Created `docs/` with `design.md`, `backlog.md`, `session_cache.md`
- Initial commit pushed to GitHub

### Current State
- Layer 1 (data collection): complete and working
- Layer 2 (optimization): complete, last run was ~18k nodes per ticker overnight
- Layer 3 (live trading): not started, `trading_engine.py` is legacy placeholder
- No pytest / unit tests — intentional for now

### Key Decisions Made
- Brute force over smart search for parameter optimization (floating point issues with fine-mesh approach)
- SQLite caching means nodes are never re-evaluated — safe to re-run sweeps
- L3 cache optimization is a known future performance improvement (Gemini suggestion, not yet implemented)

### Next Session Should
- Review `trading_engine.py` and decide whether to retrofit or replace for Layer 3
- Investigate L3 cache optimization for node evaluation speed

## 2026-07-04 — Fixed replay-harness bug (not a real live/backtest gap); root-caused v2.7's weak returns; built v2.11 LimitOrderTrailingExit

### What we did
- **Reviewed the completed v2.x backfill's post-sweep report** (`docs/post_sweep_report.md`, generated by last session's `scripts/post_sweep_report.py`): all 4 live watchlist nodes (AGQ/EDC/FAS/HIBL, v1.5) show live-parity MATCH. Best v2.5 replacement candidates found for all 4, but EDC/FAS/HIBL's candidates have suspiciously low win rates (3-16%) driven by a few outlier trades — flagged as overfit risk, not clean improvements. AGQ's candidate (45% win rate) looks more legitimate.
- **Investigated the `LimitOrderZScoreBreakout` parity MISMATCH shown in the report and found it was a bug in the test harness, not the kernel or live code.** `verify_live_parity.py`'s `replay()` checked the entry signal against bar Close instead of Low, unlike the kernel (`_simulate_limit`, correctly Low-based). This made it look like live couldn't achieve the backtest's entries. Turned out backwards: production `active_signals.py`'s `notify_limit_fill` loop polls all day every 300s (`POLL_SECS`), not gated by the buy-window check — live genuinely monitors continuously for limit-entry nodes, matching (and exceeding) the kernel's assumption. Fixed — `replay()` now uses bar Low for `LimitOrderZScoreBreakout`/`LimitOrderTrailingExit` entries (`scripts/verify_live_parity.py`). Verified: TQQQ/HIBL nodes that previously reported MISMATCH now report clean MATCH.
- **Root-caused why v2.7 (`LimitOrderZScoreBreakout`) structurally underperforms** every other version (avg alpha -61.5 vs +9 to +34 for others; loses on every per-ticker best-node comparison checked): its entry trigger (`Low <= band`, any wick) is looser/noisier than `ZScoreBreakout`'s `Close <= band` confirmed-close entry, and its fill is always capped at exactly `lower_band`, giving up the deeper entries `ZScoreBreakout` gets on real breakout days. Combined with the fixed TP/SL exit capping winners on these fat-tailed leveraged-ETF moves, this explains the gap to v2.8/2.9/2.10 (trailing exit / bounce-confirmation entry) too. Considered and rejected: an entry "buffer" (deeper Low threshold) — mathematically equivalent to the z_score_threshold knob already swept, not a real noise filter. Concluded entry noise is a real, structurally-unfixable-in-isolation cost (any real fix collapses into `TrailingBuyZScoreBreakout` or `TrendFilteredZScore`).
- **Built `LimitOrderTrailingExit` (v2.11)** to isolate whether the exit alone (fixed TP/SL to trailing stop) recovers most of v2.7's gap while holding the noisy entry constant: new strategy class (`strategies.py`, subclasses `LimitOrderZScoreBreakout`, reuses its entry + `TrailingExitZScoreBreakout`'s trailing exit), new kernel `_simulate_limit_trail` + wrapper `run_backtest_v211` (`backtester.py`), dispatch wiring in `run_optimization_sweep.py` (order-sensitive — must check before the parent `LimitOrderZScoreBreakout` branch), `_uses_fixed_sl` updated in `active_signals.py`, `verify_live_parity.py` wired for parity testing, `scripts/run_v2_backfill_sweep.sh` updated (v2.11 case added, included in the no-arg full loop). Spot-checked kernel/replay parity on SOXL/TQQQ — MATCH (same pre-existing WIN/TWIN cosmetic label quirk as v1.8, not new).
- **Launched, then stopped, the full 53-ticker v2.11 backfill** at user's request (user wants to run it themselves) — `config.json` restored to committed state, stray `config.json.bak` removed, no processes left running.

### Key Decisions Made
- Hypothesis to test once v2.11's backfill runs: it should beat v2.7 (no more capped winners) but still underperform v2.8 (same trailing exit, but clean entry, no noise tax) — if it closes that gap anyway, entry noise wasn't costing much; if not, that's confirmation entry noise is structurally limiting.
- `docs/design.md` and `docs/backlog.md` updated with the harness-bug fix, v2.7 root cause, and v2.11 design rationale.

### Next Session Should
1. Run `./scripts/run_v2_backfill_sweep.sh v2.11` (user will run manually, ~75min for 53 tickers) — not yet run.
2. Once done, compare v2.11 vs v2.7/v2.8/v2.9/v2.10 same-node-family to test the entry-noise hypothesis above.
3. Revisit the AGQ v2.5 candidate (w=10 z=1.0 tp=19 sl=11 hold=140h) and equivalents for EDC/FAS/HIBL as watchlist swap candidates — still pending from last session, EDC/FAS/HIBL's need overfit-risk review (low win rates) before promoting.
4. v1.8 WIN/TWIN labeling discrepancy in `verify_live_parity.py` — still unresolved, low priority, now also present in the v2.11 case (same root cause, not new).

## 2026-07-04 (later) — v2.11 backfill result confirms entry-noise hypothesis (worse than v2.7); fixed fixed_sl cache-key gap + WIN/TWIN mislabeling; solved v1.9/v1.10 live-execution gap via Schwab trailing-buy order

### What we did
- **Reviewed the completed v2.11 (`LimitOrderTrailingExit`) 53-ticker backfill** launched last session: clean per-ticker best-alpha comparison shows v2.11 (avg 37.5%, median -9.3%, 28/53 negative) underperforms both v2.7 (avg 87.9%, median 33.2%, 20/53 negative) and v2.8 (avg 296.0%, median 81.9%, 9/53 negative) — the opposite of the hypothesis (v2.11 was expected to beat v2.7 by removing the capped-winner problem, while still trailing v2.8). Root cause: the trailing exit only arms after clearing the `take_profit` activation threshold; until then, only the fixed floor stop protects the position. Noisy Low-touch entries get stopped out at that floor far more often than confirmed-close entries, so they rarely survive to activation — entry noise cancels out the exit improvement rather than just diluting it. Confirms (more strongly than expected) that the touch-based entry's noise is a structurally unfixable-in-isolation cost. Full detail in `docs/backlog.md`.
- **Found and fixed two bugs while investigating** (neither caused the above result, both are cache/test hygiene):
  1. `run_optimization_sweep.py:262-263` — `uses_fixed_sl` check missed `LimitOrderTrailingExit` (subclasses `LimitOrderZScoreBreakout`, not `TrailingExitZScoreBreakout`), so v2.11 cache rows stored `fixed_sl=0.0` though the run actually used the real config value (15%). Fixed going forward; existing mislabeled rows left as-is (would need a bulk `backtest_cache` `UPDATE`, blocked by the standing "don't bulk-mutate that table" rule).
  2. `scripts/verify_live_parity.py:109` — `replay()` mislabeled every trailing-stop-triggered exit as TWIN/TLOSS instead of WIN/LOSS, because `active_signals.check_sell_condition` collapses the strategy's `WIN`/`LOSS` reason into a generic `'TRAIL'` string for Slack messaging, and `replay()` didn't recognize `'TRAIL'` as a WIN/LOSS case. Fixed; verified AGQ v1.8 case (previously showing the labeling quirk) now reports clean MATCH — resolves item #4 above.
- **Solved the v1.9/v1.10 (TrailingBuyZScoreBreakout/TrailingBothZScoreBreakout) live-execution gap** — previously thought to need a `pending_entries` polling state machine tracking the running low. User confirmed Schwab supports trailing-stop-buy orders (reference price ratchets down with a falling low, triggers on a bounce off the running low — exact mechanic `_simulate_trail_buy` models). No state machine needed: convert the staged order to a trailing-stop-buy at bar close when the signal fires, same single action as today's limit→market swap. Updated `docs/code_review_findings.md` finding #3 and `docs/backlog.md` with the revised fix direction. Also discussed and ruled out stop-limit orders anywhere in this workflow (exits need guaranteed fills; no trailing-stop-limit-to-buy combo exists at Schwab anyway) — documented in `docs/operational_limits.md`. Flagged OCO/OTO bracket orders as worth investigating to reduce manual steps, but only as a bridge — user's stated plan is full Schwab API automation once the strategy is proven out.

### Key Decisions Made
- v2.11's negative result stands — not a harness artifact. v1.9/v1.10 (persistence-confirmed entry) remain the only structurally sound path to a clean entry; their live-execution gap is now considered solved in design (broker order type), not requiring new `active_signals.py` state.
- No use for stop-limit order types anywhere in the current manual execution workflow.

### Next Session Should
1. Still pending: `_STRATEGY_LABELS` entries for `TrailingBuyZScoreBreakout`/`TrailingBothZScoreBreakout` in `active_signals.py`, and confirming Schwab's order ticket accepts the needed `trail_buy_pct` values, before v1.9/v1.10 can go live.
2. Revisit AGQ/EDC/FAS/HIBL v2.5 watchlist candidates (EDC/FAS/HIBL still need overfit-risk review, low win rates) — carried over from prior sessions, not touched this session.
3. Consider whether v1.9/v1.10 are worth a live pilot now that the execution-mechanics gap is solved, or whether to wait for a v2.9/v2.10 bias-corrected backfill first (not yet run — only v2.4-2.11 have completed).
4. If ever revisiting v2.11's existing DB rows: `fixed_sl` column is mislabeled as 0.0 for all v2.11 rows (should be 15.0) — cosmetic only, but flag before trusting a raw `fixed_sl` groupby on v2.11 data specifically.
