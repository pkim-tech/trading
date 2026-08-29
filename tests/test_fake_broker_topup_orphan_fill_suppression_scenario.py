"""Fake-venue regression test for the orphaned_fill_detected false-alarm fix,
2026-08-28: drain_fill_queue's orphan-fill check (see signals_notify.py,
"ORPHAN-FILL ALERT") false-fired on BOTH legs of a real primary+top-up fill
pair -- _reconcile_fill's documented same-day top-up mechanism (CLAUDE.md's
Position sizing section) places a SEPARATE broker order for the shortfall
with no pending_buys row by design, AND the primary fill's own stream event
self-orphans once its pending_buys row is cleared by whichever path
reconciles it first -- even though the position/SL were already correctly
reconciled in both cases. Verified real against SOXL/DPST/RETL/DFEN broker
order/coverage_event data before this fix.

An earlier version of this fix keyed off a fuzzy ticker+account
db.get_watch_list_node lookup plus a node-scoped 'top_up' coverage_event
share-count match. Paired review (independent-cold + contextual Opus) found
BOTH broken against real production data: get_watch_list_node returns None
(ambiguous match) for SOXL/DPST/DFEN's real watch_list rows -- multiple
archived/paper sibling rows share the same ticker+account, and that lookup
applies no state/archived_at filter -- so the suppression never actually
fired for the tickers the real incident was about; and keying on a specific
'top_up' event only ever covered the top-up leg, never the primary leg's own
self-orphaning.

Fixed by reusing db.get_real_open_position(ticker, account) -- the exact
precedent _reconcile_buy_fill's own no-pending-rows-at-all branch already
uses for the identical bug shape, a few lines above in the same file --
bounded to a recency window (signals_config.POLL_SECS) since this call site
fires per-order-id, far more often than that sibling's coarser check.

This file proves:
1. The top-up's own fill event does NOT alert (suppressed).
2. The PRIMARY fill's own fill event, if re-drained after its pending_buys
   row was already cleared by a faster reconcile path, also does NOT alert
   (the real second half of the incident the first fix version missed).
3. A genuine orphan (no real open position at all) STILL alerts loudly.
4. A real open position exists but is STALE (opened well outside the
   recency window) -- must NOT suppress a fresh, unrelated orphan just
   because some old position happens to exist for the same ticker/account."""
import os
import sys
import tempfile
from datetime import datetime, timedelta
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

TICKER = 'TEST_TOPUP_ORPHAN'
OTHER_TICKER = 'TEST_TOPUP_ORPHAN_GENUINE'
STALE_TICKER = 'TEST_TOPUP_ORPHAN_STALE'
ACCOUNT = 'soxl_ira'
RAW_ACCOUNT_NUMBER = '45110' + os.environ.get('SCHWAB_ACCOUNT_SOXL_IRA', '931')
_IN_WINDOW_TIME = datetime(2026, 7, 29, 10, 30)


def _leg(o):
    """fake_broker stores orders in schwab-py's real shape
    (orderLegCollection[0]), not flat ticker/side keys."""
    return o['orderLegCollection'][0]


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
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER, OTHER_TICKER, STALE_TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: _IN_WINDOW_TIME)
    monkeypatch.setattr(signals_notify, 'time', type('T', (), {'sleep': staticmethod(lambda *a: None)}))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    signals_db.ensure_tables()
    yield
    Path(tmp_db.name).unlink(missing_ok=True)


def _place_primary_and_topup(fake_broker, node, sig):
    """Drives the real primary fill through drain_fill_queue (matched-pending
    path -> _reconcile_buy_fill -> _reconcile_fill's real top-up branch),
    returns (primary_order_id, topup_order_id). Primary: 10 shares @ $50 =
    $500, under target_notional=$600 -- triggers a small 2-share top-up
    (matches the real SOXL/DPST shape: "1-2 shares"). Deliberately sized so
    the top-up ISN'T close enough to the primary to trip schwab_safety's real
    duplicate-order detector (same ticker/account/similar size within
    60s/5% tolerance would legitimately BLOCK it as a suspected duplicate --
    a different, real safety feature this test must not fight)."""
    fake_broker.set_quote(TICKER, last=50.0, bid=50.0, ask=50.01)
    fake_broker.set_cash_balance(ACCOUNT, 1_000_000.0)

    r, primary_order_id = schwab_client.place_equity_buy(ACCOUNT, TICKER, 10, 50.0)
    signals_db.add_pending_buy(node, sig, channel='C0TEST', ts='1234.5', order_id=primary_order_id)
    signals_db.mark_pending_buy_placed(TICKER)
    schwab_safety.enable_auto_fill_detection(TICKER)
    schwab_safety.enable_node_auto_fill_detection(node['id'])

    schwab_stream.FILL_QUEUE.put((RAW_ACCOUNT_NUMBER, TICKER, 'BUY', 50.0, 10, primary_order_id))
    signals_notify.drain_fill_queue()

    assert signals_db.get_open_position(TICKER) is not None, "primary fill must have opened the position"
    topup_events = signals_db.get_coverage_events(scenario_key="top_up")
    assert len(topup_events) == 1 and topup_events[0]['result'] == 'placed', (
        f"expected the real top-up to have fired: {topup_events}")
    position_after_topup = signals_db.get_open_position(TICKER)
    assert position_after_topup['shares'] == 12, (
        f"expected primary 10 + top-up 2 = 12 shares, got {position_after_topup['shares']}")

    topup_order_ids = [oid for oid, o in fake_broker.orders.items()
                        if _leg(o)['instrument']['symbol'] == TICKER and _leg(o)['instruction'] == 'BUY'
                        and oid != primary_order_id]
    assert len(topup_order_ids) == 1, f"expected exactly 1 top-up order, found: {fake_broker.orders}"
    assert signals_db.get_pending_buys() == [], (
        "both orders must genuinely have no pending_buys row left at this point")
    return primary_order_id, topup_order_ids[0]


def test_topup_own_fill_event_does_not_false_fire_orphan_alert(env, fake_broker, monkeypatch):
    """The exact SOXL/DPST shape: draining the top-up order's OWN fill event
    (a real, separate broker order_id, no pending_buys row by design) must
    NOT alert -- it's already accounted for by the real open position."""
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                         stop_loss=5, max_hold_hours=7, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account=ACCOUNT, starting_notional=600)
    node = [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]
    sig = {'ticker': TICKER, 'current_price': 50.0, 'z_score': -1.4, 'last_bar': _IN_WINDOW_TIME,
           'lower_band': 49.0, 'sma': 52.0, 'std': 1.0, 'hurst': None, 'adf_p': None, 'window': 20}
    _primary_order_id, topup_order_id = _place_primary_and_topup(fake_broker, node, sig)

    posted = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (posted.append(a[0] if a else kw.get('text')), (None, None))[1])

    schwab_stream.FILL_QUEUE.put((RAW_ACCOUNT_NUMBER, TICKER, 'BUY', 50.0, 10, topup_order_id))
    signals_notify.drain_fill_queue()

    assert not any('NO pending_buys row matches' in m for m in posted), (
        f"top-up's own fill event must NOT false-fire the orphan alert: {posted}"
    )
    orphan_events = signals_db.get_coverage_events(scenario_key="orphaned_fill_detected")
    assert len(orphan_events) == 1, f"expected exactly one orphaned_fill_detected event (suppressed): {orphan_events}"
    assert orphan_events[0]['result'] == 'suppressed_reconciled_position', (
        f"expected suppression, got: {orphan_events[0]}")
    assert orphan_events[0]['node_id'] == node['id']


def test_primary_fill_own_delayed_stream_event_also_does_not_false_fire(env, fake_broker, monkeypatch):
    """The other half of the real incident (found by paired review of the
    first fix attempt): the PRIMARY fill's own stream event self-orphans if
    it's re-drained AFTER its pending_buys row was already cleared by a
    faster reconcile path -- confirmed against real coverage_events showing
    BOTH the primary and the top-up alerting in every real occurrence.
    Reproduced here by re-queuing the primary order's OWN fill a second time
    (its pending_buys row is already gone from the first drain)."""
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                         stop_loss=5, max_hold_hours=7, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account=ACCOUNT, starting_notional=600)
    node = [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]
    sig = {'ticker': TICKER, 'current_price': 50.0, 'z_score': -1.4, 'last_bar': _IN_WINDOW_TIME,
           'lower_band': 49.0, 'sma': 52.0, 'std': 1.0, 'hurst': None, 'adf_p': None, 'window': 20}
    primary_order_id, _topup_order_id = _place_primary_and_topup(fake_broker, node, sig)

    posted = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (posted.append(a[0] if a else kw.get('text')), (None, None))[1])

    # Re-drain the PRIMARY order's own fill event -- its pending_buys row was
    # already cleared by the first drain above, so this now lands in the
    # orphan-fill branch too, exactly like the real incident's primary leg.
    schwab_stream.FILL_QUEUE.put((RAW_ACCOUNT_NUMBER, TICKER, 'BUY', 50.0, 10, primary_order_id))
    signals_notify.drain_fill_queue()

    assert not any('NO pending_buys row matches' in m for m in posted), (
        f"primary fill's own delayed stream event must NOT false-fire the orphan alert: {posted}"
    )
    orphan_events = signals_db.get_coverage_events(scenario_key="orphaned_fill_detected")
    assert len(orphan_events) == 1
    assert orphan_events[0]['result'] == 'suppressed_reconciled_position'


def test_genuine_orphan_with_no_accounting_position_still_alerts(env, fake_broker, monkeypatch):
    """Control: an order that fills with NO matching pending_buys row AND NO
    real open position at all for this ticker/account must still alert
    loudly. Mirrors the real GDXU incident shape (test_fake_broker_
    orphaned_fill_alert_scenario.py) -- the suppression is narrow, not a
    blanket silence on the orphan-fill branch."""
    signals_db.add_node(OTHER_TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                         stop_loss=5, max_hold_hours=7, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account=ACCOUNT, starting_notional=505)
    fake_broker.set_quote(OTHER_TICKER, last=50.0, bid=50.0, ask=50.01)
    fake_broker.set_cash_balance(ACCOUNT, 1_000_000.0)

    r, order_id = schwab_client.place_equity_buy(ACCOUNT, OTHER_TICKER, 10, 50.0)
    assert order_id is not None
    assert signals_db.get_pending_buys() == [], "must genuinely have zero pending_buys rows"
    assert signals_db.get_open_position(OTHER_TICKER) is None, "must genuinely have zero open positions"

    posted = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (posted.append(a[0] if a else kw.get('text')), (None, None))[1])

    schwab_stream.FILL_QUEUE.put((RAW_ACCOUNT_NUMBER, OTHER_TICKER, 'BUY', 50.0, 10, order_id))
    signals_notify.drain_fill_queue()

    assert any('NO pending_buys row matches' in m for m in posted), (
        f"expected a genuine orphan-fill alert, got: {posted}"
    )
    events = signals_db.get_coverage_events(scenario_key="orphaned_fill_detected")
    assert len(events) == 1
    assert events[0]['result'] == 'alerted'


def test_stale_position_does_not_suppress_a_fresh_unrelated_orphan(env, fake_broker, monkeypatch):
    """A real open position exists for this ticker/account, but it's STALE
    (opened well outside _ORPHAN_RECONCILED_POSITION_WINDOW_SECS) -- must NOT
    suppress a fresh, unrelated orphan fill just because some old position
    happens to exist. Proves the recency bound is actually enforced, not
    just present in the code."""
    signals_db.add_node(STALE_TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                         stop_loss=5, max_hold_hours=7, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account=ACCOUNT, starting_notional=505)
    node = [n for n in signals_db.get_watchlist() if n['ticker'] == STALE_TICKER][0]

    stale_entry_time = datetime.now() - timedelta(seconds=signals_config.POLL_SECS * 3)
    signals_db.open_position(node, signal_price=50.0, signal_time=stale_entry_time,
                              entry_price=50.0, entry_time=stale_entry_time, shares=10)
    assert signals_db.get_open_position(STALE_TICKER) is not None

    fake_broker.set_quote(STALE_TICKER, last=50.0, bid=50.0, ask=50.01)
    fake_broker.set_cash_balance(ACCOUNT, 1_000_000.0)
    # is_protective=True: a second real order for a ticker/account that
    # already holds a position is otherwise blocked outright by
    # schwab_safety's existing-position guard (a real, correct safety
    # feature, not something this test should fight) -- is_protective is
    # the one sanctioned exception, matching how a real top-up order
    # actually gets placed against an existing position. This new order is
    # deliberately UNRELATED to the stale position (no pending_buys row,
    # no top_up event) -- it's simulating a fresh, genuine orphan that just
    # happens to land on a ticker/account with some old position on file.
    r, order_id = schwab_client.place_equity_buy(ACCOUNT, STALE_TICKER, 3, 50.0, is_protective=True)
    assert signals_db.get_pending_buys() == [], "must genuinely have zero pending_buys rows for this new fill"

    posted = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (posted.append(a[0] if a else kw.get('text')), (None, None))[1])

    schwab_stream.FILL_QUEUE.put((RAW_ACCOUNT_NUMBER, STALE_TICKER, 'BUY', 50.0, 3, order_id))
    signals_notify.drain_fill_queue()

    assert any('NO pending_buys row matches' in m for m in posted), (
        f"a stale, unrelated open position must not suppress a fresh orphan fill: {posted}"
    )
    events = signals_db.get_coverage_events(scenario_key="orphaned_fill_detected")
    assert len(events) == 1
    assert events[0]['result'] == 'alerted'
