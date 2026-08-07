"""Breaks the single global `reconciliation_mismatch` scenario_expectations
row (ticker=None, node_id=None) into one row per real mode='live' watch_list
node -- raised 2026-07-30: this scenario sat outside the two other per-node
"state report" tables built that session (the 12-row canary_* table and the
5-row soxl_ira staged-config table), and the user expected the same
per-node granularity here.

check_live_state_reconciliation already logs ticker/node_id on every real
coverage_events row (signals_notify.py, log_coverage_event("reconciliation_
mismatch", ..., ticker=..., node_id=...)), and _check_coverage_event
(scripts/coverage_check.py) already scopes its query by scenario['ticker']/
node_id when set -- built defensively 2026-07-30 specifically for this future
case, before any per-node scenario existed. No code change needed, only data.

Deactivates the old global row (id kept, not deleted -- same "never destroy,
just active=0" precedent as the migration that originally created the
duplicate-row confusion this session cleaned up) rather than deleting it, so
its history stays queryable.

expected_frequency='informational' for every per-node row, same tier as the
original global row -- a single node's reconciliation mismatch is trade-
conditional (depends on that node having an open position at all), same
reasoning that demoted the global row from 'daily' 2026-07-30.

Re-run is safe: add_scenario_expectation upserts on (scenario_key, node_id,
ticker, mode); a node added/removed from the live watchlist since the last
run is picked up/left stale respectively (stale rows aren't auto-removed --
re-run after any real watchlist change and manually deactivate any row for a
node no longer mode='live', same as any other scenario_expectations upkeep).

Usage:
  .venv/bin/python scripts/seed_reconciliation_mismatch_per_node.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

if __name__ == '__main__':
    db.ensure_tables()

    with db._conn() as c:
        c.execute(
            "UPDATE scenario_expectations SET active=0, updated_at=datetime('now') "
            "WHERE scenario_key='reconciliation_mismatch' AND ticker IS NULL AND node_id IS NULL"
        )
        n_deactivated = c.total_changes
        c.commit()
    print(f"deactivated {n_deactivated} old global reconciliation_mismatch row(s)")

    live_nodes = [n for n in db.get_watchlist() if n.get('state') != 'paper']
    for node in sorted(live_nodes, key=lambda n: (n['account'] or '', n['ticker'])):
        db.add_scenario_expectation(
            scenario_key='reconciliation_mismatch',
            expected_outcome=(
                f"At least one good-outcome coverage_event (informational) for {node['ticker']} "
                f"({node['account']}) -- live-state reconciliation detects and alerts on a real mismatch"
            ),
            expected_frequency='informational', check_method='coverage_event',
            ticker=node['ticker'], node_id=node['id'], mode=None,
            strategy_type=None, check_params='{}',
        )
        print(f"seeded reconciliation_mismatch  ticker={node['ticker']:6s} "
              f"account={node['account']:10s} node_id={node['id']}")

    print(f"\n{len(live_nodes)} per-node rows seeded.")
