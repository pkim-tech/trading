"""Trade-flow test accountability grid: one row per real logic branch that
needs to be proven to behave correctly, migrated from docs/live_test_coverage.md's
hand-maintained table (2026-07-27). Unlike that table, status is never typed by
hand -- compute_status() derives it live from real coverage_events/coverage_deviations
rows every time this is read, so it can't go stale the way the markdown ledger did
(caught stale once already, 2026-07-25). offline_coverage/code_path/notes stay free
text -- there's nothing to query for "does a unit test exist."

check_mechanism per row:
  'coverage_events'       -- a real log_coverage_event(scenario_key, mode, ...) call
                             exists at this code path; status is derived by looking
                             at real rows for that scenario_key, bucketed by mode.
  'scenario_expectations' -- proven (if at all) via a daily trade_lifecycle check in
                             coverage_deviations, not a raw coverage_events firing.
  'offline_only'          -- by design has no live component (kernel/parity scripts).
  'open_price_quality_log' -- proven via the dedicated open_price_quality_log table
                             (its own logging path, not coverage_events).
  'none'                  -- no code hook exists at all yet; can't be derived, only
                             a manual TODO until something is built to log it.

Run directly for a plain-text report: .venv/bin/python scripts/coverage_registry.py

offline_proof (added 2026-07-26, see docs/backlog_cache.md's re-triage entry) is a
second, orthogonal axis to status -- status above answers "is there LIVE proof this
branch works," which is correctly 'wired-never-fired' for a scenario that's only
ever exercised in pytest (isolated_db fixture keeps pytest from ever touching real
coverage_events, by design, since pytest polluting that table was itself a real
2026-07-25 incident). offline_proof answers the *other* question -- "is there ANY
proof, live or offline" -- so a policy-internal branch (decided entirely by our own
code, no broker round-trip) with a real event-asserting unit test doesn't read as
having zero coverage of any kind. Derived by grepping tests/*.py fresh every run,
same "never hand-typed" discipline as compute_status -- a hand-maintained
scenario_key -> test-name mapping would rot exactly like docs/live_test_coverage.md
did. Deliberately conservative: only a real `get_coverage_events(scenario_key=...)`
call in a test counts as 'event-asserted' (proof the log_coverage_event line itself
is wired, not just that the surrounding behavior works); the scenario_key string
appearing anywhere else in a test file counts as 'behavior-only' (some test likely
exercises this code path, but the log call itself could be deleted and nothing
would catch it); no mention at all is 'none'. status stays the single source of
truth for "verified-live" et al -- offline_proof never substitutes for it.

'scenario_expectations'-mechanism rows have a structural blind spot in the above:
their real proof asserts against trade_log/pending_buys/fake_broker order state
(e.g. tests/test_fake_broker_pinned_entry_scenario.py), never against
get_coverage_events(scenario_key=...) -- there's no coverage_events call on that
code path to assert at all, by design (see the mechanism table above). A code_path
function-name match doesn't reliably fix it either: real tests correctly drive the
real *entry point* (e.g. notify_buy_signal), not code_path's often-internal helper
names, so a name-match would miss genuine proof too (confirmed empirically against
test_fake_broker_entry_scenario.py's market_buy_placement coverage while fixing this,
2026-08-03). Fixed via an explicit, self-declared marker instead: a test file whose
module docstring contains `registry id 'some_id'` is asserting "this file's test(s)
are real evidence for REGISTRY row `some_id`" -- see _REGISTRY_ID_MARKER_RE below.
Deliberately opt-in/self-declared (matches every other proof tier's stance of "only
count it if a human deliberately wired it," not an inferred heuristic) -- a test
author adds it once, same effort as any other docstring note.
"""
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db
# Deferred-safe: scripts/coverage_check.py itself only imports coverage_registry
# inside a function body (never at module scope), so this module-level import
# can't create a real import cycle in either load order.
from scripts.coverage_check import _check_trade_lifecycle, _is_trading_day

DB_PATH = "./cache/live/trading_live.db"
_TESTS_DIR = Path(__file__).resolve().parent.parent / "tests"

REGISTRY = [
    dict(id='pre_action_state_verification',
         scenario="Real broker position compared against local DB belief at the exact moment "
                  "a real order is about to be considered",
         code_path="schwab_safety.check_order (_log_pre_action_state_verification)",
         offline_coverage="tests/test_schwab_safety.py: test_pre_action_state_verification_"
                           "logs_match/logs_mismatch/fetch_failure_does_not_block",
         check_mechanism='coverage_events', scenario_key='pre_action_state_verification',
         bad_results=[],
         notes="Built 2026-07-29 (deferred from the 2026-07-28 fix session's design list). "
               "Deliberately detection-only, not blocking -- logs 'match'/'mismatch'/"
               "'fetch_failed' on every real BUY/SELL check_order call, so a real tolerance/"
               "blocking policy can be set from actual data instead of a guess. bad_results "
               "left empty since 'mismatch' is the interesting signal to review, not "
               "necessarily a failure of this check itself."),
    dict(id='pinned_entry_trigger',
         scenario="Pinned entry trigger fires at the right bar/price",
         code_path="active_signals._scan_pinned_entry",
         offline_coverage="scripts/verify_pinned_entry_vs_backtest.py (5/6 tickers clean, AGQ explained); "
                           "live_sim_harness.py::scenario_pinned_entry_trailing_buy",
         check_mechanism='scenario_expectations', scenario_key='canary_pinned_entry',
         notes="Entry-mechanism itself not separately verified by the daily check (known MVP limitation)."),
    dict(id='market_buy_placement',
         scenario="Real market-order BUY placement + fill confirm",
         code_path="_attempt_automated_market_buy, _sync_confirm_and_protect",
         offline_coverage="test_part4_entry_trigger.py; live_sim_harness.py::scenario_ambient_market_buy_entry",
         check_mechanism='scenario_expectations', scenario_key='canary_market_buy_exit',
         notes="No real (non-dry_run) order has ever been placed by this system. Entry mechanism not "
               "separately verified by the daily check."),
    dict(id='canary_full_lifecycle',
         scenario="A/A-mirror (IVV/SPXU): full happy-path lifecycle same day -- entry -> bounce-fill -> "
                  "arm -> trailing-sell exit, hair-trigger config (fixed_sl=30% unreachable, arm/trail "
                  "thresholds 0.1%) forcing every state transition to fire same-day",
         code_path="Whole BUY->arm->TRAIL scan chain (active_signals._scan_buy_signals / "
                    "check_sell_condition) -- a full-daemon regression, not one function",
         offline_coverage="N/A -- deliberately a full-stack live regression, not isolated to one "
                           "fake_broker scenario",
         check_mechanism='scenario_expectations', scenario_key='canary_full_lifecycle',
         notes="Canary letter A (docs/design.md's A-F design, see scripts/list_canary_nodes.py). "
               "The baseline 'does the whole mechanism work end to end' proof. Added to the Grid "
               "2026-08-08 -- previously checked daily by coverage_check.py but had no Grid row."),
    dict(id='canary_early_sl',
         scenario="B/B-mirror (QQQ/QID): entry -> bounce-fill -> immediate same-day SL "
                  "(arm=10% unreachable, fixed_sl=0.1% hair-trigger)",
         code_path="check_sell_condition's SL branch, exercised via the daily poll",
         offline_coverage="N/A -- live regression",
         check_mechanism='scenario_expectations', scenario_key='canary_early_sl',
         notes="Canary letter B. Added to the Grid 2026-08-08."),
    dict(id='canary_overnight_carry',
         scenario="D/D-mirror (DIA/SDOW): trail_buy_pct=5.0% wide enough that the bounce-fill is "
                  "unlikely to complete same day -- expects a pending trailing-buy still resting "
                  "(pending_buys row) at EOD, carried into tomorrow's open",
         code_path="pending_buys tracking / running_low across a session boundary -- regression check "
                    "for the 2026-07-22 stale-cache-at-open fix",
         offline_coverage="N/A -- live regression",
         check_mechanism='scenario_expectations', scenario_key='canary_overnight_carry',
         notes="Canary letter D. A same-day fill isn't itself wrong, just not the scenario this canary "
               "is designed to exercise -- coverage_check.py's check already handles that ambiguity "
               "(either a pending row or a closed trade counts as 'something real happened,' only the "
               "former matches design intent). Added to the Grid 2026-08-08."),
    dict(id='canary_time_exit',
         scenario="F/F-mirror (XLF/FAZ): arm (take_profit=50%) and SL both practically unreachable, "
                  "max_hold_hours=2 -- the only exit path left open is TIME",
         code_path="check_sell_condition's TIME branch / exit_forced_by_hold_time",
         offline_coverage="N/A -- live regression",
         check_mechanism='scenario_expectations', scenario_key='canary_time_exit',
         notes="Canary letter F. Added to the Grid 2026-08-08."),
    dict(id='canary_bull_bear_pair',
         scenario="G pair (JNUG/JDST): same-underlying (junior gold miners) bull/bear pair, correlation-"
                  "check design",
         code_path="No dedicated code path -- both nodes run the standard mechanism; this canary is "
                    "about the PAIR relationship, not a single mechanism",
         offline_coverage="N/A -- live regression",
         check_mechanism='scenario_expectations', scenario_key='canary_bull_bear_pair',
         notes="Canary letter G. No real correlation-verification logic built yet (see "
               "docs/backlog_cache.md) -- monitored the same simple same-day-trade-happened way as "
               "every other canary. Added to the Grid 2026-08-08."),
    dict(id='sl_sync_placement',
         scenario="SL placed at signal price after fill (sync path)",
         code_path="_place_stop_loss_for_position via sync confirm",
         offline_coverage="Unit test (SL anchors to signal_price)",
         check_mechanism='coverage_events', scenario_key='sl_placement',
         bad_results=['blocked', 'failed_unexpectedly'],
         notes="2026-07-24 real attempt was blocked by the pre-fix daily_order_cap bug (now fixed) -- "
               "still needs a real successful placement observed post-fix."),
    dict(id='sl_async_fallback',
         scenario="SL placed via async fallback (timeout path)",
         code_path="check_auto_fills/drain_fill_queue/check_gap_resize fill poll",
         offline_coverage="Unit test only",
         check_mechanism='coverage_events', scenario_key='sl_placement_fast_confirm_timeout',
         notes="Timeout branch confirmed reachable live (VOO, dry_run, 2026-07-24); doesn't confirm the "
               "fallback SL placement that follows actually succeeds."),
    dict(id='sl_order_fills_independent_detection',
         scenario="A position's own resting protective stop (sl_order_id) fills independently -- "
                  "before the daemon's bar-close signal check ever computes an exit -- and gets "
                  "detected/closed promptly, not left stuck open indefinitely",
         code_path="signals_notify.check_sl_order_fills (new, polled every cycle BEFORE the "
                    "bar-close exit scan in active_signals.py's run_loop)",
         offline_coverage="tests/test_fake_broker_sl_order_fills_scenario.py: 5 tests -- "
                           "no-exit-pending close, real post-arm TRAILING_STOP fill labeled TRAIL "
                           "(not SL), trailing=True-but-unrepointed-order still labeled SL (the "
                           "mislabel a paired Opus review caught), dedup with exit_pending, and the "
                           "fewer-shares-than-tracked guard (alerts instead of auto-closing)",
         check_mechanism='coverage_events', scenario_key='automated_exit_confirmed',
         # 'closed' (the other 2 call sites sharing this scenario_key) and 'qty_mismatch'
         # (a real detected mismatch, alert-only, not a close) are both excluded -- the
         # code now logs a genuinely distinct 'closed_via_sl_order_poll' for this exact
         # path (fixed 2026-08-14 alongside this bad_results change; the notes below's
         # "distinguish by detail" claim was never actually enforced by compute_status,
         # which only ever aggregates by (mode, result), found by Opus audit). This
         # resets this row's history: real events logged before this fix (LABD 2026-08-07,
         # YINN 2026-08-12/13) used the old shared 'closed' result and can no longer be
         # told apart from the sibling paths, so they no longer count -- honest, not a
         # regression; a genuine new fill via this path will re-earn verified-live.
         bad_results=['closed', 'qty_mismatch'],
         notes="Real incident, 2026-08-07 (LABD, soxl_ira): a 1-share, hair-trigger (fixed_sl=0.3%) "
               "position's real stop filled ~7 minutes after entry, but check_own_sell_fills/"
               "check_auto_fills only ever polled trail_state.exit_pending.order_id (which only exists "
               "once OUR bar-close check has already computed an exit) -- open_positions stayed stuck "
               "for 8+ hours, and every retry against the now-terminal sl_order_id 400'd, posting a "
               "false 'UNPROTECTED -- place a stop-loss manually' alert on an already-safely-closed "
               "position. Fixed same day, verified against the real stuck row (closed correctly: "
               "entry $7.685 -> exit $7.66, SL, -0.33% pnl). exit_reason correctly derived from ORDER "
               "IDENTITY (sl_order_id == trail_state.exit_order_id), not the trailing flag -- an "
               "earlier draft keyed off state['trailing'] alone and would mislabel a genuine SL fill "
               "as TRAIL whenever arming was persisted but the real trailing-sell placement had failed "
               "(caught by a paired independent-cold + contextual Opus review before landing). Shares "
               "the automated_exit_confirmed scenario_key with check_own_sell_fills/check_auto_fills' "
               "existing exit-fill detection -- distinguish by detail containing 'via_sl_order_poll=1'. "
               "No live re-confirmation yet since the fix landed beyond the one already-stuck LABD row "
               "it was applied to manually."),
    dict(id='post_fill_topup',
         scenario="Post-fill top-up places a real order",
         code_path="_reconcile_fill",
         offline_coverage="test_part3_gap_resize.py; live_sim_harness.py::scenario_reconcile_fill_topup",
         check_mechanism='coverage_events', scenario_key='top_up',
         bad_results=['blocked', 'failed_unexpectedly', 'db_update_failed_after_real_order',
                      'overspent_no_corrective_sell'],
         notes="2026-07-24 real attempt was blocked by the pre-fix daily_order_cap bug (now fixed). "
               "Real successful top-up observed live 2026-08-10: RETL's (soxl_ira) real drought-overlay "
               "entry filled under target notional and _reconcile_fill placed a genuine top-up "
               "(result='placed', 2 shares, 78->80). Same day, ERY's staged test independently fired "
               "the overspent_no_corrective_sell branch (a stale tiny _last_sale_recovery basis from an "
               "earlier 1-share test trade, not a bug) -- confirms that branch too."),
    dict(id='gap_resize',
         scenario="Overnight gap-resize (cancel trailing buy, replace w/ market)",
         code_path="signals_notify.check_gap_resize, _GAP_CHECK_WINDOW",
         offline_coverage="Unit tests; live_sim_harness.py::scenario_gap_resize",
         check_mechanism='coverage_events', scenario_key='gap_resize',
         bad_results=['no_account', 'price_lookup_failed', 'cancel_failed', 'cancel_unconfirmed',
                      'blocked', 'rejected'],
         notes="Needs a real overnight gap past trail_buy_pct while daemon is live."),
    dict(id='exit_arm_latency',
         scenario="Exit-arm latency scan (pinned) actually evaluates a newly-closed bar",
         code_path="_scan_pinned_exit_arm",
         offline_coverage="live_sim_harness.py::scenario_pinned_exit_arm",
         check_mechanism='coverage_events', scenario_key='exit_arm_latency',
         notes="Instrumented 2026-07-28 -- logs each time the scan evaluates a genuinely new bar "
               "(not the skip-no-new-bar branch) for an automation-scoped open position."),
    dict(id='entry_abandon_timeout',
         scenario="A trailing-buy that never bounces is abandoned (real order cancelled or local "
                  "row cleared) once max_hold_hours elapses, instead of resting forever",
         code_path="signals_notify.check_entry_abandon; paper_trading.update_paper_buys (paper leg)",
         offline_coverage="tests/test_entry_abandon.py",
         check_mechanism='coverage_events', scenario_key='entry_abandon_timeout',
         bad_results=['unrecognized_account', 'no_order_id_on_file', 'cancel_failed', 'cancel_unconfirmed'],
         notes="Built 2026-07-31 (was the [HIGHEST] item in that session's exit/arm/entry audit). "
               "First-ever production caller of schwab_client.cancel_order -- a real broker mutation, "
               "not yet observed against a genuinely resting real (non-dry_run) order live. As of "
               "build time, DIA/SDOW (dry_run 'ira') are the only real pending rows exercising the "
               "no-real-order-to-cancel path."),
    # 'kernel_fill_parity' removed 2026-08-13 -- this row asked a categorically different question
    # than everything else in the Grid: does the backtest kernel's fill-resolution math agree with
    # the live code's own version of the same math (a code-consistency check between two Python
    # implementations), not "does a real execution mechanism work" (what every other row proves).
    # No live/canary node can ever touch this -- it's not a trade, it's not a broker interaction,
    # it's a manual rerun of verify_trailing_buy_resolution.py/verify_trailing_sell_resolution.py.
    # Already has a real, standing home: docs/pre_commit_checklist.md's "if active_signals.py/
    # strategies.py/backtester.py changed this session" item, run at every session wrap. The Grid
    # row was redundant documentation pointing at a check that already runs somewhere better-suited
    # to a code-review-time question.
    dict(id='open_price_quality',
         scenario="Open-price quality (real open vs. what live code captured)",
         code_path="_scan_pinned_entry logging -> open_price_quality_log",
         offline_coverage="scripts/verify_open_price_quality.py",
         check_mechanism='open_price_quality_log', scenario_key=None,
         notes="Wired 2026-07-28 to the real open_price_quality_log table (92 real rows since "
               "2026-07-22) -- this data was already being collected, it just wasn't recognized by "
               "compute_status, which only knew about the coverage_events/scenario_expectations/ "
               "offline_only mechanisms."),
    dict(id='cash_check',
         scenario="Cash-balance check blocks an order correctly",
         code_path="schwab_client.get_account_balance + schwab_safety.check_order",
         offline_coverage="8 unit tests (test_schwab_safety.py), Opus-reviewed 2026-07-21; "
                           "3 new field-preference tests 2026-08-12 (test_schwab_client.py) pin the "
                           "cashBalance/cashAvailableForTrading/availableFunds fallback order",
         check_mechanism='coverage_events', scenario_key='cash_check',
         notes="Passing path verified live+dry_run 2026-07-24. Blocking case (insufficient funds) never "
               "observed -- every real event so far shows result=passed. Fixed 2026-08-12: now reads "
               "real settled cashBalance instead of margin-inclusive availableFunds, closing the "
               "brokerage core-entry leverage gap (the invariant that used to guard this, "
               "check_brokerage_not_live_with_unresolved_leverage_gap, was removed the same day once "
               "the gap was fixed and brokerage.trading_enabled was flipped True -- see "
               "check_margin_floor_zero_for_trading_enabled_accounts for the narrower piece that's "
               "still guarded) -- same scenario_key, no new coverage_events branch, but the real "
               "fetched value changed. Confirmed live same day (real brokerage account query) that "
               "cashBalance == availableFunds on every account today (none holds margin), so no "
               "observable live behavior change yet."),
    # 'second_ticker_one_account' (scenario_key 'second_ticker_buy_blocked') removed 2026-08-13 --
    # redundant. Its only reachable result (blocked_unpriced, a price-fetch-failure edge case) is
    # excluded as non-evidence, and the real cash-aware behavior it claimed to test is already
    # fully proven by 2 sibling rows: cash_check (result='blocked_insufficient' for the blocked
    # case) and second_ticker_buy_allowed_when_cash_sufficient below (the allowed case). No harness
    # could ever make this row independently prove anything -- a Grid attribution issue, not a
    # code defect, and the row added no information once traced back to its real logging.
    dict(id='second_ticker_buy_allowed_when_cash_sufficient',
         scenario="Second-live-ticker-in-one-account BUY correctly ALLOWED when the account can "
                  "afford both reservations (non-addon path, the actual point of the 2026-08-07 fix)",
         code_path="schwab_safety.check_order's cash-check block",
         offline_coverage="test_fake_broker_check_order_guards_phase2_scenario.py: "
                           "test_second_ticker_buy_allowed_when_cash_covers_both, "
                           "test_third_ticker_buy_reserves_against_both_other_resting_orders "
                           "(3rd leg once cash is raised)",
         check_mechanism='coverage_events', scenario_key='second_ticker_buy_allowed',
         bad_results=[],
         notes="New 2026-08-07 -- replaces the unconditional block's cost (a real, fundable RETL trade "
               "was skipped today purely because LABD had a resting order, not because cash was "
               "actually short). Real live confirmation still open: needs a genuine day where 2+ "
               "soxl_ira tickers signal close together and the second one's BUY actually proceeds."),
    dict(id='addon_buying_power_check',
         scenario="Add-on-leg BUY's buying-power check (pass, insufficient, and unpriced-other-ticker "
                  "branches), including the 2026-08-10 fix folding a reservation for other tickers' "
                  "resting-order notional into this check",
         code_path="schwab_safety.check_order's is_addon_leg buying-power block "
                   "(schwab_client.get_leveraged_buying_power / get_current_price)",
         offline_coverage="test_fake_broker_check_order_guards_phase2_scenario.py: "
                           "test_addon_second_ticker_buy_allowed_when_buying_power_covers_both, "
                           "test_addon_second_ticker_buy_blocked_when_buying_power_cannot_cover_both, "
                           "test_addon_buy_blocked_unpriced_when_other_ticker_price_unavailable; "
                           "test_leveraged_buying_power.py (11 tests) covers get_leveraged_buying_power/"
                           "get_account_margin_requirement directly, incl. the min()-clamp and "
                           "inverse-fund (-leverage) cases",
         check_mechanism='coverage_events', scenario_key='addon_buying_power_check',
         bad_results=['blocked_unpriced', 'failed_closed'],
         notes="No row existed at all before 2026-08-10 -- verified directly against the live DB "
               "(zero coverage_events rows for any addon_% scenario_key as of this fix) that this "
               "was ALSO wired-never-fired, not just missing a registry row; a pre-existing "
               "gap surfaced while fixing the RETL/LABD-shaped 1-ticker-per-account bug in this "
               "path (docs/deep_backlog.md's 2026-08-09/10 entry, follow-up #3). "
               "Fixed 2026-08-12: buying_power now uses get_leveraged_buying_power, which returns "
               "min(equity/margin_req(ticker), Schwab's raw 'buyingPower' field). A first version "
               "without the clamp (raw leverage-aware term alone) was briefly wired in and found by "
               "paired Opus review to be live-reachable on soxl_ira (7 real addon_enabled nodes) "
               "with a materially overstated result -- a limited-margin IRA gets zero real leverage "
               "from Schwab (confirmed live: soxl_ira's real buyingPower == cashBalance exactly), "
               "but the unclamped formula assumed 2x anyway. The min()-clamp fixes this by "
               "construction: it can only ever tighten the raw figure (the original point -- raw "
               "'buyingPower' assumes a blanket 50% requirement, overstating real capacity for a "
               "genuine 3x fund like SOXL/HIBL) and can never loosen it beyond what Schwab's own "
               "real-time number already allows, since that number already nets out committed "
               "capital and correctly reflects zero leverage on a non-margin account. Verified live "
               "post-fix: soxl_ira returns its real $9,075.38, not the unclamped formula's inflated "
               "$20,073.76. Reservation-headroom asymmetry still open (follow-up #1 in the "
               "2026-08-09/10 entry): _reserved_other here is NOT scaled by "
               "ADDON_BUYING_POWER_HEADROOM_MULT the way the add-on's own notional is -- harmless "
               "only because buying_power==cash on every live account today (still true post-fix, "
               "by construction, for every account without genuine margin); see "
               "addon_buying_power_drift_check below, which watches for the moment that stops "
               "holding. failed_closed means the buying-power fetch itself failed and the order was "
               "refused -- the guard failing safe, not working normally, same reasoning as "
               "blocked_unpriced."),
    dict(id='addon_buying_power_drift_check',
         scenario="Daily daemon check: does buying_power still equal cash balance for every "
                  "account hosting a live addon_enabled node?",
         code_path="signals_notify.check_addon_buying_power_drift "
                   "(schwab_client.get_account_balance / get_account_buying_power)",
         offline_coverage="tests/test_addon_buying_power_drift.py (10 tests)",
         check_mechanism='coverage_events', scenario_key='addon_buying_power_drift_check',
         bad_results=['diverged', 'fetch_failed'],
         notes="New 2026-08-10, follow-up #1 of the RETL/LABD-shaped add-on buying-power fix "
               "(docs/deep_backlog.md's 2026-08-09/10 entry) -- addon_buying_power_check's other-"
               "ticker reservation is 1x, not scaled the way the add-on's own notional is, an "
               "asymmetry masked only by buying_power==cash holding true today. This check runs "
               "once/day PER ACCOUNT in the daemon (active_signals.py) and alerts unconditionally "
               "on a real divergence -- doesn't fix the asymmetry, just catches the moment its "
               "cover assumption breaks. Tracked per-account (not one global watermark) since a "
               "paired review (2026-08-10) caught the first version stamping the whole day done "
               "even on total fetch failure; fetch_failed (renamed from an inaccurate "
               "'failed_closed' the same review caught -- nothing fails closed here, the account "
               "is just skipped and retried next poll) and an unconfirmed diverged-alert post both "
               "leave that account unmarked so the next ~5min poll retries it. No live events yet "
               "(just wired in). 2026-08-12: addon_buying_power_check's real order-time source "
               "switched to get_leveraged_buying_power (min(equity/margin_req, raw buyingPower) -- "
               "see that row's notes for the full fix history). This drift check still compares the "
               "OLD raw get_account_buying_power against cashBalance -- by construction (the "
               "min()-clamp), the real order-check's number can never exceed this raw figure, so "
               "'diverged' here (buying_power != cash) still correctly flags exactly the moment a "
               "real margin account starts carrying real margin -- the premise holds, just via the "
               "clamp rather than coincidence."),
    dict(id='addon_second_ticker_buy_allowed',
         scenario="Add-on-leg BUY correctly ALLOWED alongside another ticker's resting order when "
                  "buying power covers both (the actual point of the 2026-08-10 fix, mirroring "
                  "the non-addon second_ticker_buy_allowed row)",
         code_path="schwab_safety.check_order's is_addon_leg buying-power block, same as "
                   "addon_buying_power_check",
         offline_coverage="test_fake_broker_check_order_guards_phase2_scenario.py::"
                           "test_addon_second_ticker_buy_allowed_when_buying_power_covers_both",
         check_mechanism='coverage_events', scenario_key='addon_second_ticker_buy_allowed',
         bad_results=[],
         notes="New 2026-08-10, follow-up #4 of the RETL/LABD-shaped add-on buying-power fix "
               "(docs/deep_backlog.md's 2026-08-09/10 entry) -- the add-on path previously logged "
               "no event when it let an order through alongside another ticker's resting order, "
               "asymmetric with the non-addon sibling path. No live events yet (just wired in).",
         not_prod_required_note="User's call, 2026-08-13: known edge case (needs the addon trigger to "
                                 "coincide with another ticker's resting order, a timing coincidence not "
                                 "organic trading), not planning to force-test it. Lowered out of the "
                                 "active red bucket, not deleted."),
    dict(id='daemon_exception_survival',
         scenario="Daemon survives an unhandled exception mid-loop",
         code_path="active_signals._guarded + outer try/except in run_loop",
         offline_coverage="7 unit tests (test_run_loop_fault_tolerance.py)",
         check_mechanism='coverage_events', scenario_key='daemon_section_exception',
         notes="360 real-looking rows were pytest pollution, cleaned up 2026-07-25 -- 0 real occurrences "
               "since. No test runs a real run_loop iteration end-to-end with a failing section.",
         not_prod_required_note="User's call, 2026-08-13: needs a genuine unhandled daemon-loop exception, "
                                 "a real fault no config can force regardless of live/canary/paper node "
                                 "type. Not something we're actively testing or planning to force. Lowered "
                                 "out of the active red bucket, not deleted."),
    dict(id='dup_order_no_false_block',
         scenario="Duplicate-order guard doesn't false-block a legitimate top-up",
         code_path="schwab_safety quantity-aware guard",
         offline_coverage="Unit tests (test_schwab_safety.py)",
         check_mechanism='coverage_events', scenario_key='dup_order_window_blocked',
         notes="Only exercised in unit tests so far, not against real order timing."),
    dict(id='buy_blocked_position_exists',
         scenario="A second real BUY for a ticker this account already holds is blocked, unless it's "
                  "the sanctioned post-fill top-up",
         code_path="schwab_safety.check_order (BUY branch, existing-position guard)",
         offline_coverage="tests/test_fake_broker_buy_blocked_position_exists_scenario.py (2026-08-02, "
                           "both directions: genuine 2nd BUY blocked, is_protective top-up still allowed)",
         check_mechanism='coverage_events', scenario_key='buy_blocked_position_exists',
         notes="Added 2026-08-02, closing the gap confirmed live 2026-07-24: two real resting "
               "TRAILING_STOP BUYs left get_account_balance completely unchanged, so notional_cap "
               "(per-order) and the cash check (reads that same undecremented balance) couldn't stop a "
               "second real BUY once the first had already filled (the resting-order dup guards only "
               "cover the window before a fill). Not yet observed live -- no real double-buy attempt "
               "has occurred against this guard."),
    dict(id='automated_sell_mode_skip',
         scenario="Automated sell correctly skipped for a non-live-mode node's position",
         code_path="signals_notify._attempt_automated_sell mode check",
         offline_coverage="2 unit tests (test_schwab_automation.py)",
         check_mechanism='coverage_events', scenario_key='automated_sell_mode_skip',
         notes="Instrumented 2026-07-28. No ticker has ever hit this scenario live (automation-scope "
               "tickers only run mode='live') -- expect wired-never-fired until that changes.",
         structural_note="Checked directly against the real code (2026-08-13): the log call fires only "
                          "for node.get('state') == 'paper' specifically, not any non-live state -- a "
                          "canary/dry_run node never trips it (falls through to the normal automation "
                          "path, gated only by node_automation_enabled). Requires a node that's actually "
                          "state='paper' while still carrying a real open_positions row and staying in "
                          "AUTOMATION_ENABLED_TICKERS -- a specific state-transition artifact (same shape "
                          "as UDOW's deliberately-seeded 2026-07-23 test position), not something more "
                          "canary volume or trading days can organically produce."),
    dict(id='live_state_reconciliation_mismatch',
         scenario="Live-state reconciliation detects and alerts on a real mismatch",
         code_path="signals_notify.check_live_state_reconciliation, schwab_client.get_real_position",
         offline_coverage="22 unit tests (test_live_state_reconciliation.py)",
         check_mechanism='coverage_events', scenario_key='reconciliation_mismatch',
         result_filter={'results': ['never_had_sl', 'sl_price_mismatch', 'sl_quantity_mismatch'],
                        'include': False},
         notes="8 real soxl_ira detections 2026-07-24 (GDXU/GDXD/LABU x2/ERY x4). 1,753 dry_run rows are "
               "the known intentionally-seeded UDOW fake position -- expected noise, not a gap. "
               "result_filter EXCLUDES the three Stage B/C branches added 2026-08-15 -- they share this "
               "scenario_key (_alert_reconcile_mismatch hardcodes it, passing the branch as `result`) but "
               "have their own row below; without the exclusion this row's 4,311 events for OTHER "
               "branches would render them as live-proven despite never having fired."),
    dict(id='sl_integrity_mismatch',
         scenario="Reconciliation catches a position that never had a stop-loss at all, or whose resting "
                  "stop is at the wrong price or covers the wrong share count",
         code_path="signals_notify.check_live_state_reconciliation (never_had_sl / sl_price_mismatch / "
                    "sl_quantity_mismatch branches, via _expected_sl_price/_match_resting_order/"
                    "_resting_order_quantity/_past_sl_grace)",
         offline_coverage="12 unit tests (test_live_state_reconciliation.py, Stage B/C section)",
         check_mechanism='coverage_events', scenario_key='reconciliation_mismatch',
         result_filter=['never_had_sl', 'sl_price_mismatch', 'sl_quantity_mismatch'],
         notes="Split out from live_state_reconciliation_mismatch 2026-08-15 (SOXS incident). Stage B: the "
               "pre-existing missing_sl branch is gated on sl_order_id ALREADY being truthy, so it "
               "structurally could not catch 'never had a stop at all' -- the literal SOXS condition. "
               "Stage C: has_sell_order checked neither price nor quantity, so a stop at the wrong level "
               "(the 2026-07-31 signal_price-anchor bug) or covering the wrong shares read as fully "
               "protected. Detection-only -- deliberately never auto-replaces, which would reintroduce "
               "the silent-override behavior being fixed elsewhere in the same session. Expected to stay "
               "wired-never-fired: every firing here is a real unprotected-position incident."),
    dict(id='trailing_arm_reread',
         scenario="Trailing-arm state survives notify_trailing_activated without re-arming next bar",
         code_path="signals_notify.notify_trailing_activated (re-reads via get_position_by_id)",
         offline_coverage="1 unit test (test_schwab_automation.py)",
         check_mechanism='coverage_events', scenario_key='trailing_arm_state_reread',
         notes="Confirmed live 2026-07-24 (SPY, live, trailing_preserved) -- the critical duplicate-sell "
               "fix held on a real account."),
    dict(id='dup_sell_order_blocked',
         scenario="Second live SELL order for the same ticker correctly blocked",
         code_path="schwab_safety._has_open_sell_order",
         offline_coverage="2 unit tests (test_schwab_safety.py)",
         check_mechanism='coverage_events', scenario_key='dup_sell_order_blocked',
         notes="Built 2026-07-22 as the structural fix preventing the trail_state bug from stacking "
               "two real exit orders.",
         not_prod_required_note="User's call, 2026-08-13: cannot be deterministically detuned/forced via "
                                 "node config -- needs a genuine duplicate-SELL race, not something we're "
                                 "actively testing or planning to force. Note is purely informational here "
                                 "since real evidence already exists (7x live, last 2026-07-31) -- "
                                 "compute_status only falls back to this note when there's no real "
                                 "evidence at all, so the Grid correctly keeps showing verified-live, "
                                 "tracking that it already happened organically."),
    dict(id='manual_sl_fallback_alert',
         scenario="Manual SL-price fallback alert fires correctly when trailing-sell placement fails "
                  "post-SL-cancel",
         code_path="signals_notify._attempt_automated_sell",
         offline_coverage="1 unit test (test_schwab_automation.py)",
         check_mechanism='coverage_events', scenario_key='manual_sl_fallback_alert',
         notes="Instrumented 2026-07-28. Deliberately no auto-recovery (user's call) -- needs a real "
               "failed placement to confirm the alert text/price are useful in practice.",
         not_prod_required_note="User's call, 2026-08-13: cannot be deterministically detuned/forced via "
                                 "node config -- needs a genuine broker placement failure, pure luck, not "
                                 "something we're actively testing or planning to force. Note is purely "
                                 "informational here since real evidence already exists (8x live, last "
                                 "2026-08-07) -- compute_status only falls back to this note when there's "
                                 "no real evidence at all, so the Grid correctly keeps showing "
                                 "verified-live, tracking that it already happened organically."),
    dict(id='position_lock',
         scenario="Poll loop and Slack handler can't double-open/double-close the same position",
         code_path="signals_db._position_lock around open_position/close_position",
         offline_coverage="3 unit tests (test_db_roundtrip.py); tests/test_fake_broker_position_lock_scenario.py "
                           "(2026-08-01, concurrency test)",
         check_mechanism='coverage_events', scenario_key='position_lock',
         # 2026-08-01 2nd Opus review finding: excluding only 'already_closed' was
         # incomplete -- 'acquired' fires unconditionally on EVERY open_position call
         # (no contention required at all), so the very first real live position open
         # flipped this branch verified-live off zero evidence of contention. 'closed'
         # has the same problem, plus fires before the close actually completes (see
         # the 'closed'-ordering note below) -- 'skipped_duplicate' is the only result
         # that's actual evidence the lock did something under real contention.
         bad_results=['already_closed', 'acquired', 'closed'],
         notes="Instrumented 2026-08-01 -- log_coverage_event calls added inside the already-locked "
               "block in open_position (acquired/skipped_duplicate) and close_position "
               "(closed/already_closed), observational only, doesn't change the lock's own acquire "
               "semantics (the 2026-07-28 note's stated reason for deferring this). This proves the "
               "locked block executes on every call -- it does NOT by itself prove real concurrent "
               "contention was exercised (a single uncontended open/close looks identical in these "
               "events to one that raced); the dedicated concurrency test above is what actually "
               "proves serialization. 'already_closed' is excluded via bad_results since it's a "
               "benign no-op (a routine double-call finding nothing to close) with zero lock "
               "contention, not evidence the lock protected anything (Opus review, 2026-08-01). "
               "Deprioritized 2026-08-13 (user's call): the only real contention shape this guards "
               "against is the poll loop and a Slack button click racing the same position, and the "
               "user's actual interaction pattern is kill-switch-only (rarely/never uses per-position "
               "Slack buttons for routine open/close) -- so this race is unlikely to occur in practice "
               "regardless of proof status. Still real, still worth fixing if it's ever cheap to do, "
               "just not worth flagging in routine coverage reviews going forward.",
         demote_on_all_bad=True,
         not_prod_required_note="User's call, 2026-08-13 (see notes): only 'skipped_duplicate' would "
                                 "be real evidence of the lock resolving genuine contention, and that "
                                 "requires the poll loop and a Slack button racing the same position -- "
                                 "unlikely given kill-switch-only usage. Fires constantly (acquired/"
                                 "closed on every normal call) but can structurally never look 'good' "
                                 "under that bar, so it's demoted rather than left permanently red."),
    dict(id='dup_order_retry_after_failure',
         scenario="Duplicate-order retry after a real rejected/failed order isn't wrongly blocked",
         code_path="schwab_safety._broker_confirms_order",
         offline_coverage="2 unit tests (test_schwab_safety.py)",
         check_mechanism='coverage_events', scenario_key='dup_order_retry_after_failure',
         notes="Needs a real rejected order to confirm the retry path in practice."),
    dict(id='order_retry_duplicate_prevented',
         scenario="A repeated flapping connection mid-order-placement (up-down-up, not just a single "
                  "clean drop) doesn't stack multiple real orders -- the retry loop re-checks live "
                  "broker state before each retry attempt (N>1) instead of resubmitting blind",
         code_path="schwab_client._submit_order_with_retry / _submit_replace_with_retry "
                    "(_find_recent_matching_order / _check_broker_before_retry)",
         offline_coverage="2 fake_broker tests (test_fake_broker_retry_flapping_scenario.py): a "
                           "drop-then-landed-but-lost-response sequence (asserts exactly 1 real order "
                           "reaches the broker, not 2) and an ambiguous-multi-match sequence (asserts "
                           "the loop raises _AmbiguousBrokerState and fails safe rather than guessing).",
         check_mechanism='coverage_events', scenario_key='order_retry_duplicate_prevented',
         bad_results=['ambiguous', 'check_failed'],
         notes="Added 2026-08-15 (see docs/backlog_cache.md's now-resolved 'retry blind, no "
               "broker-state re-check between attempts' item). 'prevented' = a genuine duplicate was "
               "caught and skipped, the scenario working as designed; 'ambiguous' and 'check_failed' "
               "(both in bad_results, same bad-results-vs-good-results conflation this registry exists "
               "to catch) mean the fail-safe/fall-back-to-blind-retry paths fired instead -- real "
               "evidence the guard is alert or that a broker read failed, but not evidence the happy "
               "'skip the duplicate' path itself has fired live. No live/dry_run proof yet -- needs a "
               "genuine repeated-flap incident (rare, connection-quality-dependent) to confirm outside "
               "the fake-venue harness; the residual check-then-act race documented in "
               "_submit_replace_with_retry's docstring (this loop's own broker-state read completing a "
               "moment before a delayed real acceptance shows up -- narrower than the original "
               "2026-07-27 single-clean-drop gap, which this fix closes) has no live proof either and "
               "isn't expected to for now.",
         not_prod_required_note="No forced live test planned -- flapping is connection-quality-"
                                 "dependent and not something to manufacture against a real order "
                                 "(same organic-wait posture as post_fill_topup/market_buy_placement "
                                 "in CLAUDE.md's go-live checklist). Fake-venue coverage is considered "
                                 "sufficient for now per the project's 3-bucket testing philosophy."),
    dict(id='fast_path_fill_reconciliation',
         scenario="Fast-path (websocket) fill reconciliation doesn't act on a partial/in-flight execution",
         code_path="signals_notify.drain_fill_queue (re-confirms via get_filled_order poll)",
         offline_coverage="5 unit tests (test_part3_gap_resize.py)",
         check_mechanism='coverage_events', scenario_key='fast_path_fill_reconciliation',
         bad_results=['outside_automation_scope', 'auto_fill_detection_disabled',
                      'stream_event_not_yet_confirmed_filled', 'account_number_unresolved'],
         notes="Needs a real multi-execution fill to confirm the poll-reconfirm path in practice. "
               "bad_results added 2026-07-31 (review finding): auto_fill_detection is off by default, "
               "so outside_automation_scope/auto_fill_detection_disabled will be the COMMON real "
               "result once this scenario starts firing live -- without listing them, compute_status "
               "would render this row verified-live off events that only prove the opt-in gate fired, "
               "not that the real poll-reconfirm path ran (the same sl_placement/top_up blocked-vs-"
               "succeeded conflation this registry was built to catch).",
         not_prod_required_note="User's call, 2026-08-13: known edge case (needs a genuine multi-"
                                 "execution partial fill from the real broker, pure luck -- could "
                                 "realistically go a year without a hit), not planning to force-test it. "
                                 "Lowered out of the active red bucket, not deleted."),
    dict(id='same_day_block',
         scenario="same_day_block skips correctly for margin accounts unless a node opts in via "
                  "watch_list.force_same_day_block, still blocks cash accounts unconditionally",
         code_path="schwab_safety.check_order (AccountLimits.cash_settlement_type, watch_list.force_same_day_block)",
         offline_coverage="3 unit/fake_broker tests (test_schwab_safety.py, "
                           "test_fake_broker_check_order_guards_phase2_scenario.py)",
         check_mechanism='coverage_events', scenario_key='same_day_block',
         notes="No real same-day re-buy has ever been attempted live in either account type. "
               "force_same_day_block (2026-08-11, per-node opt-in) is unset on every real node "
               "today -- built but not yet turned on anywhere."),
    dict(id='manual_buy_confirmation_account',
         scenario="Manual BUY confirmation (Executed/Filled/Manual Open) opens a position with the real "
                  "account, not NULL",
         code_path="signals_blocks._build_buy_blocks, "
                    "signals_handlers.handle_entry_price/handle_trail_buy_fill_price/handle_manual_open_price",
         offline_coverage="tests/test_coverage_check.py covers the node-identity plumbing, nothing exercises "
                           "the actual button->modal->handler chain end-to-end",
         check_mechanism='coverage_events', scenario_key='manual_buy_confirmation_account',
         bad_results=['no_account'],
         notes="Instrumented 2026-07-28 at all 3 confirmation handlers. Real bug found+fixed 2026-07-25 "
               "(both Slack button payloads omitted account/id). Not yet observed against a real Slack "
               "button click. 2026-08-14: signals_notify._ticker_block (relocated to signals_blocks 2026-08-15) dropped from code_path -- the "
               "'Manually Open' button it rendered was replaced by the node-scoped Stop/Start "
               "automation button; handle_manual_open_price is still reachable from old reports in "
               "Slack scrollback, so the handler (and this row) stay.",
         structural_note="Checked directly against the real code (2026-08-13): this fires only inside "
                          "handle_entry_price/handle_trail_buy_fill_price, reached exclusively by a human "
                          "tapping a real Slack Executed/Filled button. Canary/dry_run nodes never reach "
                          "this code path at all -- their fills are synthesized programmatically by "
                          "update_dry_run_buys (called every poll), which never renders a button. Widening "
                          "canary surface can't help; only a genuine state='live' manual confirmation can. "
                          "Given the project's stated direction of moving away from manual confirmation "
                          "toward automation, this surface may keep shrinking rather than growing.",
         not_prod_required_note="User's call, 2026-08-13: the only way to organically close this is being "
                                 "at the keyboard at the exact moment a real signal fires and manually "
                                 "tapping the button -- not something to plan around during work hours. Not "
                                 "deleted, not deprioritized as unimportant -- lowered out of the active "
                                 "red bucket until entry confirmation is fully automated (removing the "
                                 "manual click this row depends on entirely) or an organic click happens "
                                 "to occur. Revisit if/when automation coverage widens."),
    dict(id='stale_buy_button_guard',
         scenario="Stale/duplicate Executed or Filled button tap doesn't open a phantom position",
         code_path="signals_handlers.handle_entry_price/handle_trail_buy_fill_price (pending_buys-existence guard)",
         offline_coverage="None yet -- guard logic not directly unit-tested",
         check_mechanism='coverage_events', scenario_key='stale_buy_button_guard',
         notes="Instrumented 2026-07-28. Known latent gap: assumes every rendered Executed button has a "
               "backing pending_buys row, which breaks for a live TrailingExit ticker outside "
               "SCHWAB_AUTOMATION_TICKERS.",
         structural_note="Same code-path constraint as manual_buy_confirmation_account, checked "
                          "2026-08-13: only reachable via a real human Slack button tap on a state='live' "
                          "node -- canary/dry_run fills bypass this handler chain entirely via "
                          "update_dry_run_buys. Needs a genuine stale/duplicate click on a real "
                          "confirmation, not more trading volume.",
         not_prod_required_note="Same as manual_buy_confirmation_account -- user's call, 2026-08-13. Not "
                                 "something to plan around during work hours; lowered out of the active "
                                 "red bucket, not deleted."),
    dict(id='two_nodes_same_ticker_diff_accounts',
         scenario="Two concurrent live nodes on the same ticker in different accounts can both place real orders",
         code_path="schwab_safety._live_ticker_accounts/check_order (ticker->set-of-accounts)",
         offline_coverage="Unit tests updated for the new error message, no test exercises 2 real concurrent nodes",
         check_mechanism='coverage_events', scenario_key='two_nodes_same_ticker_diff_accounts',
         notes="Instrumented 2026-07-28, fires only when 2+ accounts are genuinely registered for the same "
               "live ticker. Built as part of the wl_id refactor (2026-07-25/26) -- never yet exercised "
               "with a real second node on an already-live ticker."),
    dict(id='buy_buttons_resolve_correct_node',
         scenario="BUY-side Slack buttons resolve the correct node when 2+ nodes share a ticker",
         code_path="signals_handlers.py (6 BUY handlers match/clear pending_buys by wl_id)",
         offline_coverage="None -- no test simulates 2 concurrent pending_buys rows for the same ticker",
         check_mechanism='coverage_events', scenario_key='buy_buttons_resolve_correct_node',
         notes="Instrumented 2026-07-28 in handle_entry_price/handle_trail_buy_fill_price, fires only when "
               "2+ pending buys exist for the ticker at click time. SELL-side already did this correctly "
               "via position_id.",
         structural_note="Confirmed via batch canary-forceability review, 2026-08-13: same handler-chain "
                          "constraint as manual_buy_confirmation_account -- only reachable via a real human "
                          "Slack button tap; canary/dry_run fills bypass this entirely via update_dry_run_buys.",
         not_prod_required_note="Same as manual_buy_confirmation_account -- user's call, 2026-08-13. Not "
                                 "something to plan around during work hours; lowered out of the active "
                                 "red bucket, not deleted."),
    dict(id='buy_fill_reconciles_correct_node',
         scenario="A real broker BUY fill reconciles against the correct node's pending_buys row when 2+ "
                  "are pending for the same ticker",
         code_path="signals_notify._reconcile_buy_fill (wl_id param, falls back to alert if ambiguous)",
         offline_coverage="tests/test_fake_broker_buy_button_handlers_scenario.py::"
                           "test_buy_fill_reconciles_correct_node_with_multiple_pending (2 concurrent "
                           "pending buys, same ticker, same account, calls _reconcile_buy_fill directly); "
                           "plus scripts/fake_venue_harness.py (2026-08-16), which reaches the same event "
                           "end-to-end through real check_auto_fills -> get_filled_order against a fake "
                           "broker, with 3 pendings (two of them same-account). Corrected 2026-08-16 -- "
                           "this field read 'None -- no test simulates 2 concurrent real pending buys' "
                           "and was already stale before the harness existed (hand-typed field; "
                           "offline_proof_for() is the derived one).",
         check_mechanism='coverage_events', scenario_key='buy_fill_reconciles_correct_node',
         bad_results=['no_match'],
         notes="Instrumented 2026-07-28, fires only when 2+ pendings exist for the ticker at fill time. "
               "drain_fill_queue's stream entry point passes its best-effort ticker+account-derived node id.",
         not_prod_required_note="User's call, 2026-08-13: known edge case (needs 2 real pending buys on "
                                 "the same ticker to coincide, plus a real broker fill -- not organic "
                                 "trading), not planning to force-test it. Lowered out of the active red "
                                 "bucket, not deleted."),
    dict(id='node_level_automation_pause',
         scenario="Node-level automation pause blocks real orders for just that node, not sibling nodes "
                  "on the same ticker",
         code_path="schwab_safety.pause_node_automation, node_automation_enabled",
         offline_coverage="tests/test_schwab_safety.py: test_node_id_resolves_ambiguous_sibling_same_account; "
                           "tests/test_fake_broker_check_order_guards_scenario.py: "
                           "test_node_id_disambiguates_same_ticker_account_siblings",
         check_mechanism='coverage_events', scenario_key='node_level_automation_pause',
         notes="Instrumented 2026-07-28 (blocked branch only) -- this row proves the ORDER-BLOCKING "
               "branch (check_order raising SafetyViolation), which has still never fired live. "
               "2026-08-14: a Slack button that TRIGGERS a pause now exists (per-row 🛑 Stop/▶️ Start, "
               "see the node_automation_pause_button row) -- deliberately logged under its own "
               "scenario_key, because a tap of that button is not evidence this guard blocks anything. "
               "Fuzzy-node-lookup fail-open for 2 nodes sharing both ticker AND "
               "account was a known limitation until 2026-08-10 -- fixed via node_id threaded through "
               "schwab_client's 8 order-placement functions -> approve_and_record -> check_order; every "
               "real production call site now passes node_id, so the fuzzy ticker+account lookup is only "
               "a fallback for a caller that doesn't (none remain). See docs/deep_backlog.md's 2026-08-10 "
               "entry."),
    dict(id='unknown_account_block',
         scenario="check_order blocks a real order attempt for an account not in the accounts allowlist",
         code_path="schwab_safety.check_order (ACCOUNTS.get(account) is None)",
         offline_coverage="None yet",
         check_mechanism='coverage_events', scenario_key='unknown_account_block',
         notes="Instrumented 2026-08-13 -- found by paired review of the same-night Slack "
               "noise-reduction gate: this SafetyViolation reason (plus the 5 below) previously "
               "raised with no coverage_event logged at all, so a blocked order for any of them "
               "had zero record anywhere once the gate's Slack suppression also applied to a "
               "sub-capital-at-stake node. 'blocked' is the correct/good outcome (no bad_results)."),
    dict(id='account_disabled_block',
         scenario="check_order blocks a real order attempt for an account explicitly disabled in the allowlist",
         code_path="schwab_safety.check_order (not limits.enabled)",
         offline_coverage="None yet",
         check_mechanism='coverage_events', scenario_key='account_disabled_block',
         notes="Instrumented 2026-08-13, same pass as unknown_account_block above. 'blocked' is "
               "the correct/good outcome (no bad_results)."),
    dict(id='ticker_not_live_mode_block',
         scenario="check_order blocks a real order for a ticker no longer 'live' by the time the "
                  "safety gate runs, even though an earlier step in the same poll cycle read it as live",
         code_path="schwab_safety.check_order (_live_ticker_accounts(), queried fresh each call)",
         offline_coverage="None yet",
         check_mechanism='coverage_events', scenario_key='ticker_not_live_mode_block',
         notes="Instrumented 2026-08-13, same pass as unknown_account_block above. Real trigger is "
               "a config/state race (a node demoted mid-poll-cycle), not routine -- genuinely "
               "anomalous, unlike the automation-pause rows below which are intentional. 'blocked' "
               "is the correct/good outcome (no bad_results)."),
    dict(id='ticker_account_assignment_mismatch',
         scenario="check_order blocks a real order whose (ticker, account) pair doesn't match any "
                  "live-mode watch_list assignment",
         code_path="schwab_safety.check_order (account not in ticker_accounts[ticker])",
         offline_coverage="None yet",
         check_mechanism='coverage_events', scenario_key='ticker_account_assignment_mismatch',
         notes="Instrumented 2026-08-13, same pass as unknown_account_block above. 'blocked' is "
               "the correct/good outcome (no bad_results)."),
    dict(id='ticker_not_in_automation_scope_block',
         scenario="check_order blocks a real order for a ticker not in AUTOMATION_ENABLED_TICKERS "
                  "(still manual-only)",
         code_path="schwab_safety.check_order (ticker not in AUTOMATION_ENABLED_TICKERS)",
         offline_coverage="None yet",
         check_mechanism='coverage_events', scenario_key='ticker_not_in_automation_scope_block',
         notes="Instrumented 2026-08-13, same pass as unknown_account_block above. 'blocked' is "
               "the correct/good outcome (no bad_results)."),
    dict(id='ticker_level_automation_pause',
         scenario="check_order blocks a real order via the per-ticker automation toggle (distinct "
                  "from node_level_automation_pause below, which is per-node)",
         code_path="schwab_safety.check_order (not ticker_automation_enabled(ticker))",
         offline_coverage="None yet",
         check_mechanism='coverage_events', scenario_key='ticker_level_automation_pause',
         notes="Instrumented 2026-08-13, same pass as unknown_account_block above. 'blocked' is "
               "the correct/good outcome (no bad_results)."),
    dict(id='buy_trading_day_block',
         scenario="check_order blocks a real BUY attempted on a non-NYSE-trading day",
         code_path="schwab_safety.check_order (_is_trading_day)",
         offline_coverage="None yet",
         check_mechanism='coverage_events', scenario_key='buy_trading_day_block',
         notes="Instrumented 2026-08-13, same paired-review pass as the 6 SafetyViolation "
               "reasons above. 'blocked' is the correct/good outcome (no bad_results)."),
    dict(id='buy_signal_window_block',
         scenario="check_order blocks a real BUY attempted outside the signal/open-check windows",
         code_path="schwab_safety.check_order (_SIGNAL_WINDOWS + _OPEN_CHECK_WINDOWS)",
         offline_coverage="None yet",
         check_mechanism='coverage_events', scenario_key='buy_signal_window_block',
         notes="Instrumented 2026-08-13, same pass as buy_trading_day_block above. 'blocked' is "
               "the correct/good outcome (no bad_results)."),
    dict(id='hard_order_ceiling_block',
         scenario="check_order blocks a real order whose notional exceeds the absolute HARD_ORDER_CEILING",
         code_path="schwab_safety.check_order (HARD_ORDER_CEILING)",
         offline_coverage="None yet",
         check_mechanism='coverage_events', scenario_key='hard_order_ceiling_block',
         notes="Instrumented 2026-08-13, same pass as buy_trading_day_block above. 'blocked' is "
               "the correct/good outcome (no bad_results)."),
    dict(id='notional_cap_block',
         scenario="check_order blocks a real BUY whose notional exceeds the account's own notional_cap",
         code_path="schwab_safety.check_order (AccountLimits.notional_cap)",
         offline_coverage="None yet",
         check_mechanism='coverage_events', scenario_key='notional_cap_block',
         notes="Instrumented 2026-08-13, same pass as buy_trading_day_block above. 'blocked' is "
               "the correct/good outcome (no bad_results)."),
    dict(id='daily_order_cap_block',
         scenario="check_order blocks a real BUY once an account has hit its daily order cap "
                  "(non-protective only -- a protective top-up bypasses this, logged separately "
                  "as daily_cap_protective_bypass)",
         code_path="schwab_safety.check_order (AccountLimits.daily_order_cap)",
         offline_coverage="None yet",
         check_mechanism='coverage_events', scenario_key='daily_order_cap_block',
         notes="Instrumented 2026-08-13, same pass as buy_trading_day_block above. 'blocked' is "
               "the correct/good outcome (no bad_results)."),
    dict(id='global_burst_cap_block',
         scenario="check_order blocks a real order once GLOBAL_ORDERS_PER_MINUTE (all-accounts) is hit",
         code_path="schwab_safety.check_order (GLOBAL_ORDERS_PER_MINUTE)",
         offline_coverage="None yet",
         check_mechanism='coverage_events', scenario_key='global_burst_cap_block',
         notes="Instrumented 2026-08-13, same pass as buy_trading_day_block above. Real capacity "
               "concern flagged the same session: ~25 hair-trigger canary nodes now share this "
               "12/min cap with real-money orders (see docs/backlog_cache.md). 'blocked' is the "
               "correct/good outcome (no bad_results)."),
    dict(id='node_automation_pause_button',
         scenario="A human taps the reference report's per-row 🛑 Stop / ▶️ Start button and the node's "
                  "automation flag really flips (node-scoped, siblings on the same ticker unaffected)",
         code_path="signals_blocks._ticker_block (render), "
                    "signals_handlers.handle_stop_node_automation/handle_start_node_automation",
         offline_coverage="tests/test_bulk_auto_fill_and_report_controls.py: "
                           "test_stop_handler_really_pauses_the_node/test_start_handler_really_resumes_the_node/"
                           "test_stop_handler_scope_is_one_node_not_the_ticker/"
                           "test_start_handler_does_not_claim_success_while_still_blocked/"
                           "test_stop_start_handlers_log_a_coverage_event",
         check_mechanism='coverage_events', scenario_key='node_automation_pause_button',
         notes="Added 2026-08-14 with the button itself. Deliberately SEPARATE from "
               "node_level_automation_pause: that row proves check_order actually blocks a real order "
               "for a paused node, and a button tap is not evidence of that -- sharing one key would "
               "have flipped that guard to verified-live on the first tap and inflated the readiness "
               "headline (caught by paired Opus review before it shipped). Render is gated to "
               "state=='live' rows, so paper/canary rows never show the button; paper_trading honors "
               "the same flag on all three of its entry paths independently (start_paper_buy, "
               "check_paper_drought_entry, and check_paper_addon_trigger -- the third one found "
               "missing this gate by the same 2026-08-16 paired review that added this row's own "
               "confirmation pass, since it opens NEW simulated exposure just like the other two)."),
    dict(id='oversell_guard_correct_position',
         scenario="check_order's oversell guard resolves the right position when 2 live nodes share a "
                  "ticker in different accounts",
         code_path="signals_db.get_open_position_for_account",
         offline_coverage="None yet",
         check_mechanism='coverage_events', scenario_key='sell_exceeds_position_blocked',
         notes="Free row 2026-07-28: schwab_safety.py already logs sell_exceeds_position_blocked (built "
               "2026-07-24 alongside the oversell fix) -- it just never had a registry row. 'blocked' is "
               "the correct/good outcome (no bad_results). Found by 2nd Opus review round 2026-07-26 "
               "(ticker-only lookup could resolve wrong account's position). Real instrumentation gap "
               "found and fixed 2026-08-13 (user pushback: a negative test case is real proof too, the "
               "actual gap was that the POSITIVE case -- position resolved correctly, SELL proceeds -- "
               "was never logged at all, only the 2 failure branches were): schwab_safety.py now logs "
               "result='resolved' on the success path (check_order, right after the "
               "quantity-vs-position-shares bound passes). This fires on every normal successful SELL "
               "guard pass, canary or live -- should close organically very soon, no longer a special "
               "case needing artificial DB corruption to exercise."),
    dict(id='dry_run_buy_synthesis',
         scenario="A dry_run=True account's trailing-buy/market-buy order synthesizes a real fill "
                  "(bounce-fill or immediate) since no real broker fill event will ever arrive",
         code_path="signals_notify.update_dry_run_buys/_fill_dry_run_buy",
         offline_coverage="5 unit tests (test_dry_run_sim.py); live_sim_harness.py::scenario_dry_run_sim_cycle",
         check_mechanism='coverage_events', scenario_key='entry_fill', mode_filter='dry_run',
         notes="Built 2026-07-26, fixes the canary/dry_run 'no closed trade found' false-positive. Writes "
               "to open_positions/trade_log tagged is_dry_run_sim=1. NOTE: entry_fill is also logged by "
               "paper_trading.py under mode='paper' -- mode_filter isolates this row's own dry_run path."),
    dict(id='dry_run_sim_close',
         scenario="A synthesized dry-run-sim position closes immediately on exit signal instead of "
                  "waiting on a Slack button that will never be tapped",
         code_path="signals_notify.check_dry_run_sim_sells",
         offline_coverage="1 unit test (test_dry_run_sim.py); live_sim_harness.py::scenario_dry_run_sim_cycle",
         check_mechanism='coverage_events', scenario_key='exit_fill', mode_filter='dry_run',
         notes="Opus review found _scan_pinned_exit_arm initially missed the skip guard -- fixed, "
               "regression-tested. NOTE: exit_fill is also logged by paper_trading.py under mode='paper' "
               "-- mode_filter isolates this row's own dry_run path."),
    # --- Added 2026-07-27 evening: execution-logic gaps, not just guard logic ---
    # (raised directly by the user reviewing this grid -- the registry was
    # guard-heavy and thin on "did the actual trade-flow step execute
    # correctly." First 2 rows needed no new instrumentation -- paper_trading.py
    # already logs entry_fill/exit_fill under mode='paper', just never got a
    # registry row surfacing it. The other 5 needed a real log_coverage_event
    # call added at a previously-uninstrumented site.)
    dict(id='paper_entry_fill',
         scenario="Paper-trading bounce-fill/market-fill simulation opens a position",
         code_path="paper_trading.start_paper_market_buy, paper_trading.update_paper_buys",
         offline_coverage="Exercised implicitly by any paper-mode ticker running live; no dedicated unit test",
         check_mechanism='coverage_events', scenario_key='entry_fill', mode_filter='paper',
         notes="Already instrumented (2026-07-18 build) -- just never had a REGISTRY row. This is the "
               "only live validation for all 10 real v5 watchlist tickers today (mode='research')."),
    dict(id='paper_exit_fill',
         scenario="Paper-trading simulated position closes on a real exit signal (SL/TP/TIME/TRAIL)",
         code_path="paper_trading.check_paper_sells",
         offline_coverage="Exercised implicitly by any paper-mode ticker running live; no dedicated unit test",
         check_mechanism='coverage_events', scenario_key='exit_fill', mode_filter='paper',
         notes="Already instrumented (2026-07-18 build) -- just never had a REGISTRY row."),
    dict(id='kill_switch_block',
         scenario="Global kill switch actually blocks a real order attempt when engaged",
         code_path="schwab_safety.check_order (top of function, before account/limits resolved)",
         offline_coverage="No dedicated unit test found for this exact branch",
         check_mechanism='coverage_events', scenario_key='kill_switch_block',
         notes="New instrumentation, 2026-07-27 evening. 'blocked' is the correct/good outcome for this "
               "guard (no bad_results)."),
    dict(id='automated_sell_execution',
         scenario="Automated trailing-sell actually places a real broker order (not just correctly "
                  "blocked/deferred)",
         code_path="signals_notify._attempt_automated_sell",
         offline_coverage="test_schwab_automation.py exercises the function; no assertion on the new "
                           "coverage event yet",
         check_mechanism='coverage_events', scenario_key='automated_sell_execution',
         bad_results=['cancel_failed', 'cancel_unconfirmed', 'failed_unexpectedly'],
         notes="New instrumentation, 2026-07-27 evening -- previously zero coverage_events hook existed "
               "for whether this function's real order placement (as opposed to its guard logic) ever "
               "succeeds. 'blocked' (a SafetyViolation) is left out of bad_results deliberately -- it's "
               "the correct outcome when a real guard fires, not a failure of this scenario."),
    dict(id='automated_exit_execution',
         scenario="Automated exit (TP/SL/TIME, or a hold-time-forced TRAIL) atomically replaces the "
                  "real resting protective order with a market SELL and actually places it (distinct "
                  "from automated_sell_execution above, which is the ARM-time trailing-sell placement "
                  "-- this is the later market-replace that fires once the exit condition is computed)",
         code_path="signals_notify._attempt_automated_exit_sell",
         offline_coverage="No dedicated fake_broker test found for this specific function's "
                          "coverage_events wiring -- added 2026-08-14 (Opus audit found real live "
                          "data with zero Grid row at all).",
         check_mechanism='coverage_events', scenario_key='automated_exit_execution',
         bad_results=['failed_unexpectedly'],
         notes="Added 2026-08-14 -- this scenario_key had 14 real live events (incl. 6 'blocked', 2 "
               "'failed_unexpectedly') with no corresponding REGISTRY row at all, found by an Opus "
               "audit of this file's own history/completeness. 'blocked' (a SafetyViolation) excluded "
               "from bad_results deliberately, same reasoning as automated_sell_execution's sibling "
               "exclusion -- a real guard firing correctly is not a failure of this scenario. A failed "
               "replace also triggers manual_sl_fallback_alert (its own Grid row) when a resting order "
               "existed to fall back from."),
    dict(id='automated_buy_execution',
         scenario="Automated entry (trailing-buy or market-buy) actually places a real broker order "
                  "-- distinct from market_buy_placement above, which tracks the canary-scenario "
                  "lifecycle proof for the same underlying function; this row is the narrower "
                  "raw-event question of whether the order-placement call itself (incl. its "
                  "shares_too_small/exception handling) gets exercised for real",
         code_path="signals_notify._attempt_automated_buy, _attempt_automated_market_buy",
         offline_coverage="No dedicated fake_broker test found for this specific coverage_events "
                          "wiring -- added 2026-08-14 (Opus audit found real live data with zero "
                          "Grid row at all).",
         check_mechanism='coverage_events', scenario_key='automated_buy_execution',
         bad_results=['shares_too_small'],
         notes="Added 2026-08-14 -- found by an Opus audit of this file's own history/completeness. "
               "Real caveat, not fixed here: neither function logs a coverage_event on a SUCCESSFUL "
               "placement at all (only the shares_too_small guard logs anything under this "
               "scenario_key) -- unlike automated_exit_execution's sibling, which logs 'placed' on "
               "success. That means this row can structurally never show verified-live from this "
               "code alone, even given a real successful automated entry, until a 'placed' event is "
               "added to the success path -- flagged as its own follow-up, not done tonight."),
    dict(id='price_discontinuity_ruled_out',
         scenario="A price ratio matching a known split factor is checked against a REAL confirmed "
                  "split (yfinance) and genuinely ruled out -- SL/TP/TIME checks proceed normally "
                  "instead of freezing, closing the NUGT false-positive (an ordinary ~46% rally "
                  "coincidentally matched a 1/1.5 split ratio and froze exit protection at the "
                  "position's peak)",
         code_path="signals_compute.check_sell_condition (confirmed is False branch); "
                    "signals_helpers.real_split_confirmed_since",
         offline_coverage="tests/test_corporate_action_detection.py::"
                          "test_check_sell_condition_does_not_freeze_when_no_real_split_confirmed",
         check_mechanism='coverage_events', scenario_key='price_discontinuity_ruled_out',
         bad_results=[],
         notes="Added 2026-08-14 alongside the fix itself (paired Opus review of the same session's "
               "diff caught this row missing entirely -- the exact 'real code, zero Grid row' gap "
               "this same night's earlier audit found and fixed elsewhere). Logged at most once per "
               "(ticker, day) to avoid coverage_events spam -- the ratio can hold for the whole "
               "duration of a real rally, and this branch is reached every poll while it does."),
    dict(id='time_exit_trigger_unarmed',
         scenario="A real (non-paper/dry_run-sim) position's TIME-based exit (max_hold_hours) fires "
                  "the SELL alert while the position was NEVER armed (plain hold-time expiry, exits "
                  "via the same path as a normal TP/SL/TIME market sell)",
         code_path="signals_notify.notify_sell_signal (reason=='TIME' branch, exit_forced_by_hold_time "
                    "False/absent)",
         offline_coverage="signals_compute.check_sell_condition has kernel-parity coverage via "
                           "kernel_fill_parity; the live alert-firing side was untested",
         check_mechanism='coverage_events', scenario_key='time_exit_trigger_unarmed',
         notes="Split 2026-08-13 from the former single 'time_exit_trigger' row -- found while "
               "mapping designated testers that RETL (time_exit_via_sl role) and SH (time_exit_via_trail "
               "role) exercise two genuinely different code paths that were both silently reported under "
               "one scenario_key, masking that the armed sub-case (see the sibling row below) had zero "
               "live confirmation under current code. New instrumentation, 2026-07-27 evening. Only "
               "fires the alert -- doesn't confirm the position actually gets closed (that's a manual "
               "Slack-button step for a real position, same gap as manual_buy_confirmation_account)."),
    dict(id='time_exit_trigger_armed',
         scenario="A real (non-paper/dry_run-sim) position's TIME-based exit fires while the position "
                  "WAS armed (trailing=True) -- hold-time expired mid-trail, forcing the resting "
                  "trailing-sell order to be replaced with a market sell instead of a plain exit",
         code_path="signals_notify.notify_sell_signal (reason=='TIME' branch) + "
                    "_attempt_automated_exit_sell's exit_forced_by_hold_time force-replace branch",
         offline_coverage="signals_compute.check_sell_condition has kernel-parity coverage via "
                           "kernel_fill_parity; the live alert-firing side was untested",
         check_mechanism='coverage_events', scenario_key='time_exit_trigger_armed',
         notes="Split 2026-08-13, see the sibling row above. This is the historically-buggy sub-case "
               "(SH, 2026-07-29: stuck for hours on a 50%-wide trail order that was never going to fire "
               "before the hold-time deadline). Of the 7 pre-split coverage_events rows sharing the old "
               "combined scenario_key, 5 were SH itself (2026-07-27/28, one continuous position id=18, "
               "re-alerting hourly BEFORE any automated TP/SL/TIME exit path existed at all -- evidence "
               "of the original bug firing, not a working exit, correctly left unmigrated) and SH's only "
               "real closed trade (2026-07-31) predates the 2026-08-01 exit_reason labeling fix and is "
               "stored exit_reason='TRAIL', not 'TIME' -- also not valid proof. **However**, GDXU's "
               "2026-08-07 event (position_id=71) checked directly against coverage_events and IS "
               "genuine post-both-fixes proof: a trailing_arm_state_reread/automated_sell_execution "
               "event fired before the TIME exit, confirming the position was armed when hold-time "
               "forced the exit. Migrated to this scenario_key 2026-08-13 -- verified-live (1x), not "
               "wired-never-fired. SH (node 135) was the standing tester for this role but was paused "
               "2026-08-13 (shares SPY's underlying index, freed for a possible hedge use) -- ERX "
               "(node 226, soxl_ira) now carries the time_exit_via_trail role and is currently flat, "
               "waiting on a second, independent confirmation."),
    dict(id='buy_fill_reconciled',
         scenario="A real detected BUY fill opens the position with the correct shares/price (not just "
                  "the correct node identity)",
         code_path="signals_notify._reconcile_buy_fill (after db.open_position succeeds)",
         offline_coverage="test_part3_gap_resize.py exercises the function; no assertion on the new "
                           "coverage event yet",
         check_mechanism='coverage_events', scenario_key='buy_fill_reconciled',
         notes="New instrumentation, 2026-07-27 evening -- distinct from buy_fill_reconciles_correct_node "
               "above, which is about resolving the right node when 2+ are pending, not the fill math "
               "itself."),
    dict(id='replace_target_mismatch',
         scenario="Before atomically replacing a resting protective order, the algo verifies the order "
                  "actually resting at the broker is the one it thinks it is (and announces distinctly "
                  "when it is replacing a human-placed one)",
         code_path="signals_notify._verify_resting_before_replace, called from "
                    "_attempt_automated_sell and _attempt_automated_exit_sell",
         offline_coverage="8 unit tests (test_replace_target_mismatch.py)",
         check_mechanism='coverage_events', scenario_key='replace_target_mismatch',
         notes="Bug #4 of the 2026-08-14 SOXS incident. Both automated exit paths took whatever was in "
               "pos['sl_order_id'] / trail_state.exit_order_id and replaced it the moment their exit "
               "condition fired, with no check that it matched what the algo expected -- exactly the "
               "case automation_principles.md #5 exists for, never applied to this mechanism. It bit for "
               "real: a human placed a manual (mispriced) stop and the daemon replaced it with no record "
               "that it had been deliberate. DETECTION-ONLY AND NON-BLOCKING BY DESIGN, and that is "
               "load-bearing: a blocking version would strand a position that needs to exit, strictly "
               "worse than the bug. Both call sites wrap it in try/except so a broker fetch failure "
               "cannot prevent a real exit. 'manual_order_replaced' depends on the provenance column "
               "added the same day -- it can only fire for a position reconciled via "
               "scripts/reconcile_fill_manually.py, so expect it to stay rare."),
    dict(id='orphaned_broker_position',
         scenario="An intraday ground-truth sweep confirms every real non-zero broker position has a "
                  "matching local open_positions/addon_legs row (and the mirror-image STALE/MISMATCH/"
                  "SHORT/NULL-account cases)",
         code_path="signals_notify.check_orphaned_broker_positions -> "
                    "scripts.check_untracked_positions.run_full_sweep",
         offline_coverage="17 unit tests (test_orphaned_broker_position_sweep.py) + 7 fake_broker tests "
                           "against the underlying sweep (test_fake_broker_untracked_position_sweep_scenario.py)",
         check_mechanism='coverage_events', scenario_key='orphaned_broker_position',
         bad_results=['sweep_failed'],
         notes="The SWEEP is not new (built 2026-08-07 after the GDXU incident, wired into the 07:00 "
               "readiness block 2026-08-08). What's new, 2026-08-15, is the intraday CADENCE and this "
               "instrumentation. The SOXS/2026-08-14 incident showed the gap: 07:00 is pre-market, so a "
               "fill going unreconciled at 09:30 wasn't swept for until 07:00 the NEXT morning (~22h). "
               "Now every 30min, 9:45-16:00 on trading days. Deliberately reuses run_full_sweep rather "
               "than adding a parallel checker -- two answers to 'what does the broker hold' would "
               "drift. result='clean' is logged on a no-finding sweep ON PURPOSE: without it the Grid "
               "cannot distinguish 'swept, nothing found' from 'never ran', which is exactly the "
               "invisible state fast_path_fill_reconciliation sat in for months. So this row is "
               "expected to go verified-live quickly via 'clean' events -- a 'found' event is a real "
               "incident, and 'sweep_failed' is listed as bad_results so a broken sweep can't render "
               "as proof. NOTE result='found' does NOT imply anyone was paged: a two-consecutive-sweep "
               "confirmation gate (added same day, from the cold reviewer's maintained MEDIUM) withholds "
               "the Slack alert until a finding is seen twice, because STALE/MISMATCH are transiently "
               "true during normal market-hours reconciliation lag -- a false-positive class the 07:00 "
               "pre-market cadence structurally never had. 'found' is still logged on FIRST sighting so "
               "the record stays complete; SHORT findings bypass the gate and page immediately."),
    dict(id='confirmed_fill_dropped_at_gate',
         scenario="A CONFIRMED real broker fill that the auto-fill-detection opt-in gate declined to "
                  "auto-reconcile, while a matching pending_buys row is still open, produces a distinct "
                  "loud alert instead of a stale 'still pending' reminder",
         code_path="signals_notify.check_buy_reminders (broker re-verification via "
                    "schwab_client.get_filled_order, ahead of the has_capital_at_stake mute)",
         offline_coverage="5 fake_broker tests "
                           "(test_fake_broker_confirmed_fill_dropped_at_gate_scenario.py)",
         check_mechanism='coverage_events', scenario_key='confirmed_fill_dropped_at_gate',
         notes="Built 2026-08-15 in direct response to the SOXS/ira/wl_id=206 incident (2026-08-14): a "
               "2026-08-12 trailing buy filled 09:30:05 ET and sat unreconciled for hours. BOTH "
               "fill-detection paths (check_auto_fills poll, drain_fill_queue stream) sit behind the "
               "same auto_fill_detection opt-in gate and SOXS was in neither flag file, so both dropped "
               "the fill silently and identically. drain_fill_queue's pre-existing orphaned_fill_detected "
               "alert covers only the NO-pending-row case -- the less dangerous one. This row covers the "
               "more dangerous one. Deliberately exempt from has_capital_at_stake (same precedent as "
               "check_addon_buying_power_drift): a confirmed-but-unreconciled real fill is an "
               "infrastructure-precondition failure, not routine per-node noise. Expected to stay "
               "wired-never-fired in normal operation -- every firing is a real incident."),
    dict(id='morning_report_delivery',
         scenario="The Morning Report actually posts to Slack (not just gets built)",
         code_path="signals_notify.send_reference_report (via _post_message's channel/ts return)",
         offline_coverage="No dedicated unit test found for this exact branch",
         check_mechanism='coverage_events', scenario_key='morning_report_delivery',
         bad_results=['no_delivery_confirmation'],
         notes="New instrumentation, 2026-07-27 evening -- the report silently posted with zero candidate "
               "rows for weeks (fixed 2026-07-23) with nothing tracking delivery itself; this closes that "
               "class of gap going forward. 'no_delivery_confirmation' covers both a real send failure and "
               "any non-Socket-Mode delivery path (webhook/console) that can't confirm delivery -- "
               "deliberately not counted as proof of success."),
    dict(id='node_circuit_breaker',
         scenario="Node-level circuit breaker trips on 3 consecutive real order failures/blocks or "
                  "3 consecutive live-state reconciliation mismatches for the same node",
         code_path="schwab_safety.record_node_streak (called from schwab_client.py's 6 real placement "
                    "functions, incl. place_stop_loss/replace_order_with_stop_loss -- missing from the "
                    "first version, caught by the 2026-07-30 session-wrap review -- and "
                    "signals_notify.check_live_state_reconciliation)",
         offline_coverage="tests/test_schwab_safety.py: test_order_failure_streak_trips_circuit_breaker_"
                           "after_threshold/does_not_trip_below_threshold/resets_on_a_clean_attempt/"
                           "noop_for_unresolvable_node; tests/test_live_state_reconciliation.py: "
                           "test_reconciliation_mismatch_streak_trips_breaker_after_threshold/"
                           "resets_on_a_clean_poll; test_snoozed_mismatch_does_not_feed_the_breaker_streak; "
                           "tests/test_node_circuit_breaker.py (fake_broker-driven: a real post-approval "
                           "broker submission failure/success against tests/fake_broker.py, not a dry_run "
                           "or mocked-function shortcut)",
         check_mechanism='coverage_events', scenario_key='node_circuit_breaker_tripped',
         bad_results=[],
         notes="Built 2026-07-29 (the 3rd of 3 deferred design items from 2026-07-28 night, monitor-only "
               "per explicit user call -- same phased-rollout rationale as pre_action_state_verification: "
               "logs+alerts on a trip, never calls pause_node_automation() itself. bad_results left empty "
               "since 'tripped' is the interesting signal to review, not a failure of the check itself. "
               "A same-session Sonnet review round found and fixed 2 CONFIRMED bugs in the first version: "
               "(1) the order_failures streak's hit=False reset fired unconditionally right after "
               "approve_and_record succeeded, BEFORE the real broker submission was attempted -- so a "
               "genuine broker-rejection streak could never accumulate, only pure pre-submission "
               "SafetyViolation blocks could; fixed by moving the reset to fire only after a confirmed "
               "clean outcome (dry_run pass-through or real submission success); (2) record_node_streak's "
               "state-file write had no exception handling, unlike its sibling NODE_AUTOMATION_PATH/ "
               "TICKER_AUTOMATION_PATH functions -- since this call sits unconditionally in the real "
               "order-placement control flow, a write failure (disk full, a concurrent-write race) could "
               "have propagated up and aborted an otherwise-approved real order; fixed by wrapping the "
               "whole function body in the same fire-and-forget try/except contract as log_coverage_event."),

    # -- 2026-08-09: drought overlay / margin add-on-at-arm / skim-and-reserve --
    # PAPER-ONLY (docs/design.md's 2026-08-07 "Live automation design" section)
    # -- check_paper_drought_entry/check_paper_drought_handoff are now wired
    # into active_signals.py's live poll loop (a separate paired review of that
    # wiring specifically, plus a final session-wrap review of the cumulative
    # diff); check_paper_addon_trigger/check_paper_skim were already reachable
    # via the pre-existing check_paper_sells call, no wiring needed. All real
    # DB writes stay paper-scoped (paper_positions/paper_trade_log/
    # addon_legs/paper_addon_legs/skim_reserve_log) -- every real row these log
    # will carry mode='paper' (or 'dry_run' for an is_dry_run_sim position)
    # since no schwab_client/schwab_safety call is reachable from any of these
    # paths. drought_overlay_enabled/addon_enabled/skim_enabled default to 0
    # for every real node today, so this is a no-op for the current production
    # watchlist until a node is deliberately opted in. Every scenario below is
    # offline-covered by tests/test_overlay_paper_trading.py, built the same
    # session.
    dict(id='drought_entry',
         scenario="Drought-overlay entry fires exactly once per confirmed no-signal "
                  "gap, gated by confirm_days and (if set) the intraday-vol gate",
         code_path="paper_trading.check_paper_drought_entry / signals_notify.check_drought_entry "
                    "(real, mode='live'), both over the shared paper_trading.evaluate_drought_entry",
         offline_coverage="tests/test_overlay_paper_trading.py: "
                           "test_drought_entry_fires_once_confirmed_then_never_reenters_same_gap, "
                           "test_drought_vol_gate_excludes_unknown_reading; "
                           "tests/test_fake_broker_drought_entry_scenario.py::"
                           "test_drought_entry_places_real_trailing_buy_for_trailingboth_node "
                           "(real-mode decision-event assertion, added 2026-08-13)",
         check_mechanism='coverage_events', scenario_key='drought_entry',
         bad_results=[],
         notes="A paired Opus review (2026-08-09) found and fixed a HIGH-severity bug in the first "
               "version: the once-per-gap guard was missing entirely, so a drought position stopping "
               "out early re-entered on every subsequent poll for the rest of the same gap -- the "
               "validated backtest (find_drought_windows) makes exactly one trade per gap. Fixed via "
               "drought_gap_start-keyed dedup against paper_positions/paper_trade_log. Real-mode "
               "instrumentation gap fixed 2026-08-13: signals_notify.check_drought_entry computed the "
               "same shared evaluate_drought_entry decision as paper but never logged this scenario_key "
               "itself (only the downstream drought_entry_placement) -- no fake_broker test could have "
               "asserted an event the real code never logged, which is why fake_venue_proof read 'none' "
               "despite drought_entry_placement's sibling test already covering the same real code path."),
    dict(id='drought_handoff',
         scenario="An open drought-overlay position closes the moment the node's own "
                  "core z-score signal fires again",
         code_path="paper_trading.check_paper_drought_handoff / signals_notify.check_drought_handoff (real)",
         offline_coverage="tests/test_overlay_paper_trading.py: "
                           "test_drought_handoff_closes_position_on_core_signal",
         check_mechanism='coverage_events', scenario_key='drought_handoff',
         bad_results=[],
         notes="MUST run BEFORE the node's own core buy-signal scan in the same poll cycle (the "
               "opposite ordering requirement from drought_entry, which must run AFTER). Wired into "
               "active_signals.py's run_loop at all THREE real core-entry paths (the pinned-bar "
               "loop's entry block, the open_check _scan_buy_signals call, the ambient "
               "_scan_buy_signals call) -- an independent review of the wiring found the first "
               "attempt missed the pinned-bar path entirely, since that's the primary real entry "
               "mechanism for every current drought candidate."),
    dict(id='addon_entry_fill',
         scenario="Margin add-on-at-arm leg opens the moment a core position's "
                  "trailing-sell arms, sized to match the core position's current shares",
         code_path="paper_trading.check_paper_addon_trigger / signals_notify.check_addon_trigger_real (real)",
         offline_coverage="tests/test_overlay_paper_trading.py: "
                           "test_addon_trigger_opens_leg_matching_core_shares_and_dedupes, "
                           "test_addon_never_triggers_on_a_drought_position",
         check_mechanism='coverage_events', scenario_key='addon_entry_fill',
         bad_results=[],
         notes="Deliberately its own table (addon_legs/paper_addon_legs), never open_positions/"
               "trade_log -- a cold Opus review found that sharing those tables would break "
               "get_open_position/top_up_position/set_broker_stop_price/get_held_tickers/"
               "check_order's double-buy guard, all of which assume at most one row per ticker/wl_id."),
    dict(id='addon_exit_fill',
         scenario="An open add-on leg closes in lockstep with its parent core position's "
                  "own exit (SL/TRAIL/TIME), never independently",
         code_path="paper_trading.close_paper_addon_leg_if_open / "
                    "signals_notify.close_addon_leg_real_if_open (real, 7 call sites)",
         offline_coverage="tests/test_overlay_paper_trading.py: "
                           "test_addon_leg_closes_in_lockstep_with_parent_and_applies_margin_cost, "
                           "test_close_paper_addon_leg_if_open_is_a_noop_with_no_leg",
         check_mechanism='coverage_events', scenario_key='addon_exit_fill',
         bad_results=[],
         notes="Applies the validated MARGIN_COST_FLAT_PCT haircut (0.04pp) to the leg's pnl_pct -- "
               "missing in the first version (found by review), which was 0.04pp optimistic on every "
               "leg relative to scripts/stacked_model/add_on.py's validated model. NOTE (2026-08-07): "
               "this scenario_key is now also logged by the unrelated "
               "addon_leg_independent_sl_fill_detection row below (result='sl_closed_reconcile') -- a "
               "live event here does NOT distinguish lockstep-close proof from independent-stop-fill "
               "proof; check the result value or see that row's own status instead."),
    dict(id='addon_leg_independent_sl_fill_detection',
         scenario="An add-on leg's OWN protective stop fills independently (before the parent's "
                  "lockstep exit signal is ever computed) and gets detected/closed via reconciliation, "
                  "not left stuck open",
         code_path="signals_notify.check_addon_leg_reconciliation (new poll of leg['sl_order_id'])",
         offline_coverage="No dedicated fake_broker test yet -- built same session as "
                           "sl_order_fills_independent_detection below (same shape, one level down), "
                           "not separately regression-tested.",
         check_mechanism='coverage_events', scenario_key='addon_exit_fill',
         # The code already logs a genuinely distinct result ('sl_closed_reconcile',
         # signals_notify.py ~2454) for this specific path -- but bad_results was still []
         # (found by Opus audit, 2026-08-14), so any of the sibling lockstep-close results
         # ('closed', 'closed_late_reconcile', 'dry_run_closed', all logged under this same
         # scenario_key) counted as false proof of THIS row's independent-detection path.
         # paired Opus review (2026-08-14) found this list was incomplete: paper_trading.py's
         # own add-on-leg lockstep close (paper_trading.py:1584) ALSO logs this scenario_key,
         # with result=exit_reason (the parent's real exit reason, always one of this
         # project's small fixed exit-reason vocabulary -- SL/TP/TIME/TRAIL, never a genuine
         # independent-detection result) -- paper's leg close is ALWAYS lockstep-only per its
         # own docstring ("an add-on leg NEVER independently triggers its own SL/TRAIL check"),
         # so all 4 must be excluded too, or a single paper lockstep close could flip this row
         # to paper-only with zero real independent-detection proof.
         bad_results=['closed', 'closed_late_reconcile', 'dry_run_closed', 'SL', 'TP', 'TIME', 'TRAIL'],
         notes="New 2026-08-07, same root cause and same review pass as the core-position "
               "sl_order_fills_independent_detection fix -- check_addon_leg_reconciliation only ever "
               "polled leg['exit_order_id'] (an order WE placed in response to the parent's already-"
               "computed lockstep exit), never leg['sl_order_id'] (the leg's own resting stop, "
               "continuously monitored by the broker independent of our bar-close checks). Shares the "
               "addon_exit_fill scenario_key with the lockstep-close row above -- distinguish by "
               "result='sl_closed_reconcile'. No fake_broker regression test written for this specific "
               "branch (unlike the core-position sibling, which has 5) -- open follow-up."),
    dict(id='skim_fire',
         scenario="Skim moves skim_frac of the currently-deployed strategy value into "
                  "the reserve on a new equity high >= skim_step above the last skim reference",
         code_path="paper_trading.check_paper_skim",
         offline_coverage="tests/test_overlay_paper_trading.py: "
                           "test_skim_fires_on_new_high_and_amount_shrinks_each_time, "
                           "test_skim_never_fires_on_a_wiggle_at_the_peak, "
                           "test_skim_reserve_pool_actually_gains_real_shares",
         check_mechanism='coverage_events', scenario_key='skim_fire',
         bad_results=[],
         notes="A paired review found the first version's skim amount diverged from the validated "
               "model (scripts/stacked_model/skim_reserve.py's manual_redeploy_overlay) in the "
               "WRONG DIRECTION -- it recomputed off the full undiluted notional every time instead "
               "of a shrinking fraction of the already-reduced deployed sleeve, so later skims grew "
               "instead of shrinking. Fixed via a real skim_strategy_value/skim_reserve_balance dollar "
               "ledger, marked to a real cached SPY price at every call (the reserve was never "
               "actually modeled at all in the first version)."),
    dict(id='skim_redeploy_alert',
         scenario="Alert-only (never automated) notification when equity recovers past "
                  "80% or 100% of its pre-decline peak, only if a real non-empty reserve exists",
         code_path="paper_trading.check_paper_skim",
         offline_coverage="tests/test_overlay_paper_trading.py: "
                           "test_redeploy_alert_never_fires_against_an_empty_reserve, "
                           "test_redeploy_alerts_fire_exactly_twice_across_a_real_decline_recovery_cycle",
         check_mechanism='coverage_events', scenario_key='skim_redeploy_alert',
         bad_results=[],
         notes="A paired review found the first version dropped the reference's `w_spy > 0` guard "
               "entirely -- differential testing against the reference over 500 random equity paths "
               "showed this ONE missing guard explained 100% of the divergence (alerts firing against "
               "a genuinely empty reserve). Fixed via an explicit skim_reserve_balance>0 check on both "
               "the 80% and 100% branches. Carries forward the 2026-08-08 CRITICAL anti-wiggle fix "
               "(a threshold may only fire on real recovery from a real decline, never a sub-decline "
               "wiggle at a new high) unchanged."),

    # Real-only control points, added with the real drought/add-on order-
    # placement build (docs/plans/real_order_execution_drought_addon.md,
    # Part 8) -- no paper analogue, since paper never calls schwab_client/
    # schwab_safety. Not yet exercised live -- staged real-order testing
    # (Part 12) is a deliberate later phase, organic-signal-only, executed by
    # the user, never forced by an agent.
    dict(id='drought_entry_placement',
         scenario="A real (mode='live') drought-overlay entry places a real trailing-buy or "
                  "market-buy order, dispatching on the node's own strategy exactly like core entry",
         code_path="signals_notify.check_drought_entry / notify_drought_buy_signal",
         offline_coverage="tests/test_fake_broker_drought_entry_scenario.py",
         check_mechanism='coverage_events', scenario_key='drought_entry_placement',
         bad_results=[],
         notes="Live proof observed 2026-08-10: RETL (wl_id=143, soxl_ira) fired a real drought-overlay "
               "entry (confirm_days=3), which then reconciled and topped up correctly (see "
               "post_fill_topup's notes) -- 2x live total (first signalled 2026-08-07, confirmed to "
               "actual placement+fill 2026-08-10)."),
    dict(id='drought_handoff_cancel',
         scenario="A real drought HANDOFF cancels a still-resting drought entry order (Case A) -- "
                  "including the race where the cancel attempt finds the order already FILLED",
         code_path="signals_notify.check_drought_handoff",
         offline_coverage="tests/test_fake_broker_drought_handoff_scenario.py",
         check_mechanism='coverage_events', scenario_key='drought_handoff_cancel',
         bad_results=[],
         notes="Proves the new cancel race is handled -- without this row the mechanism has no "
               "accountability record at all. No live proof yet."),
    dict(id='drought_handoff_exit_placement',
         scenario="A real drought HANDOFF places a real market SELL for an open drought position "
                  "(Case B), and does not close the DB row until the fill is confirmed",
         code_path="signals_notify.check_drought_handoff",
         offline_coverage="tests/test_fake_broker_drought_handoff_scenario.py",
         check_mechanism='coverage_events', scenario_key='drought_handoff_exit_placement',
         bad_results=[],
         notes="Key structural difference from paper's synchronous HANDOFF close -- real must persist "
               "trail_state['exit_pending'] and let check_own_sell_fills/check_auto_fills close it on "
               "an unconfirmed-fill poll. No live proof yet."),
    dict(id='addon_entry_placement',
         scenario="A real margin add-on-at-arm leg places a real MARKET BUY for exactly the parent "
                  "core position's share count, despite the core's own resting protective SELL",
         code_path="signals_notify.check_addon_trigger_real",
         offline_coverage="tests/test_fake_broker_addon_entry_scenario.py",
         check_mechanism='coverage_events', scenario_key='addon_entry_placement',
         bad_results=[],
         notes="This IS the whole point of the mechanism -- without addon_double_buy_exemption firing "
               "first, this never places a real order at all (schwab_safety.check_order's ordinary "
               "_has_open_order guard would block it 100% of the time by construction). No live proof "
               "yet -- needs a mode='live' addon_enabled node on soxl_ira and an organic core arm."),
    dict(id='addon_double_buy_exemption',
         scenario="schwab_safety.check_order's is_addon_leg exemption fires only after all five "
                  "preconditions verify true against the DB (margin account, open core position, "
                  "parent genuinely armed, no leg already open, quantity exactly equals parent shares)",
         code_path="schwab_safety.check_order (is_addon_leg branch)",
         offline_coverage="tests/test_fake_broker_addon_entry_scenario.py",
         check_mechanism='coverage_events', scenario_key='addon_double_buy_exemption',
         bad_results=[],
         notes="The single most important new row -- the accountability record for the widened gate. "
               "detail records all five verified preconditions every time it fires, so every firing is "
               "individually reviewable. No live proof yet."),
    dict(id='addon_exit_placement',
         scenario="A real add-on leg's lockstep close places a real order first (cancel if still "
                  "unfilled, replace/place a market SELL if filled) before recording the close",
         code_path="signals_notify.close_addon_leg_real_if_open",
         offline_coverage="tests/test_fake_broker_addon_lockstep_exit_scenario.py",
         check_mechanism='coverage_events', scenario_key='addon_exit_placement',
         bad_results=[],
         notes="Divergence from paper is deliberate and logged: real closes at the leg's OWN fill "
               "price/reason (slippage will differ from the parent's exact exit price), not the "
               "parent's exact values paper uses. No live proof yet."),
    dict(id='addon_leg_reconciliation',
         scenario="A real add-on leg still entry_status='placed' past a timeout is polled/cancelled/"
                  "marked abandoned; an open leg whose parent already closed (missed lockstep) is "
                  "alerted loudly, never auto-closed at a guessed price",
         code_path="signals_notify.check_addon_leg_reconciliation",
         offline_coverage="tests/test_fake_broker_addon_lockstep_exit_scenario.py",
         check_mechanism='coverage_events', scenario_key='addon_leg_reconciliation',
         bad_results=[],
         notes="Pure observation for the orphaned-leg case, matching reconcile_daily_track_nodes' own "
               "stance -- never auto-closes. No live proof yet.",
         not_prod_required_note="User's call, 2026-08-13: known edge case (needs a genuinely orphaned/"
                                 "timed-out addon leg, not a normal successful entry-fill-exit cycle), "
                                 "not planning to force-test it. Lowered out of the active red bucket, "
                                 "not deleted."),
    dict(id='drought_handoff_alert_slot_preserved',
         scenario="Core's real BUY signal doesn't burn its once-per-day buy_alerted slot while a real "
                  "drought HANDOFF is still in flight (resting cancel race or unconfirmed exit poll)",
         code_path="active_signals._scan_buy_signals (already_held branch)",
         offline_coverage="tests/test_fake_broker_drought_handoff_scenario.py",
         check_mechanism='coverage_events', scenario_key='drought_handoff_alert_slot_preserved',
         # Was bad_results=[] -- the notes below already explained why
         # 'slot_released_pending_entry' alone shouldn't count as proof, but nothing
         # actually excluded it (found by Opus audit, 2026-08-14): a single pending-entry
         # release could false-flip this row to verified-live with zero real handoff-race
         # proof on file.
         bad_results=['slot_released_pending_entry'],
         notes="Real ordering contract: HANDOFF initiates the exit before core's scan runs, so core's "
               "entry lands on a LATER poll once the fill confirms -- without this fix the already_held "
               "branch never discards buy_alerted (unlike already_pending), permanently starving core's "
               "signal for the rest of the day. Instrumented 2026-08-10 (was previously not-instrumented "
               "-- no log_coverage_event call existed on this path at all). The same release also fires "
               "for a still-resting drought ENTRY order (pre-handoff, not the unwind race this row is "
               "about) -- logged as a distinct result='slot_released_pending_entry' vs. "
               "'slot_released_handoff' precisely so that sub-case alone can't false-flip this row to "
               "verified-live. No live proof of the handoff case yet."),
    dict(id='same_bar_reentry_cooldown',
         scenario="A node's fresh BUY signal is suppressed when its bar is not strictly newer "
                  "than that node's most recent CORE exit's bar -- real (non-paper) nodes via "
                  "active_signals._scan_buy_signals, paper nodes via paper_trading.py's own "
                  "mirror (added 2026-08-14 after LABD's paper node reproduced the identical bug "
                  "shape; mode is threaded through the shared scenario_key, see compute_mode_"
                  "statuses' per-mode breakdown, not a separate Grid row)",
         code_path="active_signals._scan_buy_signals (same_bar_cooldown branch); "
                   "paper_trading._paper_reentry_cooldown_active + its start_paper_buy/"
                   "start_paper_market_buy call sites",
         offline_coverage="tests/test_same_bar_reentry_cooldown.py (real); "
                          "tests/test_paper_trading.py (paper)",
         check_mechanism='coverage_events', scenario_key='same_bar_reentry_cooldown',
         # unparseable_exit_bar is a fail-open (cooldown NOT applied) outcome, not evidence
         # the cooldown fired -- was bad_results=[], letting a single parse failure count
         # as proof despite the notes below already explaining why it shouldn't (found by
         # Opus audit, 2026-08-14).
         bad_results=['unparseable_exit_bar'],
         notes="Built 2026-08-14 after a real live incident: RETL (soxl_ira) exited via TIME at "
               "09:30:54 on 2026-08-13, a fresh trailing-buy re-entry order placed at 09:31:54 -- "
               "the same hourly bar. The backtest kernel's per-bar loop (_simulate_trail_both) "
               "structurally cannot produce this (an exit processed on bar i only reaches the "
               "entry-check branch on bar i+1), so live's decoupled exit-scan/entry-scan needed an "
               "equivalent minimum-1-bar gap. get_last_exit_bar_time (signals_db.py) is scoped to "
               "position_source='core' only, deliberately -- a drought-overlay/add-on leg sharing "
               "the same wl_id can close through call sites that don't stash exit_decision_bar "
               "(close_addon_leg_real_if_open, the HANDOFF close), and an unfiltered query would let "
               "that later NULL-exit_bar_time row mask the real core exit's bar, silently disarming "
               "the cooldown on exactly the nodes most likely to need it (RETL runs both drought and "
               "add-on). result='unparseable_exit_bar' is a distinct, non-bad_results outcome (fails "
               "open, never blocks/crashes) for a stored exit_bar_time the fixed-format strptime read "
               "can't parse -- kept separate from 'suppressed' so a parse failure can't false-inflate "
               "genuine cooldown-fired evidence. No live proof yet -- RETL's own incident predates "
               "this fix landing, so its exit_bar_time wasn't recorded at the time."),
    dict(id='abnormal_drift_alert',
         scenario="A real (broker-filled, non-paper/non-dry-run-sim) entry or exit fill's "
                  "entry_drift_pct/exit_drift_pct exceeds the calibrated threshold, for a node with "
                  "real capital at stake -- alert-only, escalation-capped at 2 Slack posts/ticker/day",
         code_path="signals_db.check_abnormal_drift (called from the single real chokepoint every "
                   "entry/exit fill passes through, open_position()/close_position(), rather than "
                   "threaded into each of their 15+ call sites across signals_notify.py/"
                   "paper_trading.py/signals_handlers.py individually)",
         offline_coverage="tests/test_fake_broker_abnormal_drift_scenario.py (6 scenarios: entry breach "
                          "alerts, exit breach alerts, below-threshold no-op, below-capital-at-stake "
                          "no-op, dry-run-sim fill no-op, 3rd-same-day-breach suppressed-but-logged)",
         check_mechanism='coverage_events', scenario_key='abnormal_drift_alert',
         bad_results=[],
         notes="Built 2026-08-15, design settled 2026-08-14 evening (docs/backlog_cache.md's "
               "'abnormal-drift liquidity-signal alert' item) -- direct empirical use of trade_log's "
               "already-tracked entry_drift_pct/exit_drift_pct as evidence of real execution slippage. "
               "Threshold calibrated off a same-day real drift-distribution audit "
               "(docs/research_log.md's 2026-08-15 entry): excluding 3 known-anomalous backdated "
               "catch-up entries, real drift is mean -0.3% to -0.5%, std 1.4-1.9% (n=25-33) -- the "
               "original 0.5% placeholder would have fired on 32-52% of real trades, so replaced with "
               "3.0% (signals_db.ABNORMAL_DRIFT_THRESHOLD_PCT, env-overridable via "
               "ABNORMAL_DRIFT_THRESHOLD_PCT), a first-pass calibration on a small sample -- revisit as "
               "more real trades (especially SL exits, the highest-variance exit_reason on file) "
               "accumulate. Gated on signals_helpers.has_capital_at_stake (same gate as every routine/ "
               "anomaly Slack alert since the 2026-08-08 redesign), so this can't fire on soxl_ira's "
               "small proving-ground tier, a paper/research-mode node, or a dry-run-sim synthetic fill. "
               "bad_results left empty since 'suppressed_daily_cap' is a correctly-working escalation "
               "cap, not a failure of the check itself. Alert-only, never automatic -- same detection- "
               "only-decide-later pattern as record_node_streak/_log_pre_action_state_verification "
               "(user's explicit call: 'if we lose 1 set of trades it is what it is'). No live proof "
               "yet -- not forceable via node config (an abnormal-drift event isn't a deterministic "
               "guard-rejection scenario), same organic-only shape as daemon_exception_survival."),
]


_EVENT_ASSERTED_RE = re.compile(r'get_coverage_events\(\s*scenario_key\s*=\s*["\'](\w+)["\']')

# Self-declared proof marker for scenario_expectations-mechanism rows -- see the
# module docstring's "structural blind spot" note. A test file's docstring
# containing `registry id 'pinned_entry_trigger'` is a deliberate assertion by
# the test's author, not an inferred match -- kept to REGISTRY 'id' (unique per
# row) rather than 'scenario_key' (can be shared/ambiguous across modes, see the
# mode_filter collision note below) for precision.
_REGISTRY_ID_MARKER_RE = re.compile(r"registry id\s+['\"](\w+)['\"]")
_REGISTRY_ID_TO_KEY = {r['id']: r['scenario_key'] for r in REGISTRY if r.get('scenario_key')}

# Files that exercise the coverage/scenario_expectations *infrastructure itself*
# (signals_db plumbing, coverage_check.py's checker logic) using scenario_key
# strings as arbitrary fixture data ('sl_placement', 'entry_fill', etc. reused
# purely as test values), not to prove any real production code path. Found by
# Opus review 2026-07-26: test_coverage_check.py:356 calls
# db.log_coverage_event('sl_placement', ...) directly and then asserts it right
# back via get_coverage_events -- matching this file made sl_placement (a real
# SL-order-placement branch with a documented, unresolved live-attempt-failed
# history) render as false 'event-asserted' proof. A structural file-purpose
# exclusion, not a per-scenario_key hand-typed mapping -- if a new meta/infra
# test file is added later that reuses real scenario_key strings as fixture
# data, add it here too.
_INFRA_TEST_FILES = {'test_coverage_check.py'}


def _quoted(key):
    return re.compile(r'["\']' + re.escape(key) + r'["\']')


_OFFLINE_PROOF_CACHE = None


def _scan_offline_proof():
    """Greps every tests/test_*.py file once (memoized per process -- test files
    don't change mid-run) and returns (event_asserted, mentioned) -- sets of
    scenario_key strings. See module docstring for what each means.

    Both 'event-asserted' and 'mentioned' require an exact quoted match
    ('key' or "key"), not a bare substring -- found by Opus review 2026-07-26
    that a raw `key in text` check false-matched a prefix collision
    (sl_placement inside sl_placement_fast_confirm_timeout) and unquoted
    mentions (a module docstring naming another test file's filename, e.g.
    'tests/test_part3_gap_resize.py' matching scenario_key gap_resize)."""
    global _OFFLINE_PROOF_CACHE
    if _OFFLINE_PROOF_CACHE is not None:
        return _OFFLINE_PROOF_CACHE
    all_keys = {r['scenario_key'] for r in REGISTRY if r.get('scenario_key')}
    event_asserted, mentioned = set(), set()
    if not _TESTS_DIR.is_dir():
        _OFFLINE_PROOF_CACHE = (event_asserted, mentioned)
        return _OFFLINE_PROOF_CACHE
    for path in sorted(_TESTS_DIR.glob("test_*.py")):
        if path.name in _INFRA_TEST_FILES:
            continue
        text = path.read_text()
        for m in _EVENT_ASSERTED_RE.finditer(text):
            if m.group(1) in all_keys:
                event_asserted.add(m.group(1))
        for m in _REGISTRY_ID_MARKER_RE.finditer(text):
            key = _REGISTRY_ID_TO_KEY.get(m.group(1))
            if key:
                event_asserted.add(key)
        for key in all_keys:
            if _quoted(key).search(text):
                mentioned.add(key)
    _OFFLINE_PROOF_CACHE = (event_asserted, mentioned)
    return _OFFLINE_PROOF_CACHE


def offline_proof_for(scenario_key, mode_filter=None):
    """Returns (proof_str, detail_str) for one scenario_key -- 'event-asserted',
    'behavior-only', or 'none'. See module docstring for the distinction.

    mode_filter (pass the REGISTRY row's mode_filter, e.g. 'dry_run'/'paper')
    guards against the documented scenario_key collision (entry_fill/exit_fill
    logged by both paper_trading.py under mode='paper' and the dry_run-fill-
    synthesis code under mode='dry_run', see the dry_run_buy_synthesis/
    paper_entry_fill REGISTRY rows) -- without it, a test proving only the
    paper path would also light up the dry_run row as proven. Requires the
    mode_filter word to appear quoted in the same test file as a light,
    derived (not hand-typed) disambiguator; not perfect -- see docs/backlog_cache.md."""
    if scenario_key is None:
        return 'none', 'No scenario_key to search test files for.'
    event_asserted, mentioned = _scan_offline_proof()
    if mode_filter and scenario_key in (event_asserted | mentioned) and not _mode_filter_match(scenario_key, mode_filter):
        return 'none', (f"Matched only in a test file that doesn't also reference mode_filter="
                         f"{mode_filter!r} -- likely proving a different mode's use of this shared "
                         f"scenario_key (see the docstring's entry_fill/exit_fill collision note).")
    if scenario_key in event_asserted:
        return 'event-asserted', 'A test asserts get_coverage_events() for this scenario_key.'
    if scenario_key in mentioned:
        return 'behavior-only', ('This scenario_key appears in a test file, but no test asserts '
                                  'the coverage event itself -- the log_coverage_event call could be '
                                  'deleted and the test would still pass.')
    return 'none', 'This scenario_key does not appear in any tests/test_*.py file.'


def _mode_filter_match(scenario_key, mode_filter):
    """For a mode_filter-scoped row, only credit a match found in a test file
    that also mentions the mode_filter word (quoted, or in the filename) --
    see offline_proof_for's docstring."""
    if not _TESTS_DIR.is_dir():
        return False
    for path in sorted(_TESTS_DIR.glob("test_*.py")):
        if path.name in _INFRA_TEST_FILES:
            continue
        text = path.read_text()
        if not (_EVENT_ASSERTED_RE.search(text) and scenario_key in text) and not _quoted(scenario_key).search(text):
            continue
        if mode_filter in path.name or _quoted(mode_filter).search(text):
            return True
    return False


# Test files built on tests/fake_broker.py -- a stronger evidence tier than
# offline_proof_for()'s general 'event-asserted', since these drive the real
# schwab_client.py/schwab_safety.py code against a stateful, evolving fake
# order book (place -> hours pass -> a later decision reads the still-resting
# order -> a guard reacts), not a single mocked function call returning a
# canned value. Built 2026-07-29 directly because that static-mock style let
# real bugs (the resting-order self-block bug, the TIME-while-armed bug) hide
# behind a fully green suite -- see docs/backlog_cache.md's 2026-07-28 (night)
# stateful-fake-order-book-fixture entry, and tests/fake_broker.py's docstring.
_FAKE_VENUE_CACHE = None

# A test file mentioning the string "fake_broker" (an import, a docstring, a
# comment) used to be enough to count as fake-venue proof -- gamed 2026-08-01:
# 2 real test files admitted in their own docstrings to importing fake_broker
# for no reason other than to satisfy this exact check, the same false-green
# shape as the already-fixed 2026-07-26 coverage_events text-scan bug, one
# layer up. Now requires the fixture actually be injected into a test
# function's signature (real pytest fixture usage), not just present in the
# file's text.
_FAKE_BROKER_FIXTURE_RE = re.compile(r'def\s+test_\w*\s*\(([^)]*)\)', re.DOTALL)


def _uses_fake_broker_fixture(text):
    return any(
        re.search(r'\bfake_broker\b', params)
        for params in _FAKE_BROKER_FIXTURE_RE.findall(text)
    )


def _scan_fake_venue_proof():
    """Mirrors _scan_offline_proof(), scoped to only files where at least one
    test function actually takes fake_broker as a fixture argument (real
    usage), not just any file that mentions the string."""
    global _FAKE_VENUE_CACHE
    if _FAKE_VENUE_CACHE is not None:
        return _FAKE_VENUE_CACHE
    all_keys = {r['scenario_key'] for r in REGISTRY if r.get('scenario_key')}
    event_asserted, mentioned = set(), set()
    if not _TESTS_DIR.is_dir():
        _FAKE_VENUE_CACHE = (event_asserted, mentioned)
        return _FAKE_VENUE_CACHE
    for path in sorted(_TESTS_DIR.glob("test_*.py")):
        text = path.read_text()
        if not _uses_fake_broker_fixture(text):
            continue
        for m in _EVENT_ASSERTED_RE.finditer(text):
            if m.group(1) in all_keys:
                event_asserted.add(m.group(1))
        for m in _REGISTRY_ID_MARKER_RE.finditer(text):
            key = _REGISTRY_ID_TO_KEY.get(m.group(1))
            if key:
                event_asserted.add(key)
        for key in all_keys:
            if _quoted(key).search(text):
                mentioned.add(key)
    _FAKE_VENUE_CACHE = (event_asserted, mentioned)
    return _FAKE_VENUE_CACHE


def fake_venue_proof_for(scenario_key):
    """Returns (proof_str, detail_str) -- 'event-asserted', 'behavior-only', or
    'none' -- same three-tier shape as offline_proof_for(), but scoped to only
    tests/fake_broker.py-based tests. No mode_filter handling: fake-broker
    scenarios test real (non-dry_run) order-placement code directly, which
    doesn't share the paper/dry_run scenario_key-collision problem
    offline_proof_for's mode_filter exists to solve."""
    if scenario_key is None:
        return 'none', 'No scenario_key to search fake_broker test files for.'
    event_asserted, mentioned = _scan_fake_venue_proof()
    if scenario_key in event_asserted:
        return 'event-asserted', 'A fake_broker test asserts get_coverage_events() for this scenario_key.'
    if scenario_key in mentioned:
        return 'behavior-only', ('This scenario_key appears in a fake_broker test, but no assertion '
                                  'checks the coverage event itself.')
    return 'none', 'No fake_broker-based test exercises this scenario_key yet.'


def _scenario_expectation_recent_proof(scenario_key, lookback_days=21):
    """Cross-checks a scenario_expectations-mechanism scenario directly against
    real trade_log/pending_buys/open_positions data over the last lookback_days
    calendar days, reusing coverage_check._check_trade_lifecycle (the same
    function the daily check itself runs) rather than re-deriving the logic --
    so this can never silently drift from what "met" actually means for that
    checker. Returns a detail string on the first real match found (most
    recent day first), or None if nothing in the window proves it.

    Exists because coverage_deviations only ever records FAILURES (a day the
    check passes writes nothing) -- so zero deviation rows is structurally
    ambiguous between "always passed silently" and "never actually checked,"
    and compute_status's caller-facing default for that ambiguity used to be
    the pessimistic 'wired-never-fired' with no attempt to resolve it.
    Confirmed real 2026-08-13: canary_time_exit (XLF/FAZ) had 7 real correct
    TIME exits on file (2026-08-04 through 2026-08-11) and still read
    wired-never-fired, since none of those passing days ever produced a
    coverage_deviations row to find."""
    expectations = [e for e in db.get_scenario_expectations(active_only=False)
                     if e['scenario_key'] == scenario_key]
    if not expectations:
        return None
    today = date.today()
    days = [today - timedelta(days=i) for i in range(lookback_days)]
    days = [d for d in days if _is_trading_day(d.isoformat())]
    for exp in expectations:
        for d in days:
            met, summary, no_activity = _check_trade_lifecycle(exp, d.isoformat())
            if met and not no_activity:
                return (f"{exp.get('ticker') or ''} node_id={exp.get('node_id')}: "
                         f"{summary} on {d.isoformat()} (direct trade_log/pending_buys "
                         f"check, {lookback_days}d lookback)")
    return None


def compute_status(row):
    """Returns (status_str, detail_str) computed live from real DB rows -- never
    a hand-typed field. status_str is one of: 'not-instrumented', 'offline-only',
    'wired-never-fired', 'paper-only', 'dry_run-only', 'verified-live',
    'live-attempt-failed' (fired for real, but the outcome was in bad_results --
    e.g. a real SL placement attempt that was blocked by a bug, not a proof the
    path works). A row can only land in bad_results if it explicitly opts in --
    "blocked" is the *correct* outcome for a guard scenario, so there's no safe
    universal rule for what counts as bad."""
    mech = row['check_mechanism']
    if mech == 'offline_only':
        return 'offline-only', 'No live component by design.'
    if mech == 'open_price_quality_log':
        with db._conn() as c:
            r = c.execute(
                "SELECT COUNT(*) n, MAX(ts) last_ts FROM open_price_quality_log"
            ).fetchone()
        if r['n'] > 0:
            return 'verified-live', f"{r['n']}x real open-price capture, last {r['last_ts']}"
        return 'wired-never-fired', 'open_price_quality_log table exists but has no rows yet.'
    if mech == 'none' or row['scenario_key'] is None:
        return 'not-instrumented', 'No log_coverage_event call exists for this path yet.'

    if mech == 'scenario_expectations':
        # coverage_deviations only ever records FAILURES (record_deviation fires when
        # a daily check is NOT met; a met day writes nothing) -- so "no rows" can mean
        # either "always passed silently" or "never actually ran," and this mechanism
        # can't tell those apart. The one genuine positive signal available is a
        # reason_by='system' row: proof the scenario deviated earlier in the day and
        # was later confirmed met on a same-day re-check (clear_deviation_if_resolved).
        # A human-authored reason only proves a deviation happened and someone looked
        # at it -- not that the scenario is currently behaving correctly. An
        # unexplained (reason IS NULL) row is the worst case: a live, unresolved
        # failure. Inverted logic here was a real CONFIRMED bug (Opus review,
        # 2026-07-27) -- a prior version fed these deviation rows into the same
        # good/bad bucketing as coverage_events, so an unexplained failure rendered
        # green ("verified-live") and a clean day with zero rows rendered red.
        with db._conn() as c:
            unexplained = c.execute(
                "SELECT COUNT(*) n, MAX(ts) last_ts FROM coverage_deviations "
                "WHERE scenario_key = ? AND reason IS NULL", (row['scenario_key'],)
            ).fetchone()
            if unexplained['n'] > 0:
                return 'deviation-unexplained', (
                    f"{unexplained['n']}x unexplained deviation, last {unexplained['last_ts']} -- "
                    f"a real, currently-unresolved failure")
            # reason_by='system' is written by TWO different call sites with opposite
            # meanings: clear_deviation_if_resolved's 'Auto-resolved: ...' (genuine proof
            # the scenario was met on a later same-day check) and coverage_check.py's
            # price-action auto-explain 'Auto-verified: ... never crossed entry threshold'
            # (the OPPOSITE of proof -- explains why nothing happened, not that it did).
            # Found 2026-08-13 (Opus review): counting both identically fabricated
            # verified-live for market_buy_placement (VOO) -- 8x 'auto-resolved' with
            # zero of them genuine. Only the 'Auto-resolved:' prefix is real proof.
            system_resolved = c.execute(
                "SELECT COUNT(*) n, MAX(ts) last_ts FROM coverage_deviations "
                "WHERE scenario_key = ? AND reason_by = 'system' AND reason LIKE 'Auto-resolved:%'",
                (row['scenario_key'],)
            ).fetchone()
            if system_resolved['n'] > 0:
                return 'verified-live', (
                    f"{system_resolved['n']}x auto-resolved (deviated then met same day), "
                    f"last {system_resolved['last_ts']}")
            any_row = c.execute(
                "SELECT COUNT(*) n FROM coverage_deviations WHERE scenario_key = ?",
                (row['scenario_key'],)
            ).fetchone()
        # No positive OR negative signal in coverage_deviations -- cross-check
        # directly against real trade_log/pending_buys/open_positions before
        # falling back to the pessimistic default (see docstring above).
        direct_proof = _scenario_expectation_recent_proof(row['scenario_key'])
        if direct_proof:
            return 'verified-live', direct_proof
        if any_row['n'] > 0:
            return 'wired-never-fired', (
                "Only human-explained historical deviation(s) exist -- explains a past "
                "failure, doesn't prove current correct behavior.")
        return 'wired-never-fired', (
            "No coverage_deviations history at all -- this mechanism can't distinguish "
            "'always passed silently' from 'daily check never actually ran.'")

    mode_filter = row.get('mode_filter')
    bad_results = set(row.get('bad_results', []))
    # result_filter (2026-08-15): the third axis, exactly parallel to
    # mode_filter, for a scenario_key shared by SEVERAL distinct logic branches
    # that are only distinguishable by their `result` value. Real case that
    # forced it: signals_notify._alert_reconcile_mismatch hardcodes
    # scenario_key='reconciliation_mismatch' and passes the branch name as
    # `result`, so the three Stage B/C branches added 2026-08-15
    # (never_had_sl/sl_price_mismatch/sl_quantity_mismatch) would have
    # inherited verified-live status from 4,311 pre-existing events belonging
    # to entirely different branches (shares/missing_sl/missing_trailing_sell)
    # -- precisely the false-proof failure this registry exists to prevent.
    # bad_results is the wrong lever for this: inverting it would land the row
    # in *-attempt-failed rather than the truthful wired-never-fired.
    # `include=False` gives the mirror image, so an existing row can EXCLUDE
    # the results that were split out into a new row.
    result_filter = row.get('result_filter')
    _clauses = ["scenario_key = ?"]
    _params = [row['scenario_key']]
    if mode_filter:
        _clauses.append("mode = ?")
        _params.append(mode_filter)
    if result_filter:
        _results = list(result_filter.get('results', result_filter)
                         if isinstance(result_filter, dict) else result_filter)
        _include = result_filter.get('include', True) if isinstance(result_filter, dict) else True
        _op = "IN" if _include else "NOT IN"
        _clauses.append(f"result {_op} ({','.join('?' * len(_results))})")
        _params.extend(_results)
    with db._conn() as c:
        rows = c.execute(
            "SELECT mode, result, COUNT(*) n, MAX(ts) last_ts FROM coverage_events "
            f"WHERE {' AND '.join(_clauses)} GROUP BY mode, result", _params
        ).fetchall()

    # Bucket by mode: total count/last_ts, plus whether every event in that mode
    # was a bad_results outcome (no good evidence) or at least one was good.
    by_mode = {}
    for r in rows:
        m = by_mode.setdefault(r['mode'], {'n': 0, 'last_ts': None, 'good_n': 0})
        m['n'] += r['n']
        m['last_ts'] = max(filter(None, [m['last_ts'], r['last_ts']]), default=None)
        if r['result'] not in bad_results:
            m['good_n'] += r['n']

    for mode, label in (('live', 'verified-live'), ('dry_run', 'dry_run-only'), ('paper', 'paper-only')):
        if mode not in by_mode:
            continue
        m = by_mode[mode]
        if m['good_n'] > 0:
            return label, f"{m['n']}x {mode}, last {m['last_ts']}"
        # A genuine good outcome always wins (organic proof is never suppressed) --
        # but a row that fires constantly and can structurally never look good
        # (e.g. position_lock: only real lock contention counts as evidence, and
        # that's unlikely given actual usage) would otherwise sit in *-attempt-failed
        # forever despite already being a deliberate, human-reviewed demotion. Found
        # 2026-08-14: the not_prod_required_note check below was unreachable for any
        # row with real event history, only ever applying to zero-event rows.
        # Gated on demote_on_all_bad, a distinct explicit opt-in -- not_prod_required_note
        # alone would silently extend this demotion to the 10 other rows that also carry
        # that field for a genuinely different reason (a scenario that's simply never
        # fired at all, not one that fires constantly and can never look good), masking a
        # real future regression on any of them the moment they start firing with bad
        # results (found by Opus review, 2026-08-14 -- only position_lock's demotion was
        # actually reasoned through and intended here).
        if row.get('demote_on_all_bad') and row.get('not_prod_required_note'):
            return 'not-prod-required', row['not_prod_required_note']
        return ('live-attempt-failed' if mode == 'live' else f'{mode}-attempt-failed',
                f"{m['n']}x {mode}, all bad_results ({bad_results}), last {m['last_ts']} -- "
                f"fired for real but never with a good outcome")
    if row.get('not_prod_required_note'):
        # A deliberate, human demotion (2026-08-13) -- not a code-derived
        # verdict like structural-gap. Treated the same as offline-only:
        # a neutral, non-red status, not something waiting to be closed.
        return 'not-prod-required', row['not_prod_required_note']
    if row.get('structural_note'):
        # Distinct from the plain wired-never-fired default below: this row
        # has been checked directly against the real code and confirmed to
        # need a specific condition organic trading volume/more canary nodes
        # can't produce on their own (see the row's structural_note) -- worth
        # surfacing as its own status rather than looking identical to "just
        # hasn't happened yet, give it more days."
        return 'structural-gap', row['structural_note']
    return 'wired-never-fired', 'scenario_key exists in code but has never logged a real event.'


# The BEST (cheapest/lowest-risk) harness CAPABLE of producing evidence for this row -- not
# the only one, and not "has it been proven yet" (that's compute_status()'s job). Three buckets
# only (2026-08-13, collapsed from an earlier 7-bucket draft per the user's correction: paper and
# canary are the same underlying thing -- neither ever reaches the real broker/venue, "canary" is
# just a paper/dry_run node that happens to be hair-triggered; that's a config choice, not a
# structural difference). Deliberately only 2 values -- a 3rd 'fake_venue' bucket was considered
# and dropped 2026-08-13 (its only member, kernel_fill_parity, was removed from REGISTRY entirely
# for asking a categorically different question -- see that removal's comment above):
#
#   'live'            -- needs real capital. Either the code path structurally short-circuits
#                        before reaching the real broker for any dry_run/paper node (is_dry_run_sim
#                        bypass, order_id always None under dry_run), or it needs a genuine human
#                        Slack click on a real position, or a genuine live-only fault/coincidence.
#   'canary'          -- a dry_run or paper node genuinely exercises the real guard/decision chain
#                        without ever needing the broker network call itself. Zero capital risk.
#
# A row with no entry here at all (see e.g. second_ticker_one_account's removal comment) means
# the live/canary question doesn't apply to it -- not an oversight, a deliberate exclusion with
# its own documented reason. best_harness_for() returns None for such an id.
#
# Built from the actual code-verified findings of a 4-agent parallel investigation (each agent
# read the real log_coverage_event call sites and traced dry_run/is_dry_run_sim short-circuits)
# plus manual checks in the same session. THIS IS A MANUAL CLASSIFICATION, NOT LIVE-COMPUTED --
# unlike every other function in this file, nothing here re-derives from current code state
# automatically. If the code changes (a dry_run bypass removed, a new path opened), this dict can
# silently go stale with no warning. Treat entries as a snapshot to verify against real code
# before relying on them for a real decision, same as any other stale-claim risk in this project.
BEST_HARNESS = {
    # --- live (26) ---
    'addon_buying_power_check': 'live',
    'addon_buying_power_drift_check': 'live',
    'addon_double_buy_exemption': 'live',
    'addon_entry_placement': 'live',
    'addon_exit_placement': 'live',
    'addon_leg_independent_sl_fill_detection': 'live',
    'addon_leg_reconciliation': 'live',        # also luck-adjacent: needs an orphaned leg
    'addon_second_ticker_buy_allowed': 'live',  # also luck-adjacent: needs a timing coincidence
    'drought_handoff': 'live',
    'drought_handoff_exit_placement': 'live',
    'automated_sell_execution': 'live',
    'exit_arm_latency': 'live',
    'post_fill_topup': 'live',
    'sl_order_fills_independent_detection': 'live',
    'sl_sync_placement': 'live',
    'time_exit_trigger_unarmed': 'live',
    'time_exit_trigger_armed': 'live',
    'trailing_arm_reread': 'live',
    'dup_sell_order_blocked': 'live',
    'buy_fill_reconciled': 'live',
    'buy_fill_reconciles_correct_node': 'live',  # also luck-adjacent: needs 2 pending buys to coincide
    'fast_path_fill_reconciliation': 'live',     # also luck-adjacent: needs a real partial fill
    'manual_sl_fallback_alert': 'live',          # also luck-adjacent: needs a genuine broker failure
    'manual_buy_confirmation_account': 'live',   # needs a real human Slack click on a live node
    'stale_buy_button_guard': 'live',            # needs a real human Slack click on a live node
    'buy_buttons_resolve_correct_node': 'live',  # needs a real human Slack click on a live node
    'daemon_exception_survival': 'live',         # genuine fault, no config forces it
    'automated_exit_execution': 'live',          # added 2026-08-14, Opus audit (real code, no row before)
    'automated_buy_execution': 'live',           # added 2026-08-14, Opus audit (real code, no row before)
    'price_discontinuity_ruled_out': 'live',     # added 2026-08-14, corp-action false-positive fix

    # --- canary -- everything else, listed explicitly so this dict stays a complete, auditable
    # map instead of relying on a silent default. Includes 2 rows that are harness-agnostic/
    # scheduled (morning_report_delivery, automated_sell_mode_skip -- the latter's real trigger
    # is specifically state=='paper'). oversell_guard_correct_position's log call was missing
    # a success-path entry (fixed 2026-08-13, schwab_safety.py) -- correctly 'canary' now that
    # it's properly instrumented. ---
    'account_disabled_block': 'canary',
    'addon_entry_fill': 'canary',
    'addon_exit_fill': 'canary',
    'buy_blocked_position_exists': 'canary',
    'canary_bull_bear_pair': 'canary',
    'canary_early_sl': 'canary',
    'canary_full_lifecycle': 'canary',
    'canary_overnight_carry': 'canary',
    'canary_time_exit': 'canary',
    'cash_check': 'canary',
    'drought_entry': 'canary',
    'drought_entry_placement': 'canary',
    'drought_handoff_alert_slot_preserved': 'canary',  # partial: only the pending-entry sub-case
    'drought_handoff_cancel': 'canary',                # partial: only the no-real-order sub-case
    'dry_run_buy_synthesis': 'canary',
    'dry_run_sim_close': 'canary',
    'dup_order_no_false_block': 'canary',
    'dup_order_retry_after_failure': 'canary',
    'entry_abandon_timeout': 'canary',
    'gap_resize': 'canary',
    'kill_switch_block': 'canary',
    'live_state_reconciliation_mismatch': 'canary',    # needs a manually-seeded fake position
    'market_buy_placement': 'canary',
    'node_circuit_breaker': 'canary',
    'node_level_automation_pause': 'canary',
    'open_price_quality': 'canary',
    'pinned_entry_trigger': 'canary',
    'position_lock': 'canary',
    'pre_action_state_verification': 'canary',
    'same_day_block': 'canary',                        # narrow: needs force_same_day_block=1
    'second_ticker_buy_allowed_when_cash_sufficient': 'canary',
    'sl_async_fallback': 'canary',
    'ticker_account_assignment_mismatch': 'canary',
    'ticker_level_automation_pause': 'canary',
    'ticker_not_in_automation_scope_block': 'canary',
    'ticker_not_live_mode_block': 'canary',
    'two_nodes_same_ticker_diff_accounts': 'canary',
    'unknown_account_block': 'canary',
    # 6 check_order guard rows added by a concurrent session, classified 2026-08-13 -- same
    # mechanism shape as cash_check/same_day_block above: check_order runs identically for a
    # dry_run node (only the final broker submission is skipped), so no real capital is needed.
    # buy_signal_window_block/buy_trading_day_block need deliberate staging (an inflated
    # starting_notional_override, a tightened cap) -- they don't fire from ordinary traffic,
    # since active_signals._in_window's own scan gate pre-empts the daemon from ever attempting
    # an out-of-window/non-trading-day BUY in the first place (found during Opus review).
    'buy_signal_window_block': 'canary',
    'buy_trading_day_block': 'canary',
    'daily_order_cap_block': 'canary',
    'global_burst_cap_block': 'canary',
    'hard_order_ceiling_block': 'canary',               # $100k HARD_ORDER_CEILING -- needs an
                                                         # inflated starting_notional_override to
                                                         # force safely on a dry_run node
    'notional_cap_block': 'canary',
    # paper_entry_fill/paper_exit_fill corrected 2026-08-13 (Opus review, same pass as
    # skim_fire/skim_redeploy_alert below) -- 'canary' was wrong: code_path is entirely
    # paper_trading.py (start_paper_market_buy/update_paper_buys, check_paper_sells), and
    # the dry_run leg of the SAME shared scenario_key is already separately covered by
    # dry_run_buy_synthesis/dry_run_sim_close (signals_notify.py, mode_filter='dry_run')
    # -- these two rows have zero incremental dry_run reachability of their own.
    'paper_entry_fill': 'paper',
    'paper_exit_fill': 'paper',
    # skim_fire/skim_redeploy_alert corrected 2026-08-13 (Opus review) -- unlike the two rows
    # above, these have NO dry_run reachability at all, not even incidentally: check_paper_skim
    # (paper_trading.py) never calls schwab_client/schwab_safety, is only ever invoked from
    # check_paper_sells' paper-position-close path, and hardcodes mode="paper" in its two
    # log_coverage_event calls. A dry_run node's real/dry_run core position close never reaches
    # this function at all -- 'canary' was factually wrong, not just an imprecise label.
    'skim_fire': 'paper',
    'skim_redeploy_alert': 'paper',
    'oversell_guard_correct_position': 'canary',
    'morning_report_delivery': 'canary',          # harness-agnostic, scheduled
    'automated_sell_mode_skip': 'canary',         # trigger is specifically state=='paper'
}


def best_harness_for(row_id):
    """Returns the BEST_HARNESS classification for a REGISTRY row id, or None if this
    manual map hasn't been extended to cover it yet (e.g. a new row added since 2026-08-13)."""
    return BEST_HARNESS.get(row_id)


STATUS_ORDER = {
    'deviation-unexplained': 0, 'not-instrumented': 1, 'wired-never-fired': 1,
    'live-attempt-failed': 1, 'dry_run-attempt-failed': 1, 'paper-attempt-failed': 1,
    'structural-gap': 1.5, 'paper-only': 2, 'dry_run-only': 3, 'offline-only': 4,
    'not-prod-required': 4, 'verified-live': 5,
}

MODES = ('paper', 'dry_run', 'live')


def compute_mode_statuses(row):
    """Returns {mode: (status_key, detail)} for mode in ('paper','dry_run','live')
    -- the per-mode breakdown compute_status() deliberately collapses into one
    overall status (priority live > dry_run > paper), which hides real gaps: a
    scenario with genuine live events but zero paper/dry_run ones reads as fully
    green with no way to eyeball which modes actually have evidence (raised by
    the user, 2026-07-26). status_key is one of: 'not-applicable' (mechanism has
    no per-mode meaning, or this REGISTRY row's mode_filter excludes this mode by
    design), 'wired-never-fired', 'deviation-unexplained', 'attempt-failed',
    'verified'."""
    mech = row['check_mechanism']

    if mech in ('none', 'offline_only'):
        label = 'not-instrumented' if mech == 'none' else 'offline-only'
        return {m: (label, '') for m in MODES}

    if mech == 'open_price_quality_log':
        # Not mode-scoped at all (the log table has no mode column) -- mirror
        # the one overall result across all three columns rather than guessing
        # a split that doesn't exist in the data.
        status, detail = compute_status(row)
        return {m: (status, detail) for m in MODES}

    if mech == 'scenario_expectations':
        result = {}
        with db._conn() as c:
            for m in MODES:
                unexplained = c.execute(
                    "SELECT COUNT(*) n, MAX(ts) last_ts FROM coverage_deviations "
                    "WHERE scenario_key = ? AND mode = ? AND reason IS NULL",
                    (row['scenario_key'], m)).fetchone()
                if unexplained['n'] > 0:
                    result[m] = ('deviation-unexplained',
                                 f"{unexplained['n']}x unexplained, last {unexplained['last_ts']}")
                    continue
                # Same fix as compute_status's identical query above (2026-08-13):
                # only 'Auto-resolved:' is genuine proof -- 'Auto-verified: ... never
                # crossed entry threshold' also carries reason_by='system' but means
                # the opposite (explains an absence, not a success).
                system_resolved = c.execute(
                    "SELECT COUNT(*) n, MAX(ts) last_ts FROM coverage_deviations "
                    "WHERE scenario_key = ? AND mode = ? AND reason_by = 'system' "
                    "AND reason LIKE 'Auto-resolved:%'",
                    (row['scenario_key'], m)).fetchone()
                if system_resolved['n'] > 0:
                    result[m] = ('verified',
                                 f"{system_resolved['n']}x auto-resolved, last {system_resolved['last_ts']}")
                    continue
                any_row = c.execute(
                    "SELECT COUNT(*) n FROM coverage_deviations WHERE scenario_key = ? AND mode = ?",
                    (row['scenario_key'], m)).fetchone()
                result[m] = ('wired-never-fired',
                             "Only human-explained deviation(s), doesn't prove current correctness."
                             if any_row['n'] > 0 else "No deviation history for this mode.")
        return result

    # mech == 'coverage_events'
    mode_filter = row.get('mode_filter')
    bad_results = set(row.get('bad_results', []))
    with db._conn() as c:
        rows = c.execute(
            "SELECT mode, result, COUNT(*) n, MAX(ts) last_ts FROM coverage_events "
            "WHERE scenario_key = ? GROUP BY mode, result", (row['scenario_key'],)
        ).fetchall()
    by_mode = {}
    for r in rows:
        m = by_mode.setdefault(r['mode'], {'n': 0, 'last_ts': None, 'good_n': 0})
        m['n'] += r['n']
        m['last_ts'] = max(filter(None, [m['last_ts'], r['last_ts']]), default=None)
        if r['result'] not in bad_results:
            m['good_n'] += r['n']

    result = {}
    for m in MODES:
        if mode_filter and m != mode_filter:
            result[m] = ('not-applicable', "This scenario is scoped to a different mode by design.")
            continue
        if m not in by_mode:
            result[m] = ('wired-never-fired', 'No real event logged for this mode.')
            continue
        info = by_mode[m]
        if info['good_n'] > 0:
            result[m] = ('verified', f"{info['n']}x, last {info['last_ts']}")
        else:
            result[m] = ('attempt-failed',
                          f"{info['n']}x, all bad_results ({bad_results}), last {info['last_ts']}")
    return result

if __name__ == '__main__':
    rows = []
    for r in REGISTRY:
        status, detail = compute_status(r)
        proof, _ = offline_proof_for(r.get('scenario_key'), r.get('mode_filter'))
        fake_venue, _ = fake_venue_proof_for(r.get('scenario_key'))
        rows.append((STATUS_ORDER[status], status, r['id'], detail, proof, fake_venue))
    rows.sort()
    for _, status, rid, detail, proof, fake_venue in rows:
        print(f"{status:18s} {proof:15s} {fake_venue:15s} {rid:35s} {detail}")
    counts = {}
    for _, status, _, _, _, _ in rows:
        counts[status] = counts.get(status, 0) + 1
    print(f"\n{len(REGISTRY)} rows total: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    proof_counts = {}
    for _, _, _, _, proof, _ in rows:
        proof_counts[proof] = proof_counts.get(proof, 0) + 1
    print("offline_proof: " + ", ".join(f"{k}={v}" for k, v in proof_counts.items()))
    fake_venue_counts = {}
    for _, _, _, _, _, fake_venue in rows:
        fake_venue_counts[fake_venue] = fake_venue_counts.get(fake_venue, 0) + 1
    print("fake_venue_proof: " + ", ".join(f"{k}={v}" for k, v in fake_venue_counts.items()))
