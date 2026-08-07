"""Real-order mirror of tests/test_overlay_paper_trading.py's truth table
(lines 539-670) -- Part 9 item 5 of docs/plans/real_order_execution_drought_
addon.md, the one file from that plan's fake_broker list not written in the
session that shipped the real drought/addon code. Paper's truth table proves
the state-machine invariants against synchronous DB writes; this proves the
SAME invariants hold against the real order-placement call paths (check_
drought_handoff, check_addon_trigger_real, close_addon_leg_real_if_open),
which paper never exercises at all.

The same 5 reachable (core, drought, addon) states as the paper file:
  1. (flat, none, none)
  2. (flat, open drought, none)
  3. (open-unarmed core, none, none)
  4. (armed core, none, none)             -- addon_enabled=0, or not yet triggered
  5. (armed core, none, open addon leg)
(drought+core) and (drought+addon) are structurally unreachable -- tested
explicitly, not just assumed."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import signals_config
import signals_db as db
import signals_notify
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_OVERLAY_TRUTH_TABLE_SCENARIO'
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
    monkeypatch.setattr(signals_notify, 'time', type('T', (), {'sleep': staticmethod(lambda *a: None)}))
    monkeypatch.setattr(signals_notify.cfg, 'INTERACTIVE', False)

    db.ensure_tables()
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                stop_loss=1, max_hold_hours=105, state='live',
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                account='soxl_ira', starting_notional=2000)
    with db._conn() as c:
        c.execute("UPDATE watch_list SET drought_overlay_enabled=1, drought_confirm_days=3 WHERE ticker=?",
                   (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]


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


def _open_core_position(node, shares=20, entry_price=50.0):
    now = datetime.now()
    db.open_position(node, signal_price=entry_price, signal_time=now, entry_price=entry_price,
                      entry_time=now, shares=shares)
    with db._conn() as c:
        c.execute("UPDATE open_positions SET account='soxl_ira' WHERE ticker=?", (node['ticker'],))
        c.commit()
    return db.get_open_position(TICKER)


def test_truth_table_core_and_drought_are_mutually_exclusive_real(env, fake_broker):
    """Real mirror of test_overlay_paper_trading.py's identical-named test --
    open_position()/open_drought_overlay_position's wl_id dedup is real-DB
    code shared with paper, but never exercised against mode='live' rows by
    the paper file at all."""
    node = _node()
    now = datetime.now()
    db.open_position(node, 100.0, now, 100.0, now, shares=10)
    opened = db.open_drought_overlay_position(node, 50.0, now, 50.0, now, confirm_days=3)
    assert opened is False, "core open -- a drought entry for the same wl_id must be rejected as a duplicate"

    core_pos = db.get_open_position(TICKER)
    db.close_position(core_pos['id'], exit_signal_price=100.0, exit_price=100.0, exit_time=now,
                       exit_reason='TIME')

    opened = db.open_drought_overlay_position(node, 50.0, now, 50.0, now, confirm_days=3)
    assert opened is True
    reopened_core = db.open_position(node, 100.0, now, 100.0, now, shares=10)
    assert reopened_core is False, "drought open -- a fresh core entry for the same wl_id must be rejected too"


def test_truth_table_addon_never_attempted_against_a_real_drought_position(env, fake_broker):
    """Real mirror -- (drought_open, addon_open) is unreachable because
    check_addon_trigger_real gates on position_source=='core' specifically
    (signals_notify.py:2025-2026), proven directly against the real function
    (not paper's check_paper_addon_trigger) with a real broker available to
    place an order into, so an empty result is proof of the guard, not an
    accident of nothing being wired up."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    fake_broker.set_buying_power('soxl_ira', 1_000_000.0)
    with db._conn() as c:
        c.execute("UPDATE watch_list SET addon_enabled=1 WHERE ticker=?", (TICKER,))
        c.commit()
    node = _node()
    now = datetime.now()
    db.open_drought_overlay_position(node, 50.0, now, 50.0, now, confirm_days=3, shares=20)
    with db._conn() as c:
        c.execute("UPDATE open_positions SET account='soxl_ira' WHERE ticker=?", (TICKER,))
        c.commit()
    dpos = db.get_open_position(TICKER)
    assert dpos['position_source'] == 'drought_overlay'
    db.update_position_trail_state(dpos['id'], {'trailing': True, 'peak': 50.0})
    dpos = db.get_open_position(TICKER)

    signals_notify.check_addon_trigger_real(dpos, current_price=55.0)

    assert len(_real_orders(fake_broker, TICKER, side='BUY')) == 0
    assert db.get_open_addon_leg_by_parent(dpos['id']) is None


def test_truth_table_all_five_reachable_states_are_constructible_real(env, fake_broker):
    """Real mirror -- each of the 5 real reachable states must actually be
    buildable via the real functions (open_position, open_drought_overlay_
    position, update_position_trail_state, open_addon_leg, close_position,
    close_addon_leg_real_if_open), not just theoretically possible."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    now = datetime.now()

    # 1. (flat, none, none)
    assert db.get_open_position(TICKER) is None

    # 2. (flat, open drought, none)
    db.open_drought_overlay_position(node, 50.0, now, 50.0, now, confirm_days=3, shares=10)
    with db._conn() as c:
        c.execute("UPDATE open_positions SET account='soxl_ira' WHERE ticker=?", (TICKER,))
        c.commit()
    dpos = db.get_open_position(TICKER)
    assert dpos is not None and dpos['position_source'] == 'drought_overlay'
    db.close_position(dpos['id'], exit_signal_price=55.0, exit_price=55.0, exit_time=now, exit_reason='HANDOFF')
    assert db.get_open_position(TICKER) is None

    # 3. (open-unarmed core, none, none)
    core_pos = _open_core_position(node, shares=10, entry_price=100.0)
    assert core_pos is not None and not core_pos['trail_state']

    # 4. (armed core, none, none) -- addon_enabled=0 on this node by default
    db.update_position_trail_state(core_pos['id'], {'trailing': True, 'peak': 105.0})
    armed_pos = db.get_open_position(TICKER)
    assert armed_pos['trail_state'].get('trailing') is True
    assert db.get_open_addon_leg_by_parent(core_pos['id']) is None

    # 5. (armed core, none, open addon leg) -- addon_enabled=1 now, real leg
    with db._conn() as c:
        c.execute("UPDATE watch_list SET addon_enabled=1 WHERE ticker=?", (TICKER,))
        c.commit()
    fake_broker.set_buying_power('soxl_ira', 1_000_000.0)
    signals_notify.check_addon_trigger_real(armed_pos, current_price=105.0)
    leg = db.get_open_addon_leg_by_parent(core_pos['id'])
    assert leg is not None, "a real add-on leg must be constructible against an armed core position"

    signals_notify.close_addon_leg_real_if_open(armed_pos, exit_price=110.0, exit_reason='TRAIL',
                                                 exit_time=now)
    assert db.get_open_addon_leg_by_parent(core_pos['id']) is None
    db.close_position(core_pos['id'], exit_signal_price=110.0, exit_price=110.0, exit_time=now,
                       exit_reason='TRAIL')
    assert db.get_open_position(TICKER) is None


def test_truth_table_addon_leg_real_exit_never_desyncs_from_parent_core_exit(env, fake_broker):
    """Real mirror of the paper file's identical-named test -- close_addon_
    leg_real_if_open never independently evaluates any exit condition; it's
    always called with the SAME reason/price the parent's own real exit
    already determined, so desync is structurally impossible. Proven by
    driving 3 different (reason, price) pairs through the real function and
    confirming each closes the leg at exactly that reason/price -- not a
    reason/price it computed on its own."""
    node = _node()
    fake_broker.set_quote(TICKER, last=52.0, bid=51.99, ask=52.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    pos = _open_core_position(node, shares=10, entry_price=100.0)
    leg_id = db.open_addon_leg(pos, shares=10, entry_price=100.0, entry_time=datetime.now(),
                               paper=False, entry_status='filled')

    for reason, price in (('SL', 97.0), ('TRAIL', 112.0), ('TIME', 101.5)):
        with db._conn() as c:
            c.execute("UPDATE addon_legs SET status='open', exit_reason=NULL, sl_order_id=NULL WHERE id=?",
                       (leg_id,))
            c.commit()
        # A real MARKET SELL fills at the broker's current quote, not the
        # price argument -- set it to match so the fill price under test is
        # unambiguous (place_equity_sell has no resting sl_order_id here, so
        # this always takes the fresh-MARKET-order branch, never REPLACE).
        fake_broker.set_quote(TICKER, last=price, bid=price - 0.01, ask=price + 0.01)
        signals_notify.close_addon_leg_real_if_open(pos, exit_price=price, exit_reason=reason,
                                                     exit_time=datetime.now())
        with db._conn() as c:
            leg = c.execute("SELECT exit_reason, exit_price FROM addon_legs WHERE id=?", (leg_id,)).fetchone()
        assert leg['exit_reason'] == reason
        assert leg['exit_price'] == price


def test_truth_table_handoff_then_core_entry_same_poll_composes_correctly_real(env, fake_broker, monkeypatch):
    """Real mirror -- the documented ordering contract (HANDOFF runs BEFORE
    the core buy-scan in the same poll) resolves the real race the same way
    paper's synchronous version does, proven here against check_drought_
    handoff (the real function, with a real cancel/exit path) instead of
    check_paper_drought_handoff's plain DB write."""
    node = _node()
    fake_broker.set_quote(TICKER, last=60.0, bid=59.99, ask=60.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)
    now = datetime.now()
    db.open_drought_overlay_position(node, 50.0, now, 50.0, now, confirm_days=3, shares=10)
    with db._conn() as c:
        c.execute("UPDATE open_positions SET account='soxl_ira' WHERE ticker=?", (TICKER,))
        c.commit()
    assert db.get_open_position(TICKER) is not None

    monkeypatch.setattr('signals_compute.compute_buy_signal',
                         lambda n, **kw: {'current_price': 60.0, 'signal': 'BUY', 'last_bar': IN_WINDOW_TIME})
    signals_notify.check_drought_handoff(node)  # runs first, per the documented ordering contract
    assert db.get_open_position(TICKER) is None, "HANDOFF must fully clear the wl_id slot before core's own entry"

    opened = db.open_position(node, 60.0, now, 60.0, now, shares=5)
    assert opened is True, "core's own entry, right after HANDOFF in the same poll, must succeed"
