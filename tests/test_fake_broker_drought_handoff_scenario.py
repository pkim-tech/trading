"""fake_broker scenarios for real drought HANDOFF (signals_notify.
check_drought_handoff), Part 5 of docs/plans/
real_order_execution_drought_addon.md -- the genuinely new race paper never
has (paper's HANDOFF is a synchronous DB write).

Case A: drought entry order still resting, unfilled -- cancel it (or, on a
race to FILLED, fall through to Case B this same poll). Case B: drought
position filled and open -- real market SELL, DB row doesn't close until the
fill is confirmed. Also covers the alert-slot preservation fix
(active_signals._scan_buy_signals' already_held branch)."""
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

TICKER = 'TEST_DROUGHT_HANDOFF_SCENARIO'
IN_WINDOW_TIME = datetime(2026, 7, 29, 10, 30)


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
    monkeypatch.setattr(schwab_safety, '_now', lambda: IN_WINDOW_TIME)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account='soxl_ira', starting_notional=2000)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET drought_overlay_enabled=1, drought_confirm_days=3 WHERE ticker=?",
                   (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _buy_signal(current_price=51.0):
    return {'current_price': current_price, 'signal': 'BUY', 'z_score': -2.5,
            'last_bar': IN_WINDOW_TIME}


def _real_orders(fake_broker_, ticker, side=None):
    out = []
    for o in fake_broker_.orders.values():
        leg = o['orderLegCollection'][0]
        if leg['instrument']['symbol'] != ticker:
            continue
        if side is not None and leg['instruction'] != side:
            continue
        out.append(o)
    return out


# ---------------------------------------------------------------------------
# Case A: resting, unfilled drought entry order
# ---------------------------------------------------------------------------

def test_handoff_cancels_resting_unfilled_drought_entry_order(env, fake_broker, monkeypatch):
    node = _node()
    fake_broker.set_quote(TICKER, last=50.0, bid=49.99, ask=50.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'TRAILING_STOP', 'BUY', 40, trail_offset=1.0)
    signals_db.add_pending_buy(node, {'current_price': 50.0, 'last_bar': IN_WINDOW_TIME}, None, None,
                                order_id=order_id, position_source='drought_overlay', drought_confirm_days=3)
    with signals_db._conn() as c:
        c.execute("UPDATE pending_buys SET order_placed=1 WHERE ticker=?", (TICKER,))
        c.commit()
    monkeypatch.setattr('signals_compute.compute_buy_signal', lambda n: _buy_signal())

    signals_notify.check_drought_handoff(node)

    assert fake_broker.orders[order_id]['status'] == 'CANCELED'
    assert signals_db.get_drought_pending_buy(node['id']) is None
    events = signals_db.get_coverage_events(scenario_key='drought_handoff_cancel')
    assert any(e['ticker'] == TICKER and e['result'] == 'cancelled_resting_entry' for e in events)


def test_handoff_cancel_race_to_filled_falls_through_to_case_b(env, fake_broker, monkeypatch):
    """Cancel attempt finds the order already FILLED -- reconciles as a real
    drought fill, then falls through and (since there's no core signal for
    the resulting open drought position to be re-checked against on THIS
    call) simply leaves it open, correctly reconciled, for HANDOFF's own
    next poll to close."""
    node = _node()
    fake_broker.set_quote(TICKER, last=50.0, bid=49.99, ask=50.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'TRAILING_STOP', 'BUY', 40, trail_offset=1.0)
    signals_db.add_pending_buy(node, {'current_price': 50.0, 'last_bar': IN_WINDOW_TIME}, None, None,
                                order_id=order_id, position_source='drought_overlay', drought_confirm_days=3)
    with signals_db._conn() as c:
        c.execute("UPDATE pending_buys SET order_placed=1 WHERE ticker=?", (TICKER,))
        c.commit()
    monkeypatch.setattr('signals_compute.compute_buy_signal', lambda n: _buy_signal())
    # Race: the order fills the instant before our cancel_order call lands.
    fake_broker.force_fill(order_id, price=50.2)

    signals_notify.check_drought_handoff(node)

    assert signals_db.get_drought_pending_buy(node['id']) is None
    pos = signals_db.get_drought_overlay_position(node['id'])
    assert pos is not None, "the raced fill must be reconciled into a real drought position, not dropped"
    events = signals_db.get_coverage_events(scenario_key='drought_handoff_cancel')
    assert any(e['ticker'] == TICKER and e['result'] == 'raced_fill' for e in events)


def test_handoff_does_nothing_without_a_core_buy_signal(env, fake_broker, monkeypatch):
    """CRITICAL guard (verbatim from check_paper_drought_handoff's own
    2026-08-09 fix): compute_buy_signal returning a HOLD dict (the normal
    case on almost every poll) must not be treated as a fired core signal."""
    node = _node()
    fake_broker.set_quote(TICKER, last=50.0, bid=49.99, ask=50.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'TRAILING_STOP', 'BUY', 40, trail_offset=1.0)
    signals_db.add_pending_buy(node, {'current_price': 50.0, 'last_bar': IN_WINDOW_TIME}, None, None,
                                order_id=order_id, position_source='drought_overlay', drought_confirm_days=3)
    monkeypatch.setattr('signals_compute.compute_buy_signal',
                         lambda n: {'current_price': 50.0, 'signal': 'HOLD', 'last_bar': IN_WINDOW_TIME})

    signals_notify.check_drought_handoff(node)

    assert fake_broker.orders[order_id]['status'] == 'WORKING'
    assert signals_db.get_drought_pending_buy(node['id']) is not None


# ---------------------------------------------------------------------------
# Case B: filled, open drought position
# ---------------------------------------------------------------------------

def test_handoff_places_real_market_sell_and_waits_for_confirmed_fill(env, fake_broker, monkeypatch):
    node = _node()
    fake_broker.set_quote(TICKER, last=51.0, bid=50.99, ask=51.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    now = datetime.now()
    signals_db.open_drought_overlay_position(node, 50.0, now, 50.0, now, confirm_days=3, shares=40)
    monkeypatch.setattr('signals_compute.compute_buy_signal', lambda n: _buy_signal(51.0))

    signals_notify.check_drought_handoff(node)

    sells = _real_orders(fake_broker, TICKER, side='SELL')
    assert len(sells) == 1
    assert sells[0]['status'] == 'FILLED'  # fake_broker fills a MARKET order immediately
    pos = signals_db.get_drought_overlay_position(node['id'])
    assert pos is None, "a confirmed fill must close the drought position"
    events = signals_db.get_coverage_events(scenario_key='drought_handoff')
    assert any(e['ticker'] == TICKER and e['result'] == 'closed' for e in events)


def test_handoff_case_b_exit_failure_is_logged_and_alerted(env, fake_broker, monkeypatch):
    """Case B's own failure branch (signals_notify.py:1607-1613) -- when
    _attempt_automated_exit_sell fails closed (here: the global kill switch,
    a real fail-closed reason, engaged the instant before HANDOFF's exit
    attempt), the drought position must be left open (never silently
    dropped) and a human alerted to close it manually."""
    node = _node()
    fake_broker.set_quote(TICKER, last=51.0, bid=50.99, ask=51.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    now = datetime.now()
    signals_db.open_drought_overlay_position(node, 50.0, now, 50.0, now, confirm_days=3, shares=40)
    monkeypatch.setattr('signals_compute.compute_buy_signal', lambda n: _buy_signal(51.0))
    schwab_safety.engage_kill_switch("test halt")

    signals_notify.check_drought_handoff(node)

    assert len(_real_orders(fake_broker, TICKER, side='SELL')) == 0
    pos = signals_db.get_drought_overlay_position(node['id'])
    assert pos is not None, "a failed exit attempt must never silently close the local row"
    events = signals_db.get_coverage_events(scenario_key='drought_handoff_exit_placement')
    assert any(e['ticker'] == TICKER and e['result'] == 'failed_or_blocked' for e in events)


def test_handoff_alert_slot_preserved_while_still_pending(env, fake_broker, monkeypatch):
    """The 0.6/5.4 fix -- while a drought HANDOFF cancel/exit is still in
    flight (order status not yet confirmed), core's real BUY signal must not
    burn its once-per-day buy_alerted slot. Drives the real
    active_signals._scan_buy_signals code path (not a reimplementation) so
    the drought_handoff_alert_slot_preserved coverage event is genuinely
    exercised, not just asserted by hand."""
    import active_signals
    node = _node()
    fake_broker.set_quote(TICKER, last=51.0, bid=50.99, ask=51.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    now = datetime.now()
    signals_db.open_drought_overlay_position(node, 50.0, now, 50.0, now, confirm_days=3, shares=40)
    pos = signals_db.get_drought_overlay_position(node['id'])
    signals_db.update_position_trail_state(
        pos['id'], {'exit_pending': {'reason': 'HANDOFF', 'order_id': 999, 'placed_at': now.strftime('%Y-%m-%d %H:%M:%S')}})

    _blocking = signals_db.get_open_position_by_wl_id(node['id'])
    _handoff_in_flight = bool(_blocking) and _blocking.get('position_source') == 'drought_overlay' and (
        (_blocking.get('trail_state') or {}).get('exit_pending', {}).get('reason') == 'HANDOFF')
    assert _handoff_in_flight, "the exit_pending marker must be recognized as a HANDOFF in flight"

    buy_sig = {'ticker': TICKER, 'window': node['window'], 'signal': 'BUY', 'z_score': -2.5,
               'current_price': 51.0, 'lower_band': 50.0, 'sma': 52.0, 'std': 1.0,
               'last_bar': IN_WINDOW_TIME, 'hurst': None, 'adf_p': None}
    monkeypatch.setattr(active_signals, 'compute_buy_signal', lambda n, price_override=None: buy_sig)
    monkeypatch.setattr(active_signals, 'notify_buy_signal', lambda n, sig: None)
    monkeypatch.setattr(active_signals, 'get_pending_buys', lambda: [])

    buy_alerted = set()  # this poll's first BUY check for this node -- slot not yet burned
    # by-book shape (2026-08-15): a live node is deduped against the 'live'
    # book only, so a paper position can never suppress its real entry.
    open_position_keys = {'live': {node['id']}, 'paper': set()}  # the open drought position makes core "already_held"

    active_signals._scan_buy_signals([node], buy_alerted, open_position_keys)

    # Without the fix, already_held would add alert_key and never discard it,
    # burning the slot for the rest of the day even though HANDOFF is still
    # in flight and core's real entry hasn't happened yet.
    assert node['id'] not in buy_alerted, "the slot must not be burned while HANDOFF is in flight"
    events = signals_db.get_coverage_events(scenario_key='drought_handoff_alert_slot_preserved')
    assert any(e['ticker'] == TICKER and e['result'] == 'slot_released_handoff' for e in events)


def test_alert_slot_burned_when_already_held_with_no_handoff_or_pending(env, fake_broker, monkeypatch):
    """Negative control for the test above -- if already_held is True for a
    reason OTHER than a drought HANDOFF/pending-entry race (e.g. the
    already_held branch's own release condition regressed to always release),
    the once-per-day buy_alerted slot must stay burned. Without this, the
    positive test above could pass even if the release condition were
    accidentally made unconditional (found by paired review, 2026-08-10)."""
    import active_signals
    node = _node()
    fake_broker.set_quote(TICKER, last=51.0, bid=50.99, ask=51.01)

    buy_sig = {'ticker': TICKER, 'window': node['window'], 'signal': 'BUY', 'z_score': -2.5,
               'current_price': 51.0, 'lower_band': 50.0, 'sma': 52.0, 'std': 1.0,
               'last_bar': IN_WINDOW_TIME, 'hurst': None, 'adf_p': None}
    monkeypatch.setattr(active_signals, 'compute_buy_signal', lambda n, price_override=None: buy_sig)
    monkeypatch.setattr(active_signals, 'notify_buy_signal', lambda n, sig: None)
    monkeypatch.setattr(active_signals, 'get_pending_buys', lambda: [])

    # already_held=True (core position already open, no drought involvement at
    # all) but no drought overlay position and no drought pending buy exist --
    # neither _handoff_in_flight nor get_drought_pending_buy can be true.
    assert signals_db.get_open_position_by_wl_id(node['id']) is None
    assert signals_db.get_drought_pending_buy(node['id']) is None

    buy_alerted = set()
    # by-book shape (2026-08-15): a live node is deduped against the 'live'
    # book only, so a paper position can never suppress its real entry.
    open_position_keys = {'live': {node['id']}, 'paper': set()}

    active_signals._scan_buy_signals([node], buy_alerted, open_position_keys)

    assert node['id'] in buy_alerted, "the slot must stay burned when already_held has no handoff/pending cause"
    events = signals_db.get_coverage_events(scenario_key='drought_handoff_alert_slot_preserved')
    assert not any(e['ticker'] == TICKER for e in events)


def test_handoff_alert_slot_preserved_pending_entry_arm(env, fake_broker, monkeypatch):
    """The sibling sub-case: a stale drought pending_buys row (Case A's
    cancel-races-to-FILLED window, docs/plans/real_order_execution_drought_
    addon.md 5.1) can coexist with a newly-open drought position whose
    trail_state has no HANDOFF exit_pending marker at all -- get_drought_
    pending_buy alone must still release the slot, logged with a DISTINCT
    result ('slot_released_pending_entry') from the HANDOFF case above, so
    a live firing of this arm alone can't misrepresent the HANDOFF race as
    proven (the exact false-positive found by paired review, 2026-08-10)."""
    import active_signals
    node = _node()
    fake_broker.set_quote(TICKER, last=51.0, bid=50.99, ask=51.01)
    now = datetime.now()
    signals_db.open_drought_overlay_position(node, 50.0, now, 50.0, now, confirm_days=3, shares=40)
    # No exit_pending/HANDOFF marker on trail_state -- _handoff_in_flight is
    # False. A stale pending_buys row is the only reason this branch fires.
    signals_db.add_pending_buy(node, {'current_price': 50.0, 'last_bar': IN_WINDOW_TIME}, None, None,
                                order_id=999, position_source='drought_overlay', drought_confirm_days=3)

    _blocking = signals_db.get_open_position_by_wl_id(node['id'])
    _handoff_in_flight = bool(_blocking) and _blocking.get('position_source') == 'drought_overlay' and (
        (_blocking.get('trail_state') or {}).get('exit_pending', {}).get('reason') == 'HANDOFF')
    assert not _handoff_in_flight, "this arm must be reached via get_drought_pending_buy alone"
    assert signals_db.get_drought_pending_buy(node['id']) is not None

    buy_sig = {'ticker': TICKER, 'window': node['window'], 'signal': 'BUY', 'z_score': -2.5,
               'current_price': 51.0, 'lower_band': 50.0, 'sma': 52.0, 'std': 1.0,
               'last_bar': IN_WINDOW_TIME, 'hurst': None, 'adf_p': None}
    monkeypatch.setattr(active_signals, 'compute_buy_signal', lambda n, price_override=None: buy_sig)
    monkeypatch.setattr(active_signals, 'notify_buy_signal', lambda n, sig: None)
    monkeypatch.setattr(active_signals, 'get_pending_buys', lambda: [])

    buy_alerted = set()
    # by-book shape (2026-08-15): a live node is deduped against the 'live'
    # book only, so a paper position can never suppress its real entry.
    open_position_keys = {'live': {node['id']}, 'paper': set()}

    active_signals._scan_buy_signals([node], buy_alerted, open_position_keys)

    assert node['id'] not in buy_alerted, "the slot must not be burned while a drought entry order is still resting"
    events = signals_db.get_coverage_events(scenario_key='drought_handoff_alert_slot_preserved')
    assert any(e['ticker'] == TICKER and e['result'] == 'slot_released_pending_entry' for e in events)
    assert not any(e['ticker'] == TICKER and e['result'] == 'slot_released_handoff' for e in events)


def test_handoff_cancel_unconfirmed_leaves_row_in_place_and_core_still_blocked(env, fake_broker, monkeypatch):
    """The 3rd Case A branch (signals_notify.py:1575-1581) -- a cancel whose
    confirmed status comes back neither FILLED nor CANCELED (broker-side
    REJECTED/EXPIRED/a failed confirm poll are all real possibilities) must
    fail closed: the local pending_buys row stays in place for a retry next
    poll, never discarded as if the real order were confirmed gone. Simulated
    by seeding the resting order already in a terminal-but-not-CANCELED state
    (REJECTED) -- fake_broker's own cancel_order only sets CANCELED on a
    still-non-terminal order, so this reaches the exact branch under test."""
    node = _node()
    fake_broker.set_quote(TICKER, last=50.0, bid=49.99, ask=50.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'TRAILING_STOP', 'BUY', 40, trail_offset=1.0)
    fake_broker.orders[order_id]['status'] = 'REJECTED'
    signals_db.add_pending_buy(node, {'current_price': 50.0, 'last_bar': IN_WINDOW_TIME}, None, None,
                                order_id=order_id, position_source='drought_overlay', drought_confirm_days=3)
    with signals_db._conn() as c:
        c.execute("UPDATE pending_buys SET order_placed=1 WHERE ticker=?", (TICKER,))
        c.commit()
    monkeypatch.setattr('signals_compute.compute_buy_signal', lambda n: _buy_signal())

    signals_notify.check_drought_handoff(node)

    # Row must still be there -- NOT cleared, unlike the confirmed-cancel case.
    pending = signals_db.get_drought_pending_buy(node['id'])
    assert pending is not None, "cancel_unconfirmed must never discard tracking of a possibly-still-real order"
    events = signals_db.get_coverage_events(scenario_key='drought_handoff_cancel')
    assert any(e['ticker'] == TICKER and e['result'] == 'cancel_unconfirmed' for e in events)

    # And core's own entry must still see itself as blocked -- the pending
    # row being present is exactly what active_signals._scan_buy_signals'
    # already_pending check keys off.
    assert signals_db.get_drought_pending_buy(node['id']) is not None
