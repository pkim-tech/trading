"""broker_stop_price must be cleared when the real stop it describes is
replaced -- found by an Opus review of the full session diff, 2026-08-01:
piece 1 (stop_status()/set_broker_stop_price_by_position) made this
previously-dead field live for the first time, but nothing cleared it when
_attempt_automated_sell (arm-time SL->trailing-sell replace) or
_attempt_automated_exit_sell (TP/SL/TIME market-sell replace) replaced the
real stop the price described. Left uncleared, stop_status() would report
'known' off a dead price -- a real SL alert firing after a replace would
falsely say "broker stop on file, no action needed" for a position actually
protected only by an unconfirmed resting order, a behavior regression from
the pre-diff cautious "check account" text."""
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

TICKER = 'TEST_BSP_CLEAR_SCENARIO'


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
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 7, 29, 10, 30))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=10,
                         stop_loss=1, max_hold_hours=100, mode='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira' WHERE ticker=?", (TICKER,))
        c.commit()

    yield
    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def test_tp_exit_replace_clears_stale_broker_stop_price(env, fake_broker):
    node = _node()
    entry_time = datetime(2026, 7, 24, 7, 38, 38)
    signals_db.open_position(node, signal_price=100.0, signal_time=entry_time,
                              entry_price=100.0, entry_time=entry_time, shares=50)
    pos = signals_db.get_open_position(TICKER)

    fake_broker.set_quote(TICKER, last=109.0, bid=108.9, ask=109.1)
    sl_order_id = fake_broker.seed_resting_order(
        'soxl_ira', TICKER, 'STOP', 'SELL', 50, stop_price=99.0)
    signals_db.set_sl_order_id_by_position(pos['id'], sl_order_id)
    signals_db.set_broker_stop_price_by_position(pos['id'], 99.0)

    pos = signals_db.get_open_position(TICKER)
    assert pos['broker_stop_price'] == 99.0  # sanity: real stop on file before the exit

    order_id = signals_notify._attempt_automated_exit_sell(pos, 'TP', 109.0)
    assert order_id is not None, "TP exit should have placed a real replacement market sell"

    pos_after = signals_db.get_open_position(TICKER)
    assert pos_after['broker_stop_price'] is None, (
        "broker_stop_price must be cleared once the real stop it described was replaced -- "
        "otherwise stop_status() reports a dead price as 'known'"
    )


def test_arm_replace_clears_stale_broker_stop_price(env, fake_broker):
    node = _node()
    entry_time = datetime(2026, 7, 24, 7, 38, 38)
    signals_db.open_position(node, signal_price=100.0, signal_time=entry_time,
                              entry_price=100.0, entry_time=entry_time, shares=50)
    pos = signals_db.get_open_position(TICKER)

    fake_broker.set_quote(TICKER, last=105.0, bid=104.9, ask=105.1)
    sl_order_id = fake_broker.seed_resting_order(
        'soxl_ira', TICKER, 'STOP', 'SELL', 50, stop_price=99.0)
    signals_db.set_sl_order_id_by_position(pos['id'], sl_order_id)
    signals_db.set_broker_stop_price_by_position(pos['id'], 99.0)
    signals_db.update_position_trail_state(pos['id'], {'trailing': True, 'peak': 105.0})

    pos = signals_db.get_open_position(TICKER)
    ok, exit_order_id = signals_notify._attempt_automated_sell(pos, 105.0)
    assert ok, "arm-time trailing-sell placement should have succeeded"

    pos_after = signals_db.get_open_position(TICKER)
    assert pos_after['broker_stop_price'] is None, (
        "arming replaces the real stop-loss with a trailing-sell -- broker_stop_price "
        "must be cleared, not left describing the now-dead stop-loss order"
    )
