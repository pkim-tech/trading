"""Config-invariant sanity checks -- catch a misconfiguration that would silently
break an assumption baked into other code, before that code hits it live.

Run automatically at active_signals.py daemon startup (non-blocking Slack alert
if any violation is found) and standalone as a pre-commit sanity check:
    .venv/bin/python signals_invariants.py

Each check function returns a list of human-readable violation strings (empty =
clean) and documents, in its own docstring, exactly which downstream code relies
on the invariant -- so a violation is actionable without a backlog lookup.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import signals_db as db
import signals_helpers as helpers
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
        if node['state'] != 'paper' and node['strategy'] == 'TrailingExitZScoreBreakout':
            if node['ticker'] not in schwab_safety.AUTOMATION_ENABLED_TICKERS:
                violations.append(
                    f"{node['ticker']} (wl_id={node['id']}) is state={node['state']!r} "
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
        if node['state'] == 'paper' and node['ticker'] in schwab_safety.AUTOMATION_ENABLED_TICKERS:
            # wl_id-keyed, not ticker-only -- a ticker-only lookup (db.get_open_position)
            # would false-positive on a deliberate live+research node pair for the same
            # ticker (e.g. DPST/GDXU) whenever the *live* node holds the real position.
            pos = db.get_open_position_by_wl_id(node['id'], paper=False)
            if pos is not None:
                violations.append(
                    f"{node['ticker']} (wl_id={node['id']}) is state={node['state']!r} but has a "
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
        if node['state'] != 'paper' and not node.get('account'):
            violations.append(
                f"{node['ticker']} (wl_id={node['id']}) is state={node['state']!r} with account=None -- "
                f"check_order will fail closed as 'unknown account' for every order attempt."
            )
    return violations


def check_market_data_freshness():
    """Every real capital-at-stake ticker's cache/research/{ticker}_1h.csv should be
    current through the last real completed trading day -- staleness here silently
    corrupts any check that reads it directly, AND (found 2026-08-27, real DPST
    incident) a SEPARATE cache (massive_hourly_derived, no automated refresh) can go
    stale in a way that doesn't raise -- evening_status.py Part 3's kernel replay
    silently succeeded on 4-day-stale massive data and reported a real, correct live
    trade as a false PHANTOM. This check can only cover _1h.csv (massive_hourly_
    derived isn't a flat file, needs its own DB-side freshness check -- not built
    here, flagged as a real follow-up in docs/backlog_cache.md), but it's the same
    failure shape: don't wait to discover staleness ad hoc inside a report's error
    path, check it as a standing invariant instead.

    _1h.csv is genuinely yahoo-sourced (data_collector.py's fetch_live_data_smart,
    confirmed directly, 2026-08-27) -- NOT stale/leftover yahoo, deliberate design
    (build_massive_hourly_derived.py's own docstring: "Yahoo hourly stays completely
    separate, used only as an independent audit signal, never consumed directly").
    That's actually what makes it a genuinely independent cross-check against a
    massive-sourced kernel replay, not a same-source self-check that couldn't catch
    a massive-specific data bug."""
    import pandas as pd
    violations = []
    today = pd.Timestamp.now(tz="America/New_York").normalize().tz_localize(None)
    last_bday = pd.bdate_range(end=today, periods=2)[0]  # yesterday's business day, or
                                                           # today if today isn't one
    for node in db.get_watchlist():
        if not helpers.has_capital_at_stake(node):
            continue
        path = f"cache/research/{node['ticker']}_1h.csv"
        try:
            # NOT usecols=[0] -- a 1-column read (index_col=0 + usecols=[0]) leaves
            # zero real columns, and pandas' .empty checks BOTH axes, so a real,
            # populated index still reads as empty=True (caught live building this
            # check, 2026-08-27 -- read the real columns, check len() not .empty).
            df = pd.read_csv(path, index_col=0, parse_dates=True)
        except FileNotFoundError:
            violations.append(f"{node['ticker']} (wl_id={node['id']}): no {path} on file at all")
            continue
        if len(df) == 0 or df.index.max().normalize() < last_bday:
            last = df.index.max() if len(df) else "empty file"
            violations.append(f"{node['ticker']} (wl_id={node['id']}): {path} stale "
                               f"(last bar {last}, need >= {last_bday.date()})")
    return violations


def check_massive_hourly_derived_freshness():
    """Sibling to check_market_data_freshness (_1h.csv/yahoo) -- covers the OTHER real
    data source, massive_hourly_derived (the DB-backed cache evening_status.py Part 3's
    kernel replay actually reads for GT nodes). Real incident, 2026-08-27: this cache
    went 4 real trading days stale with zero automated refresh, and because the
    staleness guard inside get_trades_and_bars_since_ground_truth only checks MINUTE
    data (not hourly), the kernel replay silently succeeded on stale hourly data
    instead of raising -- producing a false PHANTOM flag on a real, correct DPST trade
    rather than a loud error. check_market_data_freshness's yahoo-sourced cross-check
    catches this AFTER the fact, per-PHANTOM; this check exists to catch it BEFORE
    anything reads the stale cache at all, same as _1h.csv's own check.

    Cheap: MAX(ts) per ticker's current build, not a full DataFrame load (this DB has
    60M+ raw rows across all tickers/builds -- loading full OHLCV per ticker here would
    be real, avoidable cost for a check that only needs one timestamp)."""
    import sqlite3
    import pandas as pd
    import db_cache
    violations = []
    today = pd.Timestamp.now(tz="America/New_York").normalize().tz_localize(None)
    last_bday = pd.bdate_range(end=today, periods=2)[0]
    with sqlite3.connect(db_cache.DB_PATH) as conn:
        for node in db.get_watchlist():
            if not helpers.has_capital_at_stake(node):
                continue
            ticker = node['ticker']
            row = conn.execute("""
                SELECT MAX(d.ts) FROM massive_hourly_derived_builds b
                JOIN massive_hourly_derived d ON d.ticker=b.ticker AND d.build_id=b.id
                WHERE b.ticker=? AND b.id = (
                    SELECT b2.id FROM massive_hourly_derived_builds b2
                    WHERE b2.ticker=b.ticker AND EXISTS (
                        SELECT 1 FROM massive_hourly_derived d2
                        WHERE d2.ticker=b2.ticker AND d2.build_id=b2.id
                    )
                    ORDER BY b2.id DESC LIMIT 1
                )
            """, (ticker,)).fetchone()
            last_ts = row[0] if row else None
            if last_ts is None:
                violations.append(f"{ticker} (wl_id={node['id']}): no massive_hourly_derived "
                                   f"build with real rows on file at all")
                continue
            last_dt = pd.Timestamp(last_ts).normalize()
            if last_dt < last_bday:
                violations.append(f"{ticker} (wl_id={node['id']}): massive_hourly_derived stale "
                                   f"(last bar {last_ts}, need >= {last_bday.date()})")
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
    Uses the same explicit accounts.is_tax_advantaged lookup as add_node's
    guard (signals_db._is_tax_advantaged_account) -- no longer a substring
    guess on the account name (2026-08-11, see docs/deep_backlog.md's
    accounts-table entry). An unrecognized account alias is reported as its
    own violation below rather than letting the ValueError crash run_all().

    Excluded-ticker set (2026-08-15): unions the legacy hardcoded
    db.TAX_ADVANTAGED_EXCLUDED_TICKERS (USO/AGQ) with every ticker the real
    research-side screener (trading_universe.db's tickers.k1_status,
    scripts/candidate_full_review.py's K1_STATUS convention) has confirmed
    K-1 -- so a future K-1 ticker (UCO/SCO/ZSL/UVIX are already confirmed
    there but weren't in the hardcoded set) gets caught automatically
    instead of needing its own hardcoded addition every time. Per the
    2026-08-12 K-1 policy generalization (docs/backlog_cache.md), K-1 alone
    doesn't disqualify a ticker -- it only restricts it to the taxable
    `brokerage` account, which is exactly what this check already enforces
    (real capital in a tax-advantaged IRA/Roth/SEP account is the actual
    UBTI risk, not K-1 status by itself). The research DB query is
    best-effort: if `trading_universe.db` is unavailable/locked (e.g. a
    concurrent sweep holding it), silently falls back to the hardcoded set
    rather than blocking daemon startup on a research-side dependency.
    Scoped to db.get_live_nodes() (all watchlists), not get_watchlist()
    (active watchlist only) -- real live nodes span more than one
    watchlist, same reasoning as get_live_nodes()'s own docstring.
    """
    excluded = set(db.TAX_ADVANTAGED_EXCLUDED_TICKERS)
    try:
        import sqlite3
        import signals_config
        conn = sqlite3.connect(f"file:{signals_config.RESEARCH_DB_PATH}?mode=ro", uri=True, timeout=2)
        try:
            excluded |= {
                row[0].upper() for row in
                conn.execute("SELECT symbol FROM tickers WHERE k1_status LIKE 'CONFIRMED K-1%'").fetchall()
            }
        finally:
            conn.close()
    except Exception:
        pass

    violations = []
    for node in db.get_live_nodes():
        ticker = (node.get('ticker') or '').upper()
        account = node.get('account') or ''
        if ticker not in excluded or not account:
            continue
        try:
            is_tax_advantaged = db._is_tax_advantaged_account(account)
        except ValueError:
            violations.append(
                f"{ticker} (wl_id={node['id']}) has unrecognized account={account!r} -- "
                f"cannot verify tax-advantaged exclusion (K-1/UBTI risk check skipped)."
            )
            continue
        if is_tax_advantaged:
            violations.append(
                f"{ticker} (wl_id={node['id']}) is in account={account!r}, but "
                f"is on the tax-advantaged exclusion list (K-1/UBTI risk) -- "
                f"remove the node or move it to a taxable account."
            )
    return violations


def check_margin_floor_zero_for_trading_enabled_accounts():
    """AccountLimits.margin_floor is dormant scaffolding, not active
    functionality -- it exists to let a genuine full-margin account's real
    CORE-entry cash go negative up to a deliberately-set real borrowing
    limit (schwab_safety.py's cash check, ~line 1614), but every account
    defaults to 0.0 (cash-only core entries), matching the user's explicit
    design call that ordinary trades should stay cash-only and only add-on
    legs use margin. It's a plain DB float (accounts.margin_floor) any
    script could set -- nothing else guards it. A nonzero value on a
    trading_enabled account would silently reopen the exact core-entry
    leverage-inclusive-cash gap that check_brokerage_not_live_with_
    unresolved_leverage_gap (removed 2026-08-12 once its own two gaps were
    fixed) used to bound -- this is the narrow, still-live piece of that
    same risk, not a full replacement for that check's broader scope."""
    violations = []
    for alias, limits in schwab_safety.ACCOUNTS.items():
        if limits.trading_enabled and limits.margin_floor != 0.0:
            violations.append(
                f"accounts.{alias}.margin_floor is {limits.margin_floor} (nonzero) on a "
                f"trading_enabled account -- this lets real core-entry cash go negative on "
                f"margin, a deliberate design reversal that should be a reviewed decision, "
                f"not a silent DB edit."
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
        if node['state'] == 'paper':
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


def check_starting_notional_override_has_staged_config():
    """A node's starting_notional_override should only ever be permanent for a
    deliberately-staged test node (tracked via staged_test_config, per
    .claude/skills/live-test-node-setup/SKILL.md's convention) -- everything else
    (a manual sizing nudge, a temporary bump) is expected to get cleared again.

    Found 2026-08-26 while staging an ERY test node: db.clear_starting_notional_
    override() exists but is never called anywhere in the codebase except its own
    definition -- nothing actually clears an override once set. That makes an
    override left on a real production node (e.g. after a one-off manual nudge
    that was meant to be temporary) invisible forever, since nothing currently
    checks that a non-null override has a documented reason on file. This check
    is that documentation requirement.

    Deliberately requires a staged_test_config row whose expected_config
    literally documents 'starting_notional_override' as a key -- NOT just "any
    staged row for this wl_id" (the first version of this check, caught by
    paired review 2026-08-26): every mode='live' node gets an auto-seeded
    baseline_config row from scripts/seed_baseline_config.py regardless of
    whether it carries an override, and a node can carry other, unrelated
    staged_test_config roles (e.g. RETL's time_exit_via_sl/drought_handoff,
    both predating its override) -- "some row exists" passed both of the two
    real overrides on file without either one's row actually mentioning the
    override, which defeats the whole point.

    Deliberately queries watch_list directly (all watchlists, any state,
    archived excluded) rather than db.get_watchlist() (active-watchlist-only)
    or db.get_live_nodes() (state='live' only) -- the thing being audited is a
    column that a paper/dry_run/staged-test node can just as legitimately
    carry as a state='live' one (this file's other live-money checks use
    get_live_nodes() because THEY care about live capital specifically; this
    one cares about the column, not liveness). Archived nodes are excluded
    since they can no longer place a real order regardless of sizing config."""
    violations = []
    documented_wl_ids = {
        row['wl_id'] for row in db.get_staged_test_configs()
        if 'starting_notional_override' in row['expected_config']
    }
    with db._conn() as c:
        nodes = [dict(r) for r in c.execute(
            "SELECT * FROM watch_list WHERE archived_at IS NULL "
            "AND starting_notional_override IS NOT NULL")]
    for node in nodes:
        if node['id'] not in documented_wl_ids:
            violations.append(
                f"{node['ticker']} (wl_id={node['id']}) has starting_notional_override="
                f"${node['starting_notional_override']:,.0f} but no staged_test_config row "
                f"documents it (expected_config must include a 'starting_notional_override' "
                f"key) -- either stage it (set_staged_test_config) or clear it "
                f"(clear_starting_notional_override) if it wasn't meant to be permanent."
            )
    return violations


def check_starting_notional_override_once_stuck():
    """Companion to check_starting_notional_override_has_staged_config -- flags
    a node whose starting_notional_override_once is still set even though it
    has an open position entered AFTER the override was set. That's exactly
    the signature of "should have auto-cleared on the fill that consumed it
    (signals_db.open_position(), same transaction as the position record
    write) but didn't" -- real, ongoing verification that the consume-on-fill
    logic actually works, not just code-review confidence.

    Timezone trap, checked directly rather than assumed (found while building
    this 2026-08-26): watch_list_audit.ts is written via sqlite's
    datetime('now'), which is UTC; open_positions.entry_time is written via
    Python's datetime.now(), which is this host's LOCAL time -- confirmed via
    `timedatectl` to be America/New_York (matches CLAUDE.md's ET signal-window
    convention). Comparing the two naively would be off by 4-5 hours
    (EDT/EST) -- converts the audit timestamp to ET before comparing.

    Skips a node with no set_starting_notional_override_once audit row at all
    (can't determine when it was set -- e.g. a value written by a future
    direct-DB path this function doesn't know about) rather than guessing a
    might-be-wrong "always violated" or "never violated" default.

    Checks BOTH open_positions (still-open) and trade_log (already closed) --
    paired review finding 2026-08-26: an open_positions-only check goes silent
    the moment the consuming position closes (the row is DELETEd, not
    archived), even though a stuck field is still stuck. Both queries exclude
    is_dry_run_sim=1 rows -- open_position()'s clear itself is gated on real
    fills only (not paper, not dry-run-sim), so a dry_run node's synthesized
    fill is CORRECTLY never cleared and must not be reported as a bug (found
    live: 9 of 12 real open_positions rows on file are dry_run today)."""
    violations = []
    with db._conn() as c:
        nodes = [dict(r) for r in c.execute(
            "SELECT * FROM watch_list WHERE archived_at IS NULL "
            "AND starting_notional_override_once IS NOT NULL")]
        for node in nodes:
            pos = c.execute(
                "SELECT entry_time FROM open_positions WHERE wl_id=? AND is_dry_run_sim=0 "
                "ORDER BY entry_time DESC LIMIT 1",
                (node['id'],)
            ).fetchone()
            if pos is None:
                pos = c.execute(
                    "SELECT entry_time FROM trade_log WHERE wl_id=? AND is_dry_run_sim=0 "
                    "ORDER BY entry_time DESC LIMIT 1",
                    (node['id'],)
                ).fetchone()
            if pos is None:
                continue
            audit_row = c.execute(
                "SELECT ts FROM watch_list_audit WHERE watch_id=? "
                "AND action='set_starting_notional_override_once' ORDER BY id DESC LIMIT 1",
                (node['id'],)
            ).fetchone()
            if audit_row is None:
                continue
            set_ts_et = (datetime.strptime(audit_row[0], '%Y-%m-%d %H:%M:%S')
                         .replace(tzinfo=ZoneInfo('UTC'))
                         .astimezone(ZoneInfo('America/New_York'))
                         .replace(tzinfo=None))
            entry_time = datetime.strptime(pos[0], '%Y-%m-%d %H:%M:%S')
            if entry_time > set_ts_et:
                violations.append(
                    f"{node['ticker']} (wl_id={node['id']}) has starting_notional_override_once="
                    f"${node['starting_notional_override_once']:,.0f} set at {set_ts_et} (ET) but "
                    f"an open position entered {entry_time} (ET, after the override was set) -- "
                    f"should have auto-cleared on that fill (signals_db.open_position) but didn't."
                )
    return violations


def check_open_position_config_matches_live_node():
    """An open position's snapshotted max_hold_hours/fixed_sl/account should
    match its node's current live watch_list config, unless deliberately
    diverged.

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
    does on purpose. The point is visibility, not a hard rule.

    account (added 2026-08-18) is the same staleness shape but with sharper
    real-money consequences: several signals_notify.py call sites (e.g.
    _place_stop_loss_for_position) read node.get('account') rather than
    pos.get('account') when acting on an already-open position, so a node
    whose account column changed after entry could route a real protective
    stop-order ATTEMPT at the WRONG account -- one that doesn't hold the
    shares (check_order's own position/oversell guards would likely block
    the attempt rather than let it silently succeed, but "likely blocked" is
    not a substitute for never routing wrong in the first place). Confirmed
    2026-08-17 that no real/production code path currently mutates
    watch_list.account in place on a node with an open position -- only 2
    test/sandbox scripts mutate account at all -- but a raw
    `UPDATE watch_list SET account=...` is itself an established idiom in
    this codebase (see the dominant node-creation path's own post-insert
    account assignment), just one that currently only ever runs pre-position.
    That's a timing accident, not a structural guard, so this check is a
    real backstop, not insurance against a purely hypothetical future
    script. String comparison, not the numeric fields' abs()-based one --
    account has no meaningful "close enough" representation-difference case
    the way a float does.

    node is None (2026-08-18): a position whose node was hard-deleted (e.g.
    remove_node(), which has no open-position guard -- see its docstring)
    is flagged as its own violation rather than silently skipped. Found
    during paired review of the account-drift addition above: the
    project's OWN documented account-move convention is "retire the old
    node, create a fresh one" -- and "retire" here means remove_node()'s
    unguarded DELETE, not a state flip. Doing that while a position is open
    leaves the position's wl_id dangling with zero visibility anywhere else
    in this file (verified: no other check in CHECKS covers an orphaned
    open_positions row). This blind spot predates the account field above
    (it silently swallowed max_hold_hours/fixed_sl drift the same way since
    2026-07-29) but is called out now because the account docstring above
    specifically leans on "retire-and-recreate is the safe path" -- which is
    exactly the path this blind spot hid."""
    violations = []
    for pos in db.get_open_positions():
        node = db.get_watch_list_node_by_id(pos.get('wl_id'))
        if node is None:
            violations.append(
                f"{pos['ticker']} (position id={pos['id']}, wl_id={pos.get('wl_id')}) -- "
                f"this open position's node no longer exists in watch_list (likely deleted via "
                f"remove_node() while the position was still open) -- no config/account comparison "
                f"is possible, and the real exit-check may be running with nothing to compare against."
            )
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
        pos_account, node_account = pos.get('account'), node.get('account')
        if pos_account is not None and node_account is not None and pos_account != node_account:
            violations.append(
                f"{pos['ticker']} (position id={pos['id']}, wl_id={node['id']}) account: "
                f"position pinned to {pos_account!r} vs node's current account={node_account!r} -- "
                f"a real protective-stop placement reading node.get('account') could attempt to route "
                f"to the WRONG account for this position (check_order's own guards would likely block "
                f"the attempt rather than let it silently succeed, but should never be the only defense)."
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
    """All three tiers with real (or dry-run-against-real-code) mode='live'
    nodes -- soxl_ira (SH/RETL/GDXU/DPST/SPY), ira (the 13 canary/mirror
    nodes), and brokerage (the SOXL drought/addon dry-run canaries added
    2026-08-1x, margin-typed/dry_run=True -- the plan's own prerequisite
    rehearsal layer before any real order) -- as three back-to-back tables,
    one call site for every caller that wants the full readiness picture
    (daemon startup, the 7am/EOD daily slots, and this module's own CLI).
    brokerage added after being found missing entirely: seeding a
    staged_test_config baseline for a new live node is silently useless if
    nothing ever prints its drift status."""
    print_staged_config_status(account='soxl_ira')
    print()
    print_staged_config_status(account='ira')
    print()
    print_staged_config_status(account='brokerage')


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


def check_addon_drought_live_nodes_have_coherent_account_type():
    """A mode='live' node with addon_enabled=1 must sit on a margin_capable
    account (add-on is structurally a margin borrow -- schwab_safety.check_
    order's is_addon_leg preconditions hard-refuse a non-margin_capable
    account already). A node with drought_overlay_enabled=1 must sit on a
    cash_settlement_type=='margin' account (real drought HANDOFF closes
    drought same-day and re-enters core same-day/same-ticker, which
    same_day_block hard-blocks on a cash-settlement account --
    docs/plans/real_order_execution_drought_addon.md 0.3/0.8). These were one
    combined account_type=='margin' check before 2026-08-11's accounts-table
    split -- now checked against the specific field each flag actually
    depends on, since a future account could plausibly have one without the
    other. Fail loud at daemon start rather than at the first real arm/gap
    event, matching this file's existing pattern (e.g.
    check_starting_notional_within_account_notional_cap)."""
    violations = []
    for node in db.get_watchlist():
        if node['state'] == 'paper':
            continue
        account = node.get('account')
        limits = schwab_safety.ACCOUNTS.get(account) if account else None
        if node.get('addon_enabled') and (limits is None or not limits.margin_capable):
            violations.append(
                f"{node['ticker']} (wl_id={node['id']}) has addon_enabled=1 and mode='live' but "
                f"account={account!r} is not margin_capable "
                f"({limits.margin_capable if limits else 'unknown'!r}) -- add-on's is_addon_leg "
                f"preconditions require real margin-borrowing eligibility."
            )
        if node.get('drought_overlay_enabled') and (limits is None or limits.cash_settlement_type != 'margin'):
            violations.append(
                f"{node['ticker']} (wl_id={node['id']}) has drought_overlay_enabled=1 and mode='live' but "
                f"account={account!r} is not cash_settlement_type=='margin' "
                f"({limits.cash_settlement_type if limits else 'unknown'!r}) -- drought HANDOFF's "
                f"same-day core re-entry requires same-day cash-settlement exemption."
            )
    return violations


# Live-state tables: a retired alias here would be a real, current problem
# (a node/position/pool still pointing at an account that's been retired) --
# checked against non-retired aliases only. Historical/log tables: a retired
# alias here is EXPECTED and correct once an account is ever retired (old
# rows don't get rewritten) -- checked against every alias that ever
# existed, retired or not, so retiring an account doesn't make this
# permanently red (cold-review finding, 2026-08-11).
_ACCOUNT_COLUMN_TABLES_LIVE = [
    'watch_list', 'open_positions', 'paper_positions', 'skim_reserve_pool',
]
_ACCOUNT_COLUMN_TABLES_HISTORICAL = [
    'trade_log', 'paper_trade_log', 'addon_legs', 'paper_addon_legs',
    'coverage_snoozes', 'skim_reserve_log', 'trading_incidents',
]


def check_all_account_values_are_known_aliases():
    """Every non-NULL `account` value actually present across the 11 tables
    that carry one (confirmed via a live PRAGMA table_info sweep, 2026-08-11
    -- don't trust a hardcoded list here without reconfirming, that's exactly
    how this list itself could go stale) must exist as a known alias in the
    `accounts` table -- non-retired only for live-state tables, any alias
    ever (retired included) for historical/log tables. Replaces the FK
    enforcement `account` columns don't have (plain TEXT, no CHECK
    constraint anywhere). Depends on this: every schwab_safety.ACCOUNTS.get(
    account) call site treats an unknown key as "no limits apply" (returns
    None, usually then skipped/fails soft) rather than "this is a typo or an
    orphaned/retired alias" -- this check is what actually catches that
    class of mistake, since nothing else does."""
    violations = []
    known_live = set(schwab_safety.ACCOUNTS.keys())
    with db._conn() as c:
        known_all = {r[0] for r in c.execute("SELECT alias FROM accounts").fetchall()}
        for table, known in (
            [(t, known_live) for t in _ACCOUNT_COLUMN_TABLES_LIVE]
            + [(t, known_all) for t in _ACCOUNT_COLUMN_TABLES_HISTORICAL]
        ):
            rows = c.execute(
                f"SELECT DISTINCT account FROM {table} WHERE account IS NOT NULL"
            ).fetchall()
            for (account,) in rows:
                if account not in known:
                    scope = "non-retired " if table in _ACCOUNT_COLUMN_TABLES_LIVE else ""
                    violations.append(
                        f"{table}.account={account!r} is not a known {scope}"
                        f"account alias (accounts table)"
                    )
    return violations


def check_paper_position_on_non_paper_node():
    """A paper_positions row whose node is NOT state='paper'.

    Cheap, DB-only, and it detects the condition behind a real defect: until
    2026-08-15 active_signals unioned paper and real position keys into one
    duplicate-suppression set, so a paper position on a node that had since
    flipped to state='live' silently blocked every real BUY on it. That is not
    hypothetical -- SOXL/ira wl_id=92 ($10k live) carried an open paper
    position across its 2026-08-10 paper->live flip until 2026-08-13, roughly 3
    trading days during which its real entries were dropped with no Slack
    message and no coverage event. Only luck (no signal fired) kept it free.

    The union itself is fixed (_position_keys_by_book), so this row can no
    longer block a real entry. This check exists because NOTHING anywhere
    reported the underlying state, and it stays anomalous for other reasons: a
    live node accruing simulated fills is a config/lifecycle mistake worth
    seeing. paper_trading.check_paper_sells drains such rows on their own exit
    conditions, so no cleanup automation is needed -- only visibility."""
    violations = []
    with db._conn() as c:
        rows = c.execute(
            "SELECT pp.id, pp.ticker, pp.wl_id, pp.entry_time, wl.state, wl.account "
            "FROM paper_positions pp LEFT JOIN watch_list wl ON wl.id = pp.wl_id "
            "WHERE pp.wl_id IS NOT NULL AND (wl.state IS NULL OR wl.state != 'paper')"
        ).fetchall()
    for r in rows:
        violations.append(
            f"paper_positions id={r['id']} ({r['ticker']}, wl_id={r['wl_id']}) belongs to a node with "
            f"state={r['state']!r} account={r['account']!r}, not 'paper' -- a simulated position is "
            f"open against a non-paper node (open since {r['entry_time']})"
        )
    return violations


def check_live_node_missing_candidate_link():
    """Every state='live' watch_list node should have a recorded
    watch_list_candidate_link row (any role) tracing it back to the real
    candidate_nodes row it was actually promoted from.

    Promoted from scripts/evening_status.py's Part 2 report (2026-08-19,
    Task #7) -- that section only ever PRINTED the gap for a human to notice
    during an interactive read, so a real live node could go untraced
    indefinitely with nothing forcing anyone to look. Depends on this:
    watch_list_candidate_link exists specifically so a node's real
    core-strategy provenance is never re-derived by guessing (see that
    table's own CREATE TABLE comment for the 2026-08-11 incident that
    motivated it -- exact-param-tuple guessing got it wrong multiple times).
    An unlinked live node means that protection doesn't apply to it: any
    future question about "what candidate was this promoted from, and does
    the current live config still match" has no answer on file. Does NOT
    itself imply the node is mis-configured -- only that its provenance
    isn't recorded. get_live_nodes() already excludes archived_at IS NOT
    NULL nodes, matching evening_status.py's existing scope exactly."""
    violations = []
    linked_wl_ids = {link['wl_id'] for link in db.get_candidate_links()}
    for node in db.get_live_nodes():
        if node['id'] not in linked_wl_ids:
            violations.append(
                f"{node['ticker']} (wl_id={node['id']}, account={node.get('account')!r}) is state='live' "
                f"with no watch_list_candidate_link row -- its real candidate-promotion provenance "
                f"isn't traceable on file."
            )
    return violations


def check_live_overlay_missing_validation_link():
    """Every state='live' watch_list node with drought_overlay_enabled=1 or
    addon_enabled=1 should have a matching watch_list_overlay_link row for
    that overlay_type, pointing at where its parameters were actually
    validated.

    Built 2026-08-19 (Task #7), real incident: SOXS/ira (wl_id=206,
    real $10k) had drought_overlay_enabled=1/confirm_days=1 since 2026-08-12
    with ZERO validation ever run -- set_drought_config is a raw DB write,
    no gate anywhere required docs/overlay_parameter_robustness_process.md's
    real methodology (fit-half search, single-trade-removal stress test) to
    have actually run first. Later research (a peer session's Task #2)
    confirmed no confirm_days value 1-20 works for SOXS at all -- the `1`
    traced to an unrelated 2026-08-12 staged-test batch (FAS/TMF also set to
    confirm_days=1 that day, both genuinely tiny-notional staged-test nodes
    where a fast-triggering value makes sense) that SOXS apparently
    inherited without separate calibration for its real-capital role.
    watch_list_overlay_link exists so this can't happen silently again --
    see that table's own CREATE TABLE comment for the full design. Checks
    drought_overlay and addon independently (a node can have one, both, or
    neither enabled); a node with neither enabled is correctly skipped
    entirely, not flagged as "missing" a link it has no need for.

    A link with verdict='NO_REAL_SELECTION' is flagged SEPARATELY and more
    loudly, not treated as satisfied -- found by paired review, 2026-08-19:
    the real KORU/roth (wl_id=202, $10k) case is exactly this shape (its
    drought config was found to be a pure overfitting artifact and
    REJECTED, per docs/research_log.md, yet the node is still live with it
    enabled). A live node running a config the validation process
    explicitly rejected is a WORSE state than "never checked", and the
    original version of this check treated both identically (any row at
    all silenced it), which would have hidden the more urgent case behind
    the more routine one. Includes each violation's starting_notional so a
    genuinely real node (SOXS, $10k) doesn't read identically to a
    deliberately-detuned staged-test node (TMF, $50) that will never have
    (or need) a formal fit-half/stress-test validation -- found by both
    reviewers: without this, ~half the real violations today are staged-
    test noise indistinguishable from the real-capital ones the dispatch
    was actually motivated by."""
    violations = []
    linked = {(link['wl_id'], link['overlay_type']): link for link in db.get_overlay_links()}
    for node in db.get_live_nodes():
        for column, overlay_type in (('drought_overlay_enabled', 'drought_overlay'),
                                      ('addon_enabled', 'addon')):
            if not node.get(column):
                continue
            key = (node['id'], overlay_type)
            notional = f"${node.get('starting_notional'):,.0f}" if node.get('starting_notional') else "unknown $"
            base = (f"{node['ticker']} (wl_id={node['id']}, account={node.get('account')!r}, "
                    f"starting_notional={notional}) is state='live' with {column}=1")
            if key not in linked:
                violations.append(
                    f"{base} but no watch_list_overlay_link row for overlay_type={overlay_type!r} -- "
                    f"this overlay's parameters have no recorded validation."
                )
            elif (linked[key].get('verdict') or '').strip().upper() == 'NO_REAL_SELECTION':
                violations.append(
                    f"🚨 {base} whose linked validation (overlay_type={overlay_type!r}) has verdict="
                    f"'NO_REAL_SELECTION' -- this node is running a config the validation process "
                    f"EXPLICITLY REJECTED, not just an unvalidated one: {linked[key]['validation_ref']}"
                )
    return violations


CHECKS = [
    check_paper_position_on_non_paper_node,
    check_live_trailing_exit_automation_scope,
    check_research_mode_ticker_with_open_position_in_automation_scope,
    check_daily_sync_halted_nodes,
    check_daily_track_overlay_config_matches_live_track,
    check_live_node_missing_account,
    check_tax_advantaged_excluded_tickers,
    check_margin_floor_zero_for_trading_enabled_accounts,
    check_starting_notional_within_account_notional_cap,
    check_starting_notional_override_has_staged_config,
    check_starting_notional_override_once_stuck,
    check_open_position_config_matches_live_node,
    check_staged_config_matches_expected,
    check_addon_drought_live_nodes_have_coherent_account_type,
    check_all_account_values_are_known_aliases,
    check_market_data_freshness,
    check_massive_hourly_derived_freshness,
]

# TRACEABILITY_CHECKS: deliberately NOT in CHECKS/run_all() (2026-08-19,
# paired review finding, both independent-cold and contextual reviewers
# converged): every other CHECKS member is a "should always be zero, a
# violation means something is already broken" invariant, and run_all()'s
# every caller (daemon startup/07:00/EOD Slack alerts, this module's own
# __main__ sys.exit(1) pre-commit gate) treats ANY violation as something to
# page/fail loudly on. These two checks are structurally different: as of
# 2026-08-19 they surface 21 REAL, ALREADY-KNOWN backlog gaps (7 candidate-
# link, 14 overlay-validation) that will take real research/promotion work
# to close, not a config bug to fix same-session -- folding them into
# run_all() would have gone from a genuinely clean "all invariants hold"
# signal straight to a permanent 21-line Slack wall repeated 3x/day (daemon
# startup + 07:00 + EOD) with no acknowledgment mechanism (this project's
# existing pattern for exactly this shape -- a known, explained, expected-
# to-persist-for-a-while condition -- is coverage_deviations'
# explain_deviation ticket model, which run_all()'s callers don't have).
# Run standalone (this module's __main__, below) so a human running the
# documented pre-commit check still sees them, clearly separated and
# non-blocking (does NOT affect the sys.exit(1) exit code) -- surfaced, not
# silenced, but not turned into unbounded daemon Slack noise either. Wiring
# these into the daemon's automatic alert path is a real follow-up worth
# its own decision (an ack/exemption mechanism, or dedup-on-change instead
# of dedup-never), not decided here.
TRACEABILITY_CHECKS = [
    check_live_node_missing_candidate_link,
    check_live_overlay_missing_validation_link,
]


def run_all():
    violations = []
    for check in CHECKS:
        violations.extend(check())
    return violations


def run_traceability_checks():
    violations = []
    for check in TRACEABILITY_CHECKS:
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

    print()
    traceability_gaps = run_traceability_checks()
    if traceability_gaps:
        print(f"{len(traceability_gaps)} traceability gap(s) (backlog items, non-blocking):")
        for v in traceability_gaps:
            print(f"  - {v}")
    else:
        print("No traceability gaps.")

    if found:
        sys.exit(1)
    print("All invariants hold.")
