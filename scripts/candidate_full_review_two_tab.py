#!/usr/bin/env python
"""Phase 10 of the pipeline's phase numbering (2026-09-01, research-session dispatch,
number chosen deliberately non-sequential after Phase5 -- room reserved for future
phases 6-9): candidate report for a candidate_nodes-sourced (in-memory pipeline)
campaign. Combines candidate_full_review.py's --kernel gt --source candidate_nodes full
checklist machinery (Tab 1) with candidate_report_inmemory.py's curated top-N-per-category
picks (Tab 2) and the raw candidate_nodes population (Tab 3) into ONE xlsx, joined by
`node_id` (candidate_nodes.id, carried on all three tabs).

v2 (2026-09-01, same-day follow-up dispatch, user review of v1):
  - top_n widened from a hardcoded 2 to a --top-n CLI arg (default 5) -- both Tab 2 and
    Tab 1's scoping (Tab 1 = whatever Tab 2's curated set is) grow together.
  - New Tab 3 "All Candidates (raw)": every real candidate_nodes row for the version
    (thousands, e.g. 16,823 for v6.5), LEFT JOINed to candidate_verification_results for
    whatever lightweight metrics (core/addon/drought/core_both CAGR) already exist --
    deliberately NOT run through the full checklist (200x+ larger population than the
    curated set makes that infeasible, see Tab 1 scoping note below). Carries a
    `promoted_pick` bool marking node_ids in Tab 2's curated set.
  - --workers: optional ProcessPoolExecutor parallelization of Tab 1's checklist compute,
    throttled via campaign_registry.get_workers_budget(version) (same read-once, bounded-
    submission, fail-toward-unthrottled contract bench_phase1_phase2_inmemory.py's own
    _dispatch documents -- NOT imported from there, since that module is a gated backtest-
    kernel module; reimplemented standalone against just campaign_registry, which isn't
    gated). Default 1 (serial, original behavior) -- the caller decides when it's safe to
    ask for more (e.g. after confirming no other campaign is actively consuming CPU
    budget); this script does not check that itself.

Tab 1 SCOPE (deliberate, measured 2026-09-01 -- see run_phase10_full_review_candidate_
nodes' own docstring warning): a real candidate_nodes campaign version can sweep MANY
(fixed_sl, window) scopes per ticker (64 for v6.5), each with a dozens-large candidate
population -- computing the full checklist (~11-13s/candidate) for ALL of them would run
tens of hours. Tab 1 is instead scoped to exactly the node_ids that land on Tab 2's
curated set -- this is the finalist-explaining use build_candidate_report_ground_truth's
own docstring already describes ("explains the finalists", not the full grid), just
applied one level higher (finalists-across-the-campaign instead of finalists-within-one-
scope). Each curated candidate gets one real full-checklist row via gt_full_review_rows
(candidates_override=[that exact candidate]), grouped by real scope first so candidates
sharing a scope don't redundantly re-derive it.

Kept as plain callable functions, not CLI-only (per a future-integration note from the
2026-09-01 dispatch: this report is expected to eventually get wired into the sweep
pipeline itself, auto-run after Phase5) -- main() is a thin CLI wrapper over
build_report(), which a future pipeline caller can import and call directly.

Neither tab's own per-row compute logic is reimplemented here -- this script only
discovers the curated node set, groups by real scope, and calls the existing report
builders. See:
  - scripts/candidate_full_review.py's gt_full_review_rows (Tab 1 per-candidate compute)
  - scripts/candidate_report_inmemory.py's fetch_rows/curate (Tab 2, and Tab 1's scoping)
  - scripts/phase4_candidate_nodes_resolver.py's derive_phase25_candidates_from_
    candidate_nodes(full_population=True) (per-scope candidate dict lookup)

Usage:
    .venv/bin/python scripts/candidate_full_review_two_tab.py --version VERSION \\
        [--tickers T1,T2,...] [--top-n 5] [--workers 1] [--xlsx NAME]

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

RAW_COLUMNS = cri.COLUMNS[:14] + [
    "core_cagr_1m", "core_cagr_1s", "n_trades_1m", "n_trades_1s",
    "addon_cagr_1m", "addon_cagr_1s", "drought_cagr_1m", "drought_cagr_1s",
    "core_both_cagr_1m", "core_both_cagr_1s",
]


def _tickers_for_version(conn, version):
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM candidate_nodes WHERE version=?", (version,)).fetchall()
    return sorted(r[0] for r in rows)


def _raw_population_rows(conn, version):
    """ALL candidate_nodes rows for `version` (v2 item 2) -- deliberately NOT run through
    the full checklist (see module docstring's Tab 1 scoping rationale -- the raw
    population is 200x+ larger than the curated set; full-checklist compute against it
    would be wildly infeasible). LEFT JOIN so an unverified row (candidate_verification_
    results has no row for it yet) still appears, with NULL lightweight-metric columns --
    the true raw population, verified or not (candidate_report_inmemory.fetch_rows' own
    INNER JOIN deliberately only wants verified rows for ITS purpose; this is a different
    purpose)."""
    cn_cols = cri.COLUMNS[:14]
    q = f"""
    SELECT n.{', n.'.join(cn_cols)},
           vr.core_cagr_1m, vr.core_cagr_1s, vr.n_trades_1m, vr.n_trades_1s,
           vr.addon_cagr_1m, vr.addon_cagr_1s, vr.drought_cagr_1m, vr.drought_cagr_1s,
           vr.core_both_cagr_1m, vr.core_both_cagr_1s
    FROM candidate_nodes n
    LEFT JOIN candidate_verification_results vr ON vr.candidate_id = n.id
    WHERE n.version = ?
    """
    cur = conn.execute(q, (version,))
    return [dict(zip(RAW_COLUMNS, r)) for r in cur.fetchall()]


def _full_review_worker(payload):
    """Top-level, picklable ProcessPoolExecutor worker (v2 item 4) -- computes one scope-
    group's full-checklist rows in a fresh process with its OWN sqlite connection (a live
    sqlite3.Connection can't cross a process boundary). Returns the same flattened,
    GT_SKIP_COLUMNS-labeled row shape the serial path returns, plus any missing node_ids,
    so the parent's result handling doesn't care which path ran."""
    db_path, version, vol_gate, ticker, strategy, entry_timing, fixed_sl, window, ids = payload
    conn = sqlite3.connect(db_path, timeout=60.0)
    try:
        ensure_candidate_nodes_table(conn)
        population = derive_phase25_candidates_from_candidate_nodes(
            ticker, strategy, version, fixed_sl=fixed_sl, entry_timing=entry_timing,
            window=window, full_population=True)
        override = [c for c in population if c["id"] in ids]
        missing = sorted(ids - {c["id"] for c in override})
        if not override:
            return [], missing
        out_rows = gt_full_review_rows(conn, ticker, strategy, version, entry_timing, fixed_sl,
                                        vol_gate=vol_gate, candidates_override=override)
    finally:
        conn.close()
    csv_rows = []
    for rec in out_rows:
        row = _build_output_row(rec)
        for col in GT_SKIP_COLUMNS:
            row[col] = GT_SKIP_LABEL
        csv_rows.append(row)
    return csv_rows, missing


def _full_review_rows_for_curated(curated_rows, version, vol_gate, db_path, max_workers=1):
    """Builds Tab 1's full-checklist rows scoped to exactly `curated_rows`' node_ids --
    see module docstring for why (unscoped is tens-of-hours infeasible for a real multi-
    scope campaign). Groups by real (ticker, strategy, entry_timing, fixed_sl, window)
    scope (all present directly on each curated row -- no re-discovery needed) so
    candidates sharing a scope share one derive_phase25_candidates_from_candidate_nodes
    (full_population=True) lookup rather than repeating it per candidate.

    max_workers<=1 (default): original serial path, one shared connection.
    max_workers>1: ProcessPoolExecutor, throttled via campaign_registry.get_workers_
    budget(version) -- read ONCE (not polled mid-run, same convention bench_phase1_
    phase2_inmemory.py's own _dispatch documents), bounded-submission when genuinely
    throttled (budget < max_workers), fails toward unthrottled (submit-everything) on any
    missing/invalid/unreadable budget -- never toward a hang or crash. Caller decides
    if/when parallelizing is safe (e.g. no other campaign actively consuming CPU budget);
    this function does not check that itself."""
    groups = {}
    for r in curated_rows:
        key = (r["ticker"], r["strategy"], r["entry_timing"], r["fixed_sl"], r["window"])
        groups.setdefault(key, set()).add(r["id"])
    tasks = [(db_path, version, vol_gate, ticker, strategy, entry_timing, fixed_sl, window, ids)
             for (ticker, strategy, entry_timing, fixed_sl, window), ids in groups.items()]

    def _report(ticker, strategy, entry_timing, fixed_sl, window, ids, rows, missing):
        if missing:
            print(f"  WARNING: {ticker}/{strategy}/entry_timing={entry_timing}/fixed_sl={fixed_sl}/"
                  f"window={window}: {len(missing)} curated node_id(s) not found in the real "
                  f"candidate_nodes population for this scope: {missing} -- skipping them.")

    if max_workers <= 1:
        conn = sqlite3.connect(db_path, timeout=60.0)
        ensure_candidate_nodes_table(conn)
        csv_rows = []
        try:
            for i, (_db, _v, _vg, ticker, strategy, entry_timing, fixed_sl, window, ids) in enumerate(tasks):
                print(f"\n{'#' * 100}\n[{i + 1}/{len(tasks)}] {ticker} / {strategy} / {version} "
                      f"/ entry_timing={entry_timing} / fixed_sl={fixed_sl} / window={window} "
                      f"-- {len(ids)} curated candidate(s)\n{'#' * 100}")
                population = derive_phase25_candidates_from_candidate_nodes(
                    ticker, strategy, version, fixed_sl=fixed_sl, entry_timing=entry_timing,
                    window=window, full_population=True)
                override = [c for c in population if c["id"] in ids]
                missing = sorted(ids - {c["id"] for c in override})
                _report(ticker, strategy, entry_timing, fixed_sl, window, ids, None, missing)
                if not override:
                    continue
                try:
                    out_rows = gt_full_review_rows(conn, ticker, strategy, version, entry_timing,
                                                     fixed_sl, vol_gate=vol_gate,
                                                     candidates_override=override)
                except Exception as e:
                    import traceback
                    print(f"  UNEXPECTED error on this scope, skipping: {e}\n{traceback.format_exc()}")
                    continue
                for rec in out_rows:
                    row = _build_output_row(rec)
                    for col in GT_SKIP_COLUMNS:
                        row[col] = GT_SKIP_LABEL
                    csv_rows.append(row)
        finally:
            conn.close()
        return csv_rows

    from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
    import campaign_registry
    b = campaign_registry.get_workers_budget(version)
    budget = max_workers if not isinstance(b, int) or b <= 0 else max(1, min(b, max_workers))
    print(f"Phase 10 parallel checklist compute: {len(tasks)} scope-groups, pool size "
          f"{max_workers}, throttled budget {budget} "
          f"(campaign_registry.get_workers_budget({version!r})={b!r})")

    csv_rows = []
    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        task_iter = iter(tasks)
        in_flight = {}

        def _submit_next():
            try:
                t = next(task_iter)
            except StopIteration:
                return False
            fut = pool.submit(_full_review_worker, t)
            in_flight[fut] = t
            return True

        while len(in_flight) < budget and _submit_next():
            pass
        completed = 0
        while in_flight:
            done, _pending = wait(in_flight.keys(), return_when=FIRST_COMPLETED)
            for fut in done:
                (_db, _v, _vg, ticker, strategy, entry_timing, fixed_sl, window, ids) = in_flight.pop(fut)
                completed += 1
                try:
                    rows, missing = fut.result()
                except Exception as e:
                    import traceback
                    print(f"  UNEXPECTED error on {ticker}/{strategy}/fixed_sl={fixed_sl}/"
                          f"window={window}, skipping: {e}\n{traceback.format_exc()}")
                    rows, missing = [], []
                _report(ticker, strategy, entry_timing, fixed_sl, window, ids, rows, missing)
                csv_rows.extend(rows)
                print(f"  [{completed}/{len(tasks)}] {ticker}/{strategy}/fixed_sl={fixed_sl}/"
                      f"window={window} done ({len(rows)} rows)")
            while len(in_flight) < budget and _submit_next():
                pass
    return csv_rows


def _write_report_xlsx(out_path, full_review_rows, curated_rows, raw_rows):
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

    promoted_ids = {r["id"] for r in curated_rows}
    raw_headers = RAW_COLUMNS + ["promoted_pick"]
    raw_ws = wb.create_sheet("All Candidates (raw)")
    raw_ws.append(raw_headers)
    for cell in raw_ws[1]:
        cell.font = Font(bold=True)
    for r in raw_rows:
        raw_ws.append([r.get(c) for c in RAW_COLUMNS] + [r["id"] in promoted_ids])
    raw_ws.freeze_panes = "A2"
    for i, col in enumerate(raw_headers, start=1):
        raw_ws.column_dimensions[get_column_letter(i)].width = max(10, min(len(col) + 2, 20))

    def_ws = wb.create_sheet("Column Definitions")
    def_ws.append(["Column", "Definition"])
    for cell in def_ws[1]:
        cell.font = Font(bold=True)
    for col, definition in COLUMN_DEFS.items():
        def_ws.append([col, definition])
        def_ws.cell(row=def_ws.max_row, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    def_ws.append(["node_id join key", "Full Review's 'node_id' column == Candidates' 'Node ID' column "
                                        "== All Candidates (raw)'s 'id' column == candidate_nodes.id -- "
                                        "use it to cross-reference a row on any tab back to the others."])
    def_ws.append(["promoted_pick", "All Candidates (raw) only: True if this node_id is in the "
                                     "Candidates tab's curated top-N-per-category set."])
    def_ws.append(["Generated", _git_provenance_stamp()])
    def_ws.column_dimensions["A"].width = 32
    def_ws.column_dimensions["B"].width = 110

    wb.save(out_path)


def build_report(conn, version, tickers=None, top_n=5, vol_gate=DEFAULT_VOL_GATE,
                  workers=1, db_path=DB_PATH):
    """Callable core of this script -- a future pipeline caller (e.g. auto-run after
    Phase5, noted as a planned-but-not-built follow-up in the 2026-09-01 v2 dispatch) can
    import and call this directly instead of shelling out to main(). Returns
    (full_review_rows, curated_rows, raw_rows) -- the same three row-sets _write_report_
    xlsx consumes, so a caller that wants the data without an xlsx can use this alone."""
    ensure_candidate_nodes_table(conn)
    all_tickers = tickers or _tickers_for_version(conn, version)
    if not all_tickers:
        raise SystemExit(f"No candidate_nodes rows for version={version!r}")
    print(f"Tickers ({len(all_tickers)}): {all_tickers}")

    print(f"\n--- Building Tab 2: Candidates (curated top-{top_n}-per-category) ---")
    cand_rows_raw = cri.fetch_rows(conn, version)
    if not cand_rows_raw:
        raise SystemExit(f"No verified candidate_nodes rows for version={version!r} "
                          f"(candidate_verification_results join found nothing)")
    curated_rows, safety_known = cri.curate(cand_rows_raw, top_n=top_n)
    if not safety_known:
        print("NOTE: worst_neighbor_cagr not populated for this version -- "
              "cliff-safety filter was skipped on the Candidates tab, not applied silently.")
    if tickers:
        wanted = set(all_tickers)
        curated_rows = [r for r in curated_rows if r["ticker"] in wanted]
    print(f"Curated: {len(curated_rows)} rows across {len(set(r['ticker'] for r in curated_rows))} tickers")

    print("\n--- Building Tab 1: Full Review (scoped to Tab 2's curated node_ids -- see "
          "module docstring for why) ---")
    full_review_rows = _full_review_rows_for_curated(curated_rows, version, vol_gate, db_path,
                                                       max_workers=workers)

    print("\n--- Building Tab 3: All Candidates (raw) ---")
    raw_rows = _raw_population_rows(conn, version)
    if tickers:
        wanted = set(all_tickers)
        raw_rows = [r for r in raw_rows if r["ticker"] in wanted]
    print(f"Raw population: {len(raw_rows)} rows")

    return full_review_rows, curated_rows, raw_rows


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
    ap.add_argument("--top-n", type=int, default=5,
                     help="top-N per category (Core/Add On/Drought) for the curated set "
                          "(Tab 2 and Tab 1's scope both use this). Default 5.")
    ap.add_argument("--workers", type=int, default=1,
                     help="ProcessPoolExecutor pool size for Tab 1's checklist compute "
                          "(throttled via campaign_registry.get_workers_budget(--version)). "
                          "Default 1 (serial). Caller's responsibility to confirm no other "
                          "campaign is actively consuming CPU budget before raising this.")
    ap.add_argument("--xlsx", default=None,
                     help="output/<name>.xlsx. Default: output/candidate_full_review_<version-slug>.xlsx")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    tickers = args.tickers.split(",") if args.tickers else None
    full_review_rows, curated_rows, raw_rows = build_report(
        conn, args.version, tickers=tickers, top_n=args.top_n, vol_gate=args.vol_gate,
        workers=args.workers, db_path=args.db)

    xlsx_name = args.xlsx or f"candidate_full_review_{args.version[:60]}"
    out_path = Path("output") / (xlsx_name if xlsx_name.endswith(".xlsx") else f"{xlsx_name}.xlsx")
    out_path.parent.mkdir(exist_ok=True)
    _write_report_xlsx(out_path, full_review_rows, curated_rows, raw_rows)
    print(f"\nWrote {out_path} (Full Review: {len(full_review_rows)} rows, "
          f"Candidates: {len(curated_rows)} rows, All Candidates (raw): {len(raw_rows)} rows)")
    conn.close()


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
