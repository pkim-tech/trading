"""For each same-day exit->re-entry pair in trade_cache, simulate delaying the
re-entry to the next trading day's open (T+1 cash settlement) instead of
skipping it outright, and compare compounded/dollar return vs the unconstrained
baseline."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import pandas as pd

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "research"
NOTIONAL = 50_000


def load_hourly(ticker):
    df = pd.read_csv(CACHE_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    if close_col != "Close":
        df["Close"] = df[close_col]
    return df


def simulate(ticker):
    conn = sqlite3.connect(CACHE_DIR / "watchlist_sweep.db")
    df = pd.read_sql(
        f"SELECT entry_time, entry_price, exit_time, exit_price, return_pct "
        f"FROM trade_cache WHERE ticker='{ticker}' AND result IN ('WIN','LOSS','TWIN','TLOSS') "
        f"ORDER BY entry_time", conn)
    conn.close()
    if len(df) < 2:
        return None
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"] = pd.to_datetime(df["exit_time"])
    prev_exit = df["exit_time"].shift(1)
    same_day = df["entry_time"].dt.date == prev_exit.dt.date
    same_day.iloc[0] = False

    df_h = load_hourly(ticker)

    used_returns = []
    for i, r in df.iterrows():
        if not same_day.iloc[i]:
            used_returns.append(r["return_pct"])
            continue
        next_day = r["entry_time"].normalize() + pd.Timedelta(days=1)
        candidates = df_h.index[df_h.index >= next_day]
        if len(candidates) == 0:
            continue
        delayed_entry_time = candidates[0]
        if delayed_entry_time >= r["exit_time"]:
            continue  # trade window too short to survive a 1-day delay
        delayed_entry_price = df_h.loc[delayed_entry_time, "Close"]
        used_returns.append((r["exit_price"] - delayed_entry_price) / delayed_entry_price)

    baseline_compound = 1.0
    for r in df["return_pct"]:
        baseline_compound *= (1 + r)
    delayed_compound = 1.0
    for r in used_returns:
        delayed_compound *= (1 + r)

    return {
        "ticker": ticker,
        "n_trades": len(df),
        "n_same_day": int(same_day.sum()),
        "n_delayed_kept": len(used_returns) - (len(df) - int(same_day.sum())),
        "baseline_compound_pct": (baseline_compound - 1) * 100,
        "delayed_compound_pct": (delayed_compound - 1) * 100,
        "baseline_dollar": (df["return_pct"] * NOTIONAL).sum(),
        "delayed_dollar": sum(used_returns) * NOTIONAL,
    }


if __name__ == "__main__":
    tickers = sys.argv[1:] or ["AGQ", "EDC", "HIBL", "KORU", "LABU", "SOXL"]
    rows = [simulate(t) for t in tickers]
    rows = [r for r in rows if r]
    out = pd.DataFrame(rows)
    pd.set_option("display.width", 160)
    print(out.to_string(index=False))
