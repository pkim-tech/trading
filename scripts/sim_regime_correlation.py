"""Tests whether trade losses correlate with SPY-trend/VIX regime -- the
backlogged-but-never-run "SPY trend / VIX level as entry filter" idea
(docs/deep_backlog.md, raised before 2026-07-05, "next research direction
after ruling out Hurst/ADF"). Tags every real historical v5 trade by SPY's
position vs its 200d SMA and VIX level at entry time, then checks (1) whether
losses cluster in "bad regime" (SPY downtrend / high VIX) buckets, and (2)
the user's specific counter-hypothesis: do the LARGEST WINS disproportionately
happen in those same bad-regime conditions, which would make a naive regime
filter an antipattern for a mean-reversion strategy (filtering out entries
right before the bounce that produces the big win).

Usage: .venv/bin/python scripts/sim_regime_correlation.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import yfinance as yf

from scripts.sim_pilot_notional_drag import _load_node, _real_trades

WATCH_IDS = [86, 87, 88, 89, 90, 91, 92, 93, 94, 95]  # all 10 real v5 nodes
VIX_HIGH_THRESHOLD = 25.0


def _spy_regime_series():
    df = pd.read_csv('cache/research/SPY_1h.csv', index_col=0, parse_dates=True)
    daily = df['Close'].resample('D').last().dropna()
    sma200 = daily.rolling(200).mean()
    return daily, sma200


def _vix_series():
    vix = yf.download('^VIX', start='2022-01-01', progress=False)
    close = vix['Close']
    if hasattr(close, 'columns'):
        close = close.iloc[:, 0]
    return close


def _regime_at(date, spy_daily, spy_sma200, vix_close):
    d = pd.Timestamp(date).normalize()
    spy_dates = spy_daily.index[spy_daily.index <= d]
    vix_dates = vix_close.index[vix_close.index <= d]
    if len(spy_dates) == 0 or len(vix_dates) == 0:
        return None, None
    spy_px = spy_daily.loc[spy_dates[-1]]
    sma = spy_sma200.get(spy_dates[-1])
    if pd.isna(sma):
        spy_trend = None
    else:
        spy_trend = 'up' if spy_px >= sma else 'down'
    vix_level = vix_close.loc[vix_dates[-1]]
    vix_bucket = 'high' if vix_level >= VIX_HIGH_THRESHOLD else 'low'
    return spy_trend, vix_bucket


def main():
    spy_daily, spy_sma200 = _spy_regime_series()
    vix_close = _vix_series()

    all_tagged = []
    for wid in WATCH_IDS:
        node = _load_node(wid)
        trades = _real_trades(node)
        for t in trades:
            spy_trend, vix_bucket = _regime_at(t['Entry Time'], spy_daily, spy_sma200, vix_close)
            if spy_trend is None:
                continue
            all_tagged.append(dict(
                ticker=node['ticker'], entry_time=t['Entry Time'], ret=t['Return'],
                is_win=t['Result'] in ('WIN', 'TWIN'), spy_trend=spy_trend, vix_bucket=vix_bucket,
            ))

    df = pd.DataFrame(all_tagged)
    print(f"Total tagged trades: {len(df)} across {df['ticker'].nunique()} tickers\n")

    print("=== Win rate / mean return by regime bucket ===")
    grp = df.groupby(['spy_trend', 'vix_bucket']).agg(
        n=('ret', 'size'), win_rate=('is_win', 'mean'), mean_ret=('ret', 'mean'), sum_ret=('ret', 'sum'))
    grp['win_rate'] = (grp['win_rate'] * 100).round(1)
    grp['mean_ret'] = (grp['mean_ret'] * 100).round(2)
    grp['sum_ret'] = (grp['sum_ret'] * 100).round(1)
    print(grp)

    print("\n=== Top 20 largest wins -- what regime were they entered in? ===")
    top20 = df.nlargest(20, 'ret')[['ticker', 'entry_time', 'ret', 'spy_trend', 'vix_bucket']]
    top20['ret'] = (top20['ret'] * 100).round(1)
    print(top20.to_string(index=False))

    print("\n=== Regime distribution: top 20 wins vs. all trades ===")
    base_dist = df.groupby(['spy_trend', 'vix_bucket']).size() / len(df) * 100
    top_dist = df.nlargest(20, 'ret').groupby(['spy_trend', 'vix_bucket']).size() / 20 * 100
    comp = pd.DataFrame({'all_trades_%': base_dist.round(1), 'top20_wins_%': top_dist.round(1)}).fillna(0.0)
    print(comp)


if __name__ == '__main__':
    main()
