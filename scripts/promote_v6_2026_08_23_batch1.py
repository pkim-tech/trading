"""One-off: v6 promotion, ALL 12 (2026-08-23 batch1 + 2026-08-24 follow-on,
planner session).

Archives the 12 real v5 live nodes with no open position/pending buy and
creates their v6 replacements at the selected GT candidate_nodes ids, same
account/starting_notional, watchlist_id=65 (the standing active watchlist --
see CLAUDE.md's "Active watchlist" note, no new watchlist created for v6).

DFEN/DPST/SOXL/WEBL were deliberately excluded from the original 2026-08-23
batch1 (real open position/pending buy on their current live node, check 17).
Added 2026-08-24 (planner session) after confirming, directly against the
broker (not just the DB), that all 4 are now genuinely flat -- their DB state
had drifted (real manual SELL fills/CANCELED orders the daemon never
recorded, exactly the gap CLAUDE.md's live-trading section flags) and was
reconciled first via scripts/reconcile_flat_position.py before this script
was extended to include them. Candidate node ids for these 4 are each
scope's top-robust_alpha pick from candidate_nodes (not yet through the same
manual curation batch1's original 8 had) -- reasonable defaults, not a
promise these are the definitively best picks; revisit if a fuller review
disagrees.

Params sourced from cache/research/trading_universe.db's candidate_nodes
table (arm_pct/trail_buy_pct/trail_sell_pct already carry the real GT axis
mapping -- see strategies.resolve_axis_columns/run_optimization_sweep.py's
derive_phase25_candidates_ground_truth SQL aliasing, NOT raw backtest_cache
column names).

Run once. Re-running is safe (add_node dedups on the full param tuple), but
archive_node raises on an already-archived node, so it's not idempotent
end-to-end.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import signals_db

# (ticker, old_live_wl_id, account, starting_notional, new_candidate_node_id,
#  strategy, window, z, fixed_sl, take_profit_arg, trail_buy_pct, trail_pct, max_hold_hours)
BATCH = [
    ("AGQ",  203, "brokerage", 5000.0,  436, "TrailingExitZScoreBreakout", 10, 1.0, 2.0,  4.0, 0.0,  11.0, 91),
    ("GDXU", 229, "brokerage", 5000.0, 1314, "TrailingExitZScoreBreakout", 20, 1.5, 1.0,  9.0, 0.0,  16.0, 77),
    ("HIBL", 235, "ira",      10000.0,  907, "TrailingBothZScoreBreakout", 10, 1.0, 3.0, 28.0, 3.0,   3.0, 91),
    ("JNUG", 231, "roth",     10000.0,  879, "TrailingBothZScoreBreakout", 20, 1.5, 3.0, 30.0, 3.0,   6.0, 126),
    ("KORU", 202, "roth",     10000.0,  520, "TrailingBothZScoreBreakout", 10, 1.0, 3.0, 20.0, 10.0,  1.0, 98),
    ("LABU", 236, "ira",      10000.0, 1446, "TrailingBothZScoreBreakout", 20, 1.0, 5.0, 13.0, 7.0,   1.0, 77),
    ("NUGT", 230, "ira",      10000.0,  778, "TrailingBothZScoreBreakout", 10, 1.0, 2.0, 25.0, 5.0,   1.0, 77),
    ("UGL",  233, "brokerage", 5000.0,  993, "TrailingExitZScoreBreakout", 10, 1.0, 2.0,  2.0, 0.0,   5.0, 119),
    ("DFEN", 232, "roth",     10000.0,  832, "TrailingBothZScoreBreakout", 20, 1.0, 3.0, 30.0, 3.0,   1.0, 133),
    ("DPST", 197, "ira",      10000.0, 1293, "TrailingExitZScoreBreakout", 20, 1.0, 1.0, 13.0, 0.0,  13.0, 140),
    ("SOXL", 92,  "ira",      10000.0, 1534, "TrailingExitZScoreBreakout", 10, 1.0, 5.0,  8.0, 0.0,   8.0, 28),
    ("WEBL", 234, "brokerage", 5000.0,  965, "TrailingBothZScoreBreakout", 10, 1.5, 4.0, 28.0, 9.0,   4.0, 140),
]

WATCHLIST_ID = 65
VERSION = "v6"
ENTRY_TIMING = "open_check"


def main():
    new_wl_ids = {}
    for (ticker, old_wl_id, account, notional, node_id, strategy, window, z,
         fixed_sl, take_profit, trail_buy_pct, trail_pct, max_hold_hours) in BATCH:
        print(f"\n{ticker}: archiving old wl_id={old_wl_id}")
        signals_db.archive_node(old_wl_id)

        label = f"v6 GT promotion (candidate_nodes id={node_id})"
        signals_db.add_node(
            ticker=ticker, strategy=strategy, version=VERSION, window=window,
            take_profit=take_profit, stop_loss=fixed_sl, max_hold_hours=max_hold_hours,
            label=label,
            z_score_threshold=z, watchlist_id=WATCHLIST_ID, state="live",
            trail_buy_pct=trail_buy_pct, trail_pct=trail_pct, entry_timing=ENTRY_TIMING,
            starting_notional=notional, fixed_sl_override=fixed_sl, account=account,
        )
        # add_node() doesn't return the new row's id (confirmed 2026-08-24) -- look it
        # up by the label just set, same fix applied to promote_v6_2026_08_24_ethu_oilu.py.
        with signals_db._conn() as c:
            new_id = c.execute("SELECT id FROM watch_list WHERE label=? AND ticker=? "
                                "ORDER BY added_at DESC LIMIT 1", (label, ticker)).fetchone()[0]
        new_wl_ids[ticker] = new_id
        print(f"{ticker}: new live wl_id={new_id} (account={account}, notional=${notional:,.0f})")

    print("\n--- seeding staged_test_config baseline for new nodes ---")
    import subprocess
    subprocess.run([sys.executable, "scripts/seed_baseline_config.py"], check=True)

    print("\nDone. New wl_ids:", new_wl_ids)


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
