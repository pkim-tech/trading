# Backlog

## ✅ [live-trading][security] Resolved 2026-07-27 (night) — GDXU stale-fill incident root-caused and fixed (order_id-exact matching everywhere); automated TP/SL/TIME exits built; cancel+place replaced with atomic replace_order; canaries restored after accidental deletion

**The incident**: reviewing the day's 3 staged live tests (SPY TRAIL-exit, SH TIME-exit, GDXU
gap-resize), GDXU's real position turned out corrupted — DB said 3 shares @ $80.805, broker said 2
@ $83.76, and the real stop-loss placement had been REJECTED, leaving it unprotected for ~12h. Root
cause: `schwab_client.get_filled_order(account, ticker, side)` had no way to target a specific
order — it just returned "the most recent FILLED order matching ticker+side" from the broker's
order book. `check_gap_resize`'s replacement market-buy order took ~15min to fill (normal — placed
pre-market, before the 9:30 open), the poll loop gave up, and the fuzzy fallback matched a stale,
unrelated fill from **3 days earlier** instead of correctly returning "not filled yet."

**Fix, in order**:
1. `get_filled_order` gained an `order_id` parameter — when given, looks up that exact order and
   returns its fill only if FILLED, never substituting an unrelated match. Threaded through every
   real call site: `check_gap_resize`, `check_auto_fills` (both BUY and SELL branches — the SELL
   branch was missed in the first pass, caught by a Sonnet review), `drain_fill_queue` (needed
   `schwab_stream._parse_activity_message` to also extract `order_id`, 5-tuple → 6-tuple),
   `_sync_confirm_and_protect` (also missed in the first pass, same review).
2. Real bugs found in the same investigation, separate from tonight's session but surfaced during
   it: only the TRAIL-arm event had ever gotten an automated-sell path — TP/SL/TIME exits had
   **zero** automation, always required a manual tap regardless of automation scope (real: SH's
   TIME-exit sat unmanaged for hours). New `_attempt_automated_exit_sell` places a real market sell
   for these 3 reasons, same guards as the TRAIL path.
3. Even when an order WAS auto-placed, confirming its fill (to actually close the position) always
   required a manual tap, even though the exact order_id makes the fill unambiguous once Schwab
   confirms it. New `check_own_sell_fills` (unconditional every poll, not gated behind the existing
   opt-in `auto_fill_detection_enabled` — that toggle is for detecting a fill we *didn't* place;
   this only ever confirms our own known order_id) auto-closes once confirmed.
4. User-driven design review of the fix: cancel-then-place (used by `check_gap_resize`,
   `_attempt_automated_sell`, `_attempt_automated_exit_sell`) leaves a real window — a confirmed
   cancel followed by a failed/blocked new placement leaves nothing resting at the broker in
   between. Rewrote all three to use schwab-py's `replace_order` (cancel-old+create-new as one
   broker call) via new `schwab_client.replace_equity_order_with_market`/
   `replace_order_with_trailing_sell`. `cancel_order` is now dead code (no remaining caller).
   **One residual risk accepted, not fixed** (user's explicit call) — see the "Accepted residual
   risk" entry elsewhere in `docs/backlog_cache.md`: the retry wrapper can still fire a second
   `replace_order` against an order_id the first (client-failed, broker-succeeded) attempt already
   replaced.
5. Two independent Sonnet review rounds (not Opus — standing user preference, budget reasons) on
   the real diff; all CONFIRMED findings fixed same session, including a HIGH cross-bar duplicate
   real-order-placement risk (TP/SL/TIME had no guard against placing a second order if the first
   wasn't confirmed filled by the next bar — `sell_alerted` only dedups within one bar, and a TIME
   condition re-triggers every subsequent bar). Full suite: 302 passed. Harness: 7/7. Invariants:
   clean.

**Real state cleanup, same session**: GDXU corrected to real broker state (2 shares @ $83.76), no
stop-loss placed (deliberate — it's taking over SPY's TRAIL-exit test role instead), a real
trailing-sell placed manually (0.3%, matching SPY's tight test params) with `trail_state` seeded
armed (`peak=100`, forcing the exit condition true) so it exercises the new confirm-and-close path
fast. **SPY's real trailing-sell had actually already FILLED at 09:46 ET that morning** (+0.70%
P&L) but sat invisible in the DB as still-open for hours, nagging reminders the whole time — its
`order_id` predated tonight's fix and was never captured; retroactively closed with the real fill
data once found. SH's `max_hold_hours` bumped 11→18 (retimed to fire ~same time the next day, now
through the fixed automated path) and given a real stop-loss deliberately far out-of-the-money
(20% below market) so it's genuinely protected without pre-empting the TIME-exit test — it had
carried **zero** protective order since 2026-07-23, a real gap the user caught, unrelated to
tonight's other fixes.

**Canary restoration**: separately, the 6 canary proof-of-life nodes (`IVV`/`QQQ`/`IWM`/`DIA`/
`VOO`/`XLF`, `mode='live'`/`account='ira'`) turned out to have been accidentally deleted (not just
narrowed) during an earlier-in-the-day live-watchlist scope cleanup that was only supposed to
remove disposable one-day `soxl_test` scratch nodes — not the intended outcome. Restored via a
corrected `scripts/add_canary_nodes.py` (ticker `SPY`→`IVV`, since `SPY` is now the real
`soxl_test` live node and paper/canary dedup is ticker-only; promoted to `mode='live'`/
`account='ira'` directly in the script rather than as a separate manual step) with
`scenario_expectations.node_id` relinked to the new node ids (old ids no longer resolved,
`coverage_check.py` was falling back to ticker-only scoping). 12 resulting deviations (6 stale +
6 fresh, all "no closed trade found" — correctly explained as "nodes didn't exist today until just
now, not a real regression") explained via `explain_deviation`.

**Tooling built from real friction this session** (per explicit user direction — investigation and
staging kept happening as one-off `python -c` queries/orders instead of durable, rerunnable
scripts): `scripts/audit_live_test_candidates.py` (reports real broker+DB state and which of the
3 live-test scenarios — entry, TIME-exit, TRAIL-exit — a candidate ticker currently fits, in one
command instead of ~20 one-off queries) and `scripts/stage_live_test_order.py` (the repeatable
direct-broker-bypass tool for staging a real order outside a signal window, printing the real
cash/notional/duplicate-order/kill-switch checks `schwab_safety` would normally do automatically
before asking for confirmation). Both documented in `docs/live_test_coverage.md`'s new "Runbook:
staging a real-order live test scenario" section. 2 new memory entries saved (a reference pointer
to the runbook, and a feedback note to persist reusable techniques immediately when they come up,
not wait for session close) after the user had to re-explain the direct-bypass mechanism, which had
already been documented in an earlier session but had no memory pointer directing back to it.

## ✅ [live-trading][security] Resolved 2026-07-27 (later) — closed 2 of 3 MEDIUM gaps from the duplicate-BUY-alert fix (bounded broker-truth timeout, once/day suppression throttle); trimmed `SCHWAB_AUTOMATION_TICKERS` from 29 to the 12 tickers actually on watchlist 65
`active_signals._real_order_or_position_exists` (the broker-truth dedupe check on the pinned-entry
critical path) now runs the `_open_orders`/`get_real_position` calls inside a
`ThreadPoolExecutor(max_workers=1).result(timeout=5.0)` instead of unbounded — falls back to `False`
(proceed with the alert, existing behavior) on timeout, same as any other exception. Bound sits under
the schwab-py client's own 10s socket timeout (`_CLIENT_TIMEOUT_SECS`), not a replacement for it.
Separately, the repeat Slack suppression message (fired every poll while `already_pending` stays true)
is now throttled to once/calendar-day per node via new `signals_db.dup_alert_suppressed_today(node_id)`
(`date(ts,'localtime') = date('now','localtime')` against `coverage_events`, avoiding the UTC-vs-ET
mismatch bug class this project has hit before) — checked *before* logging the current poll's own
event so it can't self-match. The block itself (buy_alerted.discard, coverage_event logging) is
unchanged; it still re-checks broker truth every poll and self-heals once the condition clears — only
the Slack spam is throttled, not the safety behavior. Third MEDIUM finding (gate excludes tickers
outside `AUTOMATION_ENABLED_TICKERS`) is now moot for the live watchlist: `.env`'s
`SCHWAB_AUTOMATION_TICKERS` was trimmed from 29 tickers (many leftover from deleted canary/soxl_test
nodes) down to the 12 actually on watchlist 65 (AGQ,DPST,GDXU,HIBL,KORU,NUGT,SH,SOXL,SPY,UDOW,USD,YANG)
— every live node's ticker is now in scope. `sync_automation_scope()` will log the `.env` change to
`watch_list_audit` on the next daemon restart (not yet restarted). Full suite: 302 passed.
`signals_invariants.py`: clean. **Opus review of this diff was started then killed by the user
(budget/session-limit reasons) before returning a verdict — not independently reviewed.** Residual:
if reviewing this later, check the ThreadPoolExecutor-per-call pattern (new executor spawned each
call rather than shared) for resource overhead under sustained polling, and confirm the
`dup_alert_suppressed_today` check-then-log ordering holds under concurrent pinned-entry scans.

## ✅ [live-trading][security] Resolved 2026-07-27 — reconciled the 2026-07-26 duplicate-BUY incident's real DB mess against broker truth (zero exposure found), placed SPY's real pending trailing-sell, reduced watchlist 65 to 4 live nodes, and fixed `check_gap_resize`'s restart-duplication bug

**Reconciliation**: confirmed via `schwab_client.get_real_position`/`schwab_safety._open_orders` that
none of the 8 duplicate `pending_buys` rows from the prior session's incident (LABU ids 32/38 — real
money, `soxl_ira`, `dry_run=False`; DIA 16/28/35/40, IWM 34/39, QQQ 37/41 — canary, `ira`, `dry_run=True`)
had resulted in any real resting order or position — LABU showed 0 shares/0 orders despite 25+ reminders
nagging for an order that never actually reached the broker. All 8 rows cleared via
`signals_db.clear_pending_buy_by_wl_id`. Zero real risk, purely stale DB state.

**SPY's pending real exit**: the genuinely-armed `trail_state.exit_pending` (reason=TRAIL, pending since
Friday, 46+ unactioned reminders) was placed for real — `signals_notify._attempt_automated_sell` called
directly against position id 17 (`soxl_ira`), resulting in a real resting `TRAILING_STOP` SELL order
(order id 1007336072974) confirmed `AWAITING_STOP_CONDITION` at the broker for the 3 real SPY shares.

**Watchlist 65 live-mode reduction**: user's call — collapse live-mode nodes down to just SPY, SH, GDXU
(#108, the "safety net #2" gap-resize test node — user confirmed keeping it over the ambiguous "2nd GDXU
node" backlog phrasing), and DPST (the real-money volunteer). The other 11 live nodes (DIA/ERX/ERY/GDXD/
IVV/IWM/LABD/LABU/QQQ/VOO/XLF — 6 of them the 2026-07-23 canary proof-of-life nodes, 5 the `soxl_test`
one-day test nodes) were **deleted outright** via `signals_db.remove_node`, not demoted to research —
user's explicit call, since a research-mode node would just generate paper-trading noise for tickers
that have been moved on from. VOO and IVV had open `is_dry_run_sim=1` positions (ids 24/26) tied to their
nodes via `wl_id`; both were retroactively closed (`signals_db.close_position`, tagged
`DRY_RUN_RETROACTIVE_CLEANUP`, same pattern as the 2026-07-28 UDOW cleanup) before their nodes were
removed, so nothing was left dangling. Confirmed post-cleanup: `mode='live'` nodes in watchlist 65 are
exactly [136 DPST, 108 GDXU, 135 SH, 134 SPY].

**3 real-order tests staged for the next daemon restart** (per the plan in the prior session's backlog
entry): (1) **SPY trailing-sell trigger** — effectively already exercised for real via the exit-pending
placement above, no separate forced test needed. (2) **SH TIME-exit test** — node #135/position #18 both
persisted with `max_hold_hours=11` (down from 48; real held bars were 7 at staging time) and
`trail_sell_pct=50%` (up from 0.3%, so the real trailing-sell order this forces will trail 50% below peak
— practically unfillable, letting the real order-placement path get proven without risking an actual
sale of the real 50 shares). No revert needed — SH is a purpose-built test node (`version='soxl_test'`,
label "soxl_ira live-sell test"), not a real long-term trading config. (3) **GDXU gap-resize test** — a
real `TRAILING_STOP` BUY order (5 shares, ~$425 at $85, order id 1007336073086) was placed via a direct
bypass of `schwab_safety`'s signal-window gate (same pattern as the 2026-07-23/24 real-order tests,
run only after explicit user confirmation each step, since the auto-mode classifier blocks this kind of
action without it), backdated conceptually to "as if it had been placed Friday." A `pending_buys` row
(id 42, wl_id 108) was rigged with `running_low=$1.00` so the computed buy-trigger (~$1.003) is
guaranteed below the real price, forcing `check_gap_resize()` to treat it as a genuine overnight gap at
the next 9:15-9:29 ET window — expected to cancel the resting order and replace it with a real MARKET
buy sized fresh off whatever GDXU's live price is at that time (target notional $500, node's
`starting_notional`, padded 5%), opening a real ~$425-450 position. Requires the daemon to actually be
running by 9:15 ET or nothing fires.

**`check_gap_resize` restart-duplication bug, fixed**: flagged in the prior session (identical risk
shape to the BUY-duplicate-alert bug fixed that session) — a daemon restart inside the ~15min
`_GAP_CHECK_WINDOW` resets the in-memory `gap_check_alerted` once-daily gate, letting a second
invocation re-attempt cancel/replace on an already-in-flight or already-completed row (real double-order
risk). Fixed with a persisted per-row guard: new `pending_buys.gap_resize_date` column (nullable TEXT),
set via `signals_db.mark_gap_resize_attempted(pending_id, date_str)` immediately after
`check_gap_resize` confirms a row's gap condition and before any cancel/replace broker call — a
restarted re-entry sees the row already claimed for today and skips it. New regression test
(`test_gap_resize_is_idempotent_against_restart_reentry`) proves the guard survives a mid-flight crash
(cancel succeeds, replacement call raises) by re-invoking `check_gap_resize()` and asserting no second
cancel call. **Opus review found 2 LOW issues, both fixed same session**: the marker was originally
keyed on `wl_id`, which silently no-ops (matches zero rows) if `wl_id` is ever NULL — rekeyed to
`pending_buys.id` (always present); the regression test only asserted call counts, not that the marker
was actually persisted (could pass vacuously under a future change) — added an explicit
`gap_resize_date` assertion. Full suite: 302 passed (was 301). `live_sim_harness.py`: 7/7, including
`scenario_gap_resize`. `signals_invariants.py`: clean (0 known violations — the previously-accepted
UDOW violation is separately resolved as of the 2026-07-28 entry above).


## ✅ [live-trading][coverage] Resolved 2026-07-26 — re-triaged the 22 `wired-never-fired` coverage-grid rows: split policy-internal from broker-interacting, closed 20/22 with real test proof, added a derived `offline_proof` axis to the registry

**Origin**: earlier same session, raised the idea of a *deliberate* trading-day-gate-style harness to
force rarely-exercised order-placement branches (`gap_resize`, duplicate-order-guard, `kill_switch_block`,
several `wired-never-fired` grid rows) to fire on purpose. Two designs were floated (point `live_sim.py`
at a real `dry_run=True` account, vs. build a fully isolated stub `schwab_client`). An independent Opus
design review pushed back on both: option A (`dry_run=True`) short-circuits *before* the real broker
call, so it can't prove `gap_resize`'s actual question (does Schwab honor our cancel+replace) and it
pollutes the same ledgers meant to represent real trading history; option B (a stub broker) can only ever
confirm our code does what we already believe, not what Schwab actually does — recording stub firings as
`coverage_events` credit would be "counterfeit proof" the same way option A is "contaminated proof."

**Real recommendation**: split the 22 rows by what actually needs proving, not build one harness for all
of them.
- **Broker-interacting** (2 rows: `gap_resize`, `automated_sell_execution`) — the real question is
  whether Schwab agrees with our assumption, which no stub can settle. Still open — needs a deliberate
  real order on `soxl_ira`'s small-notional path, not scoped/scheduled this session.
- **Policy-internal** (the other 20) — decided entirely inside our own code/state (`schwab_safety.
  check_order`'s guards, Slack-handler dedup logic, scan-loop bar-close detection), zero broker
  dependency. Initially assumed most already had solid offline test coverage; a follow-up check found
  the real split was **3 event-asserting tests / ~8 behavior-only / ~6 with no test at all** (not the
  optimistic "most already covered" first guess) — `signals_handlers.py` in particular had zero test
  file whatsoever.

**Built to close the policy-internal gap**:
1. Added `signals_db.get_coverage_events(scenario_key=...)` assertions to ~9 existing tests across
   `tests/test_schwab_safety.py`, `tests/test_schwab_automation.py`, `tests/test_part3_gap_resize.py`,
   `tests/test_part4_entry_trigger.py` that previously only proved the underlying behavior (e.g. "duplicate
   order raises `SafetyViolation`") without asserting the `log_coverage_event` call itself — meaning the
   logging line could be silently deleted and the test would still pass.
2. Wrote 3 brand-new tests for rows with zero coverage at all: `test_node_level_automation_pause_blocks_
   order` + two sibling-node tests (`test_schwab_safety.py`), and a sibling-node fill-reconciliation
   disambiguation test for `buy_fill_reconciles_correct_node` (`test_part3_gap_resize.py`).
3. New `tests/test_signals_handlers.py` — `signals_handlers.py`'s first-ever test file. Calls the real
   `handle_entry_price` Bolt handler function directly (bypassing Bolt's dispatch, since real interactive
   buttons can't be tested that way — real clicks route to whichever process holds the Socket Mode
   connection) with a hand-constructed `body`/`client`, mirroring how `scripts/live_sim.py` already
   exercises real handler logic without a live connection. Covers `stale_buy_button_guard`,
   `buy_buttons_resolve_correct_node`, `manual_buy_confirmation_account` (including the `no_account` ->
   `unattributed` mode branch). Skips gracefully if `cfg.SOCKET_MODE` was False at import time (the
   handler functions are only defined inside `if cfg.SOCKET_MODE:`).

**New `offline_proof_for()` in `scripts/coverage_registry.py`** — a second, orthogonal axis to the
existing `status` field. `status` correctly answers "is there LIVE proof this works" (a policy-internal
branch only ever exercised in pytest's isolated DB is genuinely `wired-never-fired` by that definition —
pytest is deliberately barred from touching real `coverage_events`, since pytest polluting that table was
itself a real 2026-07-25 incident). `offline_proof_for()` answers "is there ANY proof, live or offline" —
`'event-asserted'` (a test asserts the actual `get_coverage_events()` call), `'behavior-only'` (the
scenario_key appears in a test but nothing asserts the log call itself), or `'none'`. Derived by grepping
`tests/test_*.py` fresh every run (memoized per process), same "never hand-typed" discipline as
`compute_status()` — a hand-maintained scenario_key -> test-name mapping would rot the way
`docs/live_test_coverage.md` did. Surfaced as a new "Offline proof" column in `pages/14_Coverage.py`
(verified the page loads clean via a headless Streamlit smoke test). `status` remains the single source
of truth for "verified-live" et al — `offline_proof` never substitutes for it.

**Opus review of this diff found and fixed real accuracy bugs in the derivation itself** — the same
failure class as this registry's two prior accuracy bugs (a scenario_key collision, and an inverted-logic
bug where unexplained failures once rendered as "verified-live"):
1. **HIGH** — `tests/test_coverage_check.py` (which tests the coverage/`scenario_expectations`
   *infrastructure itself*) reuses real scenario_key strings as arbitrary fixture data —
   `test_log_coverage_event_stores_node_id` calls `db.log_coverage_event('sl_placement', ...)` directly
   and asserts it right back, which made `sl_placement` (a real, still-unresolved `live-attempt-failed`
   SL-placement gap) render as false `'event-asserted'` proof. Fixed by excluding that file from the
   scan — a structural file-purpose exclusion (documented in code), not a per-scenario_key hand-typed
   mapping.
2. **MEDIUM-HIGH** — the original `key in text` substring check false-matched a prefix collision
   (`sl_placement` inside `sl_placement_fast_confirm_timeout`) and an unquoted docstring mention (one
   test file's module docstring naming `tests/test_part3_gap_resize.py` by filename, false-matching
   `gap_resize`). Fixed by requiring an exact quoted-string match (`'key'`/`"key"`).
3. **MEDIUM** — `entry_fill`/`exit_fill` are logged by both `paper_trading.py` (`mode='paper'`) and the
   dry_run-fill-synthesis code (`mode='dry_run'`) — the exact collision `bad_results`/`mode_filter`
   already exist to disambiguate on the `status` axis, re-introduced on the new `offline_proof` axis
   since it originally took only the bare scenario_key. Fixed: `offline_proof_for()` now also takes
   `mode_filter` and only credits a match found in a test file that also references that mode.
4. **LOW-MEDIUM** — `test_node_level_automation_pause_does_not_block_other_nodes` never actually created
   a second node (it paused then immediately resumed the *same* node), so node-level pause *scoping* was
   unproven despite the test's name. Renamed to `test_node_automation_resume_unblocks` (what it actually
   tests) and added two real tests: `test_node_level_automation_pause_does_not_block_sibling_node_other_
   account` (a genuine cross-account sibling node), and `test_node_level_automation_pause_no_op_for_
   ambiguous_sibling_same_account`, which pins down `schwab_safety.check_order`'s own documented `KNOWN
   LIMITATION` comment (a same-ticker-same-account ambiguous node lookup makes a node-level pause
   silently a no-op) as an accepted, tracked gap rather than an untested blind spot.
5. **LOW** — `tests/test_schwab_automation.py`/`test_part3_gap_resize.py`/`test_part4_entry_trigger.py`
   patched `TICKER_AUTOMATION_PATH` in their `env` fixture but not `NODE_AUTOMATION_PATH` — harmless
   today (the real state file doesn't exist), but a latent risk if a real production node id is ever
   paused. Added the same override `test_schwab_safety.py` already had.
6. **LOW** — `_scan_offline_proof()` was re-grepping all test files once per registry row (38 rescans of
   31 files per report). Added a module-level memo cache (safe within one process — test files don't
   change mid-run).
Two lower-severity findings were left as documented limitations rather than fixed (not currently causing
a wrong result): a test that would be silently skipped in a creds-less CI environment still counts as
full proof rather than being detected as conditional; the event-assertion regex only matches the
keyword-argument call form (every current call site uses it).

**Result**: re-ran the CLI report after the fixes — the `behavior-only` bucket dropped from 7 to 0 (all
7 prior hits were false positives from the bugs above), and several previously-inflated rows
(`sl_placement`, `gap_resize`, `cash_check`, `trailing_arm_reread`, `second_ticker_one_account`, the
paper/dry_run entry/exit fills) now correctly show `'none'` — a less flattering but honest picture.
Full suite: 301 passed (was 291 at session start). `live_sim_harness.py`: 7/7. `signals_invariants.py`:
clean. No `active_signals.py`/`signals_*.py`/`schwab_*.py`/backtest-kernel production code was touched —
only test files, `scripts/coverage_registry.py`, and `pages/14_Coverage.py`.

## ✅ [live-trading][security] Resolved 2026-07-26 — NYSE trading-day gate built and hardened after a real review round caught the fix's own retry logic double-ordering; closes the ERY-incident root cause below
Closes the root cause left open by the ERY phantom-fill incident (see the entry directly below).
Added `pandas_market_calendars` (NYSE calendar, weekends + real market holidays, not just a
`weekday() >= 5` check) and wired it in at two layers:

1. **Daemon scan gates** (`active_signals.py`): `_in_window()` — the chokepoint for both the ambient
   `_SIGNAL_WINDOWS`/`_OPEN_CHECK_WINDOWS` scan and, transitively, everything that calls it — now
   returns `False` on a non-trading day via a new `functools.lru_cache`-wrapped `_is_trading_day
   (date_str)`. A second, independent BUY entry point was found the same session and initially missed:
   `_scan_pinned_entry` (fired from the `_PINNED_BAR_TIMES` loop at 9:30/10:30/14:30/15:30, restricted
   to `AUTOMATION_ENABLED_TICKERS` — the real-order-eligible set) never routed through `_in_window` at
   all, gated only on `(hour, minute)` plus a dedup set that clears every calendar date including
   weekends — plausibly the actual path the real Sunday ERY order took. Gated the same way at its call
   site.
2. **`schwab_safety.check_order`** (the real chokepoint every order-placement path routes through via
   `approve_and_record` — manual Slack BUY confirmations, gap-correction replacement orders, automated
   pinned/ambient BUYs alike): gained its own `_is_trading_day` (duplicated, not imported, same reason
   `_SIGNAL_WINDOWS`/`_OPEN_CHECK_WINDOWS` are already mirrored there — avoids a circular import) and an
   unconditional BUY-side check, deliberately *not* exempted for `is_gap_correction` (unlike the
   existing signal-window gate, which is). Manually confirmed:
   `schwab_safety.check_order('soxl_ira', 'ERY', 2, 50.0, 'BUY')` now raises `SafetyViolation` when
   `_now()` is patched to the real incident Sunday.

**A retry (3 attempts, 5s apart) was added to `_scan_pinned_entry`'s call site same session**, to cover
a transient Schwab price-fetch failure it can hit at exactly 9:30/14:30 — but the first version
introduced a real HIGH-severity bug of its own, caught by an Opus review round before it ever reached
the daemon: `open_position_keys` was a stale once-per-loop-iteration snapshot shared across all 3
retry attempts, so `_scan_buy_signals`'s same-day-unlock branch (the buy→sell→buy-same-day allowance,
~8% of SOXL's backtested trades) could see a position that had *just filled on attempt 1* as still
`not already_held` on attempt 2 — placing a second real BUY order for the same signal. The same review
round also found the retry didn't even fix the failure it targeted (`_scan_pinned_entry` still passed a
fetch-failed ticker through to `_scan_buy_signals` with no price override, so it fired at an ambient
non-Schwab price on attempt 1 regardless of the retry) and that it was writing 3 biased
`log_open_price_quality` rows per ticker per bar, inflating the `true_open_rate` metric
`verify_open_price_quality.py` uses to gate paper→real promotion decisions. **Fixed same session**:
`_scan_pinned_entry` now returns `(summaries, failed_tickers)`, excludes any fetch-failed ticker from
that attempt's `_scan_buy_signals` call entirely (no ambient-price fallback possible), and the call site
now re-reads `open_position_keys` fresh from the DB before every attempt and only retries tickers still
in `failed_tickers` (a ticker that succeeds is permanently dropped from later attempts) — closing the
duplicate-order race structurally, not just by luck of timing, and making `log_open_price_quality`
exactly-once per ticker per bar regardless of how many attempts it took. A ticker that still fails all 3
attempts now posts a Slack alert (previously a silent `log_poll` line only) instead of vanishing for
the bar with no signal anyone could see.

**Two independent review rounds** (an initial Opus pass that found the HIGH bug above, then a second
verification pass split between Opus and Sonnet in parallel to compare detection — a live workflow test
of `[[feedback_dual_model_review_comparison]]`) — **both models independently confirmed all 4 fixes
genuinely close their targeted findings, no new bugs**. Opus additionally surfaced 3 LOW/INFO items
Sonnet's pass didn't: a fetch-fail-3x-in-a-row ticker was silently skipped (fixed, Slack alert added,
see above); the retry only covers per-ticker price-fetch failures, not a `_scan_pinned_entry`-level
exception (by design — `_guarded` swallowing an exception there correctly disables the retry rather
than looping blind); and `scripts/live_sanity_check.py` calls `schwab_client.place_order` directly,
outside `check_order` and thus outside the new gate too — deliberate (manual, typed per-ticker
confirmation tool), not a regression.

**Deliberately not built same session** (raised in the original incident backlog item, still open, see
`docs/backlog_cache.md`): a *deliberate* version of the same mechanism — feeding a real dated bar
sequence to exercise real order-placement code on purpose (to force `gap_resize`/duplicate-order-guard/
`kill_switch_block`, several of the coverage grid's `wired-never-fired` rows, to actually fire) —
distinct from today's accidental case. Not started, a design conversation only.

Full suite: 291 passed. `live_sim_harness.py`: 7/7. `signals_invariants.py`: clean.
`verify_trailing_buy_resolution.py`/`verify_trailing_sell_resolution.py` (AGQ, SOXL): both clean.

## ✅ [live-trading][security] Resolved 2026-07-26 — real incident: daemon placed a real order on a non-trading day (Sunday) against stale cached data; phantom fill traced, cleaned up, and a ticket-model incident log built
**What happened**: `active_signals.py`'s `open_check` window (9:31-9:40 ET) ran normally on Sunday
2026-07-26 against the local price cache, which still held Friday 2026-07-24's last bar (nothing
refreshes over the weekend). The z-score signal still read BUY off that stale bar, so the daemon
placed a real `TRAILING_STOP` BUY order for ERY in `soxl_ira` (a real-money account) at 13:31:29 UTC.
A fill-detection path then recorded it as filled without confirming against the broker, opening a
phantom `open_positions` row (2 sh @ $10.14, `is_dry_run_sim=0`). The follow-on stop-loss placement
attempt was correctly `REJECTED` by Schwab ("oversold/overbought — check position quantity") since no
real position actually existed — but that rejection only fired a `coverage_events` row, never a Slack
alert, so it was found by the user manually checking, not by anything paging them.

**Root cause, confirmed by reading the code**: `active_signals._in_window()` — the function every
signal-window/reminder gate in the poll loop goes through — compares only `(now.hour, now.minute)`
against a fixed range. It has no `now.weekday()` (or market-holiday) check anywhere, so it returns
identically `True` on a Sunday as on any weekday. `_previous_trading_day()` (used only by the 7am
coverage report to pick which prior day to grade) is the one place this project *does* walk back over
weekends — that capability was just never applied to the actual signal-scanning/order-placement gate.
**Root cause since fixed** — see the entry directly above (same date, later in this session): a real
NYSE trading-day gate now guards both daemon scan paths and `schwab_safety.check_order` itself.

**Blast radius, confirmed by checking every account's real order history for the day**: contained to
ERY alone. `ira` (holding the other 15 watchlist-65 nodes) is `dry_run=True` so nothing there ever
reaches the broker; `brokerage`/`sep`/`roth` had zero order activity. The same root cause did also
fire duplicate `pending_buys` rows for other tickers (e.g. QQQ, two rows for the same node, created at
both today's signal windows) but those are `ira`/dry_run and never real-money-reachable.

**Cleanup performed** (direct `signals_db`/`schwab_client` calls, backed up first — 2 separate
`trading_live.db.bak_*` snapshots this session): (1) real resting order 1007336072120 (BUY
TRAILING_STOP, 1 sh, `AWAITING_STOP_CONDITION`) cancelled and confirmed `CANCELED` via the real
post-cancel poll, not just the HTTP response. (2) The phantom `open_positions` row (id 25) removed via
`close_position()` with no `exit_price` (so no fabricated exit/P&L is written to `trade_log`);
`trade_log` id 22 instead got `exit_reason` set directly to an `ERROR_PHANTOM_FILL_NO_MARKET_OPEN`
description — deliberately not treated as a normal close, per the user's explicit call ("trade log is
wrong — we need to mark it error, not just close it"). (3) Separately, EDC's real 423-share manual-
unwind position (open since 2026-07-16, tracked by the user in their own spreadsheet, `watch_list` row
already removed 2026-07-19) was also closed out of `open_positions` at the user's request, same
no-fabricated-exit-price pattern, since it's not part of the v5 selection and being handled outside
this system.

**New**: `signals_db.trading_incidents` table + `log_incident`/`resolve_incident`/`get_incidents`
(ticket-model, never deleted, mirrors the existing `coverage_deviations` pattern) + `scripts/
trading_incidents.py` CLI (`log`/`resolve`/`list`). Distinct from `coverage_deviations` (daily
scenario-expectation misses) and `backlog_cache.md` (planning/design notes) — this is specifically
"something real and bad happened at the broker." Today's ERY incident logged as row #1, left **open**
(cleanup done, root cause not).

**Also built same session, in response to the incident**: every Slack alert now tags
`(account · LIVE/DRY-RUN)` in its header instead of nothing or a bare account name — found live when
the user couldn't tell at a glance whether a QQQ "FILL NOT CONFIRMED" reminder was real risk (it
wasn't — `ira` is dry_run) without asking. New shared `signals_helpers.mode_tag(account)`, wired into
`check_live_state_reconciliation`'s 3 mismatch alerts, `alert_stale_price_exit_suppressed`,
`_attempt_automated_sell`'s 2 UNPROTECTED alerts, `_trailing_order_blocks`, `_exit_pending_blocks`,
`_pending_buy_blocks`, and the original `_build_buy_blocks`/`_build_sell_blocks` signal alerts
(`signals_blocks.py`) — closing a backlog gap open since 2026-07-24 ("real BUY/SELL alerts carry no
mode tag"). **Opus review (background, `model: opus`) found 2 real issues, both fixed same session**:
(1) `_exit_pending_blocks` — the "EXIT NOT CONFIRMED" alert, the most urgent one by its own docstring
("real capital sitting unmanaged") — declared the `account` local but never actually added the tag to
its header text, a half-applied edit; now fixed. (2) `mode_tag`'s unknown/`None`-account fallback
defaulted to the reassuring `DRY-RUN` label — the wrong failure direction for a real-money position
with a NULL account (e.g. EDC's own case before cleanup, or any of the 18 `mode='live'` nodes with
`account IS NULL` still sitting in the archived watchlist 57); changed to a deliberately alarming
`UNKNOWN`, plus an `or 'unmapped'` display guard added to the 4 sites that were missing it (3 of them
would otherwise have rendered a literal `None`). Full suite: 291 passed. `live_sim_harness.py`: 7/7.
`signals_invariants.py`: clean.

## ✅ [live-trading][security] Resolved 2026-07-26 — `auto_fill_detection_enabled` was ticker-only-keyed, a gap the 2026-07-25/26 wl_id refactor was supposed to close but missed
**Found while investigating why a real SPY position sat unconfirmed for 2 days** (a deliberate
standing `soxl_ira` test position, not a bug) and discussing why exit confirmation isn't
automated — surfaced that `check_auto_fills`'s real auto-fill-detection path
(`schwab_safety.auto_fill_detection_enabled`, off by default, toggled via a Slack button) was
keyed purely on the ticker string, with no account/node scoping at all. Enabling it "for SPY"
would have silently applied to *any* node with that ticker in *any* account — not just the one
you actually trust. This is exactly the same bug class the wl_id refactor (`docs/deep_backlog.md`'s
2026-07-25/26 entry) fixed for `ticker_automation_enabled`/`pause_ticker_automation` via an
additive `node_automation_enabled` AND-gate, but `auto_fill_detection_enabled` itself was never
touched by that refactor — it slipped through.

**Fix**: added `schwab_safety.node_auto_fill_detection_enabled(node_id)` /
`enable_node_auto_fill_detection` / `disable_node_auto_fill_detection` — mirrors the
`node_automation_enabled` additive-layer pattern, but with the **default direction inverted**:
defaults False (including `node_id=None`), since this flag *grants* extra automation trust
rather than *restricting* it — missing node identity must not silently grant it, the opposite
fail-direction from the pause mechanism's fail-open default. Real gate is now
`auto_fill_detection_enabled(ticker) AND node_auto_fill_detection_enabled(wl_id)` at both
`check_auto_fills` call sites (buy-side via `node['id']`, sell-side via `pos['wl_id']`). The
Slack enable/disable buttons (`signals_notify._ticker_block`) now carry `{ticker, wl_id}` JSON
instead of a bare ticker string; `signals_handlers.py`'s two handlers parse it and toggle the
node-scoped flag (enable also sets the ticker-level flag; disable only clears the node-level
flag, so a sibling node's separate enablement survives). 2 new regression tests prove the exact
leak is closed (sibling node stays unaffected; `None` node_id defaults closed).

**Opus review same session found 4 issues, fixed the 2 real ones**: (1) CONFIRMED, fixed —
every already-posted reference report has old buttons carrying a bare ticker string, not JSON;
reports stay clickable indefinitely, so the new handlers' unguarded `json.loads` would have
crashed silently on any stale tap. Added a try/except in both handlers posting "stale button,
resend the report" rather than falling back to ticker-only enable (which would reintroduce the
leak). (2) CONFIRMED, cleaned up — a `_pos.wl_id` fallback in `_ticker_block` was dead code
(every row is built from a real watchlist node, so `wl_id` is always resolvable there directly).
(3) Documented, not fixed — a position whose `watch_list` node was later deleted (`wl_id=NULL`,
real example: EDC id=15, the 2026-07-19 manual-unwind row) can never get this feature enabled,
since it renders no reference-table row at all; not live-reachable today (EDC isn't in
`SCHWAB_AUTOMATION_TICKERS`) and fails in the safe direction. (4) Flagged, deferred by user
("maybe later") — disabling the ticker-level flag now has no UI caller, so there's no more
one-tap "kill it for every node on this ticker"; disabling N enabled sibling nodes now takes N
taps. Full suite: 291 passed (was 289). `live_sim_harness.py`: 7/7. `signals_invariants.py`:
clean.

## ✅ [live-trading][coverage] Resolved 2026-07-26 — `live_sim_harness.py` gained `scenario_dry_run_sim_cycle`, closing a coverage gap in the dry_run fill-synthesis testing itself
**Problem, found while deciding how to verify the previous session's dry_run fill-synthesis
work over a weekend (markets closed, no real signal windows to observe)**: neither
`scripts/live_sim.py` (the manual REPL) nor `tests/test_dry_run_sim.py` (12 unit tests, added
2026-07-26) drove `update_dry_run_buys`/`_fill_dry_run_buy`/`check_dry_run_sim_sells` end-to-end
through the real `active_signals.py`/`signals_notify.py` orchestration — the exact kind of gap
`scripts/live_sim_harness.py` exists to close for every other live-trading code path (see the
2026-07-23 entry below), but this new path had no harness scenario at all.

**Fix**: added `scenario_dry_run_sim_cycle` — creates a `mode='live'`/`account='ira'`
(dry_run=True) `TrailingBothZScoreBreakout` node, seeds a `pending_buys` row, patches
`signals_compute._current_price` to trigger a bounce-fill BUY through the real
`update_dry_run_buys`, asserts the synthesized `open_positions` row is tagged
`is_dry_run_sim=1` and the pending-buy row cleared, then patches `active_signals._load_cache`
with a hand-built OHLC bar that breaches `fixed_sl` and calls the real
`check_dry_run_sim_sells`, asserting the position closes with `exit_reason='SL'` and the
closed `trade_log` row carries `is_dry_run_sim=1`. Harness now 7/7 scenarios (~2s). Full suite:
244 passed, unchanged.

Updated `docs/live_test_coverage.md`'s two dry_run-fill-synthesis rows to list the new harness
scenario alongside the existing unit tests, and `docs/automation_principles.md` #11's scenario
count (6 → 7).

## ✅ [live-trading][coverage] Resolved 2026-07-26 — dry_run fill synthesis: a dry_run account's trailing/market-buy order now closes the loop against real price data
**Problem**: `coverage_deviations` showed real, unexplained "no closed trade found" rows for the 6
canary tickers (specifically XLF/VOO/IWM/QQQ, ids 7-10, dated 2026-07-24, still unexplained as of
this session) — traced to a real design gap, not a bug in the checker: a `dry_run=True` account's
real broker order-placement call short-circuits *before* the actual API call
(`schwab_client._place_trailing_order`/`_place_equity_order` both return `(None, None)` and post a
`"[DRY RUN] would ..."` Slack message), so the `pending_buys` row `notify_buy_signal` created never
gets a real fill confirmation — it just sits forever, `open_positions`/`trade_log` never gets a row,
and the daily coverage check correctly (if unhelpfully) flags it as a deviation every single day.
This is the same open question flagged 2026-07-24 evening ("how does dry_run test completion at
all") — the user's own candidate design from that session (stage positions as if they'd filled, the
same way paper trading does) is what got built.

**Design decision**: write to the REAL `open_positions`/`trade_log` tables (not `paper_trading.py`'s
separate `paper_positions`/`paper_trade_log`), tagged with a new `is_dry_run_sim` column — user's
explicit call, so `coverage_check.py`'s existing `trade_log`-reading logic needs no changes at all.
Schema: `is_dry_run_sim` added to `open_positions`/`trade_log` (mirrored onto `paper_positions`/
`paper_trade_log` purely so `open_position()`'s shared INSERT statement works unchanged against
either table pair — a paper position is never also dry-run-sim). `running_low` added to
`pending_buys` (mirrors `paper_pending_buys.running_low`), used only for dry_run trailing-buy
bounce tracking.

**Implementation** (`signals_notify.py`): `update_dry_run_buys()` iterates all `pending_buys`,
skips any node whose account isn't `dry_run=True` (or whose mode isn't `'live'`), then either
tracks running_low/synthesizes a bounce-fill (trailing-buy nodes, same math as
`paper_trading.update_paper_buys`) or fills immediately (market-buy-eligible nodes, same reasoning
as `paper_trading.start_paper_market_buy`) — calling `open_position(..., is_dry_run_sim=True)` and
posting a `[DRY RUN] would have filled ...` Slack message. `check_dry_run_sim_sells()` mirrors
`paper_trading.check_paper_sells()`: once `check_sell_condition` fires an exit reason on an
`is_dry_run_sim` position, closes it immediately rather than routing through `notify_sell_signal`'s
Slack-button-wait flow (correct only for a real order that might actually get a human tap). Both
wired into `active_signals.py`'s `run_loop` at the same cadence as their paper-trading equivalents;
the normal per-position exit-check loop skips `is_dry_run_sim` positions (routed to the new function
instead), as does `check_live_state_reconciliation` (no real broker state exists to compare
against for a synthetic position, by design).

**Opus review (session-wrap convention, since this touches `active_signals.py`/`signals_notify.py`/
`signals_db.py`) found 6 CONFIRMED + 2 PLAUSIBLE issues, all fixed same session**:
1. **Most severe** — `active_signals._scan_pinned_exit_arm` (a separate, earlier-firing exit-check
   loop) was missed by the original `is_dry_run_sim` skip guard. It shares the `last_seen_bar` dict
   with `check_dry_run_sim_sells`, so it would have consumed the bar-close marker out from under the
   new function (silently downgrading it to the mid-bar/continuous branch, bypassing the real
   intrabar Low/High gap-through-trigger logic) — and it would have fired real
   `notify_trailing_activated`/`notify_sell_signal` Slack flows (arm + interactive SELL button) for a
   position with no real order behind it at all. Fixed: added the same skip guard there too.
2. This also meant `check_trailing_reminders`/`check_exit_reminders` (which look mostly like no-ops
   for a sim position, since `trail_state`'s `trailing`/`exit_pending` fields are normally never set
   for one) were only safe *because* of finding #1's absence — once `notify_trailing_activated`/
   `notify_sell_signal` fired via that gap, they'd start nagging every 15 minutes forever. Resolved
   by fixing #1, no separate change needed.
3. `_fill_dry_run_buy` never checked `open_position()`'s own return value (`False` on a duplicate/
   already-open node) — its docstring explicitly warns every caller must check this "since a silent
   skip must not be reported as a fill." A duplicate fill attempt (e.g. a stale leftover
   `pending_buys` row) would have re-posted a false `[DRY RUN] would have filled` Slack message and a
   false `coverage_events` row every poll, forever. Fixed: checks `opened`, bails cleanly on `False`.
4. A `pending_buys` row with a falsy `wl_id` (legacy/pre-migration, or any future edge case) would
   never actually clear (`clear_pending_buy_by_wl_id`/`update_pending_buy_running_low` both key on
   `wl_id`, and SQL `NULL = ?` never matches) — combined with #3's missing check, this would re-fire
   a synthetic fill attempt and a false alert every poll indefinitely. Fixed: fails closed, skips any
   `wl_id`-less row entirely rather than trusting the frozen `node_json` snapshot fallback.
5. Simulated dry-run fills were contaminating two real safety/sizing code paths with no filter:
   `signals_db.closed_today()` (the same-day-rebuy cash-settlement warning/`SafetyViolation`) and
   `signals_helpers._last_sale_recovery()` (real order sizing off the last closed trade's proceeds).
   A simulated exit could have suppressed a real order or sized a real order off fake proceeds —
   currently latent (the one `dry_run=False` account, `soxl_ira`, is `account_type="margin"`, which
   `closed_today` doesn't even branch on; no shared account currently mixes sim and real trade
   history), but a real hazard the moment either changes. Fixed: both queries now filter
   `is_dry_run_sim=0`.
6. Synthetic positions were completely indistinguishable from real ones in every human-facing
   surface (`build_reference_table`/`_ticker_block`'s Held branch, `cmd_positions`,
   `scripts/open_positions_status.py`) — the exact class of ambiguity the existing 🧪CANARY/
   `(research)` tagging convention (2026-07-23) was built to prevent. Fixed: added a `🧪DRY-RUN-SIM`
   tag to the Held branch, suppressed the "Manually Close" button for a sim position (no real
   position to close), added an `is_dry_run_sim`/Sim column to both CLI tools.
7. (Plausible) No `node['mode'] == 'live'` gate on `update_dry_run_buys` — not currently reachable
   (a `research`-mode node's BUY never creates a `pending_buys` row today), but added defensively so
   a future BUY-routing change can't silently open a real `open_positions` row alongside a node's own
   `paper_positions` row for the same `wl_id`.
8. (Plausible, cosmetic) `check_gap_resize` was still reading `pending['signal_price']` as its
   running-low reference instead of the new `running_low` column — synced for consistency (no
   material behavior change pre-fix, since overnight polling never moves `running_low` anyway).

**Verification**: 12 new tests in `tests/test_dry_run_sim.py`, added incrementally as each Opus
finding was fixed — specifically target double-fill prevention, the market-buy-eligible immediate-
fill branch, the `wl_id`-less fail-closed skip, `closed_today`/`_last_sale_recovery` exclusion, and
the `_scan_pinned_exit_arm` skip guard (not just the original happy-path fill/close tests). Full
suite: 244 passed (was 238 pre-session). `scripts/live_sim_harness.py`: 6/6. `signals_invariants.py`:
unchanged, 1 known accepted violation (UDOW). `verify_trailing_buy_resolution.py`/
`verify_trailing_sell_resolution.py --tickers AGQ,SOXL`: both clean, no new mismatches (this diff
never touched `strategies.py`/`backtester.py`, run per the pre-commit checklist's
`active_signals.py`-changed trigger anyway).

**Not yet observed live** — this is new daemon logic, not yet run against a real restart. Next
natural checkpoint: the canaries' `coverage_deviations` rows should stop reappearing once the daemon
restarts with this change; watch `coverage_matrix.py`/`coverage_check.py` output for a real
`entry_fill`/`exit_fill` `mode=dry_run` row to confirm the synthesis actually fires live, not just in
tests.

## ✅ [live-trading][coverage] Resolved 2026-07-27 — `coverage_deviations` rows are now permanent record ("ticket model"), never deleted
Per user's explicit framing: an exception/deviation is an artifact of something that happened, like a
Jira ticket — once created, it should never vanish, only ever be explained. `clear_deviation_if_resolved`
(`signals_db.py`) changed from `DELETE` to auto-`UPDATE` (`reason='Auto-resolved: ...'`,
`reason_by='system'`) for a same-day unexplained deviation that a later re-check finds met — the row
stays, tagged as system-resolved rather than human-explained. Also cleared the 4 stale unexplained
XLF/VOO/IWM/QQQ rows (ids 7-10, open since 2026-07-24, root-caused by the dry_run fill synthesis gap
already fixed 2026-07-26) by explaining them directly.
**Regression found+fixed same session** (session-wrap Opus review): the auto-resolve change alone would
have let a genuine NEW same-day deviation silently inherit the stale system reason and vanish from
`get_deviations(unexplained_only=True)` — `record_deviation` now clears a `reason_by='system'` reason
(never a human one) whenever it refreshes an existing row, so a system auto-resolution yields to real
new evidence instead of masking it. New regression test
(`test_record_deviation_clears_system_reason_on_new_deviation`).
## ✅ [live-trading][security] Resolved 2026-07-25 — second Opus review round on the daily_order_cap change, 2 findings
Full-diff review of the final `schwab_safety.py` state (increment + check both BUY-only, `soxl_ira`
cap 3→100), after the first round's SELL-blocked-by-BUY-exhausted-cap bug was already fixed.
Mutation-tested the new regression test (`test_sell_not_blocked_by_buy_exhausted_daily_cap`) by
reverting the guard — confirmed it actually fails without the fix, not passing coincidentally.
Two findings:
1. **CONFIRMED, fixed same session**: `is_protective`'s docstrings (`check_order`/
   `approve_and_record`) still advertised it as covering "a stop-loss placement or post-fill
   top-up" — stale now that SELL (including stop-loss placement) is unconditionally exempt from
   `daily_order_cap` regardless of `is_protective`. The flag is dead on that path now, only
   meaningful for the BUY-side top-up. Docstrings corrected to say so explicitly.
2. **PLAUSIBLE, not fixed, user's known tradeoff**: bumping `soxl_ira`'s cap 3→100 with
   `notional_cap` unchanged at $800 removes the de-facto cumulative same-day BUY-notional bound
   the low cap was incidentally providing (~$2.4k/day → ~$80k/day). Nothing else in the codebase
   reads the counter for a second purpose (checked — `STATE_PATH` is read only in `check_order`,
   written only in `approve_and_record`), so this isn't a hidden second-order bug, just the
   explicit consequence of the bump the user already asked for. Ties into the existing
   still-open "max cumulative BUY notional per ticker per day" backlog item.
Full suite: 232 passed. `scripts/live_sim_harness.py`: 6/6 scenarios passed.
## ✅ [live-trading][coverage] Resolved 2026-07-25 — pytest was polluting the real coverage_events table with fake "daemon_section_exception" rows
Found while cross-checking `docs/live_test_coverage.md` against real `coverage_matrix.py` output
for the Monday live-test replan: `daemon_section_exception` showed 360 real-looking rows
(detail=`boom`), which turned out to be pytest fault-injection noise, not real daemon failures.
Root cause: 5 of 6 test functions in `tests/test_run_loop_fault_tolerance.py` called
`active_signals._guarded` with a real raising exception but never requested the file's own
`isolated_db` fixture — `signals_db.log_coverage_event` is fire-and-forget (never raises into the
caller), so each test passed while silently writing a real row into
`cache/live/trading_live.db`'s `coverage_events` table. Only one test out of six had opted into
isolation. Fixed: made `isolated_db` `autouse=True` for the whole file. Backed up
`trading_live.db` (`cache/live/trading_live.db.bak_20260725_114349_pre_coverage_events_test_pollution_cleanup`)
then deleted the 360 polluted rows (`scenario_key='daemon_section_exception' AND detail='boom'`).
Checked `tests/test_coverage_check.py` (the only other file touching `log_coverage_event`) — every
one of its 32 tests already explicitly requests `isolated_db`, no gap there. Full suite: 232 passed.
## ✅ [live-trading] Resolved 2026-07-23 — two logging/observability gaps found while chasing the above, both fixed
Found because the above incident was genuinely undiagnosable in real time — worth fixing on its
own merits, not just as a side effect:
1. **`logs/active_signals.log` never flushed.** `human_fh = open(HUMAN_LOG_PATH, "a")`
   (`active_signals.py`) had no explicit `.flush()` anywhere, unlike the verbose log — since it's
   not a tty, Python fully block-buffers it, so console output (including any `[slack error]`
   line) could sit invisible on disk for the buffer's lifetime. Confirmed live: the file's mtime
   was frozen for 10+ minutes while the daemon was demonstrably still looping (heartbeat proved
   it). Fixed: `open(HUMAN_LOG_PATH, "a", buffering=1)` (line-buffered). Takes effect on next
   restart — done, daemon already restarted with the fix live.
2. **`slack_message_log` recorded intent, not delivery.** `db.log_slack_message(mode, text)` was
   called *before* the real `chat_postMessage`/webhook attempt in `signals_blocks._post_message`
   — a row's existence was wrongly read as proof of a successful send mid-incident (a real mistake
   made in this session, not just a hypothetical risk). Fixed: new nullable `error` column
   (migration applied to the live DB, backed up first as
   `trading_live.db.bak_20260722_233127_pre_slack_log_migration`), and the log call moved to
   *after* the attempt, populated with the caught exception/HTTP-status string (`None` = no error
   caught). `_post_message` also now returns `(channel, ts)` reliably from a single code path.
   `send_reference_report` and `active_signals.py`'s two call sites (startup + scheduled) now
   print/capture that return value instead of discarding it, so a future incident has a real
   `ts` to check via `chat.getPermalink` instead of nothing.
Both root-caused entirely by inspection/live testing, no unit tests added yet for either (the
flush behavior and the error-column plumbing are both straightforward enough that a live restart
was the real verification — see `docs/live_test_coverage.md` if this should get a synthetic test
later). Full suite: 181 passed throughout.
## ✅ [live-trading][security] Resolved 2026-07-21 — account cash/buying-power check built: `get_account_balance` + `check_order` wiring, quantity-aware, fail-closed
Full design context (why this was needed) preserved below the line. Built this
session: `schwab_client.get_account_balance(account)` (`Client.get_account`,
parses `securitiesAccount.currentBalances.cashAvailableForTrading` — field
names follow Schwab's documented schema but are **unverified against a real
account response**, flagged for live confirmation, see `docs/live_test_coverage.md`).
Wired into `schwab_safety.check_order`'s single BUY chokepoint — every BUY-
placing path (`place_equity_buy`, `place_trailing_buy`, the top-up, the
gap-correction replacement) goes through it; confirmed by an independent Opus
review (2026-07-21) that no BUY path bypasses it. Requires
`cash_available >= notional + CASH_SAFETY_BUFFER` (`CASH_SAFETY_BUFFER = 200`,
a deliberate small flat cushion for per-order overage — fees/a quote-to-fill
price tick — instead of precise real-time accounting. Deliberately *not* a
restatement of the user's own much larger cash reserve habit (~$1,000 kept
in each account); `notional` is sized independently off `starting_notional`/
compounding logic, not off real cash, which is exactly why this check exists
at all — see `docs/automation_principles.md` #7a). Fails closed: any exception from
the balance fetch itself raises `SafetyViolation`, blocking the order rather
than letting it through unchecked. Four existing test files' fixtures
monkeypatch `get_account_balance` (large default) so existing BUY-path tests
don't hit a real API; 5 new dedicated tests in `test_schwab_safety.py`
(insufficient cash, buffer-on-top-of-notional, sufficient-cash passes,
balance-fetch-failure fails closed, SELL exempt). Full suite: 142 passed
(was 137).
**`CASH_SAFETY_BUFFER` was initially built as `1_000` — a bug, not the
intended design**: caught by the user immediately after review — the $1,000
was meant to be the user's own operational cash-reserve habit, not something
`check_order` enforces as a blocking requirement. Fixed same session:
`CASH_SAFETY_BUFFER` is `200` (a small per-order overage cushion, the only
thing actually enforced as a block), and a new, separate, **non-blocking**
`CASH_RESERVE_WATERMARK = 1_000` check posts an informational Slack warning
(💰 emoji) whenever `cash_available < CASH_RESERVE_WATERMARK` on a BUY check
— lets the user know to add cash before the reserve actually runs out,
without blocking the order. Deliberately checks the raw balance, not
"balance after this trade" (simpler, per user's own call — `automation_principles.md`
#7a). 2 new tests cover the warning firing and not firing. Full suite after
both fixes: 153 passed.
**Second real gap found by the Opus review and fixed same session**: Schwab
does not reserve buying power for a resting order (already known from the
existing same-ticker duplicate-order guard's own docstring), so **two
different live tickers sharing one account could each pass the cash check
against the same undecremented balance** — the flat $200 buffer alone
doesn't cover a second simultaneous BUY. Not reachable today (every live
ticker has its own account, per `_live_ticker_accounts`) but not structurally
enforced. Fixed with a real-order-book check (not a local heuristic, per
`automation_principles.md` #1): `schwab_safety._has_open_buy_order_in_account`
blocks a second concurrent BUY into an account that already has *any* other
ticker's resting BUY order, reusing the same `_open_orders()` fetch the
existing same-ticker guard (`_has_open_order`, refactored to share one API
call instead of two) already makes — no extra network cost. 3 new tests
cover this. Also tightened test hygiene as a side effect: the existing
same-ticker duplicate-order-book check was previously **hitting the real
Schwab API unmocked** in every BUY-path test (a pre-existing gap, not
introduced this session) — now mocked (`_open_orders` returns `[]` by
default) in all four fixtures, per `automation_principles.md` #8. Full
suite after this fix: 145 passed.
**Not yet fixed, two smaller residual findings from the same review, logged
as their own items below**: (1) the balance-fetch network call now happens
while `approve_and_record`'s cross-account file lock is held — a slow/hung
fetch would stall order processing for every account, not just the one being
checked; (2) `cashAvailableForTrading`'s exact field semantics (e.g. whether
it's margin-inclusive) are still unverified against a real account response.
**User's proposed empirical validation, still not built**: fund a real
(margin-enabled) account with a small amount (~$100), flip that one account's
`dry_run` to `False`, and place a single ~$100.50 order via a minimal
standalone script (bypassing `active_signals.py`/`signals_notify.py`/
`schwab_safety` entirely, calling `schwab_client.place_equity_buy` directly)
to observe Schwab's real behavior — this would be the first real (non-dry_run)
order this system has ever placed, so treat deliberately. Not scripted yet.
Related but distinct real-world note (not actionable code, confirm with
Schwab directly rather than assume): a short position from an oversell is
likely self-limiting in no-margin (IRA-type) accounts (order rejected, not
executed as a short), but could actually execute in the margin-enabled
account; unsure of Schwab's specific margin-call/PDT-flag policy for a brief
accidental short — don't assume benign without confirming.
Original framing (2026-07-21, before this fix): `schwab_client.py` had no
function at all to read an account's real cash balance, and
`schwab_safety.check_order` only enforced a fixed per-account `notional_cap`
(a static dollar ceiling), never actual available cash — so any BUY order
(including Part 3's top-up, which places a real order) exceeding real
available cash would either get rejected (no-margin accounts) or silently
draw on margin (margin-enabled accounts), with zero check on our side.
## ✅ [live-trading][security] Resolved 2026-07-21 — cash-balance network call held inside `approve_and_record`'s cross-account file lock
Found during the Opus review of the cash-balance check (resolved item
above). `check_order`'s cash-availability check (and the order-book fetch
for the duplicate-BUY guard) run while `approve_and_record` holds
`_open_locked()` — a global lock shared across every account. A slow or
hung Schwab call would stall order processing for every account's order,
not just the current one, for as long as schwab-py's 30s default httpx
timeout takes to fire. Fixed per the proportionate option flagged at the
time (`automation_principles.md` #7a): `schwab_client._get_client()` now
calls `client.set_timeout(_CLIENT_TIMEOUT_SECS)` (10.0s) once at client
creation, bounding every Schwab HTTP call — not just the two under the
lock — to 10s instead of 30s. Simpler than restructuring lock ordering,
same failure mode either way (fails closed once the call errors out).
New `tests/test_schwab_client.py::test_get_client_applies_short_timeout`.
Not fully closed: still bounded by 10s, not eliminated — restructuring the
lock so only the count/cap bookkeeping (not the network calls) runs under
it remains a further option if 10s is ever observed to matter in practice.
## ✅ [live-trading][security] Resolved 2026-07-21 — pre-existing test hygiene gap: some `schwab_safety` tests were silently hitting the real Schwab API
Found during the Opus review of the cash-balance check above. The same-ticker
duplicate-order-book check (`_has_open_order`, now refactored to share a
fetch with `_has_open_buy_order_in_account`) was being exercised in every
BUY-path test via a real, unmocked `get_orders_for_account` call against
whatever the actual `ira` account's real order book happened to contain at
test-run time. Ran the follow-up sweep this item asked for: every test file
that imports `schwab_safety`/`schwab_client`
(`test_schwab_safety.py`/`test_part3_gap_resize.py`/`test_part4_entry_trigger.py`/
`test_schwab_automation.py`/the new `test_schwab_client.py`) already mocks
`schwab_safety._open_orders` and `schwab_client.get_filled_order` in its
fixture — no stragglers found, nothing left to fix.
## ✅ [live-trading][security] Resolved 2026-07-21 — `active_signals.run_loop` fault tolerance built: per-section isolation + outer last-resort net
Full original context preserved below the line. Fixed this session, using
the granularity decision the item left open: **both** approaches, not one or
the other. A new `_guarded(section, fn, *args, **kwargs)` helper
(`active_signals.py`, just above `run_loop`) wraps every previously-unguarded
loop-body section (reference report, gap-resize check, window alerts,
pinned exit-arm/entry scans, the per-position exit-check loop — now also
per-position isolated via a `_check_position_exit` helper so one bad
position doesn't stop the rest — paper-sell checks, reminders, auto-fill
checks, fill-queue drain, paper-buy updates, the per-node limit-fill loop —
similarly per-node isolated — and both buy-signal scans): catches and logs
any exception, posts a rate-limited Slack alert (15min cooldown per section,
so a persistent failure doesn't spam every poll) rather than silently
swallowing it (`automation_principles.md` #4), and lets the loop continue to
its next section/iteration. On top of that, the whole loop-body block is
still wrapped in one outer try/except as a last-resort net, catching
anything that slips through an individual guard (e.g. a bug in the glue code
between sections) so the daemon can never die from an unhandled exception in
`run_loop` — logs and posts a 🔴 Slack alert, then proceeds to the next poll.
**Not yet done**: no fault-injection tests exist yet exercising any of these
paths (each guard's behavior is straightforward enough to reason about, but
per `automation_principles.md` #8, everything should be testable — add tests
that inject a raising stub into a couple of the wrapped sections and confirm
the loop survives and alerts, rather than trusting the wrapping by
inspection alone). Not yet observed live either — see
`docs/live_test_coverage.md`.
Original framing (2026-07-21, before this fix): found while discussing how to
chaos-test resilience to real-world failures (bar/data timeouts, real-time
price request timeouts, position/order-book request timeouts). Two spots
already isolated failures correctly (`_refresh`'s per-ticker timeout+catch,
`_scan_pinned_entry`'s per-ticker try/except) but everything else in
`run_loop`'s `while True:` body had no exception handling at all, and there
was no top-level try/except around the loop body either — a single
unexpected exception anywhere would propagate all the way up and kill the
entire daemon process, stopping monitoring/protection for every open
position on every ticker, not just skipping the one thing that failed.
## ✅ [live-trading][security] Resolved 2026-07-22 — `schwab_safety`'s duplicate-order guard now confirms against Schwab's real order book, not just a local pre-flight record
Full original context preserved below the line. `approve_and_record` still
writes the order into its local `recent_orders` dedup list before the real
`place_order` call happens, but `check_order`'s duplicate check now only
blocks a matching retry for a **real (non-dry_run) account** if a new
`_broker_confirms_order` cross-check finds a genuinely-accepted (WORKING/
QUEUED/FILLED, not CANCELED/EXPIRED/REJECTED/REPLACED) order for that exact
ticker/side/quantity in Schwab's real order book (`_all_orders`, a new
unfiltered sibling of `_open_orders`) — so a failed/rejected/errored prior
attempt no longer wrongly blocks a legitimate retry. Dry-run accounts keep
the old pure-local-heuristic behavior unchanged (nothing real to verify
against). 2 new tests in `test_schwab_safety.py`. Full suite after this fix
and the critical trail_state fix below: 172 passed (was 164).
Original framing (2026-07-21): found while fixing Part 3's top-up to place a
real broker order. `approve_and_record` writes the order into its local
`recent_orders` dedup list *before* the real `place_order` call happens
(`schwab_client._place_equity_order`) — if that broker call then fails/times
out/is rejected, the guard still believes it succeeded, so a legitimate
retry within `DUPLICATE_ORDER_WINDOW_SECS` (60s) was wrongly blocked as a
duplicate. Not urgent at the time — no real order had been placed yet
(`dry_run=True` everywhere, still true).
## ✅ [live-trading][security] Resolved 2026-07-22 — CRITICAL: trailing-arm state clobber caused re-arming and duplicate live trailing-sell orders (oversell risk); found by a full-stack Opus review
Found via a scoped independent Opus review of the whole automation stack
(`schwab_client.py`, `schwab_safety.py`, `active_signals.py`,
`signals_notify.py`, `signals_compute.py`, `signals_db.py`,
`paper_trading.py`), requested to look at seams between features each
already individually reviewed in prior sessions. `check_sell_condition`
(`signals_compute.py`) persists the newly-armed state (`trailing=True,
peak=P`) to the DB correctly, but `notify_trailing_activated`
(`signals_notify.py`) then merged its reminder fields onto the **stale**
in-memory `pos['trail_state']` (the pre-arm copy the caller passed in) and
overwrote the DB with it — silently losing `trailing`/`peak` right after
arming. On the next bar close the position looked unarmed again, `check_exit`
re-armed it, and (since a TrailingBoth position entered via trailing-buy has
no `sl_order_id` to cancel) `_attempt_automated_sell` placed a **second**
live trailing-sell order for the same shares — an oversell risk if both
filled. **Fixed**: new `signals_db.get_position_by_id(position_id)` (fresh
single-row lookup by primary key); `notify_trailing_activated` now re-reads
the position before merging, so it starts from the real just-armed state,
not the stale one. 1 new regression test.

**Two more findings from the same review, fixed alongside it, per explicit
user direction on scope**:
- SELL-side order placement had **no resting-order duplicate guard at all**
  (only BUY did) — this is what let the state-clobber bug above actually
  stack two live orders. Fixed: new `schwab_safety._has_open_sell_order`,
  wired into `check_order` as a same-ticker (not account-wide, unlike the
  BUY guard — an unrelated resting BUY must not block closing a position)
  SELL-side check. 2 new tests.
- `_attempt_automated_sell` cancels a resting stop-loss *before* confirming
  the trailing-sell placed; if placement then fails, the position is left
  with zero protection and no automated way to recover it. **User's explicit
  call**: don't restructure the cancel/place ordering (no real alternative
  that avoids some window of risk) — instead just make sure the user is
  told what to do. Now posts a 🚨 UNPROTECTED Slack alert with the SL price
  to manually re-place. 1 new test.

Also fixed as its own item (poll-loop/Slack-handler race, user: "yeah i
think loop poll double open/close needs a fix"): `open_position`/
`close_position` (`signals_db.py`) each did a SELECT-then-act with no lock
spanning both statements — the poll loop thread and the Socket Mode Slack
handler thread (same process, `active_signals.py` starts both) could each
pass their own existence check before either committed, risking a duplicate
open or a racing double-close silently overwriting `trade_log`'s exit
fields with the losing thread's values. Fixed with a plain
`threading.Lock()` (`signals_db._position_lock`) — single-process/
multi-thread daemon, not the cross-process concern `schwab_safety`'s file
lock guards. `close_position` now also returns `True`/`False` (was
implicit `None`) and is a safe no-op if the row is already gone. 3 new
tests.

**Not fixed, explicit user call**: kill switch / per-ticker pause also
blocks *protective* sell orders, not just new entries (found by the same
review) — this is existing, understood behavior (turning the algo off means
accepting the exposure), not a bug to patch.

Full suite: 172 passed (was 164). `verify_trailing_buy_resolution.py`/
`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` clean post-fixes.
Nothing here has been observed live — every account still `dry_run=True`.
## ✅ [live-trading][security] Resolved 2026-07-22 — live-state reconciliation check built: detection + text-only proposed remediation, never auto-executes
Related idea from the item above, split out once actually built. Originally
scoped as: does `open_positions.shares` match the broker's real position
size, and does a resting protective order (SL or trailing-sell, matching the
position's current phase) actually exist at the broker for that exact
quantity? **User explicitly flagged the danger**: this must stay detection/
alert-only (Slack notify on mismatch) — an auto-correcting version would be
a new automated-trading decision layer on top of the ones this project has
already found real bugs in, and a false-positive mismatch (legitimate
slippage, a deliberate manual override, a timing lag) would trigger a real,
wrong, automated trade to "fix" something that wasn't actually broken.
**Refined at the time**: a hybrid middle ground — on a detected mismatch,
compute and post a *proposed* remediation (e.g. "top up N shares" / "place
missing SL at $X") to Slack, rather than either silent auto-correction or a
bare alert with no suggested fix.

**Built 2026-07-22, scoped down to text-only** (confirmed with the user
before building: a clickable approve-button version would be a much bigger
build — new Slack handler, new execution path through `schwab_safety`,
needing its own dedicated safety review — deferred as a possible v2, not
built now). New `schwab_client.get_real_position(account, ticker)` (real
share quantity via `Client.get_account(fields=[Account.Fields.POSITIONS])`,
field names unverified against a real response, same caveat pattern as
`get_account_balance`). New `signals_notify.check_live_state_reconciliation`
(called every `run_loop` poll cycle via `_guarded`, automation-scope tickers
only — matches where the real order-placement risk actually is today):
compares broker-real shares vs. `open_positions.shares`, and whether the
expected resting order (SL pre-arm, trailing-sell post-arm) is actually
resting, via the existing `schwab_safety._open_orders` order-book fetch.
Posts a text-only Slack alert with the suggested fix on any mismatch —
never places an order itself. Broker treated as ground truth (automation_
principles.md #1); alerts rate-limited 15min per (position, mismatch-kind)
to avoid repeat-alert spam. 8 new tests (`tests/test_live_state_reconciliation.py`).
Full suite: 164 passed. `verify_trailing_buy_resolution.py`/
`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` clean (required
since `active_signals.py`/`signals_notify.py` changed).
Not yet done: no real mismatch has ever been observed live (every account
still `dry_run=True`) — tracked in `docs/live_test_coverage.md`.
## ✅ [backtest] Resolved 2026-07-18 — 5 tickers with a negative walk-forward fold (DPST, NUGT, RETL, UDOW, UVIX) sent to research, no per-ticker investigation
Follow-up from the walk-forward check (`docs/research_log.md` 2026-07-18 entry,
`logs/walk_forward_check.csv`). Each had exactly one mild negative-alpha fold out of 5
(DPST -11.9%, NUGT -7.2%, UDOW -3.6%, UVIX -4.0%, RETL -8.4%). **User decision: skip the
per-ticker regime investigation and just keep all 5 in research mode** rather than promoting
any of them further. NUGT/AGQ/GDXU/TQQQ/YANG were already `research`; RETL/UDOW/UVIX were
never in the live `watch_list` to begin with (screened candidates only). **DPST flipped
`live`→`research` this session** (`signals_db.set_node_mode(53, 'research')`, watchlist_id=9)
— it had been the leading candidate for the first taxable-brokerage-account ticker, but this
walk-forward flag plus its already-known thin trade count (chaos-monkey item, 2026-07-17)
was enough to deprioritize it without further digging. No live capital was at risk (DPST had
no open position at the time of the mode change).
## ✅ [backtest] Resolved 2026-07-18 — `signals_db.add_node`'s `fixed_sl` computation ignored the real per-node value for uses_fixed_sl strategies
`add_node` (`signals_db.py`) used to always compute `fixed_sl = _config_fixed_stop_loss()`
(reads `config.json`'s global `execution.fixed_stop_loss`) whenever
`strategies.uses_fixed_sl(strategy)` was true, regardless of what real per-node SL value the
caller actually wanted — no parameter existed to override it. Found 2026-07-18 promoting 19
tickers' v4 (SL=1%) nodes: every row came out with `fixed_sl=15.0` (the stale global default)
instead of the real `1.0`, silently wrong, no error; worked around that session by inserting
via direct SQL instead of `add_node`. **Fixed**: `add_node` gained a
`fixed_sl_override=None` parameter — when set, used instead of `_config_fixed_stop_loss()`.
`None` (the default) preserves old behavior for legacy v3.x callers.

## ✅ Backlog-hygiene pass, 2026-07-22 — five stale headings closed out, retroactively marked resolved
Found while doing a dedicated backlog-hygiene pass (flagged as a to-do at the end of the
prior session): these five `backlog_cache.md` headings described work that had actually
already been completed (sometimes by the item's own later progress notes, sometimes by a
separate later session), but the heading itself was never updated to say so — each one below
gets its own entry with the real resolution date and pointer.

## ✅ [live-trading][backtest] Resolved 2026-07-18 — v4 nodes (SL=1%, all 19 candidates) promoted into the real `watch_list` table, replacing v3.x SL=15% params
Heading previously read "High priority... not started, no `watch_list` rows changed yet" —
stale; its own later progress notes in the same entry already documented the real work: all
19 walk-forward-screened tickers' v4 winning nodes (`fixed_sl=1%`, pulled from
`backtest_cache` ranked by robust alpha) were inserted into watchlist 57 via direct SQL
(worked around the `add_node` `fixed_sl` bug, separately fixed the same session), each
annotated walk-forward-clean or flagged. All landed in `mode='research'`, not `mode='live'` —
"promoted into `watch_list`" meant the table, not live execution status; live/paused status is
tracked separately (see the watchlist-65 entry below).

## ✅ [live-trading] Superseded 2026-07-20 — watchlist 65 ("Live v5") is now the active watchlist, not watchlist 57 ("Live v4", GDXD+EDC only)
Heading previously described watchlist 57 (GDXD+EDC only) as "Active" — that was accurate as
of 2026-07-18 but is stale now. Watchlist 65 ("Live v5", 10 tickers: AGQ, DPST, GDXU, HIBL,
KORU, NUGT, SOXL, UDOW, USD, YANG) became the active watchlist 2026-07-20, selected from the
full 18-ticker v5 resweep by cliff-safe robust alpha (see the 2026-07-20 watchlist-65 entry
above). All 10 nodes are still `mode='research'` — nothing trades live yet. Watchlist 57's 18
v4 nodes are preserved, untouched, just no longer polled (same non-destructive
versioning pattern as every prior watchlist supersession). Always check `watchlists.is_active`
rather than trusting a hardcoded id anywhere in docs.

## ✅ [backtest] Resolved 2026-07-15/19/20 — trailing-buy fill logic kernel-correctness fix, executed in full across three sessions
Heading previously ended "Action needed: implement per the plan file. Not started" despite its
own later progress notes showing the opposite. Real timeline: kernel implemented 2026-07-15
(`_simulate_trail_both` computes possible/pessimistic/certain per node, island/cliff-safety
rank on `MIN` of the three, verified via `verify_v4_fill_bounds.py` across all 11 live nodes at
the time); `phase` column backfilled for the pre-tagging SOXL/KORU rows via
`scripts/backfill_v4_phase.py` (confirmed run clean, 0 rows left untagged — `docs/
conversation_summary.md` session ~12); gap-through-trigger entry-side fix 2026-07-19 and
exit-side fix 2026-07-20 (both in `CLAUDE.md`'s Key Files); full 18-ticker v4/v5 resweep under
the corrected kernel completed 2026-07-20. Nothing outstanding from the original plan remains.

## ✅ [backtest] Resolved 2026-07-17 — `entry_timing=open_check` live-actionable analog built
Heading previously said "Not started, not scoped beyond the shape above" — stale; the exact
proposed fix (a second poll window per signal time, ~9:31-9:40/14:31-14:40 alongside the
existing close-check windows, reusing `compute_buy_signal`, with `buy_alerted` dedup
preventing a double-fire) was built the same session as the GDXD promotion (`active_signals.
_OPEN_CHECK_WINDOWS`, `watch_list.entry_timing` column). Confirmed still live and in use today:
all 18 watchlist-65/57 v4 nodes use `entry_timing='open_check'` (`CLAUDE.md`'s Live Trading
section).

## ✅ [live-trading][security] Status corrected 2026-07-22 — one-account-per-ticker: infra built, rollout ongoing (not "not started")
Heading previously ended "Not started, no code changes yet" — stale; substantial
infrastructure now exists. `schwab_safety.ACCOUNTS` holds 4 account slots (brokerage/sep/
roth/ira) each with its own `AccountLimits` (`notional_cap`, `daily_order_cap`, `account_type`);
`watch_list.account` is a real per-node column; `_live_ticker_accounts()` derives the live
ticker→account mapping fresh from `mode='live'` rows (currently empty — no ticker is
`mode='live'` yet, so nothing is actually exercising per-ticker isolation live). The originally
floated "account nickname = ticker symbol" placeholder naming was not what shipped — actual
accounts are named by role (brokerage/sep/roth/ira), with `watch_list.account` doing the
ticker→account assignment instead. A 5th account (limited-margin IRA, funded 2026-07-22) is
queued to join this set once API token scope + compliance permission clear (see
`project_new_ira_account_status` memory) — rollout is incremental, not a single completed
migration, so this item stays open in spirit (tracking new-account onboarding) even though the
core mechanism is done.

## ✅ [live-trading][security] Resolved 2026-07-22 — `same_day_block` account-type-awareness
`schwab_safety.AccountLimits` gained `account_type` (`'cash'` or `'margin'` — regular and IRA
limited margin treated identically, since both lack the T+1 cash-settlement restriction the
same-day-rebuy check exists for). `ACCOUNTS`: brokerage is `'margin'`; sep/roth/ira are `'cash'`,
confirmed by the user. `check_order`'s same-day-rebuy check now only fires for `'cash'` accounts.
A blanket "real orders only in a confirmed margin account" gate was considered and explicitly
rejected mid-build (caused 2 real test failures before being reverted) — the user's account model
is one account per ticker, capital-capped and expanded by funding a new account rather than
liquidating an existing one (see `project_account_segregation_model` memory), so a hard
margin-only gate would've locked automation out of every existing account. 1 new test
(`test_same_day_rebuy_not_blocked_in_margin_account`). The new (5th) limited-margin IRA funded
2026-07-22 isn't in `ACCOUNTS` yet — blocked on Schwab API token scope + compliance trading
permission. Full suite: 177 passed.

## ✅ [backtest] Resolved 2026-07-22 — `auto_adjust`/split-guard reconciliation closed; data traceability (not full immutability) chosen as the design direction
Reconciled the open 2026-07-16 question (why did `data_manager.py`'s split-guard rescue still
fire for KORU if `yf.download()` already defaults to `auto_adjust=True`?): `auto_adjust=True`
only adjusts the window being fetched *right now* for corporate actions known as of today, not
rows already sitting in the local cache from a prior fetch — a new split therefore still produces
a scale cliff between stale-scale cached rows and new-scale freshly-fetched rows, which is exactly
what the split-guard detects and rescales the whole file to fix. Guard is real work, not dead
code. Since the rescale is one multiplicative factor across the whole series, downstream
%-based signals (z-score, SL/TP/arm, returns) are scale-invariant to it, so past `backtest_cache`
numbers should stay reproducible by re-running the same code. Corrected the stale in-code comment
at `data_manager.py:113-124` that had claimed the opposite (that yfinance's hourly interval isn't
retroactively split-adjusted at all). Full writeup: `docs/research_log.md`'s 2026-07-22 entry.
**Real, distinct gap raised in the same discussion, backlogged (medium priority, doesn't block
trading) rather than built**: no archived record of the exact cache-file data that fed any past
backtest — mutated in place on every split-guard rescale. User's explicit call: traceability (know
when/why/how data changed) over full immutable/versioned data linked to each `backtest_cache` row
— the latter would be real reproducibility but a much bigger lift (schema change across the whole
sweep engine, real storage growth), and isn't needed if the scale-invariance argument above holds.
Design agreed for later: a `data_mutation_log` table in `trading_universe.db` (via `db_cache.py`),
one row per split-guard rescale event with a `pre_mutation_snapshot` of the actual old data (not
just metadata about the change) — cheap since these events are rare. Not yet built.

## ✅ [live-trading][backtest] Resolved 2026-07-20 — watchlist 65 candidate testing complete; found+fixed a live/paper compliance gap and a `create_watchlist` id-burning bug
Full writeup in `docs/research_log.md` ("2026-07-20 (cont.) — Watchlist 65 candidate
testing: chaos-monkey, Stage 5 compliance gap found+fixed, watchlists.id gap
root-caused+fixed"). Short version: chaos-monkey execution-adherence extended to cover
`TrailingExitZScoreBreakout` and `open_check` (previously TB/close-only), repointed at
watchlist 65 — no node collapses even at 20% miss rate, USD's thin sample cleared. Stage
5 compliance recheck found `strategies.py::check_exit` (used by both real live positions
and paper trading) never received the 2026-07-20 exit-side gap-through-trigger kernel
fix — fixed by threading a real `open_price` through `check_sell_condition` from both
call sites and adding the Open-first gap check to `check_exit`. Verified via full
bar-by-bar parity against real historical trades (280 total across SOXL/AGQ, effectively
zero mismatches). Also found+fixed, while investigating the unexplained watchlist id
57->65 jump: `signals_db.create_watchlist`'s `INSERT OR IGNORE` silently burns an
AUTOINCREMENT id on any duplicate-name call even though no row is written (reproduced
directly) — fixed to check existence first. No node params changed; watchlist 65 is
candidate-tested and clear of blockers.

## ✅ [backtest] Resolved 2026-07-20 — last-window market-on-close vs trailing-buy comparison, MOC does not win
Full writeup in `docs/research_log.md` ("2026-07-20 — Last-window market-on-close vs
trailing-buy comparison"). Short version: for signals firing in the last daily window
(the 14:30 bar, no time left before the overnight gap), compared TB's real bounce-fill
against an MOC counterfactual entry at the same signal bar's Close, across all 4
`TrailingBothZScoreBreakout` tickers in the Live v5 watchlist (GDXU, HIBL, SOXL, USD) and
all 3 fill resolutions (possible/pessimistic/certain). TB won every ticker/resolution,
often by 5-20x on compounded return, driven by a fat right tail of large winners MOC's
earlier entry misses. Hand-verified 2 SOXL trades entry-to-exit against real OHLC bars —
every fill price traced exactly to a real Open/High/Low/Close. No 18-ticker backfill
run — the result was decisive enough on the current watchlist alone; treated as closed.
New tooling: `scripts/sim_close_vs_trail_buy.py` + `export_trades.
collect_last_window_comparisons`/`simulate_trail_both_signal_tracked`/
`_simulate_exit_from_entry`.

## ✅ Resolved 2026-07-20 — full 18-ticker v5 resweep completed; immediate-entry vs trailing-buy comparison; "Live v5" watchlist built

The interrupted resweep (started 2026-07-20, see prior entry below) was completed the same
day using the new per-ticker resumable queue tooling (`scripts/run_sweep_queue.sh` +
`scripts/campaign_config.py`) instead of the old batch `run_v4_full18_resweep.sh`/
`run_v5_full18_test.sh` scripts. Full writeup, including the immediate-entry
(`TrailingExitZScoreBreakout`) vs trailing-buy (`TrailingBothZScoreBreakout`) cliff-safety
comparison and two rounds of self-caught errors in that comparison (unfiltered-alpha
mistake, then a summary-sentence contradicting the very table it was drawn from), is in
`docs/research_log.md` ("2026-07-20 — Immediate-entry (TrailingExit) vs trailing-buy
(TrailingBoth) full 18-ticker v5 comparison").

New standing tool: `scripts/campaign_comparison_table.py` — prints the side-by-side
TB-vs-TE comparison table (best alpha, worst_neighbor/cliff status, full node params,
trades, win rate, liquidity) directly from `backtest_cache`, with a `--min-best` filter.
Built because this exact table got hand-rebuilt piece by piece several times in
conversation before being made a real script — use this going forward instead of
re-deriving it ad hoc.

**Outcome**: 10 tickers selected for a new "Live v5" watchlist (`watchlist_id=65`,
created+activated this session, superseding `watchlist_id=57` "Live v4"): AGQ, DPST,
GDXU, HIBL, KORU, NUGT, SOXL, UDOW, USD, YANG — split 4 `TrailingBothZScoreBreakout`
(GDXU/HIBL/SOXL/USD) and 6 `TrailingExitZScoreBreakout` (AGQ/DPST/KORU/NUGT/UDOW/YANG),
picked per-ticker by whichever strategy had the higher cliff-safe robust alpha at
`fixed_sl∈{1,2,3}`. TQQQ and LABU had a viable (cliff-safe) TE node but were dropped by
user call anyway. DUST/GDXD/NAIL/RETL/UVIX/ZSL excluded entirely — no cliff-safe node in
either strategy at this SL range. All 10 new nodes inserted via
`scripts/build_v5_watchlist.py` in `mode='research'` (not live, not automation-enabled) —
see the still-open candidate-testing item in `docs/backlog_cache.md` for what's needed
before any of these go further.

Also fixed while building the comparison table: `TrailingBothZScoreBreakout`'s swept
"stop_loss" grid axis is actually `trail_buy_pct` (real `stop_loss` column holds the
constant `fixed_sl`) — an ad hoc query that used the literal `stop_loss` column for the
cliff-neighborhood check produced numbers that disagreed with the real
`identify_full_mesh_candidates` log output until this was caught and fixed.

## ✅ [live-trading][security] Resolved 2026-07-21 — SELL-side automated-order attempt is now mode-gated, not just ticker-gated

Found 2026-07-19 while reviewing whether EDC (real open position, `research`-mode node) could
safely join the widened automation scope. `notify_trailing_activated` (`signals_notify.py`) is
called unconditionally from the real `open_positions` exit-check loop in `run_loop` and calls
`_attempt_automated_sell(pos, current_price)` with no check on the position's node `mode` —
only `_attempt_automated_sell`'s own `ticker not in AUTOMATION_ENABLED_TICKERS` gate stood
between a `research`-mode ticker's real open position and a real/dry-run automated sell
attempt. The BUY side didn't have this gap: `_scan_buy_signals` only reaches
`_attempt_automated_buy` when `node.get('mode')=='live'`. This is why EDC's node was removed
from `watch_list` rather than folded into the widened automation scope at the time — adding
its ticker to `AUTOMATION_ENABLED_TICKERS` while its real position's node was still `research`
would have exposed that position's exit to the automated-sell path.

**Fixed 2026-07-21**: `_attempt_automated_sell` now looks up the position's own `(ticker,
window)` node from `db.get_watchlist()` and only proceeds when a matching node exists with
`mode=='live'` — falling back to the manual flow (not KeyError) both when the mode doesn't
match and when no matching node exists at all (e.g. a since-removed node, mirroring EDC's
removal). Matches `automation_principles.md` #7 (new automation surfaces inherit the full
existing gating, not a subset). Two new tests in `tests/test_schwab_automation.py`:
`test_automated_sell_falls_back_when_node_mode_not_live`,
`test_automated_sell_falls_back_when_no_matching_node`. Full suite: 156 passed.

## [live-trading] Low priority, 2026-07-19 — paper-trading dedup is ticker-only, not `(ticker, window)`-aware

`paper_trading.start_paper_buy`'s dedup guard (`db.get_paper_pending_buy(ticker)` /
`db.get_open_position(ticker, paper=True)`) is a single-ticker lookup — unlike the real
`open_position()`, which dedups on `(ticker, window)` (`signals_db.py:737-738`, itself not
`node.id`-based, so two nodes sharing a `window` value would still collide there too). Fine
today since every automation-enabled ticker has exactly one node. Would need to become
`(ticker, window)`-aware before a ticker could ever run two nodes (e.g. a legacy v3.x node
alongside a new v4 node) through paper trading simultaneously — the second node's BUY
signal would otherwise be silently dropped as a false "already open" duplicate. Not
scoped, no ticker needs this today.

## Default rule: Slack-notify on every action-requiring state change (2026-07-07)

Established as a standing convention, not a one-off feature: any state transition in `active_signals.py` that requires the user to do something (place/cancel an order, confirm a fill, etc.) must have a corresponding Slack notification. Prompted by HIBL's arm-sell threshold crossing without an alert — but on investigation, the notification already exists (`notify_trailing_activated()`, fires on `just_activated_trailing` at `active_signals.py:1582-1583`) and today's miss was purely because the daemon was down the whole time since HIBL's buy signal (same root cause as the heartbeat/watchdog gap below), not a missing notification path.

Current coverage, for reference: buy signal (`notify_buy_signal`), sell signal/SL/TP/TIME/TRAIL (`notify_sell_signal`), trailing armed (`notify_trailing_activated`), limit fill (`notify_limit_filled`) all exist and are wired into `run_loop`. Action item going forward: any new state/strategy added to `active_signals.py` must include a matching Slack notification before being considered done — audit this against the rule at that time, don't assume coverage.

## Out-of-band heartbeat/watchdog for active_signals.py (2026-07-07)

Daemon crashed today mid-session (the `tickers`-table bug during the DB split) and there was no independent alert — Slack itself is posted *by* the daemon, so it can't reliably alert on its own death. Needs a separate standalone process: a lightweight watchdog (own process, cron or sleep-loop) that checks a heartbeat (daemon touches a file/DB row every poll cycle) and posts to Slack via its own independent webhook call if the heartbeat goes stale during market hours — so a bug in the main daemon can't take down its own alerting. Not started.

## ✅ Resolved 2026-07-12/13 — "What's close" proximity script

Covered by a different mechanism than originally scoped: the reference report's `build_reference_table` (`active_signals.py`) already shows per-ticker buy-trigger/arm-trigger distance (signed `Proximity` column) for every `mode='live'` node, and the 2026-07-12 "Resend Report" button lets the user trigger a fresh one on demand from Slack — no standalone script or slash command needed. Live-verified via multiple real sends.

## ✅ Resolved 2026-07-13 — Add account tracking (Brokerage/SEP/IRA/Roth) for portfolio performance

Built as originally scoped: `account` column added to both `open_positions` and `trade_log` (`trading_live.db`, migration in `ensure_tables()`, backup taken first — `cache/trading_live.db.bak_pre_account_migration_20260713`), threaded through `open_position()`/`log_trade_entry()` so the account is captured at execution time from the node's current `watch_list.account` value (survives later account reassignment, e.g. LABU's ira→roth move — old trades keep the account they were actually placed in). `pages/10_Open_Positions.py` shows an Account column. `pages/4_Portfolio.py` gained a new "Account Performance (live)" section: per-account realized trade count/win rate/compounded return from `trade_log`, plus open-position count/unrealized $ P&L from `open_positions` (current price via yfinance). Smoke-tested (HTTP 200, no traceback) and spot-checked against the real DB.

**Caveat**: only trades placed 2026-07-13 onward carry a real account value — the 4 positions open before the migration (AGQ/HIBL/EDC/SOXL) show as `unknown` since `account` was NULL at insert time; no historical backfill is possible (the value wasn't captured anywhere before now). The Portfolio page surfaces this as a caption when `unknown` rows exist.

## ✅ Done 2026-07-07 (evening, while user away) — Split trading_universe.db into live/research files; folded Watchlist Trade Pivot into Top Pivot; trail_pct rename still deferred

**DB split**: Built `cache/trading_live.db` as a **copy** of `watchlists`/`watch_list`/`open_positions`/`trade_log` (4/47/2/3 rows) — `cache/trading_universe.db` untouched, still holds `backtest_cache`/`hurst_cache`/`tickers` plus the original (now-redundant) copies of the live tables. Repointed `active_signals.py`'s `DB_PATH` to `trading_live.db`, added `RESEARCH_DB_PATH` for the one `hurst_cache` lookup it makes. Repointed `pages/10_Open_Positions.py`, and split `pages/4_Portfolio.py`/`pages/0_Top_Pivot.py`/`scripts/post_sweep_report.py` into dual-path (live table queries → `trading_live.db`, `backtest_cache` queries → `trading_universe.db`); `pages/0_Top_Pivot.py`'s watch_list/backtest_cache join now uses `ATTACH DATABASE` across the two files (verified working). `scripts/verify_live_parity.py` and `scripts/live_test.py` needed no changes (already DB-path-agnostic via monkeypatch / re-import).

**Correction to the original split plan**: `kv_cache` stays in the research file, not the live one — every `db_cache.py::refresh_*_cache()` function populates it from `backtest_cache` queries and it's consumed only by research-side Streamlit pages (pivot/cliff-grid/dropdown caches), not by `active_signals.py` at all.

**Not yet cut over**: the already-running `active_signals.py run` process (started 06:04 same day) still has the old code loaded and is reading/writing `trading_universe.db`'s original live tables — it was deliberately left untouched (user explicitly declined a restart mid-day). The code changes above only take effect on next restart. **Before trusting `trading_live.db` in production**: restart `active_signals.py`, confirm it comes up reading the new file, then drop the now-redundant `watchlists`/`watch_list`/`open_positions`/`trade_log` tables from `trading_universe.db` (not done — old tables currently still exist there too, in sync as of the split moment but will drift once the new daemon starts writing to `trading_live.db` only).

**Watchlist Trade Pivot fold**: absorbed `pages/12_Watchlist_Trade_Pivot.py`'s WIN/LOSS/TWIN/TLOSS/OPEN summary + drill-down into a new section at the bottom of `pages/0_Top_Pivot.py` (still reading from the `cache/watchlist_sweep.db` sandbox, unchanged) and deleted the standalone test page. Verified both pages return HTTP 200 with no tracebacks via a headless Streamlit smoke test.

**trail_pct → trail_sell_pct rename**: still deferred, now for a sharper reason than "wait for off-hours" — traced through `active_signals.py`'s `run_loop` and found `check_sell_condition` is called with no try/except around it (`active_signals.py:1522-1541`), so renaming the column out from under the running daemon would cause an uncaught `KeyError` on the very next poll cycle and kill the entire process (no monitoring on any open position until manually restarted). Do this only immediately before a planned restart, not mid-session.

## ✅ Resolved 2026-07-12 — `trail_pct`/`take_profit` rename propagation

All previously-listed files done: `pages/2_Node_Inspector.py`, `pages/3_Winners.py`, `pages/4_Portfolio.py`, `pages/10_Open_Positions.py` (`take_profit`→`axis_tp`, `trail_pct`→`trail_sell_pct` in SQL), `scripts/export_cliff_safety.py` (same rename, plus fixed a pre-existing `sl_label`/`sl_display` NameError left over from the original commit), `scripts/verify_live_parity.py` (node dict key `trail_pct`→`trail_sell_pct` to match `active_signals.py`'s expected key). `scripts/fill_trail_pct_gaps.py` needed no change — doesn't touch those columns. **Note**: other pages (`8_ADF_Filter.py`, `11_Universe_Scan.py`, `1_Spatial_Topology.py`, `7_Hurst_Filter.py`, `scripts/profile_dispatch.py`, `scripts/post_sweep_report.py`, `scripts/top_safe_nodes.py`) still query raw `take_profit` but were never in scope — they only look at non-v3.x strategies where the column is still populated; revisit only if they ever need to show `TrailingBothZScoreBreakout` rows. **Exception unchanged**: `cache/watchlist_sweep.db` is a separate, never-migrated snapshot DB.

## ✅ Mostly resolved 2026-07-13 — Live/backtest parity gap (trailing-buy/sell fill-timing)

`TrailingBothZScoreBreakout`'s trailing-buy "wait for bounce" entry has no live-orchestration implementation (hands off to a broker-side trailing-buy order) — `scripts/verify_live_parity.py` deliberately can't compare it. Instead of waiting on real broker fill data, built `scripts/verify_trailing_buy_resolution.py`: re-detects every recent signal's bounce-entry using yfinance 5-min bars (real intra-hour tracking) and diffs against what the hourly-bar kernel (`_simulate_trail_both`) would catch for the same signal. After fixing a cutoff-time bug 2026-07-13 (see below), result across all 11 watchlist-9 tickers (130/130 signals matched, last ~58d): mean price diff +0.19%. **SOXL is still the real outlier**: +1.81% mean fill-price penalty (individual signals up to +15.5%), driven by its `trail_buy_pct=1%` being far tighter than its own ~3.65% median intra-hour swing (ratio 3.65) — volatile enough to cross/re-cross the trigger within an hour, so 5-min tracking locks in an earlier/worse fill than the hourly kernel models. TQQQ shows smaller +0.84% drift; everything else is at/near parity (AGQ actually skewed favorable, -2.01%). Formalized as a repeatable procedure in `docs/watchlist_candidate_checklist.md` (checks 2/3).

Built the mirror-image check for the **exit** side 2026-07-13, `scripts/verify_trailing_sell_resolution.py`: same idea, but re-detects the peak/trail_stop crossing once trailing arms, using 5-min bars vs. the hourly kernel's trailing branch. Result: 21/21 exits matched, mean diff -0.17% — trailing-sell is already at parity across the whole watchlist (unlike entries, live trailing-sell is monitored continuously by `active_signals.py` itself, not handed off blind to a broker order, so this check mainly validates the *backtest's* hourly-bar exit modeling rather than a live-execution gap). LABU showed -4.6% on a single sample — not enough data to call a real outlier yet.

**Real bug found and fixed while building the sell-side script**: `max_hold_hours` counts hourly *bars* (~7/trading day), not calendar hours — the original buy-side script's cutoff-time math (`signal_time + timedelta(hours=max_hold_hours)`) was computing a cutoff days too early for any trade near its actual max-hold window, silently reporting fabricated "ran out of data" exits instead of real ones. Fixed in both scripts (now look up the real bar timestamp via `timestamps[entry_i + max_hold_hours]`). Rerunning the buy-side script after the fix confirmed the original SOXL finding wasn't an artifact of this bug — numbers moved only slightly (130/130 matched vs. 134/138 with the shorter truncated dataset before the fix).

**Still not fully closed** (residual, carried forward, no dedicated tracking item since — fold in if this area comes up again): both checks validate the *price* assumption is broadly sound (except SOXL entries); they don't validate the broker's trailing-buy order mechanics themselves (whether Schwab's own `TRAILING_STOP` trigger/fill logic matches the running-low model at all) — that piece still has no real-fill-time-vs-signal-time verification against actual Schwab fills.

## ✅ Resolved (db round-trip coverage built) / Dead (Task Scheduler piece dropped) 2026-07-13/14 — Test coverage & heartbeat watchdog

**Automated round-trip DB test** (`add_node`→`open_position`→`check_sell_condition`, no coverage existed as of 2026-07-13): built — `tests/test_db_roundtrip.py` and `tests/test_signal_and_notifications.py` now cover this path. Resolved.

**Heartbeat/Task Scheduler watchdog** (`scripts/check_heartbeat.py`, posts a Slack alert if `active_signals.py`'s heartbeat file goes stale — meant to catch the daemon going silent, e.g. host sleep/suspend, without relying on the daemon itself to notice its own death): scoped in full (Task Scheduler setup walkthrough, "run only when logged on" vs. password-store tradeoff, retry/missed-start behavior) then **deliberately dropped 2026-07-13** — for the failure modes it would catch (sleep/network/power), the user has no way to act on the alert remotely while at work, so the alert would be pure unactionable stress. Root cause (WSL sleep during market hours) fixed directly via a Windows power-plan change instead. `check_heartbeat.py` itself was left ~80% built and still works standalone if ever revisited — same-session fix wrapped `main()` so an unhandled crash in the check itself also posts a Slack alert, not just the two originally-expected stale/missing paths. See `docs/design.md`'s Heartbeat section for the current authoritative note.

## ✅ Resolved 2026-07-14 — trailing-buy re-entry timing after a same-day exit

No bug — same-day re-entry uses the exact same two daily signal windows as any other entry
(`_simulate_trail_both`'s new-signal detection has no calendar-day reset or first-entry-of-
day special-casing). Full experiment writeup in `docs/research_log.md` (2026-07-14 entry).

## ✅ Resolved 2026-07-15 — Phase 3 (full parameter mesh) adds no value over Phase 1/2/2.5

Phase 3 won 0/30 tagged SOXL+KORU v4 SL-sweep campaigns — Phase 1 or Phase 2 always held
the best robust-alpha node. `--max-phase` cap added to `run_optimization_sweep.py` (default
3, unchanged behavior) so future runs can skip it. Full experiment writeup in
`docs/research_log.md` (2026-07-15 entry).

## ✅ Resolved 2026-07-17 — same-day buy→sell block explored and deliberately not built

PDT rule eliminated by FINRA effective 2026-06-04 (Regulatory Notice 26-10); no broker or
regulatory reason for a same-day round-trip block remains. Real cost of blocking anyway is
high (GDXD retains only ~47% of edge if same-day exits are deferred to the next day).
Decision: proceed without a block. `schwab_safety.py`'s existing `same_day_block` (blocks
same-day *re-buy*, a different, unrelated direction) is untouched, still enforced live.
Full experiment writeup in `docs/research_log.md` (2026-07-17 entry).

## ✅ Done 2026-07-07 — Composite index `idx_bc_ticker_strategy_version` on `backtest_cache`

Added `CREATE INDEX idx_bc_ticker_strategy_version ON backtest_cache(ticker, strategy, version)` to both `cache/trading_universe.db` (86.2M rows, 430s to build) and the new `cache/watchlist_sweep.db` sandbox (34.7M rows, 49s). Found while an ad-hoc `ticker IN (...) AND strategy=? AND version LIKE 'v3.%'` query was scanning most of the table — `EXPLAIN QUERY PLAN` showed it falling back to the PK's autoindex (`strategy` is the PK's only usable leading column for that filter shape); none of the 4 existing secondary indexes had `ticker` paired with `strategy`/`version`. This index makes that exact filter shape (ticker-set + strategy + version-prefix) a pure index scan going forward.

## Watchlist-scoped trade-cache sandbox — new pattern, to formalize/absorb later (2026-07-07)

Built `cache/watchlist_sweep.db` as a disposable snapshot (not part of the live/research split below — a throwaway sandbox for prototyping) containing: a scoped `backtest_cache` subset (`TrailingBothZScoreBreakout`, v3.x, the 11 current watchlist tickers only — 34.7M rows), a copy of `watch_list` for `watchlist_id=9` ("Sweep v3 - Full"), and a new `trade_cache` table (one row per individual trade — entry/exit time/price, hours_held, Result WIN/LOSS/TWIN/TLOSS/OPEN, Return — keyed by full node identity) populated by running the actual kernel (`backtester.run_backtest_dispatch`) once per node rather than trusting `backtest_cache`'s aggregate `win_rate`/`win_twin_rate` columns (which can be stale — see finding below).

Built `pages/12_Watchlist_Trade_Pivot.py` as a **test page** against this sandbox: per-node WIN/LOSS/TWIN/TLOSS/OPEN breakdown + compounded return, with a drill-down into individual cached trades. User's explicit plan: test it standalone, then absorb into the existing `pages/0_Top_Pivot.py` later rather than keeping it as a separate permanent page.

**Related finding, not a new bug**: `backtest_cache.win_twin_rate` reads as `0.0` for any row computed before that column existed (added in commit `252b3bf`) — `run_optimization_sweep.py:66-72` explicitly documents old rows are never recomputed retroactively. SOXL's v3.18 rows are affected. The `trade_cache` sandbox sidesteps this entirely by computing fresh from the kernel instead of reading the cached aggregate.

**Open question**: should this sandbox pattern (scoped snapshot + trade_cache) become the permanent shape of the "watchlist" tier in the live/research split below, or stay a one-off testing tool? Not decided.

## ✅ Closed 2026-07-07 — SOXL/KORU live exit-strategy decision (overtaken by events)

Was an open question about whether to switch SOXL/KORU's live exit params to better-backtesting candidate configs (SOXL v3.35, KORU v3.34) — see prior analysis in git history if ever needed. Overtaken by the 2026-07-07 market swing: SOXL hit its real stop-loss and was closed out (-15.38%, logged in `trade_log`); KORU also breached its stop but the user chose to hold through it manually, working the position directly rather than via a config swap. No longer an open decision.

## Rename trail_pct → trail_sell_pct; also take_profit → trail_arm_pct for TrailingBoth; split buy/sell display columns (2026-07-06, updated 2026-07-06)

`trail_pct` (exit-side trailing %) and `trail_buy_pct` (entry-side bounce %) are asymmetrically named — the sell-side one kept its original name from `TrailingExitZScoreBreakout` (v1.8, back when there was only one trailing mechanism), while the buy-side one got a distinct name when `TrailingBothZScoreBreakout` added a second trailing axis. User wants `trail_pct` renamed to `trail_sell_pct` for symmetry/clarity, so buy-side and sell-side code/display never need to be parsed apart from a combined string.

**Added while walking through a full `TrailingBothZScoreBreakout` trade end-to-end (2026-07-06, investigating why SOXL v3.35 showed a 6837% alpha vs v3.18's 2894% — turned out to be real market data, a genuine +182% single trade during SOXL's documented April 2026 rally, not a bug)**: `take_profit` is also mis-named for this strategy — it doesn't exit the trade, it arms the trailing-sell mechanism once cleared (`check_exit` sets `state['trailing']=True` at that threshold, then rides `peak - trail_pct%` from there). Rename to `trail_arm_pct` for this strategy's semantics. Full sequence should read, in order: `trail_buy_pct` (bounce entry confirmation) → `trail_arm_pct` (threshold that arms the trailing sell, was `take_profit`) → `trail_sell_pct` (trailing-stop width, was `trail_pct`).

Decided explicitly **not** to physically reorder the DB columns to match this sequence — SQLite column order doesn't affect queries, only reordering would require a full rebuild of the 146M-row `backtest_cache` table (same risk class as the REINDEX incident earlier this session). Instead: rename only, and get the bounce→arm→trail-sell reading order via explicit column ordering in `SELECT` statements and the Cliff Safety CSV export, not the physical schema.

Scope (real column in 3 DB tables — **`open_positions` currently holds live KORU/SOXL positions**, so this must not be done carelessly mid-trade):
- DB: `ALTER TABLE ... RENAME COLUMN trail_pct TO trail_sell_pct` on `backtest_cache`, `watch_list`, `open_positions` (SQLite supports this natively, no full rebuild). Also consider renaming `take_profit` → `trail_arm_pct`, though note `take_profit` is a genuinely shared/generic axis column reused with real take-profit semantics by other strategies (`ZScoreBreakout`, `LimitExitZScoreBreakout`, etc.) — a straight column rename would mis-name it for those, so this likely needs to stay a per-strategy *display/label* rename rather than a DB column rename, unless a fuller audit says otherwise. **Back up the DB file first**, per established practice ([[feedback_backup_before_schema_migration]]).
- Code: rename the `trail_pct` param/column references across `strategies.py`, `backtester.py`, `run_optimization_sweep.py`, `active_signals.py`, `pages/0_Top_Pivot.py`, `pages/2_Node_Inspector.py`, `pages/3_Winners.py`, `pages/4_Portfolio.py`, `scripts/export_cliff_safety.py`, `scripts/fill_trail_pct_gaps.py`, `scripts/verify_live_parity.py`, `tests/test_TrailingBuyZScoreBreakout.py` — do NOT touch `trail_buy_pct` (no substring collision, but double check regex/replace scope).
- Also fix `scripts/export_cliff_safety.py`'s/`pages/0_Top_Pivot.py`'s combined `sl_display` ("Bounce % / Trail %" as one string) to output separate `trail_buy_pct`/`trail_sell_pct` numeric columns instead, ordered bounce→arm→trail-sell — this was the immediate trigger (Excel-facing CSV was mixing buy and sell values into one field).
- Verify KORU/SOXL still read back correctly from `open_positions` after the rename before considering it done.

Deferred until after live positions are settled or clearly off-hours — not done same-session as opening those two positions.

## ✅ Done (mostly) — Split trading_universe.db into separate live/research DB files (2026-07-06, cutover verified 2026-07-07)

`cache/trading_live.db` (watchlists, watch_list, open_positions, trade_log) and `cache/trading_universe.db` (backtest_cache, hurst_cache, tickers, kv_cache) now fully split, `active_signals.py` repointed and verified reading/writing the live file exclusively (confirmed via `ensure_tables()` + live position exit-check exercise). **Not yet done**: the 4 stale duplicate live tables (`watch_list`/`watchlists`/`open_positions`/`trade_log`) are still physically present in `trading_universe.db`, unused — a backup exists (`cache/trading_universe.db.bak_pre_table_drop_20260707`) but the actual `DROP TABLE` never ran. Safe to do once the `arm_sell_pct` migration (see rename item) finishes — don't run concurrent writes against the same file. Also cosmetic: the research file is still literally named `trading_universe.db`, not renamed to `trading_research.db`.

## Dead 2026-07-13 — Slack slash-command interaction for the live trading app (2026-07-06)

Originally scoped commands: `/positions` (check open positions), `/watchlist` (watchlist/buy-trigger status), `/status` (general daemon health). Superseded piecemeal by the button-based interaction pattern built across many sessions instead of a slash-command design: `/positions` ≈ `scripts/open_positions_status.py` + the manual open/close buttons (2026-07-12); `/watchlist` ≈ the reference report's Proximity column + on-demand resend button (2026-07-12, see the resolved proximity-script entry above). `/status` has no direct equivalent — resending the reference report and getting silence is a de-facto down-detector, but it's passive (relies on the user thinking to check) rather than a proactive alert. That gap is really the same problem as the still-open "Out-of-band heartbeat/watchdog" item above, not a slash command — closing this out as dead rather than carrying a stale command-design item forward.

## Split active_signals.py into modules (2026-07-06)

`active_signals.py` (1680+ lines) has grown to hold everything: DB connection/schema, watchlist CRUD, positions CRUD, signal computation, chart generation, Slack messaging, and the `run_loop` daemon + CLI, all in one file. Split in two passes, lowest-risk first:

1. **Pass 1 — watchlist + positions**: pull `get_watchlists`/`get_active_watchlist_id`/`create_watchlist`/`delete_watchlist`/`set_active_watchlist`/`get_watchlist`/`add_node`/`remove_node`/`set_node_mode`/`label_node` into `watchlist.py`, and `get_open_positions`/`update_position_trail_state`/`open_position`/`close_position`/`log_trade_entry`/`log_trade_exit` into `positions.py`. Both are pure DB CRUD, no coupling to signal logic — safest to extract first.
2. **Pass 2 — db + notify**: pull `_conn`/`ensure_tables` into `db.py` (shared base everything else imports), and the Slack/chart layer (`_post_message`, `_build_buy_blocks`, `_build_sell_blocks`, `notify_buy_signal`/`notify_sell_signal`/`notify_trailing_activated`/`notify_limit_fill`, `_chart_buy`/`_chart_sell`/`_upload_chart`) into `notify.py`.

Leaves `compute_buy_signal`/`check_sell_condition`/`run_loop`/CLI in `active_signals.py` (or a renamed daemon module) as the actual trading-logic core. Not started — deferred, live trading week takes priority.

## ✅ Done — Live-test the watchlist (2026-07-07)

Sweep 3 / "Sweep v3 - Full" watchlist is live and has been traded through a real market swing (SOXL stopped out, KORU held through a breach, HIBL entered and armed its trailing sell) — the priority this note flagged is complete.

**Milestone marker**: once the axis-schema cleanup (2026-07-05 — `strategies.py` class attributes + `validate_axis_values`, consolidating the 5 duplicated `_resolve_axis_columns`/`uses_fixed_sl` copies) and this live-test session both land, that's the end of `docs/operational_limits.md`'s Phase 1 (Manual Execution). Revisit then whether to relabel/split phases (e.g. current work retroactively "Phase 0" bootstrapping vs. a "Phase 1" that starts once Sweep 3 is the confirmed live watchlist) and define what Phase 2 actually covers (automation — see [[project_execution_automation_plan]]).

## Slack TEST MODE marker for manual notify_* testing (2026-07-05)

Found while manually testing `notify_buy_signal`/`notify_sell_signal` message rendering for SOXL/TQQQ: `SOCKET_MODE` is driven by real `.env` Slack credentials (bot/app token + `#trading` channel), so any ad-hoc script that calls a `notify_*` function posts to the real live channel with no indication it's a test — indistinguishable from an actual signal, including live Executed/Skipped buttons wired to real `open_position`/`close_position`. Add a `TEST_MODE` env var (or explicit param threaded through `_post_message`) that prefixes messages with something like "🧪 TEST MODE — " and swaps the action buttons for inert ones (or a plain-text context block) so manual testing can't be mistaken for — or accidentally trigger — a real trade.

## Automated round-trip test for active_signals.py (2026-07-05)

No test coverage exists for `active_signals.py`'s DB layer — `tests/` only exercises strategy kernels via fake dicts, never `add_node`/`open_position`/`check_sell_condition`. Found while quick-manually-testing the fixed_sl/trail_pct round-trip for a real v3.x trailing node (SOXL v3.18) and realizing there's no repeatable way to catch this axis-mapping bug class (already hit twice: the Portfolio page `load_watchlist_metrics` bug and the `dispatch_parallel_grid` `uses_fixed_sl` regression). Plan: pytest test monkeypatching `active_signals.DB_PATH` to a temp sqlite file, calling `ensure_tables()`, then driving `add_node()` with real v3.x trailing values → read back `watch_list` row → `open_position()` → read back `open_positions` row → `check_sell_condition()` with synthetic prices, asserting `trail_pct`/`fixed_sl`/`trail_buy_pct` survive each hop unmangled.

## 2026-07-05 (night) — win_twin_rate metric added; trail_pct sparse-then-fill extension; ALL53 ticker shorthand

- **Added `win_twin_rate` column to `backtest_cache`** (`run_optimization_sweep.py::init_idempotent_db`, simple `ALTER TABLE`, no PK rebuild needed): the existing `win_rate` only counts `Result=='WIN'` exactly, silently excluding profitable `TIME`-exit trades (`TWIN`) — found while investigating why a KORU node showed a 21% win rate despite yielding about the same alpha as a 71%-win-rate node (turned out 71% of its trades were actually profitable, just via `TWIN`, not counted). `win_twin_rate = (WIN+TWIN)/trades` is the real "did this trade make money" rate; old rows keep `win_twin_rate=0` (not recomputed retroactively — would require re-simulating all historical trades). Threaded through `dispatch_parallel_grid`'s cache-hit path, live-compute path, both INSERT statements, and displayed in `pages/0_Top_Pivot.py`'s Cliff Safety table alongside the original `win_rate` (kept, not replaced).
- **Extended `scripts/run_v3_backfill_sweep.sh`** with every single-percent trail_pct version 8-30% (`version = trail_pct% + 20` — same formula the existing 1-7% versions already followed, v3.21=1%...v3.27=7%) after finding `TrailingExitZScoreBreakout`'s (v3.18) full 1-30% sweep did much better at wide trail_pct (9-24%) than `TrailingBoth`'s tested 1-7% range. Sparse set (9/12/15/18/21/24/27/30% = v3.29/32/35/38/41/44/47/50) planned to run first; all in-between single-percent versions are wired and ready with no further script edits needed.
- **Built `scripts/fill_trail_pct_gaps.py`**: reads whatever sparse trail_pct data exists per ticker, finds each ticker's best value so far, and prints (doesn't execute) the `run_v3_backfill_sweep.sh` commands to backfill its immediate ±1% neighbors — mirrors the sweep engine's own coarse-then-island refinement, applied to the trail_pct axis. Already found (before any sparse run) that EDC/HIBL/SOXL's best trail_pct sits at the edge of the already-dense 1-7% range (7%), meaning 8% (v3.28) is worth checking once actually run.
- **Added an `ALL53` ticker-arg shorthand** to `run_v3_backfill_sweep.sh` (`./scripts/run_v3_backfill_sweep.sh <version> ALL53`) expanding to the full 53-ticker universe (same list as `run_v2_backfill_sweep.sh`), for running the v3.x strategy family against all 53 tickers instead of just Sweep 3's 11.

## ✅ Done 2026-07-05 — `backtest_cache` schema migration: real named columns + trail_pct as a true 4th axis (v3.x)

Built the "real named columns" option (see `docs/design.md`'s "v3.x reparameterization" section for full detail): `backtest_cache` schema rebuilt (60,364,303 rows verified carried over unchanged, no data migration needed — v1.x/v2.x rows keep their old overloaded meaning untouched), `stop_loss` now always means real SL going forward, `trail_buy_pct`/`trail_pct` are real columns. Went further than the minimal fix: `trail_pct` is now a genuine swept 4th grid axis for `TrailingBothZScoreBreakout` (`hyperparameters.trail_pcts`), replacing the old one-full-backfill-per-value pattern (v2.13-v2.17). Also fixed a pre-existing bug found along the way: Node Inspector/Portfolio only ever dispatched to `run_backtest_v17`-or-`run_backtest`, silently wrong for all 4 trailing strategies — now share one dispatch function (`backtester.py::run_backtest_dispatch`) with the sweep engine. `watch_list`/`open_positions` got a matching `trail_buy_pct` column; `add_node()` accepts real values for v3.x, falls back to legacy behavior for old nodes. v3.x backfill scope resorted and simplified same day (see `docs/design.md` "Version Changelog") — single combined tp/sl grid everywhere, TrailingBoth's trail_pct broken into per-value versions v3.21-27 instead of swept as a 4th axis in one run, restricted to Sweep 3's 11 tickers.

## ✅ Done 2026-07-05 — Fixed `add_node()`'s legacy fallback mis-mapping trail_buy_pct/trail_pct for `TrailingBothZScoreBreakout`

Found while asking "do any current watchlist winners sit in the 1/2/4/5 sl range" — the legacy fallback in `active_signals.py::add_node()` (added during the v3.x migration) always assumed the overloaded `stop_loss` value meant `trail_pct`, which is only true for `TrailingExitZScoreBreakout`/`LimitOrderTrailingExit`. For `TrailingBothZScoreBreakout` it actually means `trail_buy_pct`, with `trail_pct` a separate static per-version constant (v2.13=1%...v2.17=5%) that lived in `config.execution.trail_pct` at backfill time and can't be recovered from the row itself. All 8 Sweep 3 `TrailingBothZScoreBreakout` watch_list rows (AGQ/DPST/EDC/GDXU/HIBL/KORU/UVIX/YANG) had this backwards (`trail_buy_pct=0`, `trail_pct`=the real bounce value) — fixed in code (dispatches by strategy now, mirrors `run_optimization_sweep.py::_resolve_axis_columns`) and backfilled the 8 rows directly (backup at `cache/watch_list_backup_pre_trailfix_20260705.json`). No open position was affected (`open_positions` was empty throughout), but `check_sell_condition` would have used the wrong `trail_pct` for the first live trade on any of these 8 tickers. Also fixed `pages/0_Top_Pivot.py`'s "Cliff Safety" table, which displayed the raw overloaded `stop_loss` column with no indication of what it meant per strategy — now resolves and labels the real axis (SL / Bounce% / Trail% / Bounce%+Trail%) per row, and the neighbor-radius cliff query filters on the correct real column for v3.x rows instead of always `stop_loss`.

**Follow-up not done this session**: `pages/0_Top_Pivot.py`'s "Watchlist — Alpha by Strategy" section only queries `watchlist_id=1` and joins `backtest_cache`/`watch_list` on raw `b.stop_loss = w.stop_loss` — fine for that watchlist's real-SL strategies, but would break if it's ever extended to Sweep 3 (`watchlist_id=5`) or any future v3.x trailing-strategy node without the same axis-aware join fix.

## ✅ Done — Watchlist Repick (2026-07-04 → concluded 2026-07-07, Sweep 3/"Sweep v3 - Full" is live)

Was reviewing v2.x sweep results to repick the live watchlist (AGQ/EDC/FAS/HIBL). Entry/exit shorthand for reference: **Close** = bar-close confirmed entry, **Limit** = intrabar touch entry, **Trail** = trailing-buy bounce entry; **Fixed** = market exit at TP/SL, **Trail** = trailing-stop exit, **Limit** = limit-order exit. Existing versions: Close/Fixed=v1.5/2.5, Close/Trail=v1.8/2.8, Limit/Fixed=v1.7/2.7, Close/Limit=v2.12, Trail/Fixed=v1.9/2.9, Trail/Trail=v1.10/2.10. Concluded with the current v3.x, 10-ticker Sweep 3 watchlist now live. The "Research:" ideas below this point (post-loss cooldown, gap risk, seasonal patterns, etc.) are separate standalone research questions, still open.

Todo:
- **✅ Resolved 2026-07-05 — giving up on v2.4 (`TrendFilteredZScore`, 50-day SMA trend filter) for now**: didn't produce substantive signals. Not pursuing further.
- **✅ Resolved 2026-07-05 — giving up on limit-based order entry/exit variants for now**: v2.7 (`LimitOrderZScoreBreakout`) root-caused as structurally underperforming (see High Priority section), and unbuilt variants (Limit/Limit, Trail/Limit, v1.7-2, v1.7-3) aren't giving better signals than the close/trailing-based strategies already in place — same verdict as Hurst/ADF. Not pursuing further.
- **✅ Resolved 2026-07-05 — varying trailing-buy-entry % / trailing-sell-exit % already covered by v3.21-27**: `trail_buy_pct` is swept via the `sl` grid axis (combined grid incl. 1/2/4/5 low-end points) and `trail_pct` is swept 1-7% across the seven v3.21-27 versions (currently running backfill). No separate work needed.
- Revisit the design review list (`docs/code_review_findings.md`).

Research:
- **9:30am bar inclusion**: redoing charts/backtests to use the 9:30am bar (currently first bar is 10:30) is a big effort — changes every trading day from 7 bars to 8, requiring recalculation of max-hold-hours-to-bar-count conversions throughout `backtester.py`/sweep grids, plus re-fetching/re-caching hourly data with the new boundary. Flagged as research, not a quick todo.
- **Post-loss cooldown / trade freeze per ticker (2026-07-04)**: empirically checked the live watchlist's (AGQ/EDC/FAS/HIBL, v1.5 params) actual backtest trades — a large majority of losing exits are followed by a same-ticker re-entry within days (many same-day, in the next signal window), often at a *lower* price than the exit, not a recovered one. Mechanism: a stop-loss exit means price kept moving away from the entry, which against a lagging rolling SMA/std often makes the z-score deviation *larger*, so the same signal re-triggers almost immediately — the strategy has no memory of just having been stopped out. Proposed fix: block `check_signal` from returning BUY for N bars (e.g. 7) after that ticker's last exit. Caveat: this only delays re-entry, it doesn't prevent it if the ticker is still oversold once the freeze lifts — needs an actual kernel change (`_simulate`/`strategies.py`, new cooldown state) plus a backfill comparison against current results to know if it actually helps, not just a parameter tweak. (Side benefit: fewer close-in-time same-ticker round trips also reduces wash-sale frequency, though that's now less relevant since live testing is planned in IRA/Roth accounts — see `docs/operational_limits.md`.)
- Ways to avoid big losses, or strings of consecutive losses.
- Gap risk (overnight/weekend gaps through stop levels).
- Seasonal trade patterns.
- Tickers unrelated to a given ticker but highly correlated to its down-signals (cross-ticker signal).
- Split analysis into two halves — look for candidates whose edge holds in both halves (out-of-sample consistency) rather than just aggregate alpha.
- 3-month rolling window analysis — walk-forward validation instead of one static backtest window.
- Does higher liquidity correlate with the pattern holding up better?
- Why are some 3x leverage funds consistently more profitable (better alpha/win rate) than others across strategies/versions? Look for a common factor (underlying volatility/beta, decay from daily rebalancing, sector, liquidity, borrow cost) that predicts which leveraged ETFs are good candidates before running a full sweep on them.
- **KORU trail_pct=6-7% extension (2026-07-05)**: v2.13-17 (`TrailingBothZScoreBreakout`, trail_pct 1-5%) shows KORU's best node at 5% has 38 trades / 71% win rate, while 4 of the other 5 trail_pct values are stuck at exactly 25% win rate — likely a small-sample artifact (too few trades to mean anything at tight trail_pcts, not a real "gets worse" trend), not proof the pattern improves monotonically. Hypothesis: a tight trailing stop exits almost immediately on normal chop, so low trail_pct values barely get real trades at all; a wider stop gives the position room to actually ride the reversion, plausibly explaining both the trade-count jump and the win-rate jump together. Worth a single-ticker (KORU) rerun with `hyperparameters.trail_pcts=[1,2,3,4,5,6,7]` (now a real swept axis as of the v3.x reparameterization, see `docs/design.md`) to see whether 6-7% keeps improving, plateaus, or gives back — check trade counts at each value before trusting any win-rate comparison. Not built, one-off research item.

- **✅ Resolved 2026-07-04 — liquidity threshold (1% of 10-day avg $ volume) is fine as-is for lump-sum entry**: checked all four watchlist tickers' actual signal-window (10:25-10:40, 15:25-15:40) hourly $ volume against a $50k order, worst case HIBL's afternoon window at ~9.2% participation. Ran a square-root market-impact estimate (`impact ≈ k·σ·√participation`, using each ticker's own intrabar range as the volatility proxy) — worst-case estimated slippage ~0.4% (HIBL), everything else <0.3%. Negligible against 8-29% TP/SL bands and trade alpha in the tens-to-hundreds of percent. Tightening to 0.5% ADV would needlessly cut EDC/HIBL-type candidates for a cost that isn't real. No threshold change needed.

## High Priority

- **Trailing-buy entry fill assumes an optimistic (unproven) intrabar bar-path — needs a rerun of `_simulate_trail_buy`/`_simulate_trail_both` (v1.9/v1.10) with corrected fill logic (found 2026-07-10)**: `backtester.py`'s trailing-buy "waiting" loop (`_simulate_trail_both:602-616`, mirrored in `_simulate_trail_buy`) always updates `running_low` from the current bar's Low *before* checking whether the same bar's High clears the bounce trigger — i.e. it silently assumes the Low always happens before the High within every hourly bar, which is the best case for this strategy (lower running_low ⇒ easier/lower trigger to clear) and isn't knowable from OHLC bars alone. Built a side, read-only analysis in `scripts/export_trades.py` (`simulate_trail_both_ohlc_aware`, kept separate from the live kernel — nothing in `backtester.py`/`run_optimization_sweep.py` was touched) that resolves each bar's fill as **CERTAIN** wherever possible instead of guessing: (1) High clears the trigger from *prior* bars' already-confirmed low alone — certain regardless of this bar's own order; (2) after folding in this bar's own Low, the bar's **Close** itself clears the new trigger — certain, since Close is always chronologically last in the bar. Only when neither holds (a wick touches the trigger but Close pulls back before the bar ends, with no later bar ever confirming it) does it fall back to an Open/Close-direction heuristic — for SOXL that was just 1 of 61 entries (1.6%), so this is a solvable problem, not a fundamental blind spot.
  Result on SOXL (57-trade backtest, `TrailingBothZScoreBreakout`, current live watch_list params): 51/57 comparable trade entries shifted price/timing, and final compounded equity on $50k dropped from **$3.55M (7007% return) under the current optimistic kernel to $1.85M (3591%) under the certain-tiered logic** — a large, one-directional overstatement, not noise.
  **Action needed**: port the certain-tiered fill logic from `simulate_trail_both_ohlc_aware` back into the real numba kernels (`_simulate_trail_buy`, `_simulate_trail_both`) and rerun the full sweep for every `TrailingBuyZScoreBreakout`/`TrailingBothZScoreBreakout` node — this is the strategy family behind **all 11 tickers on the live watchlist (watchlist_id=9)**, so every live alpha/return number currently on file is inflated by an unquantified amount until this is redone. Related to (but a more precise, actionable version of) the existing "Live/backtest parity gap" P0 #3 note in `docs/backlog_cache.md` about the trailing-buy state machine having no live implementation to verify against — this finding is about the *backtest's own* fill assumption being optimistic, independent of live-parity. Not started.

- **✅ Look-ahead bias in every backtest's entry signal — fixed and tested**: Discovered 2026-07-03 — daily SMA/std indicators included the current day's own close, so intraday entry signals were scored against future information. Fixed in `backtester.py:24` (`prep_inputs`) to look up the *previous* day's indicator row, matching `active_signals.compute_buy_signal`'s live `today` cutoff. Verified via `scripts/verify_live_parity.py` (clean MATCH). Corrected data lives under the **v2.x** version namespace (v2.4-2.10 = same strategy mapping as v1.x); v1.x data left untouched for before/after comparison. Full details/quantified impact in git history if ever needed again.

- **Review P0 live-trading fixes one at a time**: 2026-07-03 follow-up session implemented fixes for code_review_findings.md P0 #1 (TIME exit wall-clock bug), #2 (fixed_sl/trail_pct round-trip), #4 (signal-window exact-minute bug), #5 (sell_alerted never cleared), #6 (app.py config corruption) — self-verified only (unit-level smoke tests + one real backfill), not yet walked through by the user. See "Fixed in follow-up session (2026-07-03, continued)" at the bottom of `docs/code_review_findings.md` for what changed and why. `active_signals.py` needs a restart once reviewed/accepted (live process won't pick up changes otherwise).

- **Manually test fixed_sl/trail_pct round-trip before promoting Sweep 3 (v3.18/v3.21-27) live**: P0 #2 fix (2026-07-03) adds `fixed_sl`/`trail_pct` columns and wires them through `add_node` → Slack BUY-button JSON → `open_position` → `check_sell_condition`, but this is currently **untested against a real trailing-strategy live position** — the current watchlist (AGQ/EDC/FAS/HIBL) is all v1.5, which doesn't exercise this code path at all. Superseded from v1.8/v1.9/v1.10 by v3.18 (`TrailingExitZScoreBreakout`)/v3.21-27 (`TrailingBothZScoreBreakout`) — needs thorough manual verification (add a node, confirm `watch_list`/`open_positions` get correct `fixed_sl`/`trail_pct` values, click through the actual Slack BUY button, verify `check_sell_condition` reads the right stop-loss) before trusting it with real money. **Planned as the focus of the next full session** — live-trading the watchlist end to end.

- **Dispatch overhead optimization**: Profiled 2026-07-03 (see `docs/dispatch_telemetry_results.md`) — the profiling script never actually measured DB-insert time; "88% result collection" is mostly just parallel kernel compute (~90% parallel efficiency), not IPC/pickling overhead. Batched the `backtest_cache` INSERT into `executemany()` (chunk size later bumped to 5000, running fine in production). No pre-change baseline was captured, so a before/after speed comparison isn't possible at this point — not worth chasing further unless a new perf question comes up.

- **Checkpoint 1 ticker-scoping bug — silently drops top candidates**: ✅ Fixed 2026-07-02. `identify_island_candidates()` (`run_optimization_sweep.py:350-360`) queried the entire historical `backtest_cache` for a version/strategy with no ticker filter, but `bh_cache` (gates Phase 2/3) is only built for the *current* run's `target_tickers`. Any run with a narrower ticker list than what's already cached from a prior broader run silently dropped legitimate top candidates at "No B&H data, skipping Phase 2" — no error, no warning surfaced anywhere except a log line. Confirmed it had happened across v1.6 (52x), v1.7 (27x), and v1.8 (3x) historically — including `MULL`, `VRTL`, `WULX`, `NBIZ`, `SMST`, the v1.6 top-5 alpha performers at the time. Fix: added `allowed_tickers` param to scope the Checkpoint 1 query to `ticker IN current tickers list`.

- **v1.9/v1.10 trailing buy strategies**: ✅ Built 2026-07-03. `TrailingBuyZScoreBreakout` (v1.9): after z-score signal, tracks running low and enters when price bounces `trail_buy_pct`% above it; fixed TP/SL exit. `TrailingBothZScoreBreakout` (v1.10): same trailing entry + trailing exit once TP activated (trail_pct=3% hardcoded). `sl` sweep axis → `trail_buy_pct` for both. Smoke test: AGQ/SOXL v1.10 beats v1.9; v1.8 beats v1.10 on AGQ but v1.10 beats v1.8 on SOXL — suggests ticker-dependent optimal. Sweep queued overnight.

- **Limit-order model variants (v1.7-2, v1.7-3) — ✅ abandoned 2026-07-05**: see Watchlist Repick section — giving up on limit-based order variants, not giving better signals.

- **Session cache two-file design**: Work account uses two files — one for session close handover, one (conversation_cache?) for top-10 session history loaded into conversation context. Need to check work account to reconstruct the design and port it here.

- **v1.6 coarse grid sweep**: ✅ Done. Step-3 [3,6,...,30] coarse + 3-island ±4 fine mesh + full mesh for cliff-safe top-10. Three-phase sweep engine built (`run_optimization_sweep.py`). v1.6 completed: 358 tickers coarse, 30 island mesh, 1 full mesh (WULX — only cliff-safe index/other candidate). SMST full mesh running separately.

- **Phase 2.5 bug — only sweeps best node's (w,z)**: Phase 2.5 runs a ±CLIFF_RADIUS TP/SL ±7h hold sweep around the true best node, but only for that node's (w, z) combo. Should sweep all 3 island centers across all (w, z) combos so cliff check has complete neighborhood data for every candidate.

- **Sweep run registry**: Add `sweep_runs` table to DB — one row per sweep execution with `run_id`, `version`, `timestamp`, `config_json` snapshot, `notes`, `phase_reached`. Lets you record why each version was run and reconstruct config if needed. Wire into sweep engine to auto-insert on start/finish. Concrete case for why this matters (2026-07-02): discovered v1.6 only partially copied v1.5.1's EDC/FAS data (AGQ fully copied — 72k/72k rows match exactly; EDC/FAS only 8k/72k copied) with no record anywhere of why, or that a copy even happened — took manual SQL archaeology to reconstruct. A run registry would have made this a one-row lookup instead.

- **Cliff check improvements**: Current `CLIFF_RADIUS=2`, `AND trades > 0` excludes NO_TRADES nodes. Consider: (1) include NO_TRADES as alpha=0 so cliff detection catches edges where signal disappears; (2) widen radius to 3 for coarse-only data where ±2 may miss real neighbors. v1.5 cliff check: 25/340 tickers safe — VRTL, WULX, CIFG, GEVX, CRDU are top safe candidates.

- **v1.7 limit order entry model**: ✅ Built. `LimitOrderZScoreBreakout` — fill on `Low <= lower_band` intrabar at `lower_band` price; intrabar stop loss checks `Low <= stop_price`; TP checks `Close >= tp_price` at bar close. New `_simulate_limit` Numba kernel + `run_backtest_v17`. Grid: w=[10,20], z=[1.0,1.5,2.0], TP/SL=[3,6,...,30], Hold=[7,14,...,140].

- **v1.8 trailing exit**: ✅ Sweep wired. `TrailingExitZScoreBreakout` — close-based entry (v1.5 style), trailing stop once TP% cleared. `trail_pct` replaces `stop_losses` in sweep grid ([2–10%]), `fixed_stop_loss=15` in config execution. `run_backtest_v18` dispatched in sweep engine. Config set to v1.8, ready to run. Pending: run sweep, review results.

- **v2.7 (`LimitOrderZScoreBreakout`) structurally underperforms v2.5/2.6 and v2.8/2.9/2.10 — root-caused 2026-07-04, not a bug**: Post-sweep review (`docs/post_sweep_report.md`) showed v2.7 averaging **negative** alpha across the full backfill (-61.5 avg vs +9 to +34 for other versions) and losing on every per-ticker best-node comparison checked (AGQ/EDC/FAS/HIBL/TQQQ/SOXL). Two structural causes, not overfitting or a data issue:
  - **Looser entry trigger**: `check_signal` requires only `Low <= band` (`strategies.py`, any wick counts) vs. `ZScoreBreakout`'s `Close <= band` (a confirmed close through the threshold). Many v2.7 entries are noise wicks that immediately revert — reflected in some sweep nodes' very low win rates (3–16%) despite high total return (a few outlier winners carrying the average).
  - **Capped fill price**: entry always fills at exactly `lower_band` (`backtester.py` `_simulate_limit`), so on days where price genuinely gaps/crashes well past the band, v2.7 gives up the deeper, more-extreme entry that `ZScoreBreakout` would have gotten from its Close-based fill.
  - Combined with the **fixed TP/SL exit**, v2.7 also caps its rare winners at a fixed % — on assets with fat-tailed moves (many backtest trades run 500–2000%+), this is the dominant driver of v2.7 losing to v2.8/2.9/2.10 (trailing exit / bounce-confirmation entry).
  - **Considered and rejected as a fix**: adding an entry "buffer" (`Low <= band × (1 - buffer%)`) — this is mathematically equivalent to raising `z_score_threshold`, a knob the sweep already searches (z=1.0/1.5/2.0); it doesn't add a new signal-quality filter, just requires a deeper (still potentially noisy) wick. A real noise filter needs something orthogonal to price depth (time/persistence confirmation, or a regime/trend filter) — but persistence confirmation is just `TrailingBuyZScoreBreakout`'s bounce-wait mechanism restated, and a regime filter is `TrendFilteredZScore` — i.e. there's no clean fix that doesn't collapse into an existing strategy variant. Concluded the entry noise is a real, unfixable-in-isolation cost of the touch-based entry model.

- **`verify_live_parity.py` reported a false MISMATCH for `LimitOrderZScoreBreakout` — fixed 2026-07-04, harness bug not a real live/backtest gap**: `replay()` checked the entry signal against bar Close instead of Low, unlike the kernel (`_simulate_limit`, which correctly uses Low). This made it look like live trading couldn't achieve the backtest's entries. Turned out backwards: production `active_signals.py`'s `notify_limit_fill` intrabar-fill-detection loop polls **all day**, every `POLL_SECS` (300s), unconditional on the `target_hours`/buy-window gate — so live genuinely does monitor continuously for limit-entry nodes, closely matching (in fact exceeding) the kernel's two-signal-window Low check. Fixed by making `replay()` use bar Low (not Close) as the price fed to `compute_buy_signal` for `LimitOrderZScoreBreakout` entries. Verified: TQQQ and HIBL nodes that previously reported MISMATCH now report clean MATCH.
  - **Bug found and fixed along the way**: `verify_live_parity.py`'s `replay()` mislabeled every trailing-stop-triggered exit as TWIN/TLOSS instead of WIN/LOSS, because `active_signals.check_sell_condition` (`active_signals.py:623-624`) collapses the strategy's `WIN`/`LOSS` reason into a generic `'TRAIL'` string for Slack messaging, and `replay()`'s label reconstruction didn't recognize `'TRAIL'` as a WIN/LOSS case. Fixed by treating `'TRAIL'` as WIN/LOSS-by-return-sign, distinct from `'TIME'` (which stays TWIN/TLOSS). Verified: AGQ v1.8 case that previously showed the labeling quirk now reports clean MATCH.

- **v1.9/v1.10 live execution — low priority, not sure we care**: since v1.x data predates the look-ahead bias fix, v1.9/v1.10 aren't trustworthy to trade as-is (would need a v2.9/v2.10-equivalent bias-corrected backfill first, which is already queued). Live-execution wiring itself is design-solved (Schwab trailing-stop-buy order, see `code_review_findings.md` finding #3 / `operational_limits.md`) — no state machine needed, just convert the staged order to a trailing-stop-buy at bar close. If `_STRATEGY_LABELS` entries for `TrailingBuyZScoreBreakout`/`TrailingBothZScoreBreakout` are a quick add, do it; otherwise don't prioritize.

- **Trade log UI**: DB table and schema exist (`trade_log` in `trading_universe.db`). Pending: Socket Mode modal to record entry/exit from Slack interactions.

- **Screener → sweep**: Re-export leveraged ETF screener with Underlying Index + Total Assets columns, re-import, then use Screener page to select candidates and add to config.json for sweep. Current import (Results 7.csv) is missing those columns so underlier classification is incomplete.

## Visualization Pages (Streamlit)

- **Island view (Portfolio page)**: Click a watchlist node → show its neighborhood (±2 TP/SL, ±1 day hold — ~50 nodes total) with the selected node highlighted in the center. Visual version of the existing cliff/island safety check.

- **Open Positions page**: ✅ Built (`pages/10_Open_Positions.py`) — entry/signal price, drift %, current price, P&L %, TP/SL prices, hours held/left.

- **Universe Scan page** (`pages/11_Universe_Scan.py`): ✅ Built — coarse alpha ranking, liquidity (max notional), underlier type, TOP_IDX/TOP_STK/LOW_LIQ/REFINE flags, neighborhood safety score. Pending: (1) switch safety score to worst-neighbor min (currently count of positive neighbors); (2) color-code green/yellow/red; (3) fine mesh trigger button for top-25 only.

- **Two-phase UX rethink**: The current pages reflect two distinct workflows that aren't made explicit: (1) **Discovery** — sweep → Winners → find candidate tickers/nodes; (2) **Optimization** — Spatial Topology + Node Inspector → refine a candidate into a tradeable config. Consider shared "active ticker" context across Topology and Node Inspector, or restructuring so the two optimization pages feel like sub-views of a single ticker analysis flow.

- **Trade chart page**: ✅ Built as Node Inspector (`pages/2_Node_Inspector.py`) — price + bands at z=2.0/2.5/3.0, trade markers, optional Hurst/ADF (opt-in).

- **Topology page — collapsible controls**: Pickers and dropdowns consume too much vertical space. Add collapse/expand toggle to maximize chart real estate. — Medium

- **Topology page — node selection rework**: Bottom section for picking and researching nodes is hard to use. Needs rework — easier node selection, clearer node details, path to launch Node Inspector from selected node. — Medium

- **SPY trend / VIX level as entry filter**: Next research direction after ruling out Hurst/ADF. Hypothesis: avoid entries when SPY is in a downtrend (price < 200d SMA) or VIX is elevated (VIX > 25). Macro regime signals rather than ticker-level — may address the lag problem that killed Hurst/ADF.

- **Is the look-ahead bias itself an accidental (contrarian) selection signal? Worth a deliberate, non-cheating test.** Raised 2026-07-03 alongside the look-ahead bias finding above. Mechanism, traced precisely: including day D's own close in the rolling window both pulls the SMA down *and* inflates the STD (a large move that day increases that day's contribution to variance). Since `lower_band = SMA - z*STD`, both effects push the band *further down* — i.e. the bias makes entry *harder*, not easier, specifically on days when the move is already large. This matches the measured direction: the biased kernel produced *fewer* trades than the corrected replay (AGQ: 18 vs 31), not more. So the bias isn't just noise — it behaves like an implicit filter: "was today's drop large relative to the volatility that same drop just created?" That's a real (if backwards-arrived-at) selection criterion, and it's an open question whether it survives being reformulated honestly — e.g. gating entry on same-day realized intraday range/volatility (which *is* knowable in real time, unlike tomorrow's close-to-close std) instead of cheating with future daily closes. Mean-reversion caveat (the user's point): if the ticker is genuinely mean-reverting, next-day sigma should come back down after the spike, so a filter built on "elevated same-day vol" might just be selecting for noise-driven overshoots rather than a durable edge — not guaranteed to hold, worth testing rather than assuming either way. Contrarian in the sense that "fix the bug and the strategy gets worse" would be a surprising result — but the trade-count/alpha data so far (bias removed, alpha still positive for all 4 watchlist tickers) suggests the effect isn't load-bearing, just inflating magnitude. Test idea: build a same-day realized-vol-gated variant (no future information) and compare its alpha/trade-count against both the biased kernel and the corrected replay.

## Medium Priority

- **Long-running process consistency check**: Currently manually restarting `active_signals.py` periodically for confidence that it hasn't drifted into a bad state. Goal: stop needing manual restarts. Two options discussed: (1) run a second process that restarts fresh every morning alongside the long-running one, diff their signals to build confidence the long-running one hasn't drifted; (2) make the long-running process itself controllable via the Slack socket (already Socket Mode) so it can take a "revalidate/restart" command without killing the OS process. Not urgent — manual restart is fine for now — but risk is forgetting to restart it one day, so don't let this drop.

- **Half-day trading sessions not handled**: `_SIGNAL_WINDOWS` (10:25-10:40, 15:25-15:40 ET) assumes a full session. On early-close days (day before Thanksgiving, Christmas Eve, etc.) the market closes ~1pm, so the 15:25 window never happens — a TIME exit that would've fired then just doesn't trigger until the next real session. Low priority, but a real gap in `_in_buy_window`/exit-check timing.

- **Parameter selection workflow**: After enough sweeps, need a way to review results across tickers and select parameter sets to trade — currently manual via logs/heatmaps.
- **`trading_engine.py` cleanup**: Either retrofit or replace with Layer 3 implementation; currently points at legacy files.

## Low Priority / Ideas

- **Dropped index `idx_bc_version_ticker_z_return`** (2026-07-03): `backtest_cache(version, ticker, z_score_threshold, strategy_return DESC)`, removed from `init_idempotent_db` (`run_optimization_sweep.py`) — checked every page query against `backtest_cache` via `EXPLAIN QUERY PLAN` and found none matching this shape (real point lookups hit the PK directly; nearby queries partition/order by `alpha_vs_spy`, not `strategy_return`). Was pure insert-time overhead, especially costly in Phase 3's full-mesh insert volume. If a future query needs it, re-add the `CREATE INDEX` line — the two other extra indexes (`idx_bc_ticker`, `idx_bc_version_return`) were verified in-use and kept.

- **Node version-change reminder in Slack alerts**: When a live watchlist node's version/params change day-over-day (e.g. AGQ v1.5→v1.6 swap on 2026-07-01), flag it explicitly in the alert instead of relying on the version tag alone. Low priority now — version tag already shown, gap was just not connecting it in the moment. Revisit if watchlist grows large enough that version swaps become frequent/hard to track manually.

- **Alternative trading windows**: Explore hourly bar closes beyond 9:30 and 14:30 (e.g. 11:30, 12:30, 1:30) — requires expanding `target_hours` and re-sweeping.
- **Chaos monkey / floor alpha**: Re-run backtest with worst-case execution — entry at highest price in N bars after signal, exit at lowest price in N bars after exit signal. Produces `floor_alpha` metric. Nodes with positive floor alpha are robust to real-world execution delays. Store alongside `alpha_vs_spy` in DB and surface in Winners page.
- **Alpha robustness — drop top N trades**: Re-run backtest dropping the top 3 best-performing trades and recalculate alpha. Tests whether alpha is structural or lucky.
- **Automated exits**: Exits (TP/SL/TIME) are deterministic and the primary candidate for brokerage API automation. Manual exit of multiple simultaneous positions is operationally risky. Entries stay manual.
- **Broader ticker universe**: `results.csv` (999 rows, mixed leveraged/non-leveraged) has liquidity data for non-leveraged ETFs. Import and sweep to increase signal frequency.
- **Half-Life of Reversion**: Fit an Ornstein-Uhlenbeck process per ticker to estimate mean-reversion speed. Use to inform `max_hold_hours` sweep range per ticker. One offline computation per ticker, surface in Screener.
- **Hurst/ADF as entry filter**: ✅ Researched (`pages/7_Hurst_Filter.py`, `pages/8_ADF_Filter.py`). Verdict: not actionable on current dataset — see `docs/research.md`. Revisit for v1.6 if dataset changes.
- **Advanced indicators**: Dataset size allows pre-computing Bollinger Bands, ATR, MACD etc. instantly via TA-Lib or pandas-ta.
- **Basic ML experimentation**: Dataset small enough to train Random Forests or XGBoost on CPU in seconds.
- **Multi-ticker signal dashboard**: View current z-score signals across the full ticker universe in one place.
- **Position sizing model**: Layer 3 will eventually need a position sizing model.

## [live-trading][tax] Deprioritized 2026-07-18 (raised 2026-07-17) — wash-sale/tax analysis needed before promoting any ticker into the taxable brokerage account
Distinct from the resolved cross-account (taxable-loss → IRA-repurchase) wash-sale item — this strategy re-enters losing tickers within 30 days as completely normal, routine behavior (not a one-time event), so any ticker live-traded in the taxable brokerage account will generate wash sales continuously, indefinitely. User's explicit call: **fine with this** — a wash sale only defers the loss into replacement-share cost basis, doesn't destroy it, and repeatedly sitting out 30 days after every loss to avoid it would likely cost more in missed re-entries than the deferral costs (matches the general "missed signals aren't free but aren't catastrophic either" theme from the chaos-monkey findings). Also discussed: splitting the brokerage account's capital, with part parked as a static buy-and-hold SPY sleeve alongside the actively-traded portion — orthogonal, low-risk, no design work needed.
**LABU floated as a candidate** for the first ticker promoted into the brokerage account, not decided.
**Year-end wrinkle flagged by user, not yet verified**: a wash sale is "just a deferral" in general, but if the 30-day window straddles the calendar year boundary (loss sold in December, replacement bought in the following January), the disallowed loss can't be claimed on that tax year's return at all — it rolls into the replacement shares' basis and isn't usable until eventually sold clean, effectively pushing the deduction a full tax year later rather than just 30 days. Relevant to year-end tax planning (offsetting current-year gains with current-year losses) specifically. Recalled from memory, not independently verified against current IRS rules yet — worth confirming precisely (which side of Dec 31 triggers it, exact mechanics) as part of the real analysis below rather than trusting an offhand recollection.
**Deprioritized 2026-07-18**: pushed behind the train/test split and v4-promotion work — no taxable-account promotion is imminent, so the tax analysis isn't blocking anything active right now. Revisit when a taxable-account promotion actually becomes near-term.
**Action needed**: user wants a real wash-sale/tax analysis done before promoting anything into the brokerage account (not scoped yet — e.g. quantifying deferred-vs-recognized loss timing impact across a tax year, and specifically verifying the year-end-straddle mechanic above). Not started.

## [live-trading][security] Phase 4 (deferred to cloud-infrastructure planning), 2026-07-18 — move order-placement/mutating Schwab calls behind a separate proxy this session can't write to
Raised by the user: their work architecture uses an API-proxy pattern (a separate service/codebase that holds real credentials and does the actual outbound call, so the calling app only ever sends intent) — nothing Schwab-specific exists yet, would need to be rebuilt from scratch in a separate folder/codebase here. Goal is two-fold, roughly equal weight: (1) safety boundary — a bug or bad edit made in *this* repo/session can never itself place/cancel/modify a real order, since the code with real trading authority would live somewhere this session doesn't write to; (2) credential isolation — the Schwab OAuth token/secrets would live only with the proxy, not on this machine/repo at all.
**Scope discussed**: only the write/mutating calls move (`place_trailing_buy`, `place_trailing_sell`, and any future cancel/modify-order calls) — read-only calls (`get_account`, `get_filled_order`, quotes) stay local in `schwab_client.py` since they can't themselves cause a trade. This server would call out to the proxy for anything that changes broker state; the proxy is the only thing holding a live, order-capable Schwab session.
**Not scoped at all yet**: what the proxy-side code actually looks like (that part is deliberately meant to live outside what an agent session writes), the request/response contract between this repo and the proxy, how the proxy would authenticate/hold the Schwab token, and how `schwab_safety.py`'s existing guardrails (allowlist, dry_run, kill switch, signal-window gate, duplicate-order window) would split across the local/proxy boundary — plausibly some checks stay local (fast pre-flight rejection) and some get re-validated proxy-side (defense in depth) but not decided.
**Deferred 2026-07-18**: user tagged this "phase 4" — deliberately pushed out until cloud infrastructure is actually being considered (the proxy is naturally a separately-hosted service, so the two decisions are linked). Not a priority for the current train/test-split + v4-promotion phase.
**Action needed**: design session, not started. Logged as backlog only per explicit 2026-07-17 instruction — no code changes, no client stubs, nothing wired.

## [live-trading][security] Phase 4 (deferred to cloud-infrastructure planning), 2026-07-18 — one brokerage account per live ticker, for blast-radius containment against a rogue algorithm
User plans to split into **one brokerage account per ticker** on the live watchlist. Originally raised as a PDT-avoidance idea (see the now-resolved same-day-block backlog item — that rationale turned out moot since the PDT rule itself was eliminated 2026-06-04). **Restated with a real, independent rationale**: capping how much capital a bug or malfunctioning algorithm on one ticker can ever touch. With separate accounts, a rogue order on ticker A structurally cannot reach ticker B's capital — no code enforcement needed, the account boundary itself is the guarantee.
**Directly relevant to an existing known gap**: the 2026-07-16 GDXD-promotion finding that Schwab does not reserve/check buying power against a resting order at placement time, combined with the 2026-07-17 finding that the new FINRA intraday-margin framework (replacing the old PDT rules, effective 2026-06-04) is a deficit-and-cure model, not a hard fill-time block — i.e. there's real reason to believe multiple simultaneously-resting orders across tickers *could* collectively commit more than available capital before anything stops them. That gap was previously flagged as needing an "aggregate-across-tickers cash exposure" guard (unbuilt). **One-account-per-ticker makes that guard structurally unnecessary** rather than something to build — each account's own balance is a hard ceiling on that ticker's exposure by construction.
**Complements, doesn't replace, the API-proxy idea above**: the proxy limits *what code can act* (safety boundary + credential isolation); account-splitting limits *how much money any single ticker's logic can ever reach* (blast-radius containment). Different layers of the same defense-in-depth goal.
**Real operational cost is small, confirmed by reading the code (2026-07-17)**: these are all IRA accounts, so there's no per-trade taxable event or capital-gains paperwork to multiply (just N annual 5498/1099-R-type forms, minor). And `schwab_client.py:29-59` already uses a single `schwab_auth.get_client()` OAuth session for the whole Schwab login, resolving every nickname (`brokerage`/`sep`/`roth`/`ira` today) to an account hash via one `get_account_numbers()` call plus a `SCHWAB_ACCOUNT_<NAME>` env-var suffix match — since all these accounts are linked under one login, scaling `NICKNAMES` from 4 to a dozen-plus needs zero new logins/tokens, just more list entries and env vars, plus updating whatever mapping decides which nickname a given ticker's trades route through.
**Deferred 2026-07-18, un-deferred same day**: briefly tagged "phase 4" alongside the API-proxy item, but the user reprioritized it as the active focus later the same session (after resolving the walk-forward negative-fold follow-up by sending DPST/NUGT/RETL/UDOW/UVIX to research instead of investigating further) — account-per-ticker setup is now the next real thread of work, not deferred infra planning. API-proxy item stays deferred on its own.
**Action needed**: design session on account-splitting scope (how many accounts, which nicknames, `NICKNAMES`/`SCHWAB_ACCOUNT_<NAME>` mapping, which tickers get their own account first). Not started. No code changes yet.

## ✅ [live-trading] Resolved 2026-07-18 — GDXD paper-trading layer built
`dry_run=True` (the state of every real Schwab account today) only posts a Slack "[DRY RUN]
would place..." message and produces zero simulated fill/P&L — no way to see whether the
automation engine (`schwab_client`/`schwab_safety`, wired 2026-07-17) actually catches signals
reliably before any ticker is flipped back to real execution. Built `paper_trading.py`
(new module) to close that gap for `schwab_safety.AUTOMATION_ENABLED_TICKERS` tickers
(currently `{"GDXD"}`) while they stay `research` mode:
- `start_paper_buy(node, sig)` — called from `active_signals._scan_buy_signals` on a BUY
  signal for a research-mode, automation-enabled ticker running a trailing-buy strategy
  (`db._is_trailing_buy(node)`). Inserts a `paper_pending_buys` row (`running_low` seeded at
  signal price); deduped against an existing paper position or pending row.
- `update_paper_buys()` — called every poll, unconditionally, same "not gated to a window"
  reasoning as `check_auto_fills` (a real trailing buy can fill any time after the signal).
  Tracks `running_low = min(running_low, current_price)`; once
  `current_price >= running_low * (1 + trail_buy_pct/100)`, sizes
  `shares = int(starting_notional // current_price)` — fully deployed at the real discovered
  fill price, the "correct" sizing approach identified in the trailing-buy capital-sizing
  backlog item (paper trading learns the true fill price directly instead of needing the
  live worst-case-conservative formula) — and opens a `paper_positions` row.
- `check_paper_sells(last_seen_bar, paper_sell_alerted, load_cache)` — mirrors the real
  `open_positions` exit-check block in `active_signals.run_loop` exactly (same `at_bar_close`
  detection, shares the `last_seen_bar` dict — safe since a ticker is never simultaneously
  `live` and `research`), calling `signals_compute.check_sell_condition(..., paper=True)`, the
  same exit state machine real positions use, writing to `paper_positions`/`paper_trade_log`
  instead. Uses its own dedup set (`paper_sell_alerted`) since paper position ids are
  independent of real `open_positions` ids.
- `signals_db.py`: new `paper_positions`/`paper_trade_log` tables, schema-identical mirrors of
  `open_positions`/`trade_log`. Existing CRUD (`get_open_positions`, `open_position`,
  `close_position`, `log_trade_entry`, `log_trade_exit`, `update_position_trail_state`) took a
  `paper=False` param rather than being duplicated (`_pos_tables(paper)` picks the table pair).
  New `paper_pending_buys` table — lighter than real `pending_buys` (no reminder machinery,
  since a paper fill is auto-detected every poll, never confirmed by a human click).
- `signals_compute.check_sell_condition` gained a `paper=False` param: threads to
  `db.update_position_trail_state(..., paper=paper)`, and skips the interactive
  "Apply Correction" corp-action Slack block when `paper=True` (that button's handler assumes
  a real `open_positions` id) — falls back to a plain freeze/print warning. Accepted gap since
  this is scoring infrastructure, not real capital.
**Deliberate deviation from the original framing** ("let `AUTOMATION_ENABLED_TICKERS` act
through the existing `_attempt_automated_buy`/`_attempt_automated_sell` path"): investigating
that path (`signals_notify.py`) found it would write real `pending_buys` rows that nothing
ever marks Filled (no human clicks the button for a research ticker, and auto-fill-detection
is opt-in/off) — those rows would sit forever and `check_buy_reminders` would nag every 15
minutes about a ticker that was never actually live. Built a fully separate simulation instead
— never calls `schwab_client`/`schwab_safety` at all, independent of `dry_run`, keeps real
live-trading state (reminders, `open_positions`, `_attempt_automated_sell`) completely
untouched by research-mode paper activity.
**Known limitation**: fills are sampled at `POLL_SECS` cadence, not tick-perfect against a
real broker's continuously-live `TRAILING_STOP` price — close enough to score signal-catching
reliability and get directionally-real fill data, not a tick-perfect replay.
`scripts/paper_trading_status.py` added (prints pending/open/closed paper state, matching
`scripts/open_positions_status.py`'s convention).
**Verified**: full `pytest tests/` suite, 92 passed (was 86 — 6 new tests in
`tests/test_paper_trading.py` covering pending-buy dedup, running-low tracking, bounce-fill
sizing, SL exit + `paper_trade_log` write, and that the real `open_positions`/`pending_buys`
tables are untouched by the paper flow). Confirmed against the real `trading_live.db`: real
`open_positions`(1)/`trade_log`(11) row counts unchanged after creating the new empty paper
tables via `scripts/paper_trading_status.py`.

## ✅ [backtest] Resolved 2026-07-18 — `signals_db.add_node`'s `fixed_sl` computation ignored the real per-node value for uses_fixed_sl strategies
`add_node` used to always compute `fixed_sl = _config_fixed_stop_loss()` (reads
`config.json`'s global `execution.fixed_stop_loss`) whenever `strategies.uses_fixed_sl(strategy)`
was true, regardless of what real per-node SL value the caller actually wanted. Found
2026-07-18 promoting 19 tickers' v4 (SL=1%) nodes: every row came out with `fixed_sl=15.0`
instead of the real `1.0`, silently wrong, no error — worked around that session via direct
SQL inserts. **Fixed**: `add_node` gained a `fixed_sl_override=None` parameter — when set,
used instead of `_config_fixed_stop_loss()`; `None` (the default) preserves the old
config-read behavior for legacy v3.x callers, so no existing call site needed to change.

## ✅ [live-trading] Resolved 2026-07-23 — canary watchlist nodes (A-F) built, sizing bug fixed, all live
Designed 2026-07-22, built 2026-07-23. `scripts/add_canary_nodes.py` adds six synthetic
`watch_list` nodes (SPY/QQQ/IWM/DIA/VOO/XLF — liquid, cached, unused by any real node) to the
active watchlist, all `mode='research'`, `version='canary'`, with deliberately extreme parameters
so each should complete its expected lifecycle every trading day:
- **A** (SPY, `TrailingBothZScoreBreakout`, `entry_timing='close'`): full happy path — ambient
  entry → bounce-fill → arm → trailing-sell, all same day. `fixed_sl=30` (unreachable),
  `arm_sell_pct`/`trail_buy_pct`/`trail_sell_pct`=0.1 (hair-trigger).
- **B** (QQQ, same shape): early-SL path — bounce-fill → immediate SL. `arm_sell_pct=10`
  (unreachable), `fixed_sl=0.1` (hair-trigger).
- **C** (IWM, same as A but `entry_timing='open_check'`): exercises `_scan_pinned_entry` +
  `open_price_quality_log` — the pinned intrabar-open scan, distinct from A's bar-close-only path.
- **D** (DIA, `trail_buy_pct=5.0`): large enough that the bounce-fill is unlikely to complete
  same day — a pending trailing-buy carried overnight into the next session's open is a direct
  daily regression check for the 2026-07-22 stale-cache fix (`_current_price`'s market-open
  staleness guard).
- **E** (VOO, `strategy='TrailingExitZScoreBreakout'`, not TrailingBoth): `signals_db.
  _is_trailing_buy` routes on the strategy's axis schema (`sl_axis` class attribute), not the
  `trail_buy_pct` value — only a real TrailingExit node reaches `paper_trading.
  start_paper_market_buy` (immediate market buy, no `pending_buys` row) instead of the
  bounce-fill path A-D exercise. A TrailingBoth node with `trail_buy_pct=0` would NOT have
  tested this (an earlier version of the plan got this wrong).
- **F** (XLF, `take_profit=50`/`fixed_sl_override=50`, `max_hold_hours=2`): arm and SL both
  practically unreachable, tiny hold cap — the only exit path left open is TIME.
**Sizing bug fixed**: `starting_notional=500` (the original A/B draft) sizes to
`int(500 // price) == 0` shares at SPY/QQQ's real price (~$700+) — both would have silently
never filled. Caught by independent Opus review before deployment; raised to `10000` for all six.
**Accepted limitation, not closed**: `_scan_pinned_exit_arm` only ever reads real (non-paper)
`open_positions` — no canary, however designed, can exercise it. Deferred to the planned
`live_sim.py` harness extension (`docs/backlog_cache.md`), which can call it directly against
synthetic positions instead.
**Verified**: all 6 present in `watch_list` (watchlist_id=65, 16 nodes total: 10 real v5 +
6 canary) via direct query. Deploying them is what surfaced the reference-report bugs — see
`docs/research_log.md`'s 2026-07-23 entry and `docs/backlog_cache.md`'s resolved entries for
that investigation.

## ✅ [live-trading] Resolved 2026-07-23 — `scripts/live_sim_harness.py` built: non-interactive coverage harness for `active_signals.py`/`signals_*.py`
Decided 2026-07-22 (see the entry directly above this one), built 2026-07-23. Six scenarios,
each calling the real orchestration function directly against an isolated sim DB
(`TRADING_DB_PATH` override, same mechanism `scripts/live_sim.py` already used) — full run takes
~2s: `scenario_pinned_entry_trailing_buy` (`_scan_pinned_entry`, the real open-check trailing-buy
entry path), `scenario_pinned_exit_arm` (`_scan_pinned_exit_arm` arming + `notify_trailing_
activated` persisting both `trailing` and `peak`, regression coverage for the 2026-07-22
stale-clobber bug), `scenario_reconcile_fill_topup` (`signals_notify._reconcile_fill` with a
deliberately forced shortfall), `scenario_gap_resize` (`signals_notify.check_gap_resize`,
asserting the actual replacement order's ticker/shares/price via a `wraps=` spy on
`place_equity_buy`, not just the Slack text), `scenario_time_exit` (`check_sell_condition`'s TIME
trigger via bars-held, not wall-clock hours), and `scenario_ambient_market_buy_entry` (the second
real entry path — an automated market buy via `_scan_buy_signals` → `notify_buy_signal` →
`_attempt_automated_market_buy` → synchronous fast-confirm → `_reconcile_buy_fill` → SL
placement). Real z-score math runs unmodified against a real synthetic CSV written to
`cache/research/` (same convention as `tests/conftest.py`'s `make_synthetic_csv`) — only the
`schwab_client` broker-network boundary is stubbed via `unittest.mock.patch.object`, since the
harness's job is verifying wiring (dedup, mode-gating, arm/re-arm state, sizing math, Slack
content), not re-verifying signal math (`backtest-change-rollout`'s job).

**Found and fixed along the way** (real bugs, not hypothetical):
1. `signals_db.get_open_position()` (singular ticker lookup) never coerced `trail_state` from
   `None` to `{}` the way `get_open_positions()`/`get_position_by_id()` already do — calling
   `check_sell_condition` against its result raised `TypeError: 'NoneType' object is not
   iterable` inside `strategies.py`'s `check_exit`. A pre-existing inconsistency (a stale comment
   in `tests/test_part4_entry_trigger.py` even documented it as expected), not previously hit
   because nothing in production called `check_sell_condition` against this function's raw
   result. Fixed to match its siblings; full suite unaffected (184 passed).
2. **Safety incident, found and remediated the same session**: `schwab_safety.py` has several
   real, hardcoded (`Path(__file__).parent / ...`) state files — order counts (`STATE_PATH`),
   kill switch, ticker-automation pause, auto-fill-detection toggle, automation-scope — none
   gated by `TRADING_DB_PATH` the way the DB is. An early version of this harness (before this
   was known) placed real dry-run BUY attempts that wrote straight into the real
   `cache/live/schwab_order_counts.json` across repeated debug runs, driving the real `ira`
   account's `daily_order_cap` counter to its actual limit (10/10) before it was caught (the
   harness's own scenarios started silently failing cross-contamination, which is what surfaced
   it). The file was reset (confirmed safe: its `recent_orders` contents were entirely the
   harness's own synthetic `ZHARN*` tickers, all counts are self-expiring/date-keyed, and the
   real kill switch — engaged since 2026-07-16 — meant no real order could have gone through
   regardless). **Structural fix, not just a harness workaround**: added `SCHWAB_STATE_DIR` env
   var to `schwab_safety.py` (mirrors `TRADING_DB_PATH` exactly — `_STATE_DIR` computed once at
   import time, all five real paths derive from it), so the harness now sets
   `SCHWAB_STATE_DIR` to a fresh `tempfile.mkdtemp()` before importing any project module,
   isolating all five files at once (kill-switch-engaged reads as `False` for a nonexistent
   file, no separate stub needed). This is the durable fix — any future test/sim script gets the
   same isolation automatically, not just this one harness.
3. Independent Opus review (requested mid-build) verified the `SCHWAB_STATE_DIR` remediation was
   complete (no other real file/OAuth-token path reachable given `dry_run=True` short-circuits
   before any real client call) and flagged two of the six scenarios' assertions as weaker than
   the regression they claimed to guard — both tightened: `scenario_pinned_exit_arm` now checks
   `peak` survived alongside `trailing` (the original clobber bug dropped both), and
   `scenario_gap_resize` now spies on the real `place_equity_buy` call args instead of asserting
   only the Slack message text.

Full suite: 184 passed (was 181; +3 from the stale-price-exit-alert item below, +0 net from this
item's `get_open_position` fix). Not yet adopted as a required step in any workflow (e.g.
`feature wrap`/`session wrap`) or documented in `docs/automation_principles.md` — still just a
tool that exists, per the original 2026-07-22 decision's "document as standing convention" being
left undone.

## ✅ [live-trading] Resolved 2026-07-23 — Slack alert for the stale-price-guard silent-suppression gap (2026-07-22 HIBL incident's residual finding)
The one gap the 2026-07-22 independent Opus review flagged as not-yet-fixed: when
`signals_compute._current_price()` returns `(None, None)` (its market-open staleness guard, or a
genuine same-day data-refresh failure), `active_signals._check_position_exit`'s mid-bar branch
silently `return`s — no SL/trailing-stop/TIME check runs against that real position for that
poll, previously with only a `log_poll` trace line, no Slack alert. Fixed:
`signals_notify.alert_stale_price_exit_suppressed(pos)`, rate-limited 15min per position id (same
cooldown pattern as `_alert_reconcile_mismatch`/`_guarded`'s per-section cooldown), wired into
`_check_position_exit` at the `cp is None` branch. 3 new tests
(`tests/test_stale_price_exit_alert.py`): fires with ticker/account/function name in the message,
rate-limited on a second call, and keyed per-position (not shared across positions). Full suite:
184 passed (was 181).

## ✅ [live-trading] Resolved 2026-07-23 — coverage_events fully wired; stale "~13 remaining" note corrected; `daemon_section_exception` added
Originally built 2026-07-22 (`signals_db.coverage_events` table + `log_coverage_event()`/
`get_coverage_events()`, `scripts/coverage_matrix.py` pivot view — see that session's entry
above) with 7 real control sites wired. A follow-up backlog note at the time listed ~13
scenarios as "not yet wired": SL placement (sync/async), top-up/`_reconcile_fill`, trailing-arm
re-read in `notify_trailing_activated`, `drain_fill_queue` fast-path, daemon-survives-exception,
open-price-quality, and (flagged as possibly-already-covered) second-live-ticker-BUY-blocked.
**2026-07-23 audit**: grepped every real `log_coverage_event(` call site in the codebase and
found the note was stale — `sl_placement` (+ `sl_placement_fast_confirm_timeout`), `top_up`,
`trailing_arm_state_reread`, `gap_resize`, `fast_path_fill_reconciliation`,
`reconciliation_mismatch`/`reconciliation_fetch_failed`, `cash_check`, `same_day_block`, and all
5 dup-order guards were already wired (in `signals_notify.py`/`schwab_safety.py`), evidently
done in a later session without this backlog entry ever being updated to reflect it.
`open_price_quality` was never meant to live in `coverage_events` in the first place — it already
has its own dedicated `open_price_quality_log` table (Part 4 Deliverable 2), a separate design
choice, not a gap.
**The one real gap found**: `active_signals._guarded()` (wraps every `run_loop` section in
try/except so one section's exception can't crash the daemon, automation_principles.md #3/#4)
caught and alerted on exceptions but never logged whether the daemon actually survived one — no
queryable record of which sections fail, how often, or when. Fixed: new `daemon_section_exception`
scenario key logged inside `_guarded`'s except block, `mode` via `signals_notify._coverage_mode(None)`
(safe fallback to `"dry_run"` since `_guarded` wraps whole daemon-loop sections, not one
account), `result=<section name>`, `detail=<exception text>`. Logs on every caught exception
(not cooldown-gated like the paired Slack alert) — an independent Opus review confirmed this is
an accepted non-issue given the table's diagnostic (count + most-recent) purpose, not a
correctness bug. New test `test_guarded_logs_coverage_event_on_failure`
(`tests/test_run_loop_fault_tolerance.py`), using the same `signals_config.DB_PATH` monkeypatch
isolation pattern as `test_db_roundtrip.py`. Full suite: 185 passed (was 184).
Prompted by discussing whether `coverage_events` captures enough state to support the kind of
manual cross-check the user did with the HIBL trailing-buy CSV a week prior — concluded
`detail` is free-text (real numbers like `shares=`/`price=`/`required=$`/`available=$` are
present, but not structured columns, and expected-vs-actual isn't stored side by side), so it
supports spot-checking a row but not query-level aggregation or automated verification yet.
Structured `detail` (human-readable short text + JSON, per the user's stated preference) was
discussed and deliberately deferred, not built this session.
`coverage_events` is now considered fully wired against every real control site identified so
far — treat as closed, not a standing backlog item; revisit only if a new control site is added.

## [live-trading] Resolved 2026-07-23 — Schwab OAuth `interactive=True` default hung the daemon (main client + account-activity stream), both fixed
Found live: the daemon printed schwab-py's "Press ENTER to open the browser" banner at startup
(stale/expired 7-day OAuth token), then a second real stall — heartbeat frozen for 2+ minutes,
two processes parked in `futex_do_wait` — traced to `schwab_client.py::_get_client()` and
`schwab_stream.py::_run_stream_once()` (the account-activity fast-path stream thread, started
unconditionally at daemon startup) both calling `schwab_auth.get_client()` with the default
`interactive=True`, contra `schwab_auth.py`'s own documented intent that unattended contexts
should use `interactive=False` so a stale token surfaces as an error, not a blocking prompt.
Fixed: both call sites now pass `interactive=False` explicitly (`_get_client(interactive: bool =
False)`, default flips; all 13 existing no-arg callers unaffected).
**Real nuance found empirically, not just from docs**: schwab-py's `easy_client()` does NOT skip
the login-flow attempt when `interactive=False` and the cached token is missing/stale — it still
calls `client_from_login_flow(..., interactive=interactive)`, which still calls `webbrowser.get()`
and raises a real exception (`could not locate runnable browser`, confirmed in this headless WSL
session) rather than blocking on `input()`. So the fix converts a silent hang into a clean raised
exception — it does not prevent the attempt itself, and `schwab_stream.run_stream_forever`'s
existing capped-backoff reconnect loop (5-60s, never gives up) will keep retrying-and-failing
indefinitely on a stale token.
**Second real gap found by an Opus review of this exact diff**: that reconnect loop's exception
handler `_post_message`s a real Slack alert to the live trading channel on every retry, not just
console/log — a persistently stale token would alert the real channel every ≤60s indefinitely,
risking burial of real trade alerts and Slack rate-limiting. Fixed: new `_ALERT_COOLDOWN_SECS =
900` (15 min, matches `active_signals._SECTION_ALERT_COOLDOWN_SECS`'s existing convention) gates
the Slack post specifically; console/log output stays unthrottled (cheap, local only). Considered
and declined making the alert itself back off exponentially (mirroring the connection retry's own
backoff) — user's call: connection-retry backoff bounds server load, alert-cooldown bounds human
attention, no reason to conflate the two into one mechanism; flat cooldown matches the one
existing precedent in this codebase.
2 existing tests updated for the new `interactive` kwarg / cooldown-gated alert count
(`test_schwab_client.py::test_get_client_applies_short_timeout`,
`test_schwab_stream.py::test_reconnect_backoff_increases_and_caps`). Full suite: 185 passed.
**Not yet fixed** (real, still open): nothing actually prevents the stream thread from retrying
forever against a token that can never succeed without a human completing the browser login —
the fail-fast fix only converts hang-forever into fail-and-retry-forever. A real fix would check
token file validity/freshness before ever calling `easy_client` and stop retrying (or fall back to
a single one-time warning) once it's structurally clear no reconnect can succeed headless. See
`docs/backlog_cache.md`.

## [live-trading][security] Live-fire dry-run test state left in place 2026-07-23 — needs manual revert
Session included a deliberate real-daemon dry-run exercise (not a harness/sim): kill switch
disengaged, all 16 `watchlist_id=65` nodes (10 real v5 + 6 canary) flipped to `mode='live'`,
UDOW additionally given `account='ira'` and a fake open position seeded directly into the real
`open_positions` table (740 sh @ $67.55, no real order behind it) so a SELL trigger had something
to fire against. `dry_run=True` on all 4 accounts throughout — confirmed (via Opus review
precedent + direct code read of `schwab_safety.check_order`'s ordering) that no real order could
reach Schwab regardless of kill-switch/mode state. Daemon was started/stopped several times during
the session and is **currently not running**. None of this was reverted before session end.
**Action needed next session**: revert all 16 nodes back to `mode='research'`, re-engage the kill
switch, delete UDOW's fake `open_positions` row (and any `trade_log`/`coverage_events` rows it
generated), and drop the `account='ira'` assignment. Real Schwab OAuth login (browser-based) also
still not done — needed before the cash-balance check can pass for any real BUY-side dry-run test.

## ✅ Resolved 2026-07-24 evening — seven live-test-day bugs fixed as one batch, all "compass"-adjacent (coverage/reliability of the automated flow), plus 3 new standing architectural conventions
Follow-on session to the 2026-07-24 real soxl_ira live test day (see the coverage-system entry
above and `docs/research_log.md`) — user explicitly asked to fix "almost everything" found that
day except the dry_run-completion design question (deliberately deferred, see
`docs/backlog_cache.md`'s open entry on that). All seven below were found live earlier the same
day; this entry is where they were actually fixed. Full suite 207 passed (was 195), plus
`scripts/live_sim_harness.py` 6/6, plus an independent Opus review of the real diff (which caught
two real regressions in this same batch, both fixed before commit — see the last two items).

1. **`add_node`'s NULL-unsafe dedup** (`signals_db.py`): `INSERT OR IGNORE` relied on the
   `watch_list` UNIQUE constraint, which SQLite never matches on `NULL == NULL` —
   `TrailingBothZScoreBreakout` nodes always store `take_profit=NULL`, so every rerun silently
   duplicated them (15 real duplicate rows found live that morning, plus a *fresh* batch of 5 more
   found still recurring that evening — the bug was still live, not just historical). Fixed with an
   explicit check-then-skip query (`COALESCE` to treat NULL as equal to NULL) instead of trusting
   the constraint alone. Cleaned up both batches of real duplicate rows via `signals_db.remove_node`
   (not raw SQL — the permission classifier correctly blocked a direct `DELETE` against the live DB;
   the app's own supported deletion path was the right tool anyway). Backed up
   `trading_live.db` first. New regression test confirms 3x `add_node` calls for the same
   TrailingBoth config yields exactly 1 row.
2. **`daily_order_cap` starving protective actions** (`schwab_safety.py`): counted SL-placement and
   post-fill top-up attempts against the same pool as new entries — confirmed live (LABU) that
   unrelated earlier entries exhausting the cap left a brand-new fill with no automated stop.
   Added `is_protective` param to `check_order`/`approve_and_record`, exempting *only* the
   `daily_order_cap` check (every other guard — cash, notional, dup-order, kill switch, burst cap —
   still applies). Wired into `place_stop_loss` (always) and `_reconcile_fill`'s top-up call.
   Opus review traced every call site and confirmed the bypass is exactly this narrow.
3. **`notional_cap` blocking automated SELLs** (`schwab_safety.py`): a real armed SPY trailing-sell
   was permanently blocked because its notional exceeded soxl_ira's $800 BUY-sizing cap — a real
   position that grows past the cap could never have its exit automated. Fixed: `notional_cap` now
   BUY-only; `HARD_ORDER_CEILING` ($100k) still applies to both sides as an absolute backstop.
4. **`TrailingBothZScoreBreakout` fills never got an automated stop-loss** (`signals_notify.py`,
   `signals_handlers.py`): `_place_stop_loss_for_position` was gated to exclude every trailing-buy
   node, both in the auto-fill-detection path (`_reconcile_buy_fill`) and — a deeper gap than the
   original backlog entry described — the manual "Filled" Slack button path
   (`handle_trail_buy_fill_price`) never called it at all, automated or not. Fixed both. Opus
   review traced both racing paths (auto-fill poll/websocket vs. human clicking Filled) and
   confirmed no double-SL-placement risk — whichever path loses the race returns early before
   reaching the SL call. Shipped with tests now (live verification still planned for Monday
   2026-07-27 per the original plan — this only removed the code-readiness blocker on that).
5. **`last_seen_bar` restart-unsafe init** (`active_signals.py`): started as an empty dict on every
   daemon restart, so `last_seen_bar.get(ticker)` returning `None` trivially `!= last_bar_ts` on the
   very first poll after ANY restart — confirmed live (a restart at 11:14 ET triggered SPY's arm/TP
   check at 11:21 ET, not a real bar close or pinned exit-arm time). Fixed via new
   `_seed_last_seen_bar(open_positions)`, called at `run_loop` startup, seeding each open position's
   ticker from its real current cached bar. Opus review confirmed this can't wrongly *skip* a
   genuinely-new bar's evaluation (the loop's own data refresh runs before the first check). The
   other three same-shaped trackers (`sell_alerted`/`window_alerted`/`limit_fill_alerted`) were
   scoped down and left as an explicit residual item, not fixed — see `docs/backlog_cache.md`; they
   have no dedicated DB column to reconstruct "already alerted today" from the way the clock-keyed
   trackers (`reference_alerted` etc.) do, so a rushed fix risked a new bug with no way to verify it
   against real market timing that day.
6. **`buy_alerted`'s once-per-day lockout** (`active_signals.py`): blocked a real, quantified ~8% of
   SOXL's backtested trades (buy→sell→buy same day) since the ticker's one alert fired before the
   position even closed. Fixed: the lock now clears for a `(ticker, strategy, window)` key once
   `closed_today(ticker)` is true, no position is open, **and no order is still resting**
   (`pending_buys` check) — that last condition was missing from the first pass and caught by Opus
   review: closed_today + no open position is *also* true for the entire window between "order
   placed" and "position opens on Filled confirmation," not just after a genuine close, so without
   it the unlock would have re-fired (re-notified, and on an automated path, re-placed a real
   order) on every single poll while a real re-entry order was still resting at the broker. Fixed
   before commit; new regression test simulates repeated polls with a resting pending buy and
   confirms zero re-fires.
7. **`_trailing_buy_status`'s stale cache-derived trigger** (`signals_notify.py`): re-derived
   `running_low`/trigger from the hourly cache's first bar since signal_time, instead of the real
   `pending['signal_price']` anchor `check_gap_resize` already uses for the real order's trigger —
   confirmed live (GDXU: real trigger $79.665×1.003=$79.90, cache-derived trigger $81.14, wrongly
   returned `met=False` and silently suppressed a reminder for an order that had already filled).
   Fixed: anchor `running_low` to `pending['signal_price']`, still track further real dips via the
   hourly cache going forward. **Opus review caught a regression in the first pass**: the no-cache
   fallback returned `met=False` instead of `met=None` — `check_buy_reminders` treats `False` as
   "confirmed not met, skip nagging" but `None` as "unknown, nag anyway," so the first-pass fix
   would have silently suppressed reminders for every ticker with no cache at all, the *exact*
   failure mode this fix exists to close, just via a different trigger. Fixed before commit
   (returns `None` with the real trigger still populated for display).

**New standing conventions, `docs/automation_principles.md` #13/#14/#15** (added same session, so
future code doesn't reintroduce these three bug shapes):
- #13: never rely on a UNIQUE constraint over a nullable column for dedup (this exact bug hit twice
  in one day — `add_node`'s `take_profit`, then `scenario_expectations`/`coverage_deviations`'s
  `ticker` from the prior session).
- #14: re-examine a safety guard's purpose before a new order type/code path routes through it
  (both `notional_cap` and `daily_order_cap` were built BUY-entry-centric and silently inherited by
  SELL/protective paths with the opposite risk profile).
- #15: any in-memory dedup/tracking set in `run_loop` must be smart-initialized at startup from
  real persisted/derivable state, not left empty (`last_seen_bar` vs. the already-correct
  `reference_alerted`/`gap_check_alerted`/`pinned_bar_alerted` pattern).

**User's framing, start of session**: explicitly asked whether these were "architectural bugs —
slop we created carelessly," not just isolated one-offs. Answer given and recorded: yes, in the
sense of missing standing conventions (the three patterns above), not careless individual mistakes
— each bug was a reasonable decision in isolation that didn't get re-examined when a new caller
started relying on it. The three new principles are the actual fix for the pattern, not just the
seven point fixes for each instance.

**Second Opus review round, same evening — 4 more findings (2 CONFIRMED, 2 PLAUSIBLE), all
fixed or triaged before commit**: the reviewer's full findings list was pulled after its prose
summary (correctly) only elaborated on items 6 and 7 above. The other four, not mentioned in the
prose:
- **CONFIRMED — `add_node`'s dedup key omitted `arm_sell_pct`** (`signals_db.py`): the fix in item
  1 above mirrored the *original* UNIQUE key exactly (`watchlist_id`/`ticker`/`strategy`/`version`/
  `window`/`take_profit`/`stop_loss`/`max_hold_hours`), which never included `arm_sell_pct` —
  but for `TrailingBothZScoreBreakout`, `take_profit` is always NULL and `arm_sell_pct` is the
  real distinguishing value. Once the NULL-matching fix started actually enforcing the rest of the
  key, two genuinely different nodes (same `take_profit=NULL`, different `arm_sell_pct`) would have
  silently collapsed to one — a new silent-drop bug introduced while fixing the old
  silent-duplicate one. Fixed: added `arm_sell_pct`/`trail_buy_pct`/`trail_sell_pct` (COALESCE'd)
  to the explicit dedup check. New regression test confirms two nodes differing only in
  `arm_sell_pct` both persist.
- **CONFIRMED — `buy_alerted`'s same-day unlock never fired for paper nodes** (`active_signals.py`,
  `signals_db.py`): `closed_today()` queried the real `trade_log` only — a paper-trading node's real
  exit only ever lands in `paper_trade_log`, so the unlock in item 6 above could never trigger for
  a paper node, silently keeping the very same-day-rebuy edge this fix exists to recover invisible
  in paper results. Fixed: `closed_today` gained a `paper=False` param (matching the `_pos_tables`
  convention used everywhere else); the unlock now passes `paper=True` for any non-`live`-mode
  node. Also generalized the pending-buy check from item 6 to union `get_paper_pending_buys()`
  alongside the real `get_pending_buys()`, since paper trailing-buy nodes have the identical
  pending-order-resting window (`paper_trading.start_paper_buy`).
- **PLAUSIBLE, addressed — SELL now bounded only by the $100k `HARD_ORDER_CEILING`**
  (`schwab_safety.py`): removing `notional_cap` from the SELL side (item 3 above) also removed the
  only real bound on SELL quantity our own code enforced — oversell protection had always relied
  entirely on Schwab's own rejection, `notional_cap` only coincidentally caught it too. Not a
  regression this session created outright (the underlying gap predates this diff), but directly
  exposed by it. Fixed with a more principled replacement: SELL quantity is now checked against
  the real `open_positions.shares` on file for the ticker (1.1‰ float tolerance), raising
  `SafetyViolation` before ever reaching the broker as a would-be short — narrower than
  `notional_cap` ever was (can't false-positive-block a legitimate large exit) but actually targets
  the real risk (our own inflated share-count bugs, e.g. an unconfirmed top-up).
- **PLAUSIBLE, not currently reachable, backlogged not coded** — the new manual-"Filled" SL call
  (item 4 above, `handle_trail_buy_fill_price`) gates only on `AUTOMATION_ENABLED_TICKERS`, not on
  `node.mode == 'live'`, unlike the BUY-side routing in `_scan_buy_signals`. Traced whether this is
  live-reachable today: the real `pending_buys` table (which is what makes the "Filled" button
  exist at all) is only ever populated from `notify_buy_signal`
  (`signals_notify.py:410`), which `_scan_buy_signals` only calls for `mode=='live'` nodes — a
  research-mode ticker's BUY routes to `paper_trading.start_paper_buy` instead, which has no
  "Filled" button at all. So this gap has no live path to it today; only a future change to that
  routing (e.g. the "Monday mode-scoping" backlog item, or the "run both real+paper regardless of
  mode" idea already on file) could make it reachable. Left as a defense-in-depth note on that
  future work rather than coded now — see `docs/backlog_cache.md`.

Full suite after this second round: 210 passed (was 195 at session start).

## ✅ [live-trading] Resolved 2026-07-25 — coverage-system "compass" v2 complete: node_id/mode/strategy_type identity, six-round Opus review chain, Streamlit dashboard, Slack-callable report
Spanned two sessions (2026-07-24 late night start through 2026-07-25). Real ask: not "add a
scenario_expectations row per known bug" but capture *all* intended behavior across paper/dry_run/
live, so a deviation with no reason is always the actionable finding (automation_principles.md #16's
"no unexplained failure" contract) — and along the way, close two architectural gaps the user flagged
directly: ticker alone isn't a real node identity key, and some safety controls weren't scoped at the
right level.

**Identity migration**: `scenario_expectations`/`coverage_deviations`/`coverage_events` gained
nullable `node_id` (FK `watch_list.id`) + `mode` (paper/dry_run/live), replacing ticker-only identity
(SQLite `UNIQUE` over nullable columns can't dedup — same bug class as `add_node`'s `take_profit=NULL`
failure). `add_scenario_expectation`/`record_deviation` rewritten with explicit COALESCE-based
check-then-upsert (automation_principles.md #13). New `signals_db.get_watch_list_node`/
`get_watch_list_node_by_id` (fail-open by design — pure observability, must never raise into live
control flow).

**Six independent Opus review rounds** on the growing diff (real money at stake) found and fixed,
with rising-then-falling severity: watchlist-scoping bugs in node resolution; `_PENDING_BUY_NODE_KEYS`
missing `'id'` (silently made ~14 of ~18 wired call sites dead code); a stale `pending_buys` account
snapshot (IVV, cleared per user decision); **the most severe finding** — `signals_blocks.py`'s
BUY-alert button and the Reference Report's "Manually Open" button both omitted `account`/`id` from
their node whitelist, so every manual BUY confirmation (the primary way live positions get opened)
could write `open_positions.account=NULL`, invisible to live-state reconciliation — fixed both
locations, a real pre-existing bug independent of the migration that this audit chain happened to
surface; a diverged same-day-buy-warning check misfiring on the live `soxl_ira` margin account; a
dead-code `_fresh_node` helper removed after its own fix made it a no-op; a fail-open regression in
an earlier round's own fix. Full suite 224 passed, `live_sim_harness.py` 6/6.

**Streamlit dashboard + strategy_type axis (2026-07-25)**: `pages/14_Coverage.py` ("Coverage
Compass") — unexplained deviations at top with an inline Explain action, today's per-scenario
pass/fail, the scenario x mode coverage matrix, per-scenario drill-down into raw events. Direct
`sqlite3` reads (not `signals_db`) to avoid constructing a Slack Bolt `App()` inside the Streamlit
process. `coverage_events` gained a nullable `strategy_type` column, auto-derived by
`log_coverage_event` from `node_id`'s real `watch_list.strategy` — no call site needed to change.
`scripts/coverage_matrix.py` gained a `--strategy` filter.

**Slack-callable report (piece #7)**: `signals_notify.send_coverage_report()` runs `scripts/
coverage_check.py`'s real check live and posts a compact summary, wired to a "🧭 Coverage Report"
button on the Morning Report. A required Opus review (automation_principles.md #12) found 2
CONFIRMED + 2 PLAUSIBLE, all fixed: an on-demand weekend tap manufactured permanent false-positive
deviations (a "trade closed today" expectation is trivially false when the market never opened —
confirmed live, 6 real Saturday rows in `trading_live.db`, cleaned up) — fixed with a weekday gate
inside `run_check` itself, covering both the CLI and the Slack caller; the Slack report re-queried
`coverage_deviations` after running the check, so a scenario that deviated earlier and became met
later still rendered UNEXPLAINED, contradicting the check that had just passed it — fixed by having
`run_check` return structured results (and a new `clear_deviation_if_resolved` delete a stale same-
day deviation once met), with the Slack report rendering from that live return value; an unknown
`check_method` silently rendered ✓, now shown as "not checked"; no exception handling meant a
failure would silently no-op the button, now posts a visible failure message.

Final state: full suite 228 passed (was 195 at the start of this arc), `live_sim_harness.py` 6/6.
All 7 pieces of the original 2026-07-24 coverage-system reframe (dashboard, 3rd axis, structured
expected-vs-actual, drill-down, no-unexplained-failure contract, Slack report) are done.

**Session-close Opus review (2026-07-25), covering the full session diff including `signals_db.py`'s
changes not yet independently reviewed** — 2 CONFIRMED + 1 PLAUSIBLE, all fixed: (1) CONFIRMED —
`scenario_key` alone is not a unique key (two active `scenario_expectations` rows can legitimately
share one `scenario_key`, disambiguated by `node_id`/`mode`, e.g. the same designed scenario run
against two nodes on purpose), but `send_coverage_report` and `pages/14_Coverage.py` both keyed their
deviation-lookup dicts by `scenario_key` alone — explaining one node's deviation would silently mask
a different node's still-unexplained one. Fixed: `coverage_check.run_check`'s result dicts now carry
`node_id`/`mode`, and both callers key by the composite `(scenario_key, ticker, node_id, mode)`. Live
data checked: the 6 pre-migration `node_id=NULL` canary rows are already `active=0` (not double-
counting today), so this wasn't yet live-triggering, but the code path was a real bug. (2) CONFIRMED
— `pages/14_Coverage.py`'s "Today's Scenarios" table inferred "✓ met" from "no deviation row today"
alone, without checking whether `coverage_check.run_check` actually evaluates that scenario at all —
an `occasional`/`regression-only`-frequency row or an unrecognized `check_method` would render green
having never been checked. Fixed: the page now imports `scripts.coverage_check.CHECKERS` and renders
"— not daily-checked" / "? unknown check_method" instead of assuming met for those cases. (3)
PLAUSIBLE — `clear_deviation_if_resolved` deleted unconditionally, including a row a human had
already explained; if the scenario later became met on a same-day re-check, the explanation and the
record that anything had deviated at all would be silently destroyed. Fixed: added `AND reason IS
NULL` to the delete, matching `record_deviation`'s existing "never clobber a reason" rule. 3 new
regression tests. Full suite 230 passed (was 228), `live_sim_harness.py` 6/6.

## ✅ wl_id-keyed refactor — implemented, reviewed (2 Opus rounds), landed 2026-07-25/26
Resolves the systemic ticker-only-keying gap scoped over the prior two sessions (see the
2026-07-25/26 design-conversation entry above, now historical) — everything in the live-trading
stack that assumed "one ticker == one active `watch_list` node" is rekeyed onto the `watch_list`
row's own PK (`id`, called `wl_id` in code/comments), across 20+ sites in `signals_db.py`,
`active_signals.py`, `paper_trading.py`, `signals_helpers.py`, `signals_handlers.py`,
`signals_notify.py`, `schwab_safety.py`.

**Schema migration** (`signals_db.ensure_tables()`): new `wl_id` column on `open_positions`,
`paper_positions`, `pending_buys`, `paper_pending_buys`. Backfill: `pending_buys`/
`paper_pending_buys` deterministically from `node_json['id']` (already embedded via
`_PENDING_BUY_NODE_KEYS`); `open_positions`/`paper_positions` best-effort matched against current
`watch_list` on `(ticker, strategy, version, window, account)`, left `NULL` on an ambiguous match
(confirmed real on `cache/live/trading_live.db`: EDC's real 423-share legacy position, id=15,
matches 2 candidate rows across 2 superseded watchlists and is correctly left `NULL` rather than
guessing). Applied to the real live DB this session (daemon confirmed not running first; backed up
to `cache/live/trading_live.db.bak_wl_id_migration_20260725_130800` beforehand).

**Key fixes on the real (non-paper) order path**: `schwab_safety._live_ticker_accounts()` changed
from `{ticker: account}` to `{ticker: set-of-accounts}` (this is what actually unblocks the
motivating SOXL-in-two-accounts design — the old single-value map hard-rejected a second live
node's real orders as "assigned to the wrong account"); `open_position()`'s duplicate-position dedup
moved from `(ticker, window)` to `wl_id` (with a `(wl_id IS NULL AND ticker=? AND window=?)`
fallback clause so legacy/unbackfillable rows keep the old protection); all 6 BUY-side Slack button
handlers now clear/mark `pending_buys` rows by `wl_id` instead of ticker, matching the SELL-side
`position_id` pattern that was already correct; `_reconcile_buy_fill` gained a `wl_id` disambiguation
hint (bails + alerts rather than guessing when it doesn't match); `_place_stop_loss_for_position`
now resolves the position by `wl_id` instead of ticker (was sizing/anchoring the stop off whichever
position had the latest `entry_time`, regardless of node); `check_order`'s oversell guard now
resolves the position by `(ticker, account)` via new `get_open_position_for_account` instead of
ticker alone.

**Additive, not built out further**: node-level automation pause (`schwab_safety.
pause_node_automation`/`resume_node_automation`/`node_automation_enabled`) alongside the existing
ticker-level pause, AND-gated (`ticker_automation_enabled(ticker) AND node_automation_enabled(wl_id)`)
— no Slack button wired yet, console/script-only. The per-node `dry_run` override idea (backlogged
2026-07-26, see below) remains gated on this landing and being observed correct live first.

**Known, deliberately-not-fixed residuals** (documented in code, not silently dropped):
- `check_order`'s `node_automation_enabled` gate still derives `_node_id` via a ticker+account fuzzy
  `get_watch_list_node()` lookup (returns `None` on ambiguity → pause silently no-ops for 2 nodes
  sharing both ticker AND account) — full plumbing of a real `wl_id` through `schwab_client`'s 6
  order-placement functions was scoped as its own follow-up, not done this session.
- `_last_sale_recovery`'s new narrowing to `(ticker, strategy, version, window, account)` is a real
  live sizing-behavior change: most v5 nodes' `trade_log` history is under a different `version`
  (v4/v3.x) so they now fall back to `starting_notional` instead of compounding off the last
  recycle. Confirmed benign (no node has a `NULL starting_notional`), but stated explicitly since
  it changes real position sizing.
- Legacy Slack messages predating the `node['id']` payload field (added 2026-07-25, commit
  `3accd4e`) would `KeyError` if clicked — confirmed no currently-outstanding `pending_buys` row
  lacks it; self-expiring, not fixed defensively.
- `_has_open_order`/`_has_open_buy_order_in_account`/`_has_open_sell_order` (schwab_safety.py)
  deliberately stay ticker(+account)-keyed — permanent policy constraint, the real broker order book
  has no `wl_id` concept; two live nodes on the same ticker in the *same* account still cannot both
  hold resting orders.
- Per-ticker pause/kill-switch stays ticker-scoped by explicit user decision (a pause is meant to be
  a blunt instrument); a proposed 3rd, strategy-wide pause level was explicitly rejected as
  over-engineering ("I'd probably just kill the full program").

**Verification**: full suite 232 passed throughout (was 232 pre-refactor too — pure rekeying, no
new test surface beyond fixing tests that hardcoded the old ticket-keyed shapes),
`live_sim_harness.py` 6/6, `verify_trailing_buy_resolution.py --tickers AGQ,SOXL` and
`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` both clean (expected — this refactor is
identity-keying only, doesn't touch signal/fill computation). Two independent Opus review rounds:
first round (against the initial implementation) found 11 issues, 2 HIGH on the real order path —
9 fixed same session, 2 left as documented limitations (above). Second round (against the fixed
diff, explicitly re-verifying the first round's fixes and sweeping fresh) confirmed all 9 fixes
landed correctly, then found 4 more real issues (1 HIGH: the oversell-guard ticker-only lookup
above; 1 MEDIUM-HIGH: `drain_fill_queue` had started passing its own fuzzy ticker+account node-id
guess as a hard disambiguation gate into `_reconcile_buy_fill`, which could block reconciliation of
a real confirmed fill — reverted to logging-only; 2 MEDIUM: `_pos_key`'s `last_seen_bar`-collision
fix wasn't applied to `paper_trading.py`'s copy of the same lookup, and `open_position_keys`'s
NULL-`wl_id` fallback was dropped in the exact same way `open_position()`'s SQL dedup already
needed one) — all 4 fixed and re-verified (full suite + harness + parity scripts green again).

**Not done this session** (deliberately deferred, per the original scoping): creating the second
SOXL `soxl_ira` node — planned as the next step once this refactor is observed correct in the field.

Also fixed opportunistically while verifying: `scripts/setup_2026_07_24_soxl_ira_live_test.py`'s
top-level code (real DB writes) was unconditionally executing on every bare `pytest` invocation
because its filename ends in `_test.py`, matching pytest's default discovery glob — wrapped in
`if __name__ == "__main__":`. Pre-existing hazard, unrelated to this refactor, found only because
the schema migration briefly broke it mid-run (caught before any real data was written past the
existing idempotent `add_node` calls).

## ✅ Resolved 2026-07-26 — Opus review of the paper_alert_verbose/account-dedup diff: 1 HIGH, 2 lower findings, all fixed
Background Opus review of the prior session's `paper_trading.py`/`signals_db.py` diff (paper-alert
suppression + the `account`-inclusive `watch_list` dedup, see the entry above) came back with:
1. **HIGH, fixed**: the new `account`-inclusive dedup broke idempotency of two live-setup scripts
   (`scripts/add_labu_backup_node.py`, `scripts/setup_2026_07_24_soxl_ira_live_test.py`) that called
   `add_node(...)` without `account`, then patched it in via a raw `UPDATE watch_list SET account=...`
   afterward. A rerun's dedup check now compares `account=NULL` against the real stored account,
   never matches, and inserts a second live node with its own `wl_id` — reproduced live against a
   copy of `trading_live.db` (91→92 rows, a duplicate LABU node). This is the exact failure mode
   behind the two `pre_dup_node_cleanup` backups already on file from 2026-07-24. Fixed: both
   scripts now pass `account="soxl_ira"` directly into `add_node`, `_set_account` helpers deleted,
   stale "rerun is a harmless no-op" docstring claims corrected. `scripts/live_sim_harness.py` had
   the identical pattern but was already safe (wipes its sim DB first) — left as-is.
2. **MEDIUM (forward hazard), fixed**: the `watch_list` account-rebuild migration
   (`signals_db.ensure_tables()`) copied columns via a hardcoded 24-column list that ran *after* the
   ALTER ladder — on any DB that hadn't been rebuilt yet, a future new column added by the ALTER
   ladder within the same `ensure_tables()` call would be silently dropped by the rebuild's fixed
   list. Fixed: column list now derived from `PRAGMA table_info(watch_list)` at runtime. Verified
   against a synthetic pre-migration DB (old schema, no `account` in `UNIQUE`, ALTER ladder + rebuild
   both firing in one call) — row survived with all 24 columns intact.
3. **LOW, fixed**: `update_paper_buys`'s fill alert read `paper_alert_verbose` off the frozen node
   snapshot captured in `pending_buys.node` at signal time, not the live `watch_list` row — flipping
   the flag on after a paper buy signal fired but before it filled wouldn't produce the "just turned
   verbosity on" alert you'd most want. Fixed: re-reads the live node via
   `get_watch_list_node_by_id` before checking the flag. Zero live impact today (0 rows in
   `paper_pending_buys` at review time).
Two PLAUSIBLE-but-not-CONFIRMED items left open, no live impact: the same rebuild's `UNIQUE(`
string-match schema check could false-positive on a hand-modified DB with no `UNIQUE` clause at all
(needs a hand-crafted DB to trigger); the rebuild has no explicit transaction wrapping (matches the
pre-existing 2026-07-18 `watchlist_id` migration's shape, not a new pattern). Full suite: 232
passed. `live_sim_harness.py`: 6/6 scenarios passed.

## ✅ Resolved 2026-07-26 — session-wrap Opus review of the above fix: 1 CONFIRMED (fixed), 1 LOW (no action)
Mandatory review of this session's own diff (`signals_db.py`/`paper_trading.py`, the 3 fixes above)
before closing. Findings:
1. **MEDIUM, CONFIRMED, fixed**: the `watch_list_new` rebuild table had no `DROP TABLE IF EXISTS`
   before its `CREATE TABLE` — reproduced live: if the `INSERT`/rebuild ever fails partway (e.g. a
   future ALTER-added column not yet reflected in the hardcoded `CREATE TABLE watch_list_new`
   definition), the orphaned table survives the failed transaction, and every subsequent
   `ensure_tables()` call (i.e. every daemon startup) dies at the `CREATE TABLE` with "table already
   exists" — a total outage, not a degraded mode, requiring manual `DROP TABLE watch_list_new` to
   recover. Real data was never at risk (the failing INSERT/DROP/RENAME transaction rolls back
   cleanly). Fixed: one `c.execute("DROP TABLE IF EXISTS watch_list_new")` before the executescript.
2. **LOW, no action**: `paper_trading.py`'s live-node re-read for the verbose-alert gate returns
   `None` (silently suppressing the alert) if the `watch_list` row is deleted between signal and
   fill — paper-only, alerts-only, no trade/P&L impact, flagged as an intentional-vs-accidental
   question rather than a defect.
Also confirmed by this review, worth keeping: the `paper_alert_verbose` bug fixed earlier this
session was actually worse than "stale" — the key was never in `_PENDING_BUY_NODE_KEYS`
(`signals_db.py`), so the frozen snapshot never carried it at all and the alert **could never fire**
regardless of flag state, not just on a narrow timing window. And the executescript→3-separate-
`execute()` change actually *improved* transaction atomicity (verified via a direct `in_transaction`
probe) rather than introducing a new crash window, since the old `executescript()` form auto-
committed the `DROP`/`RENAME` as separate transactions.
Full suite: 232 passed. `live_sim_harness.py`: 6/6 scenarios passed.

## ✅ Resolved 2026-07-26 — "run both" real+paper restructure dropped; the two-node pattern already covers it
Item 2 of the 2026-07-24 "paper trading fully dormant" Monday follow-up list (restructure the
BUY-routing if/elif so a node runs both the real/dry_run alert path and paper trading regardless of
`mode`) was dropped after verifying the wl_id refactor already makes a simpler alternative safe: two
separate `watch_list` nodes for the same ticker (one `research`, one `live`) — the exact DPST
pairing added last session — give continuous paper coverage and live trading simultaneously with no
code change. Verified before dropping: `paper_trading.py`'s dedup is `wl_id`-keyed, not
ticker-keyed, and `active_signals.py`'s `open_position_keys` (line 530-531) already merges real +
paper positions, so the two nodes don't collide. Item 3 (`account=None` gap on 15/24 `mode='live'`
nodes) remains open, still targeted for Monday 2026-07-27.

## ✅ Resolved 2026-07-26 — Morning Report / signal-window Slack alerts hit the 50-block limit again (25 nodes, up from 16 at the 2026-07-22 fix); replaced per-row block-shrinking with real chunking
The 2026-07-22 fix (collapsing 3 `actions` blocks/row into 1) bought headroom, not a permanent fix
— the watchlist grew from 16 to 25 nodes and the Morning Report hit Slack's `invalid_blocks` 50-block
cap again (confirmed in `logs/active_signals.log`). Rather than shrinking per-row block count again
(a fix with an expiration date), built `signals_blocks._post_chunked(text, fixed_blocks, units,
max_blocks=50)`: greedily packs per-row block groups into <=50-block chunks, posts the first chunk
normally and any overflow chunks as Slack thread replies (new `thread_ts`/`reply_broadcast` params on
`_post_message`) so the report still reads as one thing and overflow still triggers a mobile
notification. `signals_notify.send_reference_report` and `_send_window_alert` (the 10:25/15:25 ET
HIGH ALERT) both rewritten to build a list of row-units and call `_post_chunked` instead of
concatenating one flat `blocks` list.
**Session-wrap Opus review of the real diff (`signals_blocks.py`/`signals_notify.py`) found 4
CONFIRMED issues, all fixed same session**: (1) most severe — a chunk-2+ Slack post failure was
invisible to `log_coverage_event("morning_report_delivery", ...)`, which read only the first chunk's
`(channel, ts)` and would log "sent" even if the Buy Candidates section silently never posted; fixed
by having `_post_chunked` track delivery across every chunk and return `ts=None` if any chunk failed,
so `channel and ts` truthy-checks at call sites correctly read partial delivery as not-fully-sent. (2)
`_send_window_alert` (the signal-window HIGH ALERT, arguably more load-bearing than the morning
report — no daily backstop if it silently fails) was never converted and had the identical unbounded
50-block bug left in place; converted to `_post_chunked` too. (3) overflow thread replies were
mobile-notification-silent (no `reply_broadcast`) — fixed by adding `reply_broadcast=True` on every
chunk after the first, consistent with the project's standing "every alert must render cleanly on
mobile" convention. (4) test gap — the original tests only exercised `_post_chunked` with synthetic
block lists, nothing proved real `_ticker_block` output actually chunks correctly at scale; added
`test_send_reference_report_actually_chunks_a_large_watchlist` (60 synthetic rows through the real
`build_reference_table`→`_ticker_block`→`_post_chunked` path). 2 more regression tests target
the partial-failure detection itself. Also separately: my own test-script invocations while verifying
this fix live wrote 5 real `morning_report_delivery` rows into `cache/live/trading_live.db`'s
`coverage_events` table (not real daemon/button-triggered events) — same pollution class as the
2026-07-25 pytest incident; backed up the DB and deleted the 5 rows after confirming with the user
this wasn't the sticky `coverage_deviations` ticket model (raw event log, not a deviation record).
Full suite: 289 passed (was 277 at session start). `live_sim_harness.py`: 7/7. `signals_invariants.py`:
0 known violations (UDOW's stale test position was retroactively closed last session).

## ✅ Resolved 2026-07-26 — Trade-Flow Accountability Grid redesigned to show per-mode (Paper/Dry-run/Live) status independently, not one collapsed overall status
`compute_status()` (`scripts/coverage_registry.py`) picks one overall status per scenario in
priority order (live > dry_run > paper) and returns as soon as any mode has real events — so a
scenario with genuine live evidence *and* zero paper evidence rendered as fully green
`verified-live`, with no way to eyeball which modes actually have proof (raised directly by the
user reviewing the grid). New `compute_mode_statuses(row)` computes each of paper/dry_run/live
independently: for `coverage_events`-mechanism rows, buckets by real mode with the same
good/bad-results logic as `compute_status`; for a row scoped via `mode_filter` (e.g.
`dry_run_buy_synthesis`, which disambiguates a `scenario_key` shared with `paper_trading.py`), the
other two modes report `not-applicable` rather than a false gap; for `scenario_expectations`-
mechanism rows, queries `coverage_deviations` (which carries its own `mode` column) per mode instead
of across all modes at once; `offline_only`/`open_price_quality_log`/`none` mechanisms (not
mode-scoped by nature) mirror the same status across all three columns. `pages/14_Coverage.py`'s
main grid gained 3 new independently-colored columns (Paper/Dry-run/Live, each its own cell
background via a new `_heat_row_and_mode_cells` styling function) plus a compact `P/D/L` one-glance
column (e.g. `P🟩 D⬜ L🟥`), while keeping the existing whole-row-colored `Status` column for the
existing sort order. Confirmed live: `dry_run_buy_synthesis` now shows real `verified` dry_run
evidence (2 real events, 2026-07-26) — closes the "zero real firings ever" gap flagged at the start
of this same session. Also fixed same session: the Streamlit process itself (running since 2026-07-25)
was serving stale pre-2026-07-28 `coverage_registry.py` logic (Streamlit's file-watcher doesn't
reliably reload changes in imported-but-not-page modules) — a browser hard-refresh alone didn't fix
it, needed a process restart. 5 new regression tests in `tests/test_coverage_check.py`. Full suite:
289 passed (combined with the chunking fix above, same session).
