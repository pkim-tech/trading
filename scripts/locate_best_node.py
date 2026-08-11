"""Locates each ticker's single best (max robust-alpha) backtest_cache row.

Two uses:
1. CLI: a fast correctness smoke test for prune_backtest_cache.py -- run
   before and after a prune pass (via run_liquidity_tranches.sh) and diff
   the output. If a ticker's winning row's params change, pruning dropped
   or corrupted the true best node for that ticker.
2. Importable `best_row()` / `node_dict()` -- feeds scripts/run_overlay_shim.py
   a node dict built directly from the raw backtest_cache winning row, using
   the identical column mapping scripts/drought_detection_test.py::load_nodes
   already uses for real watch_list rows (arm_sell_pct/take_profit/fixed_sl/
   trail_buy_pct/trail_sell_pct are named identically in both tables) --
   deliberately NOT a fresh reinterpretation of backtest_cache's known
   overloaded columns (see docs/backlog_cache.md's 2026-08-07 (late) entry),
   just the same trusted mapping applied to a different source table.

Usage:
  .venv/bin/python scripts/locate_best_node.py TICKER [TICKER ...] [--version v5] [--out FILE]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = "cache/research/trading_universe.db"
ROBUST_ALPHA_SQL = ("MIN(alpha_vs_spy, COALESCE(alpha_vs_spy_pessimistic, alpha_vs_spy), "
                     "COALESCE(alpha_vs_spy_certain, alpha_vs_spy))")

NODE_COLS = ["ticker", "strategy", "window", "z_score_threshold", "arm_sell_pct",
             "take_profit", "fixed_sl", "trail_buy_pct", "trail_sell_pct",
             "max_hold_hours", "entry_timing"]


def best_row(conn: sqlite3.Connection, ticker: str, version: str = "v5") -> dict | None:
    # sweep_run_id (2026-08-11): carries the winning backtest_cache row's
    # provenance stamp (NULL for any row computed before this column existed)
    # through to node_dict()/get_or_create_candidate_node, so a real candidate
    # can be traced back to the sweep_runs row (git_commit, campaign config)
    # that computed it, not just its 'version' data tag.
    cols_sql = ", ".join(NODE_COLS)
    row = conn.execute(f"""
        SELECT {cols_sql}, stop_loss, {ROBUST_ALPHA_SQL} AS robust_alpha, trades, sweep_run_id
        FROM backtest_cache
        WHERE ticker=? AND version=? AND trades > 0
        ORDER BY robust_alpha DESC LIMIT 1
    """, (ticker, version)).fetchone()
    if row is None:
        return None
    keys = NODE_COLS + ["stop_loss", "robust_alpha", "trades", "sweep_run_id"]
    return dict(zip(keys, row))


def node_dict(conn: sqlite3.Connection, ticker: str, version: str = "v5") -> dict | None:
    """Node dict shaped exactly like drought_detection_test.load_nodes()'s output --
    same 'arm_pct' derivation (arm_sell_pct for TrailingBoth, take_profit for
    TrailingExit), so it drops directly into drought_overlay_test.py's functions."""
    row = best_row(conn, ticker, version)
    if row is None:
        return None
    row = dict(row)
    row["z"] = row.pop("z_score_threshold")
    row["version"] = version
    row["arm_pct"] = (row["arm_sell_pct"] if row["strategy"] == "TrailingBothZScoreBreakout"
                       else row["take_profit"])
    return row


def ensure_candidate_nodes_table(conn: sqlite3.Connection):
    """Interim registry between a raw backtest_cache winning row and a real
    watch_list promotion (which has no id yet, hence no wl_id to reference --
    raised 2026-08-07: "no WL id yet so can't use that"). Deduped by the full
    param tuple so re-locating the same winning node (e.g. a rerun after
    another tranche's prune) reuses the same id instead of minting a
    duplicate -- candidate_overlay_results and any future candidate-stage
    table join against this id instead of repeating all the node columns."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidate_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            strategy TEXT NOT NULL,
            version TEXT NOT NULL,
            window INTEGER NOT NULL,
            z REAL NOT NULL,
            fixed_sl REAL NOT NULL,
            arm_pct REAL NOT NULL,
            trail_buy_pct REAL NOT NULL,
            trail_sell_pct REAL NOT NULL,
            max_hold_hours INTEGER NOT NULL,
            entry_timing TEXT NOT NULL,
            robust_alpha REAL NOT NULL,
            trades INTEGER NOT NULL,
            UNIQUE(ticker, strategy, version, window, z, fixed_sl, arm_pct,
                   trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing)
        )
    """)
    # sweep_run_id (2026-08-11): the candidate's provenance -- which
    # backtest_cache row (and via it, which sweep_runs invocation: git_commit,
    # campaign params) this candidate was selected from. Nullable -- NULL for
    # every candidate registered before this column existed, and for any
    # candidate whose source backtest_cache row itself predates the
    # sweep_run_id column on that table (no backfill of either, same
    # convention as backtest_cache's own phase/generation/sweep_run_id
    # columns -- see run_optimization_sweep.py's init_idempotent_db).
    cn_cols = {row[1] for row in conn.execute("PRAGMA table_info(candidate_nodes)").fetchall()}
    if "sweep_run_id" not in cn_cols:
        conn.execute("ALTER TABLE candidate_nodes ADD COLUMN sweep_run_id INTEGER")
    # pick/comment migration, added 2026-08-09: the user's real promotion
    # decision (Pick: yes/no + a free-text comment, e.g. "SpaceX 2x") needs
    # to survive a re-run of candidate_full_review.py --xlsx, which
    # otherwise always writes a fresh sheet from the DB with no memory of
    # what was already decided. candidate_nodes is this project's existing
    # interim per-node registry (real id, deduped by full param tuple) --
    # storing the decision here instead of a separate table means the report
    # can just LEFT JOIN it back in on the same id it already computes.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(candidate_nodes)").fetchall()}
    if "pick" not in cols:
        conn.execute("ALTER TABLE candidate_nodes ADD COLUMN pick TEXT")
    if "comment" not in cols:
        conn.execute("ALTER TABLE candidate_nodes ADD COLUMN comment TEXT")
    conn.commit()


def set_pick_comment(conn: sqlite3.Connection, node_id: int, pick: str = None, comment: str = None):
    """Updates ONLY the fields actually passed (None means 'leave alone', not
    'clear it') -- so `--comment "..."` alone doesn't wipe out an existing
    pick, and vice versa."""
    if pick is not None:
        conn.execute("UPDATE candidate_nodes SET pick=? WHERE id=?", (pick, node_id))
    if comment is not None:
        conn.execute("UPDATE candidate_nodes SET comment=? WHERE id=?", (comment, node_id))
    conn.commit()


def get_pick_comment(conn: sqlite3.Connection, node_id: int):
    row = conn.execute("SELECT pick, comment FROM candidate_nodes WHERE id=?", (node_id,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def get_or_create_candidate_node(conn: sqlite3.Connection, node: dict) -> int:
    """Returns the candidate_nodes.id for this exact node param tuple, inserting
    a fresh row (with a NEW created_at/robust_alpha/trades snapshot) only if no
    matching row exists yet -- a param-identical relocate (e.g. after a prune
    validation pass) reuses the same id, but if backtest_cache's underlying
    numbers for that exact param combo somehow changed, this does NOT update
    the existing row's robust_alpha/trades snapshot (INSERT OR IGNORE semantics
    -- first-seen wins). That's a deliberate limitation, not an oversight: a
    stale snapshot on an unchanged param tuple would only happen if the same
    grid cell was recomputed with different code, which is exactly the kind of
    drift this project's history says to notice, not silently paper over."""
    import datetime as _dt
    # Cast every value to a native Python type before it ever reaches sqlite3 --
    # found 2026-08-09 that a numpy.int64 (routine when a pandas column happens
    # to be all-whole-number, e.g. TrailingExit's take_profit grid values) gets
    # silently stored as a raw binary BLOB by sqlite3's default adapter instead
    # of a number, since sqlite3 has no built-in adapter for numpy scalar types.
    # 17 existing candidate_nodes rows had this (all arm_pct, all
    # TrailingExitZScoreBreakout) -- repaired directly in the DB; this cast is
    # what stops it from recurring on the next insert.
    _INT_COLS = {"window", "max_hold_hours"}
    key_cols = ("ticker", "strategy", "version", "window", "z", "fixed_sl", "arm_pct",
                "trail_buy_pct", "trail_sell_pct", "max_hold_hours", "entry_timing")
    key_vals = tuple(int(node[c]) if c in _INT_COLS else
                      (str(node[c]) if isinstance(node[c], str) else float(node[c]))
                      for c in key_cols)
    existing = conn.execute(f"""
        SELECT id FROM candidate_nodes
        WHERE {' AND '.join(f'{c}=?' for c in key_cols)}
    """, key_vals).fetchone()
    if existing:
        return existing[0]
    # sweep_run_id: carried through from node['sweep_run_id'] when the caller
    # populated it (node_dict()/best_row() do) -- absent/None for a node dict
    # built some other way (e.g. hand-constructed), which just leaves the new
    # row's provenance NULL rather than erroring.
    sweep_run_id = node.get("sweep_run_id")
    sweep_run_id = int(sweep_run_id) if sweep_run_id is not None else None
    cur = conn.execute(f"""
        INSERT INTO candidate_nodes
            (created_at, {', '.join(key_cols)}, robust_alpha, trades, sweep_run_id)
        VALUES (?, {', '.join('?' for _ in key_cols)}, ?, ?, ?)
    """, (_dt.datetime.now().isoformat(timespec="seconds"), *key_vals,
          float(node["robust_alpha"]), int(node["trades"]), sweep_run_id))
    conn.commit()
    return cur.lastrowid


def _fmt_row(r: dict) -> str:
    if r is None:
        return "NO_DATA"
    return (f"{r['strategy']},w{r['window']},z{r['z_score_threshold']},"
            f"arm{r['arm_sell_pct']},tp{r['take_profit']},sl{r['fixed_sl']},"
            f"tb{r['trail_buy_pct']},ts{r['trail_sell_pct']},h{r['max_hold_hours']},"
            f"{r['entry_timing']},alpha={r['robust_alpha']:.1f},trades={r['trades']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--version", default="v5")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    lines = []
    for t in args.tickers:
        r = best_row(conn, t, args.version)
        line = f"{t}: {_fmt_row(r)}"
        print(line)
        lines.append(line)

    if args.out:
        Path(args.out).write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
