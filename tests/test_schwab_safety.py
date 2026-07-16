"""Tests for the Schwab order-placement safety gate (schwab_safety.py /
schwab_client.py). Uses an isolated sqlite DB (never trading_live.db) for the
watchlist and an isolated JSON file for cap/burst/duplicate state -- no real
Schwab API calls (dry_run stays True) and no real Slack posts (schwab_client's
_post_message is stubbed out)."""
import os
import sys
import tempfile
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
    assert result is None


def test_buy_outside_signal_window_blocked(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 7, 15, 12, 0))
    with pytest.raises(schwab_safety.SafetyViolation, match="outside signal windows"):
        schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)


def test_sell_outside_signal_window_not_blocked(env, monkeypatch):
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 7, 15, 12, 0))
    result = schwab_client.place_equity_sell('ira', TICKER, 5, 50.0)
    assert result is None  # dry_run -- not blocked by the time gate


def test_trailing_buy_dry_run_blocks_real_api_call(env):
    result = schwab_client.place_trailing_buy('ira', TICKER, 5, 50.0, trail_pct=1.0)
    assert result is None


def test_trailing_buy_goes_through_same_safety_checks(env):
    with pytest.raises(schwab_safety.SafetyViolation, match="assigned to account 'ira'"):
        schwab_client.place_trailing_buy('brokerage', TICKER, 5, 50.0, trail_pct=1.0)


def test_trailing_sell_dry_run_blocks_real_api_call(env):
    result = schwab_client.place_trailing_sell('ira', TICKER, 5, 50.0, trail_pct=15.0)
    assert result is None


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
    assert result is None  # dry_run -- reaches the normal dry_run path, not blocked


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


def test_notional_cap_blocked(env):
    cap = schwab_safety.ACCOUNTS['ira'].notional_cap
    with pytest.raises(schwab_safety.SafetyViolation, match="exceeds ira cap"):
        schwab_client.place_equity_buy('ira', TICKER, 1, cap + 1)


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
    assert result is None  # dry_run -- no longer blocked


def test_disabled_account_blocked(env, monkeypatch):
    monkeypatch.setattr(schwab_safety.ACCOUNTS['ira'], 'enabled', False)
    with pytest.raises(schwab_safety.SafetyViolation, match="disabled"):
        schwab_client.place_equity_buy('ira', TICKER, 5, 50.0)


def test_unknown_account_blocked(env):
    with pytest.raises(schwab_safety.SafetyViolation, match="not in the allowlist"):
        schwab_client.place_equity_buy('made_up_account', TICKER, 5, 50.0)
