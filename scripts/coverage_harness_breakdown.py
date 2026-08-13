"""Reports the Accountability Grid broken down by BEST_HARNESS (scripts/coverage_registry.py) --
which test mechanism is capable of proving each row -- CROSSED with the real, current
compute_status() for each row (live-computed from coverage_events/coverage_deviations/trade_log
every run, never hand-typed). Answers both "which harness could prove this" and "has it actually
happened" in one report, and logs each run so day-over-day/run-over-run changes are visible
without re-deriving the whole table from scratch.

Meant to run daily (morning readiness check and/or nightly EOD, same cadence as
scripts/coverage_regression_watch.py) -- built 2026-08-13 after repeatedly re-deriving this exact
crosstab as an inline one-off query in conversation.

Splits each of the 3 BEST_HARNESS buckets (live / canary / paper, the last added 2026-08-13 for
rows with zero dry_run reachability at all, e.g. skim_fire) further by whether the row also
carries a not_prod_required_note -- a row can be reachable by a harness in principle but still
not worth chasing (needs a genuine human Slack click, or a real fault/race). A row asking a
categorically different question than "does a real execution mechanism work" (e.g. kernel-vs-live
code consistency) doesn't belong in this Grid at all and should be removed from REGISTRY entirely
with a comment explaining its real home, not merely excluded from BEST_HARNESS -- see
kernel_fill_parity's and second_ticker_one_account's removal comments in coverage_registry.py for
the precedent.

Usage:
  .venv/bin/python scripts/coverage_harness_breakdown.py            -- bucket counts only
  .venv/bin/python scripts/coverage_harness_breakdown.py --list     -- + every row id per bucket
  .venv/bin/python scripts/coverage_harness_breakdown.py --status   -- + current compute_status()
                                                                        per row, logged, diffed
                                                                        against the last run
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db
from scripts.coverage_registry import REGISTRY, BEST_HARNESS, compute_status


def compute_breakdown():
    buckets = {
        'live_testable': [], 'live_not_required': [],
        'canary_testable': [], 'canary_not_required': [],
        'paper_testable': [], 'paper_not_required': [],
        'unclassified': [],
    }
    for row in REGISTRY:
        harness = BEST_HARNESS.get(row['id'])
        demoted = bool(row.get('not_prod_required_note'))
        if harness == 'live':
            key = 'live_not_required' if demoted else 'live_testable'
        elif harness == 'canary':
            key = 'canary_not_required' if demoted else 'canary_testable'
        elif harness == 'paper':
            # Genuinely no dry_run/live reachability at all (e.g. skim_fire/
            # skim_redeploy_alert, 2026-08-13) -- distinct from 'canary', which can
            # always be reached by a dry_run node even if paper happens to be cheaper.
            key = 'paper_not_required' if demoted else 'paper_testable'
        else:
            key = 'unclassified'
        buckets[key].append(row['id'])
    return buckets


def ensure_table():
    with db._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS harness_breakdown_snapshot (
            run_id      INTEGER,
            ts          TEXT NOT NULL DEFAULT (datetime('now')),
            scenario_id TEXT NOT NULL,
            bucket      TEXT NOT NULL,
            status      TEXT NOT NULL,
            detail      TEXT
        )""")
        c.commit()


def _next_run_id():
    with db._conn() as c:
        row = c.execute("SELECT MAX(run_id) FROM harness_breakdown_snapshot").fetchone()
    return (row[0] or 0) + 1


def last_run():
    """Returns (run_id, ts, {scenario_id: (bucket, status)}) for the most recent logged
    run, or (None, None, {}) if this is the first run ever -- run-keyed, not date-keyed
    (same reasoning as coverage_regression_watch.py: safe to run multiple times per day
    without one invocation clobbering another's baseline)."""
    with db._conn() as c:
        run_id = c.execute("SELECT MAX(run_id) FROM harness_breakdown_snapshot").fetchone()[0]
        if run_id is None:
            return None, None, {}
        rows = c.execute(
            "SELECT scenario_id, bucket, status, ts FROM harness_breakdown_snapshot WHERE run_id = ?",
            (run_id,)
        ).fetchall()
    ts = rows[0]['ts'] if rows else None
    return run_id, ts, {r['scenario_id']: (r['bucket'], r['status']) for r in rows}


def log_run(run_id, rows):
    with db._conn() as c:
        c.executemany(
            "INSERT INTO harness_breakdown_snapshot (run_id, scenario_id, bucket, status, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            [(run_id, k, bucket, status, detail[:200]) for k, (bucket, status, detail) in rows.items()],
        )
        c.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true', help='Print every row id under each bucket')
    ap.add_argument('--status', action='store_true',
                     help='Also compute+log real compute_status() per row, diffed against the last run')
    args = ap.parse_args()

    buckets = compute_breakdown()
    order = ['live_testable', 'live_not_required', 'canary_testable', 'canary_not_required',
             'paper_testable', 'paper_not_required', 'unclassified']
    total = sum(len(v) for v in buckets.values())
    print(f"BEST_HARNESS breakdown ({total} rows):\n")
    for k in order:
        if k == 'unclassified' and not buckets[k]:
            continue
        print(f"  {k:20s} {len(buckets[k])}")
        if args.list:
            for row_id in sorted(buckets[k]):
                print(f"    {row_id}")
    if buckets['unclassified']:
        print("\n! unclassified rows found -- REGISTRY has grown since BEST_HARNESS was last "
              "updated (scripts/coverage_registry.py's own docstring warns this map isn't "
              "live-computed), OR one of these genuinely doesn't fit live/canary and should "
              "either get a BEST_HARNESS entry or be reconsidered as a Grid row at all "
              "(see kernel_fill_parity's removal for the precedent). Investigate:")
        for row_id in sorted(buckets['unclassified']):
            print(f"    {row_id}")

    if not args.status:
        return

    ensure_table()
    byid = {r['id']: r for r in REGISTRY}
    bucket_of = {}
    for k, ids in buckets.items():
        for i in ids:
            bucket_of[i] = k

    today_rows = {}
    for row_id, row in byid.items():
        status, detail = compute_status(row)
        today_rows[row_id] = (bucket_of.get(row_id, 'unclassified'), status, detail)

    print("\n=== Crossed with real compute_status() ===")
    for k in order:
        ids = buckets.get(k, [])
        if not ids:
            continue
        print(f"\n{k} ({len(ids)}):")
        for row_id in sorted(ids):
            _, status, detail = today_rows[row_id]
            print(f"  {status:22s} {row_id:45s} {detail[:60]}")

    prior_run_id, prior_ts, prior_rows = last_run()
    print("\n=== Changed since last run ===")
    if prior_run_id is None:
        print("No prior logged run -- this is the first recorded baseline, nothing to diff yet.")
    else:
        changed = []
        for row_id, (bucket, status, _detail) in today_rows.items():
            prior = prior_rows.get(row_id)
            if prior is None or prior != (bucket, status):
                changed.append((row_id, prior, (bucket, status)))
        if not changed:
            print(f"No change vs run #{prior_run_id} ({prior_ts}).")
        else:
            print(f"Changed since run #{prior_run_id} ({prior_ts}):")
            for row_id, prior, now in sorted(changed):
                old_str = f"{prior[0]}/{prior[1]}" if prior else "(new row)"
                print(f"  {row_id:45s} {old_str} -> {now[0]}/{now[1]}")

    run_id = _next_run_id()
    log_run(run_id, today_rows)
    print(f"\nLogged as run #{run_id}.")


if __name__ == '__main__':
    main()
