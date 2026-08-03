"""
Research script: does FFT-based cycle detection on a ticker's cached hourly price series explain
why some tickers respond better than others to the current window sweep (10/20, chosen empirically
in run_optimization_sweep.py's grid, not derived from any cyclical structure)?

For each of the 10 real v5 watchlist tickers, extracts the dominant spectral period from the
detrended log-price series and from the return series, tests significance against a random-
permutation null (financial returns are close to white noise -- a raw FFT peak is not evidence of
real periodicity without this), and compares against that ticker's actual best-performing `window`
(10 or 20 bars) pulled live from backtest_cache via campaign_comparison_table.best_node.

Usage: .venv/bin/python scripts/fft_cycle_analysis.py
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.fft import rfft, rfftfreq
from scipy.signal import detrend

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.campaign_comparison_table import best_node

DB_PATH = "cache/research/trading_universe.db"
CSV_DIR = Path("cache/research")
BARS_PER_DAY = 7  # 9:30-15:30 hourly bars

TICKERS = ["AGQ", "DPST", "GDXU", "HIBL", "KORU", "NUGT", "SOXL", "UDOW", "USD", "YANG"]
STRATEGIES = ["TrailingBothZScoreBreakout", "TrailingExitZScoreBreakout"]
N_PERMUTATIONS = 1000
MIN_PERIOD_BARS = 3   # exclude near-Nyquist noise
MAX_PERIOD_BARS = 500  # exclude near-DC trend artifacts


def dominant_period(series):
    """Returns (period_bars, power_ratio, p_value) for the strongest non-trivial FFT peak."""
    n = len(series)
    freqs = rfftfreq(n, d=1.0)
    power = np.abs(rfft(series)) ** 2

    periods = np.full(n // 2 + 1, np.inf)
    nonzero = freqs > 0
    periods[nonzero] = 1.0 / freqs[nonzero]
    valid = (periods >= MIN_PERIOD_BARS) & (periods <= MAX_PERIOD_BARS)
    if not valid.any():
        return None, None, None

    peak_idx = np.argmax(power[valid])
    valid_idx = np.where(valid)[0][peak_idx]
    peak_power = power[valid_idx]
    peak_period = periods[valid_idx]
    mean_power = power[valid].mean()
    power_ratio = peak_power / mean_power

    rng = np.random.default_rng(42)
    null_peaks = np.empty(N_PERMUTATIONS)
    shuffled = series.copy()
    for i in range(N_PERMUTATIONS):
        rng.shuffle(shuffled)
        p = np.abs(rfft(shuffled)) ** 2
        null_peaks[i] = p[valid].max()
    p_value = (null_peaks >= peak_power).mean()

    return peak_period, power_ratio, p_value


def load_series(ticker):
    csv = CSV_DIR / f"{ticker}_1h.csv"
    if not csv.exists():
        return None
    df = pd.read_csv(csv, index_col=0, parse_dates=True)
    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    log_price = np.log(df[close_col].values)
    log_price_detrended = detrend(log_price)
    returns = np.diff(log_price)
    return log_price_detrended, returns


def main():
    con = sqlite3.connect(DB_PATH)
    rows = []
    for ticker in TICKERS:
        series = load_series(ticker)
        if series is None:
            print(f"{ticker}: no cached CSV, skipping")
            continue
        log_price_detrended, returns = series

        price_period, price_ratio, price_p = dominant_period(log_price_detrended)
        ret_period, ret_ratio, ret_p = dominant_period(returns)

        best = None
        for strat in STRATEGIES:
            n = best_node(con, "v5", ticker, strat, 1.0, "open_check")
            if n and (best is None or n["best"] > best["best"]):
                best = n
        window = best["window"] if best else None

        rows.append({
            "ticker": ticker,
            "window_bars": window,
            "price_period_bars": round(price_period, 1) if price_period else None,
            "price_power_ratio": round(price_ratio, 2) if price_ratio else None,
            "price_p_value": round(price_p, 3) if price_p is not None else None,
            "ret_period_bars": round(ret_period, 1) if ret_period else None,
            "ret_power_ratio": round(ret_ratio, 2) if ret_ratio else None,
            "ret_p_value": round(ret_p, 3) if ret_p is not None else None,
        })

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 160)
    print(df.to_string(index=False))
    print(f"\n(p-value = fraction of {N_PERMUTATIONS} random-shuffle nulls with a peak >= the real peak; "
          f"p < 0.05 is the usual significance bar)")


if __name__ == "__main__":
    main()
