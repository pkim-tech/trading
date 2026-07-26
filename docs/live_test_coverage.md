# Live Test Coverage Ledger

Standing record of every scenario/code-path in the live-trading automation engine that needs
to be exercised against the *real* live daemon (not just an offline replay or unit test) —
see `automation_principles.md` #10. A row only moves to Verified once actually observed
succeeding in a real live run, with a date and what was observed. Passing in isolation
(unit test, replay script) is a prerequisite, not a substitute — track that separately in the
"Offline coverage" column.

Status values: **Not started** (no live observation, may not even be built) / **Pending**
(built, `dry_run=True`, waiting for a live window to exercise it) / **Verified** (observed
live, date + note).

**2026-07-24: daily canary designed-scenarios are now structured data, not prose** —
`signals_db.scenario_expectations` (the designed-scenario mapping, e.g. the six canary
lifecycles previously only described in `deep_backlog.md`'s 2026-07-23 entry) +
`coverage_deviations` (one row per day a `daily`-frequency expectation wasn't met, `reason`
starting `NULL` until explained via `explain_deviation`). `scripts/coverage_check.py` runs the
daily expected-vs-actual check and surfaces every still-unexplained deviation — this is the
"compass" from the 2026-07-24 reframe (`docs/backlog_cache.md`): an unexplained deviation is a
bug by definition, not an acceptable end state. Seed/extend scenarios via
`scripts/seed_scenario_expectations.py`. This is separate from (and a layer above)
`coverage_events`/`coverage_matrix.py` below, which tracks raw control-site firings, not
designed-scenario completion.

**2026-07-22: most rows below now also have a queryable backing** — `signals_db.coverage_events`
(new table) is logged at ~18 real control sites across `schwab_safety.check_order`,
`signals_notify.py`, and `paper_trading.py` (`scenario_key`, `mode` ∈ paper/dry_run/live, ticker,
result, detail, timestamp). `scripts/coverage_matrix.py` pivots it (rows=scenario_key,
columns=paper/dry_run/live) so "has X actually fired, when, with what result" is answerable by
query instead of by re-reading this file's prose. This doesn't replace the table below (which
also tracks scenarios that aren't logging-instrumented yet, e.g. open-price-quality) — treat
`coverage_matrix.py` as the live/authoritative answer for any row it does cover, and update this
file's Status column to Verified once a query confirms a real (non-paper, ideally non-dry_run)
observation, same as before.

**2026-07-27: `scripts/coverage_registry.py` + `pages/14_Coverage.py`'s "Trade-Flow Test
Accountability Grid" is now the live-computed version of this table** — same 32 logic branches,
but `Status` is derived from a real query against `coverage_events`/`coverage_deviations` every
page load, never hand-typed, so it can't silently go stale the way this file's `Status` column
did (caught stale 2026-07-25, see the note below). Treat the Streamlit grid as authoritative for
current status; this file's prose/`Code path`/`Offline coverage` columns are still the richer
narrative reference, but don't trust its `Status` column over the grid's live computation.
Raised same session, not yet built: filters, direct links to the underlying tests, and row
grouping by feature area (see `docs/backlog_cache.md`).

**2026-07-28: closed 12 of the 13 remaining `not-instrumented` rows** (grid now 38, was 39 — one
dead row removed). 10 new real `log_coverage_event` sites: `automated_sell_mode_skip`
(`signals_notify._attempt_automated_sell` mode-mismatch guard), `manual_sl_fallback_alert` (same
function's post-cancel-failure UNPROTECTED alert), `exit_arm_latency`
(`active_signals._scan_pinned_exit_arm`), `node_level_automation_pause` and
`two_nodes_same_ticker_diff_accounts` (`schwab_safety.check_order`), `stale_buy_button_guard` and
`buy_buttons_resolve_correct_node` (`signals_handlers.handle_entry_price`/
`handle_trail_buy_fill_price`), `manual_buy_confirmation_account` (all 3 BUY-confirmation
handlers), `buy_fill_reconciles_correct_node` (`signals_notify._reconcile_buy_fill`'s wl_id
disambiguation, only fires when 2+ nodes are genuinely pending for the same ticker). 1 free row:
`oversell_guard_correct_position` wired to `schwab_safety.py`'s already-logged
`sell_exceeds_position_blocked` event (built 2026-07-24, never had a registry row). 1 row
(`open_price_quality`) wired to its own pre-existing `open_price_quality_log` table (92 real rows
since 2026-07-22) via a new `compute_status` mechanism, rather than duplicating into
`coverage_events` — it's now `verified-live`. Removed `live_state_reconciliation_design`, a dead
row explicitly superseded by the already-built `live_state_reconciliation_mismatch`.
**Deliberately left uninstrumented, user's call**: `position_lock` — proving contention passively
would require changing `open_position`/`close_position` from `with _position_lock` to an explicit
non-blocking-then-blocking acquire, a real behavior change to live-trading-critical dedup code, not
a side-channel log addition like the other 12. Session-wrap Opus review of the diff found zero
confirmed bugs; one nit fixed same session — `manual_buy_confirmation_account`'s `no_account` rows
now log `mode="unattributed"` (matching `gap_resize`'s existing precedent for this exact case)
instead of `_coverage_mode`'s misleading `"dry_run"` fallback. Full suite: 257 passed (unchanged
count). Harness: 7/7. `signals_invariants.py`: 1 known accepted violation (UDOW), unchanged.
`verify_trailing_buy_resolution.py`/`verify_trailing_sell_resolution.py` (AGQ,SOXL) both clean, no
new mismatch — only a side-channel log call was added to `active_signals.py`.

**2026-07-28 (evening): `coverage_snoozes` built — a time-bounded human acknowledgment for a known
scenario, replacing raw event volume as the only way to deal with a noisy known condition.** Grew
directly out of discussing `live_state_reconciliation_mismatch`'s 1753 real events for UDOW's
deliberately-seeded stale test position — noise on top of a known/accepted issue, not a new
finding each time. New `signals_db.coverage_snoozes` table (`scenario_key` + nullable
`ticker`/`account`/`node_id`/`kind` wildcard scoping, `snoozed_until`, `reason`) +
`snooze_coverage()`/`is_snoozed()`/`get_active_snoozes()`, wired into
`signals_notify._alert_reconcile_mismatch` — suppresses both the Slack alert and the
`coverage_events` log while active (not just the alert), and auto-expires rather than silencing a
scenario forever. New `scripts/snooze_coverage.py` CLI. **`scripts/coverage_check.py`'s `run_check`
also skips (not deviates) a snoozed scenario** — otherwise `reconciliation_mismatch`, the sole
`DAILY_EXPECTED_IDS` row, would mint a false unexplained `coverage_deviations` ticket every day
it's snoozed.
**Two Opus review rounds (first-pass + session-wrap) found and fixed 4 CONFIRMED bugs**: (1)
`is_snoozed`/`get_active_snoozes` compared `snoozed_until` (written in local ET time by the CLI)
against SQLite's UTC `datetime('now')` — same bug class as the daily-report's UTC/localtime trap
above — fixed to `datetime('now','localtime')`. (2) the `run_check` skip check as originally
written would have minted the false deviation ticket described above; fixed by checking
`get_active_snoozes` before calling the real checker. (3) **round 2 caught a snooze had no `kind`
scope** — `_alert_reconcile_mismatch` fires for 3 distinct kinds (`shares`/`missing_sl`/
`missing_trailing_sell`), and a bare ticker-scoped snooze (the documented UDOW use case) would
have also silenced `missing_sl`/`missing_trailing_sell` — "this position may be unprotected at the
broker," a materially more severe alert than the share-count drift actually being acknowledged;
fixed by adding a nullable `kind` column, threaded through the same wildcard-match pattern. (4)
`scripts/snooze_coverage.py` never called `db.ensure_tables()`, so it would crash against any DB
predating this feature. Also fixed. 8 new regression tests. Full suite: 278 passed (was 269).
Harness: 7/7. `signals_invariants.py`: clean (0 violations — see below, not the usual "1 known
accepted UDOW violation").
**Separately, same session: UDOW's stale test position itself was retroactively cleaned up**, not
just made snoozable — `open_positions` id 16 (`ira`, 740sh, opened 2026-07-23, predates the
2026-07-26 dry-run-fill-synthesis feature so was never tagged `is_dry_run_sim`) was manually backed
up then tagged `is_dry_run_sim=1` on both `open_positions`/`trade_log` and closed via
`signals_db.close_position()` at a real current market price ($68.17), `exit_reason=
'DRY_RUN_RETROACTIVE_CLEANUP'`. `signals_invariants.py`'s previously-accepted UDOW violation
(research-mode ticker with a real open position still automation-scoped) is now genuinely
resolved, not just accepted — confirmed clean. UDOW's `research`-mode node (id 93) is unblocked to
run purely through paper trading going forward.

**2026-07-28 (later): daily coverage report now runs automatically inside the live daemon** — a
new `coverage_event` check method (`scripts/coverage_check.py::_check_coverage_event`) extends
the existing `scenario_expectations`/`coverage_deviations` "ticket" contract (previously only the
6 canary `trade_lifecycle` scenarios) to the accountability grid's `coverage_events`-backed rows.
`active_signals.py`'s existing 7:00am `_REFERENCE_TIMES` slot now also calls
`send_coverage_report(previous_trading_day)` — the user's framing: "like pytest, but with the
market" — a daily go/no-go gate before the trading day starts, checking the prior trading day's
results, with any real regression becoming a sticky ticket that stays open until a human explains
it or a later day's genuine pass auto-resolves it (same contract as the existing sticky-deviation
model, never silent). **Two rounds of Opus review** (first-pass + session-wrap) found and fixed 4
CONFIRMED bugs total: (1) the new checker compared `date(ts)` (UTC, SQLite default) against a
local-ET-computed check_date, offsetting the daily window by ~4-5h — confirmed against real data
(212 of 1799 existing `coverage_events` rows fell on the wrong side); fixed to
`date(ts, 'localtime')`. (2) the 7:00 slot's per-loop gating meant a daemon restart any time after
7am (the normal case, since this project restarts after most source edits) would silently skip
that day's check forever — no ticket, no alert, indistinguishable from a clean day; fixed by
adding an unconditional startup call mirroring the existing reference-report pattern. (3) the
initial seed (`scripts/seed_daily_coverage_expectations.py`) marked all 14 accountability-grid
`coverage_events` rows `expected_frequency='daily'`, but replaying real history showed 12 of them
are trade-conditional (zero-or-near-zero all-time events) and would have minted 11-14 sticky
tickets every single day, burying the one real signal; fixed by trimming
`DAILY_EXPECTED_IDS` down. (4) **round 2 caught that `cash_check` (one of the two rows fix-3 kept
as "daily")  was itself trade-conditional** — it fired on only 1 of 3 real instrumented days,
since it's logged only inside a real BUY-attempt branch — so it was demoted to `occasional` too,
leaving just `live_state_reconciliation_mismatch` (itself caveated: its "daily" reliability
currently depends entirely on UDOW's known accepted stale-position violation persisting — flagged
in the seed script for revisit when that's cleaned up) as the sole `DAILY_EXPECTED_IDS` row. Also
fixed same round: `_check_coverage_event` didn't scope by ticker/node_id (same bug class already
fixed once for `_check_trade_lifecycle`'s node_id disambiguation) — zero live impact today since
every seeded row has ticker=node_id=None, but fixed defensively before any per-ticker scenario is
seeded. Full suite: 269 passed (was 257). Harness: 7/7. `signals_invariants.py`: 1 known accepted
violation (UDOW), unchanged. `verify_trailing_buy_resolution.py`/`verify_trailing_sell_resolution.py`
(AGQ,SOXL) both clean.

**2026-07-27 evening: grid widened from 32 to 39 rows** after the user flagged it was guard-heavy
and thin on execution logic. 2 rows (`paper_entry_fill`/`paper_exit_fill`) needed no new
instrumentation — `paper_trading.py` already logs `entry_fill`/`exit_fill` under `mode='paper'`,
they'd simply never gotten a registry row. 5 more needed a real `log_coverage_event` call added at
a previously-uninstrumented site: `kill_switch_block` (`schwab_safety.check_order`),
`automated_sell_execution` (`signals_notify._attempt_automated_sell`), `time_exit_trigger`
(`signals_notify.notify_sell_signal`, `reason=='TIME'` branch), `buy_fill_reconciled`
(`signals_notify._reconcile_buy_fill`, the fill/sizing math itself, distinct from the existing
node-identity-disambiguation row), and `morning_report_delivery` (`signals_notify.
send_reference_report`, whether the report actually posts, not just gets built — this broke
silently for weeks once already, 2026-07-23, with nothing tracking delivery). 5 new regression
tests added, full suite 257 passed (was 250), harness 7/7.

**2026-07-25: reconciled against real `coverage_matrix.py` output** (in prep for the Monday
2026-07-27 unattended `soxl_ira` window) — several rows below were stale ("Not started" despite a
real event already existing). 6 rows updated with real evidence: SL-sync placement and top-up both
show a real pre-fix `daily_order_cap` block (LABU, 2026-07-24, now fixed), the SL async-fallback
timeout fired for real (VOO dry_run), the cash-check passing path + real balance fetch are now
Verified, live-state-reconciliation detection is now Verified (8 real `soxl_ira` mismatches,
2026-07-24), and the trailing-arm re-read fix is now Verified live (SPY). Also found+fixed in the
same pass: `tests/test_run_loop_fault_tolerance.py` was polluting the real `coverage_events` table
with fake `daemon_section_exception` rows (360 of them, since fixed and cleaned up — see
`docs/backlog_cache.md`'s 2026-07-25 entry) — a reminder that `coverage_matrix.py` output should be
sanity-checked for test-pollution signatures (implausible volume, generic "boom"-style detail
text) before trusting a count as real coverage.

| Scenario | Code path | Offline coverage | Status | Notes |
|---|---|---|---|---|
| Pinned entry trigger fires at the right bar/price | `active_signals._scan_pinned_entry` | `scripts/verify_pinned_entry_vs_backtest.py` (5/6 tickers clean, AGQ's 1 mismatch explained); `scripts/live_sim_harness.py::scenario_pinned_entry_trailing_buy` (2026-07-23) | Pending | Needs a live trading day with the daemon actually running this code |
| Real market-order BUY placement + fill confirm | `_attempt_automated_market_buy`, `_sync_confirm_and_protect` | Unit tests (`test_part4_entry_trigger.py`); `scripts/live_sim_harness.py::scenario_ambient_market_buy_entry` (2026-07-23, full chain end-to-end incl. SL placement) | Not started | No real (non-dry_run) order ever placed by this system |
| SL placed at signal price after fill (sync path) | `_place_stop_loss_for_position` via sync confirm | Unit test (SL anchors to `signal_price`) | Not started | **Real attempt observed live 2026-07-24** (`coverage_matrix.py sl_placement`: LABU, `blocked`, "account 'soxl_ira' has hit its daily order cap (3)") — this is the pre-fix `daily_order_cap` starvation bug in action, not a successful placement. Now fixed (2026-07-25 SELL-exempt + cap bump); still needs a real successful placement observed post-fix |
| SL placed via async fallback (timeout path) | `check_auto_fills`/`drain_fill_queue`/`check_gap_resize` fill poll | Unit test only | Pending | The timeout branch itself fired for real (`coverage_matrix.py sl_placement_fast_confirm_timeout`: VOO, dry_run, `timed_out`, 2026-07-24) — confirms the fast-confirm timeout is reachable live; still doesn't confirm the fallback SL placement that follows actually succeeds, and never observed in a real (non-dry_run) account |
| Post-fill top-up places a real order | `_reconcile_fill` | Unit test (`test_part3_gap_resize.py`); `scripts/live_sim_harness.py::scenario_reconcile_fill_topup` (2026-07-23, forced shortfall) | Not started | **Real attempt observed live 2026-07-24** (`coverage_matrix.py top_up`: LABU, `blocked`, same `daily_order_cap` bug as the SL row above, same incident/timestamp) — real-money risk still open until a real successful top-up is observed post-fix |
| Overnight gap-resize (cancel trailing buy, replace w/ market) | `signals_notify.check_gap_resize`, `_GAP_CHECK_WINDOW` | Unit tests; `scripts/live_sim_harness.py::scenario_gap_resize` (2026-07-23, asserts the real replacement order's ticker/shares/price via a spy, not just the Slack text) | Not started | Needs a real overnight gap past `trail_buy_pct` while daemon is live |
| Exit-arm latency scan (pinned) | `_scan_pinned_exit_arm` | `scripts/live_sim_harness.py::scenario_pinned_exit_arm` (2026-07-23, also regression-covers the 2026-07-22 trail_state clobber bug) | Not started | |
| Trailing-buy/-stop fill resolution parity vs backtest kernel | kernel + live sizing | `verify_trailing_buy_resolution.py`/`verify_trailing_sell_resolution.py` | N/A (offline-only by design) | Rerun after any kernel/signals_notify/schwab_safety change — already a standing habit, not a live-daemon test |
| Open-price quality (real open vs. what live code captured) | `_scan_pinned_entry` logging → `open_price_quality_log` | `scripts/verify_open_price_quality.py` (script exists, no data yet) | Pending | Deliverable 2 — needs real trading-day data, can't be backfilled |
| Cash-balance check blocks an order correctly | `schwab_client.get_account_balance` + `schwab_safety.check_order` | 8 unit tests (`test_schwab_safety.py`), Opus-reviewed 2026-07-21 | Pending | **The passing path is Verified live 2026-07-24** (`coverage_matrix.py cash_check`: 4x live incl. LABU/ERY with real dollar balances e.g. "available=$1,109", 11x dry_run — confirms the real balance fetch and `cashAvailableForTrading` field name both work against a real account response, resolving that open question). The *blocking* case (insufficient funds) has never actually been observed — every real event so far shows `result=passed` |
| Second-live-ticker-in-one-account BUY correctly blocked | `schwab_safety._has_open_buy_order_in_account` | 3 unit tests (`test_schwab_safety.py`) | Not started | Not reachable today (one ticker per account) — will matter once the one-account-per-ticker rollout or any account-sharing change lands |
| Daemon survives an unhandled exception mid-loop | `active_signals._guarded` + outer try/except in `run_loop` | 7 unit tests (`test_run_loop_fault_tolerance.py`) exercise `_guarded` directly, incl. the `daemon_section_exception` `coverage_events` row (2026-07-23) | Not started | `_guarded` failures are now queryable via `scripts/coverage_matrix.py --scenario daemon_section_exception` once the real daemon logs one — still no test that runs a real (or simulated) `run_loop` iteration end-to-end with a failing section, only the isolated helper is tested |
| Duplicate-order guard doesn't false-block a legitimate top-up | `schwab_safety` quantity-aware guard | Unit tests (`test_schwab_safety.py`) | Not started | Only exercised in unit tests so far, not against real order timing |
| Live-state reconciliation (position/order-book mismatch detection) | not built | N/A | Not started | Still just a design idea in backlog |
| Automated sell correctly skipped for a non-live-mode node's position | `signals_notify._attempt_automated_sell` mode check | 2 new unit tests (`test_schwab_automation.py`) | Not started | Built this session (2026-07-21); no ticker has ever hit this exact scenario live (today's automation-scope tickers only run mode='live' nodes) |
| Live-state reconciliation detects and alerts on a real mismatch | `signals_notify.check_live_state_reconciliation`, `schwab_client.get_real_position` | 8 unit tests (`test_live_state_reconciliation.py`) | Verified | **2026-07-24, real (`dry_run=False`) `soxl_ira` account**: `coverage_matrix.py reconciliation_mismatch` shows 8 live detections (GDXU/GDXD/LABU x2/ERY x4, all `result=shares`) — confirms the detection/alert path fires for real against a real account, resolving the `get_real_position` field-name question. Detail text is empty on these rows so the specific mismatch cause wasn't captured — still worth a closer look, but the mechanism itself is proven live. (The 1,753 dry_run rows, all UDOW, are the known intentionally-seeded fake position — expected noise, not a gap.) |
| Trailing-arm state survives `notify_trailing_activated` without re-arming on the next bar | `signals_notify.notify_trailing_activated` (re-reads via `signals_db.get_position_by_id`) | 1 unit test (`test_schwab_automation.py`) | Verified | Fixed 2026-07-22 (Opus review, critical): a stale-state overwrite was clobbering `trailing`/`peak` right after arming, causing re-arming and a second live trailing-sell order for the same shares. **Confirmed live 2026-07-24** (`coverage_matrix.py trailing_arm_state_reread`: SPY, `live`, `trailing_preserved`) — the fix held on a real account |
| Second live SELL order for the same ticker correctly blocked | `schwab_safety._has_open_sell_order` (SELL-side resting-order guard) | 2 unit tests (`test_schwab_safety.py`) | Not started | Built 2026-07-22 as the structural fix that prevents the trail_state bug above (or anything else) from stacking two real exit orders |
| Manual SL-price fallback alert fires correctly when trailing-sell placement fails post-SL-cancel | `signals_notify._attempt_automated_sell` | 1 unit test (`test_schwab_automation.py`) | Not started | Built 2026-07-22; deliberately no auto-recovery here (user's call) — needs a real failed placement to confirm the alert text/price are actually useful in practice |
| Poll loop and Slack handler can't double-open/double-close the same position | `signals_db._position_lock` around `open_position`/`close_position` | 3 unit tests (`test_db_roundtrip.py`) | Not started | Fixed 2026-07-22 (Opus review); race window is narrow and never observed causing a real duplicate, but the lock is now structurally in place |
| Duplicate-order retry after a real rejected/failed order isn't wrongly blocked | `schwab_safety._broker_confirms_order` | 2 unit tests (`test_schwab_safety.py`) | Not started | Fixed 2026-07-22 — closes the "bigger structural change" backlog item; needs a real rejected order to confirm the retry path in practice |
| Fast-path (websocket) fill reconciliation doesn't act on a partial/in-flight execution | `signals_notify.drain_fill_queue` (re-confirms via `get_filled_order` poll instead of trusting the raw stream message) | 3 unit tests (`test_part3_gap_resize.py`) | Not started | Fixed 2026-07-22 — the stream message's own `filledQuantity` may represent one partial execution of a still-filling order (unverified cumulative-vs-incremental semantics); needs a real multi-execution fill to confirm the poll-reconfirm path in practice |
| `same_day_block` skips correctly for margin accounts, still blocks cash accounts | `schwab_safety.check_order` (`AccountLimits.account_type`) | 2 unit tests (`test_schwab_safety.py`) | Not started | Built 2026-07-22; no real same-day re-buy has ever been attempted live in either account type |
| Manual BUY confirmation (Executed/Filled/Manual Open) opens a position with the real account, not NULL | `signals_blocks._build_buy_blocks`, `signals_notify._ticker_block`, `signals_handlers.handle_entry_price`/`handle_trail_buy_fill_price`/`handle_manual_open_price` | New tests in `tests/test_coverage_check.py` cover the underlying node-identity plumbing, but nothing exercises the actual button→modal→handler chain end-to-end | Not started | **Real, previously-undetected bug found+fixed 2026-07-25** (Opus review rounds 4/5): both Slack button `value` payloads had always omitted `account`/`id` from their node whitelist, so a manually-confirmed fill could open `open_positions.account=NULL` -- invisible to `check_live_state_reconciliation`. Fixed by adding the missing fields to both whitelists; **not yet observed against a real Slack button click** (this session couldn't test the live workspace) -- next real manual BUY confirmation should be checked for a non-NULL `account` on the resulting `open_positions` row |
| Stale/duplicate Executed or Filled button tap doesn't open a phantom position | `signals_handlers.handle_entry_price`/`handle_trail_buy_fill_price` (new pending_buys-existence guard) | None yet -- guard logic not directly unit-tested | Not started | Built 2026-07-25 alongside the fix above; **known latent gap, not yet reachable**: the guard assumes every rendered Executed button has a backing `pending_buys` row, which breaks for a live TrailingExit ticker outside `SCHWAB_AUTOMATION_TICKERS` (see `docs/backlog_cache.md`'s 2026-07-25 round-6 entry) -- would silently discard a real fill confirmation instead of a stale one in that case |
| Two concurrent live nodes on the same ticker in different accounts can both place real orders | `schwab_safety._live_ticker_accounts`/`check_order` (ticker->set-of-accounts, membership check) | New unit tests updated for the new error message (`test_schwab_safety.py`) but no test actually exercises 2 real concurrent nodes on one ticker | Not started | Built as part of the wl_id refactor (2026-07-25/26, `docs/backlog_cache.md`) -- this is the change that actually unblocks the motivating soxl_ira-two-account design; never yet exercised with a real second node on an already-live ticker |
| BUY-side Slack buttons (Filled/Missed/Cancelled/Skipped/Manual Open) resolve the correct node when 2+ nodes share a ticker | `signals_handlers.py` (all 6 BUY handlers now match/clear pending_buys by wl_id, not ticker) | None -- no test simulates 2 concurrent pending_buys rows for the same ticker | Not started | Built same session; SELL-side already did this correctly via `position_id`, BUY-side never had a live 2-node-per-ticker scenario to expose the gap until this refactor |
| A real broker BUY fill reconciles against the correct node's pending_buys row when 2+ are pending for the same ticker | `signals_notify._reconcile_buy_fill` (new `wl_id` param, falls back to an alert if still ambiguous) | None -- no test simulates 2 concurrent real pending buys for the same ticker | Not started | Built same session; every current call site already has wl_id in scope except `drain_fill_queue`'s stream entry point, which passes its best-effort ticker+account-derived node id |
| Node-level automation pause (`schwab_safety.pause_node_automation`) blocks real orders for just that node, not sibling nodes on the same ticker | `schwab_safety.node_automation_enabled`, wired into `check_order` and `_attempt_automated_sell` | None yet | Not started | Built same session as an additive AND-gate alongside the existing ticker-level pause; no Slack button wired to it yet (console/script-only for now). **Known limitation**: `check_order`'s `_node_id` is still derived via a ticker+account fuzzy lookup (`get_watch_list_node`), not a real threaded `wl_id` — for 2 nodes sharing both ticker AND account, the lookup returns `None` and the node-level pause silently no-ops (fails open, not closed — the ticker-level pause remains the real gate) |
| `schwab_safety.check_order`'s oversell guard resolves the right position when 2 live nodes share a ticker in different accounts | `signals_db.get_open_position_for_account` (new, ticker+account keyed) | None yet | Not started | Found by a 2nd Opus review round, 2026-07-26: the guard previously used ticker-only `get_open_position`, which could resolve to a *different* node's position (wrong account) and wrongly reject a legitimate SELL as an oversell. Fixed same session, not yet observed against a real 2-account-same-ticker SELL |
| A `dry_run=True` account's trailing-buy/market-buy order synthesizes a real fill (bounce-fill or immediate) since no real broker fill event will ever arrive | `signals_notify.update_dry_run_buys`/`_fill_dry_run_buy` (new) | 5 unit tests (`test_dry_run_sim.py`) incl. double-fill and wl_id-less guards; `scripts/live_sim_harness.py::scenario_dry_run_sim_cycle` (2026-07-26, bounce-fill BUY through the real wiring) | Not started | Built 2026-07-26 -- fixes the canary/dry_run "no closed trade found" false-positive coverage_deviations (XLF/VOO/IWM/QQQ, ids 7-10, unexplained since 2026-07-24). Writes to the real `open_positions`/`trade_log` tables tagged `is_dry_run_sim=1` (user's explicit call, not the paper tables) |
| A synthesized dry-run-sim position closes immediately on exit signal instead of waiting on a Slack button that will never be tapped | `signals_notify.check_dry_run_sim_sells` (new) | 1 unit test (`test_dry_run_sim.py`); `scripts/live_sim_harness.py::scenario_dry_run_sim_cycle` (2026-07-26, SL-triggered close through the real wiring) | Not started | Built 2026-07-26, same session. Opus review found and fixed: `active_signals._scan_pinned_exit_arm` was initially missed by the skip guard (would have corrupted this function's own bar-close detection via the shared `last_seen_bar` dict, and fired real `notify_trailing_activated`/`notify_sell_signal` Slack flows on a synthetic position) -- now regression-tested (`test_scan_pinned_exit_arm_skips_dry_run_sim_position`) |

## How to update this
- New automation feature → add a row when it's built (even if `Status: Not started`).
- A scenario moves to **Verified** only after a real live occurrence is confirmed (Slack log,
  trade_log row, or direct observation) — cite the date and what was seen.
- Don't delete a row once something regresses it back to unverified — note the regression
  instead, so history isn't lost.
