"""
Research script: does price systematically drift down from the 9:30 bar to the 14:30 bar (the
two live signal-check bars), which would explain why BUY dislocation signals fire ~3x more often
at 14:30 than 9:30 (docs/research_log.md's 2026-08-03 entry-timing-seasonality entry)? Or is that
imbalance just a byproduct of more elapsed time for a large move to happen by the later check,
with no real directional bias?

For each of the 10 v5 watchlist tickers, computes the 9:30-bar-Close-to-14:30-bar-Close percent
change for every trading day with both bars present, and tests whether the mean/median is really
negative (Wilcoxon signed-rank test against 0) rather than just noise.

Usage: .venv/bin/python scripts/intraday_drift_check.py
"""
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import wilcoxon

CSV_DIR = Path("cache/research")
TICKERS = ["AGQ", "DPST", "GDXU", "HIBL", "KORU", "NUGT", "SOXL", "UDOW", "USD", "YANG"]


def daily_930_to_1430_change(ticker):
    df = pd.read_csv(CSV_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    df["hour"] = df.index.hour
    df["date"] = df.index.date

    c930 = df[df["hour"] == 9].groupby("date")[close_col].first()
    c1430 = df[df["hour"] == 14].groupby("date")[close_col].first()
    both = pd.concat([c930.rename("c930"), c1430.rename("c1430")], axis=1).dropna()
    return (both["c1430"] - both["c930"]) / both["c930"]


def main():
    rows = []
    pooled = []
    for ticker in TICKERS:
        csv = CSV_DIR / f"{ticker}_1h.csv"
        if not csv.exists():
            continue
        pct = daily_930_to_1430_change(ticker)
        pooled.append(pct)
        stat, p = wilcoxon(pct)
        rows.append({
            "ticker": ticker, "n_days": len(pct),
            "mean_pct": round(pct.mean() * 100, 3), "median_pct": round(pct.median() * 100, 3),
            "pct_days_down": round((pct < 0).mean() * 100, 1),
            "wilcoxon_p": round(p, 4),
        })

    df_out = pd.DataFrame(rows)
    pd.set_option("display.width", 160)
    print(df_out.to_string(index=False))

    all_pct = pd.concat(pooled)
    stat, p = wilcoxon(all_pct)
    print(f"\nPooled across all tickers/days (n={len(all_pct)}): mean={all_pct.mean()*100:.3f}%, "
          f"median={all_pct.median()*100:.3f}%, days_down={100*(all_pct<0).mean():.1f}%, "
          f"wilcoxon_p={p:.4f} (p<0.05 = real net drift, not just noise)")


if __name__ == "__main__":
    main()
