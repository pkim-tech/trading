"""One-off: SOXL v6.5.2 sweep-pick promotion (2026-09-10), replacing wl_id=249
(candidate_nodes id=1534, v6 GT promotion).

Checklist run this session (docs/watchlist_candidate_checklist.md): Step 0 (candidate
clearly beats live -- core_both_cagr 226.4%->440.6%, worst_neighbor_cagr 23.0%->74.2%),
checks 11/13 via GT (-44.5% max drawdown, walk-forward MARGINAL with one fragile fold
at -17.0% alpha), 14 (this script seeds it), 15 (Direxion, known/monitored sponsor),
16 (N/A -- staying in `ira`, no tax-status change), 17 (clean -- zero open positions/
pending buys on wl_id=249), 18 (SOXL already in SCHWAB_AUTOMATION_TICKERS).

Real substantive open items, not blocking but explicitly not "fully validated": TE's
drought overlay (commit 5dfc7bf, 2026-09-08) is cross-checked against only 2 tickers
total (AGQ + SOXL, the latter done this session via a magnitude comparison against TB
candidate 49622 at the same window/fixed_sl -- 482.4% vs 169.5% compounded, both
verdict OK, same shape as the AGQ check). Add-on cross-checked the same way (SOXL
1263.3% vs TB's 777.1%, both OK/STABLE) -- no red flag found, user's explicit call to
enable addon_enabled=1 from the start rather than hold it back pending a wider check.

candidate_nodes id=50073 beats id=50074 on core+drought alone (220.4% vs 215.7%) even
though 50074 edges ahead on the addon-inclusive "both" figure (447.0% vs 440.6%) --
50073 chosen as the more consistently strong pick across both scenarios.

Run once. Re-running is safe (add_node dedups on the full param tuple; archival/config
updates are idempotent).
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import signals_db

OLD_WL_ID = 249
TICKER = "SOXL"
CANDIDATE_ID = 50073
STRATEGY = "TrailingExitZScoreBreakout"
VERSION = "v6.5.2-bench-inmemory-v6-massive-w2021-08-23_2026-08-21-z0.5-1.0-1.5-2.0-isl3-pv4"
WATCHLIST_ID = 65
ENTRY_TIMING = "open_check"
ACCOUNT = "ira"
NOTIONAL = 10000.0

# Real params from candidate_nodes.params_json for id=50073:
# {entry_timing: open_check, fixed_sl: 1.0, max_hold_hours: 21, take_profit: 6.0,
#  trail_pct: 9.0, ticker: SOXL, window: 5, z_score_threshold: 1.0}
WINDOW = 5
Z_SCORE_THRESHOLD = 1.0
FIXED_SL = 1.0
TAKE_PROFIT = 6.0
TRAIL_PCT = 9.0  # -> stored_trail_sell_pct, per strategies.resolve_axis_columns('TrailingExitZScoreBreakout') == ('trail_pct', None)
MAX_HOLD_HOURS = 21


def main():
    # Archive the old node -- same convention as the ETHU 237->251 replacement
    # (archived_at set, state left as 'live' unchanged; every real query already
    # filters on `archived_at IS NULL`, not on state, to exclude it).
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET archived_at=datetime('now') WHERE id=?", (OLD_WL_ID,))
        c.commit()
    print(f"Archived old wl_id={OLD_WL_ID} (candidate_nodes id=1534)")

    label = f"v6.5.2 sweep pick (candidate_nodes id={CANDIDATE_ID}), replaces wl_id={OLD_WL_ID} (candidate_nodes id=1534). " \
            f"core_both_cagr 226.4%->440.6%, worst_neighbor_cagr 23.0%->74.2%, walk-forward MARGINAL " \
            f"(1 fragile fold, -17.0% alpha). TE drought overlay (5dfc7bf) + addon cross-checked against " \
            f"TB 49622 (same window/fixed_sl) -- both similar magnitude, both OK/STABLE, no red flag. " \
            f"See docs/backlog_cache.md 2026-09-10 entry for full checklist trace."
    print(f"\n{TICKER}: creating live node (candidate_nodes id={CANDIDATE_ID})")
    signals_db.add_node(
        ticker=TICKER, strategy=STRATEGY, version=VERSION, window=WINDOW,
        take_profit=TAKE_PROFIT, stop_loss=FIXED_SL, max_hold_hours=MAX_HOLD_HOURS,
        label=label, z_score_threshold=Z_SCORE_THRESHOLD, watchlist_id=WATCHLIST_ID,
        state="live", trail_buy_pct=None, trail_pct=TRAIL_PCT, entry_timing=ENTRY_TIMING,
        starting_notional=NOTIONAL, fixed_sl_override=FIXED_SL, account=ACCOUNT,
    )

    # add_node() doesn't return the new row's id (same as promote_v6_2026_08_24_ethu_
    # oilu.py's own convention) -- look it up by label+ticker, newest first.
    with signals_db._conn() as c:
        new_id = c.execute(
            "SELECT id FROM watch_list WHERE label=? AND ticker=? ORDER BY added_at DESC LIMIT 1",
            (label, TICKER)).fetchone()[0]
        c.execute("UPDATE watch_list SET addon_enabled=1, drought_overlay_enabled=1 WHERE id=?",
                  (new_id,))
        c.commit()
    print(f"  wl_id={new_id}, account={ACCOUNT}, notional=${NOTIONAL:,.0f}, "
          f"addon_enabled=1, drought_overlay_enabled=1")

    # Check 14 (config-drift baseline, REQUIRED ACTION at promotion time) -- seeds
    # every live node missing a staged_test_config row, safe/idempotent.
    print("\nSeeding config-drift baseline (check 14)...")
    subprocess.run([sys.executable, "scripts/seed_baseline_config.py"], check=True)


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
