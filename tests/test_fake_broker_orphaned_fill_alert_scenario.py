"""Fake-venue regression test for the orphan-fill alert added to
drain_fill_queue 2026-08-07 (the GDXU incident fix, paired-Opus-reviewed).

The original bug (found live): a real BUY fill with NO matching
pending_buys row at all was silently absorbed by
node_auto_fill_detection_enabled(None) returning False -- a bypass-staged
real order (stage_live_test_order.py) whose own node lookup failed placed a
real trailing-buy that filled, and nothing anywhere alerted for a week.

This proves the fix: a stream fill event with no matching pending_buys row,
for a ticker in automation scope, now confirms the fill via a real broker
poll and alerts loudly instead of silently continuing. Also proves the
adjacent, already-correct behavior still holds: a fill that DOES match a
pending row is unaffected by the new branch (no double-alert, normal
reconciliation still happens)."""
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

TICKER = 'TEST_ORPHAN_FILL'
ACCOUNT = 'soxl_ira'
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
    yield
    Path(tmp_db.name).unlink(missing_ok=True)


def test_orphaned_fill_alerts_loudly_with_no_pending_row(env, fake_broker, monkeypatch):
    """The exact GDXU shape: a real order fills, but NO pending_buys row was
    ever created for it (mirrors stage_live_test_order.py's node-lookup-
    failed path -- the order still places fine since check_order's own
    ticker-scope gate only needs the ticker to be live-mode on the active
    watchlist, nothing about pending_buys). drain_fill_queue must alert, not
    silently continue."""
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                         stop_loss=5, max_hold_hours=7, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account=ACCOUNT, starting_notional=505)
    fake_broker.set_quote(TICKER, last=50.0, bid=50.0, ask=50.01)
    fake_broker.set_cash_balance(ACCOUNT, 1_000_000.0)

    # Place a real order directly (bypassing the normal signal/pending flow
    # entirely, same as stage_live_test_order.py's bug did) -- no
    # add_pending_buy call at all, mirroring the node-lookup-failed path.
    r, order_id = schwab_client.place_equity_buy(ACCOUNT, TICKER, 10, 50.0)
    assert order_id is not None
    assert signals_db.get_pending_buys() == [], "must genuinely have zero pending_buys rows"

    posted = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (posted.append(a[0] if a else kw.get('text')), (None, None))[1])

    schwab_stream.FILL_QUEUE.put((RAW_ACCOUNT_NUMBER, TICKER, 'BUY', 50.0, 10, order_id))
    signals_notify.drain_fill_queue()

    assert any('NO pending_buys row matches' in m for m in posted), (
        f"expected an orphan-fill alert, got: {posted}"
    )
    assert signals_db.get_open_position(TICKER) is None, (
        "must NOT attempt to open a position -- no node to attribute it to"
    )

    events = signals_db.get_coverage_events(scenario_key="orphaned_fill_detected")
    assert len(events) == 1
    assert events[0]['result'] == 'alerted'
    assert events[0]['ticker'] == TICKER


def test_matching_pending_row_does_not_trigger_the_orphan_alert(env, fake_broker, monkeypatch):
    """Control: a fill that DOES match a real pending_buys row must not fire
    the new orphan-fill branch at all (no double-alert, existing
    auto-fill-detection gating still applies unchanged)."""
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                         stop_loss=5, max_hold_hours=7, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account=ACCOUNT, starting_notional=505)
    node = [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]
    sig = {'ticker': TICKER, 'current_price': 50.0, 'z_score': -1.4, 'last_bar': _IN_WINDOW_TIME,
           'lower_band': 49.0, 'sma': 52.0, 'std': 1.0, 'hurst': None, 'adf_p': None, 'window': 20}

    fake_broker.set_quote(TICKER, last=50.0, bid=50.0, ask=50.01)
    fake_broker.set_cash_balance(ACCOUNT, 1_000_000.0)

    r, order_id = schwab_client.place_equity_buy(ACCOUNT, TICKER, 10, 50.0)
    signals_db.add_pending_buy(node, sig, channel='C0TEST', ts='1234.5', order_id=order_id)
    signals_db.mark_pending_buy_placed(TICKER)
    schwab_safety.enable_auto_fill_detection(TICKER)
    schwab_safety.enable_node_auto_fill_detection(node['id'])

    posted = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (posted.append(a[0] if a else kw.get('text')), (None, None))[1])

    schwab_stream.FILL_QUEUE.put((RAW_ACCOUNT_NUMBER, TICKER, 'BUY', 50.0, 10, order_id))
    signals_notify.drain_fill_queue()

    assert not any('NO pending_buys row matches' in m for m in posted), (
        f"orphan alert must not fire when a pending row matches: {posted}"
    )
    assert signals_db.get_coverage_events(scenario_key="orphaned_fill_detected") == []
    assert signals_db.get_open_position(TICKER) is not None, "the real reconciliation path should still open it"
