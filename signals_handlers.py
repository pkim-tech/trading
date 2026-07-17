"""Bolt interactive handlers (Socket Mode only) -- buttons/modals for confirming
buy/sell fills and posting corrections from the reference report. Importing this
module registers the handlers with cfg.bolt_app as a side effect."""
import json
from datetime import datetime

import signals_config as cfg
import signals_db as db
import signals_compute as compute
import schwab_safety
from signals_blocks import _post_message, _price_input_block, _shares_input_block
from signals_helpers import _existing_position_note, _last_sale_recovery, clear_corp_action_alert
from signals_notify import send_reference_report

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
        suggested_shares  = int(_last_sale_recovery(ticker, data['node'].get('starting_notional')) // signal_price) if signal_price else None
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
        db.clear_pending_buy(ticker)
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
        shares     = int(body['view']['state']['values']['shares_block']['shares_input']['value'])

        opened = db.open_position(node, signal_price, signal_time, fill_price, datetime.now(), shares=shares)
        db.clear_pending_buy(ticker)

        if not opened:
            print(f"  [warn] {ticker} already has an open position — ignored duplicate Filled confirmation")
            client.chat_update(
                channel=channel, ts=ts,
                text=f"{ticker} — ALREADY OPEN, this fill was ignored",
                blocks=[{"type": "section", "text": {"type": "mrkdwn",
                         "text": f"⚠️ *{ticker}* — a position was already open, this Filled confirmation "
                                 f"was *not* recorded (no duplicate created). {_existing_position_note(ticker)}"}}],
            )
            return

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

        opened = db.open_position(node, signal_price, signal_time, exec_price, now, shares=shares)

        ticker = node['ticker']
        db.clear_pending_buy(ticker)

        if not opened:
            print(f"  [warn] {ticker} already has an open position — ignored duplicate Executed confirmation")
            client.chat_update(
                channel=channel, ts=ts,
                text=f"{ticker} — ALREADY OPEN, this fill was ignored",
                blocks=[{"type": "section", "text": {"type": "mrkdwn",
                         "text": f"⚠️ *{ticker}* — a position was already open, this Executed confirmation "
                                 f"was *not* recorded (no duplicate created). {_existing_position_note(ticker)}"}}],
            )
            return

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
        suggested_shares = int(_last_sale_recovery(ticker, data['node'].get('starting_notional')) // current_price) if current_price else None
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

        opened = db.open_position(node, price, now, price, now, shares=shares)
        db.clear_pending_buy(ticker)

        if not opened:
            print(f"  [warn] {ticker} already has an open position — ignored duplicate Manual Open")
            _post_message(f"{ticker} — ALREADY OPEN, this Manual Open was ignored",
                          blocks=[{"type": "section", "text": {"type": "mrkdwn",
                          "text": f"⚠️ *{ticker}* — a position was already open, this Manual Open "
                                  f"was *not* recorded (no duplicate created). {_existing_position_note(ticker)}"}}])
            return

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
