"""Capital-scaling readiness gate -- the explicit "are we ready for 4x/10x
real money" checklist, built 2026-08-13.

Answers a narrower question than the full Accountability Grid: not "is
everything proven" (most rows never need to be -- see coverage_proof_matrix.py
for the full breakdown), but "are the specific mechanisms whose blast radius
scales with position size proven against the REAL broker." Filter used to
build GATE_ROWS: a row qualifies only if (a) it's not yet LIVE-proven, AND
(b) its code path directly moves real order notional in a way that gets
WORSE, not just more frequent, as position size grows. A fixed-cost safety
guard (kill switch, duplicate-order block) costs about the same per incident
whether nodes are $500 or $5,000 -- not a scaling gate. A mechanism that
determines how much money moves (add-on's notional-doubling, drought
handoff's real position transitions) is.

Result: 13 rows, all currently SIMULATOR-tier (fake_broker-proven, zero real-
broker proof) as of 2026-08-13 -- 9 add-on rows ($18k real brokerage capital
already has addon_enabled=1) + 3 drought_handoff rows + drought_entry (its
own once-per-gap dedup guard, distinct from drought_entry_placement which IS
live-proven). Full reasoning: docs/conversation_summary.md's 2026-08-13 entry.

This is meant to be re-run after every session that touches the relevant
code (signals_notify.py, schwab_client.py, schwab_safety.py, paper_trading.py,
active_signals.py) -- not a one-shot read. Each run is logged to
capital_scaling_gate_log (git commit + per-row tier + cleared/total), and
diffed against the last logged run so regressions/progress are visible
without re-deriving the list from scratch.

Usage: .venv/bin/python scripts/capital_scaling_gate.py
"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db
from scripts.coverage_registry import REGISTRY
from scripts.coverage_proof_matrix import classify

# Explicit, reviewable list -- deliberately not re-derived by a heuristic
# each run, since "does this scale with $" is a judgment call that should be
# visible and revisited by a human, not silently redefined by future code.
GATE_ROWS = {
    'addon_entry_fill': "Add-on entry fill -- doubles real notional on trigger.",
    'addon_entry_placement': "Add-on entry order placement -- same.",
    'addon_exit_fill': "Add-on exit fill -- unwinds the doubled notional.",
    'addon_exit_placement': "Add-on exit order placement -- same.",
    'addon_double_buy_exemption': "Add-on's is_addon_leg exemption from the duplicate-BUY guard -- "
                                   "a bug here could either double-block a legit add-on or let a real "
                                   "double-buy through.",
    'addon_buying_power_check': "Add-on's real leveraged buying-power check -- undersizing/oversizing "
                                 "this scales directly with real margin risk.",
    'addon_second_ticker_buy_allowed': "Add-on BUY alongside another ticker's resting order -- a false "
                                        "block/allow here misjudges real available capital.",
    'addon_leg_independent_sl_fill_detection': "Add-on leg's own protective stop filling independently -- "
                                                 "a miss here leaves a real doubled position unprotected.",
    'addon_leg_reconciliation': "Orphaned/abandoned add-on leg reconciliation -- a miss here leaves real "
                                 "doubled notional untracked.",
    'drought_handoff_cancel': "Real drought HANDOFF cancelling a resting entry -- mishandled = "
                               "unintended real fill or a stuck order.",
    'drought_handoff_exit_placement': "Real drought HANDOFF exit placement -- mishandled = an unprotected "
                                       "or double-open real position, sized to starting_notional.",
    'drought_handoff_alert_slot_preserved': "Core's real BUY alert not starved by an in-flight HANDOFF -- "
                                             "a miss here silently drops a real entry signal.",
    'drought_entry': "Once-per-gap dedup guard -- a failure causes repeated real entries, cost scales "
                      "with $ per repeat.",
}

_GATE_RELEVANT_FILES = ["signals_notify.py", "schwab_client.py", "schwab_safety.py",
                         "paper_trading.py", "active_signals.py"]


def _git_state():
    """Best-effort (git_commit, dirty) for the gate-relevant files -- mirrors
    run_optimization_sweep._current_kernel_git_state's pattern, scoped to the
    files that actually implement the 13 gated mechanisms instead of the
    kernel files that function scopes to."""
    repo_dir = str(Path(__file__).resolve().parent.parent)
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True,
            text=True, timeout=5, check=True
        ).stdout.strip()
        dirty_out = subprocess.run(
            ["git", "status", "--porcelain", "--"] + _GATE_RELEVANT_FILES,
            cwd=repo_dir, capture_output=True, text=True, timeout=5, check=True
        ).stdout
        return commit, int(bool(dirty_out.strip()))
    except Exception as e:
        print(f"  [warn] could not determine git state: {e}")
        return None, None


def ensure_table():
    with db._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS capital_scaling_gate_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL DEFAULT (datetime('now')),
            git_commit      TEXT,
            files_dirty     INTEGER,
            cleared_count   INTEGER NOT NULL,
            total_count     INTEGER NOT NULL,
            ready           INTEGER NOT NULL,
            row_tiers_json  TEXT NOT NULL
        )""")
        c.commit()


def last_logged_run():
    """Returns (ts, git_commit, {row_id: tier}) for the most recent logged
    run, or None if this is the first time the gate has ever been run."""
    with db._conn() as c:
        row = c.execute(
            "SELECT ts, git_commit, row_tiers_json FROM capital_scaling_gate_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return row['ts'], row['git_commit'], json.loads(row['row_tiers_json'])


def log_run(git_commit, files_dirty, tiers, cleared, total):
    with db._conn() as c:
        c.execute(
            "INSERT INTO capital_scaling_gate_log (git_commit, files_dirty, cleared_count, total_count, "
            "ready, row_tiers_json) VALUES (?, ?, ?, ?, ?, ?)",
            (git_commit, files_dirty, cleared, total, int(cleared == total), json.dumps(tiers)),
        )
        c.commit()


def main():
    ensure_table()
    by_id = {r['id']: r for r in REGISTRY}

    tiers = {}
    for gate_id in GATE_ROWS:
        row = by_id.get(gate_id)
        if row is None:
            print(f"  [warn] gate row '{gate_id}' not found in REGISTRY -- registry drift, check this list")
            continue
        tier, status, detail, _gap = classify(row)
        tiers[gate_id] = tier

    cleared = sum(1 for t in tiers.values() if t == 'LIVE')
    total = len(GATE_ROWS)
    ready = cleared == total

    print(f"=== Capital-scaling gate: {cleared}/{total} live-proven -- "
          f"{'READY' if ready else 'NOT READY'} for a 4x/10x notional increase ===\n")
    for gate_id, rationale in GATE_ROWS.items():
        tier = tiers.get(gate_id, '???')
        mark = '✅' if tier == 'LIVE' else '⬜'
        print(f"  {mark} [{tier:9s}] {gate_id}")
        print(f"      {rationale}")

    prior = last_logged_run()
    print()
    if prior is None:
        print("No prior logged run -- this is the first recorded baseline, nothing to diff yet.")
    else:
        prior_ts, prior_commit, prior_tiers = prior
        changed = [(k, prior_tiers.get(k, '(new)'), v) for k, v in tiers.items() if prior_tiers.get(k) != v]
        if not changed:
            print(f"No change vs the last logged run ({prior_ts}).")
        else:
            print(f"Changed since the last logged run ({prior_ts}):")
            for k, old, new in changed:
                arrow = '📈' if new == 'LIVE' and old != 'LIVE' else '📉' if old == 'LIVE' and new != 'LIVE' else '  '
                print(f"  {arrow} {k:42s} {old} -> {new}")

    git_commit, files_dirty = _git_state()
    log_run(git_commit, files_dirty, tiers, cleared, total)
    print(f"\nLogged (commit={git_commit or '?'}, gate-relevant files dirty={bool(files_dirty)}).")


if __name__ == '__main__':
    main()
