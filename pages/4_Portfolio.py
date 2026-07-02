import sqlite3
import streamlit as st
import pandas as pd
from active_signals import get_watchlists
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


st.set_page_config(layout="wide", page_title="Portfolio")
st.title("Portfolio")


@st.cache_data(ttl=60)
def load_watchlist(watchlist_id=None):
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        if watchlist_id is None:
            row = c.execute("SELECT id FROM watchlists WHERE is_active=1").fetchone()
            watchlist_id = row[0] if row else None
        if watchlist_id is None:
            return []
        return [dict(r) for r in c.execute(
            "SELECT * FROM watch_list WHERE watchlist_id=? ORDER BY ticker", (watchlist_id,)
        ).fetchall()]


@st.cache_data(ttl=86400)
def load_watchlist_metrics(params_tuple):
    rows = []
    with sqlite3.connect(DB_PATH) as c:
        for (ticker, version, window, tp, sl, hold, z) in params_tuple:
            row = c.execute("""
                SELECT alpha_vs_spy, strategy_return, trades, win_rate, asset_bh, spy_bh
                FROM backtest_cache
                WHERE ticker=? AND version=? AND window=? AND take_profit=? AND stop_loss=?
                  AND max_hold_hours=? AND z_score_threshold=? AND strategy='ZScoreBreakout'
            """, (ticker, version, window, tp, sl, hold, z)).fetchone()
            t_row = c.execute(
                "SELECT avg_vol_10d, last_price FROM tickers WHERE symbol=?", (ticker,)
            ).fetchone()
            max_notional = (t_row[0] * t_row[1] * 0.01) if t_row and t_row[0] and t_row[1] else None
            t_row2 = c.execute("SELECT stock_underlier, index_underlier FROM tickers WHERE symbol=?", (ticker,)).fetchone()
            if t_row2:
                ticker_type = "STK 🔴" if (t_row2[0] and not t_row2[1]) else "IDX"
            else:
                ticker_type = "?"
            rows.append({**({'alpha': row[0], 'ret': row[1], 'trades': row[2], 'win_rate': row[3],
                              'asset_bh': row[4], 'spy_bh': row[5]} if row else
                             {'alpha': None, 'ret': None, 'trades': None, 'win_rate': None,
                              'asset_bh': None, 'spy_bh': None}),
                          'max_notional': max_notional, 'type': ticker_type})
    return rows


@st.cache_data(ttl=60)
def load_versions():
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("SELECT DISTINCT version FROM backtest_cache ORDER BY version DESC").fetchall()
    return [r[0] for r in rows]


@st.cache_data(ttl=60)
def load_top_nodes(version, min_alpha, min_trades, z_thresholds):
    placeholders = ",".join("?" * len(z_thresholds))
    with sqlite3.connect(DB_PATH) as c:
        return pd.read_sql(f"""
            SELECT ticker, strategy, '{version}' as version, window, take_profit, stop_loss,
                   max_hold_hours, z_score_threshold, trades, win_rate,
                   strategy_return, alpha_vs_spy
            FROM backtest_cache
            WHERE version=? AND alpha_vs_spy >= ? AND trades >= ?
              AND z_score_threshold IN ({placeholders})
            ORDER BY alpha_vs_spy DESC
            LIMIT 200
        """, c, params=(version, min_alpha, min_trades, *z_thresholds))


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



# ── Node selection ────────────────────────────────────────────────────────────

all_wls     = get_watchlists()
wl_names    = [w['name'] for w in all_wls]
active_name = next((w['name'] for w in all_wls if w['is_active']), wl_names[0] if wl_names else None)
_wl_idx     = wl_names.index(active_name) if active_name in wl_names else 0
_picked_name = st.sidebar.selectbox("Watchlist", wl_names, index=_wl_idx, key="portfolio_wl_picker") if wl_names else None
picked_wl_id = next((w['id'] for w in all_wls if w['name'] == _picked_name), None) if _picked_name else None

watchlist = load_watchlist(picked_wl_id)
versions  = load_versions()

nodes_to_run = []  # list of dicts with keys: ticker, strategy, window, take_profit, stop_loss, max_hold_hours, z_score_threshold, label

with st.expander("Watchlist nodes", expanded=True):
    include_watchlist = st.toggle("Include watchlist", value=True)
    if watchlist:
        wl_options = [f"{n['ticker']} {n['version']} w={n['window']} z={n['z_score_threshold']}" for n in watchlist]
        selected_wl = st.multiselect("Select nodes", wl_options, default=wl_options, key="wl_select")
        watchlist = [n for n, lbl in zip(watchlist, wl_options) if lbl in selected_wl]
        wl_df = pd.DataFrame(watchlist)[['ticker', 'strategy', 'version', 'window', 'z_score_threshold', 'take_profit', 'stop_loss', 'max_hold_hours', 'label']]
        params = tuple((r['ticker'], r['version'], r['window'], r['take_profit'], r['stop_loss'], r['max_hold_hours'], r['z_score_threshold']) for r in watchlist)
        metrics = load_watchlist_metrics(params)
        m_df = pd.DataFrame(metrics)
        wl_df = pd.concat([wl_df.reset_index(drop=True), m_df], axis=1)
        wl_df.columns = ['Ticker', 'Strategy', 'Version', 'Window', 'Z', 'TP%', 'SL%', 'Hold h', 'Label',
                         'Alpha%', 'Return%', 'Trades', 'Win%', 'Asset B&H%', 'SPY B&H%', 'Max Notional', 'Type']
        st.dataframe(wl_df, use_container_width=True, hide_index=True, column_config={
            'Alpha%':       st.column_config.NumberColumn(format="%.1f%%"),
            'Return%':      st.column_config.NumberColumn(format="%.1f%%"),
            'Win%':         st.column_config.NumberColumn(format="%.0f%%"),
            'Asset B&H%':   st.column_config.NumberColumn(format="%.1f%%"),
            'SPY B&H%':     st.column_config.NumberColumn(format="%.1f%%"),
            'Max Notional': st.column_config.NumberColumn(format="$%.0f"),
        })
    else:
        st.caption("Watchlist is empty.")
    if include_watchlist and watchlist:
        for node in watchlist:
            nodes_to_run.append({**node, 'label': f"{node['ticker']} w={node['window']} (WL)"})

with st.expander("Research nodes (from DB)", expanded=False):
    if versions:
        rc1, rc2, rc3, rc4 = st.columns(4)
        r_version    = rc1.selectbox("Version", versions, key="r_version")
        r_min_alpha  = rc2.number_input("Min alpha %", value=100.0, step=50.0, key="r_alpha")
        r_min_trades = rc3.number_input("Min trades", value=10, step=5, key="r_trades")
        r_z_options  = sorted({row[0] for row in sqlite3.connect(DB_PATH).execute(
            "SELECT DISTINCT z_score_threshold FROM backtest_cache WHERE version=?", (r_version,)
        ).fetchall()})
        r_z = rc4.multiselect("Z thresholds", r_z_options, default=r_z_options, key="r_z")

        if r_z:
            df_top = load_top_nodes(r_version, r_min_alpha, int(r_min_trades), r_z)
            if not df_top.empty:
                df_top['label'] = df_top.apply(
                    lambda r: f"{r['ticker']} w={r['window']} z={r['z_score_threshold']} TP={r['take_profit']} SL={r['stop_loss']}", axis=1
                )
                df_display = df_top[['label', 'trades', 'win_rate', 'strategy_return', 'alpha_vs_spy']].copy()
                df_display.columns = ['Node', 'Trades', 'Win %', 'Return %', 'Alpha %']
                selected_labels = st.multiselect("Add to portfolio", df_top['label'].tolist(), key="r_nodes")
                if selected_labels:
                    sel_rows = df_top[df_top['label'].isin(selected_labels)]
                    for _, row in sel_rows.iterrows():
                        nodes_to_run.append({
                            'ticker': row['ticker'], 'strategy': row['strategy'],
                            'window': row['window'], 'take_profit': row['take_profit'],
                            'stop_loss': row['stop_loss'], 'max_hold_hours': row['max_hold_hours'],
                            'z_score_threshold': row['z_score_threshold'], 'label': row['label'],
                        })
                st.dataframe(df_display, use_container_width=True, hide_index=True,
                             column_config={"Win %":    st.column_config.NumberColumn(format="%.0f%%"),
                                            "Return %": st.column_config.NumberColumn(format="%.0f%%"),
                                            "Alpha %":  st.column_config.NumberColumn(format="%.0f%%")})
            else:
                st.caption("No nodes match filters.")
    else:
        st.caption("No versions in DB.")

if not nodes_to_run:
    st.info("No nodes selected.")
    st.stop()

# ── Run backtests ─────────────────────────────────────────────────────────────

all_trades = []
with st.spinner("Running backtests..."):
    for node in nodes_to_run:
        trades = run_node_backtest(
            node['ticker'], node.get('strategy', 'ZScoreBreakout'), node['window'],
            node['take_profit'], node['stop_loss'], node['max_hold_hours'],
            node.get('z_score_threshold', 2.0)
        )
        for t in trades:
            t['Node'] = node['label']
        all_trades.extend(trades)

if not all_trades:
    st.info("No trades found.")
    st.stop()

df_all = pd.DataFrame(all_trades)
df_all['Entry Time'] = pd.to_datetime(df_all['Entry Time'])
df_all['Exit Time']  = pd.to_datetime(df_all['Exit Time'])
df_all['Return %']   = (df_all['Return'] * 100).round(1)

spy   = load_spy()
tqqq  = load_price_series('TQQQ')
nodes = sorted(df_all['Node'].unique().tolist())


# ── Chart rendering (fragment = sliders only re-run this) ─────────────────

@st.fragment
def render(df_all, spy, tqqq, nodes, nodes_to_run):
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
        fig.add_trace(go.Scatter(
            x=[row['Entry Time'], row['Exit Time']],
            y=[node_y[row['Node']], node_y[row['Node']]],
            mode='lines',
            name=result,
            legendgroup=result,
            showlegend=(result not in seen),
            line=dict(color=RESULT_COLORS[result], width=18),
            customdata=[[row['Return %'], row['hours_held'],
                         row['Entry Price'], row['Exit Price']]] * 2,
            hovertemplate=(
                f"<b>{row['Node']}</b><br>"
                "Return: %{customdata[0]}%<br>"
                "Hours: %{customdata[1]}<br>"
                "Entry: $%{customdata[2]:.4f}<br>"
                "Exit: $%{customdata[3]:.4f}"
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


render(df_all, spy, tqqq, nodes, nodes_to_run)
