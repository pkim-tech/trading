"""One-off backfill for backtest_cache rows whose win_twin_rate is stale (computed
before the column existed, never recomputed -- see docs/backlog_cache.md). Re-runs
the kernel for the exact node config and updates just that row's win_twin_rate
(and sanity-checks win_rate/trades still match, to confirm it's the same node)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import pandas as pd
import strategies
from backtester import prep_inputs
from scripts.export_trades import load_hourly, simulate_trail_both_annotated

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"

# (ticker, watch_list version) -- pulled from watchlist_id=9, matched exactly against
# their backtest_cache row (including axis_tp/max_hold_hours) in the 2026-07-13 session.
TARGETS = ["AGQ", "EDC", "YANG"]


def recalc(ticker):
    conn = sqlite3.connect(CACHE_DIR / "trading_live.db")
    row = conn.execute(
        "SELECT window, arm_sell_pct, trail_buy_pct, trail_sell_pct, fixed_sl, "
        "max_hold_hours, z_score_threshold, version FROM watch_list "
        "WHERE ticker=? AND watchlist_id=9 AND strategy='TrailingBothZScoreBreakout'",
        (ticker,),
    ).fetchone()
    conn.close()
    window, arm_sell_pct, trail_buy_pct, trail_sell_pct, fixed_sl, max_hold_hours, z_thresh, version = row

    df_h = load_hourly(ticker)
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    strat = strategies.TrailingBothZScoreBreakout(window=window, z_score_threshold=z_thresh)
    ind = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_h, ind)

    take_profit = arm_sell_pct / 100.0
    stop_loss = fixed_sl / 100.0
    trail_buy = trail_buy_pct / 100.0
    trail_sell = trail_sell_pct / 100.0

    trades = simulate_trail_both_annotated(
        p, take_profit, stop_loss, max_hold_hours, trail_buy, trail_sell, 9, 14, z_thresh,
    )
    closed = [t for t in trades if t["result"] in (0, 1, 2, 3)]  # WIN/LOSS/TWIN/TLOSS
    df_tr = pd.DataFrame(closed)
    from backtester import WIN, TWIN
    win_rate = float((df_tr["result"] == WIN).sum() / len(df_tr) * 100)
    win_twin_rate = float(df_tr["result"].isin([WIN, TWIN]).sum() / len(df_tr) * 100)

    uconn = sqlite3.connect(CACHE_DIR / "trading_universe.db")
    ucur = uconn.execute(
        "SELECT win_rate, win_twin_rate, trades FROM backtest_cache WHERE ticker=? AND "
        "strategy='TrailingBothZScoreBreakout' AND version=? AND window=? AND z_score_threshold=? "
        "AND arm_sell_pct=? AND trail_buy_pct=? AND trail_sell_pct=? AND fixed_sl=? AND max_hold_hours=?",
        (ticker, version, window, z_thresh, arm_sell_pct, trail_buy_pct, trail_sell_pct, fixed_sl, max_hold_hours),
    )
    old_win_rate, old_win_twin_rate, old_trades = ucur.fetchone()

    print(f"{ticker} {version}: old win_rate={old_win_rate:.1f} win_twin_rate={old_win_twin_rate:.1f} trades={old_trades}"
          f" | recomputed win_rate={win_rate:.1f} win_twin_rate={win_twin_rate:.1f} trades={len(df_tr)}")
    if round(old_win_rate, 1) != round(win_rate, 1) or old_trades != len(df_tr):
        print(f"  [!] win_rate/trades mismatch -- not the same node, skipping update")
        uconn.close()
        return

    uconn.execute(
        "UPDATE backtest_cache SET win_twin_rate=? WHERE ticker=? AND "
        "strategy='TrailingBothZScoreBreakout' AND version=? AND window=? AND z_score_threshold=? "
        "AND arm_sell_pct=? AND trail_buy_pct=? AND trail_sell_pct=? AND fixed_sl=? AND max_hold_hours=?",
        (win_twin_rate, ticker, version, window, z_thresh, arm_sell_pct, trail_buy_pct, trail_sell_pct, fixed_sl, max_hold_hours),
    )
    uconn.commit()
    uconn.close()
    print(f"  updated win_twin_rate {old_win_twin_rate:.1f} -> {win_twin_rate:.1f}")


if __name__ == "__main__":
    for t in TARGETS:
        recalc(t)
