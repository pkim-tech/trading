"""Fake-venue truth table for the 12 schwab_safety.check_order guard rows
that had ZERO proof of any kind before this file -- not live, not dry_run,
not even a plain-mocked unit test that asserted the real coverage_events
call (scripts/coverage_proof_matrix.py's NONE tier, 2026-08-14). Every one
of these rows already has an existing plain-mocked test in
test_schwab_safety.py that asserts the raised SafetyViolation, but none of
those tests ever call signals_db.get_coverage_events(scenario_key=...) --
so offline_proof_for() (scripts/coverage_registry.py) can't find any real
evidence the log_coverage_event call site itself actually fires, only that
the guard's exception message is right. This file closes that gap the same
way test_fake_broker_check_order_guards_scenario.py /
_phase2_scenario.py did for their own rows: drive the real
schwab_client.place_equity_buy/sell -> schwab_safety.check_order ->
approve_and_record chain against tests/fake_broker.py, and assert the real
coverage_events row lands, not just the exception.

Covers:
  - unknown_account_block
  - account_disabled_block
  - ticker_not_live_mode_block
  - ticker_account_assignment_mismatch
  - ticker_not_in_automation_scope_block
  - ticker_level_automation_pause
  - buy_trading_day_block
  - buy_signal_window_block
  - hard_order_ceiling_block
  - notional_cap_block
  - daily_order_cap_block
  - global_burst_cap_block
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

TICKER = 'TEST_PHASE3_GUARDS'
TICKER_TWO = 'TEST_PHASE3_GUARDS_2'
# A real NYSE trading day, in-window, matching the pattern of the sibling
# guard-scenario files (2026-07-30 is a Thursday).
_IN_WINDOW_TIME = datetime(2026, 7, 30, 10, 30)


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
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER, TICKER_TWO})
    monkeypatch.setattr(schwab_safety, '_now', lambda: _IN_WINDOW_TIME)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    signals_db.ensure_tables()
    yield
    schwab_safety.disengage_kill_switch()
    schwab_safety.resume_ticker_automation(TICKER)
    Path(tmp_db.name).unlink(missing_ok=True)


def _add_node(ticker, account, notional=5000, state='live'):
    signals_db.add_node(ticker, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state=state,
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account=account, starting_notional=notional)
    return [n for n in signals_db.get_watchlist()
            if n['ticker'] == ticker and n['account'] == account][0]


def _real_placed_orders(fake_broker_, ticker):
    return [o for o in fake_broker_.orders.values()
            if o['orderLegCollection'][0]['instrument']['symbol'] == ticker]


# ===========================================================================
# unknown_account_block
# ===========================================================================

def test_unknown_account_block(env, fake_broker):
    _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)

    with pytest.raises(schwab_safety.SafetyViolation, match="not in the allowlist"):
        schwab_client.place_equity_buy('made_up_test_account', TICKER, 10, 10.0)

    assert _real_placed_orders(fake_broker, TICKER) == []
    events = signals_db.get_coverage_events(scenario_key='unknown_account_block')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events)


# ===========================================================================
# account_disabled_block
# ===========================================================================

def test_account_disabled_block(env, fake_broker, monkeypatch):
    _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    monkeypatch.setattr(schwab_safety.ACCOUNTS['soxl_ira'], 'enabled', False)

    with pytest.raises(schwab_safety.SafetyViolation, match="disabled"):
        schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)

    assert _real_placed_orders(fake_broker, TICKER) == []
    events = signals_db.get_coverage_events(scenario_key='account_disabled_block')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events)


# ===========================================================================
# ticker_not_live_mode_block
# ===========================================================================

def test_ticker_not_live_mode_block(env, fake_broker):
    # A 'paper' node is deliberately excluded from _live_ticker_accounts() --
    # mirrors the real 2026-07-26 shape (a signal check earlier in the same
    # poll cycle saw the node as 'live', but by order-placement time it had
    # been demoted).
    _add_node(TICKER, 'soxl_ira', state='paper')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    with pytest.raises(schwab_safety.SafetyViolation, match="not a live-mode ticker"):
        schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)

    assert _real_placed_orders(fake_broker, TICKER) == []
    events = signals_db.get_coverage_events(scenario_key='ticker_not_live_mode_block')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events)


# ===========================================================================
# ticker_account_assignment_mismatch
# ===========================================================================

def test_ticker_account_assignment_mismatch(env, fake_broker):
    _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('ira', 1_000_000.0)

    with pytest.raises(schwab_safety.SafetyViolation, match="not assigned to account"):
        schwab_client.place_equity_buy('ira', TICKER, 10, 10.0)

    assert _real_placed_orders(fake_broker, TICKER) == []
    events = signals_db.get_coverage_events(scenario_key='ticker_account_assignment_mismatch')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events)


# ===========================================================================
# ticker_not_in_automation_scope_block
# ===========================================================================

def test_ticker_not_in_automation_scope_block(env, fake_broker):
    out_of_scope = 'TEST_PHASE3_OUT_OF_SCOPE'
    _add_node(out_of_scope, 'soxl_ira')
    fake_broker.set_quote(out_of_scope, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    with pytest.raises(schwab_safety.SafetyViolation, match="automation pilot scope"):
        schwab_client.place_equity_buy('soxl_ira', out_of_scope, 10, 10.0)

    assert _real_placed_orders(fake_broker, out_of_scope) == []
    events = signals_db.get_coverage_events(scenario_key='ticker_not_in_automation_scope_block')
    assert any(e['ticker'] == out_of_scope and e['result'] == 'blocked' for e in events)


# ===========================================================================
# ticker_level_automation_pause
# ===========================================================================

def test_ticker_level_automation_pause_blocks_then_resume_unblocks(env, fake_broker):
    _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    schwab_safety.pause_ticker_automation(TICKER, reason="test pause")
    with pytest.raises(schwab_safety.SafetyViolation, match="automation is paused"):
        schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert _real_placed_orders(fake_broker, TICKER) == []
    events = signals_db.get_coverage_events(scenario_key='ticker_level_automation_pause')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events)

    schwab_safety.resume_ticker_automation(TICKER)
    r, oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid is not None, "resuming ticker automation must unblock the same ticker's real order"
    assert len(_real_placed_orders(fake_broker, TICKER)) == 1


# ===========================================================================
# buy_trading_day_block
# ===========================================================================

def test_buy_trading_day_block(env, fake_broker, monkeypatch):
    _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    monkeypatch.setattr(schwab_safety, '_is_trading_day', lambda date_str: False)

    with pytest.raises(schwab_safety.SafetyViolation, match="not an NYSE trading day"):
        schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)

    assert _real_placed_orders(fake_broker, TICKER) == []
    events = signals_db.get_coverage_events(scenario_key='buy_trading_day_block')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events)


def test_sell_not_blocked_by_trading_day_gate(env, fake_broker, monkeypatch):
    """buy_trading_day_block is BUY-only (see check_order) -- a real SELL
    (e.g. a stop-loss firing) must never be blocked by this gate, mirroring
    the already-proven SELL exemption on the signal-window gate below."""
    _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    r, oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid is not None
    signals_db.open_position(
        _add_node(TICKER, 'soxl_ira'), signal_price=10.0, signal_time=_IN_WINDOW_TIME,
        entry_price=10.0, entry_time=_IN_WINDOW_TIME, shares=10,
    )
    monkeypatch.setattr(schwab_safety, '_is_trading_day', lambda date_str: False)

    r2, oid2 = schwab_client.place_equity_sell('soxl_ira', TICKER, 10, 10.0)
    assert oid2 is not None, "SELL must not be blocked by the BUY-only trading-day gate"


# ===========================================================================
# buy_signal_window_block
# ===========================================================================

def test_buy_signal_window_block(env, fake_broker, monkeypatch):
    _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    # Noon -- outside both _SIGNAL_WINDOWS and _OPEN_CHECK_WINDOWS.
    monkeypatch.setattr(schwab_safety, '_now', lambda: _IN_WINDOW_TIME.replace(hour=12, minute=0))

    with pytest.raises(schwab_safety.SafetyViolation, match="outside signal windows"):
        schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)

    assert _real_placed_orders(fake_broker, TICKER) == []
    events = signals_db.get_coverage_events(scenario_key='buy_signal_window_block')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events)


# ===========================================================================
# hard_order_ceiling_block
# ===========================================================================

def test_hard_order_ceiling_block(env, fake_broker, monkeypatch):
    _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 10_000_000.0)
    # notional_cap would fire first at the account's real (much lower) cap --
    # raise it out of the way so this test isolates the absolute ceiling.
    monkeypatch.setattr(schwab_safety.ACCOUNTS['soxl_ira'], 'notional_cap',
                         schwab_safety.HARD_ORDER_CEILING + 1_000_000)
    over_ceiling_qty = (schwab_safety.HARD_ORDER_CEILING // 10) + 1

    with pytest.raises(schwab_safety.SafetyViolation, match="exceeds hard ceiling"):
        schwab_client.place_equity_buy('soxl_ira', TICKER, over_ceiling_qty, 10.0)

    assert _real_placed_orders(fake_broker, TICKER) == []
    events = signals_db.get_coverage_events(scenario_key='hard_order_ceiling_block')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events)


# ===========================================================================
# notional_cap_block
# ===========================================================================

def test_notional_cap_block(env, fake_broker):
    _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    cap = schwab_safety.ACCOUNTS['soxl_ira'].notional_cap
    over_cap_qty = int(cap // 10) + 10  # notional well past the account's real cap

    with pytest.raises(schwab_safety.SafetyViolation, match="exceeds soxl_ira cap"):
        schwab_client.place_equity_buy('soxl_ira', TICKER, over_cap_qty, 10.0)

    assert _real_placed_orders(fake_broker, TICKER) == []
    events = signals_db.get_coverage_events(scenario_key='notional_cap_block')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events)


def test_notional_cap_does_not_block_sell(env, fake_broker):
    """notional_cap bounds new risk-adding exposure (BUY) only -- a real
    position that grew past the cap must still be exitable (found live
    2026-07-24, see check_order's docstring)."""
    node = _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    signals_db.open_position(node, signal_price=10.0, signal_time=_IN_WINDOW_TIME,
                              entry_price=10.0, entry_time=_IN_WINDOW_TIME, shares=1000)
    cap = schwab_safety.ACCOUNTS['soxl_ira'].notional_cap
    over_cap_qty = int(cap // 10) + 10

    r, oid = schwab_client.place_equity_sell('soxl_ira', TICKER, over_cap_qty, 10.0)
    assert oid is not None, "SELL must not be blocked by notional_cap even above the cap"


# ===========================================================================
# daily_order_cap_block
# ===========================================================================

def test_daily_order_cap_block(env, fake_broker, monkeypatch):
    monkeypatch.setattr(schwab_safety.ACCOUNTS['soxl_ira'], 'daily_order_cap', 1)
    _add_node(TICKER, 'soxl_ira', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    r1, oid1 = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid1 is not None

    with pytest.raises(schwab_safety.SafetyViolation, match="daily order cap"):
        # A different quantity than the first order, so this isn't instead
        # caught (and misattributed) by the duplicate-order-window guard.
        schwab_client.place_equity_buy('soxl_ira', TICKER, 40, 10.0)

    assert len(_real_placed_orders(fake_broker, TICKER)) == 1
    events = signals_db.get_coverage_events(scenario_key='daily_order_cap_block')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events)


def test_daily_order_cap_does_not_block_sell(env, fake_broker, monkeypatch):
    """A SELL (including a stop-loss placement) is unconditionally exempt
    from the daily cap since 2026-07-25 -- an exhausted cap from unrelated
    earlier BUYs must never block a real exit."""
    node = _add_node(TICKER, 'soxl_ira', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    monkeypatch.setattr(schwab_safety.ACCOUNTS['soxl_ira'], 'daily_order_cap', 1)

    r1, oid1 = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid1 is not None
    signals_db.open_position(node, signal_price=10.0, signal_time=_IN_WINDOW_TIME,
                              entry_price=10.0, entry_time=_IN_WINDOW_TIME, shares=10)

    r2, oid2 = schwab_client.place_equity_sell('soxl_ira', TICKER, 10, 10.0)
    assert oid2 is not None, "SELL must not be blocked by an exhausted daily BUY cap"


# ===========================================================================
# global_burst_cap_block
# ===========================================================================

def test_global_burst_cap_block(env, fake_broker, monkeypatch):
    monkeypatch.setattr(schwab_safety, 'GLOBAL_ORDERS_PER_MINUTE', 1)
    _add_node(TICKER, 'soxl_ira', notional=50_000)
    _add_node(TICKER_TWO, 'soxl_ira', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_quote(TICKER_TWO, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    r1, oid1 = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid1 is not None

    # A different ticker, so this isn't caught by the per-account duplicate-
    # order guard instead -- this test isolates the account-agnostic global
    # burst cap specifically.
    with pytest.raises(schwab_safety.SafetyViolation, match="global burst cap"):
        schwab_client.place_equity_buy('soxl_ira', TICKER_TWO, 10, 10.0)

    assert _real_placed_orders(fake_broker, TICKER_TWO) == []
    events = signals_db.get_coverage_events(scenario_key='global_burst_cap_block')
    assert any(e['result'] == 'blocked' for e in events)
