"""Real closed-trade P&L grouped by calendar year, then ticker -- the actually
tax-relevant report (real trade_log rows, not backtest hypotheticals), built
2026-08-13 alongside the backtest-report calendar-year breakdown
(scripts/calendar_year_returns.py). $ matters here specifically, unlike the
backtest reports where % is the meaningful unit -- this is for real numbers.

Deliberately NOT wired into k1_tax.py/k1_tax_forecast.py: that tool is scoped
to Section 1256/K-1 PTP tax treatment (60/40 split, per-PTP passive-loss
silo), which only applies to a handful of instruments (AGQ today) -- most
trade_log rows are ordinary short-term gains under a different tax
treatment, so auto-feeding everything into k1_tax.py's trade table would
misclassify most of it. Pull the AGQ-specific numbers from this report by
hand and feed them into `k1_tax_forecast.py trade` if wanted.

Real dollar P&L per trade = (exit_price - entry_price) * shares (long-only
strategies here, matches the existing sign convention elsewhere in this
codebase). Excludes is_dry_run_sim=1 rows by default (not real fills) and
paper_trade_log entirely unless --include-paper is passed.

Usage:
  .venv/bin/python scripts/annual_pnl_report.py
  .venv/bin/python scripts/annual_pnl_report.py --account brokerage --account ira
  .venv/bin/python scripts/annual_pnl_report.py --include-paper
  .venv/bin/python scripts/annual_pnl_report.py --csv annual_pnl
  .venv/bin/python scripts/annual_pnl_report.py --xlsx annual_pnl
"""
import argparse
import csv
import sqlite3
from datetime import datetime
from pathlib import Path

LIVE_DB = Path(__file__).resolve().parent.parent / "cache/live/trading_live.db"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def _fetch_trades(include_paper, accounts):
    con = sqlite3.connect(LIVE_DB)
    con.row_factory = sqlite3.Row
    rows = []

    real_sql = """SELECT ticker, account, entry_price, exit_price, shares, pnl_pct, exit_time, 'real' AS source
                  FROM trade_log
                  WHERE exit_time IS NOT NULL AND COALESCE(is_dry_run_sim, 0) = 0
                        AND entry_price IS NOT NULL AND exit_price IS NOT NULL AND shares IS NOT NULL"""
    params = []
    if accounts:
        real_sql += f" AND account IN ({','.join('?' * len(accounts))})"
        params.extend(accounts)
    rows.extend(con.execute(real_sql, params).fetchall())

    if include_paper:
        paper_sql = """SELECT ticker, account, entry_price, exit_price, shares, pnl_pct, exit_time, 'paper' AS source
                        FROM paper_trade_log
                        WHERE exit_time IS NOT NULL
                              AND entry_price IS NOT NULL AND exit_price IS NOT NULL AND shares IS NOT NULL"""
        paper_params = []
        if accounts:
            paper_sql += f" AND account IN ({','.join('?' * len(accounts))})"
            paper_params.extend(accounts)
        rows.extend(con.execute(paper_sql, paper_params).fetchall())

    con.close()
    return rows


def build_report(include_paper=False, accounts=None):
    """Returns (by_year, tickers_seen) where by_year is
    {year: {ticker: {'pnl_dollars', 'trades', 'wins'}}}, ordered by year ascending."""
    rows = _fetch_trades(include_paper, accounts)
    by_year = {}
    tickers_seen = set()
    for r in rows:
        try:
            year = datetime.fromisoformat(r["exit_time"]).year
        except (TypeError, ValueError):
            continue
        pnl_dollars = (r["exit_price"] - r["entry_price"]) * r["shares"]
        ticker = r["ticker"]
        tickers_seen.add(ticker)
        bucket = by_year.setdefault(year, {}).setdefault(
            ticker, {"pnl_dollars": 0.0, "trades": 0, "wins": 0})
        bucket["pnl_dollars"] += pnl_dollars
        bucket["trades"] += 1
        if pnl_dollars > 0:
            bucket["wins"] += 1
    return by_year, sorted(tickers_seen)


def _rows_for_export(by_year):
    out = []
    for year in sorted(by_year):
        year_total = 0.0
        for ticker in sorted(by_year[year]):
            b = by_year[year][ticker]
            year_total += b["pnl_dollars"]
            out.append({
                "year": year, "ticker": ticker, "pnl_dollars": round(b["pnl_dollars"], 2),
                "trades": b["trades"], "win_rate_pct": round(100 * b["wins"] / b["trades"], 1),
            })
        out.append({"year": year, "ticker": "TOTAL", "pnl_dollars": round(year_total, 2),
                     "trades": sum(b["trades"] for b in by_year[year].values()), "win_rate_pct": None})
    return out


def print_report(by_year):
    if not by_year:
        print("No real closed trades found.")
        return
    for year in sorted(by_year):
        year_total = sum(b["pnl_dollars"] for b in by_year[year].values())
        year_trades = sum(b["trades"] for b in by_year[year].values())
        print(f"\n{year}  (total: ${year_total:+,.2f}, {year_trades} trades)")
        for ticker in sorted(by_year[year]):
            b = by_year[year][ticker]
            wr = 100 * b["wins"] / b["trades"] if b["trades"] else 0.0
            print(f"  {ticker:8s} ${b['pnl_dollars']:+12,.2f}  {b['trades']:4d} trades  wr={wr:5.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", action="append", default=None, help="filter to this account (repeatable)")
    ap.add_argument("--include-paper", action="store_true",
                     help="also include paper_trade_log rows (off by default -- this report is meant "
                          "for real tax numbers)")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--xlsx", default=None)
    args = ap.parse_args()

    by_year, tickers = build_report(include_paper=args.include_paper, accounts=args.account)
    print_report(by_year)

    if args.csv or args.xlsx:
        rows = _rows_for_export(by_year)

    if args.csv:
        out_path = OUTPUT_DIR / f"{args.csv}.csv"
        out_path.parent.mkdir(exist_ok=True)
        with open(out_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["year", "ticker", "pnl_dollars", "trades", "win_rate_pct"])
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote {out_path} ({len(rows)} rows)")

    if args.xlsx:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Annual P&L"
        ws.append(["year", "ticker", "pnl_dollars", "trades", "win_rate_pct"])
        for row in rows:
            ws.append([row["year"], row["ticker"], row["pnl_dollars"], row["trades"], row["win_rate_pct"]])
        out_path = OUTPUT_DIR / f"{args.xlsx}.xlsx"
        out_path.parent.mkdir(exist_ok=True)
        wb.save(out_path)
        print(f"\nWrote {out_path} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
