"""Execution price-drift distribution report.

Makes the 2026-08-15 execution-price-drift-audit backlog item's analysis
(docs/research_log.md's "2026-08-15 -- Execution price-drift audit" entry)
repeatable, per this project's standing convention that research/audit work
with plausible re-run value should live as a real script under scripts/,
not a one-off. Reproduces the same analysis: pulls entry_drift_pct/
exit_drift_pct from trade_log (split real broker fills, is_dry_run_sim=0,
from synthesized dry-run/canary fills, is_dry_run_sim=1) and separately from
paper_trade_log, and reports mean/median/std/min/max plus |drift| threshold
breach rates (>0.5%/1%/2%/5%), pooled/per-ticker/per-side, keeping all three
populations distinct throughout (never blended).

Also useful for re-checking the calibration behind signals_db.
check_abnormal_drift's ABNORMAL_DRIFT_THRESHOLD_PCT once more real trades
accumulate.

Usage:
    .venv/bin/python scripts/drift_distribution_report.py
    .venv/bin/python scripts/drift_distribution_report.py --ticker HIBL
    .venv/bin/python scripts/drift_distribution_report.py --no-exclude-catchup
    .venv/bin/python scripts/drift_distribution_report.py --exclude-dates 2026-07-06
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_config

THRESHOLDS = [0.5, 1, 2, 5]

# The 2026-07-06 KORU/SOXL catch-up entries (backdated off a missed 2026-07-02
# signal, see CLAUDE.md's "Open positions" note) are the one known-anomalous
# cluster the original audit excluded from REAL when computing the "cleaned"
# distribution meant to inform an alert threshold. Generalized here into an
# adjustable exclusion-by-entry-date rule (--exclude-dates) rather than a
# hardcoded ticker list, so a future backdated-catchup cluster can be excluded
# the same way without editing this script.
DEFAULT_EXCLUDE_DATES = ["2026-07-06"]


def load_population(con, table, is_dry_run_sim=None):
    query = f"SELECT * FROM {table}"
    if is_dry_run_sim is not None:
        query += f" WHERE is_dry_run_sim = {int(is_dry_run_sim)}"
    df = pd.read_sql_query(query, con)
    if not df.empty:
        df["entry_time"] = pd.to_datetime(df["entry_time"], errors="coerce")
        df["exit_time"] = pd.to_datetime(df["exit_time"], errors="coerce")
    return df


def apply_exclusions(df, exclude_dates):
    if not exclude_dates or df.empty:
        return df, df.iloc[0:0]
    entry_date = df["entry_time"].dt.strftime("%Y-%m-%d")
    mask = entry_date.isin(exclude_dates)
    return df[~mask].copy(), df[mask].copy()


def side_stats(series):
    s = series.dropna()
    n = len(s)
    if n == 0:
        return None
    abs_s = s.abs()
    row = {
        "n": n,
        "mean": s.mean(),
        "median": s.median(),
        "std": s.std() if n > 1 else 0.0,
        "min": s.min(),
        "max": s.max(),
    }
    for t in THRESHOLDS:
        row[f">{t}%"] = (abs_s > t).sum() / n * 100
    return row


def fmt_row(label, row):
    if row is None:
        return f"{label:<20} n=0"
    return (
        f"{label:<20} n={row['n']:<4} mean={row['mean']:>7.2f}% median={row['median']:>7.2f}% "
        f"std={row['std']:>6.2f} min={row['min']:>8.2f}% max={row['max']:>8.2f}%  "
        f"breach: >0.5%={row['>0.5%']:>5.1f}% >1%={row['>1%']:>5.1f}% "
        f">2%={row['>2%']:>5.1f}% >5%={row['>5%']:>5.1f}%"
    )


def report_population(name, df, ticker_filter=None):
    print(f"\n=== {name} (n={len(df)}) ===")
    if ticker_filter:
        df = df[df["ticker"] == ticker_filter]
        print(f"(filtered to ticker={ticker_filter}, n={len(df)})")
    if df.empty:
        print("  (no rows)")
        return

    print("\n-- Pooled --")
    print(fmt_row("entry", side_stats(df["entry_drift_pct"])))
    print(fmt_row("exit", side_stats(df["exit_drift_pct"])))

    print("\n-- Per ticker --")
    for ticker, sub in df.groupby("ticker"):
        er = side_stats(sub["entry_drift_pct"])
        xr = side_stats(sub["exit_drift_pct"])
        if er:
            print(fmt_row(f"{ticker} entry", er))
        if xr:
            print(fmt_row(f"{ticker} exit", xr))

    if "exit_reason" in df.columns and df["exit_reason"].notna().any():
        print("\n-- By exit_reason (exit_drift_pct only) --")
        for reason, sub in df.groupby("exit_reason"):
            r = side_stats(sub["exit_drift_pct"])
            if r:
                print(fmt_row(str(reason), r))

    if "strategy" in df.columns:
        print("\n-- By strategy --")
        for strat, sub in df.groupby("strategy"):
            er = side_stats(sub["entry_drift_pct"])
            xr = side_stats(sub["exit_drift_pct"])
            if er:
                print(fmt_row(f"{strat} entry", er))
            if xr:
                print(fmt_row(f"{strat} exit", xr))

    if "account" in df.columns and df["account"].notna().any():
        print("\n-- By account --")
        for account, sub in df.groupby("account"):
            er = side_stats(sub["entry_drift_pct"])
            if er:
                print(fmt_row(f"{account} entry", er))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", help="Filter all populations to a single ticker")
    ap.add_argument(
        "--exclude-dates", nargs="*", default=DEFAULT_EXCLUDE_DATES,
        help=f"REAL-population entry dates (YYYY-MM-DD) to exclude as known-anomalous backdated catch-up entries. Default: {DEFAULT_EXCLUDE_DATES}",
    )
    ap.add_argument(
        "--no-exclude-catchup", action="store_true",
        help="Disable date-based exclusion; report REAL's raw pooled numbers only",
    )
    ap.add_argument("--db", default=str(signals_config.DB_PATH), help="Path to trading_live.db")
    args = ap.parse_args()

    exclude_dates = [] if args.no_exclude_catchup else args.exclude_dates

    con = sqlite3.connect(args.db)
    real_all = load_population(con, "trade_log", is_dry_run_sim=0)
    dry_run_sim = load_population(con, "trade_log", is_dry_run_sim=1)
    paper = load_population(con, "paper_trade_log")
    con.close()

    print(f"Drift distribution report -- db={args.db}")
    print(f"Population sizes (all rows, incl. still-open w/ NULL exit): "
          f"REAL={len(real_all)} DRY_RUN_SIM={len(dry_run_sim)} PAPER={len(paper)}")

    real_clean, real_excluded = apply_exclusions(real_all, exclude_dates)
    if exclude_dates:
        print(f"\nExcluding REAL rows with entry_time date in {exclude_dates} "
              f"(known-anomalous backdated catch-up entries, e.g. the 2026-07-06 "
              f"KORU/SOXL catch-up off a missed 2026-07-02 signal): "
              f"{len(real_excluded)} row(s) excluded.")
        if not real_excluded.empty:
            cols = ["ticker", "entry_time", "entry_drift_pct", "exit_drift_pct"]
            print(real_excluded[cols].to_string(index=False))

    report_population("REAL (is_dry_run_sim=0, excl. known-anomalous)", real_clean, args.ticker)
    if exclude_dates and not real_excluded.empty:
        report_population("REAL (is_dry_run_sim=0, RAW pooled, incl. excluded)", real_all, args.ticker)
    report_population("DRY_RUN_SIM (is_dry_run_sim=1)", dry_run_sim, args.ticker)
    report_population("PAPER (paper_trade_log)", paper, args.ticker)

    print(
        "\nNote: DRY_RUN_SIM's exit_drift is near-zero-variance by construction "
        "(fills synthesized against the same price series the exit check reads) "
        "-- reflects synthesis fidelity, not real execution quality; don't blend "
        "into a real-execution threshold discussion. PAPER typically shows worse "
        "drift than REAL, plausibly because paper's entry-fill simulation prices "
        "off cached hourly-bar data rather than a live tick (see docs/deep_backlog.md's "
        "2026-08-12 paper-vs-backtest reconciliation finding)."
    )


if __name__ == "__main__":
    main()
