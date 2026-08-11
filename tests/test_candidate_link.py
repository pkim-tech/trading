"""Tests for signals_db.set_candidate_link/get_candidate_links -- the
watch_list_candidate_link table (added 2026-08-11) that records an explicit,
user-confirmed link from a watch_list node to the candidate_nodes row it was
promoted from, replacing param-tuple guessing.

Both a cold and a contextual review (session-wrap, 2026-08-11) independently
flagged the first version as writing an unvalidated candidate_node_id -- these
tests exercise the validation added in response (wl_id must exist,
candidate_node_id must exist AND its ticker/strategy must match the node's),
using an isolated fake candidate_nodes DB rather than the real research DB."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    db.ensure_tables()

    tmp_cand_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_cand_db.close()
    cand_conn = sqlite3.connect(tmp_cand_db.name)
    cand_conn.execute("""
        CREATE TABLE candidate_nodes (
            id INTEGER PRIMARY KEY, ticker TEXT, strategy TEXT,
            window INTEGER, fixed_sl REAL, arm_pct REAL,
            trail_buy_pct REAL, trail_sell_pct REAL, robust_alpha REAL, trades INTEGER
        )
    """)
    cand_conn.executemany(
        "INSERT INTO candidate_nodes (id, ticker, strategy) VALUES (?, ?, ?)",
        [(107, 'LINKTEST', 'TrailingBothZScoreBreakout'),
         (108, 'LINKTEST', 'TrailingBothZScoreBreakout'),
         (120, 'LINKTEST', 'TrailingBothZScoreBreakout'),
         (110, 'LINKTEST_B', 'TrailingBothZScoreBreakout'),
         (999, 'WRONGTICKER', 'TrailingBothZScoreBreakout')])
    cand_conn.commit()
    cand_conn.close()
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_cand_db.name)

    yield db
    os.unlink(tmp_db.name)
    os.unlink(tmp_cand_db.name)


def _node(ticker='LINKTEST'):
    db.add_node(ticker, 'TrailingBothZScoreBreakout', 'v5', window=10, take_profit=25,
                stop_loss=1, max_hold_hours=56, trail_buy_pct=3.0, trail_pct=4.0,
                fixed_sl_override=1, state='live')
    return [n for n in db.get_watchlist() if n['ticker'] == ticker][0]


def test_set_and_get_candidate_link(isolated_db):
    node = _node()
    db.set_candidate_link(node['id'], candidate_node_id=108, role='core', note='test link')
    links = db.get_candidate_links(node['id'])
    assert len(links) == 1
    assert links[0]['candidate_node_id'] == 108
    assert links[0]['role'] == 'core'
    assert links[0]['note'] == 'test link'


def test_relinking_same_role_overwrites_not_duplicates(isolated_db):
    """A corrected decision (e.g. the DPST 107->108 correction from the same
    real session this table was built to fix) must overwrite the prior link,
    not accumulate a second row for the same role."""
    node = _node()
    db.set_candidate_link(node['id'], candidate_node_id=107, role='core')
    db.set_candidate_link(node['id'], candidate_node_id=108, role='core')
    links = db.get_candidate_links(node['id'])
    assert len(links) == 1
    assert links[0]['candidate_node_id'] == 108


def test_relinking_without_a_new_note_preserves_the_old_one(isolated_db):
    """Found by the cold review: ON CONFLICT DO UPDATE was blanking an
    existing note on any re-link call that didn't repeat it."""
    node = _node()
    db.set_candidate_link(node['id'], candidate_node_id=107, role='core', note='original reasoning')
    db.set_candidate_link(node['id'], candidate_node_id=108, role='core')
    links = db.get_candidate_links(node['id'])
    assert links[0]['note'] == 'original reasoning'


def test_distinct_roles_coexist_on_one_node(isolated_db):
    """A node's core config and its drought config can trace to different
    candidates -- both links must survive independently."""
    node = _node()
    db.set_candidate_link(node['id'], candidate_node_id=108, role='core')
    db.set_candidate_link(node['id'], candidate_node_id=120, role='drought')
    links = {l['role']: l['candidate_node_id'] for l in db.get_candidate_links(node['id'])}
    assert links == {'core': 108, 'drought': 120}


def test_role_is_case_and_whitespace_normalized(isolated_db):
    """Found by the cold review: 'core' and 'Core'/' core ' would otherwise
    create two rows under UNIQUE(wl_id, role) instead of one."""
    node = _node()
    db.set_candidate_link(node['id'], candidate_node_id=107, role='Core')
    db.set_candidate_link(node['id'], candidate_node_id=108, role=' core ')
    links = db.get_candidate_links(node['id'])
    assert len(links) == 1
    assert links[0]['role'] == 'core'
    assert links[0]['candidate_node_id'] == 108


def test_get_candidate_links_with_no_wl_id_returns_all(isolated_db):
    a = _node('LINKTEST')
    b = _node('LINKTEST_B')
    db.set_candidate_link(a['id'], candidate_node_id=108, role='core')
    db.set_candidate_link(b['id'], candidate_node_id=110, role='core')
    all_links = db.get_candidate_links()
    assert len(all_links) == 2
    assert {l['wl_id'] for l in all_links} == {a['id'], b['id']}


def test_unlinked_node_has_no_rows(isolated_db):
    node = _node()
    assert db.get_candidate_links(node['id']) == []


def test_rejects_nonexistent_wl_id(isolated_db):
    with pytest.raises(ValueError, match="no watch_list row"):
        db.set_candidate_link(999999, candidate_node_id=108, role='core')


def test_rejects_nonexistent_candidate_id(isolated_db):
    node = _node()
    with pytest.raises(ValueError, match="no candidate_nodes row"):
        db.set_candidate_link(node['id'], candidate_node_id=424242, role='core')


def test_rejects_ticker_mismatch_between_node_and_candidate(isolated_db):
    """The exact failure mode this table exists to prevent: a link that looks
    authoritative but actually points at the wrong ticker."""
    node = _node('LINKTEST')
    with pytest.raises(ValueError, match="refusing to link mismatched"):
        db.set_candidate_link(node['id'], candidate_node_id=999, role='core')


# ---------------------------------------------------------------------------
# set_drought_config
# ---------------------------------------------------------------------------

def test_set_drought_config_enables_overlay_with_given_values(isolated_db):
    node = _node()
    db.set_drought_config(node['id'], confirm_days=3, vol_gate=0.4)
    updated = [n for n in db.get_watchlist() if n['id'] == node['id']][0]
    assert updated['drought_overlay_enabled'] == 1
    assert updated['drought_confirm_days'] == 3
    assert updated['drought_vol_gate'] == 0.4


def test_set_drought_config_partial_call_does_not_null_existing_overrides(isolated_db):
    """Both reviewers independently caught this: the first version wrote all
    three override columns unconditionally, so a follow-up call to adjust
    just confirm_days silently reverted a deliberately-tuned override back to
    'use the node's own core risk params' (NULL) with no record it happened."""
    node = _node()
    db.set_drought_config(node['id'], confirm_days=3, vol_gate=0.4, sl_override=2.5)
    db.set_drought_config(node['id'], confirm_days=5, vol_gate=0.5)
    updated = [n for n in db.get_watchlist() if n['id'] == node['id']][0]
    assert updated['drought_confirm_days'] == 5
    assert updated['drought_vol_gate'] == 0.5
    assert updated['drought_sl_pct_override'] == 2.5  # untouched, not silently NULLed


def test_set_drought_config_explicit_none_does_clear_an_override(isolated_db):
    node = _node()
    db.set_drought_config(node['id'], confirm_days=3, vol_gate=0.4, sl_override=2.5)
    db.set_drought_config(node['id'], confirm_days=3, vol_gate=0.4, sl_override=None)
    updated = [n for n in db.get_watchlist() if n['id'] == node['id']][0]
    assert updated['drought_sl_pct_override'] is None


def test_set_drought_config_rejects_nonexistent_wl_id(isolated_db):
    with pytest.raises(ValueError, match="no watch_list row"):
        db.set_drought_config(999999, confirm_days=3, vol_gate=0.4)


def test_set_drought_config_writes_audit_row(isolated_db):
    """Both reviewers flagged the first version as a real config mutation on
    live nodes with zero watch_list_audit trace."""
    node = _node()
    db.set_drought_config(node['id'], confirm_days=3, vol_gate=0.4)
    audit = db.get_watchlist_audit()
    matching = [a for a in audit if a['action'] == 'set_drought_config' and a['watch_id'] == node['id']]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# set_force_same_day_block (2026-08-11) -- per-node opt-in override of
# schwab_safety.check_order's same_day_block guard, so a specific ticker can
# get the block even on a margin-account node (normally exempt). The actual
# BUY-blocking behavior is covered by tests/test_fake_broker_check_order_
# guards_phase2_scenario.py; these are the setter-level unit tests, same
# convention as set_drought_config above.
# ---------------------------------------------------------------------------

def test_set_force_same_day_block_defaults_off(isolated_db):
    node = _node()
    assert node['force_same_day_block'] == 0


def test_set_force_same_day_block_enables_and_disables(isolated_db):
    node = _node()
    db.set_force_same_day_block(node['id'], True)
    updated = [n for n in db.get_watchlist() if n['id'] == node['id']][0]
    assert updated['force_same_day_block'] == 1

    db.set_force_same_day_block(node['id'], False)
    updated = [n for n in db.get_watchlist() if n['id'] == node['id']][0]
    assert updated['force_same_day_block'] == 0


def test_set_force_same_day_block_rejects_nonexistent_wl_id(isolated_db):
    with pytest.raises(ValueError, match="no watch_list row"):
        db.set_force_same_day_block(999999, True)


def test_set_force_same_day_block_writes_audit_row(isolated_db):
    node = _node()
    db.set_force_same_day_block(node['id'], True)
    audit = db.get_watchlist_audit()
    matching = [a for a in audit if a['action'] == 'set_force_same_day_block' and a['watch_id'] == node['id']]
    assert len(matching) == 1
    assert matching[0]['detail'] == 'False -> True'
