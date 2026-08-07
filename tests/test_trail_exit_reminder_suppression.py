"""Tests for the 2026-08-02 routine-wait suppression in notify_sell_signal /
check_exit_reminders: a TRAIL exit with a confirmed-still-resting automated
order is expected behavior (the broker fills it on its own, nothing to do),
so the routine "still resting, no action needed" alert is suppressed both on
the initial SELL SIGNAL post and the first 2 reminder cycles -- only
escalating (posting) once _exit_pending_blocks' own reminder_num>=3
threshold says this has rested unusually long. TP/SL/TIME reminders, which
may genuinely need a manual tap, are unaffected.

Uses direct monkeypatching of _exit_order_resting/_post_message rather than
fake_broker -- this is Slack-message-suppression logic, not a real
order-placement guard, so a stateful order book isn't the relevant fixture
here (see docs/design.md's Test Fixtures & Coverage-Proof Techniques)."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import signals_notify

TICKER = 'TEST_TRAIL_SUPPRESS_SCENARIO'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=5.0,
                         stop_loss=0, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=0.3, fixed_sl_override=15.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira', arm_sell_pct=5.0, trail_sell_pct=0.3 "
                   "WHERE ticker=?", (TICKER,))
        c.commit()
    yield
    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _open_pos():
    entry_time = datetime(2026, 7, 27, 9, 30, 3)
    signals_db.open_position(_node(), signal_price=85.0, signal_time=entry_time,
                              entry_price=83.76, entry_time=entry_time, shares=2)
    return signals_db.get_open_position(TICKER)


def _posted(monkeypatch):
    posted = []
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda *a, **kw: (posted.append(a[0] if a else kw.get('text')), (None, None))[1])
    return posted


def test_initial_trail_alert_suppressed_when_confirmed_resting(env, monkeypatch):
    pos = _open_pos()
    posted = _posted(monkeypatch)
    monkeypatch.setattr(signals_notify, '_attempt_automated_exit_sell', lambda *a, **kw: 'order123')
    monkeypatch.setattr(signals_notify.schwab_client, 'get_filled_order', lambda *a, **kw: None)
    monkeypatch.setattr(signals_notify, '_exit_order_resting', lambda *a, **kw: True)
    monkeypatch.setattr(signals_notify, '_trail_alert_should_post_now', lambda *a, **kw: False)

    signals_notify.notify_sell_signal(pos, 'TRAIL', current_price=78.28, target_price=78.28)

    assert posted == [], "a confirmed-resting TRAIL exit must not post the routine SELL SIGNAL alert"
    fresh = signals_db.get_position_by_id(pos['id'])
    exit_pending = fresh['trail_state']['exit_pending']
    assert exit_pending['reason'] == 'TRAIL'
    assert exit_pending['reminder_channel'] is None
    assert exit_pending['last_reminder_at'] is not None, \
        "last_reminder_at must still be set so check_exit_reminders' elapsed gate works"


def test_initial_time_alert_not_suppressed(env, monkeypatch):
    """Sanity check: suppression is TRAIL-only -- a TIME exit (which has no
    equivalent 'already announced at arm time' event) must still post."""
    pos = _open_pos()
    posted = _posted(monkeypatch)
    monkeypatch.setattr(signals_notify, '_attempt_automated_exit_sell', lambda *a, **kw: None)

    signals_notify.notify_sell_signal(pos, 'TIME', current_price=78.28, target_price=78.28)

    assert len(posted) == 1


def test_reminder_1_and_2_suppressed_then_escalates_at_3(env, monkeypatch):
    pos = _open_pos()
    state = {
        'exit_pending': {
            'reason': 'TRAIL', 'current_price': 78.28, 'target_price': 78.28,
            'reminder_channel': None, 'reminder_ts': None, 'reminder_count': 0,
            'last_reminder_at': '2020-01-01 00:00:00',  # far enough in the past to always be due
            'order_id': 'order123',
        }
    }
    signals_db.update_position_trail_state(pos['id'], state)
    posted = _posted(monkeypatch)
    monkeypatch.setattr(signals_notify, '_exit_order_resting', lambda *a, **kw: True)
    monkeypatch.setattr(signals_notify, '_trail_alert_should_post_now', lambda *a, **kw: False)

    # Reminder #1 -- suppressed
    signals_notify.check_exit_reminders([pos])
    assert posted == []
    fresh = signals_db.get_position_by_id(pos['id'])
    ep = fresh['trail_state']['exit_pending']
    assert ep['reminder_count'] == 1
    ep['last_reminder_at'] = '2020-01-01 00:00:00'
    signals_db.update_position_trail_state(pos['id'], fresh['trail_state'])

    # Reminder #2 -- suppressed
    signals_notify.check_exit_reminders([signals_db.get_position_by_id(pos['id'])])
    assert posted == []
    fresh = signals_db.get_position_by_id(pos['id'])
    ep = fresh['trail_state']['exit_pending']
    assert ep['reminder_count'] == 2
    ep['last_reminder_at'] = '2020-01-01 00:00:00'
    signals_db.update_position_trail_state(pos['id'], fresh['trail_state'])

    # Reminder #3 -- escalates, must post
    signals_notify.check_exit_reminders([signals_db.get_position_by_id(pos['id'])])
    assert len(posted) == 1, "reminder #3 must escalate to a real post once routine waiting has run long"
    fresh = signals_db.get_position_by_id(pos['id'])
    assert fresh['trail_state']['exit_pending']['reminder_count'] == 3


def test_reminder_not_suppressed_when_no_longer_confirmed_resting(env, monkeypatch):
    """If the order stops being confirmed-resting (filled/canceled/unknown)
    before reminder #3, suppression must not mask that -- post immediately."""
    pos = _open_pos()
    state = {
        'exit_pending': {
            'reason': 'TRAIL', 'current_price': 78.28, 'target_price': 78.28,
            'reminder_channel': None, 'reminder_ts': None, 'reminder_count': 0,
            'last_reminder_at': '2020-01-01 00:00:00',
            'order_id': 'order123',
        }
    }
    signals_db.update_position_trail_state(pos['id'], state)
    posted = _posted(monkeypatch)
    monkeypatch.setattr(signals_notify, '_exit_order_resting', lambda *a, **kw: None)

    signals_notify.check_exit_reminders([pos])
    assert len(posted) == 1, "an unconfirmed/no-longer-resting order must not be silently suppressed"


def test_initial_alert_not_suppressed_when_too_close_to_reminder_window_close(env, monkeypatch):
    """Found by review, 2026-08-02: check_exit_reminders only runs 9:00-16:00,
    so suppressing near that cutoff (or outside the window entirely) could
    silence a real position until 9:00 the next trading day. Must fail
    toward posting instead."""
    pos = _open_pos()
    posted = _posted(monkeypatch)
    monkeypatch.setattr(signals_notify, '_attempt_automated_exit_sell', lambda *a, **kw: 'order123')
    monkeypatch.setattr(signals_notify.schwab_client, 'get_filled_order', lambda *a, **kw: None)
    monkeypatch.setattr(signals_notify, '_exit_order_resting', lambda *a, **kw: True)
    # Real (unmocked) _trail_alert_should_post_now, but time-of-day forced
    # to 15:50 -- only 10 minutes before the 16:00 reminder-loop cutoff,
    # nowhere near enough for a 3x15min escalation to complete.
    monkeypatch.setattr(signals_notify, 'datetime',
                         type('_dt', (), {'now': staticmethod(lambda: datetime(2026, 7, 27, 15, 50))}))

    signals_notify.notify_sell_signal(pos, 'TRAIL', current_price=78.28, target_price=78.28)

    assert len(posted) == 1, "too close to the reminder-window cutoff must not suppress -- fail toward posting"


def test_reminder_count_preserved_across_bar_re_fires(env, monkeypatch):
    """Found by review, 2026-08-02: sell_alerted dedups per-bar not
    across-bars, so notify_sell_signal re-enters on every new bar close
    while a TRAIL exit stays unresolved. Before this fix, each re-fire
    unconditionally overwrote exit_pending with a fresh reminder_count=0,
    discarding whatever escalation progress check_exit_reminders had
    already made toward the reminder_num>=3 threshold."""
    pos = _open_pos()
    posted = _posted(monkeypatch)
    monkeypatch.setattr(signals_notify, '_attempt_automated_exit_sell', lambda *a, **kw: 'order123')
    monkeypatch.setattr(signals_notify.schwab_client, 'get_filled_order', lambda *a, **kw: None)
    monkeypatch.setattr(signals_notify, '_exit_order_resting', lambda *a, **kw: True)
    monkeypatch.setattr(signals_notify, '_trail_alert_should_post_now', lambda *a, **kw: False)

    # First bar close -- fresh exit_pending created.
    signals_notify.notify_sell_signal(pos, 'TRAIL', current_price=78.28, target_price=78.28)
    fresh = signals_db.get_position_by_id(pos['id'])
    fresh['trail_state']['exit_pending']['reminder_count'] = 2  # simulate check_exit_reminders progress
    fresh['trail_state']['exit_pending']['last_reminder_at'] = '2020-01-01 00:00:00'
    signals_db.update_position_trail_state(pos['id'], fresh['trail_state'])

    # Next bar close, same still-resting order -- must NOT reset progress.
    signals_notify.notify_sell_signal(pos, 'TRAIL', current_price=78.28, target_price=78.28)

    assert posted == []
    fresh = signals_db.get_position_by_id(pos['id'])
    assert fresh['trail_state']['exit_pending']['reminder_count'] == 2, \
        "a re-fire on a new bar must preserve check_exit_reminders' escalation progress, not reset it"
