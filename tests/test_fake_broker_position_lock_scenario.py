"""Concurrency test for signals_db._position_lock (scenario_key 'position_lock',
instrumented 2026-08-01) -- proves the lock actually serializes open_position/
close_position against the same row, not just that the log_coverage_event
calls fire. Doesn't use tests/fake_broker.py directly (this is pure DB
concurrency, no broker round-trip involved) but lives alongside the other
fake-venue scenario tests since it's part of the same 2026-08-01 test-coverage
push, and asserts the same signals_db.get_coverage_events(...) shape."""
import os
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db

TICKER = 'TEST_POSITION_LOCK'


@pytest.fixture
def env(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, mode='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0, account='ira')
    yield
    os.unlink(tmp_db.name)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def test_concurrent_open_position_only_one_thread_wins(env):
    node = _node()
    now = datetime(2026, 8, 1, 10, 30)
    results = []
    barrier = threading.Barrier(20)

    def _attempt():
        barrier.wait()  # maximize real overlap -- all 20 threads hit open_position at once
        results.append(signals_db.open_position(node, 10.0, now, 10.0, now, shares=10))

    threads = [threading.Thread(target=_attempt) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1, f"expected exactly 1 winner across 20 racing threads, got {results.count(True)}"
    assert results.count(False) == 19

    open_positions = [p for p in signals_db.get_open_positions() if p['ticker'] == TICKER]
    assert len(open_positions) == 1, "the lock must prevent a real duplicate row, not just a duplicate return value"

    events = signals_db.get_coverage_events(scenario_key='position_lock')
    acquired = [e for e in events if e['result'] == 'acquired']
    skipped = [e for e in events if e['result'] == 'skipped_duplicate']
    assert len(acquired) == 20, "every thread should reach the locked block once"
    assert len(skipped) == 19


def test_concurrent_close_position_only_one_thread_actually_closes(env):
    node = _node()
    now = datetime(2026, 8, 1, 10, 30)
    assert signals_db.open_position(node, 10.0, now, 10.0, now, shares=10)
    pos = signals_db.get_open_position(TICKER)
    position_id = pos['id']

    results = []
    barrier = threading.Barrier(20)

    def _attempt():
        barrier.wait()
        results.append(signals_db.close_position(position_id, exit_signal_price=11.0, exit_price=11.0,
                                                   exit_time=now, exit_reason='TIME'))

    threads = [threading.Thread(target=_attempt) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1, f"expected exactly 1 thread to actually close it, got {results.count(True)}"
    assert results.count(False) == 19

    assert signals_db.get_open_position(TICKER) is None

    events = signals_db.get_coverage_events(scenario_key='position_lock')
    closed = [e for e in events if e['result'] == 'closed']
    already_closed = [e for e in events if e['result'] == 'already_closed']
    assert len(closed) == 1
    assert len(already_closed) == 19
