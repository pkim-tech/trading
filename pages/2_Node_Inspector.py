import streamlit as st
import pandas as pd
import sqlite3
import os
from pathlib import Path
from backtester import run_backtest
import strategies

DB_PATH = "./cache/trading_universe.db"
CACHE_DIR = Path("./cache")

st.set_page_config(layout="wide", page_title="Precise Coordinate Node Inspector")


@st.cache_data(ttl=60)
def get_dropdown_options():
    if not os.path.exists(DB_PATH):
        return [], [], []
    with sqlite3.connect(DB_PATH) as conn:
        tickers = [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM backtest_cache ORDER BY ticker").fetchall()]
        strats = [r[0] for r in conn.execute("SELECT DISTINCT strategy FROM backtest_cache ORDER BY strategy").fetchall()]
        versions = [r[0] for r in conn.execute("SELECT DISTINCT version FROM backtest_cache ORDER BY version DESC").fetchall()]
    return tickers, strats, versions


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


st.title("Node Inspector")

tick_opts, strat_opts, ver_opts = get_dropdown_options()

if not tick_opts:
    st.info("The historical matrix is empty. Complete an initial sweep task loop to begin analyzing detailed node pathways.")
    st.stop()

has_target = "target_node" in st.session_state
t_node = st.session_state.get("target_node", {})

c1, c2, c3 = st.columns(3)

with c1:
    t_tick = t_node.get("ticker") if has_target and t_node.get("ticker") in tick_opts else tick_opts[0]
    selected_ticker = st.selectbox("Ticker", tick_opts, index=tick_opts.index(t_tick))

with c2:
    t_strat = t_node.get("strategy") if has_target and t_node.get("strategy") in strat_opts else strat_opts[0]
    selected_strategy = st.selectbox("Strategy", strat_opts, index=strat_opts.index(t_strat))

with c3:
    t_ver = t_node.get("version") if has_target and t_node.get("version") in ver_opts else ver_opts[0]
    selected_version = st.selectbox("Version", ver_opts, index=ver_opts.index(t_ver))

df_slice = get_slice(selected_ticker, selected_strategy, selected_version)

if df_slice.empty:
    st.warning("No run coordinates match your active configuration selections.")
else:
    st.markdown("---")
    st.subheader("Isolate Specific Coordinate Vectors")

    c_w, c_tp, c_sl, c_hold, c_zt = st.columns(5)

    with c_w:
        w_opts = sorted(df_slice["window"].unique())
        t_w = t_node.get("window") if has_target and t_node.get("window") in w_opts else w_opts[0]
        target_w = st.selectbox("Lookback Window", w_opts, index=w_opts.index(t_w))

    with c_tp:
        tp_opts = sorted(df_slice["take_profit"].unique())
        t_tp = t_node.get("take_profit") if has_target and t_node.get("take_profit") in tp_opts else tp_opts[0]
        target_tp = st.selectbox("Take Profit (%)", tp_opts, index=tp_opts.index(t_tp))

    with c_sl:
        sl_opts = sorted(df_slice["stop_loss"].unique())
        t_sl = t_node.get("stop_loss") if has_target and t_node.get("stop_loss") in sl_opts else sl_opts[0]
        target_sl = st.selectbox("Stop Loss (%)", sl_opts, index=sl_opts.index(t_sl))

    with c_hold:
        h_opts = sorted(df_slice["max_hold_hours"].unique())
        t_h = t_node.get("max_hold_hours") if has_target and t_node.get("max_hold_hours") in h_opts else h_opts[0]
        target_hold = st.selectbox("Max Hold Hours", h_opts, index=h_opts.index(t_h))

    with c_zt:
        zt_opts = sorted(df_slice["z_score_threshold"].unique())
        t_zt = t_node.get("z_score_threshold", 2.0) if has_target else 2.0
        t_zt = t_zt if t_zt in zt_opts else zt_opts[0]
        target_zt = st.selectbox("Z Threshold", zt_opts, index=zt_opts.index(t_zt))

    if has_target:
        del st.session_state["target_node"]

    cache_path = CACHE_DIR / f"{selected_ticker}_1h.csv"
    if not cache_path.exists():
        st.error(f"Missing ingestion data for {selected_ticker} inside local cache targets.")
    else:
        with st.spinner("Running backtest..."):
            df_hourly_raw = pd.read_csv(cache_path, index_col=0, parse_dates=True).sort_index()
            strat_class = getattr(strategies, selected_strategy, None)
            close_col = 'Adj Close' if 'Adj Close' in df_hourly_raw.columns else 'Close'
            df_daily = df_hourly_raw.resample('D').last().dropna(subset=[close_col])

            strat_instance = strat_class(window=int(target_w), z_score_threshold=float(target_zt))
            df_daily_processed = strat_instance.generate_daily_indicators(df_daily)

            trades = run_backtest(
                df_hourly_raw, df_daily_processed, selected_ticker,
                take_profit=float(target_tp / 100.0), stop_loss=float(target_sl / 100.0),
                max_hours_to_hold=int(target_hold), z_score_threshold=float(target_zt)
            )

            df_trades = pd.DataFrame(trades)

        if df_trades.empty:
            st.warning("This node generated 0 trades.")
        else:
            df_trades["Entry Time"] = pd.to_datetime(df_trades["Entry Time"])
            if "Return %" not in df_trades.columns:
                df_trades["Return %"] = df_trades["Return"] * 100 if "Return" in df_trades.columns else 0.0

            total_trades = len(df_trades)
            pure_wins = len(df_trades[df_trades["Result"] == "WIN"])
            pure_losses = len(df_trades[df_trades["Result"] == "LOSS"])
            timeout_wins = len(df_trades[df_trades["Result"] == "TWIN"])
            timeout_losses = len(df_trades[df_trades["Result"] == "TLOSS"])

            total_timeouts = timeout_wins + timeout_losses
            effective_win_rate = ((pure_wins + timeout_wins) / total_trades) * 100 if total_trades > 0 else 0.0
            comp_ret = ((df_trades['Return %'] / 100.0 + 1).prod() - 1) * 100

            st.markdown("### Consolidated Node Profile Analytics")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total Trades", f"{total_trades}")
            m2.metric("Effective Win Rate", f"{effective_win_rate:.1f}%", help=f"Pure Wins: {pure_wins} | TWIN: {timeout_wins}")
            m3.metric("Compounded Return", f"{comp_ret:+.2f}%")
            m4.metric("Time Exits", f"{total_timeouts}", help=f"TWIN: {timeout_wins} | TLOSS: {timeout_losses}")

            st.markdown("---")
            st.markdown("### Performance by Quarter")

            df_trades['Quarter'] = df_trades['Entry Time'].dt.to_period('Q').astype(str)

            quarterly_summary = []
            for q, group in df_trades.groupby('Quarter'):
                q_trades = len(group)
                q_pure_wins = len(group[group['Result'] == 'WIN'])
                q_twin = len(group[group['Result'] == 'TWIN'])
                q_wr = ((q_pure_wins + q_twin) / q_trades) * 100 if q_trades > 0 else 0.0
                q_ret = ((group['Return %'] / 100.0 + 1).prod() - 1) * 100
                q_timeouts = len(group[group['Result'].isin(['TWIN', 'TLOSS'])])
                quarterly_summary.append({
                    "Quarter": q, "Trades": q_trades, "Win %": q_wr,
                    "Return %": q_ret, "Time Exits": q_timeouts
                })

            df_q = pd.DataFrame(quarterly_summary).sort_values("Quarter", ascending=False)
            st.dataframe(df_q.style.format({
                "Win %": "{:.1f}%", "Return %": "{:+.2f}%", "Trades": "{:.0f}", "Time Exits": "{:.0f}"
            }), hide_index=True, use_container_width=True)

            st.markdown("---")
            st.markdown("### Trade Log")

            hold_col = "hours_held" if "hours_held" in df_trades.columns else ("Hold Hours" if "Hold Hours" in df_trades.columns else None)
            ledger_cols = ["Entry Time", "Entry Price", "Exit Time", "Exit Price", "Result"]
            if hold_col:
                ledger_cols.append(hold_col)
            ledger_cols.append("Return %")

            display_ledger = df_trades[ledger_cols].copy()
            format_dict = {"Entry Price": "${:,.2f}", "Exit Price": "${:,.2f}", "Return %": "{:+.2f}%"}
            if hold_col:
                display_ledger.rename(columns={hold_col: "Bars Held"}, inplace=True)
                format_dict["Bars Held"] = "{:.0f} bars"

            st.dataframe(display_ledger.style.format(format_dict), hide_index=True, use_container_width=True)

    st.markdown("---")
    st.subheader(f"Top 50 — {selected_ticker}")

    top_50 = df_slice.nlargest(50, "alpha_vs_spy")[
        ["take_profit", "stop_loss", "max_hold_hours", "window", "strategy_return", "win_rate", "trades", "asset_bh", "alpha_vs_spy"]
    ].reset_index(drop=True)

    if not top_50.empty:
        st.dataframe(
            top_50.style.format({
                "take_profit": "{:,.0f}%", "stop_loss": "{:,.0f}%", "max_hold_hours": "{:,.0f}h",
                "window": "{:,.0f}w", "strategy_return": "{:+.2f}%", "win_rate": "{:.1f}%",
                "trades": "{:,.0f}", "asset_bh": "{:+.2f}%", "alpha_vs_spy": "{:+.2f}%"
            }),
            hide_index=False,
            use_container_width=True,
            height=500
        )
    else:
        st.caption("No nodes found.")
