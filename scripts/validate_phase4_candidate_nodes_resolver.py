"""Validates scripts/phase4_candidate_nodes_resolver.derive_phase25_candidates_from_
candidate_nodes against run_optimization_sweep.derive_phase25_candidates_ground_truth's
real backtest_cache-backed output, per Task #1's own validation sequence (docs/
backlog_cache.md "Found 2026-08-29 -- Phase 4/5's candidate resolution").

Step 1: Campaign A (v6-massive-w2021-08-23_2026-08-21, backtest_cache-backed) -- compares
the candidate_nodes-sourced set/order against the real derive_phase25_candidates_ground_
truth call, per (strategy, fixed_sl) scope.
Step 2 (--campaign-c): Campaign C (bench-inmemory-v6-massive-w2021-08-23_2026-08-21,
window=15, in-memory-only, zero backtest_cache rows) -- candidate_nodes-sourced only,
since the old function structurally can't see it; reports what it finds instead of
diffing against anything.

Usage:
  .venv/bin/python scripts/validate_phase4_candidate_nodes_resolver.py
  .venv/bin/python scripts/validate_phase4_candidate_nodes_resolver.py --campaign-c
"""
import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(ROOT))

from run_optimization_sweep import DB_PATH, derive_phase25_candidates_ground_truth
from phase4_candidate_nodes_resolver import derive_phase25_candidates_from_candidate_nodes
from prune_backtest_cache_ground_truth import _hp_for_strategy

TICKER = "SOXL"
CAMPAIGN_A_VERSION = "v6-massive-w2021-08-23_2026-08-21"
CAMPAIGN_C_VERSION = "bench-inmemory-v6-massive-w2021-08-23_2026-08-21"


def _key(c):
    return (c['window'], c['z_score_threshold'], c['take_profit'], c['stop_loss'],
            c['max_hold_hours'], c['tpct'])


def _scopes_for_version(conn, ticker, version):
    rows = conn.execute(
        "SELECT DISTINCT strategy, fixed_sl, entry_timing FROM candidate_nodes "
        "WHERE ticker=? AND version=?", (ticker, version)).fetchall()
    return sorted(rows)


def validate_campaign_a():
    with sqlite3.connect(DB_PATH) as conn:
        scopes = _scopes_for_version(conn, TICKER, CAMPAIGN_A_VERSION)
    print(f"Campaign A ({CAMPAIGN_A_VERSION}): {len(scopes)} scope(s) found in candidate_nodes")

    total_old, total_new, total_mismatch = 0, 0, 0
    for strategy, fixed_sl, entry_timing in scopes:
        hp = _hp_for_strategy(strategy)
        old = derive_phase25_candidates_ground_truth(
            TICKER, strategy, CAMPAIGN_A_VERSION, hp, fixed_sl=fixed_sl, entry_timing=entry_timing)
        new = derive_phase25_candidates_from_candidate_nodes(
            TICKER, strategy, CAMPAIGN_A_VERSION, fixed_sl=fixed_sl, entry_timing=entry_timing)
        old_keys = [_key(c) for c in old]
        new_keys = [_key(c) for c in new]
        total_old += len(old_keys)
        total_new += len(new_keys)
        set_match = set(old_keys) == set(new_keys)
        order_match = old_keys == new_keys
        status = "OK (set+order)" if order_match else ("SET-MATCH, order differs" if set_match else "MISMATCH")
        if not set_match:
            total_mismatch += 1
        print(f"  {strategy} fixed_sl={fixed_sl} entry_timing={entry_timing}: "
              f"old={len(old_keys)} new={len(new_keys)} -> {status}")
        if not set_match:
            missing = set(old_keys) - set(new_keys)
            extra = set(new_keys) - set(old_keys)
            if missing:
                print(f"    missing from new: {missing}")
            if extra:
                print(f"    extra in new: {extra}")

    print(f"\nTotals: old={total_old} new={total_new} scopes_with_set_mismatch={total_mismatch}")
    return total_mismatch == 0


def validate_campaign_c():
    with sqlite3.connect(DB_PATH) as conn:
        scopes = _scopes_for_version(conn, TICKER, CAMPAIGN_C_VERSION)
    print(f"\nCampaign C ({CAMPAIGN_C_VERSION}): {len(scopes)} scope(s) found in candidate_nodes")
    print("Filtering to window=15 -- this version string aliases TWO unrelated batches "
          "(a window=[10,20] batch from 2026-08-27/28, and the real window=15 batch from "
          "2026-08-29 this task exists to cover); see phase4_candidate_nodes_resolver.py's "
          "`window` param docstring.")
    total = 0
    for strategy, fixed_sl, entry_timing in scopes:
        new = derive_phase25_candidates_from_candidate_nodes(
            TICKER, strategy, CAMPAIGN_C_VERSION, fixed_sl=fixed_sl, entry_timing=entry_timing,
            window=15)
        if not new:
            continue
        total += len(new)
        print(f"  {strategy} fixed_sl={fixed_sl} entry_timing={entry_timing}: {len(new)} candidates resolved")
        for c in new[:3]:
            print(f"    island(TP={c['island_tp']} SL={c['island_sl']}) TP={c['take_profit']} "
                  f"SL={c['stop_loss']} hold={c['max_hold_hours']}h w={c['window']} "
                  f"z={c['z_score_threshold']} tpct={c['tpct']} robust_alpha={c['robust_alpha']:.2f}")
    print(f"\nCampaign C total candidates resolved: {total}")
    return total > 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign-c", action="store_true", help="also validate Campaign C (in-memory pipeline)")
    args = ap.parse_args()

    ok_a = validate_campaign_a()
    print(f"\nCampaign A validation: {'PASS' if ok_a else 'FAIL'}")

    if args.campaign_c:
        ok_c = validate_campaign_c()
        print(f"Campaign C validation: {'PASS (candidates resolved)' if ok_c else 'FAIL (no candidates found)'}")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
