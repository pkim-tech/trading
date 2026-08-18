"""Regression test for the 2026-08-17 RETL incident (wl_id=143, soxl_ira):
_PENDING_BUY_NODE_KEYS (signals_db.py) did not include
'starting_notional_override', so a node's real override was silently dropped
from the pending_buys.node_json snapshot taken when a trailing-buy order was
placed. Once that order rested past the poll cycle it was placed in (RETL's
sat 3 days, 2026-08-14 -> 2026-08-17), _reconcile_buy_fill/_reconcile_fill
used the stale snapshot (pending['node']) instead of a fresh watch_list read,
so _last_sale_recovery(node) fell through to the trade_log-based recovery
basis instead of returning the override directly -- silently sizing (or in
RETL's real case, skipping) the post-fill top-up against the wrong target
notional, with zero coverage_events trace.

Reproduces the exact RETL numbers: override target=$400, fill=42sh@$9.0487
(real notional $380.05, shortfall $19.95 -> expects a 2-share top-up). Before
the fix (override excluded from the snapshot), delta computed against the
stale trade_log-based recovery (~$376.43) instead came out negative-but-
inside-tolerance, and _reconcile_fill did nothing at all -- no top-up order,
no overspend alert, no coverage_event of any kind. Uses the fake_broker
harness (see docs/design.md's Test Fixtures & Coverage-Proof Techniques
table) since this exercises real production order-placement code
(_reconcile_buy_fill -> _reconcile_fill -> place_equity_buy), matching the
pattern in test_fake_broker_topup_scenario.py."""
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

TICKER = 'TEST_OVERRIDE_SNAPSHOT_SCENARIO'


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
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    # in-window, matching the real ordinary in-window fill case.
    monkeypatch.setattr(schwab_safety, '_now', lambda: datetime(2026, 8, 17, 10, 30))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira', starting_notional=800 WHERE ticker=?",
                   (TICKER,))
        c.commit()
    # RETL's real override: $400 target, deliberately different from
    # starting_notional (800) and from any trade_log-derived recovery basis
    # so the two paths can't accidentally agree.
    signals_db.set_starting_notional_override(_node_id(), 400.0)

    # a prior closed trade at a DIFFERENT recovery basis than the override --
    # mirrors RETL's real trade_log id=97 (exit_price=9.1813, shares=41 ->
    # recovery=$376.43), which is what the bug fell through to.
    with signals_db._conn() as c:
        c.execute(
            "INSERT INTO trade_log (ticker, strategy, version, window, stop_loss, max_hold_hours, "
            "signal_price, signal_time, entry_price, entry_time, entry_drift_pct, account, "
            "exit_price, shares, is_dry_run_sim, position_source, exit_time, exit_reason) "
            "VALUES (?, 'TrailingBothZScoreBreakout', 'test', 10, 1, 105, "
            "9.0, '2026-08-10 10:00:00', 9.0, '2026-08-10 10:00:00', 0.0, 'soxl_ira', "
            "9.1813, 41, 0, 'core', '2026-08-11 10:00:00', 'TRAIL')",
            (TICKER,),
        )
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node_id():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]['id']


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def test_pending_buy_snapshot_preserves_override_for_topup(env, fake_broker, monkeypatch):
    node = _node()
    assert node['starting_notional_override'] == 400.0, "test setup sanity check"

    # --- act: place the trailing-buy order (snapshots node into pending_buys
    # right now, same as the real 2026-08-14 RETL order placement) ---
    signal_price = 9.20
    sig = {'current_price': signal_price, 'last_bar': datetime(2026, 8, 14, 10, 25)}
    signals_db.add_pending_buy(node, sig, channel='C0TEST', ts='1234.5', order_id=8888888888)
    signals_db.mark_pending_buy_placed_by_wl_id(node['id'])

    # confirm the snapshot itself actually carries the override -- this is
    # the direct assertion for the root cause, independent of the downstream
    # top-up math below.
    pending = [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER][0]
    assert pending['node'].get('starting_notional_override') == 400.0, (
        "pending_buys.node_json snapshot dropped starting_notional_override -- "
        "this is the exact 2026-08-17 RETL bug (_PENDING_BUY_NODE_KEYS gap)"
    )

    # --- the order rests for days (RETL: 2026-08-14 -> 2026-08-17), then
    # fills at the real RETL numbers ---
    fill_price = 9.0487
    filled_shares = 42.0
    fake_broker.set_quote(TICKER, last=fill_price, bid=fill_price, ask=fill_price + 0.01)

    signals_notify._reconcile_buy_fill(TICKER, fill_price=fill_price, filled_shares=filled_shares,
                                        wl_id=node['id'])

    # --- expect a real 2-share top-up against the $400 override target,
    # exactly like RETL should have gotten ---
    pos = signals_db.get_open_position(TICKER)
    assert pos is not None

    target_notional = 400.0
    delta = target_notional - (fill_price * filled_shares)
    expected_topup_shares = int(delta // fill_price)
    assert expected_topup_shares == 2, "sanity check against the real RETL numbers"

    expected_total_shares = filled_shares + expected_topup_shares
    assert pos['shares'] == expected_total_shares, (
        f"expected the position to reflect a top-up against the $400 override "
        f"({filled_shares} initial + {expected_topup_shares} top-up = "
        f"{expected_total_shares}), got {pos['shares']} -- if this is 42.0, the "
        f"top-up silently didn't fire (the pre-fix RETL bug: fell through to the "
        f"stale trade_log recovery basis, delta landed inside tolerance, no-op)"
    )

    topup_events = signals_db.get_coverage_events(scenario_key='top_up')
    assert any(e['ticker'] == TICKER and e['result'] == 'placed' for e in topup_events), (
        "expected a top_up coverage_event with result='placed' -- RETL's real "
        "incident produced zero coverage_events trace at all"
    )
