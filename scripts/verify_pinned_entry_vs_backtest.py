"""Part 4 Deliverable 1 -- backtest-replay validation for the pinned entry-check
(active_signals._scan_pinned_entry) before trusting it with any real order
placement. For each automation-enabled, entry_timing='open_check',
TrailingExitZScoreBreakout node (the 6 watchlist-65 tickers Part 4 automates:
AGQ, DPST, KORU, NUGT, UDOW, YANG), runs the real backtest kernel
(backtester.run_backtest_dispatch) against cached history to get every trade's
real Entry Time/Entry Price, determines which branch fired (Open at the h0
target hour, or Close at h1) by comparing Entry Price against that bar's real
Open/Close, then replays signals_compute.compute_buy_signal(node, as_of=...,
price_override=that bar's real Open-or-Close) and asserts it reproduces the
same BUY decision -- i.e. that live-side compute_buy_signal, fed the exact
price the backtest kernel used, agrees with the backtest's own entry decision.

Offline, no live API calls -- read-only against cache/research/*.csv and the
live watch_list. Usage:
    .venv/bin/python scripts/verify_pinned_entry_vs_backtest.py [--tickers AGQ,KORU]
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
import signals_db as db
from signals_compute import compute_buy_signal
from backtester import prep_inputs, run_backtest_dispatch

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "research"
OPEN_PRICE_TOLERANCE = 1e-6


def _load_hourly(ticker):
    df = pd.read_csv(CACHE_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    df_daily = df.resample('D').last().dropna()
    return df, df_daily


def _target_nodes(tickers=None):
    nodes = [
        n for n in db.get_watchlist()
        if n['strategy'] == 'TrailingExitZScoreBreakout' and n.get('entry_timing') == 'open_check'
    ]
    if tickers:
        nodes = [n for n in nodes if n['ticker'] in tickers]
    return nodes


def verify_node(node):
    ticker = node['ticker']
    df_hourly, df_daily = _load_hourly(ticker)
    strat_cls = getattr(strategies, node['strategy'])
    strat = strat_cls(window=int(node['window']), z_score_threshold=float(node.get('z_score_threshold', 2.0)))
    indicators = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_hourly, indicators)

    trades = run_backtest_dispatch(
        strat_cls, df_hourly, indicators, ticker,
        take_profit=node.get('take_profit') or 0, sl_raw=node.get('trail_sell_pct') or 0,
        max_hours_to_hold=node['max_hold_hours'], z_score_threshold=node.get('z_score_threshold', 2.0),
        fixed_sl=node.get('fixed_sl') or 0, entry_timing='open_check', prep=p,
    )

    mismatches = []
    checked = 0
    for t in trades:
        entry_time = t['Entry Time']
        entry_price = t['Entry Price']
        bar = df_hourly.loc[entry_time]
        real_open, real_close = float(bar['Open']), float(bar['Close'])
        if abs(entry_price - real_open) < OPEN_PRICE_TOLERANCE:
            branch, price_used = 'open', real_open
        elif abs(entry_price - real_close) < OPEN_PRICE_TOLERANCE:
            branch, price_used = 'close', real_close
        else:
            # Neither -- a gap-through-trigger fill (kernel fills at the real
            # Open when it's already crossed the trigger, see 2026-07-19/20
            # exit/entry gap fixes) or an edge case; not a mismatch in the
            # replay sense (compute_buy_signal has no gap-fill concept), skip.
            continue

        checked += 1
        as_of = entry_time.normalize()
        sig = compute_buy_signal(node, as_of=as_of, price_override=price_used,
                                  df_hourly_override=df_hourly, df_daily_override=df_daily)
        if sig is None or sig['signal'] != 'BUY':
            mismatches.append({
                'ticker': ticker, 'entry_time': entry_time, 'branch': branch,
                'entry_price': entry_price, 'replay_signal': sig['signal'] if sig else 'NO_DATA',
            })
    return checked, mismatches


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', default=None, help="comma-separated, default: all 6 in scope")
    args = ap.parse_args()
    tickers = {t.strip().upper() for t in args.tickers.split(',')} if args.tickers else None

    db.ensure_tables()
    nodes = _target_nodes(tickers)
    if not nodes:
        print("No TrailingExitZScoreBreakout / entry_timing='open_check' nodes found on the active watchlist.")
        return

    all_mismatches = []
    for node in nodes:
        checked, mismatches = verify_node(node)
        status = "PASS" if not mismatches else "FAIL"
        print(f"{node['ticker']:<6} {status}  {checked - len(mismatches)}/{checked} entries reproduced")
        all_mismatches += mismatches

    if all_mismatches:
        out = Path(__file__).resolve().parent.parent / "output" / "pinned_entry_verify_mismatches.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(all_mismatches).to_csv(out, index=False)
        print(f"\n{len(all_mismatches)} mismatch(es) written to {out}")
    else:
        print("\nAll replayed entries reproduced the backtest's BUY decision.")


if __name__ == '__main__':
    main()
