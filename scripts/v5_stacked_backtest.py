"""v5-stacked: compares fixed, named combinations of the v5 core strategy plus drought
overlay / margin add-on-at-arm / put-hedge / skim-and-reserve for SOXL, AGQ, KORU --
the 2026-08-07 conversation's "new model" built on top of already-validated pieces
(docs/deep_backlog.md's v5-stacked design entry has the full rationale).

Deliberately NOT a hyperparameter grid search: the v5 core config per ticker stays
frozen (already island-verified via the real sweep) and each overlay's own mechanics
are reused as already validated -- this script only composes a short list of fixed
stacks and reports real trade-by-trade comparisons, so "which stack to run live" stays
a head-to-head comparison, not a re-opened combinatorial search.

Combination methodology (a real modeling simplification, documented so it can be
sanity-checked): core, drought-overlay, and add-on trades are merged into ONE
chronological trade sequence and compounded together as if realized sequentially on one
account. This is exactly right for core+drought (mutually exclusive in time -- drought
only exists during core's own gaps) but an approximation for add-on (which is actually
CONCURRENT margin capital layered on top of an already-open core position, not a
sequential trade) -- treating it as one more sequential compounding step is a
simplification, not a precise concurrent-capital model. Put-hedge is applied to core,
drought, AND add-on trades independently (2026-08-07, corrected from an earlier draft
that skipped add-on out of a mistaken double-hedging concern): add-on represents a
genuinely SEPARATE block of shares bought fresh at the arm bar, not the same shares core
already holds, so it needs its own put purchased at arm_i -- hedging it is not
double-counting core's own put (bought at entry_i), it protects a distinct share block
core's hedge never covered.

Usage: .venv/bin/python scripts/v5_stacked_backtest.py [--tickers SOXL AGQ KORU]
       [--watchlist-id 65] [--otm-pcts 15 25 50] [--csv]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.drought_detection_test import load_nodes
from scripts.drought_overlay_test import get_trades_and_bars
from scripts.export_trades import load_hourly
from scripts.stacked_model.trade_schema import attach_held_days
from scripts.stacked_model.drought import generate_drought_trades
from scripts.stacked_model.add_on import generate_addon_trades
from scripts.stacked_model import put_hedge
from scripts.stacked_model import skim_reserve

UNIVERSE_DB = Path(__file__).resolve().parent.parent / "cache" / "research" / "trading_universe.db"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
DEFAULT_TICKERS = ["SOXL", "AGQ", "KORU"]
DEFAULT_OTM_PCTS = [15, 25, 50]

# Add-on-at-arm requires a real margin loan -- only AGQ's live node runs in a genuine
# margin account ('brokerage'); SOXL and KORU's live nodes both run in 'ira', which
# can't borrow regardless of "limited margin" status. Also matches the planned-node
# split in docs/design.md (2026-08-07): SOXL was validated for drought overlay, AGQ
# for add-on -- these were never meant to be offered on every ticker.
ADDON_ELIGIBLE_TICKERS = {"AGQ"}

# Real, out-of-sample-validated drought configs only (docs/research_log.md's
# 2026-08-07 entry) -- generate_drought_trades(node) with no args defaults to
# confirm_days=10/no vol_gate, which is the PLAIN DEFAULT already shown NOT
# cliff-safe (worst_neighbor=-15.4%), not a real signal. Caught 2026-08-08: this
# script was silently using that unvalidated default for every ticker instead of the
# real validated config. AGQ and KORU's drought overlays were both rejected outright
# (AGQ: -14.6%, traced to one gap-through-SL loss; KORU: 531.9% "winner" traced to a
# single-trade artifact) -- they get no drought trades at all, not a default fallback.
DROUGHT_VALIDATED_CONFIG = {"SOXL": {"confirm_days": 3, "vol_gate": 0.4}}


def combine_sequential(*trade_lists, df_h):
    """Merges multiple canonical trade lists into one chronological sequence (sorted by
    real exit timestamp via df_h) -- see module docstring for what this does and does
    not model correctly per overlay type."""
    merged = [t for lst in trade_lists for t in lst]
    return sorted(merged, key=lambda t: df_h.index[t["exit_i"]])


def compounded_and_dd(trades):
    if not trades:
        return 0.0, 0.0, 0
    rets = [t["ret"] for t in trades]
    # eq must start at the real 1.0 starting point, not the first trade's own outcome --
    # otherwise a losing first trade's own drawdown is invisible (there's no earlier
    # peak to measure it against). Caught 2026-08-08: most likely to bite exactly where
    # it matters, since v5_stacked_crash_stress.py filters to decline-only trades, whose
    # first entry is often already a loser.
    eq = np.cumprod(np.r_[1.0, [1 + r for r in rets]])
    peak = np.maximum.accumulate(eq)
    dd = float(((eq - peak) / peak).min())
    return float(eq[-1] - 1.0), dd, len(trades)


def build_stacks(node, df_h, core_trades, conn, otm_pcts):
    """Returns {stack_name: trade_list} for the fixed comparison set. put-hedge is
    swept per OTM level (15/25/50%), not defaulted to one value, per the 2026-08-06/07
    decision to keep strike as an open parameter.

    Add-on stacks are always computed and reported for every ticker (never silently
    omitted) -- but only AGQ's live node runs in a real margin account
    (ADDON_ELIGIBLE_TICKERS); SOXL/KORU's stacks including add-on are backtest-only
    numbers with no real execution path today. main() tags every row with
    `addon_executable` so a reader sees "not currently executable," not absence."""
    ticker = node["ticker"]
    drought_cfg = DROUGHT_VALIDATED_CONFIG.get(ticker)
    if drought_cfg is not None:
        drought_trades, drought_df_h = generate_drought_trades(node, **drought_cfg)
    else:
        drought_trades, drought_df_h = [], df_h
    addon_trades = generate_addon_trades(core_trades, df_h)

    stacks = {
        "core": core_trades,
        "core+drought": combine_sequential(core_trades, drought_trades, df_h=df_h),
        "core+drought+addon": combine_sequential(core_trades, drought_trades, addon_trades, df_h=df_h),
    }
    for otm in otm_pcts:
        hedged_core = put_hedge.apply_hedge(core_trades, ticker, otm, conn, df_h=df_h)
        hedged_drought = put_hedge.apply_hedge(drought_trades, ticker, otm, conn, df_h=drought_df_h)
        # add-on is a genuinely SEPARATE block of shares (bought fresh at the arm bar,
        # not the same shares core already holds), so it needs its own put purchased at
        # arm_i -- hedging it here is not double-hedging the same underlying, it's
        # protecting a distinct share block that core's own put (bought at entry_i)
        # never covered in the first place.
        hedged_addon = put_hedge.apply_hedge(addon_trades, ticker, otm, conn, df_h=df_h)
        stacks[f"core+puthedge{otm}"] = hedged_core
        stacks[f"core+drought+puthedge{otm}"] = combine_sequential(hedged_core, hedged_drought, df_h=df_h)
        stacks[f"core+drought+addon+puthedge{otm}"] = combine_sequential(
            hedged_core, hedged_drought, hedged_addon, df_h=df_h)
    return stacks


def run_skim_variant(trades, df_h, spy_df_h, latency_days_list=(0, 3, 7)):
    """Applies skim-and-reserve on top of an already-built stack's trade list. Returns
    a dict of {f"skim_latency{n}": (final_return, max_dd)} plus the skim-only upper
    bound, all compared against the same-period baseline (no skim)."""
    strat_equity = skim_reserve.daily_equity_from_trades(trades, df_h)
    spy_daily = spy_df_h["Close"].resample("D").last().dropna()
    spy_equity = spy_daily.reindex(strat_equity.index, method="ffill").bfill()
    spy_equity = spy_equity / spy_equity.iloc[0]

    n = min(len(strat_equity), len(spy_equity))
    strat_arr, spy_arr = strat_equity.to_numpy()[:n], spy_equity.to_numpy()[:n]

    out = {}
    baseline_final = float(strat_arr[-1] - 1.0)
    baseline_dd = float(np.min(strat_arr / np.maximum.accumulate(strat_arr)) - 1.0)
    out["baseline"] = (baseline_final, baseline_dd)

    skim_curve, _ = skim_reserve.skim_only(strat_arr, spy_arr)
    out["skim_only_upper_bound"] = (float(skim_curve[-1] - 1.0),
                                     float(np.min(skim_curve / np.maximum.accumulate(skim_curve)) - 1.0))

    for latency in latency_days_list:
        curve, _ = skim_reserve.manual_redeploy_overlay(strat_arr, spy_arr, latency_days=latency)
        out[f"skim_redeploy_latency{latency}d"] = (
            float(curve[-1] - 1.0), float(np.min(curve / np.maximum.accumulate(curve)) - 1.0))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=DEFAULT_TICKERS)
    ap.add_argument("--watchlist-id", type=int, default=65)
    ap.add_argument("--otm-pcts", nargs="*", type=float, default=DEFAULT_OTM_PCTS)
    ap.add_argument("--csv", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(UNIVERSE_DB)
    spy_df_h = load_hourly("SPY")

    nodes = load_nodes(args.watchlist_id, args.tickers)
    all_rows = []
    for node in nodes:
        ticker = node["ticker"]

        core_trades, df_h = get_trades_and_bars(node)
        core_trades = attach_held_days(core_trades, df_h)

        stacks = build_stacks(node, df_h, core_trades, conn, args.otm_pcts)

        for stack_name, trades in stacks.items():
            comp, dd, n_trades = compounded_and_dd(trades)
            # "addon" in the stack name but the ticker has no real margin account today
            # (see ADDON_ELIGIBLE_TICKERS) -- report the number, don't hide it, but flag
            # it as not currently executable rather than implying it's a live option.
            addon_executable = "addon" not in stack_name or ticker in ADDON_ELIGIBLE_TICKERS
            # apply_hedge() now gates liquidity per real OTM strike (2026-08-08 fix) and
            # stamps every trade it touches with hedge_priced True/False -- report the
            # real fraction actually hedged per stack instead of one ticker-wide flag
            # that used a fixed 5%-OTM check regardless of the strike actually used.
            hedge_flags = [t["hedge_priced"] for t in trades if "hedge_priced" in t]
            hedged_frac = float(np.mean(hedge_flags)) if hedge_flags else None
            row = {
                "ticker": ticker, "stack": stack_name, "hedged_frac": hedged_frac,
                "addon_executable": addon_executable,
                "trades": n_trades, "compounded_pct": comp * 100, "max_dd_pct": dd * 100,
            }
            all_rows.append(row)

            skim_results = run_skim_variant(trades, df_h, spy_df_h)
            for skim_name, (final_ret, skim_dd) in skim_results.items():
                if skim_name == "baseline":
                    continue
                all_rows.append({
                    "ticker": ticker, "stack": f"{stack_name}+{skim_name}", "hedged_frac": hedged_frac,
                    "addon_executable": addon_executable,
                    "trades": n_trades, "compounded_pct": final_ret * 100, "max_dd_pct": skim_dd * 100,
                })

        print(f"{ticker}: done ({len(stacks)} base stacks)")

    df = pd.DataFrame(all_rows).sort_values(["ticker", "compounded_pct"], ascending=[True, False])
    pd.set_option("display.width", 200)
    print("\n" + df.round(1).to_string(index=False))

    if args.csv:
        OUTPUT_DIR.mkdir(exist_ok=True)
        for ticker in args.tickers:
            df[df["ticker"] == ticker].to_csv(OUTPUT_DIR / f"v5_stacked_{ticker}.csv", index=False)
        df.to_csv(OUTPUT_DIR / "v5_stacked_comparison.csv", index=False)
        print(f"\nWrote per-ticker CSVs + {OUTPUT_DIR / 'v5_stacked_comparison.csv'}")


if __name__ == "__main__":
    main()
