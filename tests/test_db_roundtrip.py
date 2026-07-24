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


def test_add_node_dedupes_trailing_both_null_take_profit(db):
    """TrailingBothZScoreBreakout always stores take_profit=NULL (arm_sell_pct
    carries the real value instead) -- SQLite's UNIQUE constraint never treats
    NULL == NULL as a conflict, so a naive INSERT OR IGNORE silently duplicates
    this node on every rerun. Confirmed live 2026-07-24 (15 real duplicate rows
    on soxl_ira). add_node must dedupe these explicitly, not rely on the
    UNIQUE constraint alone."""
    A = db
    for _ in range(3):
        A.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=20,
                   stop_loss=5, max_hold_hours=24, trail_buy_pct=3.0, trail_pct=1.0)
    watchlist = [n for n in A.get_watchlist() if n['ticker'] == TICKER]
    assert len(watchlist) == 1
    assert watchlist[0]['strategy'] == 'TrailingBothZScoreBreakout'


def test_add_node_does_not_collapse_distinct_arm_sell_pct_configs(db):
    """Found by Opus review 2026-07-24: the dedup fix above must not use only
    the original UNIQUE key (which never included arm_sell_pct) -- for
    TrailingBothZScoreBreakout, take_profit is always NULL and arm_sell_pct is
    the real distinguishing value, so two genuinely different nodes (same
    take_profit=NULL, different arm_sell_pct) must NOT collapse to one."""
    A = db
    A.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=20,
               stop_loss=5, max_hold_hours=24, trail_buy_pct=3.0, trail_pct=1.0)
    A.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=30,
               stop_loss=5, max_hold_hours=24, trail_buy_pct=3.0, trail_pct=1.0)
    watchlist = [n for n in A.get_watchlist() if n['ticker'] == TICKER]
    assert len(watchlist) == 2
    assert {n['arm_sell_pct'] for n in watchlist} == {20.0, 30.0}


def _add_node_and_open_position(A, entry_price=101.0):
    A.add_node(TICKER, 'ZScoreBreakout', 'test', window=20, take_profit=10, stop_loss=5,
               max_hold_hours=56)
    node = [n for n in A.get_watchlist() if n['ticker'] == TICKER][0]
    signal_time = datetime.now() - timedelta(hours=10)
    A.open_position(node, signal_price=100.0, signal_time=signal_time,
                     entry_price=entry_price, entry_time=signal_time, shares=50)
    return node, signal_time


def test_seed_last_seen_bar_uses_real_current_bar_for_open_positions(db):
    """Found live 2026-07-24: last_seen_bar starting as an empty dict on every
    daemon restart means the very first poll for an open position always
    treats last_seen_bar.get(ticker) (None) != last_bar_ts as a real bar
    close, regardless of actual timing (confirmed: a restart at 11:14 ET
    triggered SPY's arm/TP check at 11:21 ET, not a real bar close). Seeding
    from the real current bar at startup fixes this."""
    A = db
    _add_node_and_open_position(A)
    open_positions = [p for p in A.get_open_positions() if p['ticker'] == TICKER]
    seeded = A._seed_last_seen_bar(open_positions)
    df_hourly, _ = A._load_cache(TICKER)
    assert seeded[TICKER] == df_hourly.index[-1]


def test_scan_buy_signals_relocks_after_real_same_day_close(db, monkeypatch):
    """buy_alerted's once-per-day lockout structurally blocked a real,
    quantified slice (~8% for SOXL) of backtested trades: buy -> sell -> buy
    all in one day, since the ticker already got its one alert before the
    position even closed. Once a real exit is on file (closed_today) and no
    position is currently open, a later same-day BUY signal must be allowed
    to alert again."""
    A = db
    A.add_node(TICKER, 'ZScoreBreakout', 'test', window=20, take_profit=10, stop_loss=5,
               max_hold_hours=56)
    node = [n for n in A.get_watchlist() if n['ticker'] == TICKER][0]

    buy_sig = {'ticker': TICKER, 'window': node['window'], 'signal': 'BUY', 'z_score': -2.5,
               'current_price': 100.0, 'lower_band': 98.0, 'sma': 102.0, 'std': 1.0,
               'last_bar': datetime.now(), 'hurst': None, 'adf_p': None}
    monkeypatch.setattr(A, 'compute_buy_signal', lambda node, price_override=None: buy_sig)
    notified = []
    monkeypatch.setattr(A, 'notify_buy_signal', lambda node, sig: notified.append(sig['ticker']))

    buy_alerted = set()
    A._scan_buy_signals([node], buy_alerted, open_position_keys=set())
    assert notified == [TICKER]
    key = (TICKER, node['strategy'], node['window'])
    assert key in buy_alerted

    # Still locked: same day, no close yet -- a second scan must not re-alert.
    A._scan_buy_signals([node], buy_alerted, open_position_keys=set())
    assert notified == [TICKER]

    # Real open -> real same-day close (no closed_today entry until this).
    signal_time = datetime.now() - timedelta(hours=1)
    A.open_position(node, signal_price=100.0, signal_time=signal_time,
                     entry_price=100.0, entry_time=signal_time, shares=10)
    pos = [p for p in A.get_open_positions() if p['ticker'] == TICKER][0]
    A.close_position(pos['id'], exit_signal_price=101.0, exit_price=101.0,
                      exit_time=datetime.now(), exit_reason='TIME')
    assert A.closed_today(TICKER)

    # Real close on file, no open position -- a fresh signal must alert again.
    A._scan_buy_signals([node], buy_alerted, open_position_keys=set())
    assert notified == [TICKER, TICKER]


def test_scan_buy_signals_does_not_refire_while_reentry_order_still_pending(db, monkeypatch):
    """Found by Opus review 2026-07-24 of the fix above: closed_today(ticker)
    and no open position is ALSO true for the whole window between "order
    placed" and "position opens on Filled confirmation" -- not just after a
    genuine close. Without checking pending_buys too, the unlock would refire
    (re-notify, and on an automated path, re-place a real order) on every
    single poll while a real re-entry order is still resting at the broker."""
    A = db
    A.add_node(TICKER, 'ZScoreBreakout', 'test', window=20, take_profit=10, stop_loss=5,
               max_hold_hours=56)
    node = [n for n in A.get_watchlist() if n['ticker'] == TICKER][0]

    buy_sig = {'ticker': TICKER, 'window': node['window'], 'signal': 'BUY', 'z_score': -2.5,
               'current_price': 100.0, 'lower_band': 98.0, 'sma': 102.0, 'std': 1.0,
               'last_bar': datetime.now(), 'hurst': None, 'adf_p': None}
    monkeypatch.setattr(A, 'compute_buy_signal', lambda node, price_override=None: buy_sig)
    notified = []
    monkeypatch.setattr(A, 'notify_buy_signal', lambda node, sig: notified.append(sig['ticker']))

    buy_alerted = {(TICKER, node['strategy'], node['window'])}  # already alerted once today

    # Real prior close on file, but a real re-entry order is now resting
    # (pending_buys row exists) -- position hasn't opened yet.
    signal_time = datetime.now() - timedelta(hours=2)
    A.open_position(node, signal_price=100.0, signal_time=signal_time,
                     entry_price=100.0, entry_time=signal_time, shares=10)
    pos = [p for p in A.get_open_positions() if p['ticker'] == TICKER][0]
    A.close_position(pos['id'], exit_signal_price=101.0, exit_price=101.0,
                      exit_time=datetime.now(), exit_reason='TIME')
    assert A.closed_today(TICKER)
    A.add_pending_buy(node, buy_sig, channel='C1', ts='123.456')

    for _ in range(3):  # simulate several poll cycles while the order rests
        A._scan_buy_signals([node], buy_alerted, open_position_keys=set())
    assert notified == []  # must not refire while the order is still pending


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
    result = A.open_position(node, signal_price=100.0, signal_time=signal_time,
                              entry_price=102.0, entry_time=signal_time, shares=50)
    assert result is False
    assert len([p for p in A.get_open_positions() if p['ticker'] == TICKER]) == 1


def test_open_position_returns_true_on_real_open(db):
    A = db
    A.add_node(TICKER, 'ZScoreBreakout', 'test', window=20, take_profit=10, stop_loss=5,
               max_hold_hours=56)
    node = [n for n in A.get_watchlist() if n['ticker'] == TICKER][0]
    signal_time = datetime.now() - timedelta(hours=10)
    result = A.open_position(node, signal_price=100.0, signal_time=signal_time,
                              entry_price=101.0, entry_time=signal_time, shares=50)
    assert result is True


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
    result = A.close_position(pos['id'], exit_signal_price=95.0, exit_price=95.0,
                               exit_time=datetime.now(), exit_reason='SL')
    assert result is True
    assert len([p for p in A.get_open_positions() if p['ticker'] == TICKER]) == 0

    with A._conn() as c:
        trade_row = c.execute(
            "SELECT exit_price, exit_reason, pnl_pct FROM trade_log WHERE id = ?",
            (pos['trade_log_id'],)
        ).fetchone()
    assert trade_row['exit_price'] == 95.0
    assert trade_row['exit_reason'] == 'SL'
    assert trade_row['pnl_pct'] < 0


def test_close_position_is_idempotent_against_a_second_racing_call(db):
    # Regression test for the poll-loop-vs-Slack-handler race (Opus review,
    # 2026-07-22): a second close_position() call for a position already
    # closed (e.g. the poll loop and a button click both noticed the same
    # exit) must be a safe no-op, not a duplicate trade_log overwrite/error.
    A = db
    _add_node_and_open_position(A)
    pos = [p for p in A.get_open_positions() if p['ticker'] == TICKER][0]
    A.close_position(pos['id'], exit_signal_price=95.0, exit_price=95.0,
                      exit_time=datetime.now(), exit_reason='SL')
    second_result = A.close_position(pos['id'], exit_signal_price=90.0, exit_price=90.0,
                                      exit_time=datetime.now(), exit_reason='TIME')
    assert second_result is False

    with A._conn() as c:
        trade_row = c.execute(
            "SELECT exit_price, exit_reason FROM trade_log WHERE id = ?",
            (pos['trade_log_id'],)
        ).fetchone()
    # The first (real) close's values must survive, not get overwritten by
    # the second racing call's different price/reason.
    assert trade_row['exit_price'] == 95.0
    assert trade_row['exit_reason'] == 'SL'
