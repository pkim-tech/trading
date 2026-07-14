"""
Safety gate between active_signals.py's decision logic and the raw Schwab
client (schwab_client.py). Every order must pass through check_order()/
approve_and_record() before schwab_client calls the real API -- this module
is deliberately the only checkpoint, so a bug in active_signals.py can't
place an unbounded order.

All limits below are placeholders, not tuned real figures -- schwab_client.py
starts every account in dry_run=True until these are reviewed and explicitly
turned off per account.
"""
import fcntl
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

STATE_PATH = Path(__file__).parent / "cache" / "live" / "schwab_order_counts.json"


class SafetyViolation(Exception):
    pass


@dataclass
class AccountLimits:
    enabled: bool          # allowlist -- False blocks every order for this account
    notional_cap: float    # max $ per single order
    daily_order_cap: int   # max orders per calendar day
    dry_run: bool          # True: log what would happen, never call place_order


# Placeholder per-account config -- tune before going live. Account-risk framing
# from the 2026-07-13 research session: Brokerage/SEP are large and need tight
# controls, Roth ($50k) is deliberate play money, IRA is fine/not small.
ACCOUNTS = {
    "brokerage": AccountLimits(enabled=True, notional_cap=10_000, daily_order_cap=5,  dry_run=True),
    "sep":       AccountLimits(enabled=True, notional_cap=10_000, daily_order_cap=5,  dry_run=True),
    "roth":      AccountLimits(enabled=True, notional_cap=50_000, daily_order_cap=10, dry_run=True),
    "ira":       AccountLimits(enabled=True, notional_cap=75_000, daily_order_cap=10, dry_run=True),
}

# Absolute backstop regardless of account config -- catches a misconfigured
# per-account cap before it reaches the API.
HARD_ORDER_CEILING = 100_000


def kill_switch_engaged() -> bool:
    return os.environ.get("SCHWAB_KILL_SWITCH") == "1"


def _open_locked():
    """Opens STATE_PATH for read+write under an exclusive flock, creating it
    first if needed. Caller must close() when done (releases the lock)."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.touch(exist_ok=True)
    f = open(STATE_PATH, "r+")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def check_order(account: str, ticker: str, quantity: int, price: float, counts: dict | None = None) -> None:
    """Raises SafetyViolation if the order should not proceed. `counts`, if
    given, is used for the daily-cap check instead of re-reading the state
    file -- lets approve_and_record() validate against the exact snapshot
    it's about to increment, under one lock, instead of a separate read."""
    if kill_switch_engaged():
        raise SafetyViolation("global kill switch engaged (SCHWAB_KILL_SWITCH=1)")

    limits = ACCOUNTS.get(account)
    if limits is None:
        raise SafetyViolation(f"unknown account '{account}' -- not in the allowlist")
    if not limits.enabled:
        raise SafetyViolation(f"account '{account}' is disabled in the allowlist")

    notional = quantity * price
    if notional > HARD_ORDER_CEILING:
        raise SafetyViolation(
            f"order notional ${notional:,.0f} ({ticker} x{quantity}) exceeds hard ceiling ${HARD_ORDER_CEILING:,.0f}"
        )
    if notional > limits.notional_cap:
        raise SafetyViolation(
            f"order notional ${notional:,.0f} ({ticker} x{quantity}) exceeds {account} cap ${limits.notional_cap:,.0f}"
        )

    if counts is None:
        with _open_locked() as f:
            counts = json.loads(f.read() or "{}")
    today = counts.get(str(date.today()), {})
    count = today.get(account, 0)
    if count >= limits.daily_order_cap:
        raise SafetyViolation(f"account '{account}' has hit its daily order cap ({limits.daily_order_cap})")


def approve_and_record(account: str, ticker: str, quantity: int, price: float) -> bool:
    """Call immediately before placing a real order. Raises SafetyViolation if
    blocked; otherwise records the order against the daily cap and returns
    whether the account is in dry_run mode (caller must skip the real API
    call if so). The daily-cap check and the increment happen under the same
    file lock so two concurrent callers can't both slip past the cap."""
    with _open_locked() as f:
        counts = json.loads(f.read() or "{}")
        check_order(account, ticker, quantity, price, counts=counts)
        key = str(date.today())
        today = counts.setdefault(key, {})
        today[account] = today.get(account, 0) + 1
        f.seek(0)
        f.truncate()
        f.write(json.dumps(counts))
    return ACCOUNTS[account].dry_run
