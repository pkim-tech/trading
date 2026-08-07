"""Build the 'Live v5' watchlist from the 2026-07-20 v5 resweep review --
10 tickers, whichever strategy (TrailingBoth vs TrailingExit) had a
cliff-safe candidate with the higher robust alpha, at entry_timing=open_check.
DUST/GDXD/NAIL/RETL/UVIX/ZSL excluded (no cliff-safe node either strategy);
TQQQ/LABU excluded by user call despite having a viable TE node.

Creates the watchlist inactive (mode='research' on every node) -- does NOT
call set_active_watchlist. Activation is a separate, deliberate step.

Usage:
    .venv/bin/python scripts/build_v5_watchlist.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import signals_db as db

VERSION = "v5"
ENTRY_TIMING = "open_check"
STATE = "paper"
STARTING_NOTIONAL = 50000
LABEL = "v5 promotion 2026-07-20"

TB = "TrailingBothZScoreBreakout"
TE = "TrailingExitZScoreBreakout"

# (ticker, strategy, fixed_sl, take_profit/arm_sell_pct, trail_buy_pct, trail_sell_pct,
#  hold, window, z_score_threshold)
NODES = [
    ("AGQ",  TE, 2, 8,  None, 7.0,  84,  10, 1.0),
    ("DPST", TE, 2, 29, None, 16.0, 140, 20, 1.0),
    ("GDXU", TB, 1, 25, 3.0,  4.0,  21,  10, 1.0),
    ("HIBL", TB, 1, 10, 1.0,  5.0,  56,  20, 1.0),
    ("KORU", TE, 3, 25, None, 3.0,  105, 20, 1.5),
    ("NUGT", TE, 2, 3,  None, 18.0, 119, 10, 1.0),
    ("SOXL", TB, 2, 30, 3.0,  1.0,  70,  10, 1.0),
    ("UDOW", TE, 1, 1,  None, 11.0, 140, 10, 1.5),
    ("USD",  TB, 3, 18, 1.0,  6.0,  126, 20, 2.0),
    ("YANG", TE, 3, 5,  None, 17.0, 112, 10, 2.0),
]


def main():
    wl_id = db.create_watchlist("Live v5")
    print(f"Watchlist 'Live v5' id={wl_id} (inactive)")

    for ticker, strategy, fixed_sl, tp, trail_buy, trail_sell, hold, window, z in NODES:
        db.add_node(
            ticker=ticker, strategy=strategy, version=VERSION, window=window,
            take_profit=tp, stop_loss=fixed_sl, max_hold_hours=hold,
            label=LABEL, z_score_threshold=z, watchlist_id=wl_id, state=STATE,
            trail_buy_pct=trail_buy, trail_pct=trail_sell,
            entry_timing=ENTRY_TIMING, starting_notional=STARTING_NOTIONAL,
            fixed_sl_override=fixed_sl,
        )
        print(f"  added {ticker} ({strategy[:20]}, fixed_sl={fixed_sl}%)")

    print("\nFinal watch_list rows:")
    for row in db.get_watchlist(wl_id):
        print(" ", dict(row))


if __name__ == "__main__":
    main()
