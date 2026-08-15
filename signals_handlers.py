"""Bolt interactive handlers (Socket Mode only) -- buttons/modals for confirming
buy/sell fills and posting corrections from the reference report. Importing this
module registers the handlers with cfg.bolt_app as a side effect."""
import json
from datetime import datetime

import signals_config as cfg
import signals_db as db
import signals_compute as compute
import schwab_client
import schwab_safety
from signals_blocks import _post_message, _price_input_block, _shares_input_block
from signals_helpers import (_existing_position_note, _last_sale_recovery, clear_corp_action_alert,
                              automation_blockers_other_than_node, mode_tag)
from signals_notify import (send_reference_report, send_coverage_report, _place_stop_loss_for_position,
                             _coverage_mode, _exit_order_resting, close_addon_leg_real_if_open)

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
        db.mark_pending_buy_placed_by_wl_id(data['node']['id'])
        client.chat_update(
            channel=channel, ts=ts,
            text=f"BUY {ticker} — order placed, waiting for fill",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"*{ticker}* — trailing buy order placed, waiting for fill"}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Filled"},
                     "style": "primary", "action_id": "trail_buy_filled", "value": json.dumps(data)},
                    {"type": "button", "text": {"type": "plain_text", "text": "Missed It"},
                     "action_id": "trail_buy_missed", "value": json.dumps(data)},
                    {"type": "button", "text": {"type": "plain_text", "text": "Cancelled"},
                     "action_id": "trail_buy_cancelled", "value": json.dumps(data)},
                ]},
            ],
        )

    @cfg.bolt_app.action("trail_buy_filled")
    def handle_trail_buy_filled(ack, body, client):
        ack()
        data              = json.loads(body['actions'][0]['value'])
        channel           = body['channel']['id']
        ts                = body['message']['ts']
        ticker            = data['node']['ticker']
        signal_price      = data['signal_price']
        # D4 (docs/plans/real_order_execution_drought_addon.md): a drought
        # pending buy's suggested share prefill must use flat starting_notional,
        # not core's compounding _last_sale_recovery basis -- found by
        # contextual Opus review before this shipped (inconsistent with the
        # dispatch fix already applied to handle_entry_price's own prefill).
        _pending_for_prefill = db.get_pending_buy_by_wl_id(data['node']['id'])
        if _pending_for_prefill and _pending_for_prefill.get('position_source') == 'drought_overlay':
            suggested_shares = int((data['node'].get('starting_notional') or 50000) // signal_price) \
                if signal_price else None
        else:
            suggested_shares = int(_last_sale_recovery(data['node']) // signal_price) if signal_price else None
        client.views_open(
            trigger_id=body['trigger_id'],
            view={
                "type":             "modal",
                "callback_id":      "trail_buy_fill_price_submit",
                "private_metadata": json.dumps({"data": data, "channel": channel, "ts": ts}),
                "title":  {"type": "plain_text", "text": "Fill Price"},
                "submit": {"type": "plain_text", "text": "Confirm"},
                "close":  {"type": "plain_text", "text": "Cancel"},
                "blocks": [_price_input_block(), _shares_input_block(suggested_shares)],
            },
        )

    @cfg.bolt_app.action("trail_buy_missed")
    def handle_trail_buy_missed(ack, body, client):
        """For when the bounce trigger fired (per _trailing_buy_status) before the
        real broker order was resting -- distinct from Cancelled, which implies the
        order itself was pulled. Here the order may still be live at the broker;
        this just stops the app from nagging about a bounce that already passed it
        by. If it fills later, record it via Manual Open from the reference report."""
        ack()
        data    = json.loads(body['actions'][0]['value'])
        channel = body['channel']['id']
        ts      = body['message']['ts']
        ticker  = data['node']['ticker']
        db.clear_pending_buy_by_wl_id(data['node']['id'])
        client.chat_update(
            channel=channel, ts=ts,
            text=f"BUY {ticker} — missed it",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*BUY {ticker}* — missed it (bounce already passed before the order was live). "
                             f"No longer tracking/reminding. Order may still be resting at the broker — if it "
                             f"fills later, record it via Manual Open from the reference report."}}],
        )

    @cfg.bolt_app.action("trail_buy_cancelled")
    def handle_trail_buy_cancelled(ack, body, client):
        ack()
        data    = json.loads(body['actions'][0]['value'])
        channel = body['channel']['id']
        ts      = body['message']['ts']
        ticker  = data['node']['ticker']
        db.clear_pending_buy_by_wl_id(data['node']['id'])
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
        ticker       = node['ticker']

        fill_price = float(body['view']['state']['values']['price_block']['price_input']['value'])
        drift_pct  = (fill_price - signal_price) / signal_price * 100
        shares     = int(body['view']['state']['values']['shares_block']['shares_input']['value'])

        if not any(p['node']['id'] == node['id'] for p in db.get_pending_buys()):
            # See handle_entry_price's identical guard -- the pending_buys row
            # this button was built from is already gone (Missed It/Cancelled,
            # or a stale click on an old message). Matched on the node's own
            # wl_id, not ticker -- a ticker-only match would incorrectly pass
            # if a *different* concurrent node on the same ticker still has a
            # pending row (see docs/backlog_cache.md's wl_id refactor entry).
            print(f"  [warn] {ticker} — no pending_buys row found, ignoring stale Filled confirmation")
            db.log_coverage_event("stale_buy_button_guard", _coverage_mode(node.get('account')), ticker=ticker,
                                   node_id=node['id'], result="guard_fired", detail="trail_buy_fill_price")
            client.chat_update(
                channel=channel, ts=ts,
                text=f"{ticker} — already resolved, this confirmation was ignored",
                blocks=[{"type": "section", "text": {"type": "mrkdwn",
                         "text": f"⚠️ *{ticker}* — this signal was already resolved (missed/cancelled) -- "
                                 f"this stale confirmation was *not* recorded."}}],
            )
            return

        same_ticker_pendings = [p for p in db.get_pending_buys() if p['ticker'] == ticker]
        if len(same_ticker_pendings) > 1:
            db.log_coverage_event("buy_buttons_resolve_correct_node", _coverage_mode(node.get('account')),
                                   ticker=ticker, node_id=node['id'], result="resolved",
                                   detail=f"{len(same_ticker_pendings)} pending for {ticker}")

        # hold-time origin: pass the real fill moment (not the pending buy's
        # original, earlier signal_time) for BOTH signal_time and entry_time --
        # see the matching comment in signals_notify.py's _reconcile_buy_fill,
        # this is the manual-confirmation twin of that same automated path
        # (fixed 2026-07-31).
        fill_time = datetime.now()
        # Fetch the pending row fresh (BEFORE clearing it) for its
        # position_source discriminator -- `node` here is the Slack-metadata
        # snapshot, not a fresh pending_buys row, so it doesn't carry that
        # field (docs/plans/real_order_execution_drought_addon.md 4.3).
        # Guaranteed to exist -- the guard above already confirmed a pending
        # row for this node['id'] is present.
        pending = db.get_pending_buy_by_wl_id(node['id'])
        opened = db.open_position_from_pending(pending, signal_price, fill_time, fill_price, fill_time,
                                                shares=shares)
        db.clear_pending_buy_by_wl_id(node['id'])

        if not opened:
            print(f"  [warn] {ticker} already has an open position — ignored duplicate Filled confirmation")
            client.chat_update(
                channel=channel, ts=ts,
                text=f"{ticker} — ALREADY OPEN, this fill was ignored",
                blocks=[{"type": "section", "text": {"type": "mrkdwn",
                         "text": f"⚠️ *{ticker}* — a position was already open, this Filled confirmation "
                                 f"was *not* recorded (no duplicate created). {_existing_position_note(ticker, wl_id=node['id'])}"}}],
            )
            return

        db.log_coverage_event("manual_buy_confirmation_account",
                               _coverage_mode(node.get('account')) if node.get('account') else "unattributed",
                               ticker=ticker, node_id=node['id'],
                               result="opened" if node.get('account') else "no_account",
                               detail=f"account={node.get('account')!r}")

        if ticker in schwab_safety.AUTOMATION_ENABLED_TICKERS:
            # _place_stop_loss_for_position already handles every broker-call
            # failure it anticipates (SafetyViolation, generic Exception +
            # retry, terminal alert) -- this catches anything unanticipated
            # (e.g. a DB write failure) so it can never prevent the "Executed"/
            # "Filled" confirmation below: the real position is already open
            # at this point, and the user seeing that confirmation matters
            # more than an SL-placement failure being silent (which it isn't
            # anyway -- the position still shows up as unprotected via the
            # missing_sl reconciliation check on the next poll, or the
            # function's own UNPROTECTED alert on a caught failure). Found
            # 2026-08-01, paired independent+contextual review of the
            # handle_entry_price fix that added this call to a 2nd site.
            try:
                _place_stop_loss_for_position(node, ticker)
            except Exception as e:
                print(f"  [warn] {ticker} — unexpected error in _place_stop_loss_for_position: {e}")

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
        db.clear_pending_buy_by_wl_id(data['node']['id'])
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
        ticker       = node['ticker']

        exec_price = float(body['view']['state']['values']['price_block']['price_input']['value'])
        drift_pct  = (exec_price - signal_price) / signal_price * 100
        now        = datetime.now()
        # Fetch the pending row fresh for its position_source discriminator --
        # `node` here is the Slack-metadata snapshot, not a fresh pending_buys
        # row (docs/plans/real_order_execution_drought_addon.md 4.3). A real
        # drought manual-Executed confirmation sizes off starting_notional
        # (D4, flat -- generate_drought_trades is sizing-agnostic), never off
        # _last_sale_recovery's core-only compounding basis, which could be a
        # very different (and unrelated) number.
        pending = db.get_pending_buy_by_wl_id(node['id'])
        if pending and pending.get('position_source') == 'drought_overlay':
            shares = int((node.get('starting_notional') or 50000) // exec_price)
        else:
            shares = int(_last_sale_recovery(node) // exec_price)

        if not any(p['node']['id'] == node['id'] for p in db.get_pending_buys()):
            # The pending_buys row this button was built from is already gone
            # (Skipped, cleared by another path, or a stale/duplicate button
            # click on an old message) -- proceeding would open a real
            # position for an abandoned/already-resolved signal. Matched on
            # the node's own wl_id, not ticker -- see handle_trail_buy_fill_
            # price's identical guard.
            print(f"  [warn] {ticker} — no pending_buys row found, ignoring stale Executed confirmation")
            db.log_coverage_event("stale_buy_button_guard", _coverage_mode(node.get('account')), ticker=ticker,
                                   node_id=node['id'], result="guard_fired", detail="entry_price")
            client.chat_update(
                channel=channel, ts=ts,
                text=f"{ticker} — already resolved, this confirmation was ignored",
                blocks=[{"type": "section", "text": {"type": "mrkdwn",
                         "text": f"⚠️ *{ticker}* — this signal was already resolved (skipped/cleared) -- "
                                 f"this stale confirmation was *not* recorded."}}],
            )
            return

        same_ticker_pendings = [p for p in db.get_pending_buys() if p['ticker'] == ticker]
        if len(same_ticker_pendings) > 1:
            db.log_coverage_event("buy_buttons_resolve_correct_node", _coverage_mode(node.get('account')),
                                   ticker=ticker, node_id=node['id'], result="resolved",
                                   detail=f"{len(same_ticker_pendings)} pending for {ticker}")

        # pending is guaranteed present -- the guard above already confirmed
        # a pending row for this node['id'] exists.
        opened = db.open_position_from_pending(pending, signal_price, signal_time, exec_price, now,
                                                shares=shares)

        db.clear_pending_buy_by_wl_id(node['id'])

        if not opened:
            print(f"  [warn] {ticker} already has an open position — ignored duplicate Executed confirmation")
            client.chat_update(
                channel=channel, ts=ts,
                text=f"{ticker} — ALREADY OPEN, this fill was ignored",
                blocks=[{"type": "section", "text": {"type": "mrkdwn",
                         "text": f"⚠️ *{ticker}* — a position was already open, this Executed confirmation "
                                 f"was *not* recorded (no duplicate created). {_existing_position_note(ticker, wl_id=node['id'])}"}}],
            )
            return

        db.log_coverage_event("manual_buy_confirmation_account",
                               _coverage_mode(node.get('account')) if node.get('account') else "unattributed",
                               ticker=ticker, node_id=node['id'],
                               result="opened" if node.get('account') else "no_account",
                               detail=f"account={node.get('account')!r}")

        # Mirrors handle_trail_buy_fill_price's identical gate (found missing
        # here 2026-08-01, paired independent+contextual review + fake_broker
        # test-quality audit): every real live BUY entry is still manually
        # confirmed via Slack regardless of automation scope (only post-fill
        # housekeeping is automated today), so this is the market-buy/
        # TrailingExitZScoreBreakout twin of that trailing-buy path -- without
        # it, an automation-scoped market-buy fill opened with no automated
        # protective stop, and the missing_sl reconciliation check can't catch
        # a stop that was never attempted (it only catches one that was
        # attempted and later found missing at the broker).
        note   = f"${exec_price:.4f}  (drift: {drift_pct:+.2f}%)"
        print(f"  Position opened via Slack: {ticker} at {note}")
        client.chat_update(
            channel=channel, ts=ts,
            text=f"BUY {ticker} — Executed at {note}",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*BUY {ticker}* — Executed at {note}"}}],
        )

        if ticker in schwab_safety.AUTOMATION_ENABLED_TICKERS:
            # Placed AFTER the "Executed" confirmation above, deliberately --
            # handle_entry_price is also the manual catch-up/backdated-entry
            # flow (a genuinely missed signal, confirmed days later, see the
            # comment in _reconcile_buy_fill), where entry_price can already
            # be well past its stop by confirmation time. In that case this
            # call's own self-correcting branch fires a real forced market
            # SELL + a separate "already breached" Slack alert -- reordered
            # so the user always sees "your fill was recorded" before any
            # forced-exit alert, not after (found 2026-08-01, paired
            # independent+contextual review).
            # _place_stop_loss_for_position already handles every broker-call
            # failure it anticipates (SafetyViolation, generic Exception +
            # retry, terminal alert) -- this catches anything unanticipated
            # (e.g. a DB write failure) so it can't raise back into the
            # handler after the confirmation has already been sent.
            try:
                _place_stop_loss_for_position(node, ticker)
            except Exception as e:
                print(f"  [warn] {ticker} — unexpected error in _place_stop_loss_for_position: {e}")

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
        # Re-fetch fresh, not the stale open_positions snapshot -- same clobber
        # class documented throughout this fragile region (e.g. signals_notify.py
        # :1546-1554): a broker round-trip (cancel_order, below) can take seconds,
        # during which the poll loop may persist a newer trail_state that this
        # write would otherwise silently overwrite.
        pos = db.get_position_by_id(position_id)
        cancel_note = ""
        if pos:
            state = dict(pos.get('trail_state') or {})
            exit_pending = state.get('exit_pending') or {}
            order_id = exit_pending.get('order_id')
            reason = exit_pending.get('reason', data.get('reason'))
            hold_time_forced = bool(state.get('exit_forced_by_hold_time'))
            # A genuine TRAIL breach (not hold-time-forced) reuses the SAME
            # order placed at the earlier arm event (signals_notify.py's
            # _attempt_automated_exit_sell, reason=='TRAIL' branch) -- it's the
            # position's ONLY live protection, not a fresh order placed in
            # response to this specific alert (arming already replaced the
            # stop-loss with it). Cancelling it on Skip would leave the
            # position with zero broker protection while the alert claims "no
            # action needed" -- confirmed via paired Opus review, 2026-08-02.
            # Skip here only clears this alert's own reminder tracking; the
            # standing trailing-sell keeps resting untouched, exactly as
            # before this fix existed.
            is_standing_trail_protection = (reason == 'TRAIL' and not hold_time_forced)
            if order_id is not None and not is_standing_trail_protection:
                # Checked (not assumed) via the broker's real status, same as
                # every other _exit_order_resting caller; attempted whenever
                # status isn't confirmed terminal (True or None), not just True
                # -- an unconfirmed cancel against an already-filled/rejected
                # order is a safe no-op at the broker, but a skipped cancel
                # against a genuinely resting one leaves it live indefinitely.
                resting = _exit_order_resting(pos, reason, order_id)
                if resting is not False:
                    try:
                        _, confirmed = schwab_client.cancel_order(pos.get('account'), ticker, order_id)
                    except Exception as e:
                        confirmed = None
                        cancel_note = f" — ⚠️ cancel FAILED: {e} (order may still be resting, check broker)"
                    if confirmed == "CANCELED":
                        cancel_note = " — resting exit order cancelled"
                        state.pop('exit_pending', None)
                        if hold_time_forced:
                            # The just-cancelled order is what exit_order_id/
                            # resting_order_id resolves to for the NEXT forced-
                            # exit attempt (signals_notify.py:286-287) -- left
                            # stale, that attempt would try to replace an
                            # already-cancelled order and fail. Clearing lets
                            # it fall back to placing a fresh order instead.
                            state.pop('exit_order_id', None)
                            state['hold_time_replaced'] = False
                    elif confirmed == "FILLED":
                        # The order actually filled (a real race against the
                        # cancel, or it had already filled) -- leave exit_pending
                        # in place so check_own_sell_fills' existing polling
                        # reconciles the real close with the real fill price on
                        # the next cycle, instead of this handler guessing.
                        cancel_note = " — order had already FILLED, position will reconcile automatically"
                    elif not cancel_note:
                        # Unconfirmed (poll failed) or some other non-terminal
                        # status -- leave exit_pending in place so
                        # check_own_sell_fills/check_exit_reminders keep
                        # watching the real order instead of abandoning it.
                        cancel_note = " — cancel requested, unconfirmed at broker (still being watched)"
                else:
                    # _exit_order_resting confirmed a terminal status but only
                    # as a bool -- FILLED is terminal too, and check_own_sell_fills
                    # can only reconcile it if exit_pending (with this order_id)
                    # is still there for it to find. Get the real status before
                    # deciding whether to pop.
                    real_status = schwab_client.get_order_status(pos.get('account'), order_id)
                    if real_status == "FILLED":
                        cancel_note = " — order had already FILLED, position will reconcile automatically"
                    else:
                        state.pop('exit_pending', None)
            else:
                state.pop('exit_pending', None)
            db.update_position_trail_state(pos['id'], state)
        client.chat_update(
            channel=channel, ts=ts,
            text=f"SELL {ticker} — Skipped (position kept open){cancel_note}",
            blocks=[{"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*SELL {ticker}* — Skipped (position kept open){cancel_note}"}}],
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

        # Fetch fresh BEFORE closing -- close_position deletes the
        # open_positions row (moves it to trade_log), and
        # close_addon_leg_real_if_open (docs/plans/real_order_execution_
        # drought_addon.md 7.2) needs the position dict (id/ticker/account)
        # to find any open addon leg for it.
        pos = db.get_position_by_id(position_id)

        # exit_bar_time derived directly (found by cold review 2026-08-14) -- a manual
        # Slack "Exited" confirmation never runs check_sell_condition, so there's no
        # exit_decision_bar already stashed for close_position() to fall back on; without
        # this the same-bar re-entry cooldown has nothing to compare against for this exit.
        db.close_position(position_id,
                           exit_signal_price=signal_price, exit_price=exit_price,
                           exit_time=datetime.now(), exit_reason=data.get('reason'),
                           exit_bar_time=compute.current_bar_time(ticker))
        try:
            close_addon_leg_real_if_open(pos, exit_price, data.get('reason'), datetime.now())
        except Exception as e:
            print(f"  [warn] {ticker} — unexpected error in close_addon_leg_real_if_open: {e}")

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
        doubling as the confirmation step.

        No longer rendered as a button on NEW reference reports as of
        2026-08-14 (replaced by the node-scoped Stop/Start automation button
        -- see signals_notify._ticker_block). Kept registered deliberately:
        old reports stay clickable in Slack scrollback forever, and this is
        still a genuine correction path worth having."""
        ack()
        data   = json.loads(body['actions'][0]['value'])
        ticker = data['node']['ticker']
        current_price, _ = compute._current_price(ticker)
        suggested_shares = int(_last_sale_recovery(data['node']) // current_price) if current_price else None
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
        # For mode_tag ONLY -- the button payload node (_ticker_block's
        # node_fields) deliberately carries no 'state', and
        # effectively_dry_run treats a missing state as not-live, so passing
        # `node` straight in would label every real Manual Open "DRY-RUN":
        # the reassuring-wrong direction mode_tag's own docstring forbids.
        # Falls back to the payload only if the row is gone.
        _tag_node = db.get_watch_list_node_by_id(node.get('id')) or node

        price  = float(body['view']['state']['values']['price_block']['price_input']['value'])
        shares = int(body['view']['state']['values']['shares_block']['shares_input']['value'])
        now    = datetime.now()

        opened = db.open_position(node, price, now, price, now, shares=shares)
        db.clear_pending_buy_by_wl_id(node['id'])

        if not opened:
            print(f"  [warn] {ticker} already has an open position — ignored duplicate Manual Open")
            _post_message(f"{ticker} ({node.get('account')} · {mode_tag(node.get('account'), _tag_node)}) "
                          f"— ALREADY OPEN, this Manual Open was ignored",
                          blocks=[{"type": "section", "text": {"type": "mrkdwn",
                          "text": f"⚠️ *{ticker}* ({node.get('account')} · {mode_tag(node.get('account'), _tag_node)}) "
                                  f"— a position was already open, this Manual Open "
                                  f"was *not* recorded (no duplicate created). {_existing_position_note(ticker, wl_id=node['id'])}"}}])
            return

        db.log_coverage_event("manual_buy_confirmation_account",
                               _coverage_mode(node.get('account')) if node.get('account') else "unattributed",
                               ticker=ticker, node_id=node['id'],
                               result="opened" if node.get('account') else "no_account",
                               detail=f"account={node.get('account')!r} manual_open")

        note = f"${price:.4f}  {shares} shares"
        print(f"  Position manually opened via Slack: {ticker} at {note}")
        _tag = f"({node.get('account')} · {mode_tag(node.get('account'), _tag_node)})"
        _post_message(f"MANUAL OPEN {ticker} {_tag} — {note}",
                      blocks=[{"type": "section", "text": {"type": "mrkdwn",
                      "text": f"*MANUAL OPEN {ticker}* {_tag} — {note}"}}])

    @cfg.bolt_app.action("manual_close")
    def handle_manual_close(ack, body, client):
        """Correction path for a misclick (e.g. hit Skipped after a real exit) --
        closes a position directly from the reference report, price-entry modal
        doubling as the confirmation step.

        Unrendered on new reports since 2026-08-14, still registered -- same
        reasoning as handle_manual_open above."""
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

        # Fetch fresh BEFORE closing -- see handle_exit_price's identical
        # comment (docs/plans/real_order_execution_drought_addon.md 7.2).
        pos = db.get_position_by_id(position_id)

        # exit_bar_time derived directly, same reason as handle_exit_price above.
        db.close_position(position_id,
                           exit_signal_price=exit_price, exit_price=exit_price,
                           exit_time=now, exit_reason='MANUAL',
                           exit_bar_time=compute.current_bar_time(ticker))
        try:
            close_addon_leg_real_if_open(pos, exit_price, 'MANUAL', now)
        except Exception as e:
            print(f"  [warn] {ticker} — unexpected error in close_addon_leg_real_if_open: {e}")

        note = f"${exit_price:.4f}  (P&L: {actual_pnl:+.2f}%)"
        print(f"  Position manually closed via Slack: {ticker} at {note}")
        # pos was already re-fetched above; `or {}` guards the same
        # position-vanished case close_addon_leg_real_if_open's try/except
        # already tolerates, so the close confirmation can never be lost to
        # a labeling lookup. Node passed for the same reason signals_notify's
        # builders do: without it a node-level dry-run on a trading_enabled
        # account mislabels as LIVE.
        _acct = (pos or {}).get('account')
        _tag = f"({_acct} · {mode_tag(_acct, db.get_watch_list_node_by_id((pos or {}).get('wl_id')))})"
        _post_message(f"MANUAL CLOSE {ticker} {_tag} — {note}",
                      blocks=[{"type": "section", "text": {"type": "mrkdwn",
                      "text": f"*MANUAL CLOSE {ticker}* {_tag} — {note}"}}])

    @cfg.bolt_app.action("resend_ref_table")
    def handle_resend_ref_table(ack, body, client):
        """On-demand refresh -- posts a brand new reference report rather than
        editing the clicked one in place, so the old report (and its now-stale
        manual-open/close buttons) stays as a historical record."""
        ack()
        send_reference_report(db.get_watchlist())

    @cfg.bolt_app.action("send_coverage_report")
    def handle_send_coverage_report(ack, body, client):
        ack()
        send_coverage_report()

    @cfg.bolt_app.action("stop_engine")
    def handle_stop_engine(ack, body, client):
        ack()
        user = body.get('user', {}).get('username', 'someone')
        schwab_safety.engage_kill_switch(reason=f"Stop Engine button by {user}")
        _post_message(f"\U0001F6D1 Automated engine STOPPED by {user}")
        send_reference_report(db.get_watchlist())

    @cfg.bolt_app.action("start_engine")
    def handle_start_engine(ack, body, client):
        ack()
        user = body.get('user', {}).get('username', 'someone')
        schwab_safety.disengage_kill_switch()
        _post_message(f"▶️ Automated engine STARTED by {user}")
        send_reference_report(db.get_watchlist())

    @cfg.bolt_app.action("pause_ticker_automation")
    def handle_pause_ticker_automation(ack, body, client):
        ack()
        ticker = body['actions'][0]['value']
        user = body.get('user', {}).get('username', 'someone')
        schwab_safety.pause_ticker_automation(ticker, reason=f"Pause button by {user}")
        _post_message(f"⏸️ {ticker} automation PAUSED by {user} — still alerts normally, just won't place real orders")
        send_reference_report(db.get_watchlist())

    @cfg.bolt_app.action("resume_ticker_automation")
    def handle_resume_ticker_automation(ack, body, client):
        ack()
        ticker = body['actions'][0]['value']
        user = body.get('user', {}).get('username', 'someone')
        schwab_safety.resume_ticker_automation(ticker)
        _post_message(f"▶️ {ticker} automation RESUMED by {user}")
        send_reference_report(db.get_watchlist())

    def _log_node_automation_action(ticker, wl_id, result, user):
        """Records a human tapping the per-row Stop/Start button.

        Deliberately its OWN scenario_key, NOT the pre-existing
        'node_level_automation_pause' one. That row exists to prove a
        different thing: that check_order really raises SafetyViolation and
        blocks a real order for a paused node (schwab_safety.py's `blocked`
        branch). Since compute_status flips a row to 'verified-live' on the
        first live event whose result isn't in bad_results, sharing the key
        would have marked that guard live-proven the first time anyone tapped
        Stop -- while the branch it exists to prove had still never fired.
        That's exactly the "scenario_key shared across unrelated code paths"
        trap scripts/coverage_registry.py's module docstring warns about, and
        it would have inflated the 7am/EOD readiness headline.

        Mode comes from the node's real account, not a hardcoded 'live' -- a
        state='live' node in a non-trading_enabled account is not live."""
        node = db.get_watch_list_node_by_id(wl_id)
        account = (node or {}).get('account')
        db.log_coverage_event(
            "node_automation_pause_button",
            _coverage_mode(account) if account else "unattributed",
            ticker=ticker, node_id=wl_id, result=result,
            detail=f"Slack per-row button by {user}")

    def _node_automation_payload(raw, what):
        """Shared parse for the node-scoped Stop/Start buttons. Mirrors the
        auto-fill-detection handlers' stale-button guard exactly (same reason:
        reference reports stay clickable in Slack scrollback indefinitely, so
        a payload shape from before this change can arrive at any time) --
        returns None after posting the guidance message when it can't resolve
        a real node id, rather than guessing a target."""
        try:
            payload = json.loads(raw)
            return payload['ticker'], payload['wl_id']
        except (json.JSONDecodeError, KeyError, TypeError):
            _post_message(f"⚠️ Stale {what} button ({raw!r}) — resend the reference report and use the new one")
            return None

    @cfg.bolt_app.action("stop_node_automation")
    def handle_stop_node_automation(ack, body, client):
        """Node-scoped emergency stop from a reference-report row (Block Kit
        confirm dialog gates the tap itself). Pauses only this watch_list
        node -- sibling nodes on the same ticker in other accounts keep
        running; the header Stop Engine button is the everything-at-once
        escape hatch."""
        ack()
        parsed = _node_automation_payload(body['actions'][0]['value'], "stop-automation")
        if parsed is None:
            return
        ticker, wl_id = parsed
        user = body.get('user', {}).get('username', 'someone')
        schwab_safety.pause_node_automation(wl_id, reason=f"Stop button by {user}")
        _log_node_automation_action(ticker, wl_id, "paused_by_user", user)
        _post_message(f"🛑 {ticker} (node {wl_id}) automation STOPPED by {user} — still alerts, "
                      f"won't place new real orders. This covers SELLs too: an automated exit "
                      f"for this node will not be placed while stopped (resting broker orders untouched)")
        send_reference_report(db.get_watchlist())

    @cfg.bolt_app.action("start_node_automation")
    def handle_start_node_automation(ack, body, client):
        ack()
        parsed = _node_automation_payload(body['actions'][0]['value'], "start-automation")
        if parsed is None:
            return
        ticker, wl_id = parsed
        user = body.get('user', {}).get('username', 'someone')
        schwab_safety.resume_node_automation(wl_id)
        _log_node_automation_action(ticker, wl_id, "resumed_by_user", user)
        # Resuming the NODE flag doesn't mean the node can actually trade --
        # the kill switch and the ticker-level pause gate it independently
        # (schwab_safety.check_order). Reporting a bare "STARTED" while one of
        # those still blocks it is exactly the false-confidence case this
        # message must not create.
        blockers = automation_blockers_other_than_node(ticker)
        if blockers:
            _post_message(f"▶️ {ticker} (node {wl_id}) node-level automation STARTED by {user} — "
                          f"but STILL BLOCKED by {', '.join(blockers)}, so it will not trade yet")
        else:
            _post_message(f"▶️ {ticker} (node {wl_id}) automation STARTED by {user}")
        send_reference_report(db.get_watchlist())

    @cfg.bolt_app.action("enable_auto_fill_detection")
    def handle_enable_auto_fill_detection(ack, body, client):
        ack()
        raw = body['actions'][0]['value']
        try:
            payload = json.loads(raw)
            ticker, wl_id = payload['ticker'], payload['wl_id']
        except (json.JSONDecodeError, KeyError, TypeError):
            # A reference report posted before this node-scoped change carries a
            # bare ticker string, not JSON -- reports stay clickable indefinitely,
            # so this button will keep showing up. Don't fall back to ticker-only
            # enable (that's the exact leak this change fixed); just ask for a
            # fresh report instead.
            _post_message(f"⚠️ Stale auto-fill-detection button ({raw!r}) — resend the reference report and use the new one")
            return
        user = body.get('user', {}).get('username', 'someone')
        # Node-scoped (schwab_safety.node_auto_fill_detection_enabled) -- ticker-level
        # is just the coarse "possible at all" switch; enabling always also sets it,
        # but the real per-node grant is the node-level flag, set only for wl_id.
        schwab_safety.enable_auto_fill_detection(ticker)
        schwab_safety.enable_node_auto_fill_detection(wl_id)
        _post_message(f"🤖 {ticker} (node {wl_id}) auto-fill detection ENABLED by {user} — fills will be auto-recorded, no Filled/Exited click needed")
        send_reference_report(db.get_watchlist())

    @cfg.bolt_app.action("disable_auto_fill_detection")
    def handle_disable_auto_fill_detection(ack, body, client):
        ack()
        raw = body['actions'][0]['value']
        try:
            payload = json.loads(raw)
            ticker, wl_id = payload['ticker'], payload['wl_id']
        except (json.JSONDecodeError, KeyError, TypeError):
            _post_message(f"⚠️ Stale auto-fill-detection button ({raw!r}) — resend the reference report and use the new one")
            return
        user = body.get('user', {}).get('username', 'someone')
        # Only clears this node's flag -- a sibling node on the same ticker that's
        # separately enabled is untouched (schwab_safety.disable_node_auto_fill_detection).
        schwab_safety.disable_node_auto_fill_detection(wl_id)
        _post_message(f"🤖 {ticker} (node {wl_id}) auto-fill detection DISABLED by {user} — back to manual Filled/Exited confirmation")
        send_reference_report(db.get_watchlist())

    @cfg.bolt_app.action("apply_corp_action_correction")
    def handle_apply_corp_action_correction(ack, body, client):
        """Fixing entry_price is what clears the freeze -- check_sell_condition's
        discontinuity check naturally stops triggering once the data matches,
        no separate unfreeze step needed."""
        ack()
        data = json.loads(body['actions'][0]['value'])
        ticker = data['ticker']
        proposed = data['proposed_entry_price']
        db.correct_entry_price(ticker, proposed)
        clear_corp_action_alert(ticker)
        _post_message(f"✅ {ticker} entry_price corrected to ${proposed:.4f} -- SL/arm checks resume")
