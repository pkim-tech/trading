"""Cheap automated invariant checker for backtester.run_backtest_ground_truth (v6 GT
kernel) trades, built 2026-08-22 as the cost-conscious follow-up to the earlier SOXL
full-manual-reimplementation audit (~420k tokens). Instead of re-simulating a ticker's
entire trade history from scratch, this re-derives, PER TRADE, from raw hourly+minute
price bars, whether the claimed entry/exit/reason satisfies the real strategy rules
(strategies.TrailingExitZScoreBreakout / TrailingBothZScoreBreakout) -- narrow, local
checks bounded to each trade's own entry/exit window, not a second full state machine.

Categories checked per trade:
  - ENTRY: band condition (SMA/Std from the correct prior completed day) satisfied at
    the claimed entry mechanism (open_check immediate fill for TrailingExit; WAIT +
    trailing-buy bounce fill for TrailingBoth), with the WAIT-phase re-derived minute by
    minute from the real signal bar forward for TrailingBoth.
  - SL: exit price/time reproduced from real minute bars (open-gap first, else stop
    level), plus no earlier bar in the hold window would have triggered SL first
    (hourly Low sanity sweep).
  - TRAIL: arm event reproduced (bar close >= entry*(1+arm_pct%) at the claimed Arm
    Time/Price, none earlier), then peak/trail-stop re-derived minute by minute from
    the arm bar forward, exit price/time reproduced exactly.
  - TIME: bar-count held equals max_hours_to_hold exactly, exit price equals exit bar's
    Close.
  - OVERLAP: trades don't illegally overlap (next entry bar >= this exit bar).

Read-only / compute-only. Does not modify backtester.py/strategies.py, does not touch
live state, does not run a sweep.
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from backtester import run_backtest_ground_truth, prep_inputs, prep_minute_inputs

ROOT = Path(__file__).resolve().parent.parent
LIVE_DB = ROOT / "cache" / "live" / "trading_live.db"
HOURLY_DIR = ROOT / "cache" / "research"
MINUTE_DIR = ROOT / "cache" / "research" / "minute_data"

TICKERS = ["AGQ", "KORU", "GDXU"]
TARGET_HOURS = (9, 14)


def get_live_config(ticker):
    con = sqlite3.connect(LIVE_DB)
    con.row_factory = sqlite3.Row
    row = con.execute(
        """SELECT id, ticker, account, strategy, window, take_profit, stop_loss,
                  max_hold_hours, z_score_threshold, trail_sell_pct, fixed_sl,
                  trail_buy_pct, arm_sell_pct, entry_timing, starting_notional
           FROM watch_list WHERE ticker=? AND state='live' AND archived_at IS NULL""",
        (ticker,),
    ).fetchone()
    con.close()
    if not row:
        return None
    d = dict(row)
    d["is_both"] = d["strategy"] == "TrailingBothZScoreBreakout"
    d["arm_pct"] = d["arm_sell_pct"] if d["is_both"] else d["take_profit"]
    return d


def load_hourly(ticker):
    df = pd.read_csv(HOURLY_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.sort_index()


def load_minutes(ticker):
    df = pd.read_csv(MINUTE_DIR / f"{ticker}_1m.csv")
    ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("US/Eastern").dt.tz_localize(None)
    df = df.set_index(ts).sort_index()
    t = df.index.time
    keep = (t >= pd.Timestamp("09:30").time()) & (t < pd.Timestamp("16:00").time())
    return df.loc[keep, ["Open", "High", "Low", "Close"]]


def band_series(df_hourly, window, z_thresh):
    """Independent band-per-hourly-bar series: bar i's band uses the PRIOR completed
    day's SMA/Std (window-day rolling on daily Close), NaN outside target hours."""
    close_col = "Adj Close" if "Adj Close" in df_hourly.columns else "Close"
    daily = df_hourly.resample("D").last().dropna(subset=[close_col])
    sma = daily[close_col].rolling(window=window).mean()
    std = daily[close_col].rolling(window=window).std()
    daily_lookup = {d: i - 1 for i, d in enumerate(daily.index.strftime("%Y-%m-%d"))}
    date_strs = df_hourly.index.strftime("%Y-%m-%d")
    hours = df_hourly.index.hour
    out = np.full(len(df_hourly), np.nan)
    sma_v, std_v = sma.to_numpy(), std.to_numpy()
    for i, (d, h) in enumerate(zip(date_strs, hours)):
        if h not in TARGET_HOURS:
            continue
        di = daily_lookup.get(d, -1)
        if di < 0 or di >= len(sma_v):
            continue
        s, sd = sma_v[di], std_v[di]
        if sd == 0 or np.isnan(s) or np.isnan(sd):
            continue
        out[i] = s - sd * z_thresh
    return out


def minutes_for_bar(minute_df, bar_ts):
    """Minute bars owned by hourly bar starting at bar_ts (H:30 owns [H:30, H+1:30))."""
    end = bar_ts + pd.Timedelta(hours=1)
    return minute_df.loc[(minute_df.index >= bar_ts) & (minute_df.index < end)]


def bar_index_for_time(df_hourly_index, ts):
    """Entry/Exit Time can be a real intrabar minute timestamp (fill resolved mid-bar),
    not just a bar-aligned one -- map to the owning hourly bar (H:30 owns
    [H:30, H+1:30)) via searchsorted instead of an exact index lookup."""
    pos = df_hourly_index.searchsorted(ts, side="right") - 1
    if pos < 0 or pos >= len(df_hourly_index):
        return None
    return int(pos)


def check_sl(row, entry_bar_i, exit_bar_i, df_hourly, minute_df, fixed_sl, fill_open_check):
    ep, xp = row["Entry Price"], row["Exit Price"]
    stop_price = ep * (1 - fixed_sl / 100.0)
    issues = []

    # No-earlier-breach sweep: hourly Low across (entry_bar, exit_bar) exclusive of exit
    # bar itself (checked precisely below), inclusive of entry bar only if entry was a
    # close-check fill (continuous checks start NEXT bar) -- if open_check, continuous
    # checks start same bar right after the fill minute, handled by minute check below.
    start_i = entry_bar_i if fill_open_check else entry_bar_i + 1
    for i in range(start_i, exit_bar_i):
        if df_hourly["Low"].iloc[i] <= stop_price:
            issues.append(f"hourly bar {df_hourly.index[i]} Low={df_hourly['Low'].iloc[i]:.4f} "
                           f"<= stop_price={stop_price:.4f} BEFORE claimed exit bar {df_hourly.index[exit_bar_i]}")

    # Precise minute-level reproduction on the exit bar (skip the fill minute if the
    # exit bar IS the entry bar and entry was open_check, since that same minute can't
    # both fill the entry and exit).
    mbars = minutes_for_bar(minute_df, df_hourly.index[exit_bar_i])
    if mbars.empty:
        issues.append(f"no minute data for exit bar {df_hourly.index[exit_bar_i]} -- cannot verify exact fill")
        return issues
    skip_first = (exit_bar_i == entry_bar_i and fill_open_check)
    rows = mbars.iloc[1:] if skip_first else mbars
    derived_price, derived_time = None, None
    for ts, m in rows.iterrows():
        if m["Open"] <= stop_price:
            derived_price, derived_time = m["Open"], ts
            break
        if m["Low"] <= stop_price:
            derived_price, derived_time = stop_price, ts
            break
    if derived_price is None:
        issues.append(f"no minute on exit bar {df_hourly.index[exit_bar_i]} actually breaches "
                       f"stop_price={stop_price:.4f} -- claimed SL exit at {xp:.4f} unexplained")
    elif abs(derived_price - xp) > 1e-6:
        issues.append(f"derived SL exit price {derived_price:.4f} != claimed {xp:.4f}")
    return issues


def check_time(row, entry_bar_i, exit_bar_i, df_hourly, max_hold, armed):
    issues = []
    held = exit_bar_i - entry_bar_i
    if held != max_hold:
        issues.append(f"bars held {held} != max_hold_hours {max_hold}")
    xp = row["Exit Price"]
    close_at_exit = df_hourly["Close"].iloc[exit_bar_i]
    if abs(xp - close_at_exit) > 1e-6:
        issues.append(f"TIME exit price {xp:.4f} != exit bar Close {close_at_exit:.4f}")
    return issues


def check_trail(row, entry_bar_i, exit_bar_i, df_hourly, minute_df, ep, arm_pct, trail_sell_pct, max_hold):
    issues = []
    arm_time, arm_price = row["Arm Time"], row["Arm Price"]
    expected_arm_trigger = ep * (1 + arm_pct / 100.0)
    try:
        arm_bar_i = df_hourly.index.get_loc(arm_time)
    except KeyError:
        issues.append(f"Arm Time {arm_time} not a real hourly bar")
        return issues
    # No earlier bar between entry and arm_bar (exclusive) should have closed >= trigger.
    for i in range(entry_bar_i, arm_bar_i):
        if df_hourly["Close"].iloc[i] >= expected_arm_trigger:
            issues.append(f"bar {df_hourly.index[i]} Close={df_hourly['Close'].iloc[i]:.4f} already "
                           f">= arm trigger {expected_arm_trigger:.4f} BEFORE claimed arm bar {arm_time}")
    arm_close = df_hourly["Close"].iloc[arm_bar_i]
    if arm_close < expected_arm_trigger:
        issues.append(f"claimed arm bar {arm_time} Close={arm_close:.4f} < arm trigger {expected_arm_trigger:.4f}")
    if abs(arm_price - arm_close) > 1e-6:
        issues.append(f"Arm Price {arm_price:.4f} != arm bar Close {arm_close:.4f}")

    # Re-derive peak/trail-stop minute by minute. Arming is always bar-close-gated (no
    # open-check equivalent for the TP-cross event) -- like the WAIT-via-close-check
    # case, no minutes remain to process retroactively in the arm bar itself, so
    # continuous ARMED checks only begin the NEXT bar.
    peak = arm_close
    tsp = trail_sell_pct / 100.0
    derived_price, derived_time, found = None, None, False
    for i in range(arm_bar_i + 1, exit_bar_i + 1):
        mbars = minutes_for_bar(minute_df, df_hourly.index[i])
        if mbars.empty:
            continue
        for ts, m in mbars.iterrows():
            gap = peak * (1 - tsp)
            if m["Open"] <= gap:
                derived_price, derived_time, found = m["Open"], ts, True
                break
            if m["High"] > peak:
                peak = m["High"]
            stop = peak * (1 - tsp)
            if m["Low"] <= stop:
                derived_price, derived_time, found = stop, ts, True
                break
        if found:
            break
    xp, xt = row["Exit Price"], row["Exit Time"]
    if not found:
        # TRAIL can also exit purely via hold-time expiry while armed (no genuine
        # breach) -- reason is still TRAIL per strategies.py's collapsed WIN/LOSS
        # naming, GT kernel names it TRAIL either way. Accept if held == max_hold.
        held = exit_bar_i - entry_bar_i
        if held != max_hold:
            issues.append(f"no minute breach found through exit bar and held={held} != max_hold={max_hold} "
                           f"-- claimed TRAIL exit unexplained")
        elif abs(xp - df_hourly['Close'].iloc[exit_bar_i]) > 1e-6:
            issues.append(f"hold-time-forced TRAIL exit price {xp:.4f} != exit bar Close "
                           f"{df_hourly['Close'].iloc[exit_bar_i]:.4f}")
    else:
        if derived_time != xt:
            issues.append(f"derived TRAIL exit time {derived_time} != claimed {xt}")
        if abs(derived_price - xp) > 1e-6:
            issues.append(f"derived TRAIL exit price {derived_price:.4f} != claimed {xp:.4f}")
    return issues


def check_entry_trailing_exit(row, entry_bar_i, prev_exit_bar_i, df_hourly, band, entry_timing_open_check):
    """TrailingExitZScoreBreakout (is_both=False): entry fills immediately at the signal
    bar's Open (open_check) or Close (close_check fallback). open_check only applies if
    state was actually IDLE at the START of the entry bar -- if the previous trade's
    position was still open going into this bar (prev_exit_bar_i == entry_bar_i, i.e.
    that prior trade itself exited intrabar/this-bar-close), open_check's step never
    runs this bar and only the bar-close check can produce a same-bar re-entry."""
    issues = []
    i = entry_bar_i
    o, c = df_hourly["Open"].iloc[i], df_hourly["Close"].iloc[i]
    b = band[i]
    ep = row["Entry Price"]
    et = row["Entry Time"]
    if np.isnan(b):
        return [f"entry bar {et} has no valid band (not a target hour or no daily stats)"]
    state_idle_at_bar_start = (prev_exit_bar_i != i)
    fill_open_check = False
    if entry_timing_open_check and state_idle_at_bar_start and abs(ep - o) < 1e-9:
        fill_open_check = True
        if not (o <= b):
            issues.append(f"claimed open_check fill at Open={o:.4f} but Open > band={b:.4f}")
    elif abs(ep - c) < 1e-9:
        if entry_timing_open_check and state_idle_at_bar_start and o <= b:
            issues.append(f"entry recorded as close fill ({c:.4f}) but Open={o:.4f} already <= band={b:.4f} "
                           f"-- should have filled at Open")
        if not (c <= b):
            issues.append(f"claimed close_check fill at Close={c:.4f} but Close > band={b:.4f}")
    else:
        issues.append(f"Entry Price {ep:.4f} matches neither bar Open={o:.4f} nor Close={c:.4f}")
    return issues, fill_open_check


def _find_signal_bar(band, df_hourly, search_start, entry_bar_i, boundary_bar, entry_timing_open_check):
    """First target-hour bar at/after search_start where open_check (Open<=band, only
    if state was IDLE at that bar's own START -- not true for i==boundary_bar) or
    close-check (Close<=band) holds."""
    for i in range(search_start, entry_bar_i + 1):
        b = band[i]
        if np.isnan(b):
            continue
        o, c = df_hourly["Open"].iloc[i], df_hourly["Close"].iloc[i]
        state_idle_at_bar_start = (i != boundary_bar)
        if entry_timing_open_check and state_idle_at_bar_start and o <= b:
            return i
        if c <= b:
            return i
    return None


def _simulate_wait_from(s, df_hourly, minute_df, running_low, tbp, search_ceiling):
    """Re-derive the trailing-buy bounce fill from signal bar s forward, bounded to
    search_ceiling (inclusive). Returns (price, time) or (None, None) if no minute
    triggers the bounce within that range (i.e. WAIT would time out with no trade)."""
    for i in range(s, search_ceiling + 1):
        mbars = minutes_for_bar(minute_df, df_hourly.index[i])
        for ts, m in mbars.iterrows():
            trig_prior = running_low * (1 + tbp)
            if m["Open"] >= trig_prior:
                return m["Open"], ts
            if m["Low"] < running_low:
                running_low = m["Low"]
            trig = running_low * (1 + tbp)
            if m["High"] >= trig:
                return trig, ts
    return None, None


def check_entry_trailing_both(row, prev_exit_bar_i, df_hourly, minute_df, band, trail_buy_pct,
                               entry_timing_open_check, max_hold):
    """TrailingBothZScoreBreakout (is_both=True): WAIT starts at the first bar (>= the
    previous trade's exit bar, hour in target set) where the signal condition holds,
    then the entry price is a trailing-buy bounce off the running low, re-derived
    minute by minute from that signal bar forward. A candidate signal bar whose WAIT
    would time out (max_hold bars, no bounce) before producing a fill leaves NO trade
    in the real output -- silently advance to the next candidate after the timeout
    bar, don't stop at the first band-condition hit."""
    et, ep = row["Entry Time"], row["Entry Price"]
    entry_bar_i = bar_index_for_time(df_hourly.index, et)
    if entry_bar_i is None:
        return [f"Entry Time {et} out of hourly index range"], None

    tbp = trail_buy_pct / 100.0
    search_start = max(prev_exit_bar_i, 0)
    boundary_bar = prev_exit_bar_i

    for _ in range(1000):
        s = _find_signal_bar(band, df_hourly, search_start, entry_bar_i, boundary_bar, entry_timing_open_check)
        if s is None:
            return [f"no target-hour bar between prev exit ({prev_exit_bar_i}) and entry bar ({entry_bar_i}) "
                    f"satisfies the band condition -- WAIT should never have started"], None

        o_s, c_s = df_hourly["Open"].iloc[s], df_hourly["Close"].iloc[s]
        b_s = band[s]
        s_idle_at_start = (s != boundary_bar)
        open_fired_s = entry_timing_open_check and s_idle_at_start and o_s <= b_s
        running_low = o_s if open_fired_s else c_s
        fill_open_check = bool(open_fired_s)

        # A close-check-triggered WAIT starts conceptually at the signal bar's OWN
        # close -- no minutes remain to process retroactively in that same bar, so
        # continuous minute checks only begin the NEXT bar (mirrors check_sl's
        # analogous fill_open_check ? entry_bar_i : entry_bar_i+1 split).
        minute_search_start = s if open_fired_s else s + 1
        timeout_bar = s + max_hold
        ceiling = min(entry_bar_i, timeout_bar)
        price, time_ = _simulate_wait_from(minute_search_start, df_hourly, minute_df, running_low, tbp, ceiling)

        if price is None and timeout_bar < entry_bar_i:
            # This candidate's WAIT timed out with no fill before the claimed entry --
            # not the real signal bar for this trade; advance past the timeout and
            # look for the next candidate.
            search_start = timeout_bar
            boundary_bar = timeout_bar
            continue

        if price is None:
            return [f"no minute-level trailing-buy trigger found from signal bar {df_hourly.index[s]} "
                    f"through entry bar {et} (within max_hold={max_hold}) -- claimed entry unexplained"], fill_open_check
        issues = []
        if time_ != et:
            issues.append(f"derived entry time {time_} != claimed {et} (signal bar {df_hourly.index[s]})")
        if abs(price - ep) > 1e-6:
            issues.append(f"derived entry price {price:.4f} != claimed {ep:.4f} (signal bar {df_hourly.index[s]})")
        return issues, fill_open_check

    return ["gave up after 1000 signal-bar candidates"], None


def run_checker(ticker, cfg):
    df_hourly = load_hourly(ticker)
    minute_df = load_minutes(ticker)
    close_col = "Adj Close" if "Adj Close" in df_hourly.columns else "Close"
    df_daily = df_hourly.resample("D").last().dropna(subset=[close_col])
    strat_cls = getattr(strategies, cfg["strategy"])
    strat_instance = strat_cls(window=cfg["window"], z_score_threshold=cfg["z_score_threshold"])
    df_daily_processed = strat_instance.generate_daily_indicators(df_daily)

    trades = run_backtest_ground_truth(
        df_hourly, df_daily_processed, ticker, minute_df,
        fixed_sl=float(cfg["fixed_sl"]), arm_pct=float(cfg["arm_pct"]),
        trail_buy_pct=float(cfg["trail_buy_pct"]), trail_sell_pct=float(cfg["trail_sell_pct"]),
        max_hours_to_hold=int(cfg["max_hold_hours"]), z_score_threshold=float(cfg["z_score_threshold"]),
        is_both=cfg["is_both"], open_check_entry_timing=(cfg["entry_timing"] == "open_check"),
        same_bar_reentry=True, need_times=True,
    )

    band = band_series(df_hourly, cfg["window"], cfg["z_score_threshold"])
    entry_timing_open_check = cfg["entry_timing"] == "open_check"
    max_hold = int(cfg["max_hold_hours"])

    results = []
    prev_exit_bar_i = -1
    for k, row in enumerate(trades):
        et, xt = row["Entry Time"], row["Exit Time"]
        entry_bar_i = bar_index_for_time(df_hourly.index, et)
        exit_bar_i = bar_index_for_time(df_hourly.index, xt)
        if entry_bar_i is None or exit_bar_i is None:
            results.append({"idx": k, "row": row, "category": "OVERLAP/INDEX",
                             "issues": [f"bar lookup out of range for entry={et} or exit={xt}"]})
            continue

        entry_issues = []
        fill_open_check = entry_timing_open_check
        if cfg["is_both"]:
            entry_issues, fo = check_entry_trailing_both(
                row, prev_exit_bar_i, df_hourly, minute_df, band, cfg["trail_buy_pct"],
                entry_timing_open_check, max_hold)
            if fo is not None:
                fill_open_check = fo
        else:
            entry_issues, fill_open_check = check_entry_trailing_exit(
                row, entry_bar_i, prev_exit_bar_i, df_hourly, band, entry_timing_open_check)

        overlap_issues = []
        if entry_bar_i < prev_exit_bar_i:
            overlap_issues.append(f"entry bar {et} < previous trade's exit bar index {prev_exit_bar_i}")
        prev_exit_bar_i = exit_bar_i

        reason = row["exit_reason"]
        if reason == "SL":
            exit_issues = check_sl(row, entry_bar_i, exit_bar_i, df_hourly, minute_df,
                                    cfg["fixed_sl"], fill_open_check)
        elif reason == "TIME":
            exit_issues = check_time(row, entry_bar_i, exit_bar_i, df_hourly, max_hold, row["armed"])
        elif reason == "TRAIL":
            exit_issues = check_trail(row, entry_bar_i, exit_bar_i, df_hourly, minute_df,
                                       row["Entry Price"], cfg["arm_pct"], cfg["trail_sell_pct"], max_hold)
        else:
            exit_issues = [f"unknown exit_reason {reason}"]

        all_issues = entry_issues + overlap_issues + exit_issues
        results.append({"idx": k, "row": row, "category": reason, "issues": all_issues})
    return trades, results


def main():
    all_results = {}
    for ticker in TICKERS:
        cfg = get_live_config(ticker)
        if not cfg:
            print(f"{ticker}: SKIP, no live watch_list row")
            continue
        print(f"\n{'='*90}\n{ticker}  strategy={cfg['strategy']} window={cfg['window']} z={cfg['z_score_threshold']} "
              f"fixed_sl={cfg['fixed_sl']} arm_pct={cfg['arm_pct']} trail_buy={cfg['trail_buy_pct']} "
              f"trail_sell={cfg['trail_sell_pct']} hold={cfg['max_hold_hours']}h entry_timing={cfg['entry_timing']}\n{'='*90}")
        trades, results = run_checker(ticker, cfg)
        all_results[ticker] = (trades, results)

        by_cat = {}
        for r in results:
            by_cat.setdefault(r["category"], [0, 0])
            if r["issues"]:
                by_cat[r["category"]][1] += 1
            else:
                by_cat[r["category"]][0] += 1
        print(f"  total trades: {len(trades)}")
        for cat, (ok, bad) in sorted(by_cat.items()):
            print(f"  {cat}: {ok} pass, {bad} FLAGGED")

        flagged = [r for r in results if r["issues"]]
        if flagged:
            print(f"\n  --- FLAGGED TRADES ({len(flagged)}) ---")
            for r in flagged:
                row = r["row"]
                print(f"  [{r['idx']}] {r['category']} entry={row['Entry Time']}@{row['Entry Price']:.4f} "
                      f"exit={row['Exit Time']}@{row['Exit Price']:.4f}")
                for issue in r["issues"]:
                    print(f"      - {issue}")
        else:
            print("  no flagged trades")

    return all_results


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
