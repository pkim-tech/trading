"""Phase4 add-on overlay trade persistence -- backtest_overlay_trades, the sibling
table to backtest_winner_trades named in docs/plans/backtest_schema_v2_phase_tables.md
("Phase4 gets its own sibling, backtest_overlay_trades... partitioned into separate
results per overlay kind"). Scoped to 'addon' only for now -- apply_addon_overlay_
ground_truth returns a real per-trade list directly, so this is pure post-processing
over trades ALREADY persisted in backtest_winner_trades (no re-simulation, no data
reload). Drought is NOT covered here: simulate_drought_overlay_ground_truth only
returns an aggregate summary dict (best_confirm_days/drought_compounded_pct/etc), not
a per-trade drought-window list -- persisting real drought trades would need the
lower-level find_drought_windows/simulate_overlay calls it wraps, a separate build.

Usage: .venv/bin/python scripts/persist_addon_overlay_trades.py --ticker SOXL \\
    --strategy TrailingBothZScoreBreakout --version <version> [--fixed-sl N]

Staleness-aware read (2026-08-29, round-3 paired-review parity fix -- same pattern
already applied to bench_phase1_phase2_inmemory.py's writer and scripts/rebuild_
winner_trades.py's writer/reader): reads its core trade list via the SAME shared,
hardened candidate_verification_store.get_cached_trades Phase4/Phase5's own core-
trade readers use, instead of a third independently-duplicated raw SELECT + manual
dict-reconstruction over backtest_winner_trades. get_cached_trades' trade-dict shape
(Ticker/Entry Time/Entry Price/Exit Time/Exit Price/exit_reason/Return/armed/Arm
Time/Arm Price) is a strict SUPERSET of what apply_addon_overlay_ground_truth reads
(Entry Price/Exit Price/Return/armed/Arm Price only -- confirmed against that
function's own body) -- no shape mismatch, no adapter needed. This also gives this
script get_cached_trades' real staleness guard (kernel_version/hourly_build_id/
minute_build_id must all match) and gap-detection for free -- a node_key whose
backtest_winner_trades rows are stale or gapped is now correctly SKIPPED (with a
loud message) rather than silently overlaying garbage core trades.
"""
import argparse
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from run_optimization_sweep import DB_PATH, TRADES_DB_PATH
from backtester import apply_addon_overlay_ground_truth
from node_key import GT_TRADES_KERNEL_VERSION
from candidate_verification_store import get_cached_trades
import db_cache


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--fixed-sl", dest="fixed_sl", type=float, default=None)
    args = ap.parse_args()

    query = "SELECT DISTINCT node_key, fixed_sl FROM backtest_winner_trades WHERE version=? AND ticker=? AND strategy=?"
    params = [args.version, args.ticker, args.strategy]
    if args.fixed_sl is not None:
        query += " AND fixed_sl=?"
        params.append(args.fixed_sl)
    # Two separate connections (2026-09-13 trades_cache.db split): backtest_winner_trades
    # (read-only here, via trades_conn) now lives in TRADES_DB_PATH; backtest_overlay_trades
    # (this script's own write target, via conn) stays in DB_PATH -- these are two
    # different sqlite files, no single connection/transaction can span both.
    with sqlite3.connect(TRADES_DB_PATH, timeout=60.0) as trades_conn:
        node_keys = trades_conn.execute(query, params).fetchall()
    if not node_keys:
        raise SystemExit(f"No backtest_winner_trades rows for ticker={args.ticker} "
                          f"strategy={args.strategy} version={args.version} "
                          f"fixed_sl={args.fixed_sl} -- nothing to overlay.")
    print(f"Found {len(node_keys)} distinct node_keys to add-on-overlay.")

    with sqlite3.connect(TRADES_DB_PATH, timeout=60.0) as trades_conn, \
            sqlite3.connect(DB_PATH, timeout=60.0) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_overlay_trades (
                node_key TEXT, version TEXT, ticker TEXT, strategy TEXT, fixed_sl REAL,
                overlay_kind TEXT, trade_idx INTEGER, entry_time TEXT, entry_price REAL,
                exit_time TEXT, exit_price REAL, exit_reason TEXT, return_pct REAL,
                return_core_pct REAL, addon_applied INTEGER, return_below_floor INTEGER,
                created_at TEXT,
                UNIQUE(node_key, version, overlay_kind, trade_idx)
            )""")
        # sqlite has no "ADD COLUMN IF NOT EXISTS" (pre-3.35) -- probe-first pattern,
        # same convention every other real writer in this codebase now uses (2026-08-29
        # staleness-invalidation fix, applied here for consistency/future-proofing --
        # nothing reads these back with a staleness check today, but stamping them at
        # write time means that's possible later without a backfill).
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(backtest_overlay_trades)")}
        for col in ("kernel_version TEXT", "hourly_build_id INTEGER", "minute_build_id INTEGER"):
            name = col.split()[0]
            if name not in existing_cols:
                conn.execute(f"ALTER TABLE backtest_overlay_trades ADD COLUMN {col}")

        hourly_build_id = db_cache.get_active_build_id(args.ticker, 'hourly')
        minute_build_id = db_cache.get_active_build_id(args.ticker, 'minute')

        buffer = []
        node_keys_written = []
        n_skipped_stale = 0
        for node_key, fixed_sl in node_keys:
            trades = get_cached_trades(
                trades_conn, node_key, args.version, ticker=args.ticker,
                kernel_version=GT_TRADES_KERNEL_VERSION,
                hourly_build_id=hourly_build_id, minute_build_id=minute_build_id)
            if trades is None:
                # Stale, gapped, or genuinely missing (already-real 100% coverage by
                # construction here -- this script only ever queries node_keys that
                # JUST came back from backtest_winner_trades itself, so a None here
                # means the staleness/gap guard tripped, not a real absence) -- skip,
                # don't overlay garbage core trades. No re-simulation fallback exists
                # in this script (see module docstring: pure post-processing, no
                # kernel/data reload) -- an operator needs to re-run the real writer
                # (bench_phase1_phase2_inmemory.py / scripts/rebuild_winner_trades.py)
                # for this node_key first.
                n_skipped_stale += 1
                print(f"  SKIPPED node_key={node_key[:16]}... -- get_cached_trades "
                      f"returned None (stale/gapped backtest_winner_trades rows for "
                      f"this node_key/version -- re-run the real writer first).")
                continue
            blended = apply_addon_overlay_ground_truth(trades)
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
            node_keys_written.append(node_key)
            for i, (core, b) in enumerate(zip(trades, blended)):
                buffer.append((node_key, args.version, args.ticker, args.strategy, fixed_sl,
                               "addon", i, str(core['Entry Time']), core['Entry Price'],
                               str(core['Exit Time']), core['Exit Price'], core['exit_reason'],
                               b["Return"], b["Return_core"], int(b["addon_applied"]),
                               int(b["return_below_floor"]), now_iso,
                               GT_TRADES_KERNEL_VERSION, hourly_build_id, minute_build_id))

        # DELETE-then-INSERT, not INSERT OR IGNORE (2026-08-29, round-3 paired-review
        # parity fix -- same real gap/fix as backtest_winner_trades' own writers: a
        # re-run over an already-populated (node_key, version, overlay_kind) would
        # otherwise silently keep old (possibly stale-relative-to-the-now-corrected-
        # core-trades) overlay rows and drop every freshly-computed replacement.
        # Delete each distinct node_key's own 'addon' row set for this version first,
        # so a re-run is a genuine full replacement, never a partial merge.
        n_deleted = 0
        for nk in sorted(set(node_keys_written)):
            cur = conn.execute(
                "DELETE FROM backtest_overlay_trades WHERE node_key=? AND version=? AND overlay_kind='addon'",
                (nk, args.version))
            n_deleted += cur.rowcount
        if n_deleted:
            print(f"  (deleted {n_deleted} pre-existing addon-overlay trade row(s) across "
                  f"{len(set(node_keys_written))} node_key(s) about to be rewritten)")

        conn.executemany("""
            INSERT INTO backtest_overlay_trades
                (node_key, version, ticker, strategy, fixed_sl, overlay_kind, trade_idx,
                 entry_time, entry_price, exit_time, exit_price, exit_reason, return_pct,
                 return_core_pct, addon_applied, return_below_floor, created_at,
                 kernel_version, hourly_build_id, minute_build_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, buffer)
        conn.commit()
    print(f"Written to backtest_overlay_trades: {len(buffer)} rows across "
          f"{len(set(node_keys_written))} node_key(s) "
          f"({n_skipped_stale} node_key(s) skipped -- stale/gapped core trades).")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
