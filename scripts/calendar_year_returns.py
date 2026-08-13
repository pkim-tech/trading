"""Per-calendar-year return breakdown, alongside the existing total/annualized
CAGR -- built 2026-08-13 because every existing report shows either a raw
total-return % or one blended CAGR over the whole cached-data span
(annualized_alpha_report.py's cagr()/calendar_days()), never broken out by
calendar year. Tax prep is per calendar year, not cumulative, so "what did
this do in 2024 vs 2025 vs 2026 YTD" is the number that actually matters for
that purpose.

Total/annualized CAGR is NOT recomputed here -- reuse annualized_alpha_report's
cagr()/calendar_days() directly, this module only adds the year-by-year split.

Usage (as a library -- see candidate_full_review.py/campaign_comparison_table.py
for real call sites):
    trades, df_h = get_trades_and_bars(node)   # drought_overlay_test.py
    breakdown = calendar_year_breakdown(trades, df_h)
    print(format_calendar_years(breakdown))
"""
import numpy as np


def calendar_year_breakdown(trades, df_h):
    """trades: list of dicts with 'entry_i'/'exit_i'/'ret' (the shape produced by
    drought_overlay_test.get_trades_and_bars() and reused across this codebase).
    df_h: the hourly-bar DataFrame those bar indices index into (for real
    timestamps).

    Buckets each trade by its EXIT timestamp's calendar year -- a trade becomes
    realized/taxable at exit, not entry, matching how the IRS actually treats it.
    Compounds within each year via prod(1+ret)-1, the same convention already
    used by paper_vs_backtest_reconcile.py's p_comp.

    Returns {year: {"compounded_pct": float, "trades": int, "ytd": bool}},
    ordered by year ascending. A trade with no resolved exit_i (still open) is
    skipped -- it hasn't realized anything yet, nothing to attribute to a year.
    """
    if not trades:
        return {}

    current_year = df_h.index.max().year if len(df_h.index) else None

    by_year = {}
    for t in trades:
        exit_i = t.get("exit_i")
        if exit_i is None:
            continue
        year = df_h.index[exit_i].year
        by_year.setdefault(year, []).append(t["ret"])

    out = {}
    for year in sorted(by_year):
        rets = by_year[year]
        compounded = float(np.prod([1.0 + r for r in rets]) - 1.0)
        out[year] = {
            "compounded_pct": compounded * 100.0,
            "trades": len(rets),
            "ytd": year == current_year,
        }
    return out


def format_calendar_years(breakdown):
    """Compact display string, e.g. '2024:+42.1% 2025:+118.3% 2026(YTD):+9.7%'."""
    if not breakdown:
        return "-"
    parts = []
    for year, stats in breakdown.items():
        label = f"{year}(YTD)" if stats["ytd"] else str(year)
        parts.append(f"{label}:{stats['compounded_pct']:+.1f}%")
    return " ".join(parts)
