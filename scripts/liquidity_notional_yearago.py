"""
One-off: compare each live-watchlist ticker's 1% ADV liquidity cap
(max_notional = avg_vol_10d * last_price * 0.01, same formula as
run_optimization_sweep.py:913 / pages/11_Universe_Scan.py:99) a year ago
vs. today, and post the table to Slack.

Not wired into the daemon -- run manually:
    python scripts/liquidity_notional_yearago.py
"""
import sys
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(__file__).rsplit('/', 2)[0])
import signals_db as db
from signals_blocks import _post_message


def _avg_vol_and_price(ticker: str, end_date: datetime) -> tuple[float, float] | None:
    """10 trading days of daily bars ending at/just before end_date."""
    start = end_date - timedelta(days=20)
    df = yf.download(ticker, start=start, end=end_date + timedelta(days=1),
                      interval="1d", progress=False, auto_adjust=False)
    if df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.tail(10)
    if len(df) < 5:
        return None
    avg_vol = float(df["Volume"].mean())
    last_price = float(df["Close"].iloc[-1])
    return avg_vol, last_price


def main():
    tickers = sorted({row["ticker"] for row in db.get_watchlist()})
    today = datetime.now()
    year_ago = today - timedelta(days=365)

    rows = []
    for ticker in tickers:
        now_stats = _avg_vol_and_price(ticker, today)
        past_stats = _avg_vol_and_price(ticker, year_ago)
        if now_stats is None or past_stats is None:
            print(f"  [skip] {ticker} -- no data")
            continue
        now_vol, now_price = now_stats
        past_vol, past_price = past_stats
        now_notional = now_vol * now_price * 0.01
        past_notional = past_vol * past_price * 0.01
        pct_change = (now_notional - past_notional) / past_notional * 100 if past_notional else None
        rows.append((ticker, past_notional, now_notional, pct_change))
        print(f"  {ticker:6s} 1yr-ago 1% notional=${past_notional:>12,.0f}  "
              f"now=${now_notional:>12,.0f}  change={pct_change:+.0f}%")

    rows.sort(key=lambda r: r[2])  # ascending by current notional -- thinnest first

    lines = [f"{'Ticker':6s} {'1yr ago':>14s} {'Now':>14s} {'Change':>8s}"]
    for ticker, past_notional, now_notional, pct_change in rows:
        lines.append(f"{ticker:6s} ${past_notional:>12,.0f} ${now_notional:>12,.0f} {pct_change:>7.0f}%")
    table = "```\n" + "\n".join(lines) + "\n```"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "1% ADV Notional Cap — 1yr ago vs now"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": table}},
    ]
    _post_message("1% ADV notional cap — 1yr ago vs now", blocks=blocks)
    print("\nPosted to Slack.")


if __name__ == "__main__":
    main()
