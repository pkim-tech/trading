"""Consolidates the 6 canary-letter scenarios (A-F) plus the G bull/bear-pair concept, which
previously spread across 11 different tickers (DIA/FAZ/IVV/IWM/JDST/QID/QQQ/SDOW/SPXU/TWM/XLF,
account='ira'), onto just FAS and FAZ (account='soxl_ira') -- built 2026-08-13 after confirming
the config (z_score_threshold/window), not the specific ticker, is what makes a canary reliable:
XLF/FAZ's z=0.1/window=5 setup fired real TIME exits repeatedly through 2026-08-11 despite
looking 'wired-never-fired' in the Grid (a separate compute_status bug, filed in
docs/backlog_cache.md). FAS (Direxion 3x bull financials) and FAZ (Direxion 3x bear financials)
are a true matched-leverage inverse pair -- user's explicit call, wanted 3x on both sides, not
XLF's 1x/FAZ's 3x mismatch. Confirmed via real cached hourly data (2026-08-13): at
z=0.1/window=5, the entry threshold crosses on 40.5% of bars for FAS and 57.1% for FAZ.

Exit-side params per scenario are carried over unchanged from the real, already-tuned values
on the 6 tickers being replaced (queried directly from the live DB before writing this script)
-- not reinvented from scratch.

The bull/bear-pair scenario (previously JNUG/JDST, two different underlyings) is redefined to
share FAS/FAZ's time-exit node/config instead of a dedicated correlation check -- user confirmed
this consolidation explicitly (2026-08-13), not assumed. Caveat, found by paired review same
day: this does NOT implement an actual FAS-vs-FAZ inverse-price comparison -- it's the same
trade_lifecycle exit-reason check as canary_time_exit, just tagged with a separate
scenario_key/expected_frequency. A real pairwise check is unbuilt; see docs/backlog_cache.md.

Old 11-ticker nodes are NOT retired by this script -- they keep running in parallel until the
new FAS/FAZ nodes have organically fired and been confirmed correct, per the project's standing
"never force/fake a live test" convention (retiring them is a separate, later step).

Run once: .venv/bin/python scripts/build_fas_faz_canary_nodes.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

WATCHLIST_ID = db.get_active_watchlist_id()
ACCOUNT = 'soxl_ira'
STATE = 'dry_run'

# Scenario A: full happy-path lifecycle -- BUY -> bounce-fill -> arm -> trailing-sell.
# Was IVV. arm=0.1 (hair-trigger), fixed_sl=30 (practically unreachable).
for ticker in ('FAS', 'FAZ'):
    db.add_node(
        ticker=ticker, strategy='TrailingBothZScoreBreakout', version='canary',
        window=5, take_profit=0.1, stop_loss=0, max_hold_hours=48,
        label='CANARY-full-lifecycle (was IVV)', z_score_threshold=0.1,
        watchlist_id=WATCHLIST_ID, state=STATE, account=ACCOUNT,
        trail_buy_pct=0.1, trail_pct=0.1, entry_timing='close',
        starting_notional=500, fixed_sl_override=30,
    )

# Scenario B: early-SL path -- BUY -> bounce-fill -> immediate SL, same day.
# Was QQQ. arm=10 (practically unreachable), fixed_sl=0.1 (hair-trigger).
for ticker in ('FAS', 'FAZ'):
    db.add_node(
        ticker=ticker, strategy='TrailingBothZScoreBreakout', version='canary',
        window=5, take_profit=10, stop_loss=0, max_hold_hours=48,
        label='CANARY-early-SL (was QQQ)', z_score_threshold=0.1,
        watchlist_id=WATCHLIST_ID, state=STATE, account=ACCOUNT,
        trail_buy_pct=0.1, trail_pct=0.1, entry_timing='close',
        starting_notional=500, fixed_sl_override=0.1,
    )

# Scenario C: pinned/open_check entry timing. Was IWM. Same arm/SL shape as A
# (practically unreachable) -- entry_timing is the thing under test, not the
# exit path. NOTE: add_node's dedup key is (ticker, strategy, version, window,
# take_profit, stop_loss, max_hold_hours, arm_sell_pct, trail_buy_pct,
# trail_sell_pct, account, paper_role) -- entry_timing and fixed_sl are NOT in
# it, so an otherwise-identical row silently no-ops as a "duplicate" of
# Scenario A instead of inserting. Confirmed this bit us twice (first with
# fixed_sl_override alone, still didn't help since fixed_sl isn't in the key
# either) -- max_hold_hours=47 (not 48) is what actually makes this row
# distinct in the real dedup key, 2026-08-13.
for ticker in ('FAS', 'FAZ'):
    db.add_node(
        ticker=ticker, strategy='TrailingBothZScoreBreakout', version='canary',
        window=5, take_profit=0.1, stop_loss=0, max_hold_hours=47,
        label='CANARY-pinned-entry (was IWM)', z_score_threshold=0.1,
        watchlist_id=WATCHLIST_ID, state=STATE, account=ACCOUNT,
        trail_buy_pct=0.1, trail_pct=0.1, entry_timing='open_check',
        starting_notional=500, fixed_sl_override=30,
    )

# Scenario D: overnight carry -- wide trail_buy_pct (5%) so the trailing-buy fill
# doesn't resolve same day, forcing a real pending_buys carryover. Was DIA.
for ticker in ('FAS', 'FAZ'):
    db.add_node(
        ticker=ticker, strategy='TrailingBothZScoreBreakout', version='canary',
        window=5, take_profit=0.1, stop_loss=0, max_hold_hours=48,
        label='CANARY-overnight-carry (was DIA)', z_score_threshold=0.1,
        watchlist_id=WATCHLIST_ID, state=STATE, account=ACCOUNT,
        trail_buy_pct=5.0, trail_pct=0.1, entry_timing='close',
        starting_notional=500, fixed_sl_override=30,
    )

# Scenario E: market-buy entry mechanism (TrailingExitZScoreBreakout, not
# TrailingBoth) -- was VOO. take_profit=0 mirrors VOO's real value (arm-equivalent
# for this strategy), fixed_sl=90 practically unreachable.
for ticker in ('FAS', 'FAZ'):
    db.add_node(
        ticker=ticker, strategy='TrailingExitZScoreBreakout', version='canary',
        window=5, take_profit=0, stop_loss=0, max_hold_hours=24,
        label='CANARY-market-buy-exit (was VOO)', z_score_threshold=0.1,
        watchlist_id=WATCHLIST_ID, state=STATE, account=ACCOUNT,
        trail_buy_pct=0.0, trail_pct=0.1, entry_timing='close',
        starting_notional=500, fixed_sl_override=90,
    )

# Scenario F: TIME-only forced exit -- arm=50/fixed_sl=50 both practically
# unreachable, max_hold_hours=2 forces the hold-time exit. This is FAZ's
# existing, already-proven-live config, reused as-is; FAS gets the mirror.
# Also carries the redefined bull/bear-pair scenario (G, was JNUG/JDST) --
# same node/config as time-exit, not a real pairwise FAS-vs-FAZ check (see
# module docstring's caveat, added 2026-08-13 after paired review).
for ticker in ('FAS', 'FAZ'):
    db.add_node(
        ticker=ticker, strategy='TrailingBothZScoreBreakout', version='canary',
        window=5, take_profit=50, stop_loss=1, max_hold_hours=2,
        label='CANARY-time-exit + bull-bear-pair (was XLF/FAZ, JNUG/JDST)', z_score_threshold=0.1,
        watchlist_id=WATCHLIST_ID, state=STATE, account=ACCOUNT,
        trail_buy_pct=0.1, trail_pct=0.1, entry_timing='close',
        starting_notional=500, fixed_sl_override=50,
    )

print("Done. Run scripts/status_check.py or query watch_list directly to confirm the new nodes.")
