"""Manually reconcile a real broker fill that the daemon never recorded.

This is the tool that should have existed on 2026-08-14. A SOXS trailing-buy
(ira, wl_id=206) filled at 09:30:05 ET and sat unreconciled for hours; the
recovery was done by hand-assembling raw signals_db calls in an agent session,
which is how the position ended up with a stale pending_buys row, no top-up,
and a manually-placed stop nothing had any record of.

WHY A SCRIPT AND NOT "just call open_position": signals_notify._reconcile_buy_fill
is the ONLY path that does the whole job -- clear the matching pending_buys row,
open the position, top up to target notional, place the protective stop. Every
ad-hoc reconciliation outside it leaves some subset undone, silently. This
script drives that same real code path rather than reimplementing it, so it
cannot drift from what the daemon does.

WHAT IT ADDS on top of that path: the position is stamped provenance='manual'
(open_positions.provenance, 2026-08-15) and a distinct "human-reconciled,
verify" alert is posted. That matters downstream -- signals_notify.
_verify_resting_before_replace reads provenance, so when the daemon later
replaces the protective order on a hand-reconciled position it says so out
loud instead of silently overwriting a human's deliberate order, which is
exactly what went unnoticed during the incident.

SAFETY: dry-run by default -- prints the full plan and changes nothing unless
--commit is passed. Requires the daemon to be stopped (it would race the poll
loop otherwise). Verifies the fill against the REAL broker before acting
(automation_principles.md #1: never trust a local record as ground truth) --
it will not reconcile a fill Schwab does not confirm.

Usage:
  .venv/bin/python scripts/reconcile_fill_manually.py --wl-id 206              # inspect
  .venv/bin/python scripts/reconcile_fill_manually.py --wl-id 206 --commit
  .venv/bin/python scripts/reconcile_fill_manually.py --wl-id 206 --order-id 1007558792263 --commit
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# MUST run before any import that pulls in signals_config (schwab_client does,
# transitively). signals_config.SIM_MODE defaults ON (the 2026-08-01 fail-safe
# flip, so an ad hoc script can't accidentally post to the real channel). This
# script is the rare inverse case: it places genuinely REAL broker orders, and
# SIM_MODE gates nothing in schwab_client/schwab_safety -- so without this the
# top-up BUY and the protective stop would go to Schwab for real while every
# resulting Slack message got a "🧪 SIM MODE" prefix, rendered without its
# action buttons, and was written to slack_message_log as mode='sim'. The
# permanent record of a real-money incident recovery would read as simulated,
# at exactly the moment that mislabel is most dangerous. Mirrors
# active_signals.py's own entrypoint (setdefault, so an operator who really
# does want a silent dry run can still export SIM_MODE=1 themselves).
os.environ.setdefault('SIM_MODE', '0')

import schwab_client
import signals_db as db


def _daemon_running():
    """The poll loop would race every write this script makes (and could
    reconcile the same fill concurrently). Checked, not assumed."""
    try:
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None  # unknown -- caller decides
    return any("active_signals.py" in line and "grep" not in line for line in out.splitlines())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wl-id", type=int, required=True, help="watch_list node id to reconcile")
    ap.add_argument("--order-id", help="broker order id; defaults to the pending_buys row's own")
    ap.add_argument("--commit", action="store_true", help="actually apply (default: dry run)")
    ap.add_argument("--allow-daemon-running", action="store_true",
                     help="override the daemon-stopped safety check (not recommended)")
    args = ap.parse_args()

    node = db.get_watch_list_node_by_id(args.wl_id)
    if node is None:
        print(f"[abort] no watch_list node with id={args.wl_id}")
        return 1
    ticker, account = node["ticker"], node.get("account")
    print(f"node: wl_id={args.wl_id} {ticker} account={account} state={node.get('state')} "
          f"strategy={node.get('strategy')}")

    existing = db.get_open_position_by_wl_id(args.wl_id)
    if existing is not None:
        print(f"[abort] a position is ALREADY open for this node (id={existing['id']}, "
              f"{existing.get('shares')} shares @ ${existing.get('entry_price')}). "
              f"Nothing to reconcile -- investigate before forcing anything.")
        return 1

    pending = db.get_pending_buy_by_wl_id(args.wl_id)
    if pending is None:
        print(f"[abort] no pending_buys row for wl_id={args.wl_id}. _reconcile_buy_fill keys off that "
              f"row; without it this script has nothing to reconcile against. If the broker really "
              f"holds an untracked position, run scripts/check_untracked_positions.py first.")
        return 1
    # Cross-check an operator-supplied id against the one we captured. There is
    # no legitimate reason to override a captured broker id -- if it's wrong,
    # the pending row is what needs fixing first.
    if args.order_id and pending.get("order_id") and str(args.order_id) != str(pending["order_id"]):
        print(f"[abort] --order-id {args.order_id} differs from the pending_buys row's own order_id "
              f"{pending['order_id']}. Reconciling wl_id={args.wl_id} against a DIFFERENT order would "
              f"open this node's position at another order's price/quantity and size a REAL top-up BUY "
              f"off it. Fix the pending row if its id is wrong.")
        return 1
    order_id = args.order_id or pending.get("order_id")
    print(f"pending_buys: id={pending['id']} order_placed={pending['order_placed']} order_id={order_id}")
    if not order_id:
        print("[abort] no broker order id available (the manual 'Trailing Buy Order Placed' flow does "
              "not capture one). Pass --order-id explicitly after confirming it at the broker -- this "
              "script will not fall back to get_filled_order's order_id=None mode, which is documented "
              "as unsafe (it matched a days-old unrelated fill and corrupted a real GDXU reconciliation, "
              "2026-07-27).")
        return 1

    # Ground truth: confirm the fill at the broker before touching anything.
    try:
        fill = schwab_client.get_filled_order(account, ticker, "BUY", order_id=int(order_id))
    except Exception as e:
        print(f"[abort] could not confirm the fill at the broker: {e}")
        return 1
    if fill is None:
        print(f"[abort] broker does NOT report order {order_id} as FILLED. Nothing to reconcile.")
        return 1

    # Confirm the order we just looked up is actually THIS ticker's BUY.
    # schwab_client.get_filled_order's order_id branch matches on id ALONE --
    # it consults ticker/side only in its order_id=None heuristic path -- so an
    # id belonging to another symbol, or to a SELL, returns that order's fill
    # and everything downstream (position entry price, and a REAL top-up BUY
    # sized off it) would be built on it. This is the load-bearing check for
    # the manual-placement case, where there is no captured id to cross-check
    # against.
    try:
        real_orders = schwab_client.get_real_orders(account, ticker)
    except Exception as e:
        print(f"[abort] could not verify the order's symbol/side at the broker: {e}")
        return 1
    match = next((o for o in real_orders if str(o.get("orderId")) == str(order_id)), None)
    if match is None:
        print(f"[abort] order {order_id} reported a fill but is not among {ticker}'s orders at the "
              f"broker -- it belongs to a different symbol. Refusing to reconcile.")
        return 1
    if (match.get("instruction") or "").upper() != "BUY":
        print(f"[abort] order {order_id} is a {match.get('instruction')!r}, not a BUY. Refusing to "
              f"reconcile an entry against it.")
        return 1
    print(f"broker confirms FILLED: {fill['quantity']:g} shares @ ${fill['price']:.4f} "
          f"({ticker} BUY, order {order_id})")

    running = _daemon_running()
    # Fails CLOSED on unknown: a safety check whose only job is preventing a
    # concurrent double-reconcile must not treat "couldn't ask" as "all clear"
    # (the same reasoning check_untracked_positions applies to a broker fetch
    # failure). The override already exists for the operator who knows better.
    if running is not False and not args.allow_daemon_running:
        _state = "is RUNNING" if running else "could not be determined (ps failed)"
        print(f"[abort] active_signals.py {_state} -- stop it first (it would race these writes and "
              f"could reconcile the same fill concurrently). Override with --allow-daemon-running.")
        return 1

    print("\nplan:")
    print(f"  1. clear pending_buys row for wl_id={args.wl_id}")
    print(f"  2. open position: {fill['quantity']:g} shares @ ${fill['price']:.4f}")
    print(f"  3. top up toward target notional if under (real broker BUY)")
    print(f"  4. place the protective stop-loss if {ticker} is automation-scoped")
    print(f"  5. stamp provenance='manual' and post a human-reconciled alert")

    if not args.commit:
        print("\n[dry run] nothing changed. Re-run with --commit to apply.")
        return 0

    # Imported here, not at module scope: signals_notify constructs a Slack Bolt
    # app on import, which we only want to pay for on a real run.
    import signals_notify

    # try/finally, not a bare sequence: _reconcile_buy_fill does four things
    # (clear pending -> open position -> top up -> place SL) and a raise after
    # step 2 would otherwise leave a REAL open position stamped
    # provenance='daemon' with no manual alert -- silently disarming, for that
    # position's whole life, the very bug-#5 announcement this tranche adds.
    # The finally block also shouts about the partial state itself, since a
    # half-done reconcile leaves the top-up and/or protective stop unplaced,
    # which try/finally alone would not surface.
    reconcile_error = None
    try:
        signals_notify._reconcile_buy_fill(ticker, fill["price"], fill["quantity"],
                                            wl_id=args.wl_id, account=account)
    except Exception as e:
        reconcile_error = e
    finally:
        pos = db.get_open_position_by_wl_id(args.wl_id)
        if pos is not None:
            db.set_position_provenance(pos["id"], "manual")

    if reconcile_error is not None:
        print(f"[error] _reconcile_buy_fill raised: {reconcile_error}")
        if pos is not None:
            signals_notify._post_message(
                f"🚨 *{ticker}* ({account}) — MANUAL reconciliation raised part-way through "
                f"(`{reconcile_error}`)\nThe position IS open (id={pos['id']}) and stamped "
                f"provenance='manual', but the top-up and/or protective stop may NOT have been "
                f"placed. Verify both at the broker by hand now."
            )
        return 1

    if pos is None:
        print("[error] _reconcile_buy_fill did not open a position -- check the alerts it posted. "
              "Nothing was stamped provenance='manual'.")
        return 1

    print(f"[ok] position id={pos['id']} reconciled and stamped provenance='manual'")
    signals_notify._post_message(
        f"🧑 *{ticker}* ({account}) — position was reconciled MANUALLY (wl_id={args.wl_id}, "
        f"{fill['quantity']:g} shares @ ${fill['price']:.4f}, order {order_id})\n"
        f"(verify the protective stop is correct — the daemon will announce distinctly if it later "
        f"replaces this position's order)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
