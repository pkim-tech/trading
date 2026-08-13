"""Repoints the 13 active canary-letter scenario_expectations rows (previously spread across
11 tickers: DIA/FAZ(old)/IVV/IWM/JDST/QID/QQQ/SDOW/SPXU/TWM/XLF) onto the 12 new FAS/FAZ nodes
built by scripts/build_fas_faz_canary_nodes.py (2026-08-13). Deactivates the old rows (doesn't
delete -- matches project's pause-not-delete convention) and adds new rows for FAS+FAZ per
scenario, preserving each scenario's real check_params/expected_frequency exactly as they were.

canary_bull_bear_pair is redefined (user-confirmed 2026-08-13) to point at the same FAS/FAZ
time-exit node pair, rather than JNUG/JDST -- it now checks FAS-vs-FAZ's own inverse behavior.

Run once, after build_fas_faz_canary_nodes.py: .venv/bin/python scripts/repoint_canary_scenario_expectations.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

# Old active scenario_expectations rows (id, scenario_key) to deactivate.
OLD_IDS = [7, 8, 9, 10, 11, 12, 29, 30, 31, 32, 34, 35, 36]

# (scenario_key, expected_frequency, check_params, [FAS wl_id, FAZ wl_id])
NEW_ROWS = [
    ('canary_full_lifecycle', 'daily', {"expect_exit_reason": ["TRAIL"]}, [212, 213]),
    ('canary_early_sl', 'daily', {"expect_exit_reason": ["SL"]}, [214, 215]),
    ('canary_pinned_entry', 'informational', {"expect_exit_reason": ["TRAIL"]}, [222, 223]),
    ('canary_overnight_carry', 'daily', {"expect_pending_carryover": True}, [216, 217]),
    ('canary_market_buy_exit', 'daily', {"expect_exit_reason": ["SL", "TIME", "TRAIL"]}, [218, 219]),
    ('canary_time_exit', 'informational', {"expect_exit_reason": ["TIME"]}, [220, 221]),
    ('canary_bull_bear_pair', 'daily', {"expect_exit_reason": ["SL", "TIME", "TRAIL"]}, [220, 221]),
]

TICKER_BY_WLID = {212: 'FAS', 213: 'FAZ', 214: 'FAS', 215: 'FAZ', 216: 'FAS', 217: 'FAZ',
                  218: 'FAS', 219: 'FAZ', 220: 'FAS', 221: 'FAZ', 222: 'FAS', 223: 'FAZ'}

import json

with db._conn() as c:
    for old_id in OLD_IDS:
        c.execute("UPDATE scenario_expectations SET active=0, updated_at=datetime('now') WHERE id=?",
                   (old_id,))
    c.commit()
print(f"Deactivated {len(OLD_IDS)} old scenario_expectations rows.")

for scenario_key, freq, check_params, wl_ids in NEW_ROWS:
    for wl_id in wl_ids:
        db.add_scenario_expectation(
            scenario_key=scenario_key,
            expected_outcome=f"See check_params -- {scenario_key} on {TICKER_BY_WLID[wl_id]} (soxl_ira)",
            expected_frequency=freq,
            check_method='trade_lifecycle',
            ticker=TICKER_BY_WLID[wl_id],
            node_id=wl_id,
            mode='live',
            check_params=json.dumps(check_params),
        )
        print(f"  scenario_expectation set: {scenario_key} ticker={TICKER_BY_WLID[wl_id]} node_id={wl_id}")

print("Done.")
