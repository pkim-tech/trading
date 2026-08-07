"""Finds and fixes (ticker, version) combos where a real archive/snapshot has
more data than the live DB currently does -- built 2026-08-07 after a
ticker-only diff (comparing "is this ticker new" instead of "is this exact
ticker+version combo present") silently dropped 4 tickers' entire real v5
sweep data (SQQQ/SOXS/UCO/QLD, ~3.4M rows total) during a same-day recovery
merge. The bug: those 4 tickers already existed in an older archive (with
only legacy v3.x data), so a ticker-level "is this ticker already covered"
check said yes and never looked at whether a NEWER version's data for that
same ticker existed in a more recent snapshot.

This tool compares at the real granularity that matters -- (ticker, version)
-- against every source snapshot, not just "new ticker or not." Detects
both complete loss (live has 0 rows, source has real data) and partial loss
(live has fewer rows than a source, worth a closer look though not
automatically assumed to be a bug -- a live count could legitimately be
higher due to newer sweep work, or lower due to a correct island-only prune
already having run on it).

Usage:
  .venv/bin/python scripts/reconcile_ticker_version_gaps.py --check-only
  .venv/bin/python scripts/reconcile_ticker_version_gaps.py --check-only --sources SNAP1 SNAP2 ...
  .venv/bin/python scripts/reconcile_ticker_version_gaps.py --fix --sources SNAP1 SNAP2 ...
    (--fix inserts ONLY complete-loss combos (live=0) by default -- partial
    mismatches are reported but never auto-inserted, since "live is lower
    because it was already correctly pruned" is a normal, expected state,
    not a bug; a human should look at each partial-mismatch case.)
"""
import argparse
import sqlite3
from pathlib import Path

LIVE_DB = Path("cache/research/trading_universe.db")
DEFAULT_SOURCES = [
    "cache/research/trading_universe.db.pre_prune_20260807_042703",
    "cache/research/trading_universe.db.pre_prune_20260807_075953",
]


def counts(db_path):
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    c = conn.cursor()
    c.execute("SELECT ticker, version, COUNT(*) FROM backtest_cache WHERE trades>0 GROUP BY ticker, version")
    d = {(t, v): n for t, v, n in c.fetchall()}
    conn.close()
    return d


def find_gaps(live_counts, source_path):
    src_counts = counts(source_path)
    complete_loss = []  # live has 0, source has real data
    partial_mismatch = []  # live has some but fewer than source
    for (t, v), n in src_counts.items():
        if n == 0:
            continue
        live_n = live_counts.get((t, v), 0)
        if live_n == 0:
            complete_loss.append((t, v, n))
        elif live_n < n:
            partial_mismatch.append((t, v, live_n, n))
    return complete_loss, partial_mismatch


def cmd_check(sources):
    live_counts = counts(LIVE_DB)
    print(f"Live DB: {sum(live_counts.values()):,} rows (trades>0) across {len(live_counts)} (ticker,version) combos.\n")
    all_complete_loss = {}
    for src in sources:
        if not Path(src).exists():
            print(f"SKIP (missing): {src}")
            continue
        complete_loss, partial = find_gaps(live_counts, src)
        print(f"=== {src} ===")
        print(f"  Complete loss (live=0, source has real data): {len(complete_loss)}")
        for t, v, n in sorted(complete_loss, key=lambda x: -x[2]):
            print(f"    {t:8} {v:8} {n:>12,} rows in source, 0 in live")
            key = (t, v)
            if key not in all_complete_loss or n > all_complete_loss[key][1]:
                all_complete_loss[key] = (src, n)
        print(f"  Partial mismatch (live < source, needs human review, NOT auto-fixed): {len(partial)}")
        for t, v, live_n, n in sorted(partial, key=lambda x: -(x[3] - x[2]))[:20]:
            print(f"    {t:8} {v:8} live={live_n:>10,} source={n:>10,} (source has {n - live_n:,} more)")
        print()
    return all_complete_loss


def cmd_fix(sources):
    all_complete_loss = cmd_check(sources)
    if not all_complete_loss:
        print("Nothing to fix -- no complete-loss combos found.")
        return
    print(f"\n--- Fixing {len(all_complete_loss)} complete-loss combo(s) ---")
    conn = sqlite3.connect(str(LIVE_DB), timeout=60.0)
    for (t, v), (src, n) in all_complete_loss.items():
        conn.execute(f"ATTACH DATABASE '{src}' AS src")
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM src.backtest_cache WHERE ticker=? AND version=?", (t, v))
        src_count = c.fetchone()[0]
        conn.execute("INSERT INTO backtest_cache SELECT * FROM src.backtest_cache WHERE ticker=? AND version=?", (t, v))
        conn.commit()
        c.execute("SELECT COUNT(*) FROM backtest_cache WHERE ticker=? AND version=?", (t, v))
        after = c.fetchone()[0]
        print(f"  {t:8} {v:8}: inserted {after:,} rows from {src} (source had {src_count:,} total incl. trades=0)")
        conn.execute("DETACH DATABASE src")
    conn.close()
    print("\nDone. This is FULL, unpruned data for these combos -- if the live DB is")
    print("otherwise island-only pruned, re-run the prune (scripts/prune_backtest_cache.py")
    print("via scripts/full_db_prune_validate.py) before trusting the DB as consistently pruned.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", nargs="+", default=DEFAULT_SOURCES)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check-only", action="store_true")
    g.add_argument("--fix", action="store_true")
    args = ap.parse_args()
    if args.check_only:
        cmd_check(args.sources)
    else:
        cmd_fix(args.sources)


if __name__ == "__main__":
    main()
