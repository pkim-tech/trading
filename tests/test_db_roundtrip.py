"""Round-trip test for the real DB plumbing: add_node -> open_position ->
check_sell_condition -> close_position, against an isolated sqlite file (never
trading_live.db). Exercises actual DB reads/writes, unlike the per-strategy
signal tests which fabricate node/position dicts directly."""
import os
import sys
import tempfile
import pytest
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

import active_signals as A
import signals_config
from tests.conftest import make_synthetic_csv, cleanup_csv

TICKER = 'TEST_ROUNDTRIP'


@pytest.fixture
def db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    # DB_PATH is a module-level constant computed once at import from
    # TRADING_DB_PATH, owned by signals_config (signals_db._conn() reads it via
    # `cfg.DB_PATH` attribute access) -- patch it there directly (auto-restored
    # by monkeypatch teardown). Patching active_signals.DB_PATH instead would
    # only rebind active_signals's own re-exported copy of the name and never
    # reach the _conn() call that actually opens connections.
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))

    A.ensure_tables()
    make_synthetic_csv(TICKER, last_close=100.0)
    yield A
    cleanup_csv(TICKER)
    os.unlink(tmp_db.name)


def test_add_node_writes_watch_list_row(db):
    A = db
    A.add_node(TICKER, 'ZScoreBreakout', 'test', window=20, take_profit=10, stop_loss=5,
               max_hold_hours=56)
    watchlist = [n for n in A.get_watchlist() if n['ticker'] == TICKER]
    assert len(watchlist) == 1
    assert watchlist[0]['strategy'] == 'ZScoreBreakout'
    assert watchlist[0]['take_profit'] == 10


def _add_node_and_open_position(A, entry_price=101.0):
    A.add_node(TICKER, 'ZScoreBreakout', 'test', window=20, take_profit=10, stop_loss=5,
               max_hold_hours=56)
    node = [n for n in A.get_watchlist() if n['ticker'] == TICKER][0]
    signal_time = datetime.now() - timedelta(hours=10)
    A.open_position(node, signal_price=100.0, signal_time=signal_time,
                     entry_price=entry_price, entry_time=signal_time, shares=50)
    return node, signal_time


def test_open_position_writes_open_positions_and_trade_log(db):
    A = db
    _add_node_and_open_position(A)
    open_positions = [p for p in A.get_open_positions() if p['ticker'] == TICKER]
    assert len(open_positions) == 1
    assert open_positions[0]['entry_price'] == 101.0
    assert bool(open_positions[0]['trade_log_id']) is True


def test_open_position_skips_duplicate_ticker_window(db):
    A = db
    node, signal_time = _add_node_and_open_position(A)
    A.open_position(node, signal_price=100.0, signal_time=signal_time,
                     entry_price=102.0, entry_time=signal_time, shares=50)
    assert len([p for p in A.get_open_positions() if p['ticker'] == TICKER]) == 1


def test_check_sell_condition_sl_hit_on_db_backed_position(db):
    A = db
    _add_node_and_open_position(A)
    pos = [p for p in A.get_open_positions() if p['ticker'] == TICKER][0]
    reason, price, _ = A.check_sell_condition(pos, current_price=95.0, now=datetime.now())
    assert reason == 'SL'
    # ZScoreBreakout.check_exit is bar-close-only and returns current_price on
    # an SL hit, not a computed stop-price level (unlike the trailing strategies).
    assert price == 95.0


def test_check_sell_condition_no_exit_when_healthy(db):
    A = db
    _add_node_and_open_position(A)
    pos = [p for p in A.get_open_positions() if p['ticker'] == TICKER][0]
    reason, _, _ = A.check_sell_condition(pos, current_price=103.0, now=datetime.now())
    assert reason is None


def test_close_position_removes_open_positions_row_and_logs_exit(db):
    A = db
    _add_node_and_open_position(A)
    pos = [p for p in A.get_open_positions() if p['ticker'] == TICKER][0]
    A.close_position(pos['id'], exit_signal_price=95.0, exit_price=95.0,
                      exit_time=datetime.now(), exit_reason='SL')
    assert len([p for p in A.get_open_positions() if p['ticker'] == TICKER]) == 0

    with A._conn() as c:
        trade_row = c.execute(
            "SELECT exit_price, exit_reason, pnl_pct FROM trade_log WHERE id = ?",
            (pos['trade_log_id'],)
        ).fetchone()
    assert trade_row['exit_price'] == 95.0
    assert trade_row['exit_reason'] == 'SL'
    assert trade_row['pnl_pct'] < 0
