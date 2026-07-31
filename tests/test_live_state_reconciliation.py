"""Tests for signals_notify.check_live_state_reconciliation -- the detection-
only live-state reconciliation check (backlog 2026-07-21, automation_
principles.md #1/#5). Never places an order; only verifies the right Slack
alert text fires (or doesn't) for each mismatch shape."""
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

TICKER = 'TEST_RECONCILE'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, 'NODE_BREAKER_PATH', tmp_path / "schwab_node_breaker_state.json")
    posted = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: posted.append(a[0] if a else kw.get('text')))
    signals_notify._RECONCILE_ALERTED.clear()

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                         stop_loss=5, max_hold_hours=7, mode='live',
                         trail_buy_pct=1.0, trail_pct=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account = 'ira' WHERE ticker = ?", (TICKER,))
        c.commit()

    yield posted

    tmp_db_path = Path(tmp_db.name)
    if tmp_db_path.exists():
        tmp_db_path.unlink()


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _open_pos(shares=100):
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=shares)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira' WHERE ticker=?", (TICKER,))
        c.commit()
    return signals_db.get_open_position(TICKER)


def test_no_alert_when_state_matches(env, monkeypatch):
    pos = _open_pos(shares=100)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify.check_live_state_reconciliation([pos])
    assert env == []


def test_alerts_on_share_count_mismatch(env, monkeypatch):
    pos = _open_pos(shares=100)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 80.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify.check_live_state_reconciliation([pos])
    assert len(env) == 1
    assert 'share-count' in env[0] or 'mismatch' in env[0]
    assert '80' in env[0] and '100' in env[0]


def test_share_count_mismatch_alert_rate_limited(env, monkeypatch):
    pos = _open_pos(shares=100)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 80.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify.check_live_state_reconciliation([pos])
    signals_notify.check_live_state_reconciliation([pos])
    assert len(env) == 1  # second call suppressed by the 15-min cooldown


def test_alerts_on_missing_sl_order(env, monkeypatch):
    pos = _open_pos(shares=100)
    signals_db.set_sl_order_id(TICKER, 12345)
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])  # no resting SELL order
    signals_notify.check_live_state_reconciliation([pos])
    assert len(env) == 1
    assert 'SL' in env[0] or 'stop' in env[0].lower()


def test_no_alert_when_sl_order_actually_resting(env, monkeypatch):
    pos = _open_pos(shares=100)
    signals_db.set_sl_order_id(TICKER, 12345)
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    resting_sell_order = [{'orderLegCollection': [
        {'instruction': 'SELL', 'instrument': {'symbol': TICKER}}
    ]}]
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: resting_sell_order)
    signals_notify.check_live_state_reconciliation([pos])
    assert env == []


def test_alerts_on_missing_trailing_sell_order(env, monkeypatch):
    pos = _open_pos(shares=100)
    signals_db.update_position_trail_state(pos['id'], {'trailing': True, 'order_placed': True})
    # trail_state is JSON-parsed by get_open_positions() (the list form run_loop actually
    # passes in), unlike get_open_position()'s raw string -- match the real call site.
    pos = [p for p in signals_db.get_open_positions() if p['ticker'] == TICKER][0]
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify.check_live_state_reconciliation([pos])
    assert len(env) == 1
    assert 'trailing-sell' in env[0]


def test_alerts_when_armed_but_order_placed_never_confirmed_even_with_sell_order_resting(env, monkeypatch):
    """Regression test for the reconciliation blind spot found via the
    arming-logic walkthrough, 2026-07-31: check_sell_condition persists
    trailing=True independently of whether notify_trailing_activated ever
    actually ran (a daemon crash/restart in that window leaves this state
    permanently -- just_activated_trailing can never re-fire once
    trailing=True is already on file). The old check required
    order_placed=True to fire the missing_trailing_sell branch at all, so
    trailing=True + order_placed unset fell through both mismatch branches
    silently, forever. Confirmed here even with a resting SELL order present
    (the old original SL, still fully intact since the crash happened before
    any atomic replace was even attempted) -- has_sell_order alone can't
    distinguish "still protected by the old SL" from "genuinely
    unprotected", so this must alert regardless, since the stuck state
    itself (not just protection) is the actionable problem."""
    pos = _open_pos(shares=100)
    signals_db.set_sl_order_id(TICKER, 12345)  # the original SL, still resting
    signals_db.update_position_trail_state(pos['id'], {'trailing': True})  # order_placed never set
    pos = [p for p in signals_db.get_open_positions() if p['ticker'] == TICKER][0]
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [{
        'orderLegCollection': [{'instruction': 'SELL', 'instrument': {'symbol': TICKER}}],
    }])  # the old SL genuinely still resting
    signals_notify.check_live_state_reconciliation([pos])
    assert len(env) == 1
    assert 'never confirmed' in env[0] or 'armed' in env[0].lower()


def test_no_false_positive_for_normal_awaiting_manual_confirmation(env, monkeypatch):
    """The armed_order_never_confirmed check above must NOT fire for the
    normal, expected case: a non-live-mode/paused node where
    _attempt_automated_sell legitimately declined and notify_trailing_activated
    posted the manual arm alert instead of auto-placing -- that path DOES set
    last_reminder_at unconditionally (regardless of auto_placed), so its
    presence is what distinguishes this from the real crash-window gap.
    Confirmed reachable false-positive without the last_reminder_at gate
    (Opus review, 2026-07-31): every poll would otherwise re-flag this
    completely normal state and feed a streak hit toward the node circuit
    breaker."""
    pos = _open_pos(shares=100)
    signals_db.update_position_trail_state(pos['id'], {
        'trailing': True, 'reminder_channel': 'C1', 'reminder_ts': '1.0',
        'reminder_count': 0, 'last_reminder_at': '2026-07-31 10:00:00',
    })  # order_placed still unset -- awaiting a human tap, not a crash
    pos = [p for p in signals_db.get_open_positions() if p['ticker'] == TICKER][0]
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify.check_live_state_reconciliation([pos])
    assert env == [], f"expected no alert for the normal awaiting-manual-confirmation state, got: {env}"


def test_no_alert_outside_automation_scope(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', set())
    pos = _open_pos(shares=100)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 80.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify.check_live_state_reconciliation([pos])
    assert env == []


def test_fetch_failure_skips_position_without_raising(env, monkeypatch):
    pos = _open_pos(shares=100)

    def _raise(account, ticker):
        raise RuntimeError("network error")
    monkeypatch.setattr(schwab_client, 'get_real_position', _raise)
    signals_notify.check_live_state_reconciliation([pos])  # must not raise
    assert env == []


def test_snoozed_ticker_suppresses_both_alert_and_coverage_event(env, monkeypatch):
    pos = _open_pos(shares=100)
    signals_db.snooze_coverage('reconciliation_mismatch', '2099-01-01 00:00:00',
                                'known accepted test position', ticker=TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 80.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify.check_live_state_reconciliation([pos])
    assert env == []
    assert signals_db.get_coverage_events(scenario_key='reconciliation_mismatch') == []


def test_snoozed_different_ticker_does_not_suppress(env, monkeypatch):
    pos = _open_pos(shares=100)
    signals_db.snooze_coverage('reconciliation_mismatch', '2099-01-01 00:00:00',
                                'unrelated ticker', ticker='SOME_OTHER_TICKER')
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 80.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify.check_live_state_reconciliation([pos])
    assert len(env) == 1
    assert len(signals_db.get_coverage_events(scenario_key='reconciliation_mismatch')) == 1


def test_expired_snooze_does_not_suppress(env, monkeypatch):
    pos = _open_pos(shares=100)
    signals_db.snooze_coverage('reconciliation_mismatch', '2000-01-01 00:00:00',
                                'already expired', ticker=TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 80.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify.check_live_state_reconciliation([pos])
    assert len(env) == 1


def test_snooze_scoped_to_one_kind_does_not_suppress_missing_sl(env, monkeypatch):
    """A snooze scoped to kind='shares' (the documented UDOW use case) must
    not also silence missing_sl -- a real position may be unprotected at the
    broker, a materially more severe alert than a known share-count drift
    (found by session-wrap Opus review, 2026-07-28)."""
    pos = _open_pos(shares=100)
    signals_db.set_sl_order_id(TICKER, 12345)
    pos = signals_db.get_open_position(TICKER)
    signals_db.snooze_coverage('reconciliation_mismatch', '2099-01-01 00:00:00',
                                'known share drift only', ticker=TICKER, kind='shares')
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])  # no resting SELL order
    signals_notify.check_live_state_reconciliation([pos])
    assert len(env) == 1
    assert 'SL' in env[0] or 'stop' in env[0].lower()


def test_no_false_mismatch_for_a_position_already_closed_this_cycle(env, monkeypatch):
    """Real 2026-07-28 bug: open_positions is a snapshot taken once at the top
    of active_signals.py's poll loop. If an earlier step in the *same* cycle
    (_check_position_exit) closes a position before check_live_state_
    reconciliation runs, this function used to compare the broker's correct
    post-close state against the caller's stale in-memory row, producing a
    false mismatch. Found live: GDXU's real TRAIL close produced a false
    "shares"/"missing_trailing_sell" alert 6 seconds after a correct exit."""
    pos = _open_pos(shares=100)
    signals_db.close_position(pos['id'], exit_signal_price=55.0, exit_price=55.0,
                               exit_time=datetime.now(), exit_reason='TRAIL')
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 0.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify.check_live_state_reconciliation([pos])  # still the stale, now-closed row
    assert env == []


def test_reconciliation_mismatch_streak_trips_breaker_after_threshold(env, monkeypatch):
    # Node-level circuit breaker (monitor-only, docs/backlog_cache.md's
    # "node-level auto-pause circuit breaker" item): 3 consecutive
    # reconciliation-mismatch polls for the same node should log a
    # node_circuit_breaker_tripped event, without pausing anything.
    pos = _open_pos(shares=100)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 80.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    for _ in range(3):
        signals_notify._RECONCILE_ALERTED.clear()  # bypass the 15-min alert cooldown between polls
        signals_notify.check_live_state_reconciliation([pos])
    trips = signals_db.get_coverage_events(scenario_key='node_circuit_breaker_tripped')
    assert len(trips) == 1
    assert trips[0]['ticker'] == TICKER
    assert 'reconciliation_mismatches' in trips[0]['detail']


def test_reconciliation_mismatch_streak_resets_on_a_clean_poll(env, monkeypatch):
    pos = _open_pos(shares=100)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 80.0)
    signals_notify._RECONCILE_ALERTED.clear()
    signals_notify.check_live_state_reconciliation([pos])
    signals_notify._RECONCILE_ALERTED.clear()
    signals_notify.check_live_state_reconciliation([pos])
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)  # clean poll
    signals_notify.check_live_state_reconciliation([pos])
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 80.0)
    signals_notify._RECONCILE_ALERTED.clear()
    signals_notify.check_live_state_reconciliation([pos])
    signals_notify._RECONCILE_ALERTED.clear()
    signals_notify.check_live_state_reconciliation([pos])
    assert signals_db.get_coverage_events(scenario_key='node_circuit_breaker_tripped') == []


def test_a_position_with_unknown_shares_does_not_reset_an_in_progress_streak(env, monkeypatch):
    # Opus review, 2026-07-30: check_live_state_reconciliation's expected_shares
    # is None branch used to unconditionally call record_node_streak(hit=False)
    # -- nothing was actually checked for that poll (the shares compare above
    # is gated on expected_shares is not None, and the protective-order checks
    # below never run either), so this was recording a fabricated "clean poll"
    # that silently wiped a genuine in-progress mismatch streak. Fixed to just
    # `continue` without recording anything for that poll.
    pos = _open_pos(shares=100)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 80.0)
    for _ in range(2):
        signals_notify._RECONCILE_ALERTED.clear()
        signals_notify.check_live_state_reconciliation([pos])

    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET shares = NULL WHERE id = ?", (pos['id'],))
        c.commit()
    unknown_shares_pos = signals_db.get_open_position(TICKER)
    signals_notify.check_live_state_reconciliation([unknown_shares_pos])
    assert signals_db.get_coverage_events(scenario_key='node_circuit_breaker_tripped') == []

    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET shares = 100 WHERE id = ?", (pos['id'],))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    signals_notify._RECONCILE_ALERTED.clear()
    signals_notify.check_live_state_reconciliation([pos])  # the 3rd real mismatch -- should trip
    trips = signals_db.get_coverage_events(scenario_key='node_circuit_breaker_tripped')
    assert len(trips) == 1


def test_snoozed_mismatch_does_not_feed_the_breaker_streak(env, monkeypatch):
    pos = _open_pos(shares=100)
    signals_db.snooze_coverage('reconciliation_mismatch', '2099-01-01 00:00:00',
                                'known accepted test position', ticker=TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 80.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    for _ in range(3):
        signals_notify.check_live_state_reconciliation([pos])
    assert signals_db.get_coverage_events(scenario_key='node_circuit_breaker_tripped') == []


def test_reconciliation_mismatch_event_records_the_real_numbers(env, monkeypatch):
    # Found 2026-07-28: every reconciliation_mismatch coverage_event's detail
    # field was empty -- confirming a suspected day's worth of GDXU/SPY
    # mismatches (1899 events, all detail='') was attributable to a known,
    # already-fixed incident relied on timing correlation alone, not the
    # actual numbers. detail now carries the same text the Slack alert gets,
    # so a future investigation doesn't have to guess.
    pos = _open_pos(shares=100)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 80.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify.check_live_state_reconciliation([pos])
    events = signals_db.get_coverage_events(scenario_key="reconciliation_mismatch")
    assert len(events) == 1
    assert '80' in events[0]['detail'] and '100' in events[0]['detail']
