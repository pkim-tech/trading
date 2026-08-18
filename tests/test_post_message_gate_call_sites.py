"""Pins that the five per-position `_post_message` clusters swept 2026-08-17
actually pass the node-scoped alert gate (`node_id=`, plus `incident=True`
where a real order/position is already at stake).

tests/test_post_message_alert_gate.py already pins the GATE itself (the
suppress/fail-open logic inside signals_blocks._post_message). This file pins
the CALL SITES -- the thing that was actually wrong: ~30 per-position alerts
across the add-on leg, drought-HANDOFF, `_reconcile_fill` top-up,
`check_gap_resize` and auto-detected-fill paths had `pos`/`node`/`leg` right
there in scope and still called `_post_message` with no gate at all.

Two layers, deliberately:
  1. Behavioural -- drives the real production function with a recording
     `_post_message` and asserts the kwargs it actually received. One per
     cluster.
  2. Structural -- reuses scripts/audit_post_message_gating.py so a NEW
     ungated call site added to one of these five functions later fails here
     instead of quietly re-opening the backlog item.
"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))

import schwab_client
import schwab_safety
import signals_config
import signals_db
import signals_notify

TICKER = 'TEST_GATE_CALLSITES'
_NOW = datetime(2026, 7, 15, 10, 30)


class _Recorder:
    """Stands in for signals_notify._post_message and keeps every call."""

    def __init__(self):
        self.calls = []

    def __call__(self, text=None, blocks=None, **kw):
        self.calls.append({'text': text or '', 'kw': kw})
        return (None, None)

    def matching(self, needle):
        return [c for c in self.calls if needle in c['text']]

    def one(self, needle):
        hits = self.matching(needle)
        assert len(hits) == 1, f"expected exactly 1 alert containing {needle!r}, got {len(hits)}: " \
                               f"{[c['text'][:80] for c in self.calls]}"
        return hits[0]


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(signals_notify, '_post_message', rec)
    return rec


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    monkeypatch.setattr(schwab_safety, 'STATE_PATH', tmp_path / "counts.json")
    monkeypatch.setattr(schwab_safety, 'KILL_SWITCH_PATH', tmp_path / "kill.json")
    monkeypatch.setattr(schwab_safety, 'TICKER_AUTOMATION_PATH', tmp_path / "ticker_auto.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTOMATION_PATH', tmp_path / "node_auto.json")
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "afd.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTO_FILL_DETECTION_PATH', tmp_path / "nafd.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: _NOW)
    monkeypatch.setattr(schwab_client, '_post_message', lambda *a, **kw: (None, None))
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda account: 1_000_000.0)
    monkeypatch.setattr(schwab_client, 'place_stop_loss', lambda *a, **kw: (None, None))
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    # No real sleeps -- several of these paths poll get_filled_order in a loop.
    monkeypatch.setattr(signals_notify, 'time', type('T', (), {'sleep': staticmethod(lambda *a: None)}))
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                         stop_loss=5, max_hold_hours=7, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, starting_notional=50000,
                         account='ira')
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='ira', addon_enabled=1, drought_overlay_enabled=1 "
                  "WHERE ticker=?", (TICKER,))
        c.commit()
    yield
    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _sig(price=50.0, signal='BUY'):
    return {
        'ticker': TICKER, 'current_price': price, 'z_score': -1.4, 'signal': signal,
        'last_bar': _NOW, 'lower_band': price - 1.0, 'sma': price + 2.0, 'std': 1.0,
        'hurst': None, 'adf_p': None, 'window': 20,
    }


def _open_core_position(node, shares=10, price=50.0):
    assert signals_db.open_position(node, price, _NOW, price, _NOW, shares=shares)
    return signals_db.get_open_position(TICKER)


# ---------------------------------------------------------------------------
# Cluster 1 -- add-on leg (check_addon_trigger_real)
# ---------------------------------------------------------------------------

def test_addon_non_margin_skip_is_gated_as_incident(env, recorder, monkeypatch):
    node = _node()
    pos = _open_core_position(node)
    # 'ira' is real but the add-on path refuses a non-margin-capable account --
    # force that branch regardless of the real .env account config. The
    # `limits is None` guard matters: without it this test ERRORS instead of
    # skipping on a checkout where ACCOUNTS has no 'ira' row (paired review
    # finding, 2026-08-17 -- its two siblings guarded, this one didn't).
    limits = schwab_safety.ACCOUNTS.get('ira')
    if limits is None:
        pytest.skip("'ira' not in ACCOUNTS in this environment -- branch unreachable")
    monkeypatch.setattr(schwab_safety, 'ACCOUNTS', dict(schwab_safety.ACCOUNTS, ira=limits))
    monkeypatch.setattr(limits, 'margin_capable', False, raising=False)

    signals_notify.check_addon_trigger_real(pos, 50.0)

    call = recorder.one('is not margin-capable')
    assert call['kw']['node_id'] == pos['wl_id']
    assert call['kw']['incident'] is True


def test_addon_dry_run_sim_synthesis_is_gated_without_incident(env, recorder):
    node = _node()
    assert signals_db.open_position(node, 50.0, _NOW, 50.0, _NOW, shares=10, is_dry_run_sim=True)
    pos = signals_db.get_open_position(TICKER)
    limits = schwab_safety.ACCOUNTS.get('ira')
    if limits is not None and not getattr(limits, 'margin_capable', False):
        pytest.skip("'ira' is not margin-capable in this .env -- branch unreachable")

    signals_notify.check_addon_trigger_real(pos, 50.0)

    call = recorder.one('add-on leg synthesized')
    assert call['kw']['node_id'] == pos['wl_id']
    # Routine lifecycle notice -- keeps the stricter should_alert_live gate.
    assert call['kw'].get('incident') in (None, False)


def test_addon_leg_reconciliation_orphaned_leg_is_gated_as_incident(env, recorder, monkeypatch):
    """check_addon_leg_reconciliation juggles pos/leg/node/_leg_node/_parent_pos;
    both reviewers flagged it as the place a wrong node_id was actually
    possible, and it had no behavioural coverage. Drives the orphaned-leg
    branch (leg still open, parent core position already closed) and asserts
    the alert is keyed to the LEG's own wl_id."""
    node = _node()
    pos = _open_core_position(node, shares=10)
    leg_id = signals_db.open_addon_leg(pos, shares=5, entry_price=50.0, entry_time=_NOW,
                                        paper=False, entry_order_id=None, entry_status='filled')
    # Close the parent core position -- the leg is now orphaned.
    signals_db.close_position(pos['id'], exit_signal_price=49.0, exit_price=49.0,
                               exit_time=datetime.now(), exit_reason='SL')

    signals_notify.check_addon_leg_reconciliation([])

    call = recorder.one('parent core position has already closed')
    assert call['kw']['node_id'] == pos['wl_id']
    assert call['kw']['incident'] is True
    # Deliberately NOT auto-closed -- the alert says so, so pin it.
    assert any(l['id'] == leg_id for l in signals_db.get_open_addon_legs())


def test_close_addon_leg_exit_failure_is_gated_as_incident(env, recorder, monkeypatch):
    """close_addon_leg_real_if_open resolves `node` from pos['wl_id'] but keys
    its coverage events off leg['wl_id']; this pins that the alert uses the
    leg's own id and that a real exit-SELL failure stays incident-visible."""
    node = _node()
    pos = _open_core_position(node, shares=10)
    signals_db.open_addon_leg(pos, shares=5, entry_price=50.0, entry_time=_NOW,
                               paper=False, entry_order_id=None, entry_status='filled')

    def _boom(*a, **kw):
        raise RuntimeError("broker refused the leg exit")

    monkeypatch.setattr(schwab_client, 'place_equity_sell', _boom)
    monkeypatch.setattr(schwab_client, 'replace_equity_order_with_market', _boom)

    signals_notify.close_addon_leg_real_if_open(pos, 49.0, 'SL', datetime.now())

    call = recorder.one('exit SELL')
    assert call['kw']['node_id'] == pos['wl_id']
    assert call['kw']['incident'] is True


# ---------------------------------------------------------------------------
# Cluster 2 -- drought HANDOFF (check_drought_handoff)
# ---------------------------------------------------------------------------

def test_drought_handoff_manual_order_no_id_is_gated_as_incident(env, recorder, monkeypatch):
    node = _node()
    signals_db.add_pending_buy(node, _sig(), 'C1', 'T1', order_id=None,
                                position_source='drought_overlay')
    with signals_db._conn() as c:
        c.execute("UPDATE pending_buys SET order_placed=1 WHERE wl_id=?", (node['id'],))
        c.commit()
    monkeypatch.setattr(signals_notify.compute, 'compute_buy_signal', lambda n: _sig())
    if signals_notify._effectively_dry_run('ira', node):
        pytest.skip("'ira' is effectively dry_run in this .env -- branch unreachable")

    signals_notify.check_drought_handoff(node)

    call = recorder.one('no broker id on file')
    assert call['kw']['node_id'] == node['id']
    assert call['kw']['incident'] is True


def test_drought_handoff_cancelled_resting_entry_is_gated_without_incident(env, recorder, monkeypatch):
    node = _node()
    signals_db.add_pending_buy(node, _sig(), 'C1', 'T1', order_id='ORD-1',
                                position_source='drought_overlay')
    with signals_db._conn() as c:
        c.execute("UPDATE pending_buys SET order_placed=1 WHERE wl_id=?", (node['id'],))
        c.commit()
    monkeypatch.setattr(signals_notify.compute, 'compute_buy_signal', lambda n: _sig())
    monkeypatch.setattr(schwab_client, 'cancel_order', lambda *a, **kw: (None, 'CANCELED'))
    if signals_notify._effectively_dry_run('ira', node):
        pytest.skip("'ira' is effectively dry_run in this .env -- branch unreachable")

    signals_notify.check_drought_handoff(node)

    call = recorder.one('cancelled before it filled')
    assert call['kw']['node_id'] == node['id']
    assert call['kw'].get('incident') in (None, False)


# ---------------------------------------------------------------------------
# Cluster 3 -- _reconcile_fill top-up
# ---------------------------------------------------------------------------

def test_topup_blocked_is_gated_as_incident(env, recorder, monkeypatch):
    node = _node()

    def _blocked(*a, **kw):
        raise schwab_safety.SafetyViolation("nope")

    monkeypatch.setattr(schwab_client, 'place_equity_buy', _blocked)
    # 1 share @ $50 against a $50k target -> a real top-up is attempted.
    signals_notify._reconcile_fill(node, 50.0, 1)

    call = recorder.one('top-up buy of')
    assert 'blocked' in call['text']
    assert call['kw']['node_id'] == node['id']
    assert call['kw']['incident'] is True


def test_topup_success_is_gated_without_incident(env, recorder, monkeypatch):
    node = _node()
    _open_core_position(node, shares=1)
    monkeypatch.setattr(schwab_client, 'place_equity_buy', lambda *a, **kw: (None, None))
    signals_notify._reconcile_fill(node, 50.0, 1)

    call = recorder.one('top-up buy ')
    assert call['kw']['node_id'] == node['id']
    assert call['kw'].get('incident') in (None, False)


def test_overspend_alert_is_gated_as_incident(env, recorder):
    node = _node()
    # Fill notional far ABOVE target -> the "no corrective sell placed" branch.
    signals_notify._reconcile_fill(node, 50.0, 10_000)

    call = recorder.one('exceeded target notional')
    assert call['kw']['node_id'] == node['id']
    assert call['kw']['incident'] is True


# ---------------------------------------------------------------------------
# Cluster 4 -- check_gap_resize
# ---------------------------------------------------------------------------

def test_gap_resize_missing_account_alert_stays_ungated(env, recorder):
    """The ONE deliberate exemption in check_gap_resize. Both paired reviewers
    converged on it: this branch fires precisely because the pinned
    pending_buys snapshot has no account, so gating it would let
    _post_message re-resolve the LIVE watch_list row and potentially silence
    an alert whose whole subject is "this resting order's attribution is
    broken" -- the same reasoning that keeps _throttled_entry_abandon_alert
    ungated. Pinned as a test so a later "consistency" pass doesn't gate it
    back without reading the comment."""
    node = dict(_node())
    node['account'] = None
    signals_db.add_pending_buy(node, _sig(), 'C1', 'T1', order_id='ORD-1')
    with signals_db._conn() as c:
        c.execute("UPDATE pending_buys SET order_placed=1 WHERE wl_id=?", (node['id'],))
        c.commit()

    signals_notify.check_gap_resize()

    call = recorder.one('gap-check skipped')
    assert 'node_id' not in call['kw']
    assert 'incident' not in call['kw']


def test_gap_resize_price_lookup_failure_is_gated_as_incident(env, recorder, monkeypatch):
    node = _node()
    signals_db.add_pending_buy(node, _sig(), 'C1', 'T1', order_id='ORD-1')
    with signals_db._conn() as c:
        c.execute("UPDATE pending_buys SET order_placed=1 WHERE wl_id=?", (node['id'],))
        c.commit()

    def _boom(ticker):
        raise RuntimeError("quote feed down")

    monkeypatch.setattr(schwab_client, 'get_current_price', _boom)
    signals_notify.check_gap_resize()

    call = recorder.one('gap-check price lookup failed')
    assert call['kw']['node_id'] == node['id']
    assert call['kw']['incident'] is True


# ---------------------------------------------------------------------------
# Cluster 5 -- auto-detected fill notices
# ---------------------------------------------------------------------------

def test_sl_order_fill_qty_mismatch_is_gated_as_incident(env, recorder, monkeypatch):
    node = _node()
    pos = _open_core_position(node, shares=10)
    signals_db.set_sl_order_id_by_position(pos['id'], 'SL-1')
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_filled_order',
                         lambda *a, **kw: {'price': 49.0, 'quantity': 4})

    signals_notify.check_sl_order_fills([pos])

    call = recorder.one('needs manual reconciliation')
    assert call['kw']['node_id'] == pos['wl_id']
    assert call['kw']['incident'] is True
    # Detection-only: the position must NOT have been auto-closed.
    assert signals_db.get_open_position(TICKER) is not None


def test_sl_order_fill_close_notice_is_gated_without_incident(env, recorder, monkeypatch):
    node = _node()
    pos = _open_core_position(node, shares=10)
    signals_db.set_sl_order_id_by_position(pos['id'], 'SL-1')
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_filled_order',
                         lambda *a, **kw: {'price': 49.0, 'quantity': 10})

    signals_notify.check_sl_order_fills([pos])

    call = recorder.one('auto-detected')
    assert call['kw']['node_id'] == pos['wl_id']
    assert call['kw'].get('incident') in (None, False)


def test_own_sell_fill_notice_is_gated_without_incident(env, recorder, monkeypatch):
    node = _node()
    pos = _open_core_position(node, shares=10)
    signals_db.update_position_trail_state(
        pos['id'], {'exit_pending': {'reason': 'TRAIL', 'order_id': 'X-1', 'current_price': 49.5}})
    pos = signals_db.get_open_position(TICKER)
    monkeypatch.setattr(schwab_client, 'get_filled_order',
                         lambda *a, **kw: {'price': 49.0, 'quantity': 10})

    signals_notify.check_own_sell_fills([pos])

    call = recorder.one('auto-detected exit fill')
    assert call['kw']['node_id'] == pos['wl_id']
    assert call['kw'].get('incident') in (None, False)


# ---------------------------------------------------------------------------
# Structural guard across all five clusters
# ---------------------------------------------------------------------------

SWEPT_FUNCTIONS = {
    'check_addon_trigger_real', '_place_stop_loss_for_addon_leg',
    'check_addon_leg_reconciliation', 'close_addon_leg_real_if_open',
    'check_drought_handoff', '_reconcile_fill', 'check_gap_resize',
    'check_own_sell_fills', 'check_sl_order_fills', 'check_auto_fills',
}


def test_no_ungated_post_message_left_in_the_swept_functions():
    """A NEW ungated per-position alert added to any of the five swept
    functions later must fail here rather than silently re-opening the
    backlog item this file closes."""
    import audit_post_message_gating as audit

    rows = audit.audit(str(Path(__file__).parent.parent / 'signals_notify.py'))
    # r['deliberate'] is the audit script's registry of on-purpose exemptions,
    # keyed by (function, message marker) -- so a NEW ungated alert in one of
    # these functions still fails here even though a sibling line is exempt.
    offenders = [f"{r['func']}:{r['line']}" for r in rows
                 if r['func'] in SWEPT_FUNCTIONS and not r['gated'] and not r['deliberate']]
    assert not offenders, f"ungated per-position _post_message call sites: {offenders}"


def test_the_whole_module_has_no_unclassified_ungated_per_position_alert():
    """Module-wide backstop, beyond the five swept functions: every remaining
    ungated `_post_message` in signals_notify.py must be either system-wide,
    behind a function-level has_capital_at_stake gate, or registered as a
    deliberate exemption. Surfaced notify_limit_fill during this session."""
    import audit_post_message_gating as audit

    rows = audit.audit(str(Path(__file__).parent.parent / 'signals_notify.py'))
    candidates = [f"{r['func']}:{r['line']}" for r in rows if r['candidate']]
    assert not candidates, f"unclassified ungated per-position alerts: {candidates}"
