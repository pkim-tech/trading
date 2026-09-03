"""Persist full-review-report Excel data into a queryable DB table -- Part 1
(backfill scan), 2026-09-03. See docs/backlog_cache.md's "persist full-review-report
Excel data into a queryable DB table" entry (written up 2026-09-02).

Real recurring friction this closes: a live-promoted node's real historical checklist/
overlay numbers (core/addon/drought/both CAGR, years, worst_neighbor, etc.) only exist in
one-off output/full_review_gt*.xlsx/candidate_full_review*.xlsx/v6_5yr_primary_*.xlsx
files, keyed by nothing queryable -- finding the right one for a given node_id means
manually recalling/pointing to a filename, and multiple same-day files can genuinely
disagree (bug-fixed vs partial-window numbers) for the same node_id with no way to tell
which is authoritative except by mtime. Real example cited in the backlog: HIBL
node_id=907 appeared in 5 different xlsx files spanning 2026-08-23 19:26-23:19, with
core_both_cagr_pct values of 124.06% (partial 3.06yr window) vs the real final 102.23%
(full 4.99yr window) -- silently different depending on which file happened to be open.

Schema decision (not pinned down in the original backlog write-up, decided here): a
single `row_json` column (one JSON-encoded {header: value} dict per row) instead of ~180
hardcoded typed columns -- every real full-review vintage scanned here ranges from 132 to
184 columns and the set has changed at least 4 times already this session alone (see
candidate_full_review_two_tab.py's own v2-v6 history) -- a flexible JSON blob survives
that evolution without a migration every time a report gains/renames a column, same
rationale as this codebase's own params_json precedent (candidate_nodes.params_json)
for an analogous "evolving field set" problem.

Natural key is (node_id, source_file, sheet_name) -- sheet_name ADDED beyond the backlog's
literal (node_id, source_file, captured_at) triple, deliberately: a single 2-tab/4-tab
report can carry the SAME node_id on multiple tabs (Full Review/Combined/Candidates/raw)
with genuinely different per-tab context (e.g. Combined's own Winner-category
annotations don't exist on Full Review) -- collapsing them to one row per (node_id, file)
would need a silent tab-precedence policy this script isn't in a position to invent.
Keeping sheet_name in the key preserves all of it, losing nothing. captured_at is the
file's own mtime (matches the backlog's own worked example, which differentiated same-day
files purely by mtime).

Part 2 (wiring writes into the live report-writer scripts, e.g.
candidate_full_review_two_tab.py's build_report()/_write_report_xlsx) is DELIBERATELY NOT
done here -- awaiting explicit user confirmation per the dispatching session's own
instruction, this is read-only backfill of already-written files only.

Usage:
  .venv/bin/python scripts/candidate_full_review_snapshots.py --backfill
  .venv/bin/python scripts/candidate_full_review_snapshots.py --backfill --dry-run
"""
import argparse
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

DB_PATH = "cache/research/trading_universe.db"
OUTPUT_DIR = os.path.join(ROOT, "output")
# Both glob patterns the backlog names, plus the "Node ID" 2-tab-report variant handled
# generically below (any sheet with a real node_id-ish column, not just these filenames)
# -- kept as the scan SCOPE (which files to look at) rather than a strict content filter.
GLOB_PATTERNS = ["*full_review*.xlsx", "v6_5yr_primary_*.xlsx"]

# Column Definitions/similar meta-sheets never carry real node rows -- skip by name rather
# than by absence-of-node_id alone, so a real sheet that's merely missing a header this
# scan doesn't recognize yet fails loud (see the "skipped, no node_id column" count in the
# summary) instead of being silently conflated with a known-irrelevant meta-sheet.
SKIP_SHEET_NAMES = {"Column Definitions"}

# Real per-vintage node_id column names seen across every scanned file (confirmed via
# direct inspection, 2026-09-03, not guessed): every vintage from the original 132-col
# candidate_full_review.py report through the current 184-col two-tab report carries a
# lowercase 'node_id' column somewhere in its checklist block. The two-tab report's own
# curated-front block ALSO carries 'Node ID' (title case) -- both a real, valid identity
# for the same row when present; 'node_id' is preferred when both exist on a row (the
# checklist's own field, present in every vintage) since it's the one constant across all
# scanned files.
NODE_ID_COLUMN_CANDIDATES = ["node_id", "Node ID"]


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidate_full_review_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER NOT NULL,
            source_file TEXT NOT NULL,
            sheet_name TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            row_json TEXT NOT NULL,
            UNIQUE(node_id, source_file, sheet_name)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_candidate_full_review_snapshots_node_id
        ON candidate_full_review_snapshots(node_id)
    """)
    conn.commit()


def _file_mtime_iso(path):
    return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat()


def _rows_from_sheet(ws):
    """Yields (node_id, row_dict) for every real data row in `ws` -- None if this sheet
    has no recognized node_id column at all (caller's own signal to skip it, not silently
    treat every row as node_id=None)."""
    headers = [c.value for c in ws[1]]
    node_col_name = next((c for c in NODE_ID_COLUMN_CANDIDATES if c in headers), None)
    if node_col_name is None:
        return None
    node_idx = headers.index(node_col_name)
    for row in ws.iter_rows(min_row=2, values_only=True):
        node_id = row[node_idx]
        if node_id is None:
            continue  # a raw-tab row genuinely missing a node_id (shouldn't happen for
                       # node_id specifically, but never fabricate one) -- skip, don't
                       # insert a NOT NULL violation candidate.
        try:
            node_id_int = int(node_id)
        except (TypeError, ValueError):
            continue  # a formula cell or non-numeric junk in the node_id column -- skip
        row_dict = {h: v for h, v in zip(headers, row) if h is not None}
        yield node_id_int, row_dict


def backfill_from_output_dir(conn, output_dir=OUTPUT_DIR, dry_run=False):
    import openpyxl

    ensure_table(conn)
    files = sorted({
        p for pattern in GLOB_PATTERNS
        for p in glob.glob(os.path.join(output_dir, pattern))
    })
    stats = {"files_scanned": 0, "files_failed": 0, "sheets_skipped_no_node_id": 0,
              "rows_inserted": 0, "rows_already_present": 0}
    for path in files:
        source_file = os.path.basename(path)
        captured_at = _file_mtime_iso(path)
        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        except Exception as e:
            print(f"  SKIP (failed to open): {source_file}: {e}")
            stats["files_failed"] += 1
            continue
        stats["files_scanned"] += 1
        for sheet_name in wb.sheetnames:
            if sheet_name in SKIP_SHEET_NAMES:
                continue
            ws = wb[sheet_name]
            rows = _rows_from_sheet(ws)
            if rows is None:
                stats["sheets_skipped_no_node_id"] += 1
                continue
            for node_id, row_dict in rows:
                row_json = json.dumps(row_dict, default=str)
                if dry_run:
                    stats["rows_inserted"] += 1
                    continue
                cur = conn.execute("""
                    INSERT OR IGNORE INTO candidate_full_review_snapshots
                        (node_id, source_file, sheet_name, captured_at, row_json)
                    VALUES (?, ?, ?, ?, ?)
                """, (node_id, source_file, sheet_name, captured_at, row_json))
                if cur.rowcount:
                    stats["rows_inserted"] += 1
                else:
                    stats["rows_already_present"] += 1
        wb.close()
        print(f"  scanned {source_file} ({len(wb.sheetnames)} sheet(s))")
    if not dry_run:
        conn.commit()
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backfill", action="store_true",
                     help="scan output/*full_review*.xlsx + output/v6_5yr_primary_*.xlsx "
                          "and persist every real node_id row found.")
    ap.add_argument("--output-dir", default=OUTPUT_DIR)
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--dry-run", action="store_true",
                     help="scan and report counts only, no DB writes (INSERT OR IGNORE "
                          "is already idempotent/safe to re-run without this, but useful "
                          "to preview before the first real run).")
    args = ap.parse_args()

    if not args.backfill:
        ap.print_help()
        sys.exit(1)

    conn = sqlite3.connect(args.db, timeout=60.0)
    stats = backfill_from_output_dir(conn, output_dir=args.output_dir, dry_run=args.dry_run)
    conn.close()

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Done: {stats['files_scanned']} file(s) scanned "
          f"({stats['files_failed']} failed to open), "
          f"{stats['sheets_skipped_no_node_id']} sheet(s) skipped (no recognized node_id column), "
          f"{stats['rows_inserted']} row(s) {'would be ' if args.dry_run else ''}inserted, "
          f"{stats['rows_already_present']} already present (idempotent re-run).")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
