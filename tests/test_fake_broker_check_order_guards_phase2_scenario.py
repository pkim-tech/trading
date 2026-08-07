"""Fake-venue truth table for 6 additional schwab_safety.check_order guards,
all proven live but currently with zero fake-broker regression test coverage:
  - pre_action_state_verification (BUY and SELL)  -- log real broker position
                                   vs local DB belief at order decision time
  - cash_check (pass and fail)     -- verify account has enough cash (both branches)
  - second_ticker_one_account      -- a resting BUY for ticker A should block
                                   a NEW BUY for ticker B in the same account
  - same_day_block                 -- same-day re-buy after same-day sell is
                                   blocked in 'cash' account but allowed
                                   (result="skipped_margin_account") in 'margin'
  - dup_sell_order_blocked         -- a second concurrent SELL for the same
                                   ticker/account is blocked
  - dup_order_retry_after_failure  -- after a real rejected prior order attempt,
                                   a retry is allowed because _broker_confirms_order
                                   checks the real broker

Each test drives the real schwab_client.place_equity_buy/place_equity_sell ->
schwab_safety.check_order -> approve_and_record chain against tests/fake_broker.py.
"""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import signals_config
import signals_db
import schwab_client
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_PHASE2_GUARDS'
_IN_WINDOW_TIME = datetime(2026, 7, 31, 10, 30)


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
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER, 'TICKER_TWO'})
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


# ===========================================================================
# TEST 1: pre_action_state_verification (BUY)
# ===========================================================================

def test_pre_action_state_verification_buy_match_no_position(env, fake_broker):
    """_log_pre_action_state_verification on BUY: real broker shows 0 shares,
    local DB shows None (no open_position row) -- should log 'match'."""
    _add_node(TICKER, 'soxl_ira', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    # Real position on broker: 0 shares (nothing seeded)
    # Local DB: None (no open_position row)
    r, oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid is not None

    events = signals_db.get_coverage_events(scenario_key='pre_action_state_verification')
    assert any(e['ticker'] == TICKER and e['result'] == 'match' for e in events), \
        "pre_action_state_verification should log 'match' when broker 0 matches local None"


def test_pre_action_state_verification_buy_mismatch(env, fake_broker):
    """_log_pre_action_state_verification on BUY: real broker shows 100 shares,
    local DB shows None -- should log 'mismatch'."""
    _add_node(TICKER, 'soxl_ira', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    # Seed a real position at the broker
    fake_broker.seed_resting_order('soxl_ira', TICKER, 'MARKET', 'BUY', 100, status='FILLED')

    # But no open_position row in local DB (simulating a sync gap)
    r, oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid is not None

    events = signals_db.get_coverage_events(scenario_key='pre_action_state_verification')
    assert any(e['ticker'] == TICKER and e['result'] == 'mismatch' for e in events), \
        "pre_action_state_verification should log 'mismatch' when broker 100 != local None"


def test_pre_action_state_verification_sell_match(env, fake_broker):
    """_log_pre_action_state_verification on SELL: real broker shows 50 shares,
    local DB also shows 50 shares -- should log 'match'."""
    _add_node(TICKER, 'soxl_ira', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    # Seed an open position (fake broker has 50 shares)
    fake_broker.seed_resting_order('soxl_ira', TICKER, 'MARKET', 'BUY', 50, status='FILLED')

    # Also seed local DB position (50 shares)
    node = _add_node(TICKER, 'soxl_ira')
    signals_db.open_position(node, signal_price=10.0, signal_time=_IN_WINDOW_TIME,
                            entry_price=10.0, entry_time=_IN_WINDOW_TIME, shares=50)

    # Now place a SELL
    r, oid = schwab_client.place_equity_sell('soxl_ira', TICKER, 50, 10.0)
    assert oid is not None

    events = signals_db.get_coverage_events(scenario_key='pre_action_state_verification')
    assert any(e['ticker'] == TICKER and e['result'] == 'match' for e in events), \
        "pre_action_state_verification should log 'match' when broker 50 matches local 50"


# ===========================================================================
# TEST 2: cash_check (pass and fail)
# ===========================================================================

def test_cash_check_passes_with_sufficient_cash(env, fake_broker):
    """cash_check: account has enough cash to cover notional + buffer -- should
    pass (result='passed')."""
    _add_node(TICKER, 'soxl_ira', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    # Set cash to more than (10 shares * $10 + buffer)
    fake_broker.set_cash_balance('soxl_ira', 50_000.0)

    r, oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid is not None

    events = signals_db.get_coverage_events(scenario_key='cash_check')
    assert any(e['ticker'] == TICKER and e['result'] == 'passed' for e in events), \
        "cash_check should log 'passed' when sufficient cash"


def test_cash_check_blocks_insufficient_cash(env, fake_broker):
    """cash_check: account does NOT have enough cash to cover notional + buffer
    -- should block (result='blocked_insufficient')."""
    _add_node(TICKER, 'soxl_ira', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    # Set cash to less than (10 shares * $10 + buffer)
    fake_broker.set_cash_balance('soxl_ira', 50.0)

    with pytest.raises(schwab_safety.SafetyViolation, match="cash"):
        schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)

    assert _real_placed_orders(fake_broker, TICKER) == [], \
        "cash check must block before any real order reaches the broker"

    events = signals_db.get_coverage_events(scenario_key='cash_check')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked_insufficient' for e in events), \
        "cash_check should log 'blocked_insufficient' when insufficient cash"


# ===========================================================================
# TEST 3: second_ticker_one_account
# ===========================================================================

def test_second_ticker_buy_blocked_by_first_ticker_resting_order(env, fake_broker):
    """second_ticker_one_account: a resting BUY order for TICKER in 'soxl_ira'
    should block a NEW BUY for 'TICKER_TWO' in the same account."""
    _add_node(TICKER, 'soxl_ira', notional=50_000)
    _add_node('TICKER_TWO', 'soxl_ira', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_quote('TICKER_TWO', last=20.0, bid=20.0, ask=20.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    # Place first BUY for TICKER (will fill immediately with fake_broker's MARKET default)
    r1, oid1 = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid1 is not None

    # Seed a resting BUY order for TICKER so it stays open (simulate it stayed WORKING)
    fake_broker.orders[oid1]['status'] = 'WORKING'

    # Try to place a BUY for TICKER_TWO in the same account -- should be blocked
    with pytest.raises(schwab_safety.SafetyViolation, match="already has a resting BUY"):
        schwab_client.place_equity_buy('soxl_ira', 'TICKER_TWO', 10, 20.0)

    assert len([o for o in fake_broker.orders.values()
                if o['orderLegCollection'][0]['instrument']['symbol'] == 'TICKER_TWO']) == 0, \
        "TICKER_TWO order must not reach broker while TICKER BUY is resting"

    events = signals_db.get_coverage_events(scenario_key='second_ticker_buy_blocked')
    assert any(e['ticker'] == 'TICKER_TWO' and e['result'] == 'blocked' for e in events), \
        "second_ticker_buy_blocked should log when second ticker BUY is blocked"


# ===========================================================================
# TEST 4: same_day_block (cash vs margin)
# ===========================================================================

def test_same_day_block_cash_account_blocked(env, fake_broker):
    """same_day_block: a same-day re-buy in a 'cash' account should be blocked
    after a same-day exit (result='blocked')."""
    _add_node(TICKER, 'ira', notional=50_000)  # ira is a 'cash' account
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('ira', 1_000_000.0)

    # Seed a closed trade for today
    today = datetime.now().strftime('%Y-%m-%d')
    with signals_db._conn() as c:
        c.execute("""
            INSERT INTO trade_log
                (ticker, strategy, version, window, stop_loss, max_hold_hours, signal_price, signal_time,
                 entry_price, entry_time, entry_drift_pct, exit_price, exit_time, exit_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            TICKER, 'TrailingBothZScoreBreakout', 'test', 10, 1, 105,
            10.0, f"{today} 10:30:00",
            10.0, f"{today} 10:31:00",
            0.0,  # entry_drift_pct
            10.5, f"{today} 14:30:00",  # exit today
            "TRAIL",
        ))
        c.commit()

    # Try to place a BUY on the same day -- should be blocked
    with pytest.raises(schwab_safety.SafetyViolation, match="same-day"):
        schwab_client.place_equity_buy('ira', TICKER, 10, 10.0)

    events = signals_db.get_coverage_events(scenario_key='same_day_block')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events), \
        "same_day_block should log 'blocked' for cash account"


def test_same_day_block_margin_account_allowed(env, fake_broker):
    """same_day_block: a same-day re-buy in a 'margin' account should be
    allowed even after a same-day exit (result='skipped_margin_account')."""
    _add_node(TICKER, 'soxl_ira', notional=50_000)  # soxl_ira is 'margin'
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    # Seed a closed trade for today
    today = datetime.now().strftime('%Y-%m-%d')
    with signals_db._conn() as c:
        c.execute("""
            INSERT INTO trade_log
                (ticker, strategy, version, window, stop_loss, max_hold_hours, signal_price, signal_time,
                 entry_price, entry_time, entry_drift_pct, exit_price, exit_time, exit_reason, account)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            TICKER, 'TrailingBothZScoreBreakout', 'test', 10, 1, 105,
            10.0, f"{today} 10:30:00",
            10.0, f"{today} 10:31:00",
            0.0,  # entry_drift_pct
            10.5, f"{today} 14:30:00",  # exit today
            "TRAIL",
            'soxl_ira',
        ))
        c.commit()

    # Try to place a BUY on the same day -- should be ALLOWED for margin account
    r, oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid is not None, "margin account should be allowed same-day re-buy"

    events = signals_db.get_coverage_events(scenario_key='same_day_block')
    assert any(e['ticker'] == TICKER and e['result'] == 'skipped_margin_account' for e in events), \
        "same_day_block should log 'skipped_margin_account' for margin account"


# ===========================================================================
# TEST 5: dup_sell_order_blocked
# ===========================================================================

def test_dup_sell_order_blocked_when_one_resting(env, fake_broker):
    """dup_sell_order_blocked: a second concurrent SELL for the same ticker/account
    should be blocked while the first is still resting."""
    _add_node(TICKER, 'soxl_ira', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    # Seed an open position
    node = _add_node(TICKER, 'soxl_ira')
    signals_db.open_position(node, signal_price=10.0, signal_time=_IN_WINDOW_TIME,
                            entry_price=10.0, entry_time=_IN_WINDOW_TIME, shares=100)

    # Place a SELL order (will fail to reach broker because it's not during a
    # signal window, but we can seed it manually)
    sell_order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'STOP', 'SELL', 100,
                                                    stop_price=9.5, status='WORKING')

    # Try to place a second SELL for the same ticker -- should be blocked
    with pytest.raises(schwab_safety.SafetyViolation, match="already has a resting SELL"):
        schwab_client.place_equity_sell('soxl_ira', TICKER, 100, 10.0)

    events = signals_db.get_coverage_events(scenario_key='dup_sell_order_blocked')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events), \
        "dup_sell_order_blocked should log when second SELL is blocked"


# ===========================================================================
# TEST 6: dup_order_retry_after_failure
# ===========================================================================

def test_dup_order_retry_after_failure_allowed(env, fake_broker):
    """dup_order_retry_after_failure: after a real rejected prior order attempt,
    a retry with the same qty/ticker/side is correctly ALLOWED because
    _broker_confirms_order checks the real broker and finds no confirmed order."""
    _add_node(TICKER, 'soxl_ira', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    # Simulate the flow:
    # 1. First attempt: approve_and_record() writes the local recent_orders record
    # 2. Then place_order fails/is rejected at the broker (e.g., network error)
    # 3. Local 'recent_orders' entry exists, but broker has no confirmed order
    # 4. Retry: duplicate-order check finds the local record BUT _broker_confirms_order
    #    returns False (no confirmed order at broker), so the retry is allowed

    # To test this, we manually:
    # 1. Write to the local order counts to simulate approve_and_record having run
    # 2. Seed a REJECTED order in fake_broker (confirmed order NOT accepted)
    # 3. Try placing again -- it should hit the dup check but pass it via the retry path

    import time
    import json

    # Manually write to state file as if approve_and_record() already ran
    state = {
        str(datetime.now().date()): {"soxl_ira": 0},
        "recent_order_timestamps": [time.time() - 10],  # 10s ago
        "recent_orders": [{
            "account": "soxl_ira",
            "ticker": TICKER,
            "side": "BUY",
            "quantity": 10,
            "ts": time.time() - 10,  # 10s ago (within the 60s window)
        }],
    }
    schwab_safety.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    schwab_safety.STATE_PATH.write_text(json.dumps(state))

    # Seed a REJECTED order at the broker (simulating the first attempt failed)
    fake_broker.seed_resting_order('soxl_ira', TICKER, 'MARKET', 'BUY', 10, status='REJECTED')

    # Now retry: the duplicate check will find the local record, call _broker_confirms_order,
    # which returns False (REJECTED order doesn't count), so the retry is allowed
    r, oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid is not None, "retry after broker rejection should be allowed"

    placed = _real_placed_orders(fake_broker, TICKER)
    assert len(placed) >= 2, "the retry order should reach the broker (in addition to the REJECTED one)"

    events = signals_db.get_coverage_events(scenario_key='dup_order_retry_after_failure')
    assert any(e['ticker'] == TICKER and e['result'] == 'allowed_retry' for e in events), \
        "dup_order_retry_after_failure should log 'allowed_retry' when broker had no confirmed prior order"
