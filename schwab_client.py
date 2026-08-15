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
from datetime import datetime, timezone

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
import signals_db
from signals_blocks import _post_message
# mode_tag (2026-08-14): tags the alerts below LIVE/DRY-RUN/UNKNOWN, so a
# real-money order event is distinguishable at a glance from a simulated one.
# Deliberately applied only where the message text itself doesn't already
# settle the question -- every post-approval confirmation here is reachable
# ONLY after the `if dry_run:` early-return, and says "submitted to
# Schwab"/"FILLED"/"by Schwab" (live-only vocabulary), while the simulated
# path always says "[DRY RUN] would ...". The SafetyViolation and cancel
# paths have neither property, which is why they get the tag.
# Import is safe: signals_helpers reaches only schwab_safety/signals_config/
# signals_db, none of which import this module back.
from signals_helpers import mode_tag


def _mode_tag_for(account, node_id):
    """mode_tag(account) alone can't see a node-level dry_run/state override
    -- every one of these BLOCKED/AMBIGUOUS/cancel alerts already has node_id
    in scope, so resolve the real node and pass it through (found in review,
    2026-08-15: several call sites here were passing account alone, which
    over-labels a dry-run node's blocked order as LIVE)."""
    node = signals_db.get_watch_list_node_by_id(node_id) if node_id is not None else None
    return mode_tag(account, node)

_client = None
_account_hashes = None  # nickname -> Schwab's encrypted account hash, resolved lazily


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

# Real broker-clock vs local-clock skew tolerance for _find_recent_matching_order's
# since_ts cutoff -- widened backward a few seconds so a genuinely-just-placed
# order isn't excluded for landing at the broker fractionally "before" this
# process's own since_ts read. Small on purpose: too wide risks matching an
# unrelated older order of the same shape (see that function's docstring).
_RETRY_CLOCK_SKEW_BUFFER_SECS = 5


class _AmbiguousBrokerState(Exception):
    """Raised by _find_recent_matching_order when more than one real order
    could be the result of a prior retry attempt whose response was lost --
    deliberately never auto-resolved. A retry loop that hits this stops
    retrying and lets the exception propagate (same as any other submission
    failure) rather than guess which candidate, if any, is the real one."""


def _parse_broker_timestamp(ts):
    """Tolerant ISO-8601 parser for Schwab's enteredTime field. Handles a
    trailing 'Z' and a non-colon UTC offset (e.g. '+0000', common in
    brokerage APIs and not accepted by Python 3.10's datetime.fromisoformat
    without normalization) in addition to the already-valid form fake_broker
    produces. Returns a timezone-aware datetime, or None if unparseable --
    callers must treat None as "can't confirm, don't use this as evidence."""
    if not ts:
        return None
    ts = ts.replace('Z', '+00:00')
    if len(ts) >= 5 and ts[-5] in '+-' and ts[-3] != ':':
        ts = ts[:-2] + ':' + ts[-2:]
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _find_recent_matching_order(account, ticker, side, quantity, order_type, since_ts, baseline_ids):
    """Looks for a real order at the broker that could be the result of a
    prior retry attempt whose local response was lost to a flapping
    connection (the request landed at the broker, but the client-side
    exception/timeout meant we never saw the confirmation) -- the gap named
    in _submit_replace_with_retry's 2026-07-27 docstring, generalized to run
    before EVERY retry attempt (N>1), not left as an accepted single-drop
    risk only.

    Scope is deliberately narrow -- a candidate must pass ALL of:
      - ticker+side+quantity+orderType match exactly (order_type matters:
        found in review, a just-placed STOP LOSS is otherwise
        indistinguishable from a same-ticker/side/quantity TRAILING_STOP
        resting for an unrelated reason, e.g. an addon leg's own SL placed
        right after the parent leg's);
      - status isn't one of schwab_safety._DUPLICATE_NOT_CONFIRMED_STATUSES
        (CANCELED/EXPIRED/REJECTED/REPLACED -- the same "broker never
        actually accepted/executed this" bucket the existing duplicate-
        order guard already uses; found in review -- without this a
        REJECTED prior attempt could be mistaken for a real success,
        leaving a position genuinely unprotected while the caller believes
        it succeeded);
      - orderId not already present in `baseline_ids` (every order for this
        ticker that existed BEFORE this call's very first attempt, snapshotted
        once up front) -- the primary defense against matching a stale,
        unrelated pre-existing order (found in review: relying on the
        entered-time cutoff alone, with its several-second clock-skew
        buffer, could match a genuinely different order placed moments
        before this call started). `baseline_ids=None` means the baseline
        snapshot itself couldn't be taken (e.g. the same connection trouble
        this whole check exists to see through) -- this filter is then
        skipped, falling back to the entered-time cutoff alone (the
        original, slightly weaker guarantee), not to no check at all;
      - entered no earlier than `since_ts` minus a small clock-skew buffer
        (belt-and-suspenders alongside the baseline-id filter above, and the
        only signal available when baseline_ids is None).

    Does NOT catch exceptions from the underlying broker read (get_real_orders)
    -- callers must handle that themselves, since only they know whether/how
    to log the "couldn't confirm broker state" case distinctly from "confirmed
    no match."

    Returns None (no match found -- safe to proceed with a fresh retry), the
    single matching order dict (treat as this call's own placement having
    already succeeded), or raises _AmbiguousBrokerState if more than one
    candidate exists (fail safe -- never guess between them)."""
    orders = get_real_orders(account, ticker)
    cutoff = since_ts - _RETRY_CLOCK_SKEW_BUFFER_SECS
    candidates = []
    for o in orders:
        if o.get('instruction') != side:
            continue
        if o.get('orderType') != order_type:
            continue
        if o.get('status') in schwab_safety._DUPLICATE_NOT_CONFIRMED_STATUSES:
            continue
        try:
            if int(o.get('quantity') or 0) != int(quantity):
                continue
        except (TypeError, ValueError):
            continue
        if baseline_ids is not None and o.get('orderId') in baseline_ids:
            continue
        entered_dt = _parse_broker_timestamp(o.get('enteredTime'))
        if entered_dt is None or entered_dt.timestamp() < cutoff:
            continue
        candidates.append(o)
    if not candidates:
        return None
    if len(candidates) > 1:
        raise _AmbiguousBrokerState(
            f"{len(candidates)} matching {side} {quantity} {order_type} {ticker} orders found in "
            f"{account} entered since {since_ts}: {[c.get('orderId') for c in candidates]}")
    return candidates[0]


def _check_broker_before_retry(account, ticker, side, quantity, order_type, node_id, since_ts, baseline_ids,
                                attempt, last_exc=None):
    """Shared by _submit_order_with_retry/_submit_replace_with_retry: called
    right before a retry attempt (never the first attempt -- there's nothing
    to re-check yet), and once more after the final attempt fails (see the
    two retry functions below) -- landing-but-lost-response can happen on
    ANY attempt, including the last one, so the check must run after it too,
    not just before attempts 2..N. Returns an existing order_id if this
    retry should be skipped as a duplicate, else None (safe to proceed with
    a fresh retry, covering both "confirmed no match" and "couldn't confirm
    broker state at all" -- the latter fails toward the pre-existing blind-
    retry behavior, but unlike before 2026-08-15 it's now a logged, visible
    event instead of a silent no-op). Always logs a coverage_events row,
    matching the project's existing detection-logging convention
    (_log_pre_action_state_verification/record_node_streak in
    schwab_safety.py) -- 'prevented' (duplicate caught), 'ambiguous' (fail-
    safe halt, also posts an immediate Slack alert since this is a real
    "verify broker state directly" situation, not a routine retry), or
    'check_failed' (broker read itself failed).

    last_exc (the most recent real submission error, always set by the time
    this is called -- there's always been at least one failed attempt by
    then): chained onto a raised _AmbiguousBrokerState via `raise ... from`
    so the original connection error isn't silently dropped from whatever
    traceback/alert a caller further up eventually builds (found in review,
    2026-08-15 -- discarding it made the ambiguous case's own root cause
    harder to reconstruct after the fact)."""
    try:
        existing = _find_recent_matching_order(account, ticker, side, quantity, order_type, since_ts, baseline_ids)
    except _AmbiguousBrokerState as e:
        signals_db.log_coverage_event(
            'order_retry_duplicate_prevented', mode='live', ticker=ticker, node_id=node_id,
            result='ambiguous',
            detail=f"retry attempt {attempt + 1}: {e}")
        _post_message(
            f"\U0001F6A8 AMBIGUOUS BROKER STATE {side} {quantity} {ticker} in {account} "
            f"({_mode_tag_for(account, node_id)}): retry attempt {attempt + 1} found multiple real orders that "
            f"could be the result of a lost prior attempt -- halting retries rather than guessing "
            f"which (if any) is real. Check the broker's live order book directly before taking any "
            f"further action. ({e})", node_id=node_id)
        raise e from last_exc
    except Exception as e:
        # Broker state itself is unreachable right now (e.g. the same
        # connection flap this whole check exists to see through) -- fail
        # toward the pre-existing behavior (retry blind) rather than block
        # on a check that can't itself be answered, but log it so a repeat
        # of this isn't silently invisible the way it was before 2026-08-15.
        signals_db.log_coverage_event(
            'order_retry_duplicate_prevented', mode='live', ticker=ticker, node_id=node_id,
            result='check_failed',
            detail=f"retry attempt {attempt + 1}: couldn't read broker order book ({e})")
        return None
    if existing is None:
        return None
    existing_order_id = existing.get('orderId')
    if existing_order_id is None:
        # Malformed/unexpected broker response shape (a match was found but
        # it has no orderId) -- found in review, 2026-08-15: logging
        # 'prevented' here while returning None would have been misleading
        # (the caller falls through to a fresh blind retry regardless, since
        # None means "not skipped," so nothing was actually prevented).
        # Treat the same as "couldn't confirm" rather than claim a save that
        # didn't happen.
        signals_db.log_coverage_event(
            'order_retry_duplicate_prevented', mode='live', ticker=ticker, node_id=node_id,
            result='check_failed',
            detail=f"retry attempt {attempt + 1}: matched order has no orderId ({existing})")
        return None
    signals_db.log_coverage_event(
        'order_retry_duplicate_prevented', mode='live', ticker=ticker, node_id=node_id,
        result='prevented',
        # order_type/quantity included explicitly (2026-08-16 session-wrap
        # review finding): a single-candidate match is adopted on
        # ticker+side+quantity+orderType+status alone -- if a core position's
        # SL and its add-on leg's SL land within the same retry window with
        # matching quantities, this could adopt the wrong sibling's order id.
        # No code change to the matching logic (user's call: the downstream
        # live-state-reconciliation check would likely surface a real
        # misattribution as a broker-state mismatch, and an add-on's missing
        # "sister" order is itself a tell) -- this just makes the adopted
        # match's exact shape inspectable after the fact if that's ever
        # suspected, instead of only "an order was adopted."
        detail=f"retry attempt {attempt + 1} skipped -- order {existing_order_id} "
               f"already at broker (status={existing.get('status')}, order_type={order_type}, "
               f"quantity={quantity})")
    return existing_order_id


def _snapshot_baseline_order_ids(account, ticker, can_check):
    """Every real orderId that already exists for this ticker/account BEFORE
    the retry loop's first attempt -- the primary defense in
    _find_recent_matching_order against matching a stale, pre-existing order
    instead of one this call itself just placed. Returns None (not an empty
    set) if the snapshot itself couldn't be taken, so callers can tell
    "confirmed nothing pre-existed" apart from "unknown" -- the latter falls
    back to entered-time-only matching rather than skipping the whole check."""
    if not can_check:
        return None
    try:
        return {o.get('orderId') for o in get_real_orders(account, ticker)}
    except Exception:
        return None


def _submit_order_with_retry(account_hash, order, account=None, ticker=None, side=None,
                              quantity=None, order_type=None, node_id=None):
    """account/ticker/side/quantity/order_type (added 2026-08-15): when all
    five are given (every real call site now passes them), a retry attempt
    (N>1), AND one final check after the last attempt fails, first checks
    whether a PRIOR attempt's request actually landed at the broker despite
    a locally-observed exception -- a flapping connection (up-down-up), not
    just the single clean drop this loop already retried blind for. If a
    matching order is already resting/filled at the broker, that's treated
    as this call's own success (no resubmission); if broker state is
    ambiguous, this stops retrying and raises rather than guess. See
    _find_recent_matching_order for the exact matching rule.

    Returns the order_id directly (not the raw response) -- no caller uses
    the raw response for anything beyond Utils(...).extract_order_id(r),
    now done internally here so the fresh-placement and duplicate-recovery
    paths return the same shape."""
    last_exc = None
    since_ts = time.time()
    can_check = None not in (account, ticker, side, quantity, order_type)
    baseline_ids = _snapshot_baseline_order_ids(account, ticker, can_check)
    for attempt in range(_ORDER_SUBMIT_RETRY_ATTEMPTS):
        if attempt > 0 and can_check:
            existing_id = _check_broker_before_retry(account, ticker, side, quantity, order_type,
                                                       node_id, since_ts, baseline_ids, attempt, last_exc)
            if existing_id is not None:
                return existing_id
        try:
            r = _get_client().place_order(account_hash, order)
            r.raise_for_status()
        except Exception as e:
            last_exc = e
            if attempt < _ORDER_SUBMIT_RETRY_ATTEMPTS - 1:
                time.sleep(_ORDER_SUBMIT_RETRY_INTERVAL_SECS)
        else:
            # Deliberately outside the except above (found in review,
            # 2026-08-15): a genuinely successful placement whose response
            # extract_order_id can't parse must NOT be treated as a
            # retryable submission failure -- that would fire a real
            # duplicate resubmission for an order that already landed fine,
            # the opposite of a case this whole fix exists to prevent.
            # Matches the pre-2026-08-15 behavior, where extraction happened
            # in the caller, entirely outside this loop's retry semantics.
            return Utils(_get_client(), account_hash).extract_order_id(r)
    if can_check:
        # The LAST attempt itself could have landed at the broker with the
        # response lost, same as any earlier attempt (found in review,
        # 2026-08-15) -- under sustained flapping this is actually the
        # likeliest attempt to be missed by an "only check before attempts
        # 2..N" version of this fix, since it's the one attempt after which
        # no further retry would otherwise re-check broker state at all.
        existing_id = _check_broker_before_retry(account, ticker, side, quantity, order_type,
                                                   node_id, since_ts, baseline_ids,
                                                   _ORDER_SUBMIT_RETRY_ATTEMPTS - 1, last_exc)
        if existing_id is not None:
            return existing_id
    raise last_exc


def _submit_replace_with_retry(account_hash, order_id, order, account=None, ticker=None,
                                side=None, quantity=None, order_type=None, node_id=None):
    """Same retry shape as _submit_order_with_retry, for schwab-py's
    replace_order (cancel-old + create-new as a single broker call), and the
    same pre-retry (and post-final-attempt) broker-state check (added
    2026-08-15 -- see _submit_order_with_retry's docstring and
    _find_recent_matching_order).

    Previously-accepted residual risk (named 2026-07-27) is now closed for
    every attempt, not just "some attempt after the first": if ANY attempt's
    replace_order request actually lands at the broker (old order canceled,
    new one created) but the client-side response handling then raises
    (timeout, malformed response after a real success), the very next check
    -- whether that's before the next retry, or the final check run after
    the last attempt exhausts -- finds the new order and returns it instead
    of firing another replace_order against an order_id that's already dead
    (or resubmitting a fresh, duplicate replacement).

    What remains genuinely open is narrower than "an attempt this loop
    doesn't check": it's the inherent limit of any check-then-act pattern --
    if the broker accepts a request a moment AFTER this loop's own broker-
    state read completes (a race, not a coverage gap), no synchronous check
    can see that yet. This is a fundamentally different, much smaller
    exposure window than the old "any attempt after the first could go
    fully unverified" gap, and every caller's UNPROTECTED/manual-fallback
    messaging already assumes some form of this residual ambiguity."""
    last_exc = None
    since_ts = time.time()
    can_check = None not in (account, ticker, side, quantity, order_type)
    baseline_ids = _snapshot_baseline_order_ids(account, ticker, can_check)
    for attempt in range(_ORDER_SUBMIT_RETRY_ATTEMPTS):
        if attempt > 0 and can_check:
            existing_id = _check_broker_before_retry(account, ticker, side, quantity, order_type,
                                                       node_id, since_ts, baseline_ids, attempt, last_exc)
            if existing_id is not None:
                return existing_id
        try:
            r = _get_client().replace_order(account_hash, order_id, order)
            r.raise_for_status()
        except Exception as e:
            last_exc = e
            if attempt < _ORDER_SUBMIT_RETRY_ATTEMPTS - 1:
                time.sleep(_ORDER_SUBMIT_RETRY_INTERVAL_SECS)
        else:
            # See _submit_order_with_retry's matching comment -- a parse
            # failure on a genuinely successful replace must not trigger a
            # retryable-failure resubmission.
            return Utils(_get_client(), account_hash).extract_order_id(r)
    if can_check:
        existing_id = _check_broker_before_retry(account, ticker, side, quantity, order_type,
                                                   node_id, since_ts, baseline_ids,
                                                   _ORDER_SUBMIT_RETRY_ATTEMPTS - 1, last_exc)
        if existing_id is not None:
            return existing_id
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


def _post_order_confirmation(label, account_hash, order_id, ticker, account, submitted_msg, node_id=None):
    """Shared by every real placement call site: polls the real status and posts
    an accurate Slack message instead of always claiming success. A confirmed
    REJECTED/CANCELED/EXPIRED gets a distinct 🚫 alert and raises OrderRejected
    so the caller falls back to the manual flow instead of treating this as a
    successful placement; FILLED gets an immediate fill confirmation; anything
    else (still resting, or unconfirmed) falls back to the existing optimistic
    'submitted' message, which remains accurate for a genuinely-resting order.
    node_id: passed through to _post_message's noise-reduction gate (2026-08-13)."""
    status = _confirm_order_status(account_hash, order_id)
    if status in _ORDER_TERMINAL_BAD_STATUSES:
        _post_message(f"\U0001F6AB {label} {ticker} in {account} was {status} by Schwab "
                       f"(order {order_id}) — not resting, no position/order resulted", node_id=node_id)
        raise OrderRejected(f"{label} {ticker} order {order_id} was {status}")
    elif status == "FILLED":
        _post_message(f"✅ {label} {ticker} in {account} FILLED immediately (order {order_id})", node_id=node_id)
    else:
        _post_message(submitted_msg, node_id=node_id)


def _live_nicknames() -> list:
    """Non-retired account aliases from signals_db's `accounts` table --
    replaces the old hardcoded NICKNAMES list (2026-08-11), which was a
    second, independent nickname source kept in sync with schwab_safety.
    ACCOUNTS only by convention. Queried fresh on every call rather than
    cached separately -- cheap (one indexed SELECT), and the one real
    consumer (_resolve_account_hashes below) already caches ITS OWN result
    for the process lifetime, so this only actually runs once in practice."""
    conn = signals_db._conn()
    try:
        return [r[0] for r in conn.execute(
            "SELECT alias FROM accounts WHERE retired_at IS NULL ORDER BY alias"
        ).fetchall()]
    finally:
        conn.close()


def _resolve_account_hashes() -> dict:
    global _account_hashes
    if _account_hashes is not None:
        return _account_hashes

    # env vars hold only an account-number suffix (e.g. last 3-4 digits, as
    # shown in Schwab's own masked UI) -- the full number never needs to be
    # typed/stored, just enough digits to be unambiguous among linked accounts.
    suffixes = {n: os.environ.get(f"SCHWAB_ACCOUNT_{n.upper()}") for n in _live_nicknames()}
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


def resolve_account_alias_from_number(raw_account_number: str) -> str:
    """Reverse of the suffix match _resolve_account_hashes does above, for the
    one caller (signals_notify.drain_fill_queue) that only has Schwab's raw
    ACCT_ACTIVITY-stream `AccountNumber` (e.g. "45111931"), not an alias --
    found 2026-08-16, the fast fill-reconciliation path was passing that raw
    number straight into get_filled_order(account=...), which looks it up in
    _resolve_account_hashes()'s dict (keyed by alias, never a raw number),
    raising KeyError before anything could reconcile. Deliberately no new
    lookup table/cache -- reuses the exact same SCHWAB_ACCOUNT_<ALIAS> suffix
    env vars _resolve_account_hashes already trusts, applied directly against
    the raw number instead of against the API's accountNumber field. Raises
    loudly (mirroring _resolve_account_hashes's own ambiguous-match guard
    just above) on 0 or 2+ matches rather than silently guessing."""
    matches = [n for n in _live_nicknames()
               if os.environ.get(f"SCHWAB_ACCOUNT_{n.upper()}")
               and raw_account_number.endswith(os.environ[f"SCHWAB_ACCOUNT_{n.upper()}"])]
    if len(matches) > 1:
        raise ValueError(
            f"AccountNumber '{raw_account_number}' matches {len(matches)} live account "
            f"aliases ({matches}) via SCHWAB_ACCOUNT_* suffixes -- use more digits to disambiguate"
        )
    if not matches:
        raise ValueError(
            f"AccountNumber '{raw_account_number}' does not match any SCHWAB_ACCOUNT_<ALIAS> suffix"
        )
    return matches[0]


def _build_market_order(side: str, ticker: str, quantity: int):
    order_fn = equity_orders.equity_buy_market if side == "BUY" else equity_orders.equity_sell_market
    return order_fn(ticker, quantity)


def _place_equity_order(
    side: str, account: str, ticker: str, quantity: int, price: float, is_gap_correction: bool = False,
    is_protective: bool = False, is_addon_leg: bool = False, node_dry_run: bool = False,
    node_id: int | None = None,
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
            is_protective=is_protective, is_addon_leg=is_addon_leg, node_dry_run=node_dry_run,
            node_id=node_id)
    except schwab_safety.SafetyViolation as e:
        _post_message(f"\U0001F6AB BLOCKED {side} {quantity} {ticker} in {account} ({_mode_tag_for(account, node_id)}): {e}",
                      node_id=node_id)
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True, node_id=node_id)
        raise

    # is_protective (top-up) and is_addon_leg are mutually exclusive in
    # practice (top-ups only fire on the core position, add-on legs never
    # trigger a top-up of themselves) -- both checked explicitly rather than
    # assumed, so a message never silently drops a real distinction. Found
    # 2026-08-12: a top-up buy's FILLED-immediately confirmation looked
    # identical to the original entry fill's ("BUY YINN in soxl_ira FILLED
    # immediately" twice, no distinguishing text), reading as a possible
    # duplicate-order bug when it was the designed top-up mechanism working
    # correctly -- see docs/deep_backlog.md's 2026-08-12 (night) entry.
    # is_protective is only meaningful as "TOP-UP" for a BUY (schwab_safety's
    # own protective-order flag also covers a protective SELL/exit, where
    # "TOP-UP SELL" would be nonsense) -- gated on side too, found by paired
    # review 2026-08-13. Currently dead in practice (the only real
    # is_protective=True caller is the top-up BUY), kept defensive rather
    # than assumed since this label logic is now shared by 3 functions.
    _label = "TOP-UP " if (is_protective and side == "BUY") else ("ADD-ON " if is_addon_leg else "")
    if dry_run:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False, node_id=node_id)
        _post_message(f"[DRY RUN] would {_label}{side} {quantity} {ticker} in {account} (~${quantity * price:,.0f})", node_id=node_id)
        print(f"[DRY RUN] would {_label}{side} {quantity} {ticker} in {account} (~${quantity * price:,.0f})")
        return None, None

    account_hash = _resolve_account_hashes()[account]
    order = _build_market_order(side, ticker, quantity)
    try:
        order_id = _submit_order_with_retry(account_hash, order, account=account, ticker=ticker,
                                             side=side, quantity=quantity,
                                             order_type=OrderType.MARKET.value, node_id=node_id)
        _post_order_confirmation(
            f"{_label}{side}", account_hash, order_id, ticker, account,
            f"✅ {_label}{side} {quantity} {ticker} in {account} submitted to Schwab (~${quantity * price:,.0f})",
            node_id=node_id)
    except Exception:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True, node_id=node_id)
        raise
    schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False, node_id=node_id)
    return None, order_id


def replace_equity_order_with_market(
    account: str, ticker: str, order_id: int, side: str, quantity: int, price: float,
    is_gap_correction: bool = False, is_protective: bool = False, is_addon_leg: bool = False,
    node_dry_run: bool = False, node_id: int | None = None,
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
            node_dry_run=node_dry_run, node_id=node_id)
    except schwab_safety.SafetyViolation as e:
        _post_message(f"\U0001F6AB BLOCKED replace {order_id} with MARKET {side} {quantity} {ticker} "
                      f"in {account} ({_mode_tag_for(account, node_id)}): {e}", node_id=node_id)
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True, node_id=node_id)
        raise

    # Same TOP-UP/ADD-ON labeling gap as _place_equity_order, found by
    # paired review 2026-08-13: this function shares is_protective/
    # is_addon_leg with it but never got the label -- an add-on leg's
    # exit-via-replace looked identical to the core position's own,
    # the same confusion shape as the YINN incident that started this.
    # is_protective is only meaningful as "TOP-UP" for a BUY (schwab_safety's
    # own protective-order flag also covers a protective SELL/exit, where
    # "TOP-UP SELL" would be nonsense) -- gated on side too, found by paired
    # review 2026-08-13. Currently dead in practice (the only real
    # is_protective=True caller is the top-up BUY), kept defensive rather
    # than assumed since this label logic is now shared by 3 functions.
    _label = "TOP-UP " if (is_protective and side == "BUY") else ("ADD-ON " if is_addon_leg else "")
    if dry_run:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False, node_id=node_id)
        msg = (f"[DRY RUN] would replace order {order_id} with {_label}MARKET {side} {quantity} {ticker} "
               f"in {account} (~${quantity * price:,.0f})")
        _post_message(msg, node_id=node_id)
        print(msg)
        return None, None

    account_hash = _resolve_account_hashes()[account]
    order = _build_market_order(side, ticker, quantity)
    try:
        new_order_id = _submit_replace_with_retry(account_hash, order_id, order, account=account,
                                                    ticker=ticker, side=side, quantity=quantity,
                                                    order_type=OrderType.MARKET.value, node_id=node_id)
        _post_order_confirmation(
            f"REPLACE->{_label}MARKET {side}", account_hash, new_order_id, ticker, account,
            f"✅ Replaced order {order_id} with {_label}MARKET {side} {quantity} {ticker} in {account} "
            f"(~${quantity * price:,.0f})", node_id=node_id)
    except Exception:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True, node_id=node_id)
        raise
    schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False, node_id=node_id)
    return None, new_order_id


def place_equity_buy(account: str, ticker: str, quantity: int, price: float, is_gap_correction: bool = False,
                      is_protective: bool = False, is_addon_leg: bool = False, node_dry_run: bool = False,
                      node_id: int | None = None):
    return _place_equity_order("BUY", account, ticker, quantity, price, is_gap_correction=is_gap_correction,
                                is_protective=is_protective, is_addon_leg=is_addon_leg, node_dry_run=node_dry_run,
                                node_id=node_id)


def place_equity_sell(account: str, ticker: str, quantity: int, price: float, is_addon_leg: bool = False,
                       node_dry_run: bool = False, node_id: int | None = None):
    return _place_equity_order("SELL", account, ticker, quantity, price, is_addon_leg=is_addon_leg,
                                node_dry_run=node_dry_run, node_id=node_id)


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
    node_id: int | None = None,
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
                                                     node_dry_run=node_dry_run, node_id=node_id)
    except schwab_safety.SafetyViolation as e:
        _post_message(f"\U0001F6AB BLOCKED {label} {quantity} {ticker} in {account} ({_mode_tag_for(account, node_id)}) "
                      f"(trail={trail_pct}%): {e}", node_id=node_id)
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True, node_id=node_id)
        raise

    if dry_run:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False, node_id=node_id)
        msg = (f"[DRY RUN] would place {label} {quantity} {ticker} in {account} "
               f"(trail={trail_pct}%, ~${quantity * price:,.0f})")
        _post_message(msg, node_id=node_id)
        print(msg)
        return None, None

    account_hash = _resolve_account_hashes()[account]
    order = _build_trailing_order(side, link_basis, ticker, quantity, trail_pct)
    try:
        order_id = _submit_order_with_retry(account_hash, order, account=account, ticker=ticker,
                                             side=side, quantity=quantity,
                                             order_type=OrderType.TRAILING_STOP.value, node_id=node_id)
        _post_order_confirmation(
            label, account_hash, order_id, ticker, account,
            f"✅ {label} {quantity} {ticker} in {account} submitted to Schwab "
            f"(trail={trail_pct}%, ~${quantity * price:,.0f})", node_id=node_id)
    except Exception:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True, node_id=node_id)
        raise
    schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False, node_id=node_id)
    return None, order_id


def replace_order_with_trailing_sell(account: str, ticker: str, order_id: int, quantity: int, price: float,
                                      trail_pct: float, node_dry_run: bool = False, node_id: int | None = None):
    """Same atomic-replace idea as replace_equity_order_with_market, for the
    TRAIL arm-time swap: a resting protective SL becomes a TRAILING_STOP
    SELL, as a single broker call instead of cancel_order + place_trailing_sell.
    Used by _attempt_automated_sell. Returns (response, new_order_id);
    dry_run returns (None, None) and leaves the existing resting order
    untouched."""
    try:
        dry_run = schwab_safety.approve_and_record(account, ticker, quantity, price, "SELL",
                                                     replacing_order_id=order_id, node_dry_run=node_dry_run,
                                                     node_id=node_id)
    except schwab_safety.SafetyViolation as e:
        _post_message(f"\U0001F6AB BLOCKED replace {order_id} with TRAILING SELL {quantity} {ticker} "
                      f"in {account} ({_mode_tag_for(account, node_id)}) (trail={trail_pct}%): {e}", node_id=node_id)
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True, node_id=node_id)
        raise

    if dry_run:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False, node_id=node_id)
        msg = (f"[DRY RUN] would replace order {order_id} with TRAILING SELL {quantity} {ticker} "
               f"in {account} (trail={trail_pct}%, ~${quantity * price:,.0f})")
        _post_message(msg, node_id=node_id)
        print(msg)
        return None, None

    account_hash = _resolve_account_hashes()[account]
    order = _build_trailing_order("SELL", StopPriceLinkBasis.BID, ticker, quantity, trail_pct)
    try:
        new_order_id = _submit_replace_with_retry(account_hash, order_id, order, account=account,
                                                    ticker=ticker, side="SELL", quantity=quantity,
                                                    order_type=OrderType.TRAILING_STOP.value, node_id=node_id)
        _post_order_confirmation(
            "REPLACE->TRAILING SELL", account_hash, new_order_id, ticker, account,
            f"✅ Replaced order {order_id} with TRAILING SELL {quantity} {ticker} in {account} "
            f"(trail={trail_pct}%, ~${quantity * price:,.0f})", node_id=node_id)
    except Exception:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True, node_id=node_id)
        raise
    schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False, node_id=node_id)
    return None, new_order_id


def place_trailing_buy(account: str, ticker: str, quantity: int, price: float, trail_pct: float,
                        node_dry_run: bool = False, node_id: int | None = None):
    """trail_pct is the bounce-above-running-low trigger (matches the node's
    trail_buy_pct). ASK-linked, since a buy naturally references the ask."""
    return _place_trailing_order("BUY", StopPriceLinkBasis.ASK, account, ticker, quantity, price, trail_pct,
                                  node_dry_run=node_dry_run, node_id=node_id)


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
                         node_dry_run: bool = False, node_id: int | None = None):
    """trail_pct is the pullback-below-running-high trigger (matches the
    position's trail_sell_pct). BID-linked, since a sell naturally references
    the bid. Only relevant once the position's trailing-exit state has
    activated (strategies.TrailingBothZScoreBreakout.check_exit's
    state['trailing'] -- see signals_notify.notify_trailing_activated), same
    as the manual workflow's 'place the trailing stop order now' step."""
    return _place_trailing_order("SELL", StopPriceLinkBasis.BID, account, ticker, quantity, price, trail_pct,
                                  node_dry_run=node_dry_run, node_id=node_id)


def cancel_order(account: str, ticker: str, order_id: int, node_id: int | None = None):
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
        _post_message(f"\U0001F5D1️ confirmed cancelled resting order {order_id} "
                       f"({ticker} in {account} · {_mode_tag_for(account, node_id)})")
    elif status is None:
        _post_message(f"\U0001F5D1️ cancel request accepted for order {order_id} "
                       f"({ticker} in {account} · {_mode_tag_for(account, node_id)}) "
                       f"— status unconfirmed (poll failed)")
    else:
        _post_message(f"⚠️ cancel request accepted for order {order_id} "
                       f"({ticker} in {account} · {_mode_tag_for(account, node_id)}) but real "
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
                     node_dry_run: bool = False, node_id: int | None = None):
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
            node_dry_run=node_dry_run, node_id=node_id)
    except schwab_safety.SafetyViolation as e:
        _post_message(f"\U0001F6AB BLOCKED STOP LOSS {quantity} {ticker} in {account} ({_mode_tag_for(account, node_id)}) "
                      f"@ ${stop_price:.4f}: {e}", node_id=node_id)
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True, node_id=node_id)
        raise

    # Same labeling gap as _place_equity_order (found by paired review
    # 2026-08-13): this function always has is_protective=True (hardcoded --
    # every stop-loss is protective by nature), so only is_addon_leg varies.
    # Without this, an add-on leg's own SL was indistinguishable from the
    # core position's own "STOP LOSS TICKER submitted" -- two identical-
    # looking messages back to back for the same ticker/account.
    _label = "ADD-ON " if is_addon_leg else ""
    if dry_run:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False, node_id=node_id)
        msg = f"[DRY RUN] would place {_label}STOP LOSS {quantity} {ticker} in {account} @ ${stop_price:.4f}"
        _post_message(msg, node_id=node_id)
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
        order_id = _submit_order_with_retry(account_hash, order, account=account, ticker=ticker,
                                             side="SELL", quantity=quantity,
                                             order_type=OrderType.STOP.value, node_id=node_id)
        _post_order_confirmation(
            f"{_label}STOP LOSS", account_hash, order_id, ticker, account,
            f"✅ {_label}STOP LOSS {quantity} {ticker} in {account} submitted to Schwab @ ${stop_price:.2f}", node_id=node_id)
    except Exception:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True, node_id=node_id)
        raise
    schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False, node_id=node_id)
    return None, order_id


def replace_order_with_stop_loss(account: str, ticker: str, order_id: int, quantity: int, stop_price: float,
                                  node_dry_run: bool = False, node_id: int | None = None):
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
            node_dry_run=node_dry_run, node_id=node_id)
    except schwab_safety.SafetyViolation as e:
        _post_message(f"\U0001F6AB BLOCKED replace {order_id} with STOP LOSS {quantity} {ticker} "
                      f"in {account} ({_mode_tag_for(account, node_id)}) @ ${stop_price:.4f}: {e}", node_id=node_id)
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True, node_id=node_id)
        raise

    if dry_run:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False, node_id=node_id)
        msg = (f"[DRY RUN] would replace order {order_id} with STOP LOSS {quantity} {ticker} "
               f"in {account} @ ${stop_price:.4f}")
        _post_message(msg, node_id=node_id)
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
        new_order_id = _submit_replace_with_retry(account_hash, order_id, order, account=account,
                                                    ticker=ticker, side="SELL", quantity=quantity,
                                                    order_type=OrderType.STOP.value, node_id=node_id)
        _post_order_confirmation(
            "REPLACE->STOP LOSS", account_hash, new_order_id, ticker, account,
            f"✅ Replaced order {order_id} with STOP LOSS {quantity} {ticker} in {account} @ ${stop_price:.2f}",
            node_id=node_id)
    except Exception:
        schwab_safety.record_node_streak(ticker, account, "order_failures", hit=True, node_id=node_id)
        raise
    schwab_safety.record_node_streak(ticker, account, "order_failures", hit=False, node_id=node_id)
    return None, new_order_id


def get_account_balance(account: str) -> float:
    """Real settled cash for the account, read fresh (never cached) --
    Client.get_account, securitiesAccount.currentBalances. Prefers
    'cashBalance' (confirmed 2026-08-12 against all 4 real accounts:
    brokerage/roth/ira/soxl_ira -- literal settled cash, never inflated by
    margin loan value against a held position, unlike 'availableFunds', which
    is margin-inclusive for a genuine Reg-T account (this closed the real
    leverage-inclusive-cash gap for 'brokerage', a genuine margin account)
    and happened to be numerically identical to cashBalance on every
    account checked, since none currently holds a marginable position with
    borrowed value). Falls back to 'cashAvailableForTrading' (the original
    field name from Schwab's documented schema, confirmed 2026-07-23 to not
    exist on any real account checked so far) for a response shape this
    hasn't seen yet, then 'availableFunds' as the last resort. Note: for a
    genuine cash-type account (cash_settlement_type=='cash' -- only 'sep'
    today, trading_enabled=False), cashBalance can in principle include
    unsettled sale proceeds that cashAvailableForTrading would exclude,
    making this preference order slightly less conservative than it could
    be for that account type specifically -- inert today since
    cashAvailableForTrading has never actually appeared on a real response,
    but worth knowing if a cash-type account is ever made trading_enabled.
    Raises on any failure (network, no known field present, etc.) rather
    than returning a fallback value -- the caller (schwab_safety.check_order)
    must fail closed on a balance-check failure, not silently allow the
    order through with an unknown balance."""
    account_hash = _resolve_account_hashes()[account]
    r = _get_client().get_account(account_hash)
    r.raise_for_status()
    balances = r.json()["securitiesAccount"]["currentBalances"]
    if "cashBalance" in balances:
        return float(balances["cashBalance"])
    if "cashAvailableForTrading" in balances:
        return float(balances["cashAvailableForTrading"])
    return float(balances["availableFunds"])


def get_account_buying_power(account: str) -> float:
    """Real, raw 'buyingPower' for the account, read fresh (never cached).
    NO LONGER used by the add-on leg's real cash-availability check (see
    get_leveraged_buying_power below, 2026-08-12) -- confirmed this raw field
    is computed at a blanket 50% margin requirement, which overstates real
    capacity for a real 3x leveraged fund (e.g. SOXL/HIBL -- confirmed
    2026-08-12 JNUG/ETHU are actually 2x, fundLeverageFactor=200.0, same as
    AGQ; don't assume from ticker name alone) by roughly 1/3. Kept
    only for signals_notify.check_addon_buying_power_drift, which
    deliberately still wants the raw, un-leverage-adjusted number -- it's
    watching whether get_account_balance's own settled-cash-vs-margin
    assumption still holds for the account overall, a different question
    than 'what's the real safe order size for this specific ticker.' Raises
    on any failure, same fail-closed contract as get_account_balance."""
    account_hash = _resolve_account_hashes()[account]
    r = _get_client().get_account(account_hash)
    r.raise_for_status()
    balances = r.json()["securitiesAccount"]["currentBalances"]
    return float(balances["buyingPower"])


def get_account_margin_requirement(ticker: str) -> float:
    """Real house margin requirement fraction for `ticker`, read fresh from a
    live quote's fundamental.fundLeverageFactor (Client.get_quote). 50% for a
    2x fund, 75% for a 3x fund -- confirmed 2026-08-12 against a real
    `brokerage` account response: $20,000 cash / 0.50 = $40,000 exactly
    matches AGQ's (2x, fundLeverageFactor=200.0) real reported buyingPower.

    Uses abs(leverage) -- inverse funds report a NEGATIVE factor (confirmed
    2026-08-12: YANG=-300.0, ERY=-200.0, SH=-100.0), and a genuine 3x inverse
    fund (YANG) needs the 75% bucket exactly like a 3x long fund; the
    original version compared the raw signed value and silently misbucketed
    every inverse ticker (caught by paired Opus review before this function
    was ever wired into a real order-check path).

    A 1x/unleveraged fund falls through to the 50% bucket -- note this is
    the MORE permissive bucket (equity/0.50 > equity/0.75), not "safer" as
    an earlier version of this docstring incorrectly claimed. Raises if
    fundLeverageFactor is missing entirely from the quote -- fail closed,
    same contract as get_account_balance.

    WIRED into schwab_safety.check_order's is_addon_leg branch via
    get_leveraged_buying_power (below), as of 2026-08-12. An earlier same-day
    version was briefly wired unclamped, then reverted after a paired Opus
    review found it live-reachable on soxl_ira -- a limited-margin IRA that
    cannot actually borrow, where margin_capable=True is seeded only to
    preserve an unrelated old gate's behavior, not because it reflects real
    leverage capability -- and that equity/margin_req computes GROSS
    theoretical capacity, never subtracting capital already deployed in open
    positions, unlike Schwab's own real-time buyingPower field (confirmed
    empirically: it visibly shrinks as resting orders accumulate). Both
    concerns are now resolved by get_leveraged_buying_power's clamp (commit
    a16ede6): it returns min(equity/margin_req(ticker), Schwab's raw
    buyingPower), so the raw field's real-time netting and margin_capable
    accuracy bound this function's gross estimate on both counts."""
    r = _get_client().get_quote(ticker)
    r.raise_for_status()
    data = r.json().get(ticker)
    if not data:
        raise ValueError(f"no quote data returned for {ticker}")
    leverage = data.get('fundamental', {}).get('fundLeverageFactor')
    if leverage is None:
        raise ValueError(f"no fundLeverageFactor in quote for {ticker} -- "
                          f"can't determine real margin requirement")
    return 0.75 if abs(leverage) >= 250.0 else 0.50


def get_leveraged_buying_power(account: str, ticker: str) -> float:
    """Real, leverage-aware buying power for `ticker` in `account` --
    min(equity / get_account_margin_requirement(ticker), Schwab's own raw
    'buyingPower' field). The leverage-aware term alone (equity/margin_req)
    is GROSS theoretical capacity -- it never subtracts capital already
    deployed in open positions, and assumes real 2x/3x borrowing capability
    exists regardless of account type. Clamping to the raw 'buyingPower'
    field fixes both: that field is confirmed (2026-08-12, live) to already
    net out committed capital in real time (it visibly shrinks as resting
    orders accumulate) AND to correctly reflect zero leverage for a
    limited-margin account (soxl_ira: real buyingPower == cashBalance
    exactly, no 2x assumption) -- so the clamp can only ever TIGHTEN the raw
    figure (the original point: raw 'buyingPower' is a blanket 50%-
    requirement number that overstates real capacity for a genuine 3x fund
    like SOXL/HIBL by roughly 1/3) and can never loosen it beyond what
    Schwab itself already says is really available. Reverted-then-fixed
    2026-08-12 same session: a first version without this clamp was briefly
    wired into schwab_safety.check_order's is_addon_leg branch, found live-
    reachable on soxl_ira with a materially overstated result by a paired
    Opus review, and reverted before this clamp was added -- now wired back
    in with the clamp, verified live against real soxl_ira/brokerage data
    (see tests/test_leveraged_buying_power.py). Raises on any failure, same
    fail-closed contract as get_account_balance."""
    account_hash = _resolve_account_hashes()[account]
    r = _get_client().get_account(account_hash)
    r.raise_for_status()
    balances = r.json()["securitiesAccount"]["currentBalances"]
    equity = float(balances["equity"])
    raw_buying_power = float(balances["buyingPower"])
    margin_req = get_account_margin_requirement(ticker)
    return min(equity / margin_req, raw_buying_power)


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


def get_real_orders(account: str, ticker: str) -> list:
    """Every real order (any status, any age Schwab's default order-history
    window returns) touching `ticker` in `account`, read fresh -- one row per
    order, flattened from Schwab's orderLegCollection shape down to the
    fields callers actually use. Moved here 2026-08-10 from
    scripts/audit_live_test_candidates.py's private _real_orders (same body,
    now a public schwab_client function so it can be reused by
    signals_helpers.get_full_position_state without a scripts/ -> core-module
    import, which would invert the project's normal layering)."""
    account_hash = _resolve_account_hashes()[account]
    client = _get_client()
    r = client.get_orders_for_account(account_hash)
    r.raise_for_status()
    orders = []
    for o in r.json():
        for leg in o.get("orderLegCollection", []):
            if leg.get("instrument", {}).get("symbol") == ticker:
                orders.append({
                    "orderId": o.get("orderId"), "status": o.get("status"),
                    "orderType": o.get("orderType"), "instruction": leg.get("instruction"),
                    "quantity": leg.get("quantity"), "enteredTime": o.get("enteredTime"),
                    "stopPrice": o.get("stopPrice"),
                    "stopPriceOffset": o.get("stopPriceOffset"),  # trailing orders carry the trail % here, not stopPrice
                })
                break
    return orders


def filter_resting_orders(orders: list) -> list:
    """Subset of get_real_orders' output that's still actually resting at the
    broker (not filled/cancelled/rejected). Originally moved verbatim from
    scripts/audit_live_test_candidates.py's private _resting_orders, which
    used a hand-picked 5-status allowlist (WORKING/AWAITING_STOP_CONDITION/
    QUEUED/ACCEPTED/PENDING_ACTIVATION) -- fine for that script's own
    human-eyeballed printout, but a real gap once this became the shared
    read behind signals_helpers.get_full_position_state's mismatch
    detection (2026-08-10 paired review): an order sitting in an
    intermediate acknowledgement-phase status Schwab documents but the
    allowlist never enumerated (e.g. PENDING_ACKNOWLEDGEMENT,
    AWAITING_PARENT_ORDER) would read as 'not resting' -- a false 'no
    matching resting order' on a genuinely fine order, or a missed orphan on
    the flip side. Now the same blocklist-of-terminal-statuses
    schwab_safety.py itself uses to define 'still open at the broker'
    (schwab_safety._OPEN_ORDER_STATUSES_EXCLUDED, also referenced this way
    by signals_notify.py) -- one definition of 'resting', not two that can
    silently drift apart."""
    return [o for o in orders if o["status"] not in schwab_safety._OPEN_ORDER_STATUSES_EXCLUDED]


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
