"""Fake-venue truth table for the existing-position BUY guard (schwab_safety.
check_order, added 2026-08-02): closes the real gap confirmed 2026-07-24 --
two real resting TRAILING_STOP BUYs left get_account_balance completely
unchanged, so notional_cap (per-order) and the cash check (reads that same
undecremented balance) can't by themselves stop a second real BUY from being
approved for a ticker the account already holds. The resting-order dup guards
only cover the window before the first order fills; this guard covers after.

Two scenarios: a genuine second BUY for an already-held ticker must be
blocked (is_protective=False), and the sanctioned top-up path
(is_protective=True) must still be allowed through unchanged."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import signals_config
import signals_db
import schwab_client
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_POS_EXISTS_SCENARIO'
_IN_WINDOW_TIME = datetime(2026, 7, 29, 10, 30)


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    monkeypatch.setattr(schwab_safety, 'STATE_PATH', tmp_path / "schwab_order_counts.json")
    monkeypatch.setattr(schwab_safety, 'KILL_SWITCH_PATH', tmp_path / "schwab_kill_switch.json")
    monkeypatch.setattr(schwab_safety, 'TICKER_AUTOMATION_PATH', tmp_path / "schwab_ticker_automation.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTOMATION_PATH', tmp_path / "schwab_node_automation.json")
    monkeypatch.setattr(schwab_safety, 'NODE_BREAKER_PATH', tmp_path / "schwab_node_breaker_state.json")
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_node_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: _IN_WINDOW_TIME)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    signals_db.ensure_tables()
    yield
    schwab_safety.disengage_kill_switch()
    Path(tmp_db.name).unlink(missing_ok=True)


def _add_node(ticker, account, notional=50_000):
    signals_db.add_node(ticker, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account=account, starting_notional=notional)
    return [n for n in signals_db.get_watchlist()
            if n['ticker'] == ticker and n['account'] == account][0]


def _real_placed_orders(fake_broker_, ticker):
    return [o for o in fake_broker_.orders.values()
            if o['orderLegCollection'][0]['instrument']['symbol'] == ticker]


def test_second_buy_blocked_when_position_already_open(env, fake_broker):
    node = _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    r1, oid1 = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid1 is not None
    signals_db.open_position(node, signal_price=10.0, signal_time=_IN_WINDOW_TIME,
                              entry_price=10.0, entry_time=_IN_WINDOW_TIME, shares=10)

    with pytest.raises(schwab_safety.SafetyViolation, match="already has an open position"):
        schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)

    assert len(_real_placed_orders(fake_broker, TICKER)) == 1, \
        "the second real BUY must never reach the broker while a position is already open"
    events = signals_db.get_coverage_events(scenario_key='buy_blocked_position_exists')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events)


def test_protective_topup_still_allowed_when_position_already_open(env, fake_broker):
    node = _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    r1, oid1 = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid1 is not None
    signals_db.open_position(node, signal_price=10.0, signal_time=_IN_WINDOW_TIME,
                              entry_price=10.0, entry_time=_IN_WINDOW_TIME, shares=10)

    r2, oid2 = schwab_client.place_equity_buy('soxl_ira', TICKER, 2, 10.0, is_protective=True)
    assert oid2 is not None, "a sanctioned top-up must still be allowed through despite the open position"

    assert len(_real_placed_orders(fake_broker, TICKER)) == 2
