"""Promotes a massive_hourly_derived/massive_minute_derived build_id to be the
ACTIVE vintage db_cache.get_massive_hourly_ohlcv/get_massive_minute_ohlcv resolve
to when called with no explicit build_id -- the derived-DB-layer counterpart of
scripts/promote_market_data_pull.py (same pattern one level deeper: staging is
implicit here, since build_massive_hourly_derived.py already writes each rebuild
under its own permanent build_id and never overwrites a prior one -- "promotion"
just changes db_cache.active_builds' pointer, no file copy needed).

Real incident this exists for (2026-08-26, see docs/deep_backlog.md's
"active_builds promotion table" entry): the OLD no-build_id resolution was pure
`ORDER BY id DESC` (latest build_id that has rows) -- a `fetch_massive_minute_data.py`
refresh with no --years flag silently narrowed SOXL/DPST/DFEN's canonical minute
archive, and the next `build_massive_hourly_derived.py` run's new, narrower build
silently became "latest" with zero completeness check. A build now only becomes
active via this script -- never automatically on creation.

Two modes:
1. --migrate: one-time (idempotent, safe to re-run) backfill for a DB that
   predates active_builds -- for every (ticker, table_name) with NO existing
   active_builds row, inserts one pointing at whatever the OLD `ORDER BY id DESC`
   mechanism currently resolves to (the latest build_id with real rows), so
   nothing changes behaviorally for any ticker until a human explicitly promotes
   something different. Never touches a (ticker, table_name) that already has an
   active_builds row.
2. Explicit promotion: --ticker --table {hourly,minute,both} [--build-id N]
   (default: the newest build_id in massive_hourly_derived_builds for that
   ticker) [--force]. Refuses to promote a build whose real stored date range is
   narrower than the currently active build's on EITHER end (new start later, OR
   new end earlier) without --force -- a real, data-integrity refuse-to-narrow
   guard, not just a start-date check (this module's date range comes from the
   derived table's own MIN(ts)/MAX(ts), the actual stored rows, not the raw-pull
   metadata in massive_hourly_derived_builds, which reflects the raw CSV pulled,
   not necessarily what got resampled/stored).

Usage:
    .venv/bin/python scripts/promote_derived_build.py --migrate
    .venv/bin/python scripts/promote_derived_build.py --ticker SOXL --table both
    .venv/bin/python scripts/promote_derived_build.py --ticker SOXL --table hourly --build-id 174
    .venv/bin/python scripts/promote_derived_build.py --ticker SOXL --table both --force
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db_cache

TABLE_FOR = {
    'hourly': 'massive_hourly_derived',
    'minute': 'massive_minute_derived',
}


def _old_style_latest_with_rows(conn, ticker, table_name):
    """The exact OLD `ORDER BY b.id DESC` + EXISTS resolution db_cache.py's
    get_massive_hourly_derived/get_massive_minute_derived used before active_builds
    existed -- reproduced here ONLY for --migrate's backfill, so migration is a
    behavioral no-op by construction. Never used by the promoted read path itself
    anymore (see db_cache.py)."""
    row = conn.execute(f"""
        SELECT b.id FROM massive_hourly_derived_builds b
        WHERE b.ticker=? AND EXISTS (
            SELECT 1 FROM {TABLE_FOR[table_name]} d
            WHERE d.ticker=b.ticker AND d.build_id=b.id
        )
        ORDER BY b.id DESC LIMIT 1
    """, (ticker,)).fetchone()
    return row[0] if row else None


def _date_range(conn, ticker, table_name, build_id):
    row = conn.execute(
        f"SELECT MIN(ts), MAX(ts), COUNT(*) FROM {TABLE_FOR[table_name]} WHERE ticker=? AND build_id=?",
        (ticker, build_id)).fetchone()
    return row  # (min_ts, max_ts, row_count) -- all None if build_id has no rows


def cmd_migrate(conn):
    # Ensure every table this module touches exists first -- mirrors db_cache.py's
    # own convention (every getter/writer calls its _ensure_*_table before querying).
    # A fresh/isolated DB (or, in principle, a legacy DB that only ever wrote the
    # hourly leg) can have massive_hourly_derived_builds/massive_hourly_derived
    # without massive_minute_derived existing yet -- querying it directly would
    # raise "no such table" instead of correctly resolving "no minute build for
    # this ticker" (found via this module's own test suite).
    db_cache._ensure_massive_hourly_derived_table(conn)
    db_cache._ensure_massive_minute_derived_table(conn)
    db_cache._ensure_active_builds_table(conn)
    tickers = [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM massive_hourly_derived_builds").fetchall()]
    n_backfilled = 0
    n_skipped_existing = 0
    n_skipped_no_rows = 0
    for ticker in sorted(tickers):
        for table_name in ('hourly', 'minute'):
            if db_cache.get_active_build_id(ticker, table_name, conn=conn) is not None:
                n_skipped_existing += 1
                continue
            build_id = _old_style_latest_with_rows(conn, ticker, table_name)
            if build_id is None:
                n_skipped_no_rows += 1
                continue
            db_cache.promote_active_build(
                ticker, table_name, build_id,
                note="backfilled by scripts/promote_derived_build.py --migrate "
                     "(old ORDER BY id DESC resolution, preserved as-is)",
                conn=conn)
            n_backfilled += 1
    conn.commit()
    print(f"Migration: {n_backfilled} (ticker, table_name) pairs backfilled, "
          f"{n_skipped_existing} already had an active_builds row (untouched), "
          f"{n_skipped_no_rows} had no build with rows for that table (skipped, "
          f"matches old empty-DataFrame behavior).")
    return 0


def cmd_promote(conn, args):
    db_cache._ensure_massive_hourly_derived_table(conn)
    db_cache._ensure_massive_minute_derived_table(conn)
    db_cache._ensure_active_builds_table(conn)
    tables = ['hourly', 'minute'] if args.table == 'both' else [args.table]
    exit_code = 0
    for table_name in tables:
        build_id = args.build_id
        if build_id is None:
            row = conn.execute(
                "SELECT id FROM massive_hourly_derived_builds WHERE ticker=? ORDER BY id DESC LIMIT 1",
                (args.ticker,)).fetchone()
            if row is None:
                print(f"No massive_hourly_derived_builds rows at all for {args.ticker} -- nothing to promote.",
                      file=sys.stderr)
                exit_code = 1
                continue
            build_id = row[0]

        new_start, new_end, new_count = _date_range(conn, args.ticker, table_name, build_id)
        if new_count in (None, 0):
            print(f"REFUSED [{table_name}]: build_id={build_id} has zero rows in "
                  f"{TABLE_FOR[table_name]} for {args.ticker} -- refusing to promote an empty/orphan build.",
                  file=sys.stderr)
            exit_code = 1
            continue

        current_build_id = db_cache.get_active_build_id(args.ticker, table_name, conn=conn)
        if current_build_id is not None:
            cur_start, cur_end, cur_count = _date_range(conn, args.ticker, table_name, current_build_id)
            print(f"[{table_name}] currently active: build_id={current_build_id} "
                  f"({cur_start} -> {cur_end}, {cur_count:,} rows)")
            narrower = (new_start > cur_start) or (new_end < cur_end)
            if narrower and not args.force:
                print(f"REFUSED [{table_name}]: build_id={build_id} range ({new_start} -> {new_end}) "
                      f"is narrower than currently active build_id={current_build_id}'s "
                      f"({cur_start} -> {cur_end}) -- this would shrink what resolves as active. "
                      f"Use --force to override.", file=sys.stderr)
                exit_code = 1
                continue
        else:
            print(f"[{table_name}] no currently active build (first promotion for this ticker/table).")

        db_cache.promote_active_build(args.ticker, table_name, build_id, note=args.note, conn=conn)
        print(f"[{table_name}] PROMOTED build_id={build_id} ({new_start} -> {new_end}, "
              f"{new_count:,} rows) as active for {args.ticker}")
    conn.commit()
    return exit_code


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--migrate", action="store_true",
                     help="one-time idempotent backfill for a DB predating active_builds")
    ap.add_argument("--ticker")
    ap.add_argument("--table", choices=["hourly", "minute", "both"])
    ap.add_argument("--build-id", type=int, default=None,
                     help="default: newest build_id in massive_hourly_derived_builds for this ticker")
    ap.add_argument("--force", action="store_true", help="allow promoting a narrower (shrinking) build")
    ap.add_argument("--note", default=None, help="optional free-text note recorded with the promotion")
    args = ap.parse_args()

    if args.migrate:
        if args.ticker or args.table:
            print("--migrate is standalone -- don't combine with --ticker/--table.", file=sys.stderr)
            return 1
        with sqlite3.connect(db_cache.DB_PATH) as conn:
            return cmd_migrate(conn)

    if not args.ticker or not args.table:
        print("Either --migrate, or both --ticker and --table are required.", file=sys.stderr)
        return 1

    with sqlite3.connect(db_cache.DB_PATH) as conn:
        return cmd_promote(conn, args)


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    sys.exit(main())
