#!/usr/bin/env python3
"""
Active signal monitor. Polls cached price data and fires BUY/SELL notifications.

Usage:
    python active_signals.py          # run signal loop
    python active_signals.py list     # show watch list
    python active_signals.py add      # add a node interactively
    python active_signals.py remove   # remove a node interactively
    python active_signals.py positions  # show open positions

Environment (Socket Mode — interactive buttons):
    SLACK_BOT_TOKEN     — bot OAuth token (xoxb-...)
    SLACK_APP_TOKEN     — app-level token (xapp-...) for Socket Mode
    SLACK_CHANNEL       — channel to post to (e.g. #trading)

Environment (Webhook fallback — fire-and-forget, no buttons):
    SLACK_WEBHOOK_URL   — incoming webhook URL

    SIGNAL_POLL_SECS    — poll interval in seconds (default 300)
"""

import os
import sys
import json
import time
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import contextlib
import requests
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

matplotlib.rcParams.update({
    'figure.facecolor':  '#1e1f22',
    'axes.facecolor':    '#1e1f22',
    'savefig.facecolor': '#1e1f22',
    'text.color':        '#dbdee1',
    'axes.labelcolor':   '#dbdee1',
    'axes.edgecolor':    '#4e5058',
    'xtick.color':       '#dbdee1',
    'ytick.color':       '#dbdee1',
    'grid.color':        '#3f4147',
    'legend.facecolor':  '#2b2d31',
    'legend.edgecolor':  '#4e5058',
    'legend.labelcolor': '#dbdee1',
})
from io import BytesIO
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
import yfinance as yf
from data_manager import fetch_live_data_smart
import strategies

load_dotenv()

DB_PATH          = Path(os.environ.get("TRADING_DB_PATH", "./cache/trading_live.db"))
RESEARCH_DB_PATH = Path("./cache/trading_universe.db")
CACHE_DIR   = Path("./cache")
CONFIG_PATH = Path("./config.json")
POLL_SECS  = int(os.environ.get("SIGNAL_POLL_SECS", 300))
SLACK_HOOK = os.environ.get("SLACK_WEBHOOK_URL", "")

LOG_DIR = Path("./logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
HUMAN_LOG_PATH   = LOG_DIR / "active_signals.log"
VERBOSE_LOG_PATH = LOG_DIR / "active_signals_verbose.log"
HEARTBEAT_PATH   = CACHE_DIR / "active_signals_heartbeat.txt"


class _Tee:
    """Mirrors writes to multiple streams — used to log to a file without losing
    the live console output when running `active_signals.py run` interactively."""
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            s.write(data)

    def flush(self):
        for s in self._streams:
            s.flush()

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
SLACK_CHANNEL   = os.environ.get("SLACK_CHANNEL", "")
SOCKET_MODE     = bool(SLACK_BOT_TOKEN and SLACK_APP_TOKEN and SLACK_CHANNEL)

SLACK_CHANNEL_ID = ""
SIM_MODE         = os.environ.get("SIM_MODE") == "1"
SIM_SCENARIO     = os.environ.get("SIM_SCENARIO", "")
# Interactive buttons/reminders require the process's own Socket Mode connection to be
# the one Slack delivers the click to. The sim never starts a SocketModeHandler (only
# run_loop() does), so if it rendered real buttons, a click would be delivered to
# whichever *other* process (the live daemon) happens to be connected — using sim data
# against the live DB. SIM_MODE forces the plain-text/typed-input fallback instead.
INTERACTIVE      = SOCKET_MODE and not SIM_MODE

if SOCKET_MODE:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    bolt_app = App(token=SLACK_BOT_TOKEN)
else:
    bolt_app = None


def _resolve_channel_id():
    global SLACK_CHANNEL_ID
    if SLACK_CHANNEL_ID or not SOCKET_MODE:
        return
    try:
        r = bolt_app.client.chat_postMessage(channel=SLACK_CHANNEL, text="Signal monitor online.")
        SLACK_CHANNEL_ID = r['channel']
    except Exception as e:
        print(f"  [slack] could not resolve channel ID: {e}")


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _conn():
    c = sqlite3.connect(DB_PATH)
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
        with open(CONFIG_PATH) as f:
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


# ---------------------------------------------------------------------------
# Price data
# ---------------------------------------------------------------------------

def _load_cache(ticker):
    path = CACHE_DIR / f"{ticker}_1h.csv"
    if not path.exists():
        return None, None
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    df_daily = df.resample('D').last().dropna()
    return df, df_daily


def _current_price(ticker):
    df, _ = _load_cache(ticker)
    if df is None:
        return None, None
    prices = df['Close'].dropna()
    return float(prices.iloc[-1]), df.index[-1]


# ---------------------------------------------------------------------------
# Signal computation
# ---------------------------------------------------------------------------

def _hurst_adf(ticker, df_hourly):
    hurst = None
    try:
        with sqlite3.connect(RESEARCH_DB_PATH) as c:
            row = c.execute(
                "SELECT hurst FROM hurst_cache WHERE ticker = ? ORDER BY timestamp DESC LIMIT 1",
                (ticker,)
            ).fetchone()
        if row:
            hurst = row[0]
    except Exception:
        pass

    adf_p = None
    try:
        from statsmodels.tsa.stattools import adfuller
        close = df_hourly['Close'].dropna()
        n = min(200, len(close))
        if n >= 20:
            adf_p = adfuller(close.iloc[-n:], maxlag=1, autolag=None)[1]
    except Exception:
        pass

    return hurst, adf_p


def compute_buy_signal(node, as_of=None, price_override=None, df_hourly_override=None, df_daily_override=None):
    ticker = node['ticker']
    window = int(node['window'])

    strategy_cls = getattr(strategies, node['strategy'], None)
    if strategy_cls is None:
        return None

    if df_hourly_override is not None:
        df_hourly, df_daily = df_hourly_override, df_daily_override
    else:
        df_hourly, df_daily = _load_cache(ticker)
    if df_hourly is None or len(df_daily) < window:
        return None

    z_thresh = float(node.get('z_score_threshold', 2.0))
    strat = strategy_cls(window=window, z_score_threshold=z_thresh)
    today = (as_of if as_of is not None else pd.Timestamp.now()).normalize()
    indicators = strat.generate_daily_indicators(df_daily[df_daily.index < today])
    if indicators.empty:
        return None

    last_row      = indicators.iloc[-1]
    close_series  = df_hourly['Close'].dropna()
    last_bar      = close_series.index[-1]
    daily_closes = df_daily['Close'].dropna()
    prev_close = float(daily_closes.iloc[-1]) if not daily_closes.empty else close_series.iloc[-1]
    if price_override is not None:
        current_price = price_override
    else:
        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                hist = ex.submit(lambda: yf.Ticker(ticker).history(period='1d', interval='1m', prepost=True)).result(timeout=10)
            current_price = float(hist['Close'].iloc[-1]) if not hist.empty else close_series.iloc[-1]
        except Exception:
            current_price = close_series.iloc[-1]
    sma           = last_row['SMA']
    std           = last_row['Std']
    hurst, adf_p  = _hurst_adf(ticker, df_hourly)

    signal_ctx = {
        'current_price': current_price,
        'low':           current_price,  # no true intrabar low available live; best proxy
        'sma':           sma,
        'std':           std,
        'trend':         last_row['Trend_Filter'] if 'Trend_Filter' in indicators.columns else None,
    }

    return {
        'ticker':        ticker,
        'window':        window,
        'current_price': current_price,
        'prev_close':    prev_close,
        'sma':           sma,
        'std':           std,
        'lower_band':    sma - z_thresh * std,
        'z_score':       (current_price - sma) / std,
        'signal':        strat.check_signal(signal_ctx),
        'last_bar':      last_bar,
        'last_daily_bar': indicators.index[-1],
        'hurst':         hurst,
        'adf_p':         adf_p,
    }


def _bars_held(df_hourly, signal_time):
    """Trading-hour bars elapsed since the signal bar — mirrors the kernels'
    `held += 1` per hourly row (cached data is market-hours-only), unlike
    wall-clock hours which run ~3.5x faster than trading hours."""
    if df_hourly is None or df_hourly.empty:
        return 0
    return int((df_hourly.index > signal_time).sum())


def check_sell_condition(pos, current_price, now, at_bar_close=True, low=None, high=None, df_hourly=None):
    strategy_cls = getattr(strategies, pos['strategy'], None)
    if strategy_cls is None:
        return None, None, False
    signal_time = datetime.strptime(pos['signal_time'], '%Y-%m-%d %H:%M:%S')
    if df_hourly is None:
        df_hourly, _ = _load_cache(pos['ticker'])
    hours_held = _bars_held(df_hourly, signal_time)
    # For v1.8/v1.9/v1.10 the swept 'stop_loss' column holds trail_pct/trail_buy_pct,
    # not the real fixed SL — that comes from the node's fixed_sl column instead.
    if strategies.uses_fixed_sl(pos['strategy']):
        real_sl_pct = pos.get('fixed_sl') or 0.0
        trail_pct   = (pos.get('trail_sell_pct') or 3.0) / 100.0
    else:
        real_sl_pct = pos['stop_loss']
        trail_pct   = 0.03
    tp_pct     = _tp_or_arm_pct(pos)
    strat      = strategy_cls(window=pos['window'], trail_pct=trail_pct)
    old_state  = pos.get('trail_state', {})
    reason, price, new_state = strat.check_exit({
        'current_price':     current_price,
        # Real bar Low/High when this call represents an actual closed hourly bar;
        # otherwise current_price is the best available proxy for a mid-bar poll.
        'low':               low if low is not None else current_price,
        'high':              high if high is not None else current_price,
        'entry_price':       pos['entry_price'],
        'take_profit':       tp_pct / 100.0,
        'stop_loss':         real_sl_pct / 100.0,
        'max_hours_to_hold': pos['max_hold_hours'],
        'hours_held':        hours_held,
        'at_bar_close':      at_bar_close,
        'state':             old_state,
    })
    just_activated_trailing = bool(new_state.get('trailing')) and not old_state.get('trailing')
    if reason in ('WIN', 'LOSS'):
        reason = 'TRAIL'
    if new_state != old_state:
        update_position_trail_state(pos['id'], new_state)
    return reason, price, just_activated_trailing


# ---------------------------------------------------------------------------
# Chart generation
# ---------------------------------------------------------------------------

def _upload_chart(buf: BytesIO, filename: str, title: str):
    if not SOCKET_MODE or not SLACK_CHANNEL_ID:
        return
    try:
        bolt_app.client.files_upload_v2(
            channel=SLACK_CHANNEL_ID,
            file=buf,
            filename=filename,
            title=title,
        )
    except Exception as e:
        print(f"  [chart] upload failed: {e}")


def _chart_buy(node, sig) -> BytesIO | None:
    ticker = sig['ticker']
    window = int(node['window'])
    df_hourly, df_daily = _load_cache(ticker)
    if df_hourly is None:
        return None

    today        = pd.Timestamp.now().normalize()
    trading_days = pd.Series(df_hourly.index.normalize()).unique()
    cutoff       = trading_days[-30] if len(trading_days) >= 30 else trading_days[0]
    df_plot      = df_hourly[df_hourly.index.normalize() >= cutoff]['Close'].dropna()
    strat        = getattr(strategies, node['strategy'])(window=window)
    df_daily_in  = df_daily[df_daily.index < today]
    indicators  = strat.generate_daily_indicators(df_daily_in)

    z_thresh  = float(node.get('z_score_threshold', 2.0))
    sma_h     = indicators['SMA'].reindex(df_plot.index, method='ffill')
    std_h     = indicators['Std'].reindex(df_plot.index, method='ffill')
    upper_h   = sma_h + 2 * std_h
    lower_h   = sma_h - 2 * std_h
    trigger_h = sma_h - z_thresh * std_h

    # Positional x-axis (bar index, not calendar time) so weekend/overnight gaps
    # don't stretch out as flat empty segments.
    x = np.arange(len(df_plot))

    def _pos(ts):
        return df_plot.index.get_indexer([ts], method='nearest')[0]

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(x, df_plot.values, color='#4c9be8', linewidth=1, label='Price')
    ax.plot(x, sma_h.values, color='#f0a500', linewidth=1, label=f'SMA({window})')
    ax.fill_between(x, lower_h.values, upper_h.values, alpha=0.12, color='#f0a500')
    ax.plot(x, lower_h.values, color='#f0a500', linewidth=0.6, linestyle='--')
    ax.plot(x, trigger_h.values, color='#e74c3c', linewidth=1, linestyle='--', label=f'Trigger line (z={z_thresh:g})')

    last_pos = _pos(sig['last_bar'])
    ax.axvline(last_pos, color='#2ecc71', linewidth=1.5, linestyle='--', alpha=0.8)
    ax.scatter([last_pos], [sig['current_price']], color='#2ecc71', s=60, zorder=5)

    if len(df_daily_in) >= window and df_daily_in.index[-window] >= df_plot.index[0]:
        w_pos = _pos(df_daily_in.index[-window])
        ax.axvline(w_pos, color='white', linewidth=1.3, linestyle=':', alpha=0.9, label=f'w{window} start')

    ax.set_xlim(-2, len(x) + 1)

    ax.axhline(sig['prev_close'], color='#dbdee1', linewidth=1, linestyle=':', alpha=0.7,
               label=f"Close ${sig['prev_close']:.2f}")
    ax.axhline(sig['current_price'], color='#2ecc71', linewidth=1, linestyle='--', alpha=0.8,
               label=f"Current ${sig['current_price']:.2f}")
    ax.axhline(sig['lower_band'], color='#e74c3c', linewidth=1.2, linestyle='-', alpha=0.9,
               label=f"Trigger ${sig['lower_band']:.2f}")

    pct_away = (sig['current_price'] - sig['lower_band']) / sig['lower_band'] * 100
    fig.suptitle(f"{ticker}   trigger ${sig['lower_band']:.2f}  ({pct_away:+.1f}%)",
                 fontsize=15, fontweight='bold', color='#f0a500', y=0.98)
    ax.set_title(f"w{window} z{z_thresh:g} arm{_tp_or_arm_pct(node)} sl{node['stop_loss'] + 1}",
                 fontsize=9, color='#9aa0a6')

    tick_step = max(len(x) // 10, 1)
    tick_pos  = x[::tick_step]
    ax.set_xticks(tick_pos)
    ax.set_xticklabels([df_plot.index[i].strftime('%m/%d') for i in tick_pos])

    ax.yaxis.tick_right()
    ax.yaxis.set_label_position('right')
    ax.legend(fontsize=8, loc='upper right')
    fig.tight_layout(rect=[0, 0, 1, 0.93])

    buf = BytesIO()
    fig.savefig(buf, format='png', dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf


def _chart_sell(pos, current_price) -> BytesIO | None:
    ticker = pos['ticker']
    window = int(pos['window'])
    df_hourly, df_daily = _load_cache(ticker)
    if df_hourly is None:
        return None

    today      = pd.Timestamp.now().normalize()
    cutoff     = df_hourly.index[-1] - pd.Timedelta(days=30)
    df_plot    = df_hourly[df_hourly.index >= cutoff]['Close'].dropna()
    strat      = getattr(strategies, pos['strategy'])(window=window)
    indicators = strat.generate_daily_indicators(df_daily[df_daily.index < today])

    sma_h   = indicators['SMA'].reindex(df_plot.index, method='ffill')
    std_h   = indicators['Std'].reindex(df_plot.index, method='ffill')
    upper_h = sma_h + 2 * std_h
    lower_h = sma_h - 2 * std_h

    ep            = pos['entry_price']
    arm_price     = ep * (1 + _tp_or_arm_pct(pos) / 100)
    schwab_sl_pct = pos['stop_loss'] + 1
    sl_price      = ep * (1 - schwab_sl_pct / 100)
    entry_time = datetime.strptime(pos['entry_time'], '%Y-%m-%d %H:%M:%S')
    pct        = (current_price - ep) / ep * 100

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(df_plot.index, df_plot.values, color='#4c9be8', linewidth=1, label='Price')
    ax.plot(sma_h.index, sma_h.values, color='#f0a500', linewidth=1, label=f'SMA({window})')
    ax.fill_between(df_plot.index, lower_h, upper_h, alpha=0.12, color='#f0a500')
    ax.axhline(arm_price, color='#2ecc71', linewidth=1, linestyle='--', label=f'Arm ${arm_price:.2f}')
    ax.axhline(sl_price, color='#e74c3c', linewidth=1, linestyle='--', label=f'SL ${sl_price:.2f}')
    ax.axhline(ep, color='white', linewidth=0.8, linestyle=':', alpha=0.6, label=f'Entry ${ep:.2f}')
    if entry_time in df_plot.index or df_plot.index[0] <= entry_time <= df_plot.index[-1]:
        ax.axvline(entry_time, color='#9b59b6', linewidth=1.2, linestyle='--', alpha=0.7)
    ax.set_title(f"{ticker}  SELL SIGNAL  |  P&L {pct:+.2f}%  |  window={window}", fontsize=11)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    ax.legend(fontsize=8)
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format='png', dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

def _post_message(text, blocks=None):
    """Returns (channel, ts) when posted via the Socket Mode client (None, None
    otherwise) so callers can track a message for later reminder/supersede."""
    if SIM_MODE:
        scenario_suffix = f" ({SIM_SCENARIO})" if SIM_SCENARIO else ""
        text = f"🧪 SIM{scenario_suffix} — {text}"
        if blocks:
            # A dedicated marker block, not a rewrite of the first block's text --
            # the prior approach only patched "header"-type blocks, so any message
            # built from "section" blocks (most of them) silently shipped with no
            # visible SIM tag at all, regardless of block composition.
            scenario_str = f": {SIM_SCENARIO}" if SIM_SCENARIO else ""
            header_marker = {"type": "context", "elements": [{"type": "mrkdwn", "text": f"🧪 *SIM MODE{scenario_str}*"}]}
            footer_marker = {"type": "context", "elements": [{"type": "mrkdwn", "text": "🧪 *SIM MODE END*"}]}
            blocks = [header_marker] + blocks + [footer_marker]
    if SOCKET_MODE:
        try:
            resp = bolt_app.client.chat_postMessage(channel=SLACK_CHANNEL, text=text, blocks=blocks)
            return resp['channel'], resp['ts']
        except Exception as e:
            print(f"  [slack error] {e}")
            return None, None
    elif SLACK_HOOK:
        payload = {'text': text}
        if blocks:
            payload['blocks'] = blocks
        try:
            r = requests.post(SLACK_HOOK, json=payload, timeout=5)
            if not r.ok:
                print(f"  [slack error] HTTP {r.status_code}")
        except Exception as e:
            print(f"  [slack error] {e}")
    return None, None


def _fields_block(fields: dict):
    return {"type": "section", "fields": [
        {"type": "mrkdwn", "text": f"*{k}:*\n{v}"} for k, v in fields.items()
    ]}


def _price_input_block():
    return {
        "type":     "input",
        "block_id": "price_block",
        "label":    {"type": "plain_text", "text": "Price"},
        "element":  {
            "type":               "number_input",
            "is_decimal_allowed": True,
            "action_id":          "price_input",
            "placeholder":        {"type": "plain_text", "text": "e.g. 123.45"},
        },
    }


def _shares_input_block(initial=None):
    element = {
        "type":               "number_input",
        "is_decimal_allowed": False,
        "action_id":          "shares_input",
        "placeholder":        {"type": "plain_text", "text": "e.g. 300"},
    }
    if initial is not None:
        element["initial_value"] = str(int(initial))
    return {
        "type":     "input",
        "block_id": "shares_block",
        "label":    {"type": "plain_text", "text": "Shares"},
        "element":  element,
    }


def _build_buy_blocks(node, sig):
    ticker    = sig['ticker']
    price     = sig['current_price']
    z         = sig['z_score']
    bar_str   = sig['last_bar'].strftime('%Y-%m-%d %H:%M')

    hurst_str = f"{sig['hurst']:.3f}" if sig.get('hurst') is not None else "n/a"
    adf_str   = f"{sig['adf_p']:.3f}" if sig.get('adf_p')  is not None else "n/a"

    hold_deadline = _add_trading_hours(sig['last_bar'], node['max_hold_hours'])
    deadline_str  = hold_deadline.strftime('%a %b %d %H:%M')

    target_notional = _last_sale_recovery(ticker)
    shares = int(target_notional // price)
    schwab_sl_pct   = node['stop_loss'] + 1
    schwab_sl_price = sig['lower_band'] * (1 - schwab_sl_pct / 100)

    # avg_vol_10d only changes when someone re-runs scripts/import_tickers.py (manual,
    # not on a cron) — a locked research DB (e.g. mid-migration) is worth falling back
    # on the last-cached value for rather than crashing the daemon over a stale-by-a-day
    # sizing number.
    avg_vol_10d = None
    try:
        with sqlite3.connect(RESEARCH_DB_PATH) as _c:
            _c.row_factory = sqlite3.Row
            vol_row = _c.execute("SELECT avg_vol_10d FROM tickers WHERE symbol=?", (ticker,)).fetchone()
        avg_vol_10d = vol_row['avg_vol_10d'] if vol_row else None
        if avg_vol_10d and node.get('id') is not None:
            with _conn() as _c:
                _c.execute("UPDATE watch_list SET cached_avg_vol_10d=? WHERE id=?", (avg_vol_10d, node['id']))
                _c.commit()
    except Exception as e:
        print(f"WARNING _build_buy_blocks({ticker}): tickers lookup failed ({e}), using cached avg_vol_10d")
        avg_vol_10d = node.get('cached_avg_vol_10d')
    max_notional = avg_vol_10d * price * 0.01 if avg_vol_10d else None
    max_shares = int(max_notional // price) if max_notional else None
    max_notional_str = f"  |  max `${max_notional/1000:.0f}k` / `{max_shares} shares` @ 1% vol" if max_notional else ""

    account = node.get('account') or 'unmapped'
    sl_axis_col, _ = strategies.resolve_axis_columns(node['strategy'])
    if sl_axis_col == 'trail_buy_pct':
        trail_buy_pct = node.get('trail_buy_pct') or 0.0
        entry_line = f"🟢 *{ticker}* — BUY — Trailing Buy {trail_buy_pct:.0f}% — trigger `${price:.2f}` — `{shares} shares` (~${target_notional/1000:.0f}k) — `{account}`{max_notional_str}"
    else:
        entry_line = f"🟢 *{ticker}* — BUY — Market — `${price:.2f}` — `{shares} shares` (~${target_notional/1000:.0f}k) — `{account}`{max_notional_str}"

    warning_line = ""
    if (node.get('account') or '').lower() != 'brokerage' and closed_today(ticker):
        warning_line = (
            f"\n⚠️🔁 *SAME DAY BUY WARNING:* {ticker} already sold today in a "
            f"{node.get('account', 'non-brokerage')} account — cash may not be settled (T+1). Confirm funds are available before entering."
        )

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"{entry_line}\n🔴 *{ticker}* — SELL ALL — Stop Loss — `${schwab_sl_price:.2f}` (-{schwab_sl_pct}% from trigger){warning_line}"}},
    ]

    trailing_buy = _is_trailing_buy(node)

    if INTERACTIVE:
        value = json.dumps({
            "type":         "buy",
            "node":         {k: node.get(k) for k in ('ticker', 'strategy', 'version', 'window',
                                                        'take_profit', 'stop_loss', 'max_hold_hours', 'label',
                                                        'trail_sell_pct', 'fixed_sl', 'trail_buy_pct', 'arm_sell_pct')},
            "signal_price": price,
            "signal_time":  sig['last_bar'].strftime('%Y-%m-%d %H:%M:%S'),
            "lower_band":   sig['lower_band'],
            "z_score":      z,
        })
        if trailing_buy:
            # No price ask -- the trailing-buy fill price isn't known at alert time
            # (broker tracks the bounce-above-running-low entry itself). Opens the
            # position immediately at the signal price so arm/SL/trail triggers are
            # live right away; the real fill price (when known) only feeds a
            # separate drag/drift stat later, it doesn't retroactively move triggers.
            blocks.append({
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Trailing Buy Order Placed"},
                     "style": "primary", "action_id": "trail_buy_order_placed", "value": value},
                    {"type": "button", "text": {"type": "plain_text", "text": "Skipped"},
                     "action_id": "buy_skipped", "value": value},
                ],
            })
        else:
            blocks.append({
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Executed"},
                     "style": "primary", "action_id": "buy_executed", "value": value},
                    {"type": "button", "text": {"type": "plain_text", "text": "Skipped"},
                     "action_id": "buy_skipped", "value": value},
                ],
            })
    elif trailing_buy:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": "No interactive buttons — confirm the trailing buy order is placed in the terminal running the daemon (fill price isn't known yet)."}
        ]})
    else:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": "No interactive buttons — type the execution price into the terminal running the daemon when filled."}
        ]})

    return blocks


def _build_sell_blocks(pos, reason, current_price, target_price):
    ticker = pos['ticker']
    ep     = pos['entry_price']
    pct    = (current_price - ep) / ep * 100

    if reason == 'TP':
        emoji   = "🟢"
        label   = "TAKE PROFIT"
        action  = f"Cancel Stop Loss order — Sell All (Market) @ `${current_price:.2f}`"
    elif reason == 'SL':
        emoji   = "🔴"
        label   = "STOP LOSS HIT"
        action  = f"Check account — Stop Loss order should have auto-filled @ `${target_price:.2f}`"
    elif reason == 'TRAIL':
        emoji   = "🟢"
        label   = "TRAILING STOP"
        action  = f"Cancel Stop Loss order — Sell All (Market), trailing stop triggered @ `${target_price:.2f}`"
    else:  # TIME
        emoji   = "🔶"
        label   = "TIME EXIT"
        action  = f"Change Stop Loss → Market Close order (exit by EOD)"

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": (
                f"{emoji} *{ticker}* — {label}\n"
                f"{action}\n"
                f"entry `${ep:.2f}`  |  current `${current_price:.2f}`  |  P&L `{pct:+.1f}%`"
            )}},
    ]

    if INTERACTIVE:
        value = json.dumps({
            "type":          "sell",
            "position_id":   pos['id'],
            "ticker":        ticker,
            "current_price": current_price,
            "entry_price":   ep,
            "reason":        reason,
        })
        blocks.append({
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Exited"},
                 "style": "primary", "action_id": "sell_exited", "value": value},
                {"type": "button", "text": {"type": "plain_text", "text": "Skipped"},
                 "action_id": "sell_skipped", "value": value},
            ],
        })
    else:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": "No interactive buttons — type the exit price into the terminal running the daemon when filled."}
        ]})

    return blocks


# ---------------------------------------------------------------------------
# Bolt handlers (Socket Mode only)
# ---------------------------------------------------------------------------

if SOCKET_MODE:

    @bolt_app.action("buy_executed")
    def handle_buy_executed(ack, body, client):
        ack()
        data    = json.loads(body['actions'][0]['value'])
        channel = body['channel']['id']
        ts      = body['message']['ts']
        client.views_open(
            trigger_id=body['trigger_id'],
            view={
                "type":             "modal",
                "callback_id":      "entry_price_submit",
                "private_metadata": json.dumps({"data": data, "channel": channel, "ts": ts}),
                "title":  {"type": "plain_text", "text": "Entry Price"},
                "submit": {"type": "plain_text", "text": "Confirm"},
                "close":  {"type": "plain_text", "text": "Cancel"},
                "blocks": [_price_input_block()],
            },
        )

    @bolt_app.action("trail_buy_order_placed")
    def handle_trail_buy_order_placed(ack, body, client):
        """Order resting at the broker -- no position yet (broker tracks the
        bounce-above-running-low entry itself, still no live state machine for
        it). Just flips pending_buys.order_placed=True (stops the 'is it placed'
        nag) and swaps to Filled/Cancelled buttons; open_position() only runs
        once a real fill is separately confirmed via handle_trail_buy_filled."""
        ack()
        data    = json.loads(body['actions'][0]['value'])
        channel = body['channel']['id']
        ts      = body['message']['ts']
        ticker  = data['node']['ticker']
        mark_pending_buy_placed(ticker)
        client.chat_update(
            channel=channel, ts=ts,
            text=f"BUY {ticker} — order placed, waiting for fill",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"*{ticker}* — trailing buy order placed, waiting for fill"}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Filled"},
                     "style": "primary", "action_id": "trail_buy_filled", "value": json.dumps(data)},
                    {"type": "button", "text": {"type": "plain_text", "text": "Cancelled"},
                     "action_id": "trail_buy_cancelled", "value": json.dumps(data)},
                ]},
            ],
        )

    @bolt_app.action("trail_buy_filled")
    def handle_trail_buy_filled(ack, body, client):
        ack()
        data    = json.loads(body['actions'][0]['value'])
        channel = body['channel']['id']
        ts      = body['message']['ts']
        client.views_open(
            trigger_id=body['trigger_id'],
            view={
                "type":             "modal",
                "callback_id":      "trail_buy_fill_price_submit",
                "private_metadata": json.dumps({"data": data, "channel": channel, "ts": ts}),
                "title":  {"type": "plain_text", "text": "Fill Price"},
                "submit": {"type": "plain_text", "text": "Confirm"},
                "close":  {"type": "plain_text", "text": "Cancel"},
                "blocks": [_price_input_block()],
            },
        )

    @bolt_app.action("trail_buy_cancelled")
    def handle_trail_buy_cancelled(ack, body, client):
        ack()
        data    = json.loads(body['actions'][0]['value'])
        channel = body['channel']['id']
        ts      = body['message']['ts']
        ticker  = data['node']['ticker']
        clear_pending_buy(ticker)
        client.chat_update(
            channel=channel, ts=ts,
            text=f"BUY {ticker} — order cancelled",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*BUY {ticker}* — trailing buy order cancelled, no position"}}],
        )

    @bolt_app.view("trail_buy_fill_price_submit")
    def handle_trail_buy_fill_price(ack, body, client):
        ack()
        meta         = json.loads(body['view']['private_metadata'])
        data         = meta['data']
        channel      = meta['channel']
        ts           = meta['ts']
        node         = data['node']
        signal_price = data['signal_price']
        signal_time  = datetime.strptime(data['signal_time'], '%Y-%m-%d %H:%M:%S')
        ticker       = node['ticker']

        fill_price = float(body['view']['state']['values']['price_block']['price_input']['value'])
        drift_pct  = (fill_price - signal_price) / signal_price * 100
        shares     = int(_last_sale_recovery(ticker) // fill_price)

        open_position(node, signal_price, signal_time, fill_price, datetime.now(), shares=shares)
        clear_pending_buy(ticker)

        note = f"${fill_price:.4f}  (drift: {drift_pct:+.2f}%)  {shares} shares"
        print(f"  Trailing buy filled via Slack: {ticker} at {note}")
        client.chat_update(
            channel=channel, ts=ts,
            text=f"BUY {ticker} — Filled at {note}",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*BUY {ticker}* — Filled at {note}"}}],
        )

    @bolt_app.action("buy_skipped")
    def handle_buy_skipped(ack, body, client):
        ack()
        data    = json.loads(body['actions'][0]['value'])
        channel = body['channel']['id']
        ts      = body['message']['ts']
        ticker  = data['node']['ticker']
        clear_pending_buy(ticker)
        client.chat_update(
            channel=channel, ts=ts,
            text=f"BUY {ticker} — Skipped",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*BUY {ticker}* — Skipped"}}],
        )

    @bolt_app.view("entry_price_submit")
    def handle_entry_price(ack, body, client):
        ack()
        meta         = json.loads(body['view']['private_metadata'])
        data         = meta['data']
        channel      = meta['channel']
        ts           = meta['ts']
        node         = data['node']
        signal_price = data['signal_price']
        signal_time  = datetime.strptime(data['signal_time'], '%Y-%m-%d %H:%M:%S')

        exec_price = float(body['view']['state']['values']['price_block']['price_input']['value'])
        drift_pct  = (exec_price - signal_price) / signal_price * 100
        now        = datetime.now()
        shares     = int(50_000 // exec_price)

        open_position(node, signal_price, signal_time, exec_price, now, shares=shares)

        ticker = node['ticker']
        clear_pending_buy(ticker)
        note   = f"${exec_price:.4f}  (drift: {drift_pct:+.2f}%)"
        print(f"  Position opened via Slack: {ticker} at {note}")
        client.chat_update(
            channel=channel, ts=ts,
            text=f"BUY {ticker} — Executed at {note}",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*BUY {ticker}* — Executed at {note}"}}],
        )

    @bolt_app.action("sell_exited")
    def handle_sell_exited(ack, body, client):
        ack()
        data    = json.loads(body['actions'][0]['value'])
        channel = body['channel']['id']
        ts      = body['message']['ts']
        client.views_open(
            trigger_id=body['trigger_id'],
            view={
                "type":             "modal",
                "callback_id":      "exit_price_submit",
                "private_metadata": json.dumps({"data": data, "channel": channel, "ts": ts}),
                "title":  {"type": "plain_text", "text": "Exit Price"},
                "submit": {"type": "plain_text", "text": "Confirm"},
                "close":  {"type": "plain_text", "text": "Cancel"},
                "blocks": [_price_input_block()],
            },
        )

    @bolt_app.action("sell_skipped")
    def handle_sell_skipped(ack, body, client):
        ack()
        data    = json.loads(body['actions'][0]['value'])
        channel = body['channel']['id']
        ts      = body['message']['ts']
        ticker  = data['ticker']
        position_id = data.get('position_id')
        pos = next((p for p in get_open_positions() if p['id'] == position_id), None)
        if pos:
            state = dict(pos.get('trail_state') or {})
            state.pop('exit_pending', None)
            update_position_trail_state(pos['id'], state)
        client.chat_update(
            channel=channel, ts=ts,
            text=f"SELL {ticker} — Skipped (position kept open)",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*SELL {ticker}* — Skipped (position kept open)"}}],
        )

    @bolt_app.action("trail_order_placed")
    def handle_trail_order_placed(ack, body, client):
        ack()
        data        = json.loads(body['actions'][0]['value'])
        channel     = body['channel']['id']
        ts          = body['message']['ts']
        position_id = data['position_id']
        ticker      = data['ticker']

        positions = {p['id']: p for p in get_open_positions()}
        pos = positions.get(position_id)
        if pos:
            state = dict(pos.get('trail_state') or {})
            state['order_placed'] = True
            update_position_trail_state(position_id, state)

        client.chat_update(
            channel=channel, ts=ts,
            text=f"{ticker} — trailing order placed",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"✅ *{ticker}* — trailing stop order placed"}}],
        )

    @bolt_app.view("exit_price_submit")
    def handle_exit_price(ack, body, client):
        ack()
        meta         = json.loads(body['view']['private_metadata'])
        data         = meta['data']
        channel      = meta['channel']
        ts           = meta['ts']
        position_id  = data['position_id']
        ticker       = data['ticker']
        entry_price  = data['entry_price']
        signal_price = data['current_price']

        exit_price = float(body['view']['state']['values']['price_block']['price_input']['value'])
        drift_pct  = (exit_price - signal_price) / signal_price * 100
        actual_pnl = (exit_price - entry_price) / entry_price * 100

        close_position(position_id,
                       exit_signal_price=signal_price, exit_price=exit_price,
                       exit_time=datetime.now(), exit_reason=data.get('reason'))

        note = f"${exit_price:.4f}  (signal drift: {drift_pct:+.2f}%  P&L: {actual_pnl:+.2f}%)"
        print(f"  Position closed via Slack: {ticker} at {note}")
        client.chat_update(
            channel=channel, ts=ts,
            text=f"SELL {ticker} — Exited at {note}",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*SELL {ticker}* — Exited at {note}"}}],
        )

    @bolt_app.action("manual_open")
    def handle_manual_open(ack, body, client):
        """Correction path for a misclick (e.g. hit Skipped after a real fill) --
        opens a position directly from the reference report, price-entry modal
        doubling as the confirmation step."""
        ack()
        data   = json.loads(body['actions'][0]['value'])
        ticker = data['node']['ticker']
        current_price, _ = _current_price(ticker)
        suggested_shares = int(_last_sale_recovery(ticker) // current_price) if current_price else None
        client.views_open(
            trigger_id=body['trigger_id'],
            view={
                "type":             "modal",
                "callback_id":      "manual_open_price_submit",
                "private_metadata": json.dumps(data),
                "title":  {"type": "plain_text", "text": "Manual Open"},
                "submit": {"type": "plain_text", "text": "Confirm"},
                "close":  {"type": "plain_text", "text": "Cancel"},
                "blocks": [_price_input_block(), _shares_input_block(suggested_shares)],
            },
        )

    @bolt_app.view("manual_open_price_submit")
    def handle_manual_open_price(ack, body, client):
        ack()
        data   = json.loads(body['view']['private_metadata'])
        node   = data['node']
        ticker = node['ticker']

        price  = float(body['view']['state']['values']['price_block']['price_input']['value'])
        shares = int(body['view']['state']['values']['shares_block']['shares_input']['value'])
        now    = datetime.now()

        open_position(node, price, now, price, now, shares=shares)

        note = f"${price:.4f}  {shares} shares"
        print(f"  Position manually opened via Slack: {ticker} at {note}")
        _post_message(f"MANUAL OPEN {ticker} — {note}", blocks=[{"type": "section", "text": {"type": "mrkdwn",
                      "text": f"*MANUAL OPEN {ticker}* — {note}"}}])

    @bolt_app.action("manual_close")
    def handle_manual_close(ack, body, client):
        """Correction path for a misclick (e.g. hit Skipped after a real exit) --
        closes a position directly from the reference report, price-entry modal
        doubling as the confirmation step."""
        ack()
        data = json.loads(body['actions'][0]['value'])
        client.views_open(
            trigger_id=body['trigger_id'],
            view={
                "type":             "modal",
                "callback_id":      "manual_close_price_submit",
                "private_metadata": json.dumps(data),
                "title":  {"type": "plain_text", "text": "Manual Close"},
                "submit": {"type": "plain_text", "text": "Confirm"},
                "close":  {"type": "plain_text", "text": "Cancel"},
                "blocks": [_price_input_block()],
            },
        )

    @bolt_app.view("manual_close_price_submit")
    def handle_manual_close_price(ack, body, client):
        ack()
        data        = json.loads(body['view']['private_metadata'])
        position_id = data['position_id']
        ticker      = data['ticker']
        entry_price = data['entry_price']

        exit_price = float(body['view']['state']['values']['price_block']['price_input']['value'])
        actual_pnl = (exit_price - entry_price) / entry_price * 100
        now        = datetime.now()

        close_position(position_id,
                       exit_signal_price=exit_price, exit_price=exit_price,
                       exit_time=now, exit_reason='MANUAL')

        note = f"${exit_price:.4f}  (P&L: {actual_pnl:+.2f}%)"
        print(f"  Position manually closed via Slack: {ticker} at {note}")
        _post_message(f"MANUAL CLOSE {ticker} — {note}", blocks=[{"type": "section", "text": {"type": "mrkdwn",
                      "text": f"*MANUAL CLOSE {ticker}* — {note}"}}])

    @bolt_app.action("resend_ref_table")
    def handle_resend_ref_table(ack, body, client):
        """On-demand refresh -- posts a brand new reference report rather than
        editing the clicked one in place, so the old report (and its now-stale
        manual-open/close buttons) stays as a historical record."""
        ack()
        send_reference_report(get_watchlist())


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify_buy_signal(node, sig):
    ticker   = sig['ticker']
    price    = sig['current_price']
    z        = sig['z_score']
    bar_time = sig['last_bar']
    bar_str  = bar_time.strftime('%Y-%m-%d %H:%M')
    arm      = _tp_or_arm_pct(node)
    sl       = node['stop_loss'] + 1
    hold     = node['max_hold_hours']

    hurst_str = f"{sig['hurst']:.3f}" if sig.get('hurst') is not None else "n/a"
    adf_str   = f"{sig['adf_p']:.3f}" if sig.get('adf_p') is not None else "n/a"

    sep = '=' * 62
    print(f"\n{sep}")
    print(f"  BUY SIGNAL  {ticker}  {bar_str}")
    print(f"  Price:  ${price:.4f}   Lower band: ${sig['lower_band']:.4f}   z = {z:.2f}")
    print(f"  Node:   window={node['window']}  Arm={arm}%  SL={sl}%  hold={hold}h")
    print(f"  SMA: ${sig['sma']:.4f}   Std: ${sig['std']:.4f}")
    print(f"  Hurst (100 bars): {hurst_str}   ADF p: {adf_str}")
    if (node.get('account') or '').lower() != 'brokerage' and closed_today(ticker):
        print(f"  ⚠️🔁 SAME DAY BUY WARNING: {ticker} already sold today — cash may not be settled (T+1)")
    print(sep)

    channel, ts = _post_message(
        f"BUY SIGNAL — {ticker}  ${price:.4f}  z={z:.2f}  ({bar_str})",
        _build_buy_blocks(node, sig),
    )

    # Tracked regardless of INTERACTIVE -- a trailing-buy order is still pending
    # fill confirmation even in SIM_MODE or webhook-only (non-socket) runs, where
    # there's no button to click but the reminder loop should still nag.
    if _is_trailing_buy(node):
        add_pending_buy(node, sig, channel, ts)

    if INTERACTIVE:
        chart = _chart_buy(node, sig)
        if chart:
            _upload_chart(chart, f"{ticker}_buy.png", f"BUY — {ticker}  z={z:.2f}")
        print("  Waiting for Slack response (Executed / Skipped).")
        return

    if _is_trailing_buy(node):
        print("\nTrailing buy order placed at the broker? No position opens yet -- "
              "report the real fill separately once it happens. (y/n): ", end='', flush=True)
        try:
            resp = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            resp = ''
        if resp == 'y':
            mark_pending_buy_placed(ticker)
            print(f"  {ticker} order marked placed — no position yet, waiting for fill.")
            _post_message(f"{ticker} trailing buy order placed, waiting for fill.")
        else:
            clear_pending_buy(ticker)
            print("  Skipped.")
        return

    print("\nDid you execute? Enter price (or Enter to skip): ", end='', flush=True)
    try:
        resp = input().strip()
    except (EOFError, KeyboardInterrupt):
        resp = ''

    if resp:
        try:
            exec_price = float(resp)
            drift_pct  = (exec_price - price) / price * 100
            now        = datetime.now()
            open_position(node, price, bar_time, exec_price, now)
            clear_pending_buy(ticker)
            note = f"Entered at ${exec_price:.4f}  (drift: {drift_pct:+.2f}%)"
            print(f"  Position opened. {note}")
            _post_message(f"{ticker} position opened: {note}")
        except ValueError:
            print("  Invalid price — position not opened.")
    else:
        clear_pending_buy(ticker)
        print("  Skipped.")


def notify_limit_fill(node, current_price, lower_band):
    ticker          = node['ticker']
    schwab_sl_pct   = node['stop_loss'] + 1
    schwab_sl_price = lower_band * (1 - schwab_sl_pct / 100)
    target_notional = _last_sale_recovery(ticker)
    shares          = int(target_notional // lower_band)
    now_str = datetime.now().strftime('%H:%M:%S')

    print(f"\n  [LIMIT FILL] {ticker}  price=${current_price:.2f}  trigger=${lower_band:.2f}  {now_str}")
    print(f"  Place stop: ${schwab_sl_price:.2f} (-{schwab_sl_pct}% from trigger)")

    account = node.get('account') or 'unmapped'
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"LIMIT FILLED — {ticker}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            f"✅ *{ticker}* limit filled at `${lower_band:.2f}` — `{shares} shares` (~${target_notional/1000:.0f}k) — `{account}`\n"
            f"🔴 Place Schwab stop: `${schwab_sl_price:.2f}` (-{schwab_sl_pct}% from trigger)"
        )}},
    ]
    _post_message(f"LIMIT FILLED — {ticker} at ${lower_band:.2f}", blocks=blocks)


def notify_sell_signal(pos, reason, current_price, target_price):
    ticker     = pos['ticker']
    ep         = pos['entry_price']
    entry_time = pos['entry_time']
    pct        = (current_price - ep) / ep * 100

    reason_labels = {'TP': 'TAKE PROFIT', 'SL': 'STOP LOSS', 'TIME': 'TIME EXIT', 'TRAIL': 'TRAILING STOP'}

    sep = '=' * 62
    print(f"\n{sep}")
    print(f"  SELL SIGNAL  {ticker}  — {reason_labels[reason]}")
    print(f"  Entry: ${ep:.4f}  →  Current: ${current_price:.4f}  ({pct:+.2f}%)")
    print(f"  Target: ${target_price:.4f}   Node: Arm={_tp_or_arm_pct(pos)}%  SL={pos['stop_loss'] + 1}%  hold={pos['max_hold_hours']}h")
    print(f"  Entered: {entry_time}")
    print(sep)

    channel, ts = _post_message(
        f"SELL SIGNAL — {ticker}  {reason_labels[reason]}  ${current_price:.4f}  ({pct:+.2f}%)",
        _build_sell_blocks(pos, reason, current_price, target_price),
    )

    # Tracks the exit as unresolved until Exited/Skipped -- unlike a placed trailing-buy
    # (waiting on a broker fill we can't detect), a stalled SELL confirmation means an
    # already-open position with real capital sitting unmanaged, arguably more urgent to
    # nag about than the buy side.
    state = dict(pos.get('trail_state') or {})
    state['exit_pending'] = {
        'reason': reason, 'current_price': current_price, 'target_price': target_price,
        'reminder_channel': channel, 'reminder_ts': ts, 'reminder_count': 0,
        'last_reminder_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    update_position_trail_state(pos['id'], state)

    if INTERACTIVE:
        chart = _chart_sell(pos, current_price)
        if chart:
            _upload_chart(chart, f"{ticker}_sell.png", f"SELL — {ticker}  {reason_labels[reason]}  {pct:+.2f}%")
        print("  Waiting for Slack response (Exited / Skipped).")
        return

    print("\nDid you exit? Enter price (or Enter to skip): ", end='', flush=True)
    try:
        resp = input().strip()
    except (EOFError, KeyboardInterrupt):
        resp = ''

    if resp:
        try:
            exit_price = float(resp)
            drift_pct  = (exit_price - current_price) / current_price * 100
            actual_pnl = (exit_price - ep) / ep * 100
            note = f"Exited at ${exit_price:.4f}  (signal drift: {drift_pct:+.2f}%  P&L: {actual_pnl:+.2f}%)"
            close_position(pos['id'], exit_signal_price=current_price, exit_price=exit_price,
                           exit_time=datetime.now(), exit_reason=reason)
            print(f"  Position closed. {note}")
            _post_message(f"{ticker} position closed: {note}")
        except ValueError:
            print("  Invalid price — position kept open.")
    else:
        state = dict(pos.get('trail_state') or {})
        state.pop('exit_pending', None)
        update_position_trail_state(pos['id'], state)
        print("  Skipped — position kept open.")


TRAIL_REMINDER_MINUTES = 15


def _trailing_order_blocks(pos, current_price, reminder_num=0):
    ticker    = pos['ticker']
    ep        = pos['entry_price']
    pct       = (current_price - ep) / ep * 100
    trail_pct = pos.get('trail_sell_pct') or 3.0
    header    = f"⚠️ *{ticker}* — STILL PENDING (reminder #{reminder_num})" if reminder_num else f"🎯 *{ticker}* — TRAILING ACTIVATED — action needed"
    if reminder_num:
        body = (
            f"Order still not confirmed placed. If it should have filled by now, check Schwab — "
            f"if the trailing stop didn't take, cancel it and submit a *market* order instead to "
            f"match the strategy's expected exit, rather than leaving it unprotected."
        )
    else:
        body = (
            f"Take-profit cleared. Cancel the fixed Schwab stop and place a *trailing stop* order at `{trail_pct:.0f}%` "
            f"so it auto-adjusts with price at the broker — Slack polling alone won't catch it fast enough."
        )
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            f"{header}\n"
            f"entry `${ep:.2f}`  |  current `${current_price:.2f}`  |  P&L `{pct:+.1f}%`\n"
            f"{body}"
        )}},
    ]
    if INTERACTIVE:
        value = json.dumps({"position_id": pos['id'], "ticker": ticker})
        blocks.append({
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Order Placed"},
                 "style": "primary", "action_id": "trail_order_placed", "value": value},
            ],
        })
    return blocks


def _supersede_message(channel, ts, ticker):
    if not (SOCKET_MODE and channel and ts):
        return
    try:
        bolt_app.client.chat_update(
            channel=channel, ts=ts,
            text=f"{ticker} trailing order reminder — superseded",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"~_{ticker} trailing order reminder — superseded, see newer message below_~"}}],
        )
    except Exception as e:
        print(f"  [slack error] supersede failed: {e}")


def notify_trailing_activated(pos, current_price):
    ticker = pos['ticker']
    ep     = pos['entry_price']
    pct    = (current_price - ep) / ep * 100
    print(f"\n  [TRAILING ACTIVATED] {ticker}  entry=${ep:.4f}  now=${current_price:.4f}  ({pct:+.2f}%)")

    blocks = _trailing_order_blocks(pos, current_price, reminder_num=0)
    channel = ts = None
    if INTERACTIVE:
        try:
            resp = bolt_app.client.chat_postMessage(
                channel=SLACK_CHANNEL, text=f"TRAILING ACTIVATED — {ticker}  ${current_price:.4f}  ({pct:+.2f}%)",
                blocks=blocks)
            channel, ts = resp['channel'], resp['ts']
        except Exception as e:
            print(f"  [slack error] {e}")
    else:
        _post_message(f"TRAILING ACTIVATED — {ticker}  ${current_price:.4f}  ({pct:+.2f}%)", blocks=blocks)

    # Re-read trail_state from the DB rather than trusting `pos` -- check_sell_condition
    # already committed the fresh state (trailing=True, peak=...) just before this was
    # called, but `pos` here is the caller's pre-update copy. Merging onto the stale
    # copy would silently erase 'trailing'/'peak', breaking the trailing-stop entirely.
    with _conn() as c:
        row = c.execute("SELECT trail_state FROM open_positions WHERE id = ?", (pos['id'],)).fetchone()
    state = json.loads(row['trail_state']) if row and row['trail_state'] else {}
    state['order_placed']    = False
    state['reminder_channel'] = channel
    state['reminder_ts']      = ts
    state['reminder_count']   = 0
    state['last_reminder_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    update_position_trail_state(pos['id'], state)


def check_trailing_reminders(open_positions):
    """Nags every TRAIL_REMINDER_MINUTES until the trailing-stop order is confirmed
    placed -- a single one-time alert is too easy to miss, and an unplaced trailing
    stop between polls is a real risk if price moves fast."""
    now = datetime.now()
    for pos in open_positions:
        state = pos.get('trail_state') or {}
        if not state.get('trailing') or state.get('order_placed'):
            continue
        last_at_str = state.get('last_reminder_at')
        if not last_at_str:
            continue
        last_at = datetime.strptime(last_at_str, '%Y-%m-%d %H:%M:%S')
        if (now - last_at).total_seconds() < TRAIL_REMINDER_MINUTES * 60:
            continue
        cp, _ = _current_price(pos['ticker'])
        if cp is None:
            continue
        _supersede_message(state.get('reminder_channel'), state.get('reminder_ts'), pos['ticker'])
        reminder_num = state.get('reminder_count', 0) + 1
        blocks = _trailing_order_blocks(pos, cp, reminder_num=reminder_num)
        channel, ts = _post_message(
            f"{pos['ticker']} trailing order — still pending (reminder #{reminder_num})", blocks=blocks)
        new_state = dict(state)
        new_state['reminder_channel'] = channel
        new_state['reminder_ts']      = ts
        new_state['reminder_count']   = reminder_num
        new_state['last_reminder_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
        update_position_trail_state(pos['id'], new_state)


EXIT_REMINDER_MINUTES = 15


def _exit_pending_blocks(pos, exit_pending, reminder_num):
    """Mirrors _trailing_order_blocks for the sell side. A stalled SELL
    confirmation means an already-open position with real capital sitting
    unmanaged -- arguably more urgent than a stalled BUY, so this reuses the
    same 'Exited'/'Skipped' buttons (sell_exited/sell_skipped) as the original
    alert rather than inventing new action_ids."""
    ticker        = pos['ticker']
    ep            = pos['entry_price']
    reason        = exit_pending['reason']
    current_price = exit_pending['current_price']
    target_price  = exit_pending['target_price']
    pct           = (current_price - ep) / ep * 100
    reason_labels = {'TP': 'TAKE PROFIT', 'SL': 'STOP LOSS', 'TIME': 'TIME EXIT', 'TRAIL': 'TRAILING STOP'}

    text = (
        f"⚠️ *{ticker}* — EXIT NOT CONFIRMED (reminder #{reminder_num})\n"
        f"{reason_labels[reason]}  |  entry `${ep:.2f}`  |  signal `${current_price:.2f}`  |  P&L `{pct:+.1f}%`\n"
        f"Position may still be open and unmanaged at the broker. Confirm Exited with the real fill "
        f"price, or Skip if it turns out the exit condition no longer applies."
    )
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if INTERACTIVE:
        value = json.dumps({
            "type": "sell", "position_id": pos['id'], "ticker": ticker,
            "current_price": current_price, "entry_price": ep, "reason": reason,
        })
        blocks.append({
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Exited"},
                 "style": "primary", "action_id": "sell_exited", "value": value},
                {"type": "button", "text": {"type": "plain_text", "text": "Skipped"},
                 "action_id": "sell_skipped", "value": value},
            ],
        })
    else:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": "No interactive buttons — type the exit price into the terminal running the daemon when filled."}
        ]})
    return blocks


def check_exit_reminders(open_positions):
    """Nags every EXIT_REMINDER_MINUTES until a fired SELL signal is confirmed
    Exited or Skipped ('4r' in the buy/sell lifecycle numbering) -- mirrors
    check_trailing_reminders' supersede-not-edit-in-place pattern. Without this,
    trail_state.exit_pending is written by notify_sell_signal and cleared on
    Exited/Skipped, but nothing polls it -- a missed SELL alert would sit
    silently forever, unlike every other stage's yellow state."""
    now = datetime.now()
    for pos in open_positions:
        state = pos.get('trail_state') or {}
        exit_pending = state.get('exit_pending')
        if not exit_pending:
            continue
        last_at = datetime.strptime(exit_pending['last_reminder_at'], '%Y-%m-%d %H:%M:%S')
        if (now - last_at).total_seconds() < EXIT_REMINDER_MINUTES * 60:
            continue
        _supersede_message(exit_pending.get('reminder_channel'), exit_pending.get('reminder_ts'), pos['ticker'])
        reminder_num = exit_pending.get('reminder_count', 0) + 1
        blocks = _exit_pending_blocks(pos, exit_pending, reminder_num)
        channel, ts = _post_message(
            f"{pos['ticker']} exit — still unconfirmed (reminder #{reminder_num})", blocks=blocks)
        new_state = dict(state)
        new_exit_pending = dict(exit_pending)
        new_exit_pending['reminder_channel'] = channel
        new_exit_pending['reminder_ts']      = ts
        new_exit_pending['reminder_count']   = reminder_num
        new_exit_pending['last_reminder_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
        new_state['exit_pending'] = new_exit_pending
        update_position_trail_state(pos['id'], new_state)


BUY_REMINDER_MINUTES = 15


def _trailing_buy_status(pending):
    """Best-effort live approximation of the backtest's waiting-state bounce check
    (_simulate_trail_both's running_low/buy_trigger) -- tracks the running low across
    hourly bars since the signal fired and checks whether price has already bounced
    back up by trail_buy_pct%. Only as accurate as the hourly cache (no true intrabar
    low live, same caveat as compute_buy_signal) -- a reasonable signal for reminder
    wording, not a substitute for the real live state machine (still unimplemented,
    tracked in docs/backlog_cache.md)."""
    node = pending['node']
    trail_buy_pct = (node.get('trail_buy_pct') or 0) / 100.0
    df_hourly, _ = _load_cache(pending['ticker'])
    if df_hourly is None or not trail_buy_pct:
        return None, None
    signal_time = datetime.strptime(pending['signal_time'], '%Y-%m-%d %H:%M:%S')
    bars = df_hourly[df_hourly.index >= signal_time]
    if bars.empty:
        return None, None
    running_low = float(bars['Low'].iloc[0])
    trigger = running_low * (1 + trail_buy_pct)
    met = False
    for _, bar in bars.iterrows():
        if bar['Low'] < running_low:
            running_low = float(bar['Low'])
            trigger = running_low * (1 + trail_buy_pct)
        if bar['High'] >= trigger:
            met = True
            break
    return met, trigger


def _pending_buy_blocks(pending, reminder_num):
    """Mirrors _trailing_order_blocks for the buy side. Two distinct nag phases,
    since order_placed alone isn't resolution -- unlike the sell side (where a
    placed trailing-stop needs no further confirmation), a placed trailing-buy
    still needs a real Filled/Skip confirmation, because there's no way to detect
    a fill live -- we can only estimate whether it *should* have filled by now
    (_trailing_buy_status) and prompt, never assume it silently."""
    node    = pending['node']
    ticker  = pending['ticker']
    account = node.get('account') or 'unmapped'
    met, trigger = _trailing_buy_status(pending)

    if pending['order_placed']:
        header = f"⚠️ *{ticker}* — FILL NOT CONFIRMED (reminder #{reminder_num})"
        if met:
            action_text = (
                f"Trailing buy trigger (bounce off low, `${trigger:.2f}`) already met — this should "
                f"have filled by now. Please confirm: Filled, or Skip if it never took and you're "
                f"converting to a market order instead."
            )
        else:
            action_text = (
                f"Trailing buy trigger not yet met (needs a `{node.get('trail_buy_pct', 0):g}%` bounce "
                f"off the running low) — likely still resting, no urgency yet. Confirm Filled once it "
                f"executes, or Skip to cancel."
            )
        text = f"{header}\ntrigger `${pending['signal_price']:.2f}`  |  `{account}`\n{action_text}"
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        if INTERACTIVE:
            value = json.dumps({
                "node": node, "signal_price": pending['signal_price'], "signal_time": pending['signal_time'],
            })
            blocks.append({
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Filled"},
                     "style": "primary", "action_id": "trail_buy_filled", "value": value},
                    {"type": "button", "text": {"type": "plain_text", "text": "Skipped"},
                     "action_id": "buy_skipped", "value": value},
                ],
            })
        return blocks

    if met:
        action_text = (
            f"Trailing buy trigger (bounce off low, `${trigger:.2f}`) already met — order likely "
            f"should have filled. Check Schwab — if it hasn't taken, cancel and submit a *market* "
            f"order instead, or Skip if the setup no longer applies."
        )
    elif met is False:
        action_text = (
            f"Trailing buy trigger not yet met (needs a `{node.get('trail_buy_pct', 0):g}%` bounce "
            f"off the running low, currently `${trigger:.2f}`) — wider trail% naturally takes longer, "
            f"no urgency yet. Confirm the trailing buy order is resting at the broker, or Skip if the "
            f"setup no longer applies."
        )
    else:
        action_text = (
            f"Trailing buy order not confirmed filled. Check Schwab — if it hasn't taken, "
            f"consider canceling and submitting a *market* order instead to match the strategy's "
            f"expected entry timing, or Skip if the setup no longer applies."
        )
    text = (
        f"⚠️ *{ticker}* — BUY STILL PENDING (reminder #{reminder_num})\n"
        f"trigger `${pending['signal_price']:.2f}`  |  `{account}`\n"
        f"{action_text}"
    )
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if INTERACTIVE:
        value = json.dumps({
            "node": node, "signal_price": pending['signal_price'], "signal_time": pending['signal_time'],
        })
        blocks.append({
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Trailing Buy Order Placed"},
                 "style": "primary", "action_id": "trail_buy_order_placed", "value": value},
                {"type": "button", "text": {"type": "plain_text", "text": "Skipped"},
                 "action_id": "buy_skipped", "value": value},
            ],
        })
    return blocks


def check_buy_reminders():
    """Nags every BUY_REMINDER_MINUTES until a trailing-buy is fully resolved
    (Filled or Skipped) -- without this, a stalled trailing-buy at the broker is
    invisible until the user happens to remember to check (the gap flagged in
    docs/operational_limits.md's TrailingBoth lifecycle table, row 3). Unlike the
    sell side's order_placed (which needs no further confirmation once placed),
    the buy side keeps nagging after order_placed=True too -- there's no way to
    detect a live fill, so a placed-but-unconfirmed order still needs a real
    Filled/Skip answer, never silently assumed (_pending_buy_blocks switches
    wording/buttons for this phase)."""
    now = datetime.now()
    for pending in get_pending_buys():
        last_at = datetime.strptime(pending['last_reminder_at'], '%Y-%m-%d %H:%M:%S')
        if (now - last_at).total_seconds() < BUY_REMINDER_MINUTES * 60:
            continue
        if pending['order_placed']:
            # Fill-confirmation phase: nagging every 15min regardless of whether a fill
            # is even plausible yet is noisy (e.g. KORU's wide 12% trail_buy_pct can
            # genuinely take a while). Only start nagging once the bounce trigger has
            # plausibly been hit; met=None (unknown -- e.g. stale/missing cache) still
            # nags, erring toward not silently dropping a real stalled fill.
            met, _ = _trailing_buy_status(pending)
            if met is False:
                continue
        _supersede_message(pending['reminder_channel'], pending['reminder_ts'], pending['ticker'])
        reminder_num = pending['reminder_count'] + 1
        blocks = _pending_buy_blocks(pending, reminder_num)
        channel, ts = _post_message(
            f"{pending['ticker']} trailing buy — still pending (reminder #{reminder_num})", blocks=blocks)
        update_pending_buy_reminder(pending['id'], channel, ts, reminder_num)


# ---------------------------------------------------------------------------
# Startup report
# ---------------------------------------------------------------------------

def _add_trading_hours(start, hours):
    """Advance `start` by `hours` trading bars (market hours 9-15, Mon-Fri only)."""
    from datetime import timedelta
    dt = start
    remaining = hours
    while remaining > 0:
        dt += timedelta(hours=1)
        if dt.weekday() < 5 and 9 <= dt.hour <= 15:
            remaining -= 1
    return dt


def _proximity_emoji(pct_away):
    if pct_away < 5:
        return "🔶"
    if pct_away < 15:
        return "🟡"
    return "⚪"


def _ticker_block(row):
    """Renders one row from build_reference_table as mrkdwn prose (wraps naturally
    on mobile) instead of a fixed-width table column (unreadable on iPhone).
    Returns a list of blocks (section + optional manual-correction actions)."""
    ticker, version = row['Ticker'], row.get('Version') or ''
    account = 'bro' if (row.get('Account') or '').lower() == 'brokerage' else (row.get('Account') or '')
    account_str = f" — `{account}`" if account else ''
    proximity = row.get('Proximity')

    if row['Next Action'] == 'NO_DATA':
        return [{"type": "section", "text": {"type": "mrkdwn", "text": f"⚫ *{ticker}* `{version}`  NO_DATA"}}]

    phase = row.get('Phase') or ''
    phase_str = f"{phase} " if phase else ''
    now = row['Now']
    trigger = row['Next Trigger $']

    if row['Held']:
        pnl = row.get('PnL %')
        sl = row.get('SL $')
        sl_str = f"  sl `${sl:.2f}`" if sl is not None else "  sl `cancelled (trail order live)`"
        z = row.get('Z')
        z_str = f"{z:+.2f}" if z is not None else '?'
        tb, arm, ts = row.get('TrailBuy%'), row.get('Arm%'), row.get('TrailSell%')
        pct_str = lambda v: f"{v:g}%" if v is not None else '?'
        last_sale = row.get('Last Sale $')
        last_sale_str = f"  next buy ~`${last_sale/1000:.0f}k`" if last_sale is not None else ''
        text = (
            f"{phase_str}*{ticker}* `{version}` — {row['Hold']}{account_str}{last_sale_str}\n"
            f"now `${now:.2f}` {pnl:+.1f}%  trig `${trigger:.2f}` ({proximity:+.1f}%)\n"
            f"→ _{row['Next Action']}_{sl_str}\n"
            f"z `{z_str}`  tb `{pct_str(tb)}`  arm `{pct_str(arm)}`  ts `{pct_str(ts)}`"
        )
    else:
        overnight = row.get('Overnight %')
        tb, arm, ts = row.get('TrailBuy%'), row.get('Arm%'), row.get('TrailSell%')
        pct_str = lambda v: f"{v:g}%" if v is not None else '?'
        last_sale = row.get('Last Sale $')
        last_sale_str = f"  next buy ~`${last_sale/1000:.0f}k`" if last_sale is not None else ''
        z_trig = row.get('Z Trigger')
        z_trig_str = f"  z-trig `{z_trig:g}`" if z_trig is not None else ''
        text = (
            f"{phase_str}*{ticker}* `{version}`{account_str}{last_sale_str}\n"
            f"now `${now:.2f}` ({overnight:+.1f}% O/N)  z `{row['Z']:+.2f}`  trig `${trigger:.2f}` ({proximity:+.1f}%)\n"
            f"→ _{row['Next Action']}_  arm `${row['Arm $']:.2f}`  sl `${row['SL $']:.2f}`\n"
            f"tb `{pct_str(tb)}`  arm `{pct_str(arm)}`  ts `{pct_str(ts)}`{z_trig_str}"
        )
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    if INTERACTIVE:
        node = row.get('_node')
        if row['Held']:
            pos = row.get('_pos')
            if pos:
                value = json.dumps({"position_id": pos['id'], "ticker": ticker, "entry_price": pos['entry_price']})
                blocks.append({"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": f"Manually Close {ticker}"},
                     "action_id": "manual_close", "value": value},
                ]})
        elif node:
            node_fields = {k: node.get(k) for k in ('ticker', 'strategy', 'version', 'window',
                                                      'take_profit', 'stop_loss', 'max_hold_hours',
                                                      'trail_sell_pct', 'fixed_sl', 'trail_buy_pct', 'arm_sell_pct')}
            value = json.dumps({"node": node_fields})
            blocks.append({"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": f"Manually Open {ticker}"},
                 "action_id": "manual_open", "value": value},
            ]})

    return blocks


def _send_window_alert(label, watchlist):
    """Reuses build_reference_table so this alert shares one source of truth with
    the morning report -- correct per-position trigger (buy/arm/trailing-sell,
    not always the buy-side lower_band). Minimal by design: only tickers within
    5% of their next trigger, rendered as mobile-readable prose, not the full
    watchlist table."""
    ref_rows = build_reference_table(watchlist)
    hot = [r for r in ref_rows if isinstance(r.get('Proximity'), (int, float)) and r['Proximity'] < 5]
    alert_level = "🔶 *HIGH ALERT*" if hot else "✅ algo running, nothing within range"
    header = f"⏱ *Signal window — {label} ET* | {alert_level}"
    if not hot:
        _post_message(header)
        return
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": header}}, {"type": "divider"}]
    for r in hot:
        blocks += _ticker_block(r)
    _post_message(header, blocks=blocks)


_REF_TABLE_COLS = [
    'Phase', 'Ticker', 'Hold', 'Next Trigger $', 'Now', 'Proximity', 'Next Action',
    'Version', 'Alpha', 'Z', 'Z Trigger', 'TrailBuy%', 'Arm%', 'TrailSell%', 'Account', 'Last Sale $',
]


def _last_sale_recovery(ticker):
    """Estimated next-buy notional: proceeds (exit_price * shares) from this ticker's
    most recent closed trade, so sizing roughly compounds off the last recycle instead
    of always assuming a flat $50k. Falls back to $50k if no closed trade has shares
    logged yet. A rough estimate, not a live capital feed -- doesn't know about other
    trades competing for the same account's cash in between."""
    with _conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT exit_price, shares FROM trade_log WHERE ticker=? AND exit_price IS NOT NULL "
            "AND shares IS NOT NULL ORDER BY exit_time DESC LIMIT 1", (ticker,)
        ).fetchone()
    if row and row['exit_price'] and row['shares']:
        return row['exit_price'] * row['shares']
    return 50_000


_PHASE_GREY, _PHASE_YELLOW, _PHASE_GREEN = '⚪', '🟡', '🟢'


def _phase_emoji(pos, pending_buy):
    """Four-bubble lifecycle strip, left to right: Signal / Filled / Armed / Sold.
    Each bubble is gray (not reached), yellow (in progress, awaiting confirmation),
    or green (confirmed complete) -- a position can be filled without being armed,
    so those get separate bubbles rather than one combined ball."""
    if pos is None:
        if pending_buy is None:
            return _PHASE_GREY * 4
        order_placed = pending_buy.get('order_placed')
        signal = _PHASE_GREEN if order_placed else _PHASE_YELLOW
        fill = _PHASE_YELLOW if order_placed else _PHASE_GREY
        return f"{signal}{fill}{_PHASE_GREY}{_PHASE_GREY}"

    trail_state = pos.get('trail_state') or {}
    if trail_state.get('trailing'):
        armed = _PHASE_GREEN if trail_state.get('order_placed') else _PHASE_YELLOW
    else:
        armed = _PHASE_GREY
    sold = _PHASE_YELLOW if trail_state.get('exit_pending') else _PHASE_GREY
    return f"{_PHASE_GREEN}{_PHASE_GREEN}{armed}{sold}"


def build_reference_table(watchlist):
    """One row per live-mode ticker: buy-trigger info if flat, arm/sell-trigger
    info if held. `Proximity` is signed so negative always means the trigger has
    already been crossed (price fell through a buy/sell-trail trigger, or rose
    through an arm trigger) -- sign convention, not raw distance."""
    positions = {p['ticker']: p for p in get_open_positions()}
    pending_buys = {p['ticker']: p for p in get_pending_buys()}
    rows = []
    for node in [n for n in watchlist if n.get('mode') == 'live']:
        ticker = node['ticker']
        pos = positions.get(ticker)
        sig = compute_buy_signal(node)
        account = node.get('account') or ''
        alpha = node.get('alpha')
        last_sale = _last_sale_recovery(ticker)
        phase = _phase_emoji(pos, pending_buys.get(ticker))

        if sig is None:
            rows.append({
                'Ticker': ticker, 'Hold': '', 'Next Action': 'NO_DATA', 'Next Trigger $': None,
                'Now': None, 'Proximity': None, 'Version': node.get('version'), 'Alpha': alpha,
                'Z': None, 'Z Trigger': node.get('z_score_threshold'),
                'TrailBuy%': node.get('trail_buy_pct'), 'Arm%': node.get('arm_sell_pct'),
                'TrailSell%': node.get('trail_sell_pct'), 'Account': account, 'Last Sale $': last_sale,
                'Strategy': node['strategy'], 'Held': False, 'Phase': phase,
                '_node': node, '_pos': None, '_sig': None,
            })
            continue

        now_price = sig['current_price']
        schwab_sl_pct = node['stop_loss'] + 1

        if pos is None:
            trigger = sig['lower_band']
            trail_buy_pct = node.get('trail_buy_pct')
            rows.append({
                'Ticker': ticker, 'Hold': '',
                'Next Action': 'Waiting Buy Trigger',
                'Next Trigger $': trigger, 'Now': now_price,
                'Proximity': (now_price - trigger) / trigger * 100,
                'Version': node.get('version'), 'Alpha': alpha, 'Z': sig['z_score'],
                'Z Trigger': node.get('z_score_threshold'),
                'TrailBuy%': trail_buy_pct, 'Arm%': node.get('arm_sell_pct'),
                'TrailSell%': node.get('trail_sell_pct'), 'Account': account, 'Last Sale $': last_sale,
                'Strategy': node['strategy'], 'Held': False, 'Phase': phase,
                'SL $': trigger * (1 - schwab_sl_pct / 100), 'Arm $': trigger * (1 + _tp_or_arm_pct(node) / 100),
                'Overnight %': (now_price - sig['prev_close']) / sig['prev_close'] * 100,
                'Prev Close': sig['prev_close'], 'Data Date': sig['last_daily_bar'],
                '_node': node, '_pos': None, '_sig': sig,
            })
        else:
            df_hourly_p, _ = _load_cache(ticker)
            signal_time = datetime.fromisoformat(pos['signal_time'])
            hours_held = _bars_held(df_hourly_p, signal_time)
            hold = f"{hours_held:.0f}h/{pos['max_hold_hours']}h"
            trail_state = pos.get('trail_state') or {}
            arm_pct = pos.get('arm_sell_pct')
            trail_sell_pct = pos.get('trail_sell_pct')
            pos_schwab_sl_pct = pos['stop_loss'] + 1
            # Broker only allows one resting sell-all order per position -- once the
            # trailing-sell order is actually placed (order_placed=True), it replaces
            # the catastrophic stop, so the entry-based SL price is no longer live.
            sl_price = None if trail_state.get('order_placed') else \
                pos['entry_price'] * (1 - pos_schwab_sl_pct / 100)

            if trail_state.get('trailing'):
                peak = trail_state.get('peak', pos['entry_price'])
                trail_pct = (trail_sell_pct or 3.0) / 100.0
                trigger = peak * (1 - trail_pct)
                if trail_state.get('order_placed'):
                    next_action = f"Waiting Sell {trail_sell_pct:g}% Fill" if trail_sell_pct else 'Waiting Sell Fill'
                else:
                    next_action = f"Pending Sell {trail_sell_pct:g}%" if trail_sell_pct else 'Pending Sell'
                proximity = (now_price - trigger) / trigger * 100
            else:
                trigger = pos['entry_price'] * (1 + _tp_or_arm_pct(pos) / 100.0)
                next_action = f"Arm {arm_pct:g}%" if arm_pct else 'Arm'
                proximity = (trigger - now_price) / trigger * 100

            rows.append({
                'Ticker': ticker, 'Hold': hold, 'Next Action': next_action,
                'Next Trigger $': trigger, 'Now': now_price, 'Proximity': proximity,
                'Version': pos.get('version'), 'Alpha': alpha, 'Z': sig['z_score'],
                'Z Trigger': node.get('z_score_threshold'),
                'TrailBuy%': pos.get('trail_buy_pct'), 'Arm%': arm_pct,
                'TrailSell%': trail_sell_pct, 'Account': account, 'Last Sale $': last_sale,
                'Strategy': pos.get('strategy', node['strategy']), 'Held': True, 'Phase': phase,
                'SL $': sl_price, 'PnL %': (now_price - pos['entry_price']) / pos['entry_price'] * 100,
                '_node': node, '_pos': pos, '_sig': sig,
            })
    return rows


def format_reference_table(rows):
    def fmt(col, v):
        if v is None:
            return ''
        if col == 'Next Trigger $':
            return f"${v:.2f}"
        if col == 'Now':
            return f"${v:.2f}"
        if col == 'Proximity':
            return f"{v:+.1f}%"
        if col == 'Alpha':
            return f"{v:+.0f}"
        if col == 'Z':
            return f"{v:+.2f}"
        if col == 'Z Trigger':
            return f"{v:g}"
        if col in ('TrailBuy%', 'Arm%', 'TrailSell%'):
            return f"{v:g}"
        if col == 'Last Sale $':
            return f"${v/1000:.0f}k"
        return str(v)

    cells = [[fmt(c, r.get(c)) for c in _REF_TABLE_COLS] for r in rows]
    widths = [max(len(col), *(len(row[i]) for row in cells)) if cells else len(col)
              for i, col in enumerate(_REF_TABLE_COLS)]
    lines = [' '.join(col.ljust(widths[i]) for i, col in enumerate(_REF_TABLE_COLS))]
    for row in cells:
        lines.append(' '.join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return '\n'.join(lines)


_STRATEGY_LABELS = {
    'ZScoreBreakout':             ('BUY (bar-close)', 'At signal close: edit staged limit → market and submit'),
    'TrendFilteredZScore':        ('BUY (bar-close)', 'At signal close: edit staged limit → market and submit'),
    'TrailingExitZScoreBreakout': ('BUY (bar-close, trailing exit)', 'At signal close: edit staged limit → market and submit'),
    'LimitOrderZScoreBreakout':   ('BUY (limit)', 'Pre-market: stage limit order at trigger price (absurdly low); confirm fill intrabar'),
    'TrailingBuyZScoreBreakout':  ('BUY (bar-close, trailing entry)', 'At signal close: place a trailing buy order at trail_buy_pct% — broker handles fill timing'),
    'TrailingBothZScoreBreakout': ('BUY (bar-close, trailing entry+exit)', 'At signal close: place a trailing buy order at trail_buy_pct% — broker handles fill timing'),
}


def send_reference_report(watchlist):
    """One source of truth (build_reference_table) rendered as mobile-readable
    prose per ticker -- flat and held both shown with their real next trigger,
    grouped: held positions first, then buy candidates sorted by proximity."""
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    rows = build_reference_table(watchlist)

    def sort_key(r):
        p = r.get('Proximity')
        return p if isinstance(p, (int, float)) else float('inf')

    held_rows = sorted([r for r in rows if r['Held']], key=sort_key)
    flat_rows = sorted([r for r in rows if not r['Held']], key=sort_key)

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Morning Report — {now_str}"}},
    ]
    if INTERACTIVE:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "🔄 Resend Report"}, "action_id": "resend_ref_table"},
        ]})

    if held_rows:
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": "Open Positions"}})
        for r in held_rows:
            blocks += _ticker_block(r)
    else:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "No open positions."}]})

    blocks.append({"type": "divider"})
    blocks.append({"type": "header", "text": {"type": "plain_text", "text": "Buy Candidates"}})
    for r in flat_rows:
        blocks += _ticker_block(r)
        proximity = r.get('Proximity')
        if isinstance(proximity, (int, float)) and proximity < 5:
            chart = _chart_buy(r['_node'], r['_sig'])
            if chart:
                _upload_chart(chart, f"{r['Ticker']}_morning.png", f"{r['Ticker']} `{r['Version']}`  z={r['Z']:+.2f}")

    # Console output
    print(f"Morning Report — {now_str}")
    if held_rows:
        print("  Open positions:")
        for r in held_rows:
            print(f"    {r['Ticker']:<6} {r['Version']}  hold={r['Hold']}  now=${r['Now']:.2f}  {r['Next Action']}")
    for r in flat_rows:
        if r['Next Action'] == 'NO_DATA':
            print(f"  {r['Ticker']:<6} {r['Version']}  NO_DATA  [{r['Strategy']}]")
        else:
            emoji = _proximity_emoji(r['Proximity'])
            print(f"  {emoji} {r['Ticker']:<6} {r['Version']}  now=${r['Now']:>7.2f}  trigger=${r['Next Trigger $']:>7.2f}  ({r['Proximity']:+.1f}%)  z={r['Z']:>+5.2f}  [{r['Strategy']}]")

    _post_message(f"Morning Report — {now_str}", blocks=blocks)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

# Signal windows in ET: 10:25-10:40 (9:30 bar close) and 15:25-15:40 (14:30 bar close)
_SIGNAL_WINDOWS = [(10, 25, 10, 40), (15, 25, 15, 40)]

# Reference report fires once at each of these times daily -- early (7am) so
# there's a report before the day even starts, before the open, and before the
# afternoon signal window, so a fresh full-watchlist view lands ahead of the
# moments an action is most likely to be required. Also fires unconditionally
# on daemon startup/restart, independent of this schedule.
_REFERENCE_TIMES = [(7, 0), (9, 20), (15, 20)]

def _in_buy_window(now):
    t = (now.hour, now.minute)
    for h0, m0, h1, m1 in _SIGNAL_WINDOWS:
        if (h0, m0) <= t <= (h1, m1):
            return True
    return False


def run_loop(tickers: set = None):
    ensure_tables()

    human_fh = open(HUMAN_LOG_PATH, "a")
    sys.stdout = _Tee(sys.__stdout__, human_fh)
    sys.stderr = _Tee(sys.__stderr__, human_fh)
    verbose_fh = open(VERBOSE_LOG_PATH, "a")

    ticker_label = ",".join(sorted(tickers)) if tickers else "all"
    print(f"Signal monitor started  |  poll={POLL_SECS}s  |  tickers={ticker_label}  |  Ctrl+C to stop")

    if SOCKET_MODE:
        handler = SocketModeHandler(bolt_app, SLACK_APP_TOKEN)
        t = threading.Thread(target=handler.start, daemon=True)
        t.start()
        _resolve_channel_id()
        print("  [slack] Socket Mode active — interactive buttons enabled")
    elif SLACK_HOOK:
        print("  [slack] Webhook mode — no interactive buttons")
    else:
        print("  [info] No Slack config — console only")

    startup_wl = get_watchlist()
    if tickers:
        startup_wl = [n for n in startup_wl if n['ticker'] in tickers]
    send_reference_report(startup_wl)

    buy_alerted:        set[tuple] = set()
    sell_alerted:       set[tuple] = set()  # (position_id, bar_ts) — dedups within a bar, not across bars
    window_alerted:     set[tuple] = set()
    limit_fill_alerted: set[tuple] = set()
    last_seen_bar:      dict       = {}   # ticker -> last hourly bar timestamp checked
    last_date = datetime.now().strftime('%Y-%m-%d')
    # Slots already past today are pre-marked "done" since the unconditional
    # send_reference_report() above just covered them -- only upcoming slots fire.
    _now0 = datetime.now()
    reference_alerted: set[tuple] = {
        (last_date, f"{rh:02d}:{rm:02d}") for rh, rm in _REFERENCE_TIMES
        if (_now0.hour, _now0.minute) >= (rh, rm)
    }

    while True:
        now   = datetime.now()
        today = now.strftime('%Y-%m-%d')
        HEARTBEAT_PATH.write_text(now.strftime('%Y-%m-%d %H:%M:%S'))

        if today != last_date:
            buy_alerted.clear()
            window_alerted.clear()
            limit_fill_alerted.clear()
            reference_alerted.clear()
            last_date = today

        for rh, rm in _REFERENCE_TIMES:
            rlabel = f"{rh:02d}:{rm:02d}"
            rkey = (today, rlabel)
            if (now.hour, now.minute) >= (rh, rm) and rkey not in reference_alerted:
                reference_alerted.add(rkey)
                wl = get_watchlist()
                if tickers:
                    wl = [n for n in wl if n['ticker'] in tickers]
                send_reference_report(wl)

        watchlist = get_watchlist()
        if tickers:
            watchlist = [n for n in watchlist if n['ticker'] in tickers]
        def _refresh(ticker):
            verbose_fh.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} {ticker} ---\n")
            with contextlib.redirect_stdout(verbose_fh), contextlib.redirect_stderr(verbose_fh):
                fetch_live_data_smart(ticker)
            verbose_fh.flush()

        refresh_tickers = {p['ticker'] for p in get_open_positions()} | {n['ticker'] for n in watchlist}
        with ThreadPoolExecutor(max_workers=1) as ex:
            for t in sorted(refresh_tickers):
                try:
                    ex.submit(_refresh, t).result(timeout=15)
                except FuturesTimeoutError:
                    print(f"  [data] {t} refresh timed out — skipping")
                except Exception as e:
                    print(f"  [data] {t} refresh failed: {e}")

        # Fire once per window: notify that algo is alive anywhere inside the window
        # (POLL_SECS=300 means we rarely land on the exact opening minute).
        for wh, wm, wh1, wm1 in _SIGNAL_WINDOWS:
            label = f"{wh:02d}:{wm:02d}"
            wkey = (today, label)
            if (wh, wm) <= (now.hour, now.minute) <= (wh1, wm1) and wkey not in window_alerted:
                window_alerted.add(wkey)
                _send_window_alert(label, watchlist)

        # Exit checks run every poll cycle (not gated to the entry signal windows) —
        # the backtest evaluates TP/SL/TIME on every hourly bar once in a trade, so
        # live monitoring needs to check at least that often, not just twice a day.
        # SL/trailing checks are continuous (every poll); TP/TIME only fire when a
        # genuinely new hourly bar has closed since the last check, using that bar's
        # real Close/Low/High — not a live mid-bar tick — to match the backtest kernels.
        open_positions = get_open_positions()
        open_position_keys = {(p['ticker'], p['window']) for p in open_positions}
        for pos in open_positions:
            if tickers and pos['ticker'] not in tickers:
                continue
            df_hourly, _ = _load_cache(pos['ticker'])
            if df_hourly is None or df_hourly.empty:
                continue
            last_bar_ts = df_hourly.index[-1]
            if (pos['id'], last_bar_ts) in sell_alerted:
                continue
            at_bar_close = last_seen_bar.get(pos['ticker']) != last_bar_ts
            if at_bar_close:
                last_seen_bar[pos['ticker']] = last_bar_ts
                bar = df_hourly.iloc[-1]
                cp, low, high = float(bar['Close']), float(bar['Low']), float(bar['High'])
            else:
                cp, _ = _current_price(pos['ticker'])
                if cp is None:
                    continue
                low = high = cp
            reason, target, just_activated_trailing = check_sell_condition(
                pos, cp, now, at_bar_close=at_bar_close, low=low, high=high, df_hourly=df_hourly)
            if just_activated_trailing:
                notify_trailing_activated(pos, cp)
            if reason:
                notify_sell_signal(pos, reason, cp, target)
                sell_alerted.add((pos['id'], last_bar_ts))

        check_trailing_reminders(open_positions)
        check_exit_reminders(open_positions)
        check_buy_reminders()

        if not watchlist:
            print(f"[{now.strftime('%H:%M:%S')}] Watch list empty — add nodes with: python active_signals.py add")
            time.sleep(POLL_SECS)
            continue

        # Intrabar fill detection for limit-entry nodes (all day, not just signal window)
        for node in watchlist:
            if node.get('mode') != 'live':
                continue
            if node.get('strategy') != 'LimitOrderZScoreBreakout':
                continue
            fill_key = (node['ticker'], node['window'], today)
            if fill_key in limit_fill_alerted:
                continue
            cp, _ = _current_price(node['ticker'])
            if cp is None:
                continue
            sig = compute_buy_signal(node)
            if sig is None:
                continue
            if cp <= sig['lower_band']:
                limit_fill_alerted.add(fill_key)
                notify_limit_fill(node, cp, sig['lower_band'])

        in_window = _in_buy_window(now)
        summaries = []
        if in_window:
            for node in watchlist:
                sig = compute_buy_signal(node)
                if sig is None:
                    summaries.append(f"{node['ticker']} w={node['window']} NO_DATA")
                    continue

                alert_key = (sig['ticker'], node['strategy'], sig['window'])

                if sig['signal'] == 'BUY' and alert_key not in buy_alerted:
                    buy_alerted.add(alert_key)
                    if (sig['ticker'], sig['window']) in open_position_keys:
                        print(f"  [skip] BUY {sig['ticker']} z={sig['z_score']:+.2f} — position already open, no alert")
                    elif node.get('mode', 'live') == 'live':
                        notify_buy_signal(node, sig)
                    else:
                        print(f"  [research] BUY: {node['ticker']} z={sig['z_score']:+.2f} (no alert)")
                else:
                    mode_tag = ' [R]' if node.get('mode') == 'research' else ''
                    summaries.append(
                        f"{sig['ticker']}{mode_tag} z={sig['z_score']:+.2f} {sig['signal']}"
                    )
        else:
            summaries.append(f"outside signal window — next: 10:25 or 14:55 ET")

        if summaries:
            print(f"[{now.strftime('%H:%M:%S')}] {' | '.join(summaries)}")

        time.sleep(POLL_SECS)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_list():
    ensure_tables()
    wl = get_watchlist()
    if not wl:
        print("Watch list is empty.")
        return
    hdr = f"{'ID':<4} {'Ticker':<7} {'Win':<4} {'TP':<4} {'SL':<4} {'Hold':<6} {'Label':<20} Added"
    print(hdr)
    print('-' * len(hdr))
    for n in wl:
        print(
            f"{n['id']:<4} {n['ticker']:<7} {n['window']:<4} {_tp_or_arm_pct(n)!s:<4} "
            f"{n['stop_loss']:<4} {n['max_hold_hours']:<6} {(n.get('label') or ''):<20} {n['added_at']}"
        )


def cmd_positions():
    ensure_tables()
    positions = get_open_positions()
    if not positions:
        print("No open positions.")
        return
    hdr = f"{'ID':<4} {'Ticker':<7} {'Entry Price':<13} {'Entry Time':<22} {'Bars Held':<9} {'TP%':<5} {'SL%':<5} {'Hold'}"
    print(hdr)
    print('-' * len(hdr))
    for p in positions:
        signal_time = datetime.strptime(p['signal_time'], '%Y-%m-%d %H:%M:%S')
        df_hourly_p, _ = _load_cache(p['ticker'])
        hours = _bars_held(df_hourly_p, signal_time)
        print(
            f"{p['id']:<4} {p['ticker']:<7} ${p['entry_price']:<12.4f} "
            f"{p['entry_time']:<22} {hours:<9} {_tp_or_arm_pct(p)!s:<5} "
            f"{p['stop_loss']:<5} {p['max_hold_hours']}"
        )


def cmd_add():
    ensure_tables()
    print("Add node to watch list (values from backtest_cache):")
    ticker         = input("  ticker: ").strip().upper()
    strategy       = input("  strategy [ZScoreBreakout]: ").strip() or "ZScoreBreakout"
    version        = input("  version [v1.4]: ").strip() or "v1.4"
    window         = int(input("  window: ").strip())
    take_profit    = int(input("  take_profit: ").strip())
    stop_loss      = int(input("  stop_loss: ").strip())
    max_hold_hours    = int(input("  max_hold_hours: ").strip())
    z_score_threshold = float(input("  z_score_threshold [2.0]: ").strip() or "2.0")
    label             = input("  label (optional): ").strip()
    add_node(ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours, label, z_score_threshold)
    print(f"Added {ticker} (w={window} TP={take_profit} SL={stop_loss} hold={max_hold_hours}h Z={z_score_threshold}) label='{label}'.")


def cmd_remove():
    ensure_tables()
    cmd_list()
    if not get_watchlist():
        return
    watch_id = int(input("ID to remove: ").strip())
    remove_node(watch_id)
    print(f"Removed ID {watch_id}.")


_CMDS = {
    'run':       run_loop,
    'list':      cmd_list,
    'add':       cmd_add,
    'remove':    cmd_remove,
    'positions': cmd_positions,
}

if __name__ == '__main__':
    args = sys.argv[1:]
    cmd  = args[0] if args else 'run'

    if cmd in ('run', ) or cmd not in _CMDS:
        tickers = None
        if '--ticker' in args:
            idx     = args.index('--ticker')
            tickers = {t.strip().upper() for t in args[idx + 1].split(',')}
        run_loop(tickers=tickers)
    else:
        _CMDS[cmd]()
