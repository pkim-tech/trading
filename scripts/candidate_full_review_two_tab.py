#!/usr/bin/env python
"""Phase 10 of the pipeline's phase numbering (2026-09-01, research-session dispatch,
number chosen deliberately non-sequential after Phase5 -- room reserved for future
phases 6-9): two-tab candidate report for a candidate_nodes-sourced (in-memory pipeline)
campaign. Combines candidate_full_review.py's --kernel gt --source candidate_nodes full
checklist machinery (Tab 1) with candidate_report_inmemory.py's curated top-2-per-category
picks (Tab 2) into ONE xlsx, joined by `node_id` (candidate_nodes.id, carried on both
tabs).

Tab 1 SCOPE (deliberate, measured 2026-09-01 -- see run_phase10_full_review_candidate_
nodes' own docstring warning): a real candidate_nodes campaign version can sweep MANY
(fixed_sl, window) scopes per ticker (64 for v6.5), each with a dozens-large candidate
population -- computing the full checklist (~11-13s/candidate) for ALL of them would run
tens of hours. Tab 1 is instead scoped to exactly the node_ids that land on Tab 2's
curated set (~up to 6/ticker) -- this is the finalist-explaining use build_candidate_
report_ground_truth's own docstring already describes ("explains the finalists", not the
full grid), just applied one level higher (finalists-across-the-campaign instead of
finalists-within-one-scope). Each curated candidate gets one real full-checklist row via
gt_full_review_rows(candidates_override=[that exact candidate]), grouped by real scope
first so candidates sharing a scope don't redundantly re-derive it.

Neither tab's own per-row compute logic is reimplemented here -- this script only
discovers the curated node set, groups by real scope, and calls the existing report
builders. See:
  - scripts/candidate_full_review.py's gt_full_review_rows (Tab 1 per-candidate compute)
  - scripts/candidate_report_inmemory.py's fetch_rows/curate (Tab 2, and Tab 1's scoping)
  - scripts/phase4_candidate_nodes_resolver.py's derive_phase25_candidates_from_
    candidate_nodes(full_population=True) (per-scope candidate dict lookup)

Usage:
    .venv/bin/python scripts/candidate_full_review_two_tab.py --version VERSION [--tickers T1,T2,...] [--xlsx NAME]

If --tickers is omitted, discovers every ticker with a candidate_nodes row for --version.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from candidate_full_review import (
    DB_PATH, DEFAULT_VOL_GATE, FIELDNAMES, COLUMN_DEFS, ensure_candidate_nodes_table,
    gt_full_review_rows, _build_output_row, GT_SKIP_COLUMNS, GT_SKIP_LABEL, _git_provenance_stamp,
)
from phase4_candidate_nodes_resolver import derive_phase25_candidates_from_candidate_nodes
import candidate_report_inmemory as cri


def _tickers_for_version(conn, version):
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM candidate_nodes WHERE version=?", (version,)).fetchall()
    return sorted(r[0] for r in rows)


def _full_review_rows_for_curated(conn, curated_rows, version, vol_gate):
    """Builds Tab 1's full-checklist rows scoped to exactly `curated_rows`' node_ids --
    see module docstring for why (unscoped is tens-of-hours infeasible for a real
    multi-scope campaign). Groups by real (ticker, strategy, entry_timing, fixed_sl,
    window) scope (all present directly on each curated row -- no re-discovery needed)
    so candidates sharing a scope share one derive_phase25_candidates_from_candidate_
    nodes(full_population=True) lookup rather than repeating it per candidate."""
    groups = {}
    for r in curated_rows:
        key = (r["ticker"], r["strategy"], r["entry_timing"], r["fixed_sl"], r["window"])
        groups.setdefault(key, set()).add(r["id"])

    out_rows = []
    for (ticker, strategy, entry_timing, fixed_sl, window), ids in groups.items():
        print(f"\n{'#' * 100}\n{ticker} / {strategy} / {version} / entry_timing={entry_timing} "
              f"/ fixed_sl={fixed_sl} / window={window} -- {len(ids)} curated candidate(s)\n{'#' * 100}")
        population = derive_phase25_candidates_from_candidate_nodes(
            ticker, strategy, version, fixed_sl=fixed_sl, entry_timing=entry_timing,
            window=window, full_population=True)
        override = [c for c in population if c["id"] in ids]
        missing = ids - {c["id"] for c in override}
        if missing:
            print(f"  WARNING: {len(missing)} curated node_id(s) not found in the real "
                  f"candidate_nodes population for this scope: {sorted(missing)} -- skipping them.")
        if not override:
            continue
        try:
            out_rows.extend(gt_full_review_rows(conn, ticker, strategy, version, entry_timing,
                                                  fixed_sl, vol_gate=vol_gate,
                                                  candidates_override=override))
        except Exception as e:
            import traceback
            print(f"  UNEXPECTED error on this scope, skipping: {e}\n{traceback.format_exc()}")

    csv_rows = []
    for rec in out_rows:
        row = _build_output_row(rec)
        for col in GT_SKIP_COLUMNS:
            row[col] = GT_SKIP_LABEL
        csv_rows.append(row)
    return csv_rows


def _write_two_tab_xlsx(out_path, full_review_rows, curated_rows):
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()

    review_ws = wb.active
    review_ws.title = "Full Review"
    review_ws.append(FIELDNAMES)
    for cell in review_ws[1]:
        cell.font = Font(bold=True)
    for row in full_review_rows:
        review_ws.append([row.get(c) for c in FIELDNAMES])
    review_ws.freeze_panes = "B2"
    for i, col in enumerate(FIELDNAMES, start=1):
        review_ws.column_dimensions[get_column_letter(i)].width = max(12, min(len(col) + 2, 28))

    cand_headers = [
        "ticker", "Node ID", "Winner", "Strategy", "Core CAGR 1s", "Core CAGR 1m",
        "Add On CAGR 1s", "Drought CAGR 1s", "Worst Neighbor CAGR", "Robust Alpha",
        "Trades (sweep)", "Trades 1s", "Window", "Z", "Fixed SL", "Arm %",
        "Trail Buy %", "Trail Sell %", "Max Hold Hrs", "Entry Timing",
    ]
    cand_ws = wb.create_sheet("Candidates")
    cand_ws.append(cand_headers)
    for cell in cand_ws[1]:
        cell.font = Font(bold=True)
    curated_rows = sorted(curated_rows, key=lambda r: (r["ticker"], -(r["core_cagr_1s"] or -1e9)))
    for r in curated_rows:
        cand_ws.append([
            r["ticker"], r["id"], r["_winner"], r["strategy"],
            r["core_cagr_1s"], r["core_cagr_1m"], r["addon_cagr_1s"], r["drought_cagr_1s"],
            r["worst_neighbor_cagr"], r["robust_alpha"], r["trades"], r["n_trades_1s"],
            r["window"], r["z"], r["fixed_sl"], r["arm_pct"], r["trail_buy_pct"],
            r["trail_sell_pct"], r["max_hold_hours"], r["entry_timing"],
        ])
    cand_ws.freeze_panes = "A2"
    for i, col in enumerate(cand_headers, start=1):
        cand_ws.column_dimensions[get_column_letter(i)].width = max(10, min(len(col) + 2, 22))

    def_ws = wb.create_sheet("Column Definitions")
    def_ws.append(["Column", "Definition"])
    for cell in def_ws[1]:
        cell.font = Font(bold=True)
    for col, definition in COLUMN_DEFS.items():
        def_ws.append([col, definition])
        def_ws.cell(row=def_ws.max_row, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    def_ws.append(["node_id join key", "Full Review's 'node_id' column == Candidates' 'Node ID' column "
                                        "== candidate_nodes.id -- use it to cross-reference a curated pick "
                                        "on the Candidates tab back to its full checklist row."])
    def_ws.append(["Generated", _git_provenance_stamp()])
    def_ws.column_dimensions["A"].width = 32
    def_ws.column_dimensions["B"].width = 110

    wb.save(out_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True,
                     help="real candidate_nodes.version string for the campaign (e.g. "
                          "'v6.5-bench-inmemory-...').")
    ap.add_argument("--tickers", default=None,
                     help="comma-separated ticker list. Default: every ticker with a "
                          "candidate_nodes row for --version.")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--vol-gate", type=float, default=DEFAULT_VOL_GATE)
    ap.add_argument("--xlsx", default=None,
                     help="output/<name>.xlsx. Default: output/candidate_full_review_<version-slug>.xlsx")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    ensure_candidate_nodes_table(conn)

    tickers = args.tickers.split(",") if args.tickers else _tickers_for_version(conn, args.version)
    if not tickers:
        raise SystemExit(f"No candidate_nodes rows for version={args.version!r}")
    print(f"Tickers ({len(tickers)}): {tickers}")

    print("\n--- Building Tab 2: Candidates (curated top-2-per-category) ---")
    cand_rows_raw = cri.fetch_rows(conn, args.version)
    if not cand_rows_raw:
        raise SystemExit(f"No verified candidate_nodes rows for version={args.version!r} "
                          f"(candidate_verification_results join found nothing)")
    curated_rows, safety_known = cri.curate(cand_rows_raw)
    if not safety_known:
        print("NOTE: worst_neighbor_cagr not populated for this version -- "
              "cliff-safety filter was skipped on the Candidates tab, not applied silently.")
    if args.tickers:
        wanted = set(tickers)
        curated_rows = [r for r in curated_rows if r["ticker"] in wanted]
    print(f"Curated: {len(curated_rows)} rows across {len(set(r['ticker'] for r in curated_rows))} tickers")

    print("\n--- Building Tab 1: Full Review (scoped to Tab 2's curated node_ids -- see "
          "module docstring for why) ---")
    full_review_rows = _full_review_rows_for_curated(conn, curated_rows, args.version, args.vol_gate)

    xlsx_name = args.xlsx or f"candidate_full_review_{args.version[:60]}"
    out_path = Path("output") / (xlsx_name if xlsx_name.endswith(".xlsx") else f"{xlsx_name}.xlsx")
    out_path.parent.mkdir(exist_ok=True)
    _write_two_tab_xlsx(out_path, full_review_rows, curated_rows)
    print(f"\nWrote {out_path} (Full Review: {len(full_review_rows)} rows, "
          f"Candidates: {len(curated_rows)} rows)")
    conn.close()


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
