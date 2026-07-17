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
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

import signals_db

load_dotenv()

STATE_PATH = Path(__file__).parent / "cache" / "live" / "schwab_order_counts.json"

# Separate file (not just SCHWAB_KILL_SWITCH env var) so a Slack "Stop Engine"
# button click survives a daemon restart -- an env var set in-process would
# silently reset to "running" on the next restart, the wrong default for a
# safety-critical switch (2026-07-15).
KILL_SWITCH_PATH = Path(__file__).parent / "cache" / "live" / "schwab_kill_switch.json"

# Per-ticker on/off within AUTOMATION_ENABLED_TICKERS scope (2026-07-17) -- the
# scope set itself is a deliberate code change (widen once a pilot is proven
# out), but day to day a single ticker's automation needs to be pausable from a
# phone without touching code, same rationale as the global kill switch above.
# Same persisted-file pattern so a pause survives a daemon restart.
TICKER_AUTOMATION_PATH = Path(__file__).parent / "cache" / "live" / "schwab_ticker_automation.json"

# Mirrors active_signals._SIGNAL_WINDOWS + _OPEN_CHECK_WINDOWS -- kept as separate
# constants here (not imported) to avoid a real circular import (active_signals ->
# signals_notify -> schwab_safety). Only gates BUY orders: check_sell_condition runs
# every poll cycle all market hours, not just these windows (active_signals.py:214),
# so a SELL restricted to this window would incorrectly block a legitimate exit.
# Both window sets are allowed since an entry_timing='open_check' node's real BUY can
# fire in the earlier window (see active_signals._scan_buy_signals) -- narrowing this
# gate to only the close windows would reject every legitimate open_check automated
# order.
_SIGNAL_WINDOWS = [(10, 25, 10, 40), (15, 25, 15, 40)]
_OPEN_CHECK_WINDOWS = [(9, 31, 9, 40), (14, 31, 14, 40)]

# Duplicate-submit guard: a second order for the same account+ticker+side
# within this window is almost certainly a retry/double-call bug, not a real
# distinct signal (signal windows are 15 min wide; genuine re-entries happen
# on a completely different bar, not seconds/minutes later).
DUPLICATE_ORDER_WINDOW_SECS = 60

# One-BUY-order-per-ticker guard (2026-07-17): confirmed empirically that Schwab
# does not reserve/check buying power for a resting order at placement time (a
# real $200k TRAILING_STOP and a real limit order both left buying power
# unaffected) -- so nothing on Schwab's side stops a second BUY order for a
# ticker that already has one outstanding from being accepted too. This checks
# Schwab's own live order book (not just local state, which could drift or miss
# a manually-placed order) before allowing a new BUY. SELL is deliberately never
# blocked by this -- closing a same-day-opened position must always go through,
# same asymmetry as the same-day-re-buy guardrail above.
_OPEN_ORDER_STATUSES_EXCLUDED = {"CANCELED", "EXPIRED", "FILLED", "REJECTED", "REPLACED"}


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

# Live-automation scope, swapped 2026-07-17: GDXD replaces KORU as the sole pilot
# ticker (not additive). KORU was flat (no open position) at swap time, so no
# mid-position handoff risk either way -- GDXD was chosen instead because it's a
# small, never-traded ($5k) book, isolating the pilot's real-money exposure while
# still exercising the full automated path (including the new entry_timing=
# 'open_check' node type -- see active_signals._OPEN_CHECK_WINDOWS). GDXD's
# candidate-checklist review flagged real risk (only ~7% of robust alpha survives
# the same-day-block constraint; win rate declines in the late 30% of history) --
# accepted given the small $5k size, per user 2026-07-17. Every other live
# watchlist ticker (including KORU, still live/manual) still goes through the
# existing manual Slack workflow -- this is a restriction *on top of* the
# ticker-allowlist/mode check, not a replacement for it.
AUTOMATION_ENABLED_TICKERS = {"GDXD"}

def _now():
    """Seam for tests to monkeypatch -- the real signal windows only make
    sense at actual current wall-clock time, but tests need to simulate being
    inside/outside a window regardless of when they happen to run."""
    return datetime.now()


# Absolute backstop regardless of account config -- catches a misconfigured
# per-account cap before it reaches the API.
HARD_ORDER_CEILING = 100_000

# Global (all-accounts) burst cap, separate from each account's daily cap --
# catches a runaway loop spamming orders within a single signal-check minute
# before the daily cap would ever trip. Sized at 2x the 6-ticker live watchlist
# (buy+sell per ticker in the same minute), not Schwab's own 120/min platform limit.
GLOBAL_ORDERS_PER_MINUTE = 12


def kill_switch_engaged() -> bool:
    if os.environ.get("SCHWAB_KILL_SWITCH") == "1":
        return True
    if KILL_SWITCH_PATH.exists():
        try:
            return bool(json.loads(KILL_SWITCH_PATH.read_text()).get("engaged", False))
        except (json.JSONDecodeError, OSError):
            return False
    return False


def kill_switch_reason() -> str:
    """Human-readable source for why kill_switch_engaged() is True -- used in
    the SafetyViolation message so it doesn't misleadingly cite the env var
    when the persistent Stop-Engine flag is the actual trigger."""
    if os.environ.get("SCHWAB_KILL_SWITCH") == "1":
        return "SCHWAB_KILL_SWITCH=1 env var"
    if KILL_SWITCH_PATH.exists():
        try:
            state = json.loads(KILL_SWITCH_PATH.read_text())
            return state.get("reason") or "Stop Engine"
        except (json.JSONDecodeError, OSError):
            pass
    return "unknown"


def engage_kill_switch(reason: str = ""):
    """Persists the stopped state so it survives a daemon restart. Called by
    the Slack 'Stop Engine' button handler (signals_handlers.py)."""
    KILL_SWITCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    KILL_SWITCH_PATH.write_text(json.dumps({
        "engaged": True, "reason": reason, "at": datetime.now().isoformat(),
    }))


def disengage_kill_switch():
    """Called by the Slack 'Start Engine' button handler."""
    KILL_SWITCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    KILL_SWITCH_PATH.write_text(json.dumps({"engaged": False, "at": datetime.now().isoformat()}))


def ticker_automation_enabled(ticker: str) -> bool:
    """True unless a persisted per-ticker override has explicitly paused it --
    default-on for anything in AUTOMATION_ENABLED_TICKERS, mirroring how a
    fresh kill switch file defaults to not-engaged."""
    if TICKER_AUTOMATION_PATH.exists():
        try:
            state = json.loads(TICKER_AUTOMATION_PATH.read_text())
            if ticker in state:
                return bool(state[ticker])
        except (json.JSONDecodeError, OSError):
            pass
    return True


def pause_ticker_automation(ticker: str, reason: str = ""):
    """Called by the Slack per-ticker 'Pause Automation' button handler."""
    TICKER_AUTOMATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {}
    if TICKER_AUTOMATION_PATH.exists():
        try:
            state = json.loads(TICKER_AUTOMATION_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}
    state[ticker] = False
    state[f"{ticker}_reason"] = reason
    TICKER_AUTOMATION_PATH.write_text(json.dumps(state))


def resume_ticker_automation(ticker: str):
    """Called by the Slack per-ticker 'Resume Automation' button handler."""
    TICKER_AUTOMATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {}
    if TICKER_AUTOMATION_PATH.exists():
        try:
            state = json.loads(TICKER_AUTOMATION_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}
    state[ticker] = True
    state.pop(f"{ticker}_reason", None)
    TICKER_AUTOMATION_PATH.write_text(json.dumps(state))


def _open_locked():
    """Opens STATE_PATH for read+write under an exclusive flock, creating it
    first if needed. Caller must close() when done (releases the lock)."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.touch(exist_ok=True)
    f = open(STATE_PATH, "r+")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def _live_ticker_accounts() -> dict:
    """ticker -> assigned account nickname, for watchlist rows currently in
    'live' mode -- queried fresh (not cached) since mode/account assignment
    can change during a running day (e.g. AGQ moved to research mid-session
    2026-07-13)."""
    return {row["ticker"]: row["account"] for row in signals_db.get_watchlist() if row["mode"] == "live"}


def _has_open_order(account: str, ticker: str) -> bool:
    """True if Schwab's own live order book shows any non-terminal order for
    this ticker in this account -- a real API call, not local state, since
    local tracking could drift or miss an order placed outside our own code
    (e.g. directly in Schwab's UI, as happened during today's settlement test)."""
    import schwab_client  # local import: schwab_client imports this module at load time
    account_hash = schwab_client._resolve_account_hashes()[account]
    r = schwab_client._get_client().get_orders_for_account(account_hash)
    r.raise_for_status()
    for o in r.json():
        if o.get("status") in _OPEN_ORDER_STATUSES_EXCLUDED:
            continue
        legs = o.get("orderLegCollection", [])
        if any(leg.get("instrument", {}).get("symbol") == ticker for leg in legs):
            return True
    return False


def check_order(
    account: str, ticker: str, quantity: int, price: float, side: str, counts: dict | None = None
) -> None:
    """Raises SafetyViolation if the order should not proceed. `counts`, if
    given, is used for the daily-cap/burst-cap/duplicate checks instead of
    re-reading the state file -- lets approve_and_record() validate against
    the exact snapshot it's about to increment, under one lock, instead of a
    separate read."""
    if kill_switch_engaged():
        raise SafetyViolation(f"global kill switch engaged ({kill_switch_reason()})")

    limits = ACCOUNTS.get(account)
    if limits is None:
        raise SafetyViolation(f"unknown account '{account}' -- not in the allowlist")
    if not limits.enabled:
        raise SafetyViolation(f"account '{account}' is disabled in the allowlist")

    ticker_accounts = _live_ticker_accounts()
    if ticker not in ticker_accounts:
        raise SafetyViolation(f"'{ticker}' is not a live-mode ticker on the active watchlist")
    if ticker_accounts[ticker] != account:
        raise SafetyViolation(
            f"'{ticker}' is assigned to account '{ticker_accounts[ticker]}', not '{account}'"
        )
    if ticker not in AUTOMATION_ENABLED_TICKERS:
        raise SafetyViolation(
            f"'{ticker}' is not in the automation pilot scope {AUTOMATION_ENABLED_TICKERS} "
            f"-- still manual-only"
        )
    if not ticker_automation_enabled(ticker):
        raise SafetyViolation(f"'{ticker}' automation is paused (per-ticker toggle) -- resume from the reference report")

    # Same-day re-buy guardrail (2026-07-15): a same-day re-buy risks a real
    # Schwab good-faith violation (reusing unsettled sale proceeds in a cash
    # account) -- a hard broker-enforced constraint, unlike the same-day-sell
    # direction (a soft employer recommendation, not enforced, deliberately
    # left out).
    if side == "BUY" and signals_db.closed_today(ticker):
        raise SafetyViolation(
            f"'{ticker}' was sold today -- same-day re-buy risks a cash-account good-faith violation"
        )

    if side == "BUY" and _has_open_order(account, ticker):
        raise SafetyViolation(
            f"'{ticker}' already has an open/working order in '{account}' -- refusing a second "
            f"concurrent BUY (Schwab doesn't reserve buying power for a resting order, so nothing "
            f"else stops these from stacking)"
        )

    # Signal-window time gate, BUY only (see _SIGNAL_WINDOWS comment above).
    if side == "BUY":
        now = _now()
        t = (now.hour, now.minute)
        all_windows = _SIGNAL_WINDOWS + _OPEN_CHECK_WINDOWS
        in_window = any((h0, m0) <= t <= (h1, m1) for h0, m0, h1, m1 in all_windows)
        if not in_window:
            raise SafetyViolation(
                f"BUY outside signal windows {all_windows} (current time {t[0]:02d}:{t[1]:02d})"
            )

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

    recent = [t for t in counts.get("recent_order_timestamps", []) if time.time() - t < 60]
    if len(recent) >= GLOBAL_ORDERS_PER_MINUTE:
        raise SafetyViolation(
            f"global burst cap hit ({len(recent)} orders across all accounts in the last minute, "
            f"max {GLOBAL_ORDERS_PER_MINUTE})"
        )

    for o in counts.get("recent_orders", []):
        if (
            o["account"] == account and o["ticker"] == ticker and o["side"] == side
            and time.time() - o["ts"] < DUPLICATE_ORDER_WINDOW_SECS
        ):
            raise SafetyViolation(
                f"duplicate order: {side} {ticker} in {account} already submitted "
                f"{time.time() - o['ts']:.0f}s ago (within {DUPLICATE_ORDER_WINDOW_SECS}s window)"
            )


def approve_and_record(account: str, ticker: str, quantity: int, price: float, side: str) -> bool:
    """Call immediately before placing a real order. Raises SafetyViolation if
    blocked; otherwise records the order against the daily cap, the global
    per-minute burst cap, and the duplicate-order window, and returns whether
    the account is in dry_run mode (caller must skip the real API call if so).
    Checks and increments happen under the same file lock so two concurrent
    callers can't both slip past a cap."""
    with _open_locked() as f:
        counts = json.loads(f.read() or "{}")
        check_order(account, ticker, quantity, price, side, counts=counts)
        key = str(date.today())
        today = counts.setdefault(key, {})
        today[account] = today.get(account, 0) + 1
        recent = [t for t in counts.get("recent_order_timestamps", []) if time.time() - t < 60]
        recent.append(time.time())
        counts["recent_order_timestamps"] = recent
        recent_orders = [
            o for o in counts.get("recent_orders", [])
            if time.time() - o["ts"] < DUPLICATE_ORDER_WINDOW_SECS
        ]
        recent_orders.append({"account": account, "ticker": ticker, "side": side, "ts": time.time()})
        counts["recent_orders"] = recent_orders
        f.seek(0)
        f.truncate()
        f.write(json.dumps(counts))
    return ACCOUNTS[account].dry_run
