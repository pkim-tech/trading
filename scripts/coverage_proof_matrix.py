"""Proof-tier matrix over scripts/coverage_registry.py's REGISTRY -- answers
"what kind of evidence exists for this scenario, and what's the real gap to
the next tier" in one pass, instead of reading compute_status/compute_mode_
statuses/offline_proof_for/fake_broker_proof_for separately per row.

Built 2026-08-13 directly in response to the question "broken down by
simulator-proven, canary-proven, or live-proven (or n/a) -- what's the gap."

Tiers, highest first:
  LIVE       -- a real state='live' node (real capital) produced the evidence
  CANARY     -- dry_run/canary evidence only (coverage_events mode='dry_run',
                or scenario_expectations proof traced to a dry_run/paper-state
                node -- see _proof_node_state)
  PAPER      -- paper-only evidence (paper_trading.py, never touches schwab_client)
  SIMULATOR  -- fake_broker test asserts the real event-logging call fires
                (event-asserted fake_broker_proof), but zero real/dry_run/paper
                evidence exists yet
  UNIT-TEST  -- a plain unit test asserts the event call, but no fake_broker
                test drives it through simulated broker order-placement
  NONE       -- no proof of any kind
  N/A        -- offline_only by design (kernel/parity scripts, no live component)

Usage: .venv/bin/python scripts/coverage_proof_matrix.py [--tier TIER]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db
from scripts.coverage_registry import (
    REGISTRY, compute_status, compute_mode_statuses, offline_proof_for, fake_broker_proof_for,
)

TIER_RANK = {'LIVE': 0, 'CANARY': 1, 'PAPER': 2, 'SIMULATOR': 3, 'UNIT-TEST': 4, 'NONE': 5, 'N/A': 6}


def _node_state(node_id):
    if node_id is None:
        return None
    node = db.get_watch_list_node_by_id(node_id)
    return node.get('state') if node else None


def _gap_for(tier, row, status, detail):
    if tier == 'LIVE':
        return 'None -- real capital has produced this evidence.'
    if tier == 'CANARY':
        return 'Needs a real state=\'live\' node to exercise this, not just dry_run/canary.'
    if tier == 'PAPER':
        return 'Needs a real (dry_run or live) broker-facing trigger -- paper never calls schwab_client.'
    if tier == 'SIMULATOR':
        if row.get('structural_note'):
            return f"STRUCTURAL: {row['structural_note'][:140]}..."
        return 'Simulator-proven only -- needs an organic real/dry_run trigger, or (if truly ' \
               'unreachable) a structural_note added so this reads as structural-gap, not a plain wait.'
    if tier == 'UNIT-TEST':
        return 'No fake_broker test drives this through simulated order placement -- write one, ' \
               'mirroring an existing tests/test_fake_broker_*_scenario.py file for a similar mechanism.'
    if tier == 'NONE':
        return 'No proof of any kind. Needs at minimum a fake_broker test asserting the real ' \
               'log_coverage_event/db-state call fires.'
    return 'N/A by design -- no live component (kernel/parity check).'


def classify(row):
    """Returns (tier, status, detail, gap)."""
    mech = row['check_mechanism']
    status, detail = compute_status(row)

    if mech == 'offline_only':
        return 'N/A', status, detail, _gap_for('N/A', row, status, detail)

    if mech == 'none':
        return 'NONE', status, detail, _gap_for('NONE', row, status, detail)

    if mech == 'open_price_quality_log':
        tier = 'LIVE' if status == 'verified-live' else 'NONE'
        return tier, status, detail, _gap_for(tier, row, status, detail)

    if mech == 'scenario_expectations':
        if status == 'verified-live':
            node_id = row.get('node_id')
            # The row itself carries no node_id (that lives on the real
            # scenario_expectations DB row, not the static REGISTRY dict) --
            # parse it back out of compute_status's own detail string
            # ("TICKER node_id=NNN: ...") rather than re-querying, since
            # that's the exact node the live proof came from.
            import re
            m = re.search(r'node_id=(\d+)', detail)
            node_id = int(m.group(1)) if m else None
            state = _node_state(node_id)
            tier = 'LIVE' if state == 'live' else 'CANARY' if state is not None else 'LIVE'
            return tier, status, detail, _gap_for(tier, row, status, detail)
        if status in ('deviation-unexplained',):
            return 'NONE', status, detail, 'Real, unresolved deviation -- explain or fix, not a proof gap.'
        return 'NONE', status, detail, _gap_for('NONE', row, status, detail)

    # mech == 'coverage_events'
    modes = compute_mode_statuses(row)
    if modes.get('live', (None,))[0] == 'verified':
        tier = 'LIVE'
    elif modes.get('dry_run', (None,))[0] == 'verified':
        tier = 'CANARY'
    elif modes.get('paper', (None,))[0] == 'verified':
        tier = 'PAPER'
    else:
        fv = fake_broker_proof_for(row['scenario_key'])[0]
        op = offline_proof_for(row['scenario_key'])[0]
        if status == 'structural-gap':
            tier = 'SIMULATOR' if fv == 'event-asserted' else 'UNIT-TEST' if op == 'event-asserted' else 'NONE'
        elif fv == 'event-asserted':
            tier = 'SIMULATOR'
        elif op == 'event-asserted':
            tier = 'UNIT-TEST'
        else:
            tier = 'NONE'
    return tier, status, detail, _gap_for(tier, row, status, detail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tier', choices=list(TIER_RANK), help='Filter to one tier only')
    args = ap.parse_args()

    rows = []
    for row in REGISTRY:
        tier, status, detail, gap = classify(row)
        rows.append((tier, row['id'], status, detail, gap))

    rows.sort(key=lambda r: (TIER_RANK[r[0]], r[1]))

    counts = {}
    for tier, *_ in rows:
        counts[tier] = counts.get(tier, 0) + 1

    print(f"Proof-tier matrix ({len(rows)} rows): "
          + ", ".join(f"{t}={counts.get(t, 0)}" for t in TIER_RANK))
    print()
    for tier, id_, status, detail, gap in rows:
        if args.tier and tier != args.tier:
            continue
        print(f"[{tier:9s}] {id_:42s} status={status}")
        print(f"            evidence: {detail[:140]}")
        print(f"            gap:      {gap[:140]}")


if __name__ == '__main__':
    main()
