import numpy as np
import pandas as pd
from numba import njit

# Result codes
WIN   = 0
LOSS  = 1
TWIN  = 2
TLOSS = 3
OPEN  = 4

_RESULT_NAMES = {WIN: 'WIN', LOSS: 'LOSS', TWIN: 'TWIN', TLOSS: 'TLOSS', OPEN: 'OPEN'}

MAX_TRADES = 5000


def prep_inputs(df_hourly, df_daily_indicators):
    """Kernel input arrays. Depends only on (hourly data, indicators) — cacheable
    per (ticker, strategy, window) across grid nodes; z/tp/sl/hold are kernel args."""
    timestamps = df_hourly.index
    date_strs = timestamps.strftime('%Y-%m-%d')
    # Map each hourly bar to the most recently *completed* day's row (i-1, not i) —
    # day D's own row is built from D's close, which isn't known during D's intraday
    # bars. Mirrors active_signals.compute_buy_signal's `df_daily.index < today` cutoff.
    daily_lookup = {d: i - 1 for i, d in enumerate(df_daily_indicators.index.strftime('%Y-%m-%d'))}
    prices = df_hourly['Close'].to_numpy(dtype=np.float64)
    has_trend = 'Trend_Filter' in df_daily_indicators.columns
    return {
        'timestamps': timestamps,
        'prices':     prices,
        'highs':      df_hourly['High'].to_numpy(dtype=np.float64) if 'High' in df_hourly.columns else prices,
        'lows':       df_hourly['Low'].to_numpy(dtype=np.float64) if 'Low' in df_hourly.columns else prices,
        'opens':      df_hourly['Open'].to_numpy(dtype=np.float64) if 'Open' in df_hourly.columns else prices,
        'hours':      timestamps.hour.to_numpy(dtype=np.int64),
        'daily_idx':  np.array([daily_lookup.get(d, -1) for d in date_strs], dtype=np.int64),
        'sma_arr':    df_daily_indicators['SMA'].to_numpy(dtype=np.float64),
        'std_arr':    df_daily_indicators['Std'].to_numpy(dtype=np.float64),
        'trend_arr':  df_daily_indicators['Trend_Filter'].to_numpy(dtype=np.float64) if has_trend else np.zeros(1, dtype=np.float64),
        'has_trend':  has_trend,
    }


def _build_trades(ticker, timestamps, ei, xi, ep, xp, held, res, ret):
    trades = []
    for k in range(len(ei)):
        trades.append({
            'Ticker':      ticker,
            'Entry Time':  timestamps[ei[k]],
            'Entry Price': ep[k],
            'Exit Time':   timestamps[xi[k]],
            'Exit Price':  xp[k],
            'hours_held':  int(held[k]),
            'Result':      _RESULT_NAMES[res[k]],
            'Return':      ret[k]
        })
    return trades


# No live-watchlist strategy uses this kernel (all 11 live tickers run
# TrailingBothZScoreBreakout / _simulate_trail_both) — not in scope for the v4
# fill-optimism/worst-case-bound pass. See docs/backlog_cache.md.
@njit(cache=True)
def _simulate(prices, hours, daily_idx, sma_arr, std_arr, trend_arr, has_trend,
              take_profit, stop_loss, max_hours_to_hold, target_h0, target_h1, z_thresh):
    # Pre-allocated output arrays
    entry_i   = np.empty(MAX_TRADES, dtype=np.int64)
    exit_i    = np.empty(MAX_TRADES, dtype=np.int64)
    entry_p   = np.empty(MAX_TRADES, dtype=np.float64)
    exit_p    = np.empty(MAX_TRADES, dtype=np.float64)
    hours_held= np.empty(MAX_TRADES, dtype=np.int64)
    results   = np.empty(MAX_TRADES, dtype=np.int64)
    returns   = np.empty(MAX_TRADES, dtype=np.float64)
    count     = 0

    in_trade     = False
    entry_price  = 0.0
    entry_bar    = 0
    held         = 0

    n = len(prices)
    for i in range(n):
        cp = prices[i]

        if in_trade:
            held += 1
            pc = (cp - entry_price) / entry_price

            if pc >= take_profit:
                entry_i[count]    = entry_bar
                exit_i[count]     = i
                entry_p[count]    = entry_price
                exit_p[count]     = cp
                hours_held[count] = held
                results[count]    = WIN
                returns[count]    = pc
                count += 1
                in_trade = False
                continue

            elif pc <= -stop_loss:
                entry_i[count]    = entry_bar
                exit_i[count]     = i
                entry_p[count]    = entry_price
                exit_p[count]     = cp
                hours_held[count] = held
                results[count]    = LOSS
                returns[count]    = pc
                count += 1
                in_trade = False
                continue

            elif held >= max_hours_to_hold:
                entry_i[count]    = entry_bar
                exit_i[count]     = i
                entry_p[count]    = entry_price
                exit_p[count]     = cp
                hours_held[count] = held
                results[count]    = TWIN if pc > 0 else TLOSS
                returns[count]    = pc
                count += 1
                in_trade = False
                continue

            continue

        h = hours[i]
        if h != target_h0 and h != target_h1:
            continue

        di = daily_idx[i]
        if di < 0:
            continue

        sma = sma_arr[di]
        std = std_arr[di]
        if std == 0.0:
            continue

        lower_band = sma - std * z_thresh

        if has_trend:
            trend = trend_arr[di]
            signal = (cp <= lower_band) and (cp > trend)
        else:
            signal = cp <= lower_band

        if signal:
            in_trade    = True
            entry_price = cp
            entry_bar   = i
            held        = 0

    # Handle open position at end of data
    if in_trade:
        cp = prices[n - 1]
        pc = (cp - entry_price) / entry_price
        entry_i[count]    = entry_bar
        exit_i[count]     = n - 1
        entry_p[count]    = entry_price
        exit_p[count]     = cp
        hours_held[count] = held
        results[count]    = OPEN
        returns[count]    = pc
        count += 1

    return entry_i[:count], exit_i[:count], entry_p[:count], exit_p[:count], hours_held[:count], results[:count], returns[:count]


@njit(cache=True)
def _simulate_limit(prices, lows, hours, daily_idx, sma_arr, std_arr, trend_arr, has_trend,
                    take_profit, stop_loss, max_hours_to_hold, target_h0, target_h1, z_thresh):
    entry_i    = np.empty(MAX_TRADES, dtype=np.int64)
    exit_i     = np.empty(MAX_TRADES, dtype=np.int64)
    entry_p    = np.empty(MAX_TRADES, dtype=np.float64)
    exit_p     = np.empty(MAX_TRADES, dtype=np.float64)
    hours_held = np.empty(MAX_TRADES, dtype=np.int64)
    results    = np.empty(MAX_TRADES, dtype=np.int64)
    returns    = np.empty(MAX_TRADES, dtype=np.float64)
    count      = 0

    in_trade    = False
    entry_price = 0.0
    tp_price    = 0.0
    stop_price  = 0.0
    entry_bar   = 0
    held        = 0

    n = len(prices)
    for i in range(n):
        cp  = prices[i]
        low = lows[i]

        if in_trade:
            held += 1
            # SL first: stop order triggers intrabar
            if low <= stop_price:
                pc = (stop_price - entry_price) / entry_price
                entry_i[count]    = entry_bar
                exit_i[count]     = i
                entry_p[count]    = entry_price
                exit_p[count]     = stop_price
                hours_held[count] = held
                results[count]    = LOSS
                returns[count]    = pc
                count += 1
                in_trade = False
                continue

            # TP: bar-close check, matches live Slack signal
            if cp >= tp_price:
                pc = (cp - entry_price) / entry_price
                entry_i[count]    = entry_bar
                exit_i[count]     = i
                entry_p[count]    = entry_price
                exit_p[count]     = cp
                hours_held[count] = held
                results[count]    = WIN
                returns[count]    = pc
                count += 1
                in_trade = False
                continue

            if held >= max_hours_to_hold:
                pc = (cp - entry_price) / entry_price
                entry_i[count]    = entry_bar
                exit_i[count]     = i
                entry_p[count]    = entry_price
                exit_p[count]     = cp
                hours_held[count] = held
                results[count]    = TWIN if pc > 0 else TLOSS
                returns[count]    = pc
                count += 1
                in_trade = False
                continue

            continue

        h = hours[i]
        if h != target_h0 and h != target_h1:
            continue

        di = daily_idx[i]
        if di < 0:
            continue

        sma = sma_arr[di]
        std = std_arr[di]
        if std == 0.0:
            continue

        lower_band = sma - std * z_thresh

        if has_trend:
            trend = trend_arr[di]
            signal = (low <= lower_band) and (cp > trend)
        else:
            signal = low <= lower_band

        if signal:
            in_trade    = True
            entry_price = lower_band
            tp_price    = lower_band * (1.0 + take_profit)
            stop_price  = lower_band * (1.0 - stop_loss)
            entry_bar   = i
            held        = 0

    if in_trade:
        cp = prices[n - 1]
        pc = (cp - entry_price) / entry_price
        entry_i[count]    = entry_bar
        exit_i[count]     = n - 1
        entry_p[count]    = entry_price
        exit_p[count]     = cp
        hours_held[count] = held
        results[count]    = OPEN
        returns[count]    = pc
        count += 1

    return entry_i[:count], exit_i[:count], entry_p[:count], exit_p[:count], hours_held[:count], results[:count], returns[:count]


def run_backtest_v17(df_hourly, df_daily_indicators, ticker,
                     mode="BACKTEST", target_hours=(9, 14),
                     take_profit=0.05, stop_loss=0.15, max_hours_to_hold=28, z_score_threshold=2.0,
                     prep=None):
    p = prep if prep is not None else prep_inputs(df_hourly, df_daily_indicators)
    target_h0, target_h1 = int(target_hours[0]), int(target_hours[1])

    ei, xi, ep, xp, held, res, ret = _simulate_limit(
        p['prices'], p['lows'], p['hours'], p['daily_idx'],
        p['sma_arr'], p['std_arr'], p['trend_arr'], p['has_trend'],
        float(take_profit), float(stop_loss), int(max_hours_to_hold),
        target_h0, target_h1, float(z_score_threshold)
    )
    return _build_trades(ticker, p['timestamps'], ei, xi, ep, xp, held, res, ret)


@njit(cache=True)
def _simulate_trail(prices, highs, lows, opens, hours, daily_idx, sma_arr, std_arr, trend_arr, has_trend,
                    take_profit, stop_loss, max_hours_to_hold, trail_pct, target_h0, target_h1, z_thresh,
                    open_check_entry_timing=False):
    entry_i    = np.empty(MAX_TRADES, dtype=np.int64)
    exit_i     = np.empty(MAX_TRADES, dtype=np.int64)
    entry_p    = np.empty(MAX_TRADES, dtype=np.float64)
    exit_p     = np.empty(MAX_TRADES, dtype=np.float64)
    hours_held = np.empty(MAX_TRADES, dtype=np.int64)
    results    = np.empty(MAX_TRADES, dtype=np.int64)
    returns    = np.empty(MAX_TRADES, dtype=np.float64)
    count      = 0

    in_trade    = False
    trailing    = False
    entry_price = 0.0
    stop_price  = 0.0
    tp_price    = 0.0
    peak        = 0.0
    entry_bar   = 0
    held        = 0

    n = len(prices)
    for i in range(n):
        cp   = prices[i]
        op   = opens[i]
        high = highs[i]
        low  = lows[i]

        if in_trade:
            held += 1

            if trailing:
                # Open is chronologically first -- if it already gapped past the
                # trailing-stop confirmed through the prior bar (peak not yet
                # updated with this bar's high), that's the honest fill, not the
                # theoretical trail_stop (mirrors the entry-side gap-through-
                # trigger fix, see docs/backlog_cache.md).
                trail_stop_gap = peak * (1.0 - trail_pct)
                if op <= trail_stop_gap:
                    exit_px = op
                    pc = (exit_px - entry_price) / entry_price
                    entry_i[count]    = entry_bar
                    exit_i[count]     = i
                    entry_p[count]    = entry_price
                    exit_p[count]     = exit_px
                    hours_held[count] = held
                    results[count]    = WIN if pc > 0 else LOSS
                    returns[count]    = pc
                    count += 1
                    in_trade = False
                    trailing = False
                    continue
                if high > peak:
                    peak = high
                trail_stop = peak * (1.0 - trail_pct)
                if low <= trail_stop or held >= max_hours_to_hold:
                    exit_px = trail_stop if low <= trail_stop else cp
                    pc = (exit_px - entry_price) / entry_price
                    entry_i[count]    = entry_bar
                    exit_i[count]     = i
                    entry_p[count]    = entry_price
                    exit_p[count]     = exit_px
                    hours_held[count] = held
                    results[count]    = WIN if pc > 0 else LOSS
                    returns[count]    = pc
                    count += 1
                    in_trade = False
                    trailing = False
                continue

            # SL check -- Open-first gap check before falling to the intrabar
            # Low check (mirrors the entry-side gap-through-trigger fix).
            if op <= stop_price:
                pc = (op - entry_price) / entry_price
                entry_i[count]    = entry_bar
                exit_i[count]     = i
                entry_p[count]    = entry_price
                exit_p[count]     = op
                hours_held[count] = held
                results[count]    = LOSS
                returns[count]    = pc
                count += 1
                in_trade = False
                continue
            if low <= stop_price:
                pc = (stop_price - entry_price) / entry_price
                entry_i[count]    = entry_bar
                exit_i[count]     = i
                entry_p[count]    = entry_price
                exit_p[count]     = stop_price
                hours_held[count] = held
                results[count]    = LOSS
                returns[count]    = pc
                count += 1
                in_trade = False
                continue

            # TP activation — switch to trailing mode
            if cp >= tp_price:
                trailing = True
                peak     = cp
                continue

            # Max hold before TP
            if held >= max_hours_to_hold:
                pc = (cp - entry_price) / entry_price
                entry_i[count]    = entry_bar
                exit_i[count]     = i
                entry_p[count]    = entry_price
                exit_p[count]     = cp
                hours_held[count] = held
                results[count]    = TWIN if pc > 0 else TLOSS
                returns[count]    = pc
                count += 1
                in_trade = False
                continue

            continue

        h = hours[i]
        if h != target_h0 and h != target_h1:
            continue

        di = daily_idx[i]
        if di < 0:
            continue

        sma = sma_arr[di]
        std = std_arr[di]
        if std == 0.0:
            continue

        lower_band = sma - std * z_thresh

        fired = False
        if open_check_entry_timing:
            if has_trend:
                signal_open = (op <= lower_band) and (op > trend_arr[di])
            else:
                signal_open = op <= lower_band
            if signal_open:
                in_trade    = True
                trailing    = False
                entry_price = op
                tp_price    = op * (1.0 + take_profit)
                stop_price  = op * (1.0 - stop_loss)
                entry_bar   = i
                held        = 0
                fired = True

        if not fired:
            if has_trend:
                trend = trend_arr[di]
                signal = (cp <= lower_band) and (cp > trend)
            else:
                signal = cp <= lower_band

            if signal:
                in_trade    = True
                trailing    = False
                entry_price = cp
                tp_price    = cp * (1.0 + take_profit)
                stop_price  = cp * (1.0 - stop_loss)
                entry_bar   = i
                held        = 0

    if in_trade:
        cp = prices[n - 1]
        pc = (cp - entry_price) / entry_price
        entry_i[count]    = entry_bar
        exit_i[count]     = n - 1
        entry_p[count]    = entry_price
        exit_p[count]     = cp
        hours_held[count] = held
        results[count]    = OPEN
        returns[count]    = pc
        count += 1

    return entry_i[:count], exit_i[:count], entry_p[:count], exit_p[:count], hours_held[:count], results[:count], returns[:count]


def run_backtest_v18(df_hourly, df_daily_indicators, ticker,
                     mode="BACKTEST", target_hours=(9, 14),
                     take_profit=0.05, stop_loss=0.15, max_hours_to_hold=28,
                     z_score_threshold=2.0, trail_pct=0.03, entry_timing='close', prep=None):
    p = prep if prep is not None else prep_inputs(df_hourly, df_daily_indicators)
    target_h0, target_h1 = int(target_hours[0]), int(target_hours[1])

    ei, xi, ep, xp, held, res, ret = _simulate_trail(
        p['prices'], p['highs'], p['lows'], p['opens'], p['hours'], p['daily_idx'],
        p['sma_arr'], p['std_arr'], p['trend_arr'], p['has_trend'],
        float(take_profit), float(stop_loss), int(max_hours_to_hold), float(trail_pct),
        target_h0, target_h1, float(z_score_threshold), entry_timing == 'open_check'
    )
    return _build_trades(ticker, p['timestamps'], ei, xi, ep, xp, held, res, ret)


# No live-watchlist strategy uses this kernel (all 11 live tickers run
# TrailingBothZScoreBreakout / _simulate_trail_both) — not in scope for the v4
# fill-optimism/worst-case-bound pass. See docs/backlog_cache.md.
@njit(cache=True)
def _simulate_trail_buy(prices, highs, lows, opens, hours, daily_idx, sma_arr, std_arr, trend_arr, has_trend,
                        take_profit, stop_loss, max_hours_to_hold, trail_buy_pct, target_h0, target_h1, z_thresh):
    entry_i    = np.empty(MAX_TRADES, dtype=np.int64)
    exit_i     = np.empty(MAX_TRADES, dtype=np.int64)
    entry_p    = np.empty(MAX_TRADES, dtype=np.float64)
    exit_p     = np.empty(MAX_TRADES, dtype=np.float64)
    hours_held = np.empty(MAX_TRADES, dtype=np.int64)
    results    = np.empty(MAX_TRADES, dtype=np.int64)
    returns    = np.empty(MAX_TRADES, dtype=np.float64)
    count      = 0

    in_trade    = False
    waiting     = False
    entry_price = 0.0
    stop_price  = 0.0
    tp_price    = 0.0
    entry_bar   = 0
    held        = 0
    running_low = 0.0
    wait_bars   = 0

    n = len(prices)
    for i in range(n):
        cp   = prices[i]
        op   = opens[i]
        high = highs[i]
        low  = lows[i]

        if in_trade:
            held += 1
            if low <= stop_price:
                pc = (stop_price - entry_price) / entry_price
                entry_i[count] = entry_bar; exit_i[count] = i
                entry_p[count] = entry_price; exit_p[count] = stop_price
                hours_held[count] = held; results[count] = LOSS; returns[count] = pc
                count += 1; in_trade = False
                continue
            if cp >= tp_price:
                pc = (cp - entry_price) / entry_price
                entry_i[count] = entry_bar; exit_i[count] = i
                entry_p[count] = entry_price; exit_p[count] = cp
                hours_held[count] = held; results[count] = WIN; returns[count] = pc
                count += 1; in_trade = False
                continue
            if held >= max_hours_to_hold:
                pc = (cp - entry_price) / entry_price
                entry_i[count] = entry_bar; exit_i[count] = i
                entry_p[count] = entry_price; exit_p[count] = cp
                hours_held[count] = held
                results[count] = TWIN if pc > 0 else TLOSS; returns[count] = pc
                count += 1; in_trade = False
                continue
            continue

        if waiting:
            wait_bars += 1
            # Open is chronologically first -- an overnight/intraday gap past
            # the trigger confirmed through the prior bar is the honest fill.
            buy_trigger_gap = running_low * (1.0 + trail_buy_pct)
            if op >= buy_trigger_gap:
                entry_price = op
                tp_price    = entry_price * (1.0 + take_profit)
                stop_price  = entry_price * (1.0 - stop_loss)
                entry_bar   = i; held = 0
                in_trade = True; waiting = False
                continue
            if low < running_low:
                running_low = low
            buy_trigger = running_low * (1.0 + trail_buy_pct)
            if high >= buy_trigger:
                entry_price = buy_trigger
                tp_price    = entry_price * (1.0 + take_profit)
                stop_price  = entry_price * (1.0 - stop_loss)
                entry_bar   = i; held = 0
                in_trade = True; waiting = False
                continue
            if wait_bars >= max_hours_to_hold:
                waiting = False
            continue

        h = hours[i]
        if h != target_h0 and h != target_h1:
            continue
        di = daily_idx[i]
        if di < 0:
            continue
        sma = sma_arr[di]; std = std_arr[di]
        if std == 0.0:
            continue
        lower_band = sma - std * z_thresh
        if has_trend:
            signal = (cp <= lower_band) and (cp > trend_arr[di])
        else:
            signal = cp <= lower_band
        if signal:
            waiting = True; running_low = cp; wait_bars = 0

    if in_trade:
        cp = prices[n - 1]
        pc = (cp - entry_price) / entry_price
        entry_i[count] = entry_bar; exit_i[count] = n - 1
        entry_p[count] = entry_price; exit_p[count] = cp
        hours_held[count] = held; results[count] = OPEN; returns[count] = pc
        count += 1

    return entry_i[:count], exit_i[:count], entry_p[:count], exit_p[:count], hours_held[:count], results[:count], returns[:count]


@njit(cache=True)
def _simulate_trail_both(prices, highs, lows, hours, daily_idx, sma_arr, std_arr, trend_arr, has_trend,
                         take_profit, stop_loss, max_hours_to_hold, trail_buy_pct, trail_pct, target_h0, target_h1, z_thresh,
                         opens, open_check_entry_timing, same_day_block=False, min_hold_hours=0):
    """Three parallel trailing-buy bounce-fill resolutions, run in one pass over the
    same bars — see docs/backlog_cache.md fill-optimism item. None of OHLC's Open/
    High/Low/Close proves the true intrabar path, so none of these is a rigorous
    bound on the others; they're three honestly-labeled, differently-reasoned
    simulations, not a [worst, best] interval:
      - possible (existing, unchanged): assumes Low-before-High every ambiguous
        bar — this bar's own dip is folded into running_low before checking
        whether High clears the trigger. A plausible single guess, not a proven
        best case (if High actually came first, this fill may not have happened
        this bar at all).
      - pessimistic (new): the mirror-image single guess — assumes
        High-before-Low, so High is checked against the trigger from
        running_low as confirmed through the *prior* bar only, never benefiting
        from this bar's own dip. Always fires on the same bar as 'possible' or
        later, always at the same-or-worse trigger price — a real bracket
        partner for 'possible', unlike 'certain' below.
      - certain (new, corrected 2026-08-09): only resolves a fill when provably
        true regardless of ordering, via three real cases: (1) this bar's Open
        clears the trigger from the prior-confirmed running_low (Open is
        chronologically first) -- fills at that Open price, can be better than
        'possible'; (2) this bar's own Low doesn't move running_low (the
        trigger was frozen for the whole bar, no ordering ambiguity at all) and
        High clears it -- fills at that frozen trigger price, same determinism
        as case 1, can also be better than 'possible'; (3) this bar's own Low
        DOES move running_low (a genuinely ambiguous bar) but the bar's Close
        still clears the resulting lower trigger -- fills at
        min(buy_trigger_prior, high), the true worst-case price provable over
        BOTH intrabar orderings (see the code comment at the certain branch's
        Close-confirm case for the proof). Case 3 is a genuine
        pessimistic-price bound, unlike cases 1-2 -- 'certain' is NOT uniformly
        a no-guessing-but-possibly-optimistic resolution; it mixes a
        can-beat-possible case with a worst-case-price case depending on which
        of the three fires. Anything not covered by these three defers,
        letting running_low fall further before the next check.
    Exit-side logic (SL/TP/trailing/TIME) is identical/shared across all three —
    not an ordering ambiguity, see backlog. Both intrabar-continuous exit
    triggers (SL, trailing-stop) check the bar's Open against the level
    confirmed through the prior bar before falling to the Low check, same
    gap-through-trigger treatment as the entry side (2026-07-20 fix — the
    original fix only covered entry; exit had the identical latent bug).
    open_check_entry_timing: if True,
    also check the bar's Open against the entry threshold before falling through
    to the normal Close check (same bar/iteration, no synthetic bar) — shared by
    all three since entry-signal timing is a behavior choice, not an ambiguity.
    same_day_block: if True, mirrors schwab_safety's real cash-account same-day-
    re-buy rule — a fresh signal is ignored (not just delayed one bar) on any day
    that matches this same resolution's own most recent exit day. Because the
    signal-detection block runs again on the next eligible target-hour bar
    regardless, a blocked day naturally keeps re-checking on subsequent days
    rather than the entry being discarded outright (see docs/backlog_cache.md's
    same-day-re-buy delayed-vs-dropped item — this is the 'delayed' behavior,
    not the naive drop). Tracked independently per resolution (possible/
    pessimistic/certain) since they can produce different exit days.
    min_hold_hours: floor mirroring max_hours_to_hold's ceiling — a real firm
    compliance policy (min_hold_hours=0, the default, reproduces prior behavior
    exactly). While held < min_hold_hours, every exit branch (SL, trailing-stop
    breach, TIME) is suppressed for that bar and re-checked on the next one;
    state (peak, running_low, etc.) keeps updating normally, only the actual
    position-closing action is blocked. TP-arming (cp >= tp_price, which only
    flips state['trailing']=True in this strategy, not an exit by itself) has
    no min_hold_hours check of its own — the real exit still can't fire until
    min_hold_hours regardless, via the trailing-stop-breach branch above.
    Imprecise to call this "not gated" outright, though (contextual review
    finding, 2026-08-17): a bar whose SL condition is true but blocked by the
    floor still consumes that bar's elif chain (correctly, so the blocked SL
    isn't silently replaced by an arm), so TP-arming is skipped on that
    specific bar — a state pre-diff was unreachable (an unblocked SL bar
    always exited, never got this far). Net effect is a one-bar-later arm in
    that rare case, not a real gap. Research/backtest scope only — this
    policy requires firm compliance sign-off on both entry AND exit in real
    trading, so no live-automation path reads this field; see
    docs/backlog_cache.md."""
    entry_i    = np.empty(MAX_TRADES, dtype=np.int64)
    exit_i     = np.empty(MAX_TRADES, dtype=np.int64)
    entry_p    = np.empty(MAX_TRADES, dtype=np.float64)
    exit_p     = np.empty(MAX_TRADES, dtype=np.float64)
    hours_held = np.empty(MAX_TRADES, dtype=np.int64)
    results    = np.empty(MAX_TRADES, dtype=np.int64)
    returns    = np.empty(MAX_TRADES, dtype=np.float64)
    count      = 0

    entry_i_p    = np.empty(MAX_TRADES, dtype=np.int64)
    exit_i_p     = np.empty(MAX_TRADES, dtype=np.int64)
    entry_p_p    = np.empty(MAX_TRADES, dtype=np.float64)
    exit_p_p     = np.empty(MAX_TRADES, dtype=np.float64)
    hours_held_p = np.empty(MAX_TRADES, dtype=np.int64)
    results_p    = np.empty(MAX_TRADES, dtype=np.int64)
    returns_p    = np.empty(MAX_TRADES, dtype=np.float64)
    count_p      = 0

    entry_i_c    = np.empty(MAX_TRADES, dtype=np.int64)
    exit_i_c     = np.empty(MAX_TRADES, dtype=np.int64)
    entry_p_c    = np.empty(MAX_TRADES, dtype=np.float64)
    exit_p_c     = np.empty(MAX_TRADES, dtype=np.float64)
    hours_held_c = np.empty(MAX_TRADES, dtype=np.int64)
    results_c    = np.empty(MAX_TRADES, dtype=np.int64)
    returns_c    = np.empty(MAX_TRADES, dtype=np.float64)
    count_c      = 0

    in_trade     = False
    waiting      = False
    trailing     = False
    entry_price  = 0.0
    stop_price   = 0.0
    tp_price     = 0.0
    peak         = 0.0
    entry_bar    = 0
    held         = 0
    running_low  = 0.0
    wait_bars    = 0
    last_exit_day = -1

    in_trade_p    = False
    waiting_p     = False
    trailing_p    = False
    entry_price_p = 0.0
    stop_price_p  = 0.0
    tp_price_p    = 0.0
    peak_p        = 0.0
    entry_bar_p   = 0
    held_p        = 0
    running_low_p = 0.0
    wait_bars_p   = 0
    last_exit_day_p = -1

    in_trade_c    = False
    waiting_c     = False
    trailing_c    = False
    entry_price_c = 0.0
    stop_price_c  = 0.0
    tp_price_c    = 0.0
    peak_c        = 0.0
    entry_bar_c   = 0
    held_c        = 0
    running_low_c = 0.0
    wait_bars_c   = 0
    last_exit_day_c = -1

    n = len(prices)
    for i in range(n):
        cp   = prices[i]
        op   = opens[i]
        high = highs[i]
        low  = lows[i]

        # ── possible: Low-before-High assumption (existing/unchanged logic) ──
        if in_trade:
            held += 1
            if trailing:
                # Open-first gap check on the trailing-stop, mirrors the entry-
                # side gap-through-trigger fix (see docs/backlog_cache.md).
                trail_stop_gap = peak * (1.0 - trail_pct)
                gap_exit = op <= trail_stop_gap
                # peak/trail_stop must keep updating every bar regardless of
                # min_hold_hours (a blocked exit is not a frozen bar) -- see
                # _simulate_trail_both's docstring.
                if high > peak:
                    peak = high
                trail_stop = peak * (1.0 - trail_pct)
                if gap_exit:
                    if held >= min_hold_hours:
                        exit_px = op
                        pc = (exit_px - entry_price) / entry_price
                        entry_i[count] = entry_bar; exit_i[count] = i
                        entry_p[count] = entry_price; exit_p[count] = exit_px
                        hours_held[count] = held
                        results[count] = WIN if pc > 0 else LOSS; returns[count] = pc
                        count += 1; in_trade = False; trailing = False
                        last_exit_day = daily_idx[i]
                elif low <= trail_stop or held >= max_hours_to_hold:
                    if held >= min_hold_hours:
                        exit_px = trail_stop if low <= trail_stop else cp
                        pc = (exit_px - entry_price) / entry_price
                        entry_i[count] = entry_bar; exit_i[count] = i
                        entry_p[count] = entry_price; exit_p[count] = exit_px
                        hours_held[count] = held
                        results[count] = WIN if pc > 0 else LOSS; returns[count] = pc
                        count += 1; in_trade = False; trailing = False
                        last_exit_day = daily_idx[i]
            elif op <= stop_price:
                if held >= min_hold_hours:
                    pc = (op - entry_price) / entry_price
                    entry_i[count] = entry_bar; exit_i[count] = i
                    entry_p[count] = entry_price; exit_p[count] = op
                    hours_held[count] = held; results[count] = LOSS; returns[count] = pc
                    count += 1; in_trade = False
                    last_exit_day = daily_idx[i]
            elif low <= stop_price:
                if held >= min_hold_hours:
                    pc = (stop_price - entry_price) / entry_price
                    entry_i[count] = entry_bar; exit_i[count] = i
                    entry_p[count] = entry_price; exit_p[count] = stop_price
                    hours_held[count] = held; results[count] = LOSS; returns[count] = pc
                    count += 1; in_trade = False
                    last_exit_day = daily_idx[i]
            elif cp >= tp_price:
                trailing = True; peak = cp
            elif held >= max_hours_to_hold:
                if held >= min_hold_hours:
                    pc = (cp - entry_price) / entry_price
                    entry_i[count] = entry_bar; exit_i[count] = i
                    entry_p[count] = entry_price; exit_p[count] = cp
                    hours_held[count] = held
                    results[count] = TWIN if pc > 0 else TLOSS; returns[count] = pc
                    count += 1; in_trade = False
                    last_exit_day = daily_idx[i]
        elif waiting:
            wait_bars += 1
            # Open is chronologically first -- if it already gapped past the
            # trigger confirmed through the prior bar, that's the honest fill,
            # not the theoretical trigger price (see gap-through-trigger item,
            # docs/backlog_cache.md).
            buy_trigger_gap = running_low * (1.0 + trail_buy_pct)
            if op >= buy_trigger_gap:
                entry_price = op
                tp_price    = entry_price * (1.0 + take_profit)
                stop_price  = entry_price * (1.0 - stop_loss)
                entry_bar   = i; held = 0
                in_trade = True; waiting = False; trailing = False
            else:
                if low < running_low:
                    running_low = low
                buy_trigger = running_low * (1.0 + trail_buy_pct)
                if high >= buy_trigger:
                    entry_price = buy_trigger
                    tp_price    = entry_price * (1.0 + take_profit)
                    stop_price  = entry_price * (1.0 - stop_loss)
                    entry_bar   = i; held = 0
                    in_trade = True; waiting = False; trailing = False
                elif wait_bars >= max_hours_to_hold:
                    waiting = False
        else:
            h = hours[i]
            if h == target_h0 or h == target_h1:
                di = daily_idx[i]
                if di >= 0:
                    sma = sma_arr[di]; std = std_arr[di]
                    if std != 0.0:
                        lower_band = sma - std * z_thresh
                        blocked = same_day_block and di == last_exit_day
                        fired = False
                        if not blocked:
                            if open_check_entry_timing:
                                if has_trend:
                                    signal_open = (op <= lower_band) and (op > trend_arr[di])
                                else:
                                    signal_open = op <= lower_band
                                if signal_open:
                                    waiting = True; running_low = op; wait_bars = 0
                                    fired = True
                            if not fired:
                                if has_trend:
                                    signal = (cp <= lower_band) and (cp > trend_arr[di])
                                else:
                                    signal = cp <= lower_band
                                if signal:
                                    waiting = True; running_low = cp; wait_bars = 0

        # ── pessimistic: High-before-Low assumption (mirror of 'possible') ──
        if in_trade_p:
            held_p += 1
            if trailing_p:
                trail_stop_p_gap = peak_p * (1.0 - trail_pct)
                gap_exit_p = op <= trail_stop_p_gap
                if high > peak_p:
                    peak_p = high
                trail_stop_p = peak_p * (1.0 - trail_pct)
                if gap_exit_p:
                    if held_p >= min_hold_hours:
                        exit_px = op
                        pc = (exit_px - entry_price_p) / entry_price_p
                        entry_i_p[count_p] = entry_bar_p; exit_i_p[count_p] = i
                        entry_p_p[count_p] = entry_price_p; exit_p_p[count_p] = exit_px
                        hours_held_p[count_p] = held_p
                        results_p[count_p] = WIN if pc > 0 else LOSS; returns_p[count_p] = pc
                        count_p += 1; in_trade_p = False; trailing_p = False
                        last_exit_day_p = daily_idx[i]
                elif low <= trail_stop_p or held_p >= max_hours_to_hold:
                    if held_p >= min_hold_hours:
                        exit_px = trail_stop_p if low <= trail_stop_p else cp
                        pc = (exit_px - entry_price_p) / entry_price_p
                        entry_i_p[count_p] = entry_bar_p; exit_i_p[count_p] = i
                        entry_p_p[count_p] = entry_price_p; exit_p_p[count_p] = exit_px
                        hours_held_p[count_p] = held_p
                        results_p[count_p] = WIN if pc > 0 else LOSS; returns_p[count_p] = pc
                        count_p += 1; in_trade_p = False; trailing_p = False
                        last_exit_day_p = daily_idx[i]
            elif op <= stop_price_p:
                if held_p >= min_hold_hours:
                    pc = (op - entry_price_p) / entry_price_p
                    entry_i_p[count_p] = entry_bar_p; exit_i_p[count_p] = i
                    entry_p_p[count_p] = entry_price_p; exit_p_p[count_p] = op
                    hours_held_p[count_p] = held_p; results_p[count_p] = LOSS; returns_p[count_p] = pc
                    count_p += 1; in_trade_p = False
                    last_exit_day_p = daily_idx[i]
            elif low <= stop_price_p:
                if held_p >= min_hold_hours:
                    pc = (stop_price_p - entry_price_p) / entry_price_p
                    entry_i_p[count_p] = entry_bar_p; exit_i_p[count_p] = i
                    entry_p_p[count_p] = entry_price_p; exit_p_p[count_p] = stop_price_p
                    hours_held_p[count_p] = held_p; results_p[count_p] = LOSS; returns_p[count_p] = pc
                    count_p += 1; in_trade_p = False
                    last_exit_day_p = daily_idx[i]
            elif cp >= tp_price_p:
                trailing_p = True; peak_p = cp
            elif held_p >= max_hours_to_hold:
                if held_p >= min_hold_hours:
                    pc = (cp - entry_price_p) / entry_price_p
                    entry_i_p[count_p] = entry_bar_p; exit_i_p[count_p] = i
                    entry_p_p[count_p] = entry_price_p; exit_p_p[count_p] = cp
                    hours_held_p[count_p] = held_p
                    results_p[count_p] = TWIN if pc > 0 else TLOSS; returns_p[count_p] = pc
                    count_p += 1; in_trade_p = False
                    last_exit_day_p = daily_idx[i]
        elif waiting_p:
            wait_bars_p += 1
            # High checked against the trigger from running_low as of the PRIOR
            # bar only — never folds in this bar's own dip, unlike 'possible'.
            buy_trigger_p = running_low_p * (1.0 + trail_buy_pct)
            if op >= buy_trigger_p:
                entry_price_p = op
                tp_price_p    = entry_price_p * (1.0 + take_profit)
                stop_price_p  = entry_price_p * (1.0 - stop_loss)
                entry_bar_p   = i; held_p = 0
                in_trade_p = True; waiting_p = False; trailing_p = False
            elif high >= buy_trigger_p:
                entry_price_p = buy_trigger_p
                tp_price_p    = entry_price_p * (1.0 + take_profit)
                stop_price_p  = entry_price_p * (1.0 - stop_loss)
                entry_bar_p   = i; held_p = 0
                in_trade_p = True; waiting_p = False; trailing_p = False
            else:
                if low < running_low_p:
                    running_low_p = low
                if wait_bars_p >= max_hours_to_hold:
                    waiting_p = False
        else:
            h = hours[i]
            if h == target_h0 or h == target_h1:
                di = daily_idx[i]
                if di >= 0:
                    sma = sma_arr[di]; std = std_arr[di]
                    if std != 0.0:
                        lower_band = sma - std * z_thresh
                        blocked_p = same_day_block and di == last_exit_day_p
                        fired_p = False
                        if not blocked_p:
                            if open_check_entry_timing:
                                if has_trend:
                                    signal_open_p = (op <= lower_band) and (op > trend_arr[di])
                                else:
                                    signal_open_p = op <= lower_band
                                if signal_open_p:
                                    waiting_p = True; running_low_p = op; wait_bars_p = 0
                                    fired_p = True
                            if not fired_p:
                                if has_trend:
                                    signal_p = (cp <= lower_band) and (cp > trend_arr[di])
                                else:
                                    signal_p = cp <= lower_band
                                if signal_p:
                                    waiting_p = True; running_low_p = cp; wait_bars_p = 0

        # ── certain: only resolve a fill when provable regardless of ordering ──
        if in_trade_c:
            held_c += 1
            if trailing_c:
                trail_stop_c_gap = peak_c * (1.0 - trail_pct)
                gap_exit_c = op <= trail_stop_c_gap
                if high > peak_c:
                    peak_c = high
                trail_stop_c = peak_c * (1.0 - trail_pct)
                if gap_exit_c:
                    if held_c >= min_hold_hours:
                        exit_px = op
                        pc = (exit_px - entry_price_c) / entry_price_c
                        entry_i_c[count_c] = entry_bar_c; exit_i_c[count_c] = i
                        entry_p_c[count_c] = entry_price_c; exit_p_c[count_c] = exit_px
                        hours_held_c[count_c] = held_c
                        results_c[count_c] = WIN if pc > 0 else LOSS; returns_c[count_c] = pc
                        count_c += 1; in_trade_c = False; trailing_c = False
                        last_exit_day_c = daily_idx[i]
                elif low <= trail_stop_c or held_c >= max_hours_to_hold:
                    if held_c >= min_hold_hours:
                        exit_px = trail_stop_c if low <= trail_stop_c else cp
                        pc = (exit_px - entry_price_c) / entry_price_c
                        entry_i_c[count_c] = entry_bar_c; exit_i_c[count_c] = i
                        entry_p_c[count_c] = entry_price_c; exit_p_c[count_c] = exit_px
                        hours_held_c[count_c] = held_c
                        results_c[count_c] = WIN if pc > 0 else LOSS; returns_c[count_c] = pc
                        count_c += 1; in_trade_c = False; trailing_c = False
                        last_exit_day_c = daily_idx[i]
            elif op <= stop_price_c:
                if held_c >= min_hold_hours:
                    pc = (op - entry_price_c) / entry_price_c
                    entry_i_c[count_c] = entry_bar_c; exit_i_c[count_c] = i
                    entry_p_c[count_c] = entry_price_c; exit_p_c[count_c] = op
                    hours_held_c[count_c] = held_c; results_c[count_c] = LOSS; returns_c[count_c] = pc
                    count_c += 1; in_trade_c = False
                    last_exit_day_c = daily_idx[i]
            elif low <= stop_price_c:
                if held_c >= min_hold_hours:
                    pc = (stop_price_c - entry_price_c) / entry_price_c
                    entry_i_c[count_c] = entry_bar_c; exit_i_c[count_c] = i
                    entry_p_c[count_c] = entry_price_c; exit_p_c[count_c] = stop_price_c
                    hours_held_c[count_c] = held_c; results_c[count_c] = LOSS; returns_c[count_c] = pc
                    count_c += 1; in_trade_c = False
                    last_exit_day_c = daily_idx[i]
            elif cp >= tp_price_c:
                trailing_c = True; peak_c = cp
            elif held_c >= max_hours_to_hold:
                if held_c >= min_hold_hours:
                    pc = (cp - entry_price_c) / entry_price_c
                    entry_i_c[count_c] = entry_bar_c; exit_i_c[count_c] = i
                    entry_p_c[count_c] = entry_price_c; exit_p_c[count_c] = cp
                    hours_held_c[count_c] = held_c
                    results_c[count_c] = TWIN if pc > 0 else TLOSS; returns_c[count_c] = pc
                    count_c += 1; in_trade_c = False
                    last_exit_day_c = daily_idx[i]
        elif waiting_c:
            wait_bars_c += 1
            buy_trigger_prior = running_low_c * (1.0 + trail_buy_pct)
            if op >= buy_trigger_prior:
                entry_price_c = op
                tp_price_c    = entry_price_c * (1.0 + take_profit)
                stop_price_c  = entry_price_c * (1.0 - stop_loss)
                entry_bar_c   = i; held_c = 0
                in_trade_c = True; waiting_c = False; trailing_c = False
            else:
                updated_low_c = low if low < running_low_c else running_low_c
                if updated_low_c == running_low_c and high >= buy_trigger_prior:
                    # this bar's own low never moved the trigger, so it was
                    # frozen for the entire bar -- a High-touch here is
                    # certain regardless of intrabar ordering, same
                    # determinism as the Open-gap case above (found
                    # 2026-08-09: the Close-only check below was silently
                    # missing this deterministic case and deferring days
                    # past a real, provable fill).
                    entry_price_c = buy_trigger_prior
                    tp_price_c    = entry_price_c * (1.0 + take_profit)
                    stop_price_c  = entry_price_c * (1.0 - stop_loss)
                    entry_bar_c   = i; held_c = 0
                    in_trade_c = True; waiting_c = False; trailing_c = False
                else:
                    buy_trigger_updated = updated_low_c * (1.0 + trail_buy_pct)
                    if cp >= buy_trigger_updated:
                        # Close-confirmed fill: credit min(buy_trigger_prior, high),
                        # the true provable worst-case fill price over BOTH
                        # intrabar orderings -- not cp (2026-08-09 first-pass fix,
                        # found by paired review to still be an unproven guess:
                        # cp has no proven relation to the real fill, it's just
                        # provably >= the old buggy credit). Proof: if
                        # high >= buy_trigger_prior, the trigger is at most
                        # buy_trigger_prior for the whole bar (attained by a
                        # high-first path), so fill <= buy_trigger_prior. If
                        # high < buy_trigger_prior, the fill can't exceed the
                        # bar's own high (price must reach the trigger to cross
                        # it), so fill <= high, and high >= low*(1+trail_buy_pct)
                        # is guaranteed here since high >= cp >= buy_trigger_updated.
                        # Both bounds are tight (2026-08-09, paired Opus review +
                        # isolation test, see docs/research_log.md). This makes
                        # certain's entry a genuine worst-case-PRICE bound for
                        # this branch, not just a worst-case-FILL proof --
                        # deliberately different from the frozen-trigger case
                        # above, which can credit a price BETTER than possible.
                        entry_price_c = buy_trigger_prior if buy_trigger_prior < high else high
                        tp_price_c    = entry_price_c * (1.0 + take_profit)
                        stop_price_c  = entry_price_c * (1.0 - stop_loss)
                        entry_bar_c   = i; held_c = 0
                        in_trade_c = True; waiting_c = False; trailing_c = False
                    else:
                        running_low_c = updated_low_c
                        if wait_bars_c >= max_hours_to_hold:
                            waiting_c = False
        else:
            h = hours[i]
            if h == target_h0 or h == target_h1:
                di = daily_idx[i]
                if di >= 0:
                    sma = sma_arr[di]; std = std_arr[di]
                    if std != 0.0:
                        lower_band = sma - std * z_thresh
                        blocked_c = same_day_block and di == last_exit_day_c
                        fired_c = False
                        if not blocked_c:
                            if open_check_entry_timing:
                                if has_trend:
                                    signal_open_c = (op <= lower_band) and (op > trend_arr[di])
                                else:
                                    signal_open_c = op <= lower_band
                                if signal_open_c:
                                    waiting_c = True; running_low_c = op; wait_bars_c = 0
                                    fired_c = True
                            if not fired_c:
                                if has_trend:
                                    signal_c = (cp <= lower_band) and (cp > trend_arr[di])
                                else:
                                    signal_c = cp <= lower_band
                                if signal_c:
                                    waiting_c = True; running_low_c = cp; wait_bars_c = 0

    if in_trade:
        cp = prices[n - 1]
        pc = (cp - entry_price) / entry_price
        entry_i[count] = entry_bar; exit_i[count] = n - 1
        entry_p[count] = entry_price; exit_p[count] = cp
        hours_held[count] = held; results[count] = OPEN; returns[count] = pc
        count += 1

    if in_trade_p:
        cp = prices[n - 1]
        pc = (cp - entry_price_p) / entry_price_p
        entry_i_p[count_p] = entry_bar_p; exit_i_p[count_p] = n - 1
        entry_p_p[count_p] = entry_price_p; exit_p_p[count_p] = cp
        hours_held_p[count_p] = held_p; results_p[count_p] = OPEN; returns_p[count_p] = pc
        count_p += 1

    if in_trade_c:
        cp = prices[n - 1]
        pc = (cp - entry_price_c) / entry_price_c
        entry_i_c[count_c] = entry_bar_c; exit_i_c[count_c] = n - 1
        entry_p_c[count_c] = entry_price_c; exit_p_c[count_c] = cp
        hours_held_c[count_c] = held_c; results_c[count_c] = OPEN; returns_c[count_c] = pc
        count_c += 1

    return (entry_i[:count], exit_i[:count], entry_p[:count], exit_p[:count],
            hours_held[:count], results[:count], returns[:count],
            entry_i_p[:count_p], exit_i_p[:count_p], entry_p_p[:count_p], exit_p_p[:count_p],
            hours_held_p[:count_p], results_p[:count_p], returns_p[:count_p],
            entry_i_c[:count_c], exit_i_c[:count_c], entry_p_c[:count_c], exit_p_c[:count_c],
            hours_held_c[:count_c], results_c[:count_c], returns_c[:count_c])


@njit(cache=True)
def _simulate_certain_only(prices, highs, lows, hours, daily_idx, sma_arr, std_arr, trend_arr, has_trend,
                            take_profit, stop_loss, max_hours_to_hold, trail_buy_pct, trail_pct, target_h0, target_h1, z_thresh,
                            opens, open_check_entry_timing, same_day_block=False):
    """Standalone 'certain' resolution only -- same corrected logic as the
    certain branch of _simulate_trail_both (frozen-trigger case + real-Close
    crediting, fixed 2026-08-09), but without possible/pessimistic's parallel
    state machines. Built specifically so recomputing certain's stored
    backtest_cache columns after the 2026-08-09 fix doesn't have to pay for
    re-deriving possible/pessimistic too, which are unaffected by the bug and
    don't need to change. Not used by the live sweep engine (which still
    wants all three in one pass via _simulate_trail_both) -- this is for the
    dedicated recompute pass only. Keep in sync with _simulate_trail_both's
    certain branch if either changes. Does NOT support min_hold_hours
    (2026-08-17 addition to _simulate_trail_both) -- deliberately not
    threaded through, since this function is only for recomputing stored
    certain-resolution backtest_cache columns, which never used the floor.
    If min_hold_hours is ever wired into a recompute path that touches this
    function, it must gain the same param or it will silently emit
    non-floor-respecting certain columns."""
    entry_i    = np.empty(MAX_TRADES, dtype=np.int64)
    exit_i     = np.empty(MAX_TRADES, dtype=np.int64)
    entry_p    = np.empty(MAX_TRADES, dtype=np.float64)
    exit_p     = np.empty(MAX_TRADES, dtype=np.float64)
    hours_held = np.empty(MAX_TRADES, dtype=np.int64)
    results    = np.empty(MAX_TRADES, dtype=np.int64)
    returns    = np.empty(MAX_TRADES, dtype=np.float64)
    count      = 0

    in_trade     = False
    waiting      = False
    trailing     = False
    entry_price  = 0.0
    stop_price   = 0.0
    tp_price     = 0.0
    peak         = 0.0
    entry_bar    = 0
    held         = 0
    running_low  = 0.0
    wait_bars    = 0
    last_exit_day = -1

    n = len(prices)
    for i in range(n):
        cp   = prices[i]
        op   = opens[i]
        high = highs[i]
        low  = lows[i]

        if in_trade:
            held += 1
            if trailing:
                trail_stop_gap = peak * (1.0 - trail_pct)
                if op <= trail_stop_gap:
                    exit_px = op
                    pc = (exit_px - entry_price) / entry_price
                    entry_i[count] = entry_bar; exit_i[count] = i
                    entry_p[count] = entry_price; exit_p[count] = exit_px
                    hours_held[count] = held
                    results[count] = WIN if pc > 0 else LOSS; returns[count] = pc
                    count += 1; in_trade = False; trailing = False
                    last_exit_day = daily_idx[i]
                else:
                    if high > peak:
                        peak = high
                    trail_stop = peak * (1.0 - trail_pct)
                    if low <= trail_stop or held >= max_hours_to_hold:
                        exit_px = trail_stop if low <= trail_stop else cp
                        pc = (exit_px - entry_price) / entry_price
                        entry_i[count] = entry_bar; exit_i[count] = i
                        entry_p[count] = entry_price; exit_p[count] = exit_px
                        hours_held[count] = held
                        results[count] = WIN if pc > 0 else LOSS; returns[count] = pc
                        count += 1; in_trade = False; trailing = False
                        last_exit_day = daily_idx[i]
            elif op <= stop_price:
                pc = (op - entry_price) / entry_price
                entry_i[count] = entry_bar; exit_i[count] = i
                entry_p[count] = entry_price; exit_p[count] = op
                hours_held[count] = held; results[count] = LOSS; returns[count] = pc
                count += 1; in_trade = False
                last_exit_day = daily_idx[i]
            elif low <= stop_price:
                pc = (stop_price - entry_price) / entry_price
                entry_i[count] = entry_bar; exit_i[count] = i
                entry_p[count] = entry_price; exit_p[count] = stop_price
                hours_held[count] = held; results[count] = LOSS; returns[count] = pc
                count += 1; in_trade = False
                last_exit_day = daily_idx[i]
            elif cp >= tp_price:
                trailing = True; peak = cp
            elif held >= max_hours_to_hold:
                pc = (cp - entry_price) / entry_price
                entry_i[count] = entry_bar; exit_i[count] = i
                entry_p[count] = entry_price; exit_p[count] = cp
                hours_held[count] = held
                results[count] = TWIN if pc > 0 else TLOSS; returns[count] = pc
                count += 1; in_trade = False
                last_exit_day = daily_idx[i]
        elif waiting:
            wait_bars += 1
            buy_trigger_prior = running_low * (1.0 + trail_buy_pct)
            if op >= buy_trigger_prior:
                entry_price = op
                tp_price    = entry_price * (1.0 + take_profit)
                stop_price  = entry_price * (1.0 - stop_loss)
                entry_bar   = i; held = 0
                in_trade = True; waiting = False; trailing = False
            else:
                updated_low = low if low < running_low else running_low
                if updated_low == running_low and high >= buy_trigger_prior:
                    entry_price = buy_trigger_prior
                    tp_price    = entry_price * (1.0 + take_profit)
                    stop_price  = entry_price * (1.0 - stop_loss)
                    entry_bar   = i; held = 0
                    in_trade = True; waiting = False; trailing = False
                else:
                    buy_trigger_updated = updated_low * (1.0 + trail_buy_pct)
                    if cp >= buy_trigger_updated:
                        # min(buy_trigger_prior, high) -- see _simulate_trail_both's
                        # matching branch for the full proof; must stay in sync.
                        entry_price = buy_trigger_prior if buy_trigger_prior < high else high
                        tp_price    = entry_price * (1.0 + take_profit)
                        stop_price  = entry_price * (1.0 - stop_loss)
                        entry_bar   = i; held = 0
                        in_trade = True; waiting = False; trailing = False
                    else:
                        running_low = updated_low
                        if wait_bars >= max_hours_to_hold:
                            waiting = False
        else:
            h = hours[i]
            if h == target_h0 or h == target_h1:
                di = daily_idx[i]
                if di >= 0:
                    sma = sma_arr[di]; std = std_arr[di]
                    if std != 0.0:
                        lower_band = sma - std * z_thresh
                        blocked = same_day_block and di == last_exit_day
                        fired = False
                        if not blocked:
                            if open_check_entry_timing:
                                if has_trend:
                                    signal_open = (op <= lower_band) and (op > trend_arr[di])
                                else:
                                    signal_open = op <= lower_band
                                if signal_open:
                                    waiting = True; running_low = op; wait_bars = 0
                                    fired = True
                            if not fired:
                                if has_trend:
                                    signal = (cp <= lower_band) and (cp > trend_arr[di])
                                else:
                                    signal = cp <= lower_band
                                if signal:
                                    waiting = True; running_low = cp; wait_bars = 0

    if in_trade:
        cp = prices[n - 1]
        pc = (cp - entry_price) / entry_price
        entry_i[count] = entry_bar; exit_i[count] = n - 1
        entry_p[count] = entry_price; exit_p[count] = cp
        hours_held[count] = held; results[count] = OPEN; returns[count] = pc
        count += 1

    return (entry_i[:count], exit_i[:count], entry_p[:count], exit_p[:count],
            hours_held[:count], results[:count], returns[:count])


def run_backtest_v19(df_hourly, df_daily_indicators, ticker,
                     mode="BACKTEST", target_hours=(9, 14),
                     take_profit=0.05, stop_loss=0.15, max_hours_to_hold=28,
                     z_score_threshold=2.0, trail_buy_pct=0.02, prep=None):
    p = prep if prep is not None else prep_inputs(df_hourly, df_daily_indicators)
    target_h0, target_h1 = int(target_hours[0]), int(target_hours[1])

    ei, xi, ep, xp, held, res, ret = _simulate_trail_buy(
        p['prices'], p['highs'], p['lows'], p['opens'], p['hours'], p['daily_idx'],
        p['sma_arr'], p['std_arr'], p['trend_arr'], p['has_trend'],
        float(take_profit), float(stop_loss), int(max_hours_to_hold), float(trail_buy_pct),
        target_h0, target_h1, float(z_score_threshold)
    )
    return _build_trades(ticker, p['timestamps'], ei, xi, ep, xp, held, res, ret)


def run_backtest_v110(df_hourly, df_daily_indicators, ticker,
                      mode="BACKTEST", target_hours=(9, 14),
                      take_profit=0.05, stop_loss=0.15, max_hours_to_hold=28,
                      z_score_threshold=2.0, trail_buy_pct=0.02, trail_pct=0.03,
                      entry_timing='close', return_bounds=False, prep=None,
                      same_day_block=False, min_hold_hours=0):
    """min_hold_hours: research/backtest-only compliance-hold floor, see
    _simulate_trail_both's docstring. Default 0 reproduces prior behavior
    exactly — no live-automation path sets this to anything else."""
    p = prep if prep is not None else prep_inputs(df_hourly, df_daily_indicators)
    target_h0, target_h1 = int(target_hours[0]), int(target_hours[1])

    (ei, xi, ep, xp, held, res, ret,
     ei_p, xi_p, ep_p, xp_p, held_p, res_p, ret_p,
     ei_c, xi_c, ep_c, xp_c, held_c, res_c, ret_c) = _simulate_trail_both(
        p['prices'], p['highs'], p['lows'], p['hours'], p['daily_idx'],
        p['sma_arr'], p['std_arr'], p['trend_arr'], p['has_trend'],
        float(take_profit), float(stop_loss), int(max_hours_to_hold),
        float(trail_buy_pct), float(trail_pct),
        target_h0, target_h1, float(z_score_threshold),
        p['opens'], entry_timing == 'open_check', bool(same_day_block),
        int(min_hold_hours)
    )
    trades = _build_trades(ticker, p['timestamps'], ei, xi, ep, xp, held, res, ret)
    if return_bounds:
        trades_pessimistic = _build_trades(ticker, p['timestamps'], ei_p, xi_p, ep_p, xp_p, held_p, res_p, ret_p)
        trades_certain = _build_trades(ticker, p['timestamps'], ei_c, xi_c, ep_c, xp_c, held_c, res_c, ret_c)
        return trades, trades_pessimistic, trades_certain
    return trades


def run_backtest_certain_only(df_hourly, df_daily_indicators, ticker,
                               mode="BACKTEST", target_hours=(9, 14),
                               take_profit=0.05, stop_loss=0.15, max_hours_to_hold=28,
                               z_score_threshold=2.0, trail_buy_pct=0.02, trail_pct=0.03,
                               entry_timing='close', prep=None, same_day_block=False):
    """Certain resolution only, via the lean _simulate_certain_only kernel --
    for the backtest_cache certain-column recompute pass (see
    docs/backlog_cache.md's 2026-08-09 certain-fix entry), not the live sweep
    engine (which still wants all three resolutions from run_backtest_v110/
    _simulate_trail_both in one pass)."""
    p = prep if prep is not None else prep_inputs(df_hourly, df_daily_indicators)
    target_h0, target_h1 = int(target_hours[0]), int(target_hours[1])

    ei, xi, ep, xp, held, res, ret = _simulate_certain_only(
        p['prices'], p['highs'], p['lows'], p['hours'], p['daily_idx'],
        p['sma_arr'], p['std_arr'], p['trend_arr'], p['has_trend'],
        float(take_profit), float(stop_loss), int(max_hours_to_hold),
        float(trail_buy_pct), float(trail_pct),
        target_h0, target_h1, float(z_score_threshold),
        p['opens'], entry_timing == 'open_check', bool(same_day_block)
    )
    return _build_trades(ticker, p['timestamps'], ei, xi, ep, xp, held, res, ret)


@njit(cache=True)
def _simulate_limit_trail(prices, highs, lows, hours, daily_idx, sma_arr, std_arr, trend_arr, has_trend,
                          take_profit, stop_loss, max_hours_to_hold, trail_pct, target_h0, target_h1, z_thresh):
    entry_i    = np.empty(MAX_TRADES, dtype=np.int64)
    exit_i     = np.empty(MAX_TRADES, dtype=np.int64)
    entry_p    = np.empty(MAX_TRADES, dtype=np.float64)
    exit_p     = np.empty(MAX_TRADES, dtype=np.float64)
    hours_held = np.empty(MAX_TRADES, dtype=np.int64)
    results    = np.empty(MAX_TRADES, dtype=np.int64)
    returns    = np.empty(MAX_TRADES, dtype=np.float64)
    count      = 0

    in_trade    = False
    trailing    = False
    entry_price = 0.0
    stop_price  = 0.0
    tp_price    = 0.0
    peak        = 0.0
    entry_bar   = 0
    held        = 0

    n = len(prices)
    for i in range(n):
        cp   = prices[i]
        high = highs[i]
        low  = lows[i]

        if in_trade:
            held += 1

            if trailing:
                if high > peak:
                    peak = high
                trail_stop = peak * (1.0 - trail_pct)
                if low <= trail_stop or held >= max_hours_to_hold:
                    exit_px = trail_stop if low <= trail_stop else cp
                    pc = (exit_px - entry_price) / entry_price
                    entry_i[count]    = entry_bar
                    exit_i[count]     = i
                    entry_p[count]    = entry_price
                    exit_p[count]     = exit_px
                    hours_held[count] = held
                    results[count]    = WIN if pc > 0 else LOSS
                    returns[count]    = pc
                    count += 1
                    in_trade = False
                    trailing = False
                continue

            # SL first: stop order triggers intrabar (band-anchored, like _simulate_limit)
            if low <= stop_price:
                pc = (stop_price - entry_price) / entry_price
                entry_i[count]    = entry_bar
                exit_i[count]     = i
                entry_p[count]    = entry_price
                exit_p[count]     = stop_price
                hours_held[count] = held
                results[count]    = LOSS
                returns[count]    = pc
                count += 1
                in_trade = False
                continue

            # TP activation — switch to trailing mode
            if cp >= tp_price:
                trailing = True
                peak     = cp
                continue

            if held >= max_hours_to_hold:
                pc = (cp - entry_price) / entry_price
                entry_i[count]    = entry_bar
                exit_i[count]     = i
                entry_p[count]    = entry_price
                exit_p[count]     = cp
                hours_held[count] = held
                results[count]    = TWIN if pc > 0 else TLOSS
                returns[count]    = pc
                count += 1
                in_trade = False
                continue

            continue

        h = hours[i]
        if h != target_h0 and h != target_h1:
            continue

        di = daily_idx[i]
        if di < 0:
            continue

        sma = sma_arr[di]
        std = std_arr[di]
        if std == 0.0:
            continue

        lower_band = sma - std * z_thresh

        if has_trend:
            trend = trend_arr[di]
            signal = (low <= lower_band) and (cp > trend)
        else:
            signal = low <= lower_band

        if signal:
            in_trade    = True
            trailing    = False
            entry_price = lower_band
            tp_price    = lower_band * (1.0 + take_profit)
            stop_price  = lower_band * (1.0 - stop_loss)
            entry_bar   = i
            held        = 0

    if in_trade:
        cp = prices[n - 1]
        pc = (cp - entry_price) / entry_price
        entry_i[count]    = entry_bar
        exit_i[count]     = n - 1
        entry_p[count]    = entry_price
        exit_p[count]     = cp
        hours_held[count] = held
        results[count]    = OPEN
        returns[count]    = pc
        count += 1

    return entry_i[:count], exit_i[:count], entry_p[:count], exit_p[:count], hours_held[:count], results[:count], returns[:count]


def run_backtest_v211(df_hourly, df_daily_indicators, ticker,
                      mode="BACKTEST", target_hours=(9, 14),
                      take_profit=0.05, stop_loss=0.15, max_hours_to_hold=28,
                      z_score_threshold=2.0, trail_pct=0.03, prep=None):
    p = prep if prep is not None else prep_inputs(df_hourly, df_daily_indicators)
    target_h0, target_h1 = int(target_hours[0]), int(target_hours[1])

    ei, xi, ep, xp, held, res, ret = _simulate_limit_trail(
        p['prices'], p['highs'], p['lows'], p['hours'], p['daily_idx'],
        p['sma_arr'], p['std_arr'], p['trend_arr'], p['has_trend'],
        float(take_profit), float(stop_loss), int(max_hours_to_hold), float(trail_pct),
        target_h0, target_h1, float(z_score_threshold)
    )
    return _build_trades(ticker, p['timestamps'], ei, xi, ep, xp, held, res, ret)


@njit(cache=True)
def _simulate_close_limitexit(prices, highs, lows, hours, daily_idx, sma_arr, std_arr, trend_arr, has_trend,
                              take_profit, stop_loss, max_hours_to_hold, target_h0, target_h1, z_thresh):
    """v2.12: bar-close confirmed entry (like _simulate). SL is intrabar (Low vs stop_price,
    fixed floor). TP is a resting limit order — fills intrabar the moment High touches tp_price,
    at tp_price (guaranteed, no waiting for bar-close). TIME is bar-close."""
    entry_i    = np.empty(MAX_TRADES, dtype=np.int64)
    exit_i     = np.empty(MAX_TRADES, dtype=np.int64)
    entry_p    = np.empty(MAX_TRADES, dtype=np.float64)
    exit_p     = np.empty(MAX_TRADES, dtype=np.float64)
    hours_held = np.empty(MAX_TRADES, dtype=np.int64)
    results    = np.empty(MAX_TRADES, dtype=np.int64)
    returns    = np.empty(MAX_TRADES, dtype=np.float64)
    count      = 0

    in_trade    = False
    entry_price = 0.0
    tp_price    = 0.0
    stop_price  = 0.0
    entry_bar   = 0
    held        = 0

    n = len(prices)
    for i in range(n):
        cp   = prices[i]
        high = highs[i]
        low  = lows[i]

        if in_trade:
            held += 1

            # SL first: stop order triggers intrabar
            if low <= stop_price:
                pc = (stop_price - entry_price) / entry_price
                entry_i[count]    = entry_bar
                exit_i[count]     = i
                entry_p[count]    = entry_price
                exit_p[count]     = stop_price
                hours_held[count] = held
                results[count]    = LOSS
                returns[count]    = pc
                count += 1
                in_trade = False
                continue

            # TP: resting limit order, fills intrabar at tp_price
            if high >= tp_price:
                pc = (tp_price - entry_price) / entry_price
                entry_i[count]    = entry_bar
                exit_i[count]     = i
                entry_p[count]    = entry_price
                exit_p[count]     = tp_price
                hours_held[count] = held
                results[count]    = WIN
                returns[count]    = pc
                count += 1
                in_trade = False
                continue

            if held >= max_hours_to_hold:
                pc = (cp - entry_price) / entry_price
                entry_i[count]    = entry_bar
                exit_i[count]     = i
                entry_p[count]    = entry_price
                exit_p[count]     = cp
                hours_held[count] = held
                results[count]    = TWIN if pc > 0 else TLOSS
                returns[count]    = pc
                count += 1
                in_trade = False
                continue

            continue

        h = hours[i]
        if h != target_h0 and h != target_h1:
            continue

        di = daily_idx[i]
        if di < 0:
            continue

        sma = sma_arr[di]
        std = std_arr[di]
        if std == 0.0:
            continue

        lower_band = sma - std * z_thresh

        if has_trend:
            trend = trend_arr[di]
            signal = (cp <= lower_band) and (cp > trend)
        else:
            signal = cp <= lower_band

        if signal:
            in_trade    = True
            entry_price = cp
            tp_price    = cp * (1.0 + take_profit)
            stop_price  = cp * (1.0 - stop_loss)
            entry_bar   = i
            held        = 0

    if in_trade:
        cp = prices[n - 1]
        pc = (cp - entry_price) / entry_price
        entry_i[count]    = entry_bar
        exit_i[count]     = n - 1
        entry_p[count]    = entry_price
        exit_p[count]     = cp
        hours_held[count] = held
        results[count]    = OPEN
        returns[count]    = pc
        count += 1

    return entry_i[:count], exit_i[:count], entry_p[:count], exit_p[:count], hours_held[:count], results[:count], returns[:count]


def run_backtest_v212(df_hourly, df_daily_indicators, ticker,
                      mode="BACKTEST", target_hours=(9, 14),
                      take_profit=0.05, stop_loss=0.15, max_hours_to_hold=28,
                      z_score_threshold=2.0, prep=None):
    p = prep if prep is not None else prep_inputs(df_hourly, df_daily_indicators)
    target_h0, target_h1 = int(target_hours[0]), int(target_hours[1])

    ei, xi, ep, xp, held, res, ret = _simulate_close_limitexit(
        p['prices'], p['highs'], p['lows'], p['hours'], p['daily_idx'],
        p['sma_arr'], p['std_arr'], p['trend_arr'], p['has_trend'],
        float(take_profit), float(stop_loss), int(max_hours_to_hold),
        target_h0, target_h1, float(z_score_threshold)
    )
    return _build_trades(ticker, p['timestamps'], ei, xi, ep, xp, held, res, ret)


def run_backtest(df_hourly, df_daily_indicators, ticker,
                 mode="BACKTEST", target_hours=(9, 14),
                 take_profit=0.05, stop_loss=0.15, max_hours_to_hold=28, z_score_threshold=2.0,
                 prep=None):
    p = prep if prep is not None else prep_inputs(df_hourly, df_daily_indicators)
    target_h0, target_h1 = int(target_hours[0]), int(target_hours[1])

    ei, xi, ep, xp, held, res, ret = _simulate(
        p['prices'], p['hours'], p['daily_idx'],
        p['sma_arr'], p['std_arr'], p['trend_arr'], p['has_trend'],
        float(take_profit), float(stop_loss), int(max_hours_to_hold),
        target_h0, target_h1, float(z_score_threshold)
    )
    return _build_trades(ticker, p['timestamps'], ei, xi, ep, xp, held, res, ret)


def run_backtest_dispatch(strategy_class, df_hourly, df_daily_indicators, ticker,
                          take_profit, sl_raw, max_hours_to_hold, z_score_threshold,
                          fixed_sl=0.0, trail_pct_pct=0.0, entry_timing='close',
                          return_bounds=False, prep=None, min_hold_hours=0):
    """Strategy-aware dispatch to the correct kernel wrapper — single source of truth
    for what a raw swept 'sl_raw' grid value (plus the fixed_sl/trail_pct_pct config
    values) actually mean for a given strategy. Mirrors
    run_optimization_sweep.py::run_single_backtest_node_isolated's branches so the
    sweep engine and any UI page replaying a node can't drift apart again — see
    docs/design.md 'Grid axis meaning by strategy'.
    take_profit/sl_raw/fixed_sl/trail_pct_pct are percent-scale (e.g. 15, not 0.15).
    min_hold_hours: research/backtest-only compliance-hold floor (see
    _simulate_trail_both's docstring) -- only TrailingBothZScoreBreakout's kernel
    (run_backtest_v110) supports it. A nonzero value for any other strategy is a
    caller error, not silently ignored -- raises ValueError rather than quietly
    running without the floor a caller explicitly asked for."""
    import strategies as _strategies
    tp   = float(take_profit) / 100.0
    hold = int(max_hours_to_hold)
    z    = float(z_score_threshold)

    if int(min_hold_hours) != 0 and not issubclass(strategy_class, _strategies.TrailingBothZScoreBreakout):
        raise ValueError(
            f"run_backtest_dispatch: min_hold_hours={min_hold_hours} was requested for "
            f"{strategy_class.__name__}, but only TrailingBothZScoreBreakout's kernel "
            f"(run_backtest_v110) supports the compliance-hold floor. Refusing to silently "
            f"run without it."
        )

    if issubclass(strategy_class, _strategies.TrailingBothZScoreBreakout):
        return run_backtest_v110(df_hourly, df_daily_indicators, ticker,
            take_profit=tp, stop_loss=float(fixed_sl) / 100.0, max_hours_to_hold=hold,
            min_hold_hours=int(min_hold_hours),
            z_score_threshold=z, trail_buy_pct=float(sl_raw) / 100.0,
            trail_pct=float(trail_pct_pct) / 100.0, entry_timing=entry_timing,
            return_bounds=return_bounds, prep=prep)
    if issubclass(strategy_class, _strategies.TrailingBuyZScoreBreakout):
        return run_backtest_v19(df_hourly, df_daily_indicators, ticker,
            take_profit=tp, stop_loss=float(fixed_sl) / 100.0, max_hours_to_hold=hold,
            z_score_threshold=z, trail_buy_pct=float(sl_raw) / 100.0, prep=prep)
    if issubclass(strategy_class, _strategies.TrailingExitZScoreBreakout):
        return run_backtest_v18(df_hourly, df_daily_indicators, ticker,
            take_profit=tp, stop_loss=float(fixed_sl) / 100.0, max_hours_to_hold=hold,
            z_score_threshold=z, trail_pct=float(sl_raw) / 100.0, entry_timing=entry_timing, prep=prep)
    if issubclass(strategy_class, _strategies.LimitOrderTrailingExit):
        return run_backtest_v211(df_hourly, df_daily_indicators, ticker,
            take_profit=tp, stop_loss=float(fixed_sl) / 100.0, max_hours_to_hold=hold,
            z_score_threshold=z, trail_pct=float(sl_raw) / 100.0, prep=prep)
    if issubclass(strategy_class, _strategies.LimitOrderZScoreBreakout):
        return run_backtest_v17(df_hourly, df_daily_indicators, ticker,
            take_profit=tp, stop_loss=float(sl_raw) / 100.0, max_hours_to_hold=hold,
            z_score_threshold=z, prep=prep)
    if issubclass(strategy_class, _strategies.LimitExitZScoreBreakout):
        return run_backtest_v212(df_hourly, df_daily_indicators, ticker,
            take_profit=tp, stop_loss=float(sl_raw) / 100.0, max_hours_to_hold=hold,
            z_score_threshold=z, prep=prep)
    return run_backtest(df_hourly, df_daily_indicators, ticker,
        take_profit=tp, stop_loss=float(sl_raw) / 100.0, max_hours_to_hold=hold,
        z_score_threshold=z, prep=prep)


# ═══════════════════════ Ground-truth (v6) minute-resolution kernel ═══════════════════════
# Added 2026-08-21 per docs/plans/ground_truth_kernel_rebuild.md, Step 2. Ports the
# validated logic from scripts/sim_minute_groundtruth_independent.py (NOT redesigned —
# see that module's docstring for the full reasoning on why each choice was made:
# entry/exit are resolved on REAL 1-minute bars instead of guessing intrabar Low-vs-High
# ordering on hourly bars; signal DETECTION and arm/TP/TIME checks stay bar-close-gated,
# matching live's actual `at_bar_close` behavior). NOT a redesign — every behavior choice
# here mirrors the Python prototype line-for-line; do not "improve" anything here without
# updating the prototype first and re-running the parity gate (2a).
#
# Scope: TrailingBothZScoreBreakout (is_both=True) and TrailingExitZScoreBreakout
# (is_both=False) only — the two live-default strategies, per the plan. No trend filter
# (the validated prototype doesn't implement one either — matching it exactly is the
# parity bar, not a scope gap to "fix" here).
#
# Minute data is passed as flat float64 arrays (min_o/min_h/min_l/min_c) plus, per hourly
# bar, an (offset, count) pair into those arrays (bar_min_start/bar_min_count) — numba
# njit cannot take a dict-of-DataFrames the way the Python prototype's `m_by_bar` does, so
# prep_minute_inputs() below does that grouping once in plain Python and hands the kernel
# flat arrays, mirroring how prep_inputs() already does this for the hourly kernels.
#
# Exit reason codes (distinct from the WIN/LOSS/TWIN/TLOSS module-level codes above, which
# encode profitability, not mechanism): this kernel returns the actual exit REASON since
# that's what the parity gate checks trade-by-trade against the prototype's Trade.reason.
GT_SL    = 0
GT_TRAIL = 1
GT_TIME  = 2

# Minute-offset sentinels for the two "no ambiguity" fill cases the prototype uses (bar-
# close fill has no minute to point at; open_check market-buy fills at the bar's own Open,
# which is provably the bar's first minute — see the prototype's _open() comment).
GT_MJ_BAR_CLOSE = -2
GT_MJ_BAR_OPEN_FIRST_MINUTE = -3


def prep_minute_inputs(minute_df, df_hourly):
    """Groups a ticker's real 1-minute bars by the hourly bar that owns them (bar H:30
    owns [H:30, H+1:30)), matching sim_minute_groundtruth_independent.py's own bucketing
    exactly. Returns flat float64 minute OHLC arrays plus, per df_hourly row, the
    (start_offset, count) slice into those arrays — 0-count for hours with no real minute
    prints. minute_df must already be regular-session-filtered (09:30-16:00 ET), tz-naive,
    sorted, with Open/High/Low/Close columns (see load_minutes() in the prototype)."""
    idx = df_hourly.index
    n = len(idx)
    if len(minute_df) == 0:
        empty = np.zeros(0, dtype=np.float64)
        return dict(min_o=empty, min_h=empty, min_l=empty, min_c=empty,
                    bar_min_start=np.zeros(n, dtype=np.int64),
                    bar_min_count=np.zeros(n, dtype=np.int64),
                    bar_min_ts=np.zeros(0, dtype='datetime64[ns]'))

    mi = minute_df.index
    bucket = pd.DatetimeIndex(np.where(mi.minute >= 30, mi.floor("h") + pd.Timedelta(minutes=30),
                                       mi.floor("h") - pd.Timedelta(minutes=30)))
    grouped = {k: v for k, v in minute_df.groupby(bucket)}

    min_o_parts, min_h_parts, min_l_parts, min_c_parts, ts_parts = [], [], [], [], []
    bar_min_start = np.zeros(n, dtype=np.int64)
    bar_min_count = np.zeros(n, dtype=np.int64)
    offset = 0
    for i, t0 in enumerate(idx):
        g = grouped.get(t0)
        bar_min_start[i] = offset
        if g is None or len(g) == 0:
            bar_min_count[i] = 0
            continue
        bar_min_count[i] = len(g)
        min_o_parts.append(g["Open"].to_numpy(np.float64))
        min_h_parts.append(g["High"].to_numpy(np.float64))
        min_l_parts.append(g["Low"].to_numpy(np.float64))
        min_c_parts.append(g["Close"].to_numpy(np.float64))
        ts_parts.append(g.index.to_numpy())
        offset += len(g)

    def cat(parts):
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float64)

    all_ts = np.concatenate(ts_parts) if ts_parts else np.zeros(0, dtype='datetime64[ns]')
    return dict(min_o=cat(min_o_parts), min_h=cat(min_h_parts), min_l=cat(min_l_parts),
                min_c=cat(min_c_parts), bar_min_start=bar_min_start,
                bar_min_count=bar_min_count, bar_min_ts=all_ts)


@njit(cache=True)
def _simulate_trail_ground_truth(opens, highs, lows, closes, hours, daily_idx, sma_arr, std_arr,
                                  min_o, min_h, min_l, min_c, bar_min_start, bar_min_count,
                                  fixed_sl, arm_pct, trail_buy_pct, trail_sell_pct,
                                  max_hours_to_hold, z_thresh, target_h0, target_h1,
                                  open_check_entry_timing, is_both, same_bar_reentry):
    """Direct port of sim_minute_groundtruth_independent.py's Sim/simulate() state
    machine. See that module's docstring for WHY each choice was made; this function
    must not diverge from it behaviorally (mandatory byte-identical parity gate,
    docs/plans/ground_truth_kernel_rebuild.md Step 2a) — see module-level comment above."""
    entry_bar   = np.empty(MAX_TRADES, dtype=np.int64)
    entry_mj    = np.empty(MAX_TRADES, dtype=np.int64)
    entry_p     = np.empty(MAX_TRADES, dtype=np.float64)
    exit_bar    = np.empty(MAX_TRADES, dtype=np.int64)
    exit_mj     = np.empty(MAX_TRADES, dtype=np.int64)
    exit_p      = np.empty(MAX_TRADES, dtype=np.float64)
    reason      = np.empty(MAX_TRADES, dtype=np.int64)
    # Add-on-overlay support (2026-08-22, additive-only -- see
    # run_ground_truth_addon_overlay): records whether/where THIS trade
    # actually armed (state==STATE_ARMED reached before exit), independent
    # of its eventual exit reason (TRAIL always armed; TIME can be either;
    # SL never arms). arm_price_out mirrors `peak`'s own initialization
    # (peak = c at the HOLD->ARMED transition below) -- the real live
    # add-on leg's entry fill price is that same bar-close price, so this
    # is exactly the number a post-processing add-on simulation needs, with
    # zero re-simulation of the core state machine.
    armed_out     = np.zeros(MAX_TRADES, dtype=np.int64)
    arm_bar_out   = np.full(MAX_TRADES, -1, dtype=np.int64)
    arm_price_out = np.zeros(MAX_TRADES, dtype=np.float64)
    count = 0

    STATE_IDLE, STATE_WAIT, STATE_HOLD, STATE_ARMED = 0, 1, 2, 3
    state = STATE_IDLE
    running_low = 0.0
    wait_bar0 = 0
    cur_entry_price = 0.0
    cur_entry_bar = 0
    stop_price = 0.0
    arm_price = 0.0
    peak = 0.0
    fill_bar = -1
    fill_mj = -1
    fill_partial = False
    cur_armed = 0
    cur_arm_bar = -1
    cur_arm_price = 0.0

    n = len(closes)
    for i in range(n):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        m_start, m_count = bar_min_start[i], bar_min_count[i]

        # band_at(i)
        band = np.nan
        if hours[i] == target_h0 or hours[i] == target_h1:
            di = daily_idx[i]
            if di >= 0 and std_arr[di] != 0.0:
                band = sma_arr[di] - std_arr[di] * z_thresh

        # ── (1) open_check signal detection ──
        opened_this_bar = False
        if state == STATE_IDLE and not np.isnan(band) and open_check_entry_timing and o <= band:
            opened_this_bar = True
            if is_both:
                state = STATE_WAIT
                running_low = o
                wait_bar0 = i
            else:
                cur_entry_price = o
                cur_entry_bar = i
                stop_price = cur_entry_price * (1.0 - fixed_sl / 100.0)
                arm_price = cur_entry_price * (1.0 + arm_pct / 100.0)
                state = STATE_HOLD
                fill_bar = i
                fill_mj = GT_MJ_BAR_OPEN_FIRST_MINUTE
                fill_partial = False
                cur_armed = 0; cur_arm_bar = -1; cur_arm_price = 0.0

        # ── (2) continuous minute-level resolution ──
        if state != STATE_IDLE and m_count > 0:
            tbp = trail_buy_pct / 100.0
            tsp = trail_sell_pct / 100.0
            for j in range(m_count):
                mo, mh, ml, mc = min_o[m_start + j], min_h[m_start + j], min_l[m_start + j], min_c[m_start + j]

                if state == STATE_WAIT:
                    trig_prior = running_low * (1.0 + tbp)
                    if mo >= trig_prior:
                        cur_entry_price = mo
                        cur_entry_bar = i
                        stop_price = cur_entry_price * (1.0 - fixed_sl / 100.0)
                        arm_price = cur_entry_price * (1.0 + arm_pct / 100.0)
                        state = STATE_HOLD
                        fill_bar = i; fill_mj = j; fill_partial = False
                        cur_armed = 0; cur_arm_bar = -1; cur_arm_price = 0.0
                        continue
                    if ml < running_low:
                        running_low = ml
                    trig = running_low * (1.0 + tbp)
                    if mh >= trig:
                        cur_entry_price = trig
                        cur_entry_bar = i
                        stop_price = cur_entry_price * (1.0 - fixed_sl / 100.0)
                        arm_price = cur_entry_price * (1.0 + arm_pct / 100.0)
                        state = STATE_HOLD
                        fill_bar = i; fill_mj = j; fill_partial = True
                        cur_armed = 0; cur_arm_bar = -1; cur_arm_price = 0.0
                        continue

                elif state == STATE_HOLD:
                    if fill_partial and fill_bar == i and fill_mj == j:
                        continue
                    if mo <= stop_price:
                        entry_bar[count] = cur_entry_bar; entry_mj[count] = fill_mj if fill_bar == cur_entry_bar else GT_MJ_BAR_CLOSE
                        exit_bar[count] = i; exit_mj[count] = j
                        entry_p[count] = cur_entry_price; exit_p[count] = mo
                        reason[count] = GT_SL
                        armed_out[count] = cur_armed; arm_bar_out[count] = cur_arm_bar; arm_price_out[count] = cur_arm_price
                        count += 1; state = STATE_IDLE
                        continue
                    if ml <= stop_price:
                        entry_bar[count] = cur_entry_bar; entry_mj[count] = fill_mj if fill_bar == cur_entry_bar else GT_MJ_BAR_CLOSE
                        exit_bar[count] = i; exit_mj[count] = j
                        entry_p[count] = cur_entry_price; exit_p[count] = stop_price
                        reason[count] = GT_SL
                        armed_out[count] = cur_armed; arm_bar_out[count] = cur_arm_bar; arm_price_out[count] = cur_arm_price
                        count += 1; state = STATE_IDLE
                        continue

                elif state == STATE_ARMED:
                    gap = peak * (1.0 - tsp)
                    if mo <= gap:
                        entry_bar[count] = cur_entry_bar; entry_mj[count] = fill_mj if fill_bar == cur_entry_bar else GT_MJ_BAR_CLOSE
                        exit_bar[count] = i; exit_mj[count] = j
                        entry_p[count] = cur_entry_price; exit_p[count] = mo
                        reason[count] = GT_TRAIL
                        armed_out[count] = cur_armed; arm_bar_out[count] = cur_arm_bar; arm_price_out[count] = cur_arm_price
                        count += 1; state = STATE_IDLE
                        continue
                    if mh > peak:
                        peak = mh
                    stop = peak * (1.0 - tsp)
                    if ml <= stop:
                        entry_bar[count] = cur_entry_bar; entry_mj[count] = fill_mj if fill_bar == cur_entry_bar else GT_MJ_BAR_CLOSE
                        exit_bar[count] = i; exit_mj[count] = j
                        entry_p[count] = cur_entry_price; exit_p[count] = stop
                        reason[count] = GT_TRAIL
                        armed_out[count] = cur_armed; arm_bar_out[count] = cur_arm_bar; arm_price_out[count] = cur_arm_price
                        count += 1; state = STATE_IDLE
                        continue

        # ── (3) bar-close-gated events ──
        if state == STATE_WAIT and (i - wait_bar0) >= max_hours_to_hold:
            state = STATE_IDLE
        elif state == STATE_HOLD:
            held = i - cur_entry_bar
            if c >= arm_price:
                state = STATE_ARMED
                peak = c
                cur_armed = 1; cur_arm_bar = i; cur_arm_price = c
            elif held >= max_hours_to_hold:
                entry_bar[count] = cur_entry_bar; entry_mj[count] = fill_mj if fill_bar == cur_entry_bar else GT_MJ_BAR_CLOSE
                exit_bar[count] = i; exit_mj[count] = GT_MJ_BAR_CLOSE
                entry_p[count] = cur_entry_price; exit_p[count] = c
                reason[count] = GT_TIME
                armed_out[count] = cur_armed; arm_bar_out[count] = cur_arm_bar; arm_price_out[count] = cur_arm_price
                count += 1; state = STATE_IDLE
        elif state == STATE_ARMED and (i - cur_entry_bar) >= max_hours_to_hold:
            entry_bar[count] = cur_entry_bar; entry_mj[count] = fill_mj if fill_bar == cur_entry_bar else GT_MJ_BAR_CLOSE
            exit_bar[count] = i; exit_mj[count] = GT_MJ_BAR_CLOSE
            entry_p[count] = cur_entry_price; exit_p[count] = c
            reason[count] = GT_TIME
            armed_out[count] = cur_armed; arm_bar_out[count] = cur_arm_bar; arm_price_out[count] = cur_arm_price
            count += 1; state = STATE_IDLE

        # ── (4) close_check signal detection ──
        if state == STATE_IDLE and not np.isnan(band) and c <= band and not (opened_this_bar and not same_bar_reentry):
            if is_both:
                state = STATE_WAIT
                running_low = c
                wait_bar0 = i
            else:
                cur_entry_price = c
                cur_entry_bar = i
                stop_price = cur_entry_price * (1.0 - fixed_sl / 100.0)
                arm_price = cur_entry_price * (1.0 + arm_pct / 100.0)
                state = STATE_HOLD
                fill_bar = -1; fill_mj = -1; fill_partial = False
                cur_armed = 0; cur_arm_bar = -1; cur_arm_price = 0.0

    return (entry_bar[:count], entry_mj[:count], entry_p[:count],
            exit_bar[:count], exit_mj[:count], exit_p[:count], reason[:count],
            armed_out[:count], arm_bar_out[:count], arm_price_out[:count])


_GT_REASON_NAMES = {GT_SL: 'SL', GT_TRAIL: 'TRAIL', GT_TIME: 'TIME'}


def run_backtest_ground_truth(df_hourly, df_daily_indicators, ticker, minute_df, *,
                               fixed_sl, arm_pct, trail_buy_pct, trail_sell_pct,
                               max_hours_to_hold, z_score_threshold, is_both,
                               target_hours=(9, 14), open_check_entry_timing=True,
                               same_bar_reentry=True, prep=None, mprep=None, need_times=True):
    """Python wrapper: prep + kernel call + trade reconstruction (real timestamps, not
    the kernel's bar/minute-offset indices) for the v6 ground-truth kernel. `minute_df`
    must already be regular-session-filtered/tz-naive (sim_minute_groundtruth_independent
    .load_minutes()). Not wired into run_backtest_dispatch/the sweep engine yet —
    that's Step 3, gated on the parity check (2a) passing first.

    need_times=False skips resolve_time() (pd.Timestamp construction per trade) and
    returns trades without 'Entry Time'/'Exit Time' keys -- for the Phase1-coarse sweep-
    grid path, which only ever reads 'Return' (_summarize_trades_ground_truth) and never
    consumes timestamps. Found 2026-08-22: the mandatory byte-identical parity gate
    (tests/test_ground_truth_kernel_parity.py) asserts entry_time/exit_time, so this
    MUST stay True there (it doesn't pass need_times, so it's unaffected) -- this flag
    exists to let the sweep grid skip work the parity gate genuinely needs and the grid
    genuinely doesn't, not to weaken the gate itself.

    WHEN WIRING INTO run_backtest_dispatch (Step 3), READ THIS FIRST (flagged by the
    paired-review contextual pass, 2026-08-21): `fixed_sl`/`arm_pct`/`trail_buy_pct`/
    `trail_sell_pct` here are taken as RAW PERCENTAGES (e.g. 2.0 for 2%, matching the real
    watch_list row and the parity test's `n["fixed_sl"]`) and divided by 100 INSIDE this
    function's njit call — unlike run_backtest_dispatch's existing TrailingBoth/TrailingExit
    branches, which pre-divide by 100 before calling run_backtest_v110/v18 (e.g.
    `stop_loss=float(fixed_sl) / 100.0`). `open_check_entry_timing` is also a resolved bool
    here, not the raw `entry_timing` string those branches pass through. Copy-pasting the
    existing dispatch pattern for this function would silently pre-divide a second time
    (every threshold 100x too small) and pass the wrong type. Call this with raw
    percentages and a bool, not a second /100.0."""
    prep = prep or prep_inputs(df_hourly, df_daily_indicators)
    mprep = mprep or prep_minute_inputs(minute_df, df_hourly)

    eb, emj, ep, xb, xmj, xp, rs, armed, ab, ap = _simulate_trail_ground_truth(
        prep['opens'], prep['highs'], prep['lows'], prep['prices'], prep['hours'], prep['daily_idx'],
        prep['sma_arr'], prep['std_arr'],
        mprep['min_o'], mprep['min_h'], mprep['min_l'], mprep['min_c'],
        mprep['bar_min_start'], mprep['bar_min_count'],
        float(fixed_sl), float(arm_pct), float(trail_buy_pct), float(trail_sell_pct),
        int(max_hours_to_hold), float(z_score_threshold), int(target_hours[0]), int(target_hours[1]),
        bool(open_check_entry_timing), bool(is_both), bool(same_bar_reentry),
    )

    if not need_times:
        trades = []
        for k in range(len(eb)):
            trades.append({
                'Ticker': ticker,
                'Entry Price': float(ep[k]),
                'Exit Price': float(xp[k]),
                'exit_reason': _GT_REASON_NAMES[int(rs[k])],
                'Return': (float(xp[k]) - float(ep[k])) / float(ep[k]),
                # Add-on-overlay support -- see run_ground_truth_addon_overlay.
                # 'armed' is independent of exit_reason (TIME can be either).
                'armed': bool(armed[k]),
                'Arm Price': float(ap[k]) if armed[k] else None,
            })
        return trades

    idx = df_hourly.index
    bar_ts = mprep['bar_min_ts']

    def resolve_time(bar_i, mj):
        # GT_MJ_BAR_OPEN_FIRST_MINUTE and GT_MJ_BAR_CLOSE both resolve to the hourly
        # bar's own timestamp (idx[bar_i]), not that bar's first printed minute —
        # the prototype records entry_time=t0 for an open_check market-buy fill too
        # (sim_minute_groundtruth_independent.py's _open(t0, o, ...) call), only ever
        # passing the minute as fill_minute for the (here-unused, since fill_partial
        # =False on this path) skip-check, never as the trade's recorded time. Found
        # by the paired-review independent-cold pass (2026-08-21): resolving to the
        # bar's first REAL minute print instead diverges whenever that minute isn't
        # exactly H:30 (reproduced live on AGQ/GDXU/NUGT/DFEN/WEBL/UGL).
        if mj == GT_MJ_BAR_CLOSE or mj == -1 or mj == GT_MJ_BAR_OPEN_FIRST_MINUTE:
            return idx[bar_i]
        start = mprep['bar_min_start'][bar_i]
        return pd.Timestamp(bar_ts[start + mj])

    trades = []
    for k in range(len(eb)):
        trades.append({
            'Ticker': ticker,
            'Entry Time': resolve_time(int(eb[k]), int(emj[k])),
            'Entry Price': float(ep[k]),
            'Exit Time': resolve_time(int(xb[k]), int(xmj[k])),
            'Exit Price': float(xp[k]),
            'exit_reason': _GT_REASON_NAMES[int(rs[k])],
            'Return': (float(xp[k]) - float(ep[k])) / float(ep[k]),
            # Add-on-overlay support -- see run_ground_truth_addon_overlay.
            'armed': bool(armed[k]),
            'Arm Time': resolve_time(int(ab[k]), GT_MJ_BAR_CLOSE) if armed[k] else None,
            'Arm Price': float(ap[k]) if armed[k] else None,
        })
    return trades


def apply_addon_overlay_ground_truth(trades):
    """Post-processing pass (2026-08-22) over a run_backtest_ground_truth trade list --
    NOT a re-simulation -- that synthesizes the real add-on-at-arm mechanism's per-trade
    blended return (signals_notify.check_addon_trigger_real: on arm, a real market BUY
    for the SAME share count as the core position opens at the arm bar's own price, then
    closes at the exact same time/price as the core leg's own exit).

    Math (verified by hand against a worked example in this session's report): with N
    shares each leg, entry price E, arm price A, exit price X --
        core-only P&L/share   = X - E
        add-on P&L/share      = X - A          (only exists post-arm)
        blended P&L/share     = (X - E) + (X - A) = 2X - E - A
        blended return        = (2X - E - A) / E

    This is NOT 2x the core return: the add-on leg only doubles the ARM-TO-EXIT dollar
    P&L (its own capital base is the arm price A, not entry price E), so the blended
    return equals core_return + (X - A) / E, not 2 * core_return. The return is expressed
    on E (the core leg's own capital) because the add-on leg is real-money margin-funded
    on TOP of that capital, not drawn from it -- same convention as every other GT trade's
    'Return', which is always relative to that trade's own entry price.

    Only trades carrying armed=True get an add-on leg; SL and never-armed TIME exits are
    returned with their own core 'Return' unchanged (exit_reason=='SL' can never be armed;
    exit_reason=='TIME' can be either, which is exactly why 'armed' has to be read off the
    trade dict directly rather than inferred from exit_reason -- see run_backtest_ground_
    truth's own docstring on this same ambiguity).

    return_below_floor (2026-08-22, paired-review CRITICAL finding, both Sonnet- and
    Opus-independent passes converged on this independently): unlike a core-only trade
    (Return is always >= -1, since exit_price >= 0 and Return = (X-E)/E >= -E/E = -1), a
    blended trade has NO such floor -- (2X-E-A)/E can go below -1 whenever the exit price
    gaps far enough below the arm price (e.g. an overnight gap-down on a leveraged ETF,
    or the hold-time-forced TIME exit while still armed and deep underwater). This is not
    a pure math artifact: it's the real economics of a margin-funded add-on leg (a bad
    enough move really can wipe out more than the core leg's own capital), but it also
    means naive downstream compounding (prod(1+Return) across trades) silently breaks --
    a single such trade flips the sign of the whole product and can drive
    _cagr_from_total_return's fractional power into a complex/nan result with no
    exception raised. Flagged here (not clamped -- clamping would hide a real, not
    fabricated, loss) so every caller can decide how to handle it explicitly rather than
    silently aggregating a poisoned number."""
    out = []
    for t in trades:
        t2 = dict(t)
        t2['Return_core'] = t['Return']
        if t.get('armed'):
            entry, exitp, arm = t['Entry Price'], t['Exit Price'], t['Arm Price']
            blended = (2.0 * exitp - entry - arm) / entry
            t2['Return'] = blended
            t2['addon_applied'] = True
            t2['return_below_floor'] = blended < -1.0
        else:
            t2['addon_applied'] = False
            t2['return_below_floor'] = False
        out.append(t2)
    return out


def simulate_drought_overlay_ground_truth(trades, df_hourly_windowed, ticker, fixed_sl,
                                           arm_pct, trail_sell_pct,
                                           confirm_days_grid=None, vol_gate_grid=None):
    """GT-kernel drought overlay (2026-08-23, Phase 4 of docs/plans/ground_truth_kernel_
    rebuild.md), replacing scripts/gt_addon_winner_drought_eval.py's post-hoc single-
    fixed-confirm_days pass. Real drought design/mechanism UNCHANGED (see
    scripts/drought_overlay_test.py's own docstring) -- once a drought is confirmed
    (confirm_days no-signal trading days), buy the underlying and manage it with the
    same fixed-SL/trailing-stop primitives the core strategy already uses, handing off
    at the strategy's own next real signal if neither fires first. What changed here:
    (1) confirm_days x vol_gate is swept per candidate (scripts/drought_overlay_sweep.py's
    own CONFIRM_DAYS_GRID/VOL_GATE_GRID, reused as-is, not invented) instead of a single
    hardcoded confirm_days=3, and (2) this is called once per shortlisted Phase2.5-GT
    candidate directly off the SAME `trades` list build_candidate_report_ground_truth
    already computed for that candidate's checks 4/8/11/13 -- no second
    run_backtest_ground_truth call to reconstruct the core trade list a second time (the
    old script's own redundant "reconstructs the winner's own CORE trade list" step).

    `trades` must carry real 'Entry Time'/'Exit Time' Timestamps (need_times=True) --
    converted to bar-index positions in df_hourly_windowed via .get_loc, exactly like the
    old script's trades_to_bar_indices (GT always resolves entry/exit to a real index
    label in that same frame, so a lookup miss here would indicate a real bug upstream,
    not an expected case -- silently skipped per-trade like the original, not raised,
    since one skipped trade shouldn't block the other N-1 from finding real drought
    windows). Scope: TrailingBothZScoreBreakout candidates only, matching the legacy
    script's own "is_both assumed True" restriction and simulate_overlay's own
    proven-only-for-that-shape SL/arm/trail semantics -- callers must gate on is_both
    themselves (this function doesn't re-derive it) before calling.

    vol_gate reuses drought_overlay_sweep.py's real intraday-realized-vol entry gate
    (get_ivol_series/_entry_vol_pctile), which reads a fixed cache/research/{ticker}_1h.csv
    regardless of the campaign's own data_source -- a known limitation carried forward
    from the legacy script, not fixed here (out of scope: generalizing that helper to be
    data_source-aware). If that CSV is missing, every vol_gate!=None cell is skipped
    (silently, not fabricated) -- only vol_gate=None (the original ungated behavior)
    still gets evaluated in that case, never a hard failure.

    No cliff-safety pass is computed here (user's explicit call, carried forward from the
    legacy script's own docstring: "drought does not need its own cliff-safety pass right
    now, we'll end up doing a second sweep on the winner nodes"). The winning
    (confirm_days, vol_gate) pair is picked by the best resulting core+drought combined
    compounded return -- not cliff-safety-screened.

    Returns None if fewer than 2 real core trades exist (no drought windows are possible
    with 0 or 1 signals) or none of the input trades map onto df_hourly_windowed's index.
    Otherwise a dict: {n_core_trades, core_compounded_pct, best_confirm_days,
    best_vol_gate, n_drought_windows, n_drought_simulated, drought_compounded_pct,
    combined_compounded_pct, n_grid_cells_evaluated} -- drought_compounded_pct/
    combined_compounded_pct are None (not NaN) when the grid produced zero real drought
    windows at every (confirm_days, vol_gate) pair (e.g. a ticker with too few gaps
    between real signals to ever confirm a drought)."""
    if len(trades) < 2:
        return None
    # Lazy import (matches this file's existing lazy-import convention for the add-on
    # overlay's own consumer in run_optimization_sweep.py): scripts/drought_overlay_test.py
    # imports `from backtester import prep_inputs, OPEN` at module level, so a top-level
    # import here would be a real circular import, not just a layering nicety.
    from scripts.drought_overlay_test import find_drought_windows, simulate_overlay
    from scripts.drought_overlay_sweep import (
        CONFIRM_DAYS_GRID, VOL_GATE_GRID, get_ivol_series, _entry_vol_pctile)

    confirm_days_grid = confirm_days_grid if confirm_days_grid is not None else CONFIRM_DAYS_GRID
    vol_gate_grid = vol_gate_grid if vol_gate_grid is not None else VOL_GATE_GRID

    idx = df_hourly_windowed.index
    n_bars = len(idx)
    # CRITICAL fix (2026-08-23, paired-review independent-cold finding, empirically
    # confirmed against real SOXL trades before this fix: only 2/101 real trades
    # matched via idx.get_loc, silently collapsing this whole function to a near-
    # permanent None). The original assumption (copied from the now-superseded
    # scripts/gt_addon_winner_drought_eval.py's own docstring, never independently
    # verified) was that GT always resolves entry/exit to a real index LABEL in this
    # frame -- false: run_backtest_ground_truth's resolve_time() returns a real
    # MINUTE timestamp (not an hourly bar label) for any intrabar trail-buy fill or
    # SL/TRAIL exit (backtester.py's own GT_MJ_BAR_CLOSE/-1/GT_MJ_BAR_OPEN_FIRST_MINUTE
    # special-casing only covers bar-close/open-first-minute events). Fixed by
    # floor-mapping each real timestamp onto the hourly bar that OWNS it (bar H:30
    # owns [H:30, H+1:30), same bucketing prep_minute_inputs already uses) via
    # searchsorted instead of an exact-label get_loc -- this also sidesteps get_loc's
    # ambiguous-match behavior on a duplicate index label (a separate LOW finding
    # from the same review round).
    def _floor_bar(ts):
        pos = int(idx.searchsorted(ts, side='right')) - 1
        return pos if 0 <= pos < n_bars else None

    bar_trades = []
    for t in trades:
        signal_i = _floor_bar(t['Entry Time'])
        exit_i = _floor_bar(t['Exit Time'])
        if signal_i is None or exit_i is None:
            continue  # entry/exit fell entirely before df_hourly_windowed's own index -- skip
        bar_trades.append({'signal_i': signal_i, 'exit_i': exit_i})
    if len(bar_trades) < 2:
        return None

    core_rets = [t['Return'] for t in trades]
    core_compounded = float((pd.Series(core_rets) + 1).prod() - 1) * 100

    ivol_series = None
    if any(vg is not None for vg in vol_gate_grid):
        try:
            ivol_series = get_ivol_series(ticker)
        except (FileNotFoundError, pd.errors.ParserError, pd.errors.EmptyDataError, KeyError):
            # Missing/malformed/columnless CSV (LOW finding, paired review 2026-08-23) --
            # every vol_gate!=None cell is skipped below, never fabricated; only
            # vol_gate=None still gets evaluated.
            ivol_series = None

    cells = {}
    windows_by_cd = {}
    for confirm_days in confirm_days_grid:
        windows = find_drought_windows(bar_trades, df_hourly_windowed, confirm_days)
        if not windows:
            continue
        windows_by_cd[confirm_days] = windows
        for vol_gate in vol_gate_grid:
            if vol_gate is None:
                gated = windows
            elif ivol_series is None:
                continue  # vol gate requested but no ivol data available -- skip, don't fabricate
            else:
                gated = []
                for entry_i, backstop_i in windows:
                    entry_time = idx[entry_i + 1] if entry_i + 1 < len(idx) else idx[entry_i]
                    pctile = _entry_vol_pctile(entry_time, ivol_series)
                    if pctile is not None and pctile < vol_gate:
                        gated.append((entry_i, backstop_i))
            if not gated:
                continue
            rets = [simulate_overlay(df_hourly_windowed, entry_i, backstop_i,
                                      fixed_sl_pct=fixed_sl, arm_pct=arm_pct,
                                      trail_sell_pct=trail_sell_pct)['ret']
                    for entry_i, backstop_i in gated]
            cells[(confirm_days, vol_gate)] = rets

    if not cells:
        return {
            'n_core_trades': len(trades), 'core_compounded_pct': core_compounded,
            'best_confirm_days': None, 'best_vol_gate': None,
            'n_drought_windows': 0, 'n_drought_simulated': 0,
            'drought_compounded_pct': None, 'combined_compounded_pct': None,
            'n_grid_cells_evaluated': 0, 'best_rets': None,
        }

    def _combined_compounded(rets):
        return float((pd.Series(core_rets + rets) + 1).prod() - 1) * 100

    best_key = max(cells, key=lambda k: _combined_compounded(cells[k]))
    best_cd, best_vg = best_key
    best_rets = cells[best_key]
    drought_compounded = float((pd.Series(best_rets) + 1).prod() - 1) * 100
    return {
        'n_core_trades': len(trades), 'core_compounded_pct': core_compounded,
        'best_confirm_days': best_cd, 'best_vol_gate': best_vg,
        'n_drought_windows': len(windows_by_cd.get(best_cd, [])),
        'n_drought_simulated': len(best_rets),
        'drought_compounded_pct': drought_compounded,
        'combined_compounded_pct': _combined_compounded(best_rets),
        'n_grid_cells_evaluated': len(cells),
        # 'best_rets' (2026-08-23, GT full-review port): the raw per-window return list at
        # the winning (confirm_days, vol_gate) cell, in the SAME chronological order
        # `windows` (from find_drought_windows, an ascending bar-index scan) produced them
        # -- added so a caller can compute real win-rate/early-late-stability checks off
        # the actual windows, not just the aggregate drought_compounded_pct. None whenever
        # the winning cell doesn't exist (the `not cells` branch above).
        'best_rets': best_rets,
    }


def drought_included_excluded_ground_truth(trades, df_hourly_windowed, ticker, fixed_sl,
                                            arm_pct, trail_sell_pct, confirm_days, vol_gate):
    """GT-kernel port (2026-08-23, candidate_full_review.py GT full-review build-out) of
    scripts/candidate_full_review.drought_included_excluded_check -- docs/overlay_
    parameter_robustness_process.md step 4: confirm the entry-time intraday-vol-percentile
    gate does real differential selection (kept windows beat thrown-out windows), not just
    look profitable in isolation. Reuses the exact same building blocks simulate_drought_
    overlay_ground_truth already imports (find_drought_windows/simulate_overlay/
    get_ivol_series/_entry_vol_pctile) and the same floor-bar timestamp->bar-index mapping
    fix that function's own docstring documents (GT trade times are real minute
    timestamps, not always an exact hourly bar label) -- NOT re-derived, just inlined here
    since simulate_drought_overlay_ground_truth doesn't expose its own _floor_bar closure.

    `confirm_days` is the caller's choice -- pass the SAME winning confirm_days
    simulate_drought_overlay_ground_truth's own sweep picked (its 'best_confirm_days'
    field) so this challenge stays consistent with whatever drought_n/drought_compounded_pct
    a report is showing elsewhere for the same candidate, matching the legacy check's own
    "pulled from this node's own existing drought overlay run" convention.

    Returns None if <2 real core trades, <2 real drought windows at this confirm_days, or
    no real ivol data exists for `ticker`. Otherwise a dict (same shape as the legacy
    check): {confirm_days, vol_gate, n_included, included_compounded_pct,
    included_win_rate_pct, n_excluded, excluded_compounded_pct, excluded_win_rate_pct,
    verdict} where verdict is one of REAL_SELECTION / DISCRIMINATES_BUT_UNPROFITABLE /
    NO_REAL_SELECTION / 'N/A (all one side)' -- same requires-profitable-AND-discriminates
    convention as the legacy check (a filter that merely loses less isn't a pass)."""
    if len(trades) < 2:
        return None
    from scripts.drought_overlay_test import find_drought_windows, simulate_overlay
    from scripts.drought_overlay_sweep import get_ivol_series, _entry_vol_pctile

    idx = df_hourly_windowed.index
    n_bars = len(idx)

    def _floor_bar(ts):
        pos = int(idx.searchsorted(ts, side='right')) - 1
        return pos if 0 <= pos < n_bars else None

    bar_trades = []
    for t in trades:
        signal_i = _floor_bar(t['Entry Time'])
        exit_i = _floor_bar(t['Exit Time'])
        if signal_i is None or exit_i is None:
            continue
        bar_trades.append({'signal_i': signal_i, 'exit_i': exit_i})
    if len(bar_trades) < 2:
        return None

    windows = find_drought_windows(bar_trades, df_hourly_windowed, confirm_days)
    if len(windows) < 2:
        return None

    try:
        ivol_series = get_ivol_series(ticker)
    except (FileNotFoundError, pd.errors.ParserError, pd.errors.EmptyDataError, KeyError):
        return None
    if ivol_series is None:
        return None

    included, excluded = [], []
    for entry_i, backstop_i in windows:
        entry_time = idx[entry_i + 1] if entry_i + 1 < len(idx) else idx[entry_i]
        pctile = _entry_vol_pctile(entry_time, ivol_series)
        if pctile is None:
            continue
        ret = simulate_overlay(df_hourly_windowed, entry_i, backstop_i,
                                fixed_sl_pct=fixed_sl, arm_pct=arm_pct,
                                trail_sell_pct=trail_sell_pct)['ret']
        (included if pctile < vol_gate else excluded).append(ret)

    if len(included) < 1 or len(excluded) < 1:
        return {"confirm_days": confirm_days, "vol_gate": vol_gate,
                "n_included": len(included), "n_excluded": len(excluded),
                "verdict": "N/A (all one side)"}

    inc_comp = float((pd.Series(included) + 1).prod() - 1) * 100
    exc_comp = float((pd.Series(excluded) + 1).prod() - 1) * 100
    inc_wr = sum(1 for r in included if r > 0) / len(included) * 100
    exc_wr = sum(1 for r in excluded if r > 0) / len(excluded) * 100
    discriminates = (inc_comp > exc_comp) and (inc_wr > exc_wr)
    if discriminates and inc_comp > 0:
        verdict = "REAL_SELECTION"
    elif discriminates:
        verdict = "DISCRIMINATES_BUT_UNPROFITABLE"
    else:
        verdict = "NO_REAL_SELECTION"

    return {
        "confirm_days": confirm_days, "vol_gate": vol_gate,
        "n_included": len(included), "included_compounded_pct": inc_comp, "included_win_rate_pct": inc_wr,
        "n_excluded": len(excluded), "excluded_compounded_pct": exc_comp, "excluded_win_rate_pct": exc_wr,
        "verdict": verdict,
    }
