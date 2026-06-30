import streamlit as st
import pandas as pd
import numpy as np
from pathlib import Path
from backtester import run_backtest
import strategies
from active_signals import get_watchlist

st.set_page_config(layout="wide", page_title="Entry Delay")
st.title("Entry Delay Analysis")

CACHE_DIR = Path("./cache")
MAX_DELAY  = 4


@st.cache_data(ttl=3600)
def load_data(ticker):
    path = CACHE_DIR / f"{ticker}_1h.csv"
    if not path.exists():
        return None, None
    df_h = pd.read_csv(path, index_col=0, parse_dates=True)
    close_col = "Adj Close" if "Adj Close" in df_h.columns else "Close"
    df_daily = df_h.resample("D").last().dropna(subset=[close_col])
    return df_h, df_daily


@st.cache_data(ttl=3600)
def run_delay_analysis(ticker, strategy_name, window, tp, sl, hold, z):
    df_h, df_daily = load_data(ticker)
    if df_h is None:
        return None, None

    close_col = "Adj Close" if "Adj Close" in df_h.columns else "Close"
    strat = getattr(strategies, strategy_name)(window=window, z_score_threshold=z)
    df_ind = strat.generate_daily_indicators(df_daily)
    trades = run_backtest(df_h, df_ind, ticker,
                          take_profit=tp / 100, stop_loss=sl / 100,
                          max_hours_to_hold=hold, z_score_threshold=z)

    closed = [t for t in trades if t["Result"] in ("WIN", "LOSS", "TWIN", "TLOSS")]
    if not closed:
        return None, None

    prices = df_h[close_col].dropna()

    # Build per-trade delay table
    rows = []
    for t in closed:
        entry_time  = pd.Timestamp(t["Entry Time"])
        exit_price  = t["Exit Price"]
        base_return = t["Return"]

        # Lower band at entry date (prior day's indicators)
        entry_date  = entry_time.normalize()
        prev_ind    = df_ind[df_ind.index < entry_date]
        if prev_ind.empty:
            continue
        last_ind   = prev_ind.iloc[-1]
        lower_band = last_ind["SMA"] - z * last_ind["Std"]

        row = {"Entry Time": entry_time, "Entry Price": t["Entry Price"],
               "Exit Price": exit_price, "Result": t["Result"],
               "Base Return": base_return}

        for delay in range(1, MAX_DELAY + 1):
            delayed_time = entry_time + pd.Timedelta(hours=delay)
            # find nearest bar at or after delayed_time
            future = prices[prices.index >= delayed_time]
            if future.empty:
                row[f"R+{delay}h"] = np.nan
                row[f"missed+{delay}h"] = True
                continue
            delayed_price = future.iloc[0]
            if delayed_price > lower_band:
                row[f"R+{delay}h"] = np.nan
                row[f"missed+{delay}h"] = True
            else:
                row[f"R+{delay}h"] = (exit_price - delayed_price) / delayed_price
                row[f"missed+{delay}h"] = False

        rows.append(row)

    df_trades = pd.DataFrame(rows)

    # Summary: compounded return and miss count per delay
    summary = {"Ticker": ticker, "Base Return%": ((df_trades["Base Return"] + 1).prod() - 1) * 100,
               "Trades": len(df_trades)}
    for delay in range(1, MAX_DELAY + 1):
        col = f"R+{delay}h"
        miss_col = f"missed+{delay}h"
        entered = df_trades[~df_trades[miss_col]]
        missed  = df_trades[miss_col].sum()
        summary[f"+{delay}h Return%"] = ((entered[col] + 1).prod() - 1) * 100 if not entered.empty else np.nan
        summary[f"+{delay}h Missed"]  = int(missed)

    return summary, df_trades


wl = get_watchlist()
if not wl:
    st.info("Watch list is empty.")
    st.stop()

summaries = []
trade_details = {}
progress = st.progress(0, text="Running analysis...")
for i, node in enumerate(wl):
    ticker = node["ticker"]
    summary, df_trades = run_delay_analysis(
        ticker, node["strategy"], node["window"],
        node["take_profit"], node["stop_loss"],
        node["max_hold_hours"], node["z_score_threshold"]
    )
    if summary:
        summaries.append(summary)
        trade_details[ticker] = df_trades
    progress.progress((i + 1) / len(wl), text=f"{ticker}...")
progress.empty()

if not summaries:
    st.info("No results.")
    st.stop()

df_summary = pd.DataFrame(summaries).set_index("Ticker")

ret_cols  = ["Base Return%"] + [f"+{d}h Return%" for d in range(1, MAX_DELAY + 1)]
miss_cols = [f"+{d}h Missed"  for d in range(1, MAX_DELAY + 1)]

st.subheader("Return by Entry Delay")
st.dataframe(
    df_summary[["Trades"] + ret_cols + miss_cols],
    use_container_width=True,
    column_config={c: st.column_config.NumberColumn(format="%.1f%%")
                   for c in ret_cols},
)

st.divider()
st.subheader("Trade Detail")
selected = st.selectbox("Ticker", list(trade_details.keys()))
if selected:
    df = trade_details[selected].copy()
    display_cols = ["Entry Time", "Entry Price", "Exit Price", "Result", "Base Return"]
    for delay in range(1, MAX_DELAY + 1):
        df[f"+{delay}h"] = df.apply(
            lambda r, d=delay: "MISSED" if r[f"missed+{d}h"]
            else f"{r[f'R+{d}h']*100:.1f}%", axis=1
        )
        display_cols.append(f"+{delay}h")
    df["Base Return"] = df["Base Return"] * 100
    st.dataframe(
        df[display_cols].rename(columns={"Base Return": "Base%"}),
        use_container_width=True,
        column_config={
            "Base%": st.column_config.NumberColumn(format="%.1f%%"),
            "Entry Price": st.column_config.NumberColumn(format="$%.2f"),
            "Exit Price":  st.column_config.NumberColumn(format="$%.2f"),
        },
        hide_index=True,
    )
