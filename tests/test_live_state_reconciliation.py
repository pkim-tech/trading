"""Tests for signals_notify.check_live_state_reconciliation -- the detection-
only live-state reconciliation check (backlog 2026-07-21, automation_
principles.md #1/#5). Never places an order; only verifies the right Slack
alert text fires (or doesn't) for each mismatch shape."""
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
    signals_notify._RECONCILE_FETCH_FAIL_ALERTED.clear()

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                         stop_loss=5, max_hold_hours=7, state='live',
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


def test_fetch_failure_retries_then_alerts_and_skips_position(env, monkeypatch):
    """2026-08-10: a fetch failure now retries _RECONCILE_FETCH_RETRIES times
    before giving up, and alerts (rather than silently skipping) once all
    retries are exhausted -- a real broker-connectivity problem is itself
    worth knowing about, per the user's call."""
    pos = _open_pos(shares=100)

    call_count = 0

    def _raise(account, ticker):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("network error")
    monkeypatch.setattr(schwab_client, 'get_real_position', _raise)
    monkeypatch.setattr(signals_notify, '_RECONCILE_FETCH_RETRY_DELAY_SECS', 0)  # don't slow the test down
    signals_notify.check_live_state_reconciliation([pos])  # must not raise
    assert call_count == signals_notify._RECONCILE_FETCH_RETRIES
    assert len(env) == 1 and "fetch failed" in env[0]

    events = signals_db.get_coverage_events(scenario_key='reconciliation_fetch_failed')
    assert any(e['ticker'] == TICKER and e['result'] == 'failed_after_retries' for e in events)


def test_fetch_failure_alert_is_cooldown_gated_per_account(env, monkeypatch):
    """2026-08-10 fix (paired review finding, all 3 reviewers): the fetch-
    failure alert must not re-fire every poll during a sustained outage."""
    pos = _open_pos(shares=100)
    monkeypatch.setattr(schwab_client, 'get_real_position',
                         lambda account, ticker: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(signals_notify, '_RECONCILE_FETCH_RETRY_DELAY_SECS', 0)
    signals_notify.check_live_state_reconciliation([pos])
    signals_notify.check_live_state_reconciliation([pos])
    assert len(env) == 1  # second call's alert suppressed by the per-account cooldown


def test_second_position_same_down_account_skips_retry_sleep(env, monkeypatch):
    """2026-08-10 fix: once an account has exhausted retries once THIS CALL,
    a second position on the same account must not pay another full
    retry-and-sleep sequence -- the latency-stacking finding from the paired
    review (up to 6s x N positions ahead of the pinned entry/exit scans)."""
    pos1 = _open_pos(shares=100)
    signals_db.add_node('TEST_RECONCILE_TWO', 'TrailingBothZScoreBreakout', 'test', window=20,
                         take_profit=7, stop_loss=5, max_hold_hours=7, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='ira' WHERE ticker='TEST_RECONCILE_TWO'")
        c.commit()
    schwab_safety.AUTOMATION_ENABLED_TICKERS.add('TEST_RECONCILE_TWO')
    node2 = [n for n in signals_db.get_watchlist() if n['ticker'] == 'TEST_RECONCILE_TWO'][0]
    now = datetime.now()
    signals_db.open_position(node2, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira' WHERE ticker='TEST_RECONCILE_TWO'")
        c.commit()
    pos2 = signals_db.get_open_position('TEST_RECONCILE_TWO')

    calls = []

    def _raise(account, ticker):
        calls.append(ticker)
        raise RuntimeError("down")
    monkeypatch.setattr(schwab_client, 'get_real_position', _raise)
    monkeypatch.setattr(signals_notify, '_RECONCILE_FETCH_RETRY_DELAY_SECS', 0)
    signals_notify.check_live_state_reconciliation([pos1, pos2])
    # pos1 pays the full 3-attempt retry; pos2 (same account, already known
    # down this call) must skip straight to the failure path with 0 attempts.
    assert len(calls) == signals_notify._RECONCILE_FETCH_RETRIES
    events = signals_db.get_coverage_events(scenario_key='reconciliation_fetch_failed')
    tickers_logged = {e['ticker'] for e in events if e['result'] == 'failed_after_retries'}
    assert tickers_logged == {TICKER, 'TEST_RECONCILE_TWO'}


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


# ---------------------------------------------------------------------------
# Stage B/C widening (2026-08-15, SOXS incident) -- see the plan file's
# "Stage B/C" section. Stage B: the pre-existing missing_sl branch is gated on
# sl_order_id ALREADY being truthy, so it structurally could not catch the
# strictly worse "never had a stop at all" case -- the literal SOXS condition.
# Stage C: has_sell_order checked neither price nor quantity, so a stop resting
# at the wrong level (the 2026-07-31 signal_price-anchor bug) or covering the
# wrong share count (a top-up landing after placement) read as fully protected.
# ---------------------------------------------------------------------------

def _age_position_past_grace(pos_id, seconds=None):
    """Backdates entry_time so _past_sl_grace lets the never_had_sl branch
    through. Real positions age on their own; tests can't wait 5 minutes and
    freezegun isn't installed, so the tests below use the injectable now=
    parameter or this backdate, never a real sleep."""
    secs = seconds if seconds is not None else signals_notify._RECONCILE_MISSING_SL_GRACE_SECS + 60
    stale = (datetime.now() - timedelta(seconds=secs)).strftime('%Y-%m-%d %H:%M:%S')
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET entry_time=? WHERE id=?", (stale, pos_id))
        c.commit()


def _set_fixed_sl(pct):
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET fixed_sl=?, stop_loss=? WHERE ticker=?",
                   (pct, pct, TICKER))
        c.commit()


def _resting_stop(order_id=12345, stop_price=49.50, quantity=100.0, order_type='STOP'):
    """Mirrors tests/fake_broker.py::_make_order's real shape: quantity lives on
    the LEG, and there is deliberately NO top-level 'quantity' key. An earlier
    version of this helper set quantity at BOTH levels -- a shape neither
    fake_broker nor schwab_client.get_real_orders ever produces -- which made
    the Stage C quantity tests pass against a field the real code path would
    never have found. Caught by the cold reviewer, 2026-08-15."""
    return [{
        'orderId': order_id,
        'stopPrice': stop_price,
        'orderType': order_type,
        'orderLegCollection': [{'instruction': 'SELL', 'quantity': quantity,
                                 'instrument': {'symbol': TICKER}}],
    }]


def test_stage_b_alerts_when_position_never_had_a_stop_loss_at_all(env, monkeypatch):
    pos = _open_pos(shares=100)
    _age_position_past_grace(pos['id'])
    pos = signals_db.get_open_position(TICKER)
    assert pos['sl_order_id'] is None, "test premise: no SL was ever recorded"
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])

    signals_notify.check_live_state_reconciliation([pos])

    assert len(env) == 1, env
    assert 'UNPROTECTED' in env[0], env[0]
    events = signals_db.get_coverage_events(scenario_key='reconciliation_mismatch')
    assert [e['result'] for e in events] == ['never_had_sl'], events


def test_stage_b_stays_quiet_inside_the_placement_grace_window(env, monkeypatch):
    """A stop placement genuinely in flight must not false-alarm --
    _place_stop_loss_for_position's own retry/confirm envelope is ~16s and the
    grace window is 5 minutes, so a freshly-opened position is silent."""
    pos = _open_pos(shares=100)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify.check_live_state_reconciliation([pos])
    assert env == []


def test_stage_b_grace_window_boundary_respects_injected_now(env, monkeypatch):
    """The grace check takes now= rather than reading the clock, so both sides
    of the boundary are testable without freezegun (which isn't installed) or a
    real sleep."""
    pos = _open_pos(shares=100)
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])

    just_inside = datetime.now() + timedelta(
        seconds=signals_notify._RECONCILE_MISSING_SL_GRACE_SECS - 30)
    signals_notify.check_live_state_reconciliation([pos], now=just_inside)
    assert env == [], "still inside the grace window -- must stay silent"

    just_outside = datetime.now() + timedelta(
        seconds=signals_notify._RECONCILE_MISSING_SL_GRACE_SECS + 30)
    signals_notify.check_live_state_reconciliation([pos], now=just_outside)
    assert len(env) == 1 and 'UNPROTECTED' in env[0], env


def test_stage_b_does_not_fire_when_a_sell_order_is_actually_resting(env, monkeypatch):
    """No local sl_order_id but a real SELL IS resting (e.g. a stop placed by a
    human, or one whose id we lost) -- the position is protected in fact, so
    this must not shout UNPROTECTED. The provenance question that raises is
    bug #5's job, not this branch's."""
    pos = _open_pos(shares=100)
    _age_position_past_grace(pos['id'])
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: _resting_stop())
    signals_notify.check_live_state_reconciliation([pos])
    assert not any('UNPROTECTED' in p for p in env), env


def test_stage_b_does_not_fire_for_an_armed_position(env, monkeypatch):
    """Post-arm the protective order is a trailing-sell, not a stop -- the
    armed branches above own that case and must keep owning it."""
    pos = _open_pos(shares=100)
    _age_position_past_grace(pos['id'])
    signals_db.update_position_trail_state(pos['id'], {'trailing': True, 'order_placed': True,
                                                        'last_reminder_at': '2026-08-15 10:00:00'})
    pos = [p for p in signals_db.get_open_positions() if p['ticker'] == TICKER][0]
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: _resting_stop())
    signals_notify.check_live_state_reconciliation([pos])
    assert not any('never_had_sl' in p or 'UNPROTECTED' in p for p in env), env


def test_stage_c_alerts_when_resting_stop_is_at_the_wrong_price(env, monkeypatch):
    pos = _open_pos(shares=100)
    _set_fixed_sl(1.0)  # entry 50.00 -> algo's stop is 49.50
    signals_db.set_sl_order_id(TICKER, 12345)
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    # 42.50 is where a signal_price-anchored (2026-07-31 bug) or hand-typed
    # stop could plausibly land -- whole percent away from the algo's own.
    monkeypatch.setattr(schwab_safety, '_open_orders',
                         lambda account: _resting_stop(stop_price=42.50, quantity=100.0))

    signals_notify.check_live_state_reconciliation([pos])

    assert len(env) == 1, env
    assert '42.50' in env[0] and '49.50' in env[0], env[0]
    events = signals_db.get_coverage_events(scenario_key='reconciliation_mismatch')
    assert [e['result'] for e in events] == ['sl_price_mismatch'], events


def test_stage_c_alerts_when_resting_stop_covers_the_wrong_quantity(env, monkeypatch):
    pos = _open_pos(shares=100)
    _set_fixed_sl(1.0)
    signals_db.set_sl_order_id(TICKER, 12345)
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders',
                         lambda account: _resting_stop(stop_price=49.50, quantity=60.0))

    signals_notify.check_live_state_reconciliation([pos])

    assert len(env) == 1, env
    assert 'partially unprotected' in env[0], env[0]
    assert '60' in env[0] and '100' in env[0], env[0]
    events = signals_db.get_coverage_events(scenario_key='reconciliation_mismatch')
    assert [e['result'] for e in events] == ['sl_quantity_mismatch'], events


def test_stage_c_flags_an_oversized_stop_as_oversell_risk_not_just_a_mismatch(env, monkeypatch):
    """Direction matters: a stop for MORE shares than are held would sell into
    a short, a materially different (and worse) failure than under-coverage."""
    pos = _open_pos(shares=100)
    _set_fixed_sl(1.0)
    signals_db.set_sl_order_id(TICKER, 12345)
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders',
                         lambda account: _resting_stop(stop_price=49.50, quantity=140.0))
    signals_notify.check_live_state_reconciliation([pos])
    assert len(env) == 1 and 'OVERSELL RISK' in env[0], env


def test_stage_c_reports_price_and_quantity_as_two_separate_findings(env, monkeypatch):
    """Both wrong at once must produce both alerts -- collapsing them into one
    would hide whichever the operator didn't happen to read."""
    pos = _open_pos(shares=100)
    _set_fixed_sl(1.0)
    signals_db.set_sl_order_id(TICKER, 12345)
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders',
                         lambda account: _resting_stop(stop_price=42.50, quantity=60.0))

    signals_notify.check_live_state_reconciliation([pos])

    results = sorted(e['result'] for e in
                      signals_db.get_coverage_events(scenario_key='reconciliation_mismatch'))
    assert results == ['sl_price_mismatch', 'sl_quantity_mismatch'], results
    assert len(env) == 2, env


def test_stage_c_silent_when_price_and_quantity_both_match(env, monkeypatch):
    pos = _open_pos(shares=100)
    _set_fixed_sl(1.0)
    signals_db.set_sl_order_id(TICKER, 12345)
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders',
                         lambda account: _resting_stop(stop_price=49.50, quantity=100.0))
    signals_notify.check_live_state_reconciliation([pos])
    assert env == []
    assert signals_db.get_coverage_events(scenario_key='reconciliation_mismatch') == []


def test_stage_c_tolerates_sub_penny_broker_rounding(env, monkeypatch):
    """The broker rounds the price we submit to its own tick; that must not
    read as a real mismatch every single poll."""
    pos = _open_pos(shares=100)
    _set_fixed_sl(1.0)
    signals_db.set_sl_order_id(TICKER, 12345)
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders',
                         lambda account: _resting_stop(stop_price=49.502, quantity=100.0))
    signals_notify.check_live_state_reconciliation([pos])
    assert env == []


def test_stage_c_never_replaces_the_order_it_flags(env, monkeypatch):
    """Detection-only, explicitly: auto-replacing here would reintroduce the
    exact silent-override behavior of bug #4 that this session fixes elsewhere.
    Any real broker call from this path is a regression."""
    pos = _open_pos(shares=100)
    _set_fixed_sl(1.0)
    signals_db.set_sl_order_id(TICKER, 12345)
    pos = signals_db.get_open_position(TICKER)

    def _explode(*a, **kw):
        raise AssertionError("check_live_state_reconciliation must never place/replace an order")

    for fn in ('place_stop_loss', 'replace_order_with_stop_loss', 'place_equity_sell',
                'place_trailing_sell'):
        if hasattr(schwab_client, fn):
            monkeypatch.setattr(schwab_client, fn, _explode)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders',
                         lambda account: _resting_stop(stop_price=42.50, quantity=60.0))

    signals_notify.check_live_state_reconciliation([pos])  # must not raise
    assert len(env) == 2, env


# ---------------------------------------------------------------------------
# Regressions for the 2026-08-15 paired-review findings on Stage B/C itself.
# All four below are bugs the first version of Stage B/C actually shipped with,
# found by two independent Opus reviewers and confirmed against the code.
# ---------------------------------------------------------------------------

def test_quantity_is_read_from_the_leg_not_the_order(env, monkeypatch):
    """The order dicts here come straight from schwab_safety._open_orders --
    raw, unnormalized broker JSON. Every other quantity reader in this codebase
    takes it off the leg (schwab_client.get_real_orders, schwab_safety x2), and
    fake_broker._make_order emits NO top-level quantity at all. An order-level
    read made the whole Stage C quantity check a silent no-op against the
    project's own designated regression fixture."""
    order = _resting_stop(quantity=60.0)[0]
    assert 'quantity' not in order, "fixture must match fake_broker's real shape"
    assert signals_notify._resting_order_quantity(order) == 60.0


def test_quantity_falls_back_to_order_level_when_no_leg_carries_it(env):
    """Real Schwab responses do carry a top-level quantity -- keep working
    against them, but as the fallback, so the fixture exercises the primary."""
    assert signals_notify._resting_order_quantity(
        {'quantity': 42.0, 'orderLegCollection': []}) == 42.0


def test_no_false_quantity_mismatch_while_an_addon_leg_is_open(env, monkeypatch):
    """THE bug both reviewers independently caught. expected_shares is
    deliberately widened to core+leg for the broker share-count compare, but
    the core stop only ever covers the core leg (the add-on carries its own
    separate stop). Comparing the core stop against the widened number is
    structurally guaranteed to mismatch, firing a false 'partially unprotected'
    every poll and tripping the node circuit breaker within ~3 polls."""
    pos = _open_pos(shares=100)
    _set_fixed_sl(1.0)
    signals_db.set_sl_order_id(TICKER, 12345)
    pos = signals_db.get_open_position(TICKER)

    # A real open add-on leg of 40 shares: broker legitimately holds 140.
    monkeypatch.setattr(signals_db, 'get_open_addon_leg_by_parent',
                         lambda position_id: {'shares': 40.0})
    monkeypatch.setattr(signals_notify.db, 'get_open_addon_leg_by_parent',
                         lambda position_id: {'shares': 40.0})
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 140.0)
    # The core stop correctly covers the core 100 shares only.
    monkeypatch.setattr(schwab_safety, '_open_orders',
                         lambda account: _resting_stop(stop_price=49.50, quantity=100.0))

    signals_notify.check_live_state_reconciliation([pos])

    assert env == [], f"correct state must produce no alert at all, got: {env}"
    assert signals_db.get_coverage_events(scenario_key='reconciliation_mismatch') == []


def test_quantity_mismatch_still_fires_against_core_shares_with_a_leg_open(env, monkeypatch):
    """The corrected comparison must not go blind -- a genuinely undersized
    core stop is still caught while a leg is open."""
    pos = _open_pos(shares=100)
    _set_fixed_sl(1.0)
    signals_db.set_sl_order_id(TICKER, 12345)
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(signals_notify.db, 'get_open_addon_leg_by_parent',
                         lambda position_id: {'shares': 40.0})
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 140.0)
    monkeypatch.setattr(schwab_safety, '_open_orders',
                         lambda account: _resting_stop(stop_price=49.50, quantity=60.0))

    signals_notify.check_live_state_reconciliation([pos])

    results = [e['result'] for e in
                signals_db.get_coverage_events(scenario_key='reconciliation_mismatch')]
    assert results == ['sl_quantity_mismatch'], results
    assert '60' in env[0] and '100' in env[0], env[0]


def test_never_had_sl_alert_survives_an_unknown_expected_price(env, monkeypatch):
    """_expected_sl_price returns None when the position has no usable SL
    config -- which is EXACTLY the state that reaches this branch, since
    _place_stop_loss_for_position returns early on a falsy sl_pct. The first
    version formatted it directly, raising TypeError inside the position loop:
    _guarded catches only at whole-function granularity, so every later
    position silently lost its check and the loudest alert never posted."""
    pos = _open_pos(shares=100)
    _age_position_past_grace(pos['id'])
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET fixed_sl=NULL, stop_loss=0 WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    assert signals_notify._expected_sl_price(pos) is None, "test premise"
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])

    signals_notify.check_live_state_reconciliation([pos])  # must not raise

    assert len(env) == 1 and 'UNPROTECTED' in env[0], env
    assert 'None' not in env[0], "must omit the price, not print a fabricated/None one"


def test_a_later_position_still_gets_checked_when_an_earlier_one_has_no_sl_config(env, monkeypatch):
    """The real blast radius of the TypeError above: it escaped the per-position
    loop, so every position after the bad one lost its reconciliation check for
    that cycle."""
    pos_a = _open_pos(shares=100)
    _age_position_past_grace(pos_a['id'])
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET fixed_sl=NULL, stop_loss=0 WHERE id=?", (pos_a['id'],))
        c.commit()
    pos_a = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 80.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])

    signals_notify.check_live_state_reconciliation([pos_a, pos_a])

    kinds = {e['result'] for e in
              signals_db.get_coverage_events(scenario_key='reconciliation_mismatch')}
    assert 'shares' in kinds, f"the share-count check must still run, got {kinds}"


def test_fallback_ignores_a_non_stop_limit_sell(env, monkeypatch):
    """A manual limit SELL -- e.g. the accidental limit-sell that actually
    closed the position in the SOXS incident, or a deliberate skim -- must not
    be adopted as 'the stop'. It carries no stopPrice, so the price check would
    silently skip while the quantity check fired against the wrong order."""
    pos = _open_pos(shares=100)
    _set_fixed_sl(1.0)
    signals_db.set_sl_order_id(TICKER, 99999)  # points at an order that is gone
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    limit_sell = _resting_stop(order_id=555, stop_price=None, quantity=30.0, order_type='LIMIT')
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: limit_sell)

    signals_notify.check_live_state_reconciliation([pos])

    results = [e['result'] for e in
                signals_db.get_coverage_events(scenario_key='reconciliation_mismatch')]
    assert 'sl_quantity_mismatch' not in results, (
        f"a limit SELL is not a stop and must not drive the stop checks: {env}")


def test_fallback_still_adopts_a_replaced_stop_under_a_new_id(env, monkeypatch):
    """The fallback's real purpose: a stop replaced at the broker keeps
    protecting the position under a NEW order id, and refusing to compare would
    blind both checks in exactly the case where a replace went wrong."""
    pos = _open_pos(shares=100)
    _set_fixed_sl(1.0)
    signals_db.set_sl_order_id(TICKER, 99999)
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda account, ticker: 100.0)
    monkeypatch.setattr(schwab_safety, '_open_orders',
                         lambda account: _resting_stop(order_id=777, stop_price=42.50, quantity=100.0))

    signals_notify.check_live_state_reconciliation([pos])

    results = [e['result'] for e in
                signals_db.get_coverage_events(scenario_key='reconciliation_mismatch')]
    assert results == ['sl_price_mismatch'], results
