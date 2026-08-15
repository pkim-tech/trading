"""Tests for signals_db.archive_node/unarchive_node -- node archive state
(archived_at), orthogonal to `state`, added per docs/design.md's "Node archive
state" entry. Also covers get_watchlist()'s include_archived default and the
two real bypasses (get_live_nodes(), scripts/node_candidate_trace.py) the
design doc's call-site audit flagged as needing an explicit fix."""
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
    yield db
    Path(tmp_db.name).unlink(missing_ok=True)


def _node(ticker='ARCHTEST', state='live'):
    db.add_node(ticker, 'TrailingBothZScoreBreakout', 'v5', window=10, take_profit=25,
                stop_loss=1, max_hold_hours=56, trail_buy_pct=3.0, trail_pct=4.0,
                fixed_sl_override=1, state=state, account='soxl_ira')
    return [n for n in db.get_watchlist(include_archived=True) if n['ticker'] == ticker][0]


def _seed_open_position(wl_id, ticker):
    with db._conn() as c:
        c.execute(
            "INSERT INTO open_positions (ticker, strategy, version, window, take_profit, "
            "stop_loss, max_hold_hours, signal_price, signal_time, entry_price, entry_time, wl_id) "
            "VALUES (?, 'TrailingBothZScoreBreakout', 'v5', 10, 25, 1, 56, 10.0, "
            "'2026-01-01 10:00:00', 10.0, '2026-01-01 10:00:00', ?)",
            (ticker, wl_id),
        )
        c.commit()


def _seed_pending_buy(wl_id, ticker):
    with db._conn() as c:
        c.execute(
            "INSERT INTO pending_buys (ticker, node_json, signal_price, signal_time, "
            "last_reminder_at, created_at, wl_id) "
            "VALUES (?, '{}', 10.0, '2026-01-01 10:00:00', '2026-01-01 10:00:00', "
            "'2026-01-01 10:00:00', ?)",
            (ticker, wl_id),
        )
        c.commit()


def test_archive_node_refuses_on_open_position(isolated_db):
    node = _node()
    _seed_open_position(node['id'], node['ticker'])
    with pytest.raises(ValueError, match="open position"):
        db.archive_node(node['id'])
    reread = [n for n in db.get_watchlist(include_archived=True) if n['id'] == node['id']][0]
    assert reread['archived_at'] is None


def test_archive_node_refuses_on_unresolved_pending_buy(isolated_db):
    node = _node()
    _seed_pending_buy(node['id'], node['ticker'])
    with pytest.raises(ValueError, match="pending buy"):
        db.archive_node(node['id'])
    reread = [n for n in db.get_watchlist(include_archived=True) if n['id'] == node['id']][0]
    assert reread['archived_at'] is None


def test_archive_node_succeeds_and_sets_archived_at(isolated_db):
    node = _node()
    db.archive_node(node['id'])
    reread = [n for n in db.get_watchlist(include_archived=True) if n['id'] == node['id']][0]
    assert reread['archived_at'] is not None


def test_archive_node_logs_audit_row(isolated_db):
    node = _node()
    db.archive_node(node['id'])
    audit = db.get_watchlist_audit()
    matching = [a for a in audit if a['action'] == 'archive_node' and a['watch_id'] == node['id']]
    assert len(matching) == 1


def test_archive_node_rejects_nonexistent_wl_id(isolated_db):
    with pytest.raises(ValueError, match="no watch_list row"):
        db.archive_node(999999)


def test_unarchive_node_reverses_archive(isolated_db):
    node = _node()
    db.archive_node(node['id'])
    db.unarchive_node(node['id'])
    reread = [n for n in db.get_watchlist(include_archived=True) if n['id'] == node['id']][0]
    assert reread['archived_at'] is None


def test_unarchive_node_logs_audit_row(isolated_db):
    node = _node()
    db.archive_node(node['id'])
    db.unarchive_node(node['id'])
    audit = db.get_watchlist_audit()
    matching = [a for a in audit if a['action'] == 'unarchive_node' and a['watch_id'] == node['id']]
    assert len(matching) == 1


def test_unarchive_node_rejects_nonexistent_wl_id(isolated_db):
    with pytest.raises(ValueError, match="no watch_list row"):
        db.unarchive_node(999999)


def test_get_watchlist_excludes_archived_by_default(isolated_db):
    node = _node()
    db.archive_node(node['id'])
    active = db.get_watchlist()
    assert node['id'] not in {n['id'] for n in active}


def test_get_watchlist_include_archived_true_includes_it(isolated_db):
    node = _node()
    db.archive_node(node['id'])
    everything = db.get_watchlist(include_archived=True)
    assert node['id'] in {n['id'] for n in everything}


def test_get_watchlist_default_still_includes_unarchived_nodes(isolated_db):
    node = _node()
    active = db.get_watchlist()
    assert node['id'] in {n['id'] for n in active}


def test_get_live_nodes_excludes_archived(isolated_db):
    node = _node(state='live')
    assert node['id'] in {n['id'] for n in db.get_live_nodes()}
    db.archive_node(node['id'])
    assert node['id'] not in {n['id'] for n in db.get_live_nodes()}


def test_node_candidate_trace_surfaces_archived_node_history(isolated_db, monkeypatch):
    """scripts/node_candidate_trace.py:84 must pass include_archived=True --
    an audit/trace tool losing an archived node's candidate-link history would
    be the UNSAFE-leaning gap the design doc's call-site audit flagged."""
    node = _node()
    db.archive_node(node['id'])
    nodes = [n for n in db.get_watchlist(include_archived=True) if n['state'] in ('live', 'dry_run', 'paper')]
    assert node['id'] in {n['id'] for n in nodes}
