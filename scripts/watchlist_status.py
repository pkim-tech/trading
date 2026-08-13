"""Print the active watchlist's live nodes with trigger distance.

Usage:
  python scripts/watchlist_status.py [watchlist_id]
  python scripts/watchlist_status.py history TICKER [num_bars] [watchlist_id]

Defaults to the currently active watchlist (watchlists.is_active=1).

`history` mode calls the real compute_buy_signal() once per bar, with data
truncated as of that bar -- no separate SMA/Std reimplementation -- to check
whether a ticker's trigger was active on each of the last N hourly bars.
Needed because the daemon's own log can go dark (e.g. WSL sleep) without
that meaning the trigger wasn't hit -- this recomputes from cached price
data directly, independent of whether the daemon was alive at the time.
7 bars = 1 full trading day (6.5hr session); pass a larger N for more days.
Each bar is flagged if it falls in one of the two windows active_signals.py
actually checks live (10:25-10:40 AM / 15:25-15:40 PM ET) -- the daemon
never alerts on the other bar closes even when it's running fine.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import active_signals as a
import signals_db as db
from scripts.export_trades import load_hourly
from scripts.drought_overlay_sweep import get_ivol_series, _entry_vol_pctile

_SMA_CACHE = {}
_IVOL_CACHE = {}


def _sma50(ticker):
    """Daily-close 50-day SMA, cached per run -- informational only (added
    2026-08-07, per the KORU upswing-watch idea: no alert/gate logic here,
    just surfacing the number so a real trend-continuation check can be
    manually judged at a glance)."""
    if ticker not in _SMA_CACHE:
        try:
            daily = load_hourly(ticker)['Close'].resample('D').last().dropna()
            _SMA_CACHE[ticker] = daily.rolling(50).mean().iloc[-1] if len(daily) >= 50 else None
        except Exception:
            _SMA_CACHE[ticker] = None
    return _SMA_CACHE[ticker]


def _current_vol_pctile(ticker):
    """Latest intraday-vol percentile reading against the ticker's own full
    history (same measure validated throughout the 2026-08-07 drought-overlay
    work) -- informational only, completes the KORU upswing-watch idea's
    second condition (SMA50 above = trend, this = calm) alongside it."""
    if ticker not in _IVOL_CACHE:
        try:
            ivol = get_ivol_series(ticker)
            _IVOL_CACHE[ticker] = _entry_vol_pctile(ivol.index[-1], ivol)
        except Exception:
            _IVOL_CACHE[ticker] = None
    return _IVOL_CACHE[ticker]

_LIVE_WINDOWS = [((10, 25), (10, 40)), ((15, 25), (15, 40))]


def _in_live_window(ts):
    hm = (ts.hour, ts.minute)
    return any(w0 <= hm <= w1 for w0, w1 in _LIVE_WINDOWS)


def print_status(watchlist_id=None):
    watchlist_id = watchlist_id or a.get_active_watchlist_id()
    wl = a.get_watchlist(watchlist_id)

    rows = []
    for n in wl:
        sig = a.compute_buy_signal(n)
        if sig is None:
            continue
        cur = sig['current_price']
        # get_real_position_state (added 2026-08-05) is node-scoped -- the prior
        # ticker-keyed pending_by_ticker dict silently misattributed a pending
        # buy to whichever node happened to match the ticker string when 2+
        # nodes share a ticker (real risk now that most tickers have a
        # live-track + daily-track paper pair, plus real live/research pairs
        # like GDXU/DPST). Real pending_buy for a live node, paper_pending_buy
        # for a research node -- a node is never both.
        state = db.get_real_position_state(n['id'])
        # A resting REAL pending buy is real capital committed at the broker -- must
        # never be visually indistinguishable from a paper one (found by paired Opus
        # review, 2026-08-05: the prior version's ticker-keyed dict couldn't tell
        # them apart at all). Real takes precedence when (in theory) both existed.
        is_paper_pending = False
        pending = state['pending_buy']
        if pending is None:
            pending = state['paper_pending_buy']
            is_paper_pending = pending is not None
        if pending is not None:
            # z-score already crossed and a trailing-buy order is active/pending --
            # the number worth watching now is the bounce-above-running-low trigger,
            # not the (already-cleared, often much farther away) initial z trigger.
            _, tb_trigger = a._trailing_buy_status(pending)
            trigger = tb_trigger if tb_trigger is not None else sig['lower_band']
            phase = 'trail-buy(paper)' if is_paper_pending else 'trail-buy'
        else:
            trigger = sig['lower_band']
            phase = 'z-cross'
        pct = (cur - trigger) / trigger * 100
        # role: daily-track clones (paper_role='daily_sync') would otherwise print as
        # a byte-identical duplicate row of their live-track sibling -- same ticker/
        # account/mode, no way to tell them apart (found by paired Opus review,
        # 2026-08-05). id disambiguates unambiguously; role gives a human-readable tag.
        role = n.get('paper_role') or '-'
        sma50 = _sma50(n['ticker'])
        above_sma = (cur >= sma50) if sma50 is not None else None
        vol_pctile = _current_vol_pctile(n['ticker'])

        # Overlay flags are static config (2026-08-08 addition) -- previously
        # invisible here, had to be checked by hand-querying watch_list.
        # 'S' (force_same_day_block) added 2026-08-13 -- found completely
        # untracked anywhere despite being live on JNUG (wl_id=205) and
        # materially changing real order-placement behavior. See
        # docs/deep_backlog.md's 2026-08-13 entry.
        is_paper = n.get('state') == 'paper'
        ovl = (('D' if n.get('drought_overlay_enabled') else '') +
               ('A' if n.get('addon_enabled') else '') +
               ('S' if n.get('force_same_day_block') else ''))
        ovl = ovl or '-'
        # Open overlay positions fold into Phase (not a separate column) --
        # this is the thing actually worth noticing on a scan, and stays quiet
        # for the common case where nothing overlay-related is live.
        if n.get('drought_overlay_enabled'):
            dpos = db.get_drought_overlay_position(n['id'], paper=is_paper)
            if dpos is not None:
                phase = f"drought-held({phase})"
        if n.get('addon_enabled'):
            aleg = db.get_open_addon_leg_by_wl_id(n['id'], paper=is_paper)
            if aleg is not None:
                phase = f"{phase}+addon"

        rows.append((n['id'], n['ticker'], role, phase, trigger, cur, pct,
                     n.get('trail_buy_pct'), n.get('account'), n.get('state'), sma50, above_sma, vol_pctile, ovl))
    rows.sort(key=lambda r: r[6])

    print(f"watchlist_id={watchlist_id}\n")
    print(f"{'Id':>5} {'Ticker':<6} {'Role':<11} {'Phase':>20} {'Trigger':>10} {'Current':>10} {'%':>8} "
          f"{'TrailBuy%':>10} {'Account':>10} {'Mode':>10} {'SMA50':>10} {'>SMA50':>7} {'VolPctl':>8} {'Ovl':>4}")
    for wl_id, t, role, phase, trig, cur, pct, tb, acc, mode, sma50, above_sma, vol_pctile, ovl in rows:
        sma50_s = f"{sma50:.2f}" if sma50 is not None else "n/a"
        above_s = "" if above_sma is None else ("yes" if above_sma else "no")
        vol_s = f"{vol_pctile:.2f}" if vol_pctile is not None else "n/a"
        print(f"{wl_id:>5} {t:<6} {role:<11} {phase:>20} {trig:>10.2f} {cur:>10.2f} {pct:>7.2f}% "
              f"{str(tb):>10} {str(acc):>10} {str(mode):>10} {sma50_s:>10} {above_s:>7} {vol_s:>8} {ovl:>4}")


def print_history(ticker, num_bars=7, watchlist_id=None):
    watchlist_id = watchlist_id or a.get_active_watchlist_id()
    wl = a.get_watchlist(watchlist_id)
    matches = [n for n in wl if n['ticker'] == ticker]
    if not matches:
        print(f"no node for {ticker} on watchlist {watchlist_id}")
        return
    node = matches[0]

    df_hourly, df_daily = a._load_cache(ticker)
    if df_hourly is None:
        print(f"no cached data for {ticker}")
        return

    last_bars = df_hourly.tail(num_bars)
    print(f"{ticker}  node id={node['id']}  {node['strategy']} {node['version']}  "
          f"window={node['window']}  z_thresh={node.get('z_score_threshold', 2.0)}\n")
    print(f"{'Bar':<18} {'Close':>10} {'Trigger':>10} {'z':>7}  {'Signal':<6} Live-checked?")

    for ts in last_bars.index:
        end = df_hourly.index.get_loc(ts)
        df_hourly_trunc = df_hourly.iloc[:end + 1]
        bar_close = float(df_hourly.loc[ts, 'Close'])
        df_daily_trunc = df_daily[df_daily.index < ts.normalize()]

        sig = a.compute_buy_signal(
            node, price_override=bar_close,
            df_hourly_override=df_hourly_trunc, df_daily_override=df_daily_trunc,
        )
        if sig is None:
            print(f"{ts:%Y-%m-%d %H:%M}   insufficient history")
            continue
        live_flag = "  <-- live window" if _in_live_window(ts) else ""
        active_flag = "  *** SIGNAL ACTIVE ***" if sig['signal'] == 'BUY' else ""
        print(f"{ts:%Y-%m-%d %H:%M}   {sig['current_price']:>10.2f} {sig['lower_band']:>10.2f} "
              f"{sig['z_score']:>7.2f}  {sig['signal']:<6}{live_flag}{active_flag}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'history':
        ticker = sys.argv[2].upper()
        num_bars = int(sys.argv[3]) if len(sys.argv) > 3 else 7
        watchlist_id = int(sys.argv[4]) if len(sys.argv) > 4 else None
        print_history(ticker, num_bars, watchlist_id)
    else:
        watchlist_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
        print_status(watchlist_id)


if __name__ == '__main__':
    main()
