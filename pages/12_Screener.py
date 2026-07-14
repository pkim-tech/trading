import json
import sqlite3
import streamlit as st
import pandas as pd
from pathlib import Path

DB_PATH     = "./cache/research/trading_universe.db"
CONFIG_PATH = Path("./config.json")

st.set_page_config(layout="wide", page_title="Screener")
st.title("Screener")


@st.cache_data(ttl=300)
def load_tickers():
    with sqlite3.connect(DB_PATH) as c:
        return pd.read_sql_query("SELECT * FROM tickers", c)


df = load_tickers()

# --- Filters ---
c1, c2, c3, c4, c5, c6 = st.columns(6)
with c1:
    search = st.text_input("Symbol / Name", "")
with c2:
    min_aum = st.number_input("Min AUM ($M)", min_value=0.0, value=0.0, step=10.0)
with c3:
    min_vol = st.number_input("Min Avg Vol (10d)", min_value=0, value=0, step=100000)
with c4:
    show_inverse = st.selectbox("Inverse", ["All", "Long only", "Inverse only"])
with c5:
    show_single = st.selectbox("Single stock underlier", ["Exclude", "All", "Only"])

has_data_only    = st.toggle("Has price data", value=True)
leveraged_only   = st.toggle("Leveraged ETPs only", value=True)
with c6:
    index_search = st.text_input("Underlying Index contains", "")

lev_opts = ["All", "2x only", "3x only", "2x or 3x"]
leverage_filter = st.selectbox("Leverage", lev_opts)

c7, c8, c9, c10, c11 = st.columns(5)
with c7:
    min_1m = st.number_input("Min 1m %", value=-999.0, step=1.0, format="%.1f")
with c8:
    min_3m = st.number_input("Min 3m %", value=-999.0, step=1.0, format="%.1f")
with c9:
    min_12m = st.number_input("Min 12m %", value=-999.0, step=1.0, format="%.1f")
with c10:
    investment = st.number_input("Investment ($)", min_value=0, value=50000, step=5000)
with c11:
    vol_multiple = st.number_input("Min vol multiple", min_value=1, value=20, step=1)

# --- Apply filters ---
mask = pd.Series(True, index=df.index)

if search:
    s = search.upper()
    mask &= df['symbol'].str.upper().str.contains(s) | df['description'].str.upper().str.contains(s)

if min_aum > 0:
    mask &= df['total_assets'].fillna(0) >= min_aum * 1_000_000

if min_vol > 0:
    mask &= df['avg_vol_10d'].fillna(0) >= min_vol

if show_inverse == "Long only":
    mask &= df['inverse'] == 0
elif show_inverse == "Inverse only":
    mask &= df['inverse'] == 1

if has_data_only:
    mask &= df['has_data'] == 1

if leveraged_only:
    mask &= df['leveraged_etp'] == 'Yes'

if leverage_filter == "2x only":
    mask &= df['leverage'] == 2.0
elif leverage_filter == "3x only":
    mask &= df['leverage'] == 3.0
elif leverage_filter == "2x or 3x":
    mask &= df['leverage'].isin([2.0, 3.0])

if show_single == "Exclude":
    mask &= df['stock_underlier'].isna()
elif show_single == "Only":
    mask &= df['stock_underlier'].notna()

if index_search:
    mask &= df['underlying_index'].str.contains(index_search, case=False, na=False)

if min_1m > -999:
    mask &= df['pct_chg_1m'].fillna(-999) >= min_1m
if min_3m > -999:
    mask &= df['pct_chg_3m'].fillna(-999) >= min_3m
if min_12m > -999:
    mask &= df['pct_chg_12m'].fillna(-999) >= min_12m

if investment > 0:
    min_dollar_vol = investment * vol_multiple
    dollar_vol = df['avg_vol_10d'].fillna(0) * df['last_price'].fillna(0)
    mask &= dollar_vol >= min_dollar_vol

filtered = df[mask].copy()
filtered['dollar_vol'] = filtered['avg_vol_10d'].fillna(0) * filtered['last_price'].fillna(0)

# --- Display ---
st.caption(f"{len(filtered)} tickers")

display = filtered[[
    'symbol', 'description', 'stock_underlier', 'index_underlier', 'leverage', 'inverse',
    'has_data', 'last_price', 'dollar_vol', 'total_assets',
    'pct_chg_1m', 'pct_chg_3m', 'pct_chg_12m', 'pct_chg_3y', 'pct_chg_5y',
    'macd', 'rsi', 'sma_cross',
]].copy()

display['total_assets']   = display['total_assets'].map(lambda x: f"${x/1e6:.1f}M" if pd.notna(x) else "")
display['last_price']     = display['last_price'].map(lambda x: f"${x:.2f}" if pd.notna(x) else "")
display['dollar_vol']     = display['dollar_vol'].map(lambda x: f"${x/1e6:.1f}M" if x > 0 else "")
display['has_data']       = display['has_data'].map({0: "", 1: "Yes"})
display['inverse']        = display['inverse'].map({0: "", 1: "Yes"})

for col in ('pct_chg_1m', 'pct_chg_3m', 'pct_chg_12m', 'pct_chg_3y', 'pct_chg_5y'):
    display[col] = display[col].map(lambda x: f"{x:+.1f}%" if pd.notna(x) else "--")

display = display.rename(columns={
    'symbol': 'Symbol', 'description': 'Description',
    'stock_underlier': 'Stock Underlier', 'index_underlier': 'Index Underlier',
    'leverage': 'Lev', 'has_data': 'Data', 'inverse': 'Inv', 'last_price': 'Price', 'dollar_vol': 'Dollar Vol',
    'total_assets': 'AUM',
    'pct_chg_1m': '1m%', 'pct_chg_3m': '3m%', 'pct_chg_12m': '12m%',
    'pct_chg_3y': '3y%', 'pct_chg_5y': '5y%',
    'macd': 'MACD', 'rsi': 'RSI', 'sma_cross': 'SMA X',
})

selection = st.dataframe(
    display,
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="multi-row",
)

# --- Actions ---
selected_rows = selection.selection.rows
if selected_rows:
    selected_tickers = filtered.iloc[selected_rows]['symbol'].tolist()
    st.caption(f"Selected: {', '.join(selected_tickers)}")

    if st.button(f"Add {len(selected_tickers)} ticker(s) to config.json"):
        config = json.loads(CONFIG_PATH.read_text())
        existing = set(config.get("target_tickers", []))
        added = [t for t in selected_tickers if t not in existing]
        config["target_tickers"] = sorted(existing | set(selected_tickers))
        CONFIG_PATH.write_text(json.dumps(config, indent=4))
        if added:
            st.success(f"Added: {', '.join(added)}")
        else:
            st.info("All selected tickers already in config.json")
