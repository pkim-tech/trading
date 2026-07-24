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
