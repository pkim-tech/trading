"""
Repeatable tool for staging a deliberate real-order live test outside a normal
signal window (entry/gap-resize, TIME-exit, TRAIL-exit -- see docs/
live_test_coverage.md's "Runbook: staging a real-order live test scenario").
Bypasses schwab_client.py/schwab_safety.py entirely (same reason as
scripts/live_sanity_check.py: a deliberately-staged test isn't a real signal,
so schwab_safety.check_order's window gate would reject it) -- calls the
schwab-py client directly. Because that also skips every other guard, this
prints the real cash/duplicate-order/kill-switch/daily-cap state before
asking for confirmation, so the operator sees by hand what those guards would
normally check automatically.

Usage:
  .venv/bin/python scripts/stage_live_test_order.py --account soxl_ira --ticker SPY \\
      --side BUY --order-type TRAILING --quantity 2 --trail-pct 1.0
  .venv/bin/python scripts/stage_live_test_order.py --account soxl_ira --ticker SH \\
      --side SELL --order-type STOP --quantity 50 --stop-price 26.57
  .venv/bin/python scripts/stage_live_test_order.py --account soxl_ira --ticker GDXU \\
      --side SELL --order-type MARKET --quantity 2

Logs to signals_db.coverage_events (scenario_key="staged_live_test", mode="live") so it shows up
in scripts/coverage_matrix.py alongside everything else.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import schwab.orders.equities as equity_orders
from schwab.orders.generic import OrderBuilder
from schwab.orders.common import (
    OrderType, Session, Duration, OrderStrategyType,
    StopPriceLinkBasis, StopPriceLinkType, EquityInstruction,
)
from schwab.utils import Utils

from datetime import datetime

import schwab_auth
import schwab_safety
import signals_config as cfg
import signals_db as db
from schwab_client import get_current_price, _resolve_account_hashes


def _build_order(order_type, side, ticker, quantity, trail_pct=None, stop_price=None):
    instruction = EquityInstruction.BUY if side == "BUY" else EquityInstruction.SELL
    if order_type == "MARKET":
        order_fn = equity_orders.equity_buy_market if side == "BUY" else equity_orders.equity_sell_market
        return order_fn(ticker, quantity)
    order = OrderBuilder()
    order.set_session(Session.NORMAL)
    order.set_duration(Duration.GOOD_TILL_CANCEL)
    order.set_order_strategy_type(OrderStrategyType.SINGLE)
    if order_type == "TRAILING":
        link_basis = StopPriceLinkBasis.ASK if side == "BUY" else StopPriceLinkBasis.BID
        order.set_order_type(OrderType.TRAILING_STOP)
        order.set_stop_price_link_basis(link_basis)
        order.set_stop_price_link_type(StopPriceLinkType.PERCENT)
        order.set_stop_price_offset(trail_pct)
    elif order_type == "STOP":
        order.set_order_type(OrderType.STOP)
        order.set_stop_price(f"{stop_price:.2f}")
    else:
        raise SystemExit(f"unknown order type '{order_type}'")
    order.add_equity_leg(instruction, ticker, quantity)
    return order


def _print_manual_checks(account, ticker, side, quantity, price):
    """Prints what schwab_safety.check_order would normally check
    automatically -- this bypass skips all of it, so the operator has to
    look at these by hand before confirming."""
    import json
    cash = None
    try:
        from schwab_client import get_account_balance
        cash = get_account_balance(account)
    except Exception as e:
        print(f"  [warn] couldn't fetch real cash balance: {e}")
    notional = quantity * price
    print(f"  real cash available ({account}): {'$' + format(cash, ',.2f') if cash is not None else 'UNKNOWN'}")
    print(f"  order notional: ~${notional:,.2f}")
    if cash is not None and side == "BUY" and notional > cash:
        print(f"  \U000026A0️  notional exceeds real cash available -- Schwab will likely reject this")

    state = {}
    if schwab_safety.STATE_PATH.exists():
        try:
            state = json.loads(schwab_safety.STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    today_count = state.get(today, {}).get(account, 0)
    print(f"  orders placed today for {account} (per schwab_safety's own counter): {today_count}")
    for o in state.get("recent_orders", []):
        if o.get("account") == account and o.get("ticker") == ticker and o.get("side") == side:
            print(f"  \U000026A0️  a recent order already exists for {account}/{ticker}/{side} -- "
                  f"confirm this isn't an accidental duplicate: {o}")

    print(f"  kill switch engaged: {schwab_safety.kill_switch_engaged()}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", required=True, help="account nickname (see the accounts table / schwab_safety.ACCOUNTS)")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--side", required=True, choices=["BUY", "SELL"])
    ap.add_argument("--order-type", required=True, choices=["MARKET", "TRAILING", "STOP"])
    ap.add_argument("--quantity", required=True, type=int)
    ap.add_argument("--trail-pct", type=float, help="required for --order-type TRAILING")
    ap.add_argument("--stop-price", type=float, help="required for --order-type STOP")
    ap.add_argument("--wl-id", type=int, help="explicit watch_list node id, disambiguates when "
                     "--ticker/--account resolve to 0 or 2+ nodes (required for a TRAILING BUY "
                     "in that case -- see the fail-closed check below)")
    args = ap.parse_args()

    if args.order_type == "TRAILING" and args.trail_pct is None:
        raise SystemExit("--trail-pct is required for --order-type TRAILING")
    if args.order_type == "STOP" and args.stop_price is None:
        raise SystemExit("--stop-price is required for --order-type STOP")

    # Fail CLOSED, not open, for any real BUY: every BUY order type here
    # (TRAILING or MARKET) has the same downstream tracking dependency
    # (pending_buys -> running_low/gap_resize/fill-reconciliation ->
    # post_fill_topup -> real SL placement), all of which need a resolved,
    # real node BEFORE the order is placed. Previously this only printed a
    # warning and placed the real order anyway -- found live 2026-08-06: a
    # GDXU TRAILING BUY whose node lookup returned None (ambiguous/no match)
    # still placed a real fill that then sat completely untracked and
    # unprotected for a week, invisible to every downstream check, because
    # there was never a pending_buys row for anything to reconcile against.
    # Originally scoped to TRAILING+BUY only -- widened to any BUY after a
    # paired Opus review pointed out a staged MARKET BUY (the still-open
    # market_buy_placement go-live checklist item, DPST's real entry
    # mechanism) had zero coverage and would reproduce the identical
    # exposure, just with a faster fill.
    node = None
    if args.side == "BUY":
        if args.wl_id is not None:
            node = db.get_watch_list_node_by_id(args.wl_id)
            if node is None:
                raise SystemExit(f"--wl-id {args.wl_id} does not exist -- aborting, not placing a real order")
            # A --wl-id that doesn't actually match --ticker/--account would
            # create a pending_buys row for a DIFFERENT ticker/account than
            # the real order just placed -- the real fill stays untracked
            # (the original exposure, unchanged) AND check_gap_resize could
            # later replace/place a real order against the wrong ticker's
            # resting order_id. Found by paired Opus review before this
            # shipped -- --wl-id was accepted with existence-only checking.
            if node['ticker'] != args.ticker.upper() or node.get('account') != args.account:
                raise SystemExit(
                    f"--wl-id {args.wl_id} resolves to {node['ticker']}/{node.get('account')}, "
                    f"not {args.ticker}/{args.account} -- refusing, this would create a pending_buys "
                    f"row for the wrong ticker/account and could drive a real order against it later."
                )
        else:
            node = db.get_watch_list_node(ticker=args.ticker, account=args.account, watchlist_id=False)
            if node is None:
                with db._conn() as c:
                    candidates = c.execute(
                        "SELECT id, watchlist_id, state, version FROM watch_list WHERE ticker=? AND account=? "
                        "AND paper_role IS NULL",
                        (args.ticker, args.account)
                    ).fetchall()
                raise SystemExit(
                    f"REFUSING to place a real {args.order_type} BUY for {args.ticker}/{args.account}: "
                    f"get_watch_list_node found {'no match' if not candidates else f'{len(candidates)} ambiguous matches'} "
                    f"({[dict(r) for r in candidates]}), so no pending_buys row could be created and this fill "
                    f"would go completely untracked. Pass --wl-id to disambiguate, or fix the watch_list rows first."
                )
        # A resolved node whose state isn't 'live' looks safe (a check exists)
        # but isn't: the real order still places for real at the broker, then
        # the daemon's own update_dry_run_buys SYNTHESIZES a fill for a
        # dry_run/paper node (effectively_dry_run) and clears the pending_buys
        # row itself -- re-orphaning the real fill a different way, now with a
        # synthetic position masking the alert that would otherwise catch it.
        # Found by paired Opus review's rebuttal round: reachable today via
        # --wl-id against any of the 43 real dry_run nodes regardless of
        # --account (the auto-resolve branch can't hit it while soxl_ira has
        # zero non-live nodes, but --wl-id has no account check at all).
        if node['state'] != 'live':
            raise SystemExit(
                f"REFUSING: wl_id={node['id']} ({node['ticker']}/{node.get('account')}) has "
                f"state={node['state']!r}, not 'live' -- the real order would place, but the daemon's "
                f"dry-run fill synthesis would then re-orphan it with a fake position masking the "
                f"real one. Flip the node to state='live' first if this test genuinely needs a real order."
            )

    price = get_current_price(args.ticker)
    account_hash = _resolve_account_hashes()[args.account]
    client = schwab_auth.get_client()

    print(f"\n=== staging {args.side} {args.order_type} {args.quantity} {args.ticker} in {args.account} ===")
    print(f"  current price: ${price:.4f}")
    _print_manual_checks(args.account, args.ticker, args.side, args.quantity, price)

    detail = f"trail_pct={args.trail_pct}" if args.order_type == "TRAILING" else \
             f"stop_price={args.stop_price}" if args.order_type == "STOP" else "market"
    print(f"\n  About to submit: {args.side} {args.order_type} {args.quantity} {args.ticker} ({detail}) "
          f"in {args.account} -- bypasses every schwab_safety guard.")
    resp = input(f"  Type the ticker again to confirm, anything else to abort: ").strip()
    if resp.upper() != args.ticker.upper():
        print("  aborted")
        db.log_coverage_event("staged_live_test", "live", ticker=args.ticker, result="aborted_by_operator")
        return

    order = _build_order(args.order_type, args.side, args.ticker, args.quantity, args.trail_pct, args.stop_price)
    r = client.place_order(account_hash, order)
    r.raise_for_status()
    order_id = Utils(client, account_hash).extract_order_id(r)
    print(f"  submitted -- order_id={order_id}")
    db.log_coverage_event("staged_live_test", "live", ticker=args.ticker, result="placed",
                           detail=f"side={args.side} type={args.order_type} qty={args.quantity} order_id={order_id} {detail}")

    # A bypass-staged BUY (TRAILING or MARKET) places the real order but,
    # without this, creates no pending_buys row -- nothing in the normal
    # daemon machinery (update_real_pending_buys_running_low, check_gap_resize,
    # check_auto_fills) has anything to track or reconcile against, so the
    # real fill would sit invisible until manually reconciled by hand. Found
    # live 2026-07-29 (RETL) -- the exact gap this closes, so future staged
    # tests don't repeat it. `node` was already resolved (or the script
    # aborted) above -- the fail-closed check guarantees it's never None here.
    if args.side == "BUY":
        sig = {"current_price": price, "last_bar": datetime.now()}
        db.add_pending_buy(node, sig, channel=cfg.SLACK_CHANNEL_ID, ts="", order_id=order_id)
        db.mark_pending_buy_placed_by_wl_id(node["id"])
        print(f"  pending_buys row created (wl_id={node['id']}) -- will be tracked by "
              f"update_real_pending_buys_running_low / check_gap_resize / check_auto_fills "
              f"once the daemon is running.")


if __name__ == "__main__":
    main()
