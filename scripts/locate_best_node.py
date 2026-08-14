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


def resolve_version(conn, ticker, preferred=("v5.1", "v5")):
    """Picks which backtest_cache version backs every downstream query for
    this ticker: v5.1 whenever it has ANY real (trades>0) rows for the
    ticker, else falls back to v5. v5.1 is deliberately a PARTIAL resweep --
    only the ~40 tickers whose raw hourly data changed in the 2026-08-11
    backfill get it (see docs/backlog_cache.md's 2026-08-11 entry) -- so its
    mere presence for a ticker already means "this is the more-complete/
    current data," no numeric tie-break needed. Most tickers will only ever
    have v5 rows and just fall through to it.

    Deliberately does NOT blend rows from both versions in the same query --
    worst_neighbor()'s cliff-safety search assumes one consistent, complete
    grid at neighboring param values, and mixing versions there would search
    a grid that was never actually swept as one campaign. This only decides
    which single version's grid backs every query for the ticker (fixed
    2026-08-12 -- previously every caller passed one hardcoded --version,
    silently missing the more-current v5.1 data for tickers that had it).
    Moved here from candidate_summary_report.py 2026-08-13 to fix a circular
    import when run_overlay_shim.py needed it too -- this module has no
    dependents of its own, so it's the right shared home."""
    c = conn.cursor()
    for v in preferred:
        c.execute("SELECT 1 FROM backtest_cache WHERE ticker=? AND version=? AND trades>0 LIMIT 1", (ticker, v))
        if c.fetchone():
            return v
    return preferred[-1]


def best_row(conn: sqlite3.Connection, ticker: str, version: str = "v5") -> dict | None:
    # sweep_run_id (2026-08-11): carries the winning backtest_cache row's
    # provenance stamp (NULL for any row computed before this column existed)
    # through to node_dict()/get_or_create_candidate_node, so a real candidate
    # can be traced back to the sweep_runs row (git_commit, campaign config)
    # that computed it, not just its 'version' data tag.
    # Tie-break matches prune_backtest_cache.py's TIEBREAK_SQL (trades DESC, stop_loss,
    # max_hold_hours) -- found 2026-08-13 that without this, an exact robust_alpha tie
    # (ETHU: two rows both 320.0325..., differing only on max_hold_hours 119 vs 126) let
    # this function and top_safe_nodes.best_safe_node() (the one candidate_full_review.py
    # actually uses) silently pick different winning rows, since neither had a
    # deterministic secondary sort key.
    # run_timestamp (2026-08-13): the REAL time this row's robust_alpha was
    # actually computed -- carried through to get_or_create_candidate_node as
    # 'computed_at' so it can stamp the true computation time instead of
    # datetime.now() (found by paired review: an earlier version stamped
    # "now" on every touch, which fabricated provenance on 4 real live nodes
    # by claiming a 2026-07-20 computation happened today. Never repeat that.)
    # Extended 2026-08-13 (paired review, MEDIUM/MEDIUM-HIGH, confirmed on real data):
    # trades/stop_loss/max_hold_hours alone is NOT a total order -- 34-69 real
    # (ticker,version,strategy) groups (incl. live-linked DPST/KORU) still tie on
    # all 4 keys while differing on a real param (e.g. DPST: two rows identical
    # except arm_sell_pct 30.0 vs 29.0). SQLite's own tie-break among those is
    # unspecified and can flip on an index rebuild/prune -- adding the remaining
    # real param axes makes this a genuine total order.
    cols_sql = ", ".join(NODE_COLS)
    row = conn.execute(f"""
        SELECT {cols_sql}, stop_loss, {ROBUST_ALPHA_SQL} AS robust_alpha, trades, sweep_run_id, run_timestamp
        FROM backtest_cache
        WHERE ticker=? AND version=? AND trades > 0
        ORDER BY robust_alpha DESC, trades DESC, stop_loss, max_hold_hours,
                 z_score_threshold, COALESCE(take_profit, arm_sell_pct), trail_buy_pct,
                 trail_sell_pct, entry_timing, strategy, window
        LIMIT 1
    """, (ticker, version)).fetchone()
    if row is None:
        return None
    keys = NODE_COLS + ["stop_loss", "robust_alpha", "trades", "sweep_run_id", "computed_at"]
    return dict(zip(keys, row))


def node_from_candidate_id(conn: sqlite3.Connection, candidate_node_id: int) -> dict | None:
    """Builds a node dict directly from an existing candidate_nodes row, shaped
    identically to node_dict()'s output -- for callers (run_overlay_shim.py's
    --node-id) that need to run against the EXACT node a report already
    displays, not whatever best_row()'s own selection independently re-derives.
    Built 2026-08-13 after best_row()/node_dict() repeatedly produced a
    different winning node than candidate_full_review.py's own "best safe/
    unsafe/CAGR-safe/certain" selection for the same ticker (ETHU: differing
    max_hold_hours even after a matching tie-break fix) -- these are genuinely
    different selection functions with no guaranteed agreement, so a caller
    that needs one specific displayed row must ask for it by id, not by
    re-deriving "the best" independently."""
    row = conn.execute("""
        SELECT ticker, strategy, version, window, z, fixed_sl, arm_pct,
               trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing,
               robust_alpha, trades, robust_alpha_computed_at
        FROM candidate_nodes WHERE id=?
    """, (candidate_node_id,)).fetchone()
    if row is None:
        return None
    keys = ["ticker", "strategy", "version", "window", "z", "fixed_sl", "arm_pct",
            "trail_buy_pct", "trail_sell_pct", "max_hold_hours", "entry_timing",
            "robust_alpha", "trades", "computed_at"]
    node = dict(zip(keys, row))
    node["stop_loss"] = node["fixed_sl"]
    # computed_at is whatever this row already honestly had (possibly None) --
    # never fabricated here. get_or_create_candidate_node preserves it rather
    # than stamping "now," since re-reading an existing row isn't a new
    # computation (found by paired review: this path is maximally circular --
    # it reads robust_alpha out of candidate_nodes and would otherwise write
    # a fabricated "computed today" right back over it with zero real work done).
    return node


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
    # account_mod/account (2026-08-13) + provenance columns (2026-08-13) -- added
    # out-of-band to the live DB during that session, never given a migration guard
    # here, so any fresh DB (a test DB, a clone, another machine) crashed on the
    # very first insert/refresh (found by paired review, reproduced in
    # tests/test_locate_best_node.py). Fixed same pattern as pick/comment above.
    if "account_mod" not in cols:
        conn.execute("ALTER TABLE candidate_nodes ADD COLUMN account_mod TEXT")
    if "account" not in cols:
        conn.execute("ALTER TABLE candidate_nodes ADD COLUMN account TEXT")
    if "robust_alpha_computed_at" not in cols:
        conn.execute("ALTER TABLE candidate_nodes ADD COLUMN robust_alpha_computed_at TEXT")
    if "years_at_computation" not in cols:
        conn.execute("ALTER TABLE candidate_nodes ADD COLUMN years_at_computation REAL")
    if "data_start" not in cols:
        conn.execute("ALTER TABLE candidate_nodes ADD COLUMN data_start TEXT")
    if "data_end" not in cols:
        conn.execute("ALTER TABLE candidate_nodes ADD COLUMN data_end TEXT")
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
    validation pass) reuses the same id.

    If backtest_cache's underlying numbers for that exact param combo changed
    (the same grid cell recomputed with different kernel code, e.g. the
    2026-08-11 certain-fill-resolution fix), the existing row's
    robust_alpha/trades/sweep_run_id are refreshed in place -- fixed
    2026-08-12, since a stale sweep_run_id let node_candidate_trace.py print a
    confident git-commit stamp that didn't actually produce the numbers
    currently backing the candidate (worse than the NULL the no-backfill
    convention implies elsewhere). Prints a one-line drift notice when a
    refresh actually changes something, since this project's history says to
    notice recomputation drift, not silently paper over it."""
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
        SELECT id, robust_alpha, trades, sweep_run_id FROM candidate_nodes
        WHERE {' AND '.join(f'{c}=?' for c in key_cols)}
    """, key_vals).fetchone()
    # sweep_run_id: carried through from node['sweep_run_id'] when the caller
    # populated it (node_dict()/best_row() do) -- absent/None for a node dict
    # built some other way (e.g. hand-constructed), which just leaves the new
    # row's provenance NULL rather than erroring.
    sweep_run_id = node.get("sweep_run_id")
    if isinstance(sweep_run_id, float) and sweep_run_id != sweep_run_id:
        sweep_run_id = None  # pandas NaN (missing), not a real id -- int(nan) raises
    sweep_run_id = int(sweep_run_id) if sweep_run_id is not None else None
    # robust_alpha_computed_at (2026-08-13, per user's explicit call after finding
    # all 10 real live nodes' stored numbers were already stale relative to a real
    # data backfill, with no way to know that from the DB alone): MUST be the real
    # backtest_cache.run_timestamp the caller threaded through as node['computed_at']
    # (best_row()/node_dict() do; node_from_candidate_id() passes through whatever
    # the existing row already had). Fixed 2026-08-13 (paired review, CONFIRMED HIGH,
    # already live-verified to have corrupted 4 real nodes -- SOXL/AGQ/KORU/DPST):
    # an earlier version stamped datetime.now() unconditionally, which fabricates a
    # "computed today" claim for a row that was actually computed weeks earlier and
    # never recomputed just because this function happened to be called again. This
    # is exactly the fabricated-provenance failure mode the column was built to
    # detect, not commit. Falls back to None (honest, not "now") if the caller has
    # no real computed_at to offer.
    _real_computed_at = node.get("computed_at")
    if isinstance(_real_computed_at, float) and _real_computed_at != _real_computed_at:
        _real_computed_at = None  # pandas NaN

    if existing:
        existing_id, old_alpha, old_trades, old_sweep_run_id = existing
        new_alpha, new_trades = float(node["robust_alpha"]), int(node["trades"])
        # Never downgrade a known sweep_run_id to NULL just because this
        # particular caller didn't supply one (e.g. a hand-constructed node
        # dict) -- only overwrite it when the new call actually knows one.
        effective_sweep_run_id = sweep_run_id if sweep_run_id is not None else old_sweep_run_id
        if (old_alpha, old_trades, old_sweep_run_id) != (new_alpha, new_trades, effective_sweep_run_id):
            print(f"[locate_best_node] candidate_nodes id={existing_id} refreshed: "
                  f"robust_alpha {old_alpha:.1f}->{new_alpha:.1f}, trades {old_trades}->{new_trades}, "
                  f"sweep_run_id {old_sweep_run_id}->{effective_sweep_run_id}")
        # robust_alpha_computed_at updates to the real value the caller supplied
        # (never fabricated). data_start/data_end/years_at_computation are
        # deliberately NOT touched here -- re-touching an existing row is not a
        # new computation, and we have no honest way to know what data window
        # produced the (possibly unchanged) stored alpha, so leave whatever was
        # already there (real backfilled value, or honest NULL) alone.
        if _real_computed_at is not None:
            conn.execute("""
                UPDATE candidate_nodes SET robust_alpha=?, trades=?, sweep_run_id=?,
                       robust_alpha_computed_at=? WHERE id=?
            """, (new_alpha, new_trades, effective_sweep_run_id, _real_computed_at, existing_id))
        else:
            conn.execute("""
                UPDATE candidate_nodes SET robust_alpha=?, trades=?, sweep_run_id=? WHERE id=?
            """, (new_alpha, new_trades, effective_sweep_run_id, existing_id))
        conn.commit()
        return existing_id

    # Brand-new candidate_nodes row -- but the underlying robust_alpha may still
    # come from an OLD backtest_cache computation (best_row() just SELECTs a
    # cached row, it doesn't compute anything). "Now" is only honestly the real
    # data window when computed_at's own date really is today; otherwise this
    # new registry row would still be claiming a data window nobody actually
    # backtested against, same failure mode as the refresh-path bug above, just
    # via INSERT instead of UPDATE. Fixed same session, same paired review.
    import pandas as _pd
    _now = _dt.datetime.now()
    _now_iso = _now.isoformat(timespec="seconds")
    _insert_computed_at = _real_computed_at if _real_computed_at is not None else _now_iso
    _computed_today = (
        _real_computed_at is None
        or str(_real_computed_at)[:10] == _now.strftime("%Y-%m-%d")
    )
    _data_start = _data_end = _years = None
    if _computed_today:
        _csv_path = Path(__file__).resolve().parent.parent / "cache" / "research" / f"{node['ticker']}_1h.csv"
        if _csv_path.exists():
            _df = _pd.read_csv(_csv_path, index_col=0, parse_dates=True)
            if len(_df):
                _data_start = _df.index.min().isoformat()
                _data_end = _df.index.max().isoformat()
                _years = round((_df.index.max() - _df.index.min()).days / 365.25, 2)

    cur = conn.execute(f"""
        INSERT INTO candidate_nodes
            (created_at, {', '.join(key_cols)}, robust_alpha, trades, sweep_run_id,
             robust_alpha_computed_at, years_at_computation, data_start, data_end)
        VALUES (?, {', '.join('?' for _ in key_cols)}, ?, ?, ?, ?, ?, ?, ?)
    """, (_now_iso, *key_vals,
          float(node["robust_alpha"]), int(node["trades"]), sweep_run_id, _insert_computed_at, _years,
          _data_start, _data_end))
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
