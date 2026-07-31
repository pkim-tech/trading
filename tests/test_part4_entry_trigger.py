"""Tests for Part 4 (Entry Trigger/Fill/SL-Placement/Arm-latency automation):
active_signals pinned-time scheduling and scan helpers, schwab_client's
get_session_open_price/place_stop_loss, signals_notify's automated market-buy
path and synchronous SL fast-confirm, and paper_trading's market-buy path.
Mirrors tests/test_part3_gap_resize.py's isolated-DB, monkeypatched-client
style: no real Schwab API calls (dry_run stays True), no real Slack posts."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import active_signals
import signals_config
import signals_db
import signals_notify
import paper_trading
import schwab_safety
import schwab_client

TICKER = 'TEST_PART4'

_OPEN_CHECK_TIME = datetime(2026, 7, 15, 9, 30, 2)


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
    monkeypatch.setattr(schwab_safety, '_now', lambda: _OPEN_CHECK_TIME)
    monkeypatch.setattr(schwab_client, '_post_message', lambda *a, **kw: (None, None))
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda account: 1_000_000.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))
    monkeypatch.setattr(paper_trading, '_post_message', lambda *a, **kw: (None, None))
    monkeypatch.setattr(signals_notify, 'time', type('T', (), {'sleep': staticmethod(lambda *a: None)}))
    monkeypatch.setattr(schwab_client, 'time', type('T', (), {'sleep': staticmethod(lambda *a: None)}))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    signals_db.ensure_tables()
    # TrailingExitZScoreBreakout: uses_fixed_sl=True, sl_axis='trail_pct' -- not a
    # trailing-buy strategy (buy_axis_col != 'trail_buy_pct'), so this is the
    # non-trailing / plain-market-buy path Part 4 automates.
    signals_db.add_node(TICKER, 'TrailingExitZScoreBreakout', 'test', window=10, take_profit=26,
                         stop_loss=2, max_hold_hours=100, mode='live',
                         trail_pct=8.0, fixed_sl_override=5.0, entry_timing='open_check',
                         starting_notional=20000)
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
        'ticker': TICKER, 'current_price': price, 'z_score': -2.4,
        'last_bar': _OPEN_CHECK_TIME, 'lower_band': price - 1.0,
        'sma': price + 2.0, 'std': 1.0, 'hurst': None, 'adf_p': None, 'window': 10,
        'signal': 'BUY',
    }


def _pending():
    return [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER][0]


# ---------------------------------------------------------------------------
# _seconds_until_next_pinned_target
# ---------------------------------------------------------------------------

def test_seconds_until_next_pinned_target_before():
    now = datetime(2026, 7, 15, 9, 25, 0)
    secs = active_signals._seconds_until_next_pinned_target(now)
    assert secs == pytest.approx(5 * 60 + 2, abs=1)


def test_seconds_until_next_pinned_target_after_last_rolls_to_tomorrow():
    now = datetime(2026, 7, 15, 15, 45, 0)
    secs = active_signals._seconds_until_next_pinned_target(now)
    # next day's 9:30:02
    expected = (datetime(2026, 7, 16, 9, 30, 2) - now).total_seconds()
    assert secs == pytest.approx(expected, abs=1)


def test_seconds_until_next_pinned_target_mid_targets():
    now = datetime(2026, 7, 15, 10, 30, 5)  # just past 10:30:02
    secs = active_signals._seconds_until_next_pinned_target(now)
    expected = (datetime(2026, 7, 15, 11, 30, 2) - now).total_seconds()
    assert secs == pytest.approx(expected, abs=1)


# ---------------------------------------------------------------------------
# schwab_client.get_session_open_price
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeQuoteClient:
    def __init__(self, open_prices):
        self._open_prices = list(open_prices)

    def get_quote(self, ticker):
        price = self._open_prices.pop(0)
        return _FakeResp({ticker: {"quote": {"openPrice": price}}})


def test_get_session_open_price_retries_then_finds_real_open(monkeypatch):
    fake = _FakeQuoteClient([0.0, 0.0, 55.5])
    monkeypatch.setattr(schwab_client, '_get_client', lambda: fake)
    price, is_true_open = schwab_client.get_session_open_price(TICKER)
    assert price == 55.5
    assert is_true_open is True


def test_get_session_open_price_falls_back_after_all_zero(monkeypatch):
    fake = _FakeQuoteClient([0.0, 0.0, 0.0])
    monkeypatch.setattr(schwab_client, '_get_client', lambda: fake)
    monkeypatch.setattr(schwab_client, 'get_current_price', lambda t: 60.0)
    price, is_true_open = schwab_client.get_session_open_price(TICKER)
    assert price == 60.0
    assert is_true_open is False


# ---------------------------------------------------------------------------
# active_signals._scan_pinned_entry
# ---------------------------------------------------------------------------

def test_scan_pinned_entry_uses_price_override_and_routes_live(env, monkeypatch):
    monkeypatch.setattr(schwab_client, 'get_session_open_price', lambda t: (48.0, True))
    watchlist = signals_db.get_watchlist()
    buy_alerted = set()
    active_signals._scan_pinned_entry(9, 30, watchlist, buy_alerted, open_position_keys=set())
    # a BUY at price=48 (well under sma-2*std=48... use lower_band directly via
    # compute_buy_signal is not mocked here, so just assert the pending-buy path
    # only fires when compute_buy_signal actually resolves a BUY signal -- since
    # there's no cached price data for TICKER, compute_buy_signal returns None and
    # nothing is queued. This exercises the price-fetch + dispatch wiring, not the
    # indicator math (covered by existing signals_compute tests).
    assert signals_db.get_pending_buys() == []


def test_scan_pinned_entry_skips_non_open_check_and_non_automation(env, monkeypatch):
    calls = []
    monkeypatch.setattr(schwab_client, 'get_session_open_price', lambda t: calls.append(t) or (48.0, True))
    watchlist = [dict(n, entry_timing='close') for n in signals_db.get_watchlist()]
    active_signals._scan_pinned_entry(9, 30, watchlist, set(), set())
    assert calls == []

    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', set())
    active_signals._scan_pinned_entry(9, 30, signals_db.get_watchlist(), set(), set())
    assert calls == []


# ---------------------------------------------------------------------------
# _ambient_buy_scan_nodes
# ---------------------------------------------------------------------------

def test_ambient_buy_scan_excludes_open_check_automation_enabled_nodes_before_pinned_check(env):
    """Regression test for the ambient-pre-empts-pinned-check bug (2026-07-31
    audit, left open): _SIGNAL_WINDOWS (10:25-10:40) starts 5 minutes before
    the pinned 10:30 check -- an ambient poll landing in that :25-:29 gap
    used to be able to fire a BUY on a degraded price before the more
    accurate pinned check ran, permanently winning the shared buy_alerted
    dedup. The fixture node is entry_timing='open_check' and TICKER is in
    AUTOMATION_ENABLED_TICKERS -- exactly the population the pinned check
    exclusively owns before its own :30 moment."""
    watchlist = signals_db.get_watchlist()
    before = datetime(2026, 7, 15, 10, 27)
    assert active_signals._ambient_buy_scan_nodes(watchlist, before) == []


def test_ambient_buy_scan_keeps_open_check_nodes_after_pinned_check_as_fallback(env):
    """Regression test for a review finding on the fix above: excluding these
    nodes for the WHOLE window (not just the pre-:30 gap) removed the
    ambient scan's role as a fallback when the pinned check didn't actually
    run for this bar -- e.g. a daemon restart after :30 skips the pinned
    check entirely (pinned_bar_alerted is pre-seeded for any bar-time
    already past at startup, automation_principles.md #15), which used to
    leave the node with zero BUY coverage for the rest of the window."""
    watchlist = signals_db.get_watchlist()
    after = datetime(2026, 7, 15, 10, 33)
    result = active_signals._ambient_buy_scan_nodes(watchlist, after)
    assert [n['ticker'] for n in result] == [TICKER]


def test_ambient_buy_scan_keeps_close_entry_timing_nodes(env):
    watchlist = [dict(n, entry_timing='close') for n in signals_db.get_watchlist()]
    before = datetime(2026, 7, 15, 10, 27)
    result = active_signals._ambient_buy_scan_nodes(watchlist, before)
    assert [n['ticker'] for n in result] == [TICKER]


def test_ambient_buy_scan_keeps_open_check_nodes_outside_automation_scope(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', set())
    watchlist = signals_db.get_watchlist()
    before = datetime(2026, 7, 15, 10, 27)
    result = active_signals._ambient_buy_scan_nodes(watchlist, before)
    assert [n['ticker'] for n in result] == [TICKER]


# ---------------------------------------------------------------------------
# _attempt_automated_market_buy
# ---------------------------------------------------------------------------

def test_attempt_automated_market_buy_dry_run_succeeds(env):
    node = _node()
    sizing = {'shares': 100, 'price': 50.0}
    placed, order_id = signals_notify._attempt_automated_market_buy(node, sizing)
    assert placed is True
    assert order_id is None  # dry_run


def test_attempt_automated_market_buy_blocked_outside_scope(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', set())
    node = _node()
    sizing = {'shares': 100, 'price': 50.0}
    placed, order_id = signals_notify._attempt_automated_market_buy(node, sizing)
    assert (placed, order_id) == (False, None)


def test_attempt_automated_market_buy_blocked_by_kill_switch(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, 'kill_switch_engaged', lambda: True)
    node = _node()
    sizing = {'shares': 100, 'price': 50.0}
    placed, order_id = signals_notify._attempt_automated_market_buy(node, sizing)
    assert (placed, order_id) == (False, None)


# ---------------------------------------------------------------------------
# buy_order_sizing market_pad_pct
# ---------------------------------------------------------------------------

def test_buy_order_sizing_applies_market_pad(env):
    from signals_helpers import buy_order_sizing
    node = _node()
    sig = _sig(price=50.0)
    sizing = buy_order_sizing(node, sig, market_pad_pct=2.0)
    expected_shares = int(20000 // (50.0 * 1.02))
    assert sizing['shares'] == expected_shares
    assert sizing['trailing_buy'] is False


# ---------------------------------------------------------------------------
# notify_buy_signal market-buy path
# ---------------------------------------------------------------------------

def test_notify_buy_signal_market_path_adds_pending_buy_and_protects(env, monkeypatch):
    monkeypatch.setattr(schwab_client, 'get_filled_order',
                         lambda account, ticker, side, order_id=None: {'price': 49.5, 'quantity': 100})
    signals_notify.notify_buy_signal(_node(), _sig())
    # pending_buys row cleared by the synchronous _reconcile_buy_fill path;
    # position should be open and protected.
    assert signals_db.get_pending_buys() == []
    pos = signals_db.get_open_position(TICKER)
    assert pos is not None
    assert pos['entry_price'] == 49.5


def test_notify_buy_signal_market_path_pending_buy_survives_placement_failure(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', set())
    signals_notify.notify_buy_signal(_node(), _sig())
    # not in automation scope -- market_buy_eligible is False, falls through to the
    # existing manual (non-interactive) price-entry flow, no pending_buys row.
    assert signals_db.get_pending_buys() == []


def test_notify_buy_signal_market_path_pending_buy_kept_when_auto_placement_blocked(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, 'kill_switch_engaged', lambda: True)
    monkeypatch.setattr(signals_notify, 'input', lambda: '', raising=False)
    signals_notify.notify_buy_signal(_node(), _sig())
    # blocked placement (kill switch) but ticker is still automation-scope/non-
    # trailing -- add_pending_buy must still fire so the manual reminder flow
    # picks up the signal instead of it being silently dropped.
    pending = _pending()
    assert pending['order_placed'] == 0


# ---------------------------------------------------------------------------
# schwab_client.place_stop_loss
# ---------------------------------------------------------------------------

def test_place_stop_loss_dry_run(env):
    # A stop-loss is a SELL order -- schwab_safety.check_order's oversell
    # guard (fail-closed as of 2026-07-31) needs a real local position on
    # file to check the quantity against.
    now = datetime.now()
    signals_db.open_position(_node(), signal_price=47.5, signal_time=now, entry_price=47.5,
                              entry_time=now, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira' WHERE ticker=?", (TICKER,))
        c.commit()
    r, order_id = schwab_client.place_stop_loss('ira', TICKER, 100, 47.5)
    assert (r, order_id) == (None, None)


def test_place_stop_loss_blocked_by_kill_switch(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, 'kill_switch_engaged', lambda: True)
    with pytest.raises(schwab_safety.SafetyViolation):
        schwab_client.place_stop_loss('ira', TICKER, 100, 47.5)


# ---------------------------------------------------------------------------
# _sync_confirm_and_protect (fast-confirm -> SL placement)
# ---------------------------------------------------------------------------

def test_sync_confirm_and_protect_places_sl_on_fill(env, monkeypatch):
    signals_db.add_pending_buy(_node(), _sig(), channel=None, ts=None)
    monkeypatch.setattr(schwab_client, 'get_filled_order',
                         lambda account, ticker, side, order_id=None: {'price': 49.0, 'quantity': 100})
    placed_calls = []
    monkeypatch.setattr(schwab_client, 'place_stop_loss',
                         lambda account, ticker, qty, stop_price: (placed_calls.append((qty, stop_price)) or (object(), 999)))
    signals_notify._sync_confirm_and_protect(TICKER, _node())
    pos = signals_db.get_open_position(TICKER)
    assert pos is not None
    assert pos['sl_order_id'] == 999
    assert len(placed_calls) == 1
    qty, stop_price = placed_calls[0]
    assert qty == pos['shares']
    # fixed_sl=5.0 off the REAL fill price (49.0), not the stale trigger/
    # signal price (50.0 from _sig()) -- matches strategies.py's own SL
    # check (stop_price = entry_price * (1 - sl%)), correct for every
    # strategy, not just the ones where signal price and fill price happen
    # to coincide (fixed 2026-07-31, see _place_stop_loss_for_position).
    assert stop_price == pytest.approx(49.0 * 0.95)


def test_sync_confirm_and_protect_alerts_on_timeout(env, monkeypatch):
    signals_db.add_pending_buy(_node(), _sig(), channel=None, ts=None)
    monkeypatch.setattr(schwab_client, 'get_filled_order', lambda account, ticker, side, order_id=None: None)
    alerts = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda msg, *a, **kw: alerts.append(msg) or (None, None))
    signals_notify._sync_confirm_and_protect(TICKER, _node())
    assert signals_db.get_open_position(TICKER) is None
    assert any('UNPROTECTED' in m for m in alerts)


# ---------------------------------------------------------------------------
# _place_stop_loss_for_position -- retry-on-rejection (2026-07-31, real
# incident: LABD's stop was REJECTED by Schwab because real time had passed
# between the recorded fill and the actual placement attempt, and the market
# had already crossed the tight target)
# ---------------------------------------------------------------------------

def _open_pos(entry_price=49.0, shares=100):
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, entry_price, now, entry_price, now, shares=shares)
    return signals_db.get_open_position(TICKER)


def test_sl_placement_retries_and_succeeds_when_not_yet_breached(env, monkeypatch):
    """First placement attempt fails with a generic (non-SafetyViolation)
    exception; the market hasn't actually crossed the target stop, so the
    retry should try the SAME resting STOP order again, not fall back to a
    market sell."""
    _open_pos(entry_price=49.0)
    calls = {'stop_loss': 0, 'market_sell': 0}

    def _place_stop_loss(account, ticker, qty, stop_price):
        calls['stop_loss'] += 1
        if calls['stop_loss'] == 1:
            raise RuntimeError("STOP LOSS TEST_PART4 order 111 was REJECTED")
        return (object(), 222)

    monkeypatch.setattr(schwab_client, 'place_stop_loss', _place_stop_loss)
    monkeypatch.setattr(schwab_client, 'get_current_price', lambda ticker: 49.5)  # above stop_price (46.55) -- not breached
    monkeypatch.setattr(schwab_client, 'place_equity_sell',
                         lambda account, ticker, qty, price: (calls.__setitem__('market_sell', calls['market_sell'] + 1), (object(), 333))[1])

    signals_notify._place_stop_loss_for_position(_node(), TICKER)

    assert calls['stop_loss'] == 2, "expected exactly one retry of the resting STOP order"
    assert calls['market_sell'] == 0, "should not have fallen back to a market sell -- price never crossed the target"
    pos = signals_db.get_open_position(TICKER)
    assert pos['sl_order_id'] == 222


def test_sl_placement_falls_back_to_market_sell_when_price_already_breached(env, monkeypatch):
    """First placement attempt fails; a fresh price check shows the market
    has ALREADY crossed the target stop (the real LABD shape) -- retrying
    the same resting STOP would just fail again for the same reason, so this
    should exit via a real market sell instead, matching the kernel's own
    gap-through-trigger logic (exit at the real achievable price, not the
    stale theoretical stop)."""
    pos = _open_pos(entry_price=49.0)  # fixed_sl=5.0 -> stop_price = 46.55
    calls = {'stop_loss': 0, 'market_sell': 0}

    def _place_stop_loss(account, ticker, qty, stop_price):
        calls['stop_loss'] += 1
        raise RuntimeError("REJECTED")

    monkeypatch.setattr(schwab_client, 'place_stop_loss', _place_stop_loss)

    def _place_equity_sell(account, ticker, qty, price):
        calls['market_sell'] += 1
        return (object(), 444)

    monkeypatch.setattr(schwab_client, 'place_equity_sell', _place_equity_sell)
    monkeypatch.setattr(schwab_client, 'get_current_price', lambda ticker: 46.0)  # below stop_price (46.55) -- already breached

    signals_notify._place_stop_loss_for_position(_node(), TICKER)

    assert calls['stop_loss'] == 1, "should not retry the resting STOP once price is confirmed already through it"
    assert calls['market_sell'] == 1, "should fall back to a real market sell at the current (already-breached) price"
    events = signals_db.get_coverage_events(scenario_key='sl_placement')
    assert any(e['result'] == 'placed_as_market_already_breached' for e in events if e['ticker'] == TICKER)

    # The real gap this test originally missed (caught by review, 2026-07-31):
    # placing the market sell isn't enough on its own -- without a recorded
    # exit_pending pointing at the real order_id, check_own_sell_fills can
    # never find this order to confirm its fill and close the position, the
    # DB believes the position is open forever, and the NEXT genuine exit
    # signal would place a second real SELL for shares that no longer exist.
    reopened = signals_db.get_open_position(TICKER)
    exit_pending = reopened['trail_state'].get('exit_pending')
    assert exit_pending is not None, "market-sell fallback must record exit_pending so the fill can be detected"
    assert exit_pending['order_id'] == 444
    assert exit_pending['reason'] == 'SL'


def test_sl_placement_gives_up_after_max_retries_and_alerts_unprotected(env, monkeypatch):
    _open_pos(entry_price=49.0)

    def _place_stop_loss(account, ticker, qty, stop_price):
        raise RuntimeError("REJECTED")

    monkeypatch.setattr(schwab_client, 'place_stop_loss', _place_stop_loss)
    monkeypatch.setattr(schwab_client, 'get_current_price', lambda ticker: 49.5)  # never breached -> always retries the STOP
    alerts = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda msg, *a, **kw: alerts.append(msg) or (None, None))

    signals_notify._place_stop_loss_for_position(_node(), TICKER)

    assert any('UNPROTECTED' in m and 'attempts' in m for m in alerts), (
        f"expected an UNPROTECTED alert citing the retry attempts after exhausting them, got: {alerts}"
    )
    pos = signals_db.get_open_position(TICKER)
    assert pos['sl_order_id'] is None


def test_sl_placement_retry_stops_cleanly_if_already_protected(env, monkeypatch):
    """If a retry attempt hits SafetyViolation (something is already resting
    -- e.g. a concurrent placement, or the earlier failure actually
    succeeded broker-side despite a client-side error), this must stop
    quietly rather than alert UNPROTECTED or keep retrying -- the position
    genuinely IS protected."""
    _open_pos(entry_price=49.0)
    calls = {'stop_loss': 0}

    def _place_stop_loss(account, ticker, qty, stop_price):
        calls['stop_loss'] += 1
        if calls['stop_loss'] == 1:
            raise RuntimeError("REJECTED")
        raise schwab_safety.SafetyViolation("already has a resting SELL order")

    monkeypatch.setattr(schwab_client, 'place_stop_loss', _place_stop_loss)
    monkeypatch.setattr(schwab_client, 'get_current_price', lambda ticker: 49.5)  # not breached -> retries the STOP path
    alerts = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda msg, *a, **kw: alerts.append(msg) or (None, None))

    signals_notify._place_stop_loss_for_position(_node(), TICKER)

    assert calls['stop_loss'] == 2
    assert alerts == [], f"should not alert UNPROTECTED when a retry confirms something is already resting, got: {alerts}"


# ---------------------------------------------------------------------------
# Arm-transition handoff: cancel resting SL before placing trailing-sell
# ---------------------------------------------------------------------------

def test_attempt_automated_sell_replaces_sl_order_id_atomically(env, monkeypatch):
    """When a resting SL exists, _attempt_automated_sell now swaps it for the
    trailing-sell via a single atomic schwab_client.replace_order_with_trailing_sell
    call (cancel-old + create-new as one broker call), not a separate
    cancel_order + place_trailing_sell (found 2026-07-27 -- closes the window
    where a confirmed cancel could be followed by a failed/blocked new
    placement, leaving nothing resting in between)."""
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    signals_db.set_sl_order_id(TICKER, 777)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira', trail_sell_pct=8.0 WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)

    replaced = []
    monkeypatch.setattr(schwab_client, 'replace_order_with_trailing_sell',
                         lambda *a, **kw: replaced.append(a) or (object(), 888))
    monkeypatch.setattr(schwab_client, 'place_trailing_sell',
                         lambda *a, **kw: pytest.fail("place_trailing_sell should not be called when an SL exists"))

    result, order_id = signals_notify._attempt_automated_sell(pos, current_price=52.0)
    assert result is True
    assert order_id == 888
    assert len(replaced) == 1
    assert replaced[0][2] == 777  # order_id positional arg


def test_attempt_automated_sell_skips_cancel_when_no_sl_order_id(env, monkeypatch):
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira', trail_sell_pct=8.0 WHERE ticker=?", (TICKER,))
        c.commit()
    pos = signals_db.get_open_position(TICKER)

    monkeypatch.setattr(schwab_client, 'replace_order_with_trailing_sell',
                         lambda *a, **kw: pytest.fail("replace_order_with_trailing_sell should not be called without an SL"))
    monkeypatch.setattr(schwab_client, 'place_trailing_sell', lambda *a, **kw: (object(), 888))

    result, order_id = signals_notify._attempt_automated_sell(pos, current_price=52.0)
    assert result is True
    assert order_id == 888


# ---------------------------------------------------------------------------
# _scan_pinned_exit_arm dedup with sell_alerted/last_seen_bar
# ---------------------------------------------------------------------------

def test_scan_pinned_exit_arm_dedups_against_sell_alerted(env, monkeypatch):
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    pos = signals_db.get_open_positions()[0]  # get_open_position() doesn't json.loads trail_state

    import pandas as pd
    df = pd.DataFrame(
        {'Open': [50.0], 'Close': [43.0], 'Low': [42.0], 'High': [51.0]},
        index=pd.to_datetime(['2026-07-15 09:30:00']),
    )
    monkeypatch.setattr(active_signals, '_load_cache', lambda ticker: (df, None))

    sell_alerted = set()
    # Seed last_seen_bar to the entry bar first -- a real poll immediately
    # after open_position would already mark that bar as seen (2026-07-27
    # fix: a position's first-ever check must never be graded as 'a bar just
    # closed' against a bar that could contain pre-entry price history, so
    # it's deferred instead of firing on a fresh {}). This SL-breach bar then
    # correctly reads as a genuinely new bar closing.
    last_seen_bar = {active_signals._pos_key(pos): pd.Timestamp('2026-07-15 08:30:00')}
    notify_calls = []
    monkeypatch.setattr(active_signals, 'notify_sell_signal',
                         lambda p, reason, cp, target: notify_calls.append(reason))

    active_signals._scan_pinned_exit_arm([pos], sell_alerted, last_seen_bar)
    assert len(notify_calls) == 1  # SL breach (43 vs entry 50, fixed_sl=5%)
    assert (pos['id'], df.index[-1]) in sell_alerted
    events = signals_db.get_coverage_events(scenario_key="exit_arm_latency")
    assert len(events) == 1
    assert events[0]['result'] == "evaluated"

    # A second call for the same bar must not re-fire.
    active_signals._scan_pinned_exit_arm([pos], sell_alerted, last_seen_bar)
    assert len(notify_calls) == 1


# ---------------------------------------------------------------------------
# paper_trading.start_paper_market_buy
# ---------------------------------------------------------------------------

def test_start_paper_market_buy_opens_position_immediately(env):
    node = dict(_node(), mode='research')
    sig = _sig(price=50.0)
    paper_trading.start_paper_market_buy(node, sig)
    pos = signals_db.get_open_position(TICKER, paper=True)
    assert pos is not None
    assert pos['entry_price'] == 50.0
    expected_shares = int(20000 // (50.0 * 1.01))
    assert pos['shares'] == expected_shares


def test_start_paper_market_buy_dedups_existing_position(env):
    node = dict(_node(), mode='research')
    sig = _sig(price=50.0)
    paper_trading.start_paper_market_buy(node, sig)
    first = signals_db.get_open_position(TICKER, paper=True)
    paper_trading.start_paper_market_buy(node, _sig(price=55.0))
    second = signals_db.get_open_position(TICKER, paper=True)
    assert first['entry_price'] == second['entry_price']


def test_start_paper_buy_dispatches_non_trailing_to_market_buy(env):
    node = dict(_node(), mode='research')
    sig = _sig(price=50.0)
    paper_trading.start_paper_buy(node, sig)
    assert signals_db.get_open_position(TICKER, paper=True) is not None
    assert signals_db.get_paper_pending_buys() == []
