"""
One-command status check across everything currently in flight: daemon
health, config invariants, unexplained coverage deviations, recent
blocked/failed events, and per-ticker real broker + DB state for every
mode='live' node plus any node (any mode) with a currently open paper
position, on the active watchlist.

Built 2026-07-29 so a full picture doesn't have to be re-derived by hand
across daemon_status.py/signals_invariants.py/coverage queries/
audit_live_test_candidates.py separately each time -- exactly what the
2026-07-28 night session did manually, repeatedly, across many messages.

Usage:
  .venv/bin/python scripts/status_check.py
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db
import signals_invariants
from scripts.audit_live_test_candidates import audit_one


def _section(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _daemon_status():
    _section("DAEMON")
    r = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "daemon_status.py")],
        capture_output=True, text=True,
    )
    print((r.stdout + r.stderr).strip())


def _invariants():
    _section("INVARIANTS")
    violations = signals_invariants.run_all()
    if violations:
        print(f"{len(violations)} violation(s):")
        for v in violations:
            print(f"  - {v}")
    else:
        print("All invariants hold.")


def _unexplained_deviations():
    _section("UNEXPLAINED COVERAGE DEVIATIONS")
    rows = db.get_deviations(unexplained_only=True)
    if not rows:
        print("None.")
        return
    for r in rows:
        print(f"  [{r['id']}] {r['check_date']} {r['scenario_key']} ({r['ticker']}): {r['actual_summary']}")


def _recent_bad_events(hours=24):
    _section(f"BLOCKED/FAILED coverage_events, last {hours}h")
    with db._conn() as c:
        rows = c.execute(
            "SELECT ticker, scenario_key, result, count(*) c, max(ts) last_ts FROM coverage_events "
            "WHERE ts >= datetime('now', ?) AND result IN "
            "('blocked','failed','failed_unexpectedly','rejected') "
            "GROUP BY ticker, scenario_key, result ORDER BY c DESC",
            (f'-{hours} hours',),
        ).fetchall()
    if not rows:
        print("None.")
        return
    for r in rows:
        print(f"  {r['ticker'] or '(none)':8s} {r['scenario_key']:28s} {r['result']:10s} "
              f"{r['c']}x  last={r['last_ts']}")


def _tickers_worth_checking():
    """Every distinct ticker worth a status check: mode='live' nodes (real
    broker state matters) PLUS any node with a currently open paper
    position, even if mode='research' (SOXL/HIBL/KORU/USD/YANG hold real
    paper trades right now and were invisible to an earlier, live-only
    version of this filter -- same blind-spot shape as the Morning Report
    bug). Being in this list does not mean mode='live' -- audit_one's own
    output shows the real mode/account per ticker; don't infer it from
    inclusion here. Not hardcoded to any particular night's list, so this
    stays useful as scope changes.

    PLUS any node with a resting real or paper pending buy (bounce-fill wait
    phase) and no open position yet -- the pending-buy-invisible-to-"flat"
    fix landed in audit_one's classification (2026-08-05), but a pending-
    buy-only node with mode='research' and no open position was never
    reachable through this selector at all (found by paired Opus review,
    2026-08-05)."""
    watchlist = db.get_watchlist()
    tickers = {n['ticker'] for n in watchlist if n['state'] != 'paper'}
    tickers |= {p['ticker'] for p in db.get_open_positions(paper=True)}
    tickers |= {p['ticker'] for p in db.get_pending_buys()}
    tickers |= {p['ticker'] for p in db.get_paper_pending_buys()}
    return sorted(tickers)


def main():
    _daemon_status()
    _invariants()
    _unexplained_deviations()
    _recent_bad_events()
    _section("PER-TICKER STATE (live-mode nodes + any open paper position)")
    for ticker in _tickers_worth_checking():
        audit_one(ticker)


if __name__ == "__main__":
    main()
