"""
Research script: does any v5 watchlist ticker have a genuine, repeating multi-day price wave
(days, not the intraday 3.5-bar volatility seasonality already confirmed real, and not a discrete
peak search over the full [3,500]-bar band already refuted for signed returns in
docs/research_log.md's 2026-08-03 FFT entry)? If so, does entering near a specific phase of that
wave (e.g. the trough) produce better trade outcomes than entering at a random phase?

Improves on the original FFT attempt per an Opus review's critique (docs/research_log.md,
same date): uses Welch's method (scipy.signal.welch) instead of a raw periodogram to cut estimator
variance, restricts the search to a targeted multi-day band (21-200 bars, ~3-28 trading days) instead
of a broad global argmax, and replaces the iid-shuffle null (destroys ALL autocorrelation, not just
periodicity -- anti-conservative for heteroskedastic returns, biased toward finding false cycles) with
a moving-block bootstrap (preserves short-range autocorrelation/volatility clustering within each
block, destroys anything periodic across blocks) -- the correct null for "is there a real cycle
longer than this block, given the real local structure of the series."

If any ticker clears significance, extracts the cycle's instantaneous phase via a bandpass filter +
Hilbert transform, tags each historical trade's entry bar with its phase, and checks whether trade
outcome (return) correlates with phase (Spearman correlation, cosine of phase vs return).

Usage: .venv/bin/python scripts/multiday_cycle_entry_test.py
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import welch, butter, filtfilt, hilbert
from scipy.stats import spearmanr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtester import prep_inputs, OPEN
import strategies
from scripts.export_trades import (
    load_hourly,
    simulate_trail_both_annotated,
    simulate_trail_exit_chaos,
)

LIVE_DB = Path("cache/live/trading_live.db")
CSV_DIR = Path("cache/research")
TICKERS = ["AGQ", "DPST", "GDXU", "HIBL", "KORU", "NUGT", "SOXL", "UDOW", "USD", "YANG"]

MIN_PERIOD = 21   # ~3 trading days
MAX_PERIOD = 200  # ~28 trading days
BLOCK_LEN = 7     # one trading day -- well below MIN_PERIOD
N_BOOT = 500
TARGET_H0, TARGET_H1 = 9, 14


def welch_band_power(x, nperseg=256):
    freqs, psd = welch(x, window="hann", nperseg=min(nperseg, len(x) // 2), detrend="constant")
    periods = np.full_like(freqs, np.inf)
    nz = freqs > 0
    periods[nz] = 1.0 / freqs[nz]
    band = (periods >= MIN_PERIOD) & (periods <= MAX_PERIOD)
    if not band.any():
        return None, None, None
    peak_idx = np.argmax(psd[band])
    band_periods = periods[band]
    return band_periods[peak_idx], psd[band][peak_idx], psd[band].mean()


def block_bootstrap_sample(x, rng, block_len):
    n = len(x)
    n_blocks = int(np.ceil(n / block_len))
    starts = rng.integers(0, n - block_len + 1, size=n_blocks)
    blocks = [x[s:s + block_len] for s in starts]
    return np.concatenate(blocks)[:n]


def test_ticker_cycle(returns):
    peak_period, peak_power, band_mean = welch_band_power(returns)
    if peak_period is None:
        return None

    rng = np.random.default_rng(7)
    null_peaks = np.empty(N_BOOT)
    for i in range(N_BOOT):
        sample = block_bootstrap_sample(returns, rng, BLOCK_LEN)
        _, p, _ = welch_band_power(sample)
        null_peaks[i] = p if p is not None else 0.0
    p_value = (null_peaks >= peak_power).mean()

    return {
        "peak_period_bars": round(peak_period, 1),
        "power_ratio": round(peak_power / band_mean, 2),
        "p_value": round(p_value, 3),
    }


def load_nodes():
    con = sqlite3.connect(LIVE_DB)
    rows = con.execute("""
        SELECT MIN(id), ticker, strategy, window, z_score_threshold, arm_sell_pct, take_profit,
               fixed_sl, trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing
        FROM watch_list WHERE watchlist_id=65 AND mode='research'
        GROUP BY ticker
    """).fetchall()
    con.close()
    cols = ["id", "ticker", "strategy", "window", "z", "arm_sell_pct", "take_profit", "fixed_sl",
            "trail_buy_pct", "trail_sell_pct", "max_hold_hours", "entry_timing"]
    nodes = {r[1]: dict(zip(cols, r)) for r in rows}
    # take_profit holds the arm-sell threshold for TrailingExitZScoreBreakout nodes;
    # arm_sell_pct holds it for TrailingBothZScoreBreakout (never both populated on
    # the same row -- signals_db.py:983-988). Corrected 2026-08-04 -- this script
    # previously hardcoded TP=disabled for every TrailingExit node and never passed
    # open_check, both silently wrong for every real v5 node. See
    # docs/research_log.md's 2026-08-04 correction entry -- this script's own
    # 2026-08-03 published finding needs re-verification under the fix.
    for n in nodes.values():
        n["arm_pct"] = n["arm_sell_pct"] if n["strategy"] == "TrailingBothZScoreBreakout" else n["take_profit"]
    return nodes


def get_trades(node):
    df_h = load_hourly(node["ticker"])
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    strat_cls = getattr(strategies, node["strategy"])
    ind = strat_cls(window=node["window"]).generate_daily_indicators(df_daily)
    p = prep_inputs(df_h, ind)

    open_check = node["entry_timing"] == "open_check"
    if node["strategy"] == "TrailingBothZScoreBreakout":
        trades = simulate_trail_both_annotated(
            p, node["arm_pct"] / 100.0, node["fixed_sl"] / 100.0, node["max_hold_hours"],
            node["trail_buy_pct"] / 100.0, node["trail_sell_pct"] / 100.0,
            TARGET_H0, TARGET_H1, node["z"], open_check=open_check,
        )
    else:
        rng = np.random.default_rng(0)
        trades = simulate_trail_exit_chaos(
            p, node["arm_pct"] / 100.0, node["fixed_sl"] / 100.0, node["max_hold_hours"],
            node["trail_sell_pct"] / 100.0, TARGET_H0, TARGET_H1, node["z"],
            rng, "drop", 0.0, "drop", 0.0, open_check=open_check,
        )
    return trades, p


def phase_vs_outcome(returns, trades, p, period_bars):
    low = 1.0 / (period_bars * 1.5)
    high = 1.0 / (period_bars * 0.67)
    nyq = 0.5
    b, a = butter(2, [low / nyq, min(high / nyq, 0.99)], btype="band")
    filtered = filtfilt(b, a, returns)
    phase = np.angle(hilbert(filtered))

    rows = []
    for t in trades:
        if t["signal_i"] is None or t["signal_i"] == 0 or t["result"] == OPEN:
            continue
        idx = t["signal_i"] - 1  # returns[i] = price change ending at bar i; align to bar before signal
        if idx >= len(phase):
            continue
        rows.append({"phase": phase[idx], "ret": t["ret"]})
    if len(rows) < 10:
        return None
    df = pd.DataFrame(rows)
    df["phase_cos"] = np.cos(df["phase"])
    corr, p_corr = spearmanr(df["phase_cos"], df["ret"])
    return {"n_trades": len(df), "spearman_corr": round(corr, 3), "spearman_p": round(p_corr, 3)}


def main():
    nodes = load_nodes()
    results = []
    for ticker in TICKERS:
        csv = CSV_DIR / f"{ticker}_1h.csv"
        if not csv.exists():
            continue
        df = pd.read_csv(csv, index_col=0, parse_dates=True)
        close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
        returns = np.diff(np.log(df[close_col].values))

        cycle = test_ticker_cycle(returns)
        row = {"ticker": ticker, **(cycle or {})}

        if cycle and cycle["p_value"] < 0.05 and ticker in nodes:
            trades, p = get_trades(nodes[ticker])
            phase_result = phase_vs_outcome(returns, trades, p, cycle["peak_period_bars"])
            if phase_result:
                row.update(phase_result)

        results.append(row)

    df_out = pd.DataFrame(results)
    pd.set_option("display.width", 160)
    print(df_out.to_string(index=False))
    print(f"\n(p_value: block-bootstrap null, {N_BOOT} reps, band {MIN_PERIOD}-{MAX_PERIOD} bars; "
          f"p<0.05 = likely real cycle. spearman_p, if present, tests whether entering at a "
          f"particular cycle phase predicts better/worse trade returns.)")


if __name__ == "__main__":
    main()
