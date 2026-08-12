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
# TEST 3: second_ticker_one_account (cash-aware as of 2026-08-07)
# ===========================================================================

def test_second_ticker_buy_allowed_when_cash_covers_both(env, fake_broker):
    """Cash-aware version of the old unconditional guard (real incident,
    2026-08-07: RETL's genuine signal was blocked despite soxl_ira having
    ample headroom after LABD's own resting BUY). When the account has
    enough real cash to cover BOTH the resting order's REAL reserved amount
    (its actual resting quantity x current price, not its node's config)
    AND this new order's notional + buffer, the second ticker's BUY must
    proceed."""
    _add_node(TICKER, 'soxl_ira', notional=5_000)
    _add_node('TICKER_TWO', 'soxl_ira', notional=5_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_quote('TICKER_TWO', last=20.0, bid=20.0, ask=20.01)
    # Enough for TICKER's real $100 reservation (10 shares x $10) + TICKER_TWO's $200 notional + buffer.
    fake_broker.set_cash_balance('soxl_ira', 10_000.0)

    r1, oid1 = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid1 is not None
    fake_broker.orders[oid1]['status'] = 'WORKING'  # stays resting

    r2, oid2 = schwab_client.place_equity_buy('soxl_ira', 'TICKER_TWO', 10, 20.0)
    assert oid2 is not None, "TICKER_TWO's BUY must reach the broker when cash covers both"

    events = signals_db.get_coverage_events(scenario_key='second_ticker_buy_allowed')
    assert any(e['ticker'] == 'TICKER_TWO' and e['result'] == 'allowed_cash_sufficient' for e in events), \
        "second_ticker_buy_allowed should log when cash covers both reservations"


def test_second_ticker_buy_blocked_when_cash_cannot_cover_both(env, fake_broker):
    """The real remaining guard: a resting BUY for TICKER reserves its
    ACTUAL resting quantity x current price (10 shares @ $250 = $2,500 --
    deliberately priced above the node's own $5,000 starting_notional
    config so the real reservation, not a config number, is what drives the
    block; kept under soxl_ira's $3,000 notional_cap so TICKER's own
    placement isn't refused for an unrelated reason) against the account's
    undecremented cash balance -- if the account can't ALSO afford
    TICKER_TWO's order on top of that real reservation, it's still blocked
    (for a real, cash-grounded reason now, not unconditionally). Cash is set
    generously for TICKER's own placement (a real order's cash_check runs at
    ITS OWN placement time), then lowered before TICKER_TWO's attempt to
    simulate the account's real balance having genuinely thinned by the time
    the second signal arrives."""
    _add_node(TICKER, 'soxl_ira', notional=5_000)
    _add_node('TICKER_TWO', 'soxl_ira', notional=5_000)
    fake_broker.set_quote(TICKER, last=250.0, bid=250.0, ask=250.01)
    fake_broker.set_quote('TICKER_TWO', last=20.0, bid=20.0, ask=20.01)
    fake_broker.set_cash_balance('soxl_ira', 100_000.0)

    r1, oid1 = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 250.0)
    assert oid1 is not None
    fake_broker.orders[oid1]['status'] = 'WORKING'

    # Covers TICKER's real $2,500 reservation + its own $200 buffer, but not
    # TICKER_TWO's $200 notional on top (required = $200 + $2,500 + $200 = $2,900).
    fake_broker.set_cash_balance('soxl_ira', 2_800.0)

    with pytest.raises(schwab_safety.SafetyViolation, match="cash buffer"):
        schwab_client.place_equity_buy('soxl_ira', 'TICKER_TWO', 10, 20.0)

    assert len([o for o in fake_broker.orders.values()
                if o['orderLegCollection'][0]['instrument']['symbol'] == 'TICKER_TWO']) == 0, \
        "TICKER_TWO order must not reach broker when combined cash is insufficient"

    events = signals_db.get_coverage_events(scenario_key='cash_check')
    assert any(e['ticker'] == 'TICKER_TWO' and e['result'] == 'blocked_insufficient'
               and 'reserved for' in e['detail'] and TICKER in e['detail'] for e in events), \
        "cash_check should log the reservation detail when blocking on combined insufficiency"


def _armed_addon_node(ticker, account, entry_price, shares, notional=5_000):
    """Helper shared by the 3 addon-buying-power tests below: creates a node
    with an open, fully-armed core position so every is_addon_leg
    precondition OTHER than the buying-power/reservation check under test
    already passes."""
    node = _add_node(ticker, account, notional=notional)
    signals_db.open_position(node, signal_price=entry_price, signal_time=_IN_WINDOW_TIME,
                              entry_price=entry_price, entry_time=_IN_WINDOW_TIME, shares=shares)
    pos = signals_db.get_open_position(ticker)
    signals_db.update_position_trail_state(pos['id'], {'trailing': True, 'peak': entry_price})
    return node


def test_addon_second_ticker_buy_allowed_when_buying_power_covers_both(env, fake_broker):
    """2026-08-10 fix: is_addon_leg is NO LONGER unconditionally blocked just
    because another ticker has a resting BUY in the same account (same
    "1-ticker-per-account artifact" shape as the RETL/LABD incident, fixed
    for the ordinary-BUY case in the sibling test above). When buying power
    covers the add-on's own 2x-headroom notional PLUS the other ticker's
    real reserved amount, the add-on BUY must proceed."""
    _add_node(TICKER, 'soxl_ira', notional=5_000)
    _armed_addon_node('TICKER_TWO', 'soxl_ira', entry_price=20.0, shares=10, notional=5_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_quote('TICKER_TWO', last=20.0, bid=20.0, ask=20.01)
    fake_broker.set_cash_balance('soxl_ira', 10_000.0)
    fake_broker.set_buying_power('soxl_ira', 10_000.0)

    r1, oid1 = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid1 is not None
    fake_broker.orders[oid1]['status'] = 'WORKING'  # TICKER stays resting, reserves $100

    # required = 200*2 (addon headroom) + 100 (TICKER reservation) = $500
    schwab_safety.check_order('soxl_ira', 'TICKER_TWO', 10, 20.0, 'BUY', is_addon_leg=True)

    events = signals_db.get_coverage_events(scenario_key='addon_buying_power_check')
    assert any(e['ticker'] == 'TICKER_TWO' and e['result'] == 'passed' for e in events), \
        "addon_buying_power_check should log 'passed' when buying power covers both"
    assert any(e['ticker'] == 'TICKER_TWO' and e['result'] == 'allowed_buying_power_sufficient'
               for e in signals_db.get_coverage_events(scenario_key='addon_second_ticker_buy_allowed')), \
        "addon_second_ticker_buy_allowed should log the relaxation's payoff"


def test_addon_second_ticker_buy_blocked_when_buying_power_cannot_cover_both(env, fake_broker):
    """The real remaining guard for add-on legs: TICKER's resting order
    reserves its actual quantity x current price against the account's
    buying power -- if that leaves too little for TICKER_TWO's add-on
    headroom requirement, it's still blocked (for a real, buying-power-
    grounded reason, not unconditionally)."""
    _add_node(TICKER, 'soxl_ira', notional=5_000)
    _armed_addon_node('TICKER_TWO', 'soxl_ira', entry_price=20.0, shares=10, notional=5_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_quote('TICKER_TWO', last=20.0, bid=20.0, ask=20.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    r1, oid1 = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid1 is not None
    fake_broker.orders[oid1]['status'] = 'WORKING'  # reserves $100

    # required = 200*2 (addon headroom) + 100 (TICKER reservation) = $500; give it $499.
    # Real order-time check is leverage-aware (2026-08-12): equity/margin_req,
    # not the raw buyingPower field -- set_equity(249.5) at the default 50%
    # margin req (2x) gives buying_power=499, same ceiling as before.
    fake_broker.set_equity('soxl_ira', 249.5)

    with pytest.raises(schwab_safety.SafetyViolation, match="buying power"):
        schwab_safety.check_order('soxl_ira', 'TICKER_TWO', 10, 20.0, 'BUY', is_addon_leg=True)

    events = signals_db.get_coverage_events(scenario_key='addon_buying_power_check')
    assert any(e['ticker'] == 'TICKER_TWO' and e['result'] == 'blocked_insufficient'
               and 'reserved for' in e['detail'] and TICKER in e['detail'] for e in events), \
        "addon_buying_power_check should log the reservation detail when blocking"


def test_addon_buy_blocked_unpriced_when_other_ticker_price_unavailable(env, fake_broker, monkeypatch):
    """If a live price can't be fetched for another ticker with a resting
    BUY, the add-on order must fail closed (blocked_unpriced) rather than
    silently reserving $0 for it."""
    _add_node(TICKER, 'soxl_ira', notional=5_000)
    _armed_addon_node('TICKER_TWO', 'soxl_ira', entry_price=20.0, shares=10, notional=5_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_quote('TICKER_TWO', last=20.0, bid=20.0, ask=20.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    fake_broker.set_buying_power('soxl_ira', 1_000_000.0)

    r1, oid1 = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid1 is not None
    fake_broker.orders[oid1]['status'] = 'WORKING'

    real_get_price = schwab_client.get_current_price
    monkeypatch.setattr(schwab_client, 'get_current_price',
                         lambda t: (_ for _ in ()).throw(RuntimeError("no quote")) if t == TICKER
                         else real_get_price(t))

    with pytest.raises(schwab_safety.SafetyViolation, match="live price"):
        schwab_safety.check_order('soxl_ira', 'TICKER_TWO', 10, 20.0, 'BUY', is_addon_leg=True)

    events = signals_db.get_coverage_events(scenario_key='addon_buying_power_check')
    assert any(e['ticker'] == 'TICKER_TWO' and e['result'] == 'blocked_unpriced' for e in events), \
        "addon_buying_power_check should log blocked_unpriced when another ticker's price is unavailable"


def test_third_ticker_buy_reserves_against_both_other_resting_orders(env, fake_broker):
    """The real gap a cold Opus review caught before this shipped: once the
    unconditional one-resting-BUY-at-a-time block was relaxed, 2+ other
    tickers can genuinely have resting BUYs at once (soxl_ira has 11 live
    tickers, both daily signal windows firing near-simultaneously) -- a
    naive single-order reservation would silently ignore all but the first
    it found. A 3rd ticker's BUY must reserve against BOTH other resting
    orders' real quantity x price, not just one."""
    schwab_safety.AUTOMATION_ENABLED_TICKERS.add('TICKER_THREE')
    _add_node(TICKER, 'soxl_ira', notional=5_000)
    _add_node('TICKER_TWO', 'soxl_ira', notional=5_000)
    _add_node('TICKER_THREE', 'soxl_ira', notional=5_000)
    fake_broker.set_quote(TICKER, last=150.0, bid=150.0, ask=150.01)
    fake_broker.set_quote('TICKER_TWO', last=150.0, bid=150.0, ask=150.01)
    fake_broker.set_quote('TICKER_THREE', last=20.0, bid=20.0, ask=20.01)
    fake_broker.set_cash_balance('soxl_ira', 100_000.0)

    r1, oid1 = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 150.0)
    assert oid1 is not None
    fake_broker.orders[oid1]['status'] = 'WORKING'  # reserves $1,500

    r2, oid2 = schwab_client.place_equity_buy('soxl_ira', 'TICKER_TWO', 10, 150.0)
    assert oid2 is not None
    fake_broker.orders[oid2]['status'] = 'WORKING'  # reserves another $1,500 ($3,000 total)

    # Covers both $1,500 reservations ($3,000) + buffer, but not TICKER_THREE's
    # $200 notional on top (required = $200 + $3,000 + $200 = $3,400).
    fake_broker.set_cash_balance('soxl_ira', 3_300.0)

    with pytest.raises(schwab_safety.SafetyViolation, match="cash buffer"):
        schwab_client.place_equity_buy('soxl_ira', 'TICKER_THREE', 10, 20.0)

    events = signals_db.get_coverage_events(scenario_key='cash_check')
    blocked = [e for e in events if e['ticker'] == 'TICKER_THREE' and e['result'] == 'blocked_insufficient']
    assert blocked, "TICKER_THREE's BUY should be blocked once both other reservations are summed"
    assert TICKER in blocked[-1]['detail'] and 'TICKER_TWO' in blocked[-1]['detail'], \
        "the blocking detail should reference BOTH other resting tickers, not just one"

    # Raise cash enough to cover all three -- now it must proceed.
    fake_broker.set_cash_balance('soxl_ira', 10_000.0)
    r3, oid3 = schwab_client.place_equity_buy('soxl_ira', 'TICKER_THREE', 10, 20.0)
    assert oid3 is not None, "TICKER_THREE's BUY must proceed once cash covers all three reservations"


# ===========================================================================
# TEST 4: same_day_block (cash vs margin)
# ===========================================================================

def test_same_day_block_cash_account_blocked(env, fake_broker):
    """same_day_block: a same-day re-buy in a cash_settlement_type='cash'
    account should be blocked after a same-day exit (result='blocked'). Uses
    'sep' -- 'roth' was cash-typed before 2026-08-11 (this test's original
    target) but became real limited-margin this session
    (cash_settlement_type='margin' now); 'sep' is the one account still
    cash-typed. See accounts table / schwab_safety.ACCOUNTS."""
    _add_node(TICKER, 'sep', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('sep', 1_000_000.0)

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
        schwab_client.place_equity_buy('sep', TICKER, 10, 10.0)

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


def test_same_day_block_margin_account_force_override_blocked(env, fake_broker):
    """same_day_block: a node's own force_same_day_block=True applies the
    block even though its account is 'margin' (2026-08-11, user's own
    per-TICKER opt-in choice, not a settlement finding for margin) -- same
    fixture shape as the margin-allowed test above, just with this specific
    node's flag set via the real setter, not an account-level override."""
    node = _add_node(TICKER, 'soxl_ira', notional=50_000)  # soxl_ira is 'margin'
    signals_db.set_force_same_day_block(node['id'], True)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

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

    with pytest.raises(schwab_safety.SafetyViolation, match="same-day"):
        schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)

    events = signals_db.get_coverage_events(scenario_key='same_day_block')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events), \
        "same_day_block should log 'blocked' for a margin account with force_same_day_block=True"


def test_same_day_block_margin_account_force_override_no_false_block_when_not_closed_today(env, fake_broker):
    """force_same_day_block=True must not block a BUY when the ticker was NOT
    actually sold today -- the flag only widens WHICH accounts the guard
    applies to, it doesn't change the underlying closed_today(ticker)
    trigger condition. No trade_log row seeded here at all."""
    node = _add_node(TICKER, 'soxl_ira', notional=50_000)
    signals_db.set_force_same_day_block(node['id'], True)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    r, oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)
    assert oid is not None, "no same-day close on record -- BUY should go through despite the override"

    events = signals_db.get_coverage_events(scenario_key='same_day_block')
    assert not any(e['ticker'] == TICKER for e in events), \
        "same_day_block shouldn't even fire when the ticker wasn't closed today"


def test_same_day_block_force_override_does_not_block_protective_topup(env, fake_broker):
    """CRITICAL fix from the 2026-08-11 session-wrap cold Opus review: the
    same_day_block section (including force_same_day_block) used to run
    UNCONDITIONALLY ahead of the is_protective/is_addon_leg exemptions
    (which only covered the separate duplicate-open-position guard further
    down) -- so a sanctioned post-fill top-up BUY (is_protective=True, e.g.
    the real, live-proven post_fill_topup mechanism) on a node with
    force_same_day_block=True and a same-day exit on record would have been
    wrongly rejected as an ordinary re-buy. Since both live-enabled accounts
    (ira, soxl_ira) are margin, this override is the only way same_day_block
    can ever fire on real capital -- this must not block a protective call."""
    node = _add_node(TICKER, 'soxl_ira', notional=50_000)
    signals_db.set_force_same_day_block(node['id'], True)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

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

    # Ordinary BUY still blocked (sanity check the flag is really on)...
    with pytest.raises(schwab_safety.SafetyViolation, match="same-day"):
        schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)

    # ...but a protective top-up call must go through despite the same-day close.
    r, oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 5, 10.0, is_protective=True)
    assert oid is not None, "is_protective BUY must be exempt from same_day_block/force_same_day_block"


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
