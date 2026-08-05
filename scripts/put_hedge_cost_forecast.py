"""
Real-cost forecast for a ROLLING put hedge on the drought-overlay, built 2026-08-06 as a
direct follow-on to put_decay_forecast.py. That script forecast decay for holding ONE
contract for a fixed period; this one prices the actual proposed design (buy the nearest
available put, roll into a fresh one before hitting the steep late-life decay zone, repeat
until the drought closes) against every REAL historical drought-window duration for a
ticker -- not just p25/median/p75 -- since the overlay has no max-hold (see
docs/research_log.md's 2026-08-06 duration-vs-outcome finding: long windows are
disproportionately TRAIL wins, so an arbitrary cap would cut into exactly the tail that
matters) and durations are highly variable (<1 day to 86 days).

Cost per roll = BS theta decay over one real roll cycle (using the ticker's OWN actual
nearest-expiration gap from options_snapshot, not an assumed weekly cadence -- this is
the real constraint found earlier: only AGQ/DPST/KORU/NUGT/SOXL have weekly expirations,
the other 4 have to roll on a much coarser, more expensive cadence) + the real observed
bid-ask spread on that contract (round-trip cost of exiting one contract and entering the
next). Both use TODAY's snapshot only (no future term-structure data exists) -- same
"hold everything but the decaying variable fixed" simplification as put_decay_forecast.py.

Usage: .venv/bin/python scripts/put_hedge_cost_forecast.py [--tickers ...] [--otm-pct 5]
"""
import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.drought_detection_test import load_nodes
from scripts.drought_overlay_test import get_trades_and_bars, find_drought_windows
from scripts.put_decay_forecast import bs_put_price, DB_PATH

CONFIRM_DAYS = 10


def get_real_durations(ticker):
    nodes = load_nodes(65, [ticker])
    if not nodes:
        return []
    trades, df_h = get_trades_and_bars(nodes[0])
    windows = find_drought_windows(trades, df_h, CONFIRM_DAYS)
    durs = []
    for entry_i, gap_end in windows:
        entry_bar = entry_i + 1 if entry_i + 1 < len(df_h) else entry_i
        durs.append((df_h.index[gap_end] - df_h.index[entry_bar]).total_seconds() / 86400)
    return durs


def get_roll_contract(conn, ticker, otm_pct):
    """Nearest-expiration OTM put from today's snapshot, plus the REAL gap (days) to the
    ticker's own next-nearest expiration -- the real roll cadence, not an assumed one."""
    rows = conn.execute("""
        SELECT expiration, underlying_price, strike, bid, ask, implied_volatility
        FROM options_snapshot
        WHERE ticker=? AND snapshot_ts=(SELECT MAX(snapshot_ts) FROM options_snapshot WHERE ticker=?)
          AND implied_volatility IS NOT NULL AND implied_volatility > 0
        ORDER BY expiration, strike DESC
    """, (ticker, ticker)).fetchall()
    if not rows:
        return None
    exps = sorted(set(r[0] for r in rows))
    if len(exps) < 2:
        return None
    today = date.today()
    nearest_exp, next_exp = exps[0], exps[1]
    roll_interval_days = (date.fromisoformat(next_exp) - date.fromisoformat(nearest_exp)).days
    days_to_expiry = (date.fromisoformat(nearest_exp) - today).days
    px = rows[0][1]
    target_strike = px * (1 - otm_pct / 100.0)
    same_exp = [r for r in rows if r[0] == nearest_exp]
    best = min(same_exp, key=lambda r: abs(r[2] - target_strike))
    return {
        "expiration": nearest_exp, "underlying_price": px, "strike": best[2],
        "bid": best[3], "ask": best[4], "iv": best[5],
        "days_to_expiry": days_to_expiry, "roll_interval_days": roll_interval_days,
    }


def cost_for_duration(contract, hold_days):
    """Total forecast cost (decay + spread) of rolling this contract's shape for
    hold_days, using n_rolls = ceil(hold_days / roll_interval_days) whole rolls -- each
    roll priced as BS(T=roll_interval) - BS(T=0) [full decay of one fresh short cycle,
    the worst case if held to each contract's own expiry rather than sold early] plus
    the real observed spread once per roll."""
    S, K, iv = contract["underlying_price"], contract["strike"], contract["iv"]
    R = contract["roll_interval_days"]
    spread = contract["ask"] - contract["bid"]
    n_rolls = max(1, int(np.ceil(hold_days / R)))
    price_fresh = bs_put_price(S, K, R / 365.0, iv)
    price_at_roll_end = bs_put_price(S, K, 0.0, iv)  # intrinsic only, worst case
    decay_per_roll = price_fresh - price_at_roll_end
    return n_rolls * (decay_per_roll + spread), n_rolls


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=None)
    parser.add_argument("--otm-pct", type=float, default=5.0)
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    tickers = args.tickers or ["AGQ", "DPST", "HIBL", "KORU", "NUGT", "SOXL", "UDOW", "USD", "YANG"]

    for ticker in tickers:
        durs = get_real_durations(ticker)
        if not durs:
            print(f"{ticker}: no drought-window history, skipped")
            continue
        contract = get_roll_contract(conn, ticker, args.otm_pct)
        if contract is None:
            print(f"{ticker}: not enough expirations to determine a roll cadence, skipped")
            continue
        costs, rolls = [], []
        for d in durs:
            cost, n = cost_for_duration(contract, d)
            costs.append(cost)
            rolls.append(n)
        costs = np.array(costs)
        premium_pct = costs / contract["ask"] * 100
        print(f"\n{ticker}: roll cadence={contract['roll_interval_days']}d "
              f"(spread=${contract['ask']-contract['bid']:.2f}, IV={contract['iv']*100:.0f}%, "
              f"strike={contract['strike']} ask=${contract['ask']:.2f})")
        print(f"  n_windows={len(durs)}  mean_rolls={np.mean(rolls):.1f}  "
              f"mean_cost=${np.mean(costs):.2f} ({np.mean(premium_pct):.0f}% of one fresh premium)  "
              f"median_cost=${np.median(costs):.2f}  max_cost=${np.max(costs):.2f}")


if __name__ == "__main__":
    main()
