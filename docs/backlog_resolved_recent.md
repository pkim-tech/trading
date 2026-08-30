# Backlog — Recently Resolved

## [backtest][tooling] Resolved 2026-08-29 — `GT_CANDIDATE_TIEBREAK` comment mis-describing `stop_loss`/tpct axis fixed (`293558a`, comment-only). Full detail: `deep_backlog.md`.

## [backtest][tooling] Resolved-as-decided-won't-fix 2026-08-29 — prune-validation gate island-CENTER selection A-vs-B cross-check gap, closed per user's call, no code change. Full detail: `deep_backlog.md`.

## [backtest][tooling] Resolved 2026-08-29 — independent-reimplementation verification for GT overlays confirmed done via `scripts/sim_1s_vs_1m_groundtruth_overlays.py` (raised 2026-08-23). Full detail: `deep_backlog.md`.

## [backtest] Resolved-as-skip 2026-08-29 (recorded; user's 2026-08-23 call) — 2 real SOXL Phase2-GT scopes left incomplete by `pick_island_centers` tiebreak fix (`54cbd0c`), no recompute planned. Full detail: `deep_backlog.md`.

## [backtest] Resolved 2026-08-29 — GT cliff-safety worst-neighbor CAGR threshold confirmed at 0% (code is source of truth), stale 20% line in `ground_truth_kernel_rebuild.md` fixed. Full detail: `deep_backlog.md`.

## [backtest] Resolved 2026-08-28 (Search Completeness Audit, first real run) — SOXL TrailingBoth fixed_sl=7's generational-search winner confirmed as the TRUE global optimum (756,000-cell brute-force mesh, byte-identical top-9). Full detail: `deep_backlog.md`.

## [live-trading][testing] Resolved 2026-08-29 — `verify_real_trades_vs_kernel.py`'s still-open-trade blind spot fixed (238504c/ebf86c2), full bipartite matching. Full detail: `deep_backlog.md`.

## [backtest][specced] Resolved 2026-08-29 — fold top-X trade-sequence retention resolved via `backtest_winner_trades`/`get_cached_trades` (different mechanism than originally proposed), not the sweep-inner-loop heap design. Full detail: `deep_backlog.md`.

## [backtest][specced] Resolved 2026-08-29 — Phase 3/Phase 5 merge design question decided and built (`eca730f`): Phase 3 retired, Phase 5 widened to full `candidate_nodes` population, verified matching old Phase 3 to <0.02pp. Full detail: `deep_backlog.md`.

## [backtest][tooling] Resolved 2026-08-29 — Phase 4/5 candidate resolution gap closed, Campaign C (window=15) verification run completed (144 candidates/16 scopes, all outliers explained). Full detail: `deep_backlog.md`.

## [tooling][backtest] Resolved 2026-08-29 — `rebuild_winner_trades.py`'s latent node_key bug for TrailingExitZScoreBreakout candidates fixed (`c3f2510`), paired-reviewed, verified 0 mismatches vs. forced resim. Full detail: `deep_backlog.md`.

## [backtest][testing] Decided 2026-08-29 — spot-check Phase 5's production-kernel numbers against the outside sim only on anomaly/new-mechanism, not routinely (cost: ~6 min/candidate, would more than double Phase 5's new runtime). Full detail: `deep_backlog.md`.

## [backtest] Resolved 2026-08-29 (root-caused, script archived) — the 2026-08-24/25 "unresolved 1m-vs-1s SOXL granularity gap" (candidate_nodes id=851, 104 vs 123 trades) was a bug in the verification script, not the GT kernel. `scripts/sim_1m_vs_1s_walk.py`'s hand-written close-check evaluated the WRONG hourly bar (a separate row labeled 1hr later) instead of the same bar's own Close field, per `backtester.py`'s actual `_simulate_trail_ground_truth` close_check logic (checks `c <= band` on the same row as the open-check). Confirmed via a concrete trade-by-trade diff (27 GT trades missing from the 1m walk, 7 extra) and one fully traced example (2021-12-06 signal: GT correctly used the 9:30 bar's own Close=$58.56 to fire; the walk used the next hour's bar Close=$57.68 instead, delaying/mis-sizing the WAIT state and missing the whole day's trade). Since the bug is in the shared `walk()` function, it affected BOTH the "1m" and "1s" outputs identically — the previously-reported "1m=23.91%/1s=15.73% CAGR, 34% relative delta" finding is not trustworthy evidence of real granularity risk. The other, later-built 1m-vs-1s tool (`sim_1s_vs_1m_groundtruth.py`, forked from the parity-gated `sim_minute_groundtruth_independent.py` reference) does not share this bug and is the one that produced the legitimate, already-resolved Phase3 SOXL outlier finding (fill-bar SL not checked). `sim_1m_vs_1s_walk.py` archived to `scripts/archive/` rather than fixed, since the parity-gated tool already supersedes it. Full detail: `deep_backlog.md`.

## [live-trading][data] Resolved 2026-08-28 (dispatched, coder) — daily-close retroactive-adjustment detector + fix built for the live SMA/z-score indicator cache (Task #9); a fresh yfinance daily fetch now replaces the resampled-`_1h.csv` series feeding `compute_buy_signal`'s indicators, self-correcting by construction, with a day-over-day consistency detector, Slack alert, and Massive secondary cross-check. Paired review (2 rounds) found+fixed a real HIGH bug in the consistency gate. Full detail: `deep_backlog.md`.

## [live-trading][security] Resolved 2026-08-28 (dispatched worktree agent) — removed `signals_db.transfer_position_to_new_node()` entirely (superseded by liquidate-not-migrate rotation design, `docs/design.md`'s 2026-08-28 "quarterly ticker-rotation concept" entry), zero callers confirmed, paired-reviewed. Full detail: `deep_backlog.md`.

## [live-trading][security][HIGH] Resolved 2026-08-28 (dispatched, coder4) — signal_price-vs-real-market-data mismatch fully explained for all 3 tickers (SOXL/DPST/DFEN). SOXL was the missing piece: entry_timing='open_check' pins signal_price to the real session-open print by design, confirmed exact match ($117.37) against real Massive.com 1s data. Not a bug. Full detail: `deep_backlog.md`.

## [live-trading][testing] Resolved 2026-08-28 (stale-entry cleanup) — 08-25's "12/14 NOT CHECKED (RuntimeError)" item was already fixed by the 08-27 yahoo->massive routing fix (`5b2f29e`), just never removed from backlog_cache.md. Confirmed via 2 fresh evening_status.py 3 reruns, zero RuntimeErrors. Full detail: `deep_backlog.md`.

## [live-trading][tooling] Resolved 2026-08-28 (coder3 dispatch) — self-check guard added to the 2 remaining hardcoded `CREATE TABLE watch_list_new` migration blocks (commit `8474e0e`), paired-reviewed, no HIGH findings. Full detail: `deep_backlog.md`.

## [tooling] Resolved 2026-08-28 (coder3 dispatch) — `prune_backtest_cache_ground_truth.py --build` now has index/total+ETA progress logging (commit `b983b89`), tested end-to-end against a scratch DB copy. Full detail: `deep_backlog.md`.

## [backtest][tooling] Resolved 2026-08-28 (coder dispatch) — `active_builds` promotion table now gates `massive_hourly_derived`/`massive_minute_derived`'s "latest build" resolution instead of pure `ORDER BY id DESC`; new `scripts/promote_derived_build.py` (refuse-to-narrow guard, `--migrate` backfill), migration verified byte-identical on real live DB for all 17 live tickers. Full detail: `deep_backlog.md`.

## [process][backtest] Resolved 2026-08-28 (evening) — "agent never launches a sweep campaign itself" rule fully relaxed (general, not just schema-v2 gap-fills), after directly verifying the full `run_sweep_queue.sh` campaign path is non-destructive (INSERT OR REPLACE-only writes, guarded/dead migration DROP TABLEs, config.json trap-restored, real deletion tools untouched/still gated separately). CLAUDE.md, backtest-change-rollout skill, design.md, and memory all updated. Full detail: `deep_backlog.md`.

## [live-trading][testing][HIGH] Resolved 2026-08-28 (later) — 4 of 6 findings fixed from the paired review of the yahoo->massive/PHANTOM-cross-check fix (CHECKS carve-out narrowed after a 2nd paired-review round, holiday calendar, run_all() isolation, docstring); 1 superseded into a new backlog item (dividend-adjustment discontinuity), 1 remains open (close_check wrong-bar bug). Full detail: `deep_backlog.md`.

## [backtest][testing] Resolved 2026-08-28 (later) — SOXL Phase3 +7.23pp 1m-vs-1s outlier root-caused to a single-trade fill-bar-SL artifact (candidate_nodes 2140/2141/2142); original "wide TP/SL/long hold" framing was wrong, real driver is tight fixed_sl + wide arm_pct. Full detail: `deep_backlog.md`, full writeup: `research_log.md`.

## [live-trading][security] Resolved 2026-08-25 (dispatched session) — SL/pending-buy orders going CANCELED/REPLACED/REJECTED/EXPIRED at the broker (never just FILLED) now alert instead of silently polling forever; WEBL/DPST incidents both covered. Paired review found+fixed 1 CRITICAL (REPLACED must never auto-clear a pending-buy row) + 2 HIGH (alert-storm cooldown, missing Grid rows). Full detail: `deep_backlog.md`.

## [live-trading] Resolved 2026-08-25 (planner session) — full v6 promotion: 14 real tickers now live on v6 (was v5), CLAUDE.md's stale "v5/v5.1 default" gap closed. Real DB-vs-broker reconciliation gap found and fixed (DFEN/SOXL/WEBL/DPST) via new `scripts/reconcile_flat_position.py`; a real fill-optimism bug found and root-caused on OILU via new 1-sec/1-min Massive-data checklist scripts. Full detail: `deep_backlog.md`.

## [backtest][tooling] Resolved 2026-08-23 (even later still) — `candidate_summary_report.py`/`candidate_full_review.py` GT paths also never windowed `build_candidate_report_ground_truth` (start_date/end_date always None)
Sibling to the `data_source` fix below, found during its paired review. Added `_window_dates_from_version` (inverse of `window_version_suffix`, handles double-suffix versions) and threaded `start_date`/`end_date` into all 4 previously-hardcoded call sites in both files. Verified real KORU node_id=586 (windowed version): `n_trades` corrected 59→58, `years` corrected 4.994→3.992 (~25% annualization error, flows into addon/drought CAGR legs), `spy_bh` corrected 82.72%→66.20%; `cagr_pct` itself unaffected (cache-sourced, not window-derived). AGQ node_id=436 regression check (window wider than cached data) came back byte-identical pre/post-fix. Paired-reviewed (independent-cold + contextual Opus, both independently re-derived the numbers), no HIGH/CRITICAL findings. Follow-ups filed, not fixed: `candidate_nodes` stale trade/years/data_start columns for pre-fix rows; `scripts/locate_best_node.py` has the same bug class in a third writer; ~10,810 dead `cliff_addon_cache` rows. Full detail: `deep_backlog.md`.

## [backtest][tooling] Resolved 2026-08-23 (later still) — `candidate_summary_report.py`/`candidate_full_review.py` GT paths never passed `data_source` through, silently evaluating massive-tagged campaigns against yahoo data
Fixed both files: `data_source = "massive" if "-massive" in version else "yahoo"` resolved from the scope's own `version` string (matching `scripts/run_ground_truth_phase1.py`/`scripts/paper_vs_backtest_reconcile.py`'s existing convention), threaded into `build_candidate_report_ground_truth` (both files) and `compute_bh_returns`/`_campaign_years_for_window` (`candidate_full_review.py`). Verified against real ground truth: AGQ node_id=436 now shows 240 trades / 72 addon trades / 3438.62% addon-combined return under the fix (was 138 trades / 47 addon trades / 1180.97% under the silent yahoo default) — exact match to the independently-recomputed massive-sourced trade/addon counts. Core CAGR came back 70.39% vs the dispatch's manually-recomputed 71.40% — a ~1pp gap attributed to the manual verification tool's own methodology (direct `db_cache` reads, not the full GT kernel pipeline), not a data_source symptom, since the trade-count signal (which IS data-source-sensitive) matches exactly. Paired-reviewed (independent-cold + contextual Opus, rebuttal exchange); a separate, out-of-scope `start_date`/`end_date` windowing gap in the same call sites was found during review and filed as a new backlog item rather than fixed here. Full detail: `deep_backlog.md`.

## [backtest] Resolved 2026-08-23 (later still) — `ground_truth_kernel_rebuild.md` Step 4 (alpha removed, CAGR sole GT ranking metric) implemented end to end, paired-reviewed (2 CRITICAL/HIGH findings fixed: prune script + validator still alpha-ranked, new NaN-cagr risk in winner_index). Full detail: `deep_backlog.md`.

## [backtest][tooling] Resolved 2026-08-23 (same night, stale by the time flagged in backlog_cache.md) — `scripts/checklist_v65.py`'s v6 kernel-routing gap already fixed
Commit `63d37de` added `_has_ground_truth_v6()`: refuses/excludes any ticker with real `kernel_version='ground_truth_v6'` backtest_cache rows before the legacy hourly `replay()` touches it. The backlog_cache.md entry describing this as open was written from the earlier GT-dependency-audit finding and never reconciled against the same-night fix — caught and closed this session via direct `git log`/docstring check, not assumed from the backlog text.

## [live-trading][testing] Resolved 2026-08-22 (weekend cleanup) — `pinned_entry_trigger` and `buy_fill_reconciles_correct_node` Grid rows confirmed verified-live (real IWM/soxl_ira proof); FAS canary_market_buy_exit deviation (id=179) explained, same wick-shape non-defect as its FAZ siblings. Full detail: `deep_backlog.md`.

## [live-trading][tax] Resolved 2026-08-21 (evening) — `trading_incident #2` (GDXU wash-sale, permanent loss disallowance) closed; mitigation was already in place (node demoted, checklist item #16 added since); codified/automated wash-sale protection filed as a new, separate backlog item.

## [live-trading] Resolved 2026-08-21 — full same-day UTC/ET timestamp-comparison bug-class chain closed (5 fixes, all paired-reviewed): coverage_events.ts docs + `utc_ts_to_local()` helper (7e2255d), `coverage_ticket_table.py` timing false negative (47faf77), `evening_status.py` event_days classification skew (1472684), `verify_real_trades_vs_kernel.py` staged-window misclassification + `signals_db.py` `wl_id`-backfill migration (final commit). Started from a real SOXL misdiagnosis. Full detail: `deep_backlog.md`.

