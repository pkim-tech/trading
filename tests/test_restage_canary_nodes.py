"""Tests for scripts/restage_canary_nodes.py -- the 2026-08-08 nightly
canary-regression-safety tool. Landed with zero test coverage (the commit's
own test diff only fixed unrelated tuple-unpack signatures); this file closes
that gap.

Covers the close-only design and its guards: never touches a non-canary
node, never touches a real (not is_dry_run_sim) position, --dry-run makes no
DB change, and a close_position race (row already gone) is logged rather
than silently swallowed."""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db
from scripts.restage_canary_nodes import list_canary_nodes, _open_position_for_node, restage, main
from tests.conftest import make_synthetic_csv, cleanup_csv

TICKER = 'TEST_RESTAGE_CANARY'


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    db.ensure_tables()
    make_synthetic_csv(TICKER, last_close=99.0)
    yield db
    os.unlink(tmp_db.name)
    cleanup_csv(TICKER)


def _add_canary_node(ticker=TICKER, state='live'):
    db.add_node(ticker, 'TrailingBothZScoreBreakout', 'canary', window=5, take_profit=0.1,
                stop_loss=1.0, max_hold_hours=48, state=state, account='ira')
    with db._conn() as c:
        row = c.execute("SELECT * FROM watch_list WHERE ticker=? AND version='canary'", (ticker,)).fetchone()
    return dict(row)


def _open_pos(node, is_dry_run_sim=True, entry_price=95.0):
    now = datetime.now()
    signal_time = now - timedelta(days=3)
    db.open_position(node, entry_price, signal_time, entry_price, signal_time,
                      shares=10, is_dry_run_sim=is_dry_run_sim)
    return db.get_open_position(node['ticker'])


def test_list_canary_nodes_excludes_non_canary(isolated_db):
    _add_canary_node()
    db.add_node('TEST_RESTAGE_NONCANARY', 'TrailingBothZScoreBreakout', 'v5', window=5,
                take_profit=0.1, stop_loss=1.0, max_hold_hours=48, state='live', account='ira')
    nodes = list_canary_nodes()
    tickers = [n['ticker'] for n in nodes]
    assert TICKER in tickers
    assert 'TEST_RESTAGE_NONCANARY' not in tickers


def test_open_position_for_node_scopes_by_wl_id_not_ticker(isolated_db):
    node = _add_canary_node()
    assert _open_position_for_node(node['id']) is None
    _open_pos(node)
    pos = _open_position_for_node(node['id'])
    assert pos is not None
    assert pos['ticker'] == TICKER


def test_restage_closes_position_and_logs_event(isolated_db):
    node = _add_canary_node()
    pos = _open_pos(node)
    result = restage(node, pos, dry_run=False)
    assert result is True
    assert db.get_open_position(TICKER) is None  # actually closed, not just reported

    with db._conn() as c:
        events = c.execute(
            "SELECT * FROM coverage_events WHERE scenario_key='canary_restage' AND ticker=?", (TICKER,)
        ).fetchall()
    assert len(events) == 1
    assert events[0]['result'] == 'restaged'


def test_restage_dry_run_makes_no_change(isolated_db):
    node = _add_canary_node()
    pos = _open_pos(node)
    result = restage(node, pos, dry_run=True)
    assert result is True
    assert db.get_open_position(TICKER) is not None  # still open -- dry-run never closes

    with db._conn() as c:
        events = c.execute(
            "SELECT * FROM coverage_events WHERE scenario_key='canary_restage' AND ticker=?", (TICKER,)
        ).fetchall()
    assert events == []  # no event logged for a dry-run either


def test_restage_logs_close_failed_when_position_already_gone(isolated_db):
    node = _add_canary_node()
    pos = _open_pos(node)
    db.close_position(pos['id'], exit_signal_price=100.0, exit_price=100.0, exit_time=datetime.now(),
                       exit_reason='TIME')

    result = restage(node, pos, dry_run=False)  # pos dict is now stale -- id no longer resolves
    assert result is False

    with db._conn() as c:
        events = c.execute(
            "SELECT * FROM coverage_events WHERE scenario_key='canary_restage' AND ticker=?", (TICKER,)
        ).fetchall()
    assert len(events) == 1
    assert events[0]['result'] == 'close_failed'


def test_main_refuses_to_restage_a_real_non_dry_run_sim_position(isolated_db, capsys, monkeypatch):
    """A canary node whose position is somehow NOT is_dry_run_sim would be real
    money -- main() must refuse rather than close it, per the script's own
    documented real-money guard."""
    node = _add_canary_node()
    _open_pos(node, is_dry_run_sim=False)

    monkeypatch.setattr(sys, 'argv', ['restage_canary_nodes.py'])
    main()

    assert db.get_open_position(TICKER) is not None  # untouched
    captured = capsys.readouterr()
    assert 'refusing to restage' in captured.out


def test_main_leaves_flat_canary_node_alone(isolated_db, capsys, monkeypatch):
    _add_canary_node()  # never opened
    monkeypatch.setattr(sys, 'argv', ['restage_canary_nodes.py'])
    main()
    captured = capsys.readouterr()
    assert '0 restaged, 1 already flat, 0 skipped' in captured.out


def test_main_restages_a_dry_run_sim_canary_position(isolated_db, capsys, monkeypatch):
    node = _add_canary_node()
    _open_pos(node, is_dry_run_sim=True)
    monkeypatch.setattr(sys, 'argv', ['restage_canary_nodes.py'])
    main()
    assert db.get_open_position(TICKER) is None
    captured = capsys.readouterr()
    assert '1 restaged, 0 already flat, 0 skipped' in captured.out
