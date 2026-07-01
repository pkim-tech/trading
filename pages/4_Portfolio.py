import sqlite3
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from pathlib import Path
import numpy as np
import strategies
from backtester import run_backtest

DB_PATH   = "./cache/trading_universe.db"
CACHE_DIR = Path("./cache")

RESULT_COLORS = {
    'WIN':   '#2ecc71',
    'TWIN':  '#4c9be8',
    'LOSS':  '#e74c3c',
    'TLOSS': '#f0a500',
    'OPEN':  '#95a5a6',
}

HURST_WINDOW = 420  # 60d × 7 bars/day

st.set_page_config(layout="wide", page_title="Portfolio")
st.title("Portfolio")


@st.cache_data(ttl=60)
def load_watchlist():
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM watch_list ORDER BY ticker").fetchall()]


@st.cache_data(ttl=3600)
def load_hourly(ticker):
    p = CACHE_DIR / f"{ticker}_1h.csv"
    if not p.exists():
        return None
    return pd.read_csv(p, index_col=0, parse_dates=True).sort_index()


@st.cache_data(ttl=3600)
def load_price_series(ticker):
    p = CACHE_DIR / f"{ticker}_1h.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, index_col=0, parse_dates=True).sort_index()
    close_col = 'Adj Close' if 'Adj Close' in df.columns else 'Close'
    return df[close_col].dropna()


def load_spy():
    return load_price_series('SPY')


@st.cache_data(ttl=300)
def run_node_backtest(ticker, strategy_name, window, tp, sl, hold, zt):
    df_h = load_hourly(ticker)
    if df_h is None:
        return []
    close_col = 'Adj Close' if 'Adj Close' in df_h.columns else 'Close'
    df_daily = df_h.resample('D').last().dropna(subset=[close_col])
    strat = getattr(strategies, strategy_name)(window=window, z_score_threshold=float(zt))
    df_ind = strat.generate_daily_indicators(df_daily)
    return run_backtest(df_h, df_ind, ticker,
                        take_profit=tp / 100.0, stop_loss=sl / 100.0,
                        max_hours_to_hold=hold, z_score_threshold=float(zt))


@st.cache_data(ttl=300)
def get_hurst_series(ticker):
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT timestamp, hurst FROM hurst_cache WHERE ticker = ? ORDER BY timestamp",
            (ticker,)
        ).fetchall()
    if not rows:
        return None
    return pd.Series([r[1] for r in rows], index=pd.to_datetime([r[0] for r in rows]))


@st.cache_data(ttl=300)
def get_adf_series(ticker):
    from statsmodels.tsa.stattools import adfuller
    df_h = load_hourly(ticker)
    if df_h is None:
        return None
    close_col = 'Adj Close' if 'Adj Close' in df_h.columns else 'Close'
    close = df_h[close_col].dropna()
    window, step = HURST_WINDOW, 48
    results = []
    for i in range(window, len(close), step):
        p = adfuller(close.iloc[i - window:i], maxlag=1, autolag=None)[1]
        results.append((close.index[i - 1], p))
    if not results:
        return None
    times, pvals = zip(*results)
    return pd.Series(list(pvals), index=pd.to_datetime(list(times)))


def annotate_trades(df):
    tickers = df['Ticker'].unique()
    hurst_map = {t: get_hurst_series(t) for t in tickers}
    adf_map   = {t: get_adf_series(t)   for t in tickers}

    def lookup(series_map, ticker, ts):
        s = series_map.get(ticker)
        if s is None or s.empty:
            return np.nan
        val = s.asof(ts)
        return float(val) if pd.notna(val) else np.nan

    df = df.copy()
    df['Hurst'] = [lookup(hurst_map, r['Ticker'], r['Entry Time']) for _, r in df.iterrows()]
    df['ADF p'] = [lookup(adf_map,   r['Ticker'], r['Entry Time']) for _, r in df.iterrows()]
    return df


# ── Load data ────────────────────────────────────────────────────────────────

watchlist = load_watchlist()
if not watchlist:
    st.info("Watch list is empty. Add nodes with: .venv/bin/python3 active_signals.py add")
    st.stop()

all_trades = []
with st.spinner("Running backtests..."):
    for node in watchlist:
        trades = run_node_backtest(
            node['ticker'], node['strategy'], node['window'],
            node['take_profit'], node['stop_loss'], node['max_hold_hours'],
            node.get('z_score_threshold', 2.0)
        )
        label = f"{node['ticker']} w={node['window']}"
        for t in trades:
            t['Node'] = label
        all_trades.extend(trades)

if not all_trades:
    st.info("No trades found.")
    st.stop()

df_all = pd.DataFrame(all_trades)
df_all['Entry Time'] = pd.to_datetime(df_all['Entry Time'])
df_all['Exit Time']  = pd.to_datetime(df_all['Exit Time'])
df_all['Return %']   = (df_all['Return'] * 100).round(1)

# with st.spinner("Computing Hurst / ADF at entry..."):
#     df_all = annotate_trades(df_all)
df_all['Hurst'] = np.nan
df_all['ADF p'] = np.nan

spy   = load_spy()
tqqq  = load_price_series('TQQQ')
nodes = sorted(df_all['Node'].unique().tolist())

# ── Watchlist table ───────────────────────────────────────────────────────

with st.expander("Watchlist", expanded=True):
    wl_df = pd.DataFrame(watchlist)[['ticker', 'strategy', 'version', 'window', 'z_score_threshold', 'take_profit', 'stop_loss', 'max_hold_hours', 'label', 'added_at']]
    wl_df.columns = ['Ticker', 'Strategy', 'Version', 'Window', 'Z', 'TP%', 'SL%', 'Hold h', 'Label', 'Added']
    st.dataframe(wl_df, use_container_width=True, hide_index=True)


# ── Chart rendering (fragment = sliders only re-run this) ─────────────────

@st.fragment
def render(df_all, spy, tqqq, nodes, watchlist):
    h_has_data   = df_all['Hurst'].notna().any()
    adf_has_data = df_all['ADF p'].notna().any()

    all_tickers = sorted(df_all['Ticker'].unique().tolist())
    selected = st.multiselect("Tickers", all_tickers, default=all_tickers)
    if not selected:
        st.warning("No tickers selected.")
        return

    # c1, c2 = st.columns(2)
    # with c1:
    #     max_hurst = st.slider("Max Hurst at entry", 0.3, 0.8, 0.8, 0.01,
    #                           disabled=not h_has_data)
    # with c2:
    #     max_adf = st.slider("Max ADF p at entry", 0.0, 1.0, 1.0, 0.01,
    #                         disabled=not adf_has_data)

    df = df_all[df_all['Ticker'].isin(selected)].copy()

    if df.empty:
        st.warning("No trades pass current filters.")
        return

    # Concurrent positions
    events = []
    for _, row in df.iterrows():
        events.append((row['Entry Time'], 1))
        events.append((row['Exit Time'], -1))
    events.sort(key=lambda x: x[0])
    conc_times, conc_counts = [], []
    cnt = 0
    for t, delta in events:
        conc_times.append(t)
        cnt += delta
        conc_counts.append(cnt)
    max_concurrent = max(conc_counts)

    t_min, t_max = df['Entry Time'].min(), df['Exit Time'].max()
    spy_plot = spy[(spy.index >= t_min) & (spy.index <= t_max)] if spy is not None else None

    active_nodes = sorted(df['Node'].unique().tolist())
    node_y = {n: i for i, n in enumerate(active_nodes)}
    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        row_heights=[0.55, 0.25, 0.20],
        vertical_spacing=0.03,
        subplot_titles=("Trades", "SPY / TQQQ (normalized)", f"Concurrent  (max {max_concurrent})"),
    )

    seen = set()
    for _, row in df.sort_values('Entry Time').iterrows():
        result = row['Result']
        h_str  = f"{row['Hurst']:.3f}" if pd.notna(row['Hurst']) else "n/a"
        adf_str = f"{row['ADF p']:.3f}" if pd.notna(row['ADF p']) else "n/a"
        fig.add_trace(go.Scatter(
            x=[row['Entry Time'], row['Exit Time']],
            y=[node_y[row['Node']], node_y[row['Node']]],
            mode='lines',
            name=result,
            legendgroup=result,
            showlegend=(result not in seen),
            line=dict(color=RESULT_COLORS[result], width=18),
            customdata=[[row['Return %'], row['hours_held'],
                         row['Entry Price'], row['Exit Price'],
                         h_str, adf_str]] * 2,
            hovertemplate=(
                f"<b>{row['Node']}</b><br>"
                "Return: %{customdata[0]}%<br>"
                "Hours: %{customdata[1]}<br>"
                "Entry: $%{customdata[2]:.4f}<br>"
                "Exit: $%{customdata[3]:.4f}<br>"
                "Hurst: %{customdata[4]}<br>"
                "ADF p: %{customdata[5]}"
                "<extra></extra>"
            ),
        ), row=1, col=1)
        seen.add(result)

    n_nodes = len(active_nodes)
    fig.update_yaxes(
        tickmode='array',
        tickvals=list(node_y.values()),
        ticktext=list(node_y.keys()),
        range=[-0.5, n_nodes - 0.5],
        row=1, col=1,
    )

    if spy_plot is not None and not spy_plot.empty:
        spy_norm = spy_plot / spy_plot.iloc[0] * 100
        fig.add_trace(go.Scatter(
            x=spy_norm.index, y=spy_norm.values,
            mode='lines', name='SPY',
            line=dict(color='#f0a500', width=1),
        ), row=2, col=1)

    if tqqq is not None:
        tqqq_plot = tqqq[(tqqq.index >= t_min) & (tqqq.index <= t_max)]
        if not tqqq_plot.empty:
            tqqq_norm = tqqq_plot / tqqq_plot.iloc[0] * 100
            fig.add_trace(go.Scatter(
                x=tqqq_norm.index, y=tqqq_norm.values,
                mode='lines', name='TQQQ',
                line=dict(color='#9b59b6', width=1),
            ), row=2, col=1)

    fig.add_trace(go.Scatter(
        x=conc_times, y=conc_counts,
        mode='lines',
        line=dict(shape='hv', color='#4c9be8'),
        fill='tozeroy',
        fillcolor='rgba(76, 155, 232, 0.15)',
        showlegend=False,
    ), row=3, col=1)
    fig.update_yaxes(dtick=1, rangemode='nonnegative', row=3, col=1)

    fig.update_layout(
        height=max(550, n_nodes * 55 + 380),
        margin=dict(l=0, r=0, t=40, b=0),
        hovermode='x',
    )
    st.plotly_chart(fig, use_container_width=True)

    # Overall metrics
    wins     = df['Result'].isin(['WIN', 'TWIN']).sum()
    win_rate = wins / len(df) * 100
    avg_ret  = df['Return %'].mean()
    avg_win  = df.loc[df['Result'].isin(['WIN', 'TWIN']), 'Return %'].mean()
    avg_loss = df.loc[df['Result'].isin(['LOSS', 'TLOSS']), 'Return %'].mean()
    avg_hold = df['hours_held'].mean()

    avg_ret_all = df_all['Return %'].mean()
    avg_ret_delta = avg_ret - avg_ret_all

    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    c1.metric("Trades",         f"{len(df)} / {len(df_all)}")
    c2.metric("Win Rate",       f"{win_rate:.1f}%")
    c3.metric("Avg Return",     f"{avg_ret:+.1f}%",
              delta=f"{avg_ret_delta:+.1f}% vs unfiltered")
    c4.metric("Avg Win",        f"{avg_win:+.1f}%")
    c5.metric("Avg Loss",       f"{avg_loss:+.1f}%")
    c6.metric("Avg Hold",       f"{avg_hold:.0f}h")
    c7.metric("Max Concurrent", str(max_concurrent))

    # Per-node table: max signal (all trades) vs filtered
    base = df_all.groupby('Node')['Return %'].agg(
        Max_Trades='count',
        Max_Avg=lambda x: round(x.mean(), 1),
        Max_Total=lambda x: round(x.sum(), 1),
    )
    filt = df.groupby('Node')['Return %'].agg(
        Trades='count',
        Avg_Return=lambda x: round(x.mean(), 1),
        Total_Return=lambda x: round(x.sum(), 1),
    )
    summary = base.join(filt, how='left').rename(columns={
        'Max_Trades':  'All Trades',
        'Max_Avg':     'All Avg %',
        'Max_Total':   'All Total %',
        'Trades':      'Filt Trades',
        'Avg_Return':  'Filt Avg %',
        'Total_Return':'Filt Total %',
    })
    st.dataframe(summary, use_container_width=True)


render(df_all, spy, tqqq, nodes, watchlist)
