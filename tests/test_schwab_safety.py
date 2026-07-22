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
    with pytest.raises(schwab_safety.SafetyViolation, match="assigned to account 'ira'"):
        schwab_client.place_trailing_buy('brokerage', TICKER, 5, 50.0, trail_pct=1.0)


def test_trailing_sell_dry_run_blocks_real_api_call(env):
    result = schwab_client.place_trailing_sell('ira', TICKER, 5, 50.0, trail_pct=15.0)
    assert result == (None, None)


def test_trailing_sell_goes_through_same_safety_checks(env):
    with pytest.raises(schwab_safety.SafetyViolation, match="assigned to account 'ira'"):
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
    with pytest.raises(schwab_safety.SafetyViolation, match="assigned to account 'ira'"):
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


def test_duplicate_order_within_window_blocked(env):
    schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    with pytest.raises(schwab_safety.SafetyViolation, match="duplicate order"):
        schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)


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
    schwab_client.place_equity_sell('ira', TICKER, 5, 50.0)
    with pytest.raises(schwab_safety.SafetyViolation, match="daily order cap"):
        schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)


def test_global_burst_cap_blocked(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, 'GLOBAL_ORDERS_PER_MINUTE', 1)
    schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)
    with pytest.raises(schwab_safety.SafetyViolation, match="global burst cap"):
        schwab_client.place_equity_sell('ira', TICKER, 5, 50.0)


def test_kill_switch_blocks_everything(env, monkeypatch):
    monkeypatch.setenv('SCHWAB_KILL_SWITCH', '1')
    with pytest.raises(schwab_safety.SafetyViolation, match="kill switch"):
        schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)


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
