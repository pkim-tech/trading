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

Params-diff (added 2026-08-12, after GDXU/DFEN/KORU's real z_score_threshold=2.0
vs. linked-candidate z=1.0/1.5/1.5 mismatch went undetected by this script's
original link-existence-only report): for every linked node, the 8 real
promotion axes (window/z/fixed_sl/arm-or-take_profit/trail_buy_pct/
trail_sell_pct/max_hold_hours/entry_timing) are diffed against the candidate
row and any divergence is flagged inline with MISMATCH.

Usage: .venv/bin/python scripts/node_candidate_trace.py [--unlinked-only]
"""
import argparse
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import signals_db as db

CAND_DB = Path(__file__).parent.parent / 'cache' / 'research' / 'trading_universe.db'

# (watch_list column-getter, candidate_nodes column) pairs for the 8 real
# promotion axes. arm/take_profit is strategy-dependent -- mirrors
# signals_db._tp_or_arm_pct exactly, since that's the real function every
# live SL/arm/exit check actually reads.
_DIFF_FIELDS = [
    ('window', lambda n: n['window'], 'window'),
    ('z', lambda n: n['z_score_threshold'], 'z'),
    ('fixed_sl', lambda n: n['fixed_sl'], 'fixed_sl'),
    ('arm/tp', lambda n: db._tp_or_arm_pct(n), 'arm_pct'),
    ('trail_buy_pct', lambda n: n['trail_buy_pct'], 'trail_buy_pct'),
    ('trail_sell_pct', lambda n: n['trail_sell_pct'], 'trail_sell_pct'),
    ('max_hold_hours', lambda n: n['max_hold_hours'], 'max_hold_hours'),
    ('entry_timing', lambda n: n['entry_timing'], 'entry_timing'),
]


def _values_match(live_val, cand_val):
    if live_val is None or cand_val is None:
        return live_val == cand_val
    if isinstance(live_val, (int, float)) and isinstance(cand_val, (int, float)):
        return math.isclose(live_val, cand_val, rel_tol=1e-6, abs_tol=1e-6)
    return live_val == cand_val


def diff_node_vs_candidate(node, cand):
    """Returns a list of (field_name, live_val, cand_val) tuples for every
    mismatching axis. Empty list means fully matched."""
    mismatches = []
    for field_name, live_getter, cand_col in _DIFF_FIELDS:
        live_val = live_getter(node)
        cand_val = cand[cand_col]
        if not _values_match(live_val, cand_val):
            mismatches.append((field_name, live_val, cand_val))
    return mismatches


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
                "SELECT id, robust_alpha, trades, sweep_run_id, window, z, fixed_sl, "
                "arm_pct, trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing "
                "FROM candidate_nodes WHERE id=?",
                (link['candidate_node_id'],)).fetchone()
            if not cand:
                cand_desc = f"id={link['candidate_node_id']} (row not found -- stale reference)"
                mismatches = []
            else:
                mismatches = diff_node_vs_candidate(n, cand)
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
            flag = f"  *** {len(mismatches)} MISMATCH ***" if mismatches else ""
            print(f"{n['ticker']:7} {n['account'] or '-':10} {n['state']:8} {n['id']:>6}  "
                  f"[{link['role']}] {cand_desc}  (linked {link['linked_at']}){flag}")
            for field_name, live_val, cand_val in mismatches:
                print(f"{'':7} {'':10} {'':8} {'':>6}      {field_name}: "
                      f"live={live_val!r}  candidate={cand_val!r}")
    print(f"\n{shown} node(s) shown, {sum(1 for n in nodes if n['id'] not in links_by_wl)} of "
          f"{len(nodes)} total real/paper nodes have no recorded link.")


if __name__ == '__main__':
    main()
