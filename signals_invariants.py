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


def check_daily_sync_halted_nodes():
    """Surfaces any daily-track node (paper_role='daily_sync') with
    daily_sync_halted_at set. paper_trading.reconcile_daily_track_nodes is
    pure observation as of 2026-08-05 -- it classifies and logs every
    divergence (db.log_daily_track_reconciliation) but never sets this itself
    (the user's explicit call: reconcile answers "how far are we," a separate
    "sync" action would be the one to pause/realign a node, not built yet).
    This check exists for whenever that sync tooling lands and actually sets
    the flag -- without it, a halted node could otherwise sit invisible for
    weeks. See docs/design.md's "Two-account paper trading" section."""
    violations = []
    for node in db.get_watchlist():
        if node.get('paper_role') == 'daily_sync' and node.get('daily_sync_halted_at'):
            violations.append(
                f"{node['ticker']} (wl_id={node['id']}) daily-track halted at "
                f"{node['daily_sync_halted_at']} -- unexplained divergence from backtest replay, "
                f"needs manual review (see daily_track_reconciliation_log), then "
                f"db.set_daily_sync_halted(wl_id, halted=False) to clear."
            )
    return violations


_OVERLAY_CONFIG_COLS = (
    'drought_overlay_enabled', 'drought_confirm_days', 'drought_vol_gate',
    'drought_sl_pct_override', 'drought_arm_pct_override', 'drought_trail_pct_override',
    'addon_enabled', 'skim_enabled', 'skim_step', 'skim_frac',
)


def check_daily_track_overlay_config_matches_live_track():
    """A daily-track node (paper_role='daily_sync') and its live-track sibling
    (same ticker/strategy/version/window/account/watchlist_id, paper_role IS
    NULL) must carry IDENTICAL drought/addon/skim overlay config -- the whole
    point of the pair is to isolate live-tick-vs-Close pricing as the only
    variable under test (docs/design.md's 2026-08-07 "Live automation
    design" section), which reconcile_overlay_nodes can't do if the two
    sides are running genuinely different config.

    add_daily_track_paper_nodes.py syncs these columns at clone time (fixed
    2026-08-09 -- add_node's signature has no params for them at all, so a
    clone would otherwise silently drop them), but nothing enforces they
    STAY in sync if the live-track node's config changes afterward -- this
    check exists for exactly that drift, same shape as
    check_staged_config_matches_expected below."""
    violations = []
    nodes = db.get_watchlist()
    daily_tracks = [n for n in nodes if n.get('paper_role') == 'daily_sync']
    for dt in daily_tracks:
        sibling = next((
            n for n in nodes
            if n.get('paper_role') is None and n['ticker'] == dt['ticker']
            and n['strategy'] == dt['strategy'] and n['version'] == dt['version']
            and n['window'] == dt['window'] and n.get('account') == dt.get('account')
            and n['watchlist_id'] == dt['watchlist_id']
        ), None)
        if sibling is None:
            continue
        for col in _OVERLAY_CONFIG_COLS:
            if dt.get(col) != sibling.get(col):
                violations.append(
                    f"{dt['ticker']} daily-track (wl_id={dt['id']}) {col}={dt.get(col)!r} != "
                    f"live-track (wl_id={sibling['id']}) {col}={sibling.get(col)!r} -- "
                    f"reconcile_overlay_nodes can't isolate price-source noise if the pair's "
                    f"overlay config genuinely differs."
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


def check_tax_advantaged_excluded_tickers():
    """No mode='live' watch_list node for a TAX_ADVANTAGED_EXCLUDED_TICKERS ticker
    (e.g. USO -- see CLAUDE.md's "Ticker exclusion, decided 2026-08-04" note,
    K-1/UBTI risk) should exist in an IRA/Roth/SEP-type account.

    Scoped to mode='live' only -- the K-1/UBTI risk is real capital sitting in a
    real IRA custodian account; a research/paper node tagged with the same
    account string (e.g. AGQ's daily-track/live-track pair, account='ira',
    mode='research') never places a real order and carries zero real tax
    exposure, so it's not a violation of this invariant.

    Depends on this: add_node's own guard (signals_db.py) only fires for callers
    that pass account= at insert time -- the dominant real path (Streamlit UI
    add-to-watchlist buttons, most scripts) creates the node first and assigns
    account via a raw `UPDATE watch_list SET account=...` afterward, which
    bypasses that guard entirely. This check catches that gap after the fact.
    Same substring account-name classification as add_node's guard (correct for
    today's real account names -- brokerage/sep/roth/ira/soxl_ira -- but would
    fail open on a future name like 'hsa'/'401k'; see docs/backlog_cache.md).
    """
    violations = []
    for node in db.get_watchlist():
        ticker = (node.get('ticker') or '').upper()
        account = node.get('account') or ''
        if node.get('mode') == 'live' and ticker in db.TAX_ADVANTAGED_EXCLUDED_TICKERS and any(
                kw in account.lower() for kw in ("ira", "roth", "sep")):
            violations.append(
                f"{ticker} (wl_id={node['id']}) is in account={account!r}, but "
                f"is on the tax-advantaged exclusion list (K-1/UBTI risk) -- "
                f"remove the node or move it to a taxable account."
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


def _config_field_mismatch(field, expected, actual):
    """Returns a mismatch string, or None if the field matches. actual=None
    is real drift, reported not skipped (see check_staged_config_matches_
    expected's docstring). A non-numeric expected_config value (staged_
    test_config.expected_config is a free-form dict -- set_staged_test_config
    accepts anything JSON-serializable) is reported as its own mismatch
    instead of raising ValueError and taking down the rest of run_all(),
    which has no per-check try/except (found by Opus review, 2026-07-30)."""
    if actual is None:
        return f"{field}: expected {expected}, actual None (field missing)"
    try:
        drifted = abs(float(actual) - float(expected)) > 1e-9
    except (TypeError, ValueError):
        return f"{field}: expected {expected!r}, actual {actual!r} (non-numeric, could not compare)"
    return f"{field}: expected {expected}, actual {actual}" if drifted else None


def check_staged_config_matches_expected():
    """Every staged_test_config row's expected_config must still match its
    node's real current watch_list values -- "is this node still committed
    and staged correctly to trigger the way it's supposed to?"

    Covers the whole live watchlist as of 2026-07-30 (scripts/
    seed_baseline_config.py snapshots a 'baseline_config' row for every
    mode='live' node not already covered by one of the 3 deliberately-
    designed test roles, e.g. SH/RETL/GDXU) -- not just those 3. A mismatch
    here means the node's real config silently drifted from what was
    committed/intended (an accidental edit, a migration side-effect, a stale
    manual DB patch), independent of whether the strategy has actually
    triggered today -- distinct from (and a precondition for) whether a
    trade fires, which coverage_check.py's trade_lifecycle checks track
    separately as informational-only, not a deviation ticket. DB-only
    (no broker call), safe to run every poll/every day, unlike
    scripts/audit_live_test_candidates.py which also hits the real broker
    for resting orders."""
    violations = []
    for row in db.get_staged_test_configs():
        node = db.get_watch_list_node_by_id(row['wl_id'])
        if node is None:
            violations.append(
                f"staged_test_config wl_id={row['wl_id']} ({row['ticker']}, "
                f"role={row['scenario_role']}) references a node that no longer exists."
            )
            continue
        mismatches = []
        for field, expected in row['expected_config'].items():
            m = _config_field_mismatch(field, expected, node.get(field))
            if m:
                mismatches.append(m)
        if mismatches:
            violations.append(
                f"{node['ticker']} (wl_id={node['id']}, role={row['scenario_role']}) "
                f"config drifted from its committed baseline: {'; '.join(mismatches)}."
            )
    return violations


def staged_config_status(account=None):
    """One row per staged_test_config node (✓/✗, not just failures) -- a
    state report for the real live-tier nodes, mirroring coverage_check.py's
    canary_* block, not just an aggregate violation count. Built 2026-07-30
    after the user asked for SH/GDXU/RETL/SPY/DPST (the real soxl_ira live
    nodes) to be visible the same way the canaries are, distinct from (and
    lighter than) audit_live_test_candidates.py's --staged mode, which also
    hits the real broker for resting orders -- this is DB-only, config-drift
    only, safe to print every day.

    Returns a list of dicts: ok, ticker, account, role, summary."""
    rows = []
    for row in db.get_staged_test_configs():
        node = db.get_watch_list_node_by_id(row['wl_id'])
        if node is None:
            rows.append(dict(ok=False, ticker=row['ticker'], account=None,
                              role=row['scenario_role'], summary="node no longer exists"))
            continue
        if account and node.get('account') != account:
            continue
        mismatches = []
        for field, expected in row['expected_config'].items():
            m = _config_field_mismatch(field, expected, node.get(field))
            if m:
                mismatches.append(m)
        rows.append(dict(
            ok=not mismatches, ticker=node['ticker'], account=node.get('account'),
            role=row['scenario_role'],
            summary="; ".join(mismatches) if mismatches else "matches committed baseline",
        ))
    return rows


def print_staged_config_status(account=None):
    label = f" ({account})" if account else ""
    print(f"Live node state{label}\n")
    for r in staged_config_status(account=account):
        glyph = "✓" if r['ok'] else "✗"
        print(f"  {glyph} live_config  {r['ticker']:<6} ({r['account']})  [{r['role']}]  {r['summary']}")


def print_all_live_node_state():
    """Both real-money live tiers -- soxl_ira (SH/RETL/GDXU/DPST/SPY) and ira
    (the 13 canary/mirror nodes) -- as two back-to-back tables, one call site
    for every caller that wants the full readiness picture (daemon startup,
    the 7am/EOD daily slots, and this module's own CLI)."""
    print_staged_config_status(account='soxl_ira')
    print()
    print_staged_config_status(account='ira')


def check_sim_mode_off_for_real_daemon():
    """SIM_MODE must be False for a genuine active_signals.py run_loop
    startup. Deliberately NOT in CHECKS/run_all() -- that function also runs
    standalone (`.venv/bin/python signals_invariants.py`, the pre-commit
    checklist item), where SIM_MODE=1 is the correct, expected default and
    would false-positive here on every routine pre-commit run. Called
    directly, once, only from run_loop()'s own startup instead.

    SIM_MODE=1 is the fail-safe default (2026-08-01, after a real incident:
    an ad hoc test call posted a real unprefixed message to the live
    channel) -- active_signals.py's own entrypoint forces it back to '0' via
    os.environ.setdefault before signals_config is even imported, so this
    should never actually fire for a genuine daemon startup. If it does,
    something bypassed that -- e.g. SIM_MODE=1 was already exported in the
    shell before `python active_signals.py run` (setdefault leaves an
    existing value untouched by design), silently turning the real daemon
    into a no-op simulator: every alert gets a misleading 🧪 SIM MODE prefix,
    and INTERACTIVE becomes False, disabling every real Slack button
    (Executed/Filled/Order Placed/Exited/Skipped) -- a severe, silent
    operational failure, not merely noisy."""
    import signals_config as cfg
    if cfg.SIM_MODE:
        return [
            "signals_config.SIM_MODE is True during a real daemon startup -- every "
            "Slack alert will be misleadingly prefixed 🧪 SIM MODE and every "
            "interactive button will be disabled. SIM_MODE=1 was likely already "
            "exported in the shell before `python active_signals.py run` -- "
            "os.environ.setdefault('SIM_MODE','0') only applies when nothing "
            "already set it. Unset SIM_MODE and restart the daemon."
        ]
    return []


CHECKS = [
    check_live_trailing_exit_automation_scope,
    check_research_mode_ticker_with_open_position_in_automation_scope,
    check_daily_sync_halted_nodes,
    check_daily_track_overlay_config_matches_live_track,
    check_live_node_missing_account,
    check_tax_advantaged_excluded_tickers,
    check_brokerage_not_live_with_unresolved_leverage_gap,
    check_starting_notional_within_account_notional_cap,
    check_open_position_config_matches_live_node,
    check_staged_config_matches_expected,
]


def run_all():
    violations = []
    for check in CHECKS:
        violations.extend(check())
    return violations


if __name__ == "__main__":
    import sys
    print_all_live_node_state()
    print()
    found = run_all()
    if found:
        print(f"{len(found)} invariant violation(s):")
        for v in found:
            print(f"  - {v}")
        sys.exit(1)
    print("All invariants hold.")
