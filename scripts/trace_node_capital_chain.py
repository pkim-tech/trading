"""Traces a ticker's real chronological trade_log history (filtered to
is_dry_run_sim=0, optionally scoped to one account) and checks whether each
trade's entry notional lines up with the PRECEDING trade's exit proceeds --
regardless of which watch_list node (wl_id) either trade belonged to. This
sidesteps needing to parse watch_list's "replaces wl_id=X" label chain
(fragile -- breaks on a manually-archived node with different wording) by
just using trade_log itself as the real ground truth of what actually
happened, in order.

Real motivation (2026-09-13): a one-hop check (scripts/check_node_capital_
tracking.py, promote_candidate.py's sizing-continuity extension) only catches
a silent starting_notional reset at the MOST RECENT swap, and only while the
new node still has zero real trades (the override_once field auto-clears
after the first fill). A ticker that's swapped nodes 3+ times could have lost
real capital at an EARLIER swap that's no longer visible any other way --
this script finds it directly from the real trade sequence instead.

Folds in closed addon_legs profit/loss onto the parent trade's proceeds
(2026-09-13 fix, after a real false-positive: AGQ id=526 flagged a $328.30
"gap" that was actually id=456's addon leg closing at a real -$284.87 loss
between the core exit and the next entry -- signals_helpers._last_sale_
recovery already folds this in when it sizes the real order, this script
just didn't mirror that, so it flagged correct sizing as a gap. Mirrors
_last_sale_recovery's own leg-profit-not-proceeds rule: only (exit_price -
entry_price)*shares compounds in, never the leg's raw proceeds (a leg is
margin-financed, its own capital base is never part of the compounding
base). Does not replicate _last_sale_recovery's orphan-leg branch (a leg
whose own parent trade_log row doesn't qualify) -- out of scope for a
same-ticker chronological trace, which only ever looks at legs whose parent
IS one of the rows already in this trace.

Usage:
    .venv/bin/python scripts/trace_node_capital_chain.py --ticker AGQ
    .venv/bin/python scripts/trace_node_capital_chain.py --ticker DPST --account ira
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import signals_config


def trace(ticker, account=None):
    conn = sqlite3.connect(signals_config.DB_PATH)
    conn.row_factory = sqlite3.Row

    q = ("SELECT id, wl_id, account, entry_time, entry_price, exit_time, exit_price, "
         "shares, exit_reason FROM trade_log WHERE ticker=? AND is_dry_run_sim=0")
    params = [ticker]
    if account:
        q += " AND account=?"
        params.append(account)
    q += " ORDER BY id ASC"
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    conn.close()

    if not rows:
        print(f"{ticker}: no real trade_log rows found" + (f" for account={account}" if account else ""))
        return

    conn = sqlite3.connect(signals_config.DB_PATH)
    conn.row_factory = sqlite3.Row
    leg_q = ("SELECT SUM((exit_price - entry_price) * shares) AS profit, MAX(exit_time) AS recency "
             "FROM addon_legs WHERE parent_trade_log_id=? AND status='closed' "
             "AND exit_price IS NOT NULL AND shares IS NOT NULL AND is_dry_run_sim=0 "
             "AND exit_reason != 'ABANDONED'")

    print(f"=== {ticker}{f' ({account})' if account else ''}: {len(rows)} real trade(s), chronological ===")
    prev_proceeds = None
    prev_exit_time = None
    for r in rows:
        entry_notional = r["entry_price"] * r["shares"] if r["entry_price"] and r["shares"] else None
        exit_proceeds = r["exit_price"] * r["shares"] if r["exit_price"] and r["shares"] else None

        flag = ""
        if prev_proceeds is not None and entry_notional is not None:
            delta = entry_notional - prev_proceeds
            if abs(delta) > max(50, 0.05 * prev_proceeds):
                flag = f"  <-- GAP vs prior exit (${prev_proceeds:,.2f}): delta=${delta:,.2f}"

        print(f"id={r['id']:>5} wl_id={str(r['wl_id']):>5} account={r['account']} "
              f"entry={r['entry_time']} (${entry_notional:,.2f} notional)"
              f"{flag}")
        if r["exit_time"]:
            print(f"        exit={r['exit_time']} reason={r['exit_reason']} "
                  f"(${exit_proceeds:,.2f} proceeds)")
            prev_proceeds = exit_proceeds
            prev_exit_time = r["exit_time"]
            leg_row = conn.execute(leg_q, (r["id"],)).fetchone()
            if leg_row and leg_row["profit"] is not None:
                print(f"        + addon leg profit=${leg_row['profit']:,.2f} "
                      f"(closed {leg_row['recency']}) -> recompounded proceeds="
                      f"${max(prev_proceeds + leg_row['profit'], 0):,.2f}")
                prev_proceeds = max(prev_proceeds + leg_row["profit"], 0)
                if leg_row["recency"] and (prev_exit_time is None or leg_row["recency"] > prev_exit_time):
                    prev_exit_time = leg_row["recency"]
        else:
            print("        (still open)")
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--account", default=None)
    args = ap.parse_args()
    trace(args.ticker, args.account)


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
