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

--merge-new-fields: for every EXISTING 'baseline_config' row, adds any FIELDS
key not already present in its expected_config (from the node's current real
value) -- safe to rerun repeatedly, never touches an already-tracked field's
value (so it can't silently adopt a drifted value the way a full re-baseline
would). Built 2026-08-07 after the FIELDS tuple gained 10 overlay columns and
the committed script itself had no path to apply them to the 24 rows already
staged before that change -- previously required an ad-hoc one-off backfill
script; this makes FIELDS gaining a column self-applying going forward.

Run once (or after adding a new live/dry_run node): .venv/bin/python scripts/seed_baseline_config.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

FIELDS = ('arm_sell_pct', 'fixed_sl', 'trail_sell_pct', 'max_hold_hours',
          'trail_buy_pct', 'starting_notional',
          # window/z_score_threshold added 2026-08-11 -- found live on GDXU
          # (wl_id=208): its watch_list_candidate_link correctly pointed to
          # candidate_node_id=120 (z=1.0), but the deployed live node was
          # actually running z=2.0 -- every OTHER field matched, so this drift
          # check reported clean the whole time. These two aren't "settings"
          # the way arm/SL/trail are -- they define WHICH backtested config a
          # node structurally IS, so a drift here means the live node isn't a
          # tweaked version of the validated candidate, it's an entirely
          # different, unvalidated one. Previously omitted because the
          # original 2026-07-30 FIELDS list was scoped around risk/execution
          # parameters someone might hand-edit, not node identity -- never
          # revisited when watch_list_candidate_link (which DOES catch this,
          # but only if someone runs node_candidate_trace.py and checks by
          # eye) was built for a related but different purpose.
          'window', 'z_score_threshold',
          # Overlay params added 2026-08-06 -- previously untracked here, so a
          # silent drift in any of these (accidental edit, migration
          # side-effect, stale manual patch) rendered as a false "matches
          # committed baseline" ✓ in the daily phased monitors report.
          # None-valued override fields (drought_sl_pct_override etc., NULL
          # when the overlay is off) are excluded below same as any other
          # field -- _config_field_mismatch reports actual=None as drift
          # unconditionally, so baselining an expected None here would
          # false-positive on every run for a node that's correctly off.
          'drought_overlay_enabled', 'drought_confirm_days', 'drought_vol_gate',
          'drought_sl_pct_override', 'drought_arm_pct_override', 'drought_trail_pct_override',
          'addon_enabled', 'skim_enabled', 'skim_step', 'skim_frac')

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--merge-new-fields", action="store_true",
                     help="add any FIELDS key missing from an existing baseline_config row's "
                          "expected_config, from the node's current value -- never overwrites "
                          "an already-tracked field")
    args = ap.parse_args()

    db.ensure_tables()
    staged = {s['wl_id']: s for s in db.get_staged_test_configs()}

    if args.merge_new_fields:
        updated = 0
        for wl_id, row in staged.items():
            if row['scenario_role'] != 'baseline_config':
                continue
            node = db.get_watch_list_node_by_id(wl_id)
            if node is None:
                continue
            merged = dict(row['expected_config'])
            added = {}
            for f in FIELDS:
                if f not in merged and node.get(f) is not None:
                    merged[f] = node[f]
                    added[f] = node[f]
            if added:
                db.set_staged_test_config(wl_id, row['ticker'], row['scenario_role'], merged, notes=row['notes'])
                print(f"  {row['ticker']:6s} wl_id={wl_id:4d}  added: {added}")
                updated += 1
        print(f"\n{updated} row(s) updated with newly-tracked FIELDS.")
        return

    # state != 'paper' (not just 'live') -- covers dry_run canary/overlay
    # nodes too, matching the doc's original intent ("the whole live
    # watchlist," where "live" pre-refactor meant mode='live', a superset
    # including what's now split into 'dry_run'/'live'). Narrower to
    # state=='live' only would silently skip newly-created dry_run overlay
    # nodes going forward -- found by paired Opus review.
    already_staged = set(staged.keys())
    seeded = 0
    for node in db.get_watchlist():
        if node['state'] == 'paper' or node['id'] in already_staged:
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


if __name__ == '__main__':
    main()
