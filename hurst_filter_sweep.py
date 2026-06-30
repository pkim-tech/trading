import numpy as np
import pandas as pd
from pathlib import Path
from backtester import run_backtest
import strategies
from hurst import _hurst_vectorized

WATCHLIST = [
    ("AGQ",  10, 19,  8, 133, 2.0),
    ("DPST", 10, 21, 12, 126, 2.0),
    ("EDC",  10, 17, 17, 112, 2.0),
    ("FAS",  10, 25, 10, 133, 2.0),
    ("LABU", 20, 21, 18,  84, 2.0),
]
STRATEGY = "ZScoreBreakout"

all_results = []

for TICKER, WINDOW, TP, SL, HOLD, Z in WATCHLIST:
    df_h = pd.read_csv(f"cache/{TICKER}_1h.csv", index_col=0, parse_dates=True)
    close_col = "Adj Close" if "Adj Close" in df_h.columns else "Close"
    df_daily = df_h.resample("D").last().dropna(subset=[close_col])

    strat = getattr(strategies, STRATEGY)(window=WINDOW, z_score_threshold=Z)
    df_ind = strat.generate_daily_indicators(df_daily)
    trades = run_backtest(df_h, df_ind, TICKER,
                          take_profit=TP/100, stop_loss=SL/100,
                          max_hours_to_hold=HOLD, z_score_threshold=Z)

    closed = pd.DataFrame([t for t in trades if t["Result"] in ("WIN","LOSS","TWIN","TLOSS")])
    if closed.empty:
        print(f"{TICKER}: no closed trades")
        continue
    closed["Entry Time"] = pd.to_datetime(closed["Entry Time"])
    closed["Return"] = closed["Return"].astype(float)

    log_p = np.log(df_h[close_col].dropna().values)
    price_idx = df_h[close_col].dropna().index

    unfiltered_ret = (closed["Return"] + 1).prod() - 1

    best = {"return_pct": unfiltered_ret * 100, "hurst_window": "none", "cutoff": "none", "trades": len(closed)}

    for hw in [100, 150, 200]:
        h_vals = _hurst_vectorized(log_p, hw)
        h_series = pd.Series(h_vals, index=price_idx)
        closed["hurst"] = closed["Entry Time"].apply(
            lambda t: h_series.asof(t) if not h_series.empty and t >= h_series.index[0] else np.nan
        )
        for cutoff in np.arange(0.30, 0.75, 0.05):
            filtered = closed[closed["hurst"].isna() | (closed["hurst"] >= cutoff)]
            if len(filtered) == 0:
                continue
            ret = (filtered["Return"] + 1).prod() - 1
            if ret > best["return_pct"] / 100:
                best = {"return_pct": round(ret * 100, 1), "hurst_window": hw,
                        "cutoff": round(cutoff, 2), "trades": len(filtered)}

    all_results.append({
        "ticker":        TICKER,
        "unfiltered_%":  round(unfiltered_ret * 100, 1),
        "filtered_%":    best["return_pct"],
        "hurst_window":  best["hurst_window"],
        "cutoff":        best["cutoff"],
        "trades":        best["trades"],
        "total_trades":  len(closed),
    })

print(pd.DataFrame(all_results).to_string(index=False))
