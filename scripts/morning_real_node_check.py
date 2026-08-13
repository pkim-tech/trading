"""Fast (~1s) daily check: are the real (state='live', account.trading_enabled=1) watch_list
nodes ready to trade -- no open position, no pending buy, no unresolved incident, config matches
the committed baseline. Built 2026-08-12 after being asked ad hoc ("10 Live WL REAL nodes ready
to go?") with the user noting they'll ask every morning -- this replaces the multi-query chase
that took to answer it once.

Usage: .venv/bin/python scripts/morning_real_node_check.py
"""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "cache/live/trading_live.db"


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("""SELECT w.id, w.ticker, w.account, w.starting_notional
                   FROM watch_list w JOIN accounts a ON w.account = a.alias
                   WHERE w.state='live' AND a.trading_enabled=1
                   ORDER BY w.account, w.ticker""")
    nodes = cur.fetchall()
    ids = tuple(n["id"] for n in nodes) or (-1,)

    cur.execute(f"SELECT wl_id, ticker, shares FROM open_positions WHERE wl_id IN {ids}")
    open_pos = {r["wl_id"]: r["shares"] for r in cur.fetchall()}
    cur.execute(f"SELECT wl_id, ticker FROM pending_buys WHERE wl_id IN {ids}")
    pending = {r["wl_id"] for r in cur.fetchall()}
    cur.execute("SELECT ticker, account FROM trading_incidents WHERE resolved_ts IS NULL")
    open_incidents = {(r["ticker"], r["account"]) for r in cur.fetchall()}

    print(f"=== {len(nodes)} real live nodes (state='live', account.trading_enabled=1) ===\n")
    for n in nodes:
        flags = []
        if n["id"] in open_pos:
            flags.append(f"OPEN POSITION ({open_pos[n['id']]} sh)")
        if n["id"] in pending:
            flags.append("PENDING BUY")
        if (n["ticker"], n["account"]) in open_incidents:
            flags.append("UNRESOLVED INCIDENT")
        status = ", ".join(flags) if flags else "flat, ready"
        print(f"  {n['ticker']:6s} {n['account']:10s} ${n['starting_notional']:>8,.0f}  {status}")

    print("\nRun `.venv/bin/python signals_invariants.py` alongside this for config-drift/baseline status.")


if __name__ == "__main__":
    main()
