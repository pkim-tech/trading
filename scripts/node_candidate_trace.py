"""Reports, for every real (live/dry_run/paper) watch_list node, whether it has a
recorded watch_list_candidate_link -- a real, explicit link to the candidate_nodes
row (cache/research/trading_universe.db) it was promoted from, set via
signals_db.set_candidate_link. Built 2026-08-11 after a session of the agent
guessing this link by matching params (missed axes, wrong column for
TrailingExit strategies, matched against the wrong reference row) instead of
reading a real recorded decision -- this script exists so "which candidate does
this node trace to" never has to be re-derived by hand or by param-matching again.

An unlinked node is not necessarily wrong -- it may just predate this table
(2026-08-11) or never have gone through a documented candidate-selection
process. It IS the list of nodes worth asking the user about directly, instead
of guessing.

Usage: .venv/bin/python scripts/node_candidate_trace.py [--unlinked-only]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import signals_db as db

CAND_DB = Path(__file__).parent.parent / 'cache' / 'research' / 'trading_universe.db'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--unlinked-only', action='store_true',
                     help="only show nodes with no recorded candidate link")
    args = ap.parse_args()

    cand_conn = sqlite3.connect(CAND_DB)
    cand_conn.row_factory = sqlite3.Row

    links_by_wl = {}
    for link in db.get_candidate_links():
        links_by_wl.setdefault(link['wl_id'], []).append(link)

    nodes = [n for n in db.get_watchlist() if n['state'] in ('live', 'dry_run', 'paper')]
    nodes.sort(key=lambda n: (n['ticker'], n['account'] or '', n['id']))

    print(f"{'Ticker':7} {'Acct':10} {'State':8} {'wl_id':>6}  Candidate link")
    print("-" * 80)
    shown = 0
    for n in nodes:
        links = links_by_wl.get(n['id'])
        if args.unlinked_only and links:
            continue
        shown += 1
        if not links:
            print(f"{n['ticker']:7} {n['account'] or '-':10} {n['state']:8} {n['id']:>6}  NONE -- no recorded selection")
            continue
        for link in links:
            cand = cand_conn.execute(
                "SELECT id, robust_alpha, trades, sweep_run_id FROM candidate_nodes WHERE id=?",
                (link['candidate_node_id'],)).fetchone()
            if not cand:
                cand_desc = f"id={link['candidate_node_id']} (row not found -- stale reference)"
            else:
                cand_desc = f"id={cand['id']} alpha={cand['robust_alpha']:.1f} trades={cand['trades']}"
                # sweep_run_id (2026-08-11): NULL for any candidate registered
                # before this column existed, or sourced from a pre-existing
                # backtest_cache row computed before sweep_run_id was stamped --
                # both real, expected gaps, not a bug -- see run_optimization_
                # sweep.py's init_idempotent_db "no backfill" note.
                if cand['sweep_run_id'] is not None:
                    run = cand_conn.execute(
                        "SELECT started_at, git_commit, kernel_dirty FROM sweep_runs WHERE id=?",
                        (cand['sweep_run_id'],)).fetchone()
                    if run:
                        commit_short = (run['git_commit'] or '?')[:8]
                        dirty_flag = ' DIRTY' if run['kernel_dirty'] else ''
                        cand_desc += f"  [sweep_run={cand['sweep_run_id']} {run['started_at']} @{commit_short}{dirty_flag}]"
                    else:
                        cand_desc += f"  [sweep_run={cand['sweep_run_id']} (row not found)]"
                else:
                    cand_desc += "  [no sweep_run recorded]"
            print(f"{n['ticker']:7} {n['account'] or '-':10} {n['state']:8} {n['id']:>6}  "
                  f"[{link['role']}] {cand_desc}  (linked {link['linked_at']})")
    print(f"\n{shown} node(s) shown, {sum(1 for n in nodes if n['id'] not in links_by_wl)} of "
          f"{len(nodes)} total real/paper nodes have no recorded link.")


if __name__ == '__main__':
    main()
