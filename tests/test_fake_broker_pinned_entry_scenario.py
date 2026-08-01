"""Fake-venue test for active_signals._scan_pinned_entry (scenario_key
'canary_pinned_entry' via the daily scenario_expectations check; registry id
'pinned_entry_trigger'). The existing offline test
(tests/test_part4_entry_trigger.py::test_scan_pinned_entry_uses_price_override_and_routes_live)
explicitly does NOT exercise real indicator math (no cached price data, so
compute_buy_signal always returns None there) -- exactly the "entry-mechanism
itself not separately verified" MVP limitation the registry notes call out.
This test closes that gap: real synthetic price data drives a genuine BUY
signal through the real z-score computation, _scan_pinned_entry's true
session-Open price fetch, and a real trailing-buy order reaching fake_broker.

Note: this scenario's real check_mechanism is 'scenario_expectations' (a
daily trade_lifecycle check against trade_log), not 'coverage_events' --
scripts/coverage_registry.py's fake_venue_proof/offline_proof detectors only
recognize get_coverage_events(scenario_key=...) assertions, so this test
will NOT move that registry column even though it's real, working proof.
That's a structural blind spot in the registry's own proof-scanner for any
scenario_expectations-mechanism row, not something a test can satisfy."""
import sys
from datetime import datetime
from pathlib import Path
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import active_signals
import signals_config
import signals_db
import schwab_safety
import schwab_client

from fake_broker import fake_broker  # noqa: F401
from conftest import make_synthetic_csv, cleanup_csv

TICKER = 'TEST_PINNED_ENTRY'
_OPEN_CHECK_TIME = datetime(2026, 7, 15, 9, 30, 2)


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
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: _OPEN_CHECK_TIME)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    signals_db.ensure_tables()
    # Hair-trigger z_score_threshold + last_close far below the ~100 synthetic
    # mean so compute_buy_signal reliably fires a real BUY, matching the real
    # canary IWM node's own hair-trigger design.
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=48, mode='live', z_score_threshold=0.1,
                         trail_buy_pct=0.1, trail_pct=0.1, fixed_sl_override=1.0,
                         entry_timing='open_check', account='soxl_ira', starting_notional=800)
    make_synthetic_csv(TICKER, last_close=85.0)

    yield

    cleanup_csv(TICKER)
    Path(tmp_db.name).unlink(missing_ok=True)


def test_pinned_entry_real_signal_places_real_trailing_buy(env, fake_broker, monkeypatch):
    monkeypatch.setattr(schwab_client, 'get_session_open_price', lambda t: (85.0, True))
    fake_broker.set_quote(TICKER, last=85.0, bid=85.0, ask=85.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    watchlist = signals_db.get_watchlist()
    summaries, failed = active_signals._scan_pinned_entry(9, 30, watchlist, set(), open_position_keys=set())

    assert failed == set(), f"price fetch should not fail: {failed}"

    pendings = [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER]
    assert len(pendings) == 1, (
        "the real synthetic dip (last_close=85 vs ~100 mean) should have crossed the hair-trigger "
        "z_score_threshold=0.1 and produced a real pending trailing-buy order"
    )

    # A real (non-dry_run, soxl_ira) trailing-buy order should have reached the broker.
    ticker_orders = [o for o in fake_broker.orders.values()
                      if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER]
    assert len(ticker_orders) == 1, f"expected exactly 1 real trailing-buy order, found {ticker_orders}"
    assert ticker_orders[0]['orderType'] == 'TRAILING_STOP'

    # open_price_quality_log should record the true-Open capture this scenario is also meant to prove.
    quality_rows = [r for r in signals_db.get_open_price_quality_log() if r['ticker'] == TICKER]
    assert len(quality_rows) == 1
    assert quality_rows[0]['is_true_open'] == 1
    assert quality_rows[0]['price'] == 85.0


def test_pinned_entry_no_signal_when_price_not_oversold(env, fake_broker, monkeypatch):
    """Control case: a synthetic Open in-line with the historical mean should
    NOT cross the z-score threshold, proving the prior test's signal is real
    (driven by the actual dip), not a fixture artifact that always fires."""
    monkeypatch.setattr(schwab_client, 'get_session_open_price', lambda t: (100.0, True))
    fake_broker.set_quote(TICKER, last=100.0, bid=100.0, ask=100.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    watchlist = signals_db.get_watchlist()
    active_signals._scan_pinned_entry(9, 30, watchlist, set(), open_position_keys=set())

    pendings = [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER]
    assert pendings == [], "an in-line (non-oversold) price should not produce a BUY signal"
    assert fake_broker.orders == {}
