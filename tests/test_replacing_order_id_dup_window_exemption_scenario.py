"""Fake-venue regression for the backlog item found 2026-08-16 (fake_venue
harness scenario build): schwab_safety.check_order's replacing_order_id
param exempted an atomic-replace target from the resting-order duplicate
check (_has_open_sell_order, exclude_order_id=replacing_order_id) but NOT
from the separate recent_orders 60s timestamp-fingerprint dedup loop
(DUPLICATE_ORDER_WINDOW_SECS) -- that check has no order_id to exclude by
at all (recent_orders entries are local (account, ticker, side, quantity,
ts) records, written BEFORE the real order exists at the broker), so a
replace call's own about-to-be-swapped-out order could be seen as a
duplicate of itself.

Confirmed reachable in production, not just theoretical: an entry-time SL
placement via the manual "Filled" Slack confirmation
(signals_handlers.handle_trail_buy_fill_price -> _place_stop_loss_for_
position -> schwab_client.place_stop_loss) runs on the Slack Bolt event
handler thread, entirely independent of the poll loop's POLL_SECS cadence.
If an arm/exit-side atomic replace of that SAME order (e.g.
replace_order_with_stop_loss, called from the next poll cycle's arm/exit
scan) lands within DUPLICATE_ORDER_WINDOW_SECS, the recent_orders
fingerprint from the initial placement matches (same account/ticker/
side=SELL/quantity). Worse: for a real trading_enabled account,
_broker_confirms_order then finds the order genuinely resting at the
broker (it IS the replace's own target) -- confirming it as a real
duplicate rather than excusing it as a failed retry -- so the replace was
wrongly blocked as a duplicate of the very order it was about to replace.

Fix (schwab_safety.check_order/_broker_confirms_order, 2026-08-17,
corrected same day after paired review found the first version too broad):
NOT a blanket skip of the recent_orders fingerprint loop. For a
trading_enabled account, the loop stays live (a genuinely separate
confirming order still blocks) and replacing_order_id is threaded into
_broker_confirms_order as exclude_order_id instead -- only the replace's
own specific target order is excused from counting as broker confirmation,
mirroring _has_open_sell_order's exclude_order_id exactly. Only for a
non-trading_enabled (dry_run) account, with no real broker book to check
against, does this fall back to a blanket skip. The first version's
blanket-skip-on-presence design was itself a real gap: replacing_order_id
is sourced from local DB fields this repo has already shown can be stale
(see _verify_resting_before_replace's docstring, a real 2026-08-14
incident) -- treating it as an unconditionally-trustworthy fact would have
let a caller-side bug with a wrong/stale id disable duplicate protection
entirely, not just for its own self-collision."""
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

TICKER = 'TEST_REPLACE_DUP_WINDOW_SCENARIO'
_IN_WINDOW_TIME = datetime(2026, 8, 17, 10, 30)


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


def _real_orders(fake_broker_, ticker):
    return [o for o in fake_broker_.orders.values()
            if o['orderLegCollection'][0]['instrument']['symbol'] == ticker]


def _open_local_position(node, shares=40, price=50.0):
    # place_stop_loss's oversell guard (schwab_safety.check_order SELL branch)
    # requires a real local position on file to bound quantity against --
    # mirrors every real SL-placement call site, which always resolves a
    # position before ever reaching schwab_client.
    now = _IN_WINDOW_TIME
    signals_db.open_position(node, price, now, price, now, shares=shares)


def test_replace_of_own_recent_sl_is_not_false_blocked_as_duplicate(env, fake_broker):
    """The exact production collision: an entry-time SL placement (mirrors
    handle_trail_buy_fill_price -> _place_stop_loss_for_position) followed,
    within the 60s dup window, by an atomic replace of that SAME order
    (mirrors an arm/exit-side re-price on the very next poll) -- must
    succeed, not be blocked as a duplicate of its own target."""
    node = _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=50.0, bid=49.99, ask=50.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    _open_local_position(node)

    _resp, sl_order_id = schwab_client.place_stop_loss('soxl_ira', TICKER, 40, 49.50)
    assert sl_order_id is not None

    # Same account/ticker/side/quantity, well within DUPLICATE_ORDER_WINDOW_SECS
    # (mocked clock doesn't advance) -- exactly the shape that used to
    # false-block before the fix.
    _resp2, new_order_id = schwab_client.replace_order_with_stop_loss(
        'soxl_ira', TICKER, sl_order_id, 40, 48.75)

    assert new_order_id is not None
    orders = _real_orders(fake_broker, TICKER)
    # The original SL was replaced (CANCELED/REPLACED at the broker) and a
    # new resting STOP order exists.
    assert any(o['orderId'] == new_order_id and o['status'] == 'WORKING' for o in orders)
    events = signals_db.get_coverage_events(scenario_key='dup_order_window_blocked')
    assert not any(e['ticker'] == TICKER for e in events), \
        "the replace must never hit the fingerprint dup-window guard at all"


def test_genuine_duplicate_with_no_replacing_order_id_is_still_blocked(env, fake_broker):
    """Negative control -- the fix must not weaken the real duplicate-
    prevention guarantee. A second, brand-new SL placement (NOT a replace,
    no replacing_order_id) for the same account/ticker/side/quantity within
    the dup window must still be blocked, exactly like before.

    The first order is force-filled so the SEPARATE resting-order guard
    (_has_open_sell_order) is out of the picture and this genuinely isolates
    the recent_orders fingerprint window under test -- otherwise the
    resting-order guard would fire first (a real STOP order stays WORKING,
    unlike a MARKET order) and this wouldn't prove anything about the
    fingerprint loop specifically."""
    node = _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=50.0, bid=49.99, ask=50.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    _open_local_position(node)

    _resp, sl_order_id = schwab_client.place_stop_loss('soxl_ira', TICKER, 40, 49.50)
    assert sl_order_id is not None
    fake_broker.force_fill(sl_order_id, price=49.50)

    with pytest.raises(schwab_safety.SafetyViolation, match="duplicate"):
        schwab_client.place_stop_loss('soxl_ira', TICKER, 40, 49.50)

    orders = _real_orders(fake_broker, TICKER)
    assert len(orders) == 1, "the true duplicate (no replacing_order_id) must never reach the broker"
    events = signals_db.get_coverage_events(scenario_key='dup_order_window_blocked')
    assert any(e['ticker'] == TICKER and e['result'] == 'blocked' for e in events)


def test_replace_with_wrong_order_id_does_not_bypass_real_duplicate_protection(env, fake_broker):
    """Fixed 2026-08-17 (paired review, HIGH finding): the first version of this
    fix treated replacing_order_id's mere presence as sufficient to skip the
    ENTIRE recent_orders fingerprint loop -- meaning a caller-side bug passing
    a stale/wrong/unrelated order id (this repo has a real on-file 2026-08-14
    incident of exactly this shape, see _verify_resting_before_replace's
    docstring) could silently disable real duplicate protection for a
    completely different, genuinely-duplicate order. The corrected design
    (schwab_safety._broker_confirms_order's exclude_order_id param) only
    excuses the SPECIFIC order being replaced from counting as broker
    confirmation -- a separate, real resting order with the same fingerprint
    must still be caught. This is that test: two independent SL placements
    for the SAME (account, ticker, side, quantity) fingerprint within the
    dup-window, then a replace naming a bogus id that matches neither real
    order -- must still be BLOCKED, since a genuinely separate confirming
    order (the second real SL) is still on the real broker order book."""
    node = _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=50.0, bid=49.99, ask=50.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    _open_local_position(node)

    _resp1, sl_order_id_1 = schwab_client.place_stop_loss('soxl_ira', TICKER, 40, 49.50)
    assert sl_order_id_1 is not None
    # Force-filled (not left resting) so the SEPARATE, earlier resting-order
    # guard (_has_open_sell_order) is out of the picture and this genuinely
    # isolates the recent_orders fingerprint loop under test -- same reasoning
    # as test_genuine_duplicate_with_no_replacing_order_id_is_still_blocked
    # above. _broker_confirms_order still counts a FILLED order as real
    # confirmation (only CANCELED/EXPIRED/REJECTED/REPLACED don't), so this
    # doesn't weaken what's being proven.
    fake_broker.force_fill(sl_order_id_1, price=49.50)

    with pytest.raises(schwab_safety.SafetyViolation):
        schwab_client.replace_order_with_stop_loss(
            'soxl_ira', TICKER, sl_order_id_1 + 999_999, 40, 48.75)

    events = signals_db.get_coverage_events(scenario_key='dup_order_window_blocked')
    assert any(e['ticker'] == TICKER for e in events)
