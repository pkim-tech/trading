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
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import active_signals as a
import signals_compute as sc
import signals_db as db
import schwab_client


def _real_orders(account, ticker):
    account_hash = schwab_client._resolve_account_hashes()[account]
    client = schwab_client._get_client()
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


def _resting_orders(orders):
    return [o for o in orders if o["status"] in ("WORKING", "AWAITING_STOP_CONDITION", "QUEUED", "ACCEPTED", "PENDING_ACTIVATION")]


def _resolve_live_node(ticker):
    """get_watch_list_node returns None on ambiguity by design (see its
    docstring) -- fine for its real callers, but this script needs a specific
    node to audit. Prefer the mode='live' row when a ticker has more than
    one (e.g. a live + research pairing like GDXU); report ambiguity
    explicitly rather than silently returning nothing."""
    with db._conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM watch_list WHERE ticker = ? AND watchlist_id = ?",
            (ticker, db.get_active_watchlist_id())).fetchall()]
    if not rows:
        return None, None
    live_rows = [r for r in rows if r.get("mode") == "live"]
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
    row = staged[0]
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
            modes = ", ".join(f"{r['account']}/{r['mode']}" for r in ambiguous)
            print(f"  verdict: ambiguous -- {len(ambiguous)} nodes ({modes}), none uniquely mode='live'")
        else:
            print("  verdict: no watch_list node -- not a candidate")
        return

    # Real/dry_run wins on a collision (shouldn't happen for one node -- it's
    # either mode='live' with a real position or mode='research' with a
    # paper one, never both -- but real state should never be shadowed).
    real_pos = db.get_open_position_by_wl_id(node['id'])
    pos = real_pos or db.get_open_position_by_wl_id(node['id'], paper=True)
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

    if pos is None:
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
        for s in db.get_staged_test_configs():
            audit_one(s["ticker"], wl_id=s["wl_id"])
    else:
        for ticker in args.tickers:
            audit_one(ticker)


if __name__ == "__main__":
    main()
