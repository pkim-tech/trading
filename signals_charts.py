"""Chart PNG generation for buy/sell Slack alerts."""
from datetime import datetime
from io import BytesIO

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

matplotlib.rcParams.update({
    'figure.facecolor':  '#1e1f22',
    'axes.facecolor':    '#1e1f22',
    'savefig.facecolor': '#1e1f22',
    'text.color':        '#dbdee1',
    'axes.labelcolor':   '#dbdee1',
    'axes.edgecolor':    '#4e5058',
    'xtick.color':       '#dbdee1',
    'ytick.color':       '#dbdee1',
    'grid.color':        '#3f4147',
    'legend.facecolor':  '#2b2d31',
    'legend.edgecolor':  '#4e5058',
    'legend.labelcolor': '#dbdee1',
})

import strategies
import signals_config as cfg
import signals_db as db
import signals_compute as compute


def _upload_chart(buf: BytesIO, filename: str, title: str):
    if not cfg.SOCKET_MODE or not cfg.SLACK_CHANNEL_ID:
        return
    try:
        cfg.bolt_app.client.files_upload_v2(
            channel=cfg.SLACK_CHANNEL_ID,
            file=buf,
            filename=filename,
            title=title,
        )
    except Exception as e:
        print(f"  [chart] upload failed: {e}")


def _chart_buy(node, sig) -> BytesIO | None:
    ticker = sig['ticker']
    window = int(node['window'])
    df_hourly, df_daily = compute._load_cache(ticker)
    if df_hourly is None:
        return None

    today        = pd.Timestamp.now().normalize()
    trading_days = pd.Series(df_hourly.index.normalize()).unique()
    cutoff       = trading_days[-30] if len(trading_days) >= 30 else trading_days[0]
    df_plot      = df_hourly[df_hourly.index.normalize() >= cutoff]['Close'].dropna()
    strat        = getattr(strategies, node['strategy'])(window=window)
    df_daily_in  = df_daily[df_daily.index < today]
    indicators  = strat.generate_daily_indicators(df_daily_in)

    z_thresh  = float(node.get('z_score_threshold', 2.0))
    sma_h     = indicators['SMA'].reindex(df_plot.index, method='ffill')
    std_h     = indicators['Std'].reindex(df_plot.index, method='ffill')
    upper_h   = sma_h + 2 * std_h
    lower_h   = sma_h - 2 * std_h
    trigger_h = sma_h - z_thresh * std_h

    # Positional x-axis (bar index, not calendar time) so weekend/overnight gaps
    # don't stretch out as flat empty segments.
    x = np.arange(len(df_plot))

    def _pos(ts):
        return df_plot.index.get_indexer([ts], method='nearest')[0]

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(x, df_plot.values, color='#4c9be8', linewidth=1, label='Price')
    ax.plot(x, sma_h.values, color='#f0a500', linewidth=1, label=f'SMA({window})')
    ax.fill_between(x, lower_h.values, upper_h.values, alpha=0.12, color='#f0a500')
    ax.plot(x, lower_h.values, color='#f0a500', linewidth=0.6, linestyle='--')
    ax.plot(x, trigger_h.values, color='#e74c3c', linewidth=1, linestyle='--', label=f'Trigger line (z={z_thresh:g})')

    last_pos = _pos(sig['last_bar'])
    ax.axvline(last_pos, color='#2ecc71', linewidth=1.5, linestyle='--', alpha=0.8)
    ax.scatter([last_pos], [sig['current_price']], color='#2ecc71', s=60, zorder=5)

    if len(df_daily_in) >= window and df_daily_in.index[-window] >= df_plot.index[0]:
        w_pos = _pos(df_daily_in.index[-window])
        ax.axvline(w_pos, color='white', linewidth=1.3, linestyle=':', alpha=0.9, label=f'w{window} start')

    ax.set_xlim(-2, len(x) + 1)

    ax.axhline(sig['prev_close'], color='#dbdee1', linewidth=1, linestyle=':', alpha=0.7,
               label=f"Close ${sig['prev_close']:.2f}")
    ax.axhline(sig['current_price'], color='#2ecc71', linewidth=1, linestyle='--', alpha=0.8,
               label=f"Current ${sig['current_price']:.2f}")
    ax.axhline(sig['lower_band'], color='#e74c3c', linewidth=1.2, linestyle='-', alpha=0.9,
               label=f"Trigger ${sig['lower_band']:.2f}")

    pct_away = (sig['current_price'] - sig['lower_band']) / sig['lower_band'] * 100
    fig.suptitle(f"{ticker}   trigger ${sig['lower_band']:.2f}  ({pct_away:+.1f}%)",
                 fontsize=15, fontweight='bold', color='#f0a500', y=0.98)
    ax.set_title(f"w{window} z{z_thresh:g} arm{db._tp_or_arm_pct(node)} sl{node['stop_loss']}",
                 fontsize=9, color='#9aa0a6')

    tick_step = max(len(x) // 10, 1)
    tick_pos  = x[::tick_step]
    ax.set_xticks(tick_pos)
    ax.set_xticklabels([df_plot.index[i].strftime('%m/%d') for i in tick_pos])

    ax.yaxis.tick_right()
    ax.yaxis.set_label_position('right')
    ax.legend(fontsize=8, loc='upper right')
    fig.tight_layout(rect=[0, 0, 1, 0.93])

    buf = BytesIO()
    fig.savefig(buf, format='png', dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf


def _chart_sell(pos, current_price) -> BytesIO | None:
    ticker = pos['ticker']
    window = int(pos['window'])
    df_hourly, df_daily = compute._load_cache(ticker)
    if df_hourly is None:
        return None

    today      = pd.Timestamp.now().normalize()
    cutoff     = df_hourly.index[-1] - pd.Timedelta(days=30)
    df_plot    = df_hourly[df_hourly.index >= cutoff]['Close'].dropna()
    strat      = getattr(strategies, pos['strategy'])(window=window)
    indicators = strat.generate_daily_indicators(df_daily[df_daily.index < today])

    sma_h   = indicators['SMA'].reindex(df_plot.index, method='ffill')
    std_h   = indicators['Std'].reindex(df_plot.index, method='ffill')
    upper_h = sma_h + 2 * std_h
    lower_h = sma_h - 2 * std_h

    ep            = pos['entry_price']
    arm_price     = ep * (1 + db._tp_or_arm_pct(pos) / 100)
    bsp           = pos.get('broker_stop_price')
    sl_price      = bsp if bsp else ep * (1 - pos['stop_loss'] / 100)
    entry_time = datetime.strptime(pos['entry_time'], '%Y-%m-%d %H:%M:%S')
    pct        = (current_price - ep) / ep * 100

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(df_plot.index, df_plot.values, color='#4c9be8', linewidth=1, label='Price')
    ax.plot(sma_h.index, sma_h.values, color='#f0a500', linewidth=1, label=f'SMA({window})')
    ax.fill_between(df_plot.index, lower_h, upper_h, alpha=0.12, color='#f0a500')
    ax.axhline(arm_price, color='#2ecc71', linewidth=1, linestyle='--', label=f'Arm ${arm_price:.2f}')
    ax.axhline(sl_price, color='#e74c3c', linewidth=1, linestyle='--', label=f'SL ${sl_price:.2f}')
    ax.axhline(ep, color='white', linewidth=0.8, linestyle=':', alpha=0.6, label=f'Entry ${ep:.2f}')
    if entry_time in df_plot.index or df_plot.index[0] <= entry_time <= df_plot.index[-1]:
        ax.axvline(entry_time, color='#9b59b6', linewidth=1.2, linestyle='--', alpha=0.7)
    ax.set_title(f"{ticker}  SELL SIGNAL  |  P&L {pct:+.2f}%  |  window={window}", fontsize=11)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    ax.legend(fontsize=8)
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format='png', dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf
