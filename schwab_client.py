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

import schwab_auth
import schwab_safety

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

    numbers = {n: os.environ.get(f"SCHWAB_ACCOUNT_{n.upper()}") for n in NICKNAMES}
    r = _get_client().get_account_numbers()
    r.raise_for_status()
    number_to_hash = {a["accountNumber"]: a["hashValue"] for a in r.json()}
    _account_hashes = {
        nickname: number_to_hash[num]
        for nickname, num in numbers.items()
        if num and num in number_to_hash
    }
    return _account_hashes


def place_equity_buy(account: str, ticker: str, quantity: int, price: float):
    """price is only used for the safety-cap notional check, not sent to the
    API -- this places a market order."""
    dry_run = schwab_safety.approve_and_record(account, ticker, quantity, price)
    if dry_run:
        print(f"[DRY RUN] would BUY {quantity} {ticker} in {account} (~${quantity * price:,.0f})")
        return None

    account_hash = _resolve_account_hashes()[account]
    order = equity_orders.equity_buy_market(ticker, quantity)
    r = _get_client().place_order(account_hash, order)
    r.raise_for_status()
    return r


def place_equity_sell(account: str, ticker: str, quantity: int, price: float):
    dry_run = schwab_safety.approve_and_record(account, ticker, quantity, price)
    if dry_run:
        print(f"[DRY RUN] would SELL {quantity} {ticker} in {account} (~${quantity * price:,.0f})")
        return None

    account_hash = _resolve_account_hashes()[account]
    order = equity_orders.equity_sell_market(ticker, quantity)
    r = _get_client().place_order(account_hash, order)
    r.raise_for_status()
    return r
