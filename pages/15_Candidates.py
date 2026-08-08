import sqlite3
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from candidate_summary_report import (
    best_node, worst_neighbor, overlay_summary, annualized_excess, fill_accuracy_summary,
    liquidity_dollars_per_day, COLUMN_DEFS,
)

DB_PATH = "./cache/research/trading_universe.db"

st.set_page_config(layout="wide", page_title="Candidates")
st.title("Candidate Comparison")
st.caption(
    "Interactive version of scripts/candidate_summary_report.py -- sort/reorder/filter columns "
    "directly (click a header to sort, drag to reorder, use the toolbar to search or download CSV) "
    "instead of asking for a re-run with different column ordering."
)


@st.cache_data(ttl=300)
def load_tickers():
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("SELECT DISTINCT ticker FROM candidate_nodes ORDER BY ticker").fetchall()
    return [r[0] for r in rows]


@st.cache_data(ttl=300)
def load_backtest_versions():
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("SELECT DISTINCT version FROM backtest_cache ORDER BY version").fetchall()
    return [r[0] for r in rows]


all_tickers = load_tickers()
versions = load_backtest_versions()

c1, c2, c3 = st.columns([3, 1, 1])
with c1:
    tickers = st.multiselect("Tickers", options=all_tickers, default=all_tickers)
with c2:
    version = st.selectbox("Version", options=versions, index=versions.index("v5") if "v5" in versions else 0)
with c3:
    run_5min = st.toggle("5-min fill accuracy", value=False,
                          help="Hits yfinance once per ticker -- off by default for speed.")

if not tickers:
    st.info("Select at least one ticker.")
    st.stop()


@st.cache_data(ttl=600, show_spinner="Building candidate report...")
def build_report(tickers, version, run_5min):
    conn = sqlite3.connect(DB_PATH)
    rows = []
    for ticker in tickers:
        node = best_node(conn, ticker, version)
        if node is None:
            rows.append({"ticker": ticker, "status": "NO_DATA"})
            continue
        (strategy, window, z, entry_timing, sl, tp, hold, tb, ts,
         ralpha, sret, trades, wr, spy_bh) = node
        wn = worst_neighbor(conn, ticker, version, strategy, window, z,
                             entry_timing, sl, tp, hold, tb, ts)
        addon = overlay_summary(conn, ticker, "addon")
        drought = overlay_summary(conn, ticker, "drought")
        cliff = "CLIFF" if (wn is not None and wn < 0) else ("SAFE" if wn is not None else "?")
        days, ann_excess = annualized_excess(ticker, sret, spy_bh)
        years = round(days / 365.25, 2) if days else None
        fill_acc = fill_accuracy_summary(ticker, strategy, window, z, tb, hold) if run_5min else None
        core_mult = 1 + sret / 100
        addon_mult = (core_mult * (1 + addon[1] / 100) - 1) * 100 if addon else None
        drought_mult = (core_mult * (1 + drought[1] / 100) - 1) * 100 if drought else None
        liquidity = liquidity_dollars_per_day(conn, ticker)
        rows.append({
            "ticker": ticker, "liquidity_dollars_per_day": liquidity,
            "strategy": strategy, "core_alpha_pct": ralpha, "abs_return_pct": sret,
            "years": years, "trades": trades, "ann_excess_pct": ann_excess,
            "fillacc_possible_win_pct": fill_acc[0] if fill_acc else None,
            "fillacc_mean_err_pct": fill_acc[1] if fill_acc else None,
            "fillacc_n": fill_acc[2] if fill_acc else None,
            "worst_neighbor_pct": wn, "status": cliff,
            "addon_n": addon[0] if addon else None,
            "addon_compounded_pct": addon[1] if addon else None,
            "addon_win_rate_pct": addon[2] if addon else None,
            "drought_n": drought[0] if drought else None,
            "drought_compounded_pct": drought[1] if drought else None,
            "drought_win_rate_pct": drought[2] if drought else None,
            "x_addon_pct": addon_mult, "x_drought_pct": drought_mult,
            "window": window, "z": z, "trail_buy_pct": tb, "trail_sell_pct": ts,
            "entry_timing": entry_timing, "stop_loss": sl, "take_profit_or_arm": tp, "max_hold_hours": hold,
        })
    conn.close()
    return pd.DataFrame(rows)


df = build_report(tuple(sorted(tickers)), version, run_5min)

st.dataframe(
    df,
    use_container_width=True,
    hide_index=True,
    column_config={
        "liquidity_dollars_per_day": st.column_config.NumberColumn("Liquidity $/day", format="$%.0f"),
        "core_alpha_pct": st.column_config.NumberColumn("Core Alpha %", format="%+.1f%%"),
        "abs_return_pct": st.column_config.NumberColumn("Abs Return %", format="%+.1f%%"),
        "ann_excess_pct": st.column_config.NumberColumn("Annualized Excess %", format="%+.1f%%"),
        "fillacc_possible_win_pct": st.column_config.NumberColumn("Fill-Acc Win %", format="%.0f%%"),
        "fillacc_mean_err_pct": st.column_config.NumberColumn("Fill-Acc Mean Err %", format="%.3f%%"),
        "worst_neighbor_pct": st.column_config.NumberColumn("Worst Neighbor %", format="%+.1f%%"),
        "addon_compounded_pct": st.column_config.NumberColumn("Addon Compounded %", format="%+.2f%%"),
        "addon_win_rate_pct": st.column_config.NumberColumn("Addon Win %", format="%.0f%%"),
        "drought_compounded_pct": st.column_config.NumberColumn("Drought Compounded %", format="%+.2f%%"),
        "drought_win_rate_pct": st.column_config.NumberColumn("Drought Win %", format="%.0f%%"),
        "x_addon_pct": st.column_config.NumberColumn("x Addon %", format="%+.1f%%"),
        "x_drought_pct": st.column_config.NumberColumn("x Drought %", format="%+.1f%%"),
    },
)

st.caption(
    "Ann Excess % is CAGR-based (fair across tickers with different cached-history lengths) -- "
    "check Years/Trades before trusting a large value from a short/thin sample (e.g. SPCL: ~3000%+ "
    "off 0.31y/3 trades is an annualization artifact, not a real signal). "
    "x Addon %/x Drought % are a NAIVE multiplicative estimate, not real stacked-model math -- "
    "see candidate_summary_report.py's docstring. Liquidity should be the FIRST-pass filter, not an "
    "afterthought (see the 2026-08-07 'liquidity was never the limiting filter' finding)."
)

with st.expander("Column definitions"):
    for col, definition in COLUMN_DEFS.items():
        st.markdown(f"**{col}** -- {definition}")
