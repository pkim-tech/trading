"""WAL-safe backup of cache/live/trading_live.db for the hourly cron jobs.

Replaces a plain `cp` (2026-09-17, after signals_db._conn() switched the live
DB to WAL mode as a prerequisite for active_signals.py's parallel housekeeping
tail). A plain file copy of just the main .db file can silently miss any
commit not yet checkpointed out of the -wal sidecar, and can produce a torn
file if it races an in-progress checkpoint. sqlite3.Connection.backup() is the
same mechanism the sqlite3 CLI's ".backup" dot-command uses -- it reads
through SQLite's own page-consistent backup API regardless of journal mode,
so the destination is always a complete, self-contained single-file snapshot.
Written as a script (not inlined in crontab) because the sqlite3 CLI binary
isn't installed on this host -- only libsqlite3 (which Python's sqlite3
module already links against) is.

Usage: .venv/bin/python3 scripts/backup_live_db.py DEST_PATH
"""
import sqlite3
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "cache" / "live" / "trading_live.db"


def main():
    if len(sys.argv) != 2:
        print("usage: backup_live_db.py DEST_PATH", file=sys.stderr)
        sys.exit(1)
    dest = Path(sys.argv[1])
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    dest_conn = sqlite3.connect(dest)
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
