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

DB_PATH     = Path("./cache/trading_universe.db")
CACHE_DIR   = Path("./cache")
CONFIG_PATH = Path("./config.json")
POLL_SECS  = int(os.environ.get("SIGNAL_POLL_SECS", 300))
SLACK_HOOK = os.environ.get("SLACK_WEBHOOK_URL", "")

LOG_DIR = Path("./logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
HUMAN_LOG_PATH   = LOG_DIR / "active_signals.log"
VERBOSE_LOG_PATH = LOG_DIR / "active_signals_verbose.log"


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
                    take_profit       INTEGER NOT NULL,
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
                    take_profit       INTEGER NOT NULL,
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
        if 'trail_pct' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN trail_pct REAL")
        if 'fixed_sl' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN fixed_sl REAL")
        if 'trail_buy_pct' not in wl_cols:
            c.execute("ALTER TABLE watch_list ADD COLUMN trail_buy_pct REAL")

        # open_positions
        c.execute("""
            CREATE TABLE IF NOT EXISTS open_positions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker         TEXT NOT NULL,
                strategy       TEXT NOT NULL,
                version        TEXT NOT NULL,
                window         INTEGER NOT NULL,
                take_profit    INTEGER NOT NULL,
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
        if 'trail_pct' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN trail_pct REAL")
        if 'fixed_sl' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN fixed_sl REAL")
        if 'trail_buy_pct' not in op_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN trail_buy_pct REAL")

        # trade_log
        c.execute("""
            CREATE TABLE IF NOT EXISTS trade_log (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker              TEXT NOT NULL,
                strategy            TEXT NOT NULL,
                version             TEXT NOT NULL,
                window              INTEGER NOT NULL,
                take_profit         INTEGER NOT NULL,
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
                exit_reason         TEXT
            )
        """)
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


def _uses_fixed_sl(strategy_name):
    """v1.8/v1.9/v1.10/v2.11: the swept 'stop_loss' column actually holds trail_pct/trail_buy_pct;
    the real fixed SL comes from config.execution.fixed_stop_loss, not the node's stop_loss field."""
    strategy_cls = getattr(strategies, strategy_name, None)
    return strategy_cls is not None and issubclass(
        strategy_cls, (strategies.TrailingExitZScoreBreakout, strategies.TrailingBuyZScoreBreakout,
                        strategies.LimitOrderTrailingExit))


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


def _resolve_axis_columns(strategy_name):
    """Which real column the legacy overloaded 'stop_loss' value maps to for this
    strategy — mirrors run_optimization_sweep.py::_resolve_axis_columns. Returns
    (sl_axis_column, fourth_axis_column_or_None)."""
    cls = getattr(strategies, strategy_name, None)
    if cls is None:
        return 'stop_loss', None
    if issubclass(cls, strategies.TrailingBothZScoreBreakout):
        return 'trail_buy_pct', 'trail_pct'
    if issubclass(cls, strategies.TrailingBuyZScoreBreakout):
        return 'trail_buy_pct', None
    if issubclass(cls, (strategies.TrailingExitZScoreBreakout, strategies.LimitOrderTrailingExit)):
        return 'trail_pct', None
    return 'stop_loss', None


def add_node(ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
             label='', z_score_threshold=2.0, watchlist_id=None, mode='live',
             trail_buy_pct=None, trail_pct=None):
    """trail_buy_pct/trail_pct: pass the real values directly for v3.x nodes (where
    backtest_cache has real named columns). Omit both for legacy v1.x/v2.x nodes —
    falls back to reinterpreting stop_loss the way it's always meant for the 4
    trailing strategies (see docs/design.md 'Grid axis meaning by strategy')."""
    if watchlist_id is None:
        watchlist_id = get_active_watchlist_id()
    if _uses_fixed_sl(strategy):
        fixed_sl = _config_fixed_stop_loss()
        if trail_buy_pct is None and trail_pct is None:
            sl_axis_col, fourth_axis_col = _resolve_axis_columns(strategy)
            if sl_axis_col == 'trail_buy_pct':
                stored_trail_buy_pct = float(stop_loss)
                stored_trail_pct = (_LEGACY_TRAILING_BOTH_TRAIL_PCT.get(version, 3.0)
                                     if fourth_axis_col == 'trail_pct' else 0.0)
            else:
                stored_trail_buy_pct = 0.0
                stored_trail_pct = float(stop_loss)
        else:
            stored_trail_pct = trail_pct if trail_pct is not None else 0.0
            stored_trail_buy_pct = trail_buy_pct if trail_buy_pct is not None else 0.0
    else:
        fixed_sl = None
        stored_trail_pct = None
        stored_trail_buy_pct = None
    with _conn() as c:
        c.execute("""
            INSERT OR IGNORE INTO watch_list
                (watchlist_id, mode, ticker, strategy, version, window, take_profit,
                 stop_loss, max_hold_hours, label, z_score_threshold, trail_pct, fixed_sl, trail_buy_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (watchlist_id, mode, ticker, strategy, version, int(window), int(take_profit),
              int(stop_loss), int(max_hold_hours), label, float(z_score_threshold),
              stored_trail_pct, fixed_sl, stored_trail_buy_pct))
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


def update_position_trail_state(position_id, state):
    with _conn() as c:
        c.execute("UPDATE open_positions SET trail_state = ? WHERE id = ?",
                  (json.dumps(state), position_id))


def open_position(node, signal_price, signal_time, entry_price, entry_time):
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
        trade_log_id = log_trade_entry(node, signal_price, signal_time, entry_price, entry_time)
        c.execute("""
            INSERT INTO open_positions
                (ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
                 signal_price, signal_time, entry_price, entry_time, trade_log_id,
                 trail_pct, fixed_sl, trail_buy_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            node['ticker'], node['strategy'], node['version'],
            int(node['window']), int(node['take_profit']), int(node['stop_loss']),
            int(node['max_hold_hours']),
            float(signal_price), sig_time_str,
            float(entry_price), entry_time_str, trade_log_id,
            node.get('trail_pct'), node.get('fixed_sl'), node.get('trail_buy_pct'),
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


def log_trade_entry(node, signal_price, signal_time, entry_price, entry_time):
    sig_time_str   = signal_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(signal_time, 'strftime') else signal_time
    entry_time_str = entry_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(entry_time, 'strftime') else entry_time
    entry_drift    = (entry_price - signal_price) / signal_price * 100
    with _conn() as c:
        c.execute("""
            INSERT INTO trade_log
                (ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours,
                 signal_price, signal_time, entry_price, entry_time, entry_drift_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            node['ticker'], node['strategy'], node['version'],
            int(node['window']), int(node['take_profit']), int(node['stop_loss']),
            int(node['max_hold_hours']),
            float(signal_price), sig_time_str,
            float(entry_price), entry_time_str, entry_drift,
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
        with sqlite3.connect(DB_PATH) as c:
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
    if _uses_fixed_sl(pos['strategy']):
        real_sl_pct = pos.get('fixed_sl') or 0.0
        trail_pct   = (pos.get('trail_pct') or 3.0) / 100.0
    else:
        real_sl_pct = pos['stop_loss']
        trail_pct   = 0.03
    strat      = strategy_cls(window=pos['window'], trail_pct=trail_pct)
    old_state  = pos.get('trail_state', {})
    reason, price, new_state = strat.check_exit({
        'current_price':     current_price,
        # Real bar Low/High when this call represents an actual closed hourly bar;
        # otherwise current_price is the best available proxy for a mid-bar poll.
        'low':               low if low is not None else current_price,
        'high':              high if high is not None else current_price,
        'entry_price':       pos['entry_price'],
        'take_profit':       pos['take_profit'] / 100.0,
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
    ax.set_title(f"w{window} z{z_thresh:g} tp{node['take_profit']} sl{node['stop_loss']}",
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

    ep         = pos['entry_price']
    tp_price   = ep * (1 + pos['take_profit'] / 100)
    sl_price   = ep * (1 - pos['stop_loss'] / 100)
    entry_time = datetime.strptime(pos['entry_time'], '%Y-%m-%d %H:%M:%S')
    pct        = (current_price - ep) / ep * 100

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(df_plot.index, df_plot.values, color='#4c9be8', linewidth=1, label='Price')
    ax.plot(sma_h.index, sma_h.values, color='#f0a500', linewidth=1, label=f'SMA({window})')
    ax.fill_between(df_plot.index, lower_h, upper_h, alpha=0.12, color='#f0a500')
    ax.axhline(tp_price, color='#2ecc71', linewidth=1, linestyle='--', label=f'TP ${tp_price:.2f}')
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
    if SOCKET_MODE:
        try:
            bolt_app.client.chat_postMessage(channel=SLACK_CHANNEL, text=text, blocks=blocks)
        except Exception as e:
            print(f"  [slack error] {e}")
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


def _build_buy_blocks(node, sig):
    ticker    = sig['ticker']
    price     = sig['current_price']
    z         = sig['z_score']
    bar_str   = sig['last_bar'].strftime('%Y-%m-%d %H:%M')
    tp_price  = price * (1 + node['take_profit'] / 100)
    sl_price  = price * (1 - node['stop_loss']   / 100)

    hurst_str = f"{sig['hurst']:.3f}" if sig.get('hurst') is not None else "n/a"
    adf_str   = f"{sig['adf_p']:.3f}" if sig.get('adf_p')  is not None else "n/a"

    hold_deadline = _add_trading_hours(sig['last_bar'], node['max_hold_hours'])
    deadline_str  = hold_deadline.strftime('%a %b %d %H:%M')

    target_notional = 50_000
    shares = int(target_notional // price)
    schwab_sl_pct   = node['stop_loss'] + 1
    schwab_sl_price = sig['lower_band'] * (1 - schwab_sl_pct / 100)

    with _conn() as _c:
        vol_row = _c.execute("SELECT avg_vol_10d FROM tickers WHERE symbol=?", (ticker,)).fetchone()
    max_notional = vol_row['avg_vol_10d'] * price * 0.01 if vol_row and vol_row['avg_vol_10d'] else None
    max_shares = int(max_notional // price) if max_notional else None
    max_notional_str = f"  |  max `${max_notional/1000:.0f}k` / `{max_shares} shares` @ 1% vol" if max_notional else ""

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"🟢 *{ticker}* — BUY — Market — `${price:.2f}` — `{shares} shares` (~${target_notional/1000:.0f}k){max_notional_str}\n🔴 *{ticker}* — SELL ALL — Stop Loss — `${schwab_sl_price:.2f}` (-{schwab_sl_pct}% from trigger)"}},
    ]

    if SOCKET_MODE:
        value = json.dumps({
            "type":         "buy",
            "node":         {k: node.get(k) for k in ('ticker', 'strategy', 'version', 'window',
                                                        'take_profit', 'stop_loss', 'max_hold_hours', 'label',
                                                        'trail_pct', 'fixed_sl')},
            "signal_price": price,
            "signal_time":  sig['last_bar'].strftime('%Y-%m-%d %H:%M:%S'),
            "lower_band":   sig['lower_band'],
            "z_score":      z,
        })
        blocks.append({
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Executed"},
                 "style": "primary", "action_id": "buy_executed", "value": value},
                {"type": "button", "text": {"type": "plain_text", "text": "Skipped"},
                 "action_id": "buy_skipped", "value": value},
            ],
        })
    else:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": "Reply with execution price when filled."}
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

    if SOCKET_MODE:
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
            {"type": "mrkdwn", "text": "Reply with exit price when filled."}
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

    @bolt_app.action("buy_skipped")
    def handle_buy_skipped(ack, body, client):
        ack()
        data    = json.loads(body['actions'][0]['value'])
        channel = body['channel']['id']
        ts      = body['message']['ts']
        ticker  = data['node']['ticker']
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

        open_position(node, signal_price, signal_time, exec_price, now)

        ticker = node['ticker']
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
        client.chat_update(
            channel=channel, ts=ts,
            text=f"SELL {ticker} — Skipped (position kept open)",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*SELL {ticker}* — Skipped (position kept open)"}}],
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


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify_buy_signal(node, sig):
    ticker   = sig['ticker']
    price    = sig['current_price']
    z        = sig['z_score']
    bar_time = sig['last_bar']
    bar_str  = bar_time.strftime('%Y-%m-%d %H:%M')
    tp       = node['take_profit']
    sl       = node['stop_loss']
    hold     = node['max_hold_hours']

    hurst_str = f"{sig['hurst']:.3f}" if sig.get('hurst') is not None else "n/a"
    adf_str   = f"{sig['adf_p']:.3f}" if sig.get('adf_p') is not None else "n/a"

    sep = '=' * 62
    print(f"\n{sep}")
    print(f"  BUY SIGNAL  {ticker}  {bar_str}")
    print(f"  Price:  ${price:.4f}   Lower band: ${sig['lower_band']:.4f}   z = {z:.2f}")
    print(f"  Node:   window={node['window']}  TP={tp}%  SL={sl}%  hold={hold}h")
    print(f"  SMA: ${sig['sma']:.4f}   Std: ${sig['std']:.4f}")
    print(f"  Hurst (100 bars): {hurst_str}   ADF p: {adf_str}")
    print(sep)

    _post_message(
        f"BUY SIGNAL — {ticker}  ${price:.4f}  z={z:.2f}  ({bar_str})",
        _build_buy_blocks(node, sig),
    )

    if SOCKET_MODE:
        chart = _chart_buy(node, sig)
        if chart:
            _upload_chart(chart, f"{ticker}_buy.png", f"BUY — {ticker}  z={z:.2f}")
        print("  Waiting for Slack response (Executed / Skipped).")
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
            note = f"Entered at ${exec_price:.4f}  (drift: {drift_pct:+.2f}%)"
            print(f"  Position opened. {note}")
            _post_message(f"{ticker} position opened: {note}")
        except ValueError:
            print("  Invalid price — position not opened.")
    else:
        print("  Skipped.")


def notify_limit_fill(node, current_price, lower_band):
    ticker        = node['ticker']
    schwab_sl_pct = node['stop_loss'] + 1
    schwab_sl_price = lower_band * (1 - schwab_sl_pct / 100)
    shares = int(50_000 // lower_band)
    now_str = datetime.now().strftime('%H:%M:%S')

    print(f"\n  [LIMIT FILL] {ticker}  price=${current_price:.2f}  trigger=${lower_band:.2f}  {now_str}")
    print(f"  Place stop: ${schwab_sl_price:.2f} (-{schwab_sl_pct}% from trigger)")

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"LIMIT FILLED — {ticker}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            f"✅ *{ticker}* limit filled at `${lower_band:.2f}` — `{shares} shares`\n"
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
    print(f"  Target: ${target_price:.4f}   Node: TP={pos['take_profit']}%  SL={pos['stop_loss']}%  hold={pos['max_hold_hours']}h")
    print(f"  Entered: {entry_time}")
    print(sep)

    _post_message(
        f"SELL SIGNAL — {ticker}  {reason_labels[reason]}  ${current_price:.4f}  ({pct:+.2f}%)",
        _build_sell_blocks(pos, reason, current_price, target_price),
    )

    if SOCKET_MODE:
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
                           exit_time=datetime.now(), exit_reason='MANUAL')
            print(f"  Position closed. {note}")
            _post_message(f"{ticker} position closed: {note}")
        except ValueError:
            print("  Invalid price — position kept open.")
    else:
        print("  Skipped — position kept open.")


def notify_trailing_activated(pos, current_price):
    ticker    = pos['ticker']
    ep        = pos['entry_price']
    pct       = (current_price - ep) / ep * 100
    trail_pct = pos.get('trail_pct', 0.03) * 100
    print(f"\n  [TRAILING ACTIVATED] {ticker}  entry=${ep:.4f}  now=${current_price:.4f}  ({pct:+.2f}%)")
    _post_message(
        f"TRAILING ACTIVATED — {ticker}  ${current_price:.4f}  ({pct:+.2f}%)",
        [{"type": "section", "text": {"type": "mrkdwn", "text": (
            f"🎯 *{ticker}* — TRAILING ACTIVATED — action needed\n"
            f"entry `${ep:.2f}`  |  current `${current_price:.2f}`  |  P&L `{pct:+.1f}%`\n"
            f"Take-profit cleared. Cancel the fixed Schwab stop and place a *trailing stop* order at `{trail_pct:.0f}%` "
            f"so it auto-adjusts with price at the broker — Slack polling alone won't catch it fast enough. "
            f"Next alert fires when the trailing stop or max hold triggers."
        )}}],
    )


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


def _send_window_alert(label, watchlist):
    rows = []
    for node in watchlist:
        sig = compute_buy_signal(node)
        if sig is None:
            rows.append((float('inf'), node['ticker'], None, None))
            continue
        pct_away = (sig['current_price'] - sig['lower_band']) / sig['lower_band'] * 100
        rows.append((pct_away, node['ticker'], sig, pct_away))
    rows.sort(key=lambda x: x[0])

    hot = [t for pct, t, _, __ in rows if pct < 5]
    alert_level = "🔶 *HIGH ALERT*" if hot else "✅ algo running"
    lines = [f"⏱ *Signal window — {label} ET* | {alert_level}"]
    for pct, ticker, sig, _ in rows:
        if sig is None:
            lines.append(f"  ⚫ {ticker}  NO_DATA")
        else:
            emoji = _proximity_emoji(pct)
            lines.append(f"  {emoji} *{ticker}*  now `${sig['current_price']:.2f}`  trigger `${sig['lower_band']:.2f}` ({pct:+.1f}%)")

    _post_message("\n".join(lines))


_STRATEGY_LABELS = {
    'ZScoreBreakout':             ('BUY (bar-close)', 'At signal close: edit staged limit → market and submit'),
    'TrendFilteredZScore':        ('BUY (bar-close)', 'At signal close: edit staged limit → market and submit'),
    'TrailingExitZScoreBreakout': ('BUY (bar-close, trailing exit)', 'At signal close: edit staged limit → market and submit'),
    'LimitOrderZScoreBreakout':   ('BUY (limit)', 'Pre-market: stage limit order at trigger price (absurdly low); confirm fill intrabar'),
}


def send_startup_report(watchlist):
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')

    # Build buy candidates sorted by proximity, carrying node for strategy info
    rows = []
    for node in [n for n in watchlist if n.get('mode') == 'live']:
        sig = compute_buy_signal(node)
        if sig is None:
            rows.append((float('inf'), node['ticker'], node['strategy'], node.get('version', ''), None, None, node))
            continue
        trigger  = sig['lower_band']
        tp_price = trigger * (1 + node['take_profit'] / 100)
        sl_price = trigger * (1 - node['stop_loss']  / 100)
        pct_away = (sig['current_price'] - trigger) / trigger * 100
        rows.append((pct_away, node['ticker'], node['strategy'], node.get('version', ''), sig, {'tp': tp_price, 'sl': sl_price, 'pct': pct_away}, node))
    rows.sort(key=lambda x: x[0])

    # Group by strategy
    from itertools import groupby
    strategy_order = []
    seen = set()
    for r in rows:
        s = r[2]
        if s not in seen:
            strategy_order.append(s)
            seen.add(s)
    by_strategy = {s: [r for r in rows if r[2] == s] for s in strategy_order}


    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Morning Report — {now_str}"}},
    ]

    for strategy, group in by_strategy.items():
        label, action = _STRATEGY_LABELS.get(strategy, (strategy, ''))
        versions = ', '.join(sorted({r[3] for r in group if r[3]}))
        header_text = f"{label} — {versions}" if versions else label
        blocks.append({"type": "divider"})
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": header_text}})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": action}]})

        for _, ticker, _strat, version, sig, meta, node in group:
            if sig is None:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"⚫ *{ticker}* `{version}`  NO_DATA"}})
                continue
            emoji = _proximity_emoji(meta['pct'])
            overnight = (sig['current_price'] - sig['prev_close']) / sig['prev_close'] * 100
            data_date = pd.Timestamp(sig['last_daily_bar']).strftime('%m/%d')
            buy_type, node_action = _STRATEGY_LABELS.get(_strat, (_strat, ''))
            text = (
                f"{emoji} *{ticker}* — {buy_type} — `{version}` — trigger `${sig['lower_band']:.2f}`\n"
                f"→ _{node_action}_\n"
                f"now `${sig['current_price']:.2f}` ({overnight:+.1f}% O/N)  z `{sig['z_score']:+.2f}`\n"
                f"      trigger `${sig['lower_band']:.2f}` ({meta['pct']:+.1f}%)  tp `${meta['tp']:.2f}`  sl `${meta['sl']:.2f}`  "
                f"close `${sig['prev_close']:.2f}`  data `{data_date}`"
            )
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
            if meta['pct'] < 5:
                chart = _chart_buy(node, sig)
                if chart:
                    _upload_chart(chart, f"{ticker}_morning.png", f"{ticker} `{version}`  z={sig['z_score']:+.2f}")

    # Open positions section
    positions = get_open_positions()
    if positions:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Open Positions*"}})
        for p in positions:
            signal_time = datetime.fromisoformat(p['signal_time'])
            df_hourly_p, _ = _load_cache(p['ticker'])
            hours_held = _bars_held(df_hourly_p, signal_time)
            cp, _ = _current_price(p['ticker'])
            if cp:
                pnl = (cp - p['entry_price']) / p['entry_price'] * 100
                pnl_str = f"  P&L `{pnl:+.1f}%`"
            else:
                pnl_str = ""
            text = (
                f"📊 *{p['ticker']}*  "
                f"entry `${p['entry_price']:.2f}`  "
                f"held `{hours_held:.0f}h`  "
                f"tp `{p['take_profit']}%`  sl `{p['stop_loss']}%`"
                f"{pnl_str}"
            )
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    else:
        blocks.append({"type": "divider"})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "No open positions."}]})

    # Reconfirm reminder for hot tickers
    hot = [r[1] for r in rows if isinstance(r[0], float) and r[0] < 5]
    if hot:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"⏰ *Reconfirm limit order:* {', '.join(hot)} — signal window 10:25–10:40 AM and 3:25–3:40 PM ET"}})

    # Console output
    print(f"Morning Report — {now_str}")
    for _, ticker, strategy, version, sig, meta, _node in rows:
        if sig is None:
            print(f"  {ticker:<6} {version}  NO_DATA  [{strategy}]")
        else:
            emoji = _proximity_emoji(meta['pct'])
            label = _STRATEGY_LABELS.get(strategy, (strategy,))[0]
            print(f"  {emoji} {ticker:<6} {version}  now=${sig['current_price']:>7.2f}  trigger=${sig['lower_band']:>7.2f}  ({meta['pct']:+.1f}%)  z={sig['z_score']:>+5.2f}  [{label}]")
    if positions:
        print("  Open positions:")
        for p in positions:
            signal_time = datetime.fromisoformat(p['signal_time'])
            df_hourly_p, _ = _load_cache(p['ticker'])
            hours_held = _bars_held(df_hourly_p, signal_time)
            print(f"    {p['ticker']:<6}  entry=${p['entry_price']:.2f}  held={hours_held:.0f}h")

    _post_message(f"Morning Report — {now_str}", blocks=blocks)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

# Signal windows in ET: 10:25-10:40 (9:30 bar close) and 15:25-15:40 (14:30 bar close)
_SIGNAL_WINDOWS = [(10, 25, 10, 40), (15, 25, 15, 40)]

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
    send_startup_report(startup_wl)

    buy_alerted:        set[tuple] = set()
    sell_alerted:       set[tuple] = set()  # (position_id, bar_ts) — dedups within a bar, not across bars
    window_alerted:     set[tuple] = set()
    limit_fill_alerted: set[tuple] = set()
    last_seen_bar:      dict       = {}   # ticker -> last hourly bar timestamp checked
    last_date = datetime.now().strftime('%Y-%m-%d')
    last_morning_report_date = datetime.now().strftime('%Y-%m-%d') if datetime.now().hour >= 7 else None

    while True:
        now   = datetime.now()
        today = now.strftime('%Y-%m-%d')

        if today != last_date:
            buy_alerted.clear()
            window_alerted.clear()
            limit_fill_alerted.clear()
            last_date = today

        if now.hour >= 7 and today != last_morning_report_date:
            wl = get_watchlist()
            if tickers:
                wl = [n for n in wl if n['ticker'] in tickers]
            send_startup_report(wl)
            last_morning_report_date = today

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
        for pos in get_open_positions():
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
                    if node.get('mode', 'live') == 'live':
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
            f"{n['id']:<4} {n['ticker']:<7} {n['window']:<4} {n['take_profit']:<4} "
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
            f"{p['entry_time']:<22} {hours:<9} {p['take_profit']:<5} "
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
