"""Tests for signals_notify.check_addon_buying_power_drift -- added 2026-08-10
as follow-up #1 of the add-on buying-power reservation fix
(docs/deep_backlog.md's 2026-08-09/10 entry): the reservation for OTHER
tickers' resting-order notional is 1x while the add-on's own notional gets
ADDON_BUYING_POWER_HEADROOM_MULT (2x), currently masked only because
buying_power == cash on every real account today. This check watches for
that assumption breaking. Isolated DB, no real Slack posts, isolated state
file (tmp_path), no real broker calls (schwab_client fully monkeypatched)."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import signals_notify
import schwab_client

sys.path.insert(0, str(Path(__file__).parent))
from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_ADDON_DRIFT'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'ADDON_BUYING_POWER_DRIFT_STATE_PATH', tmp_path / "state.json")
    monkeypatch.setattr(signals_notify, '_coverage_is_trading_day', lambda date_str: True)
    posted = []

    def _fake_post(*a, **kw):
        posted.append(a[0] if a else kw.get('text'))
        return ("C1", "1.0")  # confirmed post, matches real _post_message's (channel, ts) contract
    monkeypatch.setattr(signals_notify, '_post_message', _fake_post)
    signals_db.ensure_tables()
    yield posted
    Path(tmp_db.name).unlink()


TRADING_HOURS_NOON = datetime(2026, 8, 10, 12, 0, 0)  # a Monday


def _add_addon_node(ticker='TICKER', account='soxl_ira', state='live'):
    signals_db.add_node(ticker, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state=state,
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account=account)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET addon_enabled=1 WHERE ticker=? AND account=?", (ticker, account))
        c.commit()


def test_no_addon_enabled_live_nodes_is_a_no_op(env, monkeypatch):
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: (_ for _ in ()).throw(AssertionError("should not be called")))
    signals_notify.check_addon_buying_power_drift(now=TRADING_HOURS_NOON)
    assert env == []


def test_matching_buying_power_and_cash_stays_silent(env, monkeypatch):
    _add_addon_node(TICKER, 'soxl_ira')
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: 10_000.0)
    monkeypatch.setattr(schwab_client, 'get_account_buying_power', lambda a: 10_000.0)
    signals_notify.check_addon_buying_power_drift(now=TRADING_HOURS_NOON)
    assert env == []
    events = signals_db.get_coverage_events(scenario_key='addon_buying_power_drift_check')
    assert any(e['result'] == 'no_drift' for e in events)


def test_diverging_buying_power_and_cash_alerts(env, monkeypatch):
    _add_addon_node(TICKER, 'soxl_ira')
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: 10_000.0)
    monkeypatch.setattr(schwab_client, 'get_account_buying_power', lambda a: 20_000.0)  # real leverage appeared
    signals_notify.check_addon_buying_power_drift(now=TRADING_HOURS_NOON)
    assert len(env) == 1
    assert 'soxl_ira' in env[0]
    events = signals_db.get_coverage_events(scenario_key='addon_buying_power_drift_check')
    assert any(e['result'] == 'diverged' for e in events)


def test_small_float_noise_does_not_alert(env, monkeypatch):
    _add_addon_node(TICKER, 'soxl_ira')
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: 10_000.00)
    monkeypatch.setattr(schwab_client, 'get_account_buying_power', lambda a: 10_000.20)
    signals_notify.check_addon_buying_power_drift(now=TRADING_HOURS_NOON)
    assert env == []


def test_paper_mode_node_is_not_checked(env, monkeypatch):
    _add_addon_node(TICKER, 'soxl_ira', state='paper')
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: (_ for _ in ()).throw(AssertionError("should not be called")))
    signals_notify.check_addon_buying_power_drift(now=TRADING_HOURS_NOON)
    assert env == []


def test_fetch_failure_logs_fetch_failed_without_raising(env, monkeypatch):
    _add_addon_node(TICKER, 'soxl_ira')

    def _raise(a):
        raise RuntimeError("network error")
    monkeypatch.setattr(schwab_client, 'get_account_balance', _raise)
    signals_notify.check_addon_buying_power_drift(now=TRADING_HOURS_NOON)  # must not raise
    assert env == []
    events = signals_db.get_coverage_events(scenario_key='addon_buying_power_drift_check')
    assert any(e['result'] == 'fetch_failed' for e in events)


def test_fetch_failure_does_not_advance_watermark_so_next_poll_retries(env, monkeypatch):
    """2026-08-10 fix (all 3 paired reviewers): a total fetch failure must not
    silently disable the check for the rest of the day -- the account stays
    unmarked so the next poll cycle (same day) retries it."""
    _add_addon_node(TICKER, 'soxl_ira')
    calls = []

    def _raise(a):
        calls.append(a)
        raise RuntimeError("network error")
    monkeypatch.setattr(schwab_client, 'get_account_balance', _raise)
    signals_notify.check_addon_buying_power_drift(now=TRADING_HOURS_NOON)
    signals_notify.check_addon_buying_power_drift(now=datetime(2026, 8, 10, 12, 5, 0))
    assert len(calls) == 2  # both polls actually tried -- not silently skipped after the first


def test_unconfirmed_diverged_alert_does_not_advance_watermark(env, monkeypatch):
    """2026-08-10 fix: mirrors check_intraday_risk_review's confirmed-post-
    before-advancing-watermark pattern -- a diverged account whose Slack
    alert fails to post must not be marked checked for today."""
    _add_addon_node(TICKER, 'soxl_ira')
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: 10_000.0)
    monkeypatch.setattr(schwab_client, 'get_account_buying_power', lambda a: 20_000.0)
    calls = []

    def _failed_post(*a, **kw):
        calls.append(a)
        return (None, None)  # unconfirmed, matches real _post_message's failure contract
    monkeypatch.setattr(signals_notify, '_post_message', _failed_post)
    signals_notify.check_addon_buying_power_drift(now=TRADING_HOURS_NOON)
    signals_notify.check_addon_buying_power_drift(now=datetime(2026, 8, 10, 12, 5, 0))
    assert len(calls) == 2  # retried the alert on the second poll, not silently marked done


def test_only_checks_once_per_day(env, monkeypatch):
    _add_addon_node(TICKER, 'soxl_ira')
    calls = []

    def _balance(a):
        calls.append(a)
        return 10_000.0
    monkeypatch.setattr(schwab_client, 'get_account_balance', _balance)
    monkeypatch.setattr(schwab_client, 'get_account_buying_power', lambda a: 10_000.0)
    signals_notify.check_addon_buying_power_drift(now=TRADING_HOURS_NOON)
    signals_notify.check_addon_buying_power_drift(now=datetime(2026, 8, 10, 15, 0, 0))
    assert len(calls) == 1


def test_non_trading_day_is_a_no_op(env, monkeypatch):
    monkeypatch.setattr(signals_notify, '_coverage_is_trading_day', lambda date_str: False)
    _add_addon_node(TICKER, 'soxl_ira')
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: (_ for _ in ()).throw(AssertionError("should not be called")))
    signals_notify.check_addon_buying_power_drift(now=TRADING_HOURS_NOON)
    assert env == []


def test_fake_broker_diverging_buying_power_and_cash_alerts(env, fake_broker):
    """registry id 'addon_buying_power_drift_check' -- every other test above
    monkeypatches schwab_client.get_account_balance/get_account_buying_power
    per-call, which proves the drift-detection LOGIC but never drives the
    real Client.get_account() call schwab_client.py actually makes (found
    2026-08-13, fake_venue_proof_for scan: this file had zero fake_broker
    coverage despite being a real order-adjacent check). fake_broker patches
    at the schwab-py client boundary instead, so this exercises the genuine
    get_account_balance/get_account_buying_power code paths end to end."""
    _add_addon_node(TICKER, 'soxl_ira')
    fake_broker.set_cash_balance('soxl_ira', 10_000.0)
    fake_broker.set_buying_power('soxl_ira', 20_000.0)  # real leverage appeared
    signals_notify.check_addon_buying_power_drift(now=TRADING_HOURS_NOON)
    assert len(env) == 1
    assert 'soxl_ira' in env[0]
    events = signals_db.get_coverage_events(scenario_key='addon_buying_power_drift_check')
    assert any(e['result'] == 'diverged' for e in events)
