"""Slack message posting and block (Block Kit) builders for buy/sell alerts and modals."""
import json
import sqlite3

import requests

import schwab_safety
import signals_config as cfg
import signals_db as db
from signals_helpers import (
    _add_trading_hours, _last_sale_recovery, buy_order_sizing, mode_tag,
    should_alert_live, stop_status,
)


def _post_message(text, blocks=None, thread_ts=None, reply_broadcast=False, node_id=None):
    """Returns (channel, ts) when posted via the Socket Mode client (None, None
    otherwise) so callers can track a message for later reminder/supersede.

    node_id: optional -- when given, resolves the real watch_list node and
    gates the actual Slack send on should_alert_live(node) (2026-08-13, the
    project's noise-reduction pass: canary/dry_run/soxl_ira's small live tier
    were flooding Slack with routine order-attempt messages the user
    explicitly doesn't want to see in real time). A suppressed message is
    still logged via log_slack_message below (mode 'suppressed') -- never
    silently lost, only its real-time visibility, same contract as
    has_capital_at_stake's other consumers. Omitting node_id (the default)
    always sends -- system-wide messages (EOD/coverage reports, generic
    errors) aren't ticker-scoped and were never in scope for this gate."""
    if node_id is not None:
        # Deliberately isolated in its own try/except, unlike the rest of this
        # function relying on each callee's own internal safety -- everything
        # this calls today (get_watch_list_node_by_id, should_alert_live) is
        # already defensively coded, but nothing about a noise-reduction
        # filter should ever be able to newly raise past this point and block
        # the actual Slack send (let alone anything upstream of it), the way
        # a real Slack outage/rejection already can't (both send paths below
        # have their own try/except). Fails toward sending on any surprise.
        try:
            node = db.get_watch_list_node_by_id(node_id)
            suppress = node is not None and not should_alert_live(node)
        except Exception as e:
            suppress = False
            print(f"  [alert gate error] {e} -- failing open, sending")
        if suppress:
            db.log_slack_message('suppressed', text, error=None, blocks=blocks)
            return None, None
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
    log_mode = 'sim' if cfg.SIM_MODE else ('live' if cfg.SOCKET_MODE else ('webhook' if cfg.SLACK_HOOK else 'console'))
    channel, ts, error = None, None, None
    if cfg.SOCKET_MODE:
        try:
            kwargs = {'thread_ts': thread_ts} if thread_ts else {}
            if thread_ts and reply_broadcast:
                kwargs['reply_broadcast'] = True
            resp = cfg.bolt_app.client.chat_postMessage(channel=cfg.SLACK_CHANNEL, text=text, blocks=blocks, **kwargs)
            channel, ts = resp['channel'], resp['ts']
        except Exception as e:
            error = str(e)
            print(f"  [slack error] {e}")
    elif cfg.SLACK_HOOK:
        payload = {'text': text}
        if blocks:
            payload['blocks'] = blocks
        if thread_ts:
            payload['thread_ts'] = thread_ts
            if reply_broadcast:
                payload['reply_broadcast'] = True
        try:
            r = requests.post(cfg.SLACK_HOOK, json=payload, timeout=5)
            if not r.ok:
                error = f"HTTP {r.status_code}"
                print(f"  [slack error] {error}")
        except Exception as e:
            error = str(e)
            print(f"  [slack error] {e}")
    # Logged after the attempt (not before) so a row reflects the real outcome
    # -- see log_slack_message's docstring for why this ordering matters.
    # blocks is logged as sent (post-SIM_MODE marker injection above), matching
    # `text`, which is likewise logged with its SIM prefix already applied.
    db.log_slack_message(log_mode, text, error=error, blocks=blocks)
    return channel, ts


_SLACK_MAX_BLOCKS = 50


def _post_chunked(text, fixed_blocks, units, max_blocks=_SLACK_MAX_BLOCKS):
    """Posts a message built from a growing, unbounded list of per-row block
    groups (`units`, each an atomic list of 1+ blocks that must stay together)
    without ever risking Slack's hard 50-block-per-message limit -- the watchlist
    has already broken a fixed per-row block-count budget twice (2026-07-22,
    2026-07-29) as it grew, so this chunks instead of re-shrinking again.
    `fixed_blocks` (header/controls) ride in the first chunk only. Overflow
    chunks post as thread replies to the first message, broadcast to the
    channel (`reply_broadcast`) so a reader still gets a mobile notification
    for the overflow content, not just the first chunk (2026-07-26, Opus
    review flagged silent-overflow as a mobile-readability regression).
    Returns (channel, ts) of the first message -- ts comes back None if ANY
    chunk (including the first) failed to confirm delivery, so a caller
    checking `channel and ts` can't mistake a partial post for a full one
    (2026-07-26, Opus review: a chunk-2+ failure was previously invisible to
    the morning_report_delivery coverage event)."""
    chunks, current = [], list(fixed_blocks)
    for unit in units:
        if current and len(current) + len(unit) > max_blocks:
            chunks.append(current)
            current = []
        current.extend(unit)
    if current or not chunks:
        chunks.append(current)

    channel, ts = _post_message(text, blocks=chunks[0])
    all_delivered = bool(channel and ts)
    for chunk in chunks[1:]:
        _, chunk_ts = _post_message(f"{text} (cont.)", blocks=chunk, thread_ts=ts, reply_broadcast=True)
        all_delivered = all_delivered and bool(chunk_ts)
    return channel, (ts if all_delivered else None)


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


def _build_buy_blocks(node, sig, auto_placed=False):
    ticker    = sig['ticker']
    price     = sig['current_price']
    z         = sig['z_score']
    bar_str   = sig['last_bar'].strftime('%Y-%m-%d %H:%M')

    hurst_str = f"{sig['hurst']:.3f}" if sig.get('hurst') is not None else "n/a"
    adf_str   = f"{sig['adf_p']:.3f}" if sig.get('adf_p')  is not None else "n/a"

    hold_deadline = _add_trading_hours(sig['last_bar'], node['max_hold_hours'])
    deadline_str  = hold_deadline.strftime('%a %b %d %H:%M')

    sizing = buy_order_sizing(node, sig)
    target_notional = sizing['target_notional']
    trailing_buy     = sizing['trailing_buy']
    trail_buy_pct    = sizing['trail_buy_pct']
    shares           = sizing['shares']
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
    # 🧪CANARY tag (2026-08-09): mirrors the Reference Report's existing marker
    # (signals_notify.py's build_reference_table) -- the real per-signal BUY
    # alert (this function) never had it, found 2026-07-24. Currently
    # unreachable in practice (canary nodes are dry_run/no real capital, so
    # should_alert_live/has_capital_at_stake suppresses this Slack post
    # entirely as of the 2026-08-08 capital-at-stake redesign) but cheap to
    # close now rather than leave a landmine for whenever that gating logic
    # changes -- mode_tag() itself only ever returns LIVE/DRY-RUN/UNKNOWN,
    # deliberately not overloaded here since it's shared with other alerts.
    # Substring, not exact-equality (paired review finding): canary-family
    # variant nodes (e.g. 'v5-canary-drought', 'v5-canary-drought-addon')
    # exist alongside the original bare 'canary' version and are otherwise
    # missed -- verified no non-canary version string contains "canary".
    canary_tag = ' 🧪CANARY' if 'canary' in (node.get('version') or '') else ''
    acct_tag = f"`{account} · {mode_tag(account, node)}{canary_tag}`"
    if trailing_buy:
        auto_str = "  🤖 *auto-placed at broker*" if auto_placed else ""
        entry_line = f"🟢 *{ticker}* — BUY — Trailing Buy {trail_buy_pct:.0f}% — trigger `${price:.2f}` — `{shares} shares` (~${target_notional/1000:.0f}k) — {acct_tag}{max_notional_str}{auto_str}"
    else:
        entry_line = f"🟢 *{ticker}* — BUY — Market — `${price:.2f}` — `{shares} shares` (~${target_notional/1000:.0f}k) — {acct_tag}{max_notional_str}"

    warning_line = ""
    _limits = schwab_safety.ACCOUNTS.get(node.get('account'))
    if db.closed_today(ticker):
        if _limits and _limits.cash_settlement_type == 'cash':
            warning_line = (
                f"\n⚠️🔁 *SAME DAY BUY WARNING:* {ticker} already sold today in a "
                f"{node.get('account', 'non-brokerage')} account — cash may not be settled (T+1). Confirm funds are available before entering."
            )
        elif not _limits:
            # See notify_buy_signal's matching fix -- an unrecognized account
            # must warn too, not silently assume it's safe (round 6).
            warning_line = (
                f"\n⚠️🔁 *SAME DAY BUY WARNING:* {ticker} already sold today, account "
                f"'{node.get('account')}' not recognized — cash-settlement status unknown, confirm before entering."
            )

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"{entry_line}\n🔴 *{ticker}* — SELL ALL — Stop Loss — `${schwab_sl_price:.2f}` (-{schwab_sl_pct}% from trigger){warning_line}"}},
    ]

    if cfg.INTERACTIVE:
        value = json.dumps({
            "type":         "buy",
            "node":         {k: node.get(k) for k in ('id', 'ticker', 'strategy', 'version', 'window',
                                                        'take_profit', 'stop_loss', 'max_hold_hours', 'label',
                                                        'trail_sell_pct', 'fixed_sl', 'trail_buy_pct', 'arm_sell_pct',
                                                        'starting_notional', 'account', 'state')},
            "signal_price": price,
            "signal_time":  sig['last_bar'].strftime('%Y-%m-%d %H:%M:%S'),
            "lower_band":   sig['lower_band'],
            "z_score":      z,
        })
        if trailing_buy and auto_placed:
            # Order is already resting at the broker (schwab_client placed it) --
            # skip straight to the Filled/Missed/Cancelled set, mirroring what
            # handle_trail_buy_order_placed transitions to for the manual path.
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
        elif trailing_buy:
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


def _build_sell_blocks(pos, reason, current_price, target_price, resting_confirmed=False):
    """resting_confirmed: True only when the caller has actually checked the
    real broker order status and confirmed it's still resting (not just that
    an order_id happens to be non-None -- a REJECTED/CANCELED order looks the
    same as a resting one by id alone; see signals_notify._exit_order_resting,
    the only real caller of this with resting_confirmed=True). False/default
    covers both "no automated order" and "unconfirmed" -- fails toward the
    cautious manual-action text in both cases (Opus review, 2026-08-01)."""
    ticker  = pos['ticker']
    ep      = pos['entry_price']
    pct     = (current_price - ep) / ep * 100
    account = pos.get('account') or 'unmapped'
    _node   = db.get_watch_list_node_by_id(pos.get('wl_id'))

    if reason == 'TP':
        emoji   = "🟢"
        label   = "TAKE PROFIT"
        if resting_confirmed:
            action = f"🤖 Automated exit order resting @ `${current_price:.2f}` — should fill shortly, no action needed unless this persists."
        else:
            action = f"Cancel Stop Loss order — Sell All (Market) @ `${current_price:.2f}`"
    elif reason == 'SL':
        emoji   = "🔴"
        label   = "STOP LOSS HIT"
        status, bsp = stop_status(pos)
        if status == 'known':
            action = f"Broker stop-loss on file @ `${bsp:.2f}` — should auto-fill there, no action needed. Confirm once you see the fill in your account."
        elif status == 'automation-pending':
            action = f"⚠️ Should have auto-filled @ `${target_price:.2f}` but no broker stop is on file — check account, this may be a placement failure."
        elif status == 'dry-run':
            action = f"Expected SL trigger ≈ `${target_price:.2f}` (dry-run account — no real stop is ever placed)."
        else:
            action = f"Expected SL trigger ≈ `${target_price:.2f}` — no automated stop tracked for this position, verify/stage it yourself at the broker."
    elif reason == 'TRAIL':
        emoji   = "🟢"
        label   = "TRAILING STOP"
        if resting_confirmed:
            # The stop-loss is already gone by this point -- arming replaces it
            # with a resting trailing-sell order (_attempt_automated_sell), and
            # this alert only fires when that order hasn't confirmed a fill
            # within the short poll window, which is the NORMAL case for a
            # trailing-stop (can take far longer than the poll to trigger),
            # not evidence anything is wrong (found 2026-07-28, GDXU). No
            # manual action exists here -- check_own_sell_fills keeps polling
            # the same order_id and auto-closes on fill.
            action = f"🤖 Trailing-sell order resting @ `${target_price:.2f}` — should fill shortly, no action needed unless this persists."
        else:
            action = f"Cancel Stop Loss order — Sell All (Market), trailing stop triggered @ `${target_price:.2f}`"
    else:  # TIME
        emoji   = "🔶"
        label   = "TIME EXIT"
        if resting_confirmed:
            # TIME exits route through the same _attempt_automated_exit_sell
            # market-sell path as TP -- the old unconditional "Change Stop
            # Loss -> Market Close" text told the user to modify an order
            # that was already replaced by a real market sell (Opus review,
            # 2026-08-01: this reason was originally left on the pre-fix
            # text, contradicting its own now-reason-agnostic 15-min
            # reminder).
            action = f"🤖 Automated exit order resting @ `${current_price:.2f}` — should fill shortly, no action needed unless this persists."
        else:
            action = f"Change Stop Loss → Market Close order (exit by EOD)"

    # See _build_buy_blocks' matching canary_tag comment -- same gap, same fix.
    canary_tag = ' 🧪CANARY' if 'canary' in ((_node or {}).get('version') or '') else ''
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": (
                f"{emoji} *{ticker}* ({account} · {mode_tag(account, _node)}{canary_tag}) — {label}\n"
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
