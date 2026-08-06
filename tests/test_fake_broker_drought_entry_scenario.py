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
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: IN_WINDOW_TIME)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, mode='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account='soxl_ira', starting_notional=2000)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET drought_overlay_enabled=1, drought_confirm_days=3 WHERE ticker=?",
                   (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


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


def test_drought_entry_is_a_noop_for_a_research_mode_node(env, fake_broker, monkeypatch):
    """Regression assertion: an identical research-mode node must place NO
    real order at all -- mode-symmetry with check_paper_drought_entry."""
    monkeypatch.setattr(paper_trading, 'evaluate_drought_entry', lambda node, paper=False: dict(_DECISION))
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET mode='research' WHERE ticker=?", (TICKER,))
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
