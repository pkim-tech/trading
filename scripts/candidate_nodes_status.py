"""candidate_nodes coverage/provenance status -- new, 2026-08-29 (Task #6 piece #2,
planner dispatch, SOXL Campaign B planning session).

Real gap this closes: nobody could tell, from `candidate_nodes` alone, which
(strategy, fixed_sl) x window combinations have real promoted candidates for a
ticker/version, or whether a given window's candidates came from a JOINT run
(multiple windows swept together in one invocation, e.g. `bench_phase1_phase2_
inmemory.py`'s own WINDOWS=[10,20] default) or an ISOLATED single-window run
(`--window N` override). This distinction is NOT cosmetic: `pick_island_centers`
pools across whatever windows were actually present in the run when ranking
candidates, so an isolated single-window run and a joint multi-window run that
happens to include that same window value can produce genuinely different top-9
picks for that window -- the isolated run's candidates aren't directly comparable
to a joint run's candidates at the "same" window.

Joint-run detection: candidate_nodes rows written by ONE real invocation of
`_insert_candidate_nodes_rows` all share the exact same `created_at` timestamp
(that function stamps `now_iso` once per call, reused for the whole buffer -- see
its own docstring). So for a (strategy, fixed_sl) combo, if two or more DISTINCT
window values' rows share one identical created_at, those windows were promoted
together in one run (joint); if each window's rows have their own distinct
created_at, they came from separate invocations (isolated), even if they
happen to share a version string.

Usage:
  .venv/bin/python scripts/candidate_nodes_status.py --ticker SOXL
  .venv/bin/python scripts/candidate_nodes_status.py --ticker SOXL --version bench-inmemory-v6-massive-w2021-08-23_2026-08-21
  .venv/bin/python scripts/candidate_nodes_status.py --ticker SOXL --windows 10 15 20 --fixed-sls 1 2 3
"""
import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from run_optimization_sweep import DB_PATH


def _fetch(conn, ticker, version):
    rows = conn.execute("""
        SELECT strategy, fixed_sl, window, created_at, COUNT(*) AS n
        FROM candidate_nodes
        WHERE ticker=? AND version=?
        GROUP BY strategy, fixed_sl, window, created_at
        ORDER BY strategy, fixed_sl, window, created_at
    """, (ticker, version)).fetchall()
    return rows


def print_version_report(conn, ticker, version, windows_override, fixed_sls_override):
    rows = _fetch(conn, ticker, version)
    if not rows:
        print(f"  no candidate_nodes rows for ticker={ticker} version={version!r}")
        return

    windows = sorted(windows_override) if windows_override else sorted({r[2] for r in rows})
    fixed_sls = sorted(fixed_sls_override) if fixed_sls_override else sorted({r[1] for r in rows})
    strategies = sorted({r[0] for r in rows})

    # (strategy, fixed_sl, window) -> total row count (collapsed across any created_at
    # groups -- the coverage table doesn't care how many runs contributed, only
    # whether real candidates exist there at all).
    counts = {}
    # (strategy, fixed_sl) -> {created_at: set(windows)} for the joint-run check below.
    by_created_at = {}
    for strategy, fixed_sl, window, created_at, n in rows:
        counts[(strategy, fixed_sl, window)] = counts.get((strategy, fixed_sl, window), 0) + n
        by_created_at.setdefault((strategy, fixed_sl), {}).setdefault(created_at, set()).add(window)

    print(f"\n  Coverage table (rows = strategy/fixed_sl, columns = window; cell = "
          f"candidate_nodes row count, '-' = absent):")
    header = "    " + f"{'strategy':<28}{'fixed_sl':>9}  " + "".join(f"w={w:<6}" for w in windows)
    print(header)
    for strategy in strategies:
        for fixed_sl in fixed_sls:
            if not any((strategy, fixed_sl, w) in counts for w in windows):
                continue
            cells = "".join(
                f"{counts.get((strategy, fixed_sl, w), '-'):<8}" for w in windows)
            print(f"    {strategy:<28}{fixed_sl:>9}  {cells}")

    print(f"\n  Joint-run check:")
    any_combo = False
    for (strategy, fixed_sl), created_at_map in sorted(by_created_at.items()):
        populated_windows = sorted({w for ws in created_at_map.values() for w in ws})
        if len(populated_windows) < 2:
            continue
        any_combo = True
        joint_groups = [(ca, sorted(ws)) for ca, ws in created_at_map.items() if len(ws) > 1]
        if joint_groups:
            for ca, ws in joint_groups:
                print(f"    {strategy} fixed_sl={fixed_sl}: JOINT run at {ca} -- windows {ws}")
            isolated = [w for w in populated_windows if not any(w in ws for _, ws in joint_groups)]
            if isolated:
                print(f"    {strategy} fixed_sl={fixed_sl}: ISOLATED windows (separate runs, "
                      f"NOT directly comparable to the joint group above) -- {isolated}")
        else:
            print(f"    {strategy} fixed_sl={fixed_sl}: ISOLATED runs only (every window from "
                  f"a separate invocation) -- windows {populated_windows}, "
                  f"NOT directly comparable to each other")
    if not any_combo:
        print("    (every (strategy, fixed_sl) combo here has at most one populated window "
              "-- nothing to compare)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--version", default=None, help="omit to check every real version found for this ticker")
    ap.add_argument("--windows", type=int, nargs="+", default=None,
                     help="restrict the coverage table's columns to these windows -- default: "
                          "auto-detected from the real data")
    ap.add_argument("--fixed-sls", type=float, nargs="+", default=None,
                     help="restrict the coverage table's rows to these fixed_sl values -- "
                          "default: auto-detected from the real data")
    args = ap.parse_args()

    with sqlite3.connect(DB_PATH) as conn:
        if args.version is not None:
            versions = [args.version]
        else:
            versions = sorted({r[0] for r in conn.execute(
                "SELECT DISTINCT version FROM candidate_nodes WHERE ticker=?", (args.ticker,))})
        if not versions:
            raise SystemExit(f"No candidate_nodes rows for ticker={args.ticker}.")

        for version in versions:
            print(f"\n{'='*100}\n{args.ticker} / version={version}\n{'='*100}")
            print_version_report(conn, args.ticker, version, args.windows, args.fixed_sls)


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
