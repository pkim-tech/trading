"""
Black-Scholes time-decay forecast for the put-hedge idea, built 2026-08-06 directly off
the real implied_volatility already captured in options_snapshot -- no realized-vol IV
proxy needed (the original design.md idea), since a real quoted IV is sitting in the DB
from scripts/collect_options_snapshot.py's own daily snapshot.

Real gap found in conversation: the drought-overlay has NO max-hold concept analogous to
the core strategy's max_hold_hours -- it holds until the core strategy's own next signal
(HANDOFF), which is open-ended and highly variable (per-ticker real duration distributions
range from <1 day to 86 days, see docs/research_log.md's 2026-08-06 entry). So there's no
single "hold for X days" to forecast decay against -- this script forecasts against each
ticker's own real p25/median/p75 drought-duration percentiles instead of one arbitrary X.

Method: prices the SAME contract (same strike/expiration/IV) at two points in time --
now (T = days to expiration today) and after the hold period (T = days to expiration minus
hold_days) -- holding underlying price and IV fixed. The difference isolates pure theta
decay from any price-movement effect, matching this project's existing "isolate one
variable" convention (e.g. the fill-optimism MIN/possible/pessimistic/certain resolutions).
If hold_days >= the contract's own days-to-expiration, decay is reported as "n/a (would
need to roll)" rather than extrapolating past a real option's life.

Usage: .venv/bin/python scripts/put_decay_forecast.py [--tickers ...] [--otm-pct 5]
"""
import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

import numpy as np
from scipy.stats import norm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.drought_detection_test import load_nodes
from scripts.drought_overlay_test import get_trades_and_bars, find_drought_windows

DB_PATH = Path(__file__).resolve().parent.parent / "cache" / "research" / "trading_universe.db"
RISK_FREE_RATE = 0.04


def bs_put_price(S, K, T_years, sigma, r=RISK_FREE_RATE):
    """Standard Black-Scholes European put. T_years <= 0 returns pure intrinsic value
    (contract has already expired in this forecast scenario)."""
    if T_years <= 0:
        return max(K - S, 0.0)
    if sigma <= 0:
        return max(K - S, 0.0)
    d1 = (np.log(S / K) + (r + sigma ** 2 / 2) * T_years) / (sigma * np.sqrt(T_years))
    d2 = d1 - sigma * np.sqrt(T_years)
    return K * np.exp(-r * T_years) * norm.cdf(-d2) - S * norm.cdf(-d1)


def get_duration_percentiles(ticker, watchlist_id=65, confirm_days=10):
    """Real historical drought-window durations (entry to HANDOFF/next-signal backstop)
    for one ticker, in trading days -- p25/median/p75, the hold-time scenarios this
    forecast uses instead of an arbitrary fixed X."""
    nodes = load_nodes(watchlist_id, [ticker])
    if not nodes:
        return None
    trades, df_h = get_trades_and_bars(nodes[0])
    windows = find_drought_windows(trades, df_h, confirm_days)
    if not windows:
        return None
    durations = []
    for entry_i, gap_end in windows:
        entry_bar = entry_i + 1 if entry_i + 1 < len(df_h) else entry_i
        span = (df_h.index[gap_end] - df_h.index[entry_bar]).total_seconds() / 86400
        durations.append(span)
    d = np.array(durations)
    return {"n": len(d), "p25": np.percentile(d, 25), "median": np.median(d), "p75": np.percentile(d, 75)}


def get_nearest_otm_put(conn, ticker, otm_pct, min_days_to_expiry=0):
    """Most recent snapshot's OTM put (strike <= underlying * (1 - otm_pct%)) closest to
    that target strike, for the nearest expiration with at least min_days_to_expiry left --
    lets the caller ask for a contract that can actually cover a given hold period."""
    row = conn.execute("""
        SELECT ticker, underlying_price, expiration, strike, bid, ask, implied_volatility
        FROM options_snapshot
        WHERE ticker=? AND snapshot_ts=(SELECT MAX(snapshot_ts) FROM options_snapshot WHERE ticker=?)
          AND implied_volatility IS NOT NULL AND implied_volatility > 0
        ORDER BY expiration, strike DESC
    """, (ticker, ticker)).fetchall()
    if not row:
        return None
    today = date.today()
    target_strike = row[0][1] * (1 - otm_pct / 100.0)
    candidates = [r for r in row if (date.fromisoformat(r[2]) - today).days >= min_days_to_expiry]
    if not candidates:
        return None
    nearest_exp = min(c[2] for c in candidates)
    same_exp = [c for c in candidates if c[2] == nearest_exp]
    best = min(same_exp, key=lambda c: abs(c[3] - target_strike))
    return {
        "ticker": best[0], "underlying_price": best[1], "expiration": best[2],
        "strike": best[3], "bid": best[4], "ask": best[5], "iv": best[6],
        "days_to_expiry": (date.fromisoformat(best[2]) - today).days,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=None)
    parser.add_argument("--otm-pct", type=float, default=5.0)
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    all_tickers = args.tickers or ["AGQ", "DPST", "HIBL", "KORU", "NUGT", "SOXL", "UDOW", "USD", "YANG"]

    for ticker in all_tickers:
        durs = get_duration_percentiles(ticker)
        if durs is None:
            print(f"{ticker}: no drought-window history, skipped")
            continue
        contract = get_nearest_otm_put(conn, ticker, args.otm_pct, min_days_to_expiry=int(durs["p75"]))
        if contract is None:
            print(f"{ticker}: no option contract covers even the p75 hold duration "
                  f"({durs['p75']:.0f}d) -- would need to roll mid-hold")
            continue
        S, K, iv = contract["underlying_price"], contract["strike"], contract["iv"]
        premium_now = contract["ask"]
        T0 = contract["days_to_expiry"] / 365.0
        model_price_now = bs_put_price(S, K, T0, iv)
        print(f"\n{ticker}: real drought durations n={durs['n']} "
              f"(p25={durs['p25']:.0f}d median={durs['median']:.0f}d p75={durs['p75']:.0f}d)")
        print(f"  contract: {contract['expiration']} strike={K} (IV={iv*100:.0f}%, "
              f"{contract['days_to_expiry']}d to expiry, ask=${premium_now:.2f}, "
              f"BS fair value=${model_price_now:.2f})")
        for label, hold_days in [("p25", durs["p25"]), ("median", durs["median"]), ("p75", durs["p75"])]:
            T1 = (contract["days_to_expiry"] - hold_days) / 365.0
            if T1 <= 0:
                print(f"    hold {label} ({hold_days:.0f}d): n/a -- exceeds contract life, would need to roll")
                continue
            model_price_after = bs_put_price(S, K, T1, iv)
            decay = model_price_now - model_price_after
            print(f"    hold {label} ({hold_days:.0f}d): decay=${decay:.2f} "
                  f"({decay/premium_now*100:.0f}% of ask premium)")


if __name__ == "__main__":
    main()
