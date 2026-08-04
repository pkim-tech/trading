"""Simulates wash-sale disallowance for a ticker's real backtested trade sequence if
run in a taxable brokerage account. The wash-sale rule (IRC 1091) disallows a loss
deduction on a sale if the same or a substantially identical security is bought within
30 days before OR after that sale -- the disallowed loss isn't gone, it's deferred by
adding it to the replacement shares' cost basis, recovered only once a later sale of
those shares doesn't itself trigger another wash sale.

This only checks same-ticker re-entries from the strategy's OWN trade sequence (not
any separate long-term hold in the same or a spouse's account, which would only add
more wash-sale exposure, not less -- see conversation for that broader caveat).

Usage: .venv/bin/python scripts/sim_wash_sale_impact.py TICKER [--notional 50000]
       [--start 2026-01-01]
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtester import prep_inputs, run_backtest_dispatch
import strategies

CACHE_DIR = Path("cache/research")

# real live/research node params (watchlist 65) -- extend as needed
NODES = {
    "AGQ": dict(strategy=strategies.TrailingExitZScoreBreakout, window=10, z_score_threshold=1.0,
                take_profit=8, fixed_sl=2.0, trail_buy_pct=0.0, trail_sell_pct=7.0, max_hold_hours=84,
                entry_timing="open_check"),
    "KORU": dict(strategy=strategies.TrailingExitZScoreBreakout, window=20, z_score_threshold=1.5,
                 take_profit=25, fixed_sl=3.0, trail_buy_pct=0.0, trail_sell_pct=3.0, max_hold_hours=105,
                 entry_timing="open_check"),
    "SOXL": dict(strategy=strategies.TrailingBothZScoreBreakout, window=10, z_score_threshold=1.0,
                 take_profit=30.0, fixed_sl=2.0, trail_buy_pct=3.0, trail_sell_pct=1.0, max_hold_hours=70,
                 entry_timing="open_check"),
}


def load_hourly(ticker):
    df = pd.read_csv(CACHE_DIR / f"{ticker}_1h.csv", index_col=0, parse_dates=True)
    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    if close_col != "Close":
        df["Close"] = df[close_col]
    return df


def get_real_trades(ticker):
    node = NODES[ticker]
    df_h = load_hourly(ticker)
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    strat = node["strategy"](window=node["window"], z_score_threshold=node["z_score_threshold"])
    ind = strat.generate_daily_indicators(df_daily)
    p = prep_inputs(df_h, ind)
    trades = run_backtest_dispatch(
        node["strategy"], df_h, ind, ticker,
        take_profit=node["take_profit"], sl_raw=node["trail_buy_pct"], max_hours_to_hold=node["max_hold_hours"],
        z_score_threshold=node["z_score_threshold"], fixed_sl=node["fixed_sl"], trail_pct_pct=node["trail_sell_pct"],
        entry_timing=node["entry_timing"], prep=p,
    )
    return pd.DataFrame(trades).sort_values("Entry Time").reset_index(drop=True)


def simulate_wash_sales(df, notional):
    """Sequential basis-chain simulation: a disallowed loss is added to the cost
    basis of the NEXT chronological entry within the wash window, reducing that
    trade's own eventual taxable gain/loss by exactly the carried amount. The chain
    keeps extending through consecutive washed losses until a trade finally closes
    without a nearby re-entry -- at which point the whole carried amount becomes
    deductible that year. This is a simplification (real wash-sale lot-matching can
    be more particular about which specific replacement lot absorbs a disallowed
    loss), reasonable for an estimate, not a substitute for real tax software/advice."""
    df = df.copy()
    df["raw_dollar_pnl"] = notional * df["Return"]

    carried_in = [0.0] * len(df)
    taxable_pnl = [0.0] * len(df)
    deferred = [False] * len(df)
    pending_carry = 0.0

    for i, row in df.iterrows():
        carried_in[i] = pending_carry
        adjusted_pnl = row["raw_dollar_pnl"] - pending_carry
        pending_carry = 0.0

        if adjusted_pnl < 0:
            window_start = row["Exit Time"] - pd.Timedelta(days=30)
            window_end = row["Exit Time"] + pd.Timedelta(days=30)
            other_entries = df.drop(i)["Entry Time"]
            washed = bool(((other_entries >= window_start) & (other_entries <= window_end)).any())
        else:
            washed = False  # wash-sale rule never applies to a gain

        if washed:
            deferred[i] = True
            taxable_pnl[i] = 0.0
            pending_carry = -adjusted_pnl  # carried into the NEXT trade's cost basis
        else:
            taxable_pnl[i] = adjusted_pnl

    df["carried_basis_in"] = carried_in
    df["adjusted_pnl"] = df["raw_dollar_pnl"] - df["carried_basis_in"]
    df["wash_disallowed"] = deferred
    df["taxable_pnl_this_trade"] = taxable_pnl
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker", choices=list(NODES.keys()))
    ap.add_argument("--notional", type=float, default=50000.0)
    ap.add_argument("--start", default=None)
    ap.add_argument("--tax-rate", type=float, default=0.32,
                     help="illustrative short-term/ordinary-income rate -- ALL these trades are "
                          "held days, so short-term rates apply; this is a placeholder, not your real bracket")
    args = ap.parse_args()

    trades = get_real_trades(args.ticker)
    if args.start:
        trades = trades[trades["Entry Time"] >= pd.Timestamp(args.start)].reset_index(drop=True)

    result = simulate_wash_sales(trades, args.notional)

    print(f"{args.ticker}: {len(result)} trades, ${args.notional:,.0f} notional per trade, "
          f"illustrative tax rate {args.tax_rate:.0%} (short-term/ordinary-income, since every "
          f"trade here is held days -- NOT your real bracket, adjust with --tax-rate)\n")
    print(f"{'Entry':<12}{'Exit':<12}{'Raw P&L':>12}{'Carried In':>12}{'Adjusted':>12}{'Washed?':>9}{'Taxable':>12}")
    for _, r in result.iterrows():
        print(f"{r['Entry Time'].date()!s:<12}{r['Exit Time'].date()!s:<12}"
              f"{r['raw_dollar_pnl']:>12,.0f}{r['carried_basis_in']:>12,.0f}"
              f"{r['adjusted_pnl']:>12,.0f}{'YES' if r['wash_disallowed'] else '':>9}"
              f"{r['taxable_pnl_this_trade']:>12,.0f}")

    total_taxable = result["taxable_pnl_this_trade"].sum()
    total_raw = result["raw_dollar_pnl"].sum()
    end_of_year_carry = result["carried_basis_in"].iloc[-1] if result["wash_disallowed"].iloc[-1] else 0.0
    # pending_carry after the LAST trade, if that trade itself was washed, is still stuck
    still_pending = 0.0
    if len(result) and result["wash_disallowed"].iloc[-1]:
        still_pending = -(result["raw_dollar_pnl"].iloc[-1] - result["carried_basis_in"].iloc[-1])

    tax_owed = max(0.0, total_taxable) * args.tax_rate
    print(f"\nTotal raw P&L (what actually happened, price-wise): ${total_raw:,.0f}")
    print(f"Total TAXABLE P&L this year (after wash-sale deferrals): ${total_taxable:,.0f}")
    print(f"Estimated tax owed this year at {args.tax_rate:.0%}: ${tax_owed:,.0f}")
    if still_pending:
        print(f"Still-open carried basis adjustment at year-end (unresolved chain, "
              f"still parked on an open/late position): ${still_pending:,.0f}")

    out = f"output/wash_sale_{args.ticker}.csv"
    result.to_csv(out, index=False)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
