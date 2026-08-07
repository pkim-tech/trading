"""Fake-venue test proving the production SELL path handles a REAL broker
REJECTED status correctly -- closes a gap flagged 2026-08-01: the 2026-07-23
live naked-sell/oversell test (docs/backlog_cache.md) confirmed Schwab itself
rejects an oversell cleanly, but every one of those tests went through a
direct schwab_client/schwab.orders bypass, never through the real production
chain (schwab_safety.check_order's guards + schwab_client.place_trailing_sell/
_place_trailing_order + schwab_client.OrderRejected). Separately, the existing
manual_sl_fallback_alert test (test_fake_broker_exit_lifecycle_phase2_scenario.py)
only ever mocks schwab_client.place_trailing_sell/replace_order_with_trailing_sell
with a generic raised Exception -- it never drives a REAL REJECTED status
through _post_order_confirmation's real polling-and-raise logic
(schwab_client.py's OrderRejected class). This test does.

Real chain exercised: signals_notify._attempt_automated_sell ->
schwab_client.place_trailing_sell -> _place_trailing_order ->
_post_order_confirmation -> (real order status polled via fake_broker.get_order,
returns REJECTED) -> raises OrderRejected -> propagates back up through
_attempt_automated_sell's except Exception branch -> the real fallback alert
and coverage_event logging."""
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
import schwab_client

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_ORDER_REJECTED'
_IN_WINDOW_TIME = datetime(2026, 7, 29, 10, 30)


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
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: _IN_WINDOW_TIME)
    monkeypatch.setattr(schwab_client, '_ORDER_CONFIRM_POLL_INTERVAL_SECS', 0)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account='soxl_ira', starting_notional=5000)

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def test_rejected_trailing_sell_falls_back_to_manual_with_no_state_corruption(env, fake_broker, monkeypatch):
    """A real trailing-SELL placement whose broker status polls back REJECTED
    (e.g. an oversell/naked-short attempt Schwab actually refuses, per the
    real 2026-07-23 test) must: raise OrderRejected internally, get caught by
    _attempt_automated_sell's except Exception branch, log a real
    automated_sell_execution coverage_event with a non-'placed' result, post a
    clear alert, return (False, None) so the caller falls back to manual --
    and leave local state clean (no phantom sl_order_id pointing at a dead
    order, position still open and untouched)."""
    node = _node()
    entry_price = 100.0
    shares = 50
    signals_db.open_position(node, entry_price, _IN_WINDOW_TIME, entry_price, _IN_WINDOW_TIME, shares=shares)
    pos = signals_db.get_open_position(TICKER)
    assert pos['sl_order_id'] is None  # no pre-existing resting SL for this scenario

    fake_broker.set_quote(TICKER, last=105.0, bid=105.0, ask=105.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    # Force every order status confirmation to read REJECTED -- simulates the
    # real Schwab behavior confirmed live 2026-07-23 (naked_sell/oversell both
    # got a real REJECTED with "oversold/overbought"), but through the real
    # production polling path instead of a bypass.
    from fake_broker import FakeResponse
    real_get_order = fake_broker.get_order

    def _rejecting_get_order(order_id, account_hash):
        o = fake_broker.orders.get(order_id)
        if o is not None:
            o['status'] = 'REJECTED'  # mutate the real ledger, not just the response -- matches
        return real_get_order(order_id, account_hash)  # real Schwab behavior (REJECTED is terminal)

    monkeypatch.setattr(fake_broker, 'get_order', _rejecting_get_order)

    posted = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (posted.append(a[0] if a else kw.get('text')), (None, None))[1])

    auto_placed, exit_order_id = signals_notify._attempt_automated_sell(pos, 105.0)

    assert auto_placed is False
    assert exit_order_id is None

    events = signals_db.get_coverage_events(scenario_key='automated_sell_execution')
    rejected_events = [e for e in events if e['ticker'] == TICKER]
    assert len(rejected_events) >= 1
    assert rejected_events[-1]['result'] == 'failed_unexpectedly', (
        "a real OrderRejected must be classified as an unexpected failure "
        "(not silently treated as 'blocked', which implies our own guard caught it "
        "-- Schwab's rejection is a different, more important signal)"
    )
    assert 'REJECTED' in (rejected_events[-1].get('detail') or '')

    assert any('failed unexpectedly' in (m or '') or 'REJECTED' in (m or '') for m in posted), (
        f"expected a clear fallback-to-manual alert, got: {posted}"
    )

    # State must stay clean: position untouched, no phantom SL pointer left
    # dangling at a dead/rejected order id.
    pos_after = signals_db.get_open_position(TICKER)
    assert pos_after is not None
    assert pos_after['shares'] == shares
    assert pos_after['sl_order_id'] is None

    # No real order should be left resting -- REJECTED is terminal.
    live_orders = [o for o in fake_broker.orders.values()
                   if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
                   and o['status'] not in ('REJECTED', 'CANCELED', 'FILLED')]
    assert live_orders == [], f"no order should be left resting after a REJECTED confirmation: {live_orders}"
