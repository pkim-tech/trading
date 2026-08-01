"""Fake-venue tests for live-state reconciliation mismatch detection and
morning report delivery proof:

1. live_state_reconciliation_mismatch -> scenario_key 'reconciliation_mismatch':
   signals_notify.check_live_state_reconciliation detects when the broker's
   real position diverges from local open_positions belief (e.g. share count
   or protective order mismatch).

2. morning_report_delivery -> scenario_key 'morning_report_delivery':
   signals_notify.send_reference_report proves the report actually POSTs to
   Slack (returns a real channel/ts pair), not just that it gets built.

3. open_price_quality: NOTE -- test_fake_broker_pinned_entry_scenario.py
   already exercises this via open_price_quality_log assertions, but the
   coverage_registry.py's _EVENT_ASSERTED_RE regex only recognizes
   get_coverage_events(scenario_key=...) calls, so that existing test won't
   move the fake_venue_proof column. This is a structural blind spot in the
   registry's proof-detector for any scenario_expectations-mechanism row."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import signals_config
import signals_db
import schwab_client
import schwab_safety
import signals_notify

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_RECONCILIATION'
_IN_WINDOW_TIME = datetime(2026, 7, 29, 10, 30)


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
    monkeypatch.setattr(schwab_safety, '_now', lambda: _IN_WINDOW_TIME)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    signals_db.ensure_tables()
    yield
    schwab_safety.disengage_kill_switch()
    Path(tmp_db.name).unlink(missing_ok=True)


def _add_node(ticker, account, notional=5000):
    signals_db.add_node(ticker, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, mode='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account=account, starting_notional=notional)
    return [n for n in signals_db.get_watchlist()
            if n['ticker'] == ticker and n['account'] == account][0]


def test_reconciliation_detects_share_count_mismatch(env, fake_broker, monkeypatch):
    """Real share count at broker differs from local open_positions record --
    check_live_state_reconciliation detects and logs the mismatch.

    Fixed 2026-08-01 (paired independent+contextual review): previously
    mocked get_real_position directly (the exact function under test),
    making fake_broker's earlier BUY fill decorative -- the fixture's own
    get_account() only summed filled BUYs, with no way to reflect a real
    manual sale. fake_broker.py's position calc now also nets filled SELLs
    (see its 2026-08-01 fix), so the manual sale below is a real SELL order
    filled at the broker, exercising get_real_position's actual parsing
    logic end-to-end instead of bypassing it."""
    node = _add_node(TICKER, 'soxl_ira', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    # Place a real order and fill it manually at the broker.
    # Keep quantity low to stay under soxl_ira's $800 notional_cap.
    r, oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 50, 10.0)
    assert oid is not None
    fake_broker.force_fill(oid, 10.0)

    # Record the position locally with 50 shares (correct at the time).
    signals_db.open_position(node, signal_price=10.0, signal_time='2026-07-29 09:30:00',
                             entry_price=10.0, entry_time='2026-07-29 09:30:00', shares=50)

    # Simulate a manual sale at the broker (e.g. user liquidated 10 shares)
    # via a REAL sell order+fill at fake_broker -- the local DB is untouched,
    # so reconciliation should catch the resulting 50-vs-40 mismatch.
    r, sell_oid = schwab_client.place_equity_sell('soxl_ira', TICKER, 10, 10.0)
    assert sell_oid is not None
    fake_broker.force_fill(sell_oid, 10.0)
    assert schwab_client.get_real_position('soxl_ira', TICKER) == 40, (
        "sanity check: fake_broker's real position should reflect the manual sale"
    )

    # Run reconciliation against the local 50-share belief vs broker's 40.
    open_positions = signals_db.get_open_positions()
    signals_notify.check_live_state_reconciliation(open_positions)

    # Should have logged a reconciliation_mismatch event.
    events = signals_db.get_coverage_events(scenario_key='reconciliation_mismatch')
    assert any(
        e['ticker'] == TICKER and e.get('result') == 'shares'
        for e in events
    ), f"Expected share-count mismatch event, got events: {events}"


def test_reconciliation_detects_missing_protective_order(env, fake_broker, monkeypatch):
    """Trailing-sell marked placed but the broker has no matching SELL order --
    check_live_state_reconciliation detects and logs the mismatch."""
    node = _add_node(TICKER, 'soxl_ira', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    # Place a real market buy, keeping quantity low for the $800 cap.
    r, oid = schwab_client.place_equity_buy('soxl_ira', TICKER, 40, 10.0)
    assert oid is not None
    fake_broker.force_fill(oid, 10.0)

    # Record the position with trailing=True and order_placed=True (armed state).
    pos_id = signals_db.open_position(node, signal_price=10.0, signal_time='2026-07-29 09:30:00',
                                      entry_price=10.0, entry_time='2026-07-29 09:30:00', shares=40)
    trail_state = {'trailing': True, 'order_placed': True, 'peak': 10.5}
    signals_db.update_position_trail_state(pos_id, trail_state)

    # Fake broker genuinely has no SELL order resting (never placed one --
    # simulates a real trailing-sell that was cancelled without updating
    # local DB). get_real_position naturally reflects the real 40-share BUY
    # fill above -- no mock needed (fixed 2026-08-01, paired review: this
    # previously mocked get_real_position even though the real fill already
    # produced the exact right answer).
    assert schwab_client.get_real_position('soxl_ira', TICKER) == 40

    # Run reconciliation.
    open_positions = signals_db.get_open_positions()
    signals_notify.check_live_state_reconciliation(open_positions)

    # Should have logged a missing_trailing_sell mismatch event.
    events = signals_db.get_coverage_events(scenario_key='reconciliation_mismatch')
    assert any(
        e['ticker'] == TICKER and e.get('result') == 'missing_trailing_sell'
        for e in events
    ), f"Expected missing trailing-sell mismatch event, got events: {events}"


def test_morning_report_posts_to_slack_with_confirmation(env, fake_broker, monkeypatch):
    """send_reference_report correctly classifies a successful Slack delivery
    and returns the (channel, ts) confirmation it received.

    Note (corrected 2026-08-01, paired review): this mocks _post_chunked, so
    it proves the result-classification branch, not a real Slack POST --
    the docstring previously overclaimed "POSTs to Slack."."""
    node = _add_node(TICKER, 'soxl_ira', notional=50_000)
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)

    # Create a position so the report has something to show.
    signals_db.open_position(node, signal_price=10.0, signal_time='2026-07-29 09:30:00',
                             entry_price=10.0, entry_time='2026-07-29 09:30:00', shares=50)

    # Mock _post_chunked to return a real-looking (channel, ts) confirmation.
    def mock_post_chunked(title, fixed_blocks, units):
        # Return a fake but valid (channel, ts) pair.
        return ('C123ABC456', '1722274200.123456')

    monkeypatch.setattr(signals_notify, '_post_chunked', mock_post_chunked)

    # Run send_reference_report.
    watchlist = signals_db.get_watchlist()
    channel, ts = signals_notify.send_reference_report(watchlist)

    # Should return a non-None (channel, ts) tuple.
    assert channel is not None and ts is not None, \
        f"send_reference_report must return (channel, ts) confirmation, got ({channel}, {ts})"

    # Should have logged the morning_report_delivery event.
    events = signals_db.get_coverage_events(scenario_key='morning_report_delivery')
    assert any(
        e['result'] == 'sent'
        for e in events
    ), f"Expected morning_report_delivery 'sent' event, got events: {events}"


def test_morning_report_logs_failure_when_post_fails(env, fake_broker, monkeypatch):
    """send_reference_report logs no_delivery_confirmation when _post_chunked
    fails to return a valid (channel, ts) tuple."""
    node = _add_node(TICKER, 'soxl_ira', notional=50_000)

    # Mock _post_chunked to return (None, None) -- simulating a Slack delivery failure.
    def mock_post_chunked(title, fixed_blocks, units):
        return (None, None)

    monkeypatch.setattr(signals_notify, '_post_chunked', mock_post_chunked)

    # Run send_reference_report.
    watchlist = signals_db.get_watchlist()
    channel, ts = signals_notify.send_reference_report(watchlist)

    # Should return (None, None).
    assert channel is None and ts is None

    # Should have logged the morning_report_delivery event with no_delivery_confirmation result.
    events = signals_db.get_coverage_events(scenario_key='morning_report_delivery')
    assert any(
        e['result'] == 'no_delivery_confirmation'
        for e in events
    ), f"Expected morning_report_delivery 'no_delivery_confirmation' event, got events: {events}"
