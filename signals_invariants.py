"""Config-invariant sanity checks -- catch a misconfiguration that would silently
break an assumption baked into other code, before that code hits it live.

Run automatically at active_signals.py daemon startup (non-blocking Slack alert
if any violation is found) and standalone as a pre-commit sanity check:
    .venv/bin/python signals_invariants.py

Each check function returns a list of human-readable violation strings (empty =
clean) and documents, in its own docstring, exactly which downstream code relies
on the invariant -- so a violation is actionable without a backlog lookup.
"""
import signals_db as db
import schwab_safety


def check_live_trailing_exit_automation_scope():
    """Every mode='live' TrailingExitZScoreBreakout node's ticker must be in
    schwab_safety.AUTOMATION_ENABLED_TICKERS.

    Depends on this: signals_handlers.handle_entry_price/handle_trail_buy_fill_price
    both gate on `any(p['ticker'] == ticker for p in db.get_pending_buys())` before
    opening a position -- but notify_buy_signal only calls db.add_pending_buy when
    the ticker is automation-scoped. A live TrailingExitZScoreBreakout ticker
    outside that scope still renders a normal "Executed" button, so tapping it
    after a real manual fill finds no matching pending_buys row and is silently
    discarded: no position recorded, no protective stop placed, no error shown.
    (docs/backlog_cache.md, Opus review round 6, 2026-07-25.)
    """
    violations = []
    for node in db.get_watchlist():
        if node['mode'] == 'live' and node['strategy'] == 'TrailingExitZScoreBreakout':
            if node['ticker'] not in schwab_safety.AUTOMATION_ENABLED_TICKERS:
                violations.append(
                    f"{node['ticker']} (wl_id={node['id']}) is mode='live' "
                    f"TrailingExitZScoreBreakout but not in AUTOMATION_ENABLED_TICKERS -- "
                    f"a manual 'Executed' tap would be silently discarded "
                    f"(signals_handlers.handle_entry_price/handle_trail_buy_fill_price)."
                )
    return violations


def check_research_mode_ticker_with_open_position_in_automation_scope():
    """A mode='research' node whose ticker is in AUTOMATION_ENABLED_TICKERS and
    has a real (non-paper) open position is exposed to automated-sell despite
    being research-mode.

    Depends on this: automation_principles.md #7 -- BUY-side automation is
    gated by both ticker membership and node mode (_scan_buy_signals only
    routes mode='live' nodes to the real alert path), but SELL-side automation
    (_attempt_automated_sell, via notify_trailing_activated) is gated by ticker
    membership only, not mode. A research-mode ticker with a real open position
    (e.g. left over from an earlier live stint) would still have its exit
    routed through automated-sell. (docs/backlog_cache.md, found 2026-07-22,
    not yet fixed.)

    Checked per-node (wl_id), not per-ticker -- a ticker-only position lookup
    would false-positive whenever a ticker has both a live node and a research
    node (e.g. DPST/GDXU's deliberate live+research pairing) and the *live*
    node is the one holding the real position.
    """
    violations = []
    for node in db.get_watchlist():
        if node['mode'] != 'live' and node['ticker'] in schwab_safety.AUTOMATION_ENABLED_TICKERS:
            # wl_id-keyed, not ticker-only -- a ticker-only lookup (db.get_open_position)
            # would false-positive on a deliberate live+research node pair for the same
            # ticker (e.g. DPST/GDXU) whenever the *live* node holds the real position.
            pos = db.get_open_position_by_wl_id(node['id'], paper=False)
            if pos is not None:
                violations.append(
                    f"{node['ticker']} (wl_id={node['id']}) is mode='{node['mode']}' but has a "
                    f"real open position and is in AUTOMATION_ENABLED_TICKERS -- its exit would "
                    f"still be routed through automated-sell (signals_notify.notify_trailing_activated "
                    f"-> _attempt_automated_sell, ticker-gated only, not mode-gated)."
                )
    return violations


def check_live_node_missing_account():
    """No mode='live' node should have account=None.

    Depends on this: schwab_safety.check_order requires a real account before
    it can evaluate dry_run vs live, so a live node with no account fails
    closed as "BLOCKED ... unknown account 'None'" instead of placing a real
    or dry_run order -- silently useless rather than unsafe, but defeats the
    point of the node. Fixed for the specific 2026-07-24 instances via a
    direct DB patch; add_node itself still has no guard against a future
    recurrence. (docs/backlog_cache.md, 2026-07-26.)
    """
    violations = []
    for node in db.get_watchlist():
        if node['mode'] == 'live' and not node.get('account'):
            violations.append(
                f"{node['ticker']} (wl_id={node['id']}) is mode='live' with account=None -- "
                f"check_order will fail closed as 'unknown account' for every order attempt."
            )
    return violations


def check_brokerage_not_live_with_unresolved_leverage_gap():
    """The 'brokerage' account must stay dry_run=True until the availableFunds
    leverage-inclusive-cash gap is resolved.

    Depends on this: schwab_client.get_account_balance's cash check reads
    availableFunds, which for a genuine Reg-T margin account like 'brokerage'
    can exceed settled cash (includes loan value of held marginable positions).
    check_order's cash-availability check is meant to be a conservative
    real-cash gate; against a live 'brokerage' it could currently pass a BUY
    partly funded by borrowing rather than settled cash. Bounded today only by
    dry_run=True. (docs/backlog_cache.md, Opus review 2026-07-23 night, not
    yet fixed.)
    """
    violations = []
    brokerage = schwab_safety.ACCOUNTS.get('brokerage')
    if brokerage is not None and not brokerage.dry_run:
        violations.append(
            "schwab_safety.ACCOUNTS['brokerage'].dry_run is False -- the availableFunds "
            "leverage-inclusive-cash gap (get_account_balance / check_order's cash check) "
            "was never fixed for a real margin account, only bounded by dry_run=True."
        )
    return violations


def check_starting_notional_within_account_notional_cap():
    """A mode='live' node's starting_notional must not exceed its real
    account's notional_cap (schwab_safety.ACCOUNTS).

    Depends on this: signals_helpers._last_sale_recovery (real position
    sizing) and signals_notify._reconcile_fill's post-fill top-up both target
    starting_notional as the position size to reach -- if that target is
    structurally larger than the account's real notional_cap, every entry/
    top-up attempt for the node is either guaranteed to be blocked outright or
    silently under-filled relative to what the node's own config claims it
    should hold. Found live 2026-07-29: RETL's node had starting_notional=
    $5000 in account 'soxl_ira', whose real notional_cap is $800 -- a real
    fill of 50 shares (~$495) triggered a 454-share top-up attempt that was
    only stopped by a signal-window gate firing first, not by anything
    catching the underlying config mismatch itself."""
    violations = []
    for node in db.get_watchlist():
        if node['mode'] != 'live':
            continue
        account = node.get('account')
        starting_notional = node.get('starting_notional')
        if not account or not starting_notional:
            continue
        limits = schwab_safety.ACCOUNTS.get(account)
        if limits is not None and starting_notional > limits.notional_cap:
            violations.append(
                f"{node['ticker']} (wl_id={node['id']}) starting_notional=${starting_notional:,.0f} "
                f"exceeds account {account!r}'s real notional_cap=${limits.notional_cap:,.0f} -- "
                f"every entry/top-up attempt for this node is structurally oversized for its account."
            )
    return violations


def check_open_position_config_matches_live_node():
    """An open position's snapshotted max_hold_hours/fixed_sl should match its
    node's current live watch_list config, unless deliberately diverged.

    Depends on this: an already-open position's real exit-check logic
    (signals_compute.check_sell_condition) reads pos['max_hold_hours']/
    pos['fixed_sl'] -- the value baked onto the position row at entry time
    (or last manually updated), NOT whatever the node's live config currently
    says (open_position() only snapshots once, at entry). Editing a node's
    config after it has an open position silently does nothing to that
    position unless the position row is *also* updated -- found live
    2026-07-29 (SH, twice): a node's max_hold_hours was changed without
    touching the open position, so the real exit-check kept running on the
    stale snapshotted value with no indication anything was out of sync.
    Informational, not necessarily wrong -- a deliberate mid-flight config
    change to only the node (for future entries) or only the position (a
    manual one-off override) is a legitimate, real use case this project
    does on purpose. The point is visibility, not a hard rule."""
    violations = []
    for pos in db.get_open_positions():
        node = db.get_watch_list_node_by_id(pos.get('wl_id'))
        if node is None:
            continue
        for field in ('max_hold_hours', 'fixed_sl'):
            pos_val, node_val = pos.get(field), node.get(field)
            # abs()-based, not != -- a raw float != can false-positive on pure
            # representation differences (e.g. 1 vs 1.0000000001), found by
            # session-wrap review 2026-07-29.
            if pos_val is not None and node_val is not None and abs(float(pos_val) - float(node_val)) > 1e-9:
                violations.append(
                    f"{pos['ticker']} (position id={pos['id']}, wl_id={node['id']}) {field}: "
                    f"position snapshot={pos_val} vs node's current live config={node_val} -- "
                    f"the open position's real exit-check still runs on the snapshotted value."
                )
    return violations


CHECKS = [
    check_live_trailing_exit_automation_scope,
    check_research_mode_ticker_with_open_position_in_automation_scope,
    check_live_node_missing_account,
    check_brokerage_not_live_with_unresolved_leverage_gap,
    check_starting_notional_within_account_notional_cap,
    check_open_position_config_matches_live_node,
]


def run_all():
    violations = []
    for check in CHECKS:
        violations.extend(check())
    return violations


if __name__ == "__main__":
    import sys
    found = run_all()
    if found:
        print(f"{len(found)} invariant violation(s):")
        for v in found:
            print(f"  - {v}")
        sys.exit(1)
    print("All invariants hold.")
