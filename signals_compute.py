"""
Signal computation: cached price loading, buy-signal evaluation (with the
SMA/Std indicator cache), and sell-condition checking.
"""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd
import yfinance as yf

import strategies
import signals_config as cfg
import signals_db as db


# ---------------------------------------------------------------------------
# Price data
# ---------------------------------------------------------------------------

def _load_cache(ticker):
    path = cfg.CACHE_DIR / f"{ticker}_1h.csv"
    if not path.exists():
        return None, None
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    df_daily = df.resample('D').last().dropna()
    return df, df_daily


def _current_price(ticker):
    df, _ = _load_cache(ticker)
    if df is None:
        return None, None
    prices = df['Close'].dropna()
    return float(prices.iloc[-1]), df.index[-1]


# ---------------------------------------------------------------------------
# Signal computation
# ---------------------------------------------------------------------------

def _hurst_adf(ticker, df_hourly):
    hurst = None
    try:
        with sqlite3.connect(cfg.RESEARCH_DB_PATH) as c:
            row = c.execute(
                "SELECT hurst FROM hurst_cache WHERE ticker = ? ORDER BY timestamp DESC LIMIT 1",
                (ticker,)
            ).fetchone()
        if row:
            hurst = row[0]
    except Exception:
        pass

    adf_p = None
    try:
        from statsmodels.tsa.stattools import adfuller
        close = df_hourly['Close'].dropna()
        n = min(200, len(close))
        if n >= 20:
            adf_p = adfuller(close.iloc[-n:], maxlag=1, autolag=None)[1]
    except Exception:
        pass

    return hurst, adf_p


_indicator_cache = {}  # (ticker, strategy, window) -> (cache_key, indicators df); avoids
                       # recomputing the full rolling SMA/Std history on every 5-min poll


def compute_buy_signal(node, as_of=None, price_override=None, df_hourly_override=None, df_daily_override=None):
    ticker = node['ticker']
    window = int(node['window'])

    strategy_cls = getattr(strategies, node['strategy'], None)
    if strategy_cls is None:
        return None

    if df_hourly_override is not None:
        df_hourly, df_daily = df_hourly_override, df_daily_override
    else:
        df_hourly, df_daily = _load_cache(ticker)
    if df_hourly is None or len(df_daily) < window:
        return None

    z_thresh = float(node.get('z_score_threshold', 2.0))
    strat = strategy_cls(window=window, z_score_threshold=z_thresh)
    today = (as_of if as_of is not None else pd.Timestamp.now()).normalize()
    df_daily_prior = df_daily[df_daily.index < today]

    cache_id = (ticker, node['strategy'], window)
    cache_key = (len(df_daily_prior), df_daily_prior.index[-1] if not df_daily_prior.empty else None)
    cached = _indicator_cache.get(cache_id)
    if cached is not None and cached[0] == cache_key:
        indicators = cached[1]
    else:
        indicators = strat.generate_daily_indicators(df_daily_prior)
        _indicator_cache[cache_id] = (cache_key, indicators)
    if indicators.empty:
        return None

    last_row      = indicators.iloc[-1]
    close_series  = df_hourly['Close'].dropna()
    last_bar      = close_series.index[-1]
    daily_closes = df_daily['Close'].dropna()
    prev_close = float(daily_closes.iloc[-1]) if not daily_closes.empty else close_series.iloc[-1]
    if price_override is not None:
        current_price = price_override
    else:
        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                hist = ex.submit(lambda: yf.Ticker(ticker).history(period='1d', interval='1m', prepost=True)).result(timeout=10)
            current_price = float(hist['Close'].iloc[-1]) if not hist.empty else close_series.iloc[-1]
        except Exception:
            current_price = close_series.iloc[-1]
    sma           = last_row['SMA']
    std           = last_row['Std']
    hurst, adf_p  = _hurst_adf(ticker, df_hourly)

    signal_ctx = {
        'current_price': current_price,
        'low':           current_price,  # no true intrabar low available live; best proxy
        'sma':           sma,
        'std':           std,
        'trend':         last_row['Trend_Filter'] if 'Trend_Filter' in indicators.columns else None,
    }

    return {
        'ticker':        ticker,
        'window':        window,
        'current_price': current_price,
        'prev_close':    prev_close,
        'sma':           sma,
        'std':           std,
        'lower_band':    sma - z_thresh * std,
        'z_score':       (current_price - sma) / std,
        'signal':        strat.check_signal(signal_ctx),
        'last_bar':      last_bar,
        'last_daily_bar': indicators.index[-1],
        'hurst':         hurst,
        'adf_p':         adf_p,
    }


def _bars_held(df_hourly, signal_time):
    """Trading-hour bars elapsed since the signal bar — mirrors the kernels'
    `held += 1` per hourly row (cached data is market-hours-only), unlike
    wall-clock hours which run ~3.5x faster than trading hours."""
    if df_hourly is None or df_hourly.empty:
        return 0
    return int((df_hourly.index > signal_time).sum())


def check_sell_condition(pos, current_price, now, at_bar_close=True, low=None, high=None, df_hourly=None):
    strategy_cls = getattr(strategies, pos['strategy'], None)
    if strategy_cls is None:
        return None, None, False
    signal_time = datetime.strptime(pos['signal_time'], '%Y-%m-%d %H:%M:%S')
    if df_hourly is None:
        df_hourly, _ = _load_cache(pos['ticker'])
    hours_held = _bars_held(df_hourly, signal_time)
    # For v1.8/v1.9/v1.10 the swept 'stop_loss' column holds trail_pct/trail_buy_pct,
    # not the real fixed SL — that comes from the node's fixed_sl column instead.
    if strategies.uses_fixed_sl(pos['strategy']):
        real_sl_pct = pos.get('fixed_sl') or 0.0
        trail_pct   = (pos.get('trail_sell_pct') or 3.0) / 100.0
    else:
        real_sl_pct = pos['stop_loss']
        trail_pct   = 0.03
    tp_pct     = db._tp_or_arm_pct(pos)
    strat      = strategy_cls(window=pos['window'], trail_pct=trail_pct)
    old_state  = pos.get('trail_state', {})
    reason, price, new_state = strat.check_exit({
        'current_price':     current_price,
        # Real bar Low/High when this call represents an actual closed hourly bar;
        # otherwise current_price is the best available proxy for a mid-bar poll.
        'low':               low if low is not None else current_price,
        'high':              high if high is not None else current_price,
        'entry_price':       pos['entry_price'],
        'take_profit':       tp_pct / 100.0,
        'stop_loss':         real_sl_pct / 100.0,
        'max_hours_to_hold': pos['max_hold_hours'],
        'hours_held':        hours_held,
        'at_bar_close':      at_bar_close,
        'state':             old_state,
    })
    just_activated_trailing = bool(new_state.get('trailing')) and not old_state.get('trailing')
    if reason in ('WIN', 'LOSS'):
        reason = 'TRAIL'
    if new_state != old_state:
        db.update_position_trail_state(pos['id'], new_state)
    return reason, price, just_activated_trailing
