"""One-off: v6 promotion, ETHU + OILU (2026-08-24 late night, planner session).

New tickers (not a v5-node replacement like batch1) -- both cleared the full
watchlist_candidate_checklist.md pass (checks 2/3/6/7/15 real, 1/4/8/11/13 via GT
report, 9/10 out of scope for GT, 5/12 N/A pre-promotion). OILU's check 3 needed a
real fix first (verify_trailing_sell_resolution.py switched from a 60-day yfinance
5-min pull to the cached Massive 1-min/1-sec data, see that script's own docstring) --
before the fix it had zero comparable trailing-sell exits to test against; after, all
33 real historical exits matched, one real outlier fully root-caused (2024-10-01,
same-bar High/Low ordering optimism, confirmed via true 1-second tick data) and
otherwise clean.

Both route to `brokerage` per user's own account-routing rule: OILU's add-on result
(1565.8%) meets the "very significant add-on" trigger; ETHU doesn't trigger either
brokerage rule but user chose brokerage for both this batch.

addon_enabled=1 for both -- add-on has NEVER filled live before this (addon_legs had
zero rows all session, confirmed 2026-08-24), so this promotion is also the first real
live test of the add-on mechanism, not just two new tickers on an existing one.

Params sourced from cache/research/trading_universe.db's candidate_nodes table
(node 1766 ETHU, node 1939 OILU) -- arm_pct/trail_buy_pct/trail_sell_pct already
carry the real GT axis mapping (strategies.resolve_axis_columns), NOT raw
backtest_cache column names.

Run once. Re-running is safe (add_node dedups on the full param tuple).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import signals_db

WATCHLIST_ID = 65
VERSION = "v6-massive-w2021-08-23_2026-08-21"
ENTRY_TIMING = "open_check"
ACCOUNT = "brokerage"
NOTIONAL = 5000.0

# (ticker, candidate_node_id, strategy, window, z, fixed_sl, arm_pct, trail_buy_pct,
#  trail_sell_pct, max_hold_hours)
BATCH = [
    ("ETHU", 1766, "TrailingBothZScoreBreakout", 10, 1.0, 1.0, 1.0, 2.0, 7.0, 56),
    ("OILU", 1939, "TrailingBothZScoreBreakout", 20, 1.0, 2.0, 2.0, 1.0, 7.0, 112),
]


def main():
    for (ticker, node_id, strategy, window, z, fixed_sl, arm_pct, trail_buy_pct,
         trail_sell_pct, max_hold_hours) in BATCH:
        label = f"v6 GT promotion (candidate_nodes id={node_id})"
        print(f"\n{ticker}: creating live node (candidate_nodes id={node_id})")
        signals_db.add_node(
            ticker=ticker, strategy=strategy, version=VERSION, window=window,
            take_profit=arm_pct, stop_loss=fixed_sl, max_hold_hours=max_hold_hours,
            label=label,
            z_score_threshold=z, watchlist_id=WATCHLIST_ID, state="live",
            trail_buy_pct=trail_buy_pct, trail_pct=trail_sell_pct, entry_timing=ENTRY_TIMING,
            starting_notional=NOTIONAL, fixed_sl_override=fixed_sl, account=ACCOUNT,
        )
        # add_node() doesn't return the new row's id (confirmed 2026-08-24) -- look it
        # up by the label we just set, matching the doubling-safe convention add_node
        # itself uses (dedup on the full param tuple).
        with signals_db._conn() as c:
            new_id = c.execute("SELECT id FROM watch_list WHERE label=? AND ticker=? "
                                "ORDER BY added_at DESC LIMIT 1", (label, ticker)).fetchone()[0]
            c.execute("UPDATE watch_list SET addon_enabled=1 WHERE id=?", (new_id,))
            c.commit()
        print(f"  wl_id={new_id}, account={ACCOUNT}, notional=${NOTIONAL:,.0f}, addon_enabled=1")


if __name__ == "__main__":
    main()
