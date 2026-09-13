"""Copies an existing (ticker, build_id)'s rows in massive_hourly_derived +
massive_minute_derived into a FRESH build_id, so "latest" naturally resolves
to it again -- without re-running the fetch/derive pipeline. Built 2026-08-26
to recover SOXL/DPST/DFEN after a no-args fetch_massive_minute_data.py refresh
silently created a newer, narrower build (see docs/research_log.md's
2026-08-26 entries) -- the source build's data is still correct, it's just no
longer "latest" by build_id ordering.

Never deletes or modifies the source build -- purely additive, same
multi-vintage philosophy as the rest of this schema. The new build's label
explicitly says which build it's a copy of and why, so a future session
doesn't mistake it for a genuinely fresh pull.

Usage:
    .venv/bin/python scripts/reinject_derived_build.py --ticker SOXL --source-build-id 98
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db_cache


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--source-build-id", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with sqlite3.connect(db_cache.TICKDATA_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        meta = conn.execute(
            "SELECT * FROM massive_hourly_derived_builds WHERE ticker=? AND id=?",
            (args.ticker, args.source_build_id)).fetchone()
        if meta is None:
            print(f"No build metadata found for {args.ticker} build_id={args.source_build_id}", file=sys.stderr)
            return 1
        meta = dict(meta)

        hourly_rows = conn.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM massive_hourly_derived WHERE ticker=? AND build_id=?",
            (args.ticker, args.source_build_id)).fetchone()
        minute_rows = conn.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM massive_minute_derived WHERE ticker=? AND build_id=?",
            (args.ticker, args.source_build_id)).fetchone()

        print(f"Source build {args.source_build_id} for {args.ticker}: label={meta['label']!r}")
        print(f"  hourly: {hourly_rows[0]:,} rows, {hourly_rows[1]} -> {hourly_rows[2]}")
        print(f"  minute: {minute_rows[0]:,} rows, {minute_rows[1]} -> {minute_rows[2]}")

        if args.dry_run:
            print("--dry-run: not writing anything.")
            return 0

        new_label = f"copy of build {args.source_build_id} ({meta['label']}), reinjected 2026-08-26 to fix latest-build-id regression (see docs/research_log.md)"
        new_build_id = db_cache.record_massive_hourly_build(
            args.ticker, new_label, meta["raw_data_pulled_at"], meta["raw_data_start"],
            meta["raw_data_end"], meta["dividend_data_asof"], meta["row_count"],
            meta["correction_count"], conn=conn)

        conn.execute(
            "INSERT INTO massive_hourly_derived (ticker, build_id, ts, open, high, low, close, volume, corrected) "
            "SELECT ticker, ?, ts, open, high, low, close, volume, corrected FROM massive_hourly_derived "
            "WHERE ticker=? AND build_id=?",
            (new_build_id, args.ticker, args.source_build_id))
        conn.execute(
            "INSERT INTO massive_minute_derived (ticker, build_id, ts, open, high, low, close) "
            "SELECT ticker, ?, ts, open, high, low, close FROM massive_minute_derived "
            "WHERE ticker=? AND build_id=?",
            (new_build_id, args.ticker, args.source_build_id))
        conn.commit()

        print(f"Reinjected as new build_id={new_build_id}")
        return 0


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    sys.exit(main())
