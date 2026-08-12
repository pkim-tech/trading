"""Seeds signals_db.staged_test_config with the real, currently-active live
test roles (SH/RETL/GDXU, 2026-07-29) -- the structured "what should this
node's config be" mapping that replaces re-deriving/re-explaining this by
hand each check-in. Idempotent (set_staged_test_config upserts on
(wl_id, scenario_role) -- widened 2026-08-12 from wl_id alone, so re-running
this after a scenario_role rename leaves the old-named row as a stale orphan
rather than updating it in place; clear it explicitly first if renaming).

Run once (or after any staged-test redesign): .venv/bin/python scripts/seed_staged_test_config.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

ROLES = [
    dict(
        wl_id=135, ticker='SH', scenario_role='time_exit_via_trail',
        expected_config={
            'arm_sell_pct': 0.3, 'fixed_sl': 50, 'trail_sell_pct': 50, 'max_hold_hours': 31,
        },
        notes=("Deliberately armed (arm_sell_pct hair-trigger) -- the position is SUPPOSED to be "
               "trailing=True. trail_sell_pct=50/fixed_sl=50 are both wide/inert-by-design so the "
               "only real exit path is hold-time expiry while armed -- exercises the "
               "exit_forced_by_hold_time fix (strategies.py/signals_notify._attempt_automated_exit_sell, "
               "2026-07-29). max_hold_hours=31 chosen to land the forced exit midday."),
    ),
    dict(
        wl_id=143, ticker='RETL', scenario_role='time_exit_via_sl',
        expected_config={
            'arm_sell_pct': 16.0, 'fixed_sl': 50, 'trail_sell_pct': 1.0, 'max_hold_hours': 11,
        },
        notes=("Deliberately UNARMED -- arm_sell_pct=16% is far from reachable (current price ~$10 "
               "vs entry $9.905), so it must stay on the SL path the whole test. fixed_sl=50% is a "
               "real resting STOP at $4.95 (50% below entry), wide/inert by design so it doesn't "
               "pre-empt TIME. Already proven correct via fake_broker (no code fix needed) -- this "
               "staged run is the first LIVE confirmation. max_hold_hours=11 chosen to land the "
               "forced exit midday."),
    ),
    dict(
        wl_id=108, ticker='GDXU', scenario_role='gap_resize_and_topup',
        expected_config={
            'trail_buy_pct': 1.0, 'starting_notional': 500,
        },
        notes=("Node id 108, labeled 'soxl_ira gap-resize test (safety net #2)'. Real TRAILING BUY "
               "staged 2026-07-29 via stage_live_test_order.py, deliberately undersized (2 shares, "
               "~$155) against the $500 starting_notional target so a real fill triggers a genuine "
               "post_fill_topup (never proven live before, only via fake_broker). Also the live "
               "confirmation target for update_real_pending_buys_running_low (2026-07-29 fix) -- "
               "needs a genuine GAP-UP (not gap-down) to actually exercise the bug's failure shape, "
               "see conversation 2026-07-29 for why gap-down doesn't exercise it."),
    ),
]

if __name__ == '__main__':
    db.ensure_tables()
    for r in ROLES:
        db.set_staged_test_config(r['wl_id'], r['ticker'], r['scenario_role'],
                                   r['expected_config'], notes=r['notes'])
        print(f"seeded {r['ticker']:6s} wl_id={r['wl_id']}  role={r['scenario_role']}")
    print(f"\n{len(ROLES)} staged_test_config rows seeded/updated.")
