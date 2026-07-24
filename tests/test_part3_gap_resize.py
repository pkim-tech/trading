"""Tests for Part 3's overnight gap-correction check (signals_notify.
check_gap_resize), the shared fill-reconciliation helper (_reconcile_buy_fill/
_reconcile_fill), and drain_fill_queue. Mirrors tests/test_schwab_automation.py's
isolated-DB style: no real Schwab API calls, no real Slack posts."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import signals_notify
import schwab_safety
import schwab_client
import schwab_stream

TICKER = 'TEST_GAP_RESIZE'

_IN_WINDOW_TIME = datetime(2026, 7, 15, 10, 30)


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    monkeypatch.setattr(schwab_safety, 'STATE_PATH', tmp_path / "schwab_order_counts.json")
    monkeypatch.setattr(schwab_safety, 'KILL_SWITCH_PATH', tmp_path / "schwab_kill_switch.json")
    monkeypatch.setattr(schwab_safety, 'TICKER_AUTOMATION_PATH', tmp_path / "schwab_ticker_automation.json")
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: _IN_WINDOW_TIME)
    monkeypatch.setattr(schwab_client, '_post_message', lambda *a, **kw: (None, None))
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda account: 1_000_000.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))
    monkeypatch.setattr(signals_notify, 'time', type('T', (), {'sleep': staticmethod(lambda *a: None)}))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                         stop_loss=5, max_hold_hours=7, mode='live',
                         trail_buy_pct=1.0, trail_pct=1.0, starting_notional=50000)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account = 'ira' WHERE ticker = ?", (TICKER,))
        c.commit()

    yield

    tmp_db_path = Path(tmp_db.name)
    if tmp_db_path.exists():
        tmp_db_path.unlink()


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _sig(price=50.0):
    return {
        'ticker': TICKER, 'current_price': price, 'z_score': -1.4,
        'last_bar': _IN_WINDOW_TIME, 'lower_band': price - 1.0,
        'sma': price + 2.0, 'std': 1.0, 'hurst': None, 'adf_p': None, 'window': 20,
    }


def _pending():
    return [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER][0]


def _seed_pending_order(monkeypatch, order_id=555):
    """Places the automated trailing buy (as notify_buy_signal normally would)
    so a real order_placed=1, order_id-bearing pending_buys row exists."""
    monkeypatch.setattr(schwab_client, 'place_trailing_buy',
                         lambda *a, **kw: (object(), order_id))
    signals_notify.notify_buy_signal(_node(), _sig())
    assert _pending()['order_placed'] == 1
    assert _pending()['order_id'] == order_id


# ---------------------------------------------------------------------------
# check_gap_resize -- branch B
# ---------------------------------------------------------------------------

def test_gap_resize_no_action_when_trigger_not_cleared(env, monkeypatch):
    _seed_pending_order(monkeypatch)
    # signal_price=50.0, trail_buy_pct=1.0 -> trigger = 50.5; quote below that
    monkeypatch.setattr(schwab_client, 'get_current_price', lambda ticker: 50.2)
    monkeypatch.setattr(schwab_client, 'cancel_order',
                         lambda *a, **kw: pytest.fail("cancel_order should not be called"))
    monkeypatch.setattr(schwab_client, 'place_equity_buy',
                         lambda *a, **kw: pytest.fail("place_equity_buy should not be called"))

    signals_notify.check_gap_resize()

    pending = _pending()
    assert pending['order_id'] == 555  # untouched
    assert signals_db.get_open_position(TICKER) is None


def test_gap_resize_replaces_order_when_trigger_cleared(env, monkeypatch):
    _seed_pending_order(monkeypatch, order_id=555)
    cancelled = []

    def _fake_cancel(account, ticker, order_id):
        cancelled.append(order_id)
        return object(), 'CANCELED'

    monkeypatch.setattr(schwab_client, 'get_current_price', lambda ticker: 52.0)
    monkeypatch.setattr(schwab_client, 'cancel_order', _fake_cancel)
    monkeypatch.setattr(schwab_client, 'place_equity_buy',
                         lambda account, ticker, qty, price, is_gap_correction=False: (object(), 999))
    monkeypatch.setattr(schwab_client, 'get_filled_order',
                         lambda account, ticker, side: {'price': 52.0, 'quantity': 900})

    signals_notify.check_gap_resize()

    assert cancelled == [555]
    pos = signals_db.get_open_position(TICKER)
    assert pos is not None
    assert pos['entry_price'] == 52.0
    assert [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER] == []


def test_gap_resize_dry_run_leaves_pending_row_intact(env, monkeypatch):
    """dry_run (place_equity_buy returns (None, None)) -- no real fill will ever
    appear, so check_gap_resize must not poll forever or crash."""
    _seed_pending_order(monkeypatch, order_id=555)
    monkeypatch.setattr(schwab_client, 'get_current_price', lambda ticker: 52.0)
    monkeypatch.setattr(schwab_client, 'cancel_order', lambda *a, **kw: (object(), 'CANCELED'))
    monkeypatch.setattr(schwab_client, 'place_equity_buy', lambda *a, **kw: (None, None))
    monkeypatch.setattr(schwab_client, 'get_filled_order',
                         lambda *a, **kw: pytest.fail("should not poll for a fill in dry_run"))

    signals_notify.check_gap_resize()

    assert _pending()['order_id'] is None
    assert signals_db.get_open_position(TICKER) is None


# ---------------------------------------------------------------------------
# _reconcile_buy_fill / _reconcile_fill -- idempotency + top-up
# ---------------------------------------------------------------------------

def test_reconcile_buy_fill_is_idempotent(env, monkeypatch):
    _seed_pending_order(monkeypatch)
    signals_notify._reconcile_buy_fill(TICKER, 51.0, 100)
    pos = signals_db.get_open_position(TICKER)
    shares_after_first = pos['shares']

    # Second call for "the same fill" (as would happen if both the poll and the
    # websocket path noticed it) -- pending row is already cleared, so this must
    # be a no-op, not a second top-up/duplicate position.
    signals_notify._reconcile_buy_fill(TICKER, 51.0, 100)
    pos_after_second = signals_db.get_open_position(TICKER)
    assert pos_after_second['shares'] == shares_after_first


def test_reconcile_fill_notifies_only_on_overspend(env, monkeypatch):
    _seed_pending_order(monkeypatch)
    posted = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda msg, *a, **kw: posted.append(msg))
    # fill far exceeds target_notional ($50k) -- e.g. a huge accidental quantity
    signals_notify._reconcile_buy_fill(TICKER, 51.0, 5000)
    pos = signals_db.get_open_position(TICKER)
    assert pos['shares'] == 5000  # no corrective sell -- shares unchanged
    assert any("exceeded target notional" in m for m in posted)


def test_reconcile_fill_places_real_broker_order_for_topup(env, monkeypatch):
    """2026-07-21 fix: the top-up used to only write open_positions.shares via
    db.top_up_position with no broker call at all -- the account never
    actually held the extra shares. Assert place_equity_buy is now called for
    the top-up quantity before the DB is updated."""
    _seed_pending_order(monkeypatch)
    calls = []
    monkeypatch.setattr(schwab_client, 'place_equity_buy',
                         lambda account, ticker, qty, price, is_gap_correction=False:
                             calls.append((qty, price, is_gap_correction)) or (object(), 999))
    # 100 shares @ 51 = $5100, well under the $50k target -> real top-up expected
    signals_notify._reconcile_buy_fill(TICKER, 51.0, 100)
    assert len(calls) == 1
    qty, price, is_gap_correction = calls[0]
    assert qty > 0
    assert is_gap_correction is False
    pos = signals_db.get_open_position(TICKER)
    assert pos['shares'] == 100 + qty


def test_reconcile_fill_topup_blocked_leaves_shares_unchanged(env, monkeypatch):
    """If schwab_safety blocks the top-up order, the DB must not record shares
    the account never actually bought."""
    _seed_pending_order(monkeypatch)
    posted = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda msg, *a, **kw: posted.append(msg))

    def _blocked(*a, **kw):
        raise schwab_safety.SafetyViolation("kill switch engaged")
    monkeypatch.setattr(schwab_client, 'place_equity_buy', _blocked)

    signals_notify._reconcile_buy_fill(TICKER, 51.0, 100)
    pos = signals_db.get_open_position(TICKER)
    assert pos['shares'] == 100  # no phantom top-up shares recorded
    assert any("top-up buy" in m and "blocked" in m for m in posted)


def test_reconcile_fill_topup_passes_through_gap_correction(env, monkeypatch):
    """A top-up following a gap-correction fill must itself bypass the
    signal-window gate (check_gap_resize fires outside _SIGNAL_WINDOWS/
    _OPEN_CHECK_WINDOWS) -- otherwise the top-up buy gets wrongly blocked."""
    _seed_pending_order(monkeypatch)
    calls = []
    monkeypatch.setattr(schwab_client, 'place_equity_buy',
                         lambda account, ticker, qty, price, is_gap_correction=False:
                             calls.append(is_gap_correction) or (object(), 999))
    signals_notify._reconcile_buy_fill(TICKER, 51.0, 100, is_gap_correction=True)
    assert calls == [True]


# ---------------------------------------------------------------------------
# drain_fill_queue -- fast path
# ---------------------------------------------------------------------------

def test_drain_fill_queue_reconciles_queued_fill(env, monkeypatch):
    """The stream event is only a wake-up signal -- the real price/quantity
    must come from get_filled_order's aggregated poll, not the queued values
    (which a message like this deliberately mismatches, to prove it's ignored)."""
    _seed_pending_order(monkeypatch)
    monkeypatch.setattr(schwab_client, 'get_filled_order',
                         lambda account, ticker, side: {'price': 52.0, 'quantity': 150})
    schwab_stream.FILL_QUEUE.put(('ira', TICKER, 'BUY', 51.0, 100))

    signals_notify.drain_fill_queue()

    pos = signals_db.get_open_position(TICKER)
    assert pos is not None
    assert pos['entry_price'] == 52.0  # from get_filled_order, not the queued (mismatched) 51.0


def test_drain_fill_queue_ignores_sell_events(env, monkeypatch):
    _seed_pending_order(monkeypatch)
    schwab_stream.FILL_QUEUE.put(('ira', TICKER, 'SELL', 51.0, 100))

    signals_notify.drain_fill_queue()

    # pending buy untouched by a SELL event
    assert _pending()['order_placed'] == 1
    assert signals_db.get_open_position(TICKER) is None


def test_drain_fill_queue_no_op_when_order_not_yet_settled(env, monkeypatch):
    """A partial/in-flight execution shouldn't be locked in -- if
    get_filled_order never reports the order as FILLED within the poll
    window, drain_fill_queue must leave the pending buy alone for the slow
    check_auto_fills poll to catch later, not act on an unconfirmed fill."""
    _seed_pending_order(monkeypatch)
    monkeypatch.setattr(schwab_client, 'get_filled_order', lambda account, ticker, side: None)
    monkeypatch.setattr(signals_notify.time, 'sleep', lambda secs: None)
    schwab_stream.FILL_QUEUE.put(('ira', TICKER, 'BUY', 51.0, 100))

    signals_notify.drain_fill_queue()

    assert _pending()['order_placed'] == 1
    assert signals_db.get_open_position(TICKER) is None
