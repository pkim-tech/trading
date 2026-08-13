"""Writes staged_test_config rows for the 12 FAS/FAZ canary nodes built by
scripts/build_fas_faz_canary_nodes.py (2026-08-13) -- scenario_role matches the
Grid's scenario_key where one exists, so audit_live_test_candidates.py's "grid
relevance" cross-reference works out of the box.

Run once, after build_fas_faz_canary_nodes.py: .venv/bin/python scripts/seed_fas_faz_staged_config.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

# (label substring to find the wl_id pair, scenario_role, expected_config, notes)
#
# expected_config deliberately contains ONLY real numeric watch_list columns
# (arm_sell_pct/take_profit/fixed_sl/trail_buy_pct/trail_sell_pct/max_hold_hours) --
# NOT entry_timing/strategy/revert_to/note. signals_invariants._config_field_mismatch
# can't numerically compare a string field against node.get(field) (float() raises)
# and reports it as a mismatch regardless of whether it actually matches, and a key
# with no matching watch_list column (e.g. 'note') always reads as "field missing"
# -- both are real, pre-existing limitations of that checker (confirmed live,
# 2026-08-13: including entry_timing='close' here, an exact match, still produced a
# false drift violation). Everything non-numeric goes in the free-text `notes`
# param instead, which isn't validated.
SCENARIOS = [
    ('CANARY-full-lifecycle', 'canary_full_lifecycle',
     {'arm_sell_pct': 0.1, 'fixed_sl': 30, 'trail_buy_pct': 0.1, 'trail_sell_pct': 0.1,
      'max_hold_hours': 48},
     "2026-08-13: consolidated from IVV onto FAS/FAZ. Full happy-path lifecycle -- BUY -> "
     "bounce-fill -> arm -> trailing-sell, all same day. fixed_sl=30% practically unreachable, "
     "arm_sell_pct=0.1% hair-trigger. entry_timing='close'. No revert_to -- this IS the "
     "intended permanent config, not a temporary detune."),
    ('CANARY-early-SL', 'canary_early_sl',
     {'arm_sell_pct': 10.0, 'fixed_sl': 0.1, 'trail_buy_pct': 0.1, 'trail_sell_pct': 0.1,
      'max_hold_hours': 48},
     "2026-08-13: consolidated from QQQ onto FAS/FAZ. Early-SL path -- BUY -> bounce-fill -> "
     "immediate SL, same day. arm_sell_pct=10% practically unreachable, fixed_sl=0.1% "
     "hair-trigger. entry_timing='close'."),
    ('CANARY-pinned-entry', 'canary_pinned_entry',
     {'arm_sell_pct': 0.1, 'fixed_sl': 30, 'trail_buy_pct': 0.1, 'trail_sell_pct': 0.1,
      'max_hold_hours': 47},
     "2026-08-13: consolidated from IWM onto FAS/FAZ. entry_timing='open_check' is the thing "
     "under test -- exit shape mirrors the full-lifecycle scenario. max_hold_hours=47 (not 48) "
     "deliberately, to stay distinct from the full-lifecycle row in add_node's dedup key "
     "(entry_timing/fixed_sl are NOT part of that key)."),
    ('CANARY-overnight-carry', 'canary_overnight_carry',
     {'arm_sell_pct': 0.1, 'fixed_sl': 30, 'trail_buy_pct': 5.0, 'trail_sell_pct': 0.1,
      'max_hold_hours': 48},
     "2026-08-13: consolidated from DIA onto FAS/FAZ. trail_buy_pct=5.0% (wide) so the "
     "trailing-buy fill doesn't resolve same day, forcing a real pending_buys overnight "
     "carryover. entry_timing='close'."),
    ('CANARY-market-buy-exit', 'canary_market_buy_exit',
     {'take_profit': 0, 'fixed_sl': 90, 'trail_buy_pct': 0.0, 'trail_sell_pct': 0.1,
      'max_hold_hours': 24},
     "2026-08-13: consolidated from VOO onto FAS/FAZ. Strategy is TrailingExitZScoreBreakout "
     "(market-buy entry, not trailing-buy) -- the entry mechanism itself is the point. "
     "fixed_sl=90% practically unreachable. entry_timing='close'."),
    ('CANARY-time-exit', 'canary_time_exit',
     {'arm_sell_pct': 50, 'fixed_sl': 50, 'trail_buy_pct': 0.1, 'trail_sell_pct': 0.1,
      'max_hold_hours': 2},
     "2026-08-13: FAZ's config is unchanged from its already-proven-live XLF/FAZ setup (real "
     "trade_log TIME exits confirmed through 2026-08-11); FAS gets the mirror. arm/SL both "
     "practically unreachable, max_hold_hours=2 forces the hold-time exit. entry_timing='close'."),
    ('CANARY-time-exit', 'canary_bull_bear_pair',
     {'arm_sell_pct': 50, 'fixed_sl': 50, 'trail_buy_pct': 0.1, 'trail_sell_pct': 0.1,
      'max_hold_hours': 2},
     "2026-08-13: redefined from JNUG/JDST (junior gold miners bull/bear, two different "
     "underlyings) to FAS-vs-FAZ's own inverse relationship (same underlying, matched 3x "
     "leverage) -- user explicitly confirmed this redefinition before implementation. Same "
     "node/config as canary_time_exit (multi-role node, see UNIQUE(wl_id, scenario_role)) -- "
     "this role checks FAS-vs-FAZ inverse price behavior on the SAME underlying, not a "
     "cross-underlying correlation like the old JNUG/JDST design."),
]

with db._conn() as conn:
    rows = conn.execute("SELECT id, ticker, label FROM watch_list WHERE ticker IN ('FAS','FAZ') "
                         "AND account='soxl_ira'").fetchall()

for label_substr, scenario_role, expected_config, notes in SCENARIOS:
    matches = [r for r in rows if label_substr in r['label']]
    if len(matches) != 2:
        print(f"WARNING: expected 2 nodes (FAS+FAZ) for {label_substr!r}, found {len(matches)}")
    for r in matches:
        db.set_staged_test_config(r['id'], r['ticker'], scenario_role, expected_config, notes)
        print(f"  staged_test_config set: wl_id={r['id']} ticker={r['ticker']} role={scenario_role}")

print("Done.")
