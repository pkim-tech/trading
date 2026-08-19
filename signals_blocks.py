"""Slack message posting and block (Block Kit) builders for buy/sell alerts and modals."""
import json
import sqlite3

import requests

import schwab_safety
import signals_config as cfg
import signals_db as db
from signals_helpers import (
    _add_trading_hours, _last_sale_recovery, automation_blockers_other_than_node,
    buy_order_sizing, effectively_dry_run, mode_tag, should_alert_live, stop_status,
)


def _post_message(text, blocks=None, thread_ts=None, reply_broadcast=False, node_id=None, incident=False,
                   pos=None, node=None, real_order=False):
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
    errors) aren't ticker-scoped and were never in scope for this gate.

    incident: when True, uses the more permissive effectively_dry_run(account,
    node) check instead of should_alert_live/has_capital_at_stake -- i.e. "did
    a real order get placed at all," not "is this >= the capital-at-stake
    threshold" (cfg.CAPITAL_AT_STAKE_THRESHOLD, $5,000 default -- note
    CLAUDE.md still says $10k, a stale doc claim, backlogged separately).
    Added 2026-08-17 after should_alert_live's stricter gate got
    applied to per-position error/incident alerts (UNPROTECTED, reconciliation
    mismatches, placement failures) and would have suppressed RETL's own real
    (if small, ~$400 soxl_ira) UNPROTECTED alert -- the exact incident that
    motivated adding the gate to these call sites in the first place. User's
    explicit call: soxl_ira's small live nodes place real (if tiny) orders and
    their errors are useful early-warning signal ("free dry runs for prod"),
    distinct from a canary/dry_run node's purely synthetic activity, which
    should stay suppressed. Routine alerts (BUY SIGNAL, reminders) are
    unaffected -- they keep the original should_alert_live/$10k gate.

    pos: the real open_positions row, for the position-keyed incident alerts
    (UNPROTECTED, reconciliation mismatch, stale-price exit suppression,
    replace-target mismatch, SL placement failure). When given alongside
    incident=True it REPLACES the effectively_dry_run(node) lookup entirely,
    gating instead on the position's own is_dry_run_sim flag -- the ground
    truth recorded AT ENTRY TIME about whether this position was genuinely
    opened for real (2026-08-17, closing the latent gap that motivated the
    incident flag's own backlog entry). effectively_dry_run re-derives from
    the node's CURRENT state/account, so a node demoted to paper/dry_run --
    or an account deliberately stopped -- while a real (is_dry_run_sim=0)
    position and real resting broker orders remain would have gone silent on
    exactly that position's UNPROTECTED/reconciliation alerts, precisely when
    a human has just intervened and most needs visibility. A position that
    was real when opened does not retroactively become synthetic because
    someone changed config afterwards. This also removes the node-lookup's
    staleness on `account` (the gate consulted the node's current `account`
    column rather than the account pinned on the position at order time) --
    the account is no longer consulted at all on this path.

    node: the caller's OWN in-hand node dict, used instead of re-resolving
    node_id fresh from the DB. For an incident alert with no position yet
    there is still usually a pinned anchor -- check_market_buy_rejected reads
    `pb['node']`, the frozen pending_buys.node_json snapshot, and gates its
    own "is this a real broker order" decision on it (_effectively_dry_run at
    the top of its loop), so re-resolving the LIVE node here made the loop
    decide "real order, worth polling" from pinned truth while this gate
    decided "synthetic, suppress" from current truth. That function polls a
    resting order across poll cycles and days, which is exactly the window a
    demotion lands in; its partial-fill branch ("tracking PRESERVED... a
    protective stop may be needed") means real shares sitting at the broker
    with no stop and no local position row, and would have gone silent
    (2026-08-17, both reviewers + a live-DB check finding real pending_buys
    rows carrying pinned state/account). Placement-attempt sites
    (_attempt_automated_buy, _attempt_automated_market_buy,
    _sync_confirm_and_protect) pass their own in-hand node for the same
    reason, though they have no real staleness window -- they alert
    milliseconds after their own attempt, against the very node that drove
    it. Same principle as `pos` above, just anchored to the pinned ORDER
    rather than the pinned POSITION.

    real_order: the caller has already PROVEN, from evidence stronger than
    any node snapshot, that a real broker order exists for this alert -- it
    holds a real broker order_id it is actively querying. `node` alone is not
    sufficient for that claim: it pins the node's `state`, but
    effectively_dry_run ALSO reads the account's CURRENT
    schwab_safety.ACCOUNTS[...].trading_enabled, which nothing freezes into
    node_json. So "the account was deliberately stopped while a real
    market-buy order still rests at the broker" would still have gone dark on
    a `node=`-only fix -- the account half of the same staleness bug (paired
    review rebuttal round, 2026-08-17). check_market_buy_rejected is the one
    caller: it refuses dry-run rows (_effectively_dry_run on the pinned
    snapshot) and requires a real order_id before it ever reaches its alerts,
    so realness is already established there and no re-derivation can improve
    on it. Never set this from a node/account lookup -- only from real
    evidence of a real order.

    Precedence when more than one is given: real_order (strongest -- a real
    broker order is proven to exist) > pos (a real fill happened) > node (a
    pinned/in-hand snapshot) > a fresh node_id lookup (weakest -- current
    config, correct only when genuinely nothing is pinned yet).

    NOTE: `node` also feeds the NON-incident path (should_alert_live ->
    has_capital_at_stake -> _last_sale_recovery), where a frozen node_json
    could carry a stale starting_notional. Latent only -- no caller passes
    `node=` with incident=False today."""
    if node_id is not None or pos is not None or node is not None:
        # Deliberately isolated in its own try/except, unlike the rest of this
        # function relying on each callee's own internal safety -- everything
        # this calls today (get_watch_list_node_by_id, should_alert_live,
        # effectively_dry_run) is already defensively coded, but nothing about
        # a noise-reduction filter should ever be able to newly raise past this
        # point and block the actual Slack send (let alone anything upstream of
        # it), the way a real Slack outage/rejection already can't (both send
        # paths below have their own try/except). Fails toward sending on any
        # surprise.
        try:
            if incident and real_order:
                # Proven-real broker order -- nothing to re-derive. See the
                # real_order param note above for why `node` alone can't
                # carry this (it pins state, not the account's live
                # trading_enabled flag).
                suppress = False
            elif incident and pos is not None:
                # Truthiness, not `== 1` -- open_positions.is_dry_run_sim is
                # INTEGER NOT NULL DEFAULT 0, but a hand-built/legacy dict
                # missing the key must fail toward SENDING, matching this
                # block's fail-open contract above.
                # `origin` is a pure API guard: nothing writes origin='paper'
                # into open_positions today (the column is DEFAULT 'live' and
                # no INSERT sets it), and all 16 call sites use paper=False
                # lookups, so no current caller can reach it. Kept because
                # build_reference_table merges both books into one wl_id-keyed
                # dict and destroys the table-of-origin signal -- if an
                # incident alert is ever fed from a merged/report-side dict,
                # is_dry_run_sim alone reads a paper row as real.
                suppress = bool(pos.get('is_dry_run_sim')) or pos.get('origin') == 'paper'
            else:
                # A caller-supplied node wins over a fresh lookup -- see the
                # `node` param note above. Only fall back to the DB when the
                # caller genuinely has nothing pinned.
                _node = node if node is not None else (
                    db.get_watch_list_node_by_id(node_id) if node_id is not None else None)
                if _node is None:
                    suppress = False
                elif incident:
                    suppress = effectively_dry_run(_node.get('account'), _node)
                else:
                    suppress = not should_alert_live(_node)
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
            # Shares db._PENDING_BUY_NODE_KEYS as the single field list (2026-08-19,
            # closing the recurring-duplication bug -- both lists independently went
            # stale missing starting_notional_override, see that constant's docstring).
            # Paired-review correction: this is a preventive/regression-guard fix, not
            # closing a currently-live gap -- handle_entry_price/handle_trail_buy_fill_
            # price re-fetch a fresh pending_buys row (db.open_position_from_pending)
            # and read drought overrides off ITS node_json snapshot, not this button
            # payload, so the 3 drought_*_override fields this now carries (vs. the
            # old hand-typed tuple) are currently unread on this path -- carried for
            # lockstep-by-construction symmetry with the DB-side snapshot, in case a
            # future handler path ever does read off the button's own node dict.
            "node":         {k: node.get(k) for k in db._PENDING_BUY_NODE_KEYS},
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


def _trailing_order_blocks(pos, current_price, reminder_num=0):
    ticker    = pos['ticker']
    account   = pos.get('account') or 'unmapped'
    _node     = db.get_watch_list_node_by_id(pos.get('wl_id'))
    ep        = pos['entry_price']
    pct       = (current_price - ep) / ep * 100
    shares    = pos.get('shares')
    trail_pct = pos.get('trail_sell_pct')
    order_desc = (
        f"SELL {shares:g} @ {trail_pct:g}% trail" if (shares and trail_pct)
        else "SELL (shares/trail% unavailable — check the node config)"
    )
    # Mandatory for the automated path (_attempt_automated_sell uses an
    # atomic replace specifically so the old SL is never left resting
    # alongside a new trailing-sell -- both live simultaneously for the same
    # shares is an oversell/rejected-order risk, per that function's own
    # docstring). The manual alert never said this at all -- found via
    # arming-logic walkthrough, 2026-07-31: a user following it literally
    # ends up in exactly the state the automated path goes out of its way to
    # prevent.
    cancel_note = (
        f" Cancel the existing stop-loss order ({pos['sl_order_id']}) first."
        if pos.get('sl_order_id') else ""
    )
    header    = f"⚠️ *{ticker}* ({account} · {mode_tag(account, _node)}) — STILL PENDING (reminder #{reminder_num})" if reminder_num else f"🎯 *{ticker}* ({account} · {mode_tag(account, _node)}) — TRAILING ACTIVATED — action needed"
    if reminder_num:
        text = (
            f"{header}\n"
            f"{order_desc}  |  entry `${ep:.2f}`  |  current `${current_price:.2f}`  |  P&L `{pct:+.1f}%`\n"
            f"Trailing stop order not yet confirmed placed at the broker.{cancel_note}"
        )
    else:
        text = (
            f"{header}\n"
            f"{order_desc}  |  entry `${ep:.2f}`  |  current `${current_price:.2f}`  |  P&L `{pct:+.1f}%`\n"
            f"Place the trailing stop order at the broker now.{cancel_note}"
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


def _confirm_dialog(title, text, confirm_label, deny_label="Cancel", style=None):
    """Block Kit `confirm` object -- the native "are you sure?" dialog Slack
    renders before dispatching a button's action. First use of this in the
    codebase (2026-08-14): every safety-critical control here (global kill
    switch, per-node automation stop/start) is a one-tap, no-undo action, and
    the reference report is read on mobile where a mis-tap is easy.

    Slack's schema constraints, enforced by construction rather than trusted:
    title/confirm/deny MUST be plain_text (mrkdwn is rejected outright), text
    may be mrkdwn; title is capped at 100 chars and text at 300, so both are
    truncated here instead of letting Slack reject the whole message (the same
    failure mode as the 2026-07-22 invalid_blocks incident -- one bad field
    kills the entire report, not just the row).

    Truncation is marked with an ellipsis rather than silently clipping: these
    dialogs carry the warning text a user is being asked to act on, and a
    clause disappearing off the end with no visible sign is worse than an
    obviously-cut sentence. Callers should still stay under the caps."""
    def _clip(s, limit):
        return s if len(s) <= limit else s[:limit - 1] + "…"

    obj = {
        "title":   {"type": "plain_text", "text": _clip(title, 100)},
        "text":    {"type": "mrkdwn",     "text": _clip(text, 300)},
        "confirm": {"type": "plain_text", "text": _clip(confirm_label, 30)},
        "deny":    {"type": "plain_text", "text": _clip(deny_label, 30)},
    }
    if style:
        obj["style"] = style
    return obj


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
        # No real broker fill/order exists behind this row -- must never read as
        # an actionable real position (same reasoning as the 🧪CANARY tag below,
        # Opus review 2026-07-26 flagged this row was otherwise indistinguishable
        # from a genuine held position).
        sim_tag = ' 🧪DRY-RUN-SIM' if pos and pos.get('is_dry_run_sim') else ''
        # Bug #54 (found live: AGQ). This branch tagged is_dry_run_sim but had
        # no way at all to tell a PAPER position from a real one -- build_
        # reference_table merges paper_positions and open_positions into one
        # wl_id-keyed dict, which destroys the only signal of which table the
        # row came from, so a simulated position rendered byte-identically to
        # a real held one: same entry price, same share count, same actionable
        # framing, no marker anywhere. Now reads the origin column stamped on
        # the row itself (2026-08-15), the single source of truth both this and
        # build_reference_table share, rather than each re-deriving it.
        paper_tag = ' 📄PAPER' if pos and pos.get('origin') == 'paper' else ''
        text = (
            f"{phase_str}*{ticker}* `{version}`{sim_tag}{paper_tag} — {row['Hold']}{account_str}{entry_str}\n"
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
        # Not-live rows are visible in the report (2026-07-22 fix) but must
        # never read as an actionable live trigger -- research is the normal
        # state right now (whole watchlist), canary is a synthetic test node
        # not meant to be traded at all (see the "Manually Open" suppression
        # below, automation_principles.md #0/#7).
        # Substring, not exact-equality (2026-08-09 paired review, same gap
        # found in the new BUY/SELL alert tags) -- misses canary-family
        # variants like 'v5-canary-drought-addon' otherwise.
        # Named to avoid shadowing the module-level mode_tag() function (found
        # by session-wrap review 2026-08-16 -- this function was relocated
        # into signals_blocks.py, which has several siblings that call
        # mode_tag() freely; the shadow was pre-existing but is now one edit
        # away from an UnboundLocalError on this real-money-adjacent path).
        version_mode_tag = ' 🧪CANARY' if 'canary' in ((row.get('_node') or {}).get('version') or '') \
            else (' (research)' if row.get('State') == 'paper' else '')
        text = (
            f"{phase_str}*{ticker}* `{version}`{version_mode_tag}{account_str}{last_sale_str}\n"
            f"now `${now:.2f}` ({overnight:+.1f}% O/N)  z `{row['Z']:+.2f}`  {trig_label} `${trigger:.2f}` ({proximity:+.1f}%)\n"
            f"→ _{row['Next Action']}_\n"
            f"{z_trig_str}tb `{pct_str(tb)}`  arm `{pct_str(arm)}`  ts `{pct_str(ts)}`"
        )
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    if cfg.INTERACTIVE:
        # 2026-07-22: collapsed from up to 3 separate `actions` blocks per row
        # into 1 (Slack allows up to 5 elements per actions block, we use at
        # most 3) -- with the mode filter fix above making every watchlist row
        # render instead of none, 16 rows x up to 4 blocks each blew past
        # Slack's hard 50-block-per-message limit and the report failed
        # outright (invalid_blocks). This cuts the per-row block count enough
        # to fit the full watchlist in one message again.
        elements = []
        node = row.get('_node')

        # 2026-08-14: replaced the per-row "Manually Open" button with a
        # node-scoped automation Stop/Start (see below) -- opening a new
        # position from a phone is a rare correction path, whereas halting a
        # node is the action actually wanted under time pressure. The
        # manual_open HANDLER stays registered (signals_handlers.py) for old
        # reports in Slack scrollback.
        #
        # "Manually Close" is NOT replaced -- kept for a genuinely-held real
        # position, restored here after a merge (2026-08-15) briefly dropped
        # it along with Manually Open. It serves a different purpose than
        # Stop: Stop only pauses future automated action, it does not touch
        # an order already resting or close an open position now, so the
        # correction path a misclick (or a real need to close by hand) needs
        # still has to exist independently. Origin check (2026-08-15, bugs
        # #54/#63-64): a paper row must never render this button -- its
        # position_id is a paper_positions.id, but the manual_close handler
        # resolves against open_positions, two INDEPENDENT id sequences, so
        # the id would either miss entirely or match and close an unrelated
        # REAL position. is_dry_run_sim is suppressed for the same underlying
        # reason: no real broker fill exists behind that row either.
        if row['Held']:
            pos = row.get('_pos')
            if pos and not pos.get('is_dry_run_sim') and pos.get('origin') != 'paper':
                value = json.dumps({"position_id": pos['id'], "ticker": ticker, "entry_price": pos['entry_price']})
                elements.append({"type": "button", "text": {"type": "plain_text", "text": f"Manually Close {ticker}"},
                                  "action_id": "manual_close", "value": value})
        #
        # Only state=='live' nodes get this button. An earlier draft offered it
        # on every row with a node id, reasoning that pausing risks no capital
        # -- true, but it missed that the control does NOTHING for a paper or
        # canary row: paper_trading.py runs its own simulation loop, and
        # _ticker_block is ALSO rendered by _send_window_alert against a
        # completely unfiltered watchlist (send_reference_report's
        # has_capital_at_stake filter does not apply there), so a research row
        # really would have shown a "🛑 Stop" that posted "automation STOPPED"
        # while the sim kept opening and closing positions. paper_trading now
        # honors the node flag on the entry side as well (see
        # paper_trading.start_paper_buy), but a control whose effect is
        # invisible in this report still doesn't belong on these rows.
        wl_id = node.get('id') if node else None
        if wl_id is not None and (node.get('state') == 'live'):
            # Real current state across ALL THREE gates, not just this node's
            # own flag. schwab_safety.check_order blocks on kill_switch_engaged()
            # OR node_automation_enabled() OR ticker_automation_enabled(), so
            # reading only the node flag would render "🛑 Stop" for a node the
            # kill switch has already halted -- the exact "reads as if it's
            # running when it isn't" failure this toggle exists to avoid.
            node_paused = not schwab_safety.node_automation_enabled(wl_id)
            other_blockers = automation_blockers_other_than_node(ticker, node.get('account'))
            blocked_note = f" — note: still blocked by {', '.join(other_blockers)}" if other_blockers else ""
            auto_value = json.dumps({"ticker": ticker, "wl_id": wl_id})
            if node_paused:
                elements.append({
                    "type": "button", "style": "primary",
                    "text": {"type": "plain_text", "text": f"▶️ Start {ticker}"},
                    "action_id": "start_node_automation", "value": auto_value,
                    "confirm": _confirm_dialog(
                        f"Start {ticker}?",
                        f"Resumes automation for *{ticker}* (node {wl_id}). It will place "
                        f"real orders again on its next signal{blocked_note}.",
                        "Start it"),
                })
            else:
                # Label must not claim the node is running when another layer
                # has already halted it.
                suffix = f" (already halted: {other_blockers[0]})" if other_blockers else ""
                elements.append({
                    "type": "button", "style": "danger",
                    "text": {"type": "plain_text", "text": f"🛑 Stop {ticker}{suffix}"},
                    "action_id": "stop_node_automation", "value": auto_value,
                    # Kept comfortably under _confirm_dialog's 300-char cap so
                    # the SELL warning can't be the part that gets truncated
                    # away (it is the whole point of this dialog).
                    "confirm": _confirm_dialog(
                        f"Stop {ticker}?",
                        f"Pauses node {wl_id} only; other nodes keep running.\n"
                        f"*Stops SELLs too* — if this node holds a position, its automated exit "
                        f"will NOT be placed.\nResting broker orders are NOT cancelled.",
                        "Stop it", style="danger"),
                })

        # Per-ticker automation pause/resume -- only shown for tickers actually in
        # the automation pilot scope (see schwab_safety.AUTOMATION_ENABLED_TICKERS),
        # so the other manual-only tickers don't show a button that does nothing.
        if ticker in schwab_safety.AUTOMATION_ENABLED_TICKERS:
            automation_on = schwab_safety.ticker_automation_enabled(ticker)
            elements.append(
                {"type": "button", "text": {"type": "plain_text", "text": f"⏸️ Pause {ticker} Automation"},
                 "style": "danger", "action_id": "pause_ticker_automation", "value": ticker}
                if automation_on else
                {"type": "button", "text": {"type": "plain_text", "text": f"▶️ Resume {ticker} Automation"},
                 "style": "primary", "action_id": "resume_ticker_automation", "value": ticker}
            )

            # Auto-fill-detection toggle -- separate from the placement toggle above and
            # defaults off (see schwab_safety.AUTO_FILL_DETECTION_PATH comment): placement
            # automation is proven via this session's dry-run testing, fill detection isn't
            # exercised against a real fill yet.
            # node-scoped (not ticker-only) -- see schwab_safety.node_auto_fill_detection_enabled's
            # docstring: this was the ticker-only-keying gap the 2026-07-25/26 wl_id refactor
            # missed. Every row here is built from a real watch_list node (build_reference_table),
            # so wl_id is always resolvable; still guarded rather than assumed, since a NULL-wl_id
            # open position (a watch_list row deleted out from under it, e.g. EDC id=15) would
            # never render a row/button here at all and should fail closed, not toggle every node
            # sharing the ticker.
            wl_id = node.get('id') if node else None
            if wl_id is not None:
                fill_detection_on = (schwab_safety.auto_fill_detection_enabled(ticker)
                                      and schwab_safety.node_auto_fill_detection_enabled(wl_id))
                fd_value = json.dumps({"ticker": ticker, "wl_id": wl_id})
                # 2026-08-14: the "Enable" button is no longer rendered -- the
                # end state for every real live node is auto-fill-detection ON
                # ("no manual anything"), reached deliberately in bulk via
                # schwab_safety.bulk_enable_auto_fill_detection(apply=True),
                # not one ad hoc tap at a time. "Disable" stays as the
                # emergency override. The enable HANDLER stays registered
                # (signals_handlers.py): old reports in Slack scrollback keep
                # their Enable button forever, and a stray click on one just
                # moves toward the intended all-on state, so unregistering it
                # would turn a harmless click into a dead-button confusion
                # for no safety gain.
                if fill_detection_on:
                    elements.append(
                        {"type": "button", "text": {"type": "plain_text", "text": f"🤖 Disable {ticker} Auto-Fill Detection"},
                         "style": "danger", "action_id": "disable_auto_fill_detection", "value": fd_value})

        if elements:
            blocks.append({"type": "actions", "elements": elements})

    return blocks
