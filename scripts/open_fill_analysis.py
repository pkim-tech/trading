"""
Open-fill analysis: for each watchlist ticker, compare backtest return
(entry at close of trigger bar) vs open-fill return (entry at open of
trigger bar, simulating a limit order placed the night before).

Exit price is held constant — we're measuring the entry slippage only.
"""
import sys
import pandas as pd
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtester import run_backtest
import strategies
from active_signals import get_watchlist

CACHE_DIR = Path("./cache/research")


def load_hourly(ticker):
    path = CACHE_DIR / f"{ticker}_1h.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    if close_col != "Close":
        df["Close"] = df[close_col]
    return df


def analyze(node, df_h):
    ticker = node["ticker"]
    z = node["z_score_threshold"]
    close_col = "Adj Close" if "Adj Close" in df_h.columns else "Close"

    df_daily = df_h.resample("D").last().dropna(subset=[close_col])
    strat = getattr(strategies, node["strategy"])(
        window=node["window"], z_score_threshold=z
    )
    df_ind = strat.generate_daily_indicators(df_daily)

    trades = run_backtest(
        df_h, df_ind, ticker,
        take_profit=node["take_profit"] / 100,
        stop_loss=node["stop_loss"] / 100,
        max_hours_to_hold=node["max_hold_hours"],
        z_score_threshold=z,
    )
    closed = [t for t in trades if t["Result"] in ("WIN", "LOSS", "TWIN", "TLOSS")]
    if not closed:
        return None

    open_prices = df_h["Open"]

    rows = []
    nine_thirty_count = 0
    for t in closed:
        entry_time = pd.Timestamp(t["Entry Time"])
        is_9_30 = entry_time.hour == 9

        open_price = open_prices.get(entry_time)
        if open_price is None or pd.isna(open_price):
            continue

        exit_price = t["Exit Price"]
        base_ret = t["Return"]
        open_ret = (exit_price - open_price) / open_price

        if is_9_30:
            nine_thirty_count += 1

        rows.append({
            "entry_time": entry_time,
            "is_9_30": is_9_30,
            "entry_close": t["Entry Price"],
            "entry_open": open_price,
            "exit_price": exit_price,
            "base_ret": base_ret,
            "open_ret": open_ret,
        })

    if not rows:
        return None

    df = pd.DataFrame(rows)
    n = len(df)
    base_compound = (df["base_ret"] + 1).prod() - 1
    open_compound = (df["open_ret"] + 1).prod() - 1
    drag = open_compound - base_compound

    # 9:30 bar subset
    df_9 = df[df["is_9_30"]]
    n_9 = len(df_9)
    base_9 = (df_9["base_ret"] + 1).prod() - 1 if n_9 else np.nan
    open_9 = (df_9["open_ret"] + 1).prod() - 1 if n_9 else np.nan

    avg_open_vs_close = ((df["entry_open"] - df["entry_close"]) / df["entry_close"] * 100).mean()

    return {
        "Ticker": ticker,
        "Trades": n,
        "9:30 Trades": n_9,
        "Base%": round(base_compound * 100, 1),
        "OpenFill%": round(open_compound * 100, 1),
        "Drag%": round(drag * 100, 1),
        "Base%(9:30)": round(base_9 * 100, 1) if not np.isnan(base_9) else "—",
        "OpenFill%(9:30)": round(open_9 * 100, 1) if not np.isnan(open_9) else "—",
        "AvgOpen>Close%": round(avg_open_vs_close, 2),
    }


def main():
    wl = get_watchlist()
    print(f"Running open-fill analysis for {len(wl)} tickers...\n")

    results = []
    for node in wl:
        ticker = node["ticker"]
        df_h = load_hourly(ticker)
        if df_h is None:
            print(f"  {ticker}: no cache file")
            continue
        r = analyze(node, df_h)
        if r is None:
            print(f"  {ticker}: no closed trades")
            continue
        results.append(r)
        print(f"  {ticker}: base={r['Base%']:+.1f}%  open={r['OpenFill%']:+.1f}%  drag={r['Drag%']:+.1f}%  (9:30: {r['9:30 Trades']}/{r['Trades']} trades)")

    if not results:
        print("No results.")
        return

    df_out = pd.DataFrame(results).set_index("Ticker")
    print("\n--- Summary ---")
    print(df_out.to_string())


if __name__ == "__main__":
    main()
