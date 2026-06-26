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


@njit(cache=True)
def _simulate(prices, hours, daily_idx, sma_arr, std_arr, trend_arr, has_trend,
              take_profit, stop_loss, max_hours_to_hold, target_h0, target_h1):
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

        lower_band = sma - std * 2.0

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


def run_backtest(df_hourly, df_daily_indicators, ticker,
                 mode="BACKTEST", target_hours=(9, 14),
                 take_profit=0.05, stop_loss=0.15, max_hours_to_hold=28):

    prices = df_hourly['Close'].to_numpy(dtype=np.float64)
    timestamps = df_hourly.index
    hours = timestamps.hour.to_numpy(dtype=np.int64)
    date_strs = timestamps.strftime('%Y-%m-%d')

    daily_date_strs = df_daily_indicators.index.strftime('%Y-%m-%d')
    daily_lookup = {d: i for i, d in enumerate(daily_date_strs)}
    daily_idx = np.array([daily_lookup.get(d, -1) for d in date_strs], dtype=np.int64)

    sma_arr = df_daily_indicators['SMA'].to_numpy(dtype=np.float64)
    std_arr = df_daily_indicators['Std'].to_numpy(dtype=np.float64)
    has_trend = 'Trend_Filter' in df_daily_indicators.columns
    trend_arr = df_daily_indicators['Trend_Filter'].to_numpy(dtype=np.float64) if has_trend else np.zeros(1, dtype=np.float64)

    target_h0, target_h1 = int(target_hours[0]), int(target_hours[1])

    ei, xi, ep, xp, held, res, ret = _simulate(
        prices, hours, daily_idx, sma_arr, std_arr, trend_arr, has_trend,
        float(take_profit), float(stop_loss), int(max_hours_to_hold),
        target_h0, target_h1
    )

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
