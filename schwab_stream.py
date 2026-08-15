"""
Account-activity websocket (Part 3, branch C fast path) -- wraps
schwab.streaming.StreamClient to push order-fill events onto a thread-safe
queue as soon as they happen, instead of waiting for the next check_auto_fills
poll (up to POLL_SECS=300s later). Deliberately just a latency improvement:
check_auto_fills keeps running unconditionally as the always-on fallback, so
this module going down (or never coming up) degrades reconciliation speed,
not correctness.

Runs in its own daemon thread (own asyncio loop -- StreamClient is
coroutine-based, active_signals.run_loop's main loop is plain synchronous).
signals_notify.drain_fill_queue() pops events off FILL_QUEUE and calls
_reconcile_fill from the main thread, avoiding any cross-thread sqlite access
(signals_db connections aren't shared across threads).

Field parsing below follows schwab-py's documented ACCT_ACTIVITY message shape
but is unverified against a real fill event -- same caveat as
schwab_client.get_filled_order before its first real (dry_run=False) fill.
Confirm against a real fill before trusting this beyond "worst case, the slow
poll path catches it a few minutes later."
"""
import asyncio
import json
import queue
import time
import traceback

from schwab.streaming import StreamClient

import schwab_auth
from signals_blocks import _post_message

FILL_QUEUE = queue.Queue()

_BACKOFF_STEPS = [5, 10, 20, 40, 60]  # seconds, capped at 60

# Reconnect retries as fast as every 5-60s and never gives up (see
# run_stream_forever docstring) -- without a cooldown, a persistently stale/
# missing token would Slack-alert the real trading channel on that same
# cadence indefinitely, burying real trade alerts (found by Opus review,
# 2026-07-23). Console/log output is unthrottled (cheap, stays local);
# only the Slack post is rate-limited.
_ALERT_COOLDOWN_SECS = 900  # 15 min -- matches active_signals._SECTION_ALERT_COOLDOWN_SECS
_last_alert_at = 0.0


def _parse_activity_message(msg: dict):
    """Extraction of (account, ticker, side, fill_price, filled_shares,
    order_id) for every recognizable order-fill event in an ACCT_ACTIVITY
    stream message. Returns a list (possibly empty) -- a single raw message
    can batch multiple content entries (confirmed live, 2026-08-15: batches
    of 2+ seen routinely), so this must not stop at the first one the way the
    original single-event version did.

    2026-08-15 fix: the original implementation checked
    `content.get("2") in ("OrderFill", "ExecutionActivity")` and parsed
    `content["3"]` as a numeric-key-envelope JSON blob -- a guess at
    schwab-py's documented shape that was NEVER validated against a real
    message (see the module docstring's original caveat) and never actually
    matched one: confirmed via logs/active_signals.log that this condition
    had 0 successful parses across 110 real messages, including 24 genuine
    OrderFillCompleted events. Real messages instead use a
    `MESSAGE_TYPE`/`MESSAGE_DATA` envelope, with the fill itself under
    `MESSAGE_DATA.BaseEvent.OrderFillCompletedEventOrderLegQuantityInfo`.
    Price/quantity fields are fixed-point, `lo` value / 1,000,000 (confirmed
    against multiple real fills: e.g. lo="9181300" -> $9.1813, matching the
    real RETL fill price on file; lo="49000000" -> 49.0 shares, matching the
    real filled quantity on file for that same order).

    order_id lets the caller re-confirm via schwab_client.get_filled_order's
    exact-order lookup instead of its fuzzy ticker+side fallback -- see that
    function's docstring for why the fuzzy fallback is a real hazard
    (2026-07-27 GDXU incident). The value returned here (whether cumulative
    or per-execution) doesn't need to be authoritative -- drain_fill_queue
    only ever uses this as a wake-up signal and re-confirms the real fill via
    a fresh get_filled_order poll, never trusts this value directly."""
    events = []
    health = []  # [(result, ticker), ...] -- logged by the caller, AFTER queueing
    # (2026-08-15 review finding, MEDIUM): a synchronous DB write here, ahead of
    # FILL_QUEUE.put, contends with the poll loop/_position_lock on the fast
    # path's own callback thread -- sqlite's busy-timeout could stall a real fill
    # event by seconds. Health results are collected and returned instead, so
    # _handle_activity_message can queue every event FIRST (latency-critical)
    # and log health SECOND (not).
    for content in msg.get("content", []):
        # 'received' logged for every content entry regardless of type (2026-08-15
        # review finding, HIGH): the original version of this metric only counted
        # entries already recognized as MESSAGE_TYPE=='OrderFillCompleted', which
        # means a shape-drift bug (tonight's actual original bug) would produce
        # ZERO events either way and the metric would report "no messages seen,
        # not an error" -- the reassuring reading, in exactly the scenario that
        # motivated building it. Logging 'received' unconditionally lets
        # evening_status.py tell apart: 0 messages at all (stream dead) vs. N
        # messages, 0 ever recognized as a fill (real shape drift -- alarm) vs.
        # N messages, M genuinely parsed (healthy).
        health.append(('received', None))
        if content.get("MESSAGE_TYPE") != "OrderFillCompleted":
            continue
        try:
            data = json.loads(content.get("MESSAGE_DATA", "{}"))
            fill_info = data.get("BaseEvent", {}).get("OrderFillCompletedEventOrderLegQuantityInfo", {})
            exec_info = fill_info.get("ExecutionInfo", {})
            order_info = fill_info.get("OrderInfoForTransactionPosting", {})
            account = data.get("AccountNumber")
            order_id = data.get("SchwabOrderID")
            ticker = order_info.get("Symbol")
            side = (order_info.get("BuySellCode") or "").upper()
            price_field = exec_info.get("ExecutionPrice", {})
            qty_field = exec_info.get("ExecutionQuantity", {})
            price_raw, price_scale = price_field.get("lo"), price_field.get("signScale")
            qty_raw, qty_scale = qty_field.get("lo"), qty_field.get("signScale")
            if not (account and order_id and ticker and side and price_raw and qty_raw
                    and price_scale is not None and qty_scale is not None):
                health.append(('missing_field', None))
                continue
            # .NET decimal serialization: signScale = scale*2 + sign_bit -- divisor
            # is 10**scale regardless of sign (2026-08-15 review finding: real
            # traffic shows BOTH signScale=12 [scale 6, positive] and signScale=13
            # [scale 6, negative, e.g. debit amounts] -- the prior hardcoded /1e6
            # was only correct by coincidence of every observed fill using scale 6,
            # not a validated invariant).
            fill_price = float(price_raw) / (10 ** (int(price_scale) >> 1))
            filled_shares = float(qty_raw) / (10 ** (int(qty_scale) >> 1))
            events.append((account, ticker, side, fill_price, filled_shares, order_id))
            health.append(('parsed', ticker))
        except Exception:
            health.append(('exception', None))
            continue
    return events, health


def _log_parse_health(health, source='daemon'):
    """Fire-and-forget coverage_event logging for the stream parse-success
    metric -- imported lazily to avoid schwab_stream (a low-level,
    early-imported module) taking a hard dependency on signals_db at module
    load time. Called AFTER FILL_QUEUE.put, not before -- see
    _parse_activity_message's docstring for why.
    source: coverage_events write-attribution (signals_db.COVERAGE_EVENT_
    SOURCES) -- defaults to 'daemon' since the real websocket connection only
    exists in the live daemon process; a fixture driving _handle_activity_
    message directly (e.g. the fake-venue harness) passes its own value."""
    if not health:
        return
    try:
        import signals_db
        for result, ticker in health:
            signals_db.log_coverage_event("stream_message_parsed", "live", ticker=ticker, result=result, source=source)
    except Exception:
        pass


def _handle_activity_message(msg: dict, source='daemon'):
    # Raw-message logging (2026-08-02, kept post-fix): still cheap, local-only
    # insurance against the next real shape drift going unnoticed the same
    # way this one did for 13 days.
    print(f"[schwab_stream] raw ACCT_ACTIVITY message: {msg}")
    events, health = _parse_activity_message(msg)
    if events:
        for event in events:
            print(f"[schwab_stream] parsed fill event: {event}")
            FILL_QUEUE.put(event)
    else:
        print("[schwab_stream] message did not parse as a recognizable order-fill event")
    _log_parse_health(health, source=source)


async def _run_stream_once():
    client = schwab_auth.get_client(interactive=False)
    stream = StreamClient(client)
    stream.add_account_activity_handler(_handle_activity_message)
    await stream.login()
    await stream.account_activity_sub()
    while True:
        await stream.handle_message()


def run_stream_forever():
    """Daemon-thread entry point -- started once from active_signals.run_loop
    startup. Reconnects with capped exponential backoff on any disconnect or
    exception; never gives up (a silent permanent stop would be worse than a
    noisy reconnect loop -- the Slack warning already surfaces the degraded
    state)."""
    global _last_alert_at
    backoff_idx = 0
    while True:
        try:
            asyncio.run(_run_stream_once())
        except Exception as e:
            delay = _BACKOFF_STEPS[min(backoff_idx, len(_BACKOFF_STEPS) - 1)]
            backoff_idx += 1
            print(f"[schwab_stream] disconnected: {e}\n{traceback.format_exc()}")
            now = time.time()
            if now - _last_alert_at > _ALERT_COOLDOWN_SECS:
                _last_alert_at = now
                _post_message(f"⚠️ account-activity stream disconnected: {e} — reconnecting in {delay}s")
            time.sleep(delay)
        else:
            backoff_idx = 0
