"""Tests for signals_invariants.check_open_position_config_matches_live_node's
account-drift addition (2026-08-18) -- see docs/backlog_cache.md's
2026-08-17 entry ("_place_stop_loss_for_position routes... CURRENT account,
not the position's pinned account") for the real-money reasoning: several
signals_notify.py call sites read node.get('account') on an already-open
position instead of pos.get('account'), so a node whose account column
changed after entry could route a real protective-stop attempt at the
wrong account. This check is a backstop, not a fix to those call sites.
Also covers the orphaned-position case found by paired review (a node
hard-deleted via remove_node() while its position was still open)."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import schwab_safety
import signals_invariants


@pytest.fixture
def env(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    signals_db.ensure_tables()
    schwab_safety.reload_accounts()
    yield
    Path(tmp_db.name).unlink()


def _make_node_and_position(account, node_account=None):
    """Opens a position on `account`, then (optionally) mutates the node's
    own watch_list.account to `node_account` afterward -- simulating the
    exact staleness this check exists to catch (an in-place account edit
    on a node that already has an open position)."""
    # add_node() has no return value -- fetch the freshly-inserted row's id
    # back out by ticker (unique enough in this isolated test DB).
    signals_db.add_node(
        ticker='SOXL', strategy='TrailingBothZScoreBreakout', version='v5',
        window=10, take_profit=9.0, stop_loss=1.0, max_hold_hours=48,
        label='test', trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
        account=account, starting_notional=10000.0, state='live',
    )
    conn = signals_db._conn()
    try:
        wl_id = conn.execute("SELECT id FROM watch_list WHERE ticker='SOXL'").fetchone()[0]
    finally:
        conn.close()
    node = signals_db.get_watch_list_node_by_id(wl_id)
    now = datetime.now()
    signals_db.open_position(node, 100.0, now, 100.0, now)
    if node_account is not None:
        conn = signals_db._conn()
        try:
            conn.execute("UPDATE watch_list SET account=? WHERE id=?", (node_account, wl_id))
            conn.commit()
        finally:
            conn.close()
    return wl_id


def test_matching_account_is_clean(env):
    _make_node_and_position('ira')
    assert signals_invariants.check_open_position_config_matches_live_node() == []


def test_drifted_account_flagged(env):
    _make_node_and_position('ira', node_account='roth')
    violations = signals_invariants.check_open_position_config_matches_live_node()
    assert len(violations) == 1
    assert 'SOXL' in violations[0]
    assert "'ira'" in violations[0]
    assert "'roth'" in violations[0]
    assert 'WRONG account' in violations[0]


def test_none_node_account_not_flagged(env):
    """A node with account=None (unmapped) shouldn't false-positive against
    a position that has a real pinned account -- matches
    check_live_node_missing_account's own separate scope for that gap."""
    _make_node_and_position('ira')
    conn = signals_db._conn()
    try:
        conn.execute("UPDATE watch_list SET account=NULL WHERE ticker='SOXL'")
        conn.commit()
    finally:
        conn.close()
    assert signals_invariants.check_open_position_config_matches_live_node() == []


def test_orphaned_position_flagged(env):
    """A position whose node was hard-deleted (remove_node(), which has no
    open-position guard) must not be silently skipped -- found by paired
    review 2026-08-18: this is the project's own documented account-move
    convention ("retire the old node, create a fresh one"), so it's a real
    path, not an edge case."""
    wl_id = _make_node_and_position('ira')
    signals_db.remove_node(wl_id)
    violations = signals_invariants.check_open_position_config_matches_live_node()
    assert len(violations) == 1
    assert 'SOXL' in violations[0]
    assert 'no longer exists' in violations[0]
