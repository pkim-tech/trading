"""Part 4 Deliverable 2 (follow-up half) -- joins signals_db.open_price_quality_log
(written live by active_signals._scan_pinned_entry every pinned-check fetch) against
the real cached hourly Open/Close for the same bar, once it's landed (run this the
next day, after the daily collector has appended that bar). Reports whether
get_session_open_price's `openPrice` was populated promptly (is_true_open) and how
close the fetched price came to the bar's real recorded Open/Close -- gates flipping
any automation-enabled ticker from paper-only to real order placement.

Needs at least one real trading day of the daemon running with the pinned-check
infra live -- can't be backfilled. Usage:
    .venv/bin/python scripts/verify_open_price_quality.py [--since 2026-07-22]
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "research"


def _load_hourly(ticker):
    df = pd.read_csv(CACHE_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.sort_index()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default=None, help="YYYY-MM-DD, only rows logged on/after this date")
    args = ap.parse_args()

    db.ensure_tables()
    rows = db.get_open_price_quality_log(since=args.since)
    if not rows:
        print("No open_price_quality_log rows yet -- run the daemon through at least one "
              "real pinned-check window first (9:30/10:30/14:30/15:30 ET).")
        return

    hourly_cache = {}
    report = []
    for r in rows:
        ticker = r['ticker']
        if ticker not in hourly_cache:
            try:
                hourly_cache[ticker] = _load_hourly(ticker)
            except FileNotFoundError:
                hourly_cache[ticker] = None
        df = hourly_cache[ticker]
        ts = pd.Timestamp(r['ts'])
        bar_time = ts.normalize() + pd.Timedelta(hours=r['target_h'], minutes=r['target_m'])
        real_open = real_close = None
        if df is not None and bar_time in df.index:
            real_open = float(df.loc[bar_time, 'Open'])
            real_close = float(df.loc[bar_time, 'Close'])
        report.append({
            'ts': r['ts'], 'ticker': ticker, 'target': f"{r['target_h']:02d}:{r['target_m']:02d}",
            'fetched_price': r['price'], 'is_true_open': bool(r['is_true_open']),
            'real_open': real_open, 'real_close': real_close,
            'drift_vs_open_pct': (r['price'] - real_open) / real_open * 100 if real_open else None,
        })

    df_report = pd.DataFrame(report)
    print(df_report.to_string(index=False))

    n = len(df_report)
    true_open_rate = df_report['is_true_open'].mean() * 100
    print(f"\n{n} pinned-check fetches logged. openPrice populated promptly (is_true_open) "
          f"in {true_open_rate:.1f}% of cases.")
    drifts = df_report['drift_vs_open_pct'].dropna()
    if not drifts.empty:
        print(f"vs. real cached Open -- mean |drift|: {drifts.abs().mean():.3f}%  max |drift|: {drifts.abs().max():.3f}%")

    out = Path(__file__).resolve().parent.parent / "output" / "open_price_quality_report.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df_report.to_csv(out, index=False)
    print(f"Written to {out}")


if __name__ == '__main__':
    main()
