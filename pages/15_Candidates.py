import sqlite3
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from candidate_summary_report import build_rows_for_ticker, _row_to_record, COLUMN_DEFS, ensure_candidate_nodes_table, ensure_overlay_table

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

c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
with c1:
    tickers = st.multiselect("Tickers", options=all_tickers, default=all_tickers)
with c2:
    version = st.selectbox("Version", options=versions, index=versions.index("v5") if "v5" in versions else 0)
with c3:
    run_5min = st.toggle("5-min fill accuracy", value=False,
                          help="Hits yfinance once per ticker -- off by default for speed.")
with c4:
    run_overlay = st.toggle("Compute overlay", value=False,
                             help="Runs the real drought/addon overlay backtest on demand for any of the "
                                  "3 candidate rows that don't have it yet -- off by default, can take a "
                                  "few seconds per ticker.")

min_alpha = st.number_input("Min alpha floor (for 'best safe node' search)", value=200.0, step=50.0)

if not tickers:
    st.info("Select at least one ticker.")
    st.stop()


@st.cache_data(ttl=600, show_spinner="Building candidate report...")
def build_report(tickers, version, min_alpha, skip_5min, skip_overlay):
    conn = sqlite3.connect(DB_PATH)
    ensure_candidate_nodes_table(conn)
    ensure_overlay_table(conn)
    rows = []
    for ticker in tickers:
        rows.extend(build_rows_for_ticker(conn, ticker, version, min_alpha, skip_5min, skip_overlay))
    conn.close()
    return pd.DataFrame([_row_to_record(r) for r in rows])


df = build_report(tuple(sorted(tickers)), version, min_alpha, not run_5min, not run_overlay)

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
        "fillacc_possible_mean_err_pct": st.column_config.NumberColumn("Fill-Acc Mean Err %", format="%.3f%%"),
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
