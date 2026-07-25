"""Paper-trading layer: start_paper_buy -> update_paper_buys (bounce-fill) ->
check_paper_sells (exit), against an isolated sqlite file, mirroring
tests/test_db_roundtrip.py's fixture pattern but exercising the paper=True
tables instead of the real ones."""
import os
import sys
import tempfile
import pytest
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_db as db
import signals_config
import paper_trading
from tests.conftest import make_synthetic_csv, cleanup_csv

TICKER = 'TEST_PAPER'


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    db.ensure_tables()
    make_synthetic_csv(TICKER, last_close=100.0)
    yield
    cleanup_csv(TICKER)
    os.unlink(tmp_db.name)


def _node():
    return {
        'id': 1, 'ticker': TICKER, 'strategy': 'TrailingBothZScoreBreakout', 'version': 'test',
        'window': 20, 'take_profit': None, 'stop_loss': 1, 'max_hold_hours': 7,
        'trail_buy_pct': 5.0, 'trail_sell_pct': 1.0, 'fixed_sl': 1.0, 'arm_sell_pct': 7.0,
        'starting_notional': 5000, 'account': 'ira',
    }


def _sig(price):
    return {'ticker': TICKER, 'current_price': price, 'z_score': -2.5, 'last_bar': datetime.now()}


def test_start_paper_buy_creates_pending_row(isolated_db):
    paper_trading.start_paper_buy(_node(), _sig(100.0))
    pending = db.get_paper_pending_buys()
    assert len(pending) == 1
    assert pending[0]['ticker'] == TICKER
    assert pending[0]['running_low'] == 100.0


def test_start_paper_buy_dedups_existing_pending(isolated_db):
    paper_trading.start_paper_buy(_node(), _sig(100.0))
    paper_trading.start_paper_buy(_node(), _sig(99.0))
    assert len(db.get_paper_pending_buys()) == 1


def test_update_paper_buys_tracks_running_low_without_filling(monkeypatch, isolated_db):
    paper_trading.start_paper_buy(_node(), _sig(100.0))
    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (98.0, None))
    paper_trading.update_paper_buys()
    pending = db.get_paper_pending_buys()
    assert len(pending) == 1
    assert pending[0]['running_low'] == 98.0
    assert db.get_open_positions(paper=True) == []


def test_update_paper_buys_fills_on_bounce_and_opens_paper_position(monkeypatch, isolated_db):
    paper_trading.start_paper_buy(_node(), _sig(100.0))
    # running_low drops to 90, then bounces >= 5% above it (94.5) -> fill
    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (90.0, None))
    paper_trading.update_paper_buys()
    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (95.0, None))
    paper_trading.update_paper_buys()

    assert db.get_paper_pending_buys() == []
    positions = db.get_open_positions(paper=True)
    assert len(positions) == 1
    pos = positions[0]
    assert pos['entry_price'] == 95.0
    assert pos['shares'] == int(5000 // 95.0)
    assert pos['trade_log_id'] is not None


def test_check_paper_sells_closes_on_sl_and_writes_paper_trade_log(monkeypatch, isolated_db):
    node = _node()
    signal_time = datetime.now() - timedelta(hours=1)
    db.open_position(node, signal_price=100.0, signal_time=signal_time,
                      entry_price=100.0, entry_time=signal_time, shares=50, paper=True)
    pos = db.get_open_positions(paper=True)[0]

    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (98.0, None))  # -2% > 1% fixed_sl

    from signals_compute import _load_cache
    df_hourly, _ = _load_cache(TICKER)
    # Pre-mark this bar as already seen so check_paper_sells takes the mid-bar
    # (continuous) branch, which prices off _current_price -- the synthetic CSV
    # fixture only has a Close column, no Low/High, so the bar-close branch
    # (which would read bar['Low']/bar['High']) isn't exercisable here.
    last_seen_bar = {pos['wl_id']: df_hourly.index[-1]}

    paper_trading.check_paper_sells(last_seen_bar, set(), _load_cache)

    assert db.get_open_positions(paper=True) == []
    with db._conn() as c:
        row = c.execute(
            "SELECT exit_reason, pnl_pct FROM paper_trade_log WHERE id = ?", (pos['trade_log_id'],)
        ).fetchone()
    assert row['exit_reason'] == 'SL'
    assert row['pnl_pct'] < 0


def test_real_open_positions_untouched_by_paper_flow(monkeypatch, isolated_db):
    paper_trading.start_paper_buy(_node(), _sig(100.0))
    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (90.0, None))
    paper_trading.update_paper_buys()
    monkeypatch.setattr(paper_trading, '_current_price', lambda t: (95.0, None))
    paper_trading.update_paper_buys()

    assert db.get_open_positions(paper=False) == []
    assert db.get_pending_buys() == []
