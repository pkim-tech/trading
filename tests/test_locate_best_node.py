import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.locate_best_node import ensure_candidate_nodes_table, get_or_create_candidate_node


def _node(**overrides):
    node = dict(ticker="SOXL", strategy="TrailingBothZScoreBreakout", version="v5",
                window=10, z=1.0, fixed_sl=1.0, arm_pct=29.0, trail_buy_pct=1.0,
                trail_sell_pct=1.0, max_hold_hours=96, entry_timing="open_check",
                robust_alpha=100.0, trades=50, sweep_run_id=1)
    node.update(overrides)
    return node


def test_insert_new_node():
    conn = sqlite3.connect(":memory:")
    ensure_candidate_nodes_table(conn)
    node_id = get_or_create_candidate_node(conn, _node())
    row = conn.execute("SELECT robust_alpha, trades, sweep_run_id FROM candidate_nodes WHERE id=?",
                        (node_id,)).fetchone()
    assert row == (100.0, 50, 1)


def test_relocate_identical_node_reuses_id_no_change():
    conn = sqlite3.connect(":memory:")
    ensure_candidate_nodes_table(conn)
    first_id = get_or_create_candidate_node(conn, _node())
    second_id = get_or_create_candidate_node(conn, _node())
    assert first_id == second_id
    row = conn.execute("SELECT robust_alpha, trades, sweep_run_id FROM candidate_nodes WHERE id=?",
                        (first_id,)).fetchone()
    assert row == (100.0, 50, 1)


def test_relocate_recomputed_node_refreshes_stale_stamp():
    """Same param tuple, but backtest_cache recomputed under different kernel
    code produced new alpha/trades/sweep_run_id -- the bug this fixes: the old
    behavior left the stale first-seen stamp in place forever (first-seen
    wins), so node_candidate_trace.py printed a git-commit stamp that didn't
    actually produce the current numbers."""
    conn = sqlite3.connect(":memory:")
    ensure_candidate_nodes_table(conn)
    node_id = get_or_create_candidate_node(conn, _node())
    same_id = get_or_create_candidate_node(conn, _node(robust_alpha=142.3, trades=61, sweep_run_id=7))
    assert same_id == node_id
    row = conn.execute("SELECT robust_alpha, trades, sweep_run_id FROM candidate_nodes WHERE id=?",
                        (node_id,)).fetchone()
    assert row == (142.3, 61, 7)


def test_relocate_missing_sweep_run_id_does_not_clobber_known_stamp():
    """A caller with no sweep_run_id info (node dict built some other way)
    must not erase a previously-known provenance stamp."""
    conn = sqlite3.connect(":memory:")
    ensure_candidate_nodes_table(conn)
    node_id = get_or_create_candidate_node(conn, _node(sweep_run_id=7))
    same_id = get_or_create_candidate_node(conn, _node(sweep_run_id=None))
    assert same_id == node_id
    row = conn.execute("SELECT sweep_run_id FROM candidate_nodes WHERE id=?", (node_id,)).fetchone()
    assert row[0] == 7
