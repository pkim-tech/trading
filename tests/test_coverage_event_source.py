"""Tests for coverage_events.source (write-attribution, Phase 1 -- see
docs/deep_backlog.md's "coverage_events write-attribution" entry). Covers:
schema (column exists, NULL default), a plain source='daemon' round-trip,
the fixture:+mode='live' validity warning (non-fatal, never blocks the
write), and that a real high-value call site (schwab_safety.check_order,
reached through schwab_client.place_equity_buy) now writes a real source
value by default without the caller having to pass anything."""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import schwab_safety
import schwab_client

TICKER = 'TEST_COVERAGE_SOURCE'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(schwab_safety, 'STATE_PATH', tmp_path / "schwab_order_counts.json")
    monkeypatch.setattr(schwab_safety, 'KILL_SWITCH_PATH', tmp_path / "schwab_kill_switch.json")
    monkeypatch.setattr(schwab_safety, 'TICKER_AUTOMATION_PATH', tmp_path / "schwab_ticker_automation.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTOMATION_PATH', tmp_path / "schwab_node_automation.json")
    monkeypatch.setattr(schwab_safety, 'NODE_BREAKER_PATH', tmp_path / "schwab_node_breaker_state.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_client, '_post_message', lambda *a, **kw: (None, None))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'ZScoreBreakout', 'test', window=20, take_profit=10,
                         stop_loss=5, max_hold_hours=56, state='live')
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account = 'roth' WHERE ticker = ?", (TICKER,))
        c.commit()
    yield
    Path(signals_config.DB_PATH).unlink(missing_ok=True)


def test_source_column_exists_and_defaults_null(env):
    with signals_db._conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(coverage_events)").fetchall()}
    assert 'source' in cols
    signals_db.log_coverage_event("some_scenario", "paper", ticker=TICKER, result="ok")
    events = signals_db.get_coverage_events(scenario_key="some_scenario")
    assert len(events) == 1
    assert events[0]['source'] is None


def test_source_daemon_round_trips(env):
    signals_db.log_coverage_event("some_scenario", "live", ticker=TICKER, result="ok", source="daemon")
    events = signals_db.get_coverage_events(scenario_key="some_scenario")
    assert len(events) == 1
    assert events[0]['source'] == "daemon"


def test_fixture_source_with_live_mode_warns_but_does_not_raise_or_block(env, capsys):
    # A fixture producing a mode='live' event is structurally wrong (a
    # fixture should never claim to be a real trade) -- must warn, not raise,
    # since this function must never be able to block a real order, and the
    # row must still be written (fire-and-forget contract preserved).
    signals_db.log_coverage_event("some_scenario", "live", ticker=TICKER, result="ok",
                                   source="fixture:stage_check_order_guard_scenarios")
    captured = capsys.readouterr()
    assert "fixture" in captured.out.lower()
    assert "mode='live'" in captured.out or "mode=" in captured.out
    events = signals_db.get_coverage_events(scenario_key="some_scenario")
    assert len(events) == 1
    assert events[0]['source'] == "fixture:stage_check_order_guard_scenarios"
    assert events[0]['mode'] == "live"


def test_fixture_source_with_dry_run_mode_does_not_warn(env, capsys):
    signals_db.log_coverage_event("some_scenario", "dry_run", ticker=TICKER, result="ok",
                                   source="fixture:stage_check_order_guard_scenarios")
    captured = capsys.readouterr()
    assert captured.out == ""


def test_check_order_writes_daemon_source_by_default(env, monkeypatch):
    """The real high-value call site: schwab_safety.check_order, reached the
    same way a genuine order attempt reaches it (via schwab_client), writes
    source='daemon' without the caller passing anything -- confirming the
    default threaded all the way from approve_and_record -> check_order ->
    log_coverage_event."""
    monkeypatch.setenv('SCHWAB_KILL_SWITCH', '1')
    with pytest.raises(schwab_safety.SafetyViolation, match="kill switch"):
        schwab_client.place_equity_buy('roth', TICKER, 5, 50.0)
    events = signals_db.get_coverage_events(scenario_key="kill_switch_block")
    assert len(events) == 1
    assert events[0]['source'] == "daemon"


def test_check_order_honors_explicit_source_override(env, monkeypatch):
    """A caller of check_order that already knows its own attribution (e.g.
    scripts/stage_check_order_guard_scenarios.py) can override the 'daemon'
    default explicitly."""
    monkeypatch.setenv('SCHWAB_KILL_SWITCH', '1')
    with pytest.raises(schwab_safety.SafetyViolation, match="kill switch"):
        schwab_safety.check_order('roth', TICKER, 5, 50.0, 'BUY',
                                   source='fixture:stage_check_order_guard_scenarios')
    events = signals_db.get_coverage_events(scenario_key="kill_switch_block")
    assert len(events) == 1
    assert events[0]['source'] == 'fixture:stage_check_order_guard_scenarios'
