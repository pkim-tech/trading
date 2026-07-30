"""Seeds signals_db.staged_test_config with a 'baseline_config' snapshot for
every mode='live' watch_list node that doesn't already have a staged_test_config
row -- generalizes the existing staged-test-role config-diff mechanism
(previously only covering the 3 deliberately-designed test nodes SH/RETL/GDXU)
to every live node, so a daily drift check
(signals_invariants.check_staged_config_matches_expected) can answer "is this
node still configured the way it was committed?" for the whole live watchlist,
not just the 3 explicit test roles. Built 2026-07-30 per user request: "are we
prepared for today?" should cover all live tickers, not just canaries.

Deliberately NOT idempotent in the usual "safe to rerun" sense: skips any node
that already has a staged_test_config row (of any scenario_role) rather than
overwriting it -- rerunning would otherwise silently adopt a since-drifted live
value as the new "expected" baseline, defeating the whole point of the drift
check. To intentionally re-baseline a node after a deliberate config change,
clear its row first via signals_db.clear_staged_test_config(wl_id), then rerun.

Run once (or after adding a new live node): .venv/bin/python scripts/seed_baseline_config.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

FIELDS = ('arm_sell_pct', 'fixed_sl', 'trail_sell_pct', 'max_hold_hours',
          'trail_buy_pct', 'starting_notional')

if __name__ == '__main__':
    db.ensure_tables()
    already_staged = {s['wl_id'] for s in db.get_staged_test_configs()}
    seeded = 0
    for node in db.get_watchlist():
        if node['mode'] != 'live' or node['id'] in already_staged:
            continue
        expected_config = {f: node[f] for f in FIELDS if node.get(f) is not None}
        db.set_staged_test_config(
            node['id'], node['ticker'], 'baseline_config', expected_config,
            notes="Snapshotted as the committed/intended config 2026-07-30 -- "
                  "generalizing the staged-test drift-check pattern to the full "
                  "live watchlist, not just the 3 deliberately-designed test roles.",
        )
        print(f"seeded {node['ticker']:6s} wl_id={node['id']}  {expected_config}")
        seeded += 1
    print(f"\n{seeded} baseline_config row(s) seeded (skipped {len(already_staged)} already-staged node(s)).")
