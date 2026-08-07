"""Adds six synthetic canary nodes to the active watchlist -- built 2026-07-22
after a real stale-price bug (HIBL paper trade) went unnoticed with no way to
tell whether the daemon's day-to-day polling/fill/arm/exit pipeline was
actually working. Parameters are deliberately extreme so each canary should
complete its full expected lifecycle every trading day; if it doesn't, that's
the alert -- something in the polling/caching/timing pipeline broke.

IVV/QQQ/IWM/DIA/VOO/XLF chosen because they're liquid, already have cached
data, and aren't used by any real watchlist/research node -- paper-trading
dedup is ticker-only, so sharing a ticker with a real node would collide.
(Originally SPY instead of IVV -- renamed once SPY became a real soxl_test
live node, for exactly this collision reason.)

`starting_notional=10000` (not the original 500): at SPY/QQQ/etc's real
price (~$400-700+), 500 sizes to 0 shares (`int(500 // price)`) and the
canary would silently never fill -- caught by Opus review 2026-07-22.

`_scan_pinned_exit_arm` blind spot (accepted, not closed here): it only ever
reads real (non-paper) `open_positions`, so no canary -- however designed --
can exercise it; canary exit-arm timing only covers `check_paper_sells`'
bar-close path. The pinned-scan-specific gap is left to the `live_sim.py`
harness (which can call `_scan_pinned_exit_arm` directly against synthetic
positions), not canaries.

`state='live', account='ira'` (not the original `mode='research'`) -- matches the later
promotion to a real daily dry_run proof-of-life test (`ira` stays `dry_run=True`, so this
never places a real order). Restored 2026-07-27 after the original 6 nodes were
accidentally deleted alongside unrelated soxl_test scratch nodes during a live-watchlist
cleanup -- re-running this script gives new node_ids, so scenario_expectations.node_id
needs relinking by ticker afterward (see the 2026-07-27 restoration in docs/backlog_cache.md).

Run once: python scripts/add_canary_nodes.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

WATCHLIST_ID = db.get_active_watchlist_id()

# Canary A: full happy-path -- BUY -> bounce-fill -> arm -> trailing-sell,
# all same day. SL=30% is practically unreachable; everything else hair-trigger.
db.add_node(
    ticker='IVV', strategy='TrailingBothZScoreBreakout', version='canary',
    window=5, take_profit=0.1, stop_loss=0, max_hold_hours=48,
    label='CANARY-full-lifecycle', z_score_threshold=0.1,
    watchlist_id=WATCHLIST_ID, state='live', account='ira',
    trail_buy_pct=0.1, trail_pct=0.1, entry_timing='close',
    starting_notional=10000, fixed_sl_override=30,
)

# Canary B: early-SL path -- BUY -> bounce-fill -> immediate SL, same day.
# Arm=10% is practically unreachable; SL=0.1% is hair-trigger.
db.add_node(
    ticker='QQQ', strategy='TrailingBothZScoreBreakout', version='canary',
    window=5, take_profit=10, stop_loss=0, max_hold_hours=48,
    label='CANARY-early-SL', z_score_threshold=0.1,
    watchlist_id=WATCHLIST_ID, state='live', account='ira',
    trail_buy_pct=0.1, trail_pct=0.1, entry_timing='close',
    starting_notional=10000, fixed_sl_override=0.1,
)

# Canary C: same shape as A but entry_timing='open_check' -- exercises
# _scan_pinned_entry + open_price_quality_log (the pinned intrabar open scan),
# distinct from A's bar-close-only entry path.
db.add_node(
    ticker='IWM', strategy='TrailingBothZScoreBreakout', version='canary',
    window=5, take_profit=0.1, stop_loss=0, max_hold_hours=48,
    label='CANARY-pinned-entry', z_score_threshold=0.1,
    watchlist_id=WATCHLIST_ID, state='live', account='ira',
    trail_buy_pct=0.1, trail_pct=0.1, entry_timing='open_check',
    starting_notional=10000, fixed_sl_override=30,
)

# Canary D: large trail_buy_pct (5%) so the bounce-fill is unlikely to
# complete same day -- a pending trailing-buy carried overnight into the next
# session's open is a direct daily regression check for the 2026-07-22
# stale-cache fix (_current_price's market-open staleness guard).
db.add_node(
    ticker='DIA', strategy='TrailingBothZScoreBreakout', version='canary',
    window=5, take_profit=0.1, stop_loss=0, max_hold_hours=48,
    label='CANARY-overnight-carry', z_score_threshold=0.1,
    watchlist_id=WATCHLIST_ID, state='live', account='ira',
    trail_buy_pct=5.0, trail_pct=0.1, entry_timing='close',
    starting_notional=10000, fixed_sl_override=30,
)

# Canary E: TrailingExitZScoreBreakout (not TrailingBoth) -- _is_trailing_buy
# routes on the strategy's axis schema, so only a real TrailingExit node
# reaches paper_trading.start_paper_market_buy (immediate market buy, no
# pending_buys row) instead of the bounce-fill path A-D exercise.
db.add_node(
    ticker='VOO', strategy='TrailingExitZScoreBreakout', version='canary',
    window=5, take_profit=0.1, stop_loss=0, max_hold_hours=24,
    label='CANARY-market-buy-exit', z_score_threshold=0.1,
    watchlist_id=WATCHLIST_ID, state='live', account='ira',
    trail_pct=0.1, entry_timing='close',
    starting_notional=10000, fixed_sl_override=90,
)

# Canary F: arm (take_profit) and SL both practically unreachable, tiny
# max_hold_hours=2 -- the only exit path left open is TIME.
db.add_node(
    ticker='XLF', strategy='TrailingBothZScoreBreakout', version='canary',
    window=5, take_profit=50, stop_loss=0, max_hold_hours=2,
    label='CANARY-time-exit', z_score_threshold=0.1,
    watchlist_id=WATCHLIST_ID, state='live', account='ira',
    trail_buy_pct=0.1, trail_pct=0.1, entry_timing='close',
    starting_notional=10000, fixed_sl_override=50,
)

print("Canary nodes added to watchlist_id=%d:" % WATCHLIST_ID)
for n in db.get_watchlist(WATCHLIST_ID):
    if n.get('version') == 'canary':
        print(f"  {n['ticker']:5s} {n['label']:26s} z_thresh={n['z_score_threshold']} "
              f"trail_buy={n['trail_buy_pct']} arm={n['arm_sell_pct']} "
              f"trail_sell={n['trail_sell_pct']} fixed_sl={n['fixed_sl']} "
              f"entry={n['entry_timing']}")
