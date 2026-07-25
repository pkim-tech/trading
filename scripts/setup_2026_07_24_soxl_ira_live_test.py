"""One-off setup for the 2026-07-24 soxl_ira real-order test day
(docs/live_test_plan_2026-07-24.md). Adds 6 new watch_list nodes and seeds
open_positions for the 2 real pre-staged positions (SPY/SH). Run once;
add_node's dedup includes account, so a rerun with the same account is a
harmless no-op for nodes that already exist by
(ticker, strategy, version, window, account).
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

WATCHLIST_ID = 65
VERSION = "soxl_test"


def _node_id(ticker, version, window):
    rows = db.get_watchlist()
    for r in rows:
        if r["ticker"] == ticker and r["version"] == version and r["window"] == window:
            return r
    raise RuntimeError(f"node not found after insert: {ticker} {version} w={window}")


def main():
    # 1/2 -- SPY / SH sell-only nodes. Tight arm/trail/SL (0.3%) so at least one of
    # the two (opposite-direction) realistically triggers a real SELL today
    # regardless of market direction. window=99 is a deliberately distinct,
    # unused lookback -- doesn't collide with the canary node (id=96, window=5).
    db.add_node(
        ticker="SPY", strategy="TrailingBothZScoreBreakout", version=VERSION, window=99,
        take_profit=0.3, stop_loss=0, max_hold_hours=48, label="soxl_ira live-sell test",
        z_score_threshold=2.0, watchlist_id=WATCHLIST_ID, mode="live",
        trail_buy_pct=1.0, trail_pct=0.3, entry_timing="close", starting_notional=50000,
        fixed_sl_override=0.3, account="soxl_ira",
    )

    db.add_node(
        ticker="SH", strategy="TrailingBothZScoreBreakout", version=VERSION, window=99,
        take_profit=0.3, stop_loss=0, max_hold_hours=48, label="soxl_ira live-sell test",
        z_score_threshold=2.0, watchlist_id=WATCHLIST_ID, mode="live",
        trail_buy_pct=1.0, trail_pct=0.3, entry_timing="close", starting_notional=50000,
        fixed_sl_override=0.3, account="soxl_ira",
    )

    # 3/4 -- ERX / ERY real trailing-buy + top-up test. starting_notional sized for
    # ~2 shares so a manually-fed 1-share fill leaves a real shortfall for
    # _reconcile_fill to top up (see plan doc -- forcing this requires a manual
    # real 1-share order + a manual _reconcile_fill call, not pure config).
    # entry_timing='close' (deliberately NOT open_check -- LABD covers that path).
    db.add_node(
        ticker="ERX", strategy="TrailingBothZScoreBreakout", version=VERSION, window=20,
        take_profit=5, stop_loss=0, max_hold_hours=24, label="soxl_ira live-buy test",
        z_score_threshold=1.5, watchlist_id=WATCHLIST_ID, mode="live",
        trail_buy_pct=0.5, trail_pct=2, entry_timing="close", starting_notional=190,
        fixed_sl_override=3, account="soxl_ira",
    )

    db.add_node(
        ticker="ERY", strategy="TrailingBothZScoreBreakout", version=VERSION, window=20,
        take_profit=5, stop_loss=0, max_hold_hours=24, label="soxl_ira live-buy test",
        z_score_threshold=1.5, watchlist_id=WATCHLIST_ID, mode="live",
        trail_buy_pct=0.5, trail_pct=2, entry_timing="close", starting_notional=21,
        fixed_sl_override=3, account="soxl_ira",
    )

    # 5 -- LABD real market-buy path test (_attempt_automated_market_buy,
    # TrailingExitZScoreBreakout is non-trailing-buy -> market-buy eligible).
    # entry_timing='open_check' -- gives it a shot at firing right at 9:31 ET.
    db.add_node(
        ticker="LABD", strategy="TrailingExitZScoreBreakout", version=VERSION, window=20,
        take_profit=10, stop_loss=0, max_hold_hours=24, label="soxl_ira live-buy test (market-buy path)",
        z_score_threshold=1.5, watchlist_id=WATCHLIST_ID, mode="live",
        trail_pct=0.5, entry_timing="open_check", starting_notional=22,
        fixed_sl_override=0.3, account="soxl_ira",
    )

    # 6 -- GDXD, dedicated ticker for the Part 3 branch B gap-resize test (needs
    # its own trailing-buy node, distinct from ERX/ERY so the real signal-window
    # order for those doesn't collide with a same-ticker duplicate-order guard).
    # Tight trail_buy_pct=0.3% -- maximizes the odds a real pre-market move
    # clears the trigger before the 9:15-9:29 ET gap-check window. The actual
    # resting order + pending_buys seed is a MANUAL step tomorrow morning before
    # 9:15 ET (needs the real running_low at that time) -- not done by this script.
    db.add_node(
        ticker="GDXD", strategy="TrailingBothZScoreBreakout", version=VERSION, window=20,
        take_profit=5, stop_loss=0, max_hold_hours=24, label="soxl_ira gap-resize test",
        z_score_threshold=1.5, watchlist_id=WATCHLIST_ID, mode="live",
        trail_buy_pct=0.3, trail_pct=2, entry_timing="close", starting_notional=500,
        fixed_sl_override=3, account="soxl_ira",
    )

    print("Nodes created:")
    for ticker, window in [("SPY", 99), ("SH", 99), ("ERX", 20), ("ERY", 20), ("LABD", 20), ("GDXD", 20)]:
        print(" ", dict(_node_id(ticker, VERSION, window)))

    # Seed open_positions for the 2 real pre-staged positions (SPY 3sh, SH 50sh).
    # entry_price/entry_time are placeholders (current market price, now) for the
    # purpose of exercising the SL/arm/trail mechanism -- not a claim about the
    # user's real cost basis, which isn't recorded in this system.
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    spy_node = _node_id("SPY", VERSION, 99)
    sh_node = _node_id("SH", VERSION, 99)

    opened_spy = db.open_position(spy_node, signal_price=738.18, signal_time=now_str,
                                   entry_price=738.18, entry_time=now_str, shares=3)
    opened_sh = db.open_position(sh_node, signal_price=33.52, signal_time=now_str,
                                  entry_price=33.52, entry_time=now_str, shares=50)
    print(f"SPY position seeded: {opened_spy}")
    print(f"SH position seeded: {opened_sh}")


if __name__ == "__main__":
    # Guarded (was unconditional top-level code) -- this file's name ends in
    # "_test.py", which pytest's default discovery glob matches, so a bare
    # `pytest` invocation from repo root was importing and executing this
    # one-off real-DB setup script as a side effect of test collection (found
    # while verifying the wl_id refactor, 2026-07-25/26). Never touched by
    # this refactor's actual runtime code -- purely a test-collection hazard.
    main()
