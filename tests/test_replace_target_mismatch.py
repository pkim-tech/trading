"""Bug #4 + provenance (2026-08-15): the automated exit/arm order-replace had
no mismatch check before acting.

_attempt_automated_sell and _attempt_automated_exit_sell both take whatever is
in pos['sl_order_id'] (or trail_state.exit_order_id) and atomically replace it
the moment their own exit condition fires -- with no verification that the
order actually resting at the broker is the one the algo believes it is. That
is exactly the case automation_principles.md #5 exists for, never applied to
this mechanism. It bit for real on 2026-08-14: a human placed a manual
(mispriced) stop and the daemon's own SL logic later replaced it, with nothing
anywhere recording that the order had been deliberate.

The check is DETECTION-ONLY AND NON-BLOCKING by design. These tests pin that
property hardest, because getting it wrong is worse than the original bug: a
blocking version would strand a position that needs to exit.
"""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import signals_notify
import schwab_safety

TICKER = 'TEST_REPLACECHK'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    posted = []
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda *a, **kw: posted.append(a[0] if a else kw.get('text')))
    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='ira' WHERE ticker=?", (TICKER,))
        c.commit()
    yield posted
    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _pos(shares=100):
    from datetime import datetime
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=shares)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira' WHERE ticker=?", (TICKER,))
        c.commit()
    return signals_db.get_open_position(TICKER)


def _resting(order_id=4242, quantity=100.0):
    return [{
        'orderId': order_id,
        'orderType': 'STOP',
        'stopPrice': 49.50,
        'orderLegCollection': [{'instruction': 'SELL', 'quantity': quantity,
                                 'instrument': {'symbol': TICKER}}],
    }]


def test_matching_resting_order_produces_no_noise(env, monkeypatch):
    pos = _pos(shares=100)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: _resting(quantity=100.0))
    signals_notify._verify_resting_before_replace(pos, _node(), 'ira', TICKER, 4242, 'stop-loss')
    assert env == []
    assert signals_db.get_coverage_events(scenario_key='replace_target_mismatch') == []


def test_alerts_when_the_order_being_replaced_is_not_resting_at_all(env, monkeypatch):
    pos = _pos(shares=100)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    signals_notify._verify_resting_before_replace(pos, _node(), 'ira', TICKER, 4242, 'stop-loss')
    assert len(env) == 1 and 'NOT resting' in env[0], env
    events = signals_db.get_coverage_events(scenario_key='replace_target_mismatch')
    assert [e['result'] for e in events] == ['resting_order_not_found'], events


def test_alerts_when_the_resting_order_covers_a_different_share_count(env, monkeypatch):
    pos = _pos(shares=100)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: _resting(quantity=60.0))
    signals_notify._verify_resting_before_replace(pos, _node(), 'ira', TICKER, 4242, 'stop-loss')
    assert len(env) == 1 and '60' in env[0] and '100' in env[0], env
    events = signals_db.get_coverage_events(scenario_key='replace_target_mismatch')
    assert [e['result'] for e in events] == ['quantity_mismatch'], events


def test_replacing_a_manually_placed_order_is_announced_distinctly(env, monkeypatch):
    """The 2026-08-14 case exactly: the daemon replaces a stop a person placed
    by hand. The replace is correct -- but it must never be silent."""
    pos = _pos(shares=100)
    signals_db.set_position_provenance(pos['id'], 'manual')
    pos = signals_db.get_open_position(TICKER)
    assert pos['provenance'] == 'manual'
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: _resting(quantity=100.0))

    signals_notify._verify_resting_before_replace(pos, _node(), 'ira', TICKER, 4242, 'stop-loss')

    assert len(env) == 1 and 'MANUALLY-placed' in env[0], env
    events = signals_db.get_coverage_events(scenario_key='replace_target_mismatch')
    assert [e['result'] for e in events] == ['manual_order_replaced'], events


def test_a_daemon_placed_order_is_not_announced_as_manual(env, monkeypatch):
    pos = _pos(shares=100)
    assert pos['provenance'] == 'daemon', "default must be daemon"
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: _resting(quantity=100.0))
    signals_notify._verify_resting_before_replace(pos, _node(), 'ira', TICKER, 4242, 'stop-loss')
    assert not any('MANUALLY-placed' in p for p in env), env


def test_quantity_and_manual_are_reported_as_two_separate_findings(env, monkeypatch):
    pos = _pos(shares=100)
    signals_db.set_position_provenance(pos['id'], 'manual')
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: _resting(quantity=60.0))
    signals_notify._verify_resting_before_replace(pos, _node(), 'ira', TICKER, 4242, 'stop-loss')
    results = sorted(e['result'] for e in
                      signals_db.get_coverage_events(scenario_key='replace_target_mismatch'))
    assert results == ['manual_order_replaced', 'quantity_mismatch'], results
    assert len(env) == 2, env


def test_check_never_places_cancels_or_replaces_an_order(env, monkeypatch):
    """Detection-only. Any real broker mutation from this path is a regression."""
    import schwab_client
    pos = _pos(shares=100)

    def _explode(*a, **kw):
        raise AssertionError("the pre-replace check must never mutate broker state")

    for fn in ('place_stop_loss', 'replace_order_with_stop_loss', 'place_equity_sell',
                'place_trailing_sell', 'replace_order_with_trailing_sell',
                'replace_equity_order_with_market', 'cancel_order'):
        if hasattr(schwab_client, fn):
            monkeypatch.setattr(schwab_client, fn, _explode)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: _resting(quantity=60.0))
    signals_notify._verify_resting_before_replace(pos, _node(), 'ira', TICKER, 4242, 'stop-loss')
    assert len(env) == 1


def test_a_broker_fetch_failure_cannot_block_the_exit(env, monkeypatch):
    """THE property that matters most. This check is advisory -- if it raises,
    the caller must still place the exit order. A blocking version would strand
    a position that needs to exit, which is worse than the bug being fixed."""
    pos = _pos(shares=100)

    def _boom(account):
        raise RuntimeError("broker outage")

    monkeypatch.setattr(schwab_safety, '_open_orders', _boom)
    with pytest.raises(RuntimeError):
        signals_notify._verify_resting_before_replace(pos, _node(), 'ira', TICKER, 4242, 'stop-loss')
    # ...and both real call sites wrap it, so the exit still proceeds. Pinned
    # by asserting the wrapper exists rather than re-driving the whole exit.
    import inspect
    for fn in (signals_notify._attempt_automated_sell, signals_notify._attempt_automated_exit_sell):
        src = inspect.getsource(fn)
        assert '_verify_resting_before_replace' in src, f"{fn.__name__} must call the check"
        idx = src.index('_verify_resting_before_replace')
        assert 'try:' in src[:idx], f"{fn.__name__} must wrap the check in try/except"


# ---------------------------------------------------------------------------
# Rebuttal-round fixes (2026-08-15). Every test below pins a defect the paired
# review found in the FIRST version of _verify_resting_before_replace.
# ---------------------------------------------------------------------------

def test_manual_provenance_is_announced_even_when_the_recorded_id_is_gone(env, monkeypatch):
    """The unreachability bug both reviewers caught. A human who hand-places a
    replacement stop mints a NEW broker id our DB never captured, so the
    `match is None` early return skipped the provenance check entirely -- the
    daemon replaced the human's order silently, which is bug #5's exact shape."""
    pos = _pos(shares=100)
    signals_db.set_position_provenance(pos['id'], 'manual')
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])

    signals_notify._verify_resting_before_replace(pos, _node(), 'ira', TICKER, 4242, 'stop-loss')

    results = sorted(e['result'] for e in
                      signals_db.get_coverage_events(scenario_key='replace_target_mismatch'))
    assert 'manual_order_replaced' in results, results
    assert any('MANUALLY-placed' in p for p in env), env


def test_provenance_alert_survives_a_broker_fetch_failure(env, monkeypatch):
    """Provenance is a property of the position, not the order, so it must not
    depend on a broker call that the caller deliberately swallows."""
    pos = _pos(shares=100)
    signals_db.set_position_provenance(pos['id'], 'manual')
    pos = signals_db.get_open_position(TICKER)

    def _boom(account):
        raise RuntimeError("broker outage")

    monkeypatch.setattr(schwab_safety, '_open_orders', _boom)
    with pytest.raises(RuntimeError):
        signals_notify._verify_resting_before_replace(pos, _node(), 'ira', TICKER, 4242, 'stop-loss')
    assert any('MANUALLY-placed' in p for p in env), (
        "the provenance alert must fire before the broker fetch, not after")


def test_mispriced_stop_is_caught(env, monkeypatch):
    """The LITERAL 2026-08-14 defect. A human placed a mispriced stop; the first
    version checked id-existence and quantity only, so it passed in silence
    (provenance defaults to 'daemon' when someone places a stop directly at
    Schwab). Not redundant with Stage C, whose price check is gated on
    `not trailing` and therefore stops looking exactly when this path fires."""
    pos = _pos(shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET fixed_sl=1.0, stop_loss=1.0 WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)  # entry 50.00 -> algo stop 49.50
    monkeypatch.setattr(schwab_safety, '_open_orders',
                         lambda account: [dict(_resting()[0], stopPrice=42.50)])

    signals_notify._verify_resting_before_replace(pos, _node(), 'ira', TICKER, 4242, 'stop-loss')

    results = [e['result'] for e in
                signals_db.get_coverage_events(scenario_key='replace_target_mismatch')]
    assert 'stop_price_mismatch' in results, results
    assert any('42.50' in p and '49.50' in p for p in env), env


def test_correctly_priced_stop_is_silent(env, monkeypatch):
    pos = _pos(shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET fixed_sl=1.0, stop_loss=1.0 WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_safety, '_open_orders',
                         lambda account: [dict(_resting()[0], stopPrice=49.50)])
    signals_notify._verify_resting_before_replace(pos, _node(), 'ira', TICKER, 4242, 'stop-loss')
    assert env == []


def test_an_accidental_limit_sell_recorded_as_the_stop_is_flagged(env, monkeypatch):
    """An accidental limit-sell is what actually closed the position in the
    2026-08-14 incident."""
    pos = _pos(shares=100)
    monkeypatch.setattr(schwab_safety, '_open_orders',
                         lambda account: [dict(_resting()[0], orderType='LIMIT', stopPrice=None)])
    signals_notify._verify_resting_before_replace(pos, _node(), 'ira', TICKER, 4242, 'stop-loss')
    results = [e['result'] for e in
                signals_db.get_coverage_events(scenario_key='replace_target_mismatch')]
    assert 'not_a_stop_order' in results, results


def test_trailing_sell_label_is_not_price_checked_against_the_fixed_sl(env, monkeypatch):
    """A trailing order's stopPrice is an offset-derived moving level -- price-
    checking it against the fixed SL would false-alarm on every hold-time-forced
    TRAIL replace."""
    pos = _pos(shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET fixed_sl=1.0, stop_loss=1.0 WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_safety, '_open_orders',
                         lambda account: [dict(_resting()[0], orderType='TRAILING_STOP', stopPrice=42.50)])
    signals_notify._verify_resting_before_replace(pos, _node(), 'ira', TICKER, 4242, 'trailing-sell')
    results = [e['result'] for e in
                signals_db.get_coverage_events(scenario_key='replace_target_mismatch')]
    assert 'stop_price_mismatch' not in results, results


def test_a_different_tickers_stop_is_never_adopted_as_the_substitute(env, monkeypatch):
    """_open_orders is account-wide; soxl_ira alone holds 8+ nodes. The stale-id
    fallback must not reach across symbols."""
    pos = _pos(shares=100)
    other = {
        'orderId': 777, 'orderType': 'STOP', 'stopPrice': 10.0,
        'orderLegCollection': [{'instruction': 'SELL', 'quantity': 999.0,
                                 'instrument': {'symbol': 'SOME_OTHER_TICKER'}}],
    }
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [other])
    signals_notify._verify_resting_before_replace(pos, _node(), 'ira', TICKER, 4242, 'stop-loss')
    results = [e['result'] for e in
                signals_db.get_coverage_events(scenario_key='replace_target_mismatch')]
    assert results == ['resting_order_not_found'], results
    assert not any('999' in p for p in env), env


def test_a_stale_id_with_a_real_substitute_stop_is_still_verified(env, monkeypatch):
    """Three confirmed ways our id goes stale while a real stop keeps resting.
    We must still check the substitute rather than returning blind -- while
    still saying the recorded id is dead."""
    pos = _pos(shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET fixed_sl=1.0, stop_loss=1.0 WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_safety, '_open_orders',
                         lambda account: [dict(_resting(order_id=888)[0], stopPrice=42.50)])

    signals_notify._verify_resting_before_replace(pos, _node(), 'ira', TICKER, 4242, 'stop-loss')

    results = sorted(e['result'] for e in
                      signals_db.get_coverage_events(scenario_key='replace_target_mismatch'))
    assert 'resting_order_id_stale' in results, results
    assert 'stop_price_mismatch' in results, (
        f"the substitute must still be price-checked, not skipped: {results}")


def test_a_raising_open_orders_does_not_stop_the_real_replace_from_reaching_the_broker(env, monkeypatch):
    """THE property that matters most, now proven BEHAVIORALLY rather than by
    source-substring inspection.

    Both reviewers flagged the original proof (`assert 'try:' in src[:idx]`) as
    inadequate: it matches any unrelated earlier `try:` in the function and
    would keep passing if someone unwrapped the actual call. This drives the
    real _attempt_automated_sell end-to-end with a _open_orders that raises,
    and asserts the trailing-sell replacement STILL reached the broker.

    If this ever fails, a position whose exit condition has fired cannot exit
    because an advisory check errored -- strictly worse than the bug #4 this
    whole mechanism was added to fix."""
    import schwab_client
    pos = _pos(shares=100)
    signals_db.set_sl_order_id(TICKER, 4242)
    pos = signals_db.get_open_position(TICKER)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET trail_sell_pct=1.0 WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)

    def _boom(account):
        raise RuntimeError("broker outage during the advisory check")

    monkeypatch.setattr(schwab_safety, '_open_orders', _boom)
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})

    replaced = []

    def _fake_replace(account, ticker, order_id, shares, price, trail_pct, **kw):
        replaced.append({'order_id': order_id, 'shares': shares})
        return (None, 55555)

    monkeypatch.setattr(schwab_client, 'replace_order_with_trailing_sell', _fake_replace)

    ok, new_id = signals_notify._attempt_automated_sell(pos, current_price=48.0)

    assert replaced, "the real replace must still reach the broker despite the check raising"
    assert replaced[0]['order_id'] == 4242
    assert (ok, new_id) == (True, 55555)


def test_the_advisory_check_is_actually_invoked_on_the_normal_path(env, monkeypatch):
    """Guards the opposite failure: the non-blocking wrapper must not be so
    permissive that the check silently never runs at all."""
    import schwab_client
    pos = _pos(shares=100)
    signals_db.set_sl_order_id(TICKER, 4242)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET trail_sell_pct=1.0 WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)

    seen = []
    monkeypatch.setattr(schwab_safety, '_open_orders',
                         lambda account: seen.append(account) or _resting(quantity=60.0))
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_client, 'replace_order_with_trailing_sell',
                         lambda *a, **kw: (None, 55555))

    signals_notify._attempt_automated_sell(pos, current_price=48.0)

    assert seen, "the pre-replace check must actually run on the normal path"
    results = [e['result'] for e in
                signals_db.get_coverage_events(scenario_key='replace_target_mismatch')]
    assert 'quantity_mismatch' in results, results
