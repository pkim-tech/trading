"""
Compares the Numba backtest kernels (backtester.py) against a bar-by-bar replay
using the live-monitoring decision logic (strategies.py check_signal/check_exit).

Ground truth is the kernel — if they disagree, the live logic has a bug (or vice versa).
Reports the first divergent trade so the exact bar/rule can be pinpointed.

Usage: .venv/bin/python scripts/verify_live_parity.py
"""
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import strategies
from backtester import run_backtest, run_backtest_v17, run_backtest_v18

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"

_RESULT_SIGN = {'WIN': 'WIN', 'TWIN': 'WIN', 'LOSS': 'LOSS', 'TLOSS': 'LOSS', 'OPEN': 'OPEN'}


def _load(ticker):
    df = pd.read_csv(CACHE_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    df_daily = df.resample('D').last().dropna()
    return df, df_daily


def replay(ticker, strategy_name, window, z_thresh, take_profit_pct, stop_loss_pct,
           max_hold_hours, target_hours=(9, 14), trail_pct=None):
    """Bar-by-bar replay using strategies.py check_signal/check_exit — mirrors what
    active_signals.py would decide live, called every bar (streaming)."""
    df_hourly, df_daily = _load(ticker)
    strat_cls = getattr(strategies, strategy_name)
    kwargs = {'window': window, 'z_score_threshold': z_thresh}
    if trail_pct is not None:
        kwargs['trail_pct'] = trail_pct / 100.0
    strat = strat_cls(**kwargs)
    indicators = strat.generate_daily_indicators(df_daily)

    take_profit = take_profit_pct / 100.0
    stop_loss   = stop_loss_pct / 100.0

    trades = []
    in_trade = False
    entry_price = entry_time = entry_bar_idx = None
    state = {}
    last_bar_seen = None

    daily_lookup = {d.strftime('%Y-%m-%d'): i for i, d in enumerate(indicators.index)}

    for i, ts in enumerate(df_hourly.index):
        row = df_hourly.iloc[i]
        cp, low, high = row['Close'], row['Low'], row['High']
        di = daily_lookup.get(ts.strftime('%Y-%m-%d'))
        at_bar_close = True  # every hourly row here IS a bar close (historical replay)

        if in_trade:
            hours_held = i - entry_bar_idx
            ctx = {
                'current_price': cp, 'low': low, 'high': high,
                'entry_price': entry_price,
                'take_profit': take_profit, 'stop_loss': stop_loss,
                'max_hours_to_hold': max_hold_hours,
                'hours_held': hours_held, 'at_bar_close': at_bar_close,
                'state': state,
            }
            reason, price, state = strat.check_exit(ctx)
            if reason:
                pc = (price - entry_price) / entry_price
                result = 'WIN' if reason in ('TP', 'WIN') else 'LOSS' if reason in ('SL', 'LOSS') else ('TWIN' if pc > 0 else 'TLOSS')
                trades.append({'Entry Time': entry_time, 'Exit Time': ts, 'Entry Price': entry_price,
                                'Exit Price': price, 'hours_held': hours_held, 'Result': result, 'Return': pc})
                in_trade = False
                state = {}
            continue

        if di is None:
            continue
        ind_row = indicators.iloc[di]
        if ts.hour not in target_hours:
            continue

        sig_ctx = {
            'current_price': cp, 'low': low,
            'sma': ind_row['SMA'], 'std': ind_row['Std'],
            'trend': ind_row['Trend_Filter'] if 'Trend_Filter' in indicators.columns else None,
        }
        signal = strat.check_signal(sig_ctx)
        if signal == 'BUY':
            in_trade = True
            entry_price = low if strategy_name == 'LimitOrderZScoreBreakout' else cp
            if strategy_name == 'LimitOrderZScoreBreakout':
                entry_price = ind_row['SMA'] - ind_row['Std'] * z_thresh  # lower_band, matches kernel
            entry_time = ts
            entry_bar_idx = i
            state = {}

    if in_trade:
        cp = df_hourly['Close'].iloc[-1]
        pc = (cp - entry_price) / entry_price
        trades.append({'Entry Time': entry_time, 'Exit Time': df_hourly.index[-1], 'Entry Price': entry_price,
                        'Exit Price': cp, 'hours_held': len(df_hourly) - 1 - entry_bar_idx, 'Result': 'OPEN', 'Return': pc})

    return trades


def kernel_trades(ticker, strategy_name, window, z_thresh, take_profit_pct, stop_loss_pct,
                   max_hold_hours, target_hours=(9, 14), trail_pct=None):
    df_hourly, df_daily = _load(ticker)
    strat_cls = getattr(strategies, strategy_name)
    strat = strat_cls(window=window, z_score_threshold=z_thresh)
    indicators = strat.generate_daily_indicators(df_daily)

    if strategy_name == 'TrailingExitZScoreBreakout':
        return run_backtest_v18(df_hourly, indicators, ticker, target_hours=target_hours,
                                 take_profit=take_profit_pct/100.0, stop_loss=stop_loss_pct/100.0,
                                 max_hours_to_hold=max_hold_hours, z_score_threshold=z_thresh,
                                 trail_pct=trail_pct/100.0)
    elif strategy_name == 'LimitOrderZScoreBreakout':
        return run_backtest_v17(df_hourly, indicators, ticker, target_hours=target_hours,
                                 take_profit=take_profit_pct/100.0, stop_loss=stop_loss_pct/100.0,
                                 max_hours_to_hold=max_hold_hours, z_score_threshold=z_thresh)
    else:
        return run_backtest(df_hourly, indicators, ticker, target_hours=target_hours,
                             take_profit=take_profit_pct/100.0, stop_loss=stop_loss_pct/100.0,
                             max_hours_to_hold=max_hold_hours, z_score_threshold=z_thresh)


def compare(ticker, strategy_name, window, z_thresh, tp, sl, hold, trail_pct=None):
    kt = kernel_trades(ticker, strategy_name, window, z_thresh, tp, sl, hold, trail_pct=trail_pct)
    rt = replay(ticker, strategy_name, window, z_thresh, tp, sl, hold, trail_pct=trail_pct)

    kt_closed = [t for t in kt if t['Result'] in ('WIN', 'LOSS', 'TWIN', 'TLOSS')]
    rt_closed = [t for t in rt if t['Result'] in ('WIN', 'LOSS', 'TWIN', 'TLOSS')]

    print(f"\n=== {ticker} {strategy_name} w={window} z={z_thresh} tp={tp} sl={sl} hold={hold} ===")
    print(f"kernel trades: {len(kt_closed)}   replay trades: {len(rt_closed)}")

    n = min(len(kt_closed), len(rt_closed))
    mismatch = False
    for i in range(n):
        k, r = kt_closed[i], rt_closed[i]
        if (k['Entry Time'] != r['Entry Time'] or k['Exit Time'] != r['Exit Time']
                or k['Result'] != r['Result'] or abs(k['Return'] - r['Return']) > 1e-6):
            print(f"  MISMATCH at trade #{i}:")
            print(f"    kernel: entry={k['Entry Time']} exit={k['Exit Time']} result={k['Result']} ret={k['Return']:.4f}")
            print(f"    replay: entry={r['Entry Time']} exit={r['Exit Time']} result={r['Result']} ret={r['Return']:.4f}")
            mismatch = True
            break
    if not mismatch and len(kt_closed) == len(rt_closed):
        print("  MATCH — all trades identical")
    elif not mismatch:
        print(f"  Trade count differs ({len(kt_closed)} vs {len(rt_closed)}) but first {n} match")
    return not mismatch and len(kt_closed) == len(rt_closed)


if __name__ == '__main__':
    results = []
    results.append(compare('SOXL', 'ZScoreBreakout', 10, 1.5, 29, 29, 119))
    results.append(compare('TQQQ', 'LimitOrderZScoreBreakout', 10, 2.0, 19, 19, 119))
    results.append(compare('HIBL', 'LimitOrderZScoreBreakout', 10, 2.0, 27, 23, 126))
    results.append(compare('AGQ', 'TrailingExitZScoreBreakout', 10, 1.0, 3, 15, 63, trail_pct=6))

    print(f"\n{'ALL MATCH' if all(results) else 'FAILURES PRESENT'}")
