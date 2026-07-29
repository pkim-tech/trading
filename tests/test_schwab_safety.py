"""Tests for the Schwab order-placement safety gate (schwab_safety.py /
schwab_client.py). Uses an isolated sqlite DB (never trading_live.db) for the
watchlist and an isolated JSON file for cap/burst/duplicate state -- no real
Schwab API calls (dry_run stays True) and no real Slack posts (schwab_client's
_post_message is stubbed out)."""
import os
import sys
import tempfile
import time
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime

import signals_config
import signals_db
import schwab_safety
import schwab_client

TICKER = 'TEST_SAFETY'

# Fixed inside the 10:25-10:40 ET signal window -- tests need a deterministic
# in-window time regardless of when the suite actually runs.
_IN_WINDOW_TIME = datetime(2026, 7, 15, 10, 30)


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(schwab_safety, 'STATE_PATH', tmp_path / "schwab_order_counts.json")
    monkeypatch.setattr(schwab_safety, 'KILL_SWITCH_PATH', tmp_path / "schwab_kill_switch.json")
    monkeypatch.setattr(schwab_safety, 'TICKER_AUTOMATION_PATH', tmp_path / "schwab_ticker_automation.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTOMATION_PATH', tmp_path / "schwab_node_automation.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: _IN_WINDOW_TIME)
    monkeypatch.setattr(schwab_client, '_post_message', lambda *a, **kw: (None, None))
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda account: 1_000_000.0)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'ZScoreBreakout', 'test', window=20, take_profit=10,
                         stop_loss=5, max_hold_hours=56, mode='live')
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account = 'ira' WHERE ticker = ?", (TICKER,))
        c.commit()

    yield
    os.unlink(tmp_db.name)


def test_dry_run_blocks_real_api_call(env):
    result = schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    assert result == (None, None)


def test_buy_outside_signal_window_blocked(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 7, 15, 12, 0))
    with pytest.raises(schwab_safety.SafetyViolation, match="outside signal windows"):
        schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)


def test_sell_outside_signal_window_not_blocked(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 7, 15, 12, 0))
    result = schwab_client.place_equity_sell('ira', TICKER, 5, 50.0)
    assert result == (None, None)  # dry_run -- not blocked by the time gate


def test_trailing_buy_dry_run_blocks_real_api_call(env):
    result = schwab_client.place_trailing_buy('ira', TICKER, 5, 50.0, trail_pct=1.0)
    assert result == (None, None)


def test_trailing_buy_goes_through_same_safety_checks(env):
    with pytest.raises(schwab_safety.SafetyViolation, match="not assigned to account 'brokerage'"):
        schwab_client.place_trailing_buy('brokerage', TICKER, 5, 50.0, trail_pct=1.0)


def test_trailing_sell_dry_run_blocks_real_api_call(env):
    result = schwab_client.place_trailing_sell('ira', TICKER, 5, 50.0, trail_pct=15.0)
    assert result == (None, None)


def test_trailing_sell_goes_through_same_safety_checks(env):
    with pytest.raises(schwab_safety.SafetyViolation, match="not assigned to account 'brokerage'"):
        schwab_client.place_trailing_sell('brokerage', TICKER, 5, 50.0, trail_pct=15.0)


def _get_node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def test_same_day_rebuy_blocked_after_earlier_sale(env):
    from datetime import datetime, timedelta
    node = _get_node()
    signal_time = datetime.now() - timedelta(hours=10)
    trade_id = signals_db.log_trade_entry(
        node, signal_price=100.0, signal_time=signal_time, entry_price=101.0, entry_time=signal_time
    )
    signals_db.log_trade_exit(
        trade_id, exit_signal_price=95.0, exit_price=95.0, exit_time=datetime.now(),
        exit_reason='SL', entry_price=101.0,
    )
    with pytest.raises(schwab_safety.SafetyViolation, match="good-faith violation"):
        schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    events = signals_db.get_coverage_events(scenario_key="same_day_block")
    assert len(events) == 1
    assert events[0]['result'] == "blocked"
    assert events[0]['ticker'] == TICKER


def test_same_day_rebuy_not_blocked_in_margin_account(env):
    """Margin accounts (regular or IRA limited margin) don't have the cash-
    account T+1 settlement restriction same_day_block exists for -- brokerage
    is account_type='margin' in schwab_safety.ACCOUNTS, unlike 'ira' (cash)."""
    from datetime import datetime, timedelta
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account = 'brokerage' WHERE ticker = ?", (TICKER,))
        c.commit()
    node = _get_node()
    signal_time = datetime.now() - timedelta(hours=10)
    trade_id = signals_db.log_trade_entry(
        node, signal_price=100.0, signal_time=signal_time, entry_price=101.0, entry_time=signal_time
    )
    signals_db.log_trade_exit(
        trade_id, exit_signal_price=95.0, exit_price=95.0, exit_time=datetime.now(),
        exit_reason='SL', entry_price=101.0,
    )
    result = schwab_client.place_equity_buy('brokerage', TICKER, 5, 50.0)
    assert result == (None, None)  # dry_run -- not blocked by same_day_block


def test_same_day_sell_after_buy_not_blocked(env):
    # deliberately not a guardrail (2026-07-15): a soft employer recommendation,
    # not a hard broker rule like the same-day-rebuy GFV check above
    from datetime import datetime
    node = _get_node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=100.0, signal_time=now, entry_price=101.0, entry_time=now, shares=5)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='ira' WHERE ticker=?", (TICKER,))
        c.commit()
    result = schwab_client.place_equity_sell('ira', TICKER, 5, 50.0)
    assert result == (None, None)  # dry_run -- reaches the normal dry_run path, not blocked


def test_trailing_buy_shares_duplicate_window_with_market_buy(env):
    schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    with pytest.raises(schwab_safety.SafetyViolation, match="duplicate order"):
        schwab_client.place_trailing_buy('ira', TICKER, 5, 50.0, trail_pct=1.0)


def test_wrong_account_for_ticker_blocked(env):
    with pytest.raises(schwab_safety.SafetyViolation, match="not assigned to account 'brokerage'"):
        schwab_client.place_equity_buy('brokerage', TICKER, 5, 50.0)


def test_ticker_not_on_watchlist_blocked(env):
    with pytest.raises(schwab_safety.SafetyViolation, match="not a live-mode ticker"):
        schwab_client.place_equity_buy('ira', 'NOT_A_REAL_TICKER', 5, 50.0)


def test_live_ticker_outside_automation_pilot_scope_blocked(env):
    signals_db.add_node('TEST_OTHER_LIVE', 'ZScoreBreakout', 'test', window=20, take_profit=10,
                         stop_loss=5, max_hold_hours=56, mode='live')
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account = 'ira' WHERE ticker = 'TEST_OTHER_LIVE'")
        c.commit()
    with pytest.raises(schwab_safety.SafetyViolation, match="not in the automation pilot scope"):
        schwab_client.place_equity_buy('ira', 'TEST_OTHER_LIVE', 5, 50.0)


def test_research_mode_ticker_blocked(env):
    signals_db.add_node('TEST_RESEARCH', 'ZScoreBreakout', 'test', window=20, take_profit=10,
                         stop_loss=5, max_hold_hours=56, mode='research')
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account = 'ira' WHERE ticker = 'TEST_RESEARCH'")
        c.commit()
    with pytest.raises(schwab_safety.SafetyViolation, match="not a live-mode ticker"):
        schwab_client.place_equity_buy('ira', 'TEST_RESEARCH', 5, 50.0)


def test_node_level_automation_pause_blocks_order(env):
    node = _get_node()
    schwab_safety.pause_node_automation(node['id'], reason="test pause")
    with pytest.raises(schwab_safety.SafetyViolation, match="automation paused"):
        schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    events = signals_db.get_coverage_events(scenario_key="node_level_automation_pause")
    assert len(events) == 1
    assert events[0]['result'] == "blocked"
    assert events[0]['node_id'] == node['id']


def test_node_automation_resume_unblocks(env):
    node = _get_node()
    schwab_safety.pause_node_automation(node['id'], reason="test pause")
    schwab_safety.resume_node_automation(node['id'])
    result = schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    assert result == (None, None)  # dry_run -- not blocked once resumed


def test_node_level_automation_pause_does_not_block_sibling_node_other_account(env):
    # Found by Opus review 2026-07-26: the previous version of this test
    # (renamed to test_node_automation_resume_unblocks above) never actually
    # created a second node, so node-level *scoping* was unproven -- pausing
    # node A must not block a sibling node B for the same ticker in a
    # different (unambiguous) account.
    node_a = _get_node()
    signals_db.add_node(TICKER, 'ZScoreBreakout', 'test_sibling', window=20, take_profit=10,
                         stop_loss=5, max_hold_hours=56, mode='live', account='brokerage')
    schwab_safety.pause_node_automation(node_a['id'], reason="test pause")
    result = schwab_client.place_equity_buy('brokerage', TICKER, 5, 50.0)
    assert result == (None, None)  # dry_run -- node_a's pause doesn't touch node_b


def test_node_level_automation_pause_no_op_for_ambiguous_sibling_same_account(env):
    # Documents the KNOWN LIMITATION noted in schwab_safety.check_order
    # (get_watch_list_node returns None on an ambiguous ticker+account match,
    # and node_automation_enabled(None) defaults to True) -- two nodes sharing
    # BOTH ticker and account make a node-level pause silently a no-op, since
    # check_order can't tell which node's row to look up. Not a passing-vs-
    # failing assertion of "safe" behavior -- a real, accepted gap this test
    # pins down so a future fix (or regression) is visible.
    node_a = _get_node()
    signals_db.add_node(TICKER, 'ZScoreBreakout', 'test_ambiguous', window=20, take_profit=10,
                         stop_loss=5, max_hold_hours=56, mode='live', account='ira')
    schwab_safety.pause_node_automation(node_a['id'], reason="test pause")
    result = schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    assert result == (None, None)  # not blocked -- the pause is a no-op here (known limitation)


def test_two_nodes_same_ticker_diff_accounts_logs_event(env):
    # Real gap fixed 2026-07-25 (wl_id refactor): the same ticker can be
    # deliberately live in two different accounts at once (e.g. DPST's
    # paper-vs-real pairing) -- check_order must not treat this as an
    # error, just log it for coverage visibility.
    signals_db.add_node(TICKER, 'ZScoreBreakout', 'test2', window=20, take_profit=10,
                         stop_loss=5, max_hold_hours=56, mode='live', account='brokerage')
    result = schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    assert result == (None, None)  # dry_run -- not blocked
    events = signals_db.get_coverage_events(scenario_key="two_nodes_same_ticker_diff_accounts")
    assert len(events) == 1
    assert events[0]['result'] == "allowed"


def test_duplicate_order_within_window_blocked(env):
    schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    with pytest.raises(schwab_safety.SafetyViolation, match="duplicate order"):
        schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    events = signals_db.get_coverage_events(scenario_key="dup_order_window_blocked")
    assert len(events) == 1
    assert events[0]['result'] == "blocked"


def test_duplicate_guard_is_per_side(env):
    schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    # opposite side isn't a duplicate -- should not raise
    schwab_client.place_equity_sell('ira', TICKER, 5, 50.0)


def test_duplicate_guard_allows_different_quantity(env):
    # 2026-07-21: a same-side order for a different quantity (e.g. Part 3's
    # post-fill top-up, which fires within seconds of the primary buy fill)
    # is a legitimately distinct order, not a retry/double-call bug -- must
    # not be blocked just because it shares account+ticker+side.
    schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    schwab_client.place_equity_buy('ira', TICKER, 3, 50.0)  # should not raise


def test_duplicate_guard_catches_retry_with_slightly_different_quantity(env):
    # A genuine retry re-sizing off a moved price (e.g. 980 -> 981 shares)
    # must still be caught -- exact-quantity matching alone would let this
    # through. 981 is within the 5% tolerance of 980.
    schwab_client.place_equity_buy('ira', TICKER, 980, 50.0)
    with pytest.raises(schwab_safety.SafetyViolation, match="duplicate order"):
        schwab_client.place_equity_buy('ira', TICKER, 981, 50.0)


def test_duplicate_guard_tolerance_does_not_catch_top_up_sized_order(env):
    # A real top-up (much smaller than the primary fill, e.g. 100 vs 980)
    # sits well outside the 5% tolerance and must not be blocked.
    schwab_client.place_equity_buy('ira', TICKER, 980, 50.0)
    schwab_client.place_equity_buy('ira', TICKER, 100, 50.0)  # should not raise


def test_second_sell_order_for_same_ticker_blocked(env, monkeypatch):
    # Symmetric to the BUY-side resting-order guard, added 2026-07-22 after
    # Opus review found SELL had no such check at all -- the gap that let a
    # real trail_state clobber bug place two live trailing-sell orders for
    # the same shares.
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [
        {"status": "WORKING", "orderLegCollection": [
            {"instruction": "SELL", "instrument": {"symbol": TICKER}}
        ]}
    ])
    with pytest.raises(schwab_safety.SafetyViolation, match="resting SELL order"):
        schwab_client.place_equity_sell('ira', TICKER, 5, 50.0)
    events = signals_db.get_coverage_events(scenario_key="dup_sell_order_blocked")
    assert len(events) == 1
    assert events[0]['result'] == "blocked"


def test_replace_does_not_self_block_on_the_order_it_is_replacing(env, monkeypatch):
    # Real 2026-07-28 incident: _attempt_automated_exit_sell replaces a
    # resting protective SL with a market SELL via replace_equity_order_with_
    # market, passing the SL's own order_id as the replace target. Before this
    # fix, check_order's resting-SELL guard saw that same order still resting
    # (the cancel hasn't happened yet at check time) and blocked every single
    # attempt -- SH's TIME/SL exit was stuck behind this for 4 real days.
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [
        {"status": "WORKING", "orderId": 999, "orderLegCollection": [
            {"instruction": "SELL", "instrument": {"symbol": TICKER}}
        ]}
    ])
    result = schwab_client.replace_equity_order_with_market('ira', TICKER, 999, "SELL", 5, 50.0)
    assert result == (None, None)  # dry_run short-circuit, not a SafetyViolation
    events = signals_db.get_coverage_events(scenario_key="dup_sell_order_blocked")
    assert len(events) == 0


def test_trailing_sell_replace_does_not_self_block_on_the_order_it_is_replacing(env, monkeypatch):
    # Same fix, other call site: replace_order_with_trailing_sell is the
    # TRAIL-arm swap (_attempt_automated_sell replacing a resting SL with a
    # TRAILING_STOP SELL). GDXU's real 07-27 arm event never actually
    # exercised this path -- it was staged manually via
    # stage_live_test_order.py, which bypasses schwab_safety entirely -- so
    # this fix had no test and no live proof until now.
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [
        {"status": "WORKING", "orderId": 999, "orderLegCollection": [
            {"instruction": "SELL", "instrument": {"symbol": TICKER}}
        ]}
    ])
    result = schwab_client.replace_order_with_trailing_sell('ira', TICKER, 999, 5, 50.0, 0.3)
    assert result == (None, None)  # dry_run short-circuit, not a SafetyViolation
    events = signals_db.get_coverage_events(scenario_key="dup_sell_order_blocked")
    assert len(events) == 0


def test_replace_still_blocks_on_a_different_resting_sell_order(env, monkeypatch):
    # exclude_order_id must be exact -- a genuinely different resting SELL
    # order (not the one being replaced) still must block, same as before.
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [
        {"status": "WORKING", "orderId": 111, "orderLegCollection": [
            {"instruction": "SELL", "instrument": {"symbol": TICKER}}
        ]}
    ])
    with pytest.raises(schwab_safety.SafetyViolation, match="resting SELL order"):
        schwab_client.replace_equity_order_with_market('ira', TICKER, 999, "SELL", 5, 50.0)


def test_resting_buy_for_same_ticker_does_not_block_sell(env, monkeypatch):
    # An unrelated resting BUY for this ticker must not block closing a
    # position -- only a same-side (SELL) match should.
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [
        {"status": "WORKING", "orderLegCollection": [
            {"instruction": "BUY", "instrument": {"symbol": TICKER}}
        ]}
    ])
    result = schwab_client.place_equity_sell('ira', TICKER, 5, 50.0)
    assert result == (None, None)


def test_duplicate_guard_allows_retry_when_broker_never_confirms_prior_attempt(env, monkeypatch):
    # Real (non-dry_run) account: approve_and_record() writes the local
    # 'recent_orders' record before the real place_order call happens, so a
    # failed/rejected/errored broker call still looks like a submitted
    # duplicate to the old purely-local check. The fix (2026-07-22) cross-
    # checks the broker's real order book before blocking a retry -- if
    # nothing genuinely reached the broker, the retry must go through.
    monkeypatch.setattr(schwab_safety.ACCOUNTS['ira'], 'dry_run', False)
    monkeypatch.setattr(schwab_safety, '_all_orders', lambda account: [])
    counts = {"recent_orders": [
        {"account": "ira", "ticker": TICKER, "side": "BUY", "quantity": 5, "ts": time.time()}
    ]}
    schwab_safety.check_order('ira', TICKER, 5, 50.0, 'BUY', counts=counts)  # should not raise
    events = signals_db.get_coverage_events(scenario_key="dup_order_retry_after_failure")
    assert len(events) == 1
    assert events[0]['result'] == "allowed_retry"


def test_duplicate_guard_blocks_when_broker_confirms_prior_attempt(env, monkeypatch):
    monkeypatch.setattr(schwab_safety.ACCOUNTS['ira'], 'dry_run', False)
    monkeypatch.setattr(schwab_safety, '_all_orders', lambda account: [
        {"status": "WORKING", "orderLegCollection": [
            {"instruction": "BUY", "instrument": {"symbol": TICKER}, "quantity": 5}
        ]}
    ])
    counts = {"recent_orders": [
        {"account": "ira", "ticker": TICKER, "side": "BUY", "quantity": 5, "ts": time.time()}
    ]}
    with pytest.raises(schwab_safety.SafetyViolation, match="duplicate order"):
        schwab_safety.check_order('ira', TICKER, 5, 50.0, 'BUY', counts=counts)


def test_notional_cap_blocked(env):
    cap = schwab_safety.ACCOUNTS['ira'].notional_cap
    with pytest.raises(schwab_safety.SafetyViolation, match="exceeds ira cap"):
        schwab_client.place_equity_buy('ira', TICKER, 1, cap + 1)


def test_notional_cap_does_not_block_sell(env):
    """Found live 2026-07-24: a real armed SPY trailing-sell was permanently
    blocked because its notional exceeded soxl_ira's $800 BUY-sizing cap --
    notional_cap bounds new risk-adding exposure, not closing an existing
    position, so SELL must not be gated by it."""
    cap = schwab_safety.ACCOUNTS['ira'].notional_cap
    result = schwab_client.place_equity_sell('ira', TICKER, 1, cap + 1)
    assert result == (None, None)  # dry_run -- not blocked


def test_hard_ceiling_still_blocks_sell(env):
    """notional_cap is exempt for SELL, but the absolute HARD_ORDER_CEILING
    sanity backstop must still apply to both sides."""
    with pytest.raises(schwab_safety.SafetyViolation, match="exceeds hard ceiling"):
        schwab_client.place_equity_sell('ira', TICKER, 1, schwab_safety.HARD_ORDER_CEILING + 1)


def test_sell_exceeding_real_position_blocked(env):
    """Found by Opus review 2026-07-24: with notional_cap gone from the SELL
    side, nothing on our side bounded SELL quantity at all -- a real
    position-size check replaces it, catching a would-be short/oversell
    before it reaches the broker."""
    node = [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]
    signals_db.open_position(node, signal_price=50.0, signal_time=_IN_WINDOW_TIME,
                              entry_price=50.0, entry_time=_IN_WINDOW_TIME, shares=5)
    with pytest.raises(schwab_safety.SafetyViolation, match="exceeds the 5"):
        schwab_client.place_equity_sell('ira', TICKER, 6, 50.0)
    events = signals_db.get_coverage_events(scenario_key="sell_exceeds_position_blocked")
    assert len(events) == 1
    assert events[0]['result'] == "blocked"


def test_sell_within_real_position_allowed(env):
    node = [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]
    signals_db.open_position(node, signal_price=50.0, signal_time=_IN_WINDOW_TIME,
                              entry_price=50.0, entry_time=_IN_WINDOW_TIME, shares=5)
    result = schwab_client.place_equity_sell('ira', TICKER, 5, 50.0)
    assert result == (None, None)  # dry_run -- not blocked


def test_insufficient_cash_blocked(env, monkeypatch):
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda account: 100.0)
    with pytest.raises(schwab_safety.SafetyViolation, match="cash buffer"):
        schwab_client.place_equity_buy('ira', TICKER, 1, 50.0)


def test_cash_buffer_required_on_top_of_notional(env, monkeypatch):
    # Exactly covers the notional but not the CASH_SAFETY_BUFFER on top of it.
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda account: 50.0)
    with pytest.raises(schwab_safety.SafetyViolation, match="cash buffer"):
        schwab_client.place_equity_buy('ira', TICKER, 1, 50.0)


def test_sufficient_cash_including_buffer_not_blocked(env, monkeypatch):
    monkeypatch.setattr(
        schwab_client, 'get_account_balance',
        lambda account: 50.0 + schwab_safety.CASH_SAFETY_BUFFER,
    )
    result = schwab_client.place_equity_buy('ira', TICKER, 1, 50.0)
    assert result == (None, None)  # dry_run -- passed the check, not actually submitted


def test_balance_fetch_failure_fails_closed(env, monkeypatch):
    def _raise(account):
        raise RuntimeError("API timeout")
    monkeypatch.setattr(schwab_client, 'get_account_balance', _raise)
    with pytest.raises(schwab_safety.SafetyViolation, match="could not verify"):
        schwab_client.place_equity_buy('ira', TICKER, 1, 50.0)


def test_cash_check_not_applied_to_sell(env, monkeypatch):
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda account: 0.0)
    result = schwab_client.place_equity_sell('ira', TICKER, 5, 50.0)
    assert result == (None, None)  # SELL doesn't need buying power, not blocked


def test_second_ticker_resting_buy_in_same_account_blocked(env, monkeypatch):
    # A second live ticker's BUY into the same account, while another ticker's
    # BUY is already resting -- both would otherwise check cash against the
    # same undecremented Schwab balance (Schwab doesn't reserve buying power
    # for a resting order), so a flat cash buffer alone can't catch this.
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [
        {"status": "WORKING", "orderLegCollection": [
            {"instruction": "BUY", "instrument": {"symbol": "OTHER_TICKER"}}
        ]}
    ])
    with pytest.raises(schwab_safety.SafetyViolation, match="resting BUY order for 'OTHER_TICKER'"):
        schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)


def test_resting_sell_order_for_other_ticker_does_not_block_buy(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [
        {"status": "WORKING", "orderLegCollection": [
            {"instruction": "SELL", "instrument": {"symbol": "OTHER_TICKER"}}
        ]}
    ])
    result = schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    assert result == (None, None)


def test_resting_buy_for_same_ticker_reported_by_ticker_specific_guard(env, monkeypatch):
    # Same-ticker resting order should raise via the existing _has_open_order
    # message, not the new account-wide one -- the two checks share one
    # _open_orders() fetch and must not conflict.
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [
        {"status": "WORKING", "orderLegCollection": [
            {"instruction": "BUY", "instrument": {"symbol": TICKER}}
        ]}
    ])
    with pytest.raises(schwab_safety.SafetyViolation, match="already has an open/working order"):
        schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)


def test_reserve_watermark_warning_posted_but_not_blocking(env, monkeypatch):
    # Cash comfortably covers notional + CASH_SAFETY_BUFFER, but the account's
    # raw balance is already below CASH_RESERVE_WATERMARK -- should warn, not
    # block.
    posted = []
    monkeypatch.setattr(schwab_client, '_post_message', lambda msg: posted.append(msg))
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda account: 500.0)
    result = schwab_client.place_equity_buy('ira', TICKER, 1, 50.0)
    assert result == (None, None)  # dry_run -- not blocked
    reserve_msgs = [m for m in posted if "reserve target" in m]
    assert len(reserve_msgs) == 1
    assert "$500" in reserve_msgs[0]


def test_no_reserve_warning_when_comfortably_above_watermark(env, monkeypatch):
    posted = []
    monkeypatch.setattr(schwab_client, '_post_message', lambda msg: posted.append(msg))
    monkeypatch.setattr(
        schwab_client, 'get_account_balance',
        lambda account: schwab_safety.CASH_RESERVE_WATERMARK + 1,
    )
    schwab_client.place_equity_buy('ira', TICKER, 1, 50.0)
    assert not any("reserve target" in m for m in posted)


def test_hard_ceiling_blocked_regardless_of_account_cap(env, monkeypatch):
    monkeypatch.setattr(
        schwab_safety.ACCOUNTS['ira'], 'notional_cap', schwab_safety.HARD_ORDER_CEILING + 1_000_000
    )
    with pytest.raises(schwab_safety.SafetyViolation, match="exceeds hard ceiling"):
        schwab_client.place_equity_buy('ira', TICKER, 1, schwab_safety.HARD_ORDER_CEILING + 1)


def test_daily_cap_blocked(env, monkeypatch):
    monkeypatch.setattr(schwab_safety.ACCOUNTS['ira'], 'daily_order_cap', 1)
    schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    with pytest.raises(schwab_safety.SafetyViolation, match="daily order cap"):
        schwab_client.place_equity_buy('ira', TICKER, 50, 50.0)  # qty far outside dup-order tolerance


def test_sell_does_not_increment_daily_cap(env, monkeypatch):
    """Fixed 2026-07-25: SELL orders (including protective ones) used to
    consume the same shared daily_order_cap pool as BUYs, so unrelated exits
    could starve a later entry's budget. SELLs must no longer count at all --
    matches the precedent already set for notional_cap (BUY-only)."""
    monkeypatch.setattr(schwab_safety.ACCOUNTS['ira'], 'daily_order_cap', 1)
    schwab_client.place_equity_sell('ira', TICKER, 5, 50.0)
    schwab_client.place_equity_sell('ira', TICKER, 6, 50.0)  # different qty avoids dup-order block
    schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)  # still allowed -- cap untouched by SELLs


def test_sell_not_blocked_by_buy_exhausted_daily_cap(env, monkeypatch):
    """Found by Opus review, 2026-07-25: the increment was made BUY-only but
    the check itself stayed side-agnostic, so a real exit SELL could still be
    blocked by a cap only BUYs contributed to -- dangerous since
    _attempt_automated_sell cancels the resting stop-loss before placing the
    trailing-sell, so a blocked sell here would leave a position unprotected.
    The check must be BUY-only too."""
    monkeypatch.setattr(schwab_safety.ACCOUNTS['ira'], 'daily_order_cap', 1)
    schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)  # exhaust the cap
    schwab_client.place_equity_sell('ira', TICKER, 5, 50.0)  # must not be blocked


def test_protective_stop_loss_bypasses_exhausted_daily_cap(env, monkeypatch):
    """Found live 2026-07-24: LABU's real fill had its SL placement blocked by
    an already-exhausted daily_order_cap from unrelated earlier entries,
    leaving a brand-new fill unprotected. place_stop_loss must succeed
    (is_protective=True) even once the account's cap is fully spent."""
    monkeypatch.setattr(schwab_safety.ACCOUNTS['ira'], 'daily_order_cap', 1)
    schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)  # exhaust the cap
    with pytest.raises(schwab_safety.SafetyViolation, match="daily order cap"):
        schwab_client.place_equity_buy('ira', TICKER, 50, 50.0)  # qty far outside dup-order tolerance
    result = schwab_client.place_stop_loss('ira', TICKER, 20, 45.0)  # qty far outside dup-order tolerance
    assert result == (None, None)  # dry_run -- not blocked


def test_protective_top_up_bypasses_exhausted_daily_cap(env, monkeypatch):
    """Same starvation shape as the SL case above, confirmed live for the
    top-up-buy path too ('LABU -- top-up buy of 1 shares blocked')."""
    monkeypatch.setattr(schwab_safety.ACCOUNTS['ira'], 'daily_order_cap', 1)
    schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)  # exhaust the cap
    result = schwab_client.place_equity_buy('ira', TICKER, 50, 50.0, is_protective=True)
    assert result == (None, None)  # dry_run -- not blocked, not raised


def test_global_burst_cap_blocked(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, 'GLOBAL_ORDERS_PER_MINUTE', 1)
    schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    with pytest.raises(schwab_safety.SafetyViolation, match="global burst cap"):
        schwab_client.place_equity_sell('ira', TICKER, 5, 50.0)


def test_kill_switch_blocks_everything(env, monkeypatch):
    monkeypatch.setenv('SCHWAB_KILL_SWITCH', '1')
    with pytest.raises(schwab_safety.SafetyViolation, match="kill switch"):
        schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)


def test_kill_switch_block_logs_coverage_event(env, monkeypatch):
    monkeypatch.setenv('SCHWAB_KILL_SWITCH', '1')
    with pytest.raises(schwab_safety.SafetyViolation):
        schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    events = signals_db.get_coverage_events(scenario_key="kill_switch_block")
    assert len(events) == 1
    assert events[0]['result'] == "blocked"
    assert events[0]['ticker'] == TICKER


def test_kill_switch_persists_across_calls(env):
    assert schwab_safety.kill_switch_engaged() is False
    schwab_safety.engage_kill_switch(reason="test stop")
    assert schwab_safety.kill_switch_engaged() is True
    with pytest.raises(schwab_safety.SafetyViolation, match="kill switch"):
        schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)

    schwab_safety.disengage_kill_switch()
    assert schwab_safety.kill_switch_engaged() is False
    result = schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    assert result == (None, None)  # dry_run -- no longer blocked


def test_disabled_account_blocked(env, monkeypatch):
    monkeypatch.setattr(schwab_safety.ACCOUNTS['ira'], 'enabled', False)
    with pytest.raises(schwab_safety.SafetyViolation, match="disabled"):
        schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)


def test_unknown_account_blocked(env):
    with pytest.raises(schwab_safety.SafetyViolation, match="not in the allowlist"):
        schwab_client.place_equity_buy('made_up_account', TICKER, 5, 50.0)


def test_no_hand_rolled_cancel_then_place_order_swap():
    # Static regression guard, not a runtime check: schwab_client.cancel_order()
    # must stay uncalled from anywhere in the codebase except its own def. Every
    # real order-swap path (check_gap_resize, _attempt_automated_sell,
    # _attempt_automated_exit_sell) was migrated 2026-07-27 to the atomic
    # replace_equity_order_with_market/replace_order_with_trailing_sell helpers,
    # which correctly exclude the order being replaced from schwab_safety's
    # resting-order dup guards (see test_replace_does_not_self_block_on_the_
    # order_it_is_replacing above -- fixed 2026-07-28 after that guard self-
    # blocked SH's exit for 4 real days). A hand-rolled cancel_order() + place_*
    # two-call swap anywhere else would silently reopen the same bug class (and
    # the separate atomicity gap the 2026-07-27 fix closed), so this fails fast
    # on the source itself rather than waiting for it to be found live again.
    import re
    root = Path(__file__).parent.parent
    for py_file in root.glob("*.py"):
        text = py_file.read_text()
        # negative lookbehind for '.' excludes the schwab-py library's own
        # client.cancel_order(...) call inside our wrapper -- only unqualified
        # calls to *our* cancel_order() are in scope here.
        calls = [m.start() for m in re.finditer(r"(?<!\.)\bcancel_order\(", text)]
        if py_file.name == "schwab_client.py":
            # exactly one: the def itself
            defs = [m.start() for m in re.finditer(r"def cancel_order\(", text)]
            assert len(calls) == len(defs), (
                f"schwab_client.cancel_order() is called outside its own definition "
                f"-- verify it's not being used for a hand-rolled cancel+place swap"
            )
        else:
            assert not calls, (
                f"{py_file.name} calls cancel_order() directly -- real order swaps "
                f"must go through replace_equity_order_with_market/"
                f"replace_order_with_trailing_sell instead"
            )
