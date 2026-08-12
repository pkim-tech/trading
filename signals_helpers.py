"""Small shared helpers with no cross-dependency on blocks/charts/handlers."""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import schwab_safety
import signals_config as cfg
import signals_db as db

_CORP_ACTION_ALERT_PATH = Path(__file__).parent / "cache" / "live" / "corporate_action_alerts.json"


def node_state(node):
    """The single field a node is ever in -- 'paper' / 'dry_run' / 'live',
    mutually exclusive and exhaustive, stored directly as watch_list.state
    (collapsed 2026-08-1x from the old separate mode + node-level dry_run
    override, per the user's explicit call: reasoning about two fields for
    one fact -- "what is this node doing right now" -- was the friction being
    removed). The account-level ceiling (schwab_safety.ACCOUNTS[account].
    trading_enabled) stays a genuinely separate, independently-checked fact
    -- real_order_allowed = (node.state == 'live') AND account.trading_enabled.
    This accessor exists as the one blessed way to read it (vs. reaching into
    node['state'] ad hoc everywhere) -- kept trivial on purpose."""
    return node.get('state', 'paper')


def effectively_dry_run(account, node=None):
    """True if a real order attempt for this (account, node) pair will be
    simulated rather than actually submitted:
        real_order_allowed = (node.state == 'live') AND account.trading_enabled
    i.e. this returns the negation. node.state and the account-level ceiling
    are two genuinely independent facts (2026-08-1x design decision -- the
    account flag stays an enforced ceiling, never absorbed into node.state),
    combined here rather than pre-cached, so a later account promotion/
    demotion is reflected immediately without touching every node's own row.
    The single shared implementation -- signals_notify._effectively_dry_run
    and paper_trading._overlay_mode both delegate here instead of keeping
    their own inlined copies (they can't call each other directly due to the
    signals_notify<->paper_trading import direction, but both can reach this
    module)."""
    if node is not None and node.get('state') != 'live':
        return True
    limits = schwab_safety.ACCOUNTS.get(account)
    if limits is None:
        return False
    return not limits.trading_enabled


def mode_tag(account, node=None):
    """'LIVE' / 'DRY-RUN' / 'UNKNOWN' display tag for an alert header -- shared
    by signals_notify.py and signals_blocks.py so every alert can show real
    vs. simulated status next to the account name, not the account name alone
    (found 2026-07-26: an account-only tag like '(ira)' reads identically
    whether it's real money or not -- the exact ambiguity this removes).
    Deliberately does NOT fall back to DRY-RUN for a None/unrecognized account
    the way signals_notify._coverage_mode does -- that helper only labels a
    log row, but this one labels human-facing risk, and a real-money position
    with a NULL account (e.g. a manual/legacy position) defaulting to a
    reassuring 'DRY-RUN' is the wrong failure direction (Opus review,
    2026-07-26). UNKNOWN is deliberately alarming instead.
    node: pass whenever available (2026-08-1x) -- without it, a node-forced-
    dry-run override on an otherwise-real account mislabels as LIVE, since
    this function only sees the account's own flag."""
    if account is None:
        return "UNKNOWN"
    limits = schwab_safety.ACCOUNTS.get(account)
    if limits is None:
        return "UNKNOWN"
    return "DRY-RUN" if effectively_dry_run(account, node) else "LIVE"


def has_capital_at_stake(node):
    """True only when a node is BOTH live (real order placement -- see
    effectively_dry_run) AND sized at genuinely meaningful capital
    (starting_notional >= cfg.CAPITAL_AT_STAKE_THRESHOLD). 'live' alone is
    not the same fact -- soxl_ira's nodes place real orders but at
    $500-$2,500 notional, which the user explicitly does not consider
    capital at stake (2026-08-08). Deliberately mechanical (a dollar
    comparison, not a manual per-node tag) so a node crosses this bar
    automatically once real accounts (roth/brokerage) are funded and sized
    for real, without needing another manual re-scoping pass. Zero nodes
    cross it as of 2026-08-08."""
    account = node.get('account')
    if effectively_dry_run(account, node):
        return False
    return (node.get('starting_notional') or 0) >= cfg.CAPITAL_AT_STAKE_THRESHOLD


def should_alert_live(node):
    """Whether THIS node's Slack alerts (routine AND anomaly alike) should
    post in real time at all, vs. being suppressed in favor of EOD-only
    review (2026-08-08 user call -- explicitly NOT limited to routine
    alerts: a bug affecting a shared code path could just as easily hit a
    capital-at-stake node, so sub-threshold anomalies still need reviewing,
    just not real-time paging). This gate is ONLY for the Slack post itself
    -- every caller must keep its own coverage_events/log_incident call
    unconditional regardless of this return value, so nothing is silently
    lost, only its real-time visibility. Equivalent to has_capital_at_stake
    today (single deciding factor), but named/kept separate since the
    decision of "does this alert page now" is conceptually the caller's
    question, not the same question has_capital_at_stake answers for the
    Reference Report's candidate-row filter."""
    return has_capital_at_stake(node)


def stop_status(pos):
    """Distinguishes what broker_stop_price actually tells us for a given
    position, since None is ambiguous between several very different
    situations (found 2026-08-01 reviewing the SL alert's fallback text,
    which guessed 'should have auto-filled' regardless of which case
    applied):
    - 'known': broker_stop_price is on file (our own code placed it and
      recorded it via set_broker_stop_price_by_position) -- trustworthy.
    - 'automation-pending': the ticker is in AUTOMATION_ENABLED_TICKERS, the
      account is real (not dry_run), but no price is on file yet -- a real
      anomaly worth an actionable/urgent alert (placement may have failed or
      not run yet).
    - 'dry-run': the account is dry_run -- schwab_client.place_stop_loss
      short-circuits and never places anything for these, so
      broker_stop_price can structurally never be recorded regardless of
      automation scope. Not an anomaly; must not render as one (Opus review,
      2026-08-01 -- the first version of this function ignored dry_run
      entirely and rendered a permanent false "placement failure" alarm for
      every manually-confirmed position in the 4 of 5 accounts that are
      dry_run).
    - 'manual': a real (non-dry_run) account, position never automation-
      scoped -- no automated stop was ever supposed to exist here, so
      there's nothing to detect a failure against; the alert should say
      "verify yourself," not imply automation should have handled it.
    Deliberately does NOT poll the broker or infer 'known' from anything
    other than our own recorded placement -- see docs/backlog_cache.md's
    2026-08-01 SL-alert entry for why (self-healing off broker-observed
    state risks masking a real problem, same failure class already found in
    coverage_deviations' auto-resolve bug)."""
    bsp = pos.get('broker_stop_price')
    if bsp:
        return 'known', bsp
    _node = db.get_watch_list_node_by_id(pos.get('wl_id'))
    # A real open position under a state='paper' node is itself an anomaly
    # (signals_invariants.check_research_mode_ticker_with_open_position_in_
    # automation_scope exists to catch exactly this) -- must not render as
    # the reassuring 'dry-run' effectively_dry_run(node=paper) would give it
    # (Opus review, 2026-08-06: found while auditing the mode/dry_run->state
    # collapse). Only a genuine state='dry_run' node, or a state='live' node
    # on a non-trading_enabled account, means 'dry-run' here.
    if _node is not None and _node.get('state') == 'paper':
        pass
    elif effectively_dry_run(pos.get('account'), _node):
        return 'dry-run', None
    if pos.get('ticker') in schwab_safety.AUTOMATION_ENABLED_TICKERS:
        return 'automation-pending', None
    return 'manual', None


def log_poll(msg):
    """Appends one [poll] trace line to VERBOSE_LOG_PATH -- every price/bar a
    live-trading decision point actually used, kept out of the human-readable
    log so day-to-day monitoring isn't buried. Built 2026-07-22 after a real
    stale-cache bug (HIBL paper trade) went unnoticed with no way to see what
    price/bar each poller had actually read at decision time."""
    try:
        with open(cfg.VERBOSE_LOG_PATH, "a") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [poll] {msg}\n")
    except Exception:
        pass


def _load_corp_action_alerts():
    if not _CORP_ACTION_ALERT_PATH.exists():
        return {}
    try:
        return json.loads(_CORP_ACTION_ALERT_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def already_alerted_corp_action(ticker) -> bool:
    """Prevents re-alerting every ~30s poll while a held position's entry_price
    stays stale -- one alert per detected discontinuity, not one per check."""
    return ticker in _load_corp_action_alerts()


def mark_corp_action_alerted(ticker):
    state = _load_corp_action_alerts()
    state[ticker] = True
    _CORP_ACTION_ALERT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CORP_ACTION_ALERT_PATH.write_text(json.dumps(state))


def clear_corp_action_alert(ticker):
    """Called once the correction is applied -- lets a genuinely new,
    separate discontinuity for the same ticker alert again later."""
    state = _load_corp_action_alerts()
    state.pop(ticker, None)
    _CORP_ACTION_ALERT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CORP_ACTION_ALERT_PATH.write_text(json.dumps(state))


def _add_trading_hours(start, hours):
    """Advance `start` by `hours` trading bars (market hours 9-15, Mon-Fri only)."""
    dt = start
    remaining = hours
    while remaining > 0:
        dt += timedelta(hours=1)
        if dt.weekday() < 5 and 9 <= dt.hour <= 15:
            remaining -= 1
    return dt


# Known corporate-action ratios to match against, not an arbitrary magnitude
# cutoff -- a 3x leveraged ETF can plausibly crash >66% in one real extreme
# day (ratio > 3), so magnitude alone can't tell a real crash from a split.
# A split ratio is always a clean, round number; a real market move landing
# within tolerance of one by coincidence is vanishingly unlikely regardless
# of how large the move is.
_SPLIT_RATIOS = (1.5, 2, 2.5, 3, 4, 5, 10, 15, 20, 25, 30, 40, 50)


def detect_price_discontinuity(current_price, reference_price, tolerance=0.03):
    """Returns the reference/current ratio if it closely matches a known
    split-like factor (or its inverse, for a reverse split), None otherwise.
    Detection only -- callers decide what to do with a hit; this doesn't
    freeze/block/notify on its own. (Found live 2026-07-15: KORU's ~20:1
    split silently passed every SL/arm check since nothing compared current
    price against the stale reference price.)"""
    if not current_price or not reference_price:
        return None
    ratio = reference_price / current_price
    for r in _SPLIT_RATIOS:
        if abs(ratio - r) / r < tolerance or abs(ratio - 1 / r) / (1 / r) < tolerance:
            return ratio
    return None


def nearest_split_factor(ratio):
    """Given a raw ratio that already matched in detect_price_discontinuity,
    returns the clean round-number factor it matched (e.g. 20.0, not the
    noisy 20.34 an actual fill price would produce) -- used for the proposed
    correction, since guessing off the clean factor is more sensible than the
    raw ratio. Returns None if ratio doesn't match any known factor (shouldn't
    happen if only ever called after a positive detect_price_discontinuity)."""
    candidates = list(_SPLIT_RATIOS) + [1 / r for r in _SPLIT_RATIOS]
    return min(candidates, key=lambda r: abs(ratio - r))


def _proximity_emoji(pct_away):
    if pct_away < 5:
        return "🔶"
    if pct_away < 15:
        return "🟡"
    return "⚪"


def _pos_key(pos):
    """Dict/set key for last_seen_bar, shared between active_signals.py's main
    loop/_scan_pinned_exit_arm and paper_trading.check_paper_sells (one dict,
    passed by reference across both) -- pos['wl_id'] when available, else a
    (ticker, window) fallback. A bare None would collide across every legacy/
    unbackfillable position (wl_id=NULL, see docs/backlog_cache.md's wl_id
    refactor entry): one such position's last_seen_bar write would make a
    second, unrelated None-wl_id position's at_bar_close check silently read
    as 'already seen', skipping its real bar-close exit evaluation."""
    return pos['wl_id'] if pos.get('wl_id') is not None else (pos['ticker'], pos['window'])


def resolve_at_bar_close(pos, last_bar_ts, last_seen_bar):
    """Shared at_bar_close bookkeeping for the three exit-check loops (real,
    paper, dry_run_sim). A position with no last_seen_bar entry yet is either
    (a) pre-existing at daemon startup, already seeded by _seed_last_seen_bar,
    or (b) opened just now, this run -- case (b) must NOT be graded as 'a bar
    just closed', since the current (possibly still-forming, or already
    partially-elapsed) bar's Low/Open can include real price action from
    before the position existed. Seed it as already-seen and defer the first
    real bar-close evaluation to the next genuinely new bar instead."""
    pos_key = _pos_key(pos)
    if pos_key not in last_seen_bar:
        last_seen_bar[pos_key] = last_bar_ts
        return False
    at_bar_close = last_seen_bar[pos_key] != last_bar_ts
    if at_bar_close:
        last_seen_bar[pos_key] = last_bar_ts
    return at_bar_close


def _existing_position_note(ticker, wl_id=None):
    """Formats the already-open position for a duplicate-attempt warning, so the
    user doesn't have to go run scripts/open_positions_status.py separately.
    Prefers wl_id (unambiguous) when the caller has it -- ticker alone
    arbitrarily picks the most-recently-entered position if 2+ nodes share
    a ticker."""
    pos = db.get_open_position_by_wl_id(wl_id) if wl_id is not None else db.get_open_position(ticker)
    if not pos:
        return "check `open_positions` if unsure what's live."
    return (f"currently open: `${pos['entry_price']:.4f}` x `{pos['shares']}` shares, "
            f"entered `{pos['entry_time']}` ({pos['account']}).")


def _last_sale_recovery(node, position_source='core'):
    """Estimated next-buy notional: proceeds (exit_price * shares) from this node's
    most recent closed trade, so sizing roughly compounds off the last recycle. Falls
    back to `starting_notional` (the node's own watch_list.starting_notional column)
    only if no closed trade has shares logged yet -- callers must supply a node with
    it set (no hidden flat-$50k default here) so a new pilot with a different real
    book size (e.g. GDXD's $5k) can't silently get sized like everyone else's $50k.
    Narrows by (ticker, strategy, version, window, account) -- trade_log has no
    wl_id column, so this is the closest available match to "this node's own
    history", not just "this ticker's most recent trade regardless of which node
    it belonged to" (a real live-sizing bug once 2+ nodes share a ticker with
    different starting_notional/account). A rough estimate, not a live capital
    feed -- doesn't know about other trades competing for the same account's cash
    in between. Excludes is_dry_run_sim rows -- a synthesized dry-run fill's
    proceeds must never size a real order, including in the same account if its
    dry_run flag is later flipped to live (Opus review 2026-07-26). Filters
    position_source (default 'core') -- a drought exit's proceeds must never
    size the next core entry and vice versa (real once drought/addon trades
    exist; no-op today since trade_log has zero non-core rows, see
    docs/plans/real_order_execution_drought_addon.md 0.7).

    starting_notional_override (2026-08-12), when set, is checked FIRST and
    returned directly -- bypasses both the trade_log lookup below and the
    plain starting_notional fallback. The only real lever to deliberately
    grow (or shrink) a node's sizing once it has closed a real trade; a
    plain starting_notional edit silently has no effect past that point
    (see signals_db.set_starting_notional_override)."""
    override = node.get('starting_notional_override')
    if override is not None:
        return override
    ticker = node['ticker']
    with db._conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT exit_price, shares FROM trade_log WHERE ticker=? AND strategy=? AND version=? "
            "AND window=? AND COALESCE(account,'')=COALESCE(?,'') AND exit_price IS NOT NULL "
            "AND shares IS NOT NULL AND is_dry_run_sim=0 AND position_source=? "
            "ORDER BY exit_time DESC LIMIT 1",
            (ticker, node.get('strategy'), node.get('version'), node.get('window'), node.get('account'),
             position_source),
        ).fetchone()
    if row and row['exit_price'] and row['shares']:
        return row['exit_price'] * row['shares']
    starting_notional = node.get('starting_notional')
    if starting_notional is None:
        raise ValueError(f"_last_sale_recovery({ticker}): no trade history and no starting_notional configured")
    return starting_notional


# Provisional placeholder, not derived from output/open_window_volatility_summary.csv
# as-is -- that data measures drift across the whole ambient-polling window and
# conflates detection drift (now eliminated for pinned open_check moments by
# schwab_client.get_session_open_price) with real fill-time execution slippage
# (the HIBL multi-leg-fill observation, still real). Follow-up: adapt
# scripts/sim_open_window_volatility.py (or a new variant) to isolate fill-time-only
# slippage from the openPrice reference, once real data exists (Part 4, Section 3).
DEFAULT_MARKET_ENTRY_PAD_PCT = 1.0


def buy_order_sizing(node, sig, pad_pct=1.0, market_pad_pct=DEFAULT_MARKET_ENTRY_PAD_PCT, target_notional=None):
    """Worst-case trailing-buy sizing: a real trailing-buy order fills once price
    bounces trail_buy_pct% off a running low that can fall further before that, so
    the fill price is unbounded relative to the signal-time price. Sizing off
    trail_buy_pct + pad_pct (not trail_buy_pct alone) covers ordinary same-day
    slippage between signal detection and the order actually going live at the
    broker (Part 3, 2026-07-21) -- pad_pct=1.0 is the default same-day pad.
    The overnight gap-correction path (signals_notify.check_gap_resize, branch B)
    is a plain MARKET order, sized separately (flat pad on a live quote, not this
    trailing-bounce formula). The non-trailing (plain market-buy) branch gets its
    own, smaller market_pad_pct (Part 4, Section 3) -- a market order fills near
    the signal-time price, not an unbounded bounce, so it needs less padding than
    the trailing-buy branch. Shared by _build_buy_blocks and the
    automated-placement path so there's one sizing formula, not two.

    target_notional defaults to _last_sale_recovery(node) (real trade_log
    compounding) -- callers whose fills never land in trade_log (paper_trading,
    which writes paper_trade_log instead) must pass an explicit override, since
    _last_sale_recovery would otherwise either silently fall back to
    starting_notional every time (a research-mode node with no matching real
    history, the common case) or, worse, pick up an unrelated REAL trade's
    proceeds if this exact (ticker, strategy, version, window, account) tuple
    ever also had genuine live history -- paper P&L must compound off its own
    simulated trades only, never real ones (found in the 2026-07-31 audit's
    still-open list, while aligning paper_trading's sizing formula with this
    one)."""
    ticker = sig['ticker']
    price = sig['current_price']
    if target_notional is None:
        target_notional = _last_sale_recovery(node)
    trailing_buy = db._is_trailing_buy(node)
    trail_buy_pct = node.get('trail_buy_pct') or 0.0
    if trailing_buy:
        shares = int(target_notional // (price * (1 + (trail_buy_pct + pad_pct) / 100)))
    else:
        shares = int(target_notional // (price * (1 + market_pad_pct / 100)))
    return {
        'shares': shares, 'target_notional': target_notional,
        'trailing_buy': trailing_buy, 'trail_buy_pct': trail_buy_pct, 'price': price,
    }


_PHASE_GREY, _PHASE_YELLOW, _PHASE_GREEN = '⚪', '🟡', '🟢'


def _phase_emoji(pos, pending_buy):
    """Four-bubble lifecycle strip, left to right: Signal / Filled / Armed / Sold.
    Each bubble is gray (not reached), yellow (in progress, awaiting confirmation),
    or green (confirmed complete) -- a position can be filled without being armed,
    so those get separate bubbles rather than one combined ball."""
    if pos is None:
        if pending_buy is None:
            return _PHASE_GREY * 4
        order_placed = pending_buy.get('order_placed')
        signal = _PHASE_GREEN if order_placed else _PHASE_YELLOW
        fill = _PHASE_YELLOW if order_placed else _PHASE_GREY
        return f"{signal}{fill}{_PHASE_GREY}{_PHASE_GREY}"

    trail_state = pos.get('trail_state') or {}
    if trail_state.get('trailing'):
        armed = _PHASE_GREEN if trail_state.get('order_placed') else _PHASE_YELLOW
    else:
        armed = _PHASE_GREY
    sold = _PHASE_YELLOW if trail_state.get('exit_pending') else _PHASE_GREY
    return f"{_PHASE_GREEN}{_PHASE_GREEN}{armed}{sold}"


def get_full_position_state(wl_id):
    """db.get_real_position_state(wl_id) (local: pending_buys/open_positions/
    trade_log, real-over-paper) merged with a fresh broker-side read -- the
    "check both sides" ground-truth call this project has been missing.
    db.get_real_position_state already stopped status_check.py/
    audit_live_test_candidates.py/watchlist_status.py from independently
    hand-rolling LOCAL state (2026-08-05), but none of them ever fed the
    broker's own view back into one comparable structure -- audit_one prints
    local and broker facts side by side for a human to eyeball, never diffs
    them programmatically. Built 2026-08-10 after coverage_check.py's
    _check_trade_lifecycle turned out to be a 3rd independent recurrence of
    the same "missing one of the real states" bug shape (see docs/
    backlog_cache.md's 2026-08-04 "broader diagnosis" item) -- that specific
    fix only needed the local 3 states (pending/open/closed), but the
    question "should we also check the broker side" is this function.

    Broker calls are best-effort: a fetch failure is recorded in
    broker_fetch_error rather than raised, so a transient API hiccup can't
    take down a caller iterating many tickers (matches audit_one's existing
    try/except pattern). A node with no account (never placed a real order)
    short-circuits to the local-only view with broker fields left None.

    mismatches is only populated when effectively_dry_run(account, node) is
    False -- i.e. a real order for this node actually reaches the broker.
    node['state']=='live' is NOT sufficient on its own (a paired-review
    finding, 2026-08-10, on the first version of this function, which used
    state=='live' alone): roth/brokerage sit at account.trading_enabled=False
    while carrying real state='live' nodes (GDXU/KORU/DFEN in roth,
    ETHU/AGQ in brokerage as of 2026-08-10) -- those fill via
    signals_notify.update_dry_run_buys' synthesis path (is_dry_run_sim=1
    open_positions rows), never touch the broker at all, and would
    otherwise report a permanent false "broker holds 0" mismatch. This is
    the same effectively_dry_run this module already uses elsewhere (see its
    own docstring) -- not a second, independently-drifting definition.

    mismatches branches on the REAL local fields directly (real_position/
    pending_buy), not on the merged `status` string -- `status` collapses
    onto 'holding_paper'/'pending_entry' (paper) when only paper state
    exists, which would otherwise skip real-orphan detection for a live
    node with a paper position/pending-buy also on file (both axes are
    independent, see get_real_position_state's own docstring).

    The 'holding' branch's share-count check adds any open add-on leg's
    shares to open_positions.shares before comparing against the broker's
    aggregate total -- addon_legs is a deliberately separate table (see
    open_addon_leg's docstring) sharing the same ticker/account, so the
    broker total is core+leg while open_positions.shares alone is core-only;
    9 real soxl_ira nodes carry addon_enabled=1 today. Deliberately does NOT
    also check for a missing protective order on an open position --
    signals_notify.check_live_state_reconciliation already does that, with
    retry/outage handling and Slack alerting this function doesn't
    replicate; duplicating a weaker copy of that specific check here is
    exactly the two-independently-drifting-implementations problem
    get_real_position_state itself was built to close (see its docstring).
    The 'pending_entry' branch only asserts a mismatch when
    pending_buy['order_placed'] is true -- a pending_buys row with
    order_placed=0 is the normal window between a BUY signal firing and the
    order actually being placed (manual 3-step Slack flow or automation),
    not evidence of anything wrong; and only against BUY-instruction resting
    orders, so a leftover protective SELL/stop can't mask a genuinely
    missing entry order. Order-status filtering (schwab_client.
    filter_resting_orders) mirrors schwab_safety's own open-order definition
    (a blocklist of terminal statuses), not a hand-picked allowlist, so an
    intermediate acknowledgement-phase status isn't misread as "not resting."

    Returns db.get_real_position_state(wl_id)'s dict plus: broker_shares,
    broker_resting_orders (list), broker_cash, broker_fetch_error (None on
    success), mismatches (list of str, broker treated as ground truth per
    automation_principles.md #1 -- a disagreement always means the local/DB
    side needs correcting, never the broker)."""
    import schwab_client  # local import: schwab_client -> signals_blocks -> signals_helpers
    state = db.get_real_position_state(wl_id)
    node = state['node']
    state['broker_shares'] = None
    state['broker_resting_orders'] = []
    state['broker_cash'] = None
    state['broker_fetch_error'] = None
    state['mismatches'] = []
    if node is None or not node.get('account'):
        return state
    account = node['account']
    ticker = node['ticker']
    try:
        state['broker_shares'] = schwab_client.get_real_position(account, ticker)
        orders = schwab_client.get_real_orders(account, ticker)
        state['broker_resting_orders'] = schwab_client.filter_resting_orders(orders)
        state['broker_cash'] = schwab_client.get_account_balance(account)
    except Exception as e:
        state['broker_fetch_error'] = str(e)
        return state  # can't compare without a broker read; report the failure, not a guessed mismatch

    if effectively_dry_run(account, node):
        return state  # see docstring: no real order for this node ever reaches the broker

    real_pos = state['real_position']
    real_pending = state['pending_buy']
    resting_buys = [o for o in state['broker_resting_orders'] if o.get('instruction') == 'BUY']

    if real_pos is not None:
        local_shares = real_pos.get('shares') or 0
        addon_leg = db.get_open_addon_leg_by_wl_id(wl_id, paper=False)
        if addon_leg:
            local_shares += addon_leg.get('shares') or 0
        if state['broker_shares'] == 0:
            state['mismatches'].append(
                f"local shows an open position ({local_shares} shares, incl. add-on leg) but broker holds 0")
        elif abs(state['broker_shares'] - local_shares) > 1e-6:
            state['mismatches'].append(
                f"share-count mismatch: local={local_shares} (incl. add-on leg) broker={state['broker_shares']}")
    elif real_pending is not None and real_pending.get('order_placed'):
        if not resting_buys:
            state['mismatches'].append(
                "local shows a resting pending buy (order_placed=1) but broker has no matching resting BUY order")
    else:
        # Nothing real locally (no open position, no placed pending order) --
        # regardless of what paper state might separately exist on this node.
        if state['broker_shares']:
            state['mismatches'].append(
                f"local shows no real position but broker holds {state['broker_shares']} shares "
                f"(orphaned real position)")
        elif state['broker_resting_orders']:
            state['mismatches'].append(
                "local shows no real position/pending-order but broker has a resting order for this ticker")
    return state


MAX_RUNNING_LOW_DROP_PCT = 20.0  # see signals_notify.update_real_pending_buys_running_low's
# docstring for the full rationale -- a single poll's running_low may not drop by more than
# this in one step, so one bad/thin extended-hours print can't permanently ratchet a
# trailing-buy's trigger down to a price nothing real ever confirmed. Moved here from
# signals_notify.py 2026-08-12 so paper_trading.py (which signals_notify.py itself imports,
# so it can't import back) can reuse the same bound instead of reinventing it.
