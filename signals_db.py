"""
DB layer for active_signals: watchlists, watch_list nodes, open_positions,
pending_buys (trailing-buy lifecycle), and trade_log.
"""
import json
import sqlite3
from datetime import datetime

import strategies
import signals_config as cfg


def _conn():
    c = sqlite3.connect(cfg.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def ensure_tables():
    with _conn() as c:
        # watchlists table — named profiles, one is_active at a time
        c.execute("""
            CREATE TABLE IF NOT EXISTS watchlists (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT UNIQUE NOT NULL,
                is_active  INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        c.execute("INSERT OR IGNORE INTO watchlists (name, is_active) VALUES ('main', 1)")
        if not c.execute("SELECT 1 FROM watchlists WHERE is_active=1").fetchone():
            c.execute("UPDATE watchlists SET is_active=1 WHERE name='main'")
        c.commit()

        main_id = c.execute("SELECT id FROM watchlists WHERE name='main'").fetchone()[0]

        # watch_list: create fresh or migrate from old single-list schema
        wl_cols = {r[1] for r in c.execute("PRAGMA table_info(watch_list)").fetchall()}
        if not wl_cols:
            c.execute(f"""
                CREATE TABLE watch_list (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    watchlist_id      INTEGER NOT NULL DEFAULT {main_id} REFERENCES watchlists(id),
                    mode              TEXT NOT NULL DEFAULT 'live',
                    ticker            TEXT NOT NULL,
                    strategy          TEXT NOT NULL,
                    version           TEXT NOT NULL,
                    window            INTEGER NOT NULL,
                    take_profit       INTEGER,
                    stop_loss         INTEGER NOT NULL,
                    max_hold_hours    INTEGER NOT NULL,
                    z_score_threshold REAL NOT NULL DEFAULT 2.0,
                    label             TEXT DEFAULT '',
                    added_at          TEXT DEFAULT (datetime('now')),
                    UNIQUE(watchlist_id, ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours)
                )
            """)
        elif 'watchlist_id' not in wl_cols:
            # migrate: recreate table with watchlist_id + mode + updated UNIQUE constraint
            c.executescript(f"""
                CREATE TABLE watch_list_new (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    watchlist_id      INTEGER NOT NULL DEFAULT {main_id},
                    mode              TEXT NOT NULL DEFAULT 'live',
                    ticker            TEXT NOT NULL,
                    strategy          TEXT NOT NULL,
                    version           TEXT NOT NULL,
                    window            INTEGER NOT NULL,
                    take_profit       INTEGER,
                    stop_loss         INTEGER NOT NULL,
                    max_hold_hours    INTEGER NOT NULL,
                    z_score_threshold REAL NOT NULL DEFAULT 2.0,
                    label             TEXT DEFAULT '',
                    added_at          TEXT DEFAULT (datetime('now')),
                    UNIQUE(watchlist_id, ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours)
                );
                INSERT INTO watch_list_new
                    (watchlist_id, mode, ticker, strategy, version, window, take_profit, stop_loss,
                     max_hold_hours, z_score_threshold, label, added_at)
                SELECT {main_id}, 'live', ticker, strategy, version, window, take_profit, stop_loss,
                       max_hold_hours, COALESCE(z_score_threshold, 2.0), label, added_at
                FROM watch_list;
                DROP TABLE watch_list;
                ALTER TABLE watch_list_new RENAME TO watch_list;
            """)
        else:
            if 'mode' not in wl_cols:
                c.execute("ALTER TABLE watch_list ADD COLUMN mode TEXT NOT NULL DEFAULT 'live'")
            if 'z_score_threshold' not in wl_cols:
                c.execute("ALTER TABLE watch_list ADD COLUMN z_score_threshold REAL NOT NULL DEFAULT 2.0")

        wl_cols = {r[1] for r in c.execute("PRAGMA table_info(watch_list)").fetchall()}
        if 'trail_sell_pct' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN trail_sell_pct REAL")
        if 'fixed_sl' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN fixed_sl REAL")
        if 'trail_buy_pct' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN trail_buy_pct REAL")
        if 'arm_sell_pct' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN arm_sell_pct REAL")
        if 'cached_avg_vol_10d' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN cached_avg_vol_10d REAL")
        if 'account' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN account TEXT")
        if 'alpha' not in wl_cols:
            # snapshot of backtest_cache.alpha_vs_spy at add_node/backfill time, not live-joined
            # (that DB is trading_universe.db, a separate file from this live DB) -- see
            # scripts/backfill_watch_list_alpha.py to (re)populate after adding/changing nodes.
            c.execute("ALTER TABLE watch_list ADD COLUMN alpha REAL")

        # open_positions
        c.execute("""
            CREATE TABLE IF NOT EXISTS open_positions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker         TEXT NOT NULL,
                strategy       TEXT NOT NULL,
                version        TEXT NOT NULL,
                window         INTEGER NOT NULL,
                take_profit    INTEGER,
                stop_loss      INTEGER NOT NULL,
                max_hold_hours INTEGER NOT NULL,
                signal_price   REAL NOT NULL,
                signal_time    TEXT NOT NULL,
                entry_price    REAL NOT NULL,
                entry_time     TEXT NOT NULL,
                trade_log_id   INTEGER
            )
        """)
        op_cols = {r[1] for r in c.execute("PRAGMA table_info(open_positions)").fetchall()}
        if 'trade_log_id' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN trade_log_id INTEGER")
        if 'trail_state' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN trail_state TEXT")
        if 'trail_sell_pct' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN trail_sell_pct REAL")
        if 'fixed_sl' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN fixed_sl REAL")
        if 'trail_buy_pct' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN trail_buy_pct REAL")
        if 'arm_sell_pct' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN arm_sell_pct REAL")
        if 'shares' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN shares REAL")
        if 'account' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN account TEXT")
        if 'broker_stop_price' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN broker_stop_price REAL")

        # trade_log
        c.execute("""
            CREATE TABLE IF NOT EXISTS trade_log (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker              TEXT NOT NULL,
                strategy            TEXT NOT NULL,
                version             TEXT NOT NULL,
                window              INTEGER NOT NULL,
                take_profit         INTEGER,
                stop_loss           INTEGER NOT NULL,
                max_hold_hours      INTEGER NOT NULL,
                signal_price        REAL NOT NULL,
                signal_time         TEXT NOT NULL,
                entry_price         REAL NOT NULL,
                entry_time          TEXT NOT NULL,
                entry_drift_pct     REAL NOT NULL,
                exit_signal_price   REAL,
                exit_price          REAL,
                exit_time           TEXT,
                exit_drift_pct      REAL,
                pnl_pct             REAL,
                exit_reason         TEXT,
                arm_sell_pct        REAL
            )
        """)
        tl_cols = {r[1] for r in c.execute("PRAGMA table_info(trade_log)").fetchall()}
        if 'arm_sell_pct' not in tl_cols:
            c.execute("ALTER TABLE trade_log ADD COLUMN arm_sell_pct REAL")
        if 'shares' not in tl_cols:
            c.execute("ALTER TABLE trade_log ADD COLUMN shares REAL")
        if 'account' not in tl_cols:
            c.execute("ALTER TABLE trade_log ADD COLUMN account TEXT")

        # pending_buys -- tracks a trailing-buy order from BUY alert until Executed/Skipped
        # is confirmed, so a stalled broker-side fill can be reminded on (mirrors trail_state
        # on open_positions for the sell side, which has no equivalent pre-fill row to hang state off).
        c.execute("""
            CREATE TABLE IF NOT EXISTS pending_buys (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker            TEXT NOT NULL,
                node_json         TEXT NOT NULL,
                signal_price      REAL NOT NULL,
                signal_time       TEXT NOT NULL,
                order_placed      INTEGER NOT NULL DEFAULT 0,
                reminder_channel  TEXT,
                reminder_ts       TEXT,
                reminder_count    INTEGER NOT NULL DEFAULT 0,
                last_reminder_at  TEXT NOT NULL,
                created_at        TEXT NOT NULL
            )
        """)
        pb_cols = [r[1] for r in c.execute("PRAGMA table_info(pending_buys)")]
        if 'order_placed' not in pb_cols:
            c.execute("ALTER TABLE pending_buys ADD COLUMN order_placed INTEGER NOT NULL DEFAULT 0")
        c.commit()


# ---------------------------------------------------------------------------
# Watch list CRUD
# ---------------------------------------------------------------------------

def get_watchlists():
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM watchlists ORDER BY name"
        ).fetchall()]


def get_active_watchlist_id():
    with _conn() as c:
        row = c.execute("SELECT id FROM watchlists WHERE is_active=1").fetchone()
        if row:
            return row[0]
        row = c.execute("SELECT id FROM watchlists ORDER BY id LIMIT 1").fetchone()
        return row[0] if row else None


def create_watchlist(name):
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO watchlists (name, is_active) VALUES (?, 0)", (name,))
        c.commit()
        return c.execute("SELECT id FROM watchlists WHERE name = ?", (name,)).fetchone()[0]


def delete_watchlist(watchlist_id):
    with _conn() as c:
        c.execute("DELETE FROM watch_list WHERE watchlist_id = ?", (watchlist_id,))
        c.execute("DELETE FROM watchlists WHERE id = ? AND is_active = 0", (watchlist_id,))
        c.commit()


def set_active_watchlist(watchlist_id):
    with _conn() as c:
        c.execute("UPDATE watchlists SET is_active = 0")
        c.execute("UPDATE watchlists SET is_active = 1 WHERE id = ?", (watchlist_id,))
        c.commit()


def get_watchlist(watchlist_id=None):
    with _conn() as c:
        if watchlist_id is None:
            watchlist_id = get_active_watchlist_id()
        if watchlist_id is None:
            return []
        return [dict(r) for r in c.execute(
            "SELECT * FROM watch_list WHERE watchlist_id = ? ORDER BY ticker, id",
            (watchlist_id,)
        ).fetchall()]


def _config_fixed_stop_loss():
    try:
        with open(cfg.CONFIG_PATH) as f:
            return float(json.load(f).get("execution", {}).get("fixed_stop_loss", 0))
    except Exception:
        return 0.0


# TrailingBothZScoreBreakout's static per-run exit trail % for legacy v1.10/v2.10/v2.13-17
# nodes — this constant lived in config.execution.trail_pct at backfill time, not in any
# swept column, so it can't be recovered from the node's own row; hardcode the known mapping
# (see docs/design.md "Version Changelog"). v1.10/v2.10 ran at the default 3%.
_LEGACY_TRAILING_BOTH_TRAIL_PCT = {'v2.13': 1.0, 'v2.14': 2.0, 'v2.15': 3.0, 'v2.16': 4.0, 'v2.17': 5.0}


def _tp_or_arm_pct(row):
    """take_profit is a real take-profit % for most strategies, but for
    TrailingBothZScoreBreakout it's the arm-sell threshold, stored in arm_sell_pct
    instead (take_profit is NULL on those rows)."""
    if row['strategy'] == 'TrailingBothZScoreBreakout':
        return row['arm_sell_pct']
    return row['take_profit']


def _is_trailing_buy(node):
    buy_axis_col, _ = strategies.resolve_axis_columns(node['strategy'])
    return buy_axis_col == 'trail_buy_pct'


def add_node(ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
             label='', z_score_threshold=2.0, watchlist_id=None, mode='live',
             trail_buy_pct=None, trail_pct=None):
    """trail_buy_pct/trail_pct: pass the real values directly for v3.x nodes (where
    backtest_cache has real named columns). Omit both for legacy v1.x/v2.x nodes —
    falls back to reinterpreting stop_loss the way it's always meant for the 4
    trailing strategies (see docs/design.md 'Grid axis meaning by strategy').
    For v3.x trailing-both/trailing-exit nodes, the stop_loss arg is not a real
    swept value (backtest_cache stores config.execution.fixed_stop_loss there,
    a constant) — pass whatever backtest_cache's stop_loss column shows, it's vestigial."""
    if watchlist_id is None:
        watchlist_id = get_active_watchlist_id()
    if strategies.uses_fixed_sl(strategy):
        fixed_sl = _config_fixed_stop_loss()
        if trail_buy_pct is None and trail_pct is None:
            sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy)
            if sl_axis_col == 'trail_buy_pct':
                stored_trail_buy_pct = float(stop_loss)
                stored_trail_sell_pct = (_LEGACY_TRAILING_BOTH_TRAIL_PCT.get(version, 3.0)
                                          if fourth_axis_col == 'trail_pct' else 0.0)
            else:
                stored_trail_buy_pct = 0.0
                stored_trail_sell_pct = float(stop_loss)
        else:
            # v3.x explicit pass — real values, validate against the strategy's schema.
            for w in strategies.validate_axis_values(strategy, trail_buy_pct, trail_pct):
                print(f"WARNING add_node({ticker}, {strategy}, {version}): {w}")
            stored_trail_sell_pct = trail_pct if trail_pct is not None else 0.0
            stored_trail_buy_pct = trail_buy_pct if trail_buy_pct is not None else 0.0
    else:
        # Strategy doesn't use trailing axes at all (e.g. bar-close ZScoreBreakout) —
        # flag if the caller passed either anyway, since it'll silently do nothing.
        for w in strategies.validate_axis_values(strategy, trail_buy_pct, trail_pct):
            print(f"WARNING add_node({ticker}, {strategy}, {version}): {w}")
        fixed_sl = None
        stored_trail_sell_pct = None
        stored_trail_buy_pct = None

    # take_profit is a real take-profit exit for most strategies, but for
    # TrailingBothZScoreBreakout it's actually the arm-sell threshold — store it
    # in arm_sell_pct instead so take_profit never means two different things.
    if strategy == 'TrailingBothZScoreBreakout':
        stored_take_profit = None
        stored_arm_sell_pct = float(take_profit)
    else:
        stored_take_profit = int(take_profit)
        stored_arm_sell_pct = None

    with _conn() as c:
        c.execute("""
            INSERT OR IGNORE INTO watch_list
                (watchlist_id, mode, ticker, strategy, version, window, take_profit,
                 stop_loss, max_hold_hours, label, z_score_threshold, trail_sell_pct, fixed_sl,
                 trail_buy_pct, arm_sell_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (watchlist_id, mode, ticker, strategy, version, int(window), stored_take_profit,
              int(stop_loss), int(max_hold_hours), label, float(z_score_threshold),
              stored_trail_sell_pct, fixed_sl, stored_trail_buy_pct, stored_arm_sell_pct))
        c.commit()


def remove_node(watch_id):
    with _conn() as c:
        c.execute("DELETE FROM watch_list WHERE id = ?", (watch_id,))
        c.commit()


def set_node_mode(watch_id, mode):
    with _conn() as c:
        c.execute("UPDATE watch_list SET mode = ? WHERE id = ?", (mode, watch_id))
        c.commit()


def label_node(watch_id, label):
    with _conn() as c:
        c.execute("UPDATE watch_list SET label = ? WHERE id = ?", (label, watch_id))
        c.commit()


# ---------------------------------------------------------------------------
# Open positions CRUD
# ---------------------------------------------------------------------------

def get_open_positions():
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM open_positions ORDER BY entry_time"
        ).fetchall()]
    for r in rows:
        r['trail_state'] = json.loads(r['trail_state']) if r.get('trail_state') else {}
    return rows


def get_held_tickers():
    """Single source of truth for 'is this ticker already held' -- use this instead of
    re-deriving a ticker set from get_open_positions() at each call site. A prior version
    of this exact gap (one code path filtered on it, another didn't) caused a real
    spurious-BUY-alert bug on 2026-07-08; a second, separate instance of the same gap
    (send_reference_report never filtered at all) was found 2026-07-09."""
    return {p['ticker'] for p in get_open_positions()}


_PENDING_BUY_NODE_KEYS = ('ticker', 'strategy', 'version', 'window', 'take_profit', 'stop_loss',
                          'max_hold_hours', 'label', 'trail_sell_pct', 'fixed_sl', 'trail_buy_pct',
                          'arm_sell_pct', 'account')


def add_pending_buy(node, sig, channel, ts):
    node_subset = {k: node.get(k) for k in _PENDING_BUY_NODE_KEYS}
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _conn() as c:
        c.execute(
            "INSERT INTO pending_buys (ticker, node_json, signal_price, signal_time, "
            "reminder_channel, reminder_ts, reminder_count, last_reminder_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (node['ticker'], json.dumps(node_subset), sig['current_price'],
             sig['last_bar'].strftime('%Y-%m-%d %H:%M:%S'), channel, ts, now_str, now_str),
        )
        c.commit()


def get_pending_buys():
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute("SELECT * FROM pending_buys").fetchall()]
    for r in rows:
        r['node'] = json.loads(r['node_json'])
    return rows


def clear_pending_buy(ticker):
    with _conn() as c:
        c.execute("DELETE FROM pending_buys WHERE ticker = ?", (ticker,))
        c.commit()


def mark_pending_buy_placed(ticker):
    """Order confirmed resting at the broker -- stops the 'is it placed' nag, but
    doesn't open a position (mirrors trail_state.order_placed on the sell side: a
    placed order still needs a real fill before anything is actually held).
    Resets reminder_count/last_reminder_at so the fill-confirmation phase gets
    its own reminder numbering (#1, #2, ...) instead of continuing the placement
    phase's count -- the two are different questions ('is it placed?' vs 'did it
    fill?') and sharing one counter across them reads as a lie about how many
    times you've actually been asked about the fill."""
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _conn() as c:
        c.execute(
            "UPDATE pending_buys SET order_placed=1, reminder_count=0, last_reminder_at=? WHERE ticker = ?",
            (now_str, ticker),
        )
        c.commit()


def update_pending_buy_reminder(pending_id, channel, ts, reminder_count):
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _conn() as c:
        c.execute(
            "UPDATE pending_buys SET reminder_channel=?, reminder_ts=?, reminder_count=?, last_reminder_at=? "
            "WHERE id=?",
            (channel, ts, reminder_count, now_str, pending_id),
        )
        c.commit()


def update_position_trail_state(position_id, state):
    with _conn() as c:
        c.execute("UPDATE open_positions SET trail_state = ? WHERE id = ?",
                  (json.dumps(state), position_id))


def closed_today(ticker):
    """True if this ticker had a trade_log exit today -- IRA/SEP cash accounts can't
    reuse that capital until T+1 settlement, so a same-day re-buy needs a warning."""
    today = datetime.now().strftime('%Y-%m-%d')
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM trade_log WHERE ticker = ? AND exit_time LIKE ? LIMIT 1",
            (ticker, f"{today}%"),
        ).fetchone()
    return row is not None


def open_position(node, signal_price, signal_time, entry_price, entry_time, shares=None):
    with _conn() as c:
        existing = c.execute(
            "SELECT id FROM open_positions WHERE ticker=? AND window=?",
            (node['ticker'], int(node['window']))
        ).fetchone()
        if existing:
            print(f"  [warn] position already open for {node['ticker']} w={node['window']} — skipping duplicate")
            return
        sig_time_str   = signal_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(signal_time, 'strftime') else signal_time
        entry_time_str = entry_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(entry_time, 'strftime') else entry_time
        trade_log_id = log_trade_entry(node, signal_price, signal_time, entry_price, entry_time, shares)
        tp = node.get('take_profit')
        c.execute("""
            INSERT INTO open_positions
                (ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
                 signal_price, signal_time, entry_price, entry_time, trade_log_id,
                 trail_sell_pct, fixed_sl, trail_buy_pct, arm_sell_pct, shares, account)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            node['ticker'], node['strategy'], node['version'],
            int(node['window']), int(tp) if tp is not None else None, int(node['stop_loss']),
            int(node['max_hold_hours']),
            float(signal_price), sig_time_str,
            float(entry_price), entry_time_str, trade_log_id,
            node.get('trail_sell_pct'), node.get('fixed_sl'), node.get('trail_buy_pct'),
            node.get('arm_sell_pct'), float(shares) if shares is not None else None,
            node.get('account'),
        ))
        c.commit()


def set_broker_stop_price(ticker, broker_stop_price):
    with _conn() as c:
        c.execute(
            "UPDATE open_positions SET broker_stop_price = ? WHERE ticker = ?",
            (float(broker_stop_price), ticker)
        )
        c.commit()


def close_position(position_id, exit_signal_price=None, exit_price=None, exit_time=None, exit_reason=None):
    with _conn() as c:
        if exit_price is not None:
            row = c.execute(
                "SELECT trade_log_id, entry_price FROM open_positions WHERE id = ?", (position_id,)
            ).fetchone()
            if row and row[0]:
                log_trade_exit(row[0], exit_signal_price, exit_price, exit_time, exit_reason, row[1])
        c.execute("DELETE FROM open_positions WHERE id = ?", (position_id,))
        c.commit()


def log_trade_entry(node, signal_price, signal_time, entry_price, entry_time, shares=None):
    sig_time_str   = signal_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(signal_time, 'strftime') else signal_time
    entry_time_str = entry_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(entry_time, 'strftime') else entry_time
    entry_drift    = (entry_price - signal_price) / signal_price * 100
    tp = node.get('take_profit')
    with _conn() as c:
        c.execute("""
            INSERT INTO trade_log
                (ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
                 signal_price, signal_time, entry_price, entry_time, entry_drift_pct, arm_sell_pct, shares, account)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            node['ticker'], node['strategy'], node['version'],
            int(node['window']), int(tp) if tp is not None else None, int(node['stop_loss']),
            int(node['max_hold_hours']),
            float(signal_price), sig_time_str,
            float(entry_price), entry_time_str, entry_drift, node.get('arm_sell_pct'),
            float(shares) if shares is not None else None, node.get('account'),
        ))
        c.commit()
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def log_trade_exit(trade_id, exit_signal_price, exit_price, exit_time, exit_reason, entry_price):
    exit_time_str = exit_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(exit_time, 'strftime') else exit_time
    exit_drift    = (exit_price - exit_signal_price) / exit_signal_price * 100
    pnl           = (exit_price - entry_price) / entry_price * 100
    with _conn() as c:
        c.execute("""
            UPDATE trade_log SET
                exit_signal_price = ?, exit_price = ?, exit_time = ?,
                exit_drift_pct = ?, pnl_pct = ?, exit_reason = ?
            WHERE id = ?
        """, (float(exit_signal_price), float(exit_price), exit_time_str,
              exit_drift, pnl, exit_reason, trade_id))
        c.commit()
