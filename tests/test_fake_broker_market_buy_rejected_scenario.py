"""fake_broker scenarios for signals_notify.check_market_buy_rejected (built
2026-08-17, found by paired review of the check_auto_fills market-buy fallback
fix, 2026-08-16): a market-buy-eligible node's pending_buys row whose real
broker order comes back REJECTED/CANCELED/EXPIRED must be cleared and alerted
(zero-fill case) or preserved and alerted (a real partial fill happened
first), not left polling forever with no termination path (get_filled_order
only ever reports something for a status=='FILLED' order).

Rewritten 2026-08-17 (independent-cold review of the first version, MEDIUM
finding): the original tests were named "fake_broker scenario" but never
actually used fake_broker -- they monkeypatched schwab_client.get_order_status
out entirely, so nothing exercised the real order JSON shape, account-hash
resolution, or the bare-except-returns-None path. That's exactly what let the
partial-fill HIGH gap go unnoticed. These now drive real fake_broker order
state directly (seeded via seed_resting_order, transitioned via direct dict
mutation for terminal statuses -- the same pattern already used elsewhere in
this repo, e.g. test_fake_broker_drought_handoff_scenario.py's
force_reject_next_order-adjacent direct-mutation cases)."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import signals_config
import signals_db
import signals_notify
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_MARKET_BUY_REJECTED_SCENARIO'
SIGNAL_TIME = datetime(2026, 7, 29, 10, 30)


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
    # Explicit real accounts dict (independent-cold review, MEDIUM finding: the
    # original tests relied on the real soxl_ira row's current trading_enabled
    # state, which would pass vacuously if that account is ever flipped).
    monkeypatch.setattr(schwab_safety, 'ACCOUNTS', {
        'soxl_ira': schwab_safety.AccountLimits(
            enabled=True, notional_cap=100_000, daily_order_cap=100, trading_enabled=True,
            cash_settlement_type='cash',
        ),
    })
    monkeypatch.setattr(schwab_safety, '_now', lambda: SIGNAL_TIME)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    posted = []
    monkeypatch.setattr(signals_notify, '_post_message', lambda text, *a, **kw: posted.append(text))
    signals_notify._MARKET_BUY_STATUS_UNKNOWN_SINCE.clear()
    signals_notify._MARKET_BUY_UNKNOWN_ALERTED.clear()

    signals_db.ensure_tables()
    # TrailingExitZScoreBreakout -- a market-buy (non-trailing-buy) strategy,
    # db._is_trailing_buy(node) is False for it, matching check_market_buy_
    # rejected's target population.
    signals_db.add_node(TICKER, 'TrailingExitZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         fixed_sl_override=1.0, account='soxl_ira', starting_notional=2000)

    yield posted


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _seed_pending(node, order_id):
    signals_db.add_pending_buy(node, {'current_price': 50.0, 'last_bar': SIGNAL_TIME}, None, None,
                                order_id=order_id)


def test_rejected_zero_fill_order_is_cleared_and_alerted(env, fake_broker):
    posted = env
    node = _node()
    order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'MARKET', 'BUY', 100)
    fake_broker.orders[order_id]['status'] = 'REJECTED'
    _seed_pending(node, order_id)

    signals_notify.check_market_buy_rejected()

    assert signals_db.get_pending_buy_by_wl_id(node['id']) is None, \
        "a genuinely zero-fill rejected order's row must be cleared"
    events = signals_db.get_coverage_events(scenario_key='market_buy_order_terminated')
    assert any(e['ticker'] == TICKER and e['result'] == 'cleared' and 'REJECTED' in (e['detail'] or '')
               for e in events)
    assert any('REJECTED' in m and TICKER in m for m in posted)


def test_canceled_after_partial_fill_preserves_row_not_cleared(env, fake_broker):
    """The HIGH finding this rebuild fixes: a partially-filled order that's
    later CANCELED must NOT be treated as zero-fill -- real shares may be
    unprotected. Constructs the real Schwab-shaped partial-execution detail
    directly on the fake_broker order dict (fake_broker has no built-in
    partial-fill primitive; direct mutation mirrors this repo's existing
    convention for constructing terminal-status test states, e.g. setting
    ['status'] = 'REJECTED' directly elsewhere in this test suite)."""
    posted = env
    node = _node()
    order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'MARKET', 'BUY', 100)
    fake_broker.orders[order_id]['status'] = 'CANCELED'
    fake_broker.orders[order_id]['orderActivityCollection'] = [
        {'executionLegs': [{'price': 50.10, 'quantity': 40}]}
    ]
    _seed_pending(node, order_id)

    signals_notify.check_market_buy_rejected()

    assert signals_db.get_pending_buy_by_wl_id(node['id']) is not None, \
        "a partially-filled-then-CANCELED order's row must be PRESERVED, never cleared"
    events = signals_db.get_coverage_events(scenario_key='market_buy_order_terminated')
    assert any(e['ticker'] == TICKER and e['result'] == 'partial_fill_preserved'
               and 'executed=40' in (e['detail'] or '') for e in events)
    assert any('CANCELED' in m and 'partially execut' in m.lower() and 'PRESERVED' in m for m in posted), \
        "expected an explicit partial-fill alert, not a false 'no position resulted' claim"


def test_still_working_order_is_left_alone(env, fake_broker):
    posted = env
    node = _node()
    order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'MARKET', 'BUY', 100)
    _seed_pending(node, order_id)  # status defaults to WORKING

    signals_notify.check_market_buy_rejected()

    assert signals_db.get_pending_buy_by_wl_id(node['id']) is not None
    assert not posted


def test_unconfirmable_status_fails_closed_then_alerts_after_threshold(env, fake_broker, monkeypatch):
    """get_order_detail returning None (unrecognized account, aged-out order
    id, network error) must not be misread as terminal-bad -- but per the
    contextual review's MEDIUM finding, a PERMANENTLY unconfirmable row must
    eventually alert rather than stay silent forever."""
    posted = env
    node = _node()
    order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'MARKET', 'BUY', 100)
    _seed_pending(node, order_id)

    import schwab_client
    monkeypatch.setattr(schwab_client, 'get_order_detail', lambda account, oid: None)

    signals_notify.check_market_buy_rejected()
    assert signals_db.get_pending_buy_by_wl_id(node['id']) is not None
    assert not posted, "no alert on the first unconfirmed poll"

    t0 = signals_notify._MARKET_BUY_STATUS_UNKNOWN_SINCE[node['id']]
    monkeypatch.setattr(signals_notify.time, 'time',
                         lambda: t0 + signals_notify._MARKET_BUY_UNKNOWN_ALERT_AFTER_SECS + 1)

    signals_notify.check_market_buy_rejected()
    assert signals_db.get_pending_buy_by_wl_id(node['id']) is not None, \
        "still preserved -- unconfirmable is not the same as confirmed-bad"
    assert any('unconfirmable' in m.lower() for m in posted)


def test_trailing_buy_node_is_skipped(env, fake_broker):
    """A trailing-buy node's pending row is check_entry_abandon's population,
    not this one -- must be left untouched even if its order comes back
    REJECTED (a real trailing-buy rejection is a different, already-covered
    concern -- entry_abandon's own hold-time timeout, not a status poll)."""
    posted = env
    trailing_ticker = 'TEST_MARKET_BUY_REJECTED_TRAILING'
    schwab_safety.AUTOMATION_ENABLED_TICKERS.add(trailing_ticker)
    signals_db.add_node(trailing_ticker, 'TrailingBothZScoreBreakout', 'test', window=10,
                         take_profit=16.0, stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account='soxl_ira', starting_notional=2000)
    node = [n for n in signals_db.get_watchlist() if n['ticker'] == trailing_ticker][0]
    order_id = fake_broker.seed_resting_order('soxl_ira', trailing_ticker, 'TRAILING_STOP', 'BUY', 100,
                                                trail_offset=1.0)
    fake_broker.orders[order_id]['status'] = 'REJECTED'
    _seed_pending(node, order_id)

    signals_notify.check_market_buy_rejected()

    assert signals_db.get_pending_buy_by_wl_id(node['id']) is not None
    assert not posted


def test_ticker_out_of_automation_scope_is_skipped(env, fake_broker):
    """Gated on AUTOMATION_ENABLED_TICKERS -- an out-of-scope ticker's row
    must never be touched, matching check_auto_fills' own scope gate."""
    posted = env
    node = _node()
    schwab_safety.AUTOMATION_ENABLED_TICKERS.discard(TICKER)
    order_id = fake_broker.seed_resting_order('soxl_ira', TICKER, 'MARKET', 'BUY', 100)
    fake_broker.orders[order_id]['status'] = 'REJECTED'
    _seed_pending(node, order_id)

    signals_notify.check_market_buy_rejected()

    assert signals_db.get_pending_buy_by_wl_id(node['id']) is not None
    assert not posted
