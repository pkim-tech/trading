# Backlog — Recently Resolved

## [live-trading][testing] Resolved 2026-08-22 (weekend cleanup) — `pinned_entry_trigger` and `buy_fill_reconciles_correct_node` Grid rows confirmed verified-live (real IWM/soxl_ira proof); FAS canary_market_buy_exit deviation (id=179) explained, same wick-shape non-defect as its FAZ siblings. Full detail: `deep_backlog.md`.

## [live-trading][tax] Resolved 2026-08-21 (evening) — `trading_incident #2` (GDXU wash-sale, permanent loss disallowance) closed; mitigation was already in place (node demoted, checklist item #16 added since); codified/automated wash-sale protection filed as a new, separate backlog item.

## [live-trading] Resolved 2026-08-21 — full same-day UTC/ET timestamp-comparison bug-class chain closed (5 fixes, all paired-reviewed): coverage_events.ts docs + `utc_ts_to_local()` helper (7e2255d), `coverage_ticket_table.py` timing false negative (47faf77), `evening_status.py` event_days classification skew (1472684), `verify_real_trades_vs_kernel.py` staged-window misclassification + `signals_db.py` `wl_id`-backfill migration (final commit). Started from a real SOXL misdiagnosis. Full detail: `deep_backlog.md`.

## [testing][coverage] Resolved 2026-08-20 (night) — 5 Trade-Flow Accountability Grid gaps closed (`coder2`): `automated_buy_execution` success-path logging (paired review caught a real HIGH mode-mislabeling bug — dry_run canary orders would've falsely counted as live proof), `node_automation_pause_button` (already covered, dispatch premise stale), `replace_target_mismatch`/`drought_handoff_precondition_blocked` fake_venue refusal-case legs, `price_discontinuity_ruled_out` event-assertion. Full detail: `deep_backlog.md`.

## [live-trading] Resolved 2026-08-20 (night) — 4 items from a second `planner`-dispatched batch (`coder2`): evening_status Part 3 wired into the daemon's EOD slot + persisted (`divergence_check_log`), FAS/FAZ max_hold_hours 47→48 revert, `add_node`'s `fixed_sl_override` made required (dispatch's "only harness needs a fix" claim was WRONG — caught by paired review + full suite, real blast radius was ~90 test sites + 3 production paths, all fixed), orphaned-addon-leg alert snoozes per-day (not indefinitely — a paired-review-caught safety gap in the first version). Full suite: 1376/1376. Full detail: `deep_backlog.md`.

## [live-trading][testing] Confirmed resolved-by-drift 2026-08-20 (Task #4 dispatch) — `_PENDING_BUY_NODE_KEYS` unification was already done+shipped 2026-08-19 (`ce94cdc`), backlog entry just never closed. `signals_blocks.py`'s Slack BUY-button snapshot sources `db._PENDING_BUY_NODE_KEYS` directly; `tests/test_pending_buy_node_keys_consistency.py` asserts exact set-equality via the real call path. Paired-reviewed at the time. No new work needed — verified directly (grep + `git log` + reran both tests, 2/2 pass).

## [live-trading] Resolved 2026-08-20 — 3 `planner`-dispatched items, each paired-reviewed 2 rounds: `corporate_actions` table + `detect_price_discontinuity` fix (closes the GDXU 2026-03-03/05 false-positive freeze), `auto_fill_detection_enabled`/`node_auto_fill_detection_enabled` default flip False→True (closes the `.env`-additions-bypass watch-item too), early-close market-hours guard (`_market_session_open_now`, closes finding (1) of the incident #13 deferred items). Full detail: `deep_backlog.md`.

## [testing][coverage] Confirmed resolved-by-drift 2026-08-20 (backlog review) — `coverage_check.py`'s unfiltered-`source` gap; was already fixed 2026-08-19 (`34b913b`, Task #7), backlog entry just never closed. Both `_check_coverage_event` and `_last_hit_by_mode` filter `source IS NULL OR source NOT LIKE 'fixture:%'` — verified directly in code.

## [live-trading] Resolved 2026-08-19 (night) — SOXS/ira (wl_id=206) sunset as part of the portfolio reselection; 3 open SOXS-specific backlog items (drought confirm_days=1 uncalibrated, real-vs-kernel divergence watch-item, drought-trade kernel-tooling-gap example) close as moot — node archived 2026-08-20 03:17:24 UTC. `confirm_days=1` was separately validated and rejected outright before the sunset (research: no confirm_days value 1-20 clears the cliff-safety bar, `38dc2e4`). Full detail: `deep_backlog.md`.

## [live-trading][testing] Resolved 2026-08-19 — Task #7: new watch_list_overlay_link table + 2 signals_invariants traceability checks (real live nodes missing candidate/overlay validation links), kept out of run_all()'s loud alert path (paired review HIGH finding) and run standalone/from evening_status.py instead. Backfilled SOXL (REAL_SELECTION) and KORU (NO_REAL_SELECTION, flagged distinctly) against real DB. Full detail: `deep_backlog.md`.

## [live-trading] Resolved 2026-08-19 — Tasks #4/#5/#6: real-time/EOD alerts gated on has_capital_at_stake (canary/paper no longer ride along), narrow reconciliation auto-close (broker 0 shares + confirmed FILLED/CANCELED sl_order_id). Paired review found+fixed 2 HIGH in the auto-close (exit_reason mislabeling an armed position's TRAIL exit as SL; unmarked price-approximation in trade_log) plus a caller bug that could leave a declined auto-close neither closed nor alerted. Full detail: `deep_backlog.md`.

## [live-trading][security] Resolved 2026-08-19 — SL exit redesign: a genuinely resting stop is never actively replaced (was: false SL price read → needless market-replace, the same-day SOXS incident's structural cause). Paired review found+fixed 3 HIGH (bug #4 replace-target-mismatch bypass, `exit_pending` poisoning blocking later exits, `check_sl_order_fills`'s FILLED-only detection gap). Full detail: `deep_backlog.md`.

## [live-trading][security] Resolved 2026-08-18 — drought overlay's `_pct_override` triplet: `_PENDING_BUY_NODE_KEYS` now carries the 3 override columns forward, and `open_position_from_pending`'s drought_overlay branch routes through `open_drought_overlay_position` (was calling `open_position` directly, silently skipping override resolution on the real fill path). Paired-reviewed (2 rounds), regression test verified to fail pre-fix. Full detail: `deep_backlog.md`.

## [live-trading][tooling] Resolved 2026-08-18 — check_intraday_risk_review's 3 remaining noise gaps closed: fixture filter was a no-op against real fixture rows (`_log_pre_action_state_verification` now threads `source`), grouping key now includes `mode`+`node_id` (real live-DB collisions confirmed for both), and a persisted cross-cycle cooldown stops a multi-poll-cycle incident from reposting every ~5min (was: a real 51-window storm would've posted 51 messages). 2-round paired review (independent-cold + contextual, both rounds rebutted). Full detail: `deep_backlog.md`.

## [live-trading][security] Resolved 2026-08-18 — `_fill_dry_run_buy` was opening drought-overlay pending buys as core positions (direct `open_position()` call, no `position_source` dispatch) for 4 real `state='dry_run'`/`drought_overlay_enabled=1` nodes; found during the drought-overlay fix's paired review, now routes through `open_position_from_pending`. Full detail: `deep_backlog.md`.

## [testing][coverage] Resolved 2026-08-18 — coverage_registry.py fixture-source filter: compute_status() now excludes source='fixture:...' rows from counting as Grid proof; caught a real trap before implementing (9,482/9,534 real events have source IS NULL, a naive NOT LIKE filter would have broken the Grid almost entirely). Reviewed, verified pure no-op on today's data (0 fixture rows exist yet), 2 new tests. Full detail: `deep_backlog.md`.

## [live-trading][coverage] Resolved 2026-08-18 — check_intraday_risk_review watermark gap: since_id pagination replaces the plain limit bump (which a review proved didn't actually close the gap), plus message-length caps for both coverage-events and incidents; a second review round found the fix's own watermark still only advanced on concerning events (HIGH, fixed) before shipping. Full detail: `deep_backlog.md`.

## [live-trading][security] Resolved 2026-08-18 — pending_buys.order_id auto-reconciliation (KEY item): row-scoped writes + order fingerprinting fixed 2 HIGH paired-review findings (wl_id cross-contamination between core/drought rows, unfingerprinted match reproducing the GDXU bug shape) before shipping. Full detail: `deep_backlog.md`.

## [live-trading][security] Resolved 2026-08-18 — merged-order addon/core-SL collision: paired review found the SAME CRITICAL bug (force-replace exit orphaning merged leg shares) independently via 2 code paths, plus a HIGH oversell-guard loosening, both fixed before shipping. Full detail: `deep_backlog.md`.

## [live-trading][security] Fixed 2026-08-18 — RETL top-up sizing incident: `starting_notional_override` was missing from both pending-buy node-snapshot tuples (`signals_db._PENDING_BUY_NODE_KEYS`, and a duplicated tuple in `signals_blocks.py`'s BUY button payload), causing a real ~$20/2-share top-up to use a stale fallback target with zero trace; also added a `within_tolerance` coverage_event for fills landing inside `_reconcile_fill`'s tolerance band (previously silent) and fixed a stale docstring. Paired independent-cold + contextual Opus review with rebuttal confirmed all 3 fixes; drought-overlay's identical-shaped (and worse — SL/arm/trail trigger path) twin bug and 2 other items deferred to `backlog_cache.md`. Full detail: `deep_backlog.md`.

## [portfolio][state] Resolved 2026-08-18 — 9 leftover `skim_enabled=1` paper nodes (SOXL id 175-181, AGQ id 182-183, all `ira`) turned off and archived via `signals_db.archive_node()`, cleanup consequence of the 2026-08-14 "trim" reframe that stopped skim work; verified paper-only/no live reader/daemon not running before writing, DB backed up first. State-only, no code change. Full detail: `deep_backlog.md`.

## [live-trading] Fixed 2026-08-17 — the 5 remaining ungated per-position `_post_message` clusters in `signals_notify.py` (add-on leg, drought-HANDOFF, `_reconcile_fill` top-up, `check_gap_resize`, auto-detected-fill notices): 37 call sites gated `node_id=` (+`incident=True` on the 21 error/anomaly ones), 4 deliberately left ungated (`check_gap_resize`'s no-account-on-file alert — its own subject is broken attribution). Paired independent-cold + contextual Opus review with rebuttal found no CRITICAL/HIGH and 4 confirmed real fixes applied: a missing `log_coverage_event` at `check_auto_fills`' SELL loop (broke `should_alert_live`'s unconditional-logging contract), a genuinely ungated `notify_limit_fill`, an add-on-leg "cancelled" message that could claim a never-attempted cancel, and audit-script false positives. New `scripts/audit_post_message_gating.py` + 16 regression tests. Full detail: `deep_backlog.md`.

## [live-trading][tooling] Fixed 2026-08-17 — `check_intraday_risk_review` collapsed 48 raw undifferentiated coverage_event lines down to 9: fixture-source filter, burst grouping (derived from `_RECONCILE_COOLDOWN_SECS`), and dedup against events already covered by their own dedicated alert. Paired-reviewed, 28 tests. Full detail: `deep_backlog.md`.

## [testing][naming] Fixed 2026-08-17 — `coverage_registry.py`'s `fake_venue_proof`/`_scan_fake_venue_proof` renamed to `fake_broker_proof`/`_scan_fake_broker_proof` (accurate — it only ever scanned the `fake_broker` pytest fixture, not the newer `fake_venue/` package). Pure rename, no behavior change. Full detail: `deep_backlog.md`.

## [docs] Fixed 2026-08-17 — CLAUDE.md's capital-at-stake threshold said "$10k default", real code default (`signals_config.py:37`) is $5,000 (lowered 2026-08-13); doc-only fix, no code change.

## [live-trading] Fixed 2026-08-17 — `incident=True`'s alert gate no longer re-resolves CURRENT node/account state: position-keyed alerts anchor to `pos['is_dry_run_sim']` (entry-time truth), order-keyed alerts to the caller's pinned `node` snapshot, and `check_market_buy_rejected`'s three alerts to a `real_order=True` proof-of-real-order flag (needed because `effectively_dry_run` also reads the account's live `trading_enabled`, which nothing freezes into `node_json`). Residuals (a) pinned-account staleness and (b) the missing `db.is_snoozed` lever on `alert_stale_price_exit_suppressed` closed too; (c)'s dead `[DRY RUN]` paths got the requested comments (two of three comment drafts were themselves corrected in rebuttal — the "can never fire" claim isn't an invariant in `check_dry_run_sim_sells`). Paired review (cold + contextual Opus, both rebutted) upgraded the fix twice: the `check_market_buy_rejected` misclassification came from the reviewers, not the original scope, while a `mode_tag_for_position` helper for the alert header was built and then REVERTED when the full suite disproved its premise (`is_dry_run_sim=0` also covers never-real positions on dry-run accounts) — refiled as its own item. New `tests/test_incident_alert_gate.py` (13 tests incl. an AST backstop over all call sites). Full detail: `deep_backlog.md`.

## [live-trading][security] Confirmed resolved 2026-08-17 (backlog audit) — rejected/cancelled market-buy `pending_buys` row termination gap; `check_market_buy_rejected` was already built/shipped this same week, backlog entry was just stale. Full detail: `deep_backlog.md`.

## [tooling][security] Confirmed resolved-by-drift 2026-08-17 (backlog audit) — brokerage-threshold drift-alert gap; `CAPITAL_AT_STAKE_THRESHOLD` moved to $5k, all 3 real `brokerage` nodes ($6k notional) now clear it. Sibling finding (add-on-leg drift bypass) still open, tracked in `backlog_cache.md`. Full detail: `deep_backlog.md`.

## [live-trading][security] Fixed 2026-08-17 — `check_order`'s `replacing_order_id` exemption extended to the `recent_orders` 60s dedup window (`schwab_safety._broker_confirms_order`'s new `exclude_order_id` param); confirmed reachable in production via `handle_trail_buy_fill_price`'s manual-confirmation path (outside poll cadence). Paired review of the dispatched agent's first draft found it too broad (a blanket skip would have removed real duplicate protection on unrelated BUY-side/already-filled-but-unreconciled paths) — corrected to a hybrid: exclude-by-id when a real broker book exists (`trading_enabled`), blanket skip only for dry_run accounts with no book to check. 3 regression tests, one rewritten mid-review after it was found to pin the too-broad behavior. Built via dispatched background agent + paired review, merged manually into the same working tree as the HANDOFF fix (both touch the same `check_order` block). Full detail: `deep_backlog.md`.

## [live-trading][security] Fixed 2026-08-17 — HANDOFF's dup-order-window self-collision closed via a new `is_handoff_exit` exemption (`schwab_safety.check_order`), mirroring `is_addon_leg`'s verified-not-trusted contract; paired independent-cold + contextual Opus review found 4 real issues, all fixed (weakened unit test, precondition-scope gap, dead param on the non-replace path, missing Grid row). Real broker premise (two simultaneous resting SELL orders on the same symbol/account) user-confirmed live on the Schwab web UI. 1202/1202 full suite, 7/7 live_sim_harness. A narrower, still-open sub-gap (HANDOFF exit when its own SL placement itself already failed) documented, not fixed. Full detail: `deep_backlog.md`.

## [testing] Fixed 2026-08-17 — `scripts/live_sim_harness.py`'s stale `open_position_keys` call sites: both used a bare `set()` where `_scan_pinned_entry`/`_scan_buy_signals` now expect a `{'live': ..., 'paper': ...}` dict (`active_signals._position_keys_by_book`, added 2026-08-15). Confirmed the harness's own call sites were stale, not `active_signals.py`. 7/7 scenarios now pass. Full detail: `deep_backlog.md`.

## [data][action] Fixed 2026-08-17 — `data_manager.py`'s split-guard 1-bar-overlap gap: hardcoded `consistent=True` when only one bar overlaps replaced with a real-split-confirmation gate (`signals_helpers.real_split_confirmed_since`, reused from the same-pull artifact fix), closing the spurious-whole-history-rescale risk. 2 new regression tests (`tests/test_split_guard_single_bar_overlap.py`). Full detail: `deep_backlog.md`.

## [testing][live-trading] Fixed 2026-08-17 — `coverage_check.py`'s auto-explain guard now protects `reason_by='streamlit'` (not just `'user'`) deviation reasons from being overwritten; new regression test added. Full detail: `deep_backlog.md`.

## [testing] Fixed 2026-08-17 — `tests/test_entry_abandon_truth_table.py`'s stale `cancel_order` monkeypatch (missing `node_id` param) fixed, 2 previously-failing tests now pass. Full detail: `deep_backlog.md`.

## [testing][security] Fixed 2026-08-16 — `ORPHAN_SWEEP_STATE_PATH` now honors `SCHWAB_STATE_DIR` like every other schwab_safety state file; `fake_venue/isolation.py`'s tripwire loop extended to cover it. Paired review: zero blocking findings (stale docs it left behind, fixed). Full detail: `deep_backlog.md`.

## [testing][live-trading] Fixed 2026-08-16 — `check_entry_abandon`'s `'abandoned'` coverage_event now records `did_cancel` in `detail`; missing `tests/test_fake_venue_entry_abandon_timeout_scenario.py` pytest wrapper also built. Paired review: zero findings. Full detail: `deep_backlog.md`.

## [live-trading][security] Fixed 2026-08-16 — `check_drought_handoff`'s `placed_unconfirmed` exit_pending write now includes `current_price`, closing a real deterministic KeyError in `check_own_sell_fills`. Paired review: zero blocking findings (1 MEDIUM conceded after rebuttal). Full detail: `deep_backlog.md`.

## [live-trading][security] Closed 2026-08-16 (paired-reviewed) — `check_auto_fills`'s buy-side fallback fixed for automated market-buy nodes (was dead code, gated on a flag never set for that population); fake_venue scenario upgraded to prove both fallback paths end-to-end. Full detail: `deep_backlog.md`.

## [live-trading][testing] Closed 2026-08-16 — fake-venue harness Phase 2 Category A queue (30 items), full 3-axis sweep, all built and verified. Full detail: `deep_backlog.md`.

## [live-trading][testing] Fixed 2026-08-15/16 (2 paired-review rounds) — `trading_incidents` id=9: `record_deviation`/`coverage_check.py` auto-explain persistence bug, 7 production rows repaired, ticket closed. Full detail: `deep_backlog.md`.

## [testing] Fixed 2026-08-16 — `tests/test_part3_gap_resize.py` had a real, un-timeout-bounded Schwab network call: 4 of 18 tests exercised the automated-fill branch against a `state='live'`/`account='ira'` fixture without mocking `place_stop_loss`, falling through to real OAuth. Added a default no-op mock to the shared `env` fixture. 18/18 pass. Found while diagnosing an agent stuck on this exact hang.

## [live-trading][security] Closed 2026-08-16 (retroactively reviewed + fixed) — post_fill_topup (908a6f0): 1 HIGH + 5 lower findings fixed across 2 rounds of paired review, incl. catching the FIRST fix's own gate logic being backwards (real premise error, verified independently by both reviewers and the coordinating session). Full detail: `deep_backlog.md`.

## [backtest] Closed 2026-08-16 (negative result) — "CertainEntryTrailingSell" idea: priced correctly at bar-close (not best-possible-in-bar), the result went negative. Dead end, not deferred. Full detail: `research_log.md`/`deep_backlog.md`.

## [live-trading][security] Closed 2026-08-16 (built) — per-`(account, ticker)` lock around the retry-and-confirm order-placement sequence in `schwab_client.py`, proven via a real multi-thread `fake_broker` concurrency test (max_concurrent==1). Full detail: `deep_backlog.md`.

## [live-trading][security] Closed 2026-08-16 (found stale) — `_submit_replace_with_retry` double-replace "reopened, not started" backlog note was itself stale; the real fix landed same-day it was reopened (`0fd8fb6`), confirmed by test. Full detail: `deep_backlog.md`.

## [live-trading] Closed 2026-08-16 (backlog-cleanse, peer-batch catchup) — signal/reminder dry_run visibility + channel separation: structurally moot under capital-at-stake redesign. Full detail: `deep_backlog.md`.

## [live-trading] Closed 2026-08-16 (backlog-cleanse, peer-batch catchup, remove) — v5 watchlist skews long-only / inverse counterparts idea. Full detail: `deep_backlog.md`.

## [backtest][live-trading][new-strategy] Closed 2026-08-16 (backlog-cleanse, peer-batch catchup, superseded) — monthly universe rescreen + v6 momentum-exhaustion idea, and the quarterly-resweep item that superseded it (Phase 1 already spec'd in `design.md`, nothing further needed). Full detail: `deep_backlog.md`.

## [backtest][live-trading] Closed 2026-08-16 (backlog-cleanse, peer-batch catchup, remove) — formalize manual-fill-vs-backtest-assumed-fills pattern idea. Full detail: `deep_backlog.md`.

## [live-trading][security] Closed 2026-08-15 (backlog-cleanse, user's call) — manual-"Filled" SL call ticker-gated but not mode-gated; structurally unreachable today, revisit trigger noted if manual confirmation ever returns. Full detail: `deep_backlog.md`.

## [live-trading] Closed 2026-08-15 (backlog-cleanse batch 4, user's call) — `soxl_ira` `account_type="margin"` mislabel item: real risk resolved by a separate, already-correct mechanism (`get_leveraged_buying_power` clamps add-on sizing to Schwab's real-time `buyingPower`, which for `soxl_ira` equals `cashBalance` exactly, no leverage assumption, verified live 2026-08-12). Worst case is a broker rejection, no real financial exposure. Item removed.

## [live-trading] Closed 2026-08-15 (backlog-cleanse batch 4) — GDXU stays `state='live'` decision record: one-line note with no follow-up action, already fully reflected in the DB. Item removed.

## [live-trading][coverage] Confirmed fully closed 2026-08-15 (backlog-cleanse batch 3) — drought+add-on enabled across all 11 `soxl_ira` nodes (2026-08-07); own text had no open thread. Full detail: `deep_backlog.md`.

## [testing] Confirmed fully closed 2026-08-15 (backlog-cleanse batch 2) — `test_coverage_check.py` fsync fix's backlog entry itself fully described a resolved state (`ed67909`, 67/67 passing); closed out completely rather than left with stale "not yet built" framing.

## [live-trading][coverage] Confirmed fully closed 2026-08-15 (backlog-cleanse batch 2) — unlinked-live-node check's own "only thing left open" (wiring into `evening_status.py`) verified built and live: `part4()` (registered in `PARTS`, actually called). Full detail: `deep_backlog.md`.

## [live-trading][backtest] Confirmed fully closed 2026-08-15 (backlog-cleanse batch 2) — K-1 status generalization, both pieces (`brokerage_only` flag, `check_tax_advantaged_excluded_tickers()` generalization) built and tested, 7 pinned tests, no open threads. Full detail: `deep_backlog.md`.

## [live-trading][coverage] Confirmed fully closed 2026-08-15 (backlog-cleanse batch 2) — script-based test-plan support / check_order guard-rejection staging: 10/10 rows staged and passing, registry extension built, no rework needed. Full detail: `deep_backlog.md`.

## [live-trading][tax] Built 2026-08-15 — unrealized gain/loss calculator (brokerage-only): `get_unrealized_pnl_by_ticker`/`k1_tax.unrealized_forecast`, wired into `evening_status.py` Part 2. Section 1256 MTM-liability question left informational-only, not folded into the real reserve, pending CPA confirmation. 15 new tests, rendered end-to-end against a synthetic scenario. Committed `83326af`. Full detail: `backlog_cache.md`/`design.md`.

## [testing] Fixed 2026-08-15 — `tests/test_coverage_check.py` fsync slowdown: session-scoped schema template on tmpfs. 160s -> ~11-17s under the real default config, 70-85% reduction solo, isolation deliberately regression-tested. Committed `ed67909`. Full detail: `backlog_cache.md`.

## [live-trading] Confirmed committed 2026-08-15 (backlog-cleanse batch 1) — node archive state (`watch_list.archived_at`) was already built+committed (`9c67711`); "BUILT... Not committed" wording was stale. Full detail: `deep_backlog.md`.

## [live-trading][execution] Confirmed resolved 2026-08-15 (backlog-cleanse batch 1) — execution price-drift audit already completed 2026-08-15; HIBL's flagged ~5% drift traced to paper trading, not real execution. Full detail: `research_log.md`/`deep_backlog.md`.

## [meta][process] Confirmed resolved 2026-08-15 (backlog-cleanse batch 1) — background-agent trading-hours gate: CLAUDE.md's "Background-Agent Trading-Hours Rule" section confirmed live, the "not yet added" blocker this entry cited is done. Full detail: `deep_backlog.md`.

## [live-trading][security] Removed 2026-08-15 (backlog-cleanse batch 1) — `schwab_token.json` 08:10-08:32 mutation item was a verbatim duplicate already recorded in this file; stale leftover in the open-items file dropped.

## [live-trading][coverage] Removed 2026-08-15 (confirmed stale by direct code check) — `record_deviation` doesn't refresh `expected_outcome` on rerun item duplicated an already-resolved 2026-08-09 entry; `signals_db.py:2757-2766` confirms both branches already refresh `expected_outcome` on every rerun. Item removed.

## [live-trading][tax] Folded into general portfolio-construction theme 2026-08-15 (user's call) — "run SOXL in both roth and soxl_ira" item removed standalone; technically feasible now that node-level selection works, but not being pursued near-term, part of the broader capital-allocation theme instead.

## [live-trading] Removed 2026-08-15 (user's call) — loss-streak circuit breaker item, an antipattern to the backtest: the backtested returns depend on the strategy staying in the market through loss streaks (mean-reversion captures the recovery/wins that follow), so a circuit breaker that halts trading after N losses would make live behavior diverge from what the backtest actually validated. Item removed.

## [portfolio][live-trading] Closed 2026-08-15 (user's call) — add-on's 100%-margin sizing "2027 problem" disproved: real policy is core positions in `brokerage` don't borrow at all, only the add-on leg does, so core sizing never touches margin and the liquidity/margin-availability constraint this item worried about doesn't apply as feared. Item removed.

## [live-trading][security] Closed 2026-08-15 (user's call) — `_last_sale_recovery` basis-lock: real detection built instead of full auto-recovery. `signals_notify._alert_shares_too_small` fires (has_capital_at_stake-gated, throttled) whenever a real node's computed share count would round to 0 -- the actual lock condition. Real risk profile (only tiny-notional test nodes hit this, real positions stay comfortably sized) means detection is sufficient. 2 new tests. Item removed.

## [portfolio][design] Closed 2026-08-15 (user's call) — skim's core-only equity scope item removed entirely: moot since skim itself was already descoped by the 2026-08-14 "trim" reframing (skimming was stopped as a consequence). Item removed.

## [backtest] Closed 2026-08-15 (user's call) — candidate overlay results (drought/add-on) sensitivity finding removed from backlog; user already aware and accepting the risk.

## [live-trading][coverage] Closed 2026-08-15 (user's call) — `soxl_ira` staged-test node cleanup already done in practice: down to 6-7 nodes with minimal capital requirements. Item removed.

## [portfolio][tax] Closed 2026-08-15 (user's call) — real realized-loss lookup already done manually; the $240k number used throughout tonight's tax-forecast work IS the answer. Item removed.

## [live-trading] Closed 2026-08-15 (user's call) — `same_day_block` 7/8-tickers finding folded into normal candidate-promotion practice, not tracked as a standalone item anymore. Item removed.

## [live-trading] Closed 2026-08-15 (user's call) — resync HIBL/RETL to v5 not needed; these are tuning/test nodes where version labeling doesn't matter, the only real thing that matters is RETL actually executing a trade. Item removed.

## [live-trading][backtest] Removed 2026-08-15 (user's call) — "expand paper trading" standalone entry, confirmed fully superseded by the harness reframe (not just marked superseded). Real answer: the fake-venue harness takes over that role, not more paper nodes. Full detail: `deep_backlog.md`'s harness reframe entry.

Rolling window of resolved backlog items, most recent first, for session-handoff context only.
Every entry here is also permanently recorded in `docs/deep_backlog.md` — this file exists so
`go` doesn't have to re-read the full backlog history, just what's happened in roughly the last
week. **Prune entries older than ~7 days whenever adding a new one** (drop, don't archive —
`deep_backlog.md` already has the permanent record). See `docs/backlog_cache.md`'s header note
for the full maintenance workflow.

## [live-trading][coverage] Confirmed already-closed 2026-08-15 (peer session "planner") — SOXS `ira` phantom-trade item was the same SOXS incident already fully closed (commit `484574e`'s session: root cause corrected, fill-reconciliation + state-consistency safety net + provenance tracking merged). Redundant backlog_cache.md entry removed.

## [live-trading][coverage] Folded 2026-08-15 (user's call) — closed-trade reconciliation item no longer tracked standalone; scope now lives inside the coverage_events write-attribution/data-lineage item. Pointer added there; standalone entry removed.

## [live-trading][security] Resolved 2026-08-15 (relayed via peer session "planner") — `schwab_token.json`'s 08:10-08:32 mutation was a genuine authenticated Schwab API call, not a test-isolation defect; both original suspects cleared with hard evidence. New finding: background subagent tool calls leave zero trace in the Claude Code transcript store — 2 follow-up ideas logged. Full detail: `deep_backlog.md`.

## [testing] Confirmed stale, 2026-08-15 (relayed via peer session "planner") — "2 flaky datetime.now() tests" backlog item was already resolved 2026-08-11; both named tests use a fixed synthetic 2025 date grid, never had a real clock dependency. Real cause was an unscoped pytest-xdist file race (`--dist=loadscope` fix) + a separate cross-file `schwab_safety` state-pollution gap (fixed 2026-08-16 via `conftest.py`'s autouse fixture) — see `deep_backlog.md:534`. 2 full-suite runs: 1105/1105 green both times. Entry removed.

## [live-trading][coverage] Deprioritized 2026-08-15 (relayed via peer session "planner") — Trade-Flow Grid filters + direct test links; `evening_status.py`'s report already covers the visibility need, no UI work planned unless it resurfaces with a real trigger. Full detail: `deep_backlog.md`.

## [live-trading][coverage] Confirmed stale, 2026-08-15 (today) — `_ticker_block` relocation to `signals_blocks.py` was actually redone (comment dates it 2026-08-15, landed in commit `0fd8fb6`) after the "reverted during merge" backlog entry was written; entry never got closed out. No code change needed — `signals_blocks.py:503` owns it, `signals_notify.py` re-imports. Backlog entry removed.

## [live-trading][tooling] Resolved 2026-08-16 — 3 findings from the full-session dual-Opus review fixed: `fast_path_fill_reconciliation`'s new `account_number_unresolved` result added to `bad_results` + fixed from hardcoded `mode="dry_run"` to `"unknown"`; `signals_blocks._ticker_block`'s local `mode_tag` variable renamed to stop shadowing the module-level function. Full detail: `deep_backlog.md`.

## [live-trading] Done 2026-08-15 (still later) — `soxl_ira` roster cleanup: SPY/HIBL/USD/YANG retired to paper, YINN repurposed as the node-disambiguation test pair (wl_id 199+228). Full detail: `deep_backlog.md`.

## [testing][security] Resolved 2026-08-15 — test suite was mutating real `cache/live/schwab_node_breaker_state.json`/`schwab_order_counts.json`; new `tests/conftest.py` autouse fixture isolates all 8 `schwab_safety` state paths for every test. `schwab_ticker_automation.json`'s dirty content traced to a deliberate manual staging script, not a bug; `schwab_token.json` mutation still unexplained (separate). Full detail: `deep_backlog.md`.

## [live-trading][HIGH] Resolved 2026-08-16 — stream fast-path fill reconciliation fixed: raw `AccountNumber`→alias resolution (`schwab_client.resolve_account_alias_from_number`, reuses `_resolve_account_hashes`'s suffix-match, no new lookup table) + `SchwabOrderID` string/int mismatch (reuses `drain_fill_queue`'s existing `_order_id_int`). Proven end-to-end via the fake-venue harness (real parser → real fill → real position, plus a new idempotent-redelivery leg); paired Opus review (independent-cold + contextual) found no bugs; full suite 1105/1105 (9 pre-existing `drain_fill_queue` fake_broker tests updated to push a realistic raw account number instead of a bare alias). Full detail: `deep_backlog.md`.

## [live-trading][tax] Resolved 2026-08-15 — end-of-year tax forecast script built (brokerage-only): `tax_realized_loss_baseline` table + `k1_tax.brokerage_tax_forecast()` netting/reserve math + `evening_status.py` part2 wiring, 15 pinned tests. Full detail: `deep_backlog.md`.

## [tooling] Resolved 2026-08-16 — "ready to clear?" Stop hook redesigned around a marker file (touched by session_cache_update.py) instead of "a commit happened this turn"; fixes both the false-positive and false-negative. Verified via 4 direct pipe-tests. Full detail: `deep_backlog.md`.

## [live-trading][security] Accepted residual risk, decided 2026-08-16 — `force_same_day_block` node-resolution fail-open: verified live (7/7 same_day_block events resolved correctly, 0 mismatches ever), closed not fixed. Full detail: `deep_backlog.md`.

## [live-trading] Accepted residual risk, decided 2026-08-16 — `addon_buying_power_check` flat-1x-vs-leverage-scaled asymmetry: closing rationale is behavioral (exposure stays well under the leverage ceiling), explicit revisit trigger recorded. Full detail: `deep_backlog.md`.

## [live-trading][security] Decided/skipped 2026-08-16 — don't-promote-a-2nd-live-AGQ-sibling guard: skipped, not parked; capital-scaling convention (grow the top node's notional, don't run a 2nd worse-config sibling) makes the guarded scenario structurally unlikely by design. Full detail: `deep_backlog.md`.

## [live-trading] Resolved 2026-08-15 — `_submit_order_with_retry`/`_submit_replace_with_retry` retry-blind gap fixed: broker state re-checked before every retry attempt (and once more after the last one); paired Opus review found and fixed 9 real issues across 2 passes (status/orderType filtering, baseline-orderId matching, final-attempt coverage, a try/else extraction-safety fix, a test regression, 2 new regression tests). Full detail: `deep_backlog.md`.

## [live-trading][coverage] Confirmed already resolved 2026-08-16 (targeted sweep) — `compute_status`'s clean-scenario verified-live gap (already fixed by bd8e35c), SOXS/ira stale pending BUY (confirmed cleared, no reconciliation mismatch). Full detail: `deep_backlog.md`.

## [live-trading][tooling] Resolved 2026-08-16 — `run_loop()` startup block cleanup built; paired review (independent-cold + contextual, both agents) caught the loop-based spec's own real ordering regression before it shipped, fixed via a plain helper called in original order instead. Full detail: `deep_backlog.md`.

## [live-trading][portfolio] Resolved 2026-08-16 — `evening_status.py`'s portfolio-return table gains a per-account breakdown (ticker rows + subtotal per account, `ALL` stays as grand total), closing the last gap on the original portfolio-return-calc item. Full detail: `deep_backlog.md`.

## [live-trading][coverage] Resolved 2026-08-16 — `evening_status.py` Part 3 paper-vs-kernel check gains a snooze mechanism (mirrors coverage_check.py's existing pattern); LABD snoozed 7 days, verified by rendering against real data. Full detail: `deep_backlog.md`.

## [live-trading][coverage] Resolved 2026-08-16 — Canary G retired (`scenario_expectations` 69/70 → `active=0`); 2 stale backlog items pruned (`_ticker_block` paper mislabel, SOXL/ira phantom position — both confirmed fixed by the same origin-column fix, independently by two sessions). No commit yet (uncommitted DB flip + docs edit, next in this session's batch). Full detail: `deep_backlog.md`.

## [live-trading] Resolved 2026-08-16 (commit `0c87bf2`) — session-wrap dual-Opus review of the auto-fill/Stop-Start diff found and fixed 5 real issues (Stop/Start button's incomplete gate check, Manually Close handler's zero validation — a real bugs #54/#63-64 risk via stale Slack scrollback, 2 stale docstrings, a third ungated paper entry path); also found and fixed a real incident along the way — a new test wrote to the actual production `schwab_node_automation.json` due to an isolation gap. Full detail: `deep_backlog.md`.

## [live-trading][coverage] Resolved 2026-08-16 (commit `352f134`) — `build_eod_scenario_review` duplicate canary/control bullets fixed via the Coverage Report's rollup helpers, bonus bug fixed (explained deviation shown as unresolved). **Flag: shipped without knowing a concurrent planning session was holding this item for a dedupe-vs-strip-entirely decision — may need revisiting.** Full detail: `deep_backlog.md`.

## [live-trading][coverage] Resolved 2026-08-16 (commit `435ac54`) — shadow-post Slack block content turned out already resolved by existing `blocks_json` column; 2 real test gaps in that existing coverage found and closed instead. Full detail: `deep_backlog.md`.

## [live-trading] Resolved 2026-08-16 (commits `47f6997`/`85f8187`) — auto-fill broadening + node-scoped Stop/Start Slack buttons merged to main; a real merge-conflict regression (Manually Close dropped for a real held position) caught by the full suite and fixed same day. Full detail: `deep_backlog.md`.

## [portfolio] Resolved 2026-08-1x — put-hedge skipped by explicit user call; relying on skim-and-reserve alone. `collect_options_snapshot.py` daily cron removed (no other consumer).

## [backtest][coverage] Resolved 2026-08-1x — `trade_log`/`paper_trade_log` `wl_id=NULL` backfill applied to real live DB, with a new `added_at` tie-break for a live-track/daily-track ambiguity it surfaced. Full suite 589/1 pre-existing fail.

## [live-trading][tax] Closed 2026-08-15 (backlog-cleanse) — wash-sale/tax analysis precondition now false (brokerage trading_enabled 2026-08-12); verified no cross-account ticker sharing today, no active risk. Full detail: `deep_backlog.md`.

## [portfolio][tax] Closed 2026-08-15 (backlog-cleanse) — AGQ K-1/UBTI finding superseded: its own recommendation (AGQ in brokerage) is exactly what happened.

## [portfolio][design] Closed 2026-08-15 (backlog-cleanse) — skim-and-reserve overlay: skim work stopped for now, consistent with the 2026-08-14 "trim" reframe.

## [live-trading][security] Accepted risk 2026-08-17 — HANDOFF exit_pending reminder-loop gap: user's call ("I'll survive"), no fix planned. Full detail: `deep_backlog.md`.

## [live-trading][security] Resolved 2026-08-17 — one-brokerage-account-per-ticker containment: directional call revised (multi-ticker margin now accepted), node-level shared-capital mechanism confirmed unnecessary (bounded per-node sizing + real-time buying-power check already sufficient). Full detail: `deep_backlog.md`.

## [live-trading] Built 2026-08-18 — account-drift + orphaned-position backstop added to signals_invariants.py, paired-reviewed (1 real MEDIUM found+fixed: orphaned-position blind spot on node deletion). Full detail: `deep_backlog.md`.
