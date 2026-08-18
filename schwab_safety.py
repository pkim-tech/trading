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
import threading
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
    trading_enabled: bool  # False: log what would happen, never call place_order (renamed
                           # from dry_run, 2026-08-1x, alongside the node-level state
                           # collapse -- reads as an unambiguous boolean gate:
                           # real_order_allowed = (node.state == 'live') AND account.trading_enabled
    cash_settlement_type: str  # 'cash' or 'margin' -- SAME-DAY CASH-SETTLEMENT semantics only
                                # (same_day_block below). Renamed from account_type 2026-08-11:
                                # that field used to also gate real margin-borrowing eligibility
                                # for add-on legs, a conflation that was wrong for soxl_ira
                                # (limited margin: same-day settlement, but cannot actually
                                # borrow) -- see margin_capable below, the split-out field.
    margin_capable: bool = False  # real margin-borrowing eligibility (add-on legs). NOT the
                                   # same question as cash_settlement_type=='margin' -- see above.
    margin_floor: float = 0.0
    # How far BELOW $0 the cash_check below may let cash_available go, for an
    # account that can genuinely borrow on margin -- the check floors at $0
    # for every account today (2026-08-07), which is correct for a cash
    # account and a LIMITED-margin IRA (soxl_ira: same-day settlement only,
    # cannot actually borrow) but wrong for a real full-margin account, which
    # can legitimately run cash negative up to its real margin capacity.
    # Real value for `brokerage` (the one genuine full-margin account)
    # intentionally left at the default 0.0 -- not yet known (portfolio
    # construction hasn't happened, and the account already carries some real
    # leverage as of 2026-08-07); set explicitly once a real number exists.
    # brokerage is trading_enabled=False today, so this has no live effect
    # either way yet.
    is_tax_advantaged: bool = False  # real IRA/Roth/SEP tax status -- explicit, not guessed
                                      # from the alias string (see signals_db._is_tax_advantaged_
                                      # account, replaces 3 substring guards that admitted they'd
                                      # fail open on a future account name like "hsa"/"401k").


class _AccountsDict(dict):
    """DB-backed replacement for the old hardcoded ACCOUNTS literal, loaded
    lazily from signals_db's `accounts` table on first access (mirrors
    schwab_client._account_hashes's lazy-cache-once-per-process pattern). A
    real dict subclass, not a proxy, so every existing call-site shape keeps
    working unchanged -- including test monkeypatches directly on the
    AccountLimits instances stored here (e.g.
    ACCOUNTS['roth'].trading_enabled = True). Every dict read method real
    callers use is overridden to trigger the lazy load -- NOT just
    __getitem__/get(): scripts/check_account_availability.py uses .items(),
    scripts/check_untracked_positions.py uses .keys(),
    scripts/sim_1m_four_bucket_portfolio.py uses `in ACCOUNTS` -- missing any
    of these would silently look like zero configured accounts instead of
    erroring (found in review before this shipped).

    Self-invalidating on signals_config.DB_PATH change, tracked via
    _loaded_db_path -- deliberately NOT the same no-invalidation-ever
    posture as schwab_client._account_hashes, because ~69 test files swap
    DB_PATH per-test and relying on every one of them to also call
    reload_accounts() in the right order (after their own ensure_tables()
    call) is exactly the kind of cross-test staleness bug this project's own
    history shows Opus review catches. A real daemon process only ever has
    one DB_PATH for its whole lifetime, so this check is a no-op cost there."""

    _loaded = False
    _loaded_db_path = None
    _reload_lock = threading.Lock()  # class attribute -- one lock shared across the singleton ACCOUNTS instance

    def _reload_locked(self):
        """Assumes _reload_lock is already held -- never call directly,
        only via _ensure_loaded()."""
        conn = signals_db._conn()
        try:
            rows = list(conn.execute(
                "SELECT * FROM accounts WHERE retired_at IS NULL"
            ).fetchall())
        finally:
            conn.close()
        if not rows:
            # Never cache an empty result as if it were real data -- an
            # empty `accounts` table (migration hasn't run against this DB
            # yet, or a real transaction-ordering race) must fail loud, not
            # make every account look unconfigured. A genuinely-zero-account
            # deployment isn't a real scenario for this codebase.
            raise RuntimeError(
                "accounts table has no non-retired rows -- has "
                "signals_db.ensure_tables() run against this DB_PATH yet?")
        fresh = {r['alias']: AccountLimits(
            enabled=bool(r['enabled']),
            notional_cap=r['notional_cap'],
            daily_order_cap=r['daily_order_cap'],
            trading_enabled=bool(r['trading_enabled']),
            cash_settlement_type=r['cash_settlement_type'],
            margin_capable=bool(r['margin_capable']),
            margin_floor=r['margin_floor'],
            is_tax_advantaged=bool(r['is_tax_advantaged']),
        ) for r in rows}
        self.clear()
        for k, v in fresh.items():
            dict.__setitem__(self, k, v)
        self._loaded = True
        self._loaded_db_path = signals_db.cfg.DB_PATH

    def _ensure_loaded(self):
        # Cold-review finding, 2026-08-11: the daemon runs a poll-loop thread
        # and a Slack Socket Mode handler thread concurrently (same pattern
        # signals_db._position_lock guards elsewhere). The staleness CHECK
        # itself must be inside the lock, not just the reload body -- a
        # thread that reads self._loaded==True without the lock could act on
        # a stale/transiently-cleared view while another thread is mid-
        # reload. Every read method calls this before touching self, so
        # locking here (not each caller separately) covers all of them.
        with self._reload_lock:
            if not self._loaded or self._loaded_db_path != signals_db.cfg.DB_PATH:
                self._reload_locked()

    def _reload(self):
        """Public-ish entry point for reload_accounts() -- goes through the
        same lock as _ensure_loaded()."""
        with self._reload_lock:
            self._reload_locked()

    def get(self, key, default=None):
        self._ensure_loaded()
        return dict.get(self, key, default)

    def __getitem__(self, key):
        self._ensure_loaded()
        return dict.__getitem__(self, key)

    def __contains__(self, key):
        self._ensure_loaded()
        return dict.__contains__(self, key)

    def items(self):
        self._ensure_loaded()
        return dict.items(self)

    def keys(self):
        self._ensure_loaded()
        return dict.keys(self)

    def values(self):
        self._ensure_loaded()
        return dict.values(self)

    def __iter__(self):
        self._ensure_loaded()
        return dict.__iter__(self)

    def __len__(self):
        self._ensure_loaded()
        return dict.__len__(self)


ACCOUNTS = _AccountsDict()


def reload_accounts():
    """Force a fresh read from the `accounts` table. No automatic
    invalidation exists (matching schwab_client._account_hashes's own
    no-invalidation precedent) -- call this after ensure_tables() seeds a
    different DB_PATH (tests) or after an accounts row changes mid-process."""
    ACCOUNTS._reload()

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
            _mode = "live" if (limits and limits.trading_enabled) else "dry_run"
            signals_db.log_coverage_event(
                "node_circuit_breaker_tripped", _mode, ticker=ticker, node_id=node_id,
                result="tripped", detail=f"kind={kind} streak={node_state[count_key]}"
            )
            import schwab_client  # local import: schwab_client imports this module at load time
            from signals_helpers import mode_tag  # local import: signals_helpers imports this module at load time
            _node = signals_db.get_watch_list_node_by_id(node_id)
            schwab_client._post_message(
                f"\U0001F6A8 *{ticker}* ({account} · {mode_tag(account, _node)}) node id={node_id} circuit breaker "
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


def _effective_notional(node) -> float:
    """starting_notional_override (2026-08-12) first, plain starting_notional
    otherwise -- mirrors signals_helpers._last_sale_recovery's own precedence.
    The override is the real lever that sizes orders once a node has closed a
    real trade, so a min_notional floor keyed to the raw column alone would
    misjudge any node deliberately resized through it (`is not None`, not
    `or`, so a genuine 0 override is honored rather than falling through)."""
    override = node.get('starting_notional_override')
    if override is not None:
        return override
    return node.get('starting_notional') or 0


UNREADABLE_FLAG_STATE = object()


def _raw_flag(path, key):
    """The tri-state behind auto_fill_detection_enabled's boolean: None means
    'never set', True/False mean a human (or this module) explicitly set it.
    disable_*_auto_fill_detection writes an explicit False rather than
    deleting the key, so the state files genuinely distinguish 'never enabled'
    from 'deliberately turned off' -- a distinction bulk_enable must respect
    (see its force= param).

    A file that EXISTS but can't be read returns the UNREADABLE_FLAG_STATE
    sentinel, NOT None. Collapsing that into 'never set' would fail open in
    the worst possible direction: every explicit human Disable would become
    invisible and a bulk apply=True would silently re-enable all of them,
    which is precisely the emergency override this distinction protects. A
    file that doesn't exist at all is genuinely 'no decisions recorded' and
    still returns None -- absence is not corruption."""
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return UNREADABLE_FLAG_STATE
    if not isinstance(state, dict):
        return UNREADABLE_FLAG_STATE
    if str(key) not in state:
        return None
    return bool(state[str(key)])


def resolve_auto_fill_detection_targets(min_notional=None, node_ids=None, tickers=None):
    """The real target set for bulk auto-fill-detection enablement: every
    watch_list row that is state='live' AND whose account is trading_enabled
    (i.e. exactly the nodes that can actually place a real order today), read
    FRESH from the DB on every call -- never a cached/hardcoded id list, since
    nodes get promoted/demoted between runs.

    `min_notional` is an optional floor on starting_notional. `node_ids` and
    `tickers` are FILTERS ONLY: they intersect with the set above, they can
    never add a node that isn't already a real live+trading_enabled node. That
    asymmetry is deliberate -- this function must not become a way to grant
    auto-fill trust to a paper/dry-run/disabled-account node by naming it.

    Note it checks `trading_enabled` and not `enabled`: `enabled=False` blocks
    every order for the account anyway, so a fill can't happen there for the
    flag to matter.

    The live+trading_enabled test here duplicates signals_helpers.
    effectively_dry_run's definition rather than calling it -- signals_helpers
    imports schwab_safety, so the reuse is blocked by import direction. Two
    known differences, both deliberate: this one excludes an unrecognized
    account (effectively_dry_run treats it as real), which is the safe
    direction for a function that GRANTS trust; and it reads state fresh from
    the DB rather than from a passed-in node dict."""
    targets = []
    for node in signals_db.get_live_nodes():
        account = node.get('account')
        limits = ACCOUNTS.get(account) if account else None
        if limits is None or not limits.trading_enabled:
            continue
        if min_notional is not None and _effective_notional(node) < min_notional:
            continue
        targets.append(node)

    if node_ids is not None:
        wanted_ids = {int(n) for n in node_ids}
        targets = [n for n in targets if n['id'] in wanted_ids]
    if tickers is not None:
        wanted_tickers = {str(t).upper() for t in tickers}
        targets = [n for n in targets if str(n.get('ticker') or '').upper() in wanted_tickers]
    return targets


def bulk_enable_auto_fill_detection(min_notional=None, node_ids=None, tickers=None,
                                     apply=False, force=False):
    """Enable auto-fill detection (BOTH the ticker-level and node-level flags,
    since the real gate is an AND of the two) for every node
    resolve_auto_fill_detection_targets() returns.

    `apply=False` (the DEFAULT, and the whole staging gate) computes and
    returns the exact plan while writing NOTHING to either JSON state file --
    a human runs it again with apply=True, deliberately, once the safety-net
    tranches have landed. Enable-only by design: there is no bulk-disable
    sibling, because a blast-radius-wide disable is exactly the kind of
    sweeping state change that should stay a per-node decision (the per-row
    Slack 'Disable' button remains the emergency override).

    Returns a dict: {'apply', 'force', 'targets', 'changed', 'already_enabled',
    'explicitly_disabled'}, where 'changed' is the nodes that were (or, under
    apply=False, would be) flipped. List values are [{'id', 'ticker',
    'account', 'starting_notional'}] dicts, so a caller can print a preview
    table.

    'explicitly_disabled' is the safety bucket: a node whose ticker-level or
    node-level flag is an explicit False (which is what
    disable_*_auto_fill_detection writes -- it does not delete the key) was
    deliberately switched off by a human, and since the Slack "Enable" button
    is gone, "Disable" is now the emergency override. Silently undoing one in
    a bulk sweep would defeat that, so those nodes are SKIPPED and reported
    separately; pass force=True to include them anyway. A node that was never
    set at all is not in this bucket -- absence is not a decision.

    Caveat worth knowing before running with apply=True: the ticker-level flag
    is shared by every node on that ticker. Enabling node A therefore also
    flips the coarse ticker gate for a sibling node B on the same ticker. B
    stays gated by its own node-level flag (which this only sets for real
    targets), so no untargeted node becomes auto-fill-enabled -- but a node
    that had been switched off via the ticker-level disable path specifically
    would have that coarse layer restored."""
    targets = resolve_auto_fill_detection_targets(
        min_notional=min_notional, node_ids=node_ids, tickers=tickers)

    def _summary(node):
        # Reports the EFFECTIVE notional (override-aware), i.e. the same number
        # min_notional filtered on -- previewing the raw column would show a
        # different figure than the one that actually selected the node.
        return {'id': node['id'], 'ticker': node.get('ticker'),
                'account': node.get('account'),
                'starting_notional': _effective_notional(node)}

    # Refuse to act at all on unreadable state rather than treating it as "no
    # decisions recorded" -- see _raw_flag's docstring. Checked once up front
    # (not per node) so the run either proceeds on trustworthy state or stops
    # before writing anything.
    for path in (AUTO_FILL_DETECTION_PATH, NODE_AUTO_FILL_DETECTION_PATH):
        if _raw_flag(path, '__probe__') is UNREADABLE_FLAG_STATE:
            raise RuntimeError(
                f"{path} exists but could not be parsed -- refusing to run: every explicit "
                f"human Disable in it would be invisible, and this would silently re-enable "
                f"them. Fix or remove the file first.")

    changed, already, disabled = [], [], []
    for node in targets:
        ticker, wl_id = node.get('ticker'), node['id']
        if auto_fill_detection_enabled(ticker) and node_auto_fill_detection_enabled(wl_id):
            already.append(_summary(node))
            continue
        # Node-level only. The ticker-level flag is SHARED by every node on
        # that ticker, so treating a ticker-level False as "this node was
        # deliberately disabled" would mis-attribute one node's Disable to its
        # siblings and wrongly skip them. The per-row Slack Disable button
        # calls disable_node_auto_fill_detection (node-level) precisely so it
        # doesn't affect siblings, so the node flag is the real record of a
        # human's per-node decision.
        if _raw_flag(NODE_AUTO_FILL_DETECTION_PATH, wl_id) is False and not force:
            disabled.append(_summary(node))
            continue
        if apply:
            enable_auto_fill_detection(ticker)
            enable_node_auto_fill_detection(wl_id)
        changed.append(_summary(node))

    return {'apply': apply, 'force': force, 'targets': [_summary(n) for n in targets],
            'changed': changed, 'already_enabled': already,
            'explicitly_disabled': disabled}


def format_bulk_enable_auto_fill_detection(result) -> str:
    """Human-readable preview/receipt for bulk_enable_auto_fill_detection's
    return value -- the thing a human actually reads before deciding to rerun
    with apply=True."""
    verb = "ENABLED" if result['apply'] else "would enable"
    skipped = result.get('explicitly_disabled') or []
    lines = [f"auto-fill detection: {len(result['targets'])} live+trading_enabled target node(s); "
             f"{len(result['changed'])} {verb}, {len(result['already_enabled'])} already enabled, "
             f"{len(skipped)} skipped (deliberately disabled)"]
    if not result['apply']:
        lines.append("(apply=False -- nothing was written; rerun with apply=True to commit)")

    def _fmt(label, row):
        notional = row['starting_notional']
        notional_str = f"${notional:,.0f}" if notional is not None else "?"
        return (f"  {label:>12}  id={row['id']:<5} {str(row['ticker'] or '?'):<6} "
                f"{str(row['account'] or '?'):<10} {notional_str}")

    for row in result['changed']:
        lines.append(_fmt(verb, row))
    for row in result['already_enabled']:
        lines.append(_fmt('already on', row))
    for row in skipped:
        lines.append(_fmt('SKIPPED', row))
    if skipped:
        lines.append("  ^ these were explicitly Disabled by a human (the emergency override). "
                     "Pass force=True to re-enable them anyway.")
    return "\n".join(lines)


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
        if row["state"] != "paper" and row["account"]:
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
    below (_has_open_order / _open_buy_tickers_in_account)."""
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


def _open_buy_order_quantity(orders: list, ticker: str) -> float:
    """Total resting BUY quantity for `ticker` across `orders` -- the real
    committed share count, straight from the broker's own order book (not a
    node config value). Used to reserve cash for another ticker's resting
    BUY using its ACTUAL quantity x current price, not its node's
    starting_notional -- found by contextual Opus review before this
    shipped: LABD's real resting order needed only $208 (a tiny 1-share
    node), but its starting_notional is $500 -- a ~2.4x over-reservation in
    that direction alone (worse for larger padded-sizing nodes), which
    directly undermines the fix's whole purpose of letting more tickers
    trade concurrently. The mirror risk (under-reserving a gap-resize/top-up
    order sized ABOVE starting_notional) is closed the same way, since this
    reads the real resting quantity either way."""
    total = 0.0
    for o in orders:
        for leg in o.get("orderLegCollection", []):
            if leg.get("instruction") == "BUY" and leg.get("instrument", {}).get("symbol") == ticker:
                total += leg.get("quantity") or 0.0
    return total


def _open_buy_tickers_in_account(orders: list, ticker: str) -> list[str]:
    """ALL other tickers with a resting BUY anywhere in this account (sorted,
    deduplicated) -- the multi-order-aware sibling of the earlier singular
    lookup this function replaced (deleted 2026-08-08, its job is now fully
    subsumed here). Needed once the account-wide BUY guard stopped blocking
    unconditionally (2026-08-07, cash-aware reservation fix) -- before that,
    at most ONE other resting BUY could ever exist by construction (the old
    guard blocked a 2nd from ever landing), so the old singular lookup was
    sufficient. Once a 2nd
    ticker's BUY is allowed to rest (cash permitting), a 3rd ticker's BUY
    must reserve against ALL of them, not just whichever one the singular
    lookup happened to return first -- found by cold Opus review before this
    shipped: an account like soxl_ira (11 live tickers, both daily signal
    windows firing near-simultaneously) can realistically have 2+ resting
    BUYs at once, and under-reserving for a 3rd on the missed one(s) would
    silently reopen the exact undecremented-balance risk this guard exists
    to prevent."""
    found = set()
    for o in orders:
        for leg in o.get("orderLegCollection", []):
            if leg.get("instruction") == "BUY" and leg.get("instrument", {}).get("symbol") != ticker:
                found.add(leg.get("instrument", {}).get("symbol"))
    return sorted(found)


def _has_open_sell_order(orders: list, ticker: str, exclude_order_id: int | None = None,
                          exclude_order_ids=None) -> bool:
    """True if any resting SELL order for this exact ticker already exists.
    Unlike _has_open_order (any side blocks a second BUY), this only matches
    same-side (SELL) orders -- an unrelated resting BUY for this ticker must
    not block closing a position, same asymmetry the module already applies
    elsewhere (same-day-block, notional-cap exemption) for SELL.
    exclude_order_id -- see _has_open_order's docstring; same rationale,
    for the SELL-side replace calls (_attempt_automated_exit_sell's SL/TIME
    exit, _attempt_automated_sell's TRAIL arm).
    exclude_order_ids -- additional ids to exclude (D3, docs/plans/
    real_order_execution_drought_addon.md): a margin add-on leg's own
    resting protective stop is a SECOND real, wanted SELL order for the same
    ticker -- without excluding it here, the PARENT core position's own
    ordinary exit placement (TRAIL arm, TP/SL/TIME replace) would see the
    leg's stop as a duplicate and refuse to place/replace its own order,
    even though is_addon_leg is False for that call (found by cold Opus
    review before this shipped -- the original is_addon_leg exemption only
    covered the leg's OWN placement, not the parent's, leaving the parent
    permanently exit-blocked once a leg's stop existed)."""
    exclude = set(exclude_order_ids or ())
    if exclude_order_id is not None:
        exclude.add(exclude_order_id)
    for o in orders:
        if o.get("orderId") in exclude:
            continue
        for leg in o.get("orderLegCollection", []):
            if leg.get("instruction") == "SELL" and leg.get("instrument", {}).get("symbol") == ticker:
                return True
    return False


def _has_open_buy_order_for_ticker(orders: list, ticker: str, exclude_order_id: int | None = None) -> bool:
    """Same-side (BUY) mirror of _has_open_sell_order, for the add-on leg's
    is_addon_leg exemption (docs/plans/real_order_execution_drought_addon.md
    3.1 Exemption B). _has_open_order (any side) can't be used for add-on: at
    the exact moment an add-on triggers, the just-armed parent core position
    ALWAYS has a resting protective SELL at the broker (its sl_order_id STOP
    or the arm-time TRAILING_STOP SELL _attempt_automated_sell just placed) --
    _has_open_order would block the add-on BUY 100% of the time, by
    construction, not hypothetically. This narrower check preserves the full
    2026-07-24 double-buy protection (two resting BUYs for the same ticker)
    while not self-blocking on the parent's own unrelated SELL leg."""
    for o in orders:
        if exclude_order_id is not None and o.get("orderId") == exclude_order_id:
            continue
        for leg in o.get("orderLegCollection", []):
            if leg.get("instruction") == "BUY" and leg.get("instrument", {}).get("symbol") == ticker:
                return True
    return False


def _broker_confirms_order(orders: list, ticker: str, side: str, quantity: int,
                            exclude_order_id: int | None = None) -> bool:
    """True if the real order book (all statuses, see _all_orders) has an
    order for this exact (ticker, side, quantity within tolerance) that the
    broker genuinely accepted -- i.e. not CANCELED/EXPIRED/REJECTED/REPLACED.
    Used to confirm a local 'recent_orders' duplicate candidate against ground
    truth before blocking a retry (automation_principles.md #1):
    approve_and_record() writes the local record before the real place_order
    call happens, so a failed/rejected broker call still looks like a
    submitted duplicate to the old purely-local check -- this lets a
    legitimate retry through when nothing actually reached the broker.
    exclude_order_id -- same rationale as _has_open_order/_has_open_sell_order's
    own param (2026-08-17): an atomic replace call's own target order is real
    and genuinely resting, but it must not count as broker CONFIRMATION of a
    duplicate -- of course it's resting, it's the exact order about to be
    swapped out. A genuinely separate confirming order still correctly blocks
    (the fingerprint loop itself isn't skipped); only a fingerprint whose ONLY
    broker-side match is the replace's own target falls through to the
    allowed_retry branch instead of being wrongly treated as confirmed."""
    for o in orders:
        if exclude_order_id is not None and o.get("orderId") == exclude_order_id:
            continue
        if o.get("status") in _DUPLICATE_NOT_CONFIRMED_STATUSES:
            continue
        for leg in o.get("orderLegCollection", []):
            if leg.get("instruction") != side or leg.get("instrument", {}).get("symbol") != ticker:
                continue
            qty = leg.get("quantity", 0)
            if abs(qty - quantity) <= DUPLICATE_ORDER_QUANTITY_TOLERANCE_PCT / 100 * max(qty, quantity, 1):
                return True
    return False


# D2: an add-on leg is sized at 100% of an already-deployed core position --
# by construction it's borrowing, so the ordinary cash check (which refuses
# nearly every real add-on) is replaced with a buying-power check instead,
# required to clear at least this multiple of the order notional. 2.0 =
# never consume more than half the account's real buying power on one
# add-on leg -- conservative, not derived from a backtest; loosen only with
# a deliberate decision, not silently.
ADDON_BUYING_POWER_HEADROOM_MULT = 2.0


def check_order(
    account: str, ticker: str, quantity: int, price: float, side: str, counts: dict | None = None,
    is_gap_correction: bool = False, is_protective: bool = False, replacing_order_id: int | None = None,
    is_addon_leg: bool = False, node_id: int | None = None, source: str = 'daemon',
    is_handoff_exit: bool = False,
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
    protective SL order for exactly this reason.
    is_addon_leg: a REQUEST to run stricter checks, not a trust token -- see
    the block below (BUY only). Five preconditions are verified fresh against
    the local DB before any of the four exemptions (existing-position guard,
    same-side-only dup-order check, signal-window gate, buying-power-based
    cash check) take effect; failing any of them raises exactly like a normal
    BUY would. Never exempts: kill switch, account allowlist,
    node_automation_enabled/AUTOMATION_ENABLED_TICKERS, trading-day gate,
    HARD_ORDER_CEILING, notional_cap, daily_order_cap, burst cap,
    duplicate-order window, or _open_buy_tickers_in_account (a resting BUY
    for a DIFFERENT ticker in this account still blocks -- cash-permitting
    for an ordinary BUY, buying-power-permitting for an add-on leg, per the
    2026-08-10 fix bringing the add-on path onto the same real-affordability
    model instead of an unconditional block -- see that function's
    docstring).
    node_id: the real watch_list.id this order belongs to, threaded through
    from schwab_client's 6 place_*/replace_* functions (2026-08-10) -- closes
    the wl_id-refactor gap this module's own comment used to flag as a
    separate, not-yet-done follow-up. When given, this is used directly for
    _node_id and for the local-position lookups below (get_open_position_by_
    wl_id) instead of the ambiguous ticker+account derivation, so two nodes
    sharing a (ticker, account) pair -- e.g. a dry_run rehearsal canary and a
    real live node, the AGQ/brokerage collision found 2026-08-10 -- no longer
    resolve to each other's automation-pause state or position. Optional and
    defaults to None so any caller not yet updated still gets the old
    ticker+account-derived (ambiguous-on-collision) behavior -- not every
    call site threads a node_id yet (e.g. manual/script-driven order calls).
    source: coverage_events write-attribution (signals_db.COVERAGE_EVENT_
    SOURCES, Phase 1 2026-08-16) -- passed straight through to every
    log_coverage_event call in this function. Defaults to 'daemon' since
    that's the real caller for every production order; a fixture/staging
    script calling check_order directly (e.g.
    scripts/stage_check_order_guard_scenarios.py) passes its own
    'fixture:<script_name>' explicitly.
    is_handoff_exit: a REQUEST, not a trust token (mirrors is_addon_leg's
    contract) -- verified fresh below (SELL block) against a real open
    drought-overlay position on file AND a non-null replacing_order_id
    before exempting from the dup-order-window guard. Closes the real,
    deterministic self-collision where check_drought_handoff's own exit
    replace races the protective SL _reconcile_buy_fill just placed for the
    same newly-opened drought position, milliseconds apart, well inside
    DUPLICATE_ORDER_WINDOW_SECS (2026-08-16 finding, 2026-08-17 fix)."""
    if kill_switch_engaged():
        _limits = ACCOUNTS.get(account)
        _mode = "live" if (_limits and _limits.trading_enabled) else "dry_run"
        signals_db.log_coverage_event("kill_switch_block", _mode, ticker=ticker, result="blocked",
                                       detail=kill_switch_reason(), source=source)
        raise SafetyViolation(f"global kill switch engaged ({kill_switch_reason()})")

    limits = ACCOUNTS.get(account)
    if limits is None:
        # mode is unknowable here (no ACCOUNTS row to read trading_enabled
        # from) -- logged as 'dry_run' rather than guessing 'live', matching
        # _coverage_mode's own None-account convention elsewhere.
        signals_db.log_coverage_event("unknown_account_block", "dry_run", ticker=ticker, node_id=node_id,
                                       result="blocked",
                                       detail=f"account={account!r} not in allowlist (node_id caller-supplied, unverified)", source=source)
        raise SafetyViolation(f"unknown account '{account}' -- not in the allowlist")
    if not limits.enabled:
        signals_db.log_coverage_event("account_disabled_block", "dry_run" if not limits.trading_enabled else "live",
                                       ticker=ticker, node_id=node_id, result="blocked",
                                       detail=f"account={account!r} disabled (node_id caller-supplied, unverified)", source=source)
        raise SafetyViolation(f"account '{account}' is disabled in the allowlist")
    _mode = "dry_run" if not limits.trading_enabled else "live"
    if node_id is not None:
        # Verified, not trusted from the caller (Opus review, 2026-08-10,
        # MEDIUM): the old ticker+account derivation guaranteed the resolved
        # node actually belonged to this (ticker, account) pair; a bare
        # node_id doesn't. A caller passing a moved/sibling/stale node's id
        # would otherwise silently apply the WRONG node's automation-pause
        # and position-guard state. Fails safe to the old ambiguous
        # derivation on mismatch rather than raising -- this is a real-money
        # order-placement gate, not somewhere to introduce a new way to
        # unexpectedly block a legitimate order over a caller-side bug; the
        # mismatch itself is still logged so it's visible, not silent.
        _node_check = signals_db.get_watch_list_node_by_id(node_id)
        if _node_check and (_node_check.get('ticker') != ticker or _node_check.get('account') != account):
            signals_db.log_coverage_event(
                "node_id_ticker_account_mismatch", _mode, ticker=ticker, node_id=node_id, result="fallback",
                detail=f"node.ticker={_node_check.get('ticker')!r} node.account={_node_check.get('account')!r} "
                       f"vs call ticker={ticker!r} account={account!r}", source=source)
            node_id = None
    if node_id is not None:
        _node_id = node_id
    else:
        # Fallback for callers that haven't threaded a real node_id through
        # yet (see the node_id param docstring above). get_watch_list_node()
        # returns None on an ambiguous match (2+ nodes same ticker+account),
        # so node_automation_enabled(None) below silently defaults to True in
        # exactly that case -- a node-level pause is a no-op for two
        # same-account nodes on one ticker for any caller still on this path.
        _node = signals_db.get_watch_list_node(ticker=ticker, account=account)
        _node_id = _node['id'] if _node else None

    ticker_accounts = _live_ticker_accounts()
    if ticker not in ticker_accounts:
        # Real trigger: a node_id-race, not routine -- a signal check earlier
        # in the same poll cycle read the node as state='live', but by the
        # time this order-placement gate actually runs, the node has been
        # demoted (paper/dry_run/research) or removed from the watchlist,
        # e.g. a user-initiated mode flip landing mid-cycle. Genuinely
        # anomalous (config/state mismatch), not an intentional pause like
        # node/ticker automation toggles below.
        signals_db.log_coverage_event("ticker_not_live_mode_block", _mode, ticker=ticker,
                                       node_id=_node_id, result="blocked",
                                       detail=f"'{ticker}' not in _live_ticker_accounts() at check_order time", source=source)
        raise SafetyViolation(f"'{ticker}' is not a live-mode ticker on the active watchlist")
    if account not in ticker_accounts[ticker]:
        signals_db.log_coverage_event(
            "ticker_account_assignment_mismatch", _mode, ticker=ticker, node_id=_node_id, result="blocked",
            detail=f"account={account!r} not in assigned accounts {sorted(ticker_accounts[ticker])}", source=source)
        raise SafetyViolation(
            f"'{ticker}' is not assigned to account '{account}' "
            f"(assigned accounts: {sorted(ticker_accounts[ticker])})"
        )
    if len(ticker_accounts[ticker]) > 1:
        signals_db.log_coverage_event(
            "two_nodes_same_ticker_diff_accounts", _mode, ticker=ticker, node_id=_node_id, result="allowed",
            detail=f"account={account} of {sorted(ticker_accounts[ticker])}", source=source)
    if not node_automation_enabled(_node_id):
        signals_db.log_coverage_event("node_level_automation_pause", _mode, ticker=ticker, node_id=_node_id,
                                       result="blocked", source=source)
        raise SafetyViolation(f"node id={_node_id} for '{ticker}' has automation paused")
    if ticker not in AUTOMATION_ENABLED_TICKERS:
        signals_db.log_coverage_event("ticker_not_in_automation_scope_block", _mode, ticker=ticker,
                                       node_id=_node_id, result="blocked",
                                       detail=f"'{ticker}' not in AUTOMATION_ENABLED_TICKERS", source=source)
        raise SafetyViolation(
            f"'{ticker}' is not in the automation pilot scope {AUTOMATION_ENABLED_TICKERS} "
            f"-- still manual-only"
        )
    if not ticker_automation_enabled(ticker):
        signals_db.log_coverage_event("ticker_level_automation_pause", _mode, ticker=ticker,
                                       node_id=_node_id, result="blocked", source=source)
        raise SafetyViolation(f"'{ticker}' automation is paused (per-ticker toggle) -- resume from the reference report")

    # Same-day re-buy guardrail (2026-07-15): a same-day re-buy risks a real
    # Schwab good-faith violation (reusing unsettled sale proceeds in a cash
    # account) -- a hard broker-enforced constraint, unlike the same-day-sell
    # direction (a soft employer recommendation, not enforced, deliberately
    # left out). Margin accounts (regular or IRA limited margin) don't have
    # this settlement restriction (2026-07-20 finding, resolved 2026-07-22)
    # -- skip for them, UNLESS the node itself opts in via watch_list.
    # force_same_day_block (2026-08-11: per-TICKER, not per-account -- the
    # user's own choice to apply the same discipline to a specific node,
    # regardless of account_type; not a settlement-risk finding for margin).
    # The TRIGGER (signals_db.closed_today(ticker)) is ticker-global, same as
    # the pre-existing cash-account behavior -- it does NOT scope to which
    # account/node actually closed the position. So a node with the flag set
    # can be blocked by a same-day exit that happened in a DIFFERENT account
    # entirely (both reviewers flagged this, 2026-08-11 session-wrap review).
    # This is a deliberate reuse of the existing trigger, not a new gap --
    # confirmed against the user, who was explicit that cross-account same-
    # ticker scoping is out of scope for this feature -- but the exception
    # message below says which node is affected, not implying the CLOSE
    # itself was that node's own trade.
    #
    # is_protective/is_addon_leg BUYs are exempt from this entire section
    # (found by cold Opus review before force_same_day_block shipped, 2026-
    # 08-11): this check ran unconditionally ahead of the is_protective/
    # is_addon_leg exemptions further down in the `side == "BUY"` block
    # below, which only cover the duplicate-open-position guard, not this
    # one. A sanctioned post-fill top-up (is_protective=True -- e.g.
    # post_fill_topup, real-live-proven 2026-08-10 on RETL/soxl_ira) or a
    # real add-on leg BUY landing on a day this ticker also had a same-day
    # exit would have been wrongly rejected as an ordinary re-buy. This was
    # already latent for cash accounts (never reachable -- no cash account
    # has ever been trading_enabled); force_same_day_block makes it reachable
    # for the first time, on real capital, since both live-enabled accounts
    # (ira, soxl_ira) are margin and this override is the only way
    # same_day_block can ever fire on them.
    if side == "BUY" and not is_protective and not is_addon_leg:
        # Re-fetched fresh here (not reused from the node_id-resolution block
        # above, which only keeps _node_id, not the full row) -- same
        # re-read-before-trusting convention as the rest of this function.
        _node_for_same_day = signals_db.get_watch_list_node_by_id(_node_id) if _node_id is not None else None
        _force_same_day_block = bool(_node_for_same_day and _node_for_same_day.get('force_same_day_block'))
        if signals_db.closed_today(ticker) and (limits.cash_settlement_type == "cash" or _force_same_day_block):
            signals_db.log_coverage_event(
                "same_day_block", _mode, ticker=ticker, node_id=_node_id, result="blocked",
                detail=f"account={account} cash_settlement_type={limits.cash_settlement_type} "
                       f"force_same_day_block={_force_same_day_block}", source=source)
            reason = ("same-day re-buy risks a cash-account good-faith violation" if limits.cash_settlement_type == "cash"
                       else f"node id={_node_id!r}'s force_same_day_block is set -- the same-day close may have "
                            f"happened in a different account for this ticker, not necessarily this node's own")
            raise SafetyViolation(f"'{ticker}' was sold today (some account) -- {reason}")
        elif limits.cash_settlement_type == "margin" and signals_db.closed_today(ticker):
            signals_db.log_coverage_event(
                "same_day_block", _mode, ticker=ticker, node_id=_node_id, result="skipped_margin_account",
                detail=f"account={account} cash_settlement_type={limits.cash_settlement_type}", source=source)

    if side == "BUY":
        if node_id is not None:
            # Paired Opus review, 2026-08-10 (HIGH, confirmed): a bare
            # get_open_position_by_wl_id(node_id) misses any position whose
            # wl_id doesn't match -- a legacy/unbackfilled wl_id IS NULL row
            # (open_position()'s own docstring documents these exist) or a
            # node recreated under a new id while its position stayed open
            # under the old one. Both would make this guard fail OPEN (a
            # second real BUY approved against real, untracked capital) --
            # the exact double-buy gap the 2026-08-02 existing-position guard
            # was built to close in the first place. Falls back to the
            # orphaned-only lookup (never a sibling's wl_id-tagged row --
            # that would reintroduce the misattribution this fix exists to
            # prevent) so an unattributed real position still blocks.
            _local_pos = (signals_db.get_open_position_by_wl_id(node_id)
                          or signals_db.get_orphaned_open_position_for_account(ticker, account))
        else:
            _local_pos = signals_db.get_open_position_for_account(ticker, account)
        _log_pre_action_state_verification(
            account, ticker, _node_id, _mode, "BUY",
            _local_pos['shares'] if _local_pos else None)

        if is_addon_leg:
            # Five preconditions, verified fresh against the DB -- NOT trusted
            # from the caller's claim (docs/plans/real_order_execution_drought_addon.md
            # 3.1). Any failure raises exactly like an ordinary BUY block.
            #
            # margin_capable was split out from the old overloaded account_type
            # field 2026-08-11 (see docs/deep_backlog.md's accounts-table
            # entry) specifically so this check reads real borrowing
            # eligibility instead of same-day-settlement capability -- but
            # its VALUES were seeded to exactly preserve this check's prior
            # behavior (margin_capable=True for brokerage/roth/ira/soxl_ira,
            # False only for sep), not to fix the soxl_ira mislabel itself.
            # That real fix (add-on doesn't inherently need to borrow -- a
            # cash-aware check against real available cash, falling back to
            # margin capacity only for a genuine shortfall) was designed but
            # deliberately deferred: add-on's cash draw can collide with the
            # skim-reserve mechanism's earmarked-but-not-yet-moved cash, and
            # with 8+ tickers able to trigger add-on near-concurrently, a
            # single reservation number isn't enough (same shape as the
            # multi-resting-BUY reservation fix elsewhere in this file).
            # User's call, reconfirmed 2026-08-11: moderate balances, no skim
            # active yet, not worth solving now -- see docs/backlog_cache.md's
            # 2026-08-07 "2027 problem" entries.
            if not limits.margin_capable:
                signals_db.log_coverage_event(
                    "addon_non_margin_account_blocked", _mode, ticker=ticker, node_id=_node_id,
                    result="blocked", detail=f"account={account} margin_capable={limits.margin_capable}", source=source)
                raise SafetyViolation(
                    f"add-on leg refused -- '{account}' is not margin-capable "
                    f"(margin_capable={limits.margin_capable})")
            if _local_pos is None or _local_pos.get('position_source') != 'core':
                signals_db.log_coverage_event(
                    "addon_precondition_blocked", _mode, ticker=ticker, node_id=_node_id,
                    result="blocked", detail="no_open_core_position", source=source)
                raise SafetyViolation(
                    f"add-on leg refused -- no open CORE position on file for ({ticker}, {account})")
            _trail_state = _local_pos.get('trail_state') or {}
            if _trail_state.get('trailing') is not True:
                signals_db.log_coverage_event(
                    "addon_precondition_blocked", _mode, ticker=ticker, node_id=_node_id,
                    result="blocked", detail="parent_not_armed", source=source)
                raise SafetyViolation(
                    "add-on leg refused -- parent position is not armed (trail_state.trailing is not True)")
            if signals_db.get_open_addon_leg_by_parent(_local_pos['id']) is not None:
                signals_db.log_coverage_event(
                    "addon_precondition_blocked", _mode, ticker=ticker, node_id=_node_id,
                    result="blocked", detail="leg_already_open", source=source)
                raise SafetyViolation("add-on leg refused -- a leg is already open for this parent")
            if int(quantity) != int(_local_pos['shares']):
                signals_db.log_coverage_event(
                    "addon_size_mismatch_blocked", _mode, ticker=ticker, node_id=_node_id,
                    result="blocked", detail=f"quantity={quantity} parent_shares={_local_pos['shares']:g}", source=source)
                raise SafetyViolation(
                    f"add-on leg refused -- quantity {quantity} must exactly equal the parent's "
                    f"{_local_pos['shares']:g} shares")
            # D5: combined-exposure ceiling. Deliberately conservative rather than
            # inventing a new number -- reuses the account's own notional_cap as the
            # combined-exposure bound (core position's own notional, at its real entry
            # price, plus this add-on order) instead of a bespoke multiplier. This can
            # under-permit a legitimate add-on on a node whose core leg alone is already
            # close to the cap (flagged for the user to revisit -- see D5 in the plan).
            _combined_notional = _local_pos['shares'] * _local_pos.get('entry_price', price) + quantity * price
            if _combined_notional > limits.notional_cap:
                signals_db.log_coverage_event(
                    "addon_combined_exposure_blocked", _mode, ticker=ticker, node_id=_node_id,
                    result="blocked",
                    detail=f"combined=${_combined_notional:,.0f} cap=${limits.notional_cap:,.0f}", source=source)
                raise SafetyViolation(
                    f"add-on leg refused -- combined core+addon notional ${_combined_notional:,.0f} "
                    f"exceeds {account}'s ${limits.notional_cap:,.0f} cap"
                )
            # All five preconditions passed -- this IS the accountability record for
            # the widened gate (every firing reviewable, bad_results=[]).
            signals_db.log_coverage_event(
                "addon_double_buy_exemption", _mode, ticker=ticker, node_id=_node_id,
                result="preconditions_passed",
                detail=f"parent_shares={_local_pos['shares']:g} quantity={quantity} "
                       f"combined_notional=${_combined_notional:,.0f}", source=source)

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
        # Was a KNOWN LIMITATION (found by review, 2026-08-02, same shape as
        # the _node_id ambiguity noted above): get_open_position_for_account
        # is ticker+account-keyed, not wl_id-keyed, so 2 live nodes sharing a
        # ticker+account (the real AGQ/brokerage collision found 2026-08-10)
        # would wrongly block the SECOND node's genuine first entry as if it
        # were a duplicate of the first node's position. Fixed 2026-08-10 for
        # any caller passing node_id (see that param's docstring above) --
        # _local_pos is wl_id-scoped in that case. Still ambiguous for a
        # caller that doesn't pass node_id.
        if _local_pos and not is_protective and not is_addon_leg:
            signals_db.log_coverage_event(
                "buy_blocked_position_exists", _mode, ticker=ticker, node_id=_node_id, result="blocked",
                detail=f"account={account} held_shares={_local_pos['shares']:g}", source=source)
            raise SafetyViolation(
                f"'{ticker}' already has an open position ({_local_pos['shares']:g} shares) in "
                f"'{account}' -- refusing a second real BUY (not a protective top-up)"
            )
        orders = _open_orders(account)
        _log_guard_input(account, ticker, "BUY", orders, replacing_order_id)
        # Exemption B (is_addon_leg only): at the exact moment an add-on triggers,
        # the parent core position ALWAYS has a resting protective SELL at the
        # broker (its sl_order_id STOP or the arm-time TRAILING_STOP SELL) --
        # _has_open_order (any side) would block the add-on BUY 100% of the time,
        # by construction. Swap to the same-side-only check, which still catches
        # a genuine second resting BUY for this ticker.
        _dup_check = _has_open_buy_order_for_ticker if is_addon_leg else _has_open_order
        if _dup_check(orders, ticker, exclude_order_id=replacing_order_id):
            signals_db.log_coverage_event("dup_order_blocked", _mode, ticker=ticker, node_id=_node_id, result="blocked_same_ticker", source=source)
            raise SafetyViolation(
                f"'{ticker}' already has an open/working order in '{account}' -- refusing a second "
                f"concurrent BUY (Schwab doesn't reserve buying power for a resting order, so nothing "
                f"else stops these from stacking)"
            )
        # Cash-aware as of 2026-08-07 for an ordinary (non-addon) BUY -- an
        # unconditional block here was too blunt for an account like
        # soxl_ira that deliberately hosts many tickers on one account (by
        # design, to maximize organic exercise of real order-placement code
        # paths in production, not to run each ticker at capital-constrained
        # size). Real incident: RETL's genuine signal was blocked because
        # LABD already had a resting BUY, even though soxl_ira had ~$9,884
        # left after LABD's $208 reservation -- comfortably enough for
        # RETL's $800 order too. Deferred to the cash-availability check
        # below (where `notional`/`cash_available` already live) instead of
        # raising unconditionally here -- see that block for the real
        # decision. (is_addon_leg used to be kept strict here -- see the
        # 2026-08-10 comment further down for why that's no longer true.)
        # List, not a single ticker (fixed 2026-08-07, same review pass as
        # the cash-aware guard itself): with the unconditional block gone,
        # 2+ other tickers can genuinely have resting BUYs at once (11 live
        # tickers on soxl_ira, both daily signal windows firing near-
        # simultaneously) -- see _open_buy_tickers_in_account's docstring.
        # is_addon_leg no longer raises unconditionally here (fixed
        # 2026-08-10, same "1-ticker-per-account artifact" shape as the
        # general case above -- found via a direct audit prompted by the
        # RETL/LABD incident, see docs/deep_backlog.md's 2026-08-09/10
        # entry): the old block refused ANY add-on the instant another
        # ticker had a resting BUY, with no check of whether buying power
        # actually was insufficient, despite the addon_buying_power_check
        # below existing specifically to answer that question. Reservation
        # for other_tickers is now folded into that check instead.
        other_tickers = _open_buy_tickers_in_account(orders, ticker)

    # Resting-SELL guard (2026-07-22, symmetric to the BUY-side guard above):
    # found via Opus review that SELL had no such check at all, which is what
    # let a real bug (a stale trail_state overwrite re-arming the trailing
    # exit on the next bar) place a second live trailing-sell order for the
    # same shares -- an oversell risk if both then fill. Same-ticker only,
    # not account-wide like the BUY guard: an unrelated resting BUY for this
    # ticker must not block closing a position.
    if side == "SELL":
        if node_id is not None:
            # Mirror of the BUY-side fallback above -- without it, a real
            # exit for an orphaned/unbackfilled position would fail CLOSED
            # (SafetyViolation, "no open position on file") instead of
            # finding it and letting the exit through.
            _presell_pos = (signals_db.get_open_position_by_wl_id(node_id)
                            or signals_db.get_orphaned_open_position_for_account(ticker, account))
        else:
            _presell_pos = signals_db.get_open_position_for_account(ticker, account)
        _log_pre_action_state_verification(
            account, ticker, _node_id, _mode, "SELL",
            _presell_pos['shares'] if _presell_pos else None)
        orders = _open_orders(account)
        _log_guard_input(account, ticker, "SELL", orders, replacing_order_id)
        # Addon leg lookup, NODE-scoped (via _node_id, already resolved above
        # from ticker+account) rather than parent-position-scoped -- CRITICAL
        # fix (found by cold Opus review before this shipped): the real
        # lockstep close always runs AFTER db.close_position() has already
        # DELETEd the parent's open_positions row (Part 7's own stated
        # ordering: "called AFTER the core exit's own coverage event/alert"),
        # so _presell_pos is None by the time the leg's own real exit SELL
        # reaches this guard. A parent-id-based lookup (get_open_addon_leg_
        # by_parent(_presell_pos['id'])) would therefore always come back
        # empty for the one call this exemption exists to unblock. The leg's
        # own addon_legs row is looked up independently of whether the core
        # position is still on file.
        _open_leg = signals_db.get_open_addon_leg_by_wl_id(_node_id) if _node_id is not None else None
        # is_addon_leg SELL exemption (D3, docs/plans/real_order_execution_
        # drought_addon.md 6.2): the leg's own protective stop is DELIBERATELY
        # meant to rest ALONGSIDE the parent core position's own resting
        # protective order for the same ticker -- two real, wanted SELLs, not
        # the accidental-duplicate case this guard exists to catch. Verified
        # (not trusted from the caller): an open addon leg must actually exist
        # for this node before the exemption applies.
        _addon_sell_exempt = False
        if is_addon_leg:
            if _open_leg is not None:
                _addon_sell_exempt = True
            else:
                signals_db.log_coverage_event(
                    "addon_precondition_blocked", _mode, ticker=ticker, node_id=_node_id,
                    result="blocked", detail="sell_no_open_leg_for_node", source=source)
                raise SafetyViolation(
                    f"add-on leg SELL refused for '{ticker}' -- no open addon leg on file for this node"
                )
        # Always excluded (not just when is_addon_leg): the parent core
        # position's own ordinary exit placement must not be blocked by its
        # own leg's resting stop, which is a real, wanted, SEPARATE order.
        _leg_sl_order_id = _open_leg.get('sl_order_id') if _open_leg is not None else None
        if _has_open_sell_order(orders, ticker, exclude_order_id=replacing_order_id,
                                 exclude_order_ids={_leg_sl_order_id} if _leg_sl_order_id else None) and not _addon_sell_exempt:
            signals_db.log_coverage_event("dup_sell_order_blocked", _mode, ticker=ticker, node_id=_node_id, result="blocked", source=source)
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
        # is_addon_leg bounds against the LEG's own recorded shares, not the
        # (possibly already-closed) core position's -- see the _open_leg
        # lookup rationale above. A non-addon-leg (core) SELL with an open
        # leg on file is widened to core+leg shares -- Part 8's arm-time
        # merge (docs/backlog_cache.md, 2026-08-17) legitimately folds the
        # leg's shares into the core's own single exit order, so a merged
        # quantity is expected to exceed the core's own recorded shares by
        # exactly the leg's share count. Same widening principle as
        # check_live_state_reconciliation's existing expected_shares patch
        # for the entry side (signals_notify.py) -- without it, EVERY merged
        # exit would be rejected here as a false "would-be short" before
        # Part 8 could ever place a single real order.
        #
        # Gated on `not _open_leg.get('sl_order_id')`, NOT just `_open_leg is
        # not None` (HIGH finding, paired Opus review 2026-08-17/18): a
        # widened bound must only apply once the leg's OWN resting stop is
        # actually gone -- otherwise the guard permits core+leg worth of SELL
        # orders while core+leg worth of stops are simultaneously resting
        # (the leg's own still-live stop, plus a widened core order), a real
        # naked-short exposure this guard exists to prevent. Confirmed
        # correct against _attempt_automated_sell's real ordering
        # (signals_notify.py): it clears set_addon_leg_sl_order_id(leg['id'],
        # None) BEFORE attempting the merged placement, so by the time this
        # check runs during that merge attempt, the leg's sl_order_id is
        # already None -- the widened bound applies exactly then, not before.
        # `merged_into_core` itself can't be the gate: it's only set AFTER
        # placement succeeds, so it's still 0/false at check-time too.
        if is_addon_leg and _open_leg is not None:
            pos = {'shares': _open_leg['shares']}
        elif _presell_pos is not None and _open_leg is not None and not _open_leg.get('sl_order_id'):
            pos = {'shares': _presell_pos['shares'] + _open_leg['shares']}
        else:
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
        # no-position SELL path was found (is_addon_leg's leg-close is the
        # one exception, handled by the leg-shares basis above).
        if pos is None:
            signals_db.log_coverage_event(
                "sell_exceeds_position_blocked", _mode, ticker=ticker, node_id=_node_id, result="blocked_no_position",
                detail=f"quantity={quantity:g}", source=source)
            raise SafetyViolation(
                f"SELL {quantity:g} {ticker} in '{account}' -- no open position on file for this "
                f"(ticker, account), refusing to guess a bound and place an unverified SELL"
            )
        if quantity > pos['shares'] * 1.001:  # tolerance for float share counts
            signals_db.log_coverage_event(
                "sell_exceeds_position_blocked", _mode, ticker=ticker, node_id=_node_id, result="blocked",
                detail=f"quantity={quantity:g} held={pos['shares']:g}", source=source)
            raise SafetyViolation(
                f"SELL {quantity:g} {ticker} exceeds the {pos['shares']:g} shares on file for "
                f"'{account}' -- refusing a would-be short"
            )
        # Success path, added 2026-08-13: both branches above only ever logged a FAILURE
        # (blocked_no_position, blocked) -- registry row 'oversell_guard_correct_position' had no
        # way to show positive evidence that this guard resolves the right position under real
        # 2-node-same-ticker-different-account conditions, since a clean pass was silent. Real
        # instrumentation gap, not evidence the guard "can never pass" (found via user pushback,
        # 2026-08-13 -- a negative test case genuinely does count as proof, this row's actual gap
        # was simply that the POSITIVE case was never logged at all).
        signals_db.log_coverage_event(
            "sell_exceeds_position_blocked", _mode, ticker=ticker, node_id=_node_id, result="resolved",
            detail=f"quantity={quantity:g} held={pos['shares']:g}", source=source)

    # Trading-day gate, BUY only, unconditional (including gap-correction --
    # an overnight gap only exists ahead of a real trading day, so there's no
    # legitimate reason to exempt it the way the window gate below does).
    if side == "BUY":
        now = _now()
        if not _is_trading_day(now.strftime('%Y-%m-%d')):
            signals_db.log_coverage_event("buy_trading_day_block", _mode, ticker=ticker,
                                           node_id=_node_id, result="blocked",
                                           detail=now.strftime('%Y-%m-%d'), source=source)
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
    if side == "BUY" and not is_gap_correction and not is_protective and not is_addon_leg:
        now = _now()
        t = (now.hour, now.minute)
        all_windows = _SIGNAL_WINDOWS + _OPEN_CHECK_WINDOWS
        in_window = any((h0, m0) <= t <= (h1, m1) for h0, m0, h1, m1 in all_windows)
        if not in_window:
            signals_db.log_coverage_event("buy_signal_window_block", _mode, ticker=ticker,
                                           node_id=_node_id, result="blocked",
                                           detail=f"current time {t[0]:02d}:{t[1]:02d}", source=source)
            raise SafetyViolation(
                f"BUY outside signal windows {all_windows} (current time {t[0]:02d}:{t[1]:02d})"
            )

    notional = quantity * price
    if notional > HARD_ORDER_CEILING:
        signals_db.log_coverage_event("hard_order_ceiling_block", _mode, ticker=ticker,
                                       node_id=_node_id, result="blocked",
                                       detail=f"notional=${notional:,.0f} ceiling=${HARD_ORDER_CEILING:,.0f}", source=source)
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
        signals_db.log_coverage_event("notional_cap_block", _mode, ticker=ticker,
                                       node_id=_node_id, result="blocked",
                                       detail=f"notional=${notional:,.0f} cap=${limits.notional_cap:,.0f}", source=source)
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
    if side == "BUY" and is_addon_leg:
        # Exemption D (D2): an add-on leg is sized at 100% of an already-deployed
        # position -- it's borrowing by construction, so the ordinary cash check
        # (which reads availableFunds/cashAvailableForTrading) would refuse
        # nearly every real add-on. Uses buying power instead, gated to this
        # path only -- the ordinary cash check below still applies to every
        # other BUY, including a core entry on the same margin account.
        import schwab_client  # local import: schwab_client imports this module at load time
        try:
            # Leverage-aware AND clamped (2026-08-12) -- get_leveraged_buying_power
            # returns min(equity/margin_req(ticker), Schwab's raw 'buyingPower'
            # field). A first version without the clamp was briefly wired in here,
            # found live-reachable on soxl_ira with a materially overstated result
            # (a limited-margin IRA, no real leverage -- see that function's
            # docstring), and reverted; the clamp fixes it by construction (can
            # only tighten the raw figure for a real 3x fund, never loosen it
            # beyond what Schwab's own real-time number already allows).
            buying_power = schwab_client.get_leveraged_buying_power(account, ticker)
        except Exception as e:
            signals_db.log_coverage_event(
                "addon_buying_power_check", _mode, ticker=ticker, node_id=_node_id,
                result="failed_closed", detail=str(e), source=source)
            raise SafetyViolation(f"could not verify '{account}' buying power, blocking add-on order: {e}")
        # Reserve for every other ticker's resting BUY (2026-08-10, mirrors the
        # non-addon cash-aware reservation below) -- other_tickers/orders were
        # already fetched above regardless of is_addon_leg. Real committed
        # quantity x current price, not starting_notional, same reasoning as
        # the non-addon version. Fails closed if a live price can't be
        # fetched for any of them.
        _reserved_other = 0.0
        _unpriced = []
        for _ot in other_tickers:
            _ot_qty = _open_buy_order_quantity(orders, _ot)
            try:
                _ot_price = schwab_client.get_current_price(_ot)
            except Exception:
                _unpriced.append(_ot)
                continue
            _reserved_other += _ot_qty * _ot_price
        if _unpriced:
            signals_db.log_coverage_event(
                "addon_buying_power_check", _mode, ticker=ticker, node_id=_node_id,
                result="blocked_unpriced", detail=f"account={account} other_tickers={other_tickers} "
                                                   f"unpriced={_unpriced}", source=source)
            raise SafetyViolation(
                f"account '{account}' already has resting BUY order(s) for {other_tickers}, and a live "
                f"price couldn't be fetched for {_unpriced} to estimate reserved buying power -- "
                f"refusing the add-on order"
            )
        required = notional * ADDON_BUYING_POWER_HEADROOM_MULT + _reserved_other
        if buying_power < required:
            signals_db.log_coverage_event(
                "addon_buying_power_check", _mode, ticker=ticker, node_id=_node_id,
                result="blocked_insufficient",
                detail=f"required=${required:,.0f} (mult={ADDON_BUYING_POWER_HEADROOM_MULT}, incl "
                       f"${_reserved_other:,.0f} reserved for {other_tickers}) available=${buying_power:,.0f}", source=source)
            raise SafetyViolation(
                f"add-on order notional ${notional:,.0f} x {ADDON_BUYING_POWER_HEADROOM_MULT} headroom"
                + (f" + ${_reserved_other:,.0f} reserved for {other_tickers}'s resting BUY(s)" if other_tickers else "")
                + f" = ${required:,.0f} required, but '{account}' only has ${buying_power:,.0f} buying power"
            )
        signals_db.log_coverage_event(
            "addon_buying_power_check", _mode, ticker=ticker, node_id=_node_id, result="passed",
            detail=(f"required=${required:,.0f} (incl ${_reserved_other:,.0f} reserved for "
                    f"{other_tickers}) available=${buying_power:,.0f}") if other_tickers else
                   f"required=${required:,.0f} available=${buying_power:,.0f}", source=source)
        if other_tickers:
            # Mirrors the non-addon `second_ticker_buy_allowed` event below --
            # makes the 2026-08-10 relaxation's payoff observable the same
            # way (2026-08-10, follow-up #4 from that fix's paired review).
            signals_db.log_coverage_event(
                "addon_second_ticker_buy_allowed", _mode, ticker=ticker, node_id=_node_id,
                result="allowed_buying_power_sufficient",
                detail=f"account={account} other_tickers={other_tickers} reserved=${_reserved_other:,.0f} "
                       f"notional=${notional:,.0f} available=${buying_power:,.0f}", source=source)
    elif side == "BUY":
        import schwab_client  # local import: schwab_client imports this module at load time
        try:
            cash_available = schwab_client.get_account_balance(account)
        except Exception as e:
            signals_db.log_coverage_event(
                "cash_check", _mode, ticker=ticker, node_id=_node_id, result="failed_closed", detail=str(e), source=source)
            raise SafetyViolation(f"could not verify '{account}' cash balance, blocking order: {e}")
        # Reserve for EVERY other ticker's resting BUY found above (2026-08-07,
        # replaces the old unconditional block) -- since Schwab doesn't
        # decrement cash for a resting order, `cash_available` here is the
        # SAME undecremented balance each other order's own cash check
        # already passed against. Reserving REAL quantity (straight from the
        # broker's own order book, _open_buy_order_quantity) x CURRENT price
        # -- not the node's configured starting_notional -- models "is there
        # still enough for all of them if they all eventually fill" using
        # the actual committed size, not a config target. Found by
        # contextual Opus review before this shipped: starting_notional can
        # diverge sharply from the real resting order's size (LABD's real
        # order needed $208, its node's starting_notional is $500 -- padded/
        # conservative trailing-buy sizing and top-ups mean either direction
        # is possible), which would either over-reserve (blocking a sibling
        # for capital never actually committed -- directly undermining the
        # whole point of this fix) or under-reserve (a gap-resize/top-up
        # order sized above starting_notional). Also sidesteps the
        # get_watch_list_node ambiguity gap a starting_notional lookup would
        # hit for an account with several same-ticker nodes (e.g. `ira`'s
        # AGQ/SOXL canary rows) -- no node lookup needed at all here.
        # Summed across ALL of other_tickers, not just the first (fixed same
        # session, cold Opus review: the single-order version silently
        # under-reserved once the block was relaxed enough to let 2+ resting
        # BUYs coexist). Fails closed (blocks) if a live price can't be
        # fetched for any of them -- same conservative posture as the guard
        # it replaces, not a silent $0 assumption.
        _reserved_other = 0.0
        _unpriced = []
        for _ot in other_tickers:
            _ot_qty = _open_buy_order_quantity(orders, _ot)
            try:
                _ot_price = schwab_client.get_current_price(_ot)
            except Exception:
                _unpriced.append(_ot)
                continue
            _reserved_other += _ot_qty * _ot_price
        if _unpriced:
            signals_db.log_coverage_event(
                "second_ticker_buy_blocked", _mode, ticker=ticker, node_id=_node_id,
                result="blocked_unpriced", detail=f"account={account} other_tickers={other_tickers} "
                                                   f"unpriced={_unpriced}", source=source)
            raise SafetyViolation(
                f"account '{account}' already has resting BUY order(s) for {other_tickers}, and a live "
                f"price couldn't be fetched for {_unpriced} to estimate reserved cash -- refusing a "
                f"second concurrent BUY"
            )
        required = notional + _reserved_other + CASH_SAFETY_BUFFER
        # margin_floor lets a genuine full-margin account's cash legitimately
        # go negative down to its real borrowing capacity (default 0.0 for
        # every account today -- see AccountLimits.margin_floor's docstring).
        if cash_available - required < limits.margin_floor:
            signals_db.log_coverage_event(
                "cash_check", _mode, ticker=ticker, node_id=_node_id, result="blocked_insufficient",
                detail=f"required=${required:,.0f} (incl ${_reserved_other:,.0f} reserved for "
                       f"{other_tickers}) available=${cash_available:,.0f} margin_floor=${limits.margin_floor:,.0f}", source=source)
            raise SafetyViolation(
                f"order notional ${notional:,.0f}"
                + (f" + ${_reserved_other:,.0f} reserved for {other_tickers}'s resting BUY(s)" if other_tickers else "")
                + f" + ${CASH_SAFETY_BUFFER:,.0f} cash buffer = "
                f"${required:,.0f} required, but '{account}' only has ${cash_available:,.0f} available"
                + (f" (margin floor ${limits.margin_floor:,.0f})" if limits.margin_floor else "")
            )
        signals_db.log_coverage_event(
            "cash_check", _mode, ticker=ticker, node_id=_node_id, result="passed",
            detail=(f"required=${required:,.0f} (incl ${_reserved_other:,.0f} reserved for "
                    f"{other_tickers}) available=${cash_available:,.0f}") if other_tickers else
                   f"required=${required:,.0f} available=${cash_available:,.0f}", source=source)
        if other_tickers:
            signals_db.log_coverage_event(
                "second_ticker_buy_allowed", _mode, ticker=ticker, node_id=_node_id, result="allowed_cash_sufficient",
                detail=f"account={account} other_tickers={other_tickers} reserved=${_reserved_other:,.0f} "
                       f"notional=${notional:,.0f} available=${cash_available:,.0f}", source=source)
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
                detail=f"account={account} count={count} cap={limits.daily_order_cap} side={side}", source=source)
        else:
            signals_db.log_coverage_event("daily_order_cap_block", _mode, ticker=ticker,
                                           node_id=_node_id, result="blocked",
                                           detail=f"account={account} count={count} cap={limits.daily_order_cap}", source=source)
            raise SafetyViolation(f"account '{account}' has hit its daily order cap ({limits.daily_order_cap})")

    recent = [t for t in counts.get("recent_order_timestamps", []) if time.time() - t < 60]
    if len(recent) >= GLOBAL_ORDERS_PER_MINUTE:
        signals_db.log_coverage_event("global_burst_cap_block", _mode, ticker=ticker,
                                       node_id=_node_id, result="blocked",
                                       detail=f"recent={len(recent)} max={GLOBAL_ORDERS_PER_MINUTE}", source=source)
        raise SafetyViolation(
            f"global burst cap hit ({len(recent)} orders across all accounts in the last minute, "
            f"max {GLOBAL_ORDERS_PER_MINUTE})"
        )

    # SELL-side fingerprint exemption when a known open addon leg exists for
    # this node (D3, docs/plans/real_order_execution_drought_addon.md 6.2) --
    # found by cold Opus review before this shipped, a real collision beyond
    # the resting-order guards: a leg's own SELL (its D3 protective stop)
    # always has the exact same (account, ticker, side, quantity) as the
    # parent core position's own SELL, since the leg mirrors the parent's
    # shares exactly by construction -- true in BOTH directions (the leg's
    # placement looks like a dup of the parent's recent SELL, and the
    # PARENT's own later exit then looks like a dup of the leg's recent
    # SELL). Gating on is_addon_leg alone only fixed the first direction;
    # the fingerprint check has no way to distinguish "core" from "leg" by
    # side/quantity/ticker, so it must be skipped for SELL whenever this
    # node has ANY open leg, not just when the current call is the leg's
    # own placement. The real-account fallback below (_broker_confirms_
    # order) makes this WORSE, not better, in this specific case: it treats
    # the OTHER leg's genuinely-resting SELL as proof this is a confirmed
    # duplicate retry, when it's actually proof a second, different, wanted
    # order is deliberately being placed alongside it.
    # replacing_order_id: hybrid, not a blanket skip (2026-08-17, backlog item
    # found alongside the drought-addon exemptions above; corrected same day
    # after paired review found a first-draft blanket skip too broad -- see
    # tests/test_replacing_order_id_dup_window_exemption_scenario.py's module
    # docstring for the full history). For a trading_enabled account there IS
    # a real broker book to check against, so the fingerprint loop below stays
    # live and replacing_order_id is threaded into _broker_confirms_order as
    # exclude_order_id instead -- a genuinely separate confirming order still
    # blocks, only the replace's own known target is excused from counting as
    # confirmation. Only for a non-trading_enabled (dry_run) account, with no
    # broker book to check, does this fall back to a blanket skip.
    _skip_dup_window = is_addon_leg or (replacing_order_id is not None and not limits.trading_enabled)
    if side == "SELL" and not _skip_dup_window and _node_id is not None:
        if signals_db.get_open_addon_leg_by_wl_id(_node_id) is not None:
            _skip_dup_window = True

    # HANDOFF-side fingerprint exemption (2026-08-17, mirrors the addon-leg
    # case immediately above): check_drought_handoff's own exit replace can
    # race _reconcile_buy_fill's just-placed protective SL for the SAME
    # newly-opened drought position -- identical (account, ticker, side,
    # quantity) fingerprint, milliseconds apart, well inside
    # DUPLICATE_ORDER_WINDOW_SECS. Unlike is_addon_leg, the caller's own
    # is_handoff_exit flag is NOT trusted alone -- verified fresh against a
    # real open drought position on file AND a non-null replacing_order_id
    # (HANDOFF's exit always goes through the atomic replace path; a flag
    # set without a real replace target is either a caller bug or a genuinely
    # unrelated SELL that happens to be mislabeled, and must not silently
    # skip the guard). Failing either check falls through to the normal
    # duplicate-window logic below rather than raising immediately -- the
    # underlying order may still be legitimate on its own merits (e.g. the
    # broker-confirmation fallback a few lines down).
    if side == "SELL" and not _skip_dup_window and is_handoff_exit and replacing_order_id is not None \
            and _node_id is not None:
        _drought_pos = signals_db.get_drought_overlay_position(_node_id)
        # Not just "some drought position exists" -- replacing_order_id must match
        # THAT position's own resting order (sl_order_id, or trail_state's
        # exit_order_id for the hold-time-forced path -- see
        # _attempt_automated_exit_sell's identical resolution logic) before
        # exempting. Found by independent-cold + contextual review, 2026-08-17:
        # presence-only was safe today only by coincidence of the single call
        # site deriving replacing_order_id from this same position.
        _drought_order_id = None
        if _drought_pos is not None:
            _drought_order_id = _drought_pos.get('sl_order_id') or \
                (_drought_pos.get('trail_state') or {}).get('exit_order_id')
        if _drought_pos is not None and replacing_order_id == _drought_order_id:
            _skip_dup_window = True
        else:
            signals_db.log_coverage_event(
                "drought_handoff_precondition_blocked", _mode, ticker=ticker, node_id=_node_id,
                result="not_exempted",
                detail=f"is_handoff_exit=True but replacing_order_id={replacing_order_id} doesn't match "
                       f"the open drought position's own order (wl_id={_node_id}, "
                       f"drought_position={'none' if _drought_pos is None else 'found'}, "
                       f"drought_order_id={_drought_order_id})",
                source=source)

    for o in ([] if _skip_dup_window else counts.get("recent_orders", [])):
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
        if limits.trading_enabled and not _broker_confirms_order(_all_orders(account), ticker, side, quantity,
                                                                    exclude_order_id=replacing_order_id):
            signals_db.log_coverage_event(
                "dup_order_retry_after_failure", _mode, ticker=ticker, node_id=_node_id, result="allowed_retry",
                detail=f"side={side} qty={quantity}", source=source)
            continue
        signals_db.log_coverage_event(
            "dup_order_window_blocked", _mode, ticker=ticker, node_id=_node_id, result="blocked",
            detail=f"side={side} qty={quantity} prior_qty={prior_qty:g}", source=source)
        raise SafetyViolation(
            f"duplicate order: {side} {quantity} {ticker} in {account} already submitted "
            f"{prior_qty:g} shares {time.time() - o['ts']:.0f}s ago "
            f"(within {DUPLICATE_ORDER_WINDOW_SECS}s window, {DUPLICATE_ORDER_QUANTITY_TOLERANCE_PCT}% tolerance)"
        )


def approve_and_record(
    account: str, ticker: str, quantity: int, price: float, side: str, is_gap_correction: bool = False,
    is_protective: bool = False, replacing_order_id: int | None = None, is_addon_leg: bool = False,
    node_dry_run: bool = False, node_id: int | None = None, source: str = 'daemon',
    is_handoff_exit: bool = False,
) -> bool:
    """Call immediately before placing a real order. Raises SafetyViolation if
    blocked; otherwise records the order against the daily cap, the global
    per-minute burst cap, and the duplicate-order window, and returns whether
    the order should be simulated rather than actually submitted (caller must
    skip the real API call if so). Checks and increments happen under the
    same file lock so two concurrent callers can't both slip past a cap.
    is_gap_correction bypasses only the signal-window time gate (see
    check_order) -- Part 3, branch B.
    is_protective bypasses only the daily_order_cap check, for a post-fill
    top-up BUY -- see check_order's docstring (SELL, including a stop-loss
    placement, is unconditionally exempt from this cap since 2026-07-25
    regardless of is_protective).
    replacing_order_id -- see check_order's docstring; threaded through by
    replace_equity_order_with_market/replace_order_with_trailing_sell so an
    atomic replace call doesn't self-block on the exact order it's replacing.
    node_dry_run: the per-node dry_run override (docs/backlog_cache.md, added
    2026-08-1x) -- additive/OR-logic only against the account-level flag,
    never a replacement. All the real safety guards in check_order still run
    unconditionally regardless of either flag; this only decides whether the
    caller actually submits to the broker afterward.
    node_id: threaded straight through to check_order's node_id param (see
    its docstring) -- schwab_client's place_*/replace_* functions pass this
    from the real node dict already in scope at their call sites.
    source: coverage_events write-attribution -- threaded straight through to
    check_order's source param (see its docstring); defaults to 'daemon'.
    is_handoff_exit: threaded straight through to check_order's is_handoff_exit
    param (see its docstring)."""
    with _open_locked() as f:
        counts = json.loads(f.read() or "{}")
        check_order(account, ticker, quantity, price, side, counts=counts,
                    is_gap_correction=is_gap_correction, is_protective=is_protective,
                    replacing_order_id=replacing_order_id, is_addon_leg=is_addon_leg,
                    node_id=node_id, source=source, is_handoff_exit=is_handoff_exit)
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
    return (not ACCOUNTS[account].trading_enabled) or node_dry_run
