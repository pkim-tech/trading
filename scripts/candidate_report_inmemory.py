#!/usr/bin/env python
"""Curated candidate report for the in-memory sweep pipeline (candidate_nodes +
phase4_results, falling back to Phase5's candidate_verification_results for the
historical population -- see fetch_rows) -- the successor to build_v6_promotion_combined_report.py,
which only works off the legacy backtest_cache/discover_all_gt_scopes path (see
project_in_memory_pipeline_is_production memory: candidate_nodes is production now).

Per ticker: top 2 by core CAGR (1s), top 2 by addon CAGR (1s), top 2 by drought
CAGR (1s), deduped -- same top-2-per-category convention as the legacy script's
--curated-only mode (project_candidate_report_selection_workflow memory).

NOTE: worst_neighbor_cagr (cliff-safety check) is not populated for every campaign
version -- when it's all-NULL for the requested version, the safe-only filter is
skipped and the report says so explicitly, rather than silently including
cliff-fragile nodes as if they'd passed a safety gate.

Usage:
    .venv/bin/python scripts/candidate_report_inmemory.py --version VERSION [--xlsx NAME]
"""
import argparse
import sqlite3
from pathlib import Path

DB_PATH = "cache/research/trading_universe.db"

COLUMNS = [
    "id", "ticker", "strategy", "window", "z", "fixed_sl", "arm_pct",
    "trail_buy_pct", "trail_sell_pct", "max_hold_hours", "entry_timing",
    "robust_alpha", "trades", "worst_neighbor_cagr",
    "core_cagr_1m", "core_cagr_1s", "n_trades_1m", "n_trades_1s",
    "addon_cagr_1m", "addon_cagr_1s", "drought_cagr_1m", "drought_cagr_1s",
]


def _phase4_available_columns(conn):
    """phase4_results columns this DB actually has (empty set if the table is absent).
    Column-level, not table-level: a research DB whose phase4_results predates the
    2026-09-08 Phase5-consolidation migration would pass a table-existence check and then
    die on `no such column` at query time. Read-only -- a report generator must not
    migrate a DB it was merely pointed at."""
    return {row[1] for row in conn.execute("PRAGMA table_info(phase4_results)")}


def fetch_rows(conn, version):
    """Phase4-preferred, Phase5-fallback (2026-09-08, paired-review MEDIUM finding).

    This was an INNER JOIN against candidate_verification_results, which is written ONLY
    by Phase5 -- and Phase5 stopped being invoked per campaign (see scripts/run_inmemory_
    sweep_queue.sh), so as written it returned ZERO rows for every future campaign, and
    main() below turns zero rows into a hard `SystemExit: No verified candidate_nodes
    rows` -- a silent total emptying, not merely stale numbers. Now: LEFT JOIN both
    sources, per-field COALESCE preferring Phase4 (its cagr_pct/core_addon_cagr_ungated_
    pct/core_drought_cagr_ungated_pct come off the same trade list its own checks ran
    against), falling back to Phase5 for the ~22k historical rows that only exist there.

    The WHERE clause keeps this function's original "verified rows only" contract (that's
    what distinguishes it from candidate_full_review_two_tab._raw_population_rows) by
    requiring a real CAGR from EITHER source, rather than by requiring a Phase5 row to
    exist. A bare LEFT JOIN with no such filter would instead pull in the entire
    unverified candidate_nodes population, which is a different report.

    Units: phase4_results stores real PERCENTAGES while candidate_verification_results
    stores raw FRACTIONS. The /100 below puts the Phase4 side on the fraction convention
    every consumer of these columns here (curate's sort keys, write_xlsx's cells) already
    assumes -- mixing them is a 100x error."""
    p4_cols = _phase4_available_columns(conn)

    def coalesced(p4_col, p5_col):
        if p4_col not in p4_cols:
            return f"vr.{p5_col}"
        return f"COALESCE(p4.{p4_col} / 100.0, vr.{p5_col})"

    core_1s = coalesced("cagr_pct", "core_cagr_1s")
    addon_1s = coalesced("core_addon_cagr_ungated_pct", "addon_cagr_1s")
    drought_1s = coalesced("core_drought_cagr_ungated_pct", "drought_cagr_1s")
    # Join phase4_results only when it exists -- otherwise the JOIN itself raises on an
    # older DB even though every coalesced() above has already degraded to the vr. column.
    p4_join = ("LEFT JOIN phase4_results p4 ON p4.candidate_id = n.id" if p4_cols else "")
    q = f"""
    SELECT n.{', n.'.join(COLUMNS[:14])},
           vr.core_cagr_1m, {core_1s}, vr.n_trades_1m, vr.n_trades_1s,
           vr.addon_cagr_1m, {addon_1s}, vr.drought_cagr_1m, {drought_1s}
    FROM candidate_nodes n
    LEFT JOIN candidate_verification_results vr ON vr.candidate_id = n.id
    {p4_join}
    WHERE n.version = ?
      AND ({core_1s} IS NOT NULL OR {addon_1s} IS NOT NULL OR {drought_1s} IS NOT NULL)
    """
    cur = conn.execute(q, (version,))
    return [dict(zip(COLUMNS, r)) for r in cur.fetchall()]


def curate(rows, top_n=2):
    """top_n (2026-09-01, user request via research-session dispatch, Phase 10 v2):
    top-N-per-category instead of the original hardcoded top-2. Default stays 2 --
    byte-identical behavior for every existing caller that doesn't pass this.

    The core/addon/drought sort keys below read the SAME core_cagr_1s/addon_cagr_1s/
    drought_cagr_1s dict keys fetch_rows produces, so they inherit its Phase4-preferred/
    Phase5-fallback coalescing (and its fraction units) with no change needed here --
    there is no second, independently-sourced read of those metrics in this module."""
    safety_known = any(r["worst_neighbor_cagr"] is not None for r in rows)
    if safety_known:
        rows = [r for r in rows if (r["worst_neighbor_cagr"] or 0) > 0]

    by_ticker = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)

    def key(r):
        return r["id"]

    out = []
    for ticker, trows in by_ticker.items():
        core = sorted([r for r in trows if r["core_cagr_1s"] is not None],
                       key=lambda r: r["core_cagr_1s"], reverse=True)
        addon = sorted([r for r in trows if r["addon_cagr_1s"] is not None],
                        key=lambda r: r["addon_cagr_1s"], reverse=True)
        drought = sorted([r for r in trows if r["drought_cagr_1s"] is not None],
                          key=lambda r: r["drought_cagr_1s"], reverse=True)

        winners = {}
        for label, group in (("Core", core[:top_n]), ("Add On", addon[:top_n]),
                              ("Drought", drought[:top_n])):
            for r in group:
                k = key(r)
                if k not in winners:
                    winners[k] = (r, [])
                winners[k][1].append(label)

        for r, cats in winners.values():
            r["_winner"] = ", ".join(cats)
            out.append(r)

    return out, safety_known


def write_xlsx(rows, out_path):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    headers = [
        "ticker", "Node ID", "Winner", "Strategy", "Core CAGR 1s", "Core CAGR 1m",
        "Add On CAGR 1s", "Drought CAGR 1s", "Worst Neighbor CAGR", "Robust Alpha",
        "Trades (sweep)", "Trades 1s", "Window", "Z", "Fixed SL", "Arm %",
        "Trail Buy %", "Trail Sell %", "Max Hold Hrs", "Entry Timing",
    ]
    wb = Workbook()
    ws = wb.active
    ws.title = "Candidates"
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)

    rows.sort(key=lambda r: (r["ticker"], -(r["core_cagr_1s"] or -1e9)))
    for r in rows:
        ws.append([
            r["ticker"], r["id"], r["_winner"], r["strategy"],
            r["core_cagr_1s"], r["core_cagr_1m"], r["addon_cagr_1s"], r["drought_cagr_1s"],
            r["worst_neighbor_cagr"], r["robust_alpha"], r["trades"], r["n_trades_1s"],
            r["window"], r["z"], r["fixed_sl"], r["arm_pct"], r["trail_buy_pct"],
            r["trail_sell_pct"], r["max_hold_hours"], r["entry_timing"],
        ])
    ws.freeze_panes = "A2"

    out = Path("output") / out_path
    out.parent.mkdir(exist_ok=True)
    wb.save(out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--xlsx", default="candidate_review.xlsx")
    ap.add_argument("--top-n", type=int, default=2, help="top-N per category (Core/Add On/Drought)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    rows = fetch_rows(conn, args.version)
    if not rows:
        raise SystemExit(f"No verified candidate_nodes rows for version={args.version!r}")
    curated, safety_known = curate(rows, top_n=args.top_n)
    out = write_xlsx(curated, args.xlsx)
    print(f"Wrote {out} ({len(curated)} rows, {len(set(r['ticker'] for r in rows))} tickers)")
    if not safety_known:
        print("NOTE: worst_neighbor_cagr not populated for this version -- "
              "cliff-safety filter was skipped, not applied silently.")
    conn.close()


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
