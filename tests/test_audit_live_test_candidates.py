"""Regression test for the status_check.py/audit_live_test_candidates.py blind
spot found 2026-08-04: audit_one only ever checked open_positions, so a node
mid-entry (real signal fired, trailing-buy order resting, no fill yet) rendered
as flat/no-activity -- the same "state" ground truth that get_pending_buy_by_wl_id
fixes for coverage_check.py's carryover scenario."""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db
from scripts.audit_live_test_candidates import audit_one, _scenario_relevance

TICKER = 'TEST_AUDIT_PENDING'


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    db.ensure_tables()
    yield db
    os.unlink(tmp_db.name)


def test_audit_one_reports_pending_buy_instead_of_flat(isolated_db, capsys):
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'canary', window=5, take_profit=0.1,
                stop_loss=0, max_hold_hours=48)
    node = [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]
    sig = dict(current_price=100.0, last_bar=datetime.now())
    db.add_pending_buy(node, sig, channel='C123', ts='123.456')

    audit_one(TICKER)

    out = capsys.readouterr().out
    assert 'local pending-buy row on file' in out
    assert 'entry in progress' in out
    assert 'none (flat)\n' not in out
    # Wording fixed 2026-08-07 (real user confusion: a canary node's local
    # pending_buys row used to print identically to a real resting broker
    # order) -- this node is version='canary' and defaults to a non-'live'
    # state, so it must render as CANARY specifically (LIVE checked first,
    # then CANARY as its own distinct case, not lumped into a generic
    # dry_run/paper label -- user's explicit correction).
    assert 'CANARY' in out
    assert '✅ LIVE' not in out  # LIVE branch must not have fired for a non-live node


# ---------------------------------------------------------------------------
# _scenario_relevance / STALE? banner (2026-08-10) -- closes the gap found the
# same day SH's staged 'time_exit_via_trail' detune (trail_buy_pct 1%->5%,
# widened by a LATER, unrelated session for a completely different scenario,
# post_fill_topup) sat live for days after its actual designed scenario had
# already gone verified-live via a different node (RETL) -- nothing anywhere
# connected "this staged detune's reason for existing" to "is that reason
# still true." Confirmed against the real DB the same day this landed: SH,
# ERY, and the old (sunset) GDXU wl_id=108 row all immediately flagged STALE?
# on their first real run.
# ---------------------------------------------------------------------------

def _make_staged_node(role, expected_config):
    """Opens a real open_positions row (not just a bare watch_list node) so
    audit_one reaches the 'holding' branch, which calls _print_staged_config
    unconditionally -- the flat/entry-candidate branch needs real cached
    price data (a.compute_buy_signal) this synthetic test ticker doesn't
    have, and returns before ever reaching _print_staged_config."""
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'canary', window=5, take_profit=0.1,
                stop_loss=0, max_hold_hours=48)
    node = [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]
    db.set_staged_test_config(node['id'], TICKER, role, expected_config, notes='test')
    now = datetime.now()
    db.open_position(node, signal_price=100.0, signal_time=now, entry_price=101.0,
                      entry_time=now, shares=10)
    return db.get_watch_list_node_by_id(node['id'])


def test_scenario_relevance_none_for_baseline_config():
    assert _scenario_relevance('baseline_config') is None


def test_scenario_relevance_none_for_unmapped_role():
    """A future scenario_role added to staged_test_config without a matching
    SCENARIO_ROLE_TO_GRID_IDS entry must not crash -- it's simply not checked,
    same as 'baseline_config'."""
    assert _scenario_relevance('some_brand_new_role_nobody_mapped_yet') is None


def test_scenario_relevance_reports_wired_never_fired_when_no_events(isolated_db):
    relevance = _scenario_relevance('time_exit_via_trail')
    assert len(relevance) == 1
    assert 'time_exit_trigger: wired-never-fired' in relevance[0]


def test_stale_banner_fires_when_grid_scenario_already_verified_live(isolated_db, capsys):
    node = _make_staged_node('time_exit_via_trail',
                              dict(arm_sell_pct=0.3, fixed_sl=50, trail_sell_pct=50, max_hold_hours=31))
    with db._conn() as c:
        c.execute("UPDATE watch_list SET arm_sell_pct=0.3, fixed_sl=50, trail_sell_pct=50, "
                   "max_hold_hours=31 WHERE id=?", (node['id'],))
        c.commit()
    db.log_coverage_event('time_exit_trigger', 'live', ticker=TICKER, node_id=node['id'], result='fired')

    audit_one(TICKER)

    out = capsys.readouterr().out
    assert 'grid relevance: time_exit_trigger: verified-live' in out
    assert 'STALE?' in out


def test_stale_banner_absent_when_grid_scenario_not_yet_proven(isolated_db, capsys):
    node = _make_staged_node('time_exit_via_trail',
                              dict(arm_sell_pct=0.3, fixed_sl=50, trail_sell_pct=50, max_hold_hours=31))
    with db._conn() as c:
        c.execute("UPDATE watch_list SET arm_sell_pct=0.3, fixed_sl=50, trail_sell_pct=50, "
                   "max_hold_hours=31 WHERE id=?", (node['id'],))
        c.commit()

    audit_one(TICKER)

    out = capsys.readouterr().out
    assert 'grid relevance: time_exit_trigger: wired-never-fired' in out
    assert 'STALE?' not in out


def test_stale_banner_requires_all_mapped_grid_ids_verified(isolated_db, capsys):
    """gap_resize_and_topup maps to TWO grid rows (gap_resize, post_fill_topup)
    -- only one being verified-live must not trigger the STALE? banner."""
    node = _make_staged_node('gap_resize_and_topup', dict(trail_buy_pct=1.0, starting_notional=500))
    with db._conn() as c:
        c.execute("UPDATE watch_list SET trail_buy_pct=1.0, starting_notional=500 WHERE id=?", (node['id'],))
        c.commit()
    db.log_coverage_event('gap_resize', 'live', ticker=TICKER, node_id=node['id'], result='placed')

    audit_one(TICKER)

    out = capsys.readouterr().out
    assert 'gap_resize: verified-live' in out
    assert 'post_fill_topup: wired-never-fired' in out
    assert 'STALE?' not in out


# ---------------------------------------------------------------------------
# staged_test_config multi-role (2026-08-12) -- UNIQUE(wl_id) widened to
# UNIQUE(wl_id, scenario_role) after RETL organically ended up genuinely
# serving 4 roles at once with no way to register more than one. These tests
# pin: (1) set_staged_test_config no longer clobbers a different role's row
# for the same node, (2) clear_staged_test_config's role= param removes just
# one role, (3) audit_one/_print_staged_config prints every role for a node,
# not just the first.
# ---------------------------------------------------------------------------

def test_set_staged_test_config_does_not_clobber_other_roles(isolated_db):
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'canary', window=5, take_profit=0.1,
                stop_loss=0, max_hold_hours=48)
    node = [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]

    db.set_staged_test_config(node['id'], TICKER, 'time_exit_via_sl', dict(fixed_sl=50), notes='role A')
    db.set_staged_test_config(node['id'], TICKER, 'drought_handoff', dict(drought_confirm_days=3), notes='role B')

    rows = [r for r in db.get_staged_test_configs() if r['wl_id'] == node['id']]
    roles = {r['scenario_role'] for r in rows}
    assert roles == {'time_exit_via_sl', 'drought_handoff'}


def test_set_staged_test_config_updates_in_place_for_same_role(isolated_db):
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'canary', window=5, take_profit=0.1,
                stop_loss=0, max_hold_hours=48)
    node = [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]

    db.set_staged_test_config(node['id'], TICKER, 'addon', dict(addon_enabled=1), notes='v1')
    db.set_staged_test_config(node['id'], TICKER, 'addon', dict(addon_enabled=1), notes='v2')

    rows = [r for r in db.get_staged_test_configs() if r['wl_id'] == node['id']]
    assert len(rows) == 1
    assert rows[0]['notes'] == 'v2'


def test_clear_staged_test_config_with_role_removes_only_that_role(isolated_db):
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'canary', window=5, take_profit=0.1,
                stop_loss=0, max_hold_hours=48)
    node = [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]
    db.set_staged_test_config(node['id'], TICKER, 'time_exit_via_sl', dict(fixed_sl=50))
    db.set_staged_test_config(node['id'], TICKER, 'addon', dict(addon_enabled=1))

    db.clear_staged_test_config(node['id'], role='addon')

    rows = [r for r in db.get_staged_test_configs() if r['wl_id'] == node['id']]
    assert len(rows) == 1
    assert rows[0]['scenario_role'] == 'time_exit_via_sl'


def test_clear_staged_test_config_without_role_removes_all(isolated_db):
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'canary', window=5, take_profit=0.1,
                stop_loss=0, max_hold_hours=48)
    node = [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]
    db.set_staged_test_config(node['id'], TICKER, 'time_exit_via_sl', dict(fixed_sl=50))
    db.set_staged_test_config(node['id'], TICKER, 'addon', dict(addon_enabled=1))

    db.clear_staged_test_config(node['id'])

    rows = [r for r in db.get_staged_test_configs() if r['wl_id'] == node['id']]
    assert rows == []


def test_audit_one_prints_every_role_for_a_multi_role_node(isolated_db, capsys):
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'canary', window=5, take_profit=0.1,
                stop_loss=0, max_hold_hours=48)
    node = [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]
    db.set_staged_test_config(node['id'], TICKER, 'time_exit_via_sl',
                               dict(arm_sell_pct=0.3, fixed_sl=50, trail_sell_pct=50, max_hold_hours=31))
    db.set_staged_test_config(node['id'], TICKER, 'addon', dict(addon_enabled=1))
    with db._conn() as c:
        c.execute("UPDATE watch_list SET arm_sell_pct=0.3, fixed_sl=50, trail_sell_pct=50, "
                   "max_hold_hours=31, addon_enabled=1 WHERE id=?", (node['id'],))
        c.commit()
    now = datetime.now()
    db.open_position(node, signal_price=100.0, signal_time=now, entry_price=101.0,
                      entry_time=now, shares=10)

    audit_one(TICKER)

    out = capsys.readouterr().out
    assert 'staged test role: time_exit_via_sl' in out
    assert 'staged test role: addon' in out
