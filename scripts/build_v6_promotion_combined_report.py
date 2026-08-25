#!/usr/bin/env python
"""Build the v6 promotion combined report: one xlsx covering N tickers x both
strategies x all real fixed_sl scopes under each ticker's real PRIMARY (widest-
window) GT version, with a curated 20-column front layout for promotion
decisions plus the full raw report as reference columns starting at AA.

Serves the 2026-08-23 promoter-session backlog item on report-staleness /
one-off-script drift: this replaces the ad hoc python -c invocations used that
night to build output/v6_5yr_primary_combined.xlsx.

Auto-resolves each ticker's primary version as the one with the widest real
date span among its discovered kernel_version='ground_truth_v6' scopes --
NEVER assumes a hardcoded version string, since a ticker can carry multiple
real windowed-sweep versions (see docs/backlog_cache.md's 2026-08-23 "always
scope GT backtest_cache queries by version" entry).

Usage:
    .venv/bin/python scripts/build_v6_promotion_combined_report.py TICKER [TICKER ...]
        [--strategies both|TrailingBothZScoreBreakout|TrailingExitZScoreBreakout]
        [--xlsx NAME] [--curated-only]

--curated-only (added 2026-08-24, user's real selection process): instead of dumping
every (ticker, strategy, fixed_sl) row, filter to core-safe rows (Cliff Safe == SAFE)
and, per category, an overlay stability requirement -- Add On rows need
addon_wr_tranche != FRAGILE, Drought rows need Drought WR Verdict != FRAGILE, Best-Both
has no overlay to check. Within each of the 3 categories (Add On / Drought / Best-Both,
Best-Both = core strategy_cagr_pct restricted to TrailingBothZScoreBreakout, the live
default), sort descending by that category's own metric (Add on %, Drought %, Cagr) and
keep the top 2 -- up to 6 rows per ticker, deduped (a row can win more than one category,
counted once). This mirrors the user's manual process but is a mechanical first pass --
user's own final pick also weighs robustness/walk-forward columns already present in the
raw reference block (AA+), not just the sort metric, so this is a shortlist, not the
final answer.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from candidate_full_review import (
    gt_full_review_rows, _build_output_row, GT_SKIP_COLUMNS, GT_SKIP_LABEL, FIELDNAMES,
)
from prune_backtest_cache_ground_truth import discover_all_gt_scopes

DB_PATH = "cache/research/trading_universe.db"
BOTH_STRATEGIES = ("TrailingBothZScoreBreakout", "TrailingExitZScoreBreakout")

# Real promotion decisions from scripts/promote_v6_2026_08_23_batch1.py's BATCH list
# (ticker -> candidate_nodes id actually promoted to live 2026-08-23). Only the 8
# tickers with no open position at promotion time are here -- DFEN/DPST/SOXL/WEBL
# were deliberately excluded that night (real open position/pending buy, user's own
# call) and have no promoted node_id yet.
PROMOTED_NODE_IDS = {
    "AGQ": 436, "GDXU": 1314, "HIBL": 907, "JNUG": 879,
    "KORU": 520, "LABU": 1446, "NUGT": 778, "UGL": 993,
}

CURATED_HEADERS = [
    "ticker", "#", "Node ID", "Matches Promotion", "K1", "Strategy", "Winner", "Cagr", "Worst Neighbor",
    "Target (core/addon/drought/overlay) - Manual",
    "Trades", "Years", "Cliff Safe", "Add on %", "Add on trades", "Add on tranche",
    "addon_wr_tranche", "Drought %", "Drought trades", "Drought tranche",
    "Drought WR Verdict", "Cagr Add on", "CAGR Drought", "CAGR Both",
]
MANUAL_BLANK_COLS = 6  # U-Z, left for the user's own added columns


def primary_version_for_ticker(conn, ticker):
    """Widest real date span among this ticker's discovered GT versions -- see
    candidate_full_review.py's _window_dates_from_version for the same parsing
    convention. Falls back to the raw version string if no '-w' date suffix is
    found on any scope (shouldn't happen for a real massive-tagged campaign)."""
    from candidate_full_review import _window_dates_from_version
    import datetime

    scopes = [s for s in discover_all_gt_scopes(conn) if s[0] == ticker]
    versions = {s[2] for s in scopes}
    if not versions:
        raise RuntimeError(f"No GT scopes found for {ticker!r}")

    def span_days(v):
        start, end = _window_dates_from_version(v)
        if not start or not end:
            return -1
        return (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days

    return max(versions, key=span_days)


def strategies_for_ticker(conn, ticker, version, requested):
    if requested != "both":
        return [requested]
    scopes = discover_all_gt_scopes(conn)
    return sorted({s[1] for s in scopes if s[0] == ticker and s[2] == version and s[1] in BOTH_STRATEGIES})


def build_rows(conn, tickers, strategy_arg, max_retries=3, retry_delay_secs=5):
    """Retries a scope on 'database is locked' (real, recurring tonight from
    concurrent sweep/report jobs sharing the same sqlite file) -- any other
    exception still fails that scope immediately, matching the prior one-off
    scripts' behavior.

    Prints an index/total + elapsed/ETA line per scope (2026-08-24, user's real ask
    after a 35+min curated-report run gave zero visibility into how much was left --
    total is known upfront since it's just tickers x strategies x 5 fixed_sl scopes,
    so ETA is a simple elapsed/done*remaining projection, not a real estimator)."""
    import time

    # Resolve version + strategies per ticker first so the total scope count is
    # known before any of the slow per-scope work starts.
    plan = []
    for ticker in tickers:
        version = primary_version_for_ticker(conn, ticker)
        strategies = strategies_for_ticker(conn, ticker, version, strategy_arg)
        for strategy in strategies:
            for fixed_sl in range(1, 6):
                plan.append((ticker, strategy, version, fixed_sl))

    total = len(plan)
    start = time.time()
    out_rows = []
    for i, (ticker, strategy, version, fixed_sl) in enumerate(plan, start=1):
        elapsed = time.time() - start
        eta = f", ETA {(elapsed / (i - 1) * (total - i + 1)):.0f}s" if i > 1 else ""
        print(f"[{i}/{total}, {elapsed:.0f}s elapsed{eta}] {ticker} / {strategy} / {version} / fixed_sl={fixed_sl}")
        for attempt in range(1, max_retries + 1):
            try:
                out_rows.extend(gt_full_review_rows(conn, ticker, strategy, version, "open_check", fixed_sl))
                break
            except Exception as e:
                locked = "database is locked" in str(e)
                if locked and attempt < max_retries:
                    print(f"  database locked, retry {attempt}/{max_retries} in {retry_delay_secs}s")
                    time.sleep(retry_delay_secs)
                    continue
                print(f"  ERROR (attempt {attempt}): {e}")
                break
    return out_rows


def curate_rows(csv_rows):
    """Filter to the user's real selection pass: core-safe rows, top 2 per category
    (Add On / Drought / Best-Both), deduped by (ticker, strategy, fixed_sl). A row
    can qualify for more than one category -- only emitted once, but every category
    it won is recorded in r['_winner'] (2026-08-24, user's ask) so the curated output
    shows WHY each row is here instead of leaving it implicit.

    Fallback (2026-08-24, user's ask): a ticker with zero rows qualifying for any
    category (no safe node won Add On/Drought/Best-Both top-2 -- distinct from having
    no safe node at all) still gets its single best CAGR core-safe row included,
    tagged r['_winner'] = 'Core (no category winner)', so no ticker silently drops
    out of the report entirely."""
    by_ticker = {}
    for r in csv_rows:
        by_ticker.setdefault(r["ticker"], []).append(r)

    def key(r):
        return (r["ticker"], r["strategy"], r.get("fixed_sl"))

    out = []
    for ticker, rows in by_ticker.items():
        safe = [r for r in rows if r.get("status") == "SAFE"]

        addon = [r for r in safe if r.get("addon_tranche") != "FRAGILE"
                 and r.get("addon_compounded_pct") is not None]
        addon.sort(key=lambda r: r["addon_compounded_pct"], reverse=True)

        drought = [r for r in safe if r.get("drought_tranche") != "FRAGILE"
                   and r.get("drought_compounded_pct") is not None]
        drought.sort(key=lambda r: r["drought_compounded_pct"], reverse=True)

        best_both = [r for r in safe if r.get("strategy") == "TrailingBothZScoreBreakout"]
        best_both.sort(key=lambda r: r.get("strategy_cagr_pct") or float("-inf"), reverse=True)

        winners = {}  # key(r) -> row, categories won
        for label, group in (("Add On", addon[:2]), ("Drought", drought[:2]), ("Best-Both", best_both[:2])):
            for r in group:
                k = key(r)
                if k not in winners:
                    winners[k] = (r, [])
                winners[k][1].append(label)

        if winners:
            for r, cats in winners.values():
                r["_winner"] = ", ".join(cats)
                out.append(r)
        elif safe:
            best = max(safe, key=lambda r: r.get("strategy_cagr_pct") or float("-inf"))
            best["_winner"] = "Core (no category winner)"
            out.append(best)
    return out


def write_combined_xlsx(out_rows, out_path, curated_only=False):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    csv_rows = []
    for rec in out_rows:
        row = _build_output_row(rec)
        for col in GT_SKIP_COLUMNS:
            row[col] = GT_SKIP_LABEL
        csv_rows.append(row)
    csv_rows.sort(key=lambda r: (r["ticker"], r["strategy"]))

    if curated_only:
        csv_rows = curate_rows(csv_rows)
        csv_rows.sort(key=lambda r: (r["ticker"], r["strategy"]))

    wb = Workbook()
    ws = wb.active
    ws.title = "Combined"

    full_headers = CURATED_HEADERS + [None] * MANUAL_BLANK_COLS + list(FIELDNAMES)
    ws.append(full_headers)
    for c in ws[1]:
        if c.value:
            c.font = Font(bold=True)

    for i, r in enumerate(csv_rows, start=2):
        promoted_id = PROMOTED_NODE_IDS.get(r["ticker"])
        matches_promotion = ("N/A (not yet promoted)" if promoted_id is None
                              else ("YES" if r["node_id"] == promoted_id else "no"))
        curated = [
            r["ticker"], f"=COUNTIF($A$2:A{i},A{i})", r["node_id"], matches_promotion, r.get("k1_tranche"),
            r.get("strategy"), r.get("_winner"), r["strategy_cagr_pct"],
            r["worst_neighbor_pct"], None, r["trades"], r["years"], r["status"],
            r["addon_compounded_pct"], r["addon_n"], r["addon_tranche"], r["addon_wr_tranche"],
            r["drought_compounded_pct"], r["drought_n"], r["drought_tranche"], r["drought_wr_verdict"],
            r["core_addon_cagr_pct"], r["core_drought_cagr_pct"], r["core_both_cagr_pct"],
        ]
        raw = [r.get(h) for h in FIELDNAMES]
        ws.append(curated + [None] * MANUAL_BLANK_COLS + raw)

    for i, h in enumerate(CURATED_HEADERS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = max(10, min(len(h) + 2, 30))
    ws.freeze_panes = "B2"

    out = Path("output") / out_path
    out.parent.mkdir(exist_ok=True)
    wb.save(out)
    print(f"Wrote {out} ({len(csv_rows)} rows, {len(full_headers)} columns, AA={FIELDNAMES[0]!r})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--strategies", default="both",
                     choices=["both", *BOTH_STRATEGIES])
    ap.add_argument("--xlsx", default="v6_5yr_primary_combined.xlsx")
    ap.add_argument("--curated-only", action="store_true",
                     help="filter to core-safe rows, top 2 per category (Add On/Drought/Best-Both)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    out_rows = build_rows(conn, args.tickers, args.strategies)
    write_combined_xlsx(out_rows, args.xlsx, curated_only=args.curated_only)
    conn.close()


if __name__ == "__main__":
    main()
