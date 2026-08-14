"""
Signal computation: cached price loading, buy-signal evaluation (with the
SMA/Std indicator cache), and sell-condition checking.
"""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

import strategies
import signals_config as cfg
import signals_db as db
from signals_helpers import (
    detect_price_discontinuity, nearest_split_factor,
    already_alerted_corp_action, mark_corp_action_alerted, log_poll,
)
from signals_blocks import _post_message


# ---------------------------------------------------------------------------
# Price data
# ---------------------------------------------------------------------------

def _load_cache(ticker):
    path = cfg.RESEARCH_DIR / f"{ticker}_1h.csv"
    if not path.exists():
        return None, None
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    df_daily = df.resample('D').last().dropna()
    return df, df_daily


def current_bar_time(ticker):
    """Last closed hourly bar's timestamp (a pandas.Timestamp, always tz-naive US/Eastern
    -- see _load_cache's tz_localize(None)) for `ticker`, or None if no cached data exists.

    Added 2026-08-14 for the same-bar re-entry cooldown fix's real gap (found by cold
    Opus review): a real exit that closes via a broker-side fill poll rather than our own
    bar-close check_sell_condition call (check_sl_order_fills, a manual "Exited" Slack
    confirmation in signals_handlers.py) has no bar already in hand to stash into
    trail_state['exit_decision_bar'] the way active_signals.py's own check_sell_condition
    call sites do -- this derives the same reference those sites use (df_hourly.index[-1])
    so those paths can pass exit_bar_time to close_position() directly instead of leaving
    it NULL and silently disarming the cooldown for that node."""
    df_hourly, _ = _load_cache(ticker)
    if df_hourly is None or df_hourly.empty:
        return None
    return df_hourly.index[-1]


_STALE_PRICE_MAX_AGE = timedelta(minutes=90)


def _current_price(ticker):
    """Returns (None, None) if the cache's last row is older than
    _STALE_PRICE_MAX_AGE -- a real gap found 2026-07-22 (HIBL paper trade): a
    poll landing between market open and that ticker's first same-day
    refresh would otherwise hand back yesterday's stale close as if it were
    live, letting a trailing-buy bounce-fill (or a real SL/trailing-sell
    check) act on a price that was never actually available at that moment.
    Originally a same-calendar-day check gated to weekday + post-9:30am
    only; that missed two real cases: an overnight/weekend poll (any day of
    week, any time) silently replaying a prior-day close as current, AND a
    same-day-but-hours-old bar (e.g. the day's last 15:30 bar, checked at
    11pm the same day, still passes a date-only check) -- the exact shape of
    the GDXU overnight "current $81.92" alert, 2026-07-28, which a same-day
    check alone doesn't actually catch. Widened 2026-07-31 to a straight age
    check instead, catching both. Bars are hourly and the daemon polls every
    ~5min, so 90min gives room for minor collector lag without masking a
    real staleness gap. Off-hours callers already handle a None return by
    skipping that poll's exit check (see alert_stale_price_exit_suppressed,
    itself gated to market hours only so this doesn't spam Slack all night)."""
    df, _ = _load_cache(ticker)
    if df is None:
        return None, None
    prices = df['Close'].dropna()
    if prices.empty:
        return None, None
    last_ts = df.index[-1]
    now = datetime.now()
    if now - last_ts > _STALE_PRICE_MAX_AGE:
        log_poll(f"{ticker} _current_price STALE bar={last_ts} now={now:%Y-%m-%d %H:%M:%S} -> skipped")
        return None, None
    price = float(prices.iloc[-1])
    log_poll(f"{ticker} _current_price bar={last_ts} price={price:.4f}")
    return price, last_ts


def _live_tick_price(ticker, fallback):
    """Real live 1-minute tick fetch (yfinance) -- unlike _current_price's cached
    hourly-bar snapshot (only refreshed whenever the background data-collector
    process next runs, which can lag real price movement by tens of minutes
    during a fast-moving open), this hits the market directly. Falls back to
    `fallback` (typically _current_price's cached value) on any fetch failure or
    empty result, same behavior compute_buy_signal already relied on inline
    before this was extracted 2026-08-12.

    Found live: SOXL's 2026-07-27 paper trailing-buy fill missed a real
    gap-up-then-crash entirely because update_paper_buys tracked price via
    _current_price alone -- it returned the identical $140.08 snapshot for 29
    straight minutes right after market open while the daemon's own poll loop
    kept running normally every ~34s, then jumped straight to $132.53 once the
    hourly-bar cache finally refreshed, well past where a live tick (or a real
    broker's continuously-live trailing order) would have caught the trigger
    crossing. compute_buy_signal's live-tick fetch was never affected (it's a
    different price path) -- only price-tracking loops that used
    _current_price alone during a trailing-buy wait or a real-account pending
    buy (the latter fixed 2026-07-29 via schwab_client.get_current_price,
    see signals_notify.update_real_pending_buys_running_low) had this gap.
    Paper had no broker to source a real-time quote from, so this reuses the
    same yfinance 1-minute path compute_buy_signal already trusts, instead of
    depending on schwab_client (which paper deliberately never calls)."""
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            hist = ex.submit(
                lambda: yf.Ticker(ticker).history(period='1d', interval='1m', prepost=True)
            ).result(timeout=10)
        return float(hist['Close'].iloc[-1]) if not hist.empty else fallback
    except Exception:
        return fallback


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
    # df_daily_prior (already sliced to < today, same frame the indicators are
    # computed from), not the unsliced df_daily -- the latter's last row can be
    # today's own partial/in-progress bar (live) or a day at/after `as_of` (replay),
    # either way not what "previous close" means. Found 2026-07-21 via Part 4's
    # backtest-replay verification script, which replays historical as_of dates and
    # was getting today's real (years-later) close back as "prev_close", spuriously
    # tripping the corporate-action discontinuity guard on every replayed signal.
    daily_closes = df_daily_prior['Close'].dropna()
    prev_close = float(daily_closes.iloc[-1]) if not daily_closes.empty else close_series.iloc[-1]
    if node.get('paper_role') == 'daily_sync':
        # The whole point of a daily-track node is to isolate price-source
        # timing as the only variable against a backtest replay -- it must never
        # take a live intraminute tick (nor any caller-supplied price_override,
        # which exists for a different purpose, the pinned real-broker-price
        # path) and always use the same fixed hourly-bar Close the backtest
        # kernel itself trades off of. See docs/design.md's "Two-account paper
        # trading" section.
        #
        # Only a genuine live call (no as_of/df_hourly_override -- a historical
        # replay already hands in whatever slice it means to use, as-is) needs
        # the two guards below (found by Opus review, 2026-08-05: both were
        # unconditional in the first version, silently breaking every replay
        # caller -- scripts/verify_live_parity.py, watchlist_status.py -- for a
        # daily_sync node).
        is_live_call = as_of is None and df_hourly_override is None
        if is_live_call:
            now = datetime.now()
            # yfinance's currently-updating hourly bar (this wall-clock hour) is
            # still forming -- data_manager.fetch_live_data_smart never trims it,
            # so trusting it as "the last CLOSED bar" during the back half of a
            # signal window would make daily-track effectively a live tick again,
            # defeating the whole point.
            if (close_series.index[-1].hour == now.hour
                    and close_series.index[-1].date() == now.date() and len(close_series) > 1):
                close_series = close_series.iloc[:-1]
                last_bar = close_series.index[-1]
            # Same staleness guard as _current_price's live-tick path (2026-07-22
            # HIBL gap) -- an unrefreshed cache would otherwise silently hand back
            # an arbitrarily old Close as if it were today's bar.
            if now - last_bar > _STALE_PRICE_MAX_AGE:
                return None
        current_price = float(close_series.iloc[-1])
    elif price_override is not None:
        current_price = price_override
    else:
        current_price = _live_tick_price(ticker, close_series.iloc[-1])
    discontinuity = detect_price_discontinuity(current_price, prev_close)
    if discontinuity:
        print(f"⚠️ Possible corporate action for {ticker}: prev_close={prev_close:.4f} "
              f"current={current_price:.4f} ratio={discontinuity:.2f} -- freezing new signals")
        # prev_close/SMA/Std are all computed against pre-event history, so any
        # signal here would be comparing today's real price to a stale baseline
        # (exactly what let KORU's split slip through undetected 2026-07-15).
        return None
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


def check_sell_condition(pos, current_price, now, at_bar_close=True, low=None, high=None, open_price=None, df_hourly=None, paper=False):
    strategy_cls = getattr(strategies, pos['strategy'], None)
    if strategy_cls is None:
        return None, None, False
    discontinuity = detect_price_discontinuity(current_price, pos['entry_price'])
    if discontinuity:
        ticker = pos['ticker']
        print(f"⚠️ Possible corporate action for {ticker}: entry_price={pos['entry_price']:.4f} "
              f"current={current_price:.4f} ratio={discontinuity:.2f} -- freezing SL/arm checks")
        # Paper positions skip the interactive alert -- "Apply Correction"'s handler
        # assumes a real open_positions id, and this is scoring infrastructure, not
        # real capital, so a plain freeze (no self-heal button) is an acceptable gap.
        if not paper and not already_alerted_corp_action(ticker):
            factor = nearest_split_factor(discontinuity)
            proposed_entry = pos['entry_price'] / factor
            value = json.dumps({"position_id": pos['id'], "ticker": ticker, "proposed_entry_price": proposed_entry})
            _post_message(
                f"⚠️ Possible corporate action — {ticker}",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": (
                        f"⚠️ *{ticker}* — possible corporate action (ratio≈{discontinuity:.2f}, "
                        f"nearest factor {factor}).\n"
                        f"Recorded entry: `${pos['entry_price']:.4f}`  |  Current: `${current_price:.4f}`\n"
                        f"Proposed corrected entry: `${proposed_entry:.4f}`\n"
                        f"SL/arm checks are frozen for this ticker until corrected."
                    )}},
                    {"type": "actions", "elements": [
                        {"type": "button", "text": {"type": "plain_text", "text": "Apply Correction"},
                         "style": "primary", "action_id": "apply_corp_action_correction", "value": value},
                    ]},
                ],
            )
            mark_corp_action_alerted(ticker)
        # entry_price is untrustworthy against an unadjusted corporate action --
        # a real SL check here would be mechanically true regardless of actual
        # performance (exactly the false-SL scenario found live with KORU
        # 2026-07-15). No exit signal until a human confirms/re-bases the position.
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
        # Real bar Open/Low/High when this call represents an actual closed hourly
        # bar; otherwise current_price is the best available proxy for a mid-bar
        # poll. 'open' lets check_exit apply the same gap-through-trigger fill
        # check the backtest kernel uses on SL/trailing-stop (2026-07-20 kernel
        # fix, see docs/backlog_cache.md) -- without it, a live/paper exit that
        # gapped past its trigger overnight would report the stale theoretical
        # stop/trail price instead of the real, worse fill.
        'open':              open_price if open_price is not None else current_price,
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
        # 2026-08-01: a genuine trail-stop breach and hold-time expiring while
        # armed used to both collapse to 'TRAIL' unconditionally -- correct
        # for the live order-routing logic (which always also checked the
        # separate exit_forced_by_hold_time flag, never trusted the reason
        # string alone), but wrong for every human-facing consumer (Slack
        # messages, trade_log), which had no way to tell a real trail breach
        # from a timeout without reading trail_state directly. Now reports
        # 'TIME' for the hold-time-forced case -- matches what actually
        # happened, and every reason=='TRAIL' check in signals_notify.py's
        # order-routing already reads hold_time_forced alongside it, so this
        # doesn't change any real-money control flow (see the matching
        # comments there, updated in the same change).
        reason = 'TIME' if new_state.get('exit_forced_by_hold_time') else 'TRAIL'
    if new_state != old_state:
        db.update_position_trail_state(pos['id'], new_state, paper=paper)
    return reason, price, just_activated_trailing
