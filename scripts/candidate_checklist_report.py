"""Consolidated watchlist-candidate-checklist report: for a list of tickers, pulls
each one's real v4 winning node (stop_loss=1%, entry_timing=open_check) plus
checks 1 (macro/trend), 2/3 (trailing-buy/sell fill-drift vs a 5-min replay), 6
(stock splits), 7 (fill-logic optimism -- already built into v4's robust_alpha,
no separate run needed), and 8 (trade-count fluke, read straight off `trades`).
Reuses the existing checklist scripts' functions directly (verify_trailing_buy/
sell_resolution.py, check_stock_splits.py) rather than re-implementing any of
their logic -- see docs/watchlist_candidate_checklist.md for what each check
means and why. Checks 4/9/10 (win-rate stability, same-day-block sensitivity)
need a full trade replay per ticker and are intentionally left out of this
summary pass -- run them separately for any ticker that clears this screen.

Usage: .venv/bin/python scripts/candidate_checklist_report.py TICKER [TICKER ...] [out.csv]
"""
import sys
from pathlib import Path
from datetime import timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import pandas as pd
import yfinance as yf

from check_stock_splits import check_ticker as _check_splits
from verify_trailing_buy_resolution import (
    find_hourly_signals, replay_five_min as _replay_buy, _load_hourly,
)
from verify_trailing_sell_resolution import (
    find_hourly_trailing_exits, replay_five_min as _replay_sell,
)

RESEARCH_DB = Path(__file__).resolve().parent.parent / "cache" / "research" / "trading_universe.db"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "research"
FIVE_MIN_LOOKBACK_DAYS = 58
CLIFF_RADIUS = 2

ROBUST_ALPHA_SQL = "MIN(alpha_vs_spy, alpha_vs_spy_pessimistic, alpha_vs_spy_certain)"


def best_v4_node(conn, ticker):
    row = conn.execute(f"""
        SELECT window, max_hold_hours, z_score_threshold, trail_buy_pct, trail_sell_pct,
               arm_sell_pct, trades, win_rate, win_twin_rate,
               {ROBUST_ALPHA_SQL} AS robust_alpha
        FROM backtest_cache
        WHERE version='v4' AND ticker=? AND stop_loss=1 AND entry_timing='open_check' AND trades > 0
        ORDER BY robust_alpha DESC LIMIT 1
    """, (ticker,)).fetchone()
    if not row:
        return None
    (window, hold, z, tb, ts, arm, trades, wr, wtr, robust_alpha) = row

    worst = conn.execute(f"""
        SELECT MIN({ROBUST_ALPHA_SQL}) FROM backtest_cache
        WHERE version='v4' AND ticker=? AND stop_loss=1 AND entry_timing='open_check'
          AND window=? AND z_score_threshold=?
          AND arm_sell_pct BETWEEN ? AND ?
          AND trail_buy_pct BETWEEN ? AND ?
          AND trail_sell_pct BETWEEN ? AND ?
          AND max_hold_hours BETWEEN ? AND ?
          AND trades > 0
    """, (ticker, window, z,
          arm - CLIFF_RADIUS, arm + CLIFF_RADIUS,
          tb - CLIFF_RADIUS, tb + CLIFF_RADIUS,
          ts - 1, ts + 1,
          hold - 7, hold + 7)).fetchone()[0]
    worst_neighbor = float(worst) if worst is not None else robust_alpha

    return dict(window=int(window), hold=int(hold), z=float(z), tb=float(tb), ts=float(ts),
                arm=float(arm), trades=int(trades), win_rate=float(wr),
                win_twin_rate=float(wtr or 0), win_or_time_win_rate=float(wr) + float(wtr or 0),
                robust_alpha=float(robust_alpha), worst_neighbor=worst_neighbor,
                cliff_safe=worst_neighbor >= 0)


def trend_check(ticker):
    df = pd.read_csv(CACHE_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    daily = df.resample('D').last().dropna()
    if len(daily) < 64:
        return None, None
    r30 = (daily['Close'].iloc[-1] / daily['Close'].iloc[-21] - 1) * 100
    r90 = (daily['Close'].iloc[-1] / daily['Close'].iloc[-63] - 1) * 100
    return float(r30), float(r90)


def fill_drift_buy(ticker, window, z, tb_pct, hold):
    cutoff = pd.Timestamp.now().normalize() - timedelta(days=FIVE_MIN_LOOKBACK_DAYS)
    tb = tb_pct / 100.0
    df_hourly, _ = _load_hourly(ticker)
    recent = df_hourly[df_hourly.index >= cutoff]
    intrahour_pct = (recent['High'] - recent['Low']) / recent['Close'] * 100
    ratio = intrahour_pct.median() / (tb * 100) if len(recent) else None

    signals = find_hourly_signals(ticker, window, z, tb, hold, cutoff)
    if not signals:
        return None, ratio, 0
    df_5m = yf.download(ticker, period="60d", interval="5m", multi_level_index=False, progress=False)
    df_5m.index = pd.to_datetime(df_5m.index).tz_localize(None)
    diffs = []
    for s in signals:
        r = _replay_buy(ticker, df_5m, s['signal_time'], s['signal_close'], tb, s['cutoff_time'])
        if r:
            diffs.append((r['five_min_entry_price'] - s['hourly_entry_price']) / s['hourly_entry_price'] * 100)
    if not diffs:
        return None, ratio, 0
    return sum(diffs) / len(diffs), ratio, len(diffs)


def fill_drift_sell(ticker, window, z, tb_pct, ts_pct, arm_pct, hold):
    cutoff = pd.Timestamp.now().normalize() - timedelta(days=FIVE_MIN_LOOKBACK_DAYS)
    tb, ts, arm = tb_pct / 100.0, ts_pct / 100.0, arm_pct / 100.0

    events = find_hourly_trailing_exits(ticker, window, z, tb, ts, arm, hold, cutoff)
    if not events:
        return None, 0
    df_5m = yf.download(ticker, period="60d", interval="5m", multi_level_index=False, progress=False)
    df_5m.index = pd.to_datetime(df_5m.index).tz_localize(None)
    diffs = []
    for ev in events:
        r = _replay_sell(df_5m, ev['arm_time'], ev['peak_at_arm'], ts, ev['cutoff_time'])
        if r:
            diffs.append((r['five_min_exit_price'] - ev['hourly_exit_price']) / ev['hourly_exit_price'] * 100)
    if not diffs:
        return None, 0
    return sum(diffs) / len(diffs), len(diffs)


def splits_check(ticker):
    csv_path = CACHE_DIR / f"{ticker}_1h.csv"
    flags = _check_splits(csv_path)
    return "; ".join(f"{f['split_date']} ({f['ratio']}x)" for f in flags) or "none"


def run(tickers):
    rows = []
    with sqlite3.connect(RESEARCH_DB, timeout=60) as conn:
        for ticker in tickers:
            print(f"[{ticker}] pulling v4 best node...", file=sys.stderr)
            node = best_v4_node(conn, ticker)
            if node is None:
                rows.append(dict(ticker=ticker, note="no v4 SL=1/open_check node found"))
                continue

            r30, r90 = trend_check(ticker)
            buy_mean, buy_ratio, buy_n = fill_drift_buy(ticker, node['window'], node['z'], node['tb'], node['hold'])
            sell_mean, sell_n = fill_drift_sell(
                ticker, node['window'], node['z'], node['tb'], node['ts'], node['arm'], node['hold'])
            splits = splits_check(ticker)

            rows.append(dict(
                ticker=ticker, **node,
                trend_30d=r30, trend_90d=r90,
                buy_drift_mean=buy_mean, buy_drift_ratio=buy_ratio, buy_drift_n=buy_n,
                sell_drift_mean=sell_mean, sell_drift_n=sell_n,
                splits=splits,
            ))
    return pd.DataFrame(rows)


if __name__ == '__main__':
    args = sys.argv[1:]
    out_path = 'logs/candidate_checklist_report.csv'
    if args and args[-1].endswith('.csv'):
        out_path = args.pop()
    tickers = args
    if not tickers:
        print(__doc__)
        sys.exit(1)
    df = run(tickers)
    df.to_csv(out_path, index=False)
    print(f"\nWrote {len(df)} rows to {out_path}")
    pd.set_option('display.width', 200)
    print(df.to_string(index=False))
