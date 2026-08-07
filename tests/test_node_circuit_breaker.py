"""Regression test for the gap a Sonnet review round found in the node-level
circuit breaker (2026-07-29): the order_failures streak's hit=False reset
used to fire unconditionally right after schwab_safety.approve_and_record
succeeded -- BEFORE the real broker submission was attempted -- so a
consecutive string of real broker-submission failures (as opposed to
SafetyViolation blocks caught before submission) could never actually
accumulate a streak; each attempt reset to 0 then bumped to 1. Fixed by
moving the reset to fire only after a genuinely clean outcome (a dry_run
pass-through, or a confirmed successful real submission). This test exercises
exactly the previously-untested "approve_and_record succeeds, then the real
broker call fails" path against tests/fake_broker.py's real order-placement
code, not a mocked function call."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import signals_config
import signals_db
import schwab_safety
import schwab_client

from fake_broker import fake_broker  # noqa: F401 (pytest fixture import)

TICKER = 'TEST_BREAKER_SCENARIO'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    monkeypatch.setattr(schwab_safety, 'STATE_PATH', tmp_path / "schwab_order_counts.json")
    monkeypatch.setattr(schwab_safety, 'KILL_SWITCH_PATH', tmp_path / "schwab_kill_switch.json")
    monkeypatch.setattr(schwab_safety, 'TICKER_AUTOMATION_PATH', tmp_path / "schwab_ticker_automation.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTOMATION_PATH', tmp_path / "schwab_node_automation.json")
    monkeypatch.setattr(schwab_safety, 'NODE_BREAKER_PATH', tmp_path / "schwab_node_breaker_state.json")
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_node_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 7, 29, 10, 30))
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    signals_db.ensure_tables()
    # 'soxl_ira' is the one real dry_run=False account in schwab_safety.ACCOUNTS --
    # fake_broker patches the network layer, so exercising the actual
    # non-dry_run submission path is safe here.
    signals_db.add_node(TICKER, 'ZScoreBreakout', 'test', window=20, take_profit=10,
                         stop_loss=5, max_hold_hours=56, state='live')
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account = 'soxl_ira' WHERE ticker = ?", (TICKER,))
        c.commit()

    yield
    Path(tmp_db.name).unlink(missing_ok=True)


def test_real_broker_submission_failure_accumulates_the_order_failures_streak(env, monkeypatch, fake_broker):
    monkeypatch.setattr(schwab_client, '_ORDER_SUBMIT_RETRY_INTERVAL_SECS', 0)

    def _always_fail(account_hash, order):
        raise RuntimeError("simulated broker rejection")
    monkeypatch.setattr(fake_broker, 'place_order', _always_fail)

    for _ in range(3):
        with pytest.raises(RuntimeError):
            schwab_client.place_equity_buy('soxl_ira', TICKER, 5, 50.0)
    trips = signals_db.get_coverage_events(scenario_key='node_circuit_breaker_tripped')
    assert len(trips) == 1
    assert trips[0]['ticker'] == TICKER
    assert trips[0]['result'] == 'tripped'
    assert 'order_failures' in trips[0]['detail']


def test_real_broker_submission_success_resets_the_streak(env, monkeypatch, fake_broker):
    monkeypatch.setattr(schwab_client, '_ORDER_SUBMIT_RETRY_INTERVAL_SECS', 0)
    fake_broker.set_quote(TICKER, 50.0)
    real_place_order = fake_broker.place_order

    def _always_fail(account_hash, order):
        raise RuntimeError("simulated broker rejection")

    monkeypatch.setattr(fake_broker, 'place_order', _always_fail)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            schwab_client.place_equity_buy('soxl_ira', TICKER, 5, 50.0)

    monkeypatch.setattr(fake_broker, 'place_order', real_place_order)
    schwab_client.place_equity_buy('soxl_ira', TICKER, 5, 50.0)  # real clean fill via fake_broker

    # A different quantity for the follow-up failures -- outside
    # DUPLICATE_ORDER_QUANTITY_TOLERANCE_PCT of the now-genuinely-filled
    # order above, so the real (and correct) duplicate-order guard doesn't
    # interfere with what this test is actually exercising: the breaker
    # streak's reset.
    monkeypatch.setattr(fake_broker, 'place_order', _always_fail)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            schwab_client.place_equity_buy('soxl_ira', TICKER, 13, 50.0)
    assert signals_db.get_coverage_events(scenario_key='node_circuit_breaker_tripped') == []
