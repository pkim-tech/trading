"""fake_broker scenario for the exit_fresh_market_sell use case (found by
scripts/fake_broker_coverage_matrix.py, 2026-07-31): every existing
notify_sell_signal fake_broker scenario (test_fake_broker_sh_scenario.py,
test_fake_broker_trail_exit_scenario.py) always seeds a resting SL first, so
_attempt_automated_exit_sell's REPLACE branch is well covered but its fresh-
placement fallback (no resting_order_id at all -- e.g. the entry-time SL
placement itself previously failed) had never actually been driven through
fake_broker despite `notify_sell_signal` being called by name in those
files (a grep-only coverage check would have false-positived on this)."""
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

TICKER = 'TEST_EXIT_FRESH_SCENARIO'
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
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_node_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: IN_WINDOW_TIME)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))
    monkeypatch.setattr(signals_notify, 'time', type('T', (), {'sleep': staticmethod(lambda *a: None)}))

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=1, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira' WHERE ticker=?", (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def test_time_exit_with_no_resting_order_places_a_fresh_market_sell(env, fake_broker):
    """No sl_order_id, not armed -- _attempt_automated_exit_sell's
    resting_order_id resolves to None, forcing the fresh place_equity_sell
    branch (not replace_equity_order_with_market). Verified against the fake
    broker's own resulting order and the position actually closing on the
    immediate fill."""
    node = _node()
    entry_time = datetime(2026, 7, 29, 5, 0, 0)
    signals_db.open_position(node, signal_price=50.0, signal_time=entry_time,
                              entry_price=50.0, entry_time=entry_time, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='soxl_ira' WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    assert pos['sl_order_id'] is None
    assert not (pos.get('trail_state') or {}).get('trailing')

    fake_broker.set_quote(TICKER, last=48.0, bid=47.99, ask=48.01)

    signals_notify.notify_sell_signal(pos, 'TIME', current_price=48.0, target_price=48.0)

    market_sells = [o for o in fake_broker.orders.values()
                     if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                     and o['orderType'] == 'MARKET'
                     and o['orderLegCollection'][0]['instruction'] == 'SELL']
    assert len(market_sells) == 1
    assert market_sells[0]['status'] == 'FILLED'

    assert signals_db.get_open_position(TICKER) is None  # closed on the immediate fill

    # coverage_registry.py's automated_exit_execution row (added 2026-08-14, Opus audit --
    # 14 real live events with zero Grid row until that fix) had no dedicated fake_broker
    # assertion on this exact coverage_events wiring; this scenario is the natural home
    # for the 'placed' (success) case since it already drives the real placement call.
    events = signals_db.get_coverage_events(scenario_key='automated_exit_execution')
    matches = [e for e in events if e['ticker'] == TICKER and e['result'] == 'placed']
    assert len(matches) == 1


def test_exit_placement_blocked_by_kill_switch_logs_blocked_result(env, fake_broker):
    """SafetyViolation (kill switch engaged) -- automated_exit_execution's
    'blocked' result, deliberately excluded from bad_results in the registry
    (a real guard firing correctly is not a failure of this scenario)."""
    node = _node()
    entry_time = datetime(2026, 7, 29, 5, 0, 0)
    signals_db.open_position(node, signal_price=50.0, signal_time=entry_time,
                              entry_price=50.0, entry_time=entry_time, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='soxl_ira' WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    fake_broker.set_quote(TICKER, last=48.0, bid=47.99, ask=48.01)

    schwab_safety.engage_kill_switch("test")
    try:
        signals_notify.notify_sell_signal(pos, 'TIME', current_price=48.0, target_price=48.0)
    finally:
        schwab_safety.disengage_kill_switch()

    # Position stays open -- the guard correctly blocked the real order.
    assert signals_db.get_open_position(TICKER) is not None
    events = signals_db.get_coverage_events(scenario_key='automated_exit_execution')
    matches = [e for e in events if e['ticker'] == TICKER and e['result'] == 'blocked']
    assert len(matches) == 1


def test_time_exit_while_armed_logs_time_exit_trigger_armed(env, fake_broker):
    """coverage_registry.py's time_exit_trigger_armed row (the historically-buggy SH,
    2026-07-29 sub-case: hold-time expiry while a trailing-sell is still armed) had no
    fake_broker assertion -- notify_sell_signal logs this scenario_key right at the top,
    keyed on trail_state.exit_forced_by_hold_time, before the automated execution
    attempt even runs."""
    node = _node()
    entry_time = datetime(2026, 7, 29, 5, 0, 0)
    signals_db.open_position(node, signal_price=50.0, signal_time=entry_time,
                              entry_price=50.0, entry_time=entry_time, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='soxl_ira' WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    signals_db.update_position_trail_state(
        pos['id'], {'trailing': True, 'peak': 55.0, 'exit_forced_by_hold_time': True})
    pos = signals_db.get_open_position(TICKER)
    fake_broker.set_quote(TICKER, last=48.0, bid=47.99, ask=48.01)

    signals_notify.notify_sell_signal(pos, 'TIME', current_price=48.0, target_price=48.0)

    events = signals_db.get_coverage_events(scenario_key='time_exit_trigger_armed')
    matches = [e for e in events if e['ticker'] == TICKER and e['result'] == 'alert_fired']
    assert len(matches) == 1


def test_exit_placement_unexpected_exception_logs_failed_unexpectedly(env, fake_broker, monkeypatch):
    """A non-SafetyViolation exception from the real broker call -- automated_exit_execution's
    'failed_unexpectedly' result, which IS in bad_results (not a correctly-firing guard,
    a genuine placement failure)."""
    node = _node()
    entry_time = datetime(2026, 7, 29, 5, 0, 0)
    signals_db.open_position(node, signal_price=50.0, signal_time=entry_time,
                              entry_price=50.0, entry_time=entry_time, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='soxl_ira' WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    fake_broker.set_quote(TICKER, last=48.0, bid=47.99, ask=48.01)

    import schwab_client
    def _raise(*a, **kw):
        raise RuntimeError("simulated broker outage")
    monkeypatch.setattr(schwab_client, 'place_equity_sell', _raise)

    signals_notify.notify_sell_signal(pos, 'TIME', current_price=48.0, target_price=48.0)

    assert signals_db.get_open_position(TICKER) is not None
    events = signals_db.get_coverage_events(scenario_key='automated_exit_execution')
    matches = [e for e in events if e['ticker'] == TICKER and e['result'] == 'failed_unexpectedly']
    assert len(matches) == 1
