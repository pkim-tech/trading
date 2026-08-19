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

import sqlite3

import active_signals as A
import signals_config
import signals_db as db_module
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


def test_add_node_does_not_collapse_distinct_entry_timing_configs(db):
    """Real root cause of the 2026-08-13 'Scenario C' collision: two genuinely
    distinct nodes (same ticker/strategy/window/etc., different entry_timing)
    were silently treated as duplicates by the pre-fix dedup key, worked
    around at the time via a max_hold_hours=47 hack instead of fixed here."""
    A = db
    A.add_node(TICKER, 'ZScoreBreakout', 'test', window=20, take_profit=10, stop_loss=5,
               max_hold_hours=56, entry_timing='close')
    A.add_node(TICKER, 'ZScoreBreakout', 'test', window=20, take_profit=10, stop_loss=5,
               max_hold_hours=56, entry_timing='open_check')
    watchlist = [n for n in A.get_watchlist() if n['ticker'] == TICKER]
    assert len(watchlist) == 2
    assert {n['entry_timing'] for n in watchlist} == {'close', 'open_check'}


def test_add_node_does_not_collapse_distinct_fixed_sl_configs(db):
    """Same collision shape as entry_timing above, but for fixed_sl -- two
    uses_fixed_sl-strategy nodes with the same ticker/strategy/window/etc. but
    different fixed_sl_override values are genuinely distinct real SL configs,
    not duplicates."""
    A = db
    A.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=20,
               stop_loss=5, max_hold_hours=24, trail_buy_pct=3.0, trail_pct=1.0,
               fixed_sl_override=1.0)
    A.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=20,
               stop_loss=5, max_hold_hours=24, trail_buy_pct=3.0, trail_pct=1.0,
               fixed_sl_override=2.0)
    watchlist = [n for n in A.get_watchlist() if n['ticker'] == TICKER]
    assert len(watchlist) == 2
    assert {n['fixed_sl'] for n in watchlist} == {1.0, 2.0}


def test_add_node_still_dedupes_genuinely_identical_nodes_incl_new_fields(db):
    """Regression check: two calls identical in every field, including the
    newly-added entry_timing/fixed_sl, must still collapse to one row -- the
    widened dedup key must not turn into a no-op that stops deduping at all."""
    A = db
    for _ in range(3):
        A.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=20,
                   stop_loss=5, max_hold_hours=24, trail_buy_pct=3.0, trail_pct=1.0,
                   entry_timing='open_check', fixed_sl_override=1.0)
    watchlist = [n for n in A.get_watchlist() if n['ticker'] == TICKER]
    assert len(watchlist) == 1
    assert watchlist[0]['entry_timing'] == 'open_check'
    assert watchlist[0]['fixed_sl'] == 1.0


def test_entry_timing_fixed_sl_migration_preserves_populated_old_shape_table(monkeypatch):
    """Paired-review finding, 2026-08-19: the 3 tests above all run against a
    freshly-created DB (ensure_tables() on an empty file takes the plain
    CREATE-with-current-schema path, never the migration branch), so the
    riskiest half of the entry_timing/fixed_sl fix -- the actual watch_list_new
    rebuild against REAL, PRE-EXISTING rows in the OLD UNIQUE shape -- had zero
    coverage. Hand-builds an old-shape watch_list table (UNIQUE lacking
    entry_timing/fixed_sl, matching the shape before this fix), seeds real
    rows, runs the real ensure_tables(), and asserts every row/column survives
    exactly, the new UNIQUE constraint is in place, and a second ensure_tables()
    call is a correct no-op (idempotency)."""
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    try:
        conn = sqlite3.connect(tmp_db.name)
        conn.executescript("""
            CREATE TABLE watch_list (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                watchlist_id       INTEGER NOT NULL,
                state              TEXT NOT NULL DEFAULT 'paper',
                ticker             TEXT NOT NULL,
                strategy           TEXT NOT NULL,
                version            TEXT NOT NULL,
                window             INTEGER NOT NULL,
                take_profit        INTEGER,
                stop_loss          INTEGER NOT NULL,
                max_hold_hours     INTEGER NOT NULL,
                z_score_threshold  REAL NOT NULL DEFAULT 2.0,
                label              TEXT DEFAULT '',
                added_at           TEXT DEFAULT (datetime('now')),
                trail_sell_pct     REAL,
                fixed_sl           REAL,
                trail_buy_pct      REAL,
                arm_sell_pct       REAL,
                cached_avg_vol_10d REAL,
                account            TEXT,
                alpha              REAL,
                entry_timing       TEXT NOT NULL DEFAULT 'close',
                starting_notional  REAL NOT NULL DEFAULT 50000,
                annotation         TEXT,
                paper_alert_verbose INTEGER NOT NULL DEFAULT 0,
                paper_role         TEXT,
                UNIQUE(watchlist_id, ticker, strategy, version, window, take_profit,
                       stop_loss, max_hold_hours, arm_sell_pct, trail_buy_pct,
                       trail_sell_pct, account, paper_role)
            );
        """)
        conn.execute(
            "INSERT INTO watch_list (watchlist_id, state, ticker, strategy, version, window, "
            "take_profit, stop_loss, max_hold_hours, label, trail_sell_pct, fixed_sl, "
            "trail_buy_pct, arm_sell_pct, account, entry_timing, starting_notional) "
            "VALUES (1, 'live', 'MIGTEST1', 'TrailingBothZScoreBreakout', 'test', 10, NULL, "
            "5, 24, 'pre-migration row 1', 1.0, 2.0, 3.0, 15.0, 'ira', 'close', 12345.0)")
        conn.execute(
            "INSERT INTO watch_list (watchlist_id, state, ticker, strategy, version, window, "
            "take_profit, stop_loss, max_hold_hours, label, entry_timing, starting_notional) "
            "VALUES (1, 'paper', 'MIGTEST2', 'ZScoreBreakout', 'test', 20, 10, 5, 56, "
            "'pre-migration row 2', 'open_check', 5000.0)")
        conn.commit()
        conn.close()

        monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
        db_module.ensure_tables()

        with db_module._conn() as c:
            rows = c.execute(
                "SELECT * FROM watch_list WHERE ticker LIKE 'MIGTEST%' ORDER BY ticker"
            ).fetchall()
            assert len(rows) == 2, "migration must preserve every pre-existing row"
            r1, r2 = dict(rows[0]), dict(rows[1])
            assert r1['ticker'] == 'MIGTEST1'
            assert r1['label'] == 'pre-migration row 1'
            assert r1['fixed_sl'] == 2.0
            assert r1['trail_buy_pct'] == 3.0
            assert r1['arm_sell_pct'] == 15.0
            assert r1['account'] == 'ira'
            assert r1['starting_notional'] == 12345.0
            assert r2['ticker'] == 'MIGTEST2'
            assert r2['entry_timing'] == 'open_check'
            assert r2['starting_notional'] == 5000.0

            schema_sql = c.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='watch_list'"
            ).fetchone()[0]
            unique_clause = schema_sql.split('UNIQUE(')[-1]
            assert 'entry_timing' in unique_clause
            assert 'fixed_sl' in unique_clause
            before_snapshot = [dict(r) for r in rows]

        # Idempotency: a second ensure_tables() call must be a correct no-op --
        # not re-migrate, not error, not duplicate/drop any row, and not change
        # any column value (full-row comparison, not just a count).
        db_module.ensure_tables()
        with db_module._conn() as c:
            rows_after = [dict(r) for r in c.execute(
                "SELECT * FROM watch_list WHERE ticker LIKE 'MIGTEST%' ORDER BY ticker"
            ).fetchall()]
            assert rows_after == before_snapshot
    finally:
        os.unlink(tmp_db.name)


def test_entry_timing_fixed_sl_migration_guard_fires_on_stale_column_list(monkeypatch):
    """Negative-path coverage for the paired-review-added safety guard: if the
    live watch_list somehow has a column the migration's hardcoded CREATE
    TABLE watch_list_new list doesn't know about, ensure_tables() must raise a
    specific, actionable RuntimeError -- not a bare sqlite3.OperationalError
    ('no such column') at daemon startup with no clue what's wrong."""
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    try:
        conn = sqlite3.connect(tmp_db.name)
        conn.executescript("""
            CREATE TABLE watch_list (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                watchlist_id       INTEGER NOT NULL,
                state              TEXT NOT NULL DEFAULT 'paper',
                ticker             TEXT NOT NULL,
                strategy           TEXT NOT NULL,
                version            TEXT NOT NULL,
                window             INTEGER NOT NULL,
                take_profit        INTEGER,
                stop_loss          INTEGER NOT NULL,
                max_hold_hours     INTEGER NOT NULL,
                z_score_threshold  REAL NOT NULL DEFAULT 2.0,
                label              TEXT DEFAULT '',
                added_at           TEXT DEFAULT (datetime('now')),
                trail_sell_pct     REAL,
                fixed_sl           REAL,
                trail_buy_pct      REAL,
                arm_sell_pct       REAL,
                cached_avg_vol_10d REAL,
                account            TEXT,
                alpha              REAL,
                entry_timing       TEXT NOT NULL DEFAULT 'close',
                starting_notional  REAL NOT NULL DEFAULT 50000,
                annotation         TEXT,
                paper_alert_verbose INTEGER NOT NULL DEFAULT 0,
                paper_role         TEXT,
                totally_unmirrored_future_column TEXT,
                UNIQUE(watchlist_id, ticker, strategy, version, window, take_profit,
                       stop_loss, max_hold_hours, arm_sell_pct, trail_buy_pct,
                       trail_sell_pct, account, paper_role)
            );
        """)
        conn.commit()
        conn.close()

        monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
        with pytest.raises(RuntimeError, match='totally_unmirrored_future_column'):
            db_module.ensure_tables()
    finally:
        os.unlink(tmp_db.name)


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
    node, _ = _add_node_and_open_position(A)
    open_positions = [p for p in A.get_open_positions() if p['ticker'] == TICKER]
    seeded = A._seed_last_seen_bar(open_positions)
    df_hourly, _ = A._load_cache(TICKER)
    assert seeded[node['id']] == df_hourly.index[-1]


def test_scan_buy_signals_relocks_after_real_same_day_close(db, monkeypatch):
    """buy_alerted's once-per-day lockout structurally blocked a real,
    quantified slice (~8% for SOXL) of backtested trades: buy -> sell -> buy
    all in one day, since the ticker already got its one alert before the
    position even closed. Once a real exit is on file (closed_today) and no
    position is currently open, a later same-day BUY signal must be allowed
    to alert again."""
    A = db
    A.add_node(TICKER, 'ZScoreBreakout', 'test', window=20, take_profit=10, stop_loss=5,
               max_hold_hours=56, state='live')
    node = [n for n in A.get_watchlist() if n['ticker'] == TICKER][0]

    buy_sig = {'ticker': TICKER, 'window': node['window'], 'signal': 'BUY', 'z_score': -2.5,
               'current_price': 100.0, 'lower_band': 98.0, 'sma': 102.0, 'std': 1.0,
               'last_bar': datetime.now(), 'hurst': None, 'adf_p': None}
    monkeypatch.setattr(A, 'compute_buy_signal', lambda node, price_override=None: buy_sig)
    notified = []
    monkeypatch.setattr(A, 'notify_buy_signal', lambda node, sig: notified.append(sig['ticker']))

    buy_alerted = set()
    A._scan_buy_signals([node], buy_alerted, open_position_keys={'live': set(), 'paper': set()})
    assert notified == [TICKER]
    assert node['id'] in buy_alerted

    # Still locked: same day, no close yet -- a second scan must not re-alert.
    A._scan_buy_signals([node], buy_alerted, open_position_keys={'live': set(), 'paper': set()})
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
    A._scan_buy_signals([node], buy_alerted, open_position_keys={'live': set(), 'paper': set()})
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

    buy_alerted = {node['id']}  # already alerted once today

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
        A._scan_buy_signals([node], buy_alerted, open_position_keys={'live': set(), 'paper': set()})
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


def test_previous_trading_day_walks_back_over_weekend():
    """Monday's 7am coverage check must look at Friday's results, not a
    trivially-weekend-skipped Sunday."""
    monday = datetime(2026, 7, 27, 7, 0)  # a real Monday
    assert A._previous_trading_day(monday) == '2026-07-24'  # Friday


def test_previous_trading_day_normal_weekday():
    tuesday = datetime(2026, 7, 28, 7, 0)
    assert A._previous_trading_day(tuesday) == '2026-07-27'  # Monday
