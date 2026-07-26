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
  'none'                  -- no code hook exists at all yet; can't be derived, only
                             a manual TODO until something is built to log it.

Run directly for a plain-text report: .venv/bin/python scripts/coverage_registry.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

DB_PATH = "./cache/live/trading_live.db"

REGISTRY = [
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
    dict(id='post_fill_topup',
         scenario="Post-fill top-up places a real order",
         code_path="_reconcile_fill",
         offline_coverage="test_part3_gap_resize.py; live_sim_harness.py::scenario_reconcile_fill_topup",
         check_mechanism='coverage_events', scenario_key='top_up',
         bad_results=['blocked', 'failed_unexpectedly', 'db_update_failed_after_real_order',
                      'overspent_no_corrective_sell'],
         notes="2026-07-24 real attempt was blocked by the pre-fix daily_order_cap bug (now fixed) -- "
               "real-money risk open until a real successful top-up is observed post-fix."),
    dict(id='gap_resize',
         scenario="Overnight gap-resize (cancel trailing buy, replace w/ market)",
         code_path="signals_notify.check_gap_resize, _GAP_CHECK_WINDOW",
         offline_coverage="Unit tests; live_sim_harness.py::scenario_gap_resize",
         check_mechanism='coverage_events', scenario_key='gap_resize',
         bad_results=['no_account', 'price_lookup_failed', 'cancel_failed', 'cancel_unconfirmed',
                      'blocked', 'rejected'],
         notes="Needs a real overnight gap past trail_buy_pct while daemon is live."),
    dict(id='exit_arm_latency',
         scenario="Exit-arm latency scan (pinned)",
         code_path="_scan_pinned_exit_arm",
         offline_coverage="live_sim_harness.py::scenario_pinned_exit_arm",
         check_mechanism='none', scenario_key=None,
         notes="No dedicated coverage_events hook exists for this scan itself."),
    dict(id='kernel_fill_parity',
         scenario="Trailing-buy/-stop fill resolution parity vs backtest kernel",
         code_path="kernel + live sizing",
         offline_coverage="verify_trailing_buy_resolution.py / verify_trailing_sell_resolution.py",
         check_mechanism='offline_only', scenario_key=None,
         notes="Offline-only by design -- rerun after any kernel/signals_notify/schwab_safety change."),
    dict(id='open_price_quality',
         scenario="Open-price quality (real open vs. what live code captured)",
         code_path="_scan_pinned_entry logging -> open_price_quality_log",
         offline_coverage="scripts/verify_open_price_quality.py (script exists, no data yet)",
         check_mechanism='none', scenario_key=None,
         notes="Needs real trading-day data in open_price_quality_log, can't be backfilled."),
    dict(id='cash_check',
         scenario="Cash-balance check blocks an order correctly",
         code_path="schwab_client.get_account_balance + schwab_safety.check_order",
         offline_coverage="8 unit tests (test_schwab_safety.py), Opus-reviewed 2026-07-21",
         check_mechanism='coverage_events', scenario_key='cash_check',
         notes="Passing path verified live+dry_run 2026-07-24. Blocking case (insufficient funds) never "
               "observed -- every real event so far shows result=passed."),
    dict(id='second_ticker_one_account',
         scenario="Second-live-ticker-in-one-account BUY correctly blocked",
         code_path="schwab_safety._has_open_buy_order_in_account",
         offline_coverage="3 unit tests (test_schwab_safety.py)",
         check_mechanism='coverage_events', scenario_key='second_ticker_buy_blocked',
         notes="Not reachable today (one ticker per account) -- will matter once account-sharing changes."),
    dict(id='daemon_exception_survival',
         scenario="Daemon survives an unhandled exception mid-loop",
         code_path="active_signals._guarded + outer try/except in run_loop",
         offline_coverage="7 unit tests (test_run_loop_fault_tolerance.py)",
         check_mechanism='coverage_events', scenario_key='daemon_section_exception',
         notes="360 real-looking rows were pytest pollution, cleaned up 2026-07-25 -- 0 real occurrences "
               "since. No test runs a real run_loop iteration end-to-end with a failing section."),
    dict(id='dup_order_no_false_block',
         scenario="Duplicate-order guard doesn't false-block a legitimate top-up",
         code_path="schwab_safety quantity-aware guard",
         offline_coverage="Unit tests (test_schwab_safety.py)",
         check_mechanism='coverage_events', scenario_key='dup_order_window_blocked',
         notes="Only exercised in unit tests so far, not against real order timing."),
    dict(id='live_state_reconciliation_design',
         scenario="Live-state reconciliation (position/order-book mismatch detection)",
         code_path="not built",
         offline_coverage="N/A",
         check_mechanism='none', scenario_key=None,
         notes="Superseded -- see live_state_reconciliation_mismatch below, which is built."),
    dict(id='automated_sell_mode_skip',
         scenario="Automated sell correctly skipped for a non-live-mode node's position",
         code_path="signals_notify._attempt_automated_sell mode check",
         offline_coverage="2 unit tests (test_schwab_automation.py)",
         check_mechanism='none', scenario_key=None,
         notes="No ticker has ever hit this scenario live (automation-scope tickers only run mode='live')."),
    dict(id='live_state_reconciliation_mismatch',
         scenario="Live-state reconciliation detects and alerts on a real mismatch",
         code_path="signals_notify.check_live_state_reconciliation, schwab_client.get_real_position",
         offline_coverage="8 unit tests (test_live_state_reconciliation.py)",
         check_mechanism='coverage_events', scenario_key='reconciliation_mismatch',
         notes="8 real soxl_ira detections 2026-07-24 (GDXU/GDXD/LABU x2/ERY x4). 1,753 dry_run rows are "
               "the known intentionally-seeded UDOW fake position -- expected noise, not a gap."),
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
               "two real exit orders."),
    dict(id='manual_sl_fallback_alert',
         scenario="Manual SL-price fallback alert fires correctly when trailing-sell placement fails "
                  "post-SL-cancel",
         code_path="signals_notify._attempt_automated_sell",
         offline_coverage="1 unit test (test_schwab_automation.py)",
         check_mechanism='none', scenario_key=None,
         notes="Deliberately no auto-recovery (user's call) -- needs a real failed placement to confirm "
               "the alert text/price are useful in practice."),
    dict(id='position_lock',
         scenario="Poll loop and Slack handler can't double-open/double-close the same position",
         code_path="signals_db._position_lock around open_position/close_position",
         offline_coverage="3 unit tests (test_db_roundtrip.py)",
         check_mechanism='none', scenario_key=None,
         notes="Race window is narrow and never observed causing a real duplicate."),
    dict(id='dup_order_retry_after_failure',
         scenario="Duplicate-order retry after a real rejected/failed order isn't wrongly blocked",
         code_path="schwab_safety._broker_confirms_order",
         offline_coverage="2 unit tests (test_schwab_safety.py)",
         check_mechanism='coverage_events', scenario_key='dup_order_retry_after_failure',
         notes="Needs a real rejected order to confirm the retry path in practice."),
    dict(id='fast_path_fill_reconciliation',
         scenario="Fast-path (websocket) fill reconciliation doesn't act on a partial/in-flight execution",
         code_path="signals_notify.drain_fill_queue (re-confirms via get_filled_order poll)",
         offline_coverage="3 unit tests (test_part3_gap_resize.py)",
         check_mechanism='coverage_events', scenario_key='fast_path_fill_reconciliation',
         notes="Needs a real multi-execution fill to confirm the poll-reconfirm path in practice."),
    dict(id='same_day_block',
         scenario="same_day_block skips correctly for margin accounts, still blocks cash accounts",
         code_path="schwab_safety.check_order (AccountLimits.account_type)",
         offline_coverage="2 unit tests (test_schwab_safety.py)",
         check_mechanism='coverage_events', scenario_key='same_day_block',
         notes="No real same-day re-buy has ever been attempted live in either account type."),
    dict(id='manual_buy_confirmation_account',
         scenario="Manual BUY confirmation (Executed/Filled/Manual Open) opens a position with the real "
                  "account, not NULL",
         code_path="signals_blocks._build_buy_blocks, signals_notify._ticker_block, "
                    "signals_handlers.handle_entry_price/handle_trail_buy_fill_price/handle_manual_open_price",
         offline_coverage="tests/test_coverage_check.py covers the node-identity plumbing, nothing exercises "
                           "the actual button->modal->handler chain end-to-end",
         check_mechanism='none', scenario_key=None,
         notes="Real bug found+fixed 2026-07-25 (both Slack button payloads omitted account/id). Not yet "
               "observed against a real Slack button click."),
    dict(id='stale_buy_button_guard',
         scenario="Stale/duplicate Executed or Filled button tap doesn't open a phantom position",
         code_path="signals_handlers.handle_entry_price/handle_trail_buy_fill_price (pending_buys-existence guard)",
         offline_coverage="None yet -- guard logic not directly unit-tested",
         check_mechanism='none', scenario_key=None,
         notes="Known latent gap: assumes every rendered Executed button has a backing pending_buys row, "
               "which breaks for a live TrailingExit ticker outside SCHWAB_AUTOMATION_TICKERS."),
    dict(id='two_nodes_same_ticker_diff_accounts',
         scenario="Two concurrent live nodes on the same ticker in different accounts can both place real orders",
         code_path="schwab_safety._live_ticker_accounts/check_order (ticker->set-of-accounts)",
         offline_coverage="Unit tests updated for the new error message, no test exercises 2 real concurrent nodes",
         check_mechanism='none', scenario_key=None,
         notes="Built as part of the wl_id refactor (2026-07-25/26) -- never yet exercised with a real "
               "second node on an already-live ticker."),
    dict(id='buy_buttons_resolve_correct_node',
         scenario="BUY-side Slack buttons resolve the correct node when 2+ nodes share a ticker",
         code_path="signals_handlers.py (6 BUY handlers match/clear pending_buys by wl_id)",
         offline_coverage="None -- no test simulates 2 concurrent pending_buys rows for the same ticker",
         check_mechanism='none', scenario_key=None,
         notes="SELL-side already did this correctly via position_id."),
    dict(id='buy_fill_reconciles_correct_node',
         scenario="A real broker BUY fill reconciles against the correct node's pending_buys row when 2+ "
                  "are pending for the same ticker",
         code_path="signals_notify._reconcile_buy_fill (wl_id param, falls back to alert if ambiguous)",
         offline_coverage="None -- no test simulates 2 concurrent real pending buys for the same ticker",
         check_mechanism='none', scenario_key=None,
         notes="drain_fill_queue's stream entry point passes its best-effort ticker+account-derived node id."),
    dict(id='node_level_automation_pause',
         scenario="Node-level automation pause blocks real orders for just that node, not sibling nodes "
                  "on the same ticker",
         code_path="schwab_safety.pause_node_automation, node_automation_enabled",
         offline_coverage="None yet",
         check_mechanism='none', scenario_key=None,
         notes="No Slack button wired to it yet (console/script-only). Known limitation: fuzzy node lookup "
               "fails open (not closed) for 2 nodes sharing both ticker AND account."),
    dict(id='oversell_guard_correct_position',
         scenario="check_order's oversell guard resolves the right position when 2 live nodes share a "
                  "ticker in different accounts",
         code_path="signals_db.get_open_position_for_account",
         offline_coverage="None yet",
         check_mechanism='none', scenario_key=None,
         notes="Found by 2nd Opus review round 2026-07-26 (ticker-only lookup could resolve wrong account's "
               "position). Fixed same session, not yet observed against a real 2-account-same-ticker SELL."),
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
    dict(id='time_exit_trigger',
         scenario="A real (non-paper/dry_run-sim) position's TIME-based exit (max_hold_hours) fires "
                  "the SELL alert",
         code_path="signals_notify.notify_sell_signal (reason=='TIME' branch)",
         offline_coverage="signals_compute.check_sell_condition has kernel-parity coverage via "
                           "kernel_fill_parity; the live alert-firing side was untested",
         check_mechanism='coverage_events', scenario_key='time_exit_trigger',
         notes="New instrumentation, 2026-07-27 evening. Only fires the alert -- doesn't confirm the "
               "position actually gets closed (that's a manual Slack-button step for a real position, "
               "same gap as manual_buy_confirmation_account)."),
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
]


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
            system_resolved = c.execute(
                "SELECT COUNT(*) n, MAX(ts) last_ts FROM coverage_deviations "
                "WHERE scenario_key = ? AND reason_by = 'system'", (row['scenario_key'],)
            ).fetchone()
            if system_resolved['n'] > 0:
                return 'verified-live', (
                    f"{system_resolved['n']}x auto-resolved (deviated then met same day), "
                    f"last {system_resolved['last_ts']}")
            any_row = c.execute(
                "SELECT COUNT(*) n FROM coverage_deviations WHERE scenario_key = ?",
                (row['scenario_key'],)
            ).fetchone()
            if any_row['n'] > 0:
                return 'wired-never-fired', (
                    "Only human-explained historical deviation(s) exist -- explains a past "
                    "failure, doesn't prove current correct behavior.")
            return 'wired-never-fired', (
                "No coverage_deviations history at all -- this mechanism can't distinguish "
                "'always passed silently' from 'daily check never actually ran.'")

    mode_filter = row.get('mode_filter')
    bad_results = set(row.get('bad_results', []))
    with db._conn() as c:
        if mode_filter:
            rows = c.execute(
                "SELECT mode, result, COUNT(*) n, MAX(ts) last_ts FROM coverage_events "
                "WHERE scenario_key = ? AND mode = ? GROUP BY mode, result",
                (row['scenario_key'], mode_filter)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT mode, result, COUNT(*) n, MAX(ts) last_ts FROM coverage_events "
                "WHERE scenario_key = ? GROUP BY mode, result", (row['scenario_key'],)
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
        return ('live-attempt-failed' if mode == 'live' else f'{mode}-attempt-failed',
                f"{m['n']}x {mode}, all bad_results ({bad_results}), last {m['last_ts']} -- "
                f"fired for real but never with a good outcome")
    return 'wired-never-fired', 'scenario_key exists in code but has never logged a real event.'


STATUS_ORDER = {
    'deviation-unexplained': 0, 'not-instrumented': 1, 'wired-never-fired': 1,
    'live-attempt-failed': 1, 'dry_run-attempt-failed': 1, 'paper-attempt-failed': 1,
    'paper-only': 2, 'dry_run-only': 3, 'offline-only': 4, 'verified-live': 5,
}

if __name__ == '__main__':
    rows = []
    for r in REGISTRY:
        status, detail = compute_status(r)
        rows.append((STATUS_ORDER[status], status, r['id'], detail))
    rows.sort()
    for _, status, rid, detail in rows:
        print(f"{status:18s} {rid:35s} {detail}")
    counts = {}
    for _, status, _, _ in rows:
        counts[status] = counts.get(status, 0) + 1
    print(f"\n{len(REGISTRY)} rows total: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
