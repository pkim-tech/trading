"""Tests for signals_notify.build_phased_monitors_report -- the end-of-day
text report (2026-07-30) for the two monitor-only checks built 2026-07-29
(pre_action_state_verification, node_circuit_breaker_tripped). Isolated DB,
no real Slack posts (this report is log-only by design, never posts)."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import schwab_safety
import signals_config
import signals_db
import signals_notify

CHECK_DATE = datetime.now().strftime('%Y-%m-%d')


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(schwab_safety, 'NODE_BREAKER_PATH', tmp_path / "schwab_node_breaker_state.json")
    signals_db.ensure_tables()
    yield
    Path(tmp_db.name).unlink()


def test_empty_day_reports_no_events_and_no_breaker_state(env):
    report = signals_notify.build_phased_monitors_report(CHECK_DATE)
    assert "No events" in report
    assert "No trips" in report
    assert "No breaker state file yet" in report


def test_reports_non_match_pre_action_verification_events(env):
    signals_db.log_coverage_event(
        "pre_action_state_verification", "live", ticker="TEST_EOD", node_id=1,
        result="mismatch", detail="side=BUY real_shares=5 local_shares=0")
    signals_db.log_coverage_event(
        "pre_action_state_verification", "live", ticker="TEST_EOD", node_id=1,
        result="match", detail="side=SELL real_shares=5 local_shares=5")
    report = signals_notify.build_phased_monitors_report(CHECK_DATE)
    assert "match=1, mismatch=1" in report
    assert "TEST_EOD" in report
    assert "real_shares=5 local_shares=0" in report
    # the matching row is counted but not itemized -- only non-match rows are
    assert report.count("TEST_EOD") == 1


def test_reports_circuit_breaker_trips(env):
    signals_db.log_coverage_event(
        "node_circuit_breaker_tripped", "dry_run", ticker="TEST_EOD", node_id=1,
        result="tripped", detail="kind=order_failures streak=3")
    report = signals_notify.build_phased_monitors_report(CHECK_DATE)
    assert "No trips" not in report
    assert "kind=order_failures streak=3" in report


def test_reports_current_nonzero_streak_state(env):
    signals_db.add_node('TEST_EOD', 'ZScoreBreakout', 'test', window=20, take_profit=10,
                         stop_loss=5, max_hold_hours=56, mode='live', account='ira')
    node = signals_db.get_watchlist()[0]
    schwab_safety.record_node_streak('TEST_EOD', 'ira', 'order_failures', hit=True, node_id=node['id'])
    report = signals_notify.build_phased_monitors_report(CHECK_DATE)
    assert "clean 0 streak" not in report
    assert "TEST_EOD (ira): order_failures streak=1/3" in report


def test_non_dict_breaker_state_file_does_not_crash(env, tmp_path):
    # Opus review, 2026-07-30: valid JSON that isn't a dict (e.g. a truncated
    # write leaving just a bare number, or some other corruption) used to
    # raise AttributeError on state.items() outside the try/except that only
    # guarded json.JSONDecodeError/OSError -- _guarded() would have caught it
    # in the real daemon, but scripts/phased_monitors_report.py's direct CLI
    # call would have tracebacked instead of reporting cleanly.
    schwab_safety.NODE_BREAKER_PATH.write_text("3")
    report = signals_notify.build_phased_monitors_report(CHECK_DATE)
    assert "Could not read breaker state file" in report


def test_a_different_dates_events_are_not_included(env):
    signals_db.log_coverage_event(
        "node_circuit_breaker_tripped", "live", ticker="TEST_EOD", node_id=1,
        result="tripped", detail="kind=order_failures streak=3")
    report = signals_notify.build_phased_monitors_report('2026-01-01')
    assert "No trips" in report
