"""fake_broker scenarios for the per-node dry_run override (docs/backlog_cache.md,
built 2026-08-1x after a paired Opus review of the real drought/addon canary
staging found the account-level-only dry_run model forces every mechanism
staging exercise onto its own dedicated account). Additive-only against the
account-level schwab_safety.ACCOUNTS[account].dry_run:

    real_order_allowed = (account.dry_run == False) AND (node.dry_run != True)

A node.dry_run=True node on an otherwise-REAL account (soxl_ira, dry_run=False)
must behave identically to a real account-level dry_run node -- no real order
ever lands at the broker, and every downstream consumer that decides "is there
possibly a real order to interact with" (update_dry_run_buys' fill synthesis,
check_entry_abandon's cancel decision, check_drought_handoff's cancel
decision) must reach the SAME conclusion it would for an account-level
dry_run node, not the wrong one. These tests drive the real entry points
(notify_buy_signal, update_dry_run_buys, check_entry_abandon, check_drought_
handoff, check_addon_trigger_real) against fake_broker with account='soxl_ira'
(real, dry_run=False) and node.dry_run=1 -- if the account-level `limits.
dry_run` check were still used unguarded anywhere on the path under test,
these would fail by placing/attempting a real order or by misreading the
resulting order_id=None state as an untracked manual order."""
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
import signals_compute
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_NODE_DRY_RUN_SCENARIO'
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
    monkeypatch.setattr(schwab_safety, 'NODE_BREAKER_PATH', tmp_path / "schwab_node_breaker_state.json")
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_node_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: IN_WINDOW_TIME)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))
    import time as _real_time
    monkeypatch.setattr(signals_notify, 'time', type('T', (), {'sleep': staticmethod(lambda *a: None),
                                                                 'time': staticmethod(_real_time.time)}))
    monkeypatch.setattr(signals_notify.cfg, 'INTERACTIVE', False)

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account='soxl_ira', starting_notional=2000)

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _sig(price):
    return {'ticker': TICKER, 'current_price': price, 'z_score': -2.4, 'last_bar': IN_WINDOW_TIME,
            'lower_band': price - 1.0, 'sma': price + 2.0, 'std': 1.0, 'hurst': None, 'adf_p': None,
            'window': 10}


def _real_orders(fake_broker_, ticker=TICKER, side=None):
    out = []
    for o in fake_broker_.orders.values():
        leg = o['orderLegCollection'][0]
        if leg['instrument']['symbol'] != ticker:
            continue
        if side is not None and leg['instruction'] != side:
            continue
        out.append(o)
    return out


def test_node_dry_run_places_no_real_order_on_a_real_account(env, fake_broker):
    """The core assertion this whole mechanism lives or dies on: soxl_ira is
    genuinely dry_run=False, so without the node-level override this would be
    a real order. Confirms schwab_safety.approve_and_record's OR-logic
    actually reaches place_trailing_buy's short-circuit."""
    signals_db.set_node_state(_node()['id'], 'dry_run')
    node = _node()
    assert node['state'] == 'dry_run'
    fake_broker.set_quote(TICKER, last=10.15, bid=10.14, ask=10.16)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    signals_notify.notify_buy_signal(node, _sig(10.15))

    assert len(_real_orders(fake_broker)) == 0, "node.dry_run=1 must place NO real order, even on a real account"
    pending = signals_db.get_pending_buys()
    assert len(pending) == 1 and pending[0]['order_placed'] == 1 and pending[0]['order_id'] is None


def test_node_dry_run_false_on_real_account_places_a_real_order(env, fake_broker):
    """Regression control for the test above -- the default (node.dry_run=0,
    unset) on the same real account must place a genuine real order, proving
    the OR-logic addition didn't silently widen simulation to everything."""
    node = _node()
    assert node['state'] != 'dry_run'
    fake_broker.set_quote(TICKER, last=10.15, bid=10.14, ask=10.16)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    signals_notify.notify_buy_signal(node, _sig(10.15))

    orders = _real_orders(fake_broker, side='BUY')
    assert len(orders) == 1 and orders[0]['status'] == 'WORKING'
    pending = signals_db.get_pending_buys()
    assert pending[0]['order_id'] == orders[0]['orderId']


def test_node_dry_run_pending_buy_synthesized_by_update_dry_run_buys(env, fake_broker, monkeypatch):
    """The exact stuck-forever bug the paired review found: update_dry_run_
    buys previously gated ONLY on limits.dry_run, so a node-forced-dry-run
    entry on a real account would place no real order (correct) but also
    never get picked up by the synthesis poller (bug) -- the pending row
    would sit forever with order_placed=1, order_id=None, no fill ever
    confirmed. Must now synthesize exactly like an account-level dry_run
    node does."""
    signals_db.set_node_state(_node()['id'], 'dry_run')
    node = _node()
    fake_broker.set_quote(TICKER, last=10.15, bid=10.14, ask=10.16)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    signals_notify.notify_buy_signal(node, _sig(10.15))
    assert len(_real_orders(fake_broker)) == 0

    monkeypatch.setattr(signals_compute, '_current_price', lambda t: (11.0, None) if t == TICKER else (None, None))
    signals_notify.update_dry_run_buys()

    pos = signals_db.get_open_position(TICKER)
    assert pos is not None, "node-forced-dry-run pending buy must be synthesized, not left stuck forever"
    assert pos['is_dry_run_sim'] == 1
    assert signals_db.get_pending_buys() == []


def test_node_dry_run_entry_abandon_does_not_false_alarm_manual_order(env, fake_broker, monkeypatch):
    """check_entry_abandon's own bug shape: `not limits.dry_run` alone reads
    a node-forced-dry-run's order_id=None as the manual "placed at the broker
    directly, no id captured" case and refuses to auto-clear it, alerting a
    false "cannot auto-cancel, placed manually" warning for an entry that was
    never manual at all -- it was correctly automated and correctly
    simulated. Must instead treat it exactly like the true dry_run branch:
    safe to clear, nothing real ever rested at the broker."""
    signals_db.set_node_state(_node()['id'], 'dry_run')
    node = _node()
    fake_broker.set_quote(TICKER, last=10.15, bid=10.14, ask=10.16)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    signals_notify.notify_buy_signal(node, _sig(10.15))
    pending = signals_db.get_pending_buys()[0]
    assert pending['order_placed'] == 1 and pending['order_id'] is None

    monkeypatch.setattr(signals_compute, '_load_cache', lambda t: (object(), None))
    monkeypatch.setattr(signals_compute, '_bars_held', lambda df, t: 10_000)  # force past hold-time limit
    signals_notify.check_entry_abandon()

    events = signals_db.get_coverage_events(scenario_key='entry_abandon_timeout')
    assert not any(e['result'] == 'no_order_id_on_file' for e in events), (
        "must not misread a node-forced-dry-run entry as an untracked manual order"
    )


def test_node_dry_run_addon_leg_synthesizes_via_inherited_is_dry_run_sim(env, fake_broker, monkeypatch):
    """End-to-end composition proof: check_addon_trigger_real's own is_dry_
    run_sim branch is entirely unmodified by this fix -- it already fires
    correctly for a node-forced-dry-run parent, PROVIDED the parent position
    itself actually got tagged is_dry_run_sim=1 by update_dry_run_buys' now-
    fixed synthesis. This proves the composition holds end to end: node.
    dry_run=1 -> core entry synthesizes with is_dry_run_sim=1 -> arm ->
    add-on leg synthesizes too, no real order anywhere in the chain."""
    signals_db.set_node_state(_node()['id'], 'dry_run')
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET addon_enabled=1 WHERE ticker=?", (TICKER,))
        c.commit()
    node = _node()
    fake_broker.set_quote(TICKER, last=10.15, bid=10.14, ask=10.16)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    fake_broker.set_buying_power('soxl_ira', 1_000_000.0)
    signals_notify.notify_buy_signal(node, _sig(10.15))
    monkeypatch.setattr(signals_compute, '_current_price', lambda t: (11.0, None) if t == TICKER else (None, None))
    signals_notify.update_dry_run_buys()
    pos = signals_db.get_open_position(TICKER)
    assert pos is not None and pos['is_dry_run_sim'] == 1

    signals_db.update_position_trail_state(pos['id'], {'trailing': True, 'peak': 11.0})
    pos = signals_db.get_open_position(TICKER)
    signals_notify.check_addon_trigger_real(pos, current_price=11.0)

    assert len(_real_orders(fake_broker, side='BUY')) == 0, "no real order for the leg either"
    leg = signals_db.get_open_addon_leg_by_parent(pos['id'])
    assert leg is not None and leg['is_dry_run_sim'] == 1 and leg['entry_status'] == 'filled'


def test_node_dry_run_drought_handoff_cancel_treats_as_nothing_real_to_cancel(env, fake_broker, monkeypatch):
    """check_drought_handoff's Case A has the identical bug shape as entry-
    abandon -- `not limits.dry_run` alone would attempt a real cancel_order
    call against a broker order_id that never existed (order_id is None from
    the node-forced-dry-run synthesis), which schwab_client.cancel_order
    would mishandle (no real order_id to cancel). Must resolve via the
    'nothing real to cancel, safe to clear' branch instead."""
    signals_db.set_node_state(_node()['id'], 'dry_run')
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET drought_overlay_enabled=1, drought_confirm_days=3 WHERE ticker=?",
                   (TICKER,))
        c.commit()
    node = _node()
    fake_broker.set_quote(TICKER, last=50.0, bid=49.99, ask=50.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    _decision = {'price': 50.0, 'shares': 100, 'confirm_days': 3, 'vol_gate': None,
                 'vol_pctile': None, 'gap_start': '2026-07-20 09:30:00'}
    import paper_trading
    monkeypatch.setattr(paper_trading, 'evaluate_drought_entry', lambda n, paper=False: dict(_decision))
    signals_notify.check_drought_entry(node)
    assert len(_real_orders(fake_broker, side='BUY')) == 0
    pending = signals_db.get_drought_pending_buy(node['id'])
    assert pending is not None and pending['order_id'] is None

    monkeypatch.setattr('signals_compute.compute_buy_signal',
                         lambda n: {'current_price': 51.0, 'signal': 'BUY', 'last_bar': IN_WINDOW_TIME})
    signals_notify.check_drought_handoff(node)

    events = signals_db.get_coverage_events(scenario_key='drought_handoff_cancel')
    assert any(e['result'] == 'cancelled_resting_entry_no_real_order' for e in events), (
        "must resolve via the safe-to-clear branch, never attempt a real cancel_order call"
    )
    assert signals_db.get_drought_pending_buy(node['id']) is None
