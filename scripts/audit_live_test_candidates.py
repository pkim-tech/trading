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
                })
                break
    return orders


def _resting_orders(orders):
    return [o for o in orders if o["status"] in ("WORKING", "AWAITING_STOP_CONDITION", "QUEUED", "ACCEPTED", "PENDING_ACTIVATION")]


def audit_one(ticker):
    print(f"\n=== {ticker} ===")
    pos = db.get_open_position(ticker)
    account = pos.get("account") if pos else None

    if account is None:
        node = db.get_watch_list_node(ticker=ticker)
        account = node.get("account") if node else None

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
    print(f"  real broker shares: {real_shares}")
    for o in resting:
        print(f"  resting order: {o['orderType']} {o['instruction']} qty={o['quantity']} "
              f"status={o['status']} id={o['orderId']}")
    if not resting:
        print("  resting order: none")

    if pos is None:
        print("  DB position: none (flat)")
        node = db.get_watch_list_node(ticker=ticker)
        if node is None:
            print("  verdict: no watch_list node -- not a candidate")
            return
        sig = a.compute_buy_signal(node)
        if sig is None:
            print("  verdict: no signal data available -- can't assess entry candidacy")
            return
        cur, trigger = sig["current_price"], sig["lower_band"]
        pct = (cur - trigger) / trigger * 100
        print(f"  entry trigger: ${trigger:.2f}  current: ${cur:.2f}  ({pct:+.2f}% away)")
        verdict = "ENTRY candidate (flat, real trigger distance shown above)"
        print(f"  verdict: {verdict}")
        return

    db_shares = pos.get("shares")
    mismatch = real_shares is not None and db_shares is not None and abs(real_shares - db_shares) > 1e-6
    print(f"  DB position: {db_shares:g} shares @ ${pos['entry_price']:.4f}  "
          f"(entered {pos['entry_time']})")
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", required=True, nargs="+")
    args = ap.parse_args()
    for ticker in args.tickers:
        audit_one(ticker)


if __name__ == "__main__":
    main()
