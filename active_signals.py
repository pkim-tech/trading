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

import io
import os
import sys
import json
import time
import sqlite3
import threading
import contextlib
import requests
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from io import BytesIO
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from data_manager import fetch_live_data_smart
import strategies

load_dotenv()

DB_PATH    = Path("./cache/trading_universe.db")
CACHE_DIR  = Path("./cache")
POLL_SECS  = int(os.environ.get("SIGNAL_POLL_SECS", 300))
SLACK_HOOK = os.environ.get("SLACK_WEBHOOK_URL", "")

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
        c.executescript("""
            CREATE TABLE IF NOT EXISTS watch_list (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker         TEXT NOT NULL,
                strategy       TEXT NOT NULL,
                version        TEXT NOT NULL,
                window         INTEGER NOT NULL,
                take_profit    INTEGER NOT NULL,
                stop_loss      INTEGER NOT NULL,
                max_hold_hours INTEGER NOT NULL,
                label          TEXT DEFAULT '',
                added_at       TEXT DEFAULT (datetime('now')),
                UNIQUE(ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours)
            );

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
            );

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
            );
        """)
        existing_cols = [r[1] for r in c.execute("PRAGMA table_info(open_positions)").fetchall()]
        if 'trade_log_id' not in existing_cols:
            c.execute("ALTER TABLE open_positions ADD COLUMN trade_log_id INTEGER")
        c.commit()


# ---------------------------------------------------------------------------
# Watch list CRUD
# ---------------------------------------------------------------------------

def get_watchlist():
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM watch_list ORDER BY ticker, id"
        ).fetchall()]


def add_node(ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours, label=''):
    with _conn() as c:
        c.execute("""
            INSERT OR IGNORE INTO watch_list
                (ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours, label)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (ticker, strategy, version, int(window), int(take_profit),
              int(stop_loss), int(max_hold_hours), label))
        c.commit()


def remove_node(watch_id):
    with _conn() as c:
        c.execute("DELETE FROM watch_list WHERE id = ?", (watch_id,))
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
        return [dict(r) for r in c.execute(
            "SELECT * FROM open_positions ORDER BY entry_time"
        ).fetchall()]


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
                 signal_price, signal_time, entry_price, entry_time, trade_log_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            node['ticker'], node['strategy'], node['version'],
            int(node['window']), int(node['take_profit']), int(node['stop_loss']),
            int(node['max_hold_hours']),
            float(signal_price), sig_time_str,
            float(entry_price), entry_time_str, trade_log_id,
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

def compute_buy_signal(node):
    ticker = node['ticker']
    window = int(node['window'])

    strategy_cls = getattr(strategies, node['strategy'], None)
    if strategy_cls is None:
        return None

    df_hourly, df_daily = _load_cache(ticker)
    if df_hourly is None or len(df_daily) < window:
        return None

    strat = strategy_cls(window=window)
    today = pd.Timestamp.now().normalize()
    indicators = strat.generate_daily_indicators(df_daily[df_daily.index < today])
    if indicators.empty:
        return None

    last_row      = indicators.iloc[-1]
    close_series  = df_hourly['Close'].dropna()
    current_price = close_series.iloc[-1]
    last_bar      = close_series.index[-1]
    sma           = last_row['SMA']
    std           = last_row['Std']

    return {
        'ticker':        ticker,
        'window':        window,
        'current_price': current_price,
        'sma':           sma,
        'std':           std,
        'lower_band':    sma - 2.0 * std,
        'z_score':       (current_price - sma) / std,
        'signal':        strat.check_signal(current_price, last_row),
        'last_bar':      last_bar,
    }


def check_sell_condition(pos, current_price, now):
    strategy_cls = getattr(strategies, pos['strategy'], None)
    if strategy_cls is None:
        return None, None
    entry_time = datetime.strptime(pos['entry_time'], '%Y-%m-%d %H:%M:%S')
    hours_held = (now - entry_time).total_seconds() / 3600
    strat      = strategy_cls(window=pos['window'])
    return strat.check_exit(
        current_price, pos['entry_price'],
        pos['take_profit'], pos['stop_loss'],
        hours_held, pos['max_hold_hours']
    )


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

    today      = pd.Timestamp.now().normalize()
    cutoff     = df_hourly.index[-1] - pd.Timedelta(days=30)
    df_plot    = df_hourly[df_hourly.index >= cutoff]['Close'].dropna()
    strat      = getattr(strategies, node['strategy'])(window=window)
    indicators = strat.generate_daily_indicators(df_daily[df_daily.index < today])

    sma_h   = indicators['SMA'].reindex(df_plot.index, method='ffill')
    std_h   = indicators['Std'].reindex(df_plot.index, method='ffill')
    upper_h = sma_h + 2 * std_h
    lower_h = sma_h - 2 * std_h

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(df_plot.index, df_plot.values, color='#4c9be8', linewidth=1, label='Price')
    ax.plot(sma_h.index, sma_h.values, color='#f0a500', linewidth=1, label=f'SMA({window})')
    ax.fill_between(df_plot.index, lower_h, upper_h, alpha=0.12, color='#f0a500')
    ax.plot(lower_h.index, lower_h.values, color='#f0a500', linewidth=0.6, linestyle='--')
    ax.axvline(sig['last_bar'], color='#2ecc71', linewidth=1.5, linestyle='--', alpha=0.8)
    ax.scatter([sig['last_bar']], [sig['current_price']], color='#2ecc71', s=60, zorder=5)
    ax.set_title(f"{ticker}  BUY SIGNAL  |  z = {sig['z_score']:.2f}  |  window={window}", fontsize=11)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    ax.legend(fontsize=8)
    fig.tight_layout()

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
    ticker  = sig['ticker']
    price   = sig['current_price']
    z       = sig['z_score']
    bar_str = sig['last_bar'].strftime('%Y-%m-%d %H:%M')

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"BUY SIGNAL — {ticker}"}},
        _fields_block({
            "Price":      f"${price:.4f}",
            "Lower Band": f"${sig['lower_band']:.4f}",
            "Z-Score":    f"{z:.2f}",
            "Bar":        bar_str,
            "Window":     str(node['window']),
            "TP / SL":    f"{node['take_profit']}% / {node['stop_loss']}%",
            "Max Hold":   f"{node['max_hold_hours']}h",
            "SMA":        f"${sig['sma']:.4f}",
        }),
    ]

    if SOCKET_MODE:
        value = json.dumps({
            "type":         "buy",
            "node":         {k: node[k] for k in ('ticker', 'strategy', 'version', 'window',
                                                    'take_profit', 'stop_loss', 'max_hold_hours', 'label')},
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
    reason_labels = {'TP': 'TAKE PROFIT', 'SL': 'STOP LOSS', 'TIME': 'TIME EXIT'}
    ticker     = pos['ticker']
    ep         = pos['entry_price']
    entry_time = pos['entry_time']
    pct        = (current_price - ep) / ep * 100

    blocks = [
        {"type": "header", "text": {"type": "plain_text",
         "text": f"SELL SIGNAL — {ticker} ({reason_labels[reason]})"}},
        _fields_block({
            "Entry Price":   f"${ep:.4f}",
            "Current Price": f"${current_price:.4f}",
            "P&L":           f"{pct:+.2f}%",
            "Target":        f"${target_price:.4f}",
            "TP / SL":       f"{pos['take_profit']}% / {pos['stop_loss']}%",
            "Max Hold":      f"{pos['max_hold_hours']}h",
            "Entered":       entry_time,
        }),
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

    sep = '=' * 62
    print(f"\n{sep}")
    print(f"  BUY SIGNAL  {ticker}  {bar_str}")
    print(f"  Price:  ${price:.4f}   Lower band: ${sig['lower_band']:.4f}   z = {z:.2f}")
    print(f"  Node:   window={node['window']}  TP={tp}%  SL={sl}%  hold={hold}h")
    print(f"  SMA: ${sig['sma']:.4f}   Std: ${sig['std']:.4f}")
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


def notify_sell_signal(pos, reason, current_price, target_price):
    ticker     = pos['ticker']
    ep         = pos['entry_price']
    entry_time = pos['entry_time']
    pct        = (current_price - ep) / ep * 100

    reason_labels = {'TP': 'TAKE PROFIT', 'SL': 'STOP LOSS', 'TIME': 'TIME EXIT'}

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


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_loop(tickers: set = None):
    ensure_tables()
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

    buy_alerted:  set[tuple] = set()
    sell_alerted: set[int]   = set()
    last_date = datetime.now().strftime('%Y-%m-%d')

    while True:
        now   = datetime.now()
        today = now.strftime('%Y-%m-%d')

        if today != last_date:
            buy_alerted.clear()
            last_date = today

        watchlist = get_watchlist()
        if tickers:
            watchlist = [n for n in watchlist if n['ticker'] in tickers]
        refresh_tickers = {p['ticker'] for p in get_open_positions()} | {n['ticker'] for n in watchlist}
        for t in sorted(refresh_tickers):
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    fetch_live_data_smart(t)
            except Exception as e:
                print(f"  [data] {t} refresh failed: {e}")

        for pos in get_open_positions():
            if tickers and pos['ticker'] not in tickers:
                continue
            if pos['id'] in sell_alerted:
                continue
            cp, _ = _current_price(pos['ticker'])
            if cp is None:
                continue
            reason, target = check_sell_condition(pos, cp, now)
            if reason:
                notify_sell_signal(pos, reason, cp, target)
                sell_alerted.add(pos['id'])

        if not watchlist:
            print(f"[{now.strftime('%H:%M:%S')}] Watch list empty — add nodes with: python active_signals.py add")
            time.sleep(POLL_SECS)
            continue

        summaries = []
        for node in watchlist:
            sig = compute_buy_signal(node)
            if sig is None:
                summaries.append(f"{node['ticker']} w={node['window']} NO_DATA")
                continue

            alert_key = (sig['ticker'], node['strategy'], sig['window'])

            if sig['signal'] == 'BUY' and alert_key not in buy_alerted:
                buy_alerted.add(alert_key)
                notify_buy_signal(node, sig)
            else:
                summaries.append(
                    f"{sig['ticker']} z={sig['z_score']:+.2f} {sig['signal']}"
                )

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
    now = datetime.now()
    hdr = f"{'ID':<4} {'Ticker':<7} {'Entry Price':<13} {'Entry Time':<22} {'Hours':<7} {'TP%':<5} {'SL%':<5} {'Hold'}"
    print(hdr)
    print('-' * len(hdr))
    for p in positions:
        et    = datetime.strptime(p['entry_time'], '%Y-%m-%d %H:%M:%S')
        hours = (now - et).total_seconds() / 3600
        print(
            f"{p['id']:<4} {p['ticker']:<7} ${p['entry_price']:<12.4f} "
            f"{p['entry_time']:<22} {hours:<7.1f} {p['take_profit']:<5} "
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
    max_hold_hours = int(input("  max_hold_hours: ").strip())
    label          = input("  label (optional): ").strip()
    add_node(ticker, strategy, version, window, take_profit, stop_loss, max_hold_hours, label)
    print(f"Added {ticker} (w={window} TP={take_profit} SL={stop_loss} hold={max_hold_hours}h) label='{label}'.")


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
