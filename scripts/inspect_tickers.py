#!/usr/bin/env python3
"""
Print yfinance metadata for all tickers in tickers.json.
Useful for vetting candidates before adding them to the universe.

Usage:
    python scripts/inspect_tickers.py
    python scripts/inspect_tickers.py UPRO SPXL TNA   # ad-hoc candidates
"""

import sys
import json
import yfinance as yf
from pathlib import Path

TICKERS_FILE = Path("./tickers.json")


def fetch_info(ticker: str) -> dict:
    info = yf.Ticker(ticker).info
    aum = info.get("netAssets") or info.get("totalAssets")
    return {
        "ticker":   ticker,
        "name":     info.get("longName") or info.get("shortName", ""),
        "category": info.get("category", ""),
        "family":   info.get("fundFamily", ""),
        "aum_b":    f"${aum/1e9:.1f}B" if aum else "—",
        "beta":     f"{info.get('beta3Year', ''):.2f}" if info.get("beta3Year") else "—",
        "type":     info.get("quoteType", ""),
    }


def main():
    if len(sys.argv) > 1:
        tickers = [t.upper() for t in sys.argv[1:]]
    else:
        if not TICKERS_FILE.exists():
            print(f"tickers.json not found at {TICKERS_FILE}")
            sys.exit(1)
        tickers = json.loads(TICKERS_FILE.read_text())

    rows = []
    for t in tickers:
        print(f"  fetching {t}...", end="\r")
        try:
            rows.append(fetch_info(t))
        except Exception as e:
            rows.append({"ticker": t, "name": f"ERROR: {e}", "category": "", "family": "", "aum_b": "", "beta": "", "type": ""})

    print(" " * 40, end="\r")

    col_w = {
        "ticker":   6,
        "name":     40,
        "category": 28,
        "family":   12,
        "aum_b":    8,
        "beta":     6,
        "type":     5,
    }
    header = "  ".join(k.upper().ljust(w) for k, w in col_w.items())
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(str(r[k]).ljust(w) for k, w in col_w.items()))


if __name__ == "__main__":
    main()
