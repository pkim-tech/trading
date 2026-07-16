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

import schwab.orders.equities as equity_orders
from schwab.orders.generic import OrderBuilder
from schwab.orders.common import (
    OrderType, Session, Duration, OrderStrategyType,
    StopPriceLinkBasis, StopPriceLinkType, EquityInstruction,
)

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


def _place_equity_order(side: str, account: str, ticker: str, quantity: int, price: float):
    """side is 'BUY' or 'SELL'. price is only used for the safety-cap notional
    check, not sent to the API -- this places a market order."""
    try:
        dry_run = schwab_safety.approve_and_record(account, ticker, quantity, price, side)
    except schwab_safety.SafetyViolation as e:
        _post_message(f"\U0001F6AB BLOCKED {side} {quantity} {ticker} in {account}: {e}")
        raise

    if dry_run:
        _post_message(f"[DRY RUN] would {side} {quantity} {ticker} in {account} (~${quantity * price:,.0f})")
        print(f"[DRY RUN] would {side} {quantity} {ticker} in {account} (~${quantity * price:,.0f})")
        return None

    account_hash = _resolve_account_hashes()[account]
    order_fn = equity_orders.equity_buy_market if side == "BUY" else equity_orders.equity_sell_market
    order = order_fn(ticker, quantity)
    r = _get_client().place_order(account_hash, order)
    r.raise_for_status()
    _post_message(f"✅ {side} {quantity} {ticker} in {account} submitted to Schwab (~${quantity * price:,.0f})")
    return r


def place_equity_buy(account: str, ticker: str, quantity: int, price: float):
    return _place_equity_order("BUY", account, ticker, quantity, price)


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
        return None

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
    _post_message(f"✅ {label} {quantity} {ticker} in {account} submitted to Schwab "
                  f"(trail={trail_pct}%, ~${quantity * price:,.0f})")
    return r


def place_trailing_buy(account: str, ticker: str, quantity: int, price: float, trail_pct: float):
    """trail_pct is the bounce-above-running-low trigger (matches the node's
    trail_buy_pct). ASK-linked, since a buy naturally references the ask."""
    return _place_trailing_order("BUY", StopPriceLinkBasis.ASK, account, ticker, quantity, price, trail_pct)


def place_trailing_sell(account: str, ticker: str, quantity: int, price: float, trail_pct: float):
    """trail_pct is the pullback-below-running-high trigger (matches the
    position's trail_sell_pct). BID-linked, since a sell naturally references
    the bid. Only relevant once the position's trailing-exit state has
    activated (strategies.TrailingBothZScoreBreakout.check_exit's
    state['trailing'] -- see signals_notify.notify_trailing_activated), same
    as the manual workflow's 'place the trailing stop order now' step."""
    return _place_trailing_order("SELL", StopPriceLinkBasis.BID, account, ticker, quantity, price, trail_pct)
