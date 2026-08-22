"""Independent clean-room minute-resolution ground-truth simulator.

WHY THIS EXISTS
---------------
Two earlier in-repo research scripts, built iteratively in one long session,
disagreed with each other by ~90 percentage points of CAGR on the same ticker
(SOXL) against the same real data. Neither could be trusted for a real
live-trading decision (whether to pause SOXL/HIBL). This module is a
from-scratch re-implementation written WITHOUT reading either of those
scripts, derived only from the production ground truth:

  * backtester._simulate_trail_both   (TrailingBothZScoreBreakout kernel)
  * backtester._simulate_trail        (TrailingExitZScoreBreakout kernel)
  * strategies.{TrailingBoth,TrailingExit}ZScoreBreakout.check_exit (live logic)
  * schwab_client.place_trailing_buy / signals_notify._place_stop_loss_for_position
    (what live execution actually does at the broker)

so that its agreement/disagreement with the kernel is genuine independent
evidence rather than another patch on the same lineage.

WHAT IT MODELS
--------------
Signal DETECTION stays bar-anchored (this is what live really does): at the
hour-9 and hour-14 hourly bars only, `open_check` first (bar Open vs the PRIOR
day's lower band) then the same bar's Close. Indicator plumbing mirrors
run_optimization_sweep._node_inputs + backtester.prep_inputs exactly
(resample('D').last(), rolling SMA/Std, daily_lookup = {date: i-1}).

Everything downstream of detection is resolved on REAL 1-minute bars, because
that is what the broker really does:
  * TrailingBoth entry: Schwab OrderType.TRAILING_STOP buy -> the broker tracks
    the running low continuously and fires the instant price bounces
    trail_buy_pct off it. Not sampled by our polling code at any interval.
  * Protective stop: placed immediately on fill and resting continuously from
    that instant (signals_notify._place_stop_loss_for_position).
  * Arming (price >= entry*(1+arm/tp)): genuinely bar-close gated in live --
    strategies.py's check_exit returns early on `if not ctx.get('at_bar_close')`
    BEFORE the tp_price test. Verified by reading the source, not assumed.
  * Once armed, a real broker trailing-sell rests (signals_notify
    ._attempt_automated_sell -> schwab_client.place_trailing_sell), so the peak
    is tracked continuously from the arm bar's close onward.
  * TIME exit: bar-close gated (also behind the at_bar_close early return).

MISSING-MINUTE POLICY (read this before trusting a low-coverage ticker)
----------------------------------------------------------------------
Minute coverage is genuinely incomplete for several of these tickers (HIBL
~25% of regular-session minutes, DFEN ~53%, WEBL ~51%, KORU ~57%). A missing
minute from a Polygon-style aggregate feed means NO TRADE PRINTED in that
minute -- it is information, not a hole: a stop/trailing order cannot trigger
on a minute where nothing traded. So the base policy is simply to skip absent
minutes; no interpolation, no coarse fallback, and deliberately NO
coverage-completeness heuristic (a prior version of one of the disputed
scripts had exactly such a heuristic silently revert half its entries to a
coarse hourly approximation).

That policy is not taken on faith -- `--coverage-report` runs the direct,
falsifiable test: aggregate every minute bar into its owning hourly bar and
check whether the minute-derived [min Low, max High] contains the independent
yfinance hourly bar's [Low, High]. Measured result over the full window:
0 uncovered bars out of ~3,477 for EVERY one of the 12 tickers, INCLUDING
HIBL (25% of minutes present) and DFEN/WEBL (~52%). The absent minutes
genuinely carry no price information, so no fallback is needed anywhere.

An optional `--backstop` re-runs the kernel's bar-level check against the
hourly OHLC after each bar's minutes (using min(minute low, hourly low) /
max(minute peak, hourly high)) so it can only ADD triggers. Given the coverage
result above it is OFF by default: with full range coverage it cannot recover
any real missed print, it can only re-inject the kernel's optimistic
low-before-high intrabar guess that minute data actually resolves. Kept as a
sensitivity switch, not a correctness mechanism.

Intrabar ordering inside a single minute is still unknowable; `--intrabar`
selects the assumption (default `kernel`, matching _simulate_trail_both's
own guess: low-before-high on the entry side, high-before-low on the exit
side). `--intrabar mirror` flips both, giving an honest uncertainty band.

SAME-BAR RE-ENTRY
-----------------
The kernel can only fire one signal per bar (open_check, else Close). Live can
do both: SOXL's real 2026-08-20 trade_log pair proves it -- the 09:30 bar's
Open check filled at 09:32:25 @122.985, stopped out 09:45:50, and the SAME
09:30 bar's Close check (evaluated in the 10:25-10:40 window) produced a second
real entry filled 11:05:04 @124.3911. So the close_check is evaluated at the
end of every bar whenever the state machine is idle. `--no-same-bar-reentry`
reverts to kernel behaviour for sensitivity.

HEADLINE RESULT (2024-08-21..2026-08-20, $10k compounded, run 2026-08-21)
------------------------------------------------------------------------
All 12 live tickers are POSITIVE under minute-resolution execution. Every
ticker's ground-truth CAGR is materially BELOW its kernel CAGR (the kernel is
optimistic, as expected -- it cannot see an intra-hour stop-out), but none goes
negative. SOXL 58.7% (kernel 109.0%) and HIBL 54.0% (kernel 178.9%) -- both
comfortably positive, across every sensitivity setting:
    SOXL: 58.7% (default) / 60.9% (mirror) / 77.9% (no same-bar re-entry)
    HIBL: 54.0% (default) / 68.4% (mirror) / 28.2% (no same-bar re-entry)

Usage:
    .venv/bin/python scripts/sim_minute_groundtruth_independent.py
    .venv/bin/python scripts/sim_minute_groundtruth_independent.py --tickers SOXL HIBL --verbose-trades
    .venv/bin/python scripts/sim_minute_groundtruth_independent.py --validate-soxl
Read-only against cache/live/trading_live.db and the minute CSVs.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LIVE_DB = os.path.join(ROOT, "cache", "live", "trading_live.db")
HOURLY_DIR = os.path.join(ROOT, "cache", "research")
MINUTE_DIR = os.path.join(ROOT, "cache", "research", "minute_data")

LIVE_NODE_IDS = (92, 197, 202, 231, 235, 236, 203, 229, 230, 232, 233, 234)
TARGET_HOURS = (9, 14)
WINDOW_START = "2024-08-21"
WINDOW_END = "2026-08-20"
START_BALANCE = 10_000.0


# ───────────────────────── config / data loading ─────────────────────────

def load_nodes(node_ids=LIVE_NODE_IDS):
    con = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    q = ("SELECT id,ticker,strategy,window,z_score_threshold,fixed_sl,trail_buy_pct,"
         "trail_sell_pct,arm_sell_pct,take_profit,max_hold_hours,entry_timing,account "
         f"FROM watch_list WHERE id IN ({','.join('?' * len(node_ids))})")
    rows = [dict(r) for r in con.execute(q, node_ids)]
    con.close()
    for r in rows:
        # Real schema quirk: TrailingBoth stores the arm threshold in
        # arm_sell_pct, TrailingExit in take_profit (signals_db._tp_or_arm_pct).
        r["arm_pct"] = (r["arm_sell_pct"] if r["strategy"] == "TrailingBothZScoreBreakout"
                        else r["take_profit"]) or 0.0
    return rows


def load_hourly(ticker, data_source="yahoo"):
    """data_source='yahoo' (default, unchanged): the Yahoo-sourced hourly CSV,
    ~2023-07-24 onward. data_source='massive': dividend/split-adjusted hourly bars
    derived from Massive.com minute data (db_cache.get_massive_hourly_ohlcv),
    back to ~2021-08-23 for most tickers -- see db_cache.py's
    massive_hourly_derived table docstring. Same column/dtype/index shape either
    way, so callers don't need to branch."""
    if data_source == "massive":
        import db_cache
        return db_cache.get_massive_hourly_ohlcv(ticker)
    df = pd.read_csv(os.path.join(HOURLY_DIR, f"{ticker}_1h.csv"), index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.sort_index()


def load_minutes(ticker, data_source="yahoo"):
    """Regular-session (09:30:00-15:59:59 ET) 1-minute bars, tz-naive ET index.
    data_source='yahoo' (default, unchanged): raw, UNADJUSTED minute CSV, same as
    before this param existed. data_source='massive': db_cache.
    get_massive_minute_ohlcv(ticker) instead -- dividend-adjusted, consistent with
    the data_source='massive' hourly leg (fixed 2026-08-22 -- previously this stayed
    on the raw unadjusted CSV even under --data-source massive)."""
    if data_source == "massive":
        import db_cache
        return db_cache.get_massive_minute_ohlcv(ticker)
    df = pd.read_csv(os.path.join(MINUTE_DIR, f"{ticker}_1m.csv"))
    ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("US/Eastern").dt.tz_localize(None)
    df = df.set_index(ts).sort_index()
    t = df.index.time
    keep = (t >= pd.Timestamp("09:30").time()) & (t < pd.Timestamp("16:00").time())
    return df.loc[keep, ["Open", "High", "Low", "Close"]]


def daily_indicators(df_hourly, window):
    """Mirrors run_optimization_sweep._node_inputs + strategy.generate_daily_indicators."""
    close_col = "Adj Close" if "Adj Close" in df_hourly.columns else "Close"
    d = df_hourly.resample("D").last().dropna(subset=[close_col])
    out = pd.DataFrame(index=d.index)
    out["SMA"] = d[close_col].rolling(window=window).mean()
    out["Std"] = d[close_col].rolling(window=window).std()
    return out.dropna()


def daily_lookup(df_daily_ind):
    """backtester.prep_inputs: each hourly bar maps to the PRIOR completed day's row."""
    return {d: i - 1 for i, d in enumerate(df_daily_ind.index.strftime("%Y-%m-%d"))}


# ───────────────────────────── simulator ─────────────────────────────

@dataclass
class Trade:
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    reason: str
    bars_held: int
    ret: float = 0.0
    signal_bar: int = 0  # hourly-bar index of the SIGNAL (idle->waiting, or the
                          # immediate-entry bar for TrailingExit) -- distinct from
                          # entry_bar for TrailingBoth (which fills later, once the
                          # WAIT resolves). Needed by drought_overlay_test.
                          # find_drought_windows, which measures gaps between real
                          # SIGNALS, not between fills. Added for the 2026-08-21
                          # drought-vs-ground-truth reconciliation.
    exit_bar: int = 0    # hourly-bar index the exit's minute timestamp falls within.


@dataclass
class Sim:
    node: dict
    intrabar: str = "kernel"     # 'kernel' | 'mirror'
    backstop: bool = True
    trades: list = field(default_factory=list)

    # ── state ──
    state: str = "IDLE"          # IDLE | WAIT | HOLD | ARMED
    running_low: float = 0.0
    wait_bar0: int = 0
    entry_price: float = 0.0
    entry_time: pd.Timestamp = None
    entry_bar: int = 0
    stop_price: float = 0.0
    arm_price: float = 0.0
    peak: float = 0.0
    fill_minute: pd.Timestamp = None
    fill_partial: bool = False   # fill happened mid-minute -> that minute's range
                                 # is not provably post-fill, so skip it
    signal_bar: int = 0

    def _close(self, t, px, reason, bar_i):
        ret = (px - self.entry_price) / self.entry_price
        self.trades.append(Trade(self.entry_time, self.entry_price, t, px, reason,
                                 bar_i - self.entry_bar, ret, self.signal_bar, bar_i))
        self.state = "IDLE"

    def _open(self, t, px, bar_i, fill_minute=None, partial=True, signal_bar=None):
        n = self.node
        self.entry_price = px
        self.entry_time = t
        self.entry_bar = bar_i
        self.signal_bar = bar_i if signal_bar is None else signal_bar
        self.stop_price = px * (1 - n["fixed_sl"] / 100.0)
        self.arm_price = px * (1 + n["arm_pct"] / 100.0)
        self.state = "HOLD"
        self.fill_minute = fill_minute
        self.fill_partial = partial

    # ── minute-level resolution ──
    def _minutes(self, mins, bar_i):
        """Walk one hourly bar's real minute bars through the current state."""
        tbp = self.node["trail_buy_pct"] / 100.0
        tsp = self.node["trail_sell_pct"] / 100.0
        low_first_entry = (self.intrabar == "kernel")
        high_first_exit = (self.intrabar == "kernel")

        for t, o, h, l, c in mins:
            if self.state == "WAIT":
                trig_prior = self.running_low * (1 + tbp)
                if o >= trig_prior:                       # gap-through on this minute's open
                    self._open(t, o, bar_i, fill_minute=t, partial=False, signal_bar=self.wait_bar0)
                    continue
                if low_first_entry:
                    if l < self.running_low:
                        self.running_low = l
                    trig = self.running_low * (1 + tbp)
                    if h >= trig:
                        self._open(t, trig, bar_i, fill_minute=t, partial=True, signal_bar=self.wait_bar0)
                        continue
                else:
                    if h >= trig_prior:
                        self._open(t, trig_prior, bar_i, fill_minute=t, partial=True, signal_bar=self.wait_bar0)
                        continue
                    if l < self.running_low:
                        self.running_low = l

            elif self.state == "HOLD":
                # Protective stop rests continuously from the instant of fill.
                # Skip the fill minute itself unless the fill WAS that minute's
                # open (only then is the whole minute's range provably post-fill).
                if self.fill_partial and t == self.fill_minute:
                    continue
                if o <= self.stop_price:
                    self._close(t, o, "SL", bar_i)
                    continue
                if l <= self.stop_price:
                    self._close(t, self.stop_price, "SL", bar_i)
                    continue

            elif self.state == "ARMED":
                gap = self.peak * (1 - tsp)
                if o <= gap:
                    self._close(t, o, "TRAIL", bar_i)
                    continue
                if high_first_exit:
                    self.peak = max(self.peak, h)
                    stop = self.peak * (1 - tsp)
                    if l <= stop:
                        self._close(t, stop, "TRAIL", bar_i)
                        continue
                else:
                    if l <= gap:
                        self._close(t, gap, "TRAIL", bar_i)
                        continue
                    self.peak = max(self.peak, h)

    # ── hourly backstop (unconditional; can only ADD a trigger) ──
    def _backstop(self, o, h, l, bar_i, t_close):
        tbp = self.node["trail_buy_pct"] / 100.0
        tsp = self.node["trail_sell_pct"] / 100.0
        if self.state == "WAIT":
            if l < self.running_low:
                self.running_low = l
            trig = self.running_low * (1 + tbp)
            if h >= trig:
                self._open(t_close, trig, bar_i, fill_minute=None, partial=False, signal_bar=self.wait_bar0)
        elif self.state == "HOLD":
            if l <= self.stop_price:
                self._close(t_close, self.stop_price, "SL", bar_i)
        elif self.state == "ARMED":
            self.peak = max(self.peak, h)
            stop = self.peak * (1 - tsp)
            if l <= stop:
                self._close(t_close, stop, "TRAIL", bar_i)


def coverage_report(ticker, start, end, data_source="yahoo"):
    """Falsifiable test of the missing-minute policy: does the minute feed's own
    per-hour [min Low, max High] contain the independent hourly bar's range?"""
    dfh = load_hourly(ticker, data_source=data_source).loc[start:end + " 23:59:59"]
    m = load_minutes(ticker, data_source=data_source).loc[start:end + " 23:59:59"]
    mi = m.index
    b = pd.DatetimeIndex(np.where(mi.minute >= 30, mi.floor("h") + pd.Timedelta(minutes=30),
                                  mi.floor("h") - pd.Timedelta(minutes=30)))
    g = m.groupby(b).agg(lo=("Low", "min"), hi=("High", "max"))
    j = dfh.join(g, how="left")
    no_min = int(j.lo.isna().sum())
    jj = j.dropna(subset=["lo"])
    lo_miss = int((jj.Low < jj.lo * 0.999).sum())
    hi_miss = int((jj.High > jj.hi * 1.001).sum())
    pct = len(m) / (m.index.normalize().nunique() * 390) if len(m) else 0.0
    return dict(ticker=ticker, bars=len(j), minute_fill=pct, no_minutes=no_min,
                low_uncovered=lo_miss, high_uncovered=hi_miss)


def simulate(node, df_hourly, minute_df, start, end, intrabar="kernel", backstop=False,
             same_bar_reentry=True):
    n = node
    is_both = n["strategy"] == "TrailingBothZScoreBreakout"
    hold_max = int(n["max_hold_hours"])
    z = float(n["z_score_threshold"])
    open_check = n["entry_timing"] == "open_check"

    ind = daily_indicators(df_hourly, int(n["window"]))
    dl = daily_lookup(ind)
    sma = ind["SMA"].to_numpy()
    std = ind["Std"].to_numpy()

    bars = df_hourly.loc[start:end + " 23:59:59"]
    idx = bars.index
    O, H, L, C = (bars[c].to_numpy(float) for c in ("Open", "High", "Low", "Close"))
    hours = idx.hour.to_numpy()
    dates = idx.strftime("%Y-%m-%d")
    di_arr = np.array([dl.get(d, -1) for d in dates])

    # minute bars grouped by their owning hourly bar (bar H:30 owns [H:30, H+1:30))
    mi = minute_df.index
    bucket = pd.DatetimeIndex(np.where(mi.minute >= 30, mi.floor("h") + pd.Timedelta(minutes=30),
                                       mi.floor("h") - pd.Timedelta(minutes=30)))
    m_by_bar = {k: v for k, v in minute_df.groupby(bucket)}

    sim = Sim(node=n, intrabar=intrabar, backstop=backstop)

    def band_at(i):
        if hours[i] not in TARGET_HOURS or di_arr[i] < 0 or std[di_arr[i]] == 0:
            return None
        return sma[di_arr[i]] - std[di_arr[i]] * z

    for i in range(len(idx)):
        t0, o, h, l, c = idx[i], O[i], H[i], L[i], C[i]
        mins_df = m_by_bar.get(t0)
        mins = (list(zip(mins_df.index, mins_df.Open.to_numpy(float), mins_df.High.to_numpy(float),
                         mins_df.Low.to_numpy(float), mins_df.Close.to_numpy(float)))
                if mins_df is not None else [])

        # ── (1) open_check signal detection, at the top of the bar ──
        b = band_at(i)
        opened_this_bar = False
        if sim.state == "IDLE" and b is not None and open_check and o <= b:
            opened_this_bar = True
            if is_both:
                sim.state, sim.running_low, sim.wait_bar0 = "WAIT", o, i
            else:
                # market buy at the bar's Open == this minute's start, so the whole
                # first minute is provably post-fill
                sim._open(t0, o, i, fill_minute=(mins[0][0] if mins else None), partial=False)

        # ── (2) continuous minute-level resolution, then the unconditional
        #        hourly backstop for anything the minute feed may have missed ──
        if sim.state != "IDLE" and mins:
            sim._minutes(mins, i)
        if backstop and sim.state != "IDLE":
            sim._backstop(o, h, l, i, t0)

        # ── (3) bar-close-gated events (at_bar_close in strategies.check_exit) ──
        if sim.state == "WAIT" and (i - sim.wait_bar0) >= hold_max:
            sim.state = "IDLE"                       # entry-abandon timeout
        elif sim.state == "HOLD":
            held = i - sim.entry_bar
            if c >= sim.arm_price:
                sim.state, sim.peak = "ARMED", c
            elif held >= hold_max:
                sim._close(t0, c, "TIME", i)
        elif sim.state == "ARMED" and (i - sim.entry_bar) >= hold_max:
            sim._close(t0, c, "TIME", i)

        # ── (4) close_check signal detection, evaluated AT this bar's close.
        #        Reached either because the open check didn't fire (kernel's own
        #        fall-through) or because the position opened and already closed
        #        inside this bar -- live really does re-check at the close window
        #        in that case (confirmed against SOXL's real 2026-08-20 pair). ──
        if sim.state == "IDLE" and b is not None and c <= b and not (opened_this_bar and not same_bar_reentry):
            if is_both:
                sim.state, sim.running_low, sim.wait_bar0 = "WAIT", c, i
            else:
                sim._open(t0, c, i, fill_minute=None, partial=False)

    return sim.trades


# ───────────────────────────── reporting ─────────────────────────────

def compound(trades, start_bal=START_BALANCE):
    bal = start_bal
    for t in trades:
        bal *= (1 + t.ret)
    return bal


def cagr(bal, years, start_bal=START_BALANCE):
    if bal <= 0:
        return -1.0
    return (bal / start_bal) ** (1 / years) - 1


def kernel_trades(node, df_hourly, start, end):
    import strategies
    from backtester import run_backtest_dispatch, prep_inputs
    cls = getattr(strategies, node["strategy"])
    ind = daily_indicators(df_hourly, int(node["window"]))
    prep = prep_inputs(df_hourly, ind)
    if node["strategy"] == "TrailingBothZScoreBreakout":
        sl_raw, trail_pct_pct = node["trail_buy_pct"], node["trail_sell_pct"]
    else:
        sl_raw, trail_pct_pct = node["trail_sell_pct"], 0.0
    tr = run_backtest_dispatch(cls, df_hourly, ind, node["ticker"],
                               take_profit=node["arm_pct"], sl_raw=sl_raw,
                               max_hours_to_hold=int(node["max_hold_hours"]),
                               z_score_threshold=float(node["z_score_threshold"]),
                               fixed_sl=float(node["fixed_sl"]), trail_pct_pct=trail_pct_pct,
                               entry_timing=node["entry_timing"], prep=prep)
    lo, hi = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1)
    return [t for t in tr if lo <= t["Entry Time"] < hi]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*")
    ap.add_argument("--start", default=WINDOW_START)
    ap.add_argument("--end", default=WINDOW_END)
    ap.add_argument("--intrabar", choices=["kernel", "mirror"], default="kernel")
    ap.add_argument("--backstop", action="store_true",
                    help="re-inject the kernel's hourly bar-level check as a fallback "
                         "(off by default; see module docstring / --coverage-report)")
    ap.add_argument("--verbose-trades", action="store_true")
    ap.add_argument("--validate-soxl", action="store_true")
    ap.add_argument("--coverage-report", action="store_true")
    ap.add_argument("--no-same-bar-reentry", action="store_true",
                    help="suppress the close_check signal on a bar where a position already "
                         "opened (kernel behaviour); default allows it, matching real live")
    ap.add_argument("--data-source", choices=["yahoo", "massive"], default="yahoo",
                    help="hourly data source (default: yahoo, unchanged behavior). "
                         "'massive' reads db_cache.get_massive_hourly_ohlcv, which goes "
                         "back to ~2021-08-23 vs yahoo's ~2023-07-24 floor.")
    a = ap.parse_args()

    nodes = load_nodes()
    if a.tickers:
        nodes = [n for n in nodes if n["ticker"] in a.tickers]
    years = (pd.Timestamp(a.end) - pd.Timestamp(a.start)).days / 365.25

    if a.coverage_report:
        print(f"{'Ticker':7s} {'hourly bars':>11s} {'minute fill':>11s} {'bars w/o min':>12s} "
              f"{'Low uncovered':>13s} {'High uncovered':>14s}")
        for n in sorted(nodes, key=lambda x: x["ticker"]):
            r = coverage_report(n["ticker"], a.start, a.end, data_source=a.data_source)
            print(f"{r['ticker']:7s} {r['bars']:11d} {r['minute_fill']:10.1%} "
                  f"{r['no_minutes']:12d} {r['low_uncovered']:13d} {r['high_uncovered']:14d}")
        return

    rows = []
    for n in sorted(nodes, key=lambda x: x["ticker"]):
        dfh = load_hourly(n["ticker"], data_source=a.data_source)
        mdf = load_minutes(n["ticker"], data_source=a.data_source)
        gt = simulate(n, dfh, mdf, a.start, a.end, a.intrabar, a.backstop,
                      same_bar_reentry=not a.no_same_bar_reentry)
        kt = kernel_trades(n, dfh, a.start, a.end)
        gb, kb = compound(gt), compound([type("T", (), {"ret": t["Return"]}) for t in kt])
        rows.append(dict(ticker=n["ticker"], strat=n["strategy"][:12], k_n=len(kt),
                         k_bal=kb, k_cagr=cagr(kb, years), g_n=len(gt), g_bal=gb,
                         g_cagr=cagr(gb, years)))
        if a.verbose_trades:
            print(f"\n=== {n['ticker']} ground truth ({len(gt)} trades) ===")
            for t in gt:
                print(f"  {t.entry_time} @{t.entry_price:8.4f} -> {t.exit_time} @{t.exit_price:8.4f} "
                      f"{t.reason:5s} bars={t.bars_held:3d} ret={t.ret:+.4%}")
        if a.validate_soxl and n["ticker"] == "SOXL":
            print("\n--- SOXL cross-validation vs real trade_log (2026-08-18..20) ---")
            for t in gt:
                if pd.Timestamp("2026-08-18") <= t.entry_time < pd.Timestamp("2026-08-21"):
                    print(f"  SIM  {t.entry_time} @{t.entry_price:.4f} -> {t.exit_time} "
                          f"@{t.exit_price:.4f} {t.reason}")

    print(f"\nWindow {a.start}..{a.end}  ({years:.2f}y)  intrabar={a.intrabar}  "
          f"backstop={'on' if a.backstop else 'off'}  start=${START_BALANCE:,.0f}  "
          f"same_bar_reentry={not a.no_same_bar_reentry}\n")
    hdr = f"{'Ticker':7s} {'Strategy':13s} {'K#':>4s} {'K bal':>12s} {'K CAGR':>9s}   {'G#':>4s} {'G bal':>12s} {'G CAGR':>9s}   {'delta':>9s}"
    print(hdr); print("-" * len(hdr))
    for r in rows:
        print(f"{r['ticker']:7s} {r['strat']:13s} {r['k_n']:4d} {r['k_bal']:12,.0f} {r['k_cagr']:8.1%}   "
              f"{r['g_n']:4d} {r['g_bal']:12,.0f} {r['g_cagr']:8.1%}   {r['g_cagr']-r['k_cagr']:8.1%}")


if __name__ == "__main__":
    main()
