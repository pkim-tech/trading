"""Fake-venue proof for schwab_safety.check_order's SELL-side oversell guard with
cross-account position resolution. The guard must correctly resolve the open
position for the SPECIFIC (ticker, account) pair, not just ticker-only.

Scenario_key: sell_exceeds_position_blocked

Tests:
  1. A SELL within one account's position size succeeds and reaches the broker
  2. A SELL exceeding one account's position is blocked, even though a different
     account has more shares (proves the guard is account-scoped, not ticker-only)
  3. A SELL for an account with no open position at all is blocked with result=
     "blocked_no_position" (proves fail-closed on missing position)
"""
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

TICKER = 'TEST_OVERSELL_SCENARIO'
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


def _add_node(ticker, account, notional=5000):
    """Create a watch_list node for the given (ticker, account) pair."""
    signals_db.add_node(ticker, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, mode='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account=account, starting_notional=notional)
    return [n for n in signals_db.get_watchlist()
            if n['ticker'] == ticker and n['account'] == account][0]


def _real_placed_orders(fake_broker_, ticker):
    """Filter fake broker's order book to just orders for the given ticker."""
    return [o for o in fake_broker_.orders.values()
            if o['orderLegCollection'][0]['instrument']['symbol'] == ticker]


def test_sell_within_position_size_succeeds(env, fake_broker):
    """A SELL quantity within the account's actual position size reaches the
    fake broker successfully."""
    node_soxl = _add_node(TICKER, 'soxl_ira', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    # Seed an open position in soxl_ira: 50 shares
    signals_db.open_position(
        node_soxl, signal_price=10.0, signal_time=_IN_WINDOW_TIME,
        entry_price=10.0, entry_time=_IN_WINDOW_TIME, shares=50
    )

    # Attempt SELL of 40 shares in soxl_ira (40 <= 50, should succeed)
    r, oid = schwab_client.place_equity_sell('soxl_ira', TICKER, 40, 10.0)
    assert oid is not None, "a SELL within position size must be allowed through"

    # Verify it actually reached the fake broker
    placed = _real_placed_orders(fake_broker, TICKER)
    assert len(placed) == 1, f"expected 1 real SELL order to reach the broker, got {len(placed)}"
    assert placed[0]['orderLegCollection'][0]['quantity'] == 40

    # Verify coverage_events logged success (no blocked event for this path)
    events = signals_db.get_coverage_events(scenario_key='sell_exceeds_position_blocked')
    # This test should NOT log an event (the guard was not triggered, order was allowed)
    # So we check that there's no blocking event
    assert not any(e['result'] in ('blocked', 'blocked_no_position') for e in events if e['ticker'] == TICKER)


def test_sell_exceeding_one_account_blocked_not_confused_by_other_account(env, fake_broker):
    """A SELL exceeding one account's position is blocked, even when a DIFFERENT
    account has plenty of shares. Proves the guard uses get_open_position_for_account
    (account-scoped) not just ticker-scoped logic."""
    # Two nodes, same ticker, different accounts
    node_ira = _add_node(TICKER, 'ira', notional=5_000)
    node_soxl = _add_node(TICKER, 'soxl_ira', notional=50_000)

    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('ira', 1_000_000.0)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    # Seed positions: ira has 10 shares, soxl_ira has 50 shares
    signals_db.open_position(
        node_ira, signal_price=10.0, signal_time=_IN_WINDOW_TIME,
        entry_price=10.0, entry_time=_IN_WINDOW_TIME, shares=10
    )
    signals_db.open_position(
        node_soxl, signal_price=10.0, signal_time=_IN_WINDOW_TIME,
        entry_price=10.0, entry_time=_IN_WINDOW_TIME, shares=50
    )

    # Attempt SELL of 40 shares in 'ira' (40 > 10, should be BLOCKED)
    # Even though 'soxl_ira' has 50 shares
    with pytest.raises(schwab_safety.SafetyViolation, match="exceeds.*shares"):
        schwab_client.place_equity_sell('ira', TICKER, 40, 10.0)

    # Verify the SELL never reached the fake broker
    placed = _real_placed_orders(fake_broker, TICKER)
    assert placed == [], \
        "an oversell-size SELL must never reach the broker, even if a different account has plenty"

    # Verify the guard logged a block event
    events = signals_db.get_coverage_events(scenario_key='sell_exceeds_position_blocked')
    assert any(
        e['ticker'] == TICKER and e['result'] == 'blocked' and e['detail'] and 'quantity=40' in e['detail']
        for e in events
    ), "expected a 'blocked' event for the oversell attempt"


def test_sell_for_account_with_no_position_blocked_no_position(env, fake_broker):
    """A SELL for an account with no open position at all is blocked with
    result='blocked_no_position', fail-closed safety (automation_principles.md #2)."""
    # Two nodes, same ticker, different accounts
    node_ira = _add_node(TICKER, 'ira', notional=5_000)
    node_soxl = _add_node(TICKER, 'soxl_ira', notional=50_000)

    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('ira', 1_000_000.0)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    # Seed a position ONLY in soxl_ira, NOT in ira
    signals_db.open_position(
        node_soxl, signal_price=10.0, signal_time=_IN_WINDOW_TIME,
        entry_price=10.0, entry_time=_IN_WINDOW_TIME, shares=50
    )

    # Attempt SELL in 'ira', which has NO open position
    with pytest.raises(schwab_safety.SafetyViolation, match="no open position"):
        schwab_client.place_equity_sell('ira', TICKER, 20, 10.0)

    # Verify the SELL never reached the fake broker
    placed = _real_placed_orders(fake_broker, TICKER)
    assert placed == [], \
        "a SELL with no local position must never reach the broker"

    # Verify the guard logged the specific no-position block event
    events = signals_db.get_coverage_events(scenario_key='sell_exceeds_position_blocked')
    assert any(
        e['ticker'] == TICKER and e['result'] == 'blocked_no_position'
        for e in events
    ), "expected a 'blocked_no_position' event for the no-position SELL attempt"
