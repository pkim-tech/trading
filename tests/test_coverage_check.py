"""Tests for signals_db's scenario_expectations/coverage_deviations plumbing
and scripts/coverage_check.py's checker logic -- the 2026-07-24 coverage-
system "compass": structured expected-vs-actual tracking with mandatory
reason-on-deviation, replacing prose in deep_backlog.md/live_test_coverage.md."""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db
from scripts.coverage_check import _check_trade_lifecycle

TICKER = 'TEST_CANARY'


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    db.ensure_tables()
    yield db
    os.unlink(tmp_db.name)


def test_add_scenario_expectation_roundtrip(isolated_db):
    db.add_scenario_expectation(
        scenario_key='sk_a', expected_outcome='full happy path', expected_frequency='daily',
        check_method='trade_lifecycle', ticker=TICKER, strategy_type='TrailingBothZScoreBreakout',
        check_params='{"expect_exit_reason": ["WIN", "LOSS"]}',
    )
    rows = db.get_scenario_expectations(expected_frequency='daily')
    assert len(rows) == 1
    assert rows[0]['scenario_key'] == 'sk_a'
    assert rows[0]['ticker'] == TICKER


def test_add_scenario_expectation_upserts_on_conflict(isolated_db):
    db.add_scenario_expectation('sk_a', 'v1', 'daily', 'trade_lifecycle', ticker=TICKER)
    db.add_scenario_expectation('sk_a', 'v2', 'daily', 'trade_lifecycle', ticker=TICKER)
    rows = db.get_scenario_expectations(expected_frequency='daily')
    assert len(rows) == 1
    assert rows[0]['expected_outcome'] == 'v2'


def test_add_scenario_expectation_upserts_when_ticker_is_none(isolated_db):
    """Same bug class as add_node's take_profit=NULL dedup failure -- NULL never
    conflicts with NULL under a UNIQUE constraint, so a ticker-less (control-site)
    scenario must not rely on ticker=NULL for the upsert to actually fire."""
    db.add_scenario_expectation('sk_no_ticker', 'v1', 'daily', 'coverage_event', ticker=None)
    db.add_scenario_expectation('sk_no_ticker', 'v2', 'daily', 'coverage_event', ticker=None)
    rows = db.get_scenario_expectations(expected_frequency='daily')
    assert len(rows) == 1
    assert rows[0]['expected_outcome'] == 'v2'


def test_record_deviation_upserts_when_ticker_is_none(isolated_db):
    db.record_deviation('2026-07-24', 'sk_no_ticker', 'expected', 'got A', ticker=None)
    db.record_deviation('2026-07-24', 'sk_no_ticker', 'expected', 'got B', ticker=None)
    rows = db.get_deviations()
    assert len(rows) == 1
    assert rows[0]['actual_summary'] == 'got B'


def test_record_deviation_defaults_reason_null(isolated_db):
    db.record_deviation('2026-07-24', 'sk_a', 'expected X', 'got Y', ticker=TICKER)
    unexplained = db.get_deviations(unexplained_only=True)
    assert len(unexplained) == 1
    assert unexplained[0]['reason'] is None


def test_explain_deviation_clears_unexplained_filter(isolated_db):
    db.record_deviation('2026-07-24', 'sk_a', 'expected X', 'got Y', ticker=TICKER)
    dev_id = db.get_deviations()[0]['id']
    db.explain_deviation(dev_id, 'known transient issue')
    assert db.get_deviations(unexplained_only=True) == []
    row = db.get_deviations()[0]
    assert row['reason'] == 'known transient issue'
    assert row['reason_by'] == 'user'


def test_record_deviation_rerun_preserves_existing_reason(isolated_db):
    """Re-running the daily check shouldn't clobber a reason someone already
    attached -- only actual_summary/ts should refresh."""
    db.record_deviation('2026-07-24', 'sk_a', 'expected X', 'got Y', ticker=TICKER)
    dev_id = db.get_deviations()[0]['id']
    db.explain_deviation(dev_id, 'already explained')
    db.record_deviation('2026-07-24', 'sk_a', 'expected X', 'got Y again', ticker=TICKER)
    row = db.get_deviations()[0]
    assert row['reason'] == 'already explained'
    assert row['actual_summary'] == 'got Y again'


def _add_closed_trade(exit_reason, entry_time, exit_time):
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'canary', window=5,
                take_profit=0.1, stop_loss=0, max_hold_hours=48)
    n = [x for x in db.get_watchlist() if x['ticker'] == TICKER][0]
    db.open_position(n, signal_price=100.0, signal_time=entry_time, entry_price=101.0,
                      entry_time=entry_time, shares=10)
    pos = db.get_open_position(TICKER)
    db.close_position(pos['id'], exit_signal_price=105.0, exit_price=105.0, exit_time=exit_time,
                       exit_reason=exit_reason)


def test_check_trade_lifecycle_met_when_exit_reason_matches(isolated_db):
    today = datetime.now()
    _add_closed_trade('WIN', today, today)
    check_date = today.date().isoformat()
    scenario = dict(ticker=TICKER, check_params='{"expect_exit_reason": ["WIN", "LOSS"]}')
    met, summary = _check_trade_lifecycle(scenario, check_date)
    assert met is True
    assert 'WIN' in summary


def test_check_trade_lifecycle_not_met_on_wrong_exit_reason(isolated_db):
    today = datetime.now()
    _add_closed_trade('SL', today, today)
    check_date = today.date().isoformat()
    scenario = dict(ticker=TICKER, check_params='{"expect_exit_reason": ["WIN", "LOSS"]}')
    met, summary = _check_trade_lifecycle(scenario, check_date)
    assert met is False
    assert 'SL' in summary


def test_check_trade_lifecycle_not_met_when_no_trade_closed(isolated_db):
    scenario = dict(ticker=TICKER, check_params='{"expect_exit_reason": ["TIME"]}')
    met, summary = _check_trade_lifecycle(scenario, '2026-07-24')
    assert met is False
    assert 'no closed trade' in summary


def test_check_trade_lifecycle_pending_carryover_met_by_pending_row(isolated_db):
    now = datetime.now()
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'canary', window=5, take_profit=0.1,
                stop_loss=0, max_hold_hours=48)
    n = [x for x in db.get_watchlist() if x['ticker'] == TICKER][0]
    sig = dict(current_price=100.0, last_bar=now)
    db.add_pending_buy(n, sig, channel='C123', ts='123.456')
    check_date = now.date().isoformat()
    scenario = dict(ticker=TICKER, check_params='{"expect_pending_carryover": true}')
    met, summary = _check_trade_lifecycle(scenario, check_date)
    assert met is True
    assert 'pending_buys' in summary


def test_check_trade_lifecycle_pending_carryover_not_met_when_nothing_happened(isolated_db):
    scenario = dict(ticker=TICKER, check_params='{"expect_pending_carryover": true}')
    met, summary = _check_trade_lifecycle(scenario, '2026-07-24')
    assert met is False


def test_check_trade_lifecycle_scopes_to_node_not_just_ticker(isolated_db):
    """Direct regression for the Opus-caught GDXU bug: two real nodes can share
    a ticker (differing account/version/window), and a scenario_expectations
    row scoped to one node's node_id must not be satisfied by the OTHER
    node's trade -- ticker alone is not enough."""
    today = datetime.now()
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', window=10,
                take_profit=5.0, stop_loss=1, max_hold_hours=48)
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'soxl_test', window=20,
                take_profit=5.0, stop_loss=1, max_hold_hours=48)
    node_a, node_b = [n for n in db.get_watchlist() if n['ticker'] == TICKER]
    _set_account(node_a['id'], 'ira')
    _set_account(node_b['id'], 'soxl_ira')
    node_a = db.get_watch_list_node_by_id(node_a['id'])  # re-fetch: account was stale in-memory
    node_b = db.get_watch_list_node_by_id(node_b['id'])

    # Only node_a's shape has a real closed trade today.
    with db._conn() as c:
        c.execute("""
            INSERT INTO trade_log (ticker, strategy, version, window, stop_loss, max_hold_hours,
                                    signal_price, signal_time, entry_price, entry_time, entry_drift_pct,
                                    exit_signal_price, exit_price, exit_time, exit_reason, account)
            VALUES (?, ?, ?, ?, 1, 48, 100, ?, 101, ?, 0, 105, 105, ?, 'WIN', ?)
        """, (TICKER, node_a['strategy'], node_a['version'], node_a['window'],
              today.isoformat(), today.isoformat(), today.isoformat(), node_a['account']))
        c.commit()

    check_date = today.date().isoformat()
    scenario_a = dict(ticker=TICKER, node_id=node_a['id'],
                       check_params='{"expect_exit_reason": ["WIN", "LOSS"]}')
    scenario_b = dict(ticker=TICKER, node_id=node_b['id'],
                       check_params='{"expect_exit_reason": ["WIN", "LOSS"]}')

    met_a, _ = _check_trade_lifecycle(scenario_a, check_date)
    met_b, summary_b = _check_trade_lifecycle(scenario_b, check_date)
    assert met_a is True
    # Without node scoping, node_b would have falsely inherited node_a's trade.
    assert met_b is False
    assert 'no closed trade' in summary_b


# ---------------------------------------------------------------------------
# node_id / mode identity -- 2026-07-24 late-night migration off ticker-only
# identity (proven ambiguous: two distinct watch_list nodes can share a
# ticker, the same shape as the add_node take_profit=NULL dedup bug).
# ---------------------------------------------------------------------------

def _set_account(node_id, account):
    """No signals_db setter exists for watch_list.account (it's populated by a
    one-off backfill script in production) -- direct SQL is the correct tool
    here, same as the account backfill itself."""
    with db._conn() as c:
        c.execute("UPDATE watch_list SET account = ? WHERE id = ?", (account, node_id))
        c.commit()


def _add_two_nodes_same_ticker():
    """Two real, distinct TrailingBoth nodes sharing a ticker but differing in
    arm_sell_pct (take_profit's real meaning for this strategy) -- the exact
    shape that broke the old ticker-only dedup."""
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', window=5,
                take_profit=5.0, stop_loss=1, max_hold_hours=48)
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', window=5,
                take_profit=10.0, stop_loss=1, max_hold_hours=48)
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER]


def test_get_watch_list_node_resolves_unique_match(isolated_db):
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', window=5,
                take_profit=5.0, stop_loss=1, max_hold_hours=48)
    node = [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]
    _set_account(node['id'], 'ira')
    found = db.get_watch_list_node(ticker=TICKER, account='ira')
    assert found is not None
    assert found['ticker'] == TICKER


def test_get_watch_list_node_returns_none_when_ambiguous(isolated_db):
    nodes = _add_two_nodes_same_ticker()
    assert len(nodes) == 2  # both real, distinct rows -- ticker alone can't tell them apart
    node = db.get_watch_list_node(ticker=TICKER)
    assert node is None


def test_get_watch_list_node_disambiguates_with_extra_filters(isolated_db):
    """strategy/window alone can't disambiguate the two nodes (both share
    them) -- account (or version) can, since production nodes sharing a
    ticker are always split across at least one of those, e.g. the real
    GDXU case (ira/v5/w10 vs soxl_ira/soxl_test/w20) that motivated this fix."""
    nodes = _add_two_nodes_same_ticker()
    still_ambiguous = db.get_watch_list_node(ticker=TICKER, window=5, strategy='TrailingBothZScoreBreakout')
    assert still_ambiguous is None

    lo_arm, hi_arm = sorted(nodes, key=lambda n: n['arm_sell_pct'])
    _set_account(lo_arm['id'], 'roth')
    _set_account(hi_arm['id'], 'ira')

    found_lo = db.get_watch_list_node(ticker=TICKER, account='roth')
    found_hi = db.get_watch_list_node(ticker=TICKER, account='ira')
    assert found_lo['id'] == lo_arm['id']
    assert found_hi['id'] == hi_arm['id']
    assert found_lo['id'] != found_hi['id']


def test_get_watch_list_node_returns_none_for_unknown_ticker(isolated_db):
    assert db.get_watch_list_node(ticker='NOPE') is None


def test_get_watch_list_node_scopes_to_active_watchlist(isolated_db):
    """A ticker+account pair that's unique on an old/inactive watchlist must not
    silently resolve there once a newer watchlist has superseded it -- found by
    Opus review, 2026-07-24 (AGQ/brokerage falsely resolved to an archived
    watchlist-7 v3.26 row before this filter existed)."""
    active_id = db.get_active_watchlist_id()
    old_id = db.create_watchlist('old_archived')
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v3', window=5,
                take_profit=5.0, stop_loss=1, max_hold_hours=48, watchlist_id=old_id)
    old_node = [n for n in db.get_watchlist(old_id) if n['ticker'] == TICKER][0]
    _set_account(old_node['id'], 'ira')
    # not on the active watchlist at all -- must not resolve by default
    assert db.get_watch_list_node(ticker=TICKER, account='ira') is None
    # explicit opt-out still finds it
    found = db.get_watch_list_node(ticker=TICKER, account='ira', watchlist_id=False)
    assert found is not None
    assert found['watchlist_id'] == old_id
    assert found['watchlist_id'] != active_id


def test_add_scenario_expectation_two_nodes_same_ticker_both_persist(isolated_db):
    """The real bug this migration fixes: node_id must be part of the dedup key,
    not just ticker -- two distinct nodes sharing a ticker must not collapse to
    one scenario_expectations row."""
    db.add_scenario_expectation('sk_a', 'expected for node 1', 'daily', 'trade_lifecycle',
                                 ticker=TICKER, node_id=1)
    db.add_scenario_expectation('sk_a', 'expected for node 2', 'daily', 'trade_lifecycle',
                                 ticker=TICKER, node_id=2)
    rows = db.get_scenario_expectations(expected_frequency='daily')
    assert len(rows) == 2
    assert {r['node_id'] for r in rows} == {1, 2}


def test_add_scenario_expectation_upserts_on_same_node_id(isolated_db):
    db.add_scenario_expectation('sk_a', 'v1', 'daily', 'trade_lifecycle', ticker=TICKER, node_id=1)
    db.add_scenario_expectation('sk_a', 'v2', 'daily', 'trade_lifecycle', ticker=TICKER, node_id=1)
    rows = db.get_scenario_expectations(expected_frequency='daily')
    assert len(rows) == 1
    assert rows[0]['expected_outcome'] == 'v2'


def test_add_scenario_expectation_mode_is_part_of_dedup_key(isolated_db):
    """Same scenario_key/node can legitimately have a different expectation per
    mode (paper/dry_run/live) -- mode=None means 'same across all modes'."""
    db.add_scenario_expectation('sk_a', 'live expectation', 'daily', 'coverage_event',
                                 ticker=TICKER, node_id=1, mode='live')
    db.add_scenario_expectation('sk_a', 'paper expectation', 'daily', 'coverage_event',
                                 ticker=TICKER, node_id=1, mode='paper')
    rows = db.get_scenario_expectations(expected_frequency='daily')
    assert len(rows) == 2
    assert {r['mode'] for r in rows} == {'live', 'paper'}


def test_record_deviation_dedups_on_node_id_and_mode(isolated_db):
    db.record_deviation('2026-07-24', 'sk_a', 'expected', 'got A', ticker=TICKER, node_id=1, mode='live')
    db.record_deviation('2026-07-24', 'sk_a', 'expected', 'got B', ticker=TICKER, node_id=1, mode='live')
    rows = db.get_deviations()
    assert len(rows) == 1
    assert rows[0]['actual_summary'] == 'got B'


def test_record_deviation_distinct_node_ids_do_not_collide(isolated_db):
    db.record_deviation('2026-07-24', 'sk_a', 'expected', 'got node 1', ticker=TICKER, node_id=1)
    db.record_deviation('2026-07-24', 'sk_a', 'expected', 'got node 2', ticker=TICKER, node_id=2)
    rows = db.get_deviations()
    assert len(rows) == 2


def test_log_coverage_event_stores_node_id(isolated_db):
    db.log_coverage_event('sl_placement', 'live', ticker=TICKER, node_id=42, result='placed')
    events = db.get_coverage_events(scenario_key='sl_placement')
    assert len(events) == 1
    assert events[0]['node_id'] == 42


def test_pending_buy_node_json_round_trips_id(isolated_db):
    """Regression for the Opus-review-caught bug: _PENDING_BUY_NODE_KEYS didn't
    include 'id', so every node resolved from a pending buy always had
    node.get('id') is None, silently making ~14 of the node_id call-site edits
    dead code."""
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', window=5,
                take_profit=5.0, stop_loss=1, max_hold_hours=48)
    node = [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]
    _set_account(node['id'], 'ira')
    node = db.get_watch_list_node(ticker=TICKER, account='ira')
    sig = dict(current_price=100.0, last_bar=datetime.now())
    db.add_pending_buy(node, sig, channel='C123', ts='123.456')
    pending = db.get_pending_buys()
    assert len(pending) == 1
    assert pending[0]['node']['id'] == node['id']


def test_pending_buy_node_account_stays_pinned_to_signal_time_snapshot(isolated_db):
    """signals_notify no longer refreshes a pending buy's node from watch_list
    at all (the round-3 `_fresh_node` helper was removed 2026-07-25 -- proved
    to be a pure no-op once account-refreshing was reverted, see
    docs/backlog_cache.md's round 6 entry) -- callers read `pending['node']`
    directly. This is a regression guard for that design: an already-placed
    order's real account is fixed at broker-placement time and must NOT
    silently follow a later watch_list reassignment (the real IVV incident)."""
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', window=5,
                take_profit=5.0, stop_loss=1, max_hold_hours=48)
    node = [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]
    _set_account(node['id'], 'brokerage')
    fresh_node = db.get_watch_list_node(ticker=TICKER, account='brokerage')
    sig = dict(current_price=100.0, last_bar=datetime.now())
    db.add_pending_buy(fresh_node, sig, channel='C123', ts='123.456')
    # Real node's account is reassigned AFTER the pending buy was recorded --
    # simulates the real IVV incident.
    _set_account(node['id'], 'ira')

    pending = db.get_pending_buys()[0]
    assert pending['node']['account'] == 'brokerage'  # pinned, NOT 'ira'
