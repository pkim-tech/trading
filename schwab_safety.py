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
import functools
import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
import pandas_market_calendars as mcal

import signals_db

load_dotenv()

# Overridable so a test/sim script (e.g. scripts/live_sim_harness.py) can isolate
# every real safety-state file the same way signals_config.DB_PATH is isolated via
# TRADING_DB_PATH -- found 2026-07-23 the hard way: none of these paths were
# overridable, and a harness script under active development wrote real dry-run
# BUY attempts straight into the real STATE_PATH across repeated runs, driving the
# real 'ira' account's daily_order_cap counter to its actual limit before this was
# caught. Defaults to the original hardcoded location when unset.
_STATE_DIR = Path(os.environ.get("SCHWAB_STATE_DIR", str(Path(__file__).parent / "cache" / "live")))

STATE_PATH = _STATE_DIR / "schwab_order_counts.json"

# Separate file (not just SCHWAB_KILL_SWITCH env var) so a Slack "Stop Engine"
# button click survives a daemon restart -- an env var set in-process would
# silently reset to "running" on the next restart, the wrong default for a
# safety-critical switch (2026-07-15).
KILL_SWITCH_PATH = _STATE_DIR / "schwab_kill_switch.json"

# Per-ticker on/off within AUTOMATION_ENABLED_TICKERS scope (2026-07-17) -- the
# scope set itself is a deliberate code change (widen once a pilot is proven
# out), but day to day a single ticker's automation needs to be pausable from a
# phone without touching code, same rationale as the global kill switch above.
# Same persisted-file pattern so a pause survives a daemon restart.
TICKER_AUTOMATION_PATH = _STATE_DIR / "schwab_ticker_automation.json"

# Auto-fill-detection toggle (2026-07-17) -- separate from ticker_automation_enabled
# above (which gates order *placement*) and opposite default: placement automation
# is on-by-default within AUTOMATION_ENABLED_TICKERS scope, but polling Schwab's
# order book to auto-record a fill (skipping the human Filled/Exited click) is a
# distinct, newer capability that stays off until explicitly enabled per ticker,
# since schwab_client.get_filled_order's field parsing hasn't been confirmed
# against a real fill response yet.
AUTO_FILL_DETECTION_PATH = _STATE_DIR / "schwab_auto_fill_detection.json"

# Mirrors active_signals._SIGNAL_WINDOWS + _OPEN_CHECK_WINDOWS -- kept as separate
# constants here (not imported) to avoid a real circular import (active_signals ->
# signals_notify -> schwab_safety). Only gates BUY orders: check_sell_condition runs
# every poll cycle all market hours, not just these windows (active_signals.py:214),
# so a SELL restricted to this window would incorrectly block a legitimate exit.
# Both window sets are allowed since an entry_timing='open_check' node's real BUY can
# fire in the earlier window (see active_signals._scan_buy_signals) -- narrowing this
# gate to only the close windows would reject every legitimate open_check automated
# order. Starts at :30 (not active_signals._OPEN_CHECK_WINDOWS' :31) to also admit
# Part 4's pinned single-shot entry checks, which fire at :30:02 -- one minute ahead
# of the ambient open-check poll window this mirrors.
_SIGNAL_WINDOWS = [(10, 25, 10, 40), (15, 25, 15, 40)]
_OPEN_CHECK_WINDOWS = [(9, 30, 9, 40), (14, 30, 14, 40)]

_NYSE_CAL = mcal.get_calendar('NYSE')


@functools.lru_cache(maxsize=8)
def _is_trading_day(date_str):
    """NYSE trading-day gate (weekends + market holidays) -- mirrors
    active_signals._is_trading_day (duplicated, not imported, for the same
    circular-import reason _SIGNAL_WINDOWS/_OPEN_CHECK_WINDOWS above are
    mirrored). This is the real chokepoint for it: active_signals.py's own
    gate only covers the daemon's two scan paths (_in_window, _scan_pinned_
    entry) -- a manual Slack "Manually Open" BUY or check_gap_resize's
    cancel+replace both route through check_order without ever touching
    those. Root-caused the 2026-07-26 ERY phantom-fill incident: no
    order-placement path checked weekday/holiday status anywhere, only
    (hour, minute) (Opus review finding, same session)."""
    return not _NYSE_CAL.schedule(start_date=date_str, end_date=date_str).empty

# Duplicate-submit guard: a second order for the same account+ticker+side+
# (approximately the same) quantity within this window is almost certainly a
# retry/double-call bug, not a real distinct order (signal windows are 15 min
# wide; genuine re-entries happen on a completely different bar, not
# seconds/minutes later). Matching on quantity too (not just
# account+ticker+side) is deliberate: a legitimately distinct second order
# for the same account+ticker+side -- e.g. Part 3's post-fill top-up buy,
# which fires within seconds of the primary buy fill -- almost always has a
# very different quantity, so it passes through untouched with no bypass
# flag needed. DUPLICATE_ORDER_QUANTITY_TOLERANCE_PCT (not an exact-equality
# check) accounts for a genuine retry computing a slightly different share
# count if it re-sizes off a moved price between the two attempts. Found
# 2026-07-21 while wiring the top-up to actually place a real broker order
# (previously it only wrote DB rows, so this guard was never exercised by it).
DUPLICATE_ORDER_WINDOW_SECS = 60
DUPLICATE_ORDER_QUANTITY_TOLERANCE_PCT = 5.0

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

# Statuses meaning the broker never actually accepted or executed the order --
# used by the duplicate-order guard's broker-truth check below. A local
# 'recent_orders' record with an outcome in one of these buckets must NOT
# block a retry as a duplicate: nothing dangerous is actually resting or
# filled at the broker, so re-submitting the same order is the correct thing
# to do, not a double-order risk.
_DUPLICATE_NOT_CONFIRMED_STATUSES = {"CANCELED", "EXPIRED", "REJECTED", "REPLACED"}


class SafetyViolation(Exception):
    pass


@dataclass
class AccountLimits:
    enabled: bool          # allowlist -- False blocks every order for this account
    notional_cap: float    # max $ per single order
    daily_order_cap: int   # max BUY orders per calendar day (SELLs don't count, 2026-07-25)
    dry_run: bool          # True: log what would happen, never call place_order
    account_type: str      # 'cash' or 'margin' (regular or IRA limited margin, same_day_block treats both the same)


# Placeholder per-account config -- tune before going live. Account-risk framing
# from the 2026-07-13 research session: Brokerage/SEP are large and need tight
# controls, Roth ($50k) is deliberate play money, IRA is fine/not small.
# account_type (2026-07-22): brokerage already has margin (ordinary taxable
# brokerage accounts do by default); sep/roth/ira are plain cash, confirmed by
# the user. The new (5th) limited-margin IRA funded this session isn't listed
# here yet -- not wired into .env/NICKNAMES (schwab_client.py) until its API
# token scope + compliance trading permission both clear, see the
# project_new_ira_account_status memory. Used only by same_day_block below,
# not a gate on whether an account can trade live at all -- the user's model
# is one account per ticker, growing over time as capital/liquidity needs it
# (fund a new trade in a new account rather than liquidating an existing
# one), so a blanket "cash accounts can never go live" rule was considered
# and explicitly rejected -- it would've locked automation out of every
# existing cash account.
ACCOUNTS = {
    "brokerage": AccountLimits(enabled=True, notional_cap=10_000, daily_order_cap=5,  dry_run=True, account_type="margin"),
    "sep":       AccountLimits(enabled=True, notional_cap=10_000, daily_order_cap=5,  dry_run=True, account_type="cash"),
    "roth":      AccountLimits(enabled=True, notional_cap=50_000, daily_order_cap=10, dry_run=True, account_type="cash"),
    "ira":       AccountLimits(enabled=True, notional_cap=75_000, daily_order_cap=10, dry_run=True, account_type="cash"),
    # New limited-margin IRA (2026-07-24 Friday test plan). Only ~$5k funded total,
    # and two real positions already staged, so remaining buying power is small --
    # notional_cap set conservatively low pending a real balance check.
    # dry_run=False 2026-07-24 -- the only account going live for today's real-order
    # test plan (docs/live_test_plan_2026-07-24.md). Every other account stays dry_run=True.
    "soxl_ira":  AccountLimits(enabled=True, notional_cap=800,    daily_order_cap=100, dry_run=False, account_type="margin"),
}

# Live-automation scope -- moved from a hardcoded Python literal to SCHWAB_AUTOMATION_TICKERS
# in .env (2026-07-19). Reasoning: this set is deployment-specific (which tickers *this*
# person's automation is trusted with), not something that belongs in shared/committed code --
# same rationale as SCHWAB_ACCOUNT_<NAME>/NICKNAMES already living in .env. Membership no
# longer shows up in `git log`, so every change is instead logged via
# signals_db.log_automation_scope_change (see sync_automation_scope() below) -- call that once
# at daemon startup, not here at import time, so a bare `import schwab_safety` (tests, scripts)
# never writes to the live DB as a side effect.
AUTOMATION_ENABLED_TICKERS = {
    t.strip() for t in os.environ.get("SCHWAB_AUTOMATION_TICKERS", "").split(",") if t.strip()
}

AUTOMATION_SCOPE_STATE_PATH = _STATE_DIR / "schwab_automation_scope.json"


def sync_automation_scope():
    """Compares the current AUTOMATION_ENABLED_TICKERS (read from .env at import
    time) against the last-known set persisted in AUTOMATION_SCOPE_STATE_PATH; if
    it changed, logs the old->new diff via signals_db (the only audit trail now
    that this set isn't in git) and updates the persisted state. Call once at
    daemon startup -- not on every poll loop, and not at module import time (see
    comment above AUTOMATION_ENABLED_TICKERS)."""
    old_tickers = set()
    if AUTOMATION_SCOPE_STATE_PATH.exists():
        try:
            old_tickers = set(json.loads(AUTOMATION_SCOPE_STATE_PATH.read_text()).get("tickers", []))
        except (json.JSONDecodeError, OSError):
            pass
    if old_tickers != AUTOMATION_ENABLED_TICKERS:
        signals_db.log_automation_scope_change(old_tickers, AUTOMATION_ENABLED_TICKERS)
        AUTOMATION_SCOPE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        AUTOMATION_SCOPE_STATE_PATH.write_text(json.dumps({"tickers": sorted(AUTOMATION_ENABLED_TICKERS)}))

def _now():
    """Seam for tests to monkeypatch -- the real signal windows only make
    sense at actual current wall-clock time, but tests need to simulate being
    inside/outside a window regardless of when they happen to run."""
    return datetime.now()


# Absolute backstop regardless of account config -- catches a misconfigured
# per-account cap before it reaches the API.
HARD_ORDER_CEILING = 100_000

# Fixed cash cushion required on top of a BUY's notional (automation_principles.md
# #7a) -- a small static buffer instead of exact real-time accounting for
# fees or a quote-to-fill price tick. Deliberately small: this is not the
# user's real safety margin -- the user separately keeps a much larger cash
# reserve (~$1,000) sitting in the account as their own operational habit, so
# cash_available already carries that headroom in practice. This constant
# only needs to cover per-order overage, not restate the user's reserve.
CASH_SAFETY_BUFFER = 200

# The user's own operational cash-reserve target per account (not a hard
# requirement -- see check_order's informational-only warning below). Lets
# the user know to add cash before the reserve actually runs out, rather than
# discovering it only once an order gets blocked.
CASH_RESERVE_WATERMARK = 1_000

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


NODE_AUTOMATION_PATH = _STATE_DIR / "schwab_node_automation.json"


def node_automation_enabled(node_id) -> bool:
    """Additive sibling of ticker_automation_enabled -- a node-level pause that
    can only make things MORE restrictive, never override a ticker-level
    pause (real gate is `ticker_automation_enabled(ticker) AND
    node_automation_enabled(wl_id)`, see docs/backlog_cache.md's wl_id
    refactor entry). node_id=None (the caller couldn't resolve a specific
    node, e.g. an ambiguous ticker+account match) defaults to True -- this is
    a supplementary, opt-in-to-pause toggle, not a fail-closed safety gate on
    its own; the ticker-level pause remains the real safety net when identity
    can't be resolved."""
    if node_id is None:
        return True
    if NODE_AUTOMATION_PATH.exists():
        try:
            state = json.loads(NODE_AUTOMATION_PATH.read_text())
            if str(node_id) in state:
                return bool(state[str(node_id)])
        except (json.JSONDecodeError, OSError):
            pass
    return True


def pause_node_automation(node_id, reason: str = ""):
    """Node-scoped sibling of pause_ticker_automation -- pauses just this
    watch_list node's automation, leaving sibling nodes on the same ticker
    (e.g. a different account) untouched."""
    NODE_AUTOMATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {}
    if NODE_AUTOMATION_PATH.exists():
        try:
            state = json.loads(NODE_AUTOMATION_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}
    state[str(node_id)] = False
    state[f"{node_id}_reason"] = reason
    NODE_AUTOMATION_PATH.write_text(json.dumps(state))


def resume_node_automation(node_id):
    NODE_AUTOMATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {}
    if NODE_AUTOMATION_PATH.exists():
        try:
            state = json.loads(NODE_AUTOMATION_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}
    state[str(node_id)] = True
    state.pop(f"{node_id}_reason", None)
    NODE_AUTOMATION_PATH.write_text(json.dumps(state))


NODE_BREAKER_PATH = _STATE_DIR / "schwab_node_breaker_state.json"
NODE_BREAKER_THRESHOLD = 3


def record_node_streak(ticker: str, account: str, kind: str, hit: bool, node_id=None):
    """Monitor-only node-level circuit breaker (docs/backlog_cache.md's
    'node-level auto-pause circuit breaker' item, the 3rd of 3 deferred
    design items from 2026-07-28 night). Tracks a consecutive hit/clean
    streak per (node, kind) and, once a streak crosses NODE_BREAKER_THRESHOLD,
    logs a coverage_event and posts one Slack alert -- but never calls
    pause_node_automation() itself. Deliberately phased: same rationale as
    _log_pre_action_state_verification (build the detection first, decide an
    auto-pause policy later once real trip data exists).

    kind is 'order_failures' (a real placement attempt raised SafetyViolation
    or failed at/after the broker) or 'reconciliation_mismatches' (a live-state
    reconciliation poll found the broker disagreeing with DB belief for this
    node's open position). hit=True extends the streak; hit=False (a clean
    attempt/poll) resets it to 0 and clears any prior trip, so a later real
    regression can re-alert instead of being permanently silenced by one old
    trip.

    node_id, if the caller already has it (e.g. an open position's wl_id),
    is used directly -- more precise than re-deriving it. Otherwise resolved
    via the same best-effort ticker+account lookup check_order uses, and
    silently no-ops if ambiguous/unresolvable, the same limitation
    node_automation_enabled already has.

    Fire-and-forget, like log_coverage_event/get_watch_list_node -- this is a
    pure side-channel monitor sitting directly in schwab_client.py's real
    order-placement control flow (an unconditional call between the safety
    check and the dry_run branch), so a state-file write failure (disk full,
    a concurrent-write race with the poll loop/Slack-handler thread, same
    unlocked-file pattern as NODE_AUTOMATION_PATH/TICKER_AUTOMATION_PATH)
    must never propagate and abort an otherwise-approved real order."""
    try:
        if node_id is None:
            node = signals_db.get_watch_list_node(ticker=ticker, account=account)
            node_id = node['id'] if node else None
        if node_id is None:
            return
        state = {}
        if NODE_BREAKER_PATH.exists():
            try:
                state = json.loads(NODE_BREAKER_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                state = {}
        node_state = state.setdefault(str(node_id), {})
        count_key = f"{kind}_streak"
        tripped_key = f"{kind}_tripped"
        just_tripped = False
        if hit:
            node_state[count_key] = node_state.get(count_key, 0) + 1
            if node_state[count_key] >= NODE_BREAKER_THRESHOLD and not node_state.get(tripped_key):
                node_state[tripped_key] = True
                just_tripped = True
        else:
            node_state[count_key] = 0
            node_state[tripped_key] = False
        NODE_BREAKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        NODE_BREAKER_PATH.write_text(json.dumps(state))

        if just_tripped:
            limits = ACCOUNTS.get(account)
            _mode = "live" if (limits and not limits.dry_run) else "dry_run"
            signals_db.log_coverage_event(
                "node_circuit_breaker_tripped", _mode, ticker=ticker, node_id=node_id,
                result="tripped", detail=f"kind={kind} streak={node_state[count_key]}"
            )
            import schwab_client  # local import: schwab_client imports this module at load time
            from signals_helpers import mode_tag  # local import: signals_helpers imports this module at load time
            schwab_client._post_message(
                f"\U0001F6A8 *{ticker}* ({account} · {mode_tag(account)}) node id={node_id} circuit breaker "
                f"TRIPPED: {node_state[count_key]} consecutive {kind.replace('_', ' ')} — "
                f"monitor-only, automation NOT paused. Worth a look before it repeats."
            )
    except Exception:
        pass


def auto_fill_detection_enabled(ticker: str) -> bool:
    """False unless a persisted per-ticker override has explicitly enabled it --
    opposite default from ticker_automation_enabled (see AUTO_FILL_DETECTION_PATH
    comment above)."""
    if AUTO_FILL_DETECTION_PATH.exists():
        try:
            state = json.loads(AUTO_FILL_DETECTION_PATH.read_text())
            if ticker in state:
                return bool(state[ticker])
        except (json.JSONDecodeError, OSError):
            pass
    return False


def enable_auto_fill_detection(ticker: str):
    """Called by the Slack per-ticker 'Enable Auto-Fill Detection' button handler."""
    AUTO_FILL_DETECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {}
    if AUTO_FILL_DETECTION_PATH.exists():
        try:
            state = json.loads(AUTO_FILL_DETECTION_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}
    state[ticker] = True
    AUTO_FILL_DETECTION_PATH.write_text(json.dumps(state))


def disable_auto_fill_detection(ticker: str):
    """Called by the Slack per-ticker 'Disable Auto-Fill Detection' button handler."""
    AUTO_FILL_DETECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {}
    if AUTO_FILL_DETECTION_PATH.exists():
        try:
            state = json.loads(AUTO_FILL_DETECTION_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}
    state[ticker] = False
    AUTO_FILL_DETECTION_PATH.write_text(json.dumps(state))


NODE_AUTO_FILL_DETECTION_PATH = _STATE_DIR / "schwab_node_auto_fill_detection.json"


def node_auto_fill_detection_enabled(node_id) -> bool:
    """Node-scoped sibling of auto_fill_detection_enabled -- this ticker-only
    flag was missed by the 2026-07-25/26 wl_id refactor (unlike
    ticker_automation_enabled, which got node_automation_enabled as an
    AND-gated additive layer). Without this, enabling fill-detection from one
    node's Slack row (e.g. a soxl_ira position) would silently also auto-detect
    fills for any other node sharing the same ticker (e.g. DPST/GDXU's
    live+research pairing) that was never actually vetted for it.
    Defaults False (node_id=None included) -- opposite direction from
    node_automation_enabled's fail-open default, since this flag *grants* extra
    trust rather than restricting it; missing node identity must not silently
    grant it. Real gate is `auto_fill_detection_enabled(ticker) AND
    node_auto_fill_detection_enabled(wl_id)` -- both layers must explicitly
    agree."""
    if node_id is None:
        return False
    if NODE_AUTO_FILL_DETECTION_PATH.exists():
        try:
            state = json.loads(NODE_AUTO_FILL_DETECTION_PATH.read_text())
            if str(node_id) in state:
                return bool(state[str(node_id)])
        except (json.JSONDecodeError, OSError):
            pass
    return False


def enable_node_auto_fill_detection(node_id):
    """Called by the Slack per-node 'Enable Auto-Fill Detection' button handler.
    Only sets the node-level flag -- the real gate is an AND of this and the
    ticker-level flag (enable_auto_fill_detection), which the caller must set
    separately (signals_handlers.handle_enable_auto_fill_detection does both)."""
    NODE_AUTO_FILL_DETECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {}
    if NODE_AUTO_FILL_DETECTION_PATH.exists():
        try:
            state = json.loads(NODE_AUTO_FILL_DETECTION_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}
    state[str(node_id)] = True
    NODE_AUTO_FILL_DETECTION_PATH.write_text(json.dumps(state))


def disable_node_auto_fill_detection(node_id):
    """Only clears this node's flag -- deliberately does not touch the
    ticker-level flag, so disabling one node doesn't affect a sibling node on
    the same ticker that's separately enabled."""
    NODE_AUTO_FILL_DETECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {}
    if NODE_AUTO_FILL_DETECTION_PATH.exists():
        try:
            state = json.loads(NODE_AUTO_FILL_DETECTION_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}
    state[str(node_id)] = False
    NODE_AUTO_FILL_DETECTION_PATH.write_text(json.dumps(state))


def _open_locked():
    """Opens STATE_PATH for read+write under an exclusive flock, creating it
    first if needed. Caller must close() when done (releases the lock)."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.touch(exist_ok=True)
    f = open(STATE_PATH, "r+")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def _live_ticker_accounts() -> dict:
    """ticker -> set of assigned account nicknames, for watchlist rows
    currently in 'live' mode -- queried fresh (not cached) since mode/account
    assignment can change during a running day (e.g. AGQ moved to research
    mid-session 2026-07-13). A set, not a single value: 2+ concurrent live
    nodes for the same ticker in *different* accounts is a real, intended
    configuration (e.g. the same ticker live in two separate accounts for
    blast-radius containment, see docs/backlog_cache.md's wl_id refactor
    entry) -- collapsing to one account per ticker would hard-reject the
    second node's real orders as 'assigned to the wrong account'."""
    accounts_by_ticker: dict = {}
    for row in signals_db.get_watchlist():
        # Exclude a NULL/missing account -- it can never equal a real account
        # string passed into check_order, and letting it into the set risks
        # sorted() raising TypeError (None vs str) when check_order formats
        # the rejection message below.
        if row["mode"] == "live" and row["account"]:
            accounts_by_ticker.setdefault(row["ticker"], set()).add(row["account"])
    return accounts_by_ticker


def _all_orders(account: str) -> list:
    """Schwab's full live order book for the account, every status -- a real
    API call, not local state, since local tracking could drift or miss an
    order placed outside our own code (e.g. directly in Schwab's UI, as
    happened during today's settlement test). Unlike _open_orders below, this
    includes terminal statuses (FILLED/CANCELED/REJECTED/etc) -- needed by the
    duplicate-order guard's broker-truth check, which must see a rejected/
    canceled prior attempt too, not just resting ones."""
    import schwab_client  # local import: schwab_client imports this module at load time
    account_hash = schwab_client._resolve_account_hashes()[account]
    r = schwab_client._get_client().get_orders_for_account(account_hash)
    r.raise_for_status()
    return r.json()


def _log_pre_action_state_verification(account, ticker, node_id, mode, side, local_shares):
    """Detection-only pre-action live-state check (2026-07-29 backlog item,
    deferred from the 2026-07-28 fix session): compares the real broker
    position against what our own DB believes, at the exact moment a real
    order is about to be considered -- not a periodic post-hoc reconciliation
    (check_live_state_reconciliation already does that), but the actual
    decision point. Deliberately does NOT block or raise on a mismatch yet --
    same phased-rollout reasoning as check_live_state_reconciliation's
    original design: log real data first, decide on a tolerance/blocking
    policy once there's evidence of what mismatches actually look like in
    practice, rather than guess. local_shares is the caller's own belief
    (None if it has none) -- comparison, not a authority check; the broker is
    always ground truth (automation_principles.md #1).
    Never raises past a fetch failure into the real order-approval flow --
    this is observability, not a gate, so a network blip here must not be
    able to block a legitimate real order."""
    try:
        import schwab_client  # local import: schwab_client imports this module at load time
        real_shares = schwab_client.get_real_position(account, ticker)
    except Exception as e:
        signals_db.log_coverage_event(
            "pre_action_state_verification", mode, ticker=ticker, node_id=node_id,
            result="fetch_failed", detail=f"side={side}: {e}")
        return
    if local_shares is None:
        matched = real_shares == 0
    else:
        matched = abs(real_shares - local_shares) < 1e-6
    signals_db.log_coverage_event(
        "pre_action_state_verification", mode, ticker=ticker, node_id=node_id,
        result="match" if matched else "mismatch",
        detail=f"side={side} real_shares={real_shares:g} local_shares={local_shares}")


def _open_orders(account: str) -> list:
    """Non-terminal orders only, for the concurrent-resting-order guards
    below (_has_open_order / _has_open_buy_order_in_account)."""
    return [o for o in _all_orders(account) if o.get("status") not in _OPEN_ORDER_STATUSES_EXCLUDED]


def _log_guard_input(account, ticker, side, orders, replacing_order_id):
    """Logs the raw resting-order snapshot a dup-order guard is about to
    decide against -- every guard branch below already logs its *outcome*
    (coverage_events), but never the broker state that outcome was decided
    from. Found 2026-07-28: GDXU's real 07-27 gap_resize BUY replace should
    have hit the same self-block SH's SELL replace did (a resting order for
    the same ticker existed 8 real hours before the check), and didn't --
    with no record of what _open_orders() actually returned at that moment,
    it's unexplainable after the fact. Temporary/cheap diagnostic, safe to
    remove once the next staged gap-resize test either reproduces or clears
    this. Import kept local/lazy to avoid a schwab_safety<->signals_config
    import-order dependency at module load time."""
    try:
        import signals_config as cfg
        snapshot = [{"orderId": o.get("orderId"), "status": o.get("status"),
                     "orderType": o.get("orderType")} for o in orders]
        with open(cfg.VERBOSE_LOG_PATH, "a") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [guard_input] {side} {ticker} "
                    f"account={account} replacing_order_id={replacing_order_id} orders={snapshot}\n")
    except Exception:
        pass


def _has_open_order(orders: list, ticker: str, exclude_order_id: int | None = None) -> bool:
    """True if any resting order in `orders` (see _open_orders) is for this
    ticker, regardless of side.
    Deliberately ticker(+account)-keyed, not wl_id-keyed, and not part of the
    wl_id refactor (docs/backlog_cache.md) -- the real broker's order book has
    no concept of wl_id, only ticker symbol per leg. Permanent policy
    constraint: two live nodes on the same ticker in the *same* account still
    cannot both hold resting orders, regardless of that refactor (two live
    nodes on the same ticker in *different* accounts is fine -- see
    _live_ticker_accounts).
    exclude_order_id skips the exact order a replace_order call is about to
    swap out -- without this, an atomic-replace call (which checks before the
    old order is actually gone) always sees its own target order as "already
    resting" and self-blocks (found 2026-07-28, real: SH's TIME/SL exit was
    permanently blocked by its own protective SL order for 4 days)."""
    for o in orders:
        if exclude_order_id is not None and o.get("orderId") == exclude_order_id:
            continue
        legs = o.get("orderLegCollection", [])
        if any(leg.get("instrument", {}).get("symbol") == ticker for leg in legs):
            return True
    return False


def _has_open_buy_order_in_account(orders: list, ticker: str) -> str | None:
    """Ticker symbol of another resting BUY order anywhere in this account (or
    None) -- used to block a second concurrent BUY into the same account.
    Schwab does not reserve buying power for a resting order (see
    _has_open_order's docstring context above), so the cash-availability
    check in check_order compares each BUY's notional against the same
    undecremented account balance -- a flat buffer alone can't cover two
    simultaneous BUYs competing for the same cash once a second live ticker
    ever shares an account (not reachable today: every live ticker has its
    own account, see _live_ticker_accounts, but not structurally enforced)."""
    for o in orders:
        for leg in o.get("orderLegCollection", []):
            if leg.get("instruction") == "BUY" and leg.get("instrument", {}).get("symbol") != ticker:
                return leg.get("instrument", {}).get("symbol")
    return None


def _has_open_sell_order(orders: list, ticker: str, exclude_order_id: int | None = None) -> bool:
    """True if any resting SELL order for this exact ticker already exists.
    Unlike _has_open_order (any side blocks a second BUY), this only matches
    same-side (SELL) orders -- an unrelated resting BUY for this ticker must
    not block closing a position, same asymmetry the module already applies
    elsewhere (same-day-block, notional-cap exemption) for SELL.
    exclude_order_id -- see _has_open_order's docstring; same rationale,
    for the SELL-side replace calls (_attempt_automated_exit_sell's SL/TIME
    exit, _attempt_automated_sell's TRAIL arm)."""
    for o in orders:
        if exclude_order_id is not None and o.get("orderId") == exclude_order_id:
            continue
        for leg in o.get("orderLegCollection", []):
            if leg.get("instruction") == "SELL" and leg.get("instrument", {}).get("symbol") == ticker:
                return True
    return False


def _broker_confirms_order(orders: list, ticker: str, side: str, quantity: int) -> bool:
    """True if the real order book (all statuses, see _all_orders) has an
    order for this exact (ticker, side, quantity within tolerance) that the
    broker genuinely accepted -- i.e. not CANCELED/EXPIRED/REJECTED/REPLACED.
    Used to confirm a local 'recent_orders' duplicate candidate against ground
    truth before blocking a retry (automation_principles.md #1):
    approve_and_record() writes the local record before the real place_order
    call happens, so a failed/rejected broker call still looks like a
    submitted duplicate to the old purely-local check -- this lets a
    legitimate retry through when nothing actually reached the broker."""
    for o in orders:
        if o.get("status") in _DUPLICATE_NOT_CONFIRMED_STATUSES:
            continue
        for leg in o.get("orderLegCollection", []):
            if leg.get("instruction") != side or leg.get("instrument", {}).get("symbol") != ticker:
                continue
            qty = leg.get("quantity", 0)
            if abs(qty - quantity) <= DUPLICATE_ORDER_QUANTITY_TOLERANCE_PCT / 100 * max(qty, quantity, 1):
                return True
    return False


def check_order(
    account: str, ticker: str, quantity: int, price: float, side: str, counts: dict | None = None,
    is_gap_correction: bool = False, is_protective: bool = False, replacing_order_id: int | None = None,
) -> None:
    """Raises SafetyViolation if the order should not proceed. `counts`, if
    given, is used for the daily-cap/burst-cap/duplicate checks instead of
    re-reading the state file -- lets approve_and_record() validate against
    the exact snapshot it's about to increment, under one lock, instead of a
    separate read. is_protective exempts a post-fill top-up BUY from the
    daily_order_cap check (every other guard still applies) -- found live
    2026-07-24 (LABU) that daily_order_cap counts every placement uniformly,
    so unrelated earlier entries can starve a legitimately protective
    follow-on action, leaving a brand-new fill unprotected purely from
    order-count bookkeeping, not real risk.
    (2026-07-25: SELL -- including a stop-loss placement -- is now
    unconditionally exempt from this cap regardless of is_protective, so this
    flag no longer does anything on that path; kept for the BUY-side top-up
    case and for is_protective's other callers, not removed to avoid
    unnecessarily changing call sites.)
    replacing_order_id: the real broker order_id an atomic replace_order call
    is about to swap out (see schwab_client.replace_equity_order_with_market /
    replace_order_with_trailing_sell). Excluded from the resting-order dup
    checks below -- without this, a replace call always sees its own target
    order still resting (the cancel hasn't happened yet at check time) and
    self-blocks every single attempt. Found 2026-07-28: SH's automated SL/
    TIME exit was blocked 100% of the time for 4 real days by its own
    protective SL order for exactly this reason."""
    if kill_switch_engaged():
        _limits = ACCOUNTS.get(account)
        _mode = "live" if (_limits and not _limits.dry_run) else "dry_run"
        signals_db.log_coverage_event("kill_switch_block", _mode, ticker=ticker, result="blocked",
                                       detail=kill_switch_reason())
        raise SafetyViolation(f"global kill switch engaged ({kill_switch_reason()})")

    limits = ACCOUNTS.get(account)
    if limits is None:
        raise SafetyViolation(f"unknown account '{account}' -- not in the allowlist")
    if not limits.enabled:
        raise SafetyViolation(f"account '{account}' is disabled in the allowlist")
    _mode = "dry_run" if limits.dry_run else "live"
    # KNOWN LIMITATION (docs/backlog_cache.md's wl_id refactor entry): _node_id
    # is re-derived via a ticker+account best-effort lookup because no caller
    # in the schwab_client chain threads a real wl_id through yet -- that
    # plumbing (schwab_client's 6 place_* functions -> approve_and_record ->
    # here) was scoped as its own follow-up, not done this session.
    # get_watch_list_node() returns None on an ambiguous match (2+ nodes same
    # ticker+account), so node_automation_enabled(None) below silently
    # defaults to True in exactly that case -- a node-level pause is a no-op
    # for two same-account nodes on one ticker until this is threaded properly.
    _node = signals_db.get_watch_list_node(ticker=ticker, account=account)
    _node_id = _node['id'] if _node else None

    ticker_accounts = _live_ticker_accounts()
    if ticker not in ticker_accounts:
        raise SafetyViolation(f"'{ticker}' is not a live-mode ticker on the active watchlist")
    if account not in ticker_accounts[ticker]:
        raise SafetyViolation(
            f"'{ticker}' is not assigned to account '{account}' "
            f"(assigned accounts: {sorted(ticker_accounts[ticker])})"
        )
    if len(ticker_accounts[ticker]) > 1:
        signals_db.log_coverage_event(
            "two_nodes_same_ticker_diff_accounts", _mode, ticker=ticker, node_id=_node_id, result="allowed",
            detail=f"account={account} of {sorted(ticker_accounts[ticker])}")
    if not node_automation_enabled(_node_id):
        signals_db.log_coverage_event("node_level_automation_pause", _mode, ticker=ticker, node_id=_node_id,
                                       result="blocked")
        raise SafetyViolation(f"node id={_node_id} for '{ticker}' has automation paused")
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
    # left out). Margin accounts (regular or IRA limited margin) don't have
    # this settlement restriction (2026-07-20 finding, resolved 2026-07-22)
    # -- skip for them.
    if side == "BUY" and limits.account_type == "cash" and signals_db.closed_today(ticker):
        signals_db.log_coverage_event(
            "same_day_block", _mode, ticker=ticker, node_id=_node_id, result="blocked",
            detail=f"account={account} account_type={limits.account_type}"
        )
        raise SafetyViolation(
            f"'{ticker}' was sold today -- same-day re-buy risks a cash-account good-faith violation"
        )
    elif side == "BUY" and limits.account_type == "margin" and signals_db.closed_today(ticker):
        signals_db.log_coverage_event(
            "same_day_block", _mode, ticker=ticker, node_id=_node_id, result="skipped_margin_account",
            detail=f"account={account} account_type={limits.account_type}"
        )

    if side == "BUY":
        _local_pos = signals_db.get_open_position_for_account(ticker, account)
        _log_pre_action_state_verification(
            account, ticker, _node_id, _mode, "BUY",
            _local_pos['shares'] if _local_pos else None)
        # Existing-position guard (2026-08-02): closes the real gap confirmed
        # 2026-07-24 -- Schwab doesn't decrement account balance for a resting
        # order (two real resting TRAILING_STOP BUYs left get_account_balance
        # completely unchanged), so notional_cap (per-order) and the cash check
        # (reads that same undecremented balance) can't by themselves stop a
        # second real BUY from being approved for a ticker this account
        # already holds -- the resting-order guards below only cover the
        # window before the first order fills, not after. is_protective is the
        # one sanctioned exception: _reconcile_fill's post-fill top-up is
        # completing THIS SAME position's sizing, not adding a new one.
        # KNOWN LIMITATION (found by review, 2026-08-02, same shape as the
        # _node_id ambiguity noted above): get_open_position_for_account is
        # ticker+account-keyed, not wl_id-keyed, so if 2 live nodes ever
        # shared a ticker+account, this would wrongly block the SECOND node's
        # genuine first entry as if it were a duplicate of the first node's
        # position. Not reachable today (verified: no such pairing exists on
        # the live watchlist) -- fixing it properly needs the same wl_id-
        # threaded-through-schwab_client plumbing already flagged as a
        # separate follow-up, not done here.
        if _local_pos and not is_protective:
            signals_db.log_coverage_event(
                "buy_blocked_position_exists", _mode, ticker=ticker, node_id=_node_id, result="blocked",
                detail=f"account={account} held_shares={_local_pos['shares']:g}"
            )
            raise SafetyViolation(
                f"'{ticker}' already has an open position ({_local_pos['shares']:g} shares) in "
                f"'{account}' -- refusing a second real BUY (not a protective top-up)"
            )
        orders = _open_orders(account)
        _log_guard_input(account, ticker, "BUY", orders, replacing_order_id)
        if _has_open_order(orders, ticker, exclude_order_id=replacing_order_id):
            signals_db.log_coverage_event("dup_order_blocked", _mode, ticker=ticker, node_id=_node_id, result="blocked_same_ticker")
            raise SafetyViolation(
                f"'{ticker}' already has an open/working order in '{account}' -- refusing a second "
                f"concurrent BUY (Schwab doesn't reserve buying power for a resting order, so nothing "
                f"else stops these from stacking)"
            )
        other_ticker = _has_open_buy_order_in_account(orders, ticker)
        if other_ticker:
            signals_db.log_coverage_event(
                "second_ticker_buy_blocked", _mode, ticker=ticker, node_id=_node_id, result="blocked",
                detail=f"account={account} other_ticker={other_ticker}"
            )
            raise SafetyViolation(
                f"account '{account}' already has a resting BUY order for '{other_ticker}' -- refusing "
                f"a second concurrent BUY into the same account (Schwab doesn't reserve buying power "
                f"for a resting order, so two BUYs in one account can both pass a cash check against "
                f"the same undecremented balance -- automation_principles.md #1)"
            )

    # Resting-SELL guard (2026-07-22, symmetric to the BUY-side guard above):
    # found via Opus review that SELL had no such check at all, which is what
    # let a real bug (a stale trail_state overwrite re-arming the trailing
    # exit on the next bar) place a second live trailing-sell order for the
    # same shares -- an oversell risk if both then fill. Same-ticker only,
    # not account-wide like the BUY guard: an unrelated resting BUY for this
    # ticker must not block closing a position.
    if side == "SELL":
        _presell_pos = signals_db.get_open_position_for_account(ticker, account)
        _log_pre_action_state_verification(
            account, ticker, _node_id, _mode, "SELL",
            _presell_pos['shares'] if _presell_pos else None)
        orders = _open_orders(account)
        _log_guard_input(account, ticker, "SELL", orders, replacing_order_id)
        if _has_open_sell_order(orders, ticker, exclude_order_id=replacing_order_id):
            signals_db.log_coverage_event("dup_sell_order_blocked", _mode, ticker=ticker, node_id=_node_id, result="blocked")
            raise SafetyViolation(
                f"'{ticker}' already has a resting SELL order in '{account}' -- refusing a second "
                f"concurrent SELL (prevents two live exit orders stacking for the same shares)"
            )
        # Real position-size bound, added 2026-07-24 alongside exempting SELL
        # from notional_cap (see above) -- flagged by Opus review: with
        # notional_cap gone, nothing on our side bounded SELL quantity at all
        # (oversell protection relied entirely on Schwab's own rejection).
        # This is a narrower, more principled bound than notional_cap ever
        # was: it can't false-positive-block a legitimate large exit, but it
        # does catch our own inflated-share-count bugs (e.g. a top-up that
        # recorded shares before the real fill was confirmed) before they
        # reach the broker as a would-be short.
        pos = _presell_pos
        # Fail closed when no local position row is found at all (automation_
        # principles.md #2) -- this branch used to only ever fire when `pos`
        # was truthy, silently skipping the bound entirely with no local
        # position on file, the exact "fail-open on a missing local position
        # row" gap flagged in the 2026-07-31 audit's still-open list. A first
        # fix attempt broke ~14 tests; investigation (2026-07-31, later
        # session) found those were all guard-isolation tests exercising
        # OTHER check_order guards (kill switch, cash, window, dup-order) via
        # a SELL call with no position ever seeded -- a test-fixture gap, not
        # evidence this guard is unsafe in production (every real SELL call
        # site resolves a position via get_open_position_by_wl_id/
        # get_position_by_id before ever reaching schwab_client, and the same
        # account value flows straight through, so a live position should
        # always be found here). Verified against the real trading_live.db:
        # zero open_positions rows with a NULL/empty account (the one
        # plausible legacy-data gap) as of this fix. gap_resize's MARKET-buy
        # replacement path is exempt (is_gap_correction doesn't apply to SELL
        # here -- N/A, this branch is SELL-only) -- no other legitimate
        # no-position SELL path was found.
        if pos is None:
            signals_db.log_coverage_event(
                "sell_exceeds_position_blocked", _mode, ticker=ticker, node_id=_node_id, result="blocked_no_position",
                detail=f"quantity={quantity:g}"
            )
            raise SafetyViolation(
                f"SELL {quantity:g} {ticker} in '{account}' -- no open position on file for this "
                f"(ticker, account), refusing to guess a bound and place an unverified SELL"
            )
        if quantity > pos['shares'] * 1.001:  # tolerance for float share counts
            signals_db.log_coverage_event(
                "sell_exceeds_position_blocked", _mode, ticker=ticker, node_id=_node_id, result="blocked",
                detail=f"quantity={quantity:g} held={pos['shares']:g}"
            )
            raise SafetyViolation(
                f"SELL {quantity:g} {ticker} exceeds the {pos['shares']:g} shares on file for "
                f"'{account}' -- refusing a would-be short"
            )

    # Trading-day gate, BUY only, unconditional (including gap-correction --
    # an overnight gap only exists ahead of a real trading day, so there's no
    # legitimate reason to exempt it the way the window gate below does).
    if side == "BUY":
        now = _now()
        if not _is_trading_day(now.strftime('%Y-%m-%d')):
            raise SafetyViolation(
                f"BUY blocked -- {now.strftime('%Y-%m-%d')} is not an NYSE trading day"
            )

    # Signal-window time gate, BUY only (see _SIGNAL_WINDOWS comment above).
    # Skipped for a gap-correction replacement order (Part 3, branch B) -- that
    # order is a cancel+replace of an already-approved pending buy, running in
    # the pre-open _GAP_CHECK_WINDOW deliberately outside the normal signal
    # windows; every other guard below still applies.
    # Also skipped for is_protective (2026-08-01, real live bug found via the
    # Trade-Flow Accountability Grid: the post-fill top-up BUY in
    # signals_notify._reconcile_fill is is_protective=True, and its
    # is_gap_correction passthrough only covers the ONE narrow case where the
    # triggering fill itself came from check_gap_resize -- the general case
    # (any normal trailing-buy fill landing outside the narrow signal windows,
    # which is routine: a resting order can fill hours after the signal
    # fired) was still gated, so the top-up was structurally blocked for
    # exactly the delayed-fill case it exists to handle. Confirmed live: both
    # real non-daily-cap top_up failures on file (RETL 2026-07-29 18:57, LABD
    # 2026-07-31 00:19) were this exact rejection -- 100% real-world failure
    # rate, caught only because the Accountability Grid flagged this scenario
    # as 'live-attempt-failed' (fired for real, never with a good outcome),
    # not by any test. A top-up isn't a fresh signal-driven entry -- it's
    # completing an already-approved one, same reasoning already applied to
    # is_gap_correction above.
    if side == "BUY" and not is_gap_correction and not is_protective:
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
    # notional_cap bounds new risk-adding exposure (BUY) -- a SELL closes an
    # existing position instead of opening one, so a real position that grew
    # past the cap (price appreciation, or was pre-staged larger than the cap)
    # would otherwise have its automated exit permanently dead-ended. Confirmed
    # live 2026-07-24: a real armed SPY trailing-sell (soxl_ira, cap $800) was
    # blocked at $2,227 notional, with no way to ever clear since the position
    # itself never shrinks below the cap on its own. HARD_ORDER_CEILING above
    # still applies to both sides as an absolute sanity backstop.
    if side == "BUY" and notional > limits.notional_cap:
        raise SafetyViolation(
            f"order notional ${notional:,.0f} ({ticker} x{quantity}) exceeds {account} cap ${limits.notional_cap:,.0f}"
        )

    # Real cash-availability check, BUY only (automation_principles.md #1/#2/#7a)
    # -- notional_cap above is a static ceiling, not a check against what the
    # account can actually afford right now. Not bypassed by is_gap_correction:
    # that flag exists for the signal-window timing gate specifically, this is
    # a hard financial constraint. Fails closed (blocks the order) if the
    # balance fetch itself errors -- an unchecked order is exactly the risk
    # this check exists to prevent.
    if side == "BUY":
        import schwab_client  # local import: schwab_client imports this module at load time
        try:
            cash_available = schwab_client.get_account_balance(account)
        except Exception as e:
            signals_db.log_coverage_event(
                "cash_check", _mode, ticker=ticker, node_id=_node_id, result="failed_closed", detail=str(e)
            )
            raise SafetyViolation(f"could not verify '{account}' cash balance, blocking order: {e}")
        required = notional + CASH_SAFETY_BUFFER
        if cash_available < required:
            signals_db.log_coverage_event(
                "cash_check", _mode, ticker=ticker, node_id=_node_id, result="blocked_insufficient",
                detail=f"required=${required:,.0f} available=${cash_available:,.0f}"
            )
            raise SafetyViolation(
                f"order notional ${notional:,.0f} + ${CASH_SAFETY_BUFFER:,.0f} cash buffer = "
                f"${required:,.0f} required, but '{account}' only has ${cash_available:,.0f} available"
            )
        signals_db.log_coverage_event(
            "cash_check", _mode, ticker=ticker, node_id=_node_id, result="passed",
            detail=f"required=${required:,.0f} available=${cash_available:,.0f}"
        )
        # Informational only, not blocking (automation_principles.md #4) -- the
        # user keeps CASH_RESERVE_WATERMARK as their own operational cash
        # reserve per account; this doesn't gate the order, just flags that
        # the account is already running thin so cash can be topped up before
        # it becomes a real problem.
        if cash_available < CASH_RESERVE_WATERMARK:
            try:
                schwab_client._post_message(
                    f"\U0001F4B0 '{account}' has ${cash_available:,.0f} cash available, below your "
                    f"${CASH_RESERVE_WATERMARK:,.0f} reserve target -- consider adding cash "
                    f"(not blocking this {ticker} BUY)"
                )
            except Exception:
                pass

    if counts is None:
        with _open_locked() as f:
            counts = json.loads(f.read() or "{}")
    today = counts.get(str(date.today()), {})
    count = today.get(account, 0)
    # BUY-only, matching the increment above (and notional_cap's precedent, 2026-07-24) --
    # a SELL no longer contributes to this count, so it must not be blocked by it either;
    # otherwise a real exit could be blocked by a cap only BUYs exhausted (found by Opus
    # review, 2026-07-25 -- a blocked automated trailing-sell after its stop-loss was
    # already cancelled would leave the position with zero broker-side protection).
    if side == "BUY" and count >= limits.daily_order_cap:
        if is_protective:
            signals_db.log_coverage_event(
                "daily_cap_protective_bypass", _mode, ticker=ticker, node_id=_node_id, result="allowed",
                detail=f"account={account} count={count} cap={limits.daily_order_cap} side={side}"
            )
        else:
            raise SafetyViolation(f"account '{account}' has hit its daily order cap ({limits.daily_order_cap})")

    recent = [t for t in counts.get("recent_order_timestamps", []) if time.time() - t < 60]
    if len(recent) >= GLOBAL_ORDERS_PER_MINUTE:
        raise SafetyViolation(
            f"global burst cap hit ({len(recent)} orders across all accounts in the last minute, "
            f"max {GLOBAL_ORDERS_PER_MINUTE})"
        )

    for o in counts.get("recent_orders", []):
        prior_qty = o.get("quantity")
        qty_matches = (
            prior_qty is not None
            and abs(prior_qty - quantity) <= DUPLICATE_ORDER_QUANTITY_TOLERANCE_PCT / 100 * max(prior_qty, quantity)
        )
        if not (
            o["account"] == account and o["ticker"] == ticker and o["side"] == side
            and qty_matches
            and time.time() - o["ts"] < DUPLICATE_ORDER_WINDOW_SECS
        ):
            continue
        # approve_and_record() writes this local record *before* the real
        # place_order call happens, so it can't tell a genuine resubmission
        # from a retry after a failed/rejected broker call. For a real
        # (non-dry_run) account, confirm against Schwab's real order book
        # before blocking -- ground truth, not a local heuristic
        # (automation_principles.md #1). Dry-run accounts have no broker book
        # to check against, so keep the pure local-record behavior.
        if not limits.dry_run and not _broker_confirms_order(_all_orders(account), ticker, side, quantity):
            signals_db.log_coverage_event(
                "dup_order_retry_after_failure", _mode, ticker=ticker, node_id=_node_id, result="allowed_retry",
                detail=f"side={side} qty={quantity}"
            )
            continue
        signals_db.log_coverage_event(
            "dup_order_window_blocked", _mode, ticker=ticker, node_id=_node_id, result="blocked",
            detail=f"side={side} qty={quantity} prior_qty={prior_qty:g}"
        )
        raise SafetyViolation(
            f"duplicate order: {side} {quantity} {ticker} in {account} already submitted "
            f"{prior_qty:g} shares {time.time() - o['ts']:.0f}s ago "
            f"(within {DUPLICATE_ORDER_WINDOW_SECS}s window, {DUPLICATE_ORDER_QUANTITY_TOLERANCE_PCT}% tolerance)"
        )


def approve_and_record(
    account: str, ticker: str, quantity: int, price: float, side: str, is_gap_correction: bool = False,
    is_protective: bool = False, replacing_order_id: int | None = None,
) -> bool:
    """Call immediately before placing a real order. Raises SafetyViolation if
    blocked; otherwise records the order against the daily cap, the global
    per-minute burst cap, and the duplicate-order window, and returns whether
    the account is in dry_run mode (caller must skip the real API call if so).
    Checks and increments happen under the same file lock so two concurrent
    callers can't both slip past a cap. is_gap_correction bypasses only the
    signal-window time gate (see check_order) -- Part 3, branch B.
    is_protective bypasses only the daily_order_cap check, for a post-fill
    top-up BUY -- see check_order's docstring (SELL, including a stop-loss
    placement, is unconditionally exempt from this cap since 2026-07-25
    regardless of is_protective).
    replacing_order_id -- see check_order's docstring; threaded through by
    replace_equity_order_with_market/replace_order_with_trailing_sell so an
    atomic replace call doesn't self-block on the exact order it's replacing."""
    with _open_locked() as f:
        counts = json.loads(f.read() or "{}")
        check_order(account, ticker, quantity, price, side, counts=counts,
                    is_gap_correction=is_gap_correction, is_protective=is_protective,
                    replacing_order_id=replacing_order_id)
        key = str(date.today())
        today = counts.setdefault(key, {})
        if side == "BUY":
            today[account] = today.get(account, 0) + 1
        recent = [t for t in counts.get("recent_order_timestamps", []) if time.time() - t < 60]
        recent.append(time.time())
        counts["recent_order_timestamps"] = recent
        recent_orders = [
            o for o in counts.get("recent_orders", [])
            if time.time() - o["ts"] < DUPLICATE_ORDER_WINDOW_SECS
        ]
        recent_orders.append({"account": account, "ticker": ticker, "side": side,
                               "quantity": quantity, "ts": time.time()})
        counts["recent_orders"] = recent_orders
        f.seek(0)
        f.truncate()
        f.write(json.dumps(counts))
    return ACCOUNTS[account].dry_run
