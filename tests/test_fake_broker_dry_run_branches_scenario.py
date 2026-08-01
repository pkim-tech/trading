"""Fake-venue tests for 4 dry_run/daemon-robustness coverage branches that were
previously only provable via dry_run account synthetic fill activity or never via
controlled fake-broker tests:
  - sl_async_fallback        (sl_placement_fast_confirm_timeout)
  - daemon_exception_survival (daemon_section_exception)
  - dry_run_buy_synthesis    (entry_fill, mode='dry_run')
  - dry_run_sim_close        (exit_fill, mode='dry_run')

Uses the same fake_broker fixture + isolated DB pattern as the other scenario tests.
For dry_run branches (1,3,4), uses account 'ira' (dry_run=True in schwab_safety.ACCOUNTS)
since that's the real precondition -- schwab_client short-circuits before ever
reaching fake_broker for these specific dry_run scenarios, by design (that's
the exact behavior being proven). daemon_exception_survival is the only
scenario in this file with no broker interaction at all and doesn't take the
fake_broker fixture; the other 4 test functions do take it as a real pytest
fixture argument (see scripts/coverage_registry.py's _uses_fake_broker_fixture,
which requires actual fixture injection, not just the string "fake_broker"
appearing in this file -- fixed 2026-08-01 after 2 test files in this same
commit were found gaming the older, weaker text-scan check).
"""
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import active_signals
import signals_config
import signals_db
import signals_notify
import schwab_client
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_DRY_RUN_SCENARIO'
_IN_WINDOW_TIME = datetime(2026, 7, 29, 10, 30)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated DB + state file setup matching test_fake_broker_check_order_guards_scenario.py pattern."""
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
    Path(tmp_db.name).unlink(missing_ok=True)


def _add_dry_run_node(ticker, notional=5000):
    """Helper to add a node with account='ira' (which is dry_run=True)."""
    signals_db.add_node(ticker, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, mode='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account='ira', starting_notional=notional)
    return [n for n in signals_db.get_watchlist()
            if n['ticker'] == ticker and n['account'] == 'ira'][0]


# =============================================================================
# Branch 1: sl_async_fallback (sl_placement_fast_confirm_timeout)
# =============================================================================

def test_sl_async_fallback_timeout_when_fill_not_confirmed(env, fake_broker, monkeypatch):
    """Test that _sync_confirm_and_protect logs sl_placement_fast_confirm_timeout
    when get_filled_order never returns a fill (timeout branch)."""
    node = _add_dry_run_node(TICKER)

    # Mock get_filled_order to always return None (simulating timeout)
    original_get_filled = schwab_client.get_filled_order
    call_count = [0]

    def mock_never_fills(account, ticker, side, order_id=None):
        call_count[0] += 1
        return None

    monkeypatch.setattr(schwab_client, 'get_filled_order', mock_never_fills)

    # --- act: call _sync_confirm_and_protect with a fake order_id ---
    # This should timeout after _SL_FAST_CONFIRM_ATTEMPTS * _SL_FAST_CONFIRM_INTERVAL_SECS
    # (5 attempts * 2 sec = 10 sec if real sleeps; we'll reduce interval for speed)
    monkeypatch.setattr(signals_notify, '_SL_FAST_CONFIRM_ATTEMPTS', 2)
    monkeypatch.setattr(signals_notify, '_SL_FAST_CONFIRM_INTERVAL_SECS', 0.01)

    signals_notify._sync_confirm_and_protect(TICKER, node, order_id=9999999)

    # --- post-state: verify coverage event was logged ---
    events = signals_db.get_coverage_events(scenario_key='sl_placement_fast_confirm_timeout')
    assert any(e['ticker'] == TICKER and e['result'] == 'timed_out' for e in events), (
        "expected sl_placement_fast_confirm_timeout event with result='timed_out' to be logged"
    )

    # Verify get_filled_order was polled multiple times (not just once)
    assert call_count[0] == 2, f"expected 2 poll attempts, got {call_count[0]}"


# =============================================================================
# Branch 2: daemon_exception_survival (daemon_section_exception)
# =============================================================================

def test_daemon_exception_survival_guarded_catches_exception(env):
    """Test that _guarded catches an unhandled exception, logs it, and doesn't
    re-raise it (proving daemon resilience)."""

    def failing_section():
        raise ValueError("simulated section failure")

    # --- act: call _guarded with a failing function ---
    result = active_signals._guarded("test_section", failing_section)

    # --- post-state: verify no exception propagated and coverage was logged ---
    assert result is None, "guarded must return None on exception"

    events = signals_db.get_coverage_events(scenario_key='daemon_section_exception')
    assert any(e['result'] == 'test_section' for e in events), (
        "expected daemon_section_exception event with result='test_section' to be logged"
    )


def test_daemon_exception_survival_guarded_returns_success_on_normal_path(env):
    """Mirror test: _guarded must also pass through normal returns, not just
    catch exceptions."""

    def succeeding_section():
        return {"status": "ok", "count": 42}

    # --- act: call _guarded with a normal function ---
    result = active_signals._guarded("success_section", succeeding_section)

    # --- post-state: verify return value passed through ---
    assert result == {"status": "ok", "count": 42}, "guarded must pass through normal returns"


# =============================================================================
# Branch 3: dry_run_buy_synthesis (entry_fill, mode='dry_run')
# =============================================================================

def test_dry_run_buy_synthesis_fills_pending_buy(env, fake_broker, monkeypatch):
    """Test that update_dry_run_buys synthesizes a fill for a pending buy on a
    dry_run account, opening a position and logging entry_fill."""
    node = _add_dry_run_node(TICKER, notional=5000)

    # Create a pending buy (simulating a BUY signal that was routed here)
    sig = {'current_price': 10.15, 'last_bar': _IN_WINDOW_TIME}
    signals_db.add_pending_buy(node, sig, channel='C0TEST', ts='1234.5', order_id=8888888888)

    # Mock the price lookup to return a specific price for the fill
    def mock_current_price(ticker):
        if ticker == TICKER:
            return (11.0, None)  # (price, volume)
        return (None, None)

    import signals_compute
    monkeypatch.setattr(signals_compute, '_current_price', mock_current_price)

    # --- act: call update_dry_run_buys ---
    signals_notify.update_dry_run_buys()

    # --- post-state: verify position was opened ---
    pos = signals_db.get_open_position(TICKER)
    assert pos is not None, "expected position to be opened"
    assert pos['is_dry_run_sim'] == 1, "expected is_dry_run_sim=1 tag"
    assert pos['shares'] > 0, "expected shares > 0"

    # Verify coverage event was logged (entry_fill, mode='dry_run')
    events = signals_db.get_coverage_events(scenario_key='entry_fill')
    dry_run_events = [e for e in events if e['ticker'] == TICKER and e['mode'] == 'dry_run']
    assert any(e['result'] == 'sim_filled' for e in dry_run_events), (
        "expected entry_fill event with mode='dry_run' and result='sim_filled'"
    )

    # Verify pending buy was cleared
    pending = signals_db.get_pending_buys()
    assert all(p['ticker'] != TICKER for p in pending), "pending buy should be cleared after fill"


def test_dry_run_buy_synthesis_skip_duplicate_position(env, fake_broker, monkeypatch):
    """Mirror test: if a position already exists for this node, the synthesized
    fill should skip (not reopen/duplicate) and not log a coverage event."""
    node = _add_dry_run_node(TICKER, notional=5000)

    # Pre-open a position (simulating a prior fill already landed)
    sig = {'current_price': 10.15, 'last_bar': _IN_WINDOW_TIME}
    opened1 = signals_db.open_position(node, signal_price=10.15, signal_time=_IN_WINDOW_TIME,
                                       entry_price=10.0, entry_time=_IN_WINDOW_TIME, shares=10,
                                       is_dry_run_sim=True)
    assert opened1, "setup: first open should succeed"

    # Create a pending buy (leftover from a retry/resubmission)
    signals_db.add_pending_buy(node, sig, channel='C0TEST', ts='1234.5', order_id=8888888888)

    # Mock the price lookup
    def mock_current_price(ticker):
        if ticker == TICKER:
            return (11.0, None)
        return (None, None)

    import signals_compute
    monkeypatch.setattr(signals_compute, '_current_price', mock_current_price)

    # --- act: call update_dry_run_buys again ---
    signals_notify.update_dry_run_buys()

    # --- post-state: verify position was NOT re-opened (shares unchanged) ---
    pos = signals_db.get_open_position(TICKER)
    assert pos['shares'] == 10, "position should not be modified by the duplicate fill attempt"

    # Verify pending buy was cleared (same as success path)
    pending = signals_db.get_pending_buys()
    assert all(p['ticker'] != TICKER for p in pending), "pending buy should be cleared even on duplicate"

    # Verify no duplicate entry_fill event was logged for this skip (docstring
    # claim previously had no matching assertion -- fixed 2026-08-01)
    events = signals_db.get_coverage_events(scenario_key='entry_fill', mode='dry_run')
    ticker_events = [e for e in events if e['ticker'] == TICKER]
    assert len(ticker_events) == 0, (
        f"expected no entry_fill event for the skipped duplicate, got: {ticker_events}"
    )


# =============================================================================
# Branch 4: dry_run_sim_close (exit_fill, mode='dry_run')
# =============================================================================

def test_dry_run_sim_close_exits_on_signal(env, fake_broker, monkeypatch):
    """Test that check_dry_run_sim_sells closes a dry_run_sim position when an
    exit condition (e.g., SL trigger) is detected, and logs exit_fill."""
    node = _add_dry_run_node(TICKER, notional=5000)

    # Open a dry_run_sim position
    sig = {'current_price': 10.0, 'last_bar': _IN_WINDOW_TIME}
    signal_price = 10.0
    entry_price = 10.0
    opened = signals_db.open_position(node, signal_price=signal_price, signal_time=_IN_WINDOW_TIME,
                                       entry_price=entry_price, entry_time=_IN_WINDOW_TIME, shares=50,
                                       is_dry_run_sim=True)
    assert opened, "setup: position should open"
    pos_id = signals_db.get_open_position(TICKER)['id']

    # Mock the price/bar data to trigger an SL exit (price falls below SL)
    # node.stop_loss = 1%, so SL price = 10.0 * 0.99 = 9.9
    exit_price = 9.85  # Below SL

    def mock_current_price(ticker):
        if ticker == TICKER:
            return (exit_price, None)
        return (None, None)

    import signals_compute
    monkeypatch.setattr(signals_compute, '_current_price', mock_current_price)

    # Mock the load_cache to provide a valid hourly bar at the exit price
    # (the actual bar data doesn't matter much, just needs to be valid)
    import pandas as pd
    last_bar_ts = pd.Timestamp(_IN_WINDOW_TIME)

    def mock_load_cache(ticker):
        if ticker == TICKER:
            df = pd.DataFrame({
                'Open': [10.0],
                'High': [10.0],
                'Low': [exit_price],
                'Close': [exit_price],
                'Volume': [1000000]
            }, index=pd.DatetimeIndex([last_bar_ts], name='Datetime'))
            return (df, None)
        return (None, None)

    # --- act: call check_dry_run_sim_sells ---
    dry_run_sell_alerted = set()
    last_seen_bar = {}  # Dict mapping pos_key -> last timestamp seen
    signals_notify.check_dry_run_sim_sells(last_seen_bar, dry_run_sell_alerted, mock_load_cache)

    # --- post-state: verify position was closed ---
    pos = signals_db.get_open_position(TICKER)
    assert pos is None, "expected position to be closed"

    # Verify exit_fill event was logged (mode='dry_run')
    events = signals_db.get_coverage_events(scenario_key='exit_fill')
    dry_run_events = [e for e in events if e['ticker'] == TICKER and e['mode'] == 'dry_run']
    assert len(dry_run_events) > 0, "expected exit_fill event with mode='dry_run'"
    assert any(e['result'] == 'SL' for e in dry_run_events), (
        "expected exit_fill event with result='SL' (the exit reason)"
    )


def test_dry_run_sim_close_skip_already_alerted(env, fake_broker, monkeypatch):
    """Mirror test: check_dry_run_sim_sells should skip a position already
    alerted in the same bar (doesn't re-close)."""
    node = _add_dry_run_node(TICKER, notional=5000)

    # Open a dry_run_sim position
    sig = {'current_price': 10.0, 'last_bar': _IN_WINDOW_TIME}
    opened = signals_db.open_position(node, signal_price=10.0, signal_time=_IN_WINDOW_TIME,
                                       entry_price=10.0, entry_time=_IN_WINDOW_TIME, shares=50,
                                       is_dry_run_sim=True)
    assert opened, "setup: position should open"
    pos = signals_db.get_open_position(TICKER)
    pos_id = pos['id']

    # Pre-populate the dry_run_sell_alerted set to simulate a prior close attempt
    import pandas as pd
    last_bar_ts = pd.Timestamp(_IN_WINDOW_TIME)
    dry_run_sell_alerted = {(pos_id, last_bar_ts)}
    last_seen_bar = {}  # Dict mapping pos_key -> last timestamp seen

    def mock_current_price(ticker):
        if ticker == TICKER:
            return (9.85, None)  # Below SL again
        return (None, None)

    import signals_compute
    monkeypatch.setattr(signals_compute, '_current_price', mock_current_price)

    def mock_load_cache(ticker):
        if ticker == TICKER:
            df = pd.DataFrame({
                'Open': [10.0],
                'High': [10.0],
                'Low': [9.85],
                'Close': [9.85],
                'Volume': [1000000]
            }, index=pd.DatetimeIndex([last_bar_ts], name='Datetime'))
            return (df, None)
        return (None, None)

    # --- act: call check_dry_run_sim_sells again ---
    signals_notify.check_dry_run_sim_sells(last_seen_bar, dry_run_sell_alerted, mock_load_cache)

    # --- post-state: position should still be open (skipped the duplicate close) ---
    pos = signals_db.get_open_position(TICKER)
    assert pos is not None, "position should still be open (skipped by dry_run_sell_alerted guard)"
    assert pos['id'] == pos_id, "position id should be unchanged"
