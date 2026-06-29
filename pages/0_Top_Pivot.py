import sqlite3
import streamlit as st
import pandas as pd
from db_cache import get_kv

DB_PATH = "./cache/trading_universe.db"
WINDOWS = [10, 20, 30]
Z_THRESHOLDS = [2.0, 2.5, 3.0]

st.set_page_config(layout="wide", page_title="Top Pivot")
st.title("Top Pivot")


def load_versions():
    v = get_kv("versions")
    if v is None:
        with sqlite3.connect(DB_PATH) as c:
            v = [r[0] for r in c.execute(
                "SELECT DISTINCT version FROM backtest_cache ORDER BY version DESC"
            ).fetchall()]
    return v


@st.cache_data(ttl=3600)
def load_single_stock_tickers():
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT symbol FROM tickers WHERE stock_underlier IS NOT NULL AND stock_underlier != ''"
        ).fetchall()
    return {r[0] for r in rows}


def _build_pivot(df_cells, df_meta, min_trades):
    df_filtered = df_cells[df_cells['trades'] >= min_trades]
    if df_filtered.empty:
        return pd.DataFrame()
    df_agg = df_filtered.groupby(['ticker', 'window', 'z'])['strategy_return'].max().reset_index()
    pivot = df_agg.pivot_table(index='ticker', columns=['window', 'z'], values='strategy_return')
    pivot.columns = [f"w{int(w)} z{z}" for w, z in pivot.columns]
    col_order = [f"w{w} z{z}" for w in WINDOWS for z in Z_THRESHOLDS]
    pivot = pivot[[c for c in col_order if c in pivot.columns]]
    pivot['max'] = pivot.max(axis=1)
    pivot = pivot.join(df_meta.set_index('ticker'), how='left')
    return pivot.sort_values('max', ascending=False)


@st.cache_data(ttl=3600)
def load_pivot_from_cache(version):
    cells = get_kv(f"pivot_cells_{version}")
    meta = get_kv(f"pivot_meta_{version}")
    if cells is None or meta is None:
        return None, None
    return pd.DataFrame(cells), pd.DataFrame(meta)


@st.cache_data(ttl=300)
def load_pivot_from_db(version, min_trades):
    with sqlite3.connect(DB_PATH) as c:
        df_cells = pd.read_sql_query(
            """SELECT ticker, window, COALESCE(z_score_threshold, 2.0) AS z,
                      trades, MAX(strategy_return) AS strategy_return
               FROM backtest_cache
               WHERE version = ? AND window IN (10, 20, 30)
               GROUP BY ticker, window, z_score_threshold, trades""",
            c, params=(version,)
        )
        df_meta = pd.read_sql_query(
            """WITH best AS (
                   SELECT ticker, strategy_return, alpha_vs_spy, asset_bh,
                          CASE WHEN asset_bh > 0 THEN strategy_return / asset_bh ELSE NULL END AS bh_mult,
                          ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY strategy_return DESC) AS rn
                   FROM backtest_cache
                   WHERE version = ? AND window IN (10, 20, 30)
               )
               SELECT ticker, alpha_vs_spy, asset_bh, bh_mult FROM best WHERE rn = 1""",
            c, params=(version,)
        )
    return df_cells, df_meta


def load_pivot(version, min_trades):
    df_cells, df_meta = load_pivot_from_cache(version)
    if df_cells is None:
        df_cells, df_meta = load_pivot_from_db(version, min_trades)
    return _build_pivot(df_cells, df_meta, min_trades)


versions = load_versions()
if not versions:
    st.info("No backtest results yet.")
    st.stop()

c1, c2, c3, c4, c5, c6 = st.columns(6)
with c1:
    version = st.selectbox("Version", versions)
with c2:
    min_trades = st.number_input("Min trades", min_value=1, value=5, step=1)
with c3:
    min_return = st.number_input("Min max return %", value=100.0, step=50.0, format="%.0f")
with c4:
    min_alpha = st.number_input("Min alpha %", value=0.0, step=10.0, format="%.0f")
with c5:
    min_bh_mult = st.number_input("Min B&H mult", value=1.0, step=0.5, format="%.1f")
with c6:
    exclude_single_stock = st.toggle("Exclude single-stock", value=True)

pivot = load_pivot(version, int(min_trades))
if pivot.empty:
    st.info("No data.")
    st.stop()

single_stock = load_single_stock_tickers()

if exclude_single_stock:
    pivot = pivot[~pivot.index.isin(single_stock)]

pivot = pivot[
    (pivot['max'] >= min_return) &
    (pivot['alpha_vs_spy'] >= min_alpha) &
    (pivot['bh_mult'] >= min_bh_mult)
]

if pivot.empty:
    st.info("No tickers match the current filters.")
    st.stop()

st.caption(f"{len(pivot)} tickers")

display = pivot.copy()
@st.cache_data(ttl=3600)
def load_underlier_map():
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("SELECT symbol, stock_underlier FROM tickers WHERE stock_underlier IS NOT NULL AND stock_underlier != ''").fetchall()
    return {r[0]: r[1] for r in rows}

underlier_map = load_underlier_map()
display.insert(0, 'Underlier', display.index.map(underlier_map).fillna(''))

pct_cols = [c for c in pivot.columns if c not in ('bh_mult',)]
edited = st.data_editor(
    display,
    use_container_width=True,
    hide_index=False,
    column_config={
        'Underlier': st.column_config.TextColumn("Underlier", help="Stock symbol if single-stock ETF, else blank"),
        **{col: st.column_config.NumberColumn(format="%.0f%%") for col in pct_cols},
        'bh_mult': st.column_config.NumberColumn("B&H Mult", format="%.1fx"),
    },
    disabled=[c for c in display.columns if c != 'Underlier'],
)

# Persist changes to tickers table
changed = display['Underlier'] != edited['Underlier']
if changed.any():
    with sqlite3.connect(DB_PATH) as conn:
        for ticker in display.index[changed]:
            val = edited.loc[ticker, 'Underlier'].strip() or None
            conn.execute("UPDATE tickers SET stock_underlier = ? WHERE symbol = ?", (val, ticker))
    load_single_stock_tickers.clear()
    load_underlier_map.clear()
    st.rerun()

sel = edited  # keep variable name for row selection below


selected_ticker = st.selectbox("Drill into ticker", ["—"] + list(pivot.index))
if selected_ticker != "—":
    b1, b2 = st.columns(2)
    with b1:
        if st.button("View in Winners"):
            st.session_state["winners_ticker_filter"] = [selected_ticker]
            st.switch_page("pages/3_Winners.py")
    with b2:
        if st.button("Open in Node Inspector"):
            st.session_state["target_node"] = {"ticker": selected_ticker, "version": version}
            st.switch_page("pages/2_Node_Inspector.py")
