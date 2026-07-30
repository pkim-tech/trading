"""Seeds signals_db.scenario_expectations with 'coverage_event' rows for the
Trade-Flow Accountability Grid entries that should fire at least once on any
normal trading day, given the current watchlist (6 ira canaries + soxl_ira's
real live nodes + UDOW's known research-mode-with-open-position state) --
distinct from the grid's opportunistic/real-condition-dependent rows (gap
resizes, partial fills, order rejections, etc.), which would just generate
daily noise if held to the same expectation.

Pulls scenario_key/check_mechanism/bad_results/mode_filter straight from
scripts/coverage_registry.py's REGISTRY (never hand-duplicated) so a row's
daily-check definition can't drift out of sync with its REGISTRY entry.
Re-run is safe: add_scenario_expectation upserts on (scenario_key, node_id,
ticker, mode).

Run once (or after the daily-expected set changes):
  .venv/bin/python scripts/seed_daily_coverage_expectations.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db
from scripts.coverage_registry import REGISTRY

# Registry ids expected to log a good-outcome coverage_event on essentially
# every trading day given today's watchlist shape. Deliberately excludes:
# rows already covered by a 'trade_lifecycle' scenario_expectations entry
# (pinned_entry_trigger, market_buy_placement -- see seed_scenario_
# expectations.py), open_price_quality (its own check_mechanism, not
# coverage_events), and every genuinely opportunistic/real-condition-
# dependent row (gap_resize, top_up, sl_async_fallback, dup_order/-sell
# guards, kill_switch_block, node_level_automation_pause, manual-Slack-tap
# rows, etc.) -- those would just manufacture daily noise if held to a daily
# expectation. See docs/backlog_cache.md's accountability-grid-vs-canary
# mapping discussion for the full reachability classification.
#
# Split into DAILY (checked every real trading day, deviation = a sticky
# ticket needing a human) vs OCCASIONAL (seeded/visible so the row exists in
# scenario_expectations for future promotion, but NOT auto-checked by
# run_check's expected_frequency='daily' filter -- no ticket minted just
# because a trade-conditional event didn't happen to fire that day). This
# split replaces an earlier version of this script that put all 14 ids in
# DAILY_EXPECTED_IDS -- caught by Opus review (this session) replaying the
# real check logic against the last 4 actual trading days: 11-14 deviations
# every single day, because 8 of the 14 have ZERO all-time coverage_events
# (they require a real fill/arm/sell-signal/TIME-exit that doesn't occur on
# every no-signal day) and a 9th (sl_placement) has exactly 1 event, itself a
# bad_result.
#
# Only live_state_reconciliation_mismatch is empirically daily-reliable
# today. A second session-wrap Opus review pass replayed the real 3-day
# coverage_events history and found cash_check fired on only 1 of 3 days --
# it's logged only inside schwab_safety.check_order's real-BUY-attempt
# branch, so "every real BUY attempt" IS the trade-conditional part, same
# class as the 12 rows below, not daily-reliable just because it's a guard.
# Demoted from an earlier version of this script that had it in
# DAILY_EXPECTED_IDS on that wrong premise.
#
# Fixed 2026-07-30: the caveat below came true -- UDOW's stale test position
# (the sole reason live_state_reconciliation_mismatch was empirically daily)
# was retroactively closed 2026-07-28 evening, and it produced zero
# coverage_events on 2026-07-29, correctly, since there's no longer a daily
# mismatch generator. Demoted from DAILY_EXPECTED_IDS -- seeded below with
# freq='informational' (still checked/printed every day, just never mints a
# ticket) instead of OCCASIONAL_IDS (which wouldn't be checked/shown at all)
# since the user wants to keep seeing this row's status daily.
#
# CAVEAT (historical, now resolved): live_state_reconciliation_mismatch's own
# "daily" reliability was driven entirely by UDOW's deliberately-seeded stale
# fake position (the known accepted signals_invariants violation) -- the day
# that test position was cleaned up, this row became trade-conditional too
# and needed demoting the same way.
#
# The rest stay tracked by the Trade-Flow Accountability Grid's all-time
# compute_status() (pages/14_Coverage.py) instead -- "has this ever worked,"
# a broader question than "did it happen today." Promote a row to
# DAILY_EXPECTED_IDS once its real coverage_events history (check
# scripts/coverage_matrix.py, don't assume from the code path alone -- that's
# exactly the mistake that put cash_check here originally) shows it firing
# reliably.
DAILY_EXPECTED_IDS = []

# Checked and printed every day like DAILY_EXPECTED_IDS, but a miss never
# mints a coverage_deviations ticket -- for rows whose own trigger condition
# is trade-conditional, so the user still wants daily visibility without a
# false ticket on a day the condition simply didn't fire.
INFORMATIONAL_IDS = [
    'live_state_reconciliation_mismatch',
]

OCCASIONAL_IDS = [
    'sl_sync_placement',
    'cash_check',
    'automated_sell_mode_skip',
    'trailing_arm_reread',
    'dry_run_buy_synthesis',
    'dry_run_sim_close',
    'paper_entry_fill',
    'paper_exit_fill',
    'automated_sell_execution',
    'time_exit_trigger',
    'buy_fill_reconciled',
    'morning_report_delivery',
    'exit_arm_latency',
]

if __name__ == '__main__':
    db.ensure_tables()
    by_id = {r['id']: r for r in REGISTRY}
    for freq, ids in (('daily', DAILY_EXPECTED_IDS), ('informational', INFORMATIONAL_IDS), ('occasional', OCCASIONAL_IDS)):
        for rid in ids:
            row = by_id[rid]
            assert row['check_mechanism'] == 'coverage_events', (
                f"{rid} is check_mechanism={row['check_mechanism']!r}, not 'coverage_events' -- "
                f"this seed script only supports coverage_event-mechanism rows")
            # mode is a real dedup-key column here, not stuffed into check_params --
            # entry_fill/exit_fill are logged under two different modes (paper vs
            # dry_run) for two different registry rows, and without mode in the
            # dedup key the second add_scenario_expectation call would silently
            # overwrite the first (confirmed: caught this exact collision on the
            # first real run of this script).
            check_params = json.dumps({'bad_results': row['bad_results']}) if row.get('bad_results') else '{}'
            db.add_scenario_expectation(
                scenario_key=row['scenario_key'],
                expected_outcome=f"At least one good-outcome coverage_event ({freq}) ({row['scenario']})",
                expected_frequency=freq, check_method='coverage_event',
                ticker=None, node_id=None, mode=row.get('mode_filter'),
                strategy_type=None, check_params=check_params,
            )
            print(f"seeded {row['scenario_key']:30s} mode={row.get('mode_filter')!r:8s} freq={freq:10s} <- {rid}")
