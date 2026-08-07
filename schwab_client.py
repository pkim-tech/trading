"""
Thin wrapper over the schwab-py client: account-hash resolution by nickname
and order placement gated through schwab_safety.approve_and_record(). This
is the only module that should call schwab_auth.get_client() or touch
schwab.orders directly -- active_signals.py places orders through here, never
around it.

Account nicknames (brokerage/sep/roth/ira) map to real account numbers via
env vars (SCHWAB_ACCOUNT_BROKERAGE, etc.) -- never hardcode account numbers
in source.
"""
import os
import time

import schwab.client
import schwab.orders.equities as equity_orders
from schwab.orders.generic import OrderBuilder
from schwab.orders.common import (
    OrderType, Session, Duration, OrderStrategyType,
    StopPriceLinkBasis, StopPriceLinkType, EquityInstruction,
)
from schwab.utils import Utils

import schwab_auth
import schwab_safety
from signals_blocks import _post_message

_client = None
_account_hashes = None  # nickname -> Schwab's encrypted account hash, resolved lazily

NICKNAMES = ["brokerage", "sep", "roth", "ira", "soxl_ira"]


# schwab-py defaults to a 30s httpx timeout on every call. This daemon is
# single-threaded and several calls (get_account_balance, the order-book
# fetch) run inside schwab_safety's cross-account file lock, so a slow/hung
# call stalls order processing for every account, not just the one being
# checked (found in Opus review, 2026-07-21). A short, cheap global timeout
# bounds that stall without restructuring lock ordering -- proportionate per
# automation_principles.md #7a.
_CLIENT_TIMEOUT_SECS = 10.0


def _get_client(interactive: bool = False):
    global _client
    if _client is None:
        _client = schwab_auth.get_client(interactive=interactive)
        _client.set_timeout(_CLIENT_TIMEOUT_SECS)
    return _client


# Retry only the actual broker submission call, not schwab_safety.approve_and_record()
# -- that must run exactly once per attempt (it increments the daily/burst-cap
# counters and records the duplicate-order fingerprint), so retrying it would
# make one real order attempt look like several against those caps. A generic
# exception here (timeout, connection error, a transient 5xx) is worth retrying
# since the same call may simply succeed a moment later; a SafetyViolation
# already happened earlier, before this helper is ever reached, so it's never
# what's being retried.
_ORDER_SUBMIT_RETRY_ATTEMPTS = 3
_ORDER_SUBMIT_RETRY_INTERVAL_SECS = 2


def _submit_order_with_retry(account_hash, order):
    last_exc = None
    for attempt in range(_ORDER_SUBMIT_RETRY_ATTEMPTS):
        try:
            r = _get_client().place_order(account_hash, order)
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            if attempt < _ORDER_SUBMIT_RETRY_ATTEMPTS - 1:
                time.sleep(_ORDER_SUBMIT_RETRY_INTERVAL_SECS)
    raise last_exc


def _submit_replace_with_retry(account_hash, order_id, order):
    """Same retry shape as _submit_order_with_retry, for schwab-py's
    replace_order (cancel-old + create-new as a single broker call).

    Known residual risk, accepted 2026-07-27 (see docs/backlog_cache.md):
    replace_order targets one specific order_id, unlike a fresh placement --
    if attempt 1's request actually lands at the broker (old order canceled,
    new one created) but the client-side response handling then raises
    (timeout, malformed response after a real success), a retry fires a
    SECOND replace_order against an order_id that's already dead. That call
    fails cleanly, so the caller's final exception looks identical to
    "nothing happened at all," when a real, untracked new order may already
    be resting. Every caller's UNPROTECTED/manual-fallback messaging
    currently assumes this ambiguity away. Not fixed here (Sonnet review
    found it, user's call to accept and backlog rather than fix same
    session) -- a real fix would check the target order_id's live status
    before retrying rather than retrying blind."""
    last_exc = None
    for attempt in range(_ORDER_SUBMIT_RETRY_ATTEMPTS):
        try:
            r = _get_client().replace_order(account_hash, order_id, order)
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            if attempt < _ORDER_SUBMIT_RETRY_ATTEMPTS - 1:
                time.sleep(_ORDER_SUBMIT_RETRY_INTERVAL_SECS)
    raise last_exc


# Order placement/cancellation are asynchronous -- the initial HTTP response only
# means "received," not the final verdict. Confirmed live 2026-07-24: an oversized
# BUY returned HTTP 201 (no exception) but resolved REJECTED ~0.3-0.7s later; a
# cancel_order returned HTTP 200 before the order was actually CANCELED. Without
# this poll, a real rejection looked identical to a real success in Slack. 4
# attempts / 0.5s apart bounds the wait comfortably above the observed worst case
# while still posting the Slack alert within ~1-2s of placement.
_ORDER_CONFIRM_POLL_ATTEMPTS = 4
_ORDER_CONFIRM_POLL_INTERVAL_SECS = 0.5
_ORDER_TERMINAL_BAD_STATUSES = {"REJECTED", "CANCELED", "EXPIRED"}


class OrderRejected(Exception):
    """Raised when the post-placement status poll confirms an order was
    REJECTED/CANCELED/EXPIRED rather than resting. Lets callers (all of which
    already catch schwab_safety.SafetyViolation and fall back to the manual
    flow) treat a dead-on-arrival order the same way as a blocked one --
    without this, a confirmed rejection still returned a real order_id with no
    exception, so callers set auto_placed=True and marked a pending_buys row
    as order_placed, which then nags the fill reminder forever for an order
    that will never fill, and can seed a real check_gap_resize replacement
    order off the phantom pending row the next morning (found via Opus review,
    2026-07-24, ahead of the first real dry_run=False day)."""


def _confirm_order_status(account_hash, order_id):
    """Best-effort poll of the real order status right after placement/cancel.
    Returns the status string (e.g. 'FILLED', 'REJECTED', 'AWAITING_STOP_CONDITION'),
    or None if every attempt's poll fails (network error, etc.) -- callers must
    treat None as 'unconfirmed', not as any particular status, and fall back to
    the optimistic message rather than block on this. A transient failure on
    one attempt retries the remaining attempts rather than giving up
    immediately (fixed 2026-07-24 Opus review -- returning on the first error
    silently disabled rejection detection during exactly the window right
    after placement where get_order-by-id is flakiest). Stops early once a
    terminal status (good or bad) is observed; otherwise returns whatever the
    last successful poll showed after exhausting all attempts."""
    last_status = None
    for attempt in range(_ORDER_CONFIRM_POLL_ATTEMPTS):
        try:
            r = _get_client().get_order(order_id, account_hash)
            r.raise_for_status()
            last_status = r.json().get("status")
        except Exception:
            if attempt < _ORDER_CONFIRM_POLL_ATTEMPTS - 1:
                time.sleep(_ORDER_CONFIRM_POLL_INTERVAL_SECS)
            continue
        if last_status in _ORDER_TERMINAL_BAD_STATUSES or last_status == "FILLED":
            return last_status
        if attempt < _ORDER_CONFIRM_POLL_ATTEMPTS - 1:
            time.sleep(_ORDER_CONFIRM_POLL_INTERVAL_SECS)
    return last_status


def get_order_status(account, order_id):
    """Single, non-retrying status check for a real order well after
    placement -- deliberately NOT _confirm_order_status (that function's
    4x0.5s retry loop exists to catch a status that hasn't settled yet right
    after submission; here the order may have been resting for minutes or
    hours, so a stale non-terminal read isn't going to change on a quick
    retry, and this runs inside the main poll loop across many positions
    where burning ~1.5s per check adds up). Returns the status string, or
    None if the single call fails (network error, etc.) -- treat None as
    'unconfirmed,' not as any particular status; callers should fail toward
    the cautious/manual path on None, not the reassuring one (found
    2026-08-01: the TP/TRAIL automated-exit alert used to trust a stored
    order_id's mere presence as proof an order was still resting, which is
    also true of a REJECTED/CANCELED order -- this closes that gap by
    actually checking)."""
    try:
        account_hash = _resolve_account_hashes()[account]
        r = _get_client().get_order(order_id, account_hash)
        r.raise_for_status()
        return r.json().get("status")
    except Exception:
        return None


def _post_order_confirmation(label, account_hash, order_id, ticker, account, submitted_msg):
    """Shared by every real placement call site: polls the real status and posts
    an accurate Slack message instead of always claiming success. A confirmed
    REJECTED/CANCELED/EXPIRED gets a distinct 🚫 alert and raises OrderRejected
    so the caller falls back to the manual flow instead of treating this as a
    successful placement; FILLED gets an immediate fill confirmation; anything
    else (still resting, or unconfirmed) falls back to the existing optimistic
    'submitted' message, which remains accurate for a genuinely-resting order."""
    status = _confirm_order_status(account_hash, order_id)
    if status in _ORDER_TERMINAL_BAD_STATUSES:
        _post_message(f"\U0001F6AB {label} {ticker} in {account} was {status} by Schwab "
                       f"(order {order_id}) — not resting, no position/order resulted")
        raise OrderRejected(f"{label} {ticker} order {order_id} was {status}")
    elif status == "FILLED":
        _post_message(f"✅ {label} {ticker} in {account} FILLED immediately (order {order_id})")
    else:
        _post_message(submitted_msg)


def _resolve_account_hashes() -> dict:
    global _account_hashes
    if _account_hashes is not None:
        return _account_hashes

    # env vars hold only an account-number suffix (e.g. last 3-4 digits, as
    # shown in Schwab's own masked UI) -- the full number never needs to be
    # typed/stored, just enough digits to be unambiguous among linked accounts.
    suffixes = {n: os.environ.get(f"SCHWAB_ACCOUNT_{n.upper()}") for n in NICKNAMES}
    r = _get_client().get_account_numbers()
    r.raise_for_status()
    accounts = r.json()

    _account_hashes = {}
    for nickname, suffix in suffixes.items():
        if not suffix:
            continue
        matches = [a for a in accounts if a["accountNumber"].endswith(suffix)]
        if len(matches) > 1:
            raise ValueError(
                f"SCHWAB_ACCOUNT_{nickname.upper()}='{suffix}' matches {len(matches)} linked "
                f"accounts -- use more digits to disambiguate"
            )
        if matches:
            _account_hashes[nickname] = matches[0]["hashValue"]
    return _account_hashes


def _build_market_order(side: str, ticker: str, quantity: int):
    order_fn = equity_orders.equity_buy_market if side == "BUY" else equity_orders.equity_sell_market
    return order_fn(ticker, quantity)


def _place_equity_order(
    side: str, account: str, ticker: str, quantity: int, price: float, is_gap_correction: bool = False,
    is_protective: bool = False, is_addon_leg: bool = False, node_dry_run: bool = False,
):
    """side is 'BUY' or 'SELL'. price is only used for the safety-cap notional
    check, not sent to the API -- this places a market order. Returns
    (response, order_id); dry_run returns (None, None).
    is_addon_leg threads through to schwab_safety.check_order's is_addon_leg
    exemption (docs/plans/real_order_execution_drought_addon.md 3.1) and tags
    the Slack confirmation so a real add-on fill is distinguishable from the
    core position's own order at a glance.
    node_dry_run: per-node dry_run override (additive with the account-level
    flag, see schwab_safety.approve_and_record's docstring)."""
    try:
        dry_run = schwab_safety.approve_and_record(
            account, ticker, quantity, price, side, is_gap_correction=is_gap_correction,
            is_protective=is_protective, is_addon_leg=is_addon_leg, node_dry_run=node_dry_run)
    except schwab_safety.SafetyViolation as e:
        _post_message(f"\U0001F6AB BLOCKED {side} {quantity} {ticker} in {account}: {e}")
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True)
        raise

    _label = "ADD-ON " if is_addon_leg else ""
    if dry_run:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False)
        _post_message(f"[DRY RUN] would {_label}{side} {quantity} {ticker} in {account} (~${quantity * price:,.0f})")
        print(f"[DRY RUN] would {_label}{side} {quantity} {ticker} in {account} (~${quantity * price:,.0f})")
        return None, None

    account_hash = _resolve_account_hashes()[account]
    order = _build_market_order(side, ticker, quantity)
    try:
        r = _submit_order_with_retry(account_hash, order)
        order_id = Utils(_get_client(), account_hash).extract_order_id(r)
        _post_order_confirmation(
            side, account_hash, order_id, ticker, account,
            f"✅ {_label}{side} {quantity} {ticker} in {account} submitted to Schwab (~${quantity * price:,.0f})")
    except Exception:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True)
        raise
    schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False)
    return r, order_id


def replace_equity_order_with_market(
    account: str, ticker: str, order_id: int, side: str, quantity: int, price: float,
    is_gap_correction: bool = False, is_protective: bool = False, is_addon_leg: bool = False,
    node_dry_run: bool = False,
):
    """Atomically replaces a resting order (e.g. a protective STOP) with a
    plain MARKET order of the given side/quantity, via schwab-py's
    replace_order -- a single broker call (cancel-old + create-new) instead
    of two independent calls (cancel_order then place_equity_buy/sell).

    Closes a real failure window the two-call version has: a confirmed
    cancel followed by a failed/blocked new placement leaves nothing
    resting at the broker in between, genuinely unprotected/unmanaged --
    the existing manual_sl_fallback_alert path exists specifically to catch
    this after the fact, but a single atomic call removes the gap instead
    (found 2026-07-27, raised directly by the user while reviewing the
    SH TIME-exit and check_gap_resize cancel+place patterns).

    Used by check_gap_resize (BUY side -- swaps an overnight-gapped
    trailing-buy for a market buy) and _attempt_automated_exit_sell (SELL
    side -- swaps a resting protective SL for a market sell exit on a
    TP/SL/TIME signal). Returns (response, new_order_id); dry_run returns
    (None, None) and leaves the existing resting order untouched."""
    try:
        dry_run = schwab_safety.approve_and_record(
            account, ticker, quantity, price, side, is_gap_correction=is_gap_correction,
            is_protective=is_protective, replacing_order_id=order_id, is_addon_leg=is_addon_leg,
            node_dry_run=node_dry_run)
    except schwab_safety.SafetyViolation as e:
        _post_message(f"\U0001F6AB BLOCKED replace {order_id} with MARKET {side} {quantity} {ticker} in {account}: {e}")
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True)
        raise

    if dry_run:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False)
        msg = (f"[DRY RUN] would replace order {order_id} with MARKET {side} {quantity} {ticker} "
               f"in {account} (~${quantity * price:,.0f})")
        _post_message(msg)
        print(msg)
        return None, None

    account_hash = _resolve_account_hashes()[account]
    order = _build_market_order(side, ticker, quantity)
    try:
        r = _submit_replace_with_retry(account_hash, order_id, order)
        new_order_id = Utils(_get_client(), account_hash).extract_order_id(r)
        _post_order_confirmation(
            f"REPLACE->MARKET {side}", account_hash, new_order_id, ticker, account,
            f"✅ Replaced order {order_id} with MARKET {side} {quantity} {ticker} in {account} "
            f"(~${quantity * price:,.0f})")
    except Exception:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True)
        raise
    schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False)
    return r, new_order_id


def place_equity_buy(account: str, ticker: str, quantity: int, price: float, is_gap_correction: bool = False,
                      is_protective: bool = False, is_addon_leg: bool = False, node_dry_run: bool = False):
    return _place_equity_order("BUY", account, ticker, quantity, price, is_gap_correction=is_gap_correction,
                                is_protective=is_protective, is_addon_leg=is_addon_leg, node_dry_run=node_dry_run)


def place_equity_sell(account: str, ticker: str, quantity: int, price: float, is_addon_leg: bool = False,
                       node_dry_run: bool = False):
    return _place_equity_order("SELL", account, ticker, quantity, price, is_addon_leg=is_addon_leg,
                                node_dry_run=node_dry_run)


def _build_trailing_order(side: str, link_basis: StopPriceLinkBasis, ticker: str, quantity: int, trail_pct: float):
    order = OrderBuilder()
    order.set_order_type(OrderType.TRAILING_STOP)
    order.set_session(Session.NORMAL)
    order.set_duration(Duration.GOOD_TILL_CANCEL)
    order.set_order_strategy_type(OrderStrategyType.SINGLE)
    order.set_stop_price_link_basis(link_basis)
    order.set_stop_price_link_type(StopPriceLinkType.PERCENT)
    order.set_stop_price_offset(trail_pct)
    order.add_equity_leg(
        EquityInstruction.BUY if side == "BUY" else EquityInstruction.SELL, ticker, quantity
    )
    return order


def _place_trailing_order(
    side: str, link_basis: StopPriceLinkBasis, account: str, ticker: str,
    quantity: int, price: float, trail_pct: float, node_dry_run: bool = False,
):
    """side is 'BUY' or 'SELL'. price is the current live price, used only for
    the safety-cap notional check (quantity * price), not sent to the API.
    Orders are GOOD_TILL_CANCEL, matching the manual workflow's existing
    trailing-order convention (docs/CLAUDE.md's TrailingBothZScoreBreakout
    execution notes). Schwab tracks the running high/low and fires the order
    itself; this module never polls for the bounce/pullback.
    node_dry_run: per-node dry_run override (see schwab_safety.approve_and_record)."""
    label = "TRAILING BUY" if side == "BUY" else "TRAILING SELL"
    try:
        dry_run = schwab_safety.approve_and_record(account, ticker, quantity, price, side,
                                                     node_dry_run=node_dry_run)
    except schwab_safety.SafetyViolation as e:
        _post_message(f"\U0001F6AB BLOCKED {label} {quantity} {ticker} in {account} "
                      f"(trail={trail_pct}%): {e}")
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True)
        raise

    if dry_run:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False)
        msg = (f"[DRY RUN] would place {label} {quantity} {ticker} in {account} "
               f"(trail={trail_pct}%, ~${quantity * price:,.0f})")
        _post_message(msg)
        print(msg)
        return None, None

    account_hash = _resolve_account_hashes()[account]
    order = _build_trailing_order(side, link_basis, ticker, quantity, trail_pct)
    try:
        r = _submit_order_with_retry(account_hash, order)
        order_id = Utils(_get_client(), account_hash).extract_order_id(r)
        _post_order_confirmation(
            label, account_hash, order_id, ticker, account,
            f"✅ {label} {quantity} {ticker} in {account} submitted to Schwab "
            f"(trail={trail_pct}%, ~${quantity * price:,.0f})")
    except Exception:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True)
        raise
    schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False)
    return r, order_id


def replace_order_with_trailing_sell(account: str, ticker: str, order_id: int, quantity: int, price: float,
                                      trail_pct: float, node_dry_run: bool = False):
    """Same atomic-replace idea as replace_equity_order_with_market, for the
    TRAIL arm-time swap: a resting protective SL becomes a TRAILING_STOP
    SELL, as a single broker call instead of cancel_order + place_trailing_sell.
    Used by _attempt_automated_sell. Returns (response, new_order_id);
    dry_run returns (None, None) and leaves the existing resting order
    untouched."""
    try:
        dry_run = schwab_safety.approve_and_record(account, ticker, quantity, price, "SELL",
                                                     replacing_order_id=order_id, node_dry_run=node_dry_run)
    except schwab_safety.SafetyViolation as e:
        _post_message(f"\U0001F6AB BLOCKED replace {order_id} with TRAILING SELL {quantity} {ticker} "
                      f"in {account} (trail={trail_pct}%): {e}")
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True)
        raise

    if dry_run:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False)
        msg = (f"[DRY RUN] would replace order {order_id} with TRAILING SELL {quantity} {ticker} "
               f"in {account} (trail={trail_pct}%, ~${quantity * price:,.0f})")
        _post_message(msg)
        print(msg)
        return None, None

    account_hash = _resolve_account_hashes()[account]
    order = _build_trailing_order("SELL", StopPriceLinkBasis.BID, ticker, quantity, trail_pct)
    try:
        r = _submit_replace_with_retry(account_hash, order_id, order)
        new_order_id = Utils(_get_client(), account_hash).extract_order_id(r)
        _post_order_confirmation(
            "REPLACE->TRAILING SELL", account_hash, new_order_id, ticker, account,
            f"✅ Replaced order {order_id} with TRAILING SELL {quantity} {ticker} in {account} "
            f"(trail={trail_pct}%, ~${quantity * price:,.0f})")
    except Exception:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True)
        raise
    schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False)
    return r, new_order_id


def place_trailing_buy(account: str, ticker: str, quantity: int, price: float, trail_pct: float,
                        node_dry_run: bool = False):
    """trail_pct is the bounce-above-running-low trigger (matches the node's
    trail_buy_pct). ASK-linked, since a buy naturally references the ask."""
    return _place_trailing_order("BUY", StopPriceLinkBasis.ASK, account, ticker, quantity, price, trail_pct,
                                  node_dry_run=node_dry_run)


def _order_fill(o):
    """Extracts {'price', 'quantity'} from a single order dict if it's FILLED
    with at least one execution leg, else None."""
    if o.get("status") != "FILLED":
        return None
    exec_legs = [
        el for activity in o.get("orderActivityCollection", [])
        for el in activity.get("executionLegs", [])
    ]
    if not exec_legs:
        return None
    total_qty = sum(el.get("quantity", 0) for el in exec_legs)
    if not total_qty:
        return None
    vwap = sum(el.get("price", 0) * el.get("quantity", 0) for el in exec_legs) / total_qty
    return {"price": vwap, "quantity": total_qty}


def get_filled_order(account: str, ticker: str, side: str, order_id: int = None):
    """Poll of Schwab's live order book for a FILLED order matching ticker+side.

    When order_id is given (every call site that placed the order itself
    should pass it), looks up that EXACT order only -- returns its fill if
    FILLED, else None. This is the only safe mode: a market order placed
    pre-market (e.g. check_gap_resize's 9:15-9:29 ET window) won't actually
    fill until the 9:30 open, and during that gap this must return None, not
    substitute a different, older FILLED order for the same ticker+side.

    order_id=None falls back to the old best-effort "most recent FILLED
    order for this ticker+side" heuristic, kept only for call sites with no
    specific order to check (e.g. an unattributed stream event). This mode
    is a real, known hazard -- a stale unrelated fill (a prior trade, days
    old) can be returned as if it were the order just placed, since nothing
    here scopes by date or ties the result to a specific placement. Found
    2026-07-27: this exact hazard corrupted a real GDXU reconciliation
    (matched a 2026-07-24 closed trade instead of recognizing the real
    replacement order hadn't filled yet), leaving the position's real
    stop-loss unplaced. Always prefer passing order_id.

    Field names (orderActivityCollection/executionLegs) follow Schwab's
    documented order schema. Returns {'price': float, 'quantity': float} or
    None if no matching fill is found."""
    account_hash = _resolve_account_hashes()[account]
    r = _get_client().get_orders_for_account(account_hash)
    r.raise_for_status()
    orders = r.json()

    if order_id is not None:
        for o in orders:
            if o.get("orderId") == order_id:
                return _order_fill(o)
        return None

    instruction = EquityInstruction.BUY if side == "BUY" else EquityInstruction.SELL
    candidates = []
    for o in orders:
        fill = _order_fill(o)
        if fill is None:
            continue
        legs = o.get("orderLegCollection", [])
        matches = any(
            leg.get("instrument", {}).get("symbol") == ticker
            and leg.get("instruction") == instruction.value
            for leg in legs
        )
        if not matches:
            continue
        candidates.append((o.get("closeTime") or o.get("enteredTime") or "", fill))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return candidates[-1][1]


def place_trailing_sell(account: str, ticker: str, quantity: int, price: float, trail_pct: float,
                         node_dry_run: bool = False):
    """trail_pct is the pullback-below-running-high trigger (matches the
    position's trail_sell_pct). BID-linked, since a sell naturally references
    the bid. Only relevant once the position's trailing-exit state has
    activated (strategies.TrailingBothZScoreBreakout.check_exit's
    state['trailing'] -- see signals_notify.notify_trailing_activated), same
    as the manual workflow's 'place the trailing stop order now' step."""
    return _place_trailing_order("SELL", StopPriceLinkBasis.BID, account, ticker, quantity, price, trail_pct,
                                  node_dry_run=node_dry_run)


def cancel_order(account: str, ticker: str, order_id: int):
    """Cancels a still-resting order with no replacement. check_gap_resize,
    _attempt_automated_sell, and _attempt_automated_exit_sell were all
    migrated 2026-07-27 to replace_equity_order_with_market/
    replace_order_with_trailing_sell (a single atomic broker call instead of
    a separate cancel + place), so none of those call this. The genuine
    cancel-with-no-replacement case now exists: signals_notify.
    check_entry_abandon (2026-07-31) calls this directly -- a trailing-buy
    that never bounces is abandoned outright, not replaced with anything.
    Also used for manual/REPL use. No approve_and_record gate -- this isn't a
    new placement, just withdrawing one already approved. No
    record_node_streak wiring either (unlike the six real placement
    functions) -- a failed cancel isn't a failed placement, so it
    deliberately doesn't feed the node circuit breaker's order_failures
    streak.

    Returns (response, confirmed_status) -- confirmed_status is 'CANCELED' only
    if the post-cancel poll actually confirmed it; None means unconfirmed
    (poll failed), anything else (e.g. 'FILLED') means the cancel didn't take
    effect the way the caller assumed. Callers MUST check this before treating
    the original order as gone -- a cancel HTTP 200 doesn't mean cancelled
    (confirmed live 2026-07-23 night), and proceeding as if it did risks a
    double-order (gap-resize placing a replacement while the original still
    fills) or an oversell (a new trailing-sell placed after the old SL already
    filled the shares) -- found via Opus review, 2026-07-24 session wrap."""
    account_hash = _resolve_account_hashes()[account]
    r = _get_client().cancel_order(order_id, account_hash)
    r.raise_for_status()
    status = _confirm_order_status(account_hash, order_id)
    if status == "CANCELED":
        _post_message(f"\U0001F5D1️ confirmed cancelled resting order {order_id} ({ticker} in {account})")
    elif status is None:
        _post_message(f"\U0001F5D1️ cancel request accepted for order {order_id} ({ticker} in {account}) "
                       f"— status unconfirmed (poll failed)")
    else:
        _post_message(f"⚠️ cancel request accepted for order {order_id} ({ticker} in {account}) but real "
                       f"status is '{status}', not CANCELED — may still be resting or already resolved")
    return r, status


_OPEN_PRICE_RETRY_ATTEMPTS = 3
_OPEN_PRICE_RETRY_INTERVAL_SECS = 2


def get_session_open_price(ticker: str) -> tuple[float, bool]:
    """Reads quote.openPrice (confirmed present in a real Schwab get_quote
    response, 2026-07-21) -- the session's fixed opening print, matching the
    backtest kernel's literal bar Open exactly, unlike a live tick sampled
    seconds later. openPrice is 0.0 until the session's open print lands, not
    an error -- retries briefly, then falls back to get_current_price()
    (lastPrice). Returns (price, is_true_open) so callers can distinguish and
    log which path fired."""
    for attempt in range(_OPEN_PRICE_RETRY_ATTEMPTS):
        try:
            r = _get_client().get_quote(ticker)
            r.raise_for_status()
            open_price = r.json()[ticker]["quote"]["openPrice"]
            if open_price:
                return float(open_price), True
        except Exception:
            pass
        if attempt < _OPEN_PRICE_RETRY_ATTEMPTS - 1:
            time.sleep(_OPEN_PRICE_RETRY_INTERVAL_SECS)
    return get_current_price(ticker), False


def place_stop_loss(account: str, ticker: str, quantity: int, stop_price: float, is_addon_leg: bool = False,
                     node_dry_run: bool = False):
    """Resting fixed-price STOP order -- the broker executes on breach without
    depending on our poll cadence (Part 4, Section 6), same mechanism already
    relied on for the trailing-sell order. Same OrderBuilder pattern as
    _place_trailing_order, just OrderType.STOP + set_stop_price instead of
    TRAILING_STOP + link-basis/offset. Returns (response, order_id); dry_run
    returns (None, None).
    is_addon_leg (D3, docs/plans/real_order_execution_drought_addon.md 6.2):
    threads through to schwab_safety.check_order's matching SELL-side
    exemption -- the leg's own stop is meant to rest ALONGSIDE the parent
    core position's own resting protective order, which the ordinary
    resting-SELL duplicate guard would otherwise refuse."""
    try:
        dry_run = schwab_safety.approve_and_record(
            account, ticker, quantity, stop_price, "SELL", is_protective=True, is_addon_leg=is_addon_leg,
            node_dry_run=node_dry_run)
    except schwab_safety.SafetyViolation as e:
        _post_message(f"\U0001F6AB BLOCKED STOP LOSS {quantity} {ticker} in {account} @ ${stop_price:.4f}: {e}")
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True)
        raise

    if dry_run:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False)
        msg = f"[DRY RUN] would place STOP LOSS {quantity} {ticker} in {account} @ ${stop_price:.4f}"
        _post_message(msg)
        print(msg)
        return None, None

    account_hash = _resolve_account_hashes()[account]
    order = OrderBuilder()
    order.set_order_type(OrderType.STOP)
    order.set_session(Session.NORMAL)
    order.set_duration(Duration.GOOD_TILL_CANCEL)
    order.set_order_strategy_type(OrderStrategyType.SINGLE)
    # Passed as a string, not a float -- schwab-py deprecation warning (found
    # 2026-07-24, a real STOP order placement): float truncation is deprecated
    # and will be removed. See :ref:`number_truncation` in schwab-py's docs.
    order.set_stop_price(f"{stop_price:.2f}")
    order.add_equity_leg(EquityInstruction.SELL, ticker, quantity)

    try:
        r = _submit_order_with_retry(account_hash, order)
        order_id = Utils(_get_client(), account_hash).extract_order_id(r)
        _post_order_confirmation(
            "STOP LOSS", account_hash, order_id, ticker, account,
            f"✅ STOP LOSS {quantity} {ticker} in {account} submitted to Schwab @ ${stop_price:.2f}")
    except Exception:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True)
        raise
    schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False)
    return r, order_id


def replace_order_with_stop_loss(account: str, ticker: str, order_id: int, quantity: int, stop_price: float,
                                  node_dry_run: bool = False):
    """Atomically replaces a resting order with a new fixed-price STOP order
    (a re-priced SL) -- same atomic-replace rationale as
    replace_equity_order_with_market/replace_order_with_trailing_sell: a
    separate cancel_order + place_stop_loss two-step would leave nothing
    resting at the broker in the window between a confirmed cancel and a
    failed/blocked new placement. Returns (response, new_order_id); dry_run
    returns (None, None) and leaves the existing resting order untouched.

    Not currently wired into any automated call site -- added 2026-07-29 as
    reusable infra for manually re-pricing a resting SL (used directly, once,
    to fix RETL's stop from a stale-signal_price-anchored price to a correct
    one). Intentional, not dead code; a future automated re-pricing path
    could call this the same way check_gap_resize/notify_trailing_activated
    call their siblings."""
    try:
        dry_run = schwab_safety.approve_and_record(
            account, ticker, quantity, stop_price, "SELL", is_protective=True, replacing_order_id=order_id,
            node_dry_run=node_dry_run)
    except schwab_safety.SafetyViolation as e:
        _post_message(f"\U0001F6AB BLOCKED replace {order_id} with STOP LOSS {quantity} {ticker} "
                      f"in {account} @ ${stop_price:.4f}: {e}")
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True)
        raise

    if dry_run:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False)
        msg = (f"[DRY RUN] would replace order {order_id} with STOP LOSS {quantity} {ticker} "
               f"in {account} @ ${stop_price:.4f}")
        _post_message(msg)
        print(msg)
        return None, None

    account_hash = _resolve_account_hashes()[account]
    order = OrderBuilder()
    order.set_order_type(OrderType.STOP)
    order.set_session(Session.NORMAL)
    order.set_duration(Duration.GOOD_TILL_CANCEL)
    order.set_order_strategy_type(OrderStrategyType.SINGLE)
    order.set_stop_price(f"{stop_price:.2f}")
    order.add_equity_leg(EquityInstruction.SELL, ticker, quantity)

    try:
        r = _submit_replace_with_retry(account_hash, order_id, order)
        new_order_id = Utils(_get_client(), account_hash).extract_order_id(r)
        _post_order_confirmation(
            "REPLACE->STOP LOSS", account_hash, new_order_id, ticker, account,
            f"✅ Replaced order {order_id} with STOP LOSS {quantity} {ticker} in {account} @ ${stop_price:.2f}")
    except Exception:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True)
        raise
    schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False)
    return r, new_order_id


def get_account_balance(account: str) -> float:
    """Real available cash for the account, read fresh (never cached) --
    Client.get_account, securitiesAccount.currentBalances. Confirmed 2026-07-23
    against two real margin-type accounts: 'cashAvailableForTrading' (the
    original field name, from Schwab's documented schema but never checked
    against a real response until then) doesn't exist on either -- Schwab
    returns 'availableFunds' instead for a MARGIN account. Falls back to
    'availableFunds' when the first key is missing rather than assuming one or
    the other; cash-type accounts (sep/roth) haven't been confirmed to return
    'cashAvailableForTrading' for real either, but no real response has
    disproven it yet, so it stays the first choice. Raises on any failure
    (network, both fields missing, etc.) rather than returning a fallback
    value -- the caller (schwab_safety.check_order) must fail closed on a
    balance-check failure, not silently allow the order through with an
    unknown balance."""
    account_hash = _resolve_account_hashes()[account]
    r = _get_client().get_account(account_hash)
    r.raise_for_status()
    balances = r.json()["securitiesAccount"]["currentBalances"]
    if "cashAvailableForTrading" in balances:
        return float(balances["cashAvailableForTrading"])
    return float(balances["availableFunds"])


def get_account_buying_power(account: str) -> float:
    """Real 'buyingPower' for the account, read fresh (never cached) -- used
    only by the add-on leg's cash-availability check (D2,
    docs/plans/real_order_execution_drought_addon.md 3.1 Exemption D). An
    add-on is sized at 100% of an already-deployed position (by construction
    it's borrowing), so get_account_balance's 'availableFunds'/
    'cashAvailableForTrading' would refuse nearly every real add-on -- this
    reads the margin-inclusive figure instead. Field name UNVERIFIED against a
    real Schwab response (same caveat as get_account_balance's own fields when
    first written) -- confirm against a real account before this path is
    staged live. Raises on any failure, same fail-closed contract as
    get_account_balance."""
    account_hash = _resolve_account_hashes()[account]
    r = _get_client().get_account(account_hash)
    r.raise_for_status()
    balances = r.json()["securitiesAccount"]["currentBalances"]
    return float(balances["buyingPower"])


def get_real_position(account: str, ticker: str) -> float:
    """Real share quantity held for `ticker` in `account`, read fresh (never
    cached) -- Client.get_account(fields=[Account.Fields.POSITIONS]),
    securitiesAccount.positions[].longQuantity for the matching instrument.
    Field names follow Schwab's documented schema but are unverified against
    a real account response (same caveat pattern as get_account_balance).
    Returns 0.0 if the ticker isn't held at all -- used by the live-state
    reconciliation check (signals_notify.check_live_state_reconciliation) to
    compare the broker's real position against open_positions.shares."""
    account_hash = _resolve_account_hashes()[account]
    r = _get_client().get_account(account_hash, fields=[schwab.client.Client.Account.Fields.POSITIONS])
    r.raise_for_status()
    positions = r.json()["securitiesAccount"].get("positions", [])
    for p in positions:
        if p.get("instrument", {}).get("symbol") == ticker:
            return float(p.get("longQuantity", 0.0))
    return 0.0


def _get_raw_real_positions(account: str) -> list:
    """Shared fetch behind get_all_real_positions/get_all_real_short_positions
    -- one real API call, read fresh (never cached)."""
    account_hash = _resolve_account_hashes()[account]
    r = _get_client().get_account(account_hash, fields=[schwab.client.Client.Account.Fields.POSITIONS])
    r.raise_for_status()
    return r.json()["securitiesAccount"].get("positions", [])


def get_all_real_positions(account: str) -> dict:
    """Every real LONG position currently held in `account`, read fresh --
    {ticker: longQuantity}, zero-quantity rows dropped. Sums rather than
    overwrites if Schwab ever returns 2+ rows for the same symbol (defensive
    -- positions are documented as one aggregated row per instrument, but
    silently understating a real quantity on a duplicate would be the wrong
    failure direction for a ground-truth check). Unlike get_real_position
    (single ticker, used when you already know what you're looking for),
    this is the ground-truth sweep primitive: it answers "what does the
    broker actually hold" with no assumption about which tickers should be
    checked -- see automation_principles.md #1 ("never trust a local/cached
    record as ground truth") and scripts/check_untracked_positions.py, built
    2026-08-07 after a real position (GDXU, soxl_ira) sat completely
    untracked in open_positions for a week with nothing ever sweeping the
    broker's own position list to notice. See get_all_real_short_positions
    for the short side -- deliberately NOT merged into this dict, since a
    long+short in the same symbol is a distinct, more dangerous state than a
    plain long that a single {ticker: qty} shape would obscure."""
    out = {}
    for p in _get_raw_real_positions(account):
        symbol = p.get("instrument", {}).get("symbol")
        qty = float(p.get("longQuantity", 0.0))
        if symbol and qty:
            out[symbol] = out.get(symbol, 0.0) + qty
    return out


def get_all_real_short_positions(account: str) -> dict:
    """Every real SHORT position currently held in `account` -- {ticker:
    shortQuantity}. get_all_real_positions only ever looks at longQuantity,
    so a naked/accidental short (longQuantity=0, shortQuantity>0 -- exactly
    the failure mode live_sanity_check.py's naked-SELL test exists to guard
    against) was previously invisible to the ground-truth sweep: with no
    local row it dropped out silently, and WITH a local row it rendered as
    the misleading "STALE: broker holds 0" instead of "broker is short."
    Found by paired Opus review, 2026-08-07."""
    out = {}
    for p in _get_raw_real_positions(account):
        symbol = p.get("instrument", {}).get("symbol")
        qty = float(p.get("shortQuantity", 0.0))
        if symbol and qty:
            out[symbol] = out.get(symbol, 0.0) + qty
    return out


def get_current_price(ticker: str) -> float:
    """Primary: Schwab's own get_quote, extended.lastPrice -- confirmed
    real-time (realtime: true, live quoteTime) unlike yfinance's pre-market
    feed, which can run tens of minutes stale (Part 3 design, docs/research_log.md
    2026-07-21). Falls back to yfinance's fast_info.last_price (the existing
    live-price path used elsewhere, e.g. pages/10_Open_Positions.py) on any
    Schwab-side error -- standard primary/fallback resilience, not a signal
    Schwab's feed is untrusted."""
    try:
        r = _get_client().get_quote(ticker)
        r.raise_for_status()
        quote = r.json()[ticker]
        price = quote.get("extended", {}).get("lastPrice") or quote["quote"]["lastPrice"]
        return float(price)
    except Exception:
        import yfinance as yf
        return float(yf.Ticker(ticker).fast_info.last_price)
