"""Cleans up local open_positions rows left behind after a position was sold
manually at the broker (out-of-band, not via the automated exit flow) -- the
daemon has no order_id to auto-detect that fill (check_own_sell_fills skips
whenever exit_pending['order_id'] is None, see signals_notify.py), so the row
just sits there: exit_pending rows re-fire the SL/TP/TRAIL Slack reminder
every poll cycle, and non-exit_pending rows just show up as STALE in
check_untracked_positions.py's reconciliation sweep forever.

Local-DB-only -- never touches the broker (the whole point is the broker
side is already closed), so this is safe to run any time, market hours or
not.

Dry-run by default: lists every real (is_dry_run_sim=0) open_positions row
that check_untracked_positions.py's own STALE check would flag (broker holds
0 for that ticker/account), plus any recent FILLED SELL orders on the broker
for that ticker/account (last --lookback-days, default 3) as candidate real
fill prices -- shown for you to review/pick, never auto-selected (same
stale-match hazard get_filled_order's own docstring warns about for
order_id=None lookups).

Usage:
  .venv/bin/python scripts/close_stale_manual_exits.py                # list only
  .venv/bin/python scripts/close_stale_manual_exits.py --apply --id 339 --exit-price 70.49 --exit-reason SL
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import schwab_client
import schwab_safety
import signals_db as db


def find_stale_positions():
    """Real open_positions rows whose ticker the broker no longer holds at
    all in that same account -- mirrors check_untracked_positions.py's own
    STALE finding, scoped down to just the row data this script needs."""
    stale = []
    for account in schwab_safety.ACCOUNTS:
        try:
            hashes = schwab_client._resolve_account_hashes()
        except Exception as e:
            print(f"  [error] {account}: couldn't resolve account hashes: {e}")
            continue
        if account not in hashes:
            continue
        try:
            real_positions = schwab_client.get_all_real_positions(account)
        except Exception as e:
            print(f"  [error] {account}: couldn't fetch real positions: {e}")
            continue
        with db._conn() as c:
            rows = c.execute(
                "SELECT id, ticker, shares, account, wl_id, trail_state, entry_price "
                "FROM open_positions WHERE account=? AND is_dry_run_sim=0",
                (account,),
            ).fetchall()
        for r in rows:
            if r["ticker"] not in real_positions:
                stale.append(dict(r))
    return stale


def recent_broker_sells(account, ticker, lookback_days):
    """Recent FILLED SELL orders for ticker/account, most recent first --
    candidates only, never auto-applied."""
    account_hash = schwab_client._resolve_account_hashes()[account]
    r = schwab_client._get_client().get_orders_for_account(account_hash)
    r.raise_for_status()
    orders = r.json()
    cutoff = datetime.now() - timedelta(days=lookback_days)
    candidates = []
    for o in orders:
        fill = schwab_client._order_fill(o)
        if fill is None:
            continue
        legs = o.get("orderLegCollection", [])
        matches = any(
            leg.get("instrument", {}).get("symbol") == ticker and leg.get("instruction") == "SELL"
            for leg in legs
        )
        if not matches:
            continue
        ts = o.get("closeTime") or o.get("enteredTime") or ""
        ts_normalized = ts.replace("Z", "+00:00")
        if len(ts_normalized) >= 5 and ts_normalized[-5] in "+-" and ts_normalized[-3] != ":":
            ts_normalized = ts_normalized[:-2] + ":" + ts_normalized[-2:]
        try:
            when = datetime.fromisoformat(ts_normalized).replace(tzinfo=None)
        except ValueError:
            when = None
        if when is not None and when < cutoff:
            continue
        candidates.append({"time": ts, "price": fill["price"], "quantity": fill["quantity"]})
    candidates.sort(key=lambda x: x["time"], reverse=True)
    return candidates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback-days", type=int, default=3)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--id", type=int, help="open_positions.id to close (with --apply)")
    ap.add_argument("--exit-price", type=float)
    ap.add_argument("--exit-reason", default="manual")
    args = ap.parse_args()

    if args.apply:
        if args.id is None or args.exit_price is None:
            print("--apply requires --id and --exit-price")
            sys.exit(1)
        with db._conn() as c:
            row = c.execute("SELECT ticker, account, wl_id FROM open_positions WHERE id=?", (args.id,)).fetchone()
        if row is None:
            print(f"id {args.id}: no matching open_positions row (already closed?)")
            sys.exit(1)
        closed = db.close_position(
            args.id, exit_signal_price=args.exit_price, exit_price=args.exit_price,
            exit_time=datetime.now(), exit_reason=args.exit_reason,
        )
        if closed:
            print(f"closed id={args.id} {row['ticker']} ({row['account']}) at ${args.exit_price:.4f}, reason={args.exit_reason}")
        else:
            print(f"id {args.id}: already closed (race with the daemon?)")
        return

    stale = find_stale_positions()
    if not stale:
        print("No stale positions found -- broker matches local for every real open_positions row.")
        return

    for pos in stale:
        print(f"\nid={pos['id']} {pos['ticker']} ({pos['account']}) wl_id={pos['wl_id']} "
              f"local_shares={pos['shares']:g} entry_price={pos['entry_price']}")
        raw_trail_state = pos.get("trail_state")
        trail_state = json.loads(raw_trail_state) if raw_trail_state else {}
        exit_pending = trail_state.get("exit_pending")
        if exit_pending:
            print(f"  exit_pending: reason={exit_pending.get('reason')} order_id={exit_pending.get('order_id')} "
                  f"reminder_count={exit_pending.get('reminder_count')}")
        candidates = recent_broker_sells(pos["account"], pos["ticker"], args.lookback_days)
        if not candidates:
            print(f"  no FILLED SELL orders found for {pos['ticker']} in {pos['account']} "
                  f"in the last {args.lookback_days} day(s) -- widen --lookback-days or check manually")
            continue
        for cand in candidates:
            print(f"  candidate fill: {cand['time']}  price=${cand['price']:.4f}  qty={cand['quantity']:g}")
        print(f"  to apply: .venv/bin/python scripts/close_stale_manual_exits.py --apply --id {pos['id']} "
              f"--exit-price <price> --exit-reason manual")


if __name__ == "__main__":
    main()
