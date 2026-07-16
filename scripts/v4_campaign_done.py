"""
Exit 0 if a (ticker, stop_loss, entry_timing) v4 campaign already has at least
Phase2-Island rows in backtest_cache (Phase1 always precedes Phase2, so this
means the run got at least that far -- good enough proxy for "already done"
under --max-phase 2.5, since Phase2.5 only runs conditionally on cliff-safety
and Phase3 never runs at that cap). Exit 1 otherwise.

Usage: python scripts/v4_campaign_done.py TICKER STOP_LOSS ENTRY_TIMING
"""
import sqlite3
import sys

DB = "cache/research/trading_universe.db"


def main():
    ticker, stop_loss, entry_timing = sys.argv[1], float(sys.argv[2]), sys.argv[3]
    conn = sqlite3.connect(DB, timeout=30)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT 1 FROM backtest_cache
        WHERE version = 'v4' AND ticker = ? AND stop_loss = ? AND entry_timing = ?
          AND phase = 'Phase2-Island'
        LIMIT 1
        """,
        (ticker, stop_loss, entry_timing),
    )
    sys.exit(0 if cur.fetchone() else 1)


if __name__ == "__main__":
    main()
