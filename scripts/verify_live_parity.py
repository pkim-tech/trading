"""
Compares the Numba backtest kernels (backtester.py) against active_signals.py's real
live-orchestration functions (compute_buy_signal/check_sell_condition), replayed bar-by-bar.

Nominally the kernel is ground truth — if they disagree, the live orchestration layer has
a bug (derived-input drift), not strategies.py itself (both call into the same strategies.py
check_signal/check_exit). Reports the first divergent trade so the exact bar/rule can be
pinpointed. See docs/adr/0001-live-parity-sim-vs-backtest.md for why this compares against
active_signals.py directly instead of reimplementing the replay loop's own decision logic.

KNOWN, EXPECTED mismatches as of 2026-07-03 (see docs/backlog.md "Look-ahead bias..." entry
for full detail) — every case below currently reports MISMATCH, and that is not a live bug:
  1. Look-ahead bias in the KERNEL, not live: run_optimization_sweep.py's df_daily includes
     each day's own close in that day's SMA/std, and backtester.py's daily_idx looks up a
     bar's own calendar day — so the kernel's entry signal uses same-day information no
     intraday check could actually have. active_signals.compute_buy_signal's `today` cutoff
     is the correct, realistic behavior; here the kernel is the one that's wrong. Affects
     every strategy (all share the same generate_daily_indicators pattern + daily_idx
     plumbing), so even plain ZScoreBreakout (no other known gaps) mismatches on this alone.
  2. LimitOrderZScoreBreakout's live signal check uses current_price as a proxy for intrabar
     low (no true low available live — see compute_buy_signal's 'low' key). The kernel uses
     the real bar Low. This is a live-data-availability limitation, not a bug — see the ADR's
     Consequences section.
Until #1 is fixed (backlog item, needs a sweep rerun, out of scope for a quick patch), this
script cannot report a clean MATCH — its value right now is catching *new, additional*
divergence (e.g. via first-mismatch trade index/date moving in a run-to-run diff), not a
binary pass/fail.

v1.9/v1.10 (TrailingBuyZScoreBreakout/TrailingBothZScoreBreakout) are deliberately absent
from compare() below: active_signals.py has no live entry implementation for the
trailing-buy "wait for bounce" state machine yet (tracked as P0 #3), so comparing them here
would just restate that known gap rather than test derived-input correctness. kernel_trades()
still supports them so the wiring is ready once P0 #3 lands.

Usage: .venv/bin/python scripts/verify_live_parity.py
"""
import sys
import tempfile
import contextlib
from pathlib import Path
from datetime import datetime
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import active_signals
from backtester import run_backtest, run_backtest_v17, run_backtest_v18, run_backtest_v19, run_backtest_v110, run_backtest_v211

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


def _load(ticker):
    df = pd.read_csv(CACHE_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    df_daily = df.resample('D').last().dropna()
    return df, df_daily


@contextlib.contextmanager
def _throwaway_db():
    """check_sell_condition persists trail_state via a real DB write — give it a scratch
    DB per replay so trail-state round-trips correctly without touching the live DB."""
    with tempfile.TemporaryDirectory() as d:
        orig = active_signals.DB_PATH
        active_signals.DB_PATH = Path(d) / "parity_test.db"
        try:
            active_signals.ensure_tables()
            yield
        finally:
            active_signals.DB_PATH = orig


def replay(ticker, strategy_name, window, z_thresh, take_profit_pct, stop_loss_pct,
           max_hold_hours, target_hours=(9, 14), trail_pct=None):
    """Bar-by-bar replay that calls active_signals.py's real compute_buy_signal /
    check_sell_condition — mirrors exactly what active_signals.py would decide live,
    called every bar (streaming), including its DB-backed trail-state persistence."""
    df_hourly, df_daily = _load(ticker)

    uses_fixed_sl = active_signals._uses_fixed_sl(strategy_name)
    node = {
        'ticker': ticker, 'strategy': strategy_name, 'version': 'test',
        'window': window, 'z_score_threshold': z_thresh,
        'take_profit': take_profit_pct, 'stop_loss': stop_loss_pct,
        'max_hold_hours': max_hold_hours,
        'trail_pct': trail_pct if uses_fixed_sl else None,
        'fixed_sl': stop_loss_pct if uses_fixed_sl else None,
    }

    trades = []
    in_trade = False
    entry_price = entry_time = None
    position_id = None

    with _throwaway_db():
        for i, ts in enumerate(df_hourly.index):
            row = df_hourly.iloc[i]
            cp, low, high = row['Close'], row['Low'], row['High']
            df_slice = df_hourly.iloc[:i + 1]

            if in_trade:
                pos = next(p for p in active_signals.get_open_positions() if p['id'] == position_id)
                reason, price, _ = active_signals.check_sell_condition(
                    pos, cp, ts, at_bar_close=True, low=low, high=high, df_hourly=df_slice)
                if reason:
                    signal_time = datetime.strptime(pos['signal_time'], '%Y-%m-%d %H:%M:%S')
                    hours_held = active_signals._bars_held(df_slice, signal_time)
                    pc = (price - entry_price) / entry_price
                    result = 'WIN' if reason in ('TP', 'WIN') else 'LOSS' if reason in ('SL', 'LOSS') else ('TWIN' if pc > 0 else 'TLOSS')
                    trades.append({'Entry Time': entry_time, 'Exit Time': ts, 'Entry Price': entry_price,
                                    'Exit Price': price, 'hours_held': hours_held, 'Result': result, 'Return': pc})
                    active_signals.close_position(position_id, exit_signal_price=cp, exit_price=price,
                                                   exit_time=ts, exit_reason=reason)
                    in_trade = False
                    position_id = None
                continue

            if ts.hour not in target_hours:
                continue

            # Limit-entry strategies: production polls all day (5-min cadence,
            # active_signals.py's intrabar fill-detection loop) and would catch a wick
            # through the band even if the bar's close recovers — mirror the kernel's
            # own Low-based check (backtester.py's _simulate_limit/_simulate_limit_trail)
            # rather than Close.
            is_limit_entry = strategy_name in ('LimitOrderZScoreBreakout', 'LimitOrderTrailingExit')
            entry_check_price = low if is_limit_entry else cp
            sig = active_signals.compute_buy_signal(
                node, as_of=ts, price_override=entry_check_price,
                df_hourly_override=df_slice, df_daily_override=df_daily[df_daily.index <= ts])
            if sig is None or sig['signal'] != 'BUY':
                continue

            entry_price = sig['lower_band'] if is_limit_entry else cp
            entry_time = ts
            active_signals.open_position(node, signal_price=cp, signal_time=ts, entry_price=entry_price, entry_time=ts)
            position_id = next(p['id'] for p in active_signals.get_open_positions()
                                if p['ticker'] == ticker and p['window'] == window)
            in_trade = True

        if in_trade:
            cp = df_hourly['Close'].iloc[-1]
            pc = (cp - entry_price) / entry_price
            signal_time = datetime.strptime(entry_time.strftime('%Y-%m-%d %H:%M:%S'), '%Y-%m-%d %H:%M:%S')
            hours_held = active_signals._bars_held(df_hourly, signal_time)
            trades.append({'Entry Time': entry_time, 'Exit Time': df_hourly.index[-1], 'Entry Price': entry_price,
                            'Exit Price': cp, 'hours_held': hours_held, 'Result': 'OPEN', 'Return': pc})

    return trades


def kernel_trades(ticker, strategy_name, window, z_thresh, take_profit_pct, stop_loss_pct,
                   max_hold_hours, target_hours=(9, 14), trail_pct=None, trail_buy_pct=None):
    df_hourly, df_daily = _load(ticker)
    strat_cls = getattr(active_signals.strategies, strategy_name)
    strat = strat_cls(window=window, z_score_threshold=z_thresh)
    indicators = strat.generate_daily_indicators(df_daily)

    if strategy_name == 'TrailingBothZScoreBreakout':
        return run_backtest_v110(df_hourly, indicators, ticker, target_hours=target_hours,
                                  take_profit=take_profit_pct/100.0, stop_loss=stop_loss_pct/100.0,
                                  max_hours_to_hold=max_hold_hours, z_score_threshold=z_thresh,
                                  trail_buy_pct=trail_buy_pct/100.0, trail_pct=trail_pct/100.0)
    elif strategy_name == 'TrailingBuyZScoreBreakout':
        return run_backtest_v19(df_hourly, indicators, ticker, target_hours=target_hours,
                                 take_profit=take_profit_pct/100.0, stop_loss=stop_loss_pct/100.0,
                                 max_hours_to_hold=max_hold_hours, z_score_threshold=z_thresh,
                                 trail_buy_pct=trail_buy_pct/100.0)
    elif strategy_name == 'TrailingExitZScoreBreakout':
        return run_backtest_v18(df_hourly, indicators, ticker, target_hours=target_hours,
                                 take_profit=take_profit_pct/100.0, stop_loss=stop_loss_pct/100.0,
                                 max_hours_to_hold=max_hold_hours, z_score_threshold=z_thresh,
                                 trail_pct=trail_pct/100.0)
    elif strategy_name == 'LimitOrderTrailingExit':
        return run_backtest_v211(df_hourly, indicators, ticker, target_hours=target_hours,
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
