import streamlit as st
import pandas as pd
import numpy as np
import sqlite3
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from pathlib import Path
from backtester import run_backtest_dispatch
import strategies
from active_signals import get_watchlist
from db_cache import get_kv
from statsmodels.tsa.stattools import adfuller

DB_PATH = "./cache/trading_universe.db"
CACHE_DIR = Path("./cache")

st.set_page_config(layout="wide", page_title="Node Inspector")


from hurst import _hurst_vectorized, ROLLING_WINDOW


def rolling_hurst(ticker, window=ROLLING_WINDOW):
    prices = load_hourly(ticker)
    if prices is None:
        return pd.Series(dtype=float)
    close_col = 'Adj Close' if 'Adj Close' in prices.columns else 'Close'
    log_p = np.log(prices[close_col].values)
    return pd.Series(_hurst_vectorized(log_p, window), index=prices.index)


def _load_hurst_db(ticker):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hurst_cache (
                ticker    TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                hurst     REAL,
                PRIMARY KEY (ticker, timestamp)
            )
        """)
        rows = conn.execute(
            "SELECT timestamp, hurst FROM hurst_cache WHERE ticker = ? ORDER BY timestamp",
            (ticker,)
        ).fetchall()
    if not rows:
        return None
    return pd.Series(
        [r[1] for r in rows],
        index=pd.to_datetime([r[0] for r in rows])
    )


def _save_hurst_db(ticker, series):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hurst_cache (
                ticker    TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                hurst     REAL,
                PRIMARY KEY (ticker, timestamp)
            )
        """)
        conn.executemany(
            "INSERT OR REPLACE INTO hurst_cache (ticker, timestamp, hurst) VALUES (?, ?, ?)",
            [(ticker, ts.isoformat(), val) for ts, val in series.items()]
        )


def get_hurst(ticker, is_watchlist=False):
    key = f"hurst_{ticker}"
    if key in st.session_state:
        return st.session_state[key]

    h = None
    if is_watchlist:
        h = _load_hurst_db(ticker)
        if h is not None:
            prices = load_hourly(ticker)
            if prices is not None and h.index[-1] < prices.index[-1]:
                h = None  # stale — CSV has newer bars

    if h is None:
        h = rolling_hurst(ticker)
        if is_watchlist:
            _save_hurst_db(ticker, h)

    st.session_state[key] = h
    return h


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


@st.cache_data(ttl=86400)
def _load_dropdown_opts():
    versions = get_kv("versions")
    with sqlite3.connect(DB_PATH) as _c:
        if versions is None:
            versions = [r[0] for r in _c.execute("SELECT DISTINCT version FROM backtest_cache ORDER BY version DESC").fetchall()]
        tickers = [r[0] for r in _c.execute("SELECT DISTINCT ticker FROM backtest_cache ORDER BY ticker").fetchall()]
        strats  = [r[0] for r in _c.execute("SELECT DISTINCT strategy FROM backtest_cache ORDER BY strategy").fetchall()]
    return tickers, strats, versions


@st.cache_data(ttl=86400)
def get_slice(ticker, strategy, version):
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(
            """SELECT window, max_hold_hours, axis_tp as take_profit, stop_loss,
                      COALESCE(z_score_threshold, 2.0) as z_score_threshold,
                      trades, win_rate, strategy_return, alpha_vs_spy, asset_bh,
                      COALESCE(fixed_sl, 0) as fixed_sl,
                      COALESCE(trail_buy_pct, 0) as trail_buy_pct,
                      COALESCE(trail_sell_pct, 0) as trail_pct
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


def _resolve_sl_and_trailpct(strategy_class, stop_loss, trail_buy_pct, trail_pct, fixed_sl):
    """Best-effort across v3.x (real named columns) and legacy v1.x/v2.x (overloaded
    stop_loss, trail_buy_pct/trail_pct always 0) backtest_cache rows — see
    docs/design.md 'Grid axis meaning by strategy'. Legacy TrailingBoth rows have no
    per-row trail_pct (it was a config constant, not stored) — 3.0% is the historical
    default (v2.10)."""
    if issubclass(strategy_class, strategies.TrailingBothZScoreBreakout):
        sl_raw = trail_buy_pct if trail_buy_pct else stop_loss
        tpct   = trail_pct if trail_pct else 3.0
        return sl_raw, fixed_sl, tpct
    if issubclass(strategy_class, strategies.TrailingBuyZScoreBreakout):
        return (trail_buy_pct if trail_buy_pct else stop_loss), fixed_sl, 0.0
    if issubclass(strategy_class, (strategies.TrailingExitZScoreBreakout, strategies.LimitOrderTrailingExit)):
        return (trail_pct if trail_pct else stop_loss), fixed_sl, 0.0
    return stop_loss, 0.0, 0.0


@st.cache_data(ttl=86400)
def run_cached_backtest(ticker, strategy_name, version, window, tp, sl, hold, zt,
                        fixed_sl=0.0, trail_buy_pct=0.0, trail_pct=0.0):
    df_h = load_hourly(ticker)
    if df_h is None:
        return []
    close_col = 'Adj Close' if 'Adj Close' in df_h.columns else 'Close'
    df_daily = df_h.resample('D').last().dropna(subset=[close_col])
    strat_class = getattr(strategies, strategy_name)
    strat = strat_class(window=window, z_score_threshold=float(zt))
    df_daily_proc = strat.generate_daily_indicators(df_daily)
    sl_raw, resolved_fixed_sl, tpct = _resolve_sl_and_trailpct(
        strat_class, sl, trail_buy_pct, trail_pct, fixed_sl)
    return run_backtest_dispatch(
        strat_class, df_h, df_daily_proc, ticker,
        take_profit=tp, sl_raw=sl_raw, max_hours_to_hold=hold, z_score_threshold=float(zt),
        fixed_sl=resolved_fixed_sl, trail_pct_pct=tpct
    )


@st.cache_data(ttl=86400)
def load_watchlist_metrics(params_tuple):
    cols = ['ticker','version','window','take_profit','stop_loss','max_hold_hours','z_score_threshold']
    rows = []
    with sqlite3.connect(DB_PATH) as c:
        for p in params_tuple:
            row = c.execute(
                """SELECT ticker, version, window, take_profit, stop_loss, max_hold_hours,
                          z_score_threshold, strategy_return, alpha_vs_spy, asset_bh, spy_bh,
                          CASE WHEN asset_bh > 0 THEN strategy_return / asset_bh ELSE NULL END,
                          trades, win_rate
                   FROM backtest_cache
                   WHERE strategy='ZScoreBreakout' AND ticker=? AND version=? AND window=? AND take_profit=?
                     AND stop_loss=? AND max_hold_hours=? AND z_score_threshold=?
                   LIMIT 1""", p
            ).fetchone()
            rows.append(row)
    return pd.DataFrame(rows, columns=cols + ['strategy_return','alpha_vs_spy','asset_bh','spy_bh','bh_mult','trades','win_rate'])


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
    _params = tuple((r['ticker'], r['version'], r['window'], r['take_profit'],
                     r['stop_loss'], r['max_hold_hours'], r['z_score_threshold']) for r in wl)
    metrics = load_watchlist_metrics(_params)
    wl_display = wl_df.merge(
        metrics,
        on=['ticker', 'version', 'window', 'take_profit', 'stop_loss', 'max_hold_hours', 'z_score_threshold'],
        how='left'
    )[['ticker', 'window', 'take_profit', 'stop_loss', 'max_hold_hours', 'z_score_threshold',
       'trades', 'win_rate', 'strategy_return', 'alpha_vs_spy', 'asset_bh', 'spy_bh', 'bh_mult', 'label']].rename(columns={
        'ticker': 'Ticker', 'window': 'Win', 'take_profit': 'TP%', 'stop_loss': 'SL%',
        'max_hold_hours': 'Hold h', 'z_score_threshold': 'Z',
        'trades': 'Trades', 'win_rate': 'Win%', 'strategy_return': 'Return',
        'alpha_vs_spy': 'Alpha', 'asset_bh': 'Asset B&H', 'spy_bh': 'SPY B&H',
        'bh_mult': 'B&H Mult', 'label': 'Label',
    })
    sel = st.dataframe(
        wl_display,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        height=35 * (len(wl_display) + 1) + 10,
        column_config={
            'Win%':      st.column_config.NumberColumn(format="%.0f%%"),
            'Return':    st.column_config.NumberColumn(format="%.1f%%"),
            'Alpha':     st.column_config.NumberColumn(format="%.1f%%"),
            'Asset B&H': st.column_config.NumberColumn(format="%.1f%%"),
            'SPY B&H':   st.column_config.NumberColumn(format="%.1f%%"),
            'B&H Mult':  st.column_config.NumberColumn(format="%.1fx"),
        },
    )
    rows = sel.selection.rows
    if rows:
        selected_node = wl_df.iloc[rows[0]].to_dict()

    st.divider()

# ---------------------------------------------------------------------------
# Node params — pre-filled from watchlist selection or session state
# ---------------------------------------------------------------------------
_qp = st.query_params
_qp_node = {k: _qp[k] for k in ("ticker", "version", "strategy") if k in _qp}
for _k in ("window", "take_profit", "stop_loss", "max_hold_hours"):
    if _k in _qp:
        _qp_node[_k] = int(_qp[_k])
t_node = st.session_state.pop("target_node", {}) or _qp_node or selected_node

tick_opts, strat_opts, ver_opts = _load_dropdown_opts()

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

_node_row = df_slice[
    (df_slice['window'] == target_w) & (df_slice['take_profit'] == target_tp) &
    (df_slice['stop_loss'] == target_sl) & (df_slice['max_hold_hours'] == target_hold) &
    (df_slice['z_score_threshold'] == target_zt)
]
_fixed_sl      = float(_node_row['fixed_sl'].iloc[0]) if not _node_row.empty else 0.0
_trail_buy_pct = float(_node_row['trail_buy_pct'].iloc[0]) if not _node_row.empty else 0.0
_trail_pct     = float(_node_row['trail_pct'].iloc[0]) if not _node_row.empty else 0.0

with st.spinner("Running backtest..."):
    trades = run_cached_backtest(
        selected_ticker, selected_strategy, selected_version,
        int(target_w), int(target_tp), int(target_sl), int(target_hold), float(target_zt),
        fixed_sl=_fixed_sl, trail_buy_pct=_trail_buy_pct, trail_pct=_trail_pct,
    )

df_trades = pd.DataFrame(trades)
if not df_trades.empty and "Return" in df_trades.columns and "Return %" not in df_trades.columns:
    df_trades["Return %"] = df_trades["Return"] * 100
closed = df_trades[df_trades["Result"].isin(["WIN", "LOSS", "TWIN", "TLOSS"])] if not df_trades.empty else pd.DataFrame()

if not closed.empty:
    closed = closed.copy()
    closed["Entry Time"] = pd.to_datetime(closed["Entry Time"])
    closed["Exit Time"]  = pd.to_datetime(closed["Exit Time"])


@st.fragment
def _render_hurst_section(closed, df_ind, close, ticker):
    show_hurst = st.checkbox("Show Hurst analysis")

    if show_hurst:
        _wl_set = set(w["ticker"] for w in (get_watchlist() or []))
        with st.spinner("Computing Hurst..."):
            h_series = get_hurst(ticker, is_watchlist=(ticker in _wl_set))
        show_adf = st.checkbox("Show rolling ADF p-value (slow on first load)")
        adf_series = rolling_adf(ticker) if show_adf else None

        h_cutoff = st.slider("Suppress entries where H ≥", min_value=0.30, max_value=0.70,
                             value=0.50, step=0.01, format="%.2f")
        st.caption("Hurst filter")

        if not closed.empty:
            cwh = closed.copy()
            if not h_series.empty:
                cwh["H_at_entry"] = cwh["Entry Time"].apply(
                    lambda t: h_series.asof(t) if t >= h_series.index[0] else np.nan
                )
            else:
                cwh["H_at_entry"] = np.nan
            cwh["allowed"] = cwh["H_at_entry"].isna() | (cwh["H_at_entry"] < h_cutoff)
            allowed    = cwh[cwh["allowed"]]
            suppressed = cwh[~cwh["allowed"]]
        else:
            h_series = pd.Series(dtype=float)
            allowed = suppressed = pd.DataFrame()
    else:
        h_series = pd.Series(dtype=float)
        adf_series = None
        allowed = closed
        suppressed = pd.DataFrame()

    # -----------------------------------------------------------------------
    # Chart
    # -----------------------------------------------------------------------
    st.subheader("Price + Bands")

    if show_hurst:
        n_rows = 3 if adf_series is not None else 2
        row_heights = [0.62, 0.19, 0.19] if n_rows == 3 else [0.75, 0.25]
        subplot_titles = ("", "Hurst (100 bars)", "ADF p-value (100 bars)") if n_rows == 3 else ("", "Hurst (100 bars)")
    else:
        n_rows = 1
        row_heights = [1.0]
        subplot_titles = ("",)

    fig = make_subplots(rows=n_rows, cols=1, shared_xaxes=True,
                        row_heights=row_heights, vertical_spacing=0.03,
                        subplot_titles=subplot_titles)

    close_4h = close.resample('4h').last().dropna()
    fig.add_trace(go.Scatter(
        x=close_4h.index, y=close_4h.values,
        name="Price", line=dict(color="#aaaaaa", width=1),
    ), row=1, col=1)

    if df_ind is not None:
        sma = df_ind['SMA'].reindex(close.index, method='ffill')
        std = df_ind['Std'].reindex(close.index, method='ffill')
        for z, (color, dash) in {2.0: ("#4a9eff","dash"), 2.5: ("#ff9f4a","dot"), 3.0: ("#cc44ff","dashdot")}.items():
            upper = (sma + z * std).resample('4h').last().dropna()
            lower = (sma - z * std).resample('4h').last().dropna()
            fig.add_trace(go.Scatter(x=upper.index, y=upper.values, name=f"z={z} upper",
                                     line=dict(color=color, width=1, dash=dash), opacity=0.7), row=1, col=1)
            fig.add_trace(go.Scatter(x=lower.index, y=lower.values, showlegend=False,
                                     line=dict(color=color, width=1, dash=dash), opacity=0.7), row=1, col=1)

    win_label  = "Win (H-filtered)" if show_hurst else "Win"
    loss_label = "Loss (H-filtered)" if show_hurst else "Loss"
    for subset, color, symbol, label in [
        (allowed[allowed["Result"].isin(["WIN","TWIN"])],   "#00cc66", "triangle-up",   win_label),
        (allowed[allowed["Result"].isin(["LOSS","TLOSS"])], "#ff4444", "triangle-down", loss_label),
    ]:
        if not subset.empty:
            fig.add_trace(go.Scatter(x=subset["Entry Time"], y=subset["Entry Price"],
                                     mode="markers", marker=dict(symbol=symbol, size=10, color=color),
                                     name=label), row=1, col=1)

    if not suppressed.empty:
        fig.add_trace(go.Scatter(x=suppressed["Entry Time"], y=suppressed["Entry Price"],
                                 mode="markers",
                                 marker=dict(symbol="circle-open", size=10, color="#888888", line=dict(width=2)),
                                 name="Suppressed (H≥cutoff)"), row=1, col=1)

    if not closed.empty:
        fig.add_trace(go.Scatter(x=allowed["Exit Time"], y=allowed["Exit Price"],
                                 mode="markers", marker=dict(symbol="x", size=8, color="#ffffff", opacity=0.7),
                                 name="Exit"), row=1, col=1)
        for _, tr in allowed.iterrows():
            fig.add_vrect(x0=tr["Entry Time"], x1=tr["Exit Time"],
                          fillcolor="#00cc66" if tr["Result"] in ["WIN","TWIN"] else "#ff4444",
                          opacity=0.07, line_width=0, row=1, col=1)
        for _, tr in suppressed.iterrows():
            fig.add_vrect(x0=tr["Entry Time"], x1=tr["Exit Time"],
                          fillcolor="#888888", opacity=0.04,
                          line=dict(color="#888888", width=1, dash="dot"), row=1, col=1)

    if show_hurst and not h_series.empty:
        fig.add_trace(go.Scatter(x=h_series.index, y=h_series.values,
                                 name="Hurst (100 bars)", line=dict(color="#ffdd44", width=1.5)), row=2, col=1)
        fig.add_hline(y=0.5,      line=dict(color="#555555", dash="dash",  width=1), row=2, col=1)
        fig.add_hline(y=h_cutoff, line=dict(color="#ff8800", dash="solid", width=1.5), row=2, col=1)

    if adf_series is not None:
        fig.add_trace(go.Scatter(x=adf_series.index, y=adf_series.values,
                                 name="ADF p-value (100 bars)", line=dict(color="#44ddff", width=1.5)), row=3, col=1)
        fig.add_hline(y=0.05, line=dict(color="#888888", dash="dash", width=1), row=3, col=1)

    fig.update_layout(height=800,
                      legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0),
                      margin=dict(l=50, r=20, t=30, b=20),
                      hovermode="x unified", xaxis_rangeslider_visible=False)
    fig.update_yaxes(title_text="Price", row=1, col=1)
    if show_hurst:
        fig.update_yaxes(title_text="Hurst", row=2, col=1, range=[0, 1])
    if adf_series is not None:
        fig.update_yaxes(title_text="ADF p", row=3, col=1, range=[0, 1])

    st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True})

    # -----------------------------------------------------------------------
    # Metrics
    # -----------------------------------------------------------------------
    def _metrics(df, label):
        if df.empty:
            st.caption(f"{label}: no trades")
            return
        total = len(df)
        pw  = len(df[df["Result"] == "WIN"])
        tw  = len(df[df["Result"] == "TWIN"])
        ret = ((df["Return %"] / 100.0 + 1).prod() - 1) * 100 if "Return %" in df.columns else 0.0
        wr  = (pw + tw) / total * 100
        st.caption(label)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Trades", total)
        c2.metric("Win Rate", f"{wr:.1f}%")
        c3.metric("Compounded Return", f"{ret:+.2f}%")
        c4.metric("Time Exits", len(df[df["Result"].isin(["TWIN","TLOSS"])]))

    if not closed.empty:
        if show_hurst:
            col_all, col_filt = st.columns(2)
            with col_all:
                _metrics(closed, "All trades (unfiltered)")
            with col_filt:
                _metrics(allowed, f"H-filtered (H < {h_cutoff:.2f})")
        else:
            _metrics(closed, "All trades")

        if not allowed.empty:
            quarter_label = "By Quarter (H-filtered)" if show_hurst else "By Quarter"
            st.markdown(f"#### {quarter_label}")
            aq = allowed.copy()
            aq["Quarter"] = aq["Entry Time"].dt.to_period("Q").astype(str)
            rows_q = []
            for q, g in aq.groupby("Quarter"):
                pw  = len(g[g["Result"] == "WIN"])
                tw  = len(g[g["Result"] == "TWIN"])
                ret = ((g["Return %"] / 100.0 + 1).prod() - 1) * 100 if "Return %" in g.columns else 0.0
                rows_q.append({"Quarter": q, "Trades": len(g), "Win %": (pw+tw)/len(g)*100, "Return %": ret})
            st.dataframe(pd.DataFrame(rows_q).sort_values("Quarter", ascending=False),
                         hide_index=True, use_container_width=True,
                         column_config={"Win %": st.column_config.NumberColumn(format="%.1f%%"),
                                        "Return %": st.column_config.NumberColumn(format="%+.2f%%")})

        st.markdown("#### Trade Log")
        log_cols = [c for c in ["Entry Time", "Entry Price", "Exit Time", "Exit Price",
                                 "Result", "H_at_entry", "Return %"] if c in closed.columns]
        st.dataframe(closed[log_cols].copy(), hide_index=True, use_container_width=True,
                     column_config={"Entry Price": st.column_config.NumberColumn(format="$%.2f"),
                                    "Exit Price":  st.column_config.NumberColumn(format="$%.2f"),
                                    "H_at_entry":  st.column_config.NumberColumn(format="%.3f"),
                                    "Return %":    st.column_config.NumberColumn(format="%+.2f%%")})
    else:
        st.warning("No closed trades for this node.")


_render_hurst_section(closed, df_ind, close, selected_ticker)
