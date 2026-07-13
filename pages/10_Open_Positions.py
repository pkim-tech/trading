import sqlite3
from datetime import datetime

import pandas as pd
import streamlit as st
import yfinance as yf

DB_PATH = "./cache/trading_live.db"

st.set_page_config(page_title="Open Positions", layout="wide")
st.title("Open Positions")


@st.cache_data(ttl=30)
def load_positions():
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT id, ticker, window, COALESCE(take_profit, arm_sell_pct) as take_profit, stop_loss, max_hold_hours, "
            "signal_price, entry_price, entry_time FROM open_positions ORDER BY entry_time"
        ).fetchall()
    return rows


def current_price(ticker):
    try:
        return yf.Ticker(ticker).fast_info.last_price
    except Exception:
        return None


positions = load_positions()

if not positions:
    st.info("No open positions.")
    st.stop()

rows = []
for pos_id, ticker, window, tp, sl, max_hold, signal_price, entry_price, entry_time_str in positions:
    entry_time = datetime.fromisoformat(entry_time_str)
    hours_held = (datetime.now() - entry_time).total_seconds() / 3600
    hours_left = max_hold - hours_held
    cp = current_price(ticker)
    pnl_pct = (cp - entry_price) / entry_price * 100 if cp else None
    tp_price = entry_price * (1 + tp / 100)
    sl_price = entry_price * (1 - sl / 100)
    drift_pct = (entry_price - signal_price) / signal_price * 100 if signal_price else None
    rows.append({
        "ID":         pos_id,
        "Ticker":     ticker,
        "Signal $":   signal_price,
        "Entry $":    entry_price,
        "Drift %":    drift_pct,
        "Now $":      cp,
        "P&L %":      pnl_pct,
        "TP $":       tp_price,
        "TP %":       tp,
        "SL $":       sl_price,
        "SL %":       sl,
        "Held h":     hours_held,
        "Left h":     hours_left,
        "Entry Time": entry_time.strftime("%m/%d %H:%M"),
    })

df = pd.DataFrame(rows)

st.dataframe(
    df.drop(columns=["ID"]),
    hide_index=True,
    height=35 * (len(df) + 1) + 10,
    column_config={
        "Signal $": st.column_config.NumberColumn(format="$%.2f"),
        "Entry $":  st.column_config.NumberColumn(format="$%.2f"),
        "Drift %":  st.column_config.NumberColumn(format="%+.2f%%"),
        "Now $":    st.column_config.NumberColumn(format="$%.2f"),
        "P&L %":    st.column_config.NumberColumn(format="%+.2f%%"),
        "TP $":     st.column_config.NumberColumn(format="$%.2f"),
        "SL $":     st.column_config.NumberColumn(format="$%.2f"),
        "Held h":   st.column_config.NumberColumn(format="%.1f"),
        "Left h":   st.column_config.NumberColumn(format="%.1f"),
    },
)

st.caption(f"Last updated: {datetime.now().strftime('%H:%M:%S')}  ·  Prices via yfinance fast_info")
if st.button("Refresh"):
    st.rerun()
