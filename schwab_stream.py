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


def _parse_activity_message(msg: dict):
    """Best-effort extraction of (account, ticker, side, fill_price,
    filled_shares) from an ACCT_ACTIVITY stream message. Returns None if the
    message isn't a recognizable order-fill event -- unrecognized shapes are
    silently skipped, not raised, since the slow poll path is the safety net."""
    try:
        content = msg.get("content", [{}])[0]
        if content.get("2") not in ("OrderFill", "ExecutionActivity"):
            return None
        data = json.loads(content.get("3", "{}"))
        account = data.get("accountNumber")
        legs = data.get("orderLegCollection") or data.get("executionLegs") or []
        if not legs:
            return None
        leg = legs[0]
        ticker = leg.get("instrument", {}).get("symbol") or leg.get("symbol")
        side = leg.get("instruction")
        fill_price = data.get("averageFillPrice") or leg.get("price")
        filled_shares = data.get("filledQuantity") or leg.get("quantity")
        if not (ticker and side and fill_price and filled_shares):
            return None
        return account, ticker, side, float(fill_price), float(filled_shares)
    except Exception:
        return None


def _handle_activity_message(msg: dict):
    event = _parse_activity_message(msg)
    if event is not None:
        FILL_QUEUE.put(event)


async def _run_stream_once():
    client = schwab_auth.get_client()
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
    backoff_idx = 0
    while True:
        try:
            asyncio.run(_run_stream_once())
        except Exception as e:
            delay = _BACKOFF_STEPS[min(backoff_idx, len(_BACKOFF_STEPS) - 1)]
            backoff_idx += 1
            print(f"[schwab_stream] disconnected: {e}\n{traceback.format_exc()}")
            _post_message(f"⚠️ account-activity stream disconnected: {e} — reconnecting in {delay}s")
            time.sleep(delay)
        else:
            backoff_idx = 0
