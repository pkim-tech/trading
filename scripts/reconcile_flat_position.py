"""Reconcile a live/dry_run node whose DB state (open_positions or pending_buys) has
drifted from the real broker -- specifically the two shapes tonight's real DFEN/SOXL/
WEBL/DPST incident showed:

  (a) a real SELL genuinely filled at the broker and closed the position, but the
      daemon never recorded it (open_positions row still open, trade_log row still
      has exit_time=NULL) -- the SELL-side sibling of reconcile_fill_manually.py
      (which handles the BUY-side "position opened at the broker but pending_buys
      row never cleared" case). This drives the SAME code path the daemon's own
      poll loop uses for this (signals_notify._reconcile_auto_close_flat_position,
      called from check_live_state_reconciliation) rather than reimplementing the
      close, for the same reason reconcile_fill_manually.py drives
      _reconcile_buy_fill: any ad hoc reconciliation outside the real function
      leaves some subset of the close (correct exit price, provenance stamp, alert)
      silently undone.

  (b) a pending_buys row whose order was CANCELED/REJECTED at the broker (never
      filled) but the row was never cleared -- no position exists to reconcile,
      just a stale row to remove.

SAFETY: dry-run by default -- prints the full plan and changes nothing unless
--commit is passed. Requires the daemon to be stopped for case (a) (it would race
the poll loop otherwise); case (b) is a single-row delete with no position-state
risk, so the daemon-stopped check only applies to (a). Verifies against the REAL
broker before acting in both cases -- never trusts the local DB as ground truth
(automation_principles.md #1).

Usage:
  .venv/bin/python scripts/reconcile_flat_position.py --wl-id 232              # inspect
  .venv/bin/python scripts/reconcile_flat_position.py --wl-id 232 --commit
  .venv/bin/python scripts/reconcile_flat_position.py --wl-id 197 --commit     # pending-buy case
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# See reconcile_fill_manually.py's identical comment -- SIM_MODE must be forced off
# before schwab_client/signals_notify import, or this script's real broker writes
# (none here directly, but signals_notify._reconcile_auto_close_flat_position's own
# alert/DB writes) would get silently mislabeled as simulated.
os.environ.setdefault('SIM_MODE', '0')

import schwab_client
import signals_db as db


def _to_et_naive_str(broker_ts):
    """Schwab's enteredTime is UTC ISO-8601; every other trade_log exit_time in this
    DB is a naive 'YYYY-MM-DD HH:MM:SS' ET string -- convert so this reconciled row
    matches the column's existing format instead of introducing a mixed-format one.
    Uses astimezone() with no explicit tz (converts to the HOST OS timezone), same
    reliance-on-host-tz-being-America/New_York convention signals_db.py's own
    'follows the host OS tz' comment documents -- no new tz-library dependency."""
    dt = schwab_client._parse_broker_timestamp(broker_ts)
    if dt is None:
        return None
    return dt.astimezone().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _daemon_running():
    try:
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    return any("active_signals.py" in line and "grep" not in line for line in out.splitlines())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wl-id", type=int, required=True, help="watch_list node id to reconcile")
    ap.add_argument("--commit", action="store_true", help="actually apply (default: dry run)")
    ap.add_argument("--allow-daemon-running", action="store_true",
                     help="override the daemon-stopped safety check (case (a) only, not recommended)")
    args = ap.parse_args()

    node = db.get_watch_list_node_by_id(args.wl_id)
    if node is None:
        print(f"[abort] no watch_list node with id={args.wl_id}")
        return 1
    ticker, account = node["ticker"], node.get("account")
    print(f"node: wl_id={args.wl_id} {ticker} account={account} state={node.get('state')} "
          f"strategy={node.get('strategy')}")

    pos = db.get_open_position_by_wl_id(args.wl_id)
    pending = db.get_pending_buy_by_wl_id(args.wl_id)

    if pos is None and pending is None:
        print("[abort] no open_positions row and no pending_buys row for this node. Nothing to reconcile.")
        return 1

    if pos is not None:
        return _reconcile_flat_position(args, node, ticker, account, pos)
    return _reconcile_stale_pending_buy(args, ticker, account, pending)


def _reconcile_flat_position(args, node, ticker, account, pos):
    try:
        real_shares = schwab_client.get_real_position(account, ticker)
    except Exception as e:
        print(f"[abort] could not confirm real share count at the broker: {e}")
        return 1
    print(f"open_positions: id={pos['id']} shares={pos.get('shares')} entry_time={pos.get('entry_time')} "
          f"sl_order_id={pos.get('sl_order_id')}")
    print(f"broker real shares held: {real_shares}")
    if real_shares != 0:
        print(f"[abort] broker still shows {real_shares} real shares -- this is NOT a flat/closed "
              f"position. Investigate before forcing anything (partial fill / oversell / a real "
              f"still-open position the DB happens to also agree with).")
        return 1

    open_leg = db.get_open_addon_leg_by_parent(pos['id'])
    if open_leg is not None:
        print(f"[abort] an open add-on leg (id={open_leg['id']}) exists for this position -- "
              f"core-vs-leg attribution of the flat broker state is ambiguous. Investigate by hand, "
              f"same exclusion signals_notify._reconcile_auto_close_flat_position's own caller applies.")
        return 1

    sl_status = None
    if pos.get('sl_order_id'):
        try:
            sl_status = schwab_client.get_order_status(account, pos['sl_order_id'])
        except Exception as e:
            print(f"[abort] could not fetch sl_order_id={pos['sl_order_id']}'s status: {e}")
            return 1
    print(f"sl_order_id status: {sl_status}")

    replacement_fill = None
    if sl_status == 'REPLACED':
        # Real, now-documented shape (2026-08-24 DFEN/SOXL/WEBL incident, tonight's
        # backlog item on CANCELED/REPLACED orders never being reconciled): the
        # recorded sl_order_id got REPLACED because a SEPARATE real market SELL
        # (placed manually, outside the daemon) closed the position instead. Search
        # this ticker's real recent orders for a FILLED SELL, entered after this
        # position's own entry_time, whose quantity matches -- that's the real
        # closing fill, not the (REPLACED, never-filled) recorded sl_order_id.
        try:
            real_orders = schwab_client.get_real_orders(account, ticker)
        except Exception as e:
            print(f"[abort] could not search real orders for a replacement fill: {e}")
            return 1
        entry_dt = datetime.strptime(str(pos.get('entry_time')), "%Y-%m-%d %H:%M:%S")
        candidates = []
        for o in real_orders:
            if (o.get('instruction') or '').upper() != 'SELL' or o.get('status') != 'FILLED':
                continue
            if o.get('quantity') != pos.get('shares'):
                continue
            entered = _to_et_naive_str(o.get('enteredTime'))
            if entered is None or datetime.strptime(entered, "%Y-%m-%d %H:%M:%S") < entry_dt:
                continue
            candidates.append(o)
        if len(candidates) == 1:
            replacement_fill = candidates[0]
            try:
                confirmed = schwab_client.get_filled_order(
                    account, ticker, 'SELL', order_id=int(replacement_fill['orderId']))
            except Exception as e:
                print(f"[abort] could not confirm the replacement fill's price: {e}")
                return 1
            if confirmed is None:
                print(f"[abort] order {replacement_fill['orderId']} reports FILLED in the order list "
                      f"but get_filled_order could not confirm it -- investigate by hand.")
                return 1
            replacement_fill['price'] = confirmed['price']
            replacement_fill['exit_time_et'] = _to_et_naive_str(replacement_fill['enteredTime'])
            if replacement_fill['exit_time_et'] is None:
                print(f"[abort] could not parse order {replacement_fill['orderId']}'s enteredTime "
                      f"({replacement_fill['enteredTime']!r}) -- investigate by hand.")
                return 1
            print(f"found the real closing fill: order {replacement_fill['orderId']} "
                  f"({replacement_fill['quantity']:g} shares @ ${replacement_fill['price']:.4f}, "
                  f"{replacement_fill['exit_time_et']} ET)")
        elif len(candidates) > 1:
            print(f"[abort] found {len(candidates)} candidate FILLED SELL orders matching quantity "
                  f"after entry_time -- ambiguous which one closed this position. Investigate by hand.")
            return 1

    if not (sl_status in ('FILLED', 'CANCELED') or replacement_fill is not None):
        print(f"[abort] sl_order_id status is not FILLED/CANCELED (got {sl_status!r}) and no "
              f"unambiguous replacement fill was found -- the same narrow safety gate "
              f"active_signals.py's own auto-close uses, extended only for the specific "
              f"REPLACED-by-a-real-fill case above. Investigate by hand rather than forcing this.")
        return 1

    running = _daemon_running()
    if running is not False and not args.allow_daemon_running:
        _state = "is RUNNING" if running else "could not be determined (ps failed)"
        print(f"[abort] active_signals.py {_state} -- stop it first (it would race this close). "
              f"Override with --allow-daemon-running.")
        return 1

    if replacement_fill is not None:
        print(f"\nplan: close this position directly via signals_db.close_position using the "
              f"confirmed real replacement fill (order {replacement_fill['orderId']}, "
              f"${replacement_fill['price']:.4f}) -- _reconcile_auto_close_flat_position's own "
              f"sl_order_id lookup would not find this fill (it's a different order id), so it's "
              f"applied directly here instead of through that function.")
    else:
        print("\nplan: close this position via the daemon's own "
              "_reconcile_auto_close_flat_position (real fill lookup preferred, fresh-quote fallback), "
              "same code path active_signals.py's own poll loop uses.")
    if not args.commit:
        print("\n[dry run] nothing changed. Re-run with --commit to apply.")
        return 0

    if replacement_fill is not None:
        db.close_position(
            pos['id'], exit_signal_price=replacement_fill['price'], exit_price=replacement_fill['price'],
            exit_time=replacement_fill['exit_time_et'], exit_reason='RECONCILED',
        )
        db.set_position_provenance(pos['id'], 'manual')
        import signals_notify
        signals_notify._post_message(
            f"🧑 *{ticker}* ({account}) — position was reconciled MANUALLY (wl_id={pos.get('wl_id')}, "
            f"real closing fill: order {replacement_fill['orderId']} @ ${replacement_fill['price']:.4f}, "
            f"the recorded sl_order_id had been REPLACED by this separate real SELL)."
        )
        print(f"[ok] position id={pos['id']} closed @ ${replacement_fill['price']:.4f} and reconciled.")
        return 0

    import signals_notify
    closed = signals_notify._reconcile_auto_close_flat_position(pos, account, ticker, node)
    if not closed:
        print("[error] _reconcile_auto_close_flat_position declined to close (no price available). "
              "Nothing changed -- investigate manually.")
        return 1
    print(f"[ok] position id={pos['id']} closed and reconciled.")
    return 0


def _reconcile_stale_pending_buy(args, ticker, account, pending):
    order_id = pending.get('order_id')
    print(f"pending_buys: id={pending['id']} order_placed={pending['order_placed']} order_id={order_id}")
    if not order_id:
        print("[abort] no broker order id on the pending_buys row -- nothing to verify against. "
              "Investigate by hand.")
        return 1
    try:
        status = schwab_client.get_order_status(account, order_id)
    except Exception as e:
        print(f"[abort] could not fetch order {order_id}'s status: {e}")
        return 1
    print(f"broker order status: {status}")
    if status not in ('CANCELED', 'REJECTED', 'EXPIRED'):
        print(f"[abort] order status is {status!r}, not a terminal never-filled status -- if it's "
              f"FILLED, run reconcile_fill_manually.py instead (this script only clears a stale row "
              f"for an order that never filled). Investigate before forcing anything.")
        return 1
    try:
        real_shares = schwab_client.get_real_position(account, ticker)
    except Exception as e:
        print(f"[abort] could not confirm real share count at the broker: {e}")
        return 1
    if real_shares != 0:
        print(f"[abort] broker shows {real_shares} real shares held for {ticker} despite this order "
              f"never filling -- some OTHER order opened a real position. Investigate before clearing "
              f"this pending_buys row (clearing it would make that position invisible to the daemon).")
        return 1

    print(f"\nplan: clear pending_buys row id={pending['id']} for wl_id={pending.get('wl_id')} "
          f"({ticker}, {account}) -- order {order_id} confirmed {status}, broker holds 0 real shares.")
    if not args.commit:
        print("\n[dry run] nothing changed. Re-run with --commit to apply.")
        return 0

    db.clear_pending_buy_by_wl_id(pending['wl_id'])
    print(f"[ok] pending_buys row id={pending['id']} cleared.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
