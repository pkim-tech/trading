"""One-time backfill of massive_minute_derived_builds for every existing real
minute build (docs/design.md's 2026-08-29 (very late) entry, Task #17).

Confirmed against the real DB (2026-08-29) before writing this: EVERY distinct
(ticker, build_id) pair present in massive_minute_derived already has a matching
massive_hourly_derived_builds row (same id, same ticker) -- expected, since
build_ticker() has always written both legs from the same build_id in one
transaction. So this backfill reuses label/built_at/raw_data_pulled_at/
dividend_data_asof from that sibling row (real provenance, not guessed) --
row_count and raw_data_start/raw_data_end are computed fresh from
massive_minute_derived's own MIN/MAX(ts)/COUNT(*) (the minute leg's own real
numbers, never copied from the hourly row, since row counts/ranges differ by
granularity). correction_count is always 0 (real, not unknown -- the spike-
correction step never runs against minute bars). If a future run ever finds a
minute build_id with NO matching hourly row (not the case today), it falls back
to label="backfilled, unknown provenance" and NULL for the unreconstructable
built_at/raw_data_pulled_at/dividend_data_asof fields, per this project's
"NULL/sentinel over guessing" backfill convention (see docs/plans/... active_builds
--migrate backfill, same standard).

Idempotent: record_massive_minute_build() does INSERT OR REPLACE keyed on id
(the build_id), so a rerun just re-derives the same values.

Usage:
  .venv/bin/python scripts/backfill_massive_minute_derived_builds.py [--dry-run]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db_cache

UNKNOWN_LABEL = "backfilled, unknown provenance"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help="report what would be written, without writing")
    args = ap.parse_args()

    with sqlite3.connect(db_cache.TICKDATA_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        db_cache._ensure_massive_minute_derived_builds_table(conn)
        pairs = conn.execute(
            "SELECT DISTINCT ticker, build_id FROM massive_minute_derived ORDER BY ticker, build_id"
        ).fetchall()

        n_from_sibling, n_unknown = 0, 0
        for row in pairs:
            ticker, build_id = row['ticker'], row['build_id']
            stats = conn.execute(
                "SELECT COUNT(*) AS n, MIN(ts) AS start, MAX(ts) AS end FROM massive_minute_derived "
                "WHERE ticker=? AND build_id=?", (ticker, build_id)
            ).fetchone()

            sibling = conn.execute(
                "SELECT label, built_at, raw_data_pulled_at, dividend_data_asof FROM massive_hourly_derived_builds "
                "WHERE id=? AND ticker=?", (build_id, ticker)
            ).fetchone()
            if sibling:
                label, built_at, raw_pulled_at, dividend_asof = (
                    sibling['label'], sibling['built_at'], sibling['raw_data_pulled_at'], sibling['dividend_data_asof'])
                n_from_sibling += 1
            else:
                # built_at can't be a real SQL NULL (massive_minute_derived_builds.built_at is
                # TEXT NOT NULL) -- a clearly-labeled sentinel string instead, same honesty
                # standard as UNKNOWN_LABEL, never silently falling through to record_massive_
                # minute_build's own datetime('now') default (which would fabricate a fake
                # build time for a build this script can't actually date).
                label = UNKNOWN_LABEL
                built_at = "unknown (backfilled, no sibling hourly provenance)"
                raw_pulled_at, dividend_asof = None, None
                n_unknown += 1

            print(f"{ticker:8s} build_id={build_id:4d}  rows={stats['n']:6d}  "
                  f"range=[{stats['start']} .. {stats['end']}]  "
                  f"provenance={'sibling hourly row' if sibling else 'UNKNOWN'}")

            if not args.dry_run:
                db_cache.record_massive_minute_build(
                    build_id, ticker, label, raw_pulled_at, stats['start'], stats['end'],
                    dividend_asof, stats['n'], correction_count=0, built_at=built_at, conn=conn)
        if not args.dry_run:
            conn.commit()

        print(f"\n{'[dry-run] would write' if args.dry_run else 'Wrote'} "
              f"{len(pairs)} massive_minute_derived_builds row(s): "
              f"{n_from_sibling} from sibling hourly provenance, {n_unknown} unknown/backfilled")


if __name__ == "__main__":
    import script_usage
    script_usage.record_invocation()
    main()
