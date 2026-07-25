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


CHECKS = [
    check_live_trailing_exit_automation_scope,
    check_research_mode_ticker_with_open_position_in_automation_scope,
    check_live_node_missing_account,
    check_brokerage_not_live_with_unresolved_leverage_gap,
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
