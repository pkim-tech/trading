"""Third fake_broker scenario: genuine TRAIL-exit (armed, real trail-stop
breach, not hold-time-expiry -- the counterpart to test_fake_broker_sh_scenario.py's
armed-past-max-hold test). GDXU proved this path live for real (2026-07-28,
trade_log id 24, exit_reason=TRAIL, real fill $78.275) -- this pins it down as
a fast, repeatable regression test instead of leaving it as one-off live proof."""
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

TICKER = 'TEST_TRAIL_EXIT_SCENARIO'


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
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=5.0,
                         stop_loss=0, max_hold_hours=105, mode='live',
                         trail_buy_pct=1.0, trail_pct=0.3, fixed_sl_override=15.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira', arm_sell_pct=5.0, trail_sell_pct=0.3 "
                   "WHERE ticker=?", (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def test_genuine_trail_breach_auto_closes_via_resting_order_fill(env, fake_broker, monkeypatch):
    """Held time is nowhere near max_hold_hours (105) -- the only way this
    exit can fire is a genuine trail-stop breach, mirroring GDXU's real
    proven path: armed, real trailing-sell resting, broker fills it on its
    own, our poll confirms the fill and auto-closes without placing a
    second order."""
    node = _node()
    entry_time = datetime(2026, 7, 27, 9, 30, 3)
    signals_db.open_position(node, signal_price=85.0, signal_time=entry_time,
                              entry_price=83.76, entry_time=entry_time, shares=2)
    pos = signals_db.get_open_position(TICKER)

    fake_broker.set_quote(TICKER, last=78.28, bid=78.27, ask=78.29)
    exit_order_id = fake_broker.seed_resting_order(
        'soxl_ira', TICKER, 'TRAILING_STOP', 'SELL', 2, trail_offset=0.3)

    armed_state = {
        'trailing': True, 'peak': 86.9, 'order_placed': True,
        'exit_order_id': exit_order_id,
    }
    signals_db.update_position_trail_state(pos['id'], armed_state)
    pos = signals_db.get_open_position(TICKER)

    # The real trail-stop genuinely breached -- the broker's own mechanism
    # already filled the resting order (this is what "TRAIL" is supposed to
    # mean, unlike the SH hold-time-expiry case).
    fake_broker.force_fill(exit_order_id, price=78.275)

    signals_notify.notify_sell_signal(pos, 'TRAIL', current_price=78.28, target_price=78.28)

    # --- post-state: full check ---
    ticker_orders = [o for o in fake_broker.orders.values()
                      if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER]
    assert len(ticker_orders) == 1, (
        f"expected no second order placed (must reuse the already-filled resting "
        f"order), found: {[(o['orderId'], o['orderType'], o['status']) for o in ticker_orders]}"
    )
    assert ticker_orders[0]['orderId'] == exit_order_id
    assert ticker_orders[0]['status'] == 'FILLED'

    closed_pos = signals_db.get_open_position(TICKER)
    assert closed_pos is None, "position should auto-close on the confirmed real fill"

    with signals_db._conn() as c:
        row = c.execute(
            "SELECT exit_reason, exit_price, shares FROM trade_log WHERE ticker=? ORDER BY id DESC LIMIT 1",
            (TICKER,)).fetchone()
    assert row[0] == 'TRAIL'
    assert row[1] == pytest.approx(78.275, abs=0.001)
    assert row[2] == 2
