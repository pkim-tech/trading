import streamlit as st
import sqlite3
import numpy as np
import pandas as pd
from pathlib import Path
from backtester import run_backtest
import strategies
from statsmodels.tsa.stattools import adfuller

st.set_page_config(layout="wide", page_title="ADF Filter")
st.title("ADF Filter Sweep")

DB_PATH  = "./cache/research/trading_universe.db"
STRATEGY = "ZScoreBreakout"

c1, c2, c3, c4 = st.columns(4)
MIN_TRADES  = c1.number_input("Min trades", value=5, min_value=1)
MIN_RETURN  = c2.number_input("Min max return %", value=100.0, step=50.0)
MIN_BH_MULT = c3.number_input("Min B&H mult", value=2.0, step=0.5)
ADF_WINDOW  = c4.number_input("ADF window (bars)", value=200, min_value=50, step=50)


@st.cache_data(ttl=86400)
def load_qualifying_nodes(min_trades, min_return, min_bh_mult):
    with sqlite3.connect(DB_PATH) as conn:
        single_stock = {r[0] for r in conn.execute(
            "SELECT symbol FROM tickers WHERE stock_underlier IS NOT NULL AND stock_underlier != ''"
        ).fetchall()}
        rows = conn.execute("""
            WITH best AS (
                SELECT ticker, window, COALESCE(z_score_threshold, 2.0) AS z,
                       take_profit, stop_loss, max_hold_hours, strategy_return, trades,
                       CASE WHEN asset_bh > 0 THEN strategy_return / asset_bh END AS bh_mult,
                       ROW_NUMBER() OVER (PARTITION BY ticker, window, COALESCE(z_score_threshold,2.0)
                                          ORDER BY strategy_return DESC) AS rn
                FROM backtest_cache WHERE version = 'v1.5' AND trades >= ?
            )
            SELECT ticker, window, z, take_profit, stop_loss, max_hold_hours, strategy_return, trades, bh_mult
            FROM best WHERE rn = 1 AND strategy_return >= ? AND bh_mult >= ?
        """, (min_trades, min_return / 100.0, min_bh_mult)).fetchall()
    return [r for r in rows if r[0] not in single_stock]


@st.cache_data(ttl=3600)
def run_adf_sweep(nodes, adf_window):
    results = []
    for ticker, window, z, tp, sl, hold, base_ret_db, n_trades, bh_mult in nodes:
        csv = Path(f"cache/research/{ticker}_1h.csv")
        if not csv.exists():
            continue
        df_h = pd.read_csv(csv, index_col=0, parse_dates=True)
        close_col = "Adj Close" if "Adj Close" in df_h.columns else "Close"
        df_daily = df_h.resample("D").last().dropna(subset=[close_col])
        strat = getattr(strategies, STRATEGY)(window=int(window), z_score_threshold=float(z))
        df_ind = strat.generate_daily_indicators(df_daily)
        trades = run_backtest(df_h, df_ind, ticker,
                              take_profit=tp/100, stop_loss=sl/100,
                              max_hours_to_hold=int(hold), z_score_threshold=float(z))
        closed = pd.DataFrame([t for t in trades if t["Result"] in ("WIN","LOSS","TWIN","TLOSS")])
        if closed.empty:
            continue
        closed["Entry Time"] = pd.to_datetime(closed["Entry Time"])
        closed["Return"] = closed["Return"].astype(float)
        unfiltered = (closed["Return"] + 1).prod() - 1

        prices = df_h[close_col].dropna()

        def adf_at(t):
            idx = prices.index.searchsorted(t)
            if idx < adf_window:
                return np.nan
            try:
                return adfuller(prices.iloc[idx - adf_window:idx].values, maxlag=1, autolag=None)[1]
            except Exception:
                return np.nan

        closed["adf_p"] = closed["Entry Time"].apply(adf_at)

        best_stat = {"ret": unfiltered, "cut": "-", "n": len(closed)}
        best_nonstat = {"ret": unfiltered, "cut": "-", "n": len(closed)}

        for cutoff in np.arange(0.05, 0.55, 0.05):
            for mode, mask in [
                ("stat",    closed["adf_p"].isna() | (closed["adf_p"] <  cutoff)),
                ("nonstat", closed["adf_p"].isna() | (closed["adf_p"] >= cutoff)),
            ]:
                f = closed[mask]
                if len(f) == 0:
                    continue
                ret = (f["Return"] + 1).prod() - 1
                best = best_stat if mode == "stat" else best_nonstat
                if ret > best["ret"]:
                    best["ret"] = ret
                    best["cut"] = round(cutoff, 2)
                    best["n"]   = len(f)

        results.append({
            "ticker":       ticker,
            "w":            int(window),
            "z":            z,
            "base_%":       round(unfiltered * 100, 1),
            "n_trades":     len(closed),
            "stat_%":       round(best_stat["ret"] * 100, 1),
            "stat_cut":     best_stat["cut"],
            "stat_trades":  best_stat["n"],
            "nonstat_%":    round(best_nonstat["ret"] * 100, 1),
            "nonstat_cut":  best_nonstat["cut"],
            "nonstat_trades": best_nonstat["n"],
        })
    return pd.DataFrame(results)


nodes = load_qualifying_nodes(int(MIN_TRADES), MIN_RETURN, MIN_BH_MULT)
st.caption(f"{len(nodes)} qualifying nodes across {len(set(r[0] for r in nodes))} tickers")

col_btn1, col_btn2 = st.columns([1, 5])
if col_btn2.button("Clear results"):
    st.session_state.pop("adf_sweep_result", None)
    st.rerun()

if col_btn1.button("Run sweep") or "adf_sweep_result" in st.session_state:
    if "adf_sweep_result" not in st.session_state:
        with st.spinner("Running..."):
            st.session_state["adf_sweep_result"] = run_adf_sweep(
                tuple(nodes), int(ADF_WINDOW)
            )
    df = st.session_state["adf_sweep_result"]

    df["stat_delta"]    = df["stat_%"]    - df["base_%"]
    df["nonstat_delta"] = df["nonstat_%"] - df["base_%"]
    df = df.sort_values("base_%", ascending=False)

    st.subheader("Results")
    st.dataframe(df, use_container_width=True, hide_index=True)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Nodes where stationary filter helps",    int((df["stat_delta"] > 0).sum()))
    c2.metric("Avg stationary delta",                   f"{df['stat_delta'].mean():+.1f}%")
    c3.metric("Nodes where non-stationary filter helps", int((df["nonstat_delta"] > 0).sum()))
    c4.metric("Avg non-stationary delta",               f"{df['nonstat_delta'].mean():+.1f}%")
