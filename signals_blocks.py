"""Slack message posting and block (Block Kit) builders for buy/sell alerts and modals."""
import json
import sqlite3

import requests

import signals_config as cfg
import signals_db as db
from signals_helpers import _add_trading_hours, _last_sale_recovery


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

    target_notional = _last_sale_recovery(ticker, node.get('starting_notional'))
    trailing_buy = db._is_trailing_buy(node)
    if trailing_buy:
        # Conservative worst-case sizing: a real trailing-buy order fills once price
        # bounces trail_buy_pct% off a running low that can fall further before that,
        # so the fill price is unbounded relative to the signal-time price. Sizing off
        # the worst case (no further drop, fill right at the bounce trigger) guarantees
        # the order never costs more than target_notional.
        trail_buy_pct = node.get('trail_buy_pct') or 0.0
        shares = int(target_notional // (price * (1 + trail_buy_pct / 100)))
    else:
        shares = int(target_notional // price)
    schwab_sl_pct   = node['stop_loss']
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
    if trailing_buy:
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

    if cfg.INTERACTIVE:
        value = json.dumps({
            "type":         "buy",
            "node":         {k: node.get(k) for k in ('ticker', 'strategy', 'version', 'window',
                                                        'take_profit', 'stop_loss', 'max_hold_hours', 'label',
                                                        'trail_sell_pct', 'fixed_sl', 'trail_buy_pct', 'arm_sell_pct',
                                                        'starting_notional')},
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
        bsp = pos.get('broker_stop_price')
        if bsp:
            action = f"Broker stop-loss on file @ `${bsp:.2f}` — should auto-fill there, no action needed. Confirm once you see the fill in your account."
        else:
            action = f"Check account — Stop Loss order should have auto-filled @ `${target_price:.2f}`"
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
