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

_LIVE_WINDOWS = [((10, 25), (10, 40)), ((15, 25), (15, 40))]


def _in_live_window(ts):
    hm = (ts.hour, ts.minute)
    return any(w0 <= hm <= w1 for w0, w1 in _LIVE_WINDOWS)


def print_status(watchlist_id=None):
    watchlist_id = watchlist_id or a.get_active_watchlist_id()
    wl = a.get_watchlist(watchlist_id)
    pending_by_ticker = {p['ticker']: p for p in a.get_pending_buys()}

    rows = []
    for n in wl:
        sig = a.compute_buy_signal(n)
        if sig is None:
            continue
        cur = sig['current_price']
        pending = pending_by_ticker.get(n['ticker'])
        if pending is not None:
            # z-score already crossed and a trailing-buy order is active/pending --
            # the number worth watching now is the bounce-above-running-low trigger,
            # not the (already-cleared, often much farther away) initial z trigger.
            _, tb_trigger = a._trailing_buy_status(pending)
            trigger = tb_trigger if tb_trigger is not None else sig['lower_band']
            phase = 'trail-buy'
        else:
            trigger = sig['lower_band']
            phase = 'z-cross'
        pct = (cur - trigger) / trigger * 100
        rows.append((n['ticker'], phase, trigger, cur, pct, n.get('trail_buy_pct'), n.get('account'), n.get('mode')))
    rows.sort(key=lambda r: r[4])

    print(f"watchlist_id={watchlist_id}\n")
    print(f"{'Ticker':<6} {'Phase':>9} {'Trigger':>10} {'Current':>10} {'%':>8} {'TrailBuy%':>10} {'Account':>10} {'Mode':>10}")
    for t, phase, trig, cur, pct, tb, acc, mode in rows:
        print(f"{t:<6} {phase:>9} {trig:>10.2f} {cur:>10.2f} {pct:>7.2f}% {str(tb):>10} {str(acc):>10} {str(mode):>10}")


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
