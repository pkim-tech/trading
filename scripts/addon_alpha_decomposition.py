"""Decomposes a node's total backtested compounded return into contribution
buckets (SL / TRAIL-win / TRAIL-loss / TIME / other core exits / ADDON layer),
using log-return additivity: sum(log(1+ret)) across a bucket's trades equals
that bucket's exact multiplicative contribution to the whole compounded curve,
regardless of trade order (unlike raw pct-point summation, which distorts
under compounding -- see CLAUDE.md's note on additive vs. equity-ratio framing).

Built 2026-08-17 for the backlog item-1/2 walkthrough (docs/backlog_cache.md,
add-on-leg design discussion): "how much alpha comes from trail-arm (the
add-on layer) vs trail-win vs trail-loss vs SL" for the 3 real capital-at-
stake add-on tickers (ETHU/AGQ/JNUG, account=brokerage). Uses each ticker's
REAL live watch_list config (not backtest_cache's auto-picked "best" node),
since the point is what the live node is actually doing.

Usage:
  .venv/bin/python scripts/addon_alpha_decomposition.py [TICKER ...]
  (default: the 3 real brokerage addon tickers, read live from watch_list)
"""
import argparse
import math
import sqlite3
import sys
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.drought_overlay_test import get_trades_and_bars
from scripts.stacked_model.add_on import generate_addon_trades
from backtester import WIN, LOSS, TWIN, TLOSS, OPEN

DB_PATH = "cache/live/trading_live.db"


def live_node_dict(conn, ticker):
    row = conn.execute(
        "SELECT ticker, strategy, window, take_profit, stop_loss, max_hold_hours, "
        "z_score_threshold, trail_sell_pct, fixed_sl, trail_buy_pct, arm_sell_pct, entry_timing "
        "FROM watch_list WHERE account='brokerage' AND state='live' AND ticker=?", (ticker,)
    ).fetchone()
    if row is None:
        return None
    d = dict(zip([c[0] for c in conn.execute(
        "SELECT ticker, strategy, window, take_profit, stop_loss, max_hold_hours, "
        "z_score_threshold, trail_sell_pct, fixed_sl, trail_buy_pct, arm_sell_pct, entry_timing "
        "FROM watch_list LIMIT 0").description], row))
    d["z"] = d.pop("z_score_threshold")
    d["arm_pct"] = d["arm_sell_pct"] if d["strategy"] == "TrailingBothZScoreBreakout" else d["take_profit"]
    return d


def bucket_for(trade):
    """The export_trades.py mirror kernels don't emit exit_reason directly --
    result (WIN/LOSS/TWIN/TLOSS) + arm_i together disambiguate: arm_i is only
    set once the trailing-arm branch has run, so a WIN/LOSS trade with arm_i
    set is a genuine TRAIL exit (breach or hold-time-forced-while-armed, both
    collapse to WIN/LOSS in this kernel -- see CLAUDE.md's 2026-08-01 TIME-
    while-armed note); a LOSS with arm_i None is the SL branch (always
    hardcoded LOSS, never WIN, since a long mean-reversion exit below entry
    can't be positive); TWIN/TLOSS is the never-armed hold-time TIME exit."""
    result = trade["result"]
    armed = trade.get("arm_i") is not None
    if result == OPEN:
        return "OPEN(mark-to-market)"
    if result in (TWIN, TLOSS):
        return "TIME"
    if armed:
        return "TRAIL-win" if result == WIN else "TRAIL-loss"
    return "SL"


def decompose(ticker, node):
    trades, df_h = get_trades_and_bars(node)
    addon_trades = generate_addon_trades(trades, df_h)

    log_by_bucket = defaultdict(float)
    count_by_bucket = defaultdict(int)
    for t in trades:
        b = bucket_for(t)
        log_by_bucket[b] += math.log(1.0 + t["ret"])
        count_by_bucket[b] += 1
    for t in addon_trades:
        log_by_bucket["ADDON"] += math.log(1.0 + t["ret"])
        count_by_bucket["ADDON"] += 1

    total_log = sum(log_by_bucket.values())
    total_ret = math.exp(total_log) - 1.0

    print(f"\n=== {ticker} ({node['strategy']}, live config) ===")
    print(f"total core+addon compounded return: {total_ret*100:+.1f}%  "
          f"({len(trades)} core trades, {len(addon_trades)} addon trades)")
    for b in sorted(log_by_bucket, key=lambda k: -abs(log_by_bucket[k])):
        contrib_ret = math.exp(log_by_bucket[b]) - 1.0
        share = log_by_bucket[b] / total_log * 100 if total_log != 0 else float("nan")
        print(f"  {b:12s}  n={count_by_bucket[b]:3d}  "
              f"multiplicative contribution={contrib_ret*100:+8.1f}%  "
              f"share of total log-return={share:+6.1f}%")
    return log_by_bucket, count_by_bucket


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    tickers = args.tickers or [r[0] for r in conn.execute(
        "SELECT ticker FROM watch_list WHERE account='brokerage' AND state='live' AND addon_enabled=1")]

    for ticker in tickers:
        node = live_node_dict(conn, ticker)
        if node is None:
            print(f"{ticker}: no live brokerage node found, skipping")
            continue
        try:
            decompose(ticker, node)
        except Exception as e:
            print(f"{ticker}: failed ({e})")


if __name__ == "__main__":
    main()
