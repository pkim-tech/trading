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
    ap.add_argument("--account", required=True, help="account nickname (schwab_client.NICKNAMES)")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--side", required=True, choices=["BUY", "SELL"])
    ap.add_argument("--order-type", required=True, choices=["MARKET", "TRAILING", "STOP"])
    ap.add_argument("--quantity", required=True, type=int)
    ap.add_argument("--trail-pct", type=float, help="required for --order-type TRAILING")
    ap.add_argument("--stop-price", type=float, help="required for --order-type STOP")
    args = ap.parse_args()

    if args.order_type == "TRAILING" and args.trail_pct is None:
        raise SystemExit("--trail-pct is required for --order-type TRAILING")
    if args.order_type == "STOP" and args.stop_price is None:
        raise SystemExit("--stop-price is required for --order-type STOP")

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

    # A bypass-staged TRAILING BUY places the real order but, without this,
    # creates no pending_buys row -- nothing in the normal daemon machinery
    # (update_real_pending_buys_running_low, check_gap_resize, check_auto_fills)
    # has anything to track or reconcile against, so the real fill would sit
    # invisible until manually reconciled by hand. Found live 2026-07-29
    # (RETL) -- the exact gap this closes, so future staged tests don't repeat it.
    if args.order_type == "TRAILING" and args.side == "BUY":
        node = db.get_watch_list_node(ticker=args.ticker, account=args.account, watchlist_id=False)
        if node is None:
            print(f"  [warn] no watch_list node found for {args.ticker}/{args.account} -- "
                  f"pending_buys row NOT created, this order won't be tracked by the daemon "
                  f"(running_low, gap_resize, fill reconciliation) until reconciled manually.")
        else:
            sig = {"current_price": price, "last_bar": datetime.now()}
            db.add_pending_buy(node, sig, channel=cfg.SLACK_CHANNEL_ID, ts="", order_id=order_id)
            db.mark_pending_buy_placed_by_wl_id(node["id"])
            print(f"  pending_buys row created (wl_id={node['id']}) -- will be tracked by "
                  f"update_real_pending_buys_running_low / check_gap_resize / check_auto_fills "
                  f"once the daemon is running.")


if __name__ == "__main__":
    main()
