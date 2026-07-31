"""Tests for the schwab_client/schwab_safety wiring into the live BUY/SELL flow
(signals_notify.py) -- automated order placement and the opt-in auto-fill-
detection toggle. Mirrors tests/test_schwab_safety.py's isolated-DB style: no
real Schwab API calls (dry_run stays True) and no real Slack posts."""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import signals_notify
import schwab_safety
import schwab_client

TICKER = 'TEST_AUTOMATION'

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
    monkeypatch.setattr(schwab_safety, 'NODE_AUTOMATION_PATH', tmp_path / "schwab_node_automation.json")
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_node_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: _IN_WINDOW_TIME)
    monkeypatch.setattr(schwab_client, '_post_message', lambda *a, **kw: (None, None))
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda account: 1_000_000.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                         stop_loss=5, max_hold_hours=7, mode='live',
                         trail_buy_pct=1.0, trail_pct=1.0)
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


# ---------------------------------------------------------------------------
# Automated placement
# ---------------------------------------------------------------------------

def test_automated_buy_placed_in_window(env):
    signals_notify.notify_buy_signal(_node(), _sig())
    pending = _pending()
    assert pending['order_placed'] == 1


def test_automated_buy_falls_back_outside_scope(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', set())
    signals_notify.notify_buy_signal(_node(), _sig())
    pending = _pending()
    assert pending['order_placed'] == 0


def test_automated_buy_falls_back_outside_signal_window(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 7, 15, 12, 0))
    signals_notify.notify_buy_signal(_node(), _sig())
    pending = _pending()
    assert pending['order_placed'] == 0


def test_automated_buy_falls_back_when_ticker_paused(env):
    schwab_safety.pause_ticker_automation(TICKER, reason="test pause")
    signals_notify.notify_buy_signal(_node(), _sig())
    pending = _pending()
    assert pending['order_placed'] == 0


def test_automated_sell_placed_and_marks_order_placed(env):
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    pos = signals_db.get_open_position(TICKER)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira' WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    signals_notify.notify_trailing_activated(pos, current_price=52.0)
    updated = [p for p in signals_db.get_open_positions() if p['ticker'] == TICKER][0]
    assert updated['trail_state'].get('order_placed') is True


def test_automated_sell_placed_logs_coverage_event(env):
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira' WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    signals_notify.notify_trailing_activated(pos, current_price=52.0)
    events = signals_db.get_coverage_events(scenario_key="automated_sell_execution")
    assert len(events) == 1
    assert events[0]['result'] == "placed"
    assert events[0]['ticker'] == TICKER


def test_automated_sell_falls_back_outside_scope(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', set())
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    pos = signals_db.get_open_position(TICKER)
    signals_notify.notify_trailing_activated(pos, current_price=52.0)
    updated = [p for p in signals_db.get_open_positions() if p['ticker'] == TICKER][0]
    assert not updated['trail_state'].get('order_placed')


def test_automated_sell_falls_back_when_node_mode_not_live(env):
    """Regression test for the SELL-side mode-gating gap (backlog, 2026-07-19):
    _attempt_automated_sell used to only check ticker scope, so a research-mode
    node's real position could still be routed through an automated sell. Flip
    the node to 'research' (ticker stays in AUTOMATION_ENABLED_TICKERS) and
    confirm the automated sell no longer fires."""
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    pos = signals_db.get_open_position(TICKER)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET mode='research' WHERE ticker=?", (TICKER,))
        c.commit()
    signals_notify.notify_trailing_activated(pos, current_price=52.0)
    updated = [p for p in signals_db.get_open_positions() if p['ticker'] == TICKER][0]
    assert not updated['trail_state'].get('order_placed')
    events = signals_db.get_coverage_events(scenario_key="automated_sell_mode_skip")
    assert len(events) == 1
    assert events[0]['result'] == "skipped"


def test_notify_trailing_activated_preserves_armed_state_written_by_check_sell_condition(env):
    """Regression test for the trail_state clobber bug (Opus review,
    2026-07-22): check_sell_condition persists the newly-armed state
    (trailing=True, peak=P) to the DB *before* notify_trailing_activated
    runs, but the `pos` dict callers pass in is still the pre-arm in-memory
    copy. notify_trailing_activated must merge its reminder fields onto the
    fresh DB state, not the stale pos['trail_state'] -- otherwise the arm is
    silently lost, check_exit re-arms on the next bar, and
    _attempt_automated_sell places a second live trailing-sell order for the
    same shares."""
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    pos = signals_db.get_open_position(TICKER)  # stale: trail_state == {}
    signals_db.update_position_trail_state(pos['id'], {'trailing': True, 'peak': 55.0})
    signals_notify.notify_trailing_activated(pos, current_price=52.0)
    updated = [p for p in signals_db.get_open_positions() if p['ticker'] == TICKER][0]
    assert updated['trail_state'].get('trailing') is True
    assert updated['trail_state'].get('peak') == 55.0


def test_automated_sell_notifies_sl_price_when_trailing_sell_fails_after_sl_cancel(env, monkeypatch):
    """Regression test for finding #2 (Opus review, 2026-07-22): if a resting
    stop-loss is cancelled to make way for the trailing-sell and the
    trailing-sell placement then fails, the position is genuinely
    unprotected -- there's no safe automatic recovery, so the user must be
    told the SL price to manually re-place at the broker."""
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira', sl_order_id='12345' WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)

    posted = []

    def _capture(msg, **kw):
        posted.append(msg)
        return (None, None)

    monkeypatch.setattr(signals_notify, '_post_message', _capture)
    monkeypatch.setattr(
        schwab_client, 'replace_order_with_trailing_sell',
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("order rejected")),
    )
    signals_notify.notify_trailing_activated(pos, current_price=52.0)

    unprotected_msgs = [m for m in posted if "UNPROTECTED" in m]
    assert len(unprotected_msgs) == 1
    assert f"*{TICKER}*" in unprotected_msgs[0]
    assert "(ira · DRY-RUN)" in unprotected_msgs[0]
    assert "place stop-loss SELL 100" in unprotected_msgs[0]
    # TrailingBothZScoreBreakout uses_fixed_sl -- real SL % comes from
    # pos['fixed_sl'] (config.json's fixed_stop_loss), not node['stop_loss'].
    expected_sl_price = 50.0 * (1 - pos['fixed_sl'] / 100)
    assert f"${expected_sl_price:.2f}" in unprotected_msgs[0]
    events = signals_db.get_coverage_events(scenario_key="manual_sl_fallback_alert")
    assert len(events) == 1
    assert events[0]['result'] == "alerted"


def test_notify_sell_signal_time_exit_logs_coverage_event(env):
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira' WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    signals_notify.notify_sell_signal(pos, 'TIME', current_price=51.0, target_price=51.0)
    events = signals_db.get_coverage_events(scenario_key="time_exit_trigger")
    assert len(events) == 1
    assert events[0]['result'] == "alert_fired"
    assert events[0]['ticker'] == TICKER


def test_automated_sell_replace_updates_stale_sl_order_id(env, monkeypatch):
    """Regression test for the sl_order_id-never-cleared-after-replace bug
    (2026-07-31 audit, left open): once the arm-time trailing-sell replace
    succeeds, open_positions.sl_order_id must be updated to the new resting
    order, not left pointing at the now-dead original SL forever."""
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira', sl_order_id='12345' WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'replace_order_with_trailing_sell', lambda *a, **kw: (None, 987))
    signals_notify.notify_trailing_activated(pos, current_price=52.0)
    updated = signals_db.get_open_position(TICKER)
    assert updated['sl_order_id'] == 987


def test_automated_exit_sell_replace_updates_stale_sl_order_id(env, monkeypatch):
    """Same regression, via the TP/SL/TIME exit path (_attempt_automated_exit_sell)
    instead of the TRAIL arm path -- a genuine (never-armed) TIME exit replaces
    the resting SL with a market sell; sl_order_id must track the new order."""
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira', sl_order_id='12345' WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'replace_equity_order_with_market', lambda *a, **kw: (None, 555))
    signals_notify.notify_sell_signal(pos, 'TIME', current_price=51.0, target_price=51.0)
    updated = signals_db.get_open_position(TICKER)
    assert updated['sl_order_id'] == 555


def test_automated_sell_replace_does_not_erase_sl_order_id_when_new_id_unextractable(env, monkeypatch):
    """Regression test for a review finding: a real successful replace can
    still return order_id=None (e.g. extract_order_id finds no Location
    header) -- the sl_order_id writeback must not overwrite a real, valid
    existing sl_order_id with None in that case, which would silence the
    missing_sl reconciliation check (gated on sl_order_id truthy) and drop
    the hold-time-forced exit fallback to None."""
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira', sl_order_id='12345' WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'replace_order_with_trailing_sell', lambda *a, **kw: (None, None))
    signals_notify.notify_trailing_activated(pos, current_price=52.0)
    updated = signals_db.get_open_position(TICKER)
    assert updated['sl_order_id'] == 12345  # unchanged, not erased to None


def test_automated_exit_sell_replace_does_not_erase_sl_order_id_when_new_id_unextractable(env, monkeypatch):
    """Same regression, via the TP/SL/TIME exit path."""
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira', sl_order_id='12345' WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'replace_equity_order_with_market', lambda *a, **kw: (None, None))
    signals_notify.notify_sell_signal(pos, 'TIME', current_price=51.0, target_price=51.0)
    updated = signals_db.get_open_position(TICKER)
    assert updated['sl_order_id'] == 12345  # unchanged, not erased to None


def test_automated_buy_skips_placement_when_shares_size_to_zero(env, monkeypatch):
    """Regression test for the missing shares>=1 guard (2026-07-31 audit, left
    open): a too-small notional/price combo sizing to 0 shares must not reach
    the real broker call at all (which would reject it, but only after
    dinging the node circuit breaker's order_failures streak and posting a
    misleading 'blocked'-shaped alert)."""
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET starting_notional = 1 WHERE ticker = ?", (TICKER,))
        c.commit()

    def _boom(*a, **kw):
        raise AssertionError("place_trailing_buy must not be called when shares < 1")
    monkeypatch.setattr(schwab_client, 'place_trailing_buy', _boom)

    signals_notify.notify_buy_signal(_node(), _sig())
    pending = _pending()
    assert pending['order_placed'] == 0
    events = signals_db.get_coverage_events(scenario_key="automated_buy_execution")
    assert len(events) == 1
    assert events[0]['result'] == "shares_too_small"


def test_notify_sell_signal_non_time_reason_does_not_log_time_exit_event(env):
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira' WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    signals_notify.notify_sell_signal(pos, 'SL', current_price=48.0, target_price=48.0)
    events = signals_db.get_coverage_events(scenario_key="time_exit_trigger")
    assert len(events) == 0


def test_automated_sell_falls_back_when_no_matching_node(env):
    """If the position's (ticker, window) has no corresponding watch_list row
    at all (e.g. the node was later removed, mirroring EDC's 2026-07-19
    removal), the automated sell must fail closed to manual, not KeyError."""
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    pos = signals_db.get_open_position(TICKER)
    with signals_db._conn() as c:
        c.execute("DELETE FROM watch_list WHERE ticker=?", (TICKER,))
        c.commit()
    signals_notify.notify_trailing_activated(pos, current_price=52.0)
    updated = [p for p in signals_db.get_open_positions() if p['ticker'] == TICKER][0]
    assert not updated['trail_state'].get('order_placed')


# ---------------------------------------------------------------------------
# Auto-fill-detection toggle (default off)
# ---------------------------------------------------------------------------

def test_auto_fill_detection_defaults_off(env):
    assert schwab_safety.auto_fill_detection_enabled(TICKER) is False


def test_check_auto_fills_noop_when_toggle_off(env, monkeypatch):
    signals_notify.notify_buy_signal(_node(), _sig())
    assert _pending()['order_placed'] == 1

    monkeypatch.setattr(schwab_client, 'get_filled_order',
                         lambda account, ticker, side, order_id=None: {'price': 51.0, 'quantity': 100})
    signals_notify.check_auto_fills(signals_db.get_open_positions())

    # still pending -- toggle is off, so no auto-detected fill should have landed
    assert _pending()['order_placed'] == 1
    assert signals_db.get_open_position(TICKER) is None


def test_check_auto_fills_records_buy_fill_when_enabled(env, monkeypatch):
    signals_notify.notify_buy_signal(_node(), _sig())
    assert _pending()['order_placed'] == 1
    schwab_safety.enable_auto_fill_detection(TICKER)
    schwab_safety.enable_node_auto_fill_detection(_node()['id'])

    monkeypatch.setattr(schwab_client, 'get_filled_order',
                         lambda account, ticker, side, order_id=None: {'price': 51.0, 'quantity': 100})
    signals_notify.check_auto_fills(signals_db.get_open_positions())

    pos = signals_db.get_open_position(TICKER)
    assert pos is not None
    assert pos['entry_price'] == 51.0
    # 100-share fill ($5,100) is well under the node's $50k target_notional, so
    # Part 3's post-fill top-up (_reconcile_fill) buys the remaining shares --
    # entry_price stays 51.0 since the top-up fills at the same price.
    assert pos['shares'] == 980
    assert [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER] == []


def test_node_auto_fill_detection_does_not_leak_to_sibling_node_same_ticker(env):
    """The exact gap this feature was missing after the wl_id refactor: enabling
    fill-detection from one node's Slack row must not silently enable it for a
    different node sharing the same ticker (e.g. DPST/GDXU's live+research pairing)."""
    node_a_id, node_b_id = 101, 202
    schwab_safety.enable_auto_fill_detection(TICKER)
    schwab_safety.enable_node_auto_fill_detection(node_a_id)

    assert schwab_safety.node_auto_fill_detection_enabled(node_a_id) is True
    assert schwab_safety.node_auto_fill_detection_enabled(node_b_id) is False
    # ticker-level alone (the old, buggy gate) is on, proving the node-level
    # layer is what's actually protecting node B here, not a ticker-wide off-switch.
    assert schwab_safety.auto_fill_detection_enabled(TICKER) is True


def test_node_auto_fill_detection_none_id_defaults_closed(env):
    """Unresolvable node identity must not silently grant fill-detection trust
    -- opposite fail-direction from node_automation_enabled's pause gate."""
    schwab_safety.enable_auto_fill_detection(TICKER)
    assert schwab_safety.node_auto_fill_detection_enabled(None) is False


def test_check_auto_fills_records_sell_fill_when_enabled(env, monkeypatch):
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    pos = signals_db.get_open_position(TICKER)
    state = {
        'trailing': True, 'order_placed': True,
        'exit_pending': {'reason': 'TRAIL', 'current_price': 54.0, 'target_price': 54.0,
                          'reminder_channel': None, 'reminder_ts': None, 'reminder_count': 0,
                          'last_reminder_at': now.strftime('%Y-%m-%d %H:%M:%S')},
    }
    signals_db.update_position_trail_state(pos['id'], state)
    schwab_safety.enable_auto_fill_detection(TICKER)
    schwab_safety.enable_node_auto_fill_detection(pos['wl_id'])

    monkeypatch.setattr(schwab_client, 'get_filled_order',
                         lambda account, ticker, side, order_id=None: {'price': 53.5, 'quantity': 100})
    signals_notify.check_auto_fills(signals_db.get_open_positions())

    assert signals_db.get_open_positions() == []


def test_check_buy_reminders_skips_dry_run_account(env, monkeypatch):
    """A dry_run account's pending buy is resolved entirely by
    update_dry_run_buys' own synthesis -- check_buy_reminders nagging
    'Confirm Filled/Missed It/Cancelled' is meaningless noise for it (no real
    order exists to confirm). TICKER's node is on account='ira', which is
    real dry_run=True config (not monkeypatched here -- exercising the actual
    account lookup). Found live 2026-07-29 (FAZ): 14 spurious reminders over
    ~2 hours, all before a fill that resolved automatically regardless."""
    signals_notify.notify_buy_signal(_node(), _sig())
    old_time = (datetime.now() - timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')
    with signals_db._conn() as c:
        c.execute("UPDATE pending_buys SET last_reminder_at=? WHERE ticker=?", (old_time, TICKER))
        c.commit()

    posted = []
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda *a, **kw: (posted.append(a), (None, None))[1])
    signals_notify.check_buy_reminders()

    assert posted == [], f"expected no reminder posted for a dry_run pending buy, got: {posted}"
    pending = _pending()
    assert pending['reminder_count'] == 0, "reminder_count should not have incremented"


def test_check_trailing_reminders_skips_dry_run_sim_position(env, monkeypatch):
    """Mirrors test_check_buy_reminders_skips_dry_run_account for the sell
    side: a dry_run-sim position's arm event never places a real order (see
    check_dry_run_sim_sells), so order_placed can never become True -- without
    the is_dry_run_sim skip, this would nag 'confirm order placed' forever."""
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100, is_dry_run_sim=True)
    pos = signals_db.get_open_position(TICKER)
    old_time = (now - timedelta(minutes=60)).strftime('%Y-%m-%d %H:%M:%S')
    state = {'trailing': True, 'peak': 52.0, 'last_reminder_at': old_time}
    signals_db.update_position_trail_state(pos['id'], state)

    posted = []
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda *a, **kw: (posted.append(a), (None, None))[1])
    signals_notify.check_trailing_reminders(signals_db.get_open_positions())

    assert posted == [], f"expected no reminder posted for a dry_run-sim armed position, got: {posted}"
