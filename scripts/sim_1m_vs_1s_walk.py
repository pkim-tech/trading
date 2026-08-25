"""Direct 1-minute-bar vs 1-second-bar walk-forward comparison for SOXL's real
live TrailingBothZScoreBreakout node (wl_id=92), built 2026-08-24 ("promoter"
session) to answer: how much does the entry/exit outcome actually change once
you go from 1-minute bars down to 1-second bars, using the identical
underlying price series (the 1-minute bars are built by resampling the same
real 1-second Massive data, not a separate fetch) -- isolates bar-granularity
effects from data-source effects.

Entry-side state machine (idle -> waiting -> in_trade) is a direct, plain-
Python translation of backtester.py's `_simulate_trail_both` 'possible'
resolution (Low-before-High assumption, gap-through-trigger honesty on Open),
generalized so "held"/wait duration are real elapsed hours (timestamp diffs),
not bar counts -- bar count only means hours in the original hourly kernel.
Exit-side reuses strategies.TrailingBothZScoreBreakout.check_exit() directly
(no separate reimplementation) per the project's reuse-over-rebuild
convention -- see conversation 2026-08-24.

Signal-arm gating uses the real live open_check windows (9:31-9:40,
14:31-14:40 ET), matching wl_id=92's real entry_timing='open_check' -- not
the raw hourly kernel's "once per hour" gate, which doesn't generalize to
sub-hourly bars.

Usage:
    .venv/bin/python scripts/sim_1m_vs_1s_walk.py
"""
import subprocess
import sys
import time
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies

SECOND_DATA_DIR = Path(__file__).resolve().parent.parent / "cache" / "research" / "second_data"

# Real live v5 params, wl_id=92 (SOXL, TrailingBothZScoreBreakout) -- default.
# Override via --config v6 for candidate_nodes id=851 (SOXL's real, user-selected
# v6 pick, "5yr-primary pick, post data_source/same_bar_reentry/window fixes" --
# not yet promoted live, held back only because of the real open position).
CONFIGS = {
    "v5": dict(WINDOW=10, Z_THRESH=1.0, FIXED_SL=0.02, ARM_PCT=0.30,
               TRAIL_BUY_PCT=0.03, TRAIL_SELL_PCT=0.01, MAX_HOLD_HOURS=70.0),
    "v6": dict(WINDOW=10, Z_THRESH=1.5, FIXED_SL=0.02, ARM_PCT=0.29,
               TRAIL_BUY_PCT=0.09, TRAIL_SELL_PCT=0.07, MAX_HOLD_HOURS=84.0),
}
import os
_cfg_name = os.environ.get("SIM_CONFIG", "v5")
_cfg = CONFIGS[_cfg_name]
WINDOW = _cfg["WINDOW"]
Z_THRESH = _cfg["Z_THRESH"]
FIXED_SL = _cfg["FIXED_SL"]
ARM_PCT = _cfg["ARM_PCT"]
TRAIL_BUY_PCT = _cfg["TRAIL_BUY_PCT"]
TRAIL_SELL_PCT = _cfg["TRAIL_SELL_PCT"]
MAX_HOLD_HOURS = _cfg["MAX_HOLD_HOURS"]

# Real kernel checks exactly TWO price points per target hour (backtester.py:843-869),
# not a scanned window -- found 2026-08-24 ("promoter" session), the actual bug behind
# the v6 vs 83.6% gap: at the target hour's Open (9:30/14:30), check op<=lower_band; if
# that doesn't fire, fall back to checking the SAME hourly bar's Close (60 min later,
# 10:30/15:30) via cp<=lower_band. The old OPEN_CHECK_WINDOWS scan (9:31-9:40/14:31-14:40)
# was mirroring the LIVE DAEMON's real-time polling approximation of this, not the
# backtest kernel's own exact two-point semantics -- the two are different mechanisms,
# and comparing against the kernel's own number requires matching the kernel exactly.
TARGET_HOUR_OPENS = [dtime(9, 30), dtime(14, 30)]
TARGET_HOUR_CLOSES = [dtime(10, 30), dtime(15, 30)]

STRAT = strategies.TrailingBothZScoreBreakout(stop_loss=FIXED_SL, take_profit=ARM_PCT, trail_pct=TRAIL_SELL_PCT)


def compute_daily_indicators(df):
    daily_close = df["Close"].resample("1D").last().dropna()
    sma = daily_close.rolling(WINDOW).mean()
    std = daily_close.rolling(WINDOW).std()
    return daily_close.index, sma.values, std.values


def walk(df, label):
    """df: DataFrame indexed by tz-aware ET timestamp, columns Open/High/Low/Close.
    Returns list of trade dicts."""
    daily_idx_ts, sma_arr, std_arr = compute_daily_indicators(df)
    daily_idx_ts = pd.DatetimeIndex(daily_idx_ts).date
    # Map day D to day D-1's indicator row -- day D's own SMA/Std is built from D's
    # close, which isn't known during D's intraday bars. Mirrors backtester.py's
    # prep_inputs() exactly (real look-ahead bug found 2026-08-24, "promoter"
    # session: this used to map D to D's own row here, silently peeking at each
    # day's own not-yet-known close in that same day's signal check).
    daily_lookup = {d: i - 1 for i, d in enumerate(daily_idx_ts)}

    ts = df.index
    opens = df["Open"].values
    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    dates = ts.date
    times = ts.time

    # max_hours_to_hold in the real kernel is a BAR counter (held += 1 once per
    # hourly bar), not real wall-clock elapsed hours -- hourly bars only exist
    # ~6.5 hours/trading day, so "84 held" means ~84 trading-hour bars (~2-3
    # calendar weeks), not 84 real hours (~3.5 days). Found 2026-08-24
    # ("promoter" session) via a real trade-by-trade diff against the actual
    # GT kernel output: this script's calendar-hours version was closing
    # positions via TIME a full day+ before the real kernel would even reach
    # its stop-loss on the identical first trade. Precompute a globally
    # increasing "trading-hour-bar slot" per row (regular session filtered to
    # 09:30-16:00, so hour in [9..15], 7 slots/trading day) so hours_held can
    # be a bar-count difference, matching the kernel exactly instead of a
    # timestamp subtraction.
    unique_dates, date_rank = np.unique(dates, return_inverse=True)
    hour_slot = ts.hour.to_numpy() - 9
    bar_id = date_rank * 7 + hour_slot

    n = len(df)
    trades = []

    state = "idle"  # idle -> waiting -> in_trade -> (trailing via check_exit's own state dict)
    running_low = 0.0
    entry_price = entry_time = None
    entry_bar_id = wait_start_bar_id = 0
    exit_state = {}
    last_exit_day = None

    t0 = time.time()
    report_every = max(n // 20, 1)

    for i in range(n):
        if i % report_every == 0 and i > 0:
            elapsed = time.time() - t0
            rate = i / elapsed
            eta = (n - i) / rate if rate > 0 else float("nan")
            print(f"  [{label}] {i:,}/{n:,} ({100*i/n:.0f}%) elapsed={elapsed:.1f}s eta={eta:.1f}s")

        op, high, low, cp = opens[i], highs[i], lows[i], closes[i]
        cur_time = ts[i]
        d = dates[i]
        t = times[i]

        if state == "in_trade":
            di = daily_lookup.get(d)
            std = std_arr[di] if di is not None and di < len(std_arr) else np.nan
            hours_held = bar_id[i] - entry_bar_id
            ctx = {
                "entry_price": entry_price, "stop_loss": FIXED_SL, "take_profit": ARM_PCT,
                "low": low, "high": high, "current_price": cp, "open": op,
                "hours_held": hours_held, "max_hours_to_hold": MAX_HOLD_HOURS,
                "at_bar_close": True, "state": exit_state,
            }
            reason, exit_px, new_state = STRAT.check_exit(ctx)
            exit_state = new_state
            if reason is not None:
                pc = (exit_px - entry_price) / entry_price
                trades.append(dict(entry_time=entry_time, exit_time=cur_time,
                                    entry_price=entry_price, exit_price=exit_px,
                                    reason=reason, ret_pct=pc * 100))
                state = "idle"
                last_exit_day = d
                exit_state = {}

        elif state == "waiting":
            buy_trigger_gap = running_low * (1.0 + TRAIL_BUY_PCT)
            if op >= buy_trigger_gap:
                entry_price = op
                entry_time = cur_time
                entry_bar_id = bar_id[i]
                state = "in_trade"
                exit_state = {}
            else:
                if low < running_low:
                    running_low = low
                buy_trigger = running_low * (1.0 + TRAIL_BUY_PCT)
                if high >= buy_trigger:
                    entry_price = buy_trigger
                    entry_time = cur_time
                    entry_bar_id = bar_id[i]
                    state = "in_trade"
                    exit_state = {}
                else:
                    wait_bars_held = bar_id[i] - wait_start_bar_id
                    if wait_bars_held >= MAX_HOLD_HOURS:
                        state = "idle"

        else:  # idle
            is_open_check = t in TARGET_HOUR_OPENS
            is_close_check = t in TARGET_HOUR_CLOSES
            if is_open_check or is_close_check:
                di = daily_lookup.get(d)
                if di is not None and 0 <= di < len(sma_arr):
                    sma, std = sma_arr[di], std_arr[di]
                    if std and not np.isnan(std) and not np.isnan(sma):
                        lower_band = sma - std * Z_THRESH
                        blocked = (d == last_exit_day)
                        if not blocked:
                            # Open-check first (mirrors the kernel's open_check_entry_timing
                            # branch); Close-check is only reached at a DIFFERENT bar (60
                            # min later), so no separate "fired" flag needed within one bar.
                            price_to_check = op if is_open_check else cp
                            if price_to_check <= lower_band:
                                state = "waiting"
                                running_low = price_to_check
                                wait_start_bar_id = bar_id[i]

    return trades


def load_second_data(ticker):
    override = os.environ.get("SIM_DATA_PATH_OVERRIDE")
    path = Path(override) if override else SECOND_DATA_DIR / f"{ticker}_1s.csv"
    # Chunked read with progress -- found 2026-08-24 ("promoter" session): a single
    # blocking pd.read_csv on a multi-GB file gave zero output for 5-10+ minutes,
    # exactly the "silent until it exits" problem long-job-launch's own checklist
    # exists to prevent. wc -l first (much faster than the pandas parse itself) to
    # get a real total for the elapsed/ETA line.
    total_lines = int(subprocess.run(["wc", "-l", str(path)], capture_output=True, text=True)
                       .stdout.split()[0]) - 1
    t0 = time.time()
    chunks = []
    rows_read = 0
    for chunk in pd.read_csv(path, chunksize=2_000_000):
        chunks.append(chunk)
        rows_read += len(chunk)
        elapsed = time.time() - t0
        rate = rows_read / elapsed
        eta = (total_lines - rows_read) / rate if rate > 0 else float("nan")
        print(f"  [load] {rows_read:,}/{total_lines:,} ({100*rows_read/total_lines:.0f}%) "
              f"elapsed={elapsed:.1f}s eta={eta:.1f}s")
    df = pd.concat(chunks, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York")
    df = df.set_index("timestamp").sort_index()
    df = df[["Open", "High", "Low", "Close"]]
    # Regular-session-only (09:30-16:00 ET), matching backtester.prep_minute_inputs'
    # explicit requirement -- found 2026-08-24 ("promoter" session): this used to keep
    # Massive's full extended-hours coverage (04:00-19:59 ET), feeding thin/wide-spread
    # pre-market/after-hours prints into entry/exit decisions the real GT kernel never
    # sees at all (real trades in this walk's own output were entering/exiting at times
    # like 04:00 and 17:11 ET before this fix). Set SIM_KEEP_EXTENDED_HOURS=1 to
    # reproduce the old (buggy but internally consistent) behavior for comparison.
    if os.environ.get("SIM_KEEP_EXTENDED_HOURS") != "1":
        t = df.index.time
        df = df[(t >= pd.Timestamp("09:30").time()) & (t < pd.Timestamp("16:00").time())]
    return df


def resample_to_minute(df_1s):
    agg = df_1s.resample("1min").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    return agg


def cagr_from_trades(trades, start_ts, end_ts):
    if not trades:
        return 0.0, 0
    bal = 1.0
    for t in trades:
        bal *= (1 + t["ret_pct"] / 100)
    years = (end_ts - start_ts).total_seconds() / (365.25 * 86400)
    if years <= 0 or bal <= 0:
        return float("nan"), len(trades)
    cagr = (bal ** (1 / years) - 1) * 100
    return cagr, len(trades)


def main():
    print(f"Config: {_cfg_name} -> {_cfg}")
    print("Loading 1-second SOXL data...")
    df_1s = load_second_data("SOXL")
    print(f"  {len(df_1s):,} 1-second rows, {df_1s.index.min()} -> {df_1s.index.max()}")

    print("Resampling to 1-minute bars...")
    df_1m = resample_to_minute(df_1s)
    print(f"  {len(df_1m):,} 1-minute rows")

    print("\n=== Walking 1-minute bars ===")
    t0 = time.time()
    trades_1m = walk(df_1m, "1m")
    t_1m = time.time() - t0
    cagr_1m, n_1m = cagr_from_trades(trades_1m, df_1m.index.min(), df_1m.index.max())
    print(f"  1m walk: {t_1m:.1f}s wall time, {n_1m} trades, CAGR={cagr_1m:.2f}%")

    out_dir = Path(__file__).resolve().parent.parent / "output"
    out_dir.mkdir(exist_ok=True)
    pd.DataFrame(trades_1m).to_csv(out_dir / f"sim_1m_vs_1s_soxl_{_cfg_name}_trades_1m.csv", index=False)
    print(f"  (1m trades saved early, before the 1s pass, so they're diffable without waiting)")

    print("\n=== Walking 1-second bars ===")
    t0 = time.time()
    trades_1s = walk(df_1s, "1s")
    t_1s = time.time() - t0
    cagr_1s, n_1s = cagr_from_trades(trades_1s, df_1s.index.min(), df_1s.index.max())
    print(f"  1s walk: {t_1s:.1f}s wall time, {n_1s} trades, CAGR={cagr_1s:.2f}%")

    print(f"\n=== Comparison ===")
    print(f"  1m: {n_1m} trades, CAGR={cagr_1m:.2f}%  (wall time {t_1m:.1f}s)")
    print(f"  1s: {n_1s} trades, CAGR={cagr_1s:.2f}%  (wall time {t_1s:.1f}s)")
    print(f"  delta CAGR (1m - 1s): {cagr_1m - cagr_1s:.2f} pp")

    out_dir = Path(__file__).resolve().parent.parent / "output"
    out_dir.mkdir(exist_ok=True)
    pd.DataFrame(trades_1m).to_csv(out_dir / f"sim_1m_vs_1s_soxl_{_cfg_name}_trades_1m.csv", index=False)
    pd.DataFrame(trades_1s).to_csv(out_dir / f"sim_1m_vs_1s_soxl_{_cfg_name}_trades_1s.csv", index=False)
    print(f"\n  trade lists saved to output/sim_1m_vs_1s_soxl_{_cfg_name}_trades_{{1m,1s}}.csv")


if __name__ == "__main__":
    main()
