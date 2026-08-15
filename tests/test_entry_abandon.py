"""Entry-abandon timeout: signals_notify.check_entry_abandon (real/dry_run) and
paper_trading.update_paper_buys' abandon branch (paper) -- the live equivalent
of the backtest kernel's `wait_bars >= max_hours_to_hold` (backtester.py's
_simulate_trail_buy/_simulate_trail_both). Without this, a trailing buy that
never bounces rests forever (a real GTC order for a live account), silently
blocking every other BUY. Follows tests/test_dry_run_sim.py's fixture pattern."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db
import signals_notify
import paper_trading
import schwab_client
import schwab_safety
from tests.conftest import make_synthetic_csv, cleanup_csv, _synthetic_timestamps

TICKER = 'TEST_ABANDON'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    posted = []
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda *a, **kw: (posted.append(a[0] if a else kw.get('text')), (None, None))[1])
    monkeypatch.setattr(paper_trading, '_post_message',
                         lambda *a, **kw: (posted.append(a[0] if a else kw.get('text')), (None, None))[1])
    # Module-level cooldown dict, not reset between tests by default -- clear
    # it so one test's throttled alert doesn't silently suppress another
    # test's first alert for the same (wl_id, kind) key (same pattern as
    # _RECONCILE_ALERTED.clear() in tests/test_live_state_reconciliation.py).
    signals_notify._ENTRY_ABANDON_ALERTED.clear()

    db.ensure_tables()
    make_synthetic_csv(TICKER, last_close=100.0)
    # max_hold_hours=7 bars -- fake_node/_synthetic_timestamps convention: hours_ago
    # is really bars-ago on the fixed 2025 hourly grid.
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                stop_loss=1, max_hold_hours=7, state='live',
                trail_buy_pct=1.0, trail_pct=1.0, starting_notional=5000, fixed_sl_override=1.0)

    yield posted

    cleanup_csv(TICKER)
    tmp_db_path = Path(tmp_db.name)
    if tmp_db_path.exists():
        tmp_db_path.unlink()


def _node():
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]


def _sig(price, hours_ago):
    timestamps = _synthetic_timestamps()
    last_bar = timestamps[-1 - hours_ago] if hours_ago < len(timestamps) else timestamps[0]
    return {'ticker': TICKER, 'current_price': price, 'z_score': -2.5, 'last_bar': last_bar}


def _set_account(account):
    with db._conn() as c:
        c.execute("UPDATE watch_list SET account = ? WHERE ticker = ?", (account, TICKER))
        c.commit()


def test_not_yet_at_threshold_is_untouched(env):
    node = _node()
    db.add_pending_buy(node, _sig(100.0, hours_ago=3), channel=None, ts=None)
    signals_notify.check_entry_abandon()
    assert len(db.get_pending_buys()) == 1


def test_unrecognized_account_fails_closed_and_alerts(env):
    """No existing test covered the `limits is None` branch at all before
    this (found while scoping a mutation-testing pass) -- an unrecognized
    account can't be classified dry_run vs real, so must fail closed
    (alert, leave the row untouched) rather than silently doing anything."""
    _set_account('totally_unknown_account_xyz')
    node = _node()
    db.add_pending_buy(node, _sig(100.0, hours_ago=10), channel=None, ts=None)

    signals_notify.check_entry_abandon()
    assert len(db.get_pending_buys()) == 1  # not cleared
    events = db.get_coverage_events(scenario_key="entry_abandon_timeout")
    assert any(e['result'] == 'unrecognized_account' for e in events)
    assert any("isn't recognized" in m for m in env)


def test_skips_row_gap_resize_already_touched_today(env, monkeypatch):
    """Regression test for a review finding: check_gap_resize (run earlier in
    the same run_loop iteration) can replace this row's resting trailing-buy
    with a real MARKET order and write the new order_id back onto the SAME
    pending_buys row -- the node is still _is_trailing_buy, so without a
    gap_resize_date-today skip, check_entry_abandon could cancel that
    brand-new market order in the same iteration and falsely claim "never
    bounced ... entry abandoned" when the trigger genuinely cleared moments
    earlier."""
    _set_account('soxl_ira')
    node = _node()
    db.add_pending_buy(node, _sig(100.0, hours_ago=10), channel=None, ts=None, order_id=555)
    db.mark_pending_buy_placed_by_wl_id(node['id'])
    pending_id = db.get_pending_buys()[0]['id']
    today = datetime.now().strftime('%Y-%m-%d')
    db.mark_gap_resize_attempted(pending_id, today)

    def _boom(*a, **kw):
        raise AssertionError("cancel_order must not be called on a row check_gap_resize already touched today")
    monkeypatch.setattr(schwab_client, 'cancel_order', _boom)

    signals_notify.check_entry_abandon()
    assert len(db.get_pending_buys()) == 1  # untouched


def test_dry_run_account_abandons_without_calling_broker(env, monkeypatch):
    _set_account('roth')  # real ACCOUNTS['roth'].dry_run == True
    node = _node()
    db.add_pending_buy(node, _sig(100.0, hours_ago=10), channel=None, ts=None)

    def _boom(*a, **kw):
        raise AssertionError("cancel_order must not be called for a dry_run account")
    monkeypatch.setattr(schwab_client, 'cancel_order', _boom)

    signals_notify.check_entry_abandon()
    assert db.get_pending_buys() == []
    assert any('entry abandoned' in m for m in env)
    # dry_run: nothing real ever existed to cancel -- the terminal message
    # must say so, not the generic "resting order cancelled" claim (found by
    # review: the original wording was unconditionally wrong on this path).
    assert any('no real order existed to cancel' in m for m in env)


def test_real_account_cancel_success_message_says_cancelled_not_generic(env, monkeypatch):
    _set_account('soxl_ira')
    node = _node()
    db.add_pending_buy(node, _sig(100.0, hours_ago=10), channel=None, ts=None, order_id=555)
    db.mark_pending_buy_placed_by_wl_id(node['id'])
    monkeypatch.setattr(schwab_client, 'cancel_order', lambda account, ticker, order_id, node_id=None: (None, 'CANCELED'))

    signals_notify.check_entry_abandon()
    assert any('resting order cancelled' in m for m in env)
    assert not any('no real order existed to cancel' in m for m in env)


def test_real_account_with_no_placed_order_abandons_without_calling_broker(env, monkeypatch):
    _set_account('soxl_ira')  # real ACCOUNTS['soxl_ira'].dry_run == False
    node = _node()
    db.add_pending_buy(node, _sig(100.0, hours_ago=10), channel=None, ts=None)  # order_placed defaults 0, order_id None

    def _boom(*a, **kw):
        raise AssertionError("cancel_order must not be called with no resting order")
    monkeypatch.setattr(schwab_client, 'cancel_order', _boom)

    signals_notify.check_entry_abandon()
    assert db.get_pending_buys() == []


def test_real_account_cancels_resting_order_and_abandons(env, monkeypatch):
    _set_account('soxl_ira')
    node = _node()
    db.add_pending_buy(node, _sig(100.0, hours_ago=10), channel=None, ts=None, order_id=555)
    db.mark_pending_buy_placed_by_wl_id(node['id'])

    calls = []
    monkeypatch.setattr(schwab_client, 'cancel_order', lambda account, ticker, order_id, node_id=None: (
        calls.append((account, ticker, order_id)), (None, 'CANCELED'))[1])

    signals_notify.check_entry_abandon()
    assert calls == [('soxl_ira', TICKER, 555)]
    assert db.get_pending_buys() == []
    events = db.get_coverage_events(scenario_key="entry_abandon_timeout")
    assert any(e['result'] == 'abandoned' for e in events)


def test_real_account_cancel_racing_a_fill_reconciles_instead_of_abandoning(env, monkeypatch):
    _set_account('soxl_ira')
    node = _node()
    db.add_pending_buy(node, _sig(100.0, hours_ago=10), channel=None, ts=None, order_id=555)
    db.mark_pending_buy_placed_by_wl_id(node['id'])

    monkeypatch.setattr(schwab_client, 'cancel_order', lambda account, ticker, order_id, node_id=None: (None, 'FILLED'))
    monkeypatch.setattr(schwab_client, 'get_filled_order',
                         lambda account, ticker, side, order_id=None: {'price': 101.5, 'quantity': 10})

    signals_notify.check_entry_abandon()
    assert db.get_pending_buys() == []
    positions = db.get_open_positions()
    assert len(positions) == 1
    assert positions[0]['entry_price'] == 101.5


def test_real_account_unconfirmed_cancel_leaves_row_for_retry(env, monkeypatch):
    _set_account('soxl_ira')
    node = _node()
    db.add_pending_buy(node, _sig(100.0, hours_ago=10), channel=None, ts=None, order_id=555)
    db.mark_pending_buy_placed_by_wl_id(node['id'])

    monkeypatch.setattr(schwab_client, 'cancel_order', lambda account, ticker, order_id, node_id=None: (None, None))

    signals_notify.check_entry_abandon()
    assert len(db.get_pending_buys()) == 1  # not cleared -- fail closed, retry next poll


def test_real_account_manual_placement_with_no_order_id_does_not_clear_or_false_claim(env, monkeypatch):
    """Regression test for a review finding: the manual 'Trailing Buy Order
    Placed' Slack flow sets order_placed=True but never captures a real
    broker order id (handle_trail_buy_order_placed, signals_handlers.py).
    Indistinguishable from an automated dry_run placement by (order_placed,
    order_id) alone -- must be resolved via the account's real dry_run flag,
    not just falling through to clear-and-claim-cancelled, which would
    orphan a real resting order with zero local tracking and no way to
    detect its eventual fill."""
    _set_account('soxl_ira')  # real, dry_run=False
    node = _node()
    db.add_pending_buy(node, _sig(100.0, hours_ago=10), channel=None, ts=None)  # order_id=None
    db.mark_pending_buy_placed_by_wl_id(node['id'])  # manual "Order Placed" tap

    def _boom(*a, **kw):
        raise AssertionError("cancel_order must not be called with no order id on file")
    monkeypatch.setattr(schwab_client, 'cancel_order', _boom)

    signals_notify.check_entry_abandon()
    assert len(db.get_pending_buys()) == 1  # NOT cleared -- a real order may still be resting
    events = db.get_coverage_events(scenario_key="entry_abandon_timeout")
    assert any(e['result'] == 'no_order_id_on_file' for e in events)
    assert any('cannot auto-cancel' in m for m in env)


def test_no_order_id_alert_is_throttled_across_repeated_polls(env, monkeypatch):
    """Regression test for a review finding: unlike the terminal 'abandoned'
    branch, the three branches that leave the row in place (unrecognized
    account, no order id on file, cancel failed) used to re-alert on every
    single poll forever, since bars_held only ever grows once past
    max_hold_hours -- a real stuck position could bury real trade alerts
    behind an identical repeated message. Must be cooldown-throttled the
    same way _RECONCILE_ALERTED/_RECONCILE_COOLDOWN_SECS already is
    elsewhere in this module."""
    _set_account('soxl_ira')
    node = _node()
    db.add_pending_buy(node, _sig(100.0, hours_ago=10), channel=None, ts=None)
    db.mark_pending_buy_placed_by_wl_id(node['id'])

    signals_notify.check_entry_abandon()
    signals_notify.check_entry_abandon()
    signals_notify.check_entry_abandon()

    matching = [m for m in env if 'cannot auto-cancel' in m]
    assert len(matching) == 1, f"expected exactly 1 throttled alert across 3 polls, got {len(matching)}"


def test_dry_run_account_manual_placement_with_no_order_id_still_abandons(env):
    """Contrast with the real-account case above: for a dry_run account,
    order_placed=True with no order_id is always safe to treat as nothing
    real resting (dry_run never places a real order via either the
    automated or manual path) -- must not be blocked by the real-account-only
    'no order id on file' guard above."""
    _set_account('roth')  # dry_run=True
    node = _node()
    db.add_pending_buy(node, _sig(100.0, hours_ago=10), channel=None, ts=None)  # order_id=None
    db.mark_pending_buy_placed_by_wl_id(node['id'])

    signals_notify.check_entry_abandon()
    assert db.get_pending_buys() == []


def test_account_uses_pinned_node_snapshot_not_live_watch_list_edit(env, monkeypatch):
    """Regression test for a review finding: check_entry_abandon must read
    account/max_hold_hours from pb['node'] (the signal-time pinned snapshot),
    not a live re-fetch of the watch_list row -- a real resting order's
    account is a physical fact fixed at placement. Edits the live node's
    account AFTER the pending buy was placed; the abandon check must still
    act against the account the order actually rests in."""
    _set_account('soxl_ira')  # real, dry_run=False, at placement time
    node = _node()
    db.add_pending_buy(node, _sig(100.0, hours_ago=10), channel=None, ts=None, order_id=555)
    db.mark_pending_buy_placed_by_wl_id(node['id'])

    _set_account('roth')  # live node edited to a dry_run account afterward

    calls = []
    monkeypatch.setattr(schwab_client, 'cancel_order', lambda account, ticker, order_id, node_id=None: (
        calls.append((account, ticker, order_id)), (None, 'CANCELED'))[1])

    signals_notify.check_entry_abandon()
    assert calls == [('soxl_ira', TICKER, 555)]  # cancelled against the ORIGINAL (pinned) account
    assert db.get_pending_buys() == []


def test_market_buy_node_has_no_bounce_wait_to_abandon(env):
    """A market-buy-eligible node's pending row (no trail_buy_pct concept)
    should never be touched by the abandon check -- db._is_trailing_buy gates
    it, same guard used by update_dry_run_buys/update_paper_buys."""
    db.add_node(TICKER + '_MKT', 'ZScoreBreakout', 'test', window=20, take_profit=7,
                stop_loss=1, max_hold_hours=7, state='live')
    node = [n for n in db.get_watchlist() if n['ticker'] == TICKER + '_MKT'][0]
    make_synthetic_csv(TICKER + '_MKT', last_close=100.0)
    try:
        sig = dict(_sig(100.0, hours_ago=10))
        sig['ticker'] = TICKER + '_MKT'
        db.add_pending_buy(node, sig, channel=None, ts=None)
        signals_notify.check_entry_abandon()
        assert len(db.get_pending_buys()) == 1
    finally:
        cleanup_csv(TICKER + '_MKT')


def test_paper_pending_buy_abandons_after_max_hold_hours(env):
    node = _node()
    db.add_paper_pending_buy(node, _sig(100.0, hours_ago=10))
    assert len(db.get_paper_pending_buys()) == 1
    paper_trading.update_paper_buys()
    assert db.get_paper_pending_buys() == []
    assert db.get_open_positions(paper=True) == []


def test_paper_pending_buy_not_yet_abandoned_still_tracks_running_low(env, monkeypatch):
    node = _node()
    db.add_paper_pending_buy(node, _sig(100.0, hours_ago=3))
    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (98.0, None))
    paper_trading.update_paper_buys()
    pending = db.get_paper_pending_buys()
    assert len(pending) == 1
    assert pending[0]['running_low'] == 98.0
