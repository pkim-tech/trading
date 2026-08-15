"""Fake-venue test for drain_fill_queue's fast-path fill reconciliation
(scenario_key 'fast_path_fill_reconciliation').

The real value: proves that drain_fill_queue uses the POLLED price/quantity
from get_filled_order, NOT the stream event's potentially-stale/partial
values -- guards against the 2026-07-22 incident (a partial ExecutionActivity
message misrecorded as a full fill, which would have triggered a phantom
top-up on top of an already-wrong base). starting_notional is set close to
the real fill value so _reconcile_fill's own top-up logic doesn't fire and
confound the shares assertion -- that's a different, already-tested code
path (see test_fake_broker_topup_scenario.py)."""
import os
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
import schwab_stream

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_DRAIN_FILL'
ACCOUNT = 'soxl_ira'  # dry_run=False -- a real fill/reconciliation, matching what this scenario actually guards
# FILL_QUEUE's real shape carries the raw Schwab account number, never an
# alias (2026-08-16 AccountNumber-defect fix) -- ACCOUNT above stays the alias
# for order-placement/DB calls, this is only for constructing the raw stream
# tuple. Resolves back to ACCOUNT via the real .env's SCHWAB_ACCOUNT_SOXL_IRA
# suffix (not blanked in this test module).
RAW_ACCOUNT_NUMBER = '45110' + os.environ.get('SCHWAB_ACCOUNT_SOXL_IRA', '931')
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
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_node_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: _IN_WINDOW_TIME)
    monkeypatch.setattr(signals_notify, 'time', type('T', (), {'sleep': staticmethod(lambda *a: None)}))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    signals_db.ensure_tables()
    # starting_notional close to the real fill value ($501.00 at 10 shares @
    # $50.05) so the post-fill top-up threshold isn't crossed -- keeps this
    # test isolated to the polled-vs-stream-quantity question only.
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                         stop_loss=5, max_hold_hours=7, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account=ACCOUNT, starting_notional=505)

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _sig(price=50.0):
    return {
        'ticker': TICKER, 'current_price': price, 'z_score': -1.4,
        'last_bar': _IN_WINDOW_TIME, 'lower_band': price - 1.0,
        'sma': price + 2.0, 'std': 1.0, 'hurst': None, 'adf_p': None, 'window': 20,
    }


def _enable_auto_fill_detection():
    schwab_safety.enable_auto_fill_detection(TICKER)
    schwab_safety.enable_node_auto_fill_detection(_node()['id'])


def test_polled_quantity_wins_over_stale_stream_quantity(env, fake_broker, monkeypatch):
    """Stream event carries a deliberately wrong/partial quantity+price; the
    real broker order is actually filled at a different (true, full)
    quantity+price. Assert the resulting position reflects the POLLED
    values, not the stream's."""
    node = _node()
    sig = _sig()
    fake_broker.set_quote(TICKER, last=50.0, bid=50.0, ask=50.01)
    fake_broker.set_cash_balance(ACCOUNT, 1_000_000.0)

    # Place the real order first so a real order_id/order book entry exists
    # for get_filled_order to resolve against.
    r, order_id = schwab_client.place_equity_buy(ACCOUNT, TICKER, 10, 50.0)
    assert order_id is not None
    signals_db.add_pending_buy(node, sig, channel='C0TEST', ts='1234.5', order_id=order_id)
    signals_db.mark_pending_buy_placed(TICKER)

    _enable_auto_fill_detection()

    stream_price = 50.0
    stream_shares = 5  # DELIBERATELY WRONG -- simulates a stale/partial execution report
    polled_price = 50.05
    polled_shares = 10  # CORRECT -- the true full fill

    monkeypatch.setattr(schwab_client, 'get_filled_order',
                         lambda account, ticker, side, order_id=None: {
                             'price': polled_price, 'quantity': polled_shares
                         })

    schwab_stream.FILL_QUEUE.put((RAW_ACCOUNT_NUMBER, TICKER, 'BUY', stream_price, stream_shares, order_id))

    signals_notify.drain_fill_queue()

    pos = signals_db.get_open_position(TICKER)
    assert pos is not None, "position should be created"
    assert pos['entry_price'] == polled_price, (
        f"entry_price should use POLLED value {polled_price}, not stream's {stream_price}"
    )
    assert pos['shares'] == polled_shares, (
        f"shares should use POLLED value {polled_shares}, not stream's {stream_shares}"
    )

    events = signals_db.get_coverage_events(scenario_key="fast_path_fill_reconciliation")
    assert len(events) > 0, "should have logged a coverage event"
    latest_event = max(events, key=lambda e: e['ts'])
    assert latest_event['result'] == 'confirmed_via_poll', (
        f"coverage event should show result='confirmed_via_poll', got {latest_event.get('result')}"
    )


def test_skip_when_auto_fill_detection_disabled(env, fake_broker, monkeypatch):
    """Skip branch: when auto_fill_detection is disabled, drain_fill_queue
    must skip the fill entirely (never even poll get_filled_order)."""
    node = _node()
    sig = _sig()
    fake_broker.set_quote(TICKER, last=50.0, bid=50.0, ask=50.01)
    fake_broker.set_cash_balance(ACCOUNT, 1_000_000.0)

    r, order_id = schwab_client.place_equity_buy(ACCOUNT, TICKER, 10, 50.0)
    signals_db.add_pending_buy(node, sig, channel='C0TEST', ts='1234.5', order_id=order_id)
    signals_db.mark_pending_buy_placed(TICKER)
    # DO NOT enable auto_fill_detection -- leave it OFF (the default)

    get_filled_order_called = []

    def mock_get_filled_order(account, ticker, side, order_id=None):
        get_filled_order_called.append((account, ticker, side, order_id))
        return {'price': 50.0, 'quantity': 10}

    monkeypatch.setattr(schwab_client, 'get_filled_order', mock_get_filled_order)

    schwab_stream.FILL_QUEUE.put((RAW_ACCOUNT_NUMBER, TICKER, 'BUY', 50.0, 10, order_id))

    signals_notify.drain_fill_queue()

    assert signals_db.get_open_position(TICKER) is None, \
        "position should NOT be created when auto_fill_detection is disabled"
    assert len(get_filled_order_called) == 0, \
        f"get_filled_order should never be called, but was called {len(get_filled_order_called)} times"

    events = signals_db.get_coverage_events(scenario_key="fast_path_fill_reconciliation")
    assert any(e['result'] == 'auto_fill_detection_disabled' for e in events)
