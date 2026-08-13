"""Adds scenario_expectations rows for the 12 new FAS/FAZ nodes built by
scripts/build_fas_faz_canary_nodes.py (2026-08-13), alongside (not replacing) the 13 existing
rows for the old 11-ticker canary design (DIA/FAZ(old)/IVV/IWM/JDST/QID/QQQ/SDOW/SPXU/TWM/XLF).

2026-08-13, paired-review fix: an earlier version of this script deactivated the 13 old rows
(active=0), which directly contradicted build_fas_faz_canary_nodes.py's own stated plan ("old
11-ticker nodes ... keep running in parallel until the new FAS/FAZ nodes have organically
fired and been confirmed correct") -- deactivating their scenario_expectations meant the old
nodes got ZERO daily checking during exactly the window they're meant to serve as the
fallback/comparison. Now both old and new rows stay active=1 simultaneously. This is only
safe because of a second same-day fix: coverage_check.py's trade_lifecycle lookups now
disambiguate by wl_id (node_id), not just strategy/version/window/account -- without that
fix, having 5 same-shaped FAS canary nodes AND the old rows all active at once would let
scenarios cross-satisfy/cross-fail each other even worse than the single-side version did.

canary_bull_bear_pair is repointed (user-confirmed 2026-08-13) onto the same FAS/FAZ node pair
as canary_time_exit, rather than JNUG/JDST. CAVEAT (paired review, same day): this does NOT
implement a real FAS-vs-FAZ inverse-price check -- it's the identical trade_lifecycle
exit-reason check as canary_time_exit on the same node, so expected_frequency is set to
'informational' here too (was 'daily', carried over verbatim from the old JNUG/JDST rows --
wrong on the new shared node, since 'daily' would mint a false ticket on any day the node
doesn't complete a same-day round trip, the exact case 'informational' exists to allow).
A real pairwise check is unbuilt; see docs/backlog_cache.md.

Run once, after build_fas_faz_canary_nodes.py: .venv/bin/python scripts/repoint_canary_scenario_expectations.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

# (scenario_key, expected_frequency, check_params, [FAS wl_id, FAZ wl_id])
NEW_ROWS = [
    ('canary_full_lifecycle', 'daily', {"expect_exit_reason": ["TRAIL"]}, [212, 213]),
    ('canary_early_sl', 'daily', {"expect_exit_reason": ["SL"]}, [214, 215]),
    ('canary_pinned_entry', 'informational', {"expect_exit_reason": ["TRAIL"]}, [222, 223]),
    ('canary_overnight_carry', 'daily', {"expect_pending_carryover": True}, [216, 217]),
    ('canary_market_buy_exit', 'daily', {"expect_exit_reason": ["SL", "TIME", "TRAIL"]}, [218, 219]),
    ('canary_time_exit', 'informational', {"expect_exit_reason": ["TIME"]}, [220, 221]),
    ('canary_bull_bear_pair', 'informational', {"expect_exit_reason": ["SL", "TIME", "TRAIL"]}, [220, 221]),
]

TICKER_BY_WLID = {212: 'FAS', 213: 'FAZ', 214: 'FAS', 215: 'FAZ', 216: 'FAS', 217: 'FAZ',
                  218: 'FAS', 219: 'FAZ', 220: 'FAS', 221: 'FAZ', 222: 'FAS', 223: 'FAZ'}

import json

with db._conn() as c:
    reactivated = c.execute(
        "UPDATE scenario_expectations SET active=1, updated_at=datetime('now') "
        "WHERE id IN (7,8,9,10,11,12,29,30,31,32,34,35,36) AND active=0"
    ).rowcount
    c.commit()
print(f"Reactivated {reactivated} old scenario_expectations row(s) (now running in parallel with the new FAS/FAZ rows).")

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
