"""Tests for starting_notional_override (2026-08-12) -- the lever to
deliberately resize a node's real order sizing once it has closed a real
trade, since signals_helpers._last_sale_recovery otherwise compounds
permanently off trade_log proceeds and ignores any later starting_notional
edit. Found while trying to resize AGQ/ETHU/JNUG's real brokerage nodes."""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
from signals_helpers import _last_sale_recovery

TICKER = 'TEST_NOTIONAL_OVERRIDE'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    signals_db.ensure_tables()
    signals_db.add_node(
        TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
        stop_loss=1, max_hold_hours=100, state='live', trail_buy_pct=1.0, trail_pct=1.0,
        fixed_sl_override=1.0, account='brokerage', starting_notional=2000)
    wl_id = _node()['id']
    yield wl_id
    tmp_db_path = Path(tmp_db.name)
    if tmp_db_path.exists():
        tmp_db_path.unlink()


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _log_closed_trade(exit_price, shares):
    entry_price = exit_price * 0.98
    entry_time = (datetime.now() - timedelta(days=1)).isoformat()
    exit_time = datetime.now().isoformat()
    with signals_db._conn() as c:
        c.execute("""
            INSERT INTO trade_log
                (ticker, strategy, version, window, stop_loss, max_hold_hours, account,
                 signal_price, signal_time, entry_price, entry_time, entry_drift_pct,
                 exit_price, exit_time, exit_reason, shares, is_dry_run_sim, position_source)
            VALUES (?, 'TrailingBothZScoreBreakout', 'test', 10, 1, 100, 'brokerage',
                    ?, ?, ?, ?, 0.0, ?, ?, 'SL', ?, 0, 'core')
        """, (TICKER, entry_price, entry_time, entry_price, entry_time, exit_price, exit_time, shares))
        c.commit()


def test_no_override_no_history_falls_back_to_starting_notional(env):
    node = _node()
    assert _last_sale_recovery(node) == 2000.0


def test_no_override_with_history_compounds_off_trade_log(env):
    _log_closed_trade(exit_price=25.0, shares=100)  # proceeds = 2500
    node = _node()
    assert _last_sale_recovery(node) == 2500.0


def test_starting_notional_edit_has_no_effect_once_history_exists(env):
    """The bug this feature exists to work around -- pinned here so a
    future change can't silently reintroduce reliance on it."""
    _log_closed_trade(exit_price=25.0, shares=100)  # proceeds = 2500
    wl_id = env
    signals_db.set_starting_notional(wl_id, 6000)
    node = _node()
    assert _last_sale_recovery(node) == 2500.0  # NOT 6000


def test_override_takes_precedence_over_trade_log_history(env):
    _log_closed_trade(exit_price=25.0, shares=100)  # proceeds = 2500
    wl_id = env
    signals_db.set_starting_notional_override(wl_id, 6000.0)
    node = _node()
    assert _last_sale_recovery(node) == 6000.0


def test_override_takes_precedence_with_no_history_too(env):
    wl_id = env
    signals_db.set_starting_notional_override(wl_id, 6000.0)
    node = _node()
    assert _last_sale_recovery(node) == 6000.0


def test_clear_override_resumes_trade_log_compounding(env):
    _log_closed_trade(exit_price=25.0, shares=100)  # proceeds = 2500
    wl_id = env
    signals_db.set_starting_notional_override(wl_id, 6000.0)
    assert _last_sale_recovery(_node()) == 6000.0
    signals_db.clear_starting_notional_override(wl_id)
    assert _last_sale_recovery(_node()) == 2500.0


def test_override_is_not_one_shot(env):
    """Repeated calls (as would happen across multiple signal checks before
    a real order actually places) must keep returning the override, not
    consume it after the first read."""
    wl_id = env
    signals_db.set_starting_notional_override(wl_id, 6000.0)
    node = _node()
    assert _last_sale_recovery(node) == 6000.0
    assert _last_sale_recovery(node) == 6000.0


def test_set_override_on_unknown_node_raises(env):
    with pytest.raises(ValueError, match="no watch_list row"):
        signals_db.set_starting_notional_override(999999, 6000.0)


def test_clear_override_on_unknown_node_raises(env):
    with pytest.raises(ValueError, match="no watch_list row"):
        signals_db.clear_starting_notional_override(999999)


def test_set_override_logs_audit(env):
    wl_id = env
    signals_db.set_starting_notional_override(wl_id, 6000.0)
    audit = signals_db.get_watchlist_audit(limit=5)
    assert any(a['action'] == 'set_starting_notional_override' and a['watch_id'] == wl_id for a in audit)
