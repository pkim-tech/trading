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

NICKNAMES = ["brokerage", "sep", "roth", "ira"]


def _get_client():
    global _client
    if _client is None:
        _client = schwab_auth.get_client()
    return _client


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


def _place_equity_order(
    side: str, account: str, ticker: str, quantity: int, price: float, is_gap_correction: bool = False,
):
    """side is 'BUY' or 'SELL'. price is only used for the safety-cap notional
    check, not sent to the API -- this places a market order. Returns
    (response, order_id); dry_run returns (None, None)."""
    try:
        dry_run = schwab_safety.approve_and_record(
            account, ticker, quantity, price, side, is_gap_correction=is_gap_correction)
    except schwab_safety.SafetyViolation as e:
        _post_message(f"\U0001F6AB BLOCKED {side} {quantity} {ticker} in {account}: {e}")
        raise

    if dry_run:
        _post_message(f"[DRY RUN] would {side} {quantity} {ticker} in {account} (~${quantity * price:,.0f})")
        print(f"[DRY RUN] would {side} {quantity} {ticker} in {account} (~${quantity * price:,.0f})")
        return None, None

    account_hash = _resolve_account_hashes()[account]
    order_fn = equity_orders.equity_buy_market if side == "BUY" else equity_orders.equity_sell_market
    order = order_fn(ticker, quantity)
    r = _get_client().place_order(account_hash, order)
    r.raise_for_status()
    order_id = Utils(_get_client(), account_hash).extract_order_id(r)
    _post_message(f"✅ {side} {quantity} {ticker} in {account} submitted to Schwab (~${quantity * price:,.0f})")
    return r, order_id


def place_equity_buy(account: str, ticker: str, quantity: int, price: float, is_gap_correction: bool = False):
    return _place_equity_order("BUY", account, ticker, quantity, price, is_gap_correction=is_gap_correction)


def place_equity_sell(account: str, ticker: str, quantity: int, price: float):
    return _place_equity_order("SELL", account, ticker, quantity, price)


def _place_trailing_order(
    side: str, link_basis: StopPriceLinkBasis, account: str, ticker: str,
    quantity: int, price: float, trail_pct: float,
):
    """side is 'BUY' or 'SELL'. price is the current live price, used only for
    the safety-cap notional check (quantity * price), not sent to the API.
    Orders are GOOD_TILL_CANCEL, matching the manual workflow's existing
    trailing-order convention (docs/CLAUDE.md's TrailingBothZScoreBreakout
    execution notes). Schwab tracks the running high/low and fires the order
    itself; this module never polls for the bounce/pullback."""
    label = "TRAILING BUY" if side == "BUY" else "TRAILING SELL"
    try:
        dry_run = schwab_safety.approve_and_record(account, ticker, quantity, price, side)
    except schwab_safety.SafetyViolation as e:
        _post_message(f"\U0001F6AB BLOCKED {label} {quantity} {ticker} in {account} "
                      f"(trail={trail_pct}%): {e}")
        raise

    if dry_run:
        msg = (f"[DRY RUN] would place {label} {quantity} {ticker} in {account} "
               f"(trail={trail_pct}%, ~${quantity * price:,.0f})")
        _post_message(msg)
        print(msg)
        return None, None

    account_hash = _resolve_account_hashes()[account]
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

    r = _get_client().place_order(account_hash, order)
    r.raise_for_status()
    order_id = Utils(_get_client(), account_hash).extract_order_id(r)
    _post_message(f"✅ {label} {quantity} {ticker} in {account} submitted to Schwab "
                  f"(trail={trail_pct}%, ~${quantity * price:,.0f})")
    return r, order_id


def place_trailing_buy(account: str, ticker: str, quantity: int, price: float, trail_pct: float):
    """trail_pct is the bounce-above-running-low trigger (matches the node's
    trail_buy_pct). ASK-linked, since a buy naturally references the ask."""
    return _place_trailing_order("BUY", StopPriceLinkBasis.ASK, account, ticker, quantity, price, trail_pct)


def get_filled_order(account: str, ticker: str, side: str):
    """Best-effort poll of Schwab's live order book for the most recent FILLED
    order matching ticker+side -- used by signals_notify.check_auto_fills to
    auto-record a fill without a human clicking Filled/Exited. Field names
    (orderActivityCollection/executionLegs) follow Schwab's documented order
    schema but are unverified against a real fill response -- confirm against
    one real (dry_run=False) fill before trusting this for anything beyond the
    opt-in auto-fill-detection toggle, which defaults off. Returns
    {'price': float, 'quantity': float} or None if no matching fill is found."""
    account_hash = _resolve_account_hashes()[account]
    r = _get_client().get_orders_for_account(account_hash)
    r.raise_for_status()
    instruction = EquityInstruction.BUY if side == "BUY" else EquityInstruction.SELL
    candidates = []
    for o in r.json():
        if o.get("status") != "FILLED":
            continue
        legs = o.get("orderLegCollection", [])
        matches = any(
            leg.get("instrument", {}).get("symbol") == ticker
            and leg.get("instruction") == instruction.value
            for leg in legs
        )
        if not matches:
            continue
        exec_legs = [
            el for activity in o.get("orderActivityCollection", [])
            for el in activity.get("executionLegs", [])
        ]
        if not exec_legs:
            continue
        total_qty = sum(el.get("quantity", 0) for el in exec_legs)
        if not total_qty:
            continue
        vwap = sum(el.get("price", 0) * el.get("quantity", 0) for el in exec_legs) / total_qty
        candidates.append((o.get("closeTime") or o.get("enteredTime") or "", vwap, total_qty))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    _, price, qty = candidates[-1]
    return {"price": price, "quantity": qty}


def place_trailing_sell(account: str, ticker: str, quantity: int, price: float, trail_pct: float):
    """trail_pct is the pullback-below-running-high trigger (matches the
    position's trail_sell_pct). BID-linked, since a sell naturally references
    the bid. Only relevant once the position's trailing-exit state has
    activated (strategies.TrailingBothZScoreBreakout.check_exit's
    state['trailing'] -- see signals_notify.notify_trailing_activated), same
    as the manual workflow's 'place the trailing stop order now' step."""
    return _place_trailing_order("SELL", StopPriceLinkBasis.BID, account, ticker, quantity, price, trail_pct)


def cancel_order(account: str, ticker: str, order_id: int):
    """Cancels a still-resting order -- used by signals_notify.check_gap_resize
    (Part 3, branch B) to pull a stale trailing-buy order once an overnight
    gap has already cleared its trigger, before replacing it with a plain
    MARKET order. No approve_and_record gate -- this isn't a new placement,
    just withdrawing one already approved."""
    account_hash = _resolve_account_hashes()[account]
    r = _get_client().cancel_order(order_id, account_hash)
    r.raise_for_status()
    _post_message(f"\U0001F5D1️ cancelled resting order {order_id} ({ticker} in {account})")
    return r


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


def place_stop_loss(account: str, ticker: str, quantity: int, stop_price: float):
    """Resting fixed-price STOP order -- the broker executes on breach without
    depending on our poll cadence (Part 4, Section 6), same mechanism already
    relied on for the trailing-sell order. Same OrderBuilder pattern as
    _place_trailing_order, just OrderType.STOP + set_stop_price instead of
    TRAILING_STOP + link-basis/offset. Returns (response, order_id); dry_run
    returns (None, None)."""
    try:
        dry_run = schwab_safety.approve_and_record(account, ticker, quantity, stop_price, "SELL")
    except schwab_safety.SafetyViolation as e:
        _post_message(f"\U0001F6AB BLOCKED STOP LOSS {quantity} {ticker} in {account} @ ${stop_price:.4f}: {e}")
        raise

    if dry_run:
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
    order.set_stop_price(stop_price)
    order.add_equity_leg(EquityInstruction.SELL, ticker, quantity)

    r = _get_client().place_order(account_hash, order)
    r.raise_for_status()
    order_id = Utils(_get_client(), account_hash).extract_order_id(r)
    _post_message(f"✅ STOP LOSS {quantity} {ticker} in {account} submitted to Schwab @ ${stop_price:.4f}")
    return r, order_id


def get_account_balance(account: str) -> float:
    """Real available cash for the account, read fresh (never cached) --
    Client.get_account, securitiesAccount.currentBalances.cashAvailableForTrading.
    Field name follows Schwab's documented schema but is unverified against a
    real account response (same caveat pattern as get_filled_order) -- confirm
    against a real account before trusting this beyond the automation_principles.md
    #2 fail-closed design it's built for. Raises on any failure (network,
    missing field, etc.) rather than returning a fallback value -- the caller
    (schwab_safety.check_order) must fail closed on a balance-check failure,
    not silently allow the order through with an unknown balance."""
    account_hash = _resolve_account_hashes()[account]
    r = _get_client().get_account(account_hash)
    r.raise_for_status()
    return float(r.json()["securitiesAccount"]["currentBalances"]["cashAvailableForTrading"])


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
