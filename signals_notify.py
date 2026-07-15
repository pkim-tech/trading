"""
Slack-facing core: notify_* functions, reminder loops, and the
reference-table/morning-report builder.

Chart generation lives in signals_charts.py, Slack block/message builders
in signals_blocks.py, Bolt interactive handlers in signals_handlers.py.
"""
import json
from datetime import datetime

import signals_config as cfg
import signals_db as db
import signals_compute as compute
from signals_charts import _chart_buy, _chart_sell, _upload_chart
from signals_blocks import _post_message, _build_buy_blocks, _build_sell_blocks
from signals_helpers import (
    _proximity_emoji, _existing_position_note, _last_sale_recovery, _phase_emoji,
)


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
    sl       = node['stop_loss']
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
            opened     = db.open_position(node, price, bar_time, exec_price, now)
            db.clear_pending_buy(ticker)
            if not opened:
                print(f"  [warn] {ticker} already has an open position — ignored duplicate")
                _post_message(f"{ticker} — ALREADY OPEN, this fill was ignored. {_existing_position_note(ticker)}")
            else:
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
    schwab_sl_pct   = node['stop_loss']
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
    print(f"  Target: ${target_price:.4f}   Node: Arm={db._tp_or_arm_pct(pos)}%  SL={pos['stop_loss']}%  hold={pos['max_hold_hours']}h")
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
    bsp = pos.get('broker_stop_price')

    if reason == 'SL' and bsp:
        status_line = (
            f"Protected by broker stop-loss on file @ `${bsp:.2f}` — should auto-fill there without "
            f"action from you. Confirm here once you see the fill in your account."
        )
    else:
        status_line = (
            f"Position may still be open and unmanaged at the broker. Confirm Exited with the real fill "
            f"price, or Skip if it turns out the exit condition no longer applies."
        )
    text = (
        f"⚠️ *{ticker}* — EXIT NOT CONFIRMED (reminder #{reminder_num})\n"
        f"{reason_labels[reason]}  |  entry `${ep:.2f}`  |  signal `${current_price:.2f}`  |  P&L `{pct:+.1f}%`\n"
        f"{status_line}"
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
            f"Confirm Filled with the real fill price, Missed It if the bounce already passed before the "
            f"order was live, or Cancelled if the order didn't go through."
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
                    {"type": "button", "text": {"type": "plain_text", "text": "Missed It"},
                     "action_id": "trail_buy_missed", "value": value},
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
        pct_str = lambda v: f"{v:g}%" if v is not None else '?'
        trig_label = row.get('Trigger Label', 'trig')
        pos = row.get('_pos')
        shares_str = f" x `{pos['shares']:g}`" if pos and pos.get('shares') is not None else ''
        entry_str = f"  `${pos['entry_price']:.2f}`{shares_str}" if pos else ''
        armed = bool((pos or {}).get('trail_state', {}).get('trailing'))
        if armed:
            arm_ts_line = ''
        else:
            arm, ts = row.get('Arm%'), row.get('TrailSell%')
            arm_ts_line = f"\narm `{pct_str(arm)}`  ts `{pct_str(ts)}`"
        text = (
            f"{phase_str}*{ticker}* `{version}` — {row['Hold']}{account_str}{entry_str}\n"
            f"now `${now:.2f}` {pnl:+.1f}%  {trig_label} `${trigger:.2f}` ({proximity:+.1f}%)\n"
            f"→ _{row['Next Action']}_{sl_str}{arm_ts_line}"
        )
    else:
        overnight = row.get('Overnight %')
        tb, arm, ts = row.get('TrailBuy%'), row.get('Arm%'), row.get('TrailSell%')
        pct_str = lambda v: f"{v:g}%" if v is not None else '?'
        last_sale = row.get('Last Sale $')
        last_sale_str = f"  next buy ~`${last_sale/1000:.0f}k`" if last_sale is not None else ''
        z_trig = row.get('Z Trigger')
        z_trig_str = f"z1 `{z_trig:g}`  " if z_trig is not None else ''
        trig_label = row.get('Trigger Label', 'trig')
        text = (
            f"{phase_str}*{ticker}* `{version}`{account_str}{last_sale_str}\n"
            f"now `${now:.2f}` ({overnight:+.1f}% O/N)  z `{row['Z']:+.2f}`  {trig_label} `${trigger:.2f}` ({proximity:+.1f}%)\n"
            f"→ _{row['Next Action']}_\n"
            f"{z_trig_str}tb `{pct_str(tb)}`  arm `{pct_str(arm)}`  ts `{pct_str(ts)}`"
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
        schwab_sl_pct = node['stop_loss']

        if pos is None:
            pending = pending_buys.get(ticker)
            trail_buy_pct = node.get('trail_buy_pct')
            if pending is not None:
                # z already crossed, trailing-buy order active -- the bounce-above-
                # running-low trigger is the number that actually matters now, not
                # the (already-cleared, often much farther away) initial z trigger.
                _, tb_trigger = _trailing_buy_status(pending)
                trigger = tb_trigger if tb_trigger is not None else sig['lower_band']
                next_action = 'Waiting Trail-Buy Bounce'
                trigger_label = 'tb-bounce'
            else:
                trigger = sig['lower_band']
                next_action = 'Waiting Buy Trigger'
                trigger_label = 'z-cross'
            rows.append({
                'Ticker': ticker, 'Hold': '',
                'Next Action': next_action, 'Trigger Label': trigger_label,
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
            bsp = pos.get('broker_stop_price')
            # Broker only allows one resting sell-all order per position -- once the
            # trailing-sell order is actually placed (order_placed=True), it replaces
            # the catastrophic stop, so the entry-based SL price is no longer live.
            if trail_state.get('order_placed'):
                sl_price = None
            elif bsp:
                sl_price = bsp
            else:
                sl_price = pos['entry_price'] * (1 - pos['stop_loss'] / 100)

            if trail_state.get('trailing'):
                peak = trail_state.get('peak', pos['entry_price'])
                trail_pct = (trail_sell_pct or 3.0) / 100.0
                trigger = peak * (1 - trail_pct)
                if trail_state.get('order_placed'):
                    next_action = f"Waiting Sell {trail_sell_pct:g}% Fill" if trail_sell_pct else 'Waiting Sell Fill'
                else:
                    next_action = f"Pending Sell {trail_sell_pct:g}%" if trail_sell_pct else 'Pending Sell'
                proximity = (now_price - trigger) / trigger * 100
                trigger_label = 'trail-sell'
            else:
                # Two triggers are simultaneously live here: SL protects right now,
                # Arm is the next threshold that swaps SL for the trailing sell.
                trigger = pos['entry_price'] * (1 + db._tp_or_arm_pct(pos) / 100.0)
                next_action = f"Arm {arm_pct:g}%" if arm_pct else 'Arm'
                proximity = (trigger - now_price) / trigger * 100
                trigger_label = 'arm'

            rows.append({
                'Ticker': ticker, 'Hold': hold, 'Next Action': next_action, 'Trigger Label': trigger_label,
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
