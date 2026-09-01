"""Dedicated Slack "trade control" channel: one self-updating message per
real-live node, edited in place through the whole trade lifecycle.

WHY THIS EXISTS (2026-08-17). During a real Schwab API outage the user could
not find a pending "Trailing Buy Order Placed" confirmation for SOXS -- the
button was posted correctly, it was just buried in normal channel scrollback
under everything else the daemon says in a day. Reducing alert VOLUME (the
separate should_alert_live/capital-at-stake noise work) does not fix that:
the missing capability is a FIXED PLACE to look that always shows current
order-control state for the real tickers, with any pending control action
reachable from that same place, no scrolling.

WHAT IT IS NOT. It is not a replacement for, or a redirect of, the main
channel (cfg.SLACK_CHANNEL) -- that keeps posting exactly what it posts
today, unchanged. A full live/dry_run channel split was considered and
deliberately rejected earlier (docs/deep_backlog.md, 2026-08-16 "channel
separation"); this is the narrow, additive slice of it. It is also not a
price dashboard: every fact it renders comes from the local DB (watch_list /
pending_buys / open_positions / trail_state), so a sync costs no yfinance
call, no broker call, and cannot introduce a new failure mode into the poll
loop. The flip side, and the one place it is deliberately weaker than the
main channel's own reminders: it cannot re-verify at render time whether an
automated exit order is really still resting (_exit_order_resting is a real
broker round-trip), so it says "tracked" rather than "confirmed resting" and
leans on local evidence (a stored order_id, a known broker_stop_price) to
decide whether an exit_pending is genuinely waiting on the user.

SCOPE. Exactly the nodes signals_helpers.has_capital_at_stake() accepts
(state='live' AND account trading_enabled AND real notional >=
cfg.CAPITAL_AT_STAKE_THRESHOLD), over db.get_live_nodes() -- every watchlist,
not just the active one, since real live nodes genuinely span several today
(see that function's docstring). Derived live on every sync, never a
hardcoded ticker list, so a node that crosses the threshold by compounding,
or a newly-funded account, joins on its own. A node that drops back out gets
its card retired (one final edit, buttons removed) rather than left sitting
there with live-looking buttons -- but never while it still holds an open
position or a resting pending buy, since has_capital_at_stake can also fall
below the bar transiently (its _last_sale_recovery lookup swallows errors and
falls back to the static column) and losing the card for a node with real
exposure is the exact failure this feature exists to prevent.

ACTIVATION (nothing here posts until this is done -- the feature is inert by
default and cannot create the channel itself: channel creation needs either a
human in Slack or a `channels:manage` bot scope this app does not have):
  1. Create a private or public channel in Slack (e.g. #trade-control).
  2. Invite this bot to it (`/invite @<bot name>` in that channel).
  3. Copy the channel ID (channel name -> View channel details -> bottom of
     the About tab; looks like C0123ABCDEF).
  4. Put `SLACK_TRADE_CONTROL_CHANNEL=C0123ABCDEF` in .env.
  5. Restart the daemon (cfg reads .env once at import).
A channel ID is preferred, but a "#name" works too: the tracking row records
BOTH the configured string and the channel id Slack resolved it to, and
chat_update always uses the resolved id (Slack returns an id from
chat_postMessage regardless of which form was posted to). Without that split,
a name-configured channel would look like a different channel on every sync
and post a fresh duplicate card every poll cycle.

INTERACTION WITH THE EXISTING BUTTON HANDLERS. The cards carry the SAME
action_ids as the main-channel alerts (trail_buy_order_placed, trail_buy_
filled, sell_exited, ...) so signals_handlers.py works unmodified. Two known,
accepted consequences, both pre-existing in kind:
  * The same confirmation is now tappable from two messages (the original
    alert in the main channel and the card here). Duplicate/stale taps were
    already possible -- an old alert stays clickable in scrollback forever --
    and every one of those handlers already has a stale-click guard (e.g.
    handle_trail_buy_fill_price's "no pending_buys row found" branch).
  * Those handlers chat_update the message that was clicked, so a tap here
    briefly replaces a card with the handler's own confirmation text. The
    next sync restores the card (the tap changed real state, so the
    signature changed); FORCE_REFRESH_MINUTES bounds the worst case where
    it did not (a stale-click guard that changed nothing).
The node-scoped stop/start buttons are the exception -- those handlers post
to the MAIN channel and resend the reference report there, so a tap on a card
produces no visible reply in this channel (the card itself flips to the
opposite button on the next sync).
"""
import hashlib
import json
from datetime import datetime, timedelta

import schwab_safety
import signals_config as cfg
import signals_db as db
import strategies
from signals_blocks import _confirm_dialog
from signals_helpers import (
    _phase_emoji, automation_blockers_other_than_node, has_capital_at_stake, mode_tag,
)

# Re-render a card even when its content signature is unchanged, if it has not
# been touched in this long. Two purposes: repair a card that one of the
# shared button handlers overwrote with its own text without any state change
# behind it (a stale-click guard branch), and keep the "_updated" line on the
# card honest enough to be trusted as a liveness signal.
FORCE_REFRESH_MINUTES = 30

# A persistent failure (bot removed from the channel, channel archived) would
# otherwise mint one coverage_events row per poll cycle all day. Throttled so
# the signal stays legible -- see the logging block in sync_trade_control_channel.
FAILURE_LOG_THROTTLE_MINUTES = 60
_FAILURE_LOG_STATE = {'last_logged_at': None}

# Mirrors signals_notify.check_exit_reminders' own escalation threshold: past
# this many unconfirmed exit reminders, "the automated order is probably still
# resting" stops being the likely explanation. Kept as a named constant so the
# two thresholds are visibly the same decision, not a coincidence.
EXIT_ESCALATION_REMINDERS = 3


def trade_control_channel():
    """The configured channel, or '' when the feature is inert."""
    return (cfg.SLACK_TRADE_CONTROL_CHANNEL or "").strip()


def _client():
    return cfg.bolt_app.client


def real_live_nodes(nodes=None):
    """The real-live set, derived every call (never cached, never hardcoded).
    Defaults to db.get_live_nodes() -- ALL watchlists, not just the active one
    (see that function's docstring: real live nodes span several today, and
    scoping to the active watchlist would silently miss real money, as well as
    mass-retire every card the day the active watchlist is superseded)."""
    nodes = nodes if nodes is not None else db.get_live_nodes()
    return sorted(
        [n for n in nodes if has_capital_at_stake(n)],
        key=lambda n: (n.get('ticker') or '', n.get('id') or 0),
    )


def _signature(parts):
    """Hash of everything that MUST trigger a re-render. Deliberately excludes
    the card's own "_updated <timestamp>" line: hashing the rendered blocks
    directly (the first version of this) put the current minute inside the
    hash, so the signature changed every poll cycle, every card was rewritten
    ~288x/day, and FORCE_REFRESH_MINUTES became unreachable dead code. Both
    review agents caught this independently and reproduced it."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _fmt_price(v):
    return f"${v:.2f}" if isinstance(v, (int, float)) else "?"


def _sl_price(pos):
    """The position's REAL stop-loss price. For the two live-default
    strategies (uses_fixed_sl) the swept `stop_loss` column is vestigial and
    the real SL lives in `fixed_sl` -- same resolution signals_compute.py's
    check_sell_condition and the EOD position-trigger summary already use.
    Reading `stop_loss` directly (the first version of this) rendered a
    materially wrong price, and only on the branch that matters most: the
    fallback fires exactly when broker_stop_price is NULL, i.e. when SL
    placement failed or the account is dry-run."""
    bsp = pos.get('broker_stop_price')
    if bsp:
        return bsp
    ep = pos.get('entry_price')
    if not ep:
        return None
    if strategies.uses_fixed_sl(pos.get('strategy') or ''):
        pct = pos.get('fixed_sl') or 0.0
    else:
        pct = pos.get('stop_loss')
    return ep * (1 - pct / 100.0) if pct is not None else None


def _buy_action_payload(pending):
    """Byte-identical to the payload signals_notify._pending_buy_blocks builds
    for the same buttons -- pinned by tests/test_trade_control_channel.py so
    the two cannot silently drift apart. Deliberately re-built here rather
    than calling that function: it also runs _trailing_buy_status (hourly-cache
    scan) to pick its reminder wording, which this card has no use for."""
    return json.dumps({
        "node":         pending['node'],
        "signal_price": pending['signal_price'],
        "signal_time":  pending['signal_time'],
    })


def _exit_action_payload(pos, exit_pending):
    """Same relationship to signals_notify._exit_pending_blocks as
    _buy_action_payload has to _pending_buy_blocks (and same test pinning it).
    Not reused directly because that builder calls _exit_order_resting(), a
    real broker round-trip, purely to choose its wording -- unacceptable on a
    per-poll, per-node path."""
    return json.dumps({
        "type":          "sell",
        "position_id":   pos['id'],
        "ticker":        pos['ticker'],
        "current_price": exit_pending['current_price'],
        "entry_price":   pos['entry_price'],
        "reason":        exit_pending['reason'],
    })


def _automation_button(node):
    """The node-scoped stop/start control, same action_ids and confirm dialogs
    as the reference report's per-row version. Included on every card
    deliberately: halting one node is the thing actually wanted under time
    pressure, and the Morning Report (the only other place it lives) is a
    once-a-day message that scrolls away like everything else. Unlike the
    fill/exit buttons, these handlers do NOT chat_update the clicked message,
    so tapping one here cannot disturb the card."""
    wl_id = node.get('id')
    ticker = node.get('ticker')
    if wl_id is None:
        return None
    paused = not schwab_safety.node_automation_enabled(wl_id)
    blockers = automation_blockers_other_than_node(ticker, node.get('account'))
    value = json.dumps({"ticker": ticker, "wl_id": wl_id})
    if paused:
        note = f" — note: still blocked by {', '.join(blockers)}" if blockers else ""
        return {
            "type": "button", "style": "primary",
            "text": {"type": "plain_text", "text": f"▶️ Start {ticker}"},
            "action_id": "start_node_automation", "value": value,
            "confirm": _confirm_dialog(
                f"Start {ticker}?",
                f"Resumes automation for *{ticker}* (node {wl_id}). It will place "
                f"real orders again on its next signal{note}.",
                "Start it"),
        }
    suffix = f" (already halted: {blockers[0]})" if blockers else ""
    return {
        "type": "button", "style": "danger",
        "text": {"type": "plain_text", "text": f"🛑 Stop {ticker}{suffix}"},
        "action_id": "stop_node_automation", "value": value,
        "confirm": _confirm_dialog(
            f"Stop {ticker}?",
            f"Pauses node {wl_id} only; other nodes keep running.\n"
            f"*Stops SELLs too* — if this node holds a position, its automated exit "
            f"will NOT be placed.\nResting broker orders are NOT cancelled.",
            "Stop it", style="danger"),
    }


def control_state(node, pos, pending):
    """The card's headline lifecycle phase. Position state wins over a pending
    buy when both exist -- an open position is strictly the higher-risk fact,
    and this ordering matches signals_helpers._phase_emoji (which
    short-circuits on `pos is not None`), so the bubble strip and the text can
    never contradict each other. A surviving pending row's own buttons are
    still rendered alongside (see build_card): the headline picks ONE phase,
    the buttons never drop an outstanding tap."""
    if pos is not None:
        trail_state = pos.get('trail_state') or {}
        if trail_state.get('exit_pending'):
            return 'awaiting_exit'
        if trail_state.get('trailing'):
            # order_placed here is the TRAILING-SELL order, not the entry. The
            # automated path (signals_notify._attempt_automated_sell) sets it
            # itself; when that didn't happen the position is armed with no
            # protective order resting, and the user owes a manual placement +
            # confirmation -- a genuine outstanding control action.
            return 'armed' if trail_state.get('order_placed') else 'awaiting_trail_order'
        return 'held'
    if pending is not None:
        return 'awaiting_fill' if pending.get('order_placed') else 'awaiting_order_placed'
    return 'flat'


def exit_probably_handled(pos, exit_pending):
    """Local-evidence answer to "is this exit already being handled without
    me?" -- a tracked automated order id, or a real broker stop on file for an
    SL exit. Weaker than signals_notify._exit_pending_blocks' fresh
    _exit_order_resting() check (a real broker call, deliberately not made on
    this per-poll path), so the wording it drives says "tracked", never
    "confirmed resting".

    Load-bearing for more than wording: without it every exit_pending rendered
    as 🔔 ACTION NEEDED with a primary-styled Exited button, including the
    routine TRAIL case that check_exit_reminders goes out of its way to stay
    QUIET about (the 2026-08-02 fix). That is an invitation to tap Exited on a
    position whose real order is still resting -- handle_exit_price would
    close it locally at a typed price with no stale guard, leaving local-flat
    vs broker-long divergence."""
    if exit_pending.get('order_id'):
        return True
    return exit_pending.get('reason') == 'SL' and bool(pos.get('broker_stop_price'))


def action_needed(pos, pending):
    """Whether a human tap is genuinely outstanding right now.

    The reminder_count escalation is load-bearing, not decoration: an
    exit_pending's stored order_id proves an order was PLACED, never that it
    is still resting (a REJECTED/CANCELED/already-filled order is
    indistinguishable by id alone -- the exact reasoning behind
    _exit_pending_blocks' own fresh recheck). check_exit_reminders escalates a
    still-unconfirmed exit at reminder_num >= 3 (~45 min) for that reason, so
    this surface escalates on the same threshold rather than staying quiet
    forever on the strength of an id (both reviewers, 2026-08-17)."""
    if pending is not None:
        return True
    if pos is None:
        return False
    trail_state = pos.get('trail_state') or {}
    exit_pending = trail_state.get('exit_pending')
    if exit_pending:
        if exit_pending.get('reminder_count', 0) >= EXIT_ESCALATION_REMINDERS:
            return True
        return not exit_probably_handled(pos, exit_pending)
    return bool(trail_state.get('trailing')) and not trail_state.get('order_placed')


def _pending_elements(node, pending):
    """Buttons for an outstanding pending_buys row. Branches on the node's real
    entry mechanism: a non-trailing-buy node's real confirmation is
    `buy_executed` (which opens the fill-price modal), NOT
    `trail_buy_order_placed` -- rendering trailing-buy wording/buttons for a
    TrailingExitZScoreBreakout market-buy entry would put a button on the card
    that resolves nothing."""
    value = _buy_action_payload(pending)
    if not db._is_trailing_buy(node):
        return [
            {"type": "button", "text": {"type": "plain_text", "text": "Executed"},
             "style": "primary", "action_id": "buy_executed", "value": value},
            {"type": "button", "text": {"type": "plain_text", "text": "Skipped"},
             "action_id": "buy_skipped", "value": value},
        ]
    if pending.get('order_placed'):
        return [
            {"type": "button", "text": {"type": "plain_text", "text": "Filled"},
             "style": "primary", "action_id": "trail_buy_filled", "value": value},
            {"type": "button", "text": {"type": "plain_text", "text": "Missed It"},
             "action_id": "trail_buy_missed", "value": value},
            {"type": "button", "text": {"type": "plain_text", "text": "Cancelled"},
             "action_id": "trail_buy_cancelled", "value": value},
        ]
    return [
        {"type": "button", "text": {"type": "plain_text", "text": "Trailing Buy Order Placed"},
         "style": "primary", "action_id": "trail_buy_order_placed", "value": value},
        {"type": "button", "text": {"type": "plain_text", "text": "Skipped"},
         "action_id": "buy_skipped", "value": value},
    ]


def _position_elements(pos):
    """Buttons for an outstanding action on an open position: confirm the exit,
    or confirm the trailing-stop order was placed."""
    trail_state = pos.get('trail_state') or {}
    exit_pending = trail_state.get('exit_pending')
    if exit_pending:
        handled = exit_probably_handled(pos, exit_pending)
        return [
            {"type": "button", "text": {"type": "plain_text", "text": "Exited"},
             # No primary style when an automated order is already tracked --
             # the tap is a correction path there, not the expected next step.
             **({} if handled else {"style": "primary"}),
             "action_id": "sell_exited", "value": _exit_action_payload(pos, exit_pending)},
            {"type": "button", "text": {"type": "plain_text", "text": "Skipped"},
             "action_id": "sell_skipped", "value": _exit_action_payload(pos, exit_pending)},
        ]
    if trail_state.get('trailing') and not trail_state.get('order_placed'):
        # Same payload signals_notify._trailing_order_blocks builds.
        return [
            {"type": "button", "text": {"type": "plain_text", "text": "Order Placed"},
             "style": "primary", "action_id": "trail_order_placed",
             "value": json.dumps({"position_id": pos['id'], "ticker": pos['ticker']})},
        ]
    return []


def _headline_and_detail(node, pos, pending, state):
    if state == 'awaiting_order_placed':
        if not db._is_trailing_buy(node):
            headline = "BUY signal fired — confirm the market buy was executed"
        else:
            headline = "BUY signal fired — confirm the trailing buy order is resting at the broker"
        detail = (f"signal {_fmt_price(pending.get('signal_price'))}  |  "
                  f"since {pending.get('signal_time') or '?'}")
    elif state == 'awaiting_fill':
        headline = "Order placed at the broker — confirm the fill (or that it was missed/cancelled)"
        detail = (f"signal {_fmt_price(pending.get('signal_price'))}  |  "
                  f"since {pending.get('signal_time') or '?'}"
                  + (f"  |  order {pending['order_id']}" if pending.get('order_id') else ""))
    elif state == 'awaiting_exit':
        exit_pending = (pos.get('trail_state') or {})['exit_pending']
        reason = exit_pending.get('reason')
        if exit_probably_handled(pos, exit_pending):
            headline = (f"{reason} exit fired — automated exit order tracked, should fill on its own "
                        f"(tap Exited only if you've verified a real fill yourself)")
        else:
            headline = f"{reason} exit fired — no automated exit order tracked, confirm the exit"
        detail = (f"entry {_fmt_price(pos.get('entry_price'))}  |  "
                  f"signal {_fmt_price(exit_pending.get('current_price'))}  |  "
                  f"target {_fmt_price(exit_pending.get('target_price'))}")
    elif state in ('armed', 'awaiting_trail_order'):
        trail_state = pos.get('trail_state') or {}
        peak = trail_state.get('peak') or pos.get('entry_price')
        trail_pct = pos.get('trail_sell_pct')
        trigger = peak * (1 - trail_pct / 100.0) if (peak and trail_pct) else None
        headline = ("Armed — trailing sell order tracked at the broker" if state == 'armed'
                    else "Armed — place the trailing stop order at the broker, then confirm")
        detail = (f"entry {_fmt_price(pos.get('entry_price'))} x `{pos.get('shares')}`  |  "
                  f"peak {_fmt_price(peak)}  |  trail-sell {_fmt_price(trigger)}")
    elif state == 'held':
        arm_pct = db._tp_or_arm_pct(pos)
        arm = (pos['entry_price'] * (1 + arm_pct / 100.0)
               if (pos.get('entry_price') and arm_pct is not None) else None)
        headline = "Held — waiting to arm"
        detail = (f"entry {_fmt_price(pos.get('entry_price'))} x `{pos.get('shares')}`  |  "
                  f"sl {_fmt_price(_sl_price(pos))}  |  arm {_fmt_price(arm)}")
    else:
        headline = "Flat — no position, no resting order"
        detail = "waiting for the next buy signal"
    return headline, detail


def build_card(node, pos, pending, now=None):
    """(text, blocks, signature) for one node's persistent card.

    `text` is the mobile notification/fallback line, so the actionable fact
    leads it (standing convention, CLAUDE.md 2026-07-22). Note this only
    produces a real push on the FIRST post -- a chat_update is silent, by
    Slack's design. That is intended: the main channel still fires the
    notifications, this channel is the fixed place to LOOK.

    `signature` deliberately excludes the rendered "_updated" timestamp -- see
    _signature."""
    now = now or datetime.now()
    ticker = node.get('ticker')
    account = node.get('account') or 'unmapped'
    state = control_state(node, pos, pending)
    needs_action = action_needed(pos, pending)
    phase = _phase_emoji(pos, pending)
    acct_tag = f"`{account} · {mode_tag(account, node)}`"
    flag = "🔔 ACTION NEEDED — " if needs_action else ""
    headline, detail = _headline_and_detail(node, pos, pending, state)

    elements = []
    if pos is not None:
        elements += _position_elements(pos)
    if pending is not None:
        # Rendered even when a position is open (the headline shows the
        # position, the higher-risk fact) -- a surviving pending row still
        # carries a real, resolvable tap and must never be silently dropped.
        elements += _pending_elements(node, pending)

    text = f"{flag}{ticker} ({account}) — {headline}"
    body = (
        f"{phase} *{ticker}* {acct_tag} — {flag}{headline}\n"
        f"{detail}\n"
        f"_updated {now.strftime('%Y-%m-%d %H:%M')}_"
    )
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": body}}]

    if cfg.INTERACTIVE:
        auto = _automation_button(node)
        if auto is not None:
            elements.append(auto)
        if elements:
            blocks.append({"type": "actions", "elements": elements})

    signature = _signature({
        'state': state, 'phase': phase, 'flag': flag,
        'headline': headline, 'detail': detail,
        'actions': [(e.get('action_id'), e.get('value'), e.get('text', {}).get('text'))
                    for e in elements],
    })
    return text, blocks, signature


def _retired_card(node):
    ticker = node.get('ticker') if isinstance(node, dict) else node
    text = f"{ticker} — no longer tracked in trade control"
    return text, [{"type": "section", "text": {"type": "mrkdwn", "text":
        f"⚫ *{ticker}* — no longer live above the capital-at-stake bar, and holding no "
        f"open position or resting buy. Card retired; it will come back automatically if "
        f"the node re-enters the real-live set."}}]


def _stale(tracked, now):
    updated_at = tracked.get('updated_at')
    if not updated_at:
        return True
    try:
        last = datetime.strptime(updated_at, '%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return True
    return (now - last) >= timedelta(minutes=FORCE_REFRESH_MINUTES)


def _is_missing_message(exc):
    """Slack says the message is gone (deleted by hand) -- the one error worth
    self-healing by dropping the tracking row so the next sync posts fresh.
    Checks the SlackApiError response payload as well as the string form, since
    the two differ between slack_sdk versions and error shapes."""
    response = getattr(exc, 'response', None)
    try:
        if response is not None and response.get('error') == 'message_not_found':
            return True
    except Exception:
        pass
    return 'message_not_found' in str(exc)


def _failure_log_due(now):
    last = _FAILURE_LOG_STATE['last_logged_at']
    return last is None or (now - last) >= timedelta(minutes=FAILURE_LOG_THROTTLE_MINUTES)


def sync_trade_control_channel(nodes=None, now=None):
    """Post-or-edit one card per real-live node. Safe to call every poll: it
    is DB-only, and it issues a Slack call only when a card's content
    signature actually changed (or FORCE_REFRESH_MINUTES elapsed). Returns a
    small dict of what it did, for logging/tests.

    Every Slack call is individually wrapped: one node's failure (a deleted
    message, a channel the bot got removed from) must not stop the remaining
    nodes' cards from updating."""
    result = {'channel': trade_control_channel(), 'posted': [], 'updated': [], 'retired': [], 'errors': []}
    channel = result['channel']
    if not channel:
        return result
    if not cfg.SOCKET_MODE:
        # chat_update has no webhook equivalent -- a persistent, edited-in-place
        # message is only possible over the Socket Mode/bot-token client.
        return result
    if cfg.SIM_MODE:
        # An ad hoc/sim invocation must never write real message timestamps into
        # trade_control_messages (they would then be edited forever as if real).
        return result

    now = now or datetime.now()

    positions = {}
    for p in db.get_open_positions():
        if p.get('is_dry_run_sim'):
            continue
        if p.get('wl_id') is not None:
            positions[p['wl_id']] = p
    pending_buys = {}
    for p in db.get_pending_buys():
        # Keyed off the real wl_id column, not p['node']['id'] -- a legacy row
        # whose node_json predates that key would otherwise raise here, OUTSIDE
        # the per-node try below, taking down every card's sync at once.
        wl_id = p.get('wl_id') if p.get('wl_id') is not None else (p.get('node') or {}).get('id')
        if wl_id is not None:
            pending_buys[wl_id] = p

    # The render set is the real-live set PLUS any already-tracked node that
    # still has real exposure. Those two have to move together: the retire loop
    # below refuses to retire a card while a position/pending buy is open (a
    # transient has_capital_at_stake dip must not delete the card of a node
    # holding real money), so without adding the same nodes back here, such a
    # card would be neither updated nor retired -- frozen forever, mid-
    # lifecycle, with live real-money buttons on it. Found by both reviewers in
    # the rebuttal round, 2026-08-17.
    render = {n['id']: n for n in real_live_nodes(nodes)}
    for tracked in db.get_trade_control_messages():
        nid = tracked['node_id']
        if nid in render:
            continue
        if positions.get(nid) or pending_buys.get(nid):
            node = db.get_watch_list_node_by_id(nid)
            if node is not None:
                render[nid] = node
    render_nodes = sorted(render.values(), key=lambda n: (n.get('ticker') or '', n.get('id') or 0))

    for node in render_nodes:
        try:
            text, blocks, signature = build_card(
                node, positions.get(node['id']), pending_buys.get(node['id']), now=now)
            tracked = db.get_trade_control_message(node['id'])
            # A NULL configured_channel is a row written before that column
            # existed, not evidence of a different channel -- treating it as a
            # mismatch would make the first sync after the migration retire and
            # re-post every existing card. It backfills on the next real update.
            tracked_cfg = (tracked or {}).get('configured_channel')
            if tracked is not None and tracked_cfg is not None and tracked_cfg != channel:
                # The configured channel changed under us. Retire the old card
                # first -- otherwise it is orphaned in the abandoned channel
                # with live real-money buttons on it forever.
                try:
                    r_text, r_blocks = _retired_card(node)
                    _client().chat_update(channel=tracked['channel'], ts=tracked['message_ts'],
                                           text=r_text, blocks=r_blocks)
                except Exception as e:
                    result['errors'].append(f"{node.get('ticker')} (orphan cleanup): {e}")
                tracked = None
            if tracked is None:
                resp = _client().chat_postMessage(channel=channel, text=text, blocks=blocks)
                # resp['channel'] is always the resolved ID, even when posting
                # to a "#name" -- store both so a name-configured channel does
                # not look different on every sync (and repost every cycle).
                db.set_trade_control_message(node['id'], resp['channel'], resp['ts'], signature,
                                             configured_channel=channel, updated_at=now)
                result['posted'].append(node['ticker'])
                continue
            if tracked['fingerprint'] == signature and not _stale(tracked, now):
                continue
            _client().chat_update(channel=tracked['channel'], ts=tracked['message_ts'],
                                   text=text, blocks=blocks)
            db.set_trade_control_message(node['id'], tracked['channel'], tracked['message_ts'],
                                         signature, configured_channel=channel, updated_at=now)
            result['updated'].append(node['ticker'])
        except Exception as e:
            # A card the user deleted in Slack can never be edited again --
            # without this the node's card would stay permanently broken (every
            # later sync retrying the same dead ts). Dropping the tracking row
            # makes the next sync post a fresh card. Deliberately narrow: any
            # other failure (rate limit, transient outage) keeps the row, since
            # clearing on those would post a duplicate card next cycle.
            if _is_missing_message(e):
                db.clear_trade_control_message(node['id'])
            result['errors'].append(f"{node.get('ticker')}: {e}")
            print(f"  [trade_control] {node.get('ticker')}: {e}")

    for tracked in db.get_trade_control_messages():
        # `render` already contains both the real-live set and every tracked
        # node still holding real exposure (see its construction above) --
        # has_capital_at_stake can drop below the bar for reasons that are not
        # "this node is done" (a transient _last_sale_recovery failure falling
        # back to the static column, a losing trade shrinking effective
        # notional), and a card is most needed exactly when a position is open.
        if tracked['node_id'] in render:
            continue
        node = db.get_watch_list_node_by_id(tracked['node_id']) or {'ticker': f"node {tracked['node_id']}"}
        try:
            # Edited wherever the card actually lives (tracked['channel'], the
            # resolved id), not only when that matches the currently-configured
            # channel: a card left in an abandoned channel still shows live
            # real-money buttons, which is precisely what retiring prevents.
            text, blocks = _retired_card(node)
            _client().chat_update(channel=tracked['channel'], ts=tracked['message_ts'],
                                   text=text, blocks=blocks)
            # Only cleared once the edit actually succeeded -- dropping the row
            # on a transient failure would strand a live-button card in Slack
            # with nothing left tracking it. A permanent failure keeps retrying,
            # which the throttled failure logging below keeps from getting noisy.
            db.clear_trade_control_message(tracked['node_id'])
            result['retired'].append(node.get('ticker'))
        except Exception as e:
            result['errors'].append(f"retire {node.get('ticker')}: {e}")
            print(f"  [trade_control] retire {node.get('ticker')}: {e}")

    # Deliberately only on a card being created/retired or a real failure --
    # not on routine edits, which happen many times a day and would bury the
    # signal. "live" unconditionally, same reasoning as
    # send_reference_report's morning_report_delivery event: this is a
    # channel-level delivery fact, not scoped to any one account's dry_run
    # flag, and _coverage_mode(None) would wrongly label it dry_run forever.
    #
    # result='failed' (not 'error') is load-bearing: signals_notify's
    # _CONCERNING_RESULT_SUBSTRINGS matches on "fail", so a broken control
    # surface gets picked up by the existing check_intraday_risk_review Slack
    # alert for free instead of needing its own alert path. Throttled, because
    # a persistent failure (bot removed from the channel, channel archived --
    # neither self-heals) would otherwise mint one row every poll cycle all
    # day and recreate exactly the noise problem that review layer exists to
    # avoid.
    if result['errors']:
        if _failure_log_due(now):
            _FAILURE_LOG_STATE['last_logged_at'] = now
            db.log_coverage_event(
                "trade_control_sync", "live", result="failed",
                detail="; ".join(result['errors'])[:500])
    elif result['posted'] or result['retired']:
        db.log_coverage_event(
            "trade_control_sync", "live", result="delivered",
            detail=f"posted={len(result['posted'])} retired={len(result['retired'])}")
    return result
