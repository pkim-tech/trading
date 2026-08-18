"""Tests for signals_notify.alert_stale_price_exit_suppressed -- the Slack
alert for the residual gap flagged in the 2026-07-22 HIBL stale-cache
incident: a real position's mid-bar exit check silently no-ops when
`_current_price` returns None (stale/missing same-day data), previously with
only a `log_poll` trace line and no Slack alert."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import signals_notify

TICKER = 'TEST_STALE_PRICE'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    posted = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: posted.append(a[0] if a else kw.get('text')))
    signals_notify._STALE_PRICE_ALERTED.clear()

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                         stop_loss=5, max_hold_hours=7, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account = 'ira' WHERE ticker = ?", (TICKER,))
        c.commit()

    node = [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira' WHERE ticker=?", (TICKER,))
        c.commit()

    yield posted

    tmp_db_path = Path(tmp_db.name)
    if tmp_db_path.exists():
        tmp_db_path.unlink()


def _pos():
    return signals_db.get_open_position(TICKER)


def test_alerts_when_price_suppressed(env):
    signals_notify.alert_stale_price_exit_suppressed(_pos())
    assert len(env) == 1
    assert TICKER in env[0]
    assert 'ira' in env[0]
    assert '_current_price' in env[0]
    # Asserts the real coverage_events row, not just the Slack text -- the Grid
    # row (scripts/coverage_registry.py, 'stale_price_exit_check_skipped')
    # cites this file as its offline proof, and offline_proof_for() only scores
    # that 'event-asserted' when the log call itself is actually asserted.
    events = signals_db.get_coverage_events(scenario_key='stale_price_exit_check_skipped')
    assert len(events) == 1
    assert events[0]['ticker'] == TICKER
    assert events[0]['result'] == 'skipped'


def test_alert_rate_limited(env):
    pos = _pos()
    signals_notify.alert_stale_price_exit_suppressed(pos)
    signals_notify.alert_stale_price_exit_suppressed(pos)
    assert len(env) == 1  # second call suppressed by the 15-min cooldown


def test_alert_keyed_per_position_not_shared(env, monkeypatch):
    pos = _pos()
    signals_notify.alert_stale_price_exit_suppressed(pos)
    monkeypatch.setitem(signals_notify._STALE_PRICE_ALERTED, str(pos['id']) + '_other', 0)
    # A different position id must not be suppressed by this position's cooldown.
    other_pos = dict(pos)
    other_pos['id'] = pos['id'] + 1
    signals_notify.alert_stale_price_exit_suppressed(other_pos)
    assert len(env) == 2
