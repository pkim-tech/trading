import json
import sqlite3
import streamlit as st
import pandas as pd
from pathlib import Path

DB_PATH    = "./cache/trading_universe.db"
CONFIG_PATH = Path("./config.json")

st.set_page_config(layout="wide", page_title="Sweep Status")
st.title("Sweep Status")

cfg = json.loads(CONFIG_PATH.read_text())
hp  = cfg["hyperparameters"]
config_expected_per_ticker = (
    len(hp.get("z_score_thresholds", [2.0])) *
    len(hp["windows"]) *
    len(hp["take_profits"]) *
    len(hp["stop_losses"]) *
    len(hp["hold_time_caps"])
)

@st.cache_data(ttl=30)
def load_data(version):
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query("""
            SELECT ticker,
                   COUNT(*) AS cached,
                   SUM(CASE WHEN trades > 0 THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN trades = 0 THEN 1 ELSE 0 END) AS no_trades,
                   MAX(run_timestamp) AS last_run
            FROM backtest_cache
            WHERE version = ?
            GROUP BY ticker
            ORDER BY ticker
        """, conn, params=(version,))
    return df


with sqlite3.connect(DB_PATH) as conn:
    versions = [r[0] for r in conn.execute(
        "SELECT DISTINCT version FROM backtest_cache ORDER BY version DESC"
    ).fetchall()]

if not versions:
    st.warning("No sweep data in DB yet.")
    st.stop()

version = st.selectbox("Version", versions)
target  = cfg.get("target_tickers", [])

df = load_data(version)

# Merge with full target ticker list so not-started tickers show up
all_tickers = pd.DataFrame({"ticker": target})
df = all_tickers.merge(df, on="ticker", how="left").fillna(0)
df[["cached", "success", "no_trades"]] = df[["cached", "success", "no_trades"]].astype(int)

def get_data_date(ticker):
    p = Path(f"./cache/{ticker}_1h.csv")
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p, index_col=0, parse_dates=True)
        return df.index.max().strftime("%Y-%m-%d")
    except Exception:
        return None

df["data_thru"] = df["ticker"].apply(get_data_date)

# Use max cached nodes across tickers for this version as the expected count,
# falling back to config-derived value if the version is still in progress
version_max = int(df["cached"].max()) if not df.empty and df["cached"].max() > 0 else 0
expected_per_ticker = max(version_max, config_expected_per_ticker) if version == cfg.get("version") else version_max or config_expected_per_ticker
df["expected"] = expected_per_ticker
df["pct"]      = (df["cached"] / df["expected"] * 100).round(1)
df["status"]   = df["cached"].apply(
    lambda n: "Done" if n >= expected_per_ticker else ("Partial" if n > 0 else "Not started")
)

# --- Summary metrics ---
total_expected = len(target) * expected_per_ticker
total_cached   = df["cached"].sum()
total_success  = df["success"].sum()
total_no_trades = df["no_trades"].sum()
done_count     = (df["status"] == "Done").sum()
partial_count  = (df["status"] == "Partial").sum()
not_started    = (df["status"] == "Not started").sum()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Tickers", f"{done_count} done / {len(target)} total")
c2.metric("Nodes cached", f"{total_cached:,} / {total_expected:,}")
c3.metric("Overall progress", f"{total_cached / total_expected * 100:.1f}%")
c4.metric("Partial / Not started", f"{partial_count} / {not_started}")

c5, c6 = st.columns(2)
c5.metric("SUCCESS nodes", f"{total_success:,}")
c6.metric("NO_TRADES nodes", f"{total_no_trades:,}")

st.divider()

# --- Filter ---
status_filter = st.multiselect("Filter by status", ["Done", "Partial", "Not started"],
                                default=["Partial", "Not started"])
display = df[df["status"].isin(status_filter)] if status_filter else df

# --- Progress bar column ---
def fmt_bar(pct):
    filled = int(pct / 5)
    return f"{'█' * filled}{'░' * (20 - filled)} {pct:.0f}%"

display = display.copy()
display["progress"] = display["pct"].apply(fmt_bar)

st.dataframe(
    display[["ticker", "status", "data_thru", "cached", "expected", "success", "no_trades", "progress"]].rename(columns={
        "ticker": "Ticker", "status": "Status", "data_thru": "Data Thru",
        "cached": "Cached", "expected": "Expected", "success": "Success",
        "no_trades": "No Trades", "progress": "Progress"
    }),
    use_container_width=True,
    hide_index=True,
)
