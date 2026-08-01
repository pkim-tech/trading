"""Fake-venue test for signals_notify._attempt_automated_sell's mode-gating.

Scenario: a research-mode node (not live) with a ticker in
AUTOMATION_ENABLED_TICKERS holds a real open position. When
_attempt_automated_sell is called, does it correctly skip the automated
placement (mode guard), or does it proceed anyway (known gap in
docs/automation_principles.md #7)?

This test empirically verifies the actual behavior: the code at
signals_notify.py:93-98 reads the node's mode and should skip if not 'live'.
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
import signals_notify
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_AUTOMATED_SELL_MODE_SKIP'


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
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_node_auto_fill_detection.json")
    # This ticker is in automation scope, so normally eligible for automated sell
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 7, 29, 10, 30))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])

    signals_db.ensure_tables()

    # Create a RESEARCH-mode node (NOT live) with the ticker in automation scope
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20,
                        take_profit=5.0, stop_loss=1.0, max_hold_hours=105,
                        mode='research',  # <-- KEY: not 'live'
                        trail_buy_pct=1.0, trail_pct=0.3, fixed_sl_override=1.0,
                        account='soxl_ira', starting_notional=5000)

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def test_research_mode_node_skips_automated_sell(env, fake_broker, monkeypatch):
    """When a research-mode node has a real open position in an
    automation-enabled ticker, _attempt_automated_sell should skip
    (return False) instead of placing an order, because the mode check
    should gate it.

    This empirically verifies the behavior: either the mode guard works
    correctly (returns False, logs automated_sell_mode_skip), or it
    incorrectly proceeds (the known gap documented in CLAUDE.md).
    """
    node = _node()
    assert node['mode'] == 'research', "Test setup: node should be research mode"
    assert TICKER in schwab_safety.AUTOMATION_ENABLED_TICKERS, "Test setup: ticker should be in automation scope"

    # Create a real open position via the node
    entry_time = datetime(2026, 7, 27, 9, 30, 3)
    signals_db.open_position(node, signal_price=85.0, signal_time=entry_time,
                              entry_price=83.76, entry_time=entry_time, shares=2)
    pos = signals_db.get_open_position(TICKER)
    assert pos is not None, "Test setup: position should be created"
    assert pos['wl_id'] == node['id'], "Position should be linked to the research-mode node"

    # Set up fake broker with a quote and no resting orders
    fake_broker.set_quote(TICKER, last=84.0, bid=83.99, ask=84.01)
    fake_broker.set_cash_balance('soxl_ira', 100_000.0)

    # Call _attempt_automated_sell directly
    auto_placed, exit_order_id = signals_notify._attempt_automated_sell(pos, current_price=84.0)

    # --- Assertions ---
    # The mode guard should prevent the automated sell from proceeding
    assert auto_placed is False, (
        "EMPIRICAL FINDING: research-mode node's automated sell should be skipped. "
        "If this fails, the known gap in docs/automation_principles.md #7 is "
        "still unfixed (SELL-side automation gated by ticker only, not mode)."
    )
    assert exit_order_id is None, "On skip, order_id should be None"

    # Verify the coverage event was logged with result='skipped'
    events = signals_db.get_coverage_events(scenario_key='automated_sell_mode_skip')
    matching_events = [e for e in events if e['ticker'] == TICKER and e['result'] == 'skipped']
    assert len(matching_events) > 0, (
        f"Expected automated_sell_mode_skip event with result='skipped', "
        f"got: {[e['result'] for e in events]}"
    )
    event = matching_events[0]
    assert event['detail'] == "node_mode='research'", (
        f"Event detail should show node_mode='research', got: {event['detail']}"
    )

    # Verify NO order reached the fake broker
    ticker_orders = [o for o in fake_broker.orders.values()
                     if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER]
    assert len(ticker_orders) == 0, (
        f"No order should reach fake_broker for a research-mode node, "
        f"but found {len(ticker_orders)}: {[(o['orderId'], o['orderType'], o['status']) for o in ticker_orders]}"
    )

    # Position should still be open (no fill/close from the skipped order)
    still_open = signals_db.get_open_position(TICKER)
    assert still_open is not None, "Position should remain open (not closed by skipped order)"
    assert still_open['id'] == pos['id'], "Should still be the same position"
