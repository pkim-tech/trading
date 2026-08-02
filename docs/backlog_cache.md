# Backlog Cache

> **Reminder (to Claude, added 2026-07-27)**: keep entries here to 1-2 lines — a one-line pointer
> to `docs/deep_backlog.md`'s full-detail entry, plus a second line only for something genuinely
> still-open. The full incident writeup (root cause, fix, review findings, test counts) belongs in
> `deep_backlog.md`, not here, and NOT inline in `CLAUDE.md` either — CLAUDE.md should stay a
> living reference, not an ever-growing changelog. Caught live after this got violated twice in
> one sitting (once here, once in CLAUDE.md) before being fixed.

## [live-trading][security] Resolved 2026-08-01 (session-wrap review) — final whole-diff Opus review of all 5 session pieces together found 2 real cross-piece bugs
Full detail: `docs/deep_backlog.md`'s entry. HIGH: `broker_stop_price` (piece 1) went live but was never cleared on replace, a real alert-accuracy regression — fixed + tested. MEDIUM: `SIM_MODE` fail-safe (piece 4) was disabled by any library import of `active_signals` — fixed via `__name__ == '__main__'` gating.

## [live-trading][security] Resolved 2026-08-01 — real live-trading bug found via the Accountability Grid: post-fill top-up BUYs were blocked 100% of the time outside signal windows
Full detail: `docs/deep_backlog.md`'s entry. `is_protective` now exempts the signal-window gate, matching `is_gap_correction`'s existing exemption. 2 confirmed real failures on file (RETL, LABD).

## [live-trading][coverage] Open, raised 2026-08-01 — 14 Accountability Grid rows still wired-never-fired; 3 flagged suspicious, not yet investigated
Full detail: `docs/deep_backlog.md`'s entry. `automated_sell_mode_skip`/`fast_path_fill_reconciliation`/`manual_buy_confirmation_account` should plausibly have fired by now — natural next-session starting point.

## [live-trading][security] Resolved 2026-08-01 (evening) — real incident: an ad hoc test call posted a real Slack message; SIM_MODE flipped to a fail-safe default
Full detail: `docs/deep_backlog.md`'s 2026-08-01 (evening) entry / `CLAUDE.md`'s Live Trading section. Real daemon launch command unchanged — `active_signals.py` now forces SIM_MODE=0 for itself before `signals_config` is even imported.

## [live-trading][coverage] Resolved 2026-08-01 — reconciliation_mismatch broken out per-node (20 rows, was 1 global)
Full detail: `docs/deep_backlog.md`'s 2026-08-01 entry. Also found+fixed a real regression while validating: the EOD Slack report would have posted 20 lines/day instead of 1; grouped into a summary line.

## [backtest][live-trading] Open, raised 2026-08-01 — formalize/write down the pattern separating manual-fill execution reality from automated/backtest-assumed fills
Currently scattered across `docs/operational_limits.md`'s Phase 1/2 marker and `sim_chaos_monkey.py`; not yet scoped where it should actually live.

## [live-trading][security] Resolved (retroactively confirmed 2026-08-01) — 2026-07-23 night soxl_ira live-order testing findings, all closed within days
Full detail: `docs/deep_backlog.md`'s 2026-08-01 entry. Async-confirmation gap was fixed 2026-07-24 (`fda9b2a`); left marked `Open` for a week past its actual resolution.

## [live-trading][coverage] Resolved 2026-08-01 — GDXU TRAIL-exit alert wording: TP/TRAIL/TIME sell alerts no longer claim "Cancel Stop Loss order" for a position already managed by a resting automated exit order
Full detail: `docs/deep_backlog.md`'s 2026-08-01 (evening) entry. 2 review rounds (Opus) found and fixed a HIGH gap (order_id presence isn't proof of a resting order) and 2 MEDIUM gaps before landing.

## [live-trading] Open, raised 2026-08-01 — broker_stop_price never cleared after a replace; Skip abandons tracking of a real resting order
Both deferred from the alert-wording fix above (touches fragile `_attempt_automated_sell`/`_attempt_automated_exit_sell`). Full detail: `docs/deep_backlog.md`'s 2026-08-01 entry.

## [live-trading][security] Resolved 2026-08-01 (late) — `handle_entry_price` never auto-placed a protective stop for automation-scoped market-buy fills; new paired independent+contextual review pattern adopted
Full detail: `docs/deep_backlog.md`'s 2026-08-01 (late) entry.

## [backtest] Resolved 2026-08-01 (compounding-drag item) / Open (bear-market + regime items) — independent Opus challenge of the 2026-08-01 research tangent, then a redo + user correction on the drag finding
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Open, raised 2026-08-01 — HIBL/USD/YANG pilot nodes (154/155/156, $60-200 starting_notional) skip 71-87% of signal-window trades for lack of an affordable share
The real cost of a skipped trade is a missed real broker order-flow event toward closing the `wired-never-fired` coverage gaps (see `docs/deep_backlog.md`'s 2026-08-01 entry) — the only thing these 3 nodes exist to produce.

## [live-trading][security] Resolved 2026-07-31 (session wrap) — a 3rd independent review of the complete production diff found 3 more real issues before commit, all fixed
Full detail: `docs/deep_backlog.md`'s same-day entry (top).

## [live-trading][testing] Resolved 2026-08-01 — fake_broker coverage pushed 6/41 → 37/41 tracked branches with a real regression test
Full detail: commit `f3b9bab`, `docs/deep_backlog.md`'s 2026-08-01 entry.

## [live-trading][security] Resolved 2026-07-31 — full exit/arm/entry execution-path audit (9 bugs) + same-day follow-up pass (8 more) + a same-day independent review of that follow-up (8 more, 1 real-money)
Full detail: `docs/deep_backlog.md`'s three 2026-07-31 entries (top).

## [live-trading][docs] Resolved 2026-07-31 — `enable_node_auto_fill_detection(node_id)`'s docstring corrected (dropped the false "also sets the ticker-level flag" claim). Full detail: `docs/deep_backlog.md`'s 2026-07-30 (evening) entry.

## [live-trading][coverage] Open, raised 2026-07-30 — should canary scenario_expectations tests appear on the Trade-Flow Accountability Grid?
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Resolved 2026-07-29 — all 3 deferred design items from 2026-07-28 (night) now built: stateful fake order-book test fixture (`tests/fake_broker.py`), pre-action live-state verification (`schwab_safety._log_pre_action_state_verification`, detection-only), and a node-level circuit breaker (`schwab_safety.record_node_streak`, monitor-only). Full detail: `docs/deep_backlog.md`'s 2026-07-29 entries (top two).

## [live-trading][coverage] Resolved 2026-08-01 — JDST re-paired with JNUG as a same-underlying bull/bear pair (new `canary_bull_bear_pair` scenario), not VOO's E-scenario mirror
Full detail: commit `f3b9bab`, `docs/deep_backlog.md`'s 2026-08-01 entry.

## [live-trading] Superseded 2026-08-01 — GDXD's old $5k-pilot-node role has no live successor plan; DPST now fills the "small real-money live volunteer" slot instead
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Resolved 2026-07-28 (night) — resting-order dup guards self-blocked their own replace calls; SH's automated exit was stuck for 4 real days. Full detail: `docs/deep_backlog.md`'s 2026-07-28 (night) entry (top).

## [live-trading][coverage] Partially resolved 2026-08-01 — the 07-27 canary_* duplicate-row concern was already inert; cleaned up
Full detail: `docs/deep_backlog.md`'s 2026-08-01 entry. Root cause: a `mode` column added mid-migration (07-25) broke the dedup key; the old rows were already `active=0` and excluded everywhere — never a live bug. 1 follow-up still open (see entry).

## [live-trading][coverage] Open, raised 2026-07-28 — two live-alert wording/staleness bugs found reviewing GDXU's TRAIL-exit test, not fixed yet
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Resolved 2026-07-27 (night, 2nd session) — `at_bar_close` bookkeeping bug caused false near-instant SL exits (paper trading, and unfixed in real `_scan_pinned_exit_arm` until a review round caught it). Full detail: `docs/deep_backlog.md`'s 2026-07-27 (night, 2nd session) entry (top).

## [live-trading][security] Resolved 2026-07-27 (night) — GDXU stale-fill incident fixed (order_id-exact matching everywhere, replaces the fuzzy "most recent fill" hazard); automated TP/SL/TIME exits built; cancel+place replaced with atomic `replace_order`; 6 canary nodes restored after accidental deletion. Full detail: `docs/deep_backlog.md`'s 2026-07-27 (night) entry (top).

## [live-trading][security] Accepted residual risk, 2026-07-27 — `_submit_replace_with_retry`'s retry can fire a second `replace_order` against an already-replaced order_id
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Resolved 2026-07-26 (evening) — BUY-signal duplicate-alert fix: `pending_wl_ids` now actually enforced + new broker-truth check (`_real_order_or_position_exists`) before firing a fresh live BUY alert
Full detail: `docs/deep_backlog.md`'s 2026-07-27 (later) entry.

## [live-trading] Partially resolved 2026-07-27 (later) — `.env`'s `SCHWAB_AUTOMATION_TICKERS` trimmed from 29 to the 12 tickers on watchlist 65. Research universe/`trading_universe.db` scope not touched — still open if that broader trim is wanted.
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Resolved 2026-07-27 — watchlist 65 live-mode nodes reduced to SPY/SH/GDXU(#108)/DPST; 11 other live nodes deleted (not demoted), 2 dry_run_sim positions (VOO/IVV) retroactively closed first. Full detail: `docs/deep_backlog.md`'s 2026-07-27 entry.

## [live-trading][security] Resolved 2026-07-27 — SPY's real pending trailing-stop exit placed for real (order id 1007336072974, resting `AWAITING_STOP_CONDITION`, `soxl_ira`). Full detail: `docs/deep_backlog.md`'s 2026-07-27 entry.

## [live-trading] Open, raised 2026-07-26 (evening), staged 2026-07-27 — 3 of 3 planned real-order tests now progressing; daemon currently down, blocking the remaining 2 from resolving
Full detail: `docs/deep_backlog.md`'s 2026-07-27 entry.

## [live-trading][security] Resolved 2026-07-26 — NYSE trading-day gate built (`pandas_market_calendars`), guarding both daemon scan paths and `schwab_safety.check_order` itself; a retry added to the fix introduced and then closed its own HIGH duplicate-BUY bug, caught by review before reaching the daemon. Full detail: `docs/deep_backlog.md`'s 2026-07-26 entry (top).

## [live-trading][coverage] Resolved 2026-07-26 — re-triaged the 22 `wired-never-fired` coverage-grid rows instead of building a stub-broker harness; 20/22 already had (or now have) real offline test proof, only 2 genuinely need a real order
Grew out of the "deliberate trading-day-gate-style simulation" idea (raised earlier same session, see `docs/deep_backlog.md`'s 2026-07-26 entry for the full split/re-triage writeup) — an Opus design review found that most `wired-never-fired` rows are **policy-internal** (decided entirely inside our own code, no real broker round-trip needed to prove correct) rather than broker-interacting, so a stub-broker harness was the wrong tool for most of them.

## [live-trading][security] Resolved 2026-07-26 — ERY phantom-fill incident cleaned up; new `trading_incidents` ticket log; every Slack alert now tags `(account · LIVE/DRY-RUN)`. Full detail: `docs/deep_backlog.md`'s 2026-07-26 entry (top).

## [live-trading][security] Resolved 2026-07-26 — `auto_fill_detection_enabled` was ticker-only-keyed (a gap the wl_id refactor missed); now AND-gated on ticker + node (`wl_id`), defaulting closed. Opus review found+fixed a stale-button crash on old Slack messages + dead-code cleanup; an orphaned-node (`wl_id=NULL`) lockout and loss of one-tap bulk-disable were documented/deferred ("maybe later"). Full detail: `docs/deep_backlog.md`'s 2026-07-26 entry (top). Full suite: 291 passed. Harness: 7/7.

## [live-trading][coverage] Resolved 2026-07-26 — Morning Report/signal-window alerts hit Slack's 50-block limit again (25 nodes); replaced per-row shrinking with real chunking + threading (`_post_chunked`); Trade-Flow Accountability Grid redesigned with independent Paper/Dry-run/Live columns instead of one collapsed status. Full detail: `docs/deep_backlog.md`'s 2026-07-26 entries (both, at the end of the file).

## [live-trading][coverage] Resolved 2026-07-28 (evening) — coverage snoozes built (time-bounded acknowledgment for a known noisy scenario); UDOW's stale test position retroactively cleaned up
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][coverage] Resolved 2026-07-28 (later) — daily coverage report ("like pytest, but with the market") now runs inside the live daemon at 7am, checking the previous trading day
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][coverage] Resolved 2026-07-28 — closed 12 of 13 remaining `not-instrumented` rows in the accountability grid (38 rows, down from 39 — one dead row removed)
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][coverage] Resolved 2026-07-27 evening — widened the accountability grid from 32 to 39 rows, closing 5 real execution-logic gaps (not just guard logic)
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][coverage] Resolved 2026-07-27 — `coverage_deviations` rows are now permanent record ("ticket model"), never deleted; `record_deviation` fixed to not let a system auto-resolve mask a new real deviation. Full detail: `docs/deep_backlog.md`'s 2026-07-27 entry.

## [live-trading][coverage] Open, raised 2026-07-27 — Trade-Flow Test Accountability Grid (`pages/14_Coverage.py`, backed by `scripts/coverage_registry.py`) needs filters + direct links to underlying tests
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][coverage] Resolved 2026-07-26 — `live_sim_harness.py` gained `scenario_dry_run_sim_cycle`, closing a coverage gap in testing the dry_run fill-synthesis logic itself (neither `live_sim.py`'s REPL nor the unit tests drove it end-to-end through the real daemon wiring). Full detail: `docs/deep_backlog.md`'s 2026-07-26 entry (top).

## [live-trading][coverage] Resolved 2026-07-26 — dry_run fill synthesis built: a dry_run account's trailing/market-buy order now closes the loop against real price data instead of stalling forever. Full detail: `docs/deep_backlog.md`'s 2026-07-26 entry.

## [live-trading][security] Resolved 2026-07-26 — `signals_invariants.py` built: startup + pre-commit config-invariant checks, 4 checks live. Full detail: `CLAUDE.md`'s Key Files entry.
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Resolved 2026-07-26 — "run both" real+paper restructure dropped, two-node pattern already covers it. Full detail: `docs/deep_backlog.md`'s 2026-07-26 entry.

## [live-trading][security] Resolved 2026-07-26 — session-wrap Opus review found+fixed an orphan-table forward hazard in the watch_list rebuild migration. Full detail: `docs/deep_backlog.md`'s 2026-07-26 entry.

## [live-trading][security] Resolved 2026-07-26 — Opus review of paper_alert_verbose/account-dedup diff: HIGH duplicate-node idempotency bug in 2 live-setup scripts, plus a rebuild forward-hazard and a stale-snapshot alert gate, all fixed. Full detail: `docs/deep_backlog.md`'s 2026-07-26 entry.

## [live-trading][security] Resolved 2026-07-25/26 — wl_id-keyed refactor implemented, reviewed (2 Opus rounds), landed. Full detail: `docs/deep_backlog.md`'s 2026-07-25/26 entry.

## [live-trading][security] Idea, raised 2026-07-26, explicitly gated on the wl_id refactor above landing and being observed correct first — per-node `dry_run` override, additive/OR-logic only, never replacing the account-level flag
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Resolved 2026-07-25 — second Opus review round on the daily_order_cap change: 1 stale-docstring fix, 1 plausible tradeoff accepted (cap bump reduces a de-facto cumulative BUY-notional bound). Full detail: `docs/deep_backlog.md`'s 2026-07-25 entry.

## [live-trading][coverage] Resolved 2026-07-25 — pytest was polluting the real `coverage_events` table via a missing `isolated_db` fixture; fixed, 360 polluted rows cleaned up. Full detail: `docs/deep_backlog.md`'s 2026-07-25 entry.

## [live-trading][compliance] Open, raised 2026-07-25 (3rd time this has come up in conversation, not previously backlogged) — FINRA eliminated the PDT rule/$25k threshold in 2026; re-evaluate daily_order_cap's purpose and confirm Schwab's rollout status
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest][live-trading] Research idea, raised 2026-07-25 — monthly universe rescreen (recurring cadence) + a possible "v6" momentum-exhaustion-bounce strategy variant
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Resolved 2026-07-25 — coverage-system "compass" v2: node_id/mode identity migration + six-round Opus review chain. Full detail: `docs/deep_backlog.md`'s 2026-07-25 entry.

## [live-trading][security] Latent, found by Opus review round 6, 2026-07-25 -- stale-pending-buys guard could silently discard a real manual fill confirmation if a ticker is ever outside SCHWAB_AUTOMATION_TICKERS
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Deferred, not currently reachable, found by Opus review 2026-07-24 evening — manual-"Filled" SL call is ticker-gated but not mode-gated
Add a `node.get('mode', 'live') == 'live'` guard alongside the ticker check if/when that routing changes — see `docs/deep_backlog.md`'s 2026-07-24 evening batch-fix entry for full context.

## [live-trading][coverage] Resolved 2026-07-26 — dry_run fill synthesis built (see the 2026-07-26 resolved entry above), closing this gap: dry_run does NOT auto-complete without it, which is exactly why the synthesis exists.
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][coverage] Minor, found 2026-07-24 ~evening via Opus review — record_deviation doesn't refresh expected_outcome on rerun
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Design note, raised 2026-07-24 ~16:20 ET — paper trading and dry_run test genuinely different things, not redundant with each other
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Idea, raised 2026-07-24 ~15:05 ET — open a new margin account strictly dedicated to one real production ticker; keep soxl_ira as the standing multi-ticker test account
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][tax] Idea, raised 2026-07-24 ~15:10 ET — run SOXL in both `roth` and `soxl_ira`, but `roth` needs to become limited-margin first
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Resolved 2026-07-24 evening — `daily_order_cap` no longer starves SL-placement/top-up. Full detail: `docs/deep_backlog.md`'s 2026-07-24 evening batch-fix entry.

## [live-trading][security] Resolved 2026-07-24 evening — `notional_cap` now BUY-only, real position-size check added for SELL. Full detail: `docs/deep_backlog.md`'s 2026-07-24 evening batch-fix entry.

## [live-trading] Resolved 2026-07-25 — coverage-system reframe complete: Streamlit dashboard, strategy_type axis, structured expected-vs-actual, drill-down, Slack-callable report (all 7 pieces). Full detail: `docs/deep_backlog.md`'s 2026-07-25 entry.

## [live-trading][security] Resolved 2026-07-24 evening — `last_seen_bar` now seeded from real current bar at startup. Full detail: `docs/deep_backlog.md`'s 2026-07-24 evening batch-fix entry. Residual: `sell_alerted`/`window_alerted`/`limit_fill_alerted` still restart-unsafe, deliberately not fixed (no clean persisted-state reconstruction) — see that entry.

## [live-trading] Open, raised 2026-07-24 ~10:55 ET — retry `check_gap_resize`'s cancel+replace test properly pre-market on a future day, not mid-day
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Open, found 2026-07-24 ~10:35 ET — real BUY/SELL Slack alerts carry no canary tag, unlike the Reference Report or paper-trading's console tag
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][backtest] Resolved 2026-07-24 evening — `buy_alerted` now unlocks after a genuine same-day close (real or paper), gated against resting pending buys. Full detail: `docs/deep_backlog.md`'s 2026-07-24 evening batch-fix entry.

## [live-trading] Resolved (confirmed stale 2026-08-01) — paper trading is currently fully dormant system-wide
Full detail: `docs/deep_backlog.md`'s entry. Resolved by the 2026-07-25 mode-flip back to `research`; verified 2026-08-01 (32 closed trades, 6 open positions, genuinely active).

## [live-trading] Far-backlog, raised 2026-07-25, deprioritized same day — v5 watchlist skews long-only, consider adding inverse counterparts
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Resolved 2026-07-24 evening — `_trailing_buy_status` now anchors to the real `signal_price` trigger instead of a cache-derived one. Full detail: `docs/deep_backlog.md`'s 2026-07-24 evening batch-fix entry.

## [live-trading] Resolved 2026-07-24 evening — `TrailingBothZScoreBreakout` fills now get a real automated stop-loss (both auto-fill and manual-Filled paths). Full detail: `docs/deep_backlog.md`'s 2026-07-24 evening batch-fix entry. Live verification still planned for Monday 2026-07-27 per the original plan.

## [live-trading] Resolved 2026-08-01 (evening) — SL Slack alert falls back to a generic "should have auto-filled" guess; split into known/automation-pending/dry-run/manual code paths
Full detail: `docs/deep_backlog.md`'s 2026-08-01 (evening) entry. Opus review of v1 caught 2 real bugs (dry-run false-alarm, a write-suppression gap), both fixed before commit. Full suite: 473 passed.

## [live-trading][security] Resolved 2026-07-24 evening — `add_node`'s NULL-unsafe dedup fixed (explicit check-then-skip, includes `arm_sell_pct`/trail axes), existing duplicate rows cleaned up. Full detail: `docs/deep_backlog.md`'s 2026-07-24 evening batch-fix entry.

## [live-trading][security] Elevated priority 2026-07-24 morning — real evidence that neither notional_cap nor the cash check would catch a same-account double-buy
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Idea, raised 2026-07-24 morning — permanent canary tickers in the dormant `ira` account as a standing delayed-regression test
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Resolved 2026-07-26, confirmed stale 2026-08-01 — Morning Report block-limit failure regressed (23 nodes now)
Full detail: `docs/deep_backlog.md`'s entry. Resolved via real chunking (`_post_chunked`), not the ticker-trim originally called for — confirmed present in code.

## [live-trading] Open, raised 2026-07-24 morning — signal/reminder alerts don't show dry_run status, only real order-placement messages do
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Open, raised 2026-07-24 morning, CORRECTED 2026-07-24 ~10:15 ET — split paper-trading/dry-run/live notifications into separate channels (or otherwise reduce chattiness)
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Open, raised by session-wrap Opus review 2026-07-23 night — `availableFunds` is leverage-inclusive for a real margin account, unverified whether that's safe for `brokerage`
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Open, planning in progress 2026-07-23 night — Friday 2026-07-24 real-account test plan on the new limited-margin IRA
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Open, raised 2026-07-23 night — Schwab auth flow is "clunky", no cleaner unattended path designed yet
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Superseded 2026-07-23 night — "revert live-fire dry-run test state" (see item above)
Full incident detail still in `docs/deep_backlog.md`'s 2026-07-23 "Live-fire dry-run test state left in place" entry.

## [live-trading] Resolved 2026-07-23 — Schwab OAuth `interactive=True` default hung the daemon (main client + stream thread), both fixed; alert-spam gap also closed. Full detail: `docs/deep_backlog.md`'s 2026-07-23 entry.

## [live-trading][security] Resolved 2026-07-22 — stale-cache race at market open (HIBL paper trade entered and SL'd in 31 seconds); `_current_price()` now rejects cache older than today at market open. Full writeup: `docs/research_log.md`'s 2026-07-22 "HIBL paper trade" entry.
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Resolved 2026-07-22/23 — canary watchlist nodes for daily paper-trading proof-of-life, all six built and live. Full design writeup: `docs/deep_backlog.md`'s 2026-07-23 entry.

## [live-trading][security] Resolved 2026-07-23 — Morning Report silently rendered empty for weeks (mode filter), then broke outright once fixed (Slack block-limit); both fixed. Full incident writeup: `docs/research_log.md`'s 2026-07-23 entry.
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Resolved 2026-07-23 — two logging/observability gaps fixed: `active_signals.log` wasn't flushing; `slack_message_log` recorded intent not delivery (now has a real `error` column). Full detail: `docs/deep_backlog.md`'s 2026-07-23 entry.

## [live-trading] Resolved 2026-07-23 — `scripts/live_sim_harness.py` built (non-interactive coverage harness), wired into `session wrap`. Full detail: `docs/deep_backlog.md`'s 2026-07-23 entry.

## [backtest] Open, paused 2026-07-22 — "v6" idle-capital parking idea; inconclusive, downturn-specific follow-up queued
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Resolved 2026-07-23 — coverage_events fully wired (stale "~13 remaining" note corrected, `daemon_section_exception` added). Full detail: `docs/deep_backlog.md`'s 2026-07-23 entry.

## [live-trading][security] Planned for Friday (2026-07-24 WFH day) — real-account sanity tests: oversized BUY + naked SELL across several tickers, on the new limited-margin IRA only
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Idea, raised 2026-07-22, not designed — small (10-share) real EDC pilot position, held ~1 month to shake out issues before scaling
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][tax] Open question, raised 2026-07-22 — which ticker (SOXL vs AGQ) goes to the taxable brokerage account first; wash-sale cross-account mechanic needs one more confirmation
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Not started, discussed at length 2026-07-22 — max cumulative BUY notional per ticker per day (backstop against a repeat-buy/runaway bug)
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Low priority, idea raised 2026-07-21 — should `CASH_SAFETY_BUFFER` scale with order size instead of staying a flat $200?
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Resolved 2026-07-21 — account cash/buying-power check built (`get_account_balance` + `check_order` wiring, quantity-aware, fail-closed). Full detail: `docs/deep_backlog.md`'s 2026-07-21 entry.

## [live-trading][security] Resolved 2026-07-21 — cash-balance network call moved inside `approve_and_record`'s cross-account file lock, closing a TOCTOU race. Full detail: `docs/deep_backlog.md`'s 2026-07-21 entry.

## [live-trading][security] Resolved 2026-07-21 — pre-existing test hygiene gap: some `schwab_safety` tests were silently hitting the real Schwab API; fixed. Full detail: `docs/deep_backlog.md`'s 2026-07-21 entry.

## [live-trading][security] Resolved 2026-07-21 — `active_signals.run_loop` fault tolerance built: per-section isolation + outer last-resort net, so one section's exception can't kill the daemon. Full detail: `docs/deep_backlog.md`'s 2026-07-21 entry.

## [live-trading][security] Resolved 2026-07-22 — `schwab_safety`'s duplicate-order guard now confirms against Schwab's real order book, not just a local pre-flight record. Full detail: `docs/deep_backlog.md`'s 2026-07-22 entry.

## [live-trading][security] Resolved 2026-07-22 — CRITICAL: trailing-arm state clobber caused re-arming and duplicate live trailing-sell orders (oversell risk); found by a full-stack Opus review, fixed. Full detail: `docs/deep_backlog.md`'s 2026-07-22 entry.

## [live-trading][security] Resolved 2026-07-22 — live-state reconciliation check built: detection + text-only proposed remediation, never auto-executes. Full detail: `docs/deep_backlog.md`'s 2026-07-22 entry.

## [live-trading] Implemented 2026-07-21, not yet live-tested — Entry Trigger/Fill/SL-Placement/Arm-latency automation for TrailingExitZScoreBreakout (Part 4)
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Implemented 2026-07-21, not yet live-tested — trailing-buy budget adherence (Part 3: padded sizing + overnight gap guard + post-fill top-up)
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][backtest] Resolved 2026-07-21 — SOXL's watchlist-65 node stays TrailingBoth
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][tax] Active hold, set 2026-07-20 — don't buy GDXU/AGQ in any IRA-type account before their wash-sale clearance dates
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Resolved 2026-07-22 — `same_day_block` is now account-type-aware (cash vs. margin/limited-margin IRA). Full detail: `docs/deep_backlog.md`'s 2026-07-22 entry.

## [backtest] Resolved 2026-07-20 — last-window MOC vs trailing-buy; MOC does not win, see `docs/deep_backlog.md`/`docs/research_log.md`

## [backtest] Research idea, not started, 2026-07-20 — is overnight gap frequency/magnitude asymmetric (up-gap vs down-gap)?
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest] Resolved 2026-07-20 — full 18-ticker v5 resweep completed; see `docs/deep_backlog.md` and `docs/research_log.md`

## [live-trading][backtest] Resolved 2026-07-20 — watchlist 65 candidate testing complete, found+fixed 2 real bugs, see `docs/deep_backlog.md`/`docs/research_log.md`

## [live-trading] Resolved 2026-07-19 — `AUTOMATION_ENABLED_TICKERS` moved to `.env`, widened to all 18 v4 tickers; EDC's v3.27 node removed from `watch_list`. Full writeup: `docs/design.md` (Layer 3).
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Resolved 2026-07-21 — SELL-side automated-order attempt is now mode-gated, not just ticker-gated. Full detail: `docs/deep_backlog.md`'s 2026-07-21 entry.

## [live-trading] Low priority, 2026-07-19 — paper-trading dedup is ticker-only, not `(ticker, window)`-aware
Full detail in `docs/deep_backlog.md`.

## [live-trading] Resolved 2026-07-18 — GDXD paper-trading layer built, `add_node` fixed_sl bug fixed. Full writeup: `docs/deep_backlog.md`'s 2026-07-18 entry.

## [backtest] Resolved 2026-07-18 — 5 tickers with a negative walk-forward fold (DPST, NUGT, RETL, UDOW, UVIX) sent to research, no per-ticker investigation; DPST flipped live→research. Full detail: `docs/deep_backlog.md`'s 2026-07-18 entry.

## [live-trading][security] Phase 4 (deferred to cloud-infrastructure planning), 2026-07-18 — move order-placement/mutating Schwab calls behind a separate proxy this session can't write to
Full detail in `docs/deep_backlog.md`.

## [backtest] Resolved 2026-07-18 — `signals_db.add_node`'s `fixed_sl` computation ignored the real per-node value for `uses_fixed_sl` strategies; fixed via `fixed_sl_override`. Full detail: `docs/deep_backlog.md`'s 2026-07-18 entry.

## [live-trading][security] High priority, active focus as of 2026-07-18 — one brokerage account per live ticker, for blast-radius containment against a rogue algorithm
Full detail in `docs/deep_backlog.md`.

## [live-trading][tax] Deprioritized 2026-07-18, 2026-07-17 — wash-sale/tax analysis needed before promoting any ticker into the taxable brokerage account
Full detail moved to `docs/deep_backlog.md` ("Deprioritized 2026-07-18" entry).

## [live-trading][security] Resolved 2026-07-17 — same-day buy→sell block explored and deliberately NOT built
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][backtest] High priority (raised 2026-07-18) — dividend cash isn't credited into P&L/SL/arm tracking; material for DPST specifically
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest][live-trading] High priority, found+partially fixed 2026-07-19 — gap-through-trigger fill optimism: neither the backtest kernel nor live sizing ever modeled overnight gaps past trail_buy_pct
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] High priority, confirmed 2026-07-18 — trailing-buy order needs re-sizing as the trigger price moves, to actually use all budgeted capital
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest][live-trading] High priority, 2026-07-16 — trailing-buy sizing formula spends money that isn't guaranteed to be there; backtest's compounding formula assumes it too
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest] Resolved 2026-07-22 — split-guard/`auto_adjust` reconciliation closed; GDXD numbers verified clean; data traceability chosen over full immutability. Full writeup: `docs/research_log.md`'s 2026-07-22 entry.
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest] Medium priority, designed-not-built 2026-07-22 — data mutation log (traceability, not full immutability/versioning) for historical price cache rescales
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest] High priority, 2026-07-16 — execution-adherence robustness ("chaos monkey"), distinct from island/robust-alpha
## ✅ Resolved 2026-07-17 — `entry_timing=open_check` live-actionable analog built; see `docs/deep_backlog.md`

## [backtest] Idea, not scoped, 2026-07-15 — eventually delete v3.x once v4 is a confirmed superset
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest] Medium priority, 2026-07-15 (revised) — v4 sweep disk footprint: 11-ticker watchlist is fine, full 53-ticker universe is not
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest] Resolved 2026-07-15 — Phase 3 (full mesh) adds no value in every campaign tested so far
## ✅ Resolved 2026-07-15/19/20 — trailing-buy fill logic kernel-correctness fix, executed in full; see `docs/deep_backlog.md`

## [live-trading] Mostly resolved 2026-07-15 — corporate-action (stock split) defense
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Resolved (was stale) 2026-07-15 — HIBL trailing-buy order
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] In progress, wiring done 2026-07-17 — Schwab API automation, dry-run cutover not started
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest] Medium priority, 2026-07-10 — same-bar arm/take-profit trigger not checked at entry
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest] Open question, 2026-07-09 (rescoped 2026-07-13, buffer question resolved 2026-07-14) — fixed_sl=15% itself still needs a real sweep
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest] High priority, 2026-07-16 — need a proper delayed-entry simulation for the same-day-re-buy constraint, not just a trade-list filter
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest] Resolved 2026-07-14 — trailing-buy re-entry timing after a same-day exit
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [execution] Research question, 2026-07-15 — when would TWAP/VWAP order execution become worth considering
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest] Idea, not scoped, 2026-07-18 — daily-bar strategy variant, for much longer backtest history / real bear-market regime coverage
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Open question, 2026-08-01 — is a production-path oversell/rejection test even constructible?
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Idea, not built, 2026-08-01 — real correlation-verification logic for the new JNUG/JDST "G" canary pair
Today's fix (docs/deep_backlog.md's 2026-08-01 entry) only restored their real config/labels/scenario_expectations row (`canary_bull_bear_pair`) -- monitored the same simple same-day-trade-happened way as every other canary.

## [backtest] Idea, not scoped, 2026-08-01 — FFT-based cycle detection to inform z-score window selection / regime structure
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [data] Idea, not scoped, 2026-08-01 — start recording our own 1-minute bars now, so a future "we need historical data" request never hits an expired retention window again
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest][data] In progress, 2026-08-01 (evening) — prune `backtest_cache` to island-only, uniformly, for every ticker/version; execution started, not yet completed
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).
