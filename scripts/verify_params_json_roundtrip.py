"""Verifies candidate_nodes.params_json round-trips correctly against the row's own
flat columns, for every row bench_phase1_phase2_inmemory.py's _insert_candidate_nodes_rows
actually populated params_json for (v6.3 verification pass, 2026-08-29).

For each row with params_json IS NOT NULL:
1. json.loads(row.params_json) -> decoded
2. Invert _insert_candidate_nodes_rows' strategy remap to recover the RAW take_profit/
   stop_loss/trail_sell_pct values build_params_dict() actually takes (see
   feedback_backtest_cache_axis_column_remapping memory -- flat arm_pct/trail_buy_pct/
   trail_sell_pct are already strategy-remapped, NOT the same as build_params_dict's raw
   inputs), then call the real build_params_dict() with those recovered raw inputs.
3. Compare decoded == reconstructed field-by-field.
4. Compare node_key(recovered raw inputs) against a hash computed directly off the
   decoded JSON's own canonical form -- confirms params_json is sufficient on its own to
   reproduce the same node_key a flat-column caller would get.

Read-only -- never touches candidate_nodes. If ANY row fails, per the Review-Gate
Persistence Rule this would implicate bench_phase1_phase2_inmemory.py (hard-gated) or
node_key.py -- STOP and report, do not attempt a fix here.

Usage:
  .venv/bin/python scripts/verify_params_json_roundtrip.py [--db PATH]
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from node_key import build_params_dict, node_key

DB_PATH = "cache/research/trading_universe.db"


def recover_raw_inputs(row: dict):
    """Inverts _insert_candidate_nodes_rows' strategy-axis remap (bench_phase1_phase2_
    inmemory.py lines ~154-162) to recover the raw (take_profit, stop_loss,
    trail_sell_pct) build_params_dict()/node_key() actually take. Returns None if the
    row's strategy uses the sl_axis='stop_loss' fallback case -- that raw value isn't
    recoverable from candidate_nodes' flat columns at all (never populated by any real
    strategy as of this check; flagged rather than silently guessed)."""
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(row["strategy"])
    take_profit = row["arm_pct"]
    if sl_axis_col == "trail_buy_pct":
        stop_loss = row["trail_buy_pct"]
        trail_sell_pct = row["trail_sell_pct"] if fourth_axis_col == "trail_pct" else 0.0
    elif sl_axis_col == "trail_pct":
        stop_loss = row["trail_sell_pct"]
        trail_sell_pct = 0.0
    else:
        return None
    return take_profit, stop_loss, trail_sell_pct


def check_row(row: dict):
    """Returns (passed: bool, detail: str)."""
    try:
        decoded = json.loads(row["params_json"])
    except (TypeError, json.JSONDecodeError) as e:
        return False, f"json.loads failed: {e}"

    raw = recover_raw_inputs(row)
    if raw is None:
        return False, f"strategy {row['strategy']!r} uses unrecoverable sl_axis='stop_loss' fallback"
    take_profit, stop_loss, trail_sell_pct = raw

    reconstructed = build_params_dict(
        row["strategy"], row["ticker"], row["fixed_sl"], row["window"], row["z"],
        row["max_hold_hours"], take_profit, stop_loss, trail_sell_pct, row["entry_timing"],
        strategies.resolve_axis_columns)

    if decoded != reconstructed:
        diffs = {k: (decoded.get(k), reconstructed.get(k))
                 for k in set(decoded) | set(reconstructed) if decoded.get(k) != reconstructed.get(k)}
        return False, f"field mismatch: {diffs}"

    key_from_flat = node_key(
        row["strategy"], row["ticker"], row["fixed_sl"], row["window"], row["z"],
        row["max_hold_hours"], take_profit, stop_loss, trail_sell_pct, row["entry_timing"],
        strategies.resolve_axis_columns)
    canonical_from_json = json.dumps(decoded, sort_keys=True)
    key_from_json = hashlib.sha256(f"{row['strategy']}|{canonical_from_json}".encode()).hexdigest()

    if key_from_flat != key_from_json:
        return False, f"node_key mismatch: flat={key_from_flat} json={key_from_json}"

    return True, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--quiet", action="store_true", help="only print failures + summary")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, ticker, strategy, version, window, z, fixed_sl, arm_pct,
               trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing, params_json
        FROM candidate_nodes WHERE params_json IS NOT NULL
        ORDER BY id
    """).fetchall()

    n_pass = n_fail = 0
    failures = []
    for r in rows:
        row = dict(r)
        passed, detail = check_row(row)
        if passed:
            n_pass += 1
            if not args.quiet:
                print(f"id={row['id']:>5} {row['ticker']:<6} {row['strategy']:<28} PASS")
        else:
            n_fail += 1
            failures.append((row["id"], row["ticker"], row["strategy"], detail))
            print(f"id={row['id']:>5} {row['ticker']:<6} {row['strategy']:<28} FAIL -- {detail}")

    print(f"\n{n_pass}/{n_pass + n_fail} rows passed round-trip check.")
    if n_fail:
        print(f"{n_fail} FAILURE(S) -- see rows above. Do NOT attempt a fix here; this "
              f"implicates bench_phase1_phase2_inmemory.py or node_key.py (hard-gated) -- "
              f"report with root-cause hypothesis per the Review-Gate Persistence Rule.")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    import script_usage
    script_usage.record_invocation()
    main()
