"""For every row in the Accountability Grid (scripts/coverage_registry.py), shows which real
ticker/node is DESIGNATED to test it -- not which one historically produced evidence (that's a
different, backward-looking question; see coverage_proof_matrix.py/compute_status() for that).

Two real, existing sources of "designed to test" data, no new tracking invented:
  1. signals_db.scenario_expectations' own node_id column -- the real assignment for the canary
     A-G letter scenarios (check_mechanism='scenario_expectations').
  2. signals_db.staged_test_config's scenario_role column, cross-referenced against
     scripts/coverage_check.py's SCENARIO_ROLE_TO_GRID_IDS map -- the real assignment for
     coverage_events-mechanism rows tied to a staged node role (time_exit_via_trail/_sl,
     gap_resize_and_topup, drought_handoff, addon).

A row with neither is correctly "-- none designated --" -- most guard rows (kill_switch_block,
dup_order_no_false_block, etc.) apply to ANY real order attempt, not one specific node by design,
and several execution-lifecycle rows (sl_sync_placement, exit_arm_latency, automated_sell_
execution, buy_fill_reconciled...) currently have no explicit staged assignment at all -- that
absence is itself real information, not a gap in this script.

Built 2026-08-13 as a real script per the project's own convention against throwaway analysis.

Usage: .venv/bin/python scripts/coverage_designated_tester.py [--undesignated-only]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db
from scripts.coverage_registry import REGISTRY
from scripts.coverage_check import SCENARIO_ROLE_TO_GRID_IDS


def compute_designations():
    """Returns {grid_id: [(ticker, node_id), ...]} -- empty list means none designated."""
    role_assign = {}
    for r in db.get_staged_test_configs():
        for gid in SCENARIO_ROLE_TO_GRID_IDS.get(r['scenario_role'], []):
            role_assign.setdefault(gid, []).append((r['ticker'], r['wl_id']))

    se_assign = {}
    for r in db.get_scenario_expectations(active_only=False):
        if r.get('node_id') is not None:
            se_assign.setdefault(r['scenario_key'], []).append((r['ticker'], r['node_id']))

    result = {}
    for row in REGISTRY:
        designated = role_assign.get(row['id'])
        if not designated and row['check_mechanism'] == 'scenario_expectations':
            designated = se_assign.get(row['scenario_key'])
        result[row['id']] = designated or []
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--undesignated-only', action='store_true',
                     help='Only print rows with no designated tester')
    args = ap.parse_args()

    designations = compute_designations()
    n_designated = sum(1 for v in designations.values() if v)
    print(f"Designated testers ({n_designated}/{len(designations)} rows have one):\n")
    for row in REGISTRY:
        who = designations[row['id']]
        if args.undesignated_only and who:
            continue
        who_str = '; '.join(f'{t}(node {n})' for t, n in who) if who else '-- none designated --'
        print(f"  {row['id']:42s} {who_str}")


if __name__ == '__main__':
    main()
