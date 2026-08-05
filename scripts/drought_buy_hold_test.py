"""
Research script: tests a specific overlay idea for strategy "droughts" (extended no-trade
stretches, the real mechanism behind SOXL's weak 2026 -- docs/research_log.md's 2026-08-04 late
entries). Rather than predicting when a drought will start (drought_detection_test.py found
~zero advance warning for that), this asks a different, directly testable question: if you simply
buy-and-hold the underlying from drought start through drought end (which, by construction, is
the moment the strategy's own next real signal fires -- i.e. price has already dipped back through
the z-band), does the stable gain from riding the quiet stretch outweigh the eventual dip that
ends it? No prediction needed -- the "exit" is just handing off to the strategy's own next trade.

For each of the 185 real historical droughts (all 10 v5 tickers, >= 10 trading days with zero
signals -- same drought list as drought_detection_test.py), computes:
  - buy_hold_ret: Close at drought end / Close at drought start - 1
  - peak_ret: the best Close reached during the window, relative to start -- how much of the
    stable gain existed at its best point, before any give-back into the eventual dip
  - giveback: peak_ret - buy_hold_ret -- how much of the peak gain was lost by the time the
    trigger fired

Usage: .venv/bin/python scripts/drought_buy_hold_test.py [--tickers ...] [--watchlist-id 65]
       [--min-drought-days 10] [--csv]
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.export_trades import load_hourly
from scripts.drought_detection_test import load_nodes, get_signal_days, find_droughts

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=None)
    parser.add_argument("--watchlist-id", type=int, default=65)
    parser.add_argument("--min-drought-days", type=int, default=10)
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()

    nodes = load_nodes(args.watchlist_id, args.tickers)
    rows = []
    for node in nodes:
        try:
            signal_days, df_h = get_signal_days(node)
        except Exception as e:
            print(f"{node['ticker']}: failed ({e})")
            continue

        daily = df_h.resample("D").last().dropna(subset=["Close"])
        all_days = list(daily.index.normalize().unique())
        droughts = find_droughts(signal_days, all_days, args.min_drought_days)

        for start, end, gap_len in droughts:
            window = daily.loc[start:end, "Close"]
            if len(window) < 2:
                continue
            start_px, end_px = window.iloc[0], window.iloc[-1]
            peak_px = window.max()
            buy_hold_ret = float(end_px / start_px - 1)
            peak_ret = float(peak_px / start_px - 1)
            rows.append({
                "ticker": node["ticker"], "start": str(start.date()), "end": str(end.date()),
                "gap_trading_days": gap_len, "buy_hold_ret": buy_hold_ret,
                "peak_ret": peak_ret, "giveback": peak_ret - buy_hold_ret,
            })

    df = pd.DataFrame(rows)
    if df.empty:
        print("No droughts found.")
        return
    pd.set_option("display.width", 160)
    pd.set_option("display.max_rows", None)

    if args.csv:
        OUTPUT_DIR.mkdir(exist_ok=True)
        df.to_csv(OUTPUT_DIR / "drought_buy_hold_test.csv", index=False)
        print(f"Wrote {OUTPUT_DIR / 'drought_buy_hold_test.csv'}\n")

    print(df.to_string(index=False))

    print(f"\n--- Pooled (n={len(df)} droughts across {df['ticker'].nunique()} tickers) ---")
    print(f"buy_hold_ret: mean={df['buy_hold_ret'].mean()*100:.2f}%  median={df['buy_hold_ret'].median()*100:.2f}%  "
          f"win_rate={(df['buy_hold_ret'] > 0).mean():.3f}")
    print(f"peak_ret:     mean={df['peak_ret'].mean()*100:.2f}%  median={df['peak_ret'].median()*100:.2f}%")
    print(f"giveback:     mean={df['giveback'].mean()*100:.2f}%  median={df['giveback'].median()*100:.2f}%  "
          f"(fraction of peak gain lost by the time the trigger fired)")
    compounded = float(np.prod(1 + df["buy_hold_ret"]) - 1)
    print(f"Compounded return if applied to EVERY drought sequentially (not realistic -- "
          f"droughts overlap across tickers -- but a bound): {compounded*100:.1f}%")

    print(f"\n--- Per-ticker ---")
    per_ticker = df.groupby("ticker").agg(
        n=("buy_hold_ret", "size"),
        mean_buy_hold_ret=("buy_hold_ret", "mean"),
        win_rate=("buy_hold_ret", lambda s: (s > 0).mean()),
        mean_giveback=("giveback", "mean"),
        compounded=("buy_hold_ret", lambda s: float(np.prod(1 + s) - 1)),
    ).round(4)
    print(per_ticker.to_string())


if __name__ == "__main__":
    main()
