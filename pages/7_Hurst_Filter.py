import streamlit as st
import sqlite3
import numpy as np
import pandas as pd
from pathlib import Path
from backtester import run_backtest
import strategies
from hurst import _hurst_vectorized
from db_cache import get_kv

st.set_page_config(layout="wide", page_title="Hurst Filter")
st.title("Hurst Filter Sweep")

DB_PATH = "./cache/trading_universe.db"

c1, c2, c3, c4 = st.columns(4)
MIN_TRADES  = c1.number_input("Min trades", value=5, min_value=1)
MIN_RETURN  = c2.number_input("Min max return %", value=100.0, step=50.0)
MIN_BH_MULT = c3.number_input("Min B&H mult", value=2.0, step=0.5)
HURST_WINS  = c4.multiselect("Hurst windows", [100, 150, 200], default=[100, 150, 200])
STRATEGY    = "ZScoreBreakout"


@st.cache_data(ttl=86400)
def load_qualifying_nodes(min_trades, min_return, min_bh_mult):
    with sqlite3.connect(DB_PATH) as conn:
        single_stock = {r[0] for r in conn.execute(
            "SELECT symbol FROM tickers WHERE stock_underlier IS NOT NULL AND stock_underlier != ''"
        ).fetchall()}
        rows = conn.execute("""
            WITH best AS (
                SELECT ticker, window, COALESCE(z_score_threshold, 2.0) AS z,
                       take_profit, stop_loss, max_hold_hours, strategy_return, trades, alpha_vs_spy, asset_bh,
                       CASE WHEN asset_bh > 0 THEN strategy_return / asset_bh END AS bh_mult,
                       ROW_NUMBER() OVER (PARTITION BY ticker, window, COALESCE(z_score_threshold,2.0)
                                          ORDER BY strategy_return DESC) AS rn
                FROM backtest_cache
                WHERE version = 'v1.5' AND trades >= ?
            )
            SELECT ticker, window, z, take_profit, stop_loss, max_hold_hours,
                   strategy_return, trades, bh_mult
            FROM best
            WHERE rn = 1 AND strategy_return >= ? AND bh_mult >= ?
        """, (min_trades, min_return / 100.0, min_bh_mult)).fetchall()
    return [r for r in rows if r[0] not in single_stock]


@st.cache_data(ttl=3600)
def run_hurst_sweep(nodes, hurst_windows):
    results = []
    for ticker, window, z, tp, sl, hold, base_ret, n_trades, bh_mult in nodes:
        csv = Path(f"cache/{ticker}_1h.csv")
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

        log_p = np.log(df_h[close_col].dropna().values)
        price_idx = df_h[close_col].dropna().index

        best_mr = {"ret": unfiltered, "hw": "-", "cut": "-", "n": len(closed)}
        best_mo = {"ret": unfiltered, "hw": "-", "cut": "-", "n": len(closed)}

        for hw in hurst_windows:
            h_vals = _hurst_vectorized(log_p, hw)
            h_series = pd.Series(h_vals, index=price_idx)
            closed["hurst"] = closed["Entry Time"].apply(
                lambda t: h_series.asof(t) if not h_series.empty and t >= h_series.index[0] else np.nan
            )
            for cutoff in np.arange(0.30, 0.75, 0.05):
                for mode, mask in [
                    ("mr", closed["hurst"].isna() | (closed["hurst"] <  cutoff)),
                    ("mo", closed["hurst"].isna() | (closed["hurst"] >= cutoff)),
                ]:
                    f = closed[mask]
                    if len(f) == 0:
                        continue
                    ret = (f["Return"] + 1).prod() - 1
                    best = best_mr if mode == "mr" else best_mo
                    if ret > best["ret"]:
                        best["ret"] = ret
                        best["hw"]  = hw
                        best["cut"] = round(cutoff, 2)
                        best["n"]   = len(f)

        results.append({
            "ticker":         ticker,
            "w":              int(window),
            "z":              z,
            "tp":             tp,
            "sl":             sl,
            "hold":           int(hold),
            "base_%":         round(unfiltered * 100, 1),
            "n_trades":       len(closed),
            "mr_best_%":      round(best_mr["ret"] * 100, 1),
            "mr_hw":          best_mr["hw"],
            "mr_cut":         best_mr["cut"],
            "mr_trades":      best_mr["n"],
            "mo_best_%":      round(best_mo["ret"] * 100, 1),
            "mo_hw":          best_mo["hw"],
            "mo_cut":         best_mo["cut"],
            "mo_trades":      best_mo["n"],
        })
    return pd.DataFrame(results)


nodes = load_qualifying_nodes(int(MIN_TRADES), MIN_RETURN, MIN_BH_MULT)
st.caption(f"{len(nodes)} qualifying nodes across {len(set(r[0] for r in nodes))} tickers")

col_btn1, col_btn2 = st.columns([1, 5])
if col_btn2.button("Clear results"):
    st.session_state.pop("hurst_sweep_result", None)
    st.rerun()
if col_btn1.button("Run sweep") or "hurst_sweep_result" in st.session_state:
    if "hurst_sweep_result" not in st.session_state:
        with st.spinner("Running..."):
            st.session_state["hurst_sweep_result"] = run_hurst_sweep(
                tuple(nodes), tuple(HURST_WINS)
            )
    df = st.session_state["hurst_sweep_result"]

    df["mr_delta"] = df["mr_best_%"] - df["base_%"]
    df["mo_delta"] = df["mo_best_%"] - df["base_%"]
    df = df.sort_values("base_%", ascending=False)

    st.subheader("Results")
    st.dataframe(df, use_container_width=True, hide_index=True)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Nodes where MR filter helps", int((df["mr_delta"] > 0).sum()))
    c2.metric("Avg MR delta", f"{df['mr_delta'].mean():+.1f}%")
    c3.metric("Nodes where MO filter helps", int((df["mo_delta"] > 0).sum()))
    c4.metric("Avg MO delta", f"{df['mo_delta'].mean():+.1f}%")
