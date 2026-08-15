"""Best-effort, one-time backfill of coverage_events.source for existing rows
(docs/deep_backlog.md's "coverage_events write-attribution" entry, Phase 1,
2026-08-16). Per that design's point 3, this is deliberately NOT a rigorous
forensic reconstruction -- only the one identifiable group of existing rows
(the 42 STAGE_GUARD_TEST rows already known to come from
scripts/stage_check_order_guard_scenarios.py, a synthetic never-traded
ticker used specifically for that staging script) gets backfilled. Every
other pre-existing row is left source=NULL ("unknown/pre-attribution"),
which is expected, not a bug -- "we'll get cleaner over time," not a target
for this pass.

Usage:
  .venv/bin/python scripts/backfill_coverage_event_source.py [--dry-run]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

STAGE_GUARD_TEST_TICKER = "STAGE_GUARD_TEST"
BACKFILL_SOURCE = "fixture:stage_check_order_guard_scenarios"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                     help="report counts without writing")
    args = ap.parse_args()

    with db._conn() as c:
        candidates = c.execute(
            "SELECT COUNT(*) FROM coverage_events WHERE ticker = ? AND source IS NULL",
            (STAGE_GUARD_TEST_TICKER,),
        ).fetchone()[0]
        already_tagged = c.execute(
            "SELECT COUNT(*) FROM coverage_events WHERE ticker = ? AND source = ?",
            (STAGE_GUARD_TEST_TICKER, BACKFILL_SOURCE),
        ).fetchone()[0]
        total_null = c.execute(
            "SELECT COUNT(*) FROM coverage_events WHERE source IS NULL"
        ).fetchone()[0]

        if args.dry_run:
            print(f"Would tag {candidates} STAGE_GUARD_TEST row(s) as source={BACKFILL_SOURCE!r} "
                  f"({already_tagged} already tagged).")
        else:
            c.execute(
                "UPDATE coverage_events SET source = ? WHERE ticker = ? AND source IS NULL",
                (BACKFILL_SOURCE, STAGE_GUARD_TEST_TICKER),
            )
            c.commit()
            print(f"Tagged {candidates} STAGE_GUARD_TEST row(s) as source={BACKFILL_SOURCE!r} "
                  f"({already_tagged} were already tagged from a prior run).")

    remaining_null = total_null - (candidates if not args.dry_run else 0)
    print(f"source still NULL after this run: {remaining_null} row(s) -- expected, "
          f"unattributed history is left as-is per the best-effort design.")


if __name__ == '__main__':
    main()
