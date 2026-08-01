"""Fake-broker tests for exit/SL lifecycle branches (5 scenarios).

These branches are verified-live (proven by real daemon activity in
trading_live.db's coverage_events table) but have zero fake-broker-driven
regression tests -- only real production logs prove they work, nothing in the
test suite would catch a regression:

1. sl_sync_placement -> _place_stop_loss_for_position (places real STOP order)
2. exit_arm_latency -> _scan_pinned_exit_arm (bar-close exit-arm check)
3. trailing_arm_reread -> notify_trailing_activated (re-reads fresh position)
4. manual_sl_fallback_alert -> _attempt_automated_sell (fallback alert fires)
5. automated_sell_execution -> _attempt_automated_sell (successful TRAIL order)
"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import active_signals
import signals_blocks
import signals_compute
import signals_config
import signals_db
import signals_notify
import schwab_client
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_EXIT_LIFECYCLE'
SIGNAL_TIME = datetime(2026, 8, 1, 14, 30)  # 14:30 ET (bar close at 15:30)
IN_WINDOW_TIME = datetime(2026, 8, 1, 15, 35)  # 15:35 ET (inside signal window)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated test DB + state files, matching the pattern from
    test_fake_broker_*.py tests."""
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
    monkeypatch.setattr(schwab_safety, '_now', lambda: IN_WINDOW_TIME)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(signals_blocks, '_post_message', lambda *args, **kwargs: (None, None))

    signals_db.ensure_tables()
    yield
    schwab_safety.disengage_kill_switch()
    Path(tmp_db.name).unlink(missing_ok=True)


def _add_node(ticker, account='soxl_ira', notional=5000):
    """Add a live-mode node for testing."""
    signals_db.add_node(ticker, 'TrailingBothZScoreBreakout', 'test', window=10,
                         take_profit=16.0, stop_loss=1.0, max_hold_hours=105,
                         mode='live', trail_buy_pct=1.0, trail_pct=1.0,
                         fixed_sl_override=1.0, account=account, starting_notional=notional)
    return [n for n in signals_db.get_watchlist()
            if n['ticker'] == ticker and n['account'] == account][0]


def _setup_hourly_data(ticker, bar_ts):
    """Create hourly OHLC data that can be loaded by the daemon."""
    data = {
        'Open': [100.0, 101.0, 102.0, 103.0],
        'High': [101.0, 102.0, 103.0, 104.0],
        'Low': [99.0, 100.0, 101.0, 102.0],
        'Close': [101.0, 102.0, 103.0, 103.5],
        'Volume': [1000, 1000, 1000, 1000],
    }
    df = pd.DataFrame(data)
    # Index is bar start times: 13:30, 14:30, 15:30, 16:30
    df.index = pd.DatetimeIndex([
        datetime(2026, 8, 1, 13, 30),
        datetime(2026, 8, 1, 14, 30),
        datetime(2026, 8, 1, 15, 30),
        datetime(2026, 8, 1, 16, 30),
    ])
    df.index.name = 'Datetime'
    return df


def test_sl_sync_placement_places_real_stop_order(env, fake_broker):
    """Scenario: sl_sync_placement
    _place_stop_loss_for_position places a real protective STOP order at the
    broker after a position opens (sync confirm path).
    """
    node = _add_node(TICKER, 'soxl_ira')
    signal_price = 103.0
    entry_price = 102.0
    shares = 49

    # Open a position (seeded directly, matching existing test patterns)
    opened = signals_db.open_position(
        node, signal_price, SIGNAL_TIME, entry_price, SIGNAL_TIME, shares=shares
    )
    assert opened is True
    pos = signals_db.get_open_position(TICKER)

    # Set up broker quote/cash for the SL order
    fake_broker.set_quote(TICKER, last=102.0, bid=102.0, ask=102.01)
    fake_broker.set_cash_balance('soxl_ira', 100_000.0)

    # --- act: place stop-loss order ---
    signals_notify._place_stop_loss_for_position(node, TICKER)

    # --- assert: real STOP order reached the broker ---
    stop_orders = [o for o in fake_broker.orders.values()
                   if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                   and o['orderType'] == 'STOP']
    assert len(stop_orders) == 1, (
        f"expected exactly 1 STOP order to reach fake_broker, found {len(stop_orders)}"
    )
    stop_order = stop_orders[0]
    assert stop_order['orderLegCollection'][0]['instruction'] == 'SELL'
    assert stop_order['orderLegCollection'][0]['quantity'] == shares
    assert float(stop_order['stopPrice']) == pytest.approx(entry_price * 0.99, rel=0.01)  # 1% SL

    # --- assert: coverage event fired ---
    events = signals_db.get_coverage_events(scenario_key='sl_placement')
    assert any(e['ticker'] == TICKER and e['result'] == 'placed' for e in events), (
        f"expected 'sl_placement' event with result='placed', got: "
        f"{[(e['result'], e.get('detail')) for e in events]}"
    )


def test_exit_arm_latency_evaluates_bar_close(env, fake_broker, monkeypatch):
    """Scenario: exit_arm_latency
    _scan_pinned_exit_arm evaluates a newly-closed bar for arm/exit conditions
    on a real open position.
    """
    node = _add_node(TICKER, 'soxl_ira')
    signal_price = 100.0
    entry_price = 101.0
    shares = 50

    # Open position
    signals_db.open_position(
        node, signal_price, SIGNAL_TIME, entry_price, SIGNAL_TIME, shares=shares
    )
    pos = signals_db.get_open_position(TICKER)

    # Mock the data_cache so the pinned scan can load hourly data
    bar_ts = datetime(2026, 8, 1, 15, 30)
    df = _setup_hourly_data(TICKER, bar_ts)

    def mock_load_cache(ticker):
        if ticker == TICKER:
            return df, None
        return None, None

    monkeypatch.setattr(active_signals, '_load_cache', mock_load_cache)

    # Build last_seen_bar keyed by wl_id (node ID) as _pos_key does
    # The key needs to be the prior bar timestamp so resolve_at_bar_close
    # will see a new bar (15:30) and return True
    last_seen_bar = {pos['wl_id']: datetime(2026, 8, 1, 14, 30)}

    # --- act: run pinned exit-arm scan ---
    sell_alerted = set()
    active_signals._scan_pinned_exit_arm([pos], sell_alerted, last_seen_bar)

    # --- assert: coverage event fired ---
    events = signals_db.get_coverage_events(scenario_key='exit_arm_latency')
    ticker_events = [e for e in events if e['ticker'] == TICKER]
    assert len(ticker_events) > 0, (
        f"expected 'exit_arm_latency' event for {TICKER}, got no events"
    )
    assert any(e['result'] == 'evaluated' for e in ticker_events), (
        f"expected 'exit_arm_latency' event with result='evaluated', "
        f"got: {[(e['result'], e.get('detail')) for e in ticker_events]}"
    )


def test_trailing_arm_reread_preserves_armed_state(env, fake_broker, monkeypatch):
    """Scenario: trailing_arm_reread
    notify_trailing_activated re-reads the position fresh via get_position_by_id
    before merging in the just-armed trail_state, so a stale in-memory copy
    can't clobber it (2026-07-22 CRITICAL bug fix).
    """
    node = _add_node(TICKER, 'soxl_ira')
    signal_price = 100.0
    entry_price = 100.5
    shares = 50

    # Open position
    signals_db.open_position(
        node, signal_price, SIGNAL_TIME, entry_price, SIGNAL_TIME, shares=shares
    )
    pos = signals_db.get_open_position(TICKER)

    # Manually arm the position (set trailing=True in trail_state)
    trail_state = {
        'trailing': True,
        'peak': 103.0,
        'exit_forced_by_hold_time': False,
    }
    signals_db.update_position_trail_state(pos['id'], trail_state)

    # Mock _post_message to avoid Slack sends
    monkeypatch.setattr(signals_blocks, '_post_message',
                        lambda *args, **kwargs: ('CH123', '1234.5'))

    fake_broker.set_quote(TICKER, last=102.0, bid=102.0, ask=102.01)
    fake_broker.set_cash_balance('soxl_ira', 100_000.0)

    # --- act: notify_trailing_activated (will call _attempt_automated_sell) ---
    signals_notify.notify_trailing_activated(pos, 102.0)

    # --- assert: coverage event fired with trailing_preserved ---
    events = signals_db.get_coverage_events(scenario_key='trailing_arm_state_reread')
    trailing_events = [e for e in events if e['ticker'] == TICKER]
    assert any(e['result'] == 'trailing_preserved' for e in trailing_events), (
        f"expected 'trailing_arm_state_reread' with result='trailing_preserved', "
        f"got: {[(e['result'], e.get('detail')) for e in trailing_events]}"
    )

    # --- assert: the re-fetched position still has trailing=True ---
    fresh_pos = signals_db.get_position_by_id(pos['id'])
    assert fresh_pos is not None
    fresh_state = fresh_pos.get('trail_state') or {}
    assert fresh_state.get('trailing') is True, (
        f"trailing flag should be preserved after notify_trailing_activated, "
        f"got: {fresh_state}"
    )


def test_manual_sl_fallback_alert_fires_when_sell_placement_fails(env, fake_broker, monkeypatch):
    """Scenario: manual_sl_fallback_alert
    When the resting SL is cancelled but the trailing-sell placement THEN fails,
    a manual-SL-price fallback alert fires (deliberately no auto-recovery).
    """
    node = _add_node(TICKER, 'soxl_ira')
    signal_price = 100.0
    entry_price = 101.0
    shares = 50

    # Open position with an existing sl_order_id (as if a stop was placed)
    signals_db.open_position(
        node, signal_price, SIGNAL_TIME, entry_price, SIGNAL_TIME, shares=shares
    )
    pos = signals_db.get_open_position(TICKER)
    # Simulate that a STOP order exists at the broker
    fake_sl_order_id = 9999999999
    signals_db.set_sl_order_id_by_position(pos['id'], fake_sl_order_id)
    # Re-fetch to get the updated sl_order_id
    pos = signals_db.get_open_position(TICKER)

    fake_broker.set_quote(TICKER, last=102.0, bid=102.0, ask=102.01)
    fake_broker.set_cash_balance('soxl_ira', 100_000.0)

    # Mock both place_trailing_sell and replace_order_with_trailing_sell to fail
    # (simulating a broker rejection). Since we have sl_order_id, it will call
    # replace_order_with_trailing_sell.
    def mock_place_fail(*args, **kwargs):
        raise Exception("Simulated trailing-sell placement failure at broker")

    def mock_replace_fail(*args, **kwargs):
        raise Exception("Simulated trailing-sell replace failure at broker")

    monkeypatch.setattr(schwab_client, 'place_trailing_sell', mock_place_fail)
    monkeypatch.setattr(schwab_client, 'replace_order_with_trailing_sell', mock_replace_fail)

    # --- act: attempt automated sell (which will fail and trigger fallback alert) ---
    result = signals_notify._attempt_automated_sell(pos, 102.0)
    # Should return (False, None) due to exception handling
    assert result == (False, None)

    # --- assert: manual_sl_fallback_alert event fired ---
    events = signals_db.get_coverage_events(scenario_key='manual_sl_fallback_alert')
    fallback_events = [e for e in events if e['ticker'] == TICKER]
    assert len(fallback_events) > 0, (
        f"expected 'manual_sl_fallback_alert' event, got none"
    )
    assert any(e['result'] == 'alerted' for e in fallback_events), (
        f"expected result='alerted', got: {[(e['result'], e.get('detail')) for e in fallback_events]}"
    )


def test_automated_sell_execution_places_real_trailing_order(env, fake_broker):
    """Scenario: automated_sell_execution
    _attempt_automated_sell's successful path places a real trailing-sell order
    at the broker (not just correctly blocked/deferred).
    """
    node = _add_node(TICKER, 'soxl_ira')
    signal_price = 100.0
    entry_price = 101.0
    shares = 50

    # Open position
    signals_db.open_position(
        node, signal_price, SIGNAL_TIME, entry_price, SIGNAL_TIME, shares=shares
    )
    pos = signals_db.get_open_position(TICKER)

    fake_broker.set_quote(TICKER, last=102.0, bid=102.0, ask=102.01)
    fake_broker.set_cash_balance('soxl_ira', 100_000.0)

    # --- act: attempt automated sell ---
    auto_placed, exit_order_id = signals_notify._attempt_automated_sell(pos, 102.0)

    # --- assert: order was placed successfully ---
    assert auto_placed is True, "automated sell should succeed"
    assert exit_order_id is not None, "exit_order_id should be returned on success"

    # --- assert: real TRAILING SELL order reached the broker ---
    sell_orders = [o for o in fake_broker.orders.values()
                   if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                   and o['orderLegCollection'][0]['instruction'] == 'SELL']
    assert len(sell_orders) >= 1, (
        f"expected at least 1 SELL order to reach fake_broker, found {len(sell_orders)}"
    )

    # Find the trailing sell order (should be TRAILING_STOP type)
    trailing_sells = [o for o in sell_orders if 'TRAILING' in o['orderType']]
    assert len(trailing_sells) >= 1, (
        f"expected at least 1 TRAILING SELL, found {len(sell_orders)} total sells: "
        f"{[(o['orderType'], o['status']) for o in sell_orders]}"
    )

    # --- assert: coverage event fired with result='placed' ---
    events = signals_db.get_coverage_events(scenario_key='automated_sell_execution')
    placed_events = [e for e in events if e['ticker'] == TICKER and e['result'] == 'placed']
    assert len(placed_events) >= 1, (
        f"expected 'automated_sell_execution' with result='placed', "
        f"got: {[(e['result'], e.get('detail')) for e in events if e['ticker'] == TICKER]}"
    )
