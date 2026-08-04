# Backlog — Recently Resolved

Rolling window of resolved backlog items, most recent first, for session-handoff context only.
Every entry here is also permanently recorded in `docs/deep_backlog.md` — this file exists so
`go` doesn't have to re-read the full backlog history, just what's happened in roughly the last
week. **Prune entries older than ~7 days whenever adding a new one** (drop, don't archive —
`deep_backlog.md` already has the permanent record). See `docs/backlog_cache.md`'s header note
for the full maintenance workflow.

## [live-trading] Resolved 2026-08-03 — HIBL/USD/YANG flipped research→live with planned notional ($2,500/$1,000/$2,500); soxl_ira notional_cap raised 800→3000 to match
Caught by `signals_invariants.py` before it could bite live — old $800 cap would've structurally blocked every entry for the resized nodes. Independent Opus review found 2 real follow-on points left open by user's call. Full detail: `docs/deep_backlog.md`'s 2026-08-03 entry.

## [backtest] Resolved 2026-08-03 — SOXS added to real v5 backtest_cache; TrailingExitZScoreBreakout fixed_sl=2 is the cliff-safe winner (73 trades, 11.0% win rate, +113.0% alpha)
`TrailingBothZScoreBreakout` has no cliff-safe node at any fixed_sl (an earlier mid-sweep read was from an incomplete Phase1-only pass). Full detail: `docs/deep_backlog.md`'s 2026-08-03 entry.

## [backtest] Resolved 2026-08-03 — paired-ticker/idle-capital strategy paradigm refuted with real cross-checked evidence (90-pair matrix, mean~1.0, no consistent edge); framework abandoned
Individual v5 nodes' own edges unaffected — only the combination mechanism was refuted. Full detail: `docs/research_log.md`'s 2026-08-03 entry, `docs/deep_backlog.md`'s pointer entry.

## [backtest] Resolved 2026-08-03 — FFT cycle detection to inform z-score window selection: negative result
No significant periodicity found in any of the 10 v5 watchlist tickers' return series (permutation-null p=0.076-0.837, all above 0.05); no correlation with the empirically-chosen `window` (10 vs 20). Full detail: `docs/research_log.md`'s 2026-08-03 entry, `docs/deep_backlog.md`'s pointer entry. The regime-detection half of the original idea is re-opened separately in `docs/backlog_cache.md`.

## [live-trading][security] Closed 2026-08-02 (stale, not new work) — `live_sanity_check.py`'s oversized-BUY/naked-SELL tests were actually run 2026-07-23; and the related "is an oversell test constructible" question resolved to "no, structurally guaranteed by account mechanics"
Both were mistakenly still open in `backlog_cache.md`. Full detail: `docs/deep_backlog.md`'s two entries (search "closed 2026-08-02").

## [live-trading][security] Resolved 2026-08-02 — existing-position BUY guard closes the real double-buy gap confirmed 2026-07-24
Full detail: `docs/deep_backlog.md`'s entry. `check_order` now blocks a 2nd real BUY when a position already exists for (ticker, account), unless `is_protective` (top-up). Ticker+account-keyed (not node-keyed) is a documented, currently-latent limitation. Full suite: 507 passed.

## [live-trading][coverage] Resolved 2026-08-02 — TRAIL-exit reminder spam fixed: routine "still resting" alert suppressed until reminder #3 (~45min), only the arm-time ping and eventual fill/escalation alerts remain
Full detail: `docs/deep_backlog.md`'s entry. Cold Opus review found+fixed a HIGH gap (could've gone silent up to ~17.75h near/outside the 9-16 reminder window) plus 2 lower issues before landing.

## [backtest][data] Resolved 2026-08-02 — `backtest_cache` pruned to island-only (65GB → 256MB, 493,720 of 167.5M rows kept); original moved aside, not deleted
Full detail: `docs/deep_backlog.md`'s 2026-08-02 entry. Integrity-checked, spot-checked against pre-prune numbers.

## [live-trading][security] Resolved 2026-08-02 — broker_stop_price clearing (already fixed 2026-08-01) + Skip now cancels a real resting FRESH exit order, but never the standing TRAIL protection
Full detail: `docs/deep_backlog.md`'s 2026-08-02 entry. Paired review caught a HIGH regression in the first draft (would've cancelled a position's only protection); rewritten + tested. Full suite: 499 passed.

## [live-trading][security] Resolved 2026-08-01 (session-wrap review) — final whole-diff Opus review of all 5 session pieces together found 2 real cross-piece bugs
Full detail: `docs/deep_backlog.md`'s entry. HIGH: `broker_stop_price` (piece 1) went live but was never cleared on replace, a real alert-accuracy regression — fixed + tested. MEDIUM: `SIM_MODE` fail-safe (piece 4) was disabled by any library import of `active_signals` — fixed via `__name__ == '__main__'` gating.

## [live-trading][security] Resolved 2026-08-01 — real live-trading bug found via the Accountability Grid: post-fill top-up BUYs were blocked 100% of the time outside signal windows
Full detail: `docs/deep_backlog.md`'s entry. `is_protective` now exempts the signal-window gate, matching `is_gap_correction`'s existing exemption. 2 confirmed real failures on file (RETL, LABD).

## [backtest] Resolved 2026-08-01 (compounding-drag item) — independent Opus challenge of the 2026-08-01 research tangent, then a redo + user correction on the drag finding
Bear-market/regime items from the same challenge are still open — see `docs/backlog_cache.md`. Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry.

## [live-trading][security] Resolved 2026-08-01 (evening) — real incident: an ad hoc test call posted a real Slack message; SIM_MODE flipped to a fail-safe default
Full detail: `docs/deep_backlog.md`'s 2026-08-01 (evening) entry / `CLAUDE.md`'s Live Trading section. Real daemon launch command unchanged — `active_signals.py` now forces SIM_MODE=0 for itself before `signals_config` is even imported.

## [live-trading][coverage] Resolved 2026-08-01 — reconciliation_mismatch broken out per-node (20 rows, was 1 global)
Full detail: `docs/deep_backlog.md`'s 2026-08-01 entry. Also found+fixed a real regression while validating: the EOD Slack report would have posted 20 lines/day instead of 1; grouped into a summary line.

## [live-trading][security] Resolved (retroactively confirmed 2026-08-01) — 2026-07-23 night soxl_ira live-order testing findings, all closed within days
Full detail: `docs/deep_backlog.md`'s 2026-08-01 entry. Async-confirmation gap was fixed 2026-07-24 (`fda9b2a`); left marked `Open` for a week past its actual resolution.

## [live-trading][coverage] Resolved 2026-08-01 — GDXU TRAIL-exit alert wording: TP/TRAIL/TIME sell alerts no longer claim "Cancel Stop Loss order" for a position already managed by a resting automated exit order
Full detail: `docs/deep_backlog.md`'s 2026-08-01 (evening) entry. 2 review rounds (Opus) found and fixed a HIGH gap (order_id presence isn't proof of a resting order) and 2 MEDIUM gaps before landing.

## [live-trading][security] Resolved 2026-08-01 (late) — `handle_entry_price` never auto-placed a protective stop for automation-scoped market-buy fills; new paired independent+contextual review pattern adopted
Full detail: `docs/deep_backlog.md`'s 2026-08-01 (late) entry.

## [live-trading][testing] Resolved 2026-08-01 — fake_broker coverage pushed 6/41 → 37/41 tracked branches with a real regression test
Full detail: commit `f3b9bab`, `docs/deep_backlog.md`'s 2026-08-01 entry.

## [live-trading][coverage] Resolved 2026-08-01 — JDST re-paired with JNUG as a same-underlying bull/bear pair (new `canary_bull_bear_pair` scenario), not VOO's E-scenario mirror
Full detail: commit `f3b9bab`, `docs/deep_backlog.md`'s 2026-08-01 entry.

## [live-trading] Superseded 2026-08-01 — GDXD's old $5k-pilot-node role has no live successor plan; DPST now fills the "small real-money live volunteer" slot instead
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry.

## [live-trading][coverage] Partially resolved 2026-08-01 — the 07-27 canary_* duplicate-row concern was already inert; cleaned up
Full detail: `docs/deep_backlog.md`'s 2026-08-01 entry. Root cause: a `mode` column added mid-migration (07-25) broke the dedup key; the old rows were already `active=0` and excluded everywhere — never a live bug. 1 follow-up still open.

## [live-trading] Resolved (confirmed stale 2026-08-01) — paper trading is currently fully dormant system-wide
Resolved by the 2026-07-25 mode-flip back to `research`; verified 2026-08-01 (32 closed trades, 6 open positions, genuinely active).

## [live-trading] Resolved 2026-08-01 (evening) — SL Slack alert falls back to a generic "should have auto-filled" guess; split into known/automation-pending/dry-run/manual code paths
Full detail: `docs/deep_backlog.md`'s 2026-08-01 (evening) entry. Opus review of v1 caught 2 real bugs (dry-run false-alarm, a write-suppression gap), both fixed before commit. Full suite: 473 passed.

## [live-trading] Resolved 2026-07-26, confirmed stale 2026-08-01 — Morning Report block-limit failure regressed (23 nodes now)
Resolved via real chunking (`_post_chunked`), not the ticker-trim originally called for — confirmed present in code.

## [live-trading][security] Resolved 2026-07-31 (session wrap) — a 3rd independent review of the complete production diff found 3 more real issues before commit, all fixed
Full detail: `docs/deep_backlog.md`'s same-day entry (top).

## [live-trading][security] Resolved 2026-07-31 — full exit/arm/entry execution-path audit (9 bugs) + same-day follow-up pass (8 more) + a same-day independent review of that follow-up (8 more, 1 real-money)
Full detail: `docs/deep_backlog.md`'s three 2026-07-31 entries (top).

## [live-trading][docs] Resolved 2026-07-31 — `enable_node_auto_fill_detection(node_id)`'s docstring corrected. Full detail: `docs/deep_backlog.md`'s 2026-07-30 (evening) entry.

## [live-trading][security] Resolved 2026-07-29 — all 3 deferred design items from 2026-07-28 (night) now built: `tests/fake_broker.py`, `schwab_safety._log_pre_action_state_verification` (detection-only), `schwab_safety.record_node_streak` (monitor-only)
Full detail: `docs/deep_backlog.md`'s 2026-07-29 entries (top two).

## [live-trading][security] Resolved 2026-07-28 (night) — resting-order dup guards self-blocked their own replace calls; SH's automated exit was stuck for 4 real days
Full detail: `docs/deep_backlog.md`'s 2026-07-28 (night) entry (top).

## [live-trading][security] Resolved 2026-07-27 (night, 2nd session) — `at_bar_close` bookkeeping bug caused false near-instant SL exits
Full detail: `docs/deep_backlog.md`'s 2026-07-27 (night, 2nd session) entry (top).

## [live-trading][security] Resolved 2026-07-27 (night) — GDXU stale-fill incident fixed (order_id-exact matching everywhere); automated TP/SL/TIME exits built; cancel+place replaced with atomic `replace_order`; 6 canary nodes restored after accidental deletion
Full detail: `docs/deep_backlog.md`'s 2026-07-27 (night) entry (top).

## [live-trading][security] Resolved 2026-07-27 — watchlist 65 live-mode nodes reduced to SPY/SH/GDXU(#108)/DPST; 11 other live nodes deleted; 2 dry_run_sim positions retroactively closed first
Full detail: `docs/deep_backlog.md`'s 2026-07-27 entry.

## [live-trading][security] Resolved 2026-07-27 — SPY's real pending trailing-stop exit placed for real (order id 1007336072974, resting, `soxl_ira`)
Full detail: `docs/deep_backlog.md`'s 2026-07-27 entry.

## [live-trading][coverage] Resolved 2026-07-28 (evening) — coverage snoozes built (time-bounded acknowledgment for a known noisy scenario); UDOW's stale test position retroactively cleaned up
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry.

## [live-trading][coverage] Resolved 2026-07-28 (later) — daily coverage report ("like pytest, but with the market") now runs inside the live daemon at 7am, checking the previous trading day
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry.

## [live-trading][coverage] Resolved 2026-07-28 — closed 12 of 13 remaining `not-instrumented` rows in the accountability grid (38 rows, down from 39)
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry.

## [live-trading][coverage] Resolved 2026-07-27 evening — widened the accountability grid from 32 to 39 rows, closing 5 real execution-logic gaps
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry.

## [live-trading][coverage] Resolved 2026-07-27 — `coverage_deviations` rows are now permanent record ("ticket model"), never deleted
Full detail: `docs/deep_backlog.md`'s 2026-07-27 entry.

## [live-trading][security] Resolved 2026-07-25/26 — wl_id-keyed refactor implemented, reviewed (2 Opus rounds), landed
Full detail: `docs/deep_backlog.md`'s 2026-07-25/26 entry.
