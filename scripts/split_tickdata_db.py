#!/usr/bin/env python3
"""One-time split: move the massive_* tick-data tables (plus active_builds, the
promotion pointer that references them) out of trading_universe.db into their own
tickdata.db. Run with --copy first, then --verify, then --drop-and-vacuum once
verification passes. Each phase is separate and re-runnable -- --copy uses
CREATE TABLE IF NOT EXISTS + INSERT OR IGNORE (skips rows already present via the
table's own PK), so a re-run after a partial failure resumes rather than
duplicating.

--root overrides the repo root the cache/research/*.db paths are resolved under
(needed when this script is invoked from a git worktree copy that doesn't have its
own cache/ directory -- the real trading_universe.db lives only in the main
checkout)."""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

TABLES = [
    ("massive_dividends_raw", """
        CREATE TABLE IF NOT EXISTS massive_dividends_raw (
            ticker                        TEXT NOT NULL,
            ex_dividend_date              TEXT NOT NULL,
            historical_adjustment_factor  REAL NOT NULL,
            cash_amount                   REAL,
            fetched_at                    TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (ticker, ex_dividend_date)
        )
    """),
    ("massive_hourly_derived", """
        CREATE TABLE IF NOT EXISTS massive_hourly_derived (
            ticker     TEXT NOT NULL,
            build_id   INTEGER NOT NULL,
            ts         TEXT NOT NULL,
            open       REAL NOT NULL,
            high       REAL NOT NULL,
            low        REAL NOT NULL,
            close      REAL NOT NULL,
            volume     REAL,
            corrected  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (ticker, build_id, ts)
        )
    """),
    ("massive_hourly_corrections", """
        CREATE TABLE IF NOT EXISTS massive_hourly_corrections (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT NOT NULL,
            build_id    INTEGER NOT NULL,
            ts          TEXT NOT NULL,
            field       TEXT NOT NULL,
            raw_value   REAL NOT NULL,
            new_value   REAL NOT NULL,
            reason      TEXT NOT NULL,
            detected_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """),
    ("massive_hourly_derived_builds", """
        CREATE TABLE IF NOT EXISTS massive_hourly_derived_builds (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker                TEXT NOT NULL,
            label                 TEXT NOT NULL,
            built_at              TEXT NOT NULL DEFAULT (datetime('now')),
            raw_data_pulled_at    TEXT,
            raw_data_start        TEXT,
            raw_data_end          TEXT,
            dividend_data_asof    TEXT,
            row_count             INTEGER,
            correction_count      INTEGER
        )
    """),
    ("massive_minute_derived", """
        CREATE TABLE IF NOT EXISTS massive_minute_derived (
            ticker     TEXT NOT NULL,
            build_id   INTEGER NOT NULL,
            ts         TEXT NOT NULL,
            open       REAL NOT NULL,
            high       REAL NOT NULL,
            low        REAL NOT NULL,
            close      REAL NOT NULL,
            PRIMARY KEY (ticker, build_id, ts)
        )
    """),
    ("massive_minute_derived_builds", """
        CREATE TABLE IF NOT EXISTS massive_minute_derived_builds (
            id                    INTEGER PRIMARY KEY,
            ticker                TEXT NOT NULL,
            label                 TEXT NOT NULL,
            built_at              TEXT NOT NULL DEFAULT (datetime('now')),
            raw_data_pulled_at    TEXT,
            raw_data_start        TEXT,
            raw_data_end          TEXT,
            dividend_data_asof    TEXT,
            row_count             INTEGER,
            correction_count      INTEGER
        )
    """),
    ("massive_second_derived", """
        CREATE TABLE IF NOT EXISTS massive_second_derived (
            ticker     TEXT NOT NULL,
            build_id   INTEGER NOT NULL,
            ts         TEXT NOT NULL,
            open       REAL NOT NULL,
            high       REAL NOT NULL,
            low        REAL NOT NULL,
            close      REAL NOT NULL,
            volume     REAL,
            corrected  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (ticker, build_id, ts)
        )
    """),
    ("massive_second_derived_builds", """
        CREATE TABLE IF NOT EXISTS massive_second_derived_builds (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker                TEXT NOT NULL,
            label                 TEXT NOT NULL,
            built_at              TEXT NOT NULL DEFAULT (datetime('now')),
            raw_data_pulled_at    TEXT,
            raw_data_start        TEXT,
            raw_data_end          TEXT,
            dividend_data_asof    TEXT,
            row_count             INTEGER,
            correction_count      INTEGER
        )
    """),
    ("active_builds", """
        CREATE TABLE IF NOT EXISTS active_builds (
            ticker      TEXT NOT NULL,
            table_name  TEXT NOT NULL CHECK (table_name IN ('hourly', 'minute', 'second')),
            build_id    INTEGER NOT NULL,
            promoted_at TEXT NOT NULL DEFAULT (datetime('now')),
            note        TEXT,
            PRIMARY KEY (ticker, table_name)
        )
    """),
]

NUMERIC_CHECKSUM_COL = {
    "massive_dividends_raw": "historical_adjustment_factor",
    "massive_hourly_derived": "close",
    "massive_hourly_corrections": "new_value",
    "massive_hourly_derived_builds": "row_count",
    "massive_minute_derived": "close",
    "massive_minute_derived_builds": "row_count",
    "massive_second_derived": "close",
    "massive_second_derived_builds": "row_count",
    "active_builds": "build_id",
}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def do_copy(src_db, dst_db):
    src = sqlite3.connect(src_db)
    src.execute("ATTACH DATABASE ? AS dst", (dst_db,))
    for name, create_sql in TABLES:
        t0 = time.time()
        src.execute(create_sql.replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE IF NOT EXISTS dst."))
        n_before = src.execute(f"SELECT COUNT(*) FROM dst.{name}").fetchone()[0]
        src.execute(f"INSERT OR IGNORE INTO dst.{name} SELECT * FROM main.{name}")
        src.commit()
        n_after = src.execute(f"SELECT COUNT(*) FROM dst.{name}").fetchone()[0]
        log(f"copied {name}: {n_before} -> {n_after} rows ({time.time()-t0:.1f}s)")
    src.close()
    log("copy phase done")


def do_verify(src_db, dst_db):
    src = sqlite3.connect(src_db)
    dst = sqlite3.connect(dst_db)
    all_ok = True
    for name, _ in TABLES:
        t0 = time.time()
        n_src = src.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        n_dst = dst.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        col = NUMERIC_CHECKSUM_COL[name]
        sum_src = src.execute(f"SELECT COALESCE(SUM({col}), 0) FROM {name}").fetchone()[0]
        sum_dst = dst.execute(f"SELECT COALESCE(SUM({col}), 0) FROM {name}").fetchone()[0]
        ok = (n_src == n_dst) and (abs(sum_src - sum_dst) < 1e-6)
        all_ok = all_ok and ok
        log(f"verify {name}: src={n_src} dst={n_dst} sum_src={sum_src} sum_dst={sum_dst} "
            f"{'OK' if ok else 'MISMATCH'} ({time.time()-t0:.1f}s)")
    src.close()
    dst.close()
    if not all_ok:
        log("VERIFICATION FAILED -- not safe to drop originals")
        sys.exit(1)
    log("verification passed for all tables")


def do_drop_and_vacuum(src_db):
    src = sqlite3.connect(src_db)
    for name, _ in TABLES:
        src.execute(f"DROP TABLE IF EXISTS {name}")
        log(f"dropped {name} from {src_db}")
    src.commit()
    log("running VACUUM (this can take a while)...")
    t0 = time.time()
    src.execute("VACUUM")
    log(f"VACUUM done ({time.time()-t0:.1f}s)")
    src.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                    help="repo root containing cache/research/*.db")
    p.add_argument("--copy", action="store_true")
    p.add_argument("--verify", action="store_true")
    p.add_argument("--drop-and-vacuum", action="store_true")
    args = p.parse_args()
    root = Path(args.root)
    src_db = str(root / "cache" / "research" / "trading_universe.db")
    dst_db = str(root / "cache" / "research" / "tickdata.db")
    if args.copy:
        do_copy(src_db, dst_db)
    elif args.verify:
        do_verify(src_db, dst_db)
    elif args.drop_and_vacuum:
        do_drop_and_vacuum(src_db)
    else:
        p.error("pass one of --copy / --verify / --drop-and-vacuum")
