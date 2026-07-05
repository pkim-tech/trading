import numpy as np
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
def _simulate_trail(prices, highs, lows, hours, daily_idx, sma_arr, std_arr, trend_arr, has_trend,
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

            # SL check
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
                     z_score_threshold=2.0, trail_pct=0.03, prep=None):
    p = prep if prep is not None else prep_inputs(df_hourly, df_daily_indicators)
    target_h0, target_h1 = int(target_hours[0]), int(target_hours[1])

    ei, xi, ep, xp, held, res, ret = _simulate_trail(
        p['prices'], p['highs'], p['lows'], p['hours'], p['daily_idx'],
        p['sma_arr'], p['std_arr'], p['trend_arr'], p['has_trend'],
        float(take_profit), float(stop_loss), int(max_hours_to_hold), float(trail_pct),
        target_h0, target_h1, float(z_score_threshold)
    )
    return _build_trades(ticker, p['timestamps'], ei, xi, ep, xp, held, res, ret)


@njit(cache=True)
def _simulate_trail_buy(prices, highs, lows, hours, daily_idx, sma_arr, std_arr, trend_arr, has_trend,
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
                         take_profit, stop_loss, max_hours_to_hold, trail_buy_pct, trail_pct, target_h0, target_h1, z_thresh):
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
    trailing    = False
    entry_price = 0.0
    stop_price  = 0.0
    tp_price    = 0.0
    peak        = 0.0
    entry_bar   = 0
    held        = 0
    running_low = 0.0
    wait_bars   = 0

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
                    entry_i[count] = entry_bar; exit_i[count] = i
                    entry_p[count] = entry_price; exit_p[count] = exit_px
                    hours_held[count] = held
                    results[count] = WIN if pc > 0 else LOSS; returns[count] = pc
                    count += 1; in_trade = False; trailing = False
                continue
            if low <= stop_price:
                pc = (stop_price - entry_price) / entry_price
                entry_i[count] = entry_bar; exit_i[count] = i
                entry_p[count] = entry_price; exit_p[count] = stop_price
                hours_held[count] = held; results[count] = LOSS; returns[count] = pc
                count += 1; in_trade = False
                continue
            if cp >= tp_price:
                trailing = True; peak = cp
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
            if low < running_low:
                running_low = low
            buy_trigger = running_low * (1.0 + trail_buy_pct)
            if high >= buy_trigger:
                entry_price = buy_trigger
                tp_price    = entry_price * (1.0 + take_profit)
                stop_price  = entry_price * (1.0 - stop_loss)
                entry_bar   = i; held = 0
                in_trade = True; waiting = False; trailing = False
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


def run_backtest_v19(df_hourly, df_daily_indicators, ticker,
                     mode="BACKTEST", target_hours=(9, 14),
                     take_profit=0.05, stop_loss=0.15, max_hours_to_hold=28,
                     z_score_threshold=2.0, trail_buy_pct=0.02, prep=None):
    p = prep if prep is not None else prep_inputs(df_hourly, df_daily_indicators)
    target_h0, target_h1 = int(target_hours[0]), int(target_hours[1])

    ei, xi, ep, xp, held, res, ret = _simulate_trail_buy(
        p['prices'], p['highs'], p['lows'], p['hours'], p['daily_idx'],
        p['sma_arr'], p['std_arr'], p['trend_arr'], p['has_trend'],
        float(take_profit), float(stop_loss), int(max_hours_to_hold), float(trail_buy_pct),
        target_h0, target_h1, float(z_score_threshold)
    )
    return _build_trades(ticker, p['timestamps'], ei, xi, ep, xp, held, res, ret)


def run_backtest_v110(df_hourly, df_daily_indicators, ticker,
                      mode="BACKTEST", target_hours=(9, 14),
                      take_profit=0.05, stop_loss=0.15, max_hours_to_hold=28,
                      z_score_threshold=2.0, trail_buy_pct=0.02, trail_pct=0.03, prep=None):
    p = prep if prep is not None else prep_inputs(df_hourly, df_daily_indicators)
    target_h0, target_h1 = int(target_hours[0]), int(target_hours[1])

    ei, xi, ep, xp, held, res, ret = _simulate_trail_both(
        p['prices'], p['highs'], p['lows'], p['hours'], p['daily_idx'],
        p['sma_arr'], p['std_arr'], p['trend_arr'], p['has_trend'],
        float(take_profit), float(stop_loss), int(max_hours_to_hold),
        float(trail_buy_pct), float(trail_pct),
        target_h0, target_h1, float(z_score_threshold)
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
                          fixed_sl=0.0, trail_pct_pct=0.0, prep=None):
    """Strategy-aware dispatch to the correct kernel wrapper — single source of truth
    for what a raw swept 'sl_raw' grid value (plus the fixed_sl/trail_pct_pct config
    values) actually mean for a given strategy. Mirrors
    run_optimization_sweep.py::run_single_backtest_node_isolated's branches so the
    sweep engine and any UI page replaying a node can't drift apart again — see
    docs/design.md 'Grid axis meaning by strategy'.
    take_profit/sl_raw/fixed_sl/trail_pct_pct are percent-scale (e.g. 15, not 0.15)."""
    import strategies as _strategies
    tp   = float(take_profit) / 100.0
    hold = int(max_hours_to_hold)
    z    = float(z_score_threshold)

    if issubclass(strategy_class, _strategies.TrailingBothZScoreBreakout):
        return run_backtest_v110(df_hourly, df_daily_indicators, ticker,
            take_profit=tp, stop_loss=float(fixed_sl) / 100.0, max_hours_to_hold=hold,
            z_score_threshold=z, trail_buy_pct=float(sl_raw) / 100.0,
            trail_pct=float(trail_pct_pct) / 100.0, prep=prep)
    if issubclass(strategy_class, _strategies.TrailingBuyZScoreBreakout):
        return run_backtest_v19(df_hourly, df_daily_indicators, ticker,
            take_profit=tp, stop_loss=float(fixed_sl) / 100.0, max_hours_to_hold=hold,
            z_score_threshold=z, trail_buy_pct=float(sl_raw) / 100.0, prep=prep)
    if issubclass(strategy_class, _strategies.TrailingExitZScoreBreakout):
        return run_backtest_v18(df_hourly, df_daily_indicators, ticker,
            take_profit=tp, stop_loss=float(fixed_sl) / 100.0, max_hours_to_hold=hold,
            z_score_threshold=z, trail_pct=float(sl_raw) / 100.0, prep=prep)
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
