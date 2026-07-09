"""Export every hourly bar for a ticker with the prior-day SMA/Std/z-score-trigger
used by compute_buy_signal, for manual inspection in a spreadsheet."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import pandas as pd
import strategies

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


def load_hourly(ticker):
    df = pd.read_csv(CACHE_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    if close_col != "Close":
        df["Close"] = df[close_col]
    return df


def get_node(ticker, watchlist_id=9):
    conn = sqlite3.connect(Path(__file__).resolve().parent.parent / "cache" / "trading_live.db")
    c = conn.cursor()
    c.execute(
        "SELECT window, arm_sell_pct, trail_buy_pct, trail_sell_pct, fixed_sl, "
        "max_hold_hours, z_score_threshold FROM watch_list WHERE ticker=? AND watchlist_id=?",
        (ticker, watchlist_id),
    )
    row = c.fetchone()
    conn.close()
    return dict(zip(
        ["window", "arm_sell_pct", "trail_buy_pct", "trail_sell_pct", "fixed_sl",
         "max_hold_hours", "z_score_threshold"], row))


def main(ticker):
    node = get_node(ticker)
    df_h = load_hourly(ticker)
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])

    strat = strategies.TrailingBothZScoreBreakout(window=node["window"],
                                                    z_score_threshold=node["z_score_threshold"])
    ind = strat.generate_daily_indicators(df_daily)  # SMA/Std indexed by day, already prior-day-safe via dropna

    # Map each hourly bar to the most recently *completed* day's indicator row (i-1),
    # matching prep_inputs' daily_idx logic in backtester.py.
    daily_dates = ind.index.normalize()
    out_rows = []
    for ts, bar in df_h.iterrows():
        day = ts.normalize()
        prior_days = daily_dates[daily_dates < day]
        if len(prior_days) == 0:
            continue
        prior_day = prior_days.max()
        sma, std = ind.loc[prior_day, "SMA"], ind.loc[prior_day, "Std"]
        if pd.isna(sma) or pd.isna(std) or std == 0:
            continue
        z = (bar["Close"] - sma) / std
        lower_band = sma - std * node["z_score_threshold"]
        out_rows.append({
            "Datetime": ts, "Close": bar["Close"], "Low": bar["Low"], "High": bar["High"],
            "PriorDay": prior_day.date(), "SMA": sma, "Std": std,
            "Z": z, "ZTrigger": node["z_score_threshold"], "LowerBand": lower_band,
            "WouldBuy": bar["Close"] <= lower_band,
        })

    out = pd.DataFrame(out_rows)
    out_path = CACHE_DIR / f"{ticker}_signal_bars.csv"
    out.to_csv(out_path, index=False)
    print(f"Wrote {len(out)} bars to {out_path}")


if __name__ == "__main__":
    ticker = sys.argv[1] if len(sys.argv) > 1 else "SOXL"
    main(ticker)
