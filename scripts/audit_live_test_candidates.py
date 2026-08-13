"""
Audits a set of candidate tickers against the 3 live-test scenarios (entry/
gap-resize, TIME-exit, TRAIL-exit -- see docs/live_test_coverage.md's
"Runbook: staging a real-order live test scenario") and reports which role
each candidate currently fits, from real broker + DB state -- so this
doesn't have to be re-derived by hand (one-off queries) each time.

Usage:
  .venv/bin/python scripts/audit_live_test_candidates.py --tickers SPY SH GDXU

For each ticker, reports:
  - real broker position (shares) and resting orders (type/side/qty/status)
  - DB open_positions row: shares, entry_price, trail_state (armed? peak?
    exit_order_id?), max_hold_hours vs real held bars
  - a DB-vs-broker share mismatch flag if they disagree
  - if flat (no DB position): the real z-score trigger distance (entry-test
    candidacy)
  - a verdict: which of entry / TIME-exit / TRAIL-exit this ticker currently
    fits, or "unclear" if it fits none cleanly
  - --staged mode only: whether the Grid scenario(s) this node's staged role
    exists to prove are ALREADY verified-live (possibly via a different
    node) -- a "STALE?" banner flags a staged detune that may no longer be
    doing anything useful. See SCENARIO_ROLE_TO_GRID_IDS below; extend it
    when staging a new scenario_role.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import active_signals as a
import signals_compute as sc
import signals_db as db
import schwab_client
import signals_helpers
from scripts.coverage_registry import REGISTRY, compute_status

# scenario_role (staged_test_config, free text) -> the Trade-Flow Accountability
# Grid row id(s) (scripts/coverage_registry.py's REGISTRY) it exists to prove.
# 'baseline_config' isn't a test scenario at all -- it's the generic per-node
# drift-check baseline every live node gets (signals_invariants.
# check_staged_config_matches_expected), not staged FOR anything, so it's
# deliberately absent here. Built 2026-08-10 after SH's staged 'time_exit_via_trail'
# detune (trail_buy_pct widened 1%->5% by a LATER, unrelated session for an
# entirely different scenario, post_fill_topup -- never reflected in this row
# at all) sat untouched for days after its actual scenario had already gone
# verified-live via a different node (RETL) and its config had drifted for a
# different, undocumented reason -- nothing anywhere connected "this staged
# detune's reason for existing" to "is that reason still true." Extend this
# dict when a new staged scenario_role is introduced (live-test-node-setup
# skill should update it as part of staging a new role).
SCENARIO_ROLE_TO_GRID_IDS = {
    'time_exit_via_trail': ['time_exit_trigger_armed'],
    'time_exit_via_sl': ['time_exit_trigger_unarmed'],
    'gap_resize_and_topup': ['gap_resize', 'post_fill_topup'],
    # Added 2026-08-12 alongside the staged_test_config multi-role migration
    # -- RETL genuinely carries both roles simultaneously (drought_overlay_
    # enabled=1 + addon_enabled=1), previously untracked since one node could
    # only hold one scenario_role before. Keep in sync with the duplicate
    # copy in scripts/coverage_check.py (see that copy's comment for why it's
    # duplicated, not imported).
    'drought_handoff': ['drought_entry', 'drought_handoff', 'drought_entry_placement',
                         'drought_handoff_cancel', 'drought_handoff_exit_placement',
                         'drought_handoff_alert_slot_preserved'],
    'addon': ['addon_entry_fill', 'addon_entry_placement', 'addon_exit_fill',
              'addon_exit_placement', 'addon_leg_independent_sl_fill_detection',
              'addon_leg_reconciliation', 'addon_second_ticker_buy_allowed',
              'addon_buying_power_check', 'addon_double_buy_exemption'],
}


def _scenario_relevance(scenario_role):
    """Returns a list of 'grid_id: status' strings for a staged scenario_role
    that maps to one or more Grid rows -- None if the role isn't a mapped
    test scenario (e.g. 'baseline_config', or a role added to staged_test_config
    without a corresponding SCENARIO_ROLE_TO_GRID_IDS entry yet)."""
    grid_ids = SCENARIO_ROLE_TO_GRID_IDS.get(scenario_role)
    if not grid_ids:
        return None
    by_id = {row['id']: row for row in REGISTRY}
    out = []
    for gid in grid_ids:
        row = by_id.get(gid)
        if row is None:
            out.append(f"{gid}: (no matching Grid row -- registry may have renamed/removed it)")
            continue
        status, detail = compute_status(row)
        out.append(f"{gid}: {status} ({detail})")
    return out


# _real_orders/_resting_orders moved to schwab_client.get_real_orders/
# filter_resting_orders 2026-08-10 so signals_helpers.get_full_position_state
# can reuse them without importing a scripts/ module from a core module.
_real_orders = schwab_client.get_real_orders
_resting_orders = schwab_client.filter_resting_orders


def _resolve_live_node(ticker):
    """get_watch_list_node returns None on ambiguity by design (see its
    docstring) -- fine for its real callers, but this script needs a specific
    node to audit. Prefer the mode='live' row when a ticker has more than
    one (e.g. a live + research pairing like GDXU); report ambiguity
    explicitly rather than silently returning nothing.

    Excludes paper_role='daily_sync' rows (the 2026-08-05 daily-track clones)
    -- without this, every v5 ticker with a daily-track node now resolves
    ambiguously (live-track research node vs. its daily-track sibling) even
    though a daily-track node is never the right thing to audit via a plain
    ticker lookup; it only ever gets addressed by its specific wl_id."""
    with db._conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM watch_list WHERE ticker = ? AND watchlist_id = ? AND paper_role IS NULL",
            (ticker, db.get_active_watchlist_id())).fetchall()]
    if not rows:
        return None, None
    live_rows = [r for r in rows if r.get("state") != "paper"]
    if len(live_rows) == 1:
        return live_rows[0], None
    if len(rows) == 1:
        return rows[0], None
    return None, rows


def _print_staged_config(node, source):
    """Looks up staged_test_config for this node and diffs expected_config
    against the real current value (source = pos if open, else node) --
    field by field, formulaic, not re-derived/re-explained by hand. source
    must expose the same keys as watch_list/open_positions (arm_sell_pct,
    fixed_sl, trail_sell_pct, max_hold_hours, trail_buy_pct,
    starting_notional)."""
    staged = [s for s in db.get_staged_test_configs() if s["wl_id"] == node["id"]]
    if not staged:
        return
    # A node can now genuinely carry more than one row (2026-08-12 migration,
    # see RETL: time_exit_via_sl + drought_handoff + post_fill_topup + addon
    # simultaneously) -- loop over every role instead of only staged[0], or
    # every role past the first would silently go unreported.
    for row in staged:
        print(f"  staged test role: {row['scenario_role']}")
        if row.get("notes"):
            print(f"    notes: {row['notes']}")
        mismatches = []
        for field, expected in row["expected_config"].items():
            actual = source.get(field)
            if actual is None:
                continue
            if abs(float(actual) - float(expected)) > 0.01:
                mismatches.append(f"{field}: expected {expected}, actual {actual}")
        if mismatches:
            print(f"  \U000026A0️  STAGED CONFIG MISMATCH: {'; '.join(mismatches)}")
        else:
            print(f"  staged config: matches expected ({', '.join(f'{k}={v}' for k, v in row['expected_config'].items())})")
        relevance = _scenario_relevance(row['scenario_role'])
        if relevance is not None:
            all_verified = all(r.startswith(f"{gid}: verified-live")
                                for r, gid in zip(relevance, SCENARIO_ROLE_TO_GRID_IDS[row['scenario_role']]))
            for r in relevance:
                print(f"    grid relevance: {r}")
            if all_verified:
                print(f"  \U0001F9F9 STALE? every Grid scenario this staged role exists to prove is already "
                      f"verified-live -- confirm whether this node should keep running hot. The staged "
                      f"config is a reusable regression fixture (see the live-test-node-setup skill's "
                      f"regression-pass mode) -- pause it from active live duty (state flip) rather than "
                      f"reverting/deleting its detune, so it can be re-run after future trading-code changes.")
        elif row['scenario_role'] == 'baseline_config' and _looks_like_buried_scenario(row.get('notes')):
            # LABD (wl_id=152) sat stuck for ~22h with a real, already-proven test
            # purpose ("SCENARIO CONTEXT... confirmed live 2026-08-07") written as
            # free-text prose inside a baseline_config row instead of its own
            # scenario_role -- invisible to the STALE check above since
            # scenario_role='baseline_config' is deliberately excluded from
            # SCENARIO_ROLE_TO_GRID_IDS (found+fixed 2026-08-11). This can't
            # compute an actual STALE verdict (no scenario_role to look up), so it
            # just surfaces the mismatch itself: prose claims a real test scenario,
            # role field says "not a test scenario at all."
            print(f"  ⚠️  scenario prose found in a baseline_config row -- this staged test "
                  f"purpose isn't tracked by the STALE check above (only scenario_role values in "
                  f"SCENARIO_ROLE_TO_GRID_IDS are). Give it its own scenario_role, or fold the "
                  f"scenario notes back into a properly-mapped row.")


def _looks_like_buried_scenario(notes):
    """Heuristic for the LABD failure shape: a baseline_config row's notes
    describing a real test scenario (arm/exit mechanics, a live-confirmation
    claim) instead of just the generic drift-check snapshot boilerplate every
    other baseline_config row carries. Deliberately loose (substring match,
    not a structured field) -- false positives just print an extra line for a
    human to dismiss; a false negative silently recreates the exact bug this
    check exists to catch."""
    if not notes:
        return False
    return 'SCENARIO CONTEXT' in notes.upper()


def audit_one(ticker, wl_id=None):
    print(f"\n=== {ticker} ===")
    # Node-scoped throughout (wl_id, not bare ticker) -- db.get_open_position(
    # ticker) resolves ambiguously ("whichever position has the latest
    # entry_time") once 2+ nodes exist for a ticker, which is now the normal
    # case (GDXU/DPST/RETL all have 2+ nodes). Resolving the node first via
    # _resolve_live_node (already ambiguity-safe, fixed 2026-07-28) and then
    # looking up by wl_id closes that gap (found by the user, 2026-07-29).
    #
    # wl_id, when given (e.g. --staged, which knows the exact node each
    # staged_test_config row belongs to), skips ticker-based resolution
    # entirely -- without this, two staged tests sharing one ticker would
    # both hit _resolve_live_node's ambiguity path and neither would show
    # its actual staged config (found live 2026-07-29, fixed before it was
    # ever actually hit).
    if wl_id is not None:
        node = db.get_watch_list_node_by_id(wl_id)
        ambiguous = None
        if node is None:
            print(f"  verdict: staged_test_config references wl_id={wl_id}, but no such node exists "
                  f"-- stale/dangling row, run scripts/seed_staged_test_config.py or clear it")
            return
    else:
        node, ambiguous = _resolve_live_node(ticker)
    if node is None:
        if ambiguous:
            modes = ", ".join(f"{r['account']}/{r['state']}" for r in ambiguous)
            print(f"  verdict: ambiguous -- {len(ambiguous)} nodes ({modes}), none uniquely non-paper")
        else:
            print("  verdict: no watch_list node -- not a candidate")
        return

    # get_real_position_state (added 2026-08-05) replaces this script's own
    # real/paper/pending assembly -- real wins on a collision (shouldn't
    # happen for one node -- it's either mode='live' with a real position or
    # mode='research' with a paper one, never both -- but real state should
    # never be shadowed).
    state = db.get_real_position_state(node['id'])
    real_pos = state['real_position']
    pos = real_pos or state['paper_position']
    is_paper = real_pos is None and pos is not None
    account = node.get("account")

    real_shares = None
    resting = []
    if account:
        try:
            real_shares = schwab_client.get_real_position(account, ticker)
        except Exception as e:
            print(f"  [warn] couldn't fetch real position: {e}")
        try:
            resting = _resting_orders(_real_orders(account, ticker))
        except Exception as e:
            print(f"  [warn] couldn't fetch real orders: {e}")

    print(f"  account: {account}")
    print(f"  node config: starting_notional=${node.get('starting_notional'):,.0f}  "
          f"trail_buy_pct={node.get('trail_buy_pct')}%  fixed_sl={node.get('fixed_sl')}%  "
          f"trail_sell_pct={node.get('trail_sell_pct')}%  arm_sell_pct={node.get('arm_sell_pct')}%")
    print(f"  real broker shares: {real_shares}")
    entry_price_for_pct = pos["entry_price"] if pos else None
    for o in resting:
        extra = ""
        if entry_price_for_pct:
            if o["orderType"] == "STOP" and o.get("stopPrice") is not None:
                pct_away = (entry_price_for_pct - o["stopPrice"]) / entry_price_for_pct * 100
                extra = f"  stopPrice=${o['stopPrice']:.2f} ({pct_away:+.1f}% from entry)"
            elif o["orderType"] == "TRAILING_STOP" and o.get("stopPriceOffset") is not None:
                extra = f"  trail_offset={o['stopPriceOffset']}"
        print(f"  resting order: {o['orderType']} {o['instruction']} qty={o['quantity']} "
              f"status={o['status']} id={o['orderId']}{extra}")
    if not resting:
        print("  resting order: none")

    # Overlay state (2026-08-08 addition) -- drought/addon run on their own
    # tables (drought is a normal open_positions/paper_positions row tagged
    # position_source='drought_overlay'; addon is a separate addon_legs/
    # paper_addon_legs table entirely), so neither was visible here before --
    # a real add-on BUY or drought entry could be sitting open on this node
    # with no trace in the morning check. is_paper mirrors node.state, not
    # the core position's own real/paper split, since a drought/addon leg
    # can exist independently of whatever core currently shows.
    overlay_is_paper = node.get('state') == 'paper'
    if node.get('drought_overlay_enabled'):
        dpos = db.get_drought_overlay_position(node['id'], paper=overlay_is_paper)
        if dpos is not None and dpos.get('status', 'open') != 'closed':
            dtag = " [PAPER]" if overlay_is_paper else ""
            print(f"  drought overlay position{dtag}: {dpos.get('shares'):g} shares @ "
                  f"${dpos['entry_price']:.4f} (entered {dpos['entry_time']})")
        else:
            print("  drought overlay position: none")
    if node.get('addon_enabled'):
        aleg = db.get_open_addon_leg_by_wl_id(node['id'], paper=overlay_is_paper)
        if aleg is not None:
            atag = " [PAPER]" if overlay_is_paper else ""
            print(f"  addon leg{atag}: {aleg.get('shares'):g} shares @ "
                  f"${aleg['entry_price']:.4f} (entered {aleg['entry_time']}, status={aleg.get('status')})")
        else:
            print("  addon leg: none")

    if pos is None:
        # A resting trailing-buy has no open_positions row yet (that's only created
        # on fill) -- checking pos alone here previously reported "flat" for a node
        # that's actually mid-entry, waiting on a broker fill (found 2026-08-04,
        # same blind spot as coverage_check.py's carryover-scoping bug above).
        pending = state['pending_buy']
        if pending:
            print(f"  DB position: none (flat) -- local pending-buy row on file: "
                  f"signal_price=${pending['signal_price']:.2f} signal_time={pending['signal_time']} "
                  f"order_placed={bool(pending['order_placed'])} order_id={pending.get('order_id')}")
            # A pending_buys row is a LOCAL record, not proof a real broker
            # order exists -- three genuinely different situations all
            # printed order_placed/order_id and looked identical without
            # this, which was the direct cause of a real confusion (user
            # checked Schwab for a ticker this script called "pending buy
            # resting" and found nothing -- 2026-08-07). LIVE-vs-not is the
            # single most important fact and must be checked first, not
            # buried after a generic dry_run/paper label (user's explicit
            # correction, same session) -- CANARY is a real, deliberately
            # distinct sub-case of not-live (version='canary' proof-of-life
            # nodes, dry_run by design), not just another flavor of "paper."
            _is_live = node.get('state') == 'live' and not signals_helpers.effectively_dry_run(account, node)
            _is_canary = node.get('version') == 'canary'
            if _is_live:
                if pending.get('order_id'):
                    print(f"  ✅ LIVE -- real broker order on file: id={pending['order_id']} -- if you "
                          f"don't see this at Schwab, that's a real discrepancy, verify directly against "
                          f"the order id.")
                elif pending.get('order_placed'):
                    print("  ⚠️  LIVE, but order_placed=True with NO order_id captured -- either placed "
                          "manually at the broker (never confirmed here) or an automated placement whose "
                          "id we couldn't extract. Cannot confirm a real order exists from this record "
                          "alone; verify at the broker directly.")
                else:
                    print("  ⚠️  LIVE, but order_placed=False -- no real order was ever placed for this "
                          "signal (e.g. blocked by a guard before reaching the broker, or still awaiting "
                          "manual confirmation). Nothing should exist at the broker for this yet.")
            elif _is_canary:
                print("  🧪 CANARY -- proof-of-life test node, dry_run by design. No real broker order was "
                      "ever placed or possible for this signal. Waiting on a synthetic bounce-fill "
                      "(update_dry_run_buys), not a real resting order. Check nothing at the broker.")
            else:
                print("  📝 PAPER/DRY_RUN -- this node's real order attempts are simulated (not a canary, "
                      "not live). No real broker order was ever placed or possible for this signal. "
                      "Waiting on a synthetic bounce-fill (update_dry_run_buys), not a real resting order.")
            print("  verdict: entry in progress (pending buy) -- not a real entry candidate right now")
            return
        paper_pending = state['paper_pending_buy']
        if paper_pending:
            print(f"  DB position: none (flat), but a paper pending buy is resting -- "
                  f"signal_price=${paper_pending['signal_price']:.2f} "
                  f"signal_time={paper_pending['signal_time']}")
            print("  verdict: entry in progress (paper pending buy) -- not a real entry candidate right now")
            return
        print("  DB position: none (flat)")
        sig = a.compute_buy_signal(node)
        if sig is None:
            print("  verdict: no signal data available -- can't assess entry candidacy")
            return
        cur, trigger = sig["current_price"], sig["lower_band"]
        pct = (cur - trigger) / trigger * 100
        print(f"  entry trigger: ${trigger:.2f}  current: ${cur:.2f}  ({pct:+.2f}% away)")
        verdict = "ENTRY candidate (flat, real trigger distance shown above)"
        print(f"  verdict: {verdict}")
        _print_staged_config(node, node)
        return

    db_shares = pos.get("shares")
    is_dry_run_sim = bool(pos.get("is_dry_run_sim"))
    tag = " [PAPER]" if is_paper else (" [DRY-RUN-SIM]" if is_dry_run_sim else "")
    print(f"  DB position{tag}: {db_shares:g} shares @ ${pos['entry_price']:.4f}  "
          f"(entered {pos['entry_time']})")
    # A dry_run-sim position never places a real broker order by design (the
    # whole reason it's synthesized) -- real_shares=0 there is correct, not a
    # mismatch. Missing this check produces a false MISMATCH warning on every
    # is_dry_run_sim position (found live 2026-07-29, IWM canary).
    mismatch = (not is_paper and not is_dry_run_sim and real_shares is not None
                and db_shares is not None and abs(real_shares - db_shares) > 1e-6)
    if mismatch:
        print(f"  \U000026A0️  MISMATCH: DB says {db_shares:g}, broker says {real_shares:g}")

    df_hourly, _ = sc._load_cache(ticker)
    held = None
    if df_hourly is not None:
        from datetime import datetime
        signal_time = datetime.strptime(pos["signal_time"], "%Y-%m-%d %H:%M:%S")
        held = sc._bars_held(df_hourly, signal_time)
    max_hold = pos.get("max_hold_hours")
    print(f"  held bars: {held}  /  max_hold_hours: {max_hold}")

    state = pos.get("trail_state") or {}
    armed = bool(state.get("trailing"))
    exit_order_id = state.get("exit_order_id") or (state.get("exit_pending") or {}).get("order_id")
    print(f"  trail_state: trailing={armed}  peak={state.get('peak')}  exit_order_id={exit_order_id}")
    print(f"  sl_order_id: {pos.get('sl_order_id')}")

    # Cross-check the DB's configured fixed_sl% against the real resting STOP order's
    # actual price -- found live 2026-07-29: SH's fixed_sl showed 0.3% (hair-trigger) in
    # the DB while the real order was placed far out-of-the-money ($26.57, ~20.7% away),
    # a real drift between config and the live order with no automated way to catch it
    # before this (the two numbers were never checked against each other).
    stop_orders = [o for o in resting if o["orderType"] == "STOP" and o.get("stopPrice") is not None]
    if stop_orders and pos.get("fixed_sl") is not None:
        real_pct = (pos["entry_price"] - stop_orders[0]["stopPrice"]) / pos["entry_price"] * 100
        configured_pct = pos["fixed_sl"]
        if abs(real_pct - configured_pct) > 1.0:
            print(f"  \U000026A0️  MISMATCH: fixed_sl configured at {configured_pct}%, but the real "
                  f"resting STOP order is {real_pct:.1f}% away from entry")

    # Arm-trigger distance -- for a deliberately-staged TIME-exit-via-SL test
    # (position should stay unarmed until max_hold_hours fires), this is the
    # real check that the test won't accidentally arm early. arm_sell_pct is
    # stored as take_profit for TrailingBothZScoreBreakout (see signals_db.
    # add_node) but reads back out under pos['arm_sell_pct'].
    arm_pct = pos.get("arm_sell_pct")
    if not armed and arm_pct is not None:
        arm_trigger = pos["entry_price"] * (1 + arm_pct / 100)
        pct_away = (arm_trigger - pos["entry_price"]) / pos["entry_price"] * 100
        print(f"  arm trigger: ${arm_trigger:.4f}  ({pct_away:+.2f}% above entry, arm_sell_pct={arm_pct}%)")

    if armed:
        verdict = "TRAIL-exit candidate (armed" + (", order tracked" if exit_order_id else ", NO order_id tracked -- won't auto-close") + ")"
    elif held is not None and max_hold is not None and held < max_hold:
        remaining = max_hold - held
        verdict = f"TIME-exit candidate (fires in ~{remaining} more held bars)"
    elif held is not None and max_hold is not None and held >= max_hold:
        verdict = "TIME-exit OVERDUE -- should have fired already, check why it hasn't"
    else:
        verdict = "unclear -- not armed, no max_hold_hours data"
    print(f"  verdict: {verdict}")
    _print_staged_config(node, pos)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", nargs="+", help="explicit ticker list")
    ap.add_argument("--staged", action="store_true",
                     help="audit every ticker currently in signals_db.staged_test_config "
                          "instead of an explicit list -- dynamic, not hand-typed")
    args = ap.parse_args()
    if not args.tickers and not args.staged:
        ap.error("pass --tickers T [T ...] or --staged")
    if args.staged:
        # Dedupe on wl_id -- a node can now hold multiple staged_test_config
        # rows (one per scenario_role, 2026-08-12 migration), but audit_one
        # already prints every role for a node in one pass (_print_staged_config
        # loops over all of them internally) -- without this, a multi-role node
        # like RETL would get audited (including a real broker query) once per
        # role instead of once total. First-seen order preserved, not sorted.
        seen_wl_ids = []
        for s in db.get_staged_test_configs():
            if s["wl_id"] not in seen_wl_ids:
                seen_wl_ids.append(s["wl_id"])
                audit_one(s["ticker"], wl_id=s["wl_id"])
    else:
        for ticker in args.tickers:
            audit_one(ticker)


if __name__ == "__main__":
    main()
