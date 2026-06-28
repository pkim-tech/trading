import streamlit as st
import pandas as pd
import numpy as np
import sqlite3
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from pathlib import Path
from backtester import run_backtest
import strategies
from active_signals import get_watchlist
from statsmodels.tsa.stattools import adfuller

DB_PATH = "./cache/trading_universe.db"
CACHE_DIR = Path("./cache")
ROLLING_WINDOW = 30 * 7  # ~30 trading days in hourly bars

st.set_page_config(layout="wide", page_title="Node Inspector")


def _hurst(ts):
    lags = [2, 4, 8, 16, 32]
    lags = [l for l in lags if l < len(ts) // 2]
    if len(lags) < 2:
        return np.nan
    variances = []
    for lag in lags:
        v = np.var(ts[lag:] - ts[:-lag])
        if v <= 0:
            return np.nan
        variances.append(v)
    m = np.polyfit(np.log(lags[:len(variances)]), np.log(variances), 1)
    return m[0] / 2.0


@st.cache_data(ttl=3600)
def rolling_hurst(ticker, window=ROLLING_WINDOW, step=12):
    prices = load_hourly(ticker)
    if prices is None:
        return pd.Series(dtype=float)
    close_col = 'Adj Close' if 'Adj Close' in prices.columns else 'Close'
    log_p = np.log(prices[close_col].values)
    result = np.full(len(log_p), np.nan)
    for i in range(window, len(log_p), step):
        result[i] = _hurst(log_p[i - window:i])
    s = pd.Series(result, index=prices.index)
    return s.interpolate(method='time').where(s.notna() | s.shift().notna())


@st.cache_data(ttl=3600)
def rolling_adf(ticker, window=ROLLING_WINDOW, step=48):
    prices = load_hourly(ticker)
    if prices is None:
        return pd.Series(dtype=float)
    close_col = 'Adj Close' if 'Adj Close' in prices.columns else 'Close'
    vals = prices[close_col].values
    result = np.full(len(vals), np.nan)
    for i in range(window, len(vals), step):
        try:
            p = adfuller(vals[i - window:i], maxlag=1, autolag=None)[1]
            result[i] = p
        except Exception:
            pass
    s = pd.Series(result, index=prices.index)
    return s.interpolate(method='time').where(s.notna() | s.shift().notna())


@st.cache_data(ttl=60)
def get_slice(ticker, strategy, version):
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(
            """SELECT window, max_hold_hours, take_profit, stop_loss,
                      COALESCE(z_score_threshold, 2.0) as z_score_threshold,
                      trades, win_rate, strategy_return, alpha_vs_spy, asset_bh
               FROM backtest_cache
               WHERE ticker = ? AND strategy = ? AND version = ?""",
            conn, params=(ticker, strategy, version)
        )


@st.cache_data(ttl=300)
def load_hourly(ticker):
    p = CACHE_DIR / f"{ticker}_1h.csv"
    if not p.exists():
        return None
    return pd.read_csv(p, index_col=0, parse_dates=True).sort_index()


@st.cache_data(ttl=300)
def run_cached_backtest(ticker, strategy_name, version, window, tp, sl, hold, zt):
    df_h = load_hourly(ticker)
    if df_h is None:
        return []
    close_col = 'Adj Close' if 'Adj Close' in df_h.columns else 'Close'
    df_daily = df_h.resample('D').last().dropna(subset=[close_col])
    strat_class = getattr(strategies, strategy_name)
    strat = strat_class(window=window, z_score_threshold=float(zt))
    df_daily_proc = strat.generate_daily_indicators(df_daily)
    return run_backtest(df_h, df_daily_proc, ticker,
                        take_profit=tp / 100.0, stop_loss=sl / 100.0,
                        max_hours_to_hold=hold, z_score_threshold=float(zt))


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
    close = df_h[close_col]
    return df_ind, close


st.title("Node Inspector")

# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------
wl = get_watchlist()
wl_df = pd.DataFrame(wl) if wl else pd.DataFrame()

selected_node = {}
if not wl_df.empty:
    st.subheader("Watch List")
    wl_display = wl_df[['ticker', 'strategy', 'version', 'window', 'take_profit',
                          'stop_loss', 'max_hold_hours', 'z_score_threshold', 'label']].rename(columns={
        'ticker': 'Ticker', 'strategy': 'Strategy', 'version': 'Version',
        'window': 'Win', 'take_profit': 'TP%', 'stop_loss': 'SL%',
        'max_hold_hours': 'Hold h', 'z_score_threshold': 'Z', 'label': 'Label',
    })
    sel = st.dataframe(
        wl_display,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        height=200,
    )
    rows = sel.selection.rows
    if rows:
        selected_node = wl_df.iloc[rows[0]].to_dict()

    st.divider()

# ---------------------------------------------------------------------------
# Node params — pre-filled from watchlist selection or session state
# ---------------------------------------------------------------------------
t_node = st.session_state.pop("target_node", {}) or selected_node

with sqlite3.connect(DB_PATH) as _c:
    tick_opts = [r[0] for r in _c.execute("SELECT DISTINCT ticker FROM backtest_cache ORDER BY ticker").fetchall()]
    strat_opts = [r[0] for r in _c.execute("SELECT DISTINCT strategy FROM backtest_cache ORDER BY strategy").fetchall()]
    ver_opts   = [r[0] for r in _c.execute("SELECT DISTINCT version FROM backtest_cache ORDER BY version DESC").fetchall()]

if not tick_opts:
    st.info("No backtest data yet.")
    st.stop()

def _idx(lst, val, fallback=0):
    return lst.index(val) if val in lst else fallback

c1, c2, c3 = st.columns(3)
with c1:
    selected_ticker   = st.selectbox("Ticker",   tick_opts, index=_idx(tick_opts, t_node.get("ticker")))
with c2:
    selected_strategy = st.selectbox("Strategy", strat_opts, index=_idx(strat_opts, t_node.get("strategy")))
with c3:
    selected_version  = st.selectbox("Version",  ver_opts,  index=_idx(ver_opts,  t_node.get("version")))

df_slice = get_slice(selected_ticker, selected_strategy, selected_version)

if df_slice.empty:
    st.warning("No cached results for this ticker/strategy/version.")
    st.stop()

c_w, c_tp, c_sl, c_hold, c_zt = st.columns(5)

def _sel(col, key, fallback):
    opts = sorted(df_slice[key].unique())
    v = t_node.get(key, fallback)
    return col.selectbox(key.replace("_", " ").title(), opts, index=_idx(opts, v))

with c_w:    target_w    = _sel(c_w,    "window",           df_slice["window"].min())
with c_tp:   target_tp   = _sel(c_tp,   "take_profit",      df_slice["take_profit"].min())
with c_sl:   target_sl   = _sel(c_sl,   "stop_loss",        df_slice["stop_loss"].min())
with c_hold: target_hold = _sel(c_hold, "max_hold_hours",   df_slice["max_hold_hours"].min())
with c_zt:   target_zt   = _sel(c_zt,   "z_score_threshold", 2.0)

# ---------------------------------------------------------------------------
# Load data + run backtest
# ---------------------------------------------------------------------------
df_h = load_hourly(selected_ticker)
if df_h is None:
    st.error(f"No hourly data for {selected_ticker}.")
    st.stop()

close_col = 'Adj Close' if 'Adj Close' in df_h.columns else 'Close'
close = df_h[close_col]

df_ind, _ = compute_bands(selected_ticker, selected_strategy, int(target_w))

with st.spinner("Running backtest..."):
    trades = run_cached_backtest(
        selected_ticker, selected_strategy, selected_version,
        int(target_w), int(target_tp), int(target_sl), int(target_hold), float(target_zt),
    )

df_trades = pd.DataFrame(trades)
if not df_trades.empty and "Return" in df_trades.columns and "Return %" not in df_trades.columns:
    df_trades["Return %"] = df_trades["Return"] * 100
closed = df_trades[df_trades["Result"].isin(["WIN", "LOSS", "TWIN", "TLOSS"])] if not df_trades.empty else pd.DataFrame()

# ---------------------------------------------------------------------------
# Compute indicators (needed before chart for slider)
# ---------------------------------------------------------------------------
with st.spinner("Computing Hurst..."):
    h_series = rolling_hurst(selected_ticker)

show_adf = st.checkbox("Show rolling ADF p-value (slow on first load)")
adf_series = rolling_adf(selected_ticker) if show_adf else None

# ---------------------------------------------------------------------------
# Hurst filter slider
# ---------------------------------------------------------------------------
st.subheader("Hurst Filter")
h_cutoff = st.slider("Suppress entries where H ≥", min_value=0.30, max_value=0.70,
                     value=0.50, step=0.01, format="%.2f")

# Classify trades as allowed / suppressed based on H at entry time
if not closed.empty:
    closed = closed.copy()
    closed["Entry Time"] = pd.to_datetime(closed["Entry Time"])
    closed["Exit Time"]  = pd.to_datetime(closed["Exit Time"])
    closed["H_at_entry"] = closed["Entry Time"].apply(
        lambda t: h_series.asof(t) if t >= h_series.index[0] else np.nan
    )
    closed["allowed"] = closed["H_at_entry"].isna() | (closed["H_at_entry"] < h_cutoff)
    allowed    = closed[closed["allowed"]]
    suppressed = closed[~closed["allowed"]]
else:
    allowed = suppressed = pd.DataFrame()

# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------
st.subheader("Price + Bands")

n_rows = 3 if show_adf else 2
row_heights = [0.62, 0.19, 0.19] if show_adf else [0.75, 0.25]
subplot_titles = ("", "Hurst (30d)", "ADF p-value (30d)") if show_adf else ("", "Hurst (30d)")

fig = make_subplots(rows=n_rows, cols=1, shared_xaxes=True,
                    row_heights=row_heights,
                    vertical_spacing=0.03,
                    subplot_titles=subplot_titles)

# Downsample to 4h for display performance
close_4h = close.resample('4h').last().dropna()

# Price
fig.add_trace(go.Scatter(
    x=close_4h.index, y=close_4h.values,
    name="Price", line=dict(color="#aaaaaa", width=1),
), row=1, col=1)

# Bollinger bands for each z
if df_ind is not None:
    sma = df_ind['SMA'].reindex(close.index, method='ffill')
    std = df_ind['Std'].reindex(close.index, method='ffill')
    band_styles = {
        2.0: ("#4a9eff", "dash"),
        2.5: ("#ff9f4a", "dot"),
        3.0: ("#cc44ff", "dashdot"),
    }
    for z, (color, dash) in band_styles.items():
        upper = (sma + z * std).resample('4h').last().dropna()
        lower = (sma - z * std).resample('4h').last().dropna()
        fig.add_trace(go.Scatter(
            x=upper.index, y=upper.values,
            name=f"z={z} upper", line=dict(color=color, width=1, dash=dash), opacity=0.7,
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=lower.index, y=lower.values,
            name=f"z={z} lower", line=dict(color=color, width=1, dash=dash), opacity=0.7,
            showlegend=False,
        ), row=1, col=1)

# Trade markers — allowed trades (normal colors)
for subset, color, symbol, label in [
    (allowed[allowed["Result"].isin(["WIN","TWIN"])],  "#00cc66", "triangle-up",   "Win (allowed)"),
    (allowed[allowed["Result"].isin(["LOSS","TLOSS"])], "#ff4444", "triangle-down", "Loss (allowed)"),
]:
    if not subset.empty:
        fig.add_trace(go.Scatter(
            x=subset["Entry Time"], y=subset["Entry Price"],
            mode="markers", marker=dict(symbol=symbol, size=10, color=color),
            name=label,
        ), row=1, col=1)

# Suppressed trades (grey, hollow)
if not suppressed.empty:
    fig.add_trace(go.Scatter(
        x=suppressed["Entry Time"], y=suppressed["Entry Price"],
        mode="markers",
        marker=dict(symbol="circle-open", size=10, color="#888888", line=dict(width=2)),
        name="Suppressed (H≥cutoff)",
    ), row=1, col=1)

# Exit markers
if not closed.empty:
    fig.add_trace(go.Scatter(
        x=allowed["Exit Time"], y=allowed["Exit Price"],
        mode="markers", marker=dict(symbol="x", size=8, color="#ffffff", opacity=0.7),
        name="Exit",
    ), row=1, col=1)

    # Shade allowed trade periods only
    for _, tr in allowed.iterrows():
        is_win = tr["Result"] in ["WIN", "TWIN"]
        fig.add_vrect(
            x0=tr["Entry Time"], x1=tr["Exit Time"],
            fillcolor="#00cc66" if is_win else "#ff4444",
            opacity=0.07, line_width=0, row=1, col=1,
        )

# Rolling Hurst + threshold line
fig.add_trace(go.Scatter(
    x=h_series.index, y=h_series.values,
    name="Hurst (30d)", line=dict(color="#ffdd44", width=1.5),
), row=2, col=1)
fig.add_hline(y=0.5,      line=dict(color="#555555", dash="dash",  width=1), row=2, col=1)
fig.add_hline(y=h_cutoff, line=dict(color="#ff8800", dash="solid", width=1.5), row=2, col=1)

# Rolling ADF p-value (optional)
if show_adf and adf_series is not None:
    fig.add_trace(go.Scatter(
        x=adf_series.index, y=adf_series.values,
        name="ADF p-value (30d)", line=dict(color="#44ddff", width=1.5),
    ), row=3, col=1)
    fig.add_hline(y=0.05, line=dict(color="#888888", dash="dash", width=1), row=3, col=1)

fig.update_layout(
    height=800,
    legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0),
    margin=dict(l=50, r=20, t=30, b=20),
    hovermode="x unified",
    xaxis_rangeslider_visible=False,
)
fig.update_yaxes(title_text="Price", row=1, col=1)
fig.update_yaxes(title_text="Hurst", row=2, col=1, range=[0, 1])
if show_adf:
    fig.update_yaxes(title_text="ADF p", row=3, col=1, range=[0, 1])

st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True})

# ---------------------------------------------------------------------------
# Metrics — unfiltered vs H-filtered side by side
# ---------------------------------------------------------------------------
def _metrics(df, label):
    if df.empty:
        st.caption(f"{label}: no trades")
        return
    total = len(df)
    pw = len(df[df["Result"] == "WIN"])
    tw = len(df[df["Result"] == "TWIN"])
    ret = ((df["Return %"] / 100.0 + 1).prod() - 1) * 100 if "Return %" in df.columns else 0.0
    wr  = (pw + tw) / total * 100
    st.caption(label)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Trades", total)
    c2.metric("Win Rate", f"{wr:.1f}%")
    c3.metric("Compounded Return", f"{ret:+.2f}%")
    c4.metric("Time Exits", len(df[df["Result"].isin(["TWIN","TLOSS"])]))

if not closed.empty:
    col_all, col_filt = st.columns(2)
    with col_all:
        _metrics(closed, "All trades (unfiltered)")
    with col_filt:
        _metrics(allowed, f"H-filtered (H < {h_cutoff:.2f})")

    # Quarterly breakdown (filtered)
    if not allowed.empty:
        st.markdown("#### By Quarter (H-filtered)")
        allowed = allowed.copy()
        allowed["Quarter"] = allowed["Entry Time"].dt.to_period("Q").astype(str)
        rows_q = []
        for q, g in allowed.groupby("Quarter"):
            pw = len(g[g["Result"] == "WIN"])
            tw = len(g[g["Result"] == "TWIN"])
            ret = ((g["Return %"] / 100.0 + 1).prod() - 1) * 100 if "Return %" in g.columns else 0.0
            rows_q.append({"Quarter": q, "Trades": len(g), "Win %": (pw+tw)/len(g)*100, "Return %": ret})
        df_q = pd.DataFrame(rows_q).sort_values("Quarter", ascending=False)
        st.dataframe(df_q, hide_index=True, use_container_width=True,
                     column_config={
                         "Win %":    st.column_config.NumberColumn(format="%.1f%%"),
                         "Return %": st.column_config.NumberColumn(format="%+.2f%%"),
                     })

    # Trade log
    st.markdown("#### Trade Log")
    log_cols = [c for c in ["Entry Time", "Entry Price", "Exit Time", "Exit Price", "Result", "H_at_entry", "Return %"] if c in closed.columns]
    log_df = closed[log_cols].copy()
    st.dataframe(
        log_df,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Entry Price":  st.column_config.NumberColumn(format="$%.2f"),
            "Exit Price":   st.column_config.NumberColumn(format="$%.2f"),
            "H_at_entry":   st.column_config.NumberColumn(format="%.3f"),
            "Return %":     st.column_config.NumberColumn(format="%+.2f%%"),
        },
    )
else:
    st.warning("No closed trades for this node.")
