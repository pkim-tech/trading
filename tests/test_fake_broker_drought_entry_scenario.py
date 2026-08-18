"""fake_broker scenarios for real drought-overlay entry (signals_notify.
check_drought_entry/notify_drought_buy_signal), Part 4 of docs/plans/
real_order_execution_drought_addon.md.

paper_trading.evaluate_drought_entry's own eligibility logic (checkpoint-bar
counting, once-per-gap dedup, vol gate) is already exercised by
tests/test_overlay_paper_trading.py against the SAME shared function real
code now calls too -- these tests focus on what's actually NEW here: real
order placement/dispatch once a decision is eligible, which is why
evaluate_drought_entry is monkeypatched to a canned decision rather than
re-proving its own eligibility logic against real cached price data.

Regression assertion: an identical mode='research' node must place NO
broker order at all (check_drought_entry's mode gate)."""
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
import paper_trading
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_DROUGHT_ENTRY_SCENARIO'
MARKET_TICKER = 'TEST_DROUGHT_ENTRY_MARKET_SCENARIO'
IN_WINDOW_TIME = datetime(2026, 7, 29, 10, 30)

_DECISION = {'price': 50.0, 'shares': 100, 'confirm_days': 3, 'vol_gate': None,
             'vol_pctile': None, 'gap_start': '2026-07-20 09:30:00'}


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
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER, MARKET_TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: IN_WINDOW_TIME)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))
    # _sync_confirm_and_protect's fast-confirm retry loop (market-buy drought
    # entries only -- trailing-buy drought entries never reach this) sleeps
    # between polls; fake_broker fills a MARKET order on the same tick it's
    # placed, so the real fill is always found on attempt 1, but patch it out
    # anyway to match test_fake_broker_entry_scenario.py's convention and
    # guarantee this can never introduce real wall-clock delay.
    monkeypatch.setattr(signals_notify, 'time', type('T', (), {'sleep': staticmethod(lambda *a: None)}))
    monkeypatch.setattr(signals_notify.cfg, 'INTERACTIVE', False)

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account='soxl_ira', starting_notional=2000)
    signals_db.add_node(MARKET_TICKER, 'TrailingExitZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_pct=1.0, fixed_sl_override=1.0,
                         account='soxl_ira', starting_notional=2000)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET drought_overlay_enabled=1, drought_confirm_days=3 WHERE ticker IN (?, ?)",
                   (TICKER, MARKET_TICKER))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _market_node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == MARKET_TICKER][0]


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _real_orders(fake_broker_, ticker, side=None):
    out = []
    for o in fake_broker_.orders.values():
        leg = o['orderLegCollection'][0]
        if leg['instrument']['symbol'] != ticker:
            continue
        if side is not None and leg['instruction'] != side:
            continue
        out.append(o)
    return out


def test_drought_entry_places_real_trailing_buy_for_trailingboth_node(env, fake_broker, monkeypatch):
    monkeypatch.setattr(paper_trading, 'evaluate_drought_entry', lambda node, paper=False: dict(_DECISION))
    fake_broker.set_quote(TICKER, last=50.0, bid=49.99, ask=50.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    node = _node()

    signals_notify.check_drought_entry(node)

    orders = _real_orders(fake_broker, TICKER, side='BUY')
    assert len(orders) == 1, "a real trailing-buy order must be placed for an eligible drought entry"
    assert orders[0]['orderType'] == 'TRAILING_STOP'

    pending = signals_db.get_drought_pending_buy(node['id'])
    assert pending is not None
    assert pending['position_source'] == 'drought_overlay'
    assert pending['drought_confirm_days'] == 3
    assert pending['order_placed'] == 1
    events = signals_db.get_coverage_events(scenario_key='drought_entry_placement')
    assert any(e['ticker'] == TICKER and e['result'] == 'signalled' for e in events)
    # registry id 'drought_entry' -- the decision-layer event (2026-08-13 fix:
    # the real path only ever logged drought_entry_placement, never the
    # once-per-gap dedup guard's own decision event paper's caller logs).
    decision_events = signals_db.get_coverage_events(scenario_key='drought_entry')
    assert any(e['ticker'] == TICKER and e['mode'] != 'paper' and e['result'] == 'signalled'
               for e in decision_events)


def test_drought_entry_respects_capital_at_stake_alert_gate(env, fake_broker, monkeypatch):
    """2026-08-09 paired review finding: notify_drought_buy_signal called
    _post_message unconditionally, missing the should_alert_live gate every
    other real BUY alert (notify_buy_signal) already has -- found because it
    directly affects 11 real soxl_ira nodes ($500-$2,500, all below the
    capital-at-stake bar) and 3 dry_run brokerage canary-drought nodes.
    Tracking (pending_buy, coverage_events, real order placement) must stay
    unconditional -- only the Slack post itself should be gated."""
    monkeypatch.setattr(paper_trading, 'evaluate_drought_entry', lambda node, paper=False: dict(_DECISION))
    fake_broker.set_quote(TICKER, last=50.0, bid=49.99, ask=50.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    posts = []
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda *a, **kw: (posts.append(a), (None, None))[1])
    node = _node()  # starting_notional=2000, real soxl_ira -- sub-capital-at-stake

    signals_notify.check_drought_entry(node)

    assert posts == [], "sub-capital-at-stake node must get zero real-time Slack post"
    # Tracking still fires regardless of the alert gate.
    orders = _real_orders(fake_broker, TICKER, side='BUY')
    assert len(orders) == 1
    pending = signals_db.get_drought_pending_buy(node['id'])
    assert pending is not None and pending['order_placed'] == 1


def test_drought_entry_alerts_for_a_capital_at_stake_node(env, fake_broker, monkeypatch):
    """Mirror of the gate test above -- a node crossing the capital-at-stake
    bar must still get its real-time Slack post."""
    monkeypatch.setattr(paper_trading, 'evaluate_drought_entry', lambda node, paper=False: dict(_DECISION))
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET starting_notional=50000 WHERE ticker=?", (TICKER,))
        c.commit()
    fake_broker.set_quote(TICKER, last=50.0, bid=49.99, ask=50.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    posts = []
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda *a, **kw: (posts.append(a), ('C1', '1'))[1])
    node = _node()

    signals_notify.check_drought_entry(node)

    assert len(posts) == 1, "a capital-at-stake node must still get its real-time drought entry alert"
    assert 'DROUGHT ENTRY SIGNAL' in posts[0][0]


def test_drought_entry_is_a_noop_for_a_research_mode_node(env, fake_broker, monkeypatch):
    """Regression assertion: an identical research-mode node must place NO
    real order at all -- mode-symmetry with check_paper_drought_entry."""
    monkeypatch.setattr(paper_trading, 'evaluate_drought_entry', lambda node, paper=False: dict(_DECISION))
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET state='paper' WHERE ticker=?", (TICKER,))
        c.commit()
    fake_broker.set_quote(TICKER, last=50.0, bid=49.99, ask=50.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    node = _node()

    signals_notify.check_drought_entry(node)

    assert len(_real_orders(fake_broker, TICKER)) == 0
    assert signals_db.get_drought_pending_buy(node['id']) is None


def test_drought_entry_is_a_noop_when_evaluate_drought_entry_returns_none(env, fake_broker, monkeypatch):
    monkeypatch.setattr(paper_trading, 'evaluate_drought_entry', lambda node, paper=False: None)
    fake_broker.set_quote(TICKER, last=50.0, bid=49.99, ask=50.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    node = _node()

    signals_notify.check_drought_entry(node)

    assert len(_real_orders(fake_broker, TICKER)) == 0
    assert signals_db.get_drought_pending_buy(node['id']) is None


def test_drought_pending_buy_fill_opens_a_drought_overlay_position_not_core(env, fake_broker, monkeypatch):
    """Threads position_source through the fill dispatch (Part 4.3) --
    signals_db.open_position_from_pending, not the plain open_position default."""
    monkeypatch.setattr(paper_trading, 'evaluate_drought_entry', lambda node, paper=False: dict(_DECISION))
    fake_broker.set_quote(TICKER, last=50.0, bid=49.99, ask=50.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    node = _node()
    signals_notify.check_drought_entry(node)
    orders = _real_orders(fake_broker, TICKER, side='BUY')
    order_id = orders[0]['orderId']

    fake_broker.force_fill(order_id, price=50.5)
    signals_notify._reconcile_buy_fill(TICKER, 50.5, 100, wl_id=node['id'])

    pos = signals_db.get_open_position(TICKER)
    assert pos is not None
    assert pos['position_source'] == 'drought_overlay'
    assert pos['drought_confirm_days'] == 3
    assert signals_db.get_drought_pending_buy(node['id']) is None


def test_drought_pending_buy_fill_resolves_pct_overrides_onto_the_real_position(env, fake_broker, monkeypatch):
    """2026-08-18 fix: the real fill path (this test's own dispatch chain --
    check_drought_entry -> _reconcile_buy_fill -> open_position_from_pending)
    used to call open_position() directly for a drought_overlay fill, which
    never read drought_sl_pct_override/drought_arm_pct_override/
    drought_trail_pct_override at all -- only paper_trading.py's call to
    open_drought_overlay_position exercised that resolution logic. Latent
    (zero live nodes set these columns as of the fix), but sits on the real
    SL/arm/trailing-stop TRIGGER PERCENTAGE path, not just position sizing.
    Sets drought_sl_pct_override to a value DISTINCT from the node's own
    fixed_sl (1.0, from fixed_sl_override in the env fixture) so a silent
    fall-through to the node's core default would be caught."""
    monkeypatch.setattr(paper_trading, 'evaluate_drought_entry', lambda node, paper=False: dict(_DECISION))
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET drought_sl_pct_override=2.5 WHERE ticker=?", (TICKER,))
        c.commit()
    fake_broker.set_quote(TICKER, last=50.0, bid=49.99, ask=50.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    node = _node()
    assert node['fixed_sl'] == 1.0  # sanity: override is distinct from the node's own default
    signals_notify.check_drought_entry(node)
    orders = _real_orders(fake_broker, TICKER, side='BUY')
    order_id = orders[0]['orderId']

    fake_broker.force_fill(order_id, price=50.5)
    signals_notify._reconcile_buy_fill(TICKER, 50.5, 100, wl_id=node['id'])

    pos = signals_db.get_open_position(TICKER)
    assert pos is not None
    assert pos['position_source'] == 'drought_overlay'
    assert pos['fixed_sl'] == 2.5, "drought_sl_pct_override was not resolved onto the real fill's SL trigger"


def test_drought_entry_places_real_market_buy_for_trailingexit_node(env, fake_broker, monkeypatch):
    """Market-buy variant of the trailing-buy test above -- notify_drought_
    buy_signal dispatches on db._is_trailing_buy(node), so a TrailingExitZScoreBreakout
    drought node must go through _attempt_automated_market_buy/_sync_confirm_and_protect
    instead, filling immediately (fake_broker's same-tick MARKET semantics) and
    opening a drought_overlay position with a real resting STOP -- all in one
    check_drought_entry call, unlike the trailing-buy path which stays pending."""
    monkeypatch.setattr(paper_trading, 'evaluate_drought_entry', lambda node, paper=False: dict(_DECISION))
    fake_broker.set_quote(MARKET_TICKER, last=50.0, bid=49.99, ask=50.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    node = _market_node()

    signals_notify.check_drought_entry(node)

    market_orders = _real_orders(fake_broker, MARKET_TICKER, side='BUY')
    assert len(market_orders) >= 1
    assert all(o['orderType'] == 'MARKET' and o['status'] == 'FILLED' for o in market_orders)

    pos = signals_db.get_open_position(MARKET_TICKER)
    assert pos is not None
    assert pos['position_source'] == 'drought_overlay'
    assert pos['drought_confirm_days'] == 3

    stop_orders = [o for o in fake_broker.orders.values()
                    if o['orderLegCollection'][0]['instrument']['symbol'] == MARKET_TICKER
                    and o['orderType'] == 'STOP']
    assert len(stop_orders) == 1
    assert stop_orders[0]['status'] == 'WORKING'
    assert pos['sl_order_id'] == stop_orders[0]['orderId']
    assert signals_db.get_drought_pending_buy(node['id']) is None
