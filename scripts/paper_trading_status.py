"""Print current paper-trading state: pending simulated buys, open paper
positions, and a running P&L summary from closed paper trades.

Usage:
  python scripts/paper_trading_status.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db


def _print_table(rows, cols, empty_msg):
    if not rows:
        print(empty_msg)
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def main():
    db.ensure_tables()

    print("=== Paper pending buys (awaiting simulated bounce-fill) ===")
    _print_table(
        db.get_paper_pending_buys(),
        ["ticker", "signal_price", "signal_time", "running_low", "created_at"],
        "None.",
    )

    print("\n=== Paper open positions ===")
    _print_table(
        db.get_open_positions(paper=True),
        ["ticker", "strategy", "shares", "entry_price", "entry_time", "signal_time"],
        "None.",
    )

    print("\n=== Paper trade log (closed trades) ===")
    with db._conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT ticker, entry_price, exit_price, entry_time, exit_time, pnl_pct, exit_reason "
            "FROM paper_trade_log WHERE exit_time IS NOT NULL ORDER BY exit_time"
        ).fetchall()]
    _print_table(rows, ["ticker", "entry_price", "exit_price", "entry_time", "exit_time", "pnl_pct", "exit_reason"],
                 "None yet.")
    if rows:
        pnls = [r['pnl_pct'] for r in rows if r['pnl_pct'] is not None]
        wins = sum(1 for p in pnls if p > 0)
        print(f"\n{len(pnls)} closed trades  |  win rate {wins}/{len(pnls)}  |  "
              f"mean pnl_pct {sum(pnls)/len(pnls):+.2f}%")


if __name__ == "__main__":
    main()
