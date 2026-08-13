"""Reproduces the full Accountability Grid ticket-coverage table (BEST_HARNESS x
compute_status() x designated tester, live+canary+paper together) that was previously
rebuilt ad hoc via one-off queries during the 2026-08-13 ticket-coverage promotion
pass -- see docs/grid_ticker_coverage_promotion_process.md for the exercise this
supports. Built as a real, rerunnable script per that doc's own convention against
throwaway analysis.

Also runs two integrity checks that a hand-built table can't catch by eye:

1. **Ticker mismatch**: a designated tester is a (ticker, node_id) pair sourced from
   scenario_expectations.node_id / staged_test_config.wl_id (see
   coverage_designated_tester.py) -- if the node_id's REAL current watch_list.ticker
   doesn't match the stored ticker string, the mapping has gone stale (a node was
   re-ticker'd, or the wrong node_id was ever recorded). Flags any drift.

2. **Timing discrepancy**: any coverage_events/coverage_deviations row whose timestamp
   predates its own node_id's watch_list.added_at is impossible -- the event was
   logged (or backfilled) against a node that didn't exist yet at that timestamp.
   This is the generic version of the 2026-08-13 FAS/FAZ backfill-artifact bug
   (8 coverage_deviations rows minted for check_date before the node existed) --
   this check catches the SAME shape anywhere else in the DB, not just that one
   incident, and catches it in coverage_events too (not just coverage_deviations,
   which the one-off fix didn't check).

Usage:
  .venv/bin/python scripts/coverage_ticket_table.py             -- full table
  .venv/bin/python scripts/coverage_ticket_table.py --checks-only -- integrity checks only
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db
from scripts.coverage_registry import REGISTRY, compute_status, BEST_HARNESS
from scripts.coverage_designated_tester import compute_designations


def build_table():
    byid = {r['id']: r for r in REGISTRY}
    designations = compute_designations()
    rows = []
    for row in REGISTRY:
        harness = BEST_HARNESS.get(row['id'])
        demoted = bool(row.get('not_prod_required_note'))
        status, detail = compute_status(row)
        who = designations.get(row['id'], [])
        rows.append(dict(id=row['id'], harness=harness, demoted=demoted,
                          status=status, detail=detail, testers=who))
    return rows


def check_ticker_alignment():
    """Every (ticker, node_id) designated-tester pair -- does node_id's real,
    current watch_list.ticker actually match the stored ticker string?"""
    designations = compute_designations()
    mismatches = []
    with db._conn() as c:
        for grid_id, pairs in designations.items():
            for ticker, node_id in pairs:
                row = c.execute("SELECT ticker FROM watch_list WHERE id=?", (node_id,)).fetchone()
                if row is None:
                    mismatches.append((grid_id, ticker, node_id, 'NODE DELETED'))
                elif row['ticker'] != ticker:
                    mismatches.append((grid_id, ticker, node_id, f"now ticker={row['ticker']!r}"))
    return mismatches


def check_tester_activity():
    """A designated tester that's been paused (state='paper'/'research', not 'live'/
    'dry_run') doesn't automatically get removed from staged_test_config -- per the
    live-test-node-setup skill's own convention, a paused node stays as a rerunnable
    regression fixture, not a stale error. But this silently passes the ticker-alignment
    check with no signal that the tester is currently inactive (found by Opus review
    2026-08-13, re: SH/135 paused for time_exit_trigger_armed while ERX/226 took over
    active duty) -- surfaced here as informational, not a failure."""
    designations = compute_designations()
    inactive = []
    with db._conn() as c:
        for grid_id, pairs in designations.items():
            for ticker, node_id in pairs:
                row = c.execute("SELECT state FROM watch_list WHERE id=?", (node_id,)).fetchone()
                if row and row['state'] not in ('live', 'dry_run'):
                    inactive.append((grid_id, ticker, node_id, row['state']))
    return inactive


def check_timing_discrepancies():
    """Any coverage_events row logged against a node_id whose log timestamp predates
    that node's own watch_list.added_at, OR any coverage_deviations row whose
    check_date (the trading day being verified, not the log ts) predates the node's
    creation DATE -- the real shape of the 2026-08-13 FAS/FAZ backfill-artifact bug
    (a manual `coverage_check.py --date <past>` run minted deviations for a check_date
    before the node existed; the deviation's own log ts was same-day, after creation,
    so comparing ts alone misses this -- check_date is the column that matters here).

    added_at is stored via SQLite's datetime('now') -- UTC -- while check_date is an ET
    trading-calendar date; compared here via 'localtime' (system TZ confirmed ET), same
    fix as coverage_check.py's run_check guard (found by the same Opus review).

    Returns (unexplained, historical) -- unexplained are new problems needing attention
    (reason IS NULL); historical already carry an explanation (from this same class of
    bug, already corrected once) and are reported separately so a clean prior fix doesn't
    read as permanent noise on every future run."""
    unexplained, historical = [], []
    with db._conn() as c:
        node_created = {r['id']: r['d'] for r in c.execute(
            "SELECT id, date(added_at, 'localtime') AS d FROM watch_list WHERE added_at IS NOT NULL")}
        rows = c.execute(
            "SELECT id, node_id, ts FROM coverage_events WHERE node_id IS NOT NULL").fetchall()
        for r in rows:
            created = node_created.get(r['node_id'])
            if created and r['ts'] < created:
                unexplained.append(('coverage_events', r['id'], r['node_id'], r['ts'], created))
        rows = c.execute(
            "SELECT id, node_id, check_date, reason FROM coverage_deviations "
            "WHERE node_id IS NOT NULL AND check_date IS NOT NULL").fetchall()
        for r in rows:
            created = node_created.get(r['node_id'])
            if created and r['check_date'] < created:
                entry = ('coverage_deviations (check_date)', r['id'], r['node_id'],
                          r['check_date'], created)
                (historical if r['reason'] else unexplained).append(entry)
    return unexplained, historical


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--checks-only', action='store_true',
                     help='Skip the full table, just run the two integrity checks')
    args = ap.parse_args()

    if not args.checks_only:
        rows = build_table()
        for harness_group in ('live', 'canary', 'paper', None):
            group_rows = [r for r in rows if r['harness'] == harness_group]
            if not group_rows:
                continue
            label = harness_group or 'UNCLASSIFIED'
            print(f"\n=== {label} ({len(group_rows)}) ===")
            for r in sorted(group_rows, key=lambda x: x['id']):
                tag = ' [demoted]' if r['demoted'] else ''
                who = '; '.join(f'{t}({n})' for t, n in r['testers']) if r['testers'] else '-- none --'
                print(f"  {r['id']:44s}{tag:10s} {r['status']:22s} {who}")

    print("\n=== Integrity checks ===")
    mismatches = check_ticker_alignment()
    if mismatches:
        print(f"\n! {len(mismatches)} ticker mismatch(es):")
        for grid_id, ticker, node_id, detail in mismatches:
            print(f"    {grid_id}: stored ticker={ticker!r} node_id={node_id} -- {detail}")
    else:
        print("Ticker alignment: clean -- every designated tester's stored ticker matches its "
              "node's real current watch_list.ticker.")

    unexplained, historical = check_timing_discrepancies()
    if unexplained:
        print(f"\n! {len(unexplained)} UNEXPLAINED timing discrepancy(ies) (event predates node "
              f"creation, needs a reason/investigation):")
        for table, rid, node_id, ts, created in unexplained:
            print(f"    {table} id={rid} node_id={node_id}: event ts={ts} but node added_at={created}")
    if historical:
        print(f"\n  ({len(historical)} historical timing discrepancy(ies), already explained -- "
              f"informational only, not new noise): " +
              ", ".join(f"id={rid}" for _, rid, *_ in historical))
    if not unexplained and not historical:
        print("Timing: clean -- no coverage_events/coverage_deviations row predates its node's "
              "own creation.")

    inactive = check_tester_activity()
    if inactive:
        print(f"\n  ({len(inactive)} designated tester(s) currently paused, not active -- still "
              f"valid as a rerunnable regression fixture, just not proving anything right now):")
        for grid_id, ticker, node_id, state in inactive:
            print(f"    {grid_id}: {ticker}(node {node_id}) state={state!r}")
    else:
        print("Tester activity: clean -- every designated tester is currently 'live'/'dry_run'.")

    unclassified = [r for r in REGISTRY if BEST_HARNESS.get(r['id']) not in ('live', 'canary', 'paper')]
    if unclassified:
        print(f"\n! {len(unclassified)} unclassified row(s): {[r['id'] for r in unclassified]}")
    else:
        print(f"Classification: clean -- all {len(REGISTRY)} rows have a BEST_HARNESS entry.")


if __name__ == '__main__':
    main()
