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
from scripts.coverage_check import _check_trade_lifecycle, _check_coverage_event, run_check
from scripts.coverage_registry import compute_status

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


def test_record_deviation_clears_system_reason_on_new_deviation(isolated_db):
    """Opus review, 2026-07-27: a system-authored auto-resolution (from
    clear_deviation_if_resolved) is not testimony about a NEW failure -- if the
    scenario deviates again the same day, the stale system reason must be cleared
    back to unexplained, or the genuine new failure would silently hide behind it
    and vanish from get_deviations(unexplained_only=True)."""
    db.record_deviation('2026-07-24', 'sk_f', 'expected X', 'got Y', ticker=TICKER)
    db.clear_deviation_if_resolved('2026-07-24', 'sk_f', ticker=TICKER)
    row = db.get_deviations()[0]
    assert row['reason_by'] == 'system'

    db.record_deviation('2026-07-24', 'sk_f', 'expected X', 'got Y again -- real new failure', ticker=TICKER)
    row = db.get_deviations()[0]
    assert row['reason'] is None
    assert row['reason_by'] is None
    assert row['actual_summary'] == 'got Y again -- real new failure'
    assert len(db.get_deviations(unexplained_only=True)) == 1


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


def test_send_coverage_report_surfaces_unexplained_deviation(isolated_db, monkeypatch):
    """signals_notify.send_coverage_report is piece #7 of the coverage-system
    reframe (Slack-callable report) -- it must run the real check live (not
    just read stale coverage_deviations rows) and post the unexplained
    deviation, since that's the actionable fact per automation_principles.md
    #16's "no unexplained failure" contract."""
    import signals_notify
    captured = {}
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda text, blocks=None: (captured.setdefault('text', text), ('C1', '1'))[1])

    db.add_scenario_expectation('sk_a', 'expected happy path', 'daily', 'trade_lifecycle',
                                 ticker=TICKER, check_params='{"expect_exit_reason": ["WIN"]}')
    check_date = '2026-07-24'  # a Friday -- Saturday/Sunday are gated, see below
    signals_notify.send_coverage_report(check_date)

    assert 'UNEXPLAINED' in captured['text']
    assert 'sk_a' in captured['text']
    unexplained = db.get_deviations(unexplained_only=True)
    assert len(unexplained) == 1
    assert unexplained[0]['scenario_key'] == 'sk_a'


def test_send_coverage_report_reflects_explained_deviation(isolated_db, monkeypatch):
    import signals_notify
    captured = {}
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda text, blocks=None: (captured.setdefault('text', text), ('C1', '1'))[1])

    db.add_scenario_expectation('sk_b', 'expected happy path', 'daily', 'trade_lifecycle',
                                 ticker=TICKER, check_params='{"expect_exit_reason": ["WIN"]}')
    check_date = '2026-07-24'  # a Friday -- Saturday/Sunday are gated, see below
    signals_notify.send_coverage_report(check_date)
    dev_id = db.get_deviations()[0]['id']
    db.explain_deviation(dev_id, 'known -- no signal today')

    captured.clear()
    signals_notify.send_coverage_report(check_date)
    assert 'No unexplained deviations' in captured['text']
    assert '(explained)' in captured['text']


def test_send_coverage_report_does_not_collapse_rows_sharing_a_scenario_key(isolated_db, monkeypatch):
    """Opus review, 2026-07-25: scenario_key alone is not a unique key -- two
    active scenario_expectations rows can share one scenario_key when
    disambiguated by node_id (e.g. the same designed scenario run against two
    different nodes on purpose). Explaining one node's deviation must not mask
    the other node's still-unexplained one in the Slack report."""
    import signals_notify
    captured = {}
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda text, blocks=None: (captured.setdefault('text', text), ('C1', '1'))[1])

    db.add_scenario_expectation('sk_shared', 'expected happy path', 'daily', 'trade_lifecycle',
                                 ticker=TICKER, node_id=201, check_params='{"expect_exit_reason": ["WIN"]}')
    db.add_scenario_expectation('sk_shared', 'expected happy path', 'daily', 'trade_lifecycle',
                                 ticker=TICKER, node_id=202, check_params='{"expect_exit_reason": ["WIN"]}')
    check_date = '2026-07-24'
    signals_notify.send_coverage_report(check_date)
    assert len(db.get_deviations()) == 2

    dev_node_201 = [d for d in db.get_deviations() if d['node_id'] == 201][0]
    db.explain_deviation(dev_node_201['id'], 'known issue on node 201')

    captured.clear()
    signals_notify.send_coverage_report(check_date)
    assert ':red_circle: 1 UNEXPLAINED' in captured['text']  # node 202's deviation, not masked by node 201's


def test_send_coverage_report_refuses_weekend(isolated_db, monkeypatch):
    """Opus review, 2026-07-25: a trade_lifecycle expectation like 'a trade
    closed today' is trivially, permanently false on a day the market never
    opened -- confirmed live, this produced real unexplained deviation rows
    dated a Saturday before this gate was added. The Slack button (unlike the
    CLI tool, normally run deliberately) makes an accidental weekend tap easy."""
    import signals_notify
    captured = {}
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda text, blocks=None: (captured.setdefault('text', text), ('C1', '1'))[1])

    db.add_scenario_expectation('sk_c', 'expected happy path', 'daily', 'trade_lifecycle',
                                 ticker=TICKER, check_params='{"expect_exit_reason": ["WIN"]}')
    signals_notify.send_coverage_report('2026-07-25')  # a Saturday

    assert 'weekend' in captured['text'].lower()
    assert db.get_deviations() == []  # no deviation row manufactured


def test_send_coverage_report_clears_stale_deviation_once_met(isolated_db, monkeypatch):
    """Opus review, 2026-07-25: a scenario that deviated earlier in the day
    (no trade yet) and is genuinely met by the time of a later re-check must
    not still be reported as ✗ UNEXPLAINED -- the report contradicting the
    check it just ran. 2026-07-27: the once-stale row is no longer deleted --
    it's auto-explained and kept as permanent record (deviations are treated
    like tickets, not transient flags)."""
    import signals_notify
    captured = {}
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda text, blocks=None: (captured.setdefault('text', text), ('C1', '1'))[1])

    db.add_scenario_expectation('sk_d', 'expected happy path', 'daily', 'trade_lifecycle',
                                 ticker=TICKER, check_params='{"expect_exit_reason": ["WIN"]}')
    check_date = '2026-07-24'
    signals_notify.send_coverage_report(check_date)  # first tap: no trade yet -> deviated
    assert len(db.get_deviations()) == 1

    _add_closed_trade('WIN', datetime(2026, 7, 24), datetime(2026, 7, 24))  # trade lands later

    captured.clear()
    signals_notify.send_coverage_report(check_date)  # second tap: now met
    assert 'UNEXPLAINED' not in captured['text']
    assert '✓' in captured['text']
    rows = db.get_deviations()
    assert len(rows) == 1  # row kept, not deleted
    assert rows[0]['reason']  # auto-explained, not left unexplained


def test_clear_deviation_if_resolved_preserves_explained_row(isolated_db):
    """Opus review, 2026-07-25: a deviation a human already explained is real
    historical record (something did deviate and someone looked at it), not
    noise -- must never be silently destroyed just because the scenario is
    later met on a same-day re-check."""
    db.record_deviation('2026-07-24', 'sk_e', 'expected X', 'got Y', ticker=TICKER)
    dev_id = db.get_deviations()[0]['id']
    db.explain_deviation(dev_id, 'known transient issue')

    db.clear_deviation_if_resolved('2026-07-24', 'sk_e', ticker=TICKER)

    rows = db.get_deviations()
    assert len(rows) == 1
    assert rows[0]['reason'] == 'known transient issue'


def test_compute_status_scenario_expectations_unexplained_is_worst(isolated_db):
    """Opus review, 2026-07-27 (CONFIRMED, most severe finding): the original
    compute_status fed unexplained-deviation rows into the same good/bad
    bucketing as coverage_events, so an unexplained (currently failing)
    scenario rendered green 'verified-live' -- exactly backwards for an
    accountability tool. An unexplained deviation must be the worst status."""
    db.record_deviation('2026-07-24', 'sk_reg1', 'expected X', 'got Y', ticker=TICKER)
    row = dict(check_mechanism='scenario_expectations', scenario_key='sk_reg1')
    status, detail = compute_status(row)
    assert status == 'deviation-unexplained'


def test_compute_status_scenario_expectations_system_resolved_is_verified(isolated_db):
    """A same-day auto-resolution (deviated then met) is genuine positive
    evidence -- the scenario really did pass, just not on the first check."""
    db.record_deviation('2026-07-24', 'sk_reg2', 'expected X', 'got Y', ticker=TICKER)
    db.clear_deviation_if_resolved('2026-07-24', 'sk_reg2', ticker=TICKER)
    row = dict(check_mechanism='scenario_expectations', scenario_key='sk_reg2')
    status, detail = compute_status(row)
    assert status == 'verified-live'


def test_compute_status_scenario_expectations_human_explained_is_not_verified(isolated_db):
    """A human-explained historical deviation proves a failure happened and was
    looked at -- it does NOT prove the scenario currently behaves correctly, so
    this must not render as verified-live."""
    db.record_deviation('2026-07-24', 'sk_reg3', 'expected X', 'got Y', ticker=TICKER)
    dev_id = db.get_deviations()[0]['id']
    db.explain_deviation(dev_id, 'known one-off issue')
    row = dict(check_mechanism='scenario_expectations', scenario_key='sk_reg3')
    status, detail = compute_status(row)
    assert status == 'wired-never-fired'


def test_compute_status_scenario_expectations_no_history_is_not_verified(isolated_db):
    """No coverage_deviations rows at all is ambiguous (could mean 'always
    passed silently' or 'daily check never ran') -- must not default to
    verified-live without positive evidence."""
    row = dict(check_mechanism='scenario_expectations', scenario_key='sk_reg_never_seen')
    status, detail = compute_status(row)
    assert status == 'wired-never-fired'


def test_compute_status_bad_results_downgrades_verified(isolated_db):
    """A real live event whose only result is in bad_results (e.g. a blocked
    SL placement) must not render as verified-live -- it's evidence of a
    failure, not proof the path works."""
    db.log_coverage_event('sk_reg4', 'live', result='blocked')
    row = dict(check_mechanism='coverage_events', scenario_key='sk_reg4', bad_results=['blocked'])
    status, detail = compute_status(row)
    assert status == 'live-attempt-failed'

    db.log_coverage_event('sk_reg4', 'live', result='placed')
    status, detail = compute_status(row)
    assert status == 'verified-live'


def test_check_coverage_event_met_when_good_result_fires_today(isolated_db):
    check_date = datetime.now().strftime('%Y-%m-%d')
    db.log_coverage_event('cov_daily_a', 'live', result='placed')
    scenario = dict(scenario_key='cov_daily_a', check_params='{}')
    met, summary = _check_coverage_event(scenario, check_date)
    assert met is True
    assert '1x event' in summary


def test_check_coverage_event_not_met_when_no_events_today(isolated_db):
    check_date = datetime.now().strftime('%Y-%m-%d')
    scenario = dict(scenario_key='cov_daily_missing', check_params='{}')
    met, summary = _check_coverage_event(scenario, check_date)
    assert met is False
    assert 'no coverage_events' in summary


def test_check_coverage_event_not_met_when_only_bad_results_fire(isolated_db):
    check_date = datetime.now().strftime('%Y-%m-%d')
    db.log_coverage_event('cov_daily_b', 'live', result='blocked')
    scenario = dict(scenario_key='cov_daily_b',
                     check_params='{"bad_results": ["blocked"]}')
    met, summary = _check_coverage_event(scenario, check_date)
    assert met is False
    assert 'all bad_results' in summary


def test_check_coverage_event_respects_mode_filter(isolated_db):
    check_date = datetime.now().strftime('%Y-%m-%d')
    db.log_coverage_event('cov_daily_c', 'paper', result='market_filled')
    scenario = dict(scenario_key='cov_daily_c', mode='dry_run', check_params='{}')
    met, summary = _check_coverage_event(scenario, check_date)
    assert met is False  # only a paper event exists, dry_run filter excludes it

    db.log_coverage_event('cov_daily_c', 'dry_run', result='market_filled')
    met, summary = _check_coverage_event(scenario, check_date)
    assert met is True


def test_check_coverage_event_mode_filter_isolates_shared_scenario_key(isolated_db):
    """entry_fill/exit_fill are logged by both the dry_run-sim path and
    paper_trading.py under the same scenario_key -- a daily-expected row for
    one mode must not be satisfied by the other mode's events."""
    check_date = datetime.now().strftime('%Y-%m-%d')
    db.log_coverage_event('entry_fill', 'paper', result='market_filled')
    dry_run_row = dict(scenario_key='entry_fill', mode='dry_run', check_params='{}')
    met, _ = _check_coverage_event(dry_run_row, check_date)
    assert met is False


def test_check_coverage_event_scopes_to_ticker_and_node_id(isolated_db):
    """Same bug class as _check_trade_lifecycle's node_id disambiguation --
    a scenario carrying a specific ticker/node_id must not be satisfied by a
    different ticker's/node's event under the same scenario_key."""
    check_date = datetime.now().strftime('%Y-%m-%d')
    db.log_coverage_event('cov_daily_g', 'live', ticker='OTHER', node_id=999, result='placed')
    scenario = dict(scenario_key='cov_daily_g', ticker='TARGET', node_id=42, check_params='{}')
    met, summary = _check_coverage_event(scenario, check_date)
    assert met is False

    db.log_coverage_event('cov_daily_g', 'live', ticker='TARGET', node_id=42, result='placed')
    met, summary = _check_coverage_event(scenario, check_date)
    assert met is True


def test_check_coverage_event_ignores_events_from_other_days(isolated_db):
    check_date = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
    db.log_coverage_event('cov_daily_d', 'live', result='placed')  # logged "today", not check_date
    scenario = dict(scenario_key='cov_daily_d', check_params='{}')
    met, summary = _check_coverage_event(scenario, check_date)
    assert met is False


def test_check_coverage_event_uses_local_not_utc_date():
    """Real regression guard: coverage_events.ts is stored as SQLite
    datetime('now') (UTC). A UTC timestamp early in the ET day (e.g. 01:00
    UTC == 20:00 ET the *previous* day) must be attributed to the ET date,
    not the UTC date -- a plain date(ts) comparison would misdate it and
    silently fail to find the event (confirmed live: 212 of 1799 real rows
    fell on the wrong side of this exact boundary before the fix). This
    test doesn't use the isolated_db/log_coverage_event fixture path because
    that always stamps ts=UTC-now() -- it inserts a fixed cross-boundary
    timestamp directly to make the UTC/ET disagreement deterministic
    regardless of what time the suite happens to run."""
    import os
    import tempfile
    import signals_config
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    orig = signals_config.DB_PATH
    signals_config.DB_PATH = Path(tmp_db.name)
    try:
        db.ensure_tables()
        with db._conn() as c:
            # 2026-07-25 01:00 UTC == 2026-07-24 ~20:00-21:00 ET (EDT, UTC-4)
            c.execute("INSERT INTO coverage_events (ts, scenario_key, mode, result) "
                       "VALUES ('2026-07-25 01:00:00', 'cov_tz', 'live', 'placed')")
            c.commit()
        scenario = dict(scenario_key='cov_tz', check_params='{}')
        met, summary = _check_coverage_event(scenario, '2026-07-24')
        assert met is True, f"UTC 2026-07-25 01:00 should attribute to ET 2026-07-24: {summary}"
        met, summary = _check_coverage_event(scenario, '2026-07-25')
        assert met is False, f"must not also match the UTC calendar date: {summary}"
    finally:
        signals_config.DB_PATH = orig
        os.unlink(tmp_db.name)


def test_run_check_records_deviation_for_missing_daily_coverage_event(isolated_db):
    check_date = '2026-07-24'  # a Friday
    db.add_scenario_expectation(
        scenario_key='cov_daily_e', expected_outcome='fires daily', expected_frequency='daily',
        check_method='coverage_event', check_params='{}')
    results = run_check(check_date)
    assert any(r['scenario_key'] == 'cov_daily_e' and r['status'] == 'deviated' for r in results)
    deviations = db.get_deviations(check_date=check_date, unexplained_only=True)
    assert any(d['scenario_key'] == 'cov_daily_e' for d in deviations)


def test_run_check_marks_met_for_present_daily_coverage_event(isolated_db):
    check_date = '2026-07-24'  # a Friday
    with db._conn() as c:
        c.execute("INSERT INTO coverage_events (ts, scenario_key, mode, result) VALUES (?, ?, ?, ?)",
                   (f"{check_date} 10:00:00", 'cov_daily_f', 'live', 'placed'))
        c.commit()
    db.add_scenario_expectation(
        scenario_key='cov_daily_f', expected_outcome='fires daily', expected_frequency='daily',
        check_method='coverage_event', check_params='{}')
    results = run_check(check_date)
    assert any(r['scenario_key'] == 'cov_daily_f' and r['status'] == 'met' for r in results)
