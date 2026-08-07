"""Fourth fake_broker scenario: post_fill_topup (real scenario_key 'top_up',
_reconcile_fill). Both real historical attempts (RETL 2026-07-29, LABU
2026-07-24) were legitimately blocked by real guards (signal-window gate,
daily-order-cap) -- not malfunctions, just never yet observed succeeding.
This pins down whether the GOOD path (real fill under target notional, top-up
buy placed and recorded) actually works when nothing blocks it -- something
no real event has ever proven."""
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

TICKER = 'TEST_TOPUP_SCENARIO'


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
    # 10:30 ET -- inside the real (10,25,10,40) signal window, so the top-up
    # buy's own signal-window guard passes on its own merits, not via
    # is_gap_correction bypassing it -- this is the ordinary in-window case,
    # matching most real fills.
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 7, 29, 10, 30))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0)
    with signals_db._conn() as c:
        # $800 matches soxl_ira's real notional_cap (schwab_safety.ACCOUNTS) --
        # sizing the scenario to actually fit within the real guard, not an
        # arbitrary bigger number that would itself get correctly blocked.
        c.execute("UPDATE watch_list SET account='soxl_ira', starting_notional=800 WHERE ticker=?",
                   (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def test_topup_places_real_order_and_updates_position_when_unblocked(env, fake_broker, monkeypatch):
    node = _node()
    signal_price = 10.15
    fill_price = 10.00
    initial_shares = 40.0  # 40 * $10 = $400, well under the $800 target notional

    sig = {'current_price': signal_price, 'last_bar': datetime(2026, 7, 29, 10, 25)}
    signals_db.add_pending_buy(node, sig, channel='C0TEST', ts='1234.5', order_id=8888888888)
    signals_db.mark_pending_buy_placed_by_wl_id(node['id'])

    fake_broker.set_quote(TICKER, last=fill_price, bid=fill_price, ask=fill_price + 0.01)

    # --- act: real fill reconciliation, which internally calls _reconcile_fill ---
    signals_notify._reconcile_buy_fill(TICKER, fill_price=fill_price, filled_shares=initial_shares,
                                        wl_id=node['id'])

    # --- post-state: full check ---
    pos = signals_db.get_open_position(TICKER)
    assert pos is not None

    target_notional = 800.0
    delta = target_notional - (fill_price * initial_shares)
    expected_topup_shares = int(delta // fill_price)
    assert expected_topup_shares > 0, "test setup should genuinely need a top-up"

    expected_total_shares = initial_shares + expected_topup_shares
    assert pos['shares'] == expected_total_shares, (
        f"expected position to reflect the top-up: {initial_shares} initial + "
        f"{expected_topup_shares} top-up = {expected_total_shares}, got {pos['shares']}"
    )

    ticker_orders = [o for o in fake_broker.orders.values()
                      if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER]
    topup_orders = [o for o in ticker_orders if o['orderType'] == 'MARKET'
                     and o['orderLegCollection'][0]['instruction'] == 'BUY'
                     and o['status'] == 'FILLED']
    assert len(topup_orders) == 1, (
        f"expected exactly one real top-up MARKET BUY placed at the broker, found: "
        f"{[(o['orderId'], o['orderType'], o['status']) for o in ticker_orders]}"
    )
    assert topup_orders[0]['orderLegCollection'][0]['quantity'] == expected_topup_shares

    topup_events = signals_db.get_coverage_events(scenario_key='top_up')
    assert any(e['ticker'] == TICKER and e['result'] == 'placed' for e in topup_events), (
        "expected a top_up coverage_event with result='placed' -- the first-ever "
        "proof (real or fake-venue) that this path succeeds when unblocked, "
        "distinct from both real historical attempts which were legitimately "
        "blocked (signal-window gate, daily-order-cap)"
    )
