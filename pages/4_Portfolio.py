import sqlite3
import streamlit as st
import pandas as pd
from active_signals import (get_watchlists, get_watchlist, add_node, remove_node,
                             label_node, create_watchlist, delete_watchlist,
                             set_active_watchlist, set_node_mode)
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from pathlib import Path
import numpy as np
import strategies
from backtester import run_backtest_dispatch

DB_PATH   = "./cache/trading_universe.db"
LIVE_DB_PATH = "./cache/trading_live.db"
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
    with sqlite3.connect(LIVE_DB_PATH) as c:
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
        for (ticker, strategy, version, window, tp, sl, hold, z, fixed_sl, trail_buy_pct, trail_pct) in params_tuple:
            row = c.execute("""
                SELECT alpha_vs_spy, strategy_return, trades, win_rate, asset_bh, spy_bh
                FROM backtest_cache
                WHERE ticker=? AND strategy=? AND version=? AND window=? AND take_profit=? AND stop_loss=?
                  AND max_hold_hours=? AND z_score_threshold=?
                  AND COALESCE(fixed_sl,0)=? AND COALESCE(trail_buy_pct,0)=? AND COALESCE(trail_pct,0)=?
            """, (ticker, strategy, version, window, tp, sl, hold, z,
                  fixed_sl or 0.0, trail_buy_pct or 0.0, trail_pct or 0.0)).fetchone()
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
                   strategy_return, alpha_vs_spy,
                   COALESCE(fixed_sl, 0) as fixed_sl,
                   COALESCE(trail_buy_pct, 0) as trail_buy_pct,
                   COALESCE(trail_pct, 0) as trail_pct
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
def compute_bands(ticker, strategy_name, window):
    df_h = load_hourly(ticker)
    if df_h is None:
        return None, None
    close_col = 'Adj Close' if 'Adj Close' in df_h.columns else 'Close'
    df_daily = df_h.resample('D').last().dropna(subset=[close_col])
    strat_class = getattr(strategies, strategy_name, None)
    if strat_class is None:
        return None, None
    strat = strat_class(window=window, z_score_threshold=2.0)
    df_ind = strat.generate_daily_indicators(df_daily)
    df_ind = df_ind.reindex(df_h.index, method='ffill')
    return df_ind, df_h[close_col]


def _resolve_sl_and_trailpct(strategy_class, stop_loss, trail_buy_pct, trail_pct, fixed_sl):
    """Best-effort across v3.x (real named columns) and legacy v1.x/v2.x (overloaded
    stop_loss, trail_buy_pct not tracked pre-v3.x) watch_list/backtest_cache rows —
    see docs/design.md 'Grid axis meaning by strategy'. Legacy TrailingBoth rows have
    no per-row trail_pct (it was a config constant, not stored) — 3.0% is the
    historical default (v2.10)."""
    if issubclass(strategy_class, strategies.TrailingBothZScoreBreakout):
        sl_raw = trail_buy_pct if trail_buy_pct else stop_loss
        tpct   = trail_pct if trail_pct else 3.0
        return sl_raw, fixed_sl, tpct
    if issubclass(strategy_class, strategies.TrailingBuyZScoreBreakout):
        return (trail_buy_pct if trail_buy_pct else stop_loss), fixed_sl, 0.0
    if issubclass(strategy_class, (strategies.TrailingExitZScoreBreakout, strategies.LimitOrderTrailingExit)):
        return (trail_pct if trail_pct else stop_loss), fixed_sl, 0.0
    return stop_loss, 0.0, 0.0


@st.cache_data(ttl=300)
def run_node_backtest(ticker, strategy_name, window, tp, sl, hold, zt,
                      fixed_sl=0.0, trail_buy_pct=0.0, trail_pct=0.0):
    df_h = load_hourly(ticker)
    if df_h is None:
        return []
    close_col = 'Adj Close' if 'Adj Close' in df_h.columns else 'Close'
    df_daily = df_h.resample('D').last().dropna(subset=[close_col])
    strat_class = getattr(strategies, strategy_name)
    strat = strat_class(window=window, z_score_threshold=float(zt))
    df_ind = strat.generate_daily_indicators(df_daily)
    sl_raw, resolved_fixed_sl, tpct = _resolve_sl_and_trailpct(
        strat_class, sl, trail_buy_pct, trail_pct, fixed_sl)
    return run_backtest_dispatch(
        strat_class, df_h, df_ind, ticker,
        take_profit=tp, sl_raw=sl_raw, max_hours_to_hold=hold, z_score_threshold=float(zt),
        fixed_sl=resolved_fixed_sl, trail_pct_pct=tpct
    )



# ── Node selection ────────────────────────────────────────────────────────────

all_wls     = get_watchlists()
wl_names    = [w['name'] for w in all_wls]
active_name = next((w['name'] for w in all_wls if w['is_active']), wl_names[0] if wl_names else None)
_wl_idx     = wl_names.index(active_name) if active_name in wl_names else 0
_picked_name = st.sidebar.selectbox("Watchlist", wl_names, index=_wl_idx, key="portfolio_wl_picker") if wl_names else None
picked_wl   = next((w for w in all_wls if w['name'] == _picked_name), None)
picked_wl_id = picked_wl['id'] if picked_wl else None

# Sidebar: watchlist create / delete / set-active
with st.sidebar:
    st.divider()
    if picked_wl:
        if picked_wl['is_active']:
            st.caption(f"**{_picked_name}** — active signals list")
        elif st.button("Set as active signals list"):
            set_active_watchlist(picked_wl['id'])
            st.cache_data.clear()
            st.rerun()
    new_wl_name = st.text_input("New watchlist", placeholder="Name…", label_visibility="collapsed")
    c_create, c_delete = st.columns(2)
    if c_create.button("Create", use_container_width=True) and new_wl_name.strip():
        create_watchlist(new_wl_name.strip())
        st.cache_data.clear()
        st.rerun()
    can_delete = picked_wl and not picked_wl['is_active']
    if c_delete.button("Delete list", disabled=not can_delete, use_container_width=True):
        delete_watchlist(picked_wl['id'])
        st.cache_data.clear()
        st.rerun()

watchlist = load_watchlist(picked_wl_id)
versions  = load_versions()

nodes_to_run = []  # list of dicts with keys: ticker, strategy, window, take_profit, stop_loss, max_hold_hours, z_score_threshold, label

# ── Watchlist table ───────────────────────────────────────────────────────────
selected_wl_node = None
if watchlist:
    wl_raw = pd.DataFrame(watchlist)
    params = tuple((r['ticker'], r['strategy'], r['version'], r['window'], r['take_profit'], r['stop_loss'], r['max_hold_hours'], r['z_score_threshold'],
                    r.get('fixed_sl'), r.get('trail_buy_pct'), r.get('trail_pct')) for r in watchlist)
    metrics = load_watchlist_metrics(params)
    m_df = pd.DataFrame(metrics)
    wl_base = pd.concat([wl_raw[['id', 'mode', 'ticker', 'strategy', 'version', 'window', 'z_score_threshold',
                                   'take_profit', 'stop_loss', 'max_hold_hours', 'label']].reset_index(drop=True), m_df], axis=1)
    wl_base['watch'] = True

    wl_display = wl_base.rename(columns={
        'id': 'ID', 'mode': 'Mode', 'ticker': 'Ticker', 'strategy': 'Strategy', 'version': 'Version',
        'window': 'Window', 'z_score_threshold': 'Z', 'take_profit': 'TP%', 'stop_loss': 'SL%',
        'max_hold_hours': 'Hold h', 'label': 'Label',
        'alpha': 'Alpha%', 'ret': 'Return%', 'trades': 'Trades', 'win_rate': 'Win%',
        'asset_bh': 'Asset B&H%', 'spy_bh': 'SPY B&H%', 'max_notional': 'Max Notional', 'type': 'Type',
        'watch': 'Watch',
    })

    wl_edited = st.data_editor(
        wl_display, use_container_width=True, hide_index=True,
        height=35 * (len(wl_display) + 1) + 10,
        column_config={
            'ID':           st.column_config.NumberColumn('ID', disabled=True),
            'Mode':         st.column_config.SelectboxColumn('Mode', options=['live', 'research'], required=True),
            'Label':        st.column_config.TextColumn('Label'),
            'Watch':        st.column_config.CheckboxColumn('Watch', help='Uncheck to remove'),
            'Alpha%':       st.column_config.NumberColumn(format='%.1f%%'),
            'Return%':      st.column_config.NumberColumn(format='%.1f%%'),
            'Win%':         st.column_config.NumberColumn(format='%.0f%%'),
            'Asset B&H%':   st.column_config.NumberColumn(format='%.1f%%'),
            'SPY B&H%':     st.column_config.NumberColumn(format='%.1f%%'),
            'Max Notional': st.column_config.NumberColumn(format='$%.0f'),
        },
        disabled=[c for c in wl_display.columns if c not in ('Mode', 'Label', 'Watch')],
    )

    # Remove unchecked rows
    for i in wl_display.index[wl_edited['Watch'] == False]:
        remove_node(int(wl_display.loc[i, 'ID']))
        st.cache_data.clear()
        st.rerun()

    # Save mode / label edits
    mode_changed  = wl_display['Mode']  != wl_edited['Mode']
    label_changed = wl_display['Label'] != wl_edited['Label']
    for i in wl_display.index[mode_changed]:
        set_node_mode(int(wl_display.loc[i, 'ID']), wl_edited.loc[i, 'Mode'])
    for i in wl_display.index[label_changed]:
        label_node(int(wl_display.loc[i, 'ID']), wl_edited.loc[i, 'Label'])
    if mode_changed.any() or label_changed.any():
        st.cache_data.clear()
        st.rerun()

    # Row picker for the chart (data_editor doesn't support on_select)
    node_labels = [f"{n['ticker']}  w={n['window']}  z={n['z_score_threshold']}  TP={n['take_profit']}  SL={n['stop_loss']}" for n in watchlist]
    chart_pick = st.selectbox("Chart node", ['— none —'] + node_labels, key='wl_chart_pick')
    if chart_pick != '— none —':
        selected_wl_node = watchlist[node_labels.index(chart_pick)]

    for node in watchlist:
        nodes_to_run.append({**node, 'label': f"{node['ticker']} w={node['window']} (WL)"})
else:
    st.caption("Watchlist is empty.")

# ── Price + bands chart for selected watchlist node ───────────────────────────
if selected_wl_node:
    n = selected_wl_node
    st.subheader(f"{n['ticker']} — Price + Bands")
    df_ind, close = compute_bands(n['ticker'], n.get('strategy', 'ZScoreBreakout'), int(n['window']))
    with st.spinner("Running backtest..."):
        trades = run_node_backtest(
            n['ticker'], n.get('strategy', 'ZScoreBreakout'),
            int(n['window']), int(n['take_profit']), int(n['stop_loss']),
            int(n['max_hold_hours']), float(n['z_score_threshold']),
            fixed_sl=float(n.get('fixed_sl') or 0), trail_buy_pct=float(n.get('trail_buy_pct') or 0),
            trail_pct=float(n.get('trail_pct') or 0),
        )
    df_trades = pd.DataFrame(trades)
    closed = pd.DataFrame()
    if not df_trades.empty:
        if 'Return %' not in df_trades.columns and 'Return' in df_trades.columns:
            df_trades['Return %'] = df_trades['Return'] * 100
        closed = df_trades[df_trades['Result'].isin(['WIN', 'LOSS', 'TWIN', 'TLOSS'])].copy()
        closed['Entry Time'] = pd.to_datetime(closed['Entry Time'])
        closed['Exit Time']  = pd.to_datetime(closed['Exit Time'])

    fig_bands = go.Figure()
    if close is not None:
        close_4h = close.resample('4h').last().dropna()
        fig_bands.add_trace(go.Scatter(
            x=close_4h.index, y=close_4h.values,
            name='Price', line=dict(color='#aaaaaa', width=1),
        ))
    if df_ind is not None:
        sma = df_ind['SMA'].reindex(close.index, method='ffill')
        std = df_ind['Std'].reindex(close.index, method='ffill')
        for z_val, (color, dash) in {2.0: ('#4a9eff', 'dash'), 2.5: ('#ff9f4a', 'dot'), 3.0: ('#cc44ff', 'dashdot')}.items():
            upper = (sma + z_val * std).resample('4h').last().dropna()
            lower = (sma - z_val * std).resample('4h').last().dropna()
            fig_bands.add_trace(go.Scatter(x=upper.index, y=upper.values, name=f'z={z_val} upper',
                                           line=dict(color=color, width=1, dash=dash), opacity=0.7))
            fig_bands.add_trace(go.Scatter(x=lower.index, y=lower.values, showlegend=False,
                                           line=dict(color=color, width=1, dash=dash), opacity=0.7))
    if not closed.empty:
        wins  = closed[closed['Result'].isin(['WIN', 'TWIN'])]
        losses = closed[closed['Result'].isin(['LOSS', 'TLOSS'])]
        if not wins.empty:
            fig_bands.add_trace(go.Scatter(x=wins['Entry Time'], y=wins['Entry Price'],
                                           mode='markers', name='Win',
                                           marker=dict(symbol='triangle-up', size=10, color='#2ecc71')))
        if not losses.empty:
            fig_bands.add_trace(go.Scatter(x=losses['Entry Time'], y=losses['Entry Price'],
                                           mode='markers', name='Loss',
                                           marker=dict(symbol='triangle-down', size=10, color='#e74c3c')))
        fig_bands.add_trace(go.Scatter(x=closed['Exit Time'], y=closed['Exit Price'],
                                       mode='markers', name='Exit',
                                       marker=dict(symbol='x', size=8, color='#ffffff', opacity=0.7)))
        for _, tr in closed.iterrows():
            fig_bands.add_vrect(
                x0=tr['Entry Time'], x1=tr['Exit Time'],
                fillcolor='#2ecc71' if tr['Result'] in ['WIN', 'TWIN'] else '#e74c3c',
                opacity=0.07, line_width=0,
            )
    fig_bands.update_layout(
        height=500, margin=dict(l=0, r=0, t=30, b=0),
        hovermode='x unified', xaxis_rangeslider_visible=False,
        legend=dict(orientation='h', yanchor='bottom', y=1.01, xanchor='left', x=0),
    )
    st.plotly_chart(fig_bands, use_container_width=True, config={'scrollZoom': True})

st.divider()

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
                r_sel = st.dataframe(
                    df_display, use_container_width=True, hide_index=True,
                    on_select="rerun", selection_mode="single-row",
                    column_config={"Win %":    st.column_config.NumberColumn(format="%.0f%%"),
                                   "Return %": st.column_config.NumberColumn(format="%.0f%%"),
                                   "Alpha %":  st.column_config.NumberColumn(format="%.0f%%")},
                )
                if r_sel.selection.rows:
                    r_idx = r_sel.selection.rows[0]
                    r_row = df_top.iloc[r_idx]
                    st.caption(f"**{r_row['ticker']}**  w={r_row['window']}  z={r_row['z_score_threshold']}  TP={r_row['take_profit']}  SL={r_row['stop_loss']}  hold={r_row['max_hold_hours']}h")
                    ac1, ac2 = st.columns(2)
                    with ac1:
                        if picked_wl_id and st.button("Add to watchlist", use_container_width=True):
                            # trail_buy_pct/trail_pct are 0 (not populated) for legacy
                            # v1.x/v2.x rows — only pass real values for v3.x rows;
                            # None lets add_node fall back to its legacy path.
                            _has_new_cols = bool(r_row['trail_buy_pct']) or bool(r_row['trail_pct'])
                            add_node(r_row['ticker'], r_row['strategy'], r_version,
                                     int(r_row['window']), int(r_row['take_profit']),
                                     int(r_row['stop_loss']), int(r_row['max_hold_hours']),
                                     z_score_threshold=float(r_row['z_score_threshold']),
                                     watchlist_id=picked_wl_id,
                                     trail_buy_pct=float(r_row['trail_buy_pct']) if _has_new_cols else None,
                                     trail_pct=float(r_row['trail_pct']) if _has_new_cols else None)
                            st.cache_data.clear()
                            st.rerun()
                    with ac2:
                        if st.button("Add to portfolio view", use_container_width=True):
                            nodes_to_run.append({
                                'ticker': r_row['ticker'], 'strategy': r_row['strategy'],
                                'window': r_row['window'], 'take_profit': r_row['take_profit'],
                                'stop_loss': r_row['stop_loss'], 'max_hold_hours': r_row['max_hold_hours'],
                                'z_score_threshold': r_row['z_score_threshold'], 'label': r_row['label'],
                                'fixed_sl': r_row['fixed_sl'], 'trail_buy_pct': r_row['trail_buy_pct'],
                                'trail_pct': r_row['trail_pct'],
                            })
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
            node.get('z_score_threshold', 2.0),
            fixed_sl=float(node.get('fixed_sl') or 0), trail_buy_pct=float(node.get('trail_buy_pct') or 0),
            trail_pct=float(node.get('trail_pct') or 0),
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
    all_nodes = sorted(df_all['Node'].unique().tolist())
    selected = st.multiselect("Nodes", all_nodes, default=all_nodes)
    if not selected:
        st.warning("No nodes selected.")
        return

    # c1, c2 = st.columns(2)
    # with c1:
    #     max_hurst = st.slider("Max Hurst at entry", 0.3, 0.8, 0.8, 0.01,
    #                           disabled=not h_has_data)
    # with c2:
    #     max_adf = st.slider("Max ADF p at entry", 0.0, 1.0, 1.0, 0.01,
    #                         disabled=not adf_has_data)

    df = df_all[df_all['Node'].isin(selected)].copy()

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
