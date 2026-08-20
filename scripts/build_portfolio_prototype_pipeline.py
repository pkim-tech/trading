"""Regenerates the portfolio-construction prototype workbook end-to-end, chaining the
two existing steps that previously had to be run and wired together by hand
(docs/portfolio_construction_notes.md's walkthrough): candidate_full_review.py -->
build_portfolio_prototype.py. Built 2026-08-19 after confirming 6 of the 10 real live
tickers' picks were made off a stale ~1.11yr sweep (v5) while a full ~2.9yr resweep
(v5.1) already existed uninspected -- this makes "get a current prototype file"
a single command instead of a two-step manual process with a hardcoded stale
source file (build_portfolio_prototype.py's own default SRC points at a specific
2026-08-13 snapshot).

Default ticker universe: distinct tickers in watch_list_candidate_link (the real,
explicit node->candidate link table, not a hardcoded list) -- exactly the 10 real
live tickers as of 2026-08-19 (AGQ/DFEN/DPST/ETHU/GDXU/JNUG/KORU/NUGT/SOXL/SOXS).
Override with --tickers to scope to a subset (e.g. just the 6 tickers whose v5.1
data postdates their original pick), or pass --universe to widen to every
liquidity-screened candidate on file (candidate_nodes' full ticker list, minus
sweep_tranches' real disqualifications -- concentration/diversification/weak-CAGR
removals -- and USO, excluded per CLAUDE.md's 2026-08-04 standing K-1/UBTI decision,
distinct from the general K1-restricts-to-brokerage policy the rest of the universe
gets tagged with, not excluded from). Built 2026-08-19 per the user's explicit call
("we might end up picking a ticker not in the top 10 / not in the current watchlist")
-- portfolio construction shouldn't be scoped to only the tickers already promoted.

Each step's own auto-resolution (candidate_full_review.py resolves v5 vs v5.1 per
ticker, picking whichever has data) means this always reflects whatever the most
recent COMPLETED sweep is -- it does not launch or wait on any new sweep itself.

Usage: .venv/bin/python scripts/build_portfolio_prototype_pipeline.py [--tickers T ...]
"""
import argparse
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LIVE_DB = "cache/live/trading_live.db"
PYTHON = ".venv/bin/python"


RESEARCH_DB = "cache/research/trading_universe.db"

# sweep_tranches rows with active=0 whose removal was a data-management artifact,
# not a real disqualification (DFEN was moved to its own tranche for a targeted
# resweep after a bad-tick fix -- it's one of the 10 real live tickers, not excluded).
BOOKKEEPING_ONLY_REMOVALS = {"DFEN"}
# CLAUDE.md 2026-08-04: confirmed K-1 oil futures commodity pool, real UBTI exposure
# in IRA/Roth -- "candidacy is dead on this basis," a harder exclusion than the
# general K1-restricts-to-brokerage policy the rest of the universe gets tagged with.
HARD_EXCLUDED = {"USO"}


def default_tickers():
    conn = sqlite3.connect(LIVE_DB)
    conn.row_factory = sqlite3.Row
    wl_ids = [r["wl_id"] for r in conn.execute("SELECT DISTINCT wl_id FROM watch_list_candidate_link")]
    if not wl_ids:
        conn.close()
        return []
    placeholders = ",".join("?" * len(wl_ids))
    rows = conn.execute(
        f"SELECT DISTINCT ticker FROM watch_list WHERE id IN ({placeholders})", wl_ids
    ).fetchall()
    conn.close()
    return sorted(r["ticker"] for r in rows)


def universe_tickers():
    """Every liquidity-screened candidate on file (candidate_nodes' full ticker
    list), minus real sweep_tranches disqualifications and USO."""
    conn = sqlite3.connect(RESEARCH_DB)
    all_tickers = {r[0] for r in conn.execute("SELECT DISTINCT ticker FROM candidate_nodes")}
    removed = {r[0] for r in conn.execute("SELECT ticker FROM sweep_tranches WHERE active=0")}
    conn.close()
    excluded = (removed - BOOKKEEPING_ONLY_REMOVALS) | HARD_EXCLUDED
    return sorted(all_tickers - excluded)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*", default=None,
                     help="defaults to every ticker in watch_list_candidate_link (the real live set)")
    ap.add_argument("--universe", action="store_true",
                     help="widen to every liquidity-screened candidate on file instead of just the real live set")
    ap.add_argument("--base-name", default="portfolio_prototype",
                     help="base name for the raw candidate_full_review.py xlsx (timestamp auto-appended)")
    args = ap.parse_args()

    if args.tickers:
        tickers = args.tickers
    elif args.universe:
        tickers = universe_tickers()
    else:
        tickers = default_tickers()
    if not tickers:
        print("No tickers resolved (empty watch_list_candidate_link and none passed via --tickers)", file=sys.stderr)
        sys.exit(1)
    print(f"Tickers: {' '.join(tickers)}", file=sys.stderr)

    raw_result = subprocess.run(
        [PYTHON, "scripts/candidate_full_review.py", *tickers, "--xlsx", args.base_name],
        capture_output=True, text=True,
    )
    print(raw_result.stdout)
    if raw_result.returncode != 0:
        print(raw_result.stderr, file=sys.stderr)
        sys.exit(raw_result.returncode)

    m = re.search(r"Wrote (output/\S+\.xlsx)", raw_result.stdout)
    if not m:
        print("Could not find the xlsx path in candidate_full_review.py's output", file=sys.stderr)
        sys.exit(1)
    raw_path = m.group(1)

    dst_path = raw_path.replace(".xlsx", "_built.xlsx")
    build_result = subprocess.run(
        [PYTHON, "scripts/build_portfolio_prototype.py", raw_path, dst_path],
        capture_output=True, text=True,
    )
    print(build_result.stdout)
    if build_result.returncode != 0:
        print(build_result.stderr, file=sys.stderr)
        sys.exit(build_result.returncode)

    print(f"\nDone. Raw report: {raw_path}\nPortfolio prototype: {dst_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
