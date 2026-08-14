"""fake_broker scenario for check_sl_order_fills (signals_notify.py, added
2026-08-07) -- built after a real live incident: LABD's resting protective
stop-loss filled for real at the broker minutes after entry, but nothing
polled that order directly (check_own_sell_fills/check_auto_fills only ever
recheck trail_state.exit_pending.order_id, which only exists once OUR
bar-close signal check has already computed an exit and called
_attempt_automated_exit_sell). The position sat stuck open locally for 8+
hours; the reconciliation-mismatch check flagged the symptom every poll
(detection-only, never closes the position) while every retry against the
now-terminal sl_order_id 400'd and posted a false "UNPROTECTED -- place a
stop-loss manually" alert on an already-safely-closed position.

fake_broker.advance_price's own resting-STOP-order auto-trigger (mirrors a
real order firing on its own between polls, independent of whether our code
is watching) is the exact mechanism to reproduce this without any of our
code ever being involved in the fill -- matching how the real incident
happened."""
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

TICKER = 'TEST_SL_ORDER_FILLS_SCENARIO'


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
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 8, 7, 9, 32, 53))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=50.0,
                         stop_loss=1, max_hold_hours=100, state='live',
                         trail_buy_pct=1.0, trail_pct=0.3, fixed_sl_override=0.3)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira' WHERE ticker=?", (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _open_position_with_resting_sl(fake_broker, stop_price=7.6619):
    entry_time = datetime(2026, 8, 7, 9, 32, 53)
    node = _node()
    signals_db.open_position(node, signal_price=7.64, signal_time=entry_time,
                              entry_price=7.685, entry_time=entry_time, shares=1)
    sl_order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'STOP', 'SELL', 1,
                                                  stop_price=stop_price)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='soxl_ira', sl_order_id=? WHERE ticker=?",
                   (sl_order_id, TICKER))
        c.commit()
    return signals_db.get_open_position(TICKER), sl_order_id


def test_sl_fill_with_no_exit_pending_closes_the_position(env, fake_broker):
    """The exact LABD shape: the real stop fires on its own (fake_broker's
    own trigger simulation, not any of our code) before our bar-close check
    ever computes an exit_pending. Without check_sl_order_fills, nothing
    would ever notice."""
    pos, sl_order_id = _open_position_with_resting_sl(fake_broker)
    assert not (pos.get('trail_state') or {}).get('exit_pending')

    fake_broker.advance_price(TICKER, last=7.66, bid=7.66, ask=7.66)
    assert fake_broker.orders[sl_order_id]['status'] == 'FILLED'
    assert signals_db.get_open_position(TICKER) is not None  # still open locally, undetected

    signals_notify.check_sl_order_fills([pos])

    assert signals_db.get_open_position(TICKER) is None
    with signals_db._conn() as c:
        rows = c.execute(
            "SELECT exit_reason, exit_price FROM trade_log WHERE ticker=?", (TICKER,)).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 'SL'
    assert rows[0][1] == pytest.approx(7.66)

    # registry id 'sl_order_fills_independent_detection' -- prior versions of
    # this file exercised the real behavior (position closes correctly) but
    # never asserted the log_coverage_event call itself, so the event line
    # could be silently deleted and nothing here would catch it (found
    # 2026-08-13, fake_venue_proof_for scan).
    # result is now 'closed_via_sl_order_poll' (was 'closed', identical to 2 sibling call
    # sites sharing this scenario_key -- coverage_registry.py's bad_results couldn't
    # actually distinguish them by detail text, so a sibling event was silently counting
    # as this path's proof; fixed 2026-08-14, Opus audit).
    events = signals_db.get_coverage_events(scenario_key='automated_exit_confirmed')
    matches = [e for e in events if e['ticker'] == TICKER and e['result'] == 'closed_via_sl_order_poll'
               and 'via_sl_order_poll=1' in (e['detail'] or '')]
    assert len(matches) == 1


def test_sl_fill_after_real_arm_is_labeled_trail(env, fake_broker):
    """The real post-arm shape: _attempt_automated_sell replaced the entry
    STOP with a genuine TRAILING_STOP order, repointing BOTH sl_order_id
    (open_positions column) and trail_state.exit_order_id to it (see
    signals_notify.py:168/2023) -- exit_reason must be derived from that
    order-identity match, not state['trailing'] alone (a paired Opus review,
    2026-08-07, found the trailing-flag-only version mislabels a genuine SL
    fill as TRAIL whenever arming was persisted but the real placement
    failed -- see the sibling test below). advance_price doesn't
    auto-trigger TRAILING_STOP (see fake_broker.py's own comment), so this
    uses force_fill directly -- still zero involvement from
    check_sl_order_fills itself in producing the fill."""
    pos, entry_sl_id = _open_position_with_resting_sl(fake_broker)
    trail_order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'TRAILING_STOP', 'SELL', 1,
                                                      trail_offset=0.3)
    state = dict(pos.get('trail_state') or {})
    state['trailing'] = True
    state['peak'] = 7.80
    state['exit_order_id'] = trail_order_id
    signals_db.update_position_trail_state(pos['id'], state)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET sl_order_id=? WHERE ticker=?", (trail_order_id, TICKER))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    assert pos['sl_order_id'] == trail_order_id
    assert pos['sl_order_id'] != entry_sl_id

    fake_broker.force_fill(trail_order_id, price=7.66)
    signals_notify.check_sl_order_fills([pos])

    with signals_db._conn() as c:
        rows = c.execute(
            "SELECT exit_reason FROM trade_log WHERE ticker=?", (TICKER,)).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 'TRAIL'


def test_sl_fill_with_trailing_flag_but_unrepointed_order_is_labeled_sl_not_trail(env, fake_broker):
    """The mislabel this fix specifically prevents: check_sell_condition
    persists trailing=True the moment a position arms, independent of
    whether _attempt_automated_sell's own placement actually succeeded (a
    SafetyViolation, broker exception, paused node, non-live node, or a
    ticker outside AUTOMATION_ENABLED_TICKERS all return early with
    sl_order_id left untouched -- see that function's docstring). If the
    ORIGINAL stop then fills, it's a real SL breach and must be labeled 'SL'
    even though trailing is True -- keying off state['trailing'] alone
    (an earlier draft of this fix) would wrongly call this 'TRAIL'."""
    pos, sl_order_id = _open_position_with_resting_sl(fake_broker)
    state = dict(pos.get('trail_state') or {})
    state['trailing'] = True
    state['peak'] = 7.80
    # exit_order_id deliberately NOT set -- mirrors _attempt_automated_sell
    # returning (False, None) without ever repointing sl_order_id.
    signals_db.update_position_trail_state(pos['id'], state)
    pos = signals_db.get_open_position(TICKER)
    assert pos['sl_order_id'] == sl_order_id  # still the original entry STOP

    fake_broker.advance_price(TICKER, last=7.66, bid=7.66, ask=7.66)
    signals_notify.check_sl_order_fills([pos])

    with signals_db._conn() as c:
        rows = c.execute(
            "SELECT exit_reason FROM trade_log WHERE ticker=?", (TICKER,)).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 'SL'


def test_sl_fill_already_covered_by_exit_pending_is_not_double_processed(env, fake_broker):
    """When exit_pending.order_id already equals sl_order_id (the normal case
    once our own bar-close check has fired), check_own_sell_fills/
    check_auto_fills already own that order -- check_sl_order_fills must skip
    it, not race to close the same position twice."""
    pos, sl_order_id = _open_position_with_resting_sl(fake_broker)
    state = dict(pos.get('trail_state') or {})
    state['exit_pending'] = {
        'reason': 'SL', 'current_price': 7.66, 'target_price': 7.6619,
        'order_id': sl_order_id, 'reminder_count': 0,
    }
    signals_db.update_position_trail_state(pos['id'], state)
    pos = signals_db.get_open_position(TICKER)

    fake_broker.advance_price(TICKER, last=7.66, bid=7.66, ask=7.66)
    signals_notify.check_sl_order_fills([pos])

    # Skipped -- still open, since this order_id belongs to
    # check_own_sell_fills'/check_auto_fills' poll, not this function's.
    assert signals_db.get_open_position(TICKER) is not None

    signals_notify.check_own_sell_fills([pos])
    assert signals_db.get_open_position(TICKER) is None
    with signals_db._conn() as c:
        rows = c.execute(
            "SELECT exit_reason FROM trade_log WHERE ticker=?", (TICKER,)).fetchall()
    assert len(rows) == 1


def test_sl_fill_for_fewer_shares_than_tracked_alerts_instead_of_closing(env, fake_broker):
    """A real, narrow residual case flagged in review: a resting stop sized
    for fewer shares than open_positions currently tracks (e.g. a
    top_up_position failure, or an addon leg sized independently of the
    core stop) shouldn't have its fill treated as proof the WHOLE local
    position is flat -- that would understate real open exposure at the
    broker. Alert and leave the position open for manual reconciliation,
    matching the existing top_up_position 'UNDERSTATED' alert pattern
    instead of inventing a new auto-correction."""
    entry_time = datetime(2026, 8, 7, 9, 32, 53)
    node = _node()
    signals_db.open_position(node, signal_price=7.64, signal_time=entry_time,
                              entry_price=7.685, entry_time=entry_time, shares=2)
    sl_order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'STOP', 'SELL', 1,
                                                  stop_price=7.6619)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='soxl_ira', sl_order_id=? WHERE ticker=?",
                   (sl_order_id, TICKER))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    assert pos['shares'] == 2

    fake_broker.advance_price(TICKER, last=7.66, bid=7.66, ask=7.66)
    assert fake_broker.orders[sl_order_id]['status'] == 'FILLED'

    signals_notify.check_sl_order_fills([pos])

    # Not auto-closed -- real exposure (the 2nd share) would be silently lost.
    assert signals_db.get_open_position(TICKER) is not None
    with signals_db._conn() as c:
        # trade_log's row is created at open_position time (exit_reason NULL
        # until closed) -- a mismatch must leave it unresolved, not filled in.
        rows = c.execute("SELECT exit_reason FROM trade_log WHERE ticker=?", (TICKER,)).fetchall()
    assert len(rows) == 1
    assert rows[0][0] is None
