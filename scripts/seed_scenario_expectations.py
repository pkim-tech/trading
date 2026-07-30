"""Seeds signals_db.scenario_expectations with the structured designed-scenario
mapping for the six canary nodes -- previously only prose in deep_backlog.md's
2026-07-23 entry. Piece #3 of the 2026-07-24 coverage-system reframe: turn
"what should this node do" into real, queryable data instead of something a
human has to remember and cross-reference by hand.

Tickers reflect the live watchlist as of 2026-07-24 (canary A was originally
SPY, renamed to IVV during today's account-collision fix -- see
docs/backlog_cache.md). Re-run is safe: add_scenario_expectation upserts on
(scenario_key, ticker).

Run once (or after any canary redesign): .venv/bin/python scripts/seed_scenario_expectations.py

Fixed 2026-07-29: expect_exit_reason lists used to say ["WIN", "LOSS"] for the
trailing-stop-exit canaries -- those are strategies.check_exit's *internal*
labels, but signals_compute.py collapses both into 'TRAIL' before it's ever
written to trade_log.exit_reason (reason in ('WIN','LOSS') -> 'TRAIL'). The
real column value is always 'TRAIL', never 'WIN'/'LOSS', so these three
scenarios could never pass as originally written -- caught via 3 real
unexplained coverage_deviations (ids 41-43, 2026-07-28).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

SCENARIOS = [
    dict(
        scenario_key='canary_full_lifecycle', ticker='IVV', strategy_type='TrailingBothZScoreBreakout',
        expected_outcome="Full happy path same day: entry -> bounce-fill -> arm -> trailing-sell "
                          "(fixed_sl=30% unreachable, arm/trail thresholds hair-trigger at 0.1%).",
        expected_frequency='daily', check_method='trade_lifecycle',
        check_params='{"expect_exit_reason": ["TRAIL"]}',
    ),
    dict(
        scenario_key='canary_early_sl', ticker='QQQ', strategy_type='TrailingBothZScoreBreakout',
        expected_outcome="Early-SL path same day: entry -> bounce-fill -> immediate SL "
                          "(arm=10% unreachable, fixed_sl=0.1% hair-trigger).",
        expected_frequency='daily', check_method='trade_lifecycle',
        check_params='{"expect_exit_reason": ["SL"]}',
    ),
    dict(
        scenario_key='canary_pinned_entry', ticker='IWM', strategy_type='TrailingBothZScoreBreakout',
        expected_outcome="Same shape as canary_full_lifecycle but entry_timing='open_check' -- "
                          "exercises _scan_pinned_entry + open_price_quality_log, not the bar-close "
                          "entry path. Entry-mechanism itself is not separately verified by this "
                          "check (known MVP limitation) -- only that a same-day trade closed.",
        expected_frequency='daily', check_method='trade_lifecycle',
        check_params='{"expect_exit_reason": ["TRAIL"]}',
    ),
    dict(
        scenario_key='canary_overnight_carry', ticker='DIA', strategy_type='TrailingBothZScoreBreakout',
        expected_outcome="trail_buy_pct=5.0% is wide enough that the bounce-fill is unlikely to "
                          "complete same day -- expect a pending trailing-buy still resting "
                          "(pending_buys row) at end of day, carried into tomorrow's open, as a "
                          "daily regression check for the 2026-07-22 stale-cache-at-open fix. A "
                          "same-day fill isn't itself wrong, just not the scenario this canary is "
                          "designed to exercise -- either a pending row or a closed trade counts as "
                          "'something real happened,' but only the former matches the design intent.",
        expected_frequency='daily', check_method='trade_lifecycle',
        check_params='{"expect_pending_carryover": true}',
    ),
    dict(
        scenario_key='canary_market_buy_exit', ticker='VOO', strategy_type='TrailingExitZScoreBreakout',
        expected_outcome="TrailingExitZScoreBreakout (not TrailingBoth) -- entry should be an "
                          "immediate market buy (no pending_buys row, _is_trailing_buy routes off "
                          "the strategy's axis schema), then a normal same-day exit. Entry mechanism "
                          "itself not separately verified by this check (known MVP limitation) -- "
                          "only that a same-day trade closed.",
        expected_frequency='daily', check_method='trade_lifecycle',
        check_params='{"expect_exit_reason": ["SL", "TIME", "TRAIL"]}',
    ),
    dict(
        scenario_key='canary_time_exit', ticker='XLF', strategy_type='TrailingBothZScoreBreakout',
        expected_outcome="Arm (take_profit=50%) and SL both practically unreachable, "
                          "max_hold_hours=2 -- the only exit path left open is TIME.",
        expected_frequency='daily', check_method='trade_lifecycle',
        check_params='{"expect_exit_reason": ["TIME"]}',
    ),
]

if __name__ == '__main__':
    db.ensure_tables()
    for s in SCENARIOS:
        node = db.get_watch_list_node(ticker=s['ticker'], version='canary')
        node_id = node['id'] if node else None
        if node_id is None:
            # get_watch_list_node fails open (returns None on ambiguity/error, not
            # just "not found") -- since node_id is part of this row's dedup key,
            # a transient failure here would duplicate the row on rerun instead of
            # upserting it. Flag loudly rather than silently seeding node_id=None.
            print(f"  WARNING: could not resolve a node_id for {s['ticker']} (canary) -- "
                  f"check watch_list before trusting this seed")
        db.add_scenario_expectation(
            scenario_key=s['scenario_key'], expected_outcome=s['expected_outcome'],
            expected_frequency=s['expected_frequency'], check_method=s['check_method'],
            ticker=s['ticker'], node_id=node_id, mode='live',
            strategy_type=s['strategy_type'], check_params=s['check_params'],
        )
        print(f"seeded {s['scenario_key']:28s} {s['ticker']:5s} node_id={node_id}")
    print(f"\n{len(SCENARIOS)} scenario_expectations rows seeded/updated.")
