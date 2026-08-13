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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from top_safe_nodes import CLIFF_RADIUS  # single source of truth for neighbor width --
# importing (not re-hardcoding) so this and top_safe_nodes.py's own cliff-safety check
# can't drift apart again the way they did before 2026-08-07 (this prune kept only
# +/-2 grid steps while top_safe_nodes.py's real check needs +/-CLIFF_RADIUS=3, silently
# dropping ~49% of the neighbor cells cliff-safety needs -- found by paired Opus review).

DB_PATH = Path("cache/research/trading_universe.db")
PRUNED_PATH = Path("cache/research/trading_universe_pruned.db")
VALIDATION_SENTINEL = Path("cache/research/.prune_validated")

ROBUST_ALPHA_SQL = ("MIN(alpha_vs_spy, COALESCE(alpha_vs_spy_pessimistic, alpha_vs_spy), "
                     "COALESCE(alpha_vs_spy_certain, alpha_vs_spy))")

# Deterministic tiebreaker appended to every ORDER BY ... LIMIT 1 winner-selection query
# in this module -- found 2026-08-07 that 5,800 of 11,288 real (ticker,strategy,version,
# window,z,entry_timing) groups have a tied max robust_alpha, so without this the winner
# (and therefore the island's own center) was arbitrary/non-reproducible run to run.
TIEBREAK_SQL = "trades DESC, stop_loss, max_hold_hours"


def _groups(conn, ticker):
    # entry_timing included 2026-08-07 -- it's a real backtest_cache PK column (close vs
    # open_check are different sweep campaigns), previously pooled into one group here,
    # which silently dropped the losing timing's real island in any group holding both
    # (measured: 22 of 23 such groups lost data this way).
    c = conn.cursor()
    c.execute("""SELECT DISTINCT strategy, version, window, z_score_threshold, entry_timing
                 FROM backtest_cache WHERE ticker=?""", (ticker,))
    return c.fetchall()


# take_profit is NULL for TrailingBothZScoreBreakout rows -- that strategy stores its
# arm value in arm_sell_pct instead (same root cause documented in top_safe_nodes.py).
# COALESCE pulls whichever real column is actually populated for this row's strategy,
# so a plain `take_profit IN (?)` filter never silently matches nothing on NULL.
TP_COL_SQL = "COALESCE(take_profit, arm_sell_pct)"


def _island_rowids_for_group(conn, ticker, strategy, version, window, z_score_threshold, entry_timing):
    """Winning row = best robust_alpha in this group (trades>0 required, ties broken
    deterministically -- see TIEBREAK_SQL). Island = winning row's own axis values +/-
    the CLIFF_RADIUS nearest distinct values that exist in this group on take_profit and
    stop_loss (index-based, handles varying step sizes per strategy/version naturally,
    no hardcoded step size -- but the RADIUS itself is imported from top_safe_nodes.py so
    it can't silently fall short of what that cliff-safety check actually needs), and any
    max_hold_hours within +/-24h of the winner's (already wider than the +/-7h
    top_safe_nodes.py needs, so untouched)."""
    c = conn.cursor()
    c.execute(f"""
        SELECT {TP_COL_SQL}, stop_loss, max_hold_hours, {ROBUST_ALPHA_SQL} AS robust_alpha
        FROM backtest_cache
        WHERE ticker=? AND strategy=? AND version=? AND window=? AND z_score_threshold=?
              AND entry_timing=? AND trades > 0
        ORDER BY robust_alpha DESC, {TIEBREAK_SQL} LIMIT 1
    """, (ticker, strategy, version, window, z_score_threshold, entry_timing))
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
                  AND entry_timing=?
        """, (ticker, strategy, version, window, z_score_threshold, entry_timing))
        vals = sorted(set(r[0] for r in c.fetchall() if r[0] is not None))
        if center not in vals:
            return {center}
        idx = vals.index(center)
        return set(vals[max(0, idx - CLIFF_RADIUS):min(len(vals), idx + CLIFF_RADIUS + 1)])

    tp_keep = list(nearest_values(TP_COL_SQL, w_tp))
    sl_keep = list(nearest_values('stop_loss', w_sl))
    tp_ph = ','.join('?' * len(tp_keep))
    sl_ph = ','.join('?' * len(sl_keep))
    c.execute(f"""SELECT rowid FROM backtest_cache
                  WHERE ticker=? AND strategy=? AND version=? AND window=? AND z_score_threshold=?
                  AND entry_timing=?
                  AND {TP_COL_SQL} IN ({tp_ph}) AND stop_loss IN ({sl_ph})
                  AND ABS(max_hold_hours - ?) <= 24""",
              [ticker, strategy, version, window, z_score_threshold, entry_timing] + tp_keep + sl_keep + [w_hold])
    return [r[0] for r in c.fetchall()]


def _compute_keep_rowids(conn):
    c = conn.cursor()
    c.execute("SELECT DISTINCT ticker FROM backtest_cache")
    all_tickers = [r[0] for r in c.fetchall()]
    keep = set()
    for ticker in all_tickers:
        for strategy, version, window, z, entry_timing in _groups(conn, ticker):
            keep.update(_island_rowids_for_group(conn, ticker, strategy, version, window, z, entry_timing))
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

    # Indexes are NOT covered by the schema copy above (that only copies
    # sqlite_master type='table' rows) -- found 2026-08-07 the hard way: the
    # 2026-08-07 swap silently dropped idx_bc_ticker/idx_bc_version_ticker_strategy,
    # invisible to full_db_prune_validate.py (which only checks row content),
    # and only surfaced as query performance degrading afterward. Mirrors
    # run_optimization_sweep.rebuild_indexes() exactly -- keep the two in sync.
    # SQLite's schema-qualified CREATE INDEX puts the schema prefix on the INDEX
    # NAME, not the table name (`CREATE INDEX schema.idx ON table(...)`, not
    # `CREATE INDEX idx ON schema.table(...)`) -- got this backwards on the first
    # pass, which crashed with "near '.': syntax error" the first time this code
    # path actually ran (a `--build` against real tranche data), since the earlier
    # fix was only Python-syntax-checked, never smoke-tested end to end.
    print("Rebuilding indexes on pruned.backtest_cache...")
    conn.execute("CREATE INDEX IF NOT EXISTS pruned.idx_bc_version_window ON backtest_cache(version, window)")
    conn.execute("CREATE INDEX IF NOT EXISTS pruned.idx_bc_version_ticker_strategy ON backtest_cache(version, ticker, strategy)")
    conn.execute("CREATE INDEX IF NOT EXISTS pruned.idx_bc_version_return ON backtest_cache(version, strategy_return)")
    conn.execute("CREATE INDEX IF NOT EXISTS pruned.idx_bc_ticker ON backtest_cache(ticker)")
    conn.commit()

    conn.close()
    print(f"\nBuilt {PRUNED_PATH} -- original {DB_PATH} untouched. "
          f"Inspect it, then run --swap to replace the original.")


def pruned_fingerprint():
    """Binds a validation pass to one specific build -- mtime+size of PRUNED_PATH,
    good enough to detect "a different --build ran since you validated," not meant
    to be cryptographically strong."""
    st = PRUNED_PATH.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def live_db_fingerprint():
    """Binds a validation pass to the live DB's state at that moment -- mtime+size
    of DB_PATH. Added 2026-08-07: the sentinel previously bound ONLY to the pruned
    file, not the live DB, so a sweep writing new rows into DB_PATH during a long
    manual validation run (real today: 40+ min, spanning a lock-contention crash
    and an index rebuild) could have its new rows silently discarded on swap --
    the sentinel would still read as valid since it never checked the live DB at
    all. The normal automated path (run_liquidity_tranches.sh's tight
    sweep-then-immediately-prune sequencing) doesn't hit this in practice, but
    nothing enforced that assumption before now."""
    st = DB_PATH.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def write_validation_sentinel():
    """Called by scripts/full_db_prune_validate.py only after every check passes --
    cmd_swap refuses to run without a fresh, matching sentinel (found 2026-08-07:
    cmd_swap previously only checked PRUNED_PATH.exists(), so a stale or
    never-validated build could be swapped in by hand with no gate at all)."""
    VALIDATION_SENTINEL.write_text(f"{pruned_fingerprint()}|{live_db_fingerprint()}")


def cmd_swap():
    # Every refusal branch below calls sys.exit(1), not a plain `return` --
    # found 2026-08-12 that a plain return exits 0, which `set -e` in
    # run_liquidity_tranches.sh can't catch, so the script would silently
    # continue past a swap that never happened, print "Extract validation OK
    # -- swapped in.", and mark the tranche done with the live DB unchanged.
    # This is the exact historical bug this module's own comments describe
    # fixing once already (see the Phase-2 rewrite note above) -- it had crept
    # back in because these specific branches were never touched by that fix.
    if not PRUNED_PATH.exists():
        print("No pruned DB found -- run --build first.")
        sys.exit(1)
    if not VALIDATION_SENTINEL.exists():
        print("No validation sentinel found -- run scripts/full_db_prune_validate.py "
              "(or another validator that calls write_validation_sentinel()) and confirm "
              "it passes before swapping. Refusing to swap an unvalidated build.")
        sys.exit(1)
    recorded = VALIDATION_SENTINEL.read_text().strip()
    recorded_pruned, _, recorded_live = recorded.partition("|")
    if recorded_pruned != pruned_fingerprint():
        print("Validation sentinel is STALE -- the pruned DB has changed (or been "
              "rebuilt) since the last passing validation. Refusing to swap. "
              "Re-run the validator against the current --build output first.")
        sys.exit(1)
    if not recorded_live or recorded_live != live_db_fingerprint():
        print("Validation sentinel is STALE -- the LIVE DB has changed since the "
              "last passing validation (e.g. a sweep wrote new rows). Swapping now "
              "would silently discard that new data. Refusing to swap. Re-run the "
              "validator against the current live DB first.")
        sys.exit(1)
    # DB_PATH runs in WAL journal mode -- shutil.move() below only renames the
    # main .db file, not its -wal/-shm sidecars. Found 2026-08-12 the hard way:
    # a swap that ran while another process (a read-only diagnostic query) still
    # held DB_PATH open left a live, un-checkpointed trading_universe.db-wal
    # behind under the OLD canonical name after the rename. The pruned file then
    # got moved INTO that same canonical name and SQLite auto-adopted the
    # orphaned -wal purely by filename adjacency -- replaying WAL frames written
    # against the original (much larger) file's page layout onto the pruned
    # file's completely different layout, corrupting its schema b-tree
    # (sqlite_master ended up holding backtest_cache row data). Checkpointing
    # with TRUNCATE here forces the -wal file to be fully flushed and removed;
    # if any other connection is still open against DB_PATH, TRUNCATE can't
    # fully clear it and reports nonzero remaining log/checkpointed frames --
    # refuse to swap in that case rather than risk the same corruption again.
    check_conn = sqlite3.connect(DB_PATH, timeout=60.0)
    busy, log_frames, checkpointed = check_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    check_conn.close()
    if busy or log_frames > 0:
        print(f"Refusing to swap -- WAL checkpoint didn't fully clear "
              f"(busy={busy}, log_frames={log_frames}, checkpointed={checkpointed}). "
              f"Another connection is still holding {DB_PATH} open (e.g. a lingering "
              f"read/diagnostic query) -- close it and re-run --swap.")
        sys.exit(1)
    for sidecar_suffix in ("-wal", "-shm"):
        p = DB_PATH.with_name(DB_PATH.name + sidecar_suffix)
        if p.exists():
            print(f"Refusing to swap -- {p} still present after a clean WAL checkpoint "
                  f"(unexpected). Investigate before retrying.")
            sys.exit(1)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    moved_aside = DB_PATH.with_name(f"trading_universe.db.pre_prune_{ts}")
    shutil.move(str(DB_PATH), str(moved_aside))
    shutil.move(str(PRUNED_PATH), str(DB_PATH))
    # PRUNED_PATH was built as a fresh file via ATTACH DATABASE, which defaults to
    # rollback-journal mode (not WAL) -- confirmed empirically after this bug (no
    # -wal/-shm ever appears for it). Still, verify explicitly rather than assume,
    # since a future change to cmd_build() could set WAL on the attached db too.
    for sidecar_suffix in ("-wal", "-shm"):
        p = PRUNED_PATH.with_name(PRUNED_PATH.name + sidecar_suffix)
        if p.exists():
            dest = moved_aside.with_name(moved_aside.name + sidecar_suffix)
            print(f"WARNING: unexpected {p} found for the pruned build -- moving "
                  f"alongside {DB_PATH.name} rather than leaving it orphaned.")
            shutil.move(str(p), str(DB_PATH.with_name(DB_PATH.name + sidecar_suffix)))
    VALIDATION_SENTINEL.unlink()
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
