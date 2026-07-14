"""Check whether active_signals.py is running, and whether it's stale.

Usage:
  python scripts/daemon_status.py

Reports: process running y/n (with PID/start time), and whether the
process start time is older than the newest mtime among the live-trading
source files -- if so, the running daemon predates recent edits and needs
a restart to pick them up.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE_SOURCE_FILES = [
    "active_signals.py",
    "signals_config.py",
    "signals_db.py",
    "signals_compute.py",
    "signals_notify.py",
    "strategies.py",
    "backtester.py",
]


def _find_daemon_pid():
    out = subprocess.run(
        ["pgrep", "-f", "active_signals.py run"], capture_output=True, text=True
    ).stdout.strip()
    pids = [p for p in out.splitlines() if p]
    return pids[0] if pids else None


def main():
    pid = _find_daemon_pid()
    if not pid:
        print("active_signals.py: NOT RUNNING")
        sys.exit(1)

    start_epoch = int(
        subprocess.run(
            ["stat", "-c", "%Y", f"/proc/{pid}"], capture_output=True, text=True
        ).stdout.strip()
    )

    newest_path, newest_mtime = None, 0
    for name in LIVE_SOURCE_FILES:
        p = ROOT / name
        if not p.exists():
            continue
        mtime = p.stat().st_mtime
        if mtime > newest_mtime:
            newest_path, newest_mtime = name, mtime

    print(f"active_signals.py: RUNNING (pid {pid})")
    print(f"  process start:  {subprocess.run(['date', '-d', f'@{start_epoch}'], capture_output=True, text=True).stdout.strip()}")
    newest_date = subprocess.run(
        ["date", "-d", f"@{int(newest_mtime)}"], capture_output=True, text=True
    ).stdout.strip()
    print(f"  newest source:  {newest_path} @ {newest_date}")

    if newest_mtime > start_epoch:
        print("  STALE: daemon predates the newest live-trading source edit -- restart to pick up changes.")
    else:
        print("  current: daemon started after all live-trading source edits.")


if __name__ == "__main__":
    main()
