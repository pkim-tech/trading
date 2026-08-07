"""Prunes backtest_cache to island-only retention, uniformly across every
ticker/version (decided 2026-08-01, see docs/backlog_cache.md's "Decided, not
yet executed" entry) -- a stored backtest result inherits the trust level of
the code that produced it; islands (winning node's neighborhood) are the same
granularity the project's own cliff-safety/robustness checking already uses,
so there's no separate "full grid" tier for any ticker, watchlist or not.

Builds a fresh, small DB and swaps files rather than DELETE+VACUUM in place --
avoids both a slow NOT-IN scan across 167M rows and a slow VACUUM rewrite of a
65GB file. The original stays untouched until the final swap step.

Backup already exists (cron: cache/research/trading_universe_daily.db.bak,
refreshed daily 2am) -- confirmed same day before running this.

Usage:
  .venv/bin/python scripts/prune_backtest_cache.py --dry-run    (report only)
  .venv/bin/python scripts/prune_backtest_cache.py --build      (writes trading_universe_pruned.db, does not touch the original)
  .venv/bin/python scripts/prune_backtest_cache.py --swap       (moves original aside, renames pruned into place -- run only after inspecting --build's output)
"""
import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = Path("cache/research/trading_universe.db")
PRUNED_PATH = Path("cache/research/trading_universe_pruned.db")

ROBUST_ALPHA_SQL = ("MIN(alpha_vs_spy, COALESCE(alpha_vs_spy_pessimistic, alpha_vs_spy), "
                     "COALESCE(alpha_vs_spy_certain, alpha_vs_spy))")


def _groups(conn, ticker):
    c = conn.cursor()
    c.execute("""SELECT DISTINCT strategy, version, window, z_score_threshold
                 FROM backtest_cache WHERE ticker=?""", (ticker,))
    return c.fetchall()


# take_profit is NULL for TrailingBothZScoreBreakout rows -- that strategy stores its
# arm value in arm_sell_pct instead (same root cause documented in top_safe_nodes.py).
# COALESCE pulls whichever real column is actually populated for this row's strategy,
# so a plain `take_profit IN (?)` filter never silently matches nothing on NULL.
TP_COL_SQL = "COALESCE(take_profit, arm_sell_pct)"


def _island_rowids_for_group(conn, ticker, strategy, version, window, z_score_threshold):
    """Winning row = best robust_alpha in this group. Island = winning row's
    own axis values +/- the 2 nearest distinct values that exist in this group
    on take_profit and stop_loss (handles varying step sizes per strategy/
    version naturally, no hardcoded step size), and any max_hold_hours within
    +/-24h of the winner's -- roughly the project's existing "Island view"
    size (~50 nodes: 5 TP x 5 SL x 2 hold)."""
    c = conn.cursor()
    c.execute(f"""
        SELECT {TP_COL_SQL}, stop_loss, max_hold_hours, {ROBUST_ALPHA_SQL} AS robust_alpha
        FROM backtest_cache
        WHERE ticker=? AND strategy=? AND version=? AND window=? AND z_score_threshold=? AND trades > 0
        ORDER BY robust_alpha DESC LIMIT 1
    """, (ticker, strategy, version, window, z_score_threshold))
    winner = c.fetchone()
    if winner is None:
        return []
    w_tp, w_sl, w_hold, _ = winner

    def nearest_values(col_sql, center):
        if center is None:
            return {None}
        c.execute(f"""
            SELECT DISTINCT {col_sql} FROM backtest_cache
            WHERE ticker=? AND strategy=? AND version=? AND window=? AND z_score_threshold=?
        """, (ticker, strategy, version, window, z_score_threshold))
        vals = sorted(set(r[0] for r in c.fetchall() if r[0] is not None))
        if center not in vals:
            return {center}
        idx = vals.index(center)
        return set(vals[max(0, idx - 2):min(len(vals), idx + 3)])

    tp_keep = list(nearest_values(TP_COL_SQL, w_tp))
    sl_keep = list(nearest_values('stop_loss', w_sl))
    tp_ph = ','.join('?' * len(tp_keep))
    sl_ph = ','.join('?' * len(sl_keep))
    c.execute(f"""SELECT rowid FROM backtest_cache
                  WHERE ticker=? AND strategy=? AND version=? AND window=? AND z_score_threshold=?
                  AND {TP_COL_SQL} IN ({tp_ph}) AND stop_loss IN ({sl_ph})
                  AND ABS(max_hold_hours - ?) <= 24""",
              [ticker, strategy, version, window, z_score_threshold] + tp_keep + sl_keep + [w_hold])
    return [r[0] for r in c.fetchall()]


def _compute_keep_rowids(conn):
    c = conn.cursor()
    c.execute("SELECT DISTINCT ticker FROM backtest_cache")
    all_tickers = [r[0] for r in c.fetchall()]
    keep = set()
    for ticker in all_tickers:
        for strategy, version, window, z in _groups(conn, ticker):
            keep.update(_island_rowids_for_group(conn, ticker, strategy, version, window, z))
    return keep


def cmd_dry_run():
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM backtest_cache")
    total_before = c.fetchone()[0]
    keep = _compute_keep_rowids(conn)
    print(f"Total rows before: {total_before:,}")
    print(f"Rows to keep (islands, all tickers/versions): {len(keep):,} ({len(keep)/total_before*100:.4f}%)")
    print(f"Rows to drop: {total_before - len(keep):,}")
    print("\n--dry-run only, nothing written. Re-run with --build to write the pruned DB.")
    conn.close()


def cmd_build():
    if PRUNED_PATH.exists():
        PRUNED_PATH.unlink()
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    keep = _compute_keep_rowids(conn)
    print(f"Computed {len(keep):,} rows to keep.")

    conn.execute(f"ATTACH DATABASE '{PRUNED_PATH}' AS pruned")
    c = conn.cursor()
    c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    for (create_sql,) in c.fetchall():
        conn.execute(create_sql.replace("CREATE TABLE", "CREATE TABLE pruned.", 1)
                     if create_sql.upper().startswith("CREATE TABLE")
                     else create_sql)

    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = [r[0] for r in c.fetchall()]
    for t in tables:
        if t == 'backtest_cache':
            continue
        print(f"Copying full table: {t}")
        conn.execute(f"INSERT INTO pruned.{t} SELECT * FROM main.{t}")

    print("Copying kept backtest_cache rows...")
    keep_list = list(keep)
    batch = 5000
    for i in range(0, len(keep_list), batch):
        chunk = keep_list[i:i + batch]
        ph = ','.join('?' * len(chunk))
        conn.execute(f"INSERT INTO pruned.backtest_cache SELECT * FROM main.backtest_cache WHERE rowid IN ({ph})", chunk)
    conn.commit()

    c.execute("SELECT COUNT(*) FROM pruned.backtest_cache")
    n = c.fetchone()[0]
    print(f"Pruned backtest_cache row count: {n:,}")
    conn.close()
    print(f"\nBuilt {PRUNED_PATH} -- original {DB_PATH} untouched. "
          f"Inspect it, then run --swap to replace the original.")


def cmd_swap():
    if not PRUNED_PATH.exists():
        print("No pruned DB found -- run --build first.")
        return
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    moved_aside = DB_PATH.with_name(f"trading_universe.db.pre_prune_{ts}")
    shutil.move(str(DB_PATH), str(moved_aside))
    shutil.move(str(PRUNED_PATH), str(DB_PATH))
    print(f"Original moved to {moved_aside} (not deleted -- remove manually once confirmed).")
    print(f"{PRUNED_PATH.name} is now {DB_PATH.name}.")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--dry-run', action='store_true')
    g.add_argument('--build', action='store_true')
    g.add_argument('--swap', action='store_true')
    args = ap.parse_args()
    if args.dry_run:
        cmd_dry_run()
    elif args.build:
        cmd_build()
    elif args.swap:
        cmd_swap()


if __name__ == '__main__':
    main()
