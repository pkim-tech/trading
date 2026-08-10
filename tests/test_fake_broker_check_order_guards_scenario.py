"""Fake-venue truth table for 4 of schwab_safety.check_order's guards that
had never been exercised against a real (fake) broker order book, only
plain-mocked unit tests (test_schwab_safety.py) or never at all:
  - kill_switch_block        (global kill switch blocks a real placement)
  - dup_order_no_false_block (a genuinely different order does NOT get
                               falsely caught by the duplicate-order guard)
  - two_nodes_same_ticker_diff_accounts (2 live nodes, same ticker, separate
                               accounts -- both allowed through independently)
  - node_level_automation_pause (a paused node's real order is blocked;
                               resuming unblocks it)

Each test drives the real schwab_client.place_equity_buy -> schwab_safety.
check_order -> approve_and_record chain against tests/fake_broker.py, and
asserts both the real coverage_events row AND whether an order actually
reached (or didn't reach) the fake broker -- the two guard-isolation unit
tests in test_schwab_safety.py only ever assert the raised exception /
in-process coverage_events call, never a real order book."""
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

TICKER = 'TEST_GUARDS_SCENARIO'
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
    signals_db.add_node(ticker, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account=account, starting_notional=notional)
    return [n for n in signals_db.get_watchlist()
            if n['ticker'] == ticker and n['account'] == account][0]


def _real_placed_orders(fake_broker_, ticker):
    return [o for o in fake_broker_.orders.values()
            if o['orderLegCollection'][0]['instrument']['symbol'] == ticker]


def test_kill_switch_blocks_real_placement(env, fake_broker):
    _add_node(TICKER, 'roth')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('roth', 100_000.0)
    schwab_safety.engage_kill_switch("test halt")

    with pytest.raises(schwab_safety.SafetyViolation, match="kill switch"):
        schwab_client.place_equity_buy('roth', TICKER, 10, 10.0)

    assert _real_placed_orders(fake_broker, TICKER) == [], \
        "kill switch must block before any real order reaches the broker"
    events = signals_db.get_coverage_events(scenario_key='kill_switch_block')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events)


def test_dup_order_does_not_false_block_a_genuinely_different_order(env, fake_broker):
    # soxl_ira, not ira -- ira is dry_run=True and short-circuits before
    # ever reaching the broker (place_equity_buy returns (None, None) by
    # design), which would make a real-order-count assertion meaningless.
    _add_node(TICKER, 'soxl_ira', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    # First order fills immediately (fake_broker's default), freeing the
    # resting-order dup guard; the *quantity-window* dup guard (the one this
    # test targets) is a separate, time-based check against recent_orders,
    # independent of whether the prior order is still resting.
    r1, oid1 = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid1 is not None

    # Second order: same ticker/account/side, but a quantity far outside
    # DUPLICATE_ORDER_QUANTITY_TOLERANCE_PCT (a legitimate top-up-shaped
    # resubmission, not an accidental double-click) -- must NOT be caught by
    # the duplicate-order-window guard.
    r2, oid2 = schwab_client.place_equity_buy('soxl_ira', TICKER, 40, 10.0)
    assert oid2 is not None, "a genuinely different-sized order must not be false-blocked as a duplicate"

    placed = _real_placed_orders(fake_broker, TICKER)
    assert len(placed) == 2, f"expected 2 real distinct orders to reach the broker, got {len(placed)}"


def test_dup_order_correctly_blocks_a_true_duplicate(env, fake_broker):
    """Mirror of the false-block test above -- proves the guard's positive
    case still works (a same-quantity resubmission within the duplicate
    window IS blocked), so both directions of this guard are proven, not
    just the "doesn't false-block" half."""
    _add_node(TICKER, 'soxl_ira', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    r1, oid1 = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid1 is not None

    with pytest.raises(schwab_safety.SafetyViolation, match="duplicate"):
        schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)

    assert len(_real_placed_orders(fake_broker, TICKER)) == 1, \
        "the true duplicate must never reach the broker"
    events = signals_db.get_coverage_events(scenario_key='dup_order_window_blocked')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events)


def test_two_nodes_same_ticker_diff_accounts_both_allowed(env, fake_broker):
    # ira (dry_run=True) + soxl_ira (dry_run=False) -- deliberately mixed,
    # matching the real account population (only one real/live account
    # exists in ACCOUNTS today). The guard itself doesn't care about
    # dry_run; what matters is neither account's order is rejected as
    # "assigned to the wrong account" for this ticker.
    _add_node(TICKER, 'roth')
    _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('roth', 1_000_000.0)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    r1, oid1 = schwab_client.place_equity_buy('roth', TICKER, 10, 10.0)  # dry_run -> (None, None), no exception
    r2, oid2 = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid2 is not None, "the real (non-dry_run) account's node must be allowed through"

    assert len(_real_placed_orders(fake_broker, TICKER)) == 1  # only soxl_ira actually reaches the broker

    events = signals_db.get_coverage_events(scenario_key='two_nodes_same_ticker_diff_accounts')
    assert any(e['ticker'] == TICKER and e['result'] == 'allowed' for e in events)


def test_node_level_automation_pause_blocks_then_resume_unblocks(env, fake_broker):
    node = _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    schwab_safety.pause_node_automation(node['id'], reason="test pause")
    with pytest.raises(schwab_safety.SafetyViolation, match="automation paused"):
        schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert _real_placed_orders(fake_broker, TICKER) == [], \
        "a paused node's order must never reach the broker"
    events = signals_db.get_coverage_events(scenario_key='node_level_automation_pause')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events)

    schwab_safety.resume_node_automation(node['id'])
    r, oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid is not None, "resuming automation must unblock the same node's real order"
    assert len(_real_placed_orders(fake_broker, TICKER)) == 1
