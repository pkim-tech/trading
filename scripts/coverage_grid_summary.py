"""Coverage-grid summary for the nightly EOD routine (2026-08-13, rebuilt same day after the
first version's 'first seen' framing was correctly called out as meaningless -- it measured
when THIS SCRIPT started snapshotting, not any real history). The real signal already exists
in scripts/coverage_registry.py's own compute_status() detail string, which carries a genuine
"last <timestamp>" for any scenario_key with real coverage_events/coverage_deviations history
in ANY mode (paper/dry_run/live) -- weeks of paper-mode logging already show up there. This
script surfaces that directly (full grid, sorted worst-first) plus a day-over-day status-change
diff against a persisted snapshot table (still useful for "is this improving" trend, unlike the
scrapped aging metric). A scenario_key with genuinely zero real events in any mode (most addon_*
rows) has no "last" timestamp at all -- that's accurate, not an artifact: several of those
(addon_*) are real-order-only by design and structurally can't fire in paper (paper trading
doesn't simulate add-on legs -- see paper_addon_legs vs paper_trade_log split in the codebase).

Usage: .venv/bin/python scripts/coverage_grid_summary.py [--date YYYY-MM-DD]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import sqlite3
from datetime import date

from scripts.coverage_registry import REGISTRY, compute_status, STATUS_ORDER

DB_PATH = Path(__file__).resolve().parent.parent / "cache" / "live" / "trading_live.db"


def ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS coverage_grid_snapshot (
        snapshot_date TEXT NOT NULL,
        scenario_key TEXT NOT NULL,
        status TEXT NOT NULL,
        PRIMARY KEY (snapshot_date, scenario_key)
    )""")
    conn.commit()


def compute_today(conn, today):
    rows = {}
    for r in REGISTRY:
        status, detail = compute_status(r)
        rows[r["id"]] = (status, detail)
    conn.executemany(
        "INSERT OR REPLACE INTO coverage_grid_snapshot (snapshot_date, scenario_key, status) VALUES (?, ?, ?)",
        [(today, k, v[0]) for k, v in rows.items()],
    )
    conn.commit()
    return rows


def prior_snapshot(conn, today):
    row = conn.execute(
        "SELECT MAX(snapshot_date) FROM coverage_grid_snapshot WHERE snapshot_date < ?", (today,)
    ).fetchone()
    prior_date = row[0]
    if not prior_date:
        return None, {}
    rows = conn.execute(
        "SELECT scenario_key, status FROM coverage_grid_snapshot WHERE snapshot_date = ?", (prior_date,)
    ).fetchall()
    return prior_date, {k: v for k, v in rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=str(date.today()))
    args = parser.parse_args()
    today = args.date

    if today != str(date.today()):
        print(
            f"error: --date {today} is not today ({date.today()}). compute_status() only "
            "computes LIVE status right now -- it can't reconstruct historical status -- so "
            "passing a past/future date would silently write TODAY's live status mislabeled "
            f"under snapshot_date='{today}'. Refusing to proceed. --date only controls which "
            "prior stored snapshot the diff compares against; it does not select a historical "
            "compute date.",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    ensure_table(conn)
    today_rows = compute_today(conn, today)
    prior_date, prior_statuses = prior_snapshot(conn, today)

    print(f"=== Full coverage grid, worst-first ({today}) ===")
    ordered = sorted(today_rows.items(), key=lambda kv: STATUS_ORDER.get(kv[1][0], 99))
    for k, (status, detail) in ordered:
        print(f"  {status:20s} {k:42s} {detail}")

    print(f"\n=== Day-over-day change ===")
    if prior_date is None:
        print("No prior snapshot on file -- this is the first recorded baseline, nothing to diff yet.")
    else:
        changed = [(k, prior_statuses.get(k, "(new row)"), v[0])
                   for k, v in today_rows.items() if prior_statuses.get(k) != v[0]]
        if not changed:
            print(f"No status change vs prior snapshot ({prior_date}).")
        else:
            print(f"Changed since {prior_date}:")
            for k, old, new in sorted(changed):
                print(f"  {k:42s} {old} -> {new}")

    counts = {}
    for _, (status, _) in today_rows.items():
        counts[status] = counts.get(status, 0) + 1
    print(f"\n{len(today_rows)} rows total: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    conn.close()


if __name__ == "__main__":
    main()
