"""
Slack-facing layer: chart generation, message/block builders, notify_*
functions, reminder loops, the Bolt interactive handlers, and the
reference-table/morning-report builder.
"""
import json
import sqlite3
from datetime import datetime
from io import BytesIO

import numpy as np
import pandas as pd
import requests
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

import strategies
import signals_config as cfg
import signals_db as db
import signals_compute as compute


# ---------------------------------------------------------------------------
# Chart generation
# ---------------------------------------------------------------------------

def _upload_chart(buf: BytesIO, filename: str, title: str):
    if not cfg.SOCKET_MODE or not cfg.SLACK_CHANNEL_ID:
        return
    try:
        cfg.bolt_app.client.files_upload_v2(
            channel=cfg.SLACK_CHANNEL_ID,
            file=buf,
            filename=filename,
            title=title,
        )
    except Exception as e:
        print(f"  [chart] upload failed: {e}")


def _chart_buy(node, sig) -> BytesIO | None:
    ticker = sig['ticker']
    window = int(node['window'])
    df_hourly, df_daily = compute._load_cache(ticker)
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
    ax.set_title(f"w{window} z{z_thresh:g} arm{db._tp_or_arm_pct(node)} sl{node['stop_loss'] + 1}",
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
    df_hourly, df_daily = compute._load_cache(ticker)
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
    arm_price     = ep * (1 + db._tp_or_arm_pct(pos) / 100)
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
    if cfg.SIM_MODE:
        scenario_suffix = f" ({cfg.SIM_SCENARIO})" if cfg.SIM_SCENARIO else ""
        text = f"🧪 SIM{scenario_suffix} — {text}"
        if blocks:
            # A dedicated marker block, not a rewrite of the first block's text --
            # the prior approach only patched "header"-type blocks, so any message
            # built from "section" blocks (most of them) silently shipped with no
            # visible SIM tag at all, regardless of block composition.
            scenario_str = f": {cfg.SIM_SCENARIO}" if cfg.SIM_SCENARIO else ""
            header_marker = {"type": "context", "elements": [{"type": "mrkdwn", "text": f"🧪 *SIM MODE{scenario_str}*"}]}
            footer_marker = {"type": "context", "elements": [{"type": "mrkdwn", "text": "🧪 *SIM MODE END*"}]}
            blocks = [header_marker] + blocks + [footer_marker]
    if cfg.SOCKET_MODE:
        try:
            resp = cfg.bolt_app.client.chat_postMessage(channel=cfg.SLACK_CHANNEL, text=text, blocks=blocks)
            return resp['channel'], resp['ts']
        except Exception as e:
            print(f"  [slack error] {e}")
            return None, None
    elif cfg.SLACK_HOOK:
        payload = {'text': text}
        if blocks:
            payload['blocks'] = blocks
        try:
            r = requests.post(cfg.SLACK_HOOK, json=payload, timeout=5)
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
        with sqlite3.connect(cfg.RESEARCH_DB_PATH) as _c:
            _c.row_factory = sqlite3.Row
            vol_row = _c.execute("SELECT avg_vol_10d FROM tickers WHERE symbol=?", (ticker,)).fetchone()
        avg_vol_10d = vol_row['avg_vol_10d'] if vol_row else None
        if avg_vol_10d and node.get('id') is not None:
            with db._conn() as _c:
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
    if (node.get('account') or '').lower() != 'brokerage' and db.closed_today(ticker):
        warning_line = (
            f"\n⚠️🔁 *SAME DAY BUY WARNING:* {ticker} already sold today in a "
            f"{node.get('account', 'non-brokerage')} account — cash may not be settled (T+1). Confirm funds are available before entering."
        )

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"{entry_line}\n🔴 *{ticker}* — SELL ALL — Stop Loss — `${schwab_sl_price:.2f}` (-{schwab_sl_pct}% from trigger){warning_line}"}},
    ]

    trailing_buy = db._is_trailing_buy(node)

    if cfg.INTERACTIVE:
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

    if cfg.INTERACTIVE:
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

if cfg.SOCKET_MODE:

    @cfg.bolt_app.action("buy_executed")
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

    @cfg.bolt_app.action("trail_buy_order_placed")
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
        db.mark_pending_buy_placed(ticker)
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

    @cfg.bolt_app.action("trail_buy_filled")
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

    @cfg.bolt_app.action("trail_buy_cancelled")
    def handle_trail_buy_cancelled(ack, body, client):
        ack()
        data    = json.loads(body['actions'][0]['value'])
        channel = body['channel']['id']
        ts      = body['message']['ts']
        ticker  = data['node']['ticker']
        db.clear_pending_buy(ticker)
        client.chat_update(
            channel=channel, ts=ts,
            text=f"BUY {ticker} — order cancelled",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*BUY {ticker}* — trailing buy order cancelled, no position"}}],
        )

    @cfg.bolt_app.view("trail_buy_fill_price_submit")
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

        db.open_position(node, signal_price, signal_time, fill_price, datetime.now(), shares=shares)
        db.clear_pending_buy(ticker)

        note = f"${fill_price:.4f}  (drift: {drift_pct:+.2f}%)  {shares} shares"
        print(f"  Trailing buy filled via Slack: {ticker} at {note}")
        client.chat_update(
            channel=channel, ts=ts,
            text=f"BUY {ticker} — Filled at {note}",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*BUY {ticker}* — Filled at {note}"}}],
        )

    @cfg.bolt_app.action("buy_skipped")
    def handle_buy_skipped(ack, body, client):
        ack()
        data    = json.loads(body['actions'][0]['value'])
        channel = body['channel']['id']
        ts      = body['message']['ts']
        ticker  = data['node']['ticker']
        db.clear_pending_buy(ticker)
        client.chat_update(
            channel=channel, ts=ts,
            text=f"BUY {ticker} — Skipped",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*BUY {ticker}* — Skipped"}}],
        )

    @cfg.bolt_app.view("entry_price_submit")
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

        db.open_position(node, signal_price, signal_time, exec_price, now, shares=shares)

        ticker = node['ticker']
        db.clear_pending_buy(ticker)
        note   = f"${exec_price:.4f}  (drift: {drift_pct:+.2f}%)"
        print(f"  Position opened via Slack: {ticker} at {note}")
        client.chat_update(
            channel=channel, ts=ts,
            text=f"BUY {ticker} — Executed at {note}",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*BUY {ticker}* — Executed at {note}"}}],
        )

    @cfg.bolt_app.action("sell_exited")
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

    @cfg.bolt_app.action("sell_skipped")
    def handle_sell_skipped(ack, body, client):
        ack()
        data    = json.loads(body['actions'][0]['value'])
        channel = body['channel']['id']
        ts      = body['message']['ts']
        ticker  = data['ticker']
        position_id = data.get('position_id')
        pos = next((p for p in db.get_open_positions() if p['id'] == position_id), None)
        if pos:
            state = dict(pos.get('trail_state') or {})
            state.pop('exit_pending', None)
            db.update_position_trail_state(pos['id'], state)
        client.chat_update(
            channel=channel, ts=ts,
            text=f"SELL {ticker} — Skipped (position kept open)",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*SELL {ticker}* — Skipped (position kept open)"}}],
        )

    @cfg.bolt_app.action("trail_order_placed")
    def handle_trail_order_placed(ack, body, client):
        ack()
        data        = json.loads(body['actions'][0]['value'])
        channel     = body['channel']['id']
        ts          = body['message']['ts']
        position_id = data['position_id']
        ticker      = data['ticker']

        positions = {p['id']: p for p in db.get_open_positions()}
        pos = positions.get(position_id)
        if pos:
            state = dict(pos.get('trail_state') or {})
            state['order_placed'] = True
            db.update_position_trail_state(position_id, state)

        client.chat_update(
            channel=channel, ts=ts,
            text=f"{ticker} — trailing order placed",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"✅ *{ticker}* — trailing stop order placed"}}],
        )

    @cfg.bolt_app.view("exit_price_submit")
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

        db.close_position(position_id,
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

    @cfg.bolt_app.action("manual_open")
    def handle_manual_open(ack, body, client):
        """Correction path for a misclick (e.g. hit Skipped after a real fill) --
        opens a position directly from the reference report, price-entry modal
        doubling as the confirmation step."""
        ack()
        data   = json.loads(body['actions'][0]['value'])
        ticker = data['node']['ticker']
        current_price, _ = compute._current_price(ticker)
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

    @cfg.bolt_app.view("manual_open_price_submit")
    def handle_manual_open_price(ack, body, client):
        ack()
        data   = json.loads(body['view']['private_metadata'])
        node   = data['node']
        ticker = node['ticker']

        price  = float(body['view']['state']['values']['price_block']['price_input']['value'])
        shares = int(body['view']['state']['values']['shares_block']['shares_input']['value'])
        now    = datetime.now()

        db.open_position(node, price, now, price, now, shares=shares)

        note = f"${price:.4f}  {shares} shares"
        print(f"  Position manually opened via Slack: {ticker} at {note}")
        _post_message(f"MANUAL OPEN {ticker} — {note}", blocks=[{"type": "section", "text": {"type": "mrkdwn",
                      "text": f"*MANUAL OPEN {ticker}* — {note}"}}])

    @cfg.bolt_app.action("manual_close")
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

    @cfg.bolt_app.view("manual_close_price_submit")
    def handle_manual_close_price(ack, body, client):
        ack()
        data        = json.loads(body['view']['private_metadata'])
        position_id = data['position_id']
        ticker      = data['ticker']
        entry_price = data['entry_price']

        exit_price = float(body['view']['state']['values']['price_block']['price_input']['value'])
        actual_pnl = (exit_price - entry_price) / entry_price * 100
        now        = datetime.now()

        db.close_position(position_id,
                           exit_signal_price=exit_price, exit_price=exit_price,
                           exit_time=now, exit_reason='MANUAL')

        note = f"${exit_price:.4f}  (P&L: {actual_pnl:+.2f}%)"
        print(f"  Position manually closed via Slack: {ticker} at {note}")
        _post_message(f"MANUAL CLOSE {ticker} — {note}", blocks=[{"type": "section", "text": {"type": "mrkdwn",
                      "text": f"*MANUAL CLOSE {ticker}* — {note}"}}])

    @cfg.bolt_app.action("resend_ref_table")
    def handle_resend_ref_table(ack, body, client):
        """On-demand refresh -- posts a brand new reference report rather than
        editing the clicked one in place, so the old report (and its now-stale
        manual-open/close buttons) stays as a historical record."""
        ack()
        send_reference_report(db.get_watchlist())


# ---------------------------------------------------------------------------
# Buy / sell notifications
# ---------------------------------------------------------------------------

def notify_buy_signal(node, sig):
    ticker   = sig['ticker']
    price    = sig['current_price']
    z        = sig['z_score']
    bar_time = sig['last_bar']
    bar_str  = bar_time.strftime('%Y-%m-%d %H:%M')
    arm      = db._tp_or_arm_pct(node)
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
    if (node.get('account') or '').lower() != 'brokerage' and db.closed_today(ticker):
        print(f"  ⚠️🔁 SAME DAY BUY WARNING: {ticker} already sold today — cash may not be settled (T+1)")
    print(sep)

    channel, ts = _post_message(
        f"BUY SIGNAL — {ticker}  ${price:.4f}  z={z:.2f}  ({bar_str})",
        _build_buy_blocks(node, sig),
    )

    # Tracked regardless of INTERACTIVE -- a trailing-buy order is still pending
    # fill confirmation even in SIM_MODE or webhook-only (non-socket) runs, where
    # there's no button to click but the reminder loop should still nag.
    if db._is_trailing_buy(node):
        db.add_pending_buy(node, sig, channel, ts)

    if cfg.INTERACTIVE:
        chart = _chart_buy(node, sig)
        if chart:
            _upload_chart(chart, f"{ticker}_buy.png", f"BUY — {ticker}  z={z:.2f}")
        print("  Waiting for Slack response (Executed / Skipped).")
        return

    if db._is_trailing_buy(node):
        print("\nTrailing buy order placed at the broker? No position opens yet -- "
              "report the real fill separately once it happens. (y/n): ", end='', flush=True)
        try:
            resp = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            resp = ''
        if resp == 'y':
            db.mark_pending_buy_placed(ticker)
            print(f"  {ticker} order marked placed — no position yet, waiting for fill.")
            _post_message(f"{ticker} trailing buy order placed, waiting for fill.")
        else:
            db.clear_pending_buy(ticker)
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
            db.open_position(node, price, bar_time, exec_price, now)
            db.clear_pending_buy(ticker)
            note = f"Entered at ${exec_price:.4f}  (drift: {drift_pct:+.2f}%)"
            print(f"  Position opened. {note}")
            _post_message(f"{ticker} position opened: {note}")
        except ValueError:
            print("  Invalid price — position not opened.")
    else:
        db.clear_pending_buy(ticker)
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
    print(f"  Target: ${target_price:.4f}   Node: Arm={db._tp_or_arm_pct(pos)}%  SL={pos['stop_loss'] + 1}%  hold={pos['max_hold_hours']}h")
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
    db.update_position_trail_state(pos['id'], state)

    if cfg.INTERACTIVE:
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
            db.close_position(pos['id'], exit_signal_price=current_price, exit_price=exit_price,
                               exit_time=datetime.now(), exit_reason=reason)
            print(f"  Position closed. {note}")
            _post_message(f"{ticker} position closed: {note}")
        except ValueError:
            print("  Invalid price — position kept open.")
    else:
        state = dict(pos.get('trail_state') or {})
        state.pop('exit_pending', None)
        db.update_position_trail_state(pos['id'], state)
        print("  Skipped — position kept open.")


TRAIL_REMINDER_MINUTES = 15


def _trailing_order_blocks(pos, current_price, reminder_num=0):
    ticker    = pos['ticker']
    ep        = pos['entry_price']
    pct       = (current_price - ep) / ep * 100
    header    = f"⚠️ *{ticker}* — STILL PENDING (reminder #{reminder_num})" if reminder_num else f"🎯 *{ticker}* — TRAILING ACTIVATED — action needed"
    if reminder_num:
        text = (
            f"{header}\n"
            f"entry `${ep:.2f}`  |  current `${current_price:.2f}`  |  P&L `{pct:+.1f}%`\n"
            f"Trailing stop order not yet confirmed placed at the broker."
        )
    else:
        text = (
            f"{header}\n"
            f"entry `${ep:.2f}`  |  current `${current_price:.2f}`  |  P&L `{pct:+.1f}%`\n"
            f"Place the trailing stop order at the broker now."
        )
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if cfg.INTERACTIVE:
        value = json.dumps({"position_id": pos['id'], "ticker": ticker})
        blocks.append({
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Order Placed"},
                 "style": "primary", "action_id": "trail_order_placed", "value": value},
            ],
        })
    else:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": "No interactive buttons — confirm the trailing stop order is placed in the terminal running the daemon."}
        ]})
    return blocks


def _supersede_message(channel, ts, ticker):
    if not (cfg.SOCKET_MODE and channel and ts):
        return
    try:
        cfg.bolt_app.client.chat_update(
            channel=channel, ts=ts,
            text=f"{ticker} trailing order reminder — superseded",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"~_{ticker} trailing order reminder — superseded, see newer message below_~"}}],
        )
    except Exception as e:
        print(f"  [slack error] supersede failed: {e}")


def notify_trailing_activated(pos, current_price):
    ticker = pos['ticker']
    blocks = _trailing_order_blocks(pos, current_price, reminder_num=0)
    channel, ts = _post_message(
        f"{ticker} trailing stop activated — place order", blocks=blocks)
    state = dict(pos.get('trail_state') or {})
    state['reminder_channel'] = channel
    state['reminder_ts']      = ts
    state['reminder_count']   = 0
    state['last_reminder_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.update_position_trail_state(pos['id'], state)


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
        cp, _ = compute._current_price(pos['ticker'])
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
        db.update_position_trail_state(pos['id'], new_state)


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
    if cfg.INTERACTIVE:
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
    a stalled SELL confirmation is invisible until the user happens to remember."""
    now = datetime.now()
    for pos in open_positions:
        state = pos.get('trail_state') or {}
        exit_pending = state.get('exit_pending')
        if not exit_pending:
            continue
        last_at_str = exit_pending.get('last_reminder_at')
        if not last_at_str:
            continue
        last_at = datetime.strptime(last_at_str, '%Y-%m-%d %H:%M:%S')
        if (now - last_at).total_seconds() < EXIT_REMINDER_MINUTES * 60:
            continue
        _supersede_message(exit_pending.get('reminder_channel'), exit_pending.get('reminder_ts'), pos['ticker'])
        reminder_num = exit_pending.get('reminder_count', 0) + 1
        blocks = _exit_pending_blocks(pos, exit_pending, reminder_num)
        channel, ts = _post_message(
            f"{pos['ticker']} exit — still not confirmed (reminder #{reminder_num})", blocks=blocks)
        new_state = dict(state)
        new_exit_pending = dict(exit_pending)
        new_exit_pending['reminder_channel'] = channel
        new_exit_pending['reminder_ts']      = ts
        new_exit_pending['reminder_count']   = reminder_num
        new_exit_pending['last_reminder_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
        new_state['exit_pending'] = new_exit_pending
        db.update_position_trail_state(pos['id'], new_state)


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
    df_hourly, _ = compute._load_cache(pending['ticker'])
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
    ticker = pending['ticker']
    node = pending['node']
    placed = pending['order_placed']
    met, trigger = _trailing_buy_status(pending)

    if placed:
        header = f"⚠️ *{ticker}* — FILL NOT CONFIRMED (reminder #{reminder_num})"
        trigger_str = f"  |  bounce trigger `${trigger:.2f}`" if trigger is not None else ""
        text = (
            f"{header}\n"
            f"Trailing buy order placed at the broker but not yet confirmed filled{trigger_str}.\n"
            f"Confirm Filled with the real fill price, or Cancelled if the order didn't go through."
        )
    else:
        header = f"⚠️ *{ticker}* — ORDER NOT CONFIRMED PLACED (reminder #{reminder_num})"
        text = (
            f"{header}\n"
            f"BUY signal fired but no confirmation the trailing buy order was placed at the broker.\n"
            f"Confirm once it's resting at the broker, or Skip if you're not taking this trade."
        )
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    if cfg.INTERACTIVE:
        value = json.dumps({"node": node, "signal_price": pending['signal_price'],
                             "signal_time": pending['signal_time']})
        if placed:
            blocks.append({
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Filled"},
                     "style": "primary", "action_id": "trail_buy_filled", "value": value},
                    {"type": "button", "text": {"type": "plain_text", "text": "Cancelled"},
                     "action_id": "trail_buy_cancelled", "value": value},
                ],
            })
        else:
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
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": "No interactive buttons — confirm in the terminal running the daemon."}
        ]})
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
    for pending in db.get_pending_buys():
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
        db.update_pending_buy_reminder(pending['id'], channel, ts, reminder_num)


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

    if cfg.INTERACTIVE:
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
    with db._conn() as c:
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
    positions = {p['ticker']: p for p in db.get_open_positions()}
    pending_buys = {p['ticker']: p for p in db.get_pending_buys()}
    rows = []
    for node in [n for n in watchlist if n.get('mode') == 'live']:
        ticker = node['ticker']
        pos = positions.get(ticker)
        sig = compute.compute_buy_signal(node)
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
                'SL $': trigger * (1 - schwab_sl_pct / 100), 'Arm $': trigger * (1 + db._tp_or_arm_pct(node) / 100),
                'Overnight %': (now_price - sig['prev_close']) / sig['prev_close'] * 100,
                'Prev Close': sig['prev_close'], 'Data Date': sig['last_daily_bar'],
                '_node': node, '_pos': None, '_sig': sig,
            })
        else:
            df_hourly_p, _ = compute._load_cache(ticker)
            signal_time = datetime.fromisoformat(pos['signal_time'])
            hours_held = compute._bars_held(df_hourly_p, signal_time)
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
                trigger = pos['entry_price'] * (1 + db._tp_or_arm_pct(pos) / 100.0)
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
    if cfg.INTERACTIVE:
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
