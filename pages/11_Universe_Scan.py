import streamlit as st
import sqlite3
import pandas as pd

DB_PATH = "./cache/research/trading_universe.db"
COARSE_VALS = list(range(3, 31, 3))  # [3,6,9,...,30]
MIN_NOTIONAL = 50_000                 # default liquidity floor ($)
TOP_N_INDEX  = 20
TOP_N_STOCK  = 5

st.set_page_config(layout="wide", page_title="Universe Scan")
st.title("Universe Scan")


@st.cache_data(ttl=300)
def load_versions():
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute("SELECT DISTINCT version FROM backtest_cache ORDER BY version DESC").fetchall()
    return [r[0] for r in rows]


@st.cache_data(ttl=300)
def load_coarse(version):
    placeholders = ",".join("?" * len(COARSE_VALS))
    with sqlite3.connect(DB_PATH) as c:
        df = pd.read_sql(f"""
            SELECT ticker, window, z_score_threshold, take_profit, stop_loss,
                   max_hold_hours, trades, alpha_vs_spy, strategy_return
            FROM backtest_cache
            WHERE version=? AND strategy='ZScoreBreakout' AND trades > 0
              AND take_profit IN ({placeholders})
              AND stop_loss   IN ({placeholders})
        """, c, params=(version, *COARSE_VALS, *COARSE_VALS))
    return df


@st.cache_data(ttl=3600)
def load_tickers():
    with sqlite3.connect(DB_PATH) as c:
        return pd.read_sql("""
            SELECT symbol, avg_vol_10d, last_price, total_assets,
                   stock_underlier, index_underlier, leverage, inverse
            FROM tickers
        """, c)


# ── Controls ─────────────────────────────────────────────────────────────────

versions = load_versions()
if not versions:
    st.info("No backtest data in DB.")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
version      = c1.selectbox("Version", versions)
liq_floor    = c2.number_input("Min max-notional ($k)", value=MIN_NOTIONAL // 1000, step=10) * 1000
top_n_index  = c3.number_input("Top N index underliers", value=TOP_N_INDEX, step=5)
top_n_stock  = c4.number_input("Top N stock underliers", value=TOP_N_STOCK, step=1)

# ── Data ─────────────────────────────────────────────────────────────────────

df_coarse = load_coarse(version)
df_tickers = load_tickers()

if df_coarse.empty:
    st.info(f"No coarse nodes found for version {version}.")
    st.stop()

# Best coarse node per ticker
best_per_ticker = (
    df_coarse.sort_values("alpha_vs_spy", ascending=False)
    .groupby("ticker")
    .first()
    .reset_index()
    .rename(columns={
        "alpha_vs_spy":    "best_alpha",
        "strategy_return": "best_return",
        "take_profit":     "best_tp",
        "stop_loss":       "best_sl",
        "max_hold_hours":  "best_hold",
        "window":          "best_window",
        "z_score_threshold": "best_z",
    })
)[["ticker", "best_alpha", "best_return", "best_tp", "best_sl", "best_hold", "best_window", "best_z", "trades"]]

# Top 3 coarse nodes per ticker
top3 = (
    df_coarse.sort_values("alpha_vs_spy", ascending=False)
    .groupby("ticker")
    .head(3)
    .groupby("ticker")
    .apply(lambda g: g[["take_profit","stop_loss","max_hold_hours","alpha_vs_spy"]].values.tolist(), include_groups=False)
    .reset_index()
    .rename(columns={0: "top3_nodes"})
)
best_per_ticker = best_per_ticker.merge(top3, on="ticker", how="left")

# Join liquidity
df_tickers["max_notional"] = df_tickers["avg_vol_10d"] * df_tickers["last_price"] * 0.01
liq = df_tickers[["symbol","max_notional","total_assets","stock_underlier","index_underlier","leverage","inverse"]].rename(columns={"symbol":"ticker"})
df = best_per_ticker.merge(liq, on="ticker", how="left")

# Underlier type label
def underlier_type(row):
    if pd.notna(row.get("index_underlier")) and row["index_underlier"]:
        return "index"
    if pd.notna(row.get("stock_underlier")) and row["stock_underlier"]:
        return "stock"
    return "unknown"

df["underlier"] = df.apply(underlier_type, axis=1)

# ── Flags ─────────────────────────────────────────────────────────────────────

df["flag_low_liq"] = df["max_notional"].isna() | (df["max_notional"] < liq_floor)

idx_rank  = df[df["underlier"] == "index"].sort_values("best_alpha", ascending=False).head(int(top_n_index))["ticker"]
stk_rank  = df[df["underlier"] == "stock"].sort_values("best_alpha", ascending=False).head(int(top_n_stock))["ticker"]
df["flag_top_index"] = df["ticker"].isin(idx_rank)
df["flag_top_stock"] = df["ticker"].isin(stk_rank)
df["flag_refine"]    = ~df["flag_low_liq"] & (df["flag_top_index"] | df["flag_top_stock"])

def flags(row):
    parts = []
    if row["flag_low_liq"]:   parts.append("LOW_LIQ")
    if row["flag_top_index"]: parts.append("TOP_IDX")
    if row["flag_top_stock"]: parts.append("TOP_STK")
    if row["flag_refine"]:    parts.append("REFINE")
    return " ".join(parts)

df["flags"] = df.apply(flags, axis=1)

# Neighborhood safety: count coarse nodes within ±3 TP and ±3 SL of best node with alpha > 0
def neighborhood_score(row):
    sub = df_coarse[df_coarse["ticker"] == row["ticker"]]
    if sub.empty:
        return None
    neighbors = sub[
        (sub["take_profit"].between(row["best_tp"] - 3, row["best_tp"] + 3)) &
        (sub["stop_loss"].between(row["best_sl"] - 3, row["best_sl"] + 3))
    ]
    if neighbors.empty:
        return 0
    return int((neighbors["alpha_vs_spy"] > 0).sum())

df["safety"] = df.apply(neighborhood_score, axis=1)

# ── Summary ───────────────────────────────────────────────────────────────────

m1, m2, m3, m4 = st.columns(4)
m1.metric("Tickers scanned",   len(df))
m2.metric("Low liquidity",     int(df["flag_low_liq"].sum()))
m3.metric("Top index (refine)", int((df["flag_top_index"] & ~df["flag_low_liq"]).sum()))
m4.metric("Top stock (refine)", int((df["flag_top_stock"] & ~df["flag_low_liq"]).sum()))

# ── Refined tickers section ───────────────────────────────────────────────────

st.subheader("Selected for fine mesh")
df_refine = df[df["flag_refine"]].sort_values("best_alpha", ascending=False)
if df_refine.empty:
    st.caption("No tickers pass filters.")
else:
    disp_r = df_refine[["ticker","underlier","max_notional","best_alpha","best_tp","best_sl","best_hold","trades","safety","flags"]].copy()
    disp_r.columns = ["Ticker","Underlier","Max Notional","Best Alpha %","TP","SL","Hold h","Trades","Safety","Flags"]
    st.dataframe(
        disp_r,
        use_container_width=True,
        hide_index=True,
        height=35 * (len(disp_r) + 1) + 10,
        column_config={
            "Max Notional": st.column_config.NumberColumn(format="$%.0f"),
            "Best Alpha %": st.column_config.NumberColumn(format="%+.1f%%"),
        }
    )

# ── Full universe table ────────────────────────────────────────────────────────

st.subheader("Full universe")
show_all = st.toggle("Show all tickers", value=False)
df_show = df if show_all else df[df["best_alpha"] > 0]
df_show = df_show.sort_values("best_alpha", ascending=False)

disp = df_show[["ticker","underlier","max_notional","best_alpha","best_tp","best_sl","best_hold","best_window","best_z","trades","safety","flags"]].copy()
disp.columns = ["Ticker","Underlier","Max Notional","Best Alpha %","TP","SL","Hold h","Window","Z","Trades","Safety","Flags"]

st.dataframe(
    disp,
    use_container_width=True,
    hide_index=True,
    height=min(600, 35 * (len(disp) + 1) + 10),
    column_config={
        "Max Notional": st.column_config.NumberColumn(format="$%.0f"),
        "Best Alpha %": st.column_config.NumberColumn(format="%+.1f%%"),
    }
)
