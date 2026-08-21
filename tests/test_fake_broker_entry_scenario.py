"""fake_broker scenarios for the two entry-placement use cases that had zero
fake_broker coverage before 2026-07-31 (found via scripts/
fake_broker_coverage_matrix.py, built the same day after the entry-abandon
scenario found a real bug in the fixture itself): trailing-buy entry
(_attempt_automated_buy -> place_trailing_buy, the live-default strategy's
own entry mechanism) and market-buy entry (_attempt_automated_market_buy ->
place_equity_buy). Both previously only had per-function-mock tests
(tests/test_part4_entry_trigger.py) -- this drives the real notify_buy_signal
entrypoint against a stateful simulated order book instead, asserting on the
fake broker's own resulting order state.

test_market_buy_entry_fills_and_protects_with_a_real_stop is real evidence for
registry id 'market_buy_placement' (scripts/coverage_registry.py) -- it drives
the real entry point rather than calling _attempt_automated_market_buy by name
directly, so a code_path-name-match scanner would miss it; see that registry
row's check_mechanism='scenario_expectations' note for why this marker
convention exists instead of a get_coverage_events() assertion."""
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

TRAILING_TICKER = 'TEST_ENTRY_TRAILING'
MARKET_TICKER = 'TEST_ENTRY_MARKET'
IN_WINDOW_TIME = datetime(2026, 7, 29, 10, 30)


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
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_node_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TRAILING_TICKER, MARKET_TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: IN_WINDOW_TIME)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))
    monkeypatch.setattr(signals_notify, 'time', type('T', (), {'sleep': staticmethod(lambda *a: None)}))
    monkeypatch.setattr(signals_notify.cfg, 'INTERACTIVE', False)

    signals_db.ensure_tables()
    signals_db.add_node(TRAILING_TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0)
    signals_db.add_node(MARKET_TICKER, 'TrailingExitZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_pct=1.0, fixed_sl_override=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira', starting_notional=800 WHERE ticker IN (?, ?)",
                   (TRAILING_TICKER, MARKET_TICKER))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node(ticker):
    return [n for n in signals_db.get_watchlist() if n['ticker'] == ticker][0]


def _sig(ticker, price):
    return {
        'ticker': ticker, 'current_price': price, 'z_score': -2.4,
        'last_bar': IN_WINDOW_TIME, 'lower_band': price - 1.0,
        'sma': price + 2.0, 'std': 1.0, 'hurst': None, 'adf_p': None, 'window': 10,
    }


def test_trailing_buy_entry_places_a_real_resting_order(env, fake_broker):
    """The live-default strategy's own entry mechanism: a real BUY signal
    must result in a genuine TRAILING_STOP BUY order resting at the broker,
    with the pending_buys row correctly tracking its real order_id -- not
    just that place_trailing_buy was called with plausible-looking args."""
    fake_broker.set_quote(TRAILING_TICKER, last=10.15, bid=10.14, ask=10.16)
    node = _node(TRAILING_TICKER)
    sig = _sig(TRAILING_TICKER, 10.15)

    signals_notify.notify_buy_signal(node, sig)

    pending = [p for p in signals_db.get_pending_buys() if p['ticker'] == TRAILING_TICKER]
    assert len(pending) == 1
    assert pending[0]['order_placed'] == 1
    order_id = pending[0]['order_id']
    assert order_id is not None

    order = fake_broker.orders[order_id]
    assert order['status'] == 'WORKING'
    assert order['orderType'] == 'TRAILING_STOP'
    assert order['orderLegCollection'][0]['instruction'] == 'BUY'
    assert order['orderLegCollection'][0]['instrument']['symbol'] == TRAILING_TICKER

    # Regression for coverage_registry.py's 'automated_buy_execution' row:
    # before this fix, _attempt_automated_buy's success path logged nothing
    # at all -- only its two failure branches did -- so this scenario_key
    # could never show a successful outcome no matter how many real
    # automated buys succeeded live.
    placed_events = [e for e in signals_db.get_coverage_events(scenario_key='automated_buy_execution')
                      if e['ticker'] == TRAILING_TICKER and e['result'] == 'placed']
    assert len(placed_events) == 1
    assert placed_events[0]['mode'] == 'live'
    assert placed_events[0]['node_id'] == node['id']
    # See test_trailing_buy_entry_dry_run_node_logs_dry_run_mode_not_live below for the
    # regression that actually distinguishes account-only vs node-aware mode lookup --
    # this node is state='live' in a trading-enabled account, so both forms agree here.


def test_market_buy_entry_fills_and_protects_with_a_real_stop(env, fake_broker):
    """Market-buy entry: a real MARKET order fills immediately (fake_broker's
    own same-tick fill semantics, matching real same-tick market-order
    behavior), and _sync_confirm_and_protect's synchronous fast-confirm path
    must open the real position and place a genuine protective STOP -- all
    against real broker state, not mocked returns."""
    fake_broker.set_quote(MARKET_TICKER, last=20.50, bid=20.49, ask=20.51)
    node = _node(MARKET_TICKER)
    sig = _sig(MARKET_TICKER, 20.50)

    signals_notify.notify_buy_signal(node, sig)

    positions = signals_db.get_open_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos['ticker'] == MARKET_TICKER
    assert pos['entry_price'] == 20.50

    # >= 1, not exactly 1 -- market_pad_pct sizing can undersize the initial
    # fill slightly, triggering a real 2nd post-fill top-up market buy
    # (_reconcile_fill), itself a real, correct, already-covered scenario --
    # not something this test needs to assert an exact count against.
    market_orders = [o for o in fake_broker.orders.values()
                      if o['orderLegCollection'][0]['instrument']['symbol'] == MARKET_TICKER
                      and o['orderType'] == 'MARKET']
    assert len(market_orders) >= 1
    assert all(o['status'] == 'FILLED' for o in market_orders)

    stop_orders = [o for o in fake_broker.orders.values()
                    if o['orderLegCollection'][0]['instrument']['symbol'] == MARKET_TICKER
                    and o['orderType'] == 'STOP']
    assert len(stop_orders) == 1
    assert stop_orders[0]['status'] == 'WORKING'
    assert pos['sl_order_id'] == stop_orders[0]['orderId']

    # Same regression as test_trailing_buy_entry_places_a_real_resting_order
    # above, for _attempt_automated_market_buy's success path.
    placed_events = [e for e in signals_db.get_coverage_events(scenario_key='automated_buy_execution')
                      if e['ticker'] == MARKET_TICKER and e['result'] == 'placed']
    assert len(placed_events) == 1


def test_trailing_buy_entry_dry_run_node_logs_dry_run_mode_not_live(env, fake_broker):
    """2026-08-20 paired-review finding (both independent-cold and contextual agents,
    same conclusion): _coverage_mode(account) alone (no node) reads the ACCOUNT's
    trading_enabled flag, not the NODE's real state -- soxl_ira is trading-enabled, so
    a dry_run node's simulated (never-placed-at-the-broker) order would be logged
    mode='live', falsely flipping scripts/coverage_registry.py's automated_buy_execution
    row to verified-live off a simulated order. The two tests above use state='live'
    nodes, where account-only and node-aware modes happen to agree -- this is the one
    that actually distinguishes them."""
    dry_run_ticker = 'TEST_ENTRY_TRAILING_DRYRUN'
    schwab_safety.AUTOMATION_ENABLED_TICKERS.add(dry_run_ticker)
    signals_db.add_node(dry_run_ticker, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='dry_run',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira', starting_notional=800 WHERE ticker=?",
                   (dry_run_ticker,))
        c.commit()
    fake_broker.set_quote(dry_run_ticker, last=10.15, bid=10.14, ask=10.16)
    node = _node(dry_run_ticker)
    sig = _sig(dry_run_ticker, 10.15)

    signals_notify.notify_buy_signal(node, sig)

    placed_events = [e for e in signals_db.get_coverage_events(scenario_key='automated_buy_execution')
                      if e['ticker'] == dry_run_ticker and e['result'] == 'placed']
    assert len(placed_events) == 1
    assert placed_events[0]['mode'] == 'dry_run', \
        "a dry_run node's simulated order must never log mode='live'"
