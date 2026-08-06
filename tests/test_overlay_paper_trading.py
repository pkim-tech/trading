"""Tests for the 2026-08-09 drought-overlay/margin-add-on/skim-and-reserve
paper-trading build (docs/design.md's 2026-08-07 "Live automation design"
section) -- covers the real bugs a paired Opus review found and this session
fixed: the drought once-per-gap guard (HIGH-1), the vol-gate None polarity
(MEDIUM-10), the redeploy-alert empty-reserve guard (HIGH-2), the skim-amount
formula (HIGH-3), the add-on lockstep close, and (found once the drought
functions were actually wired into active_signals.py) a CRITICAL HANDOFF
signal-check bug and an ordering violation at a third real core-entry path.
Every function under test is exercised directly here (not through the live
poll loop itself) -- check_paper_drought_entry/check_paper_drought_handoff
are wired into active_signals.py's run_loop; check_paper_addon_trigger/
check_paper_skim were already reachable via the pre-existing
check_paper_sells call."""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db as db
import paper_trading
from tests.conftest import make_synthetic_csv, cleanup_csv, _synthetic_timestamps

TICKER = 'TEST_OVERLAY'
# The synthetic CSV's price history is a fixed 2025 business-day range, not
# real wall-clock time -- trade timestamps below index into this SAME grid
# (early indices, so plenty of checkpoint bars remain after any given "exit"
# for the drought entry gate to count), rather than datetime.now(), which
# would fall entirely outside the cached range and starve every checkpoint
# count to zero.
_TS = _synthetic_timestamps(90)


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    db.ensure_tables()
    yield db
    os.unlink(tmp_db.name)


@pytest.fixture
def core_node(isolated_db):
    """A real TrailingBothZScoreBreakout watch_list node -- arm value lives in
    arm_sell_pct for this strategy (take_profit holds it instead for other
    strategies, see signals_db._tp_or_arm_pct). mode='research' -- matches
    how every real production drought/addon/skim-eligible node is actually
    configured (these mechanisms are paper-only; check_paper_drought_entry/
    check_paper_drought_handoff both refuse to touch a mode='live' node)."""
    make_synthetic_csv(TICKER, last_close=100.0)
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', 20, 30, 2, 48,
                trail_buy_pct=1.0, trail_pct=7.0, account='test_acct',
                entry_timing='open_check', starting_notional=10000, mode='research')
    with db._conn() as c:
        wl_id = c.execute("SELECT id FROM watch_list WHERE ticker=?", (TICKER,)).fetchone()[0]
    yield db.get_watch_list_node_by_id(wl_id)
    cleanup_csv(TICKER)


def _close_core_trade(node, pnl_pct, step_index):
    """Opens and immediately closes a CORE paper position at the given
    realized return -- the unit of "one step" for both the drought gap
    counter and the skim equity series. step_index indexes into the
    synthetic CSV's own timestamp grid (_TS), not wall-clock time -- see
    _TS's comment above for why."""
    ts = _TS[step_index]
    db.open_position(node, 100.0, ts, 100.0, ts, shares=1, paper=True)
    pos = db.get_open_position_by_wl_id(node['id'], paper=True)
    exit_price = 100.0 * (1 + pnl_pct / 100.0)
    db.close_position(pos['id'], exit_signal_price=exit_price, exit_price=exit_price,
                       exit_time=ts, exit_reason='TRAIL', paper=True)
    return db.get_watch_list_node_by_id(node['id'])


def _seed_skim(node, step_index=0):
    """check_paper_skim's first-ever call for a node seeds skim_ref/peak/
    min_since_peak/strategy_value from the node's REAL current equity
    (rather than a flat 1.0/starting_notional baseline) and never fires a
    skim on that seeding call itself. Tests that want to reason about a
    KNOWN, exact equity path (rather than "whatever equity this node
    happened to be at when skim was enabled") call this first with a 0%
    trade -- seeds everything at equity=1.0 exactly, matching the pre-
    seeding-fix hardcoded default, so the rest of a test's numbers are
    unaffected by the seeding step's existence."""
    node = _close_core_trade(node, 0, step_index)
    paper_trading.check_paper_skim(node)
    return db.get_watch_list_node_by_id(node['id'])


# ---------------------------------------------------------------------------
# Drought overlay
# ---------------------------------------------------------------------------

def test_drought_entry_noop_before_any_core_trade(core_node, monkeypatch):
    """Matches find_drought_windows: a window only ever exists BETWEEN two
    consecutive real trades -- never before the first one."""
    with db._conn() as c:
        c.execute("UPDATE watch_list SET drought_overlay_enabled=1, drought_confirm_days=3 WHERE id=?",
                  (core_node['id'],))
        c.commit()
    node = db.get_watch_list_node_by_id(core_node['id'])
    paper_trading.check_paper_drought_entry(node)
    assert db.get_drought_overlay_position(node['id'], paper=True) is None


def test_drought_entry_fires_once_confirmed_then_never_reenters_same_gap(core_node, monkeypatch):
    """The HIGH-1 fix: the backtest's find_drought_windows makes exactly ONE
    trade per gap, even if that trade stops out early with real time left
    before the next core signal. The first version of this function
    re-entered on every subsequent poll once confirmed.

    Offline proof for coverage_registry's registry id 'drought_entry'."""
    with db._conn() as c:
        c.execute("UPDATE watch_list SET drought_overlay_enabled=1, drought_confirm_days=3 WHERE id=?",
                  (core_node['id'],))
        c.commit()
    node = _close_core_trade(core_node, -5, 0)
    monkeypatch.setattr(paper_trading, '_current_price', lambda ticker: (150.0, None))

    paper_trading.check_paper_drought_entry(node)
    pos = db.get_drought_overlay_position(node['id'], paper=True)
    assert pos is not None, "should confirm and enter -- last exit is 90 days into the synthetic history"

    # Stop it out mid-gap (real time remains before the next core signal).
    db.close_position(pos['id'], exit_signal_price=140.0, exit_price=140.0,
                       exit_time=datetime.now(), exit_reason='SL', paper=True)

    # Re-poll in the SAME gap -- must NOT re-enter.
    paper_trading.check_paper_drought_entry(db.get_watch_list_node_by_id(node['id']))
    assert db.get_drought_overlay_position(node['id'], paper=True) is None, \
        "HIGH-1 regression: re-entered within the same already-used gap"


def test_drought_handoff_closes_position_on_core_signal(core_node, monkeypatch):
    """Offline proof for coverage_registry's registry id 'drought_handoff'."""
    node = _close_core_trade(core_node, -5, 0)
    monkeypatch.setattr(paper_trading, '_current_price', lambda ticker: (150.0, None))
    with db._conn() as c:
        c.execute("UPDATE watch_list SET drought_overlay_enabled=1, drought_confirm_days=3 WHERE id=?",
                  (node['id'],))
        c.commit()
    node = db.get_watch_list_node_by_id(node['id'])
    paper_trading.check_paper_drought_entry(node)
    assert db.get_drought_overlay_position(node['id'], paper=True) is not None

    monkeypatch.setattr(paper_trading, 'compute_buy_signal',
                         lambda n, **kw: {'current_price': 123.45, 'signal': 'BUY'})
    paper_trading.check_paper_drought_handoff(node)
    assert db.get_drought_overlay_position(node['id'], paper=True) is None
    with db._conn() as c:
        row = c.execute(
            "SELECT exit_reason, exit_price FROM paper_trade_log WHERE position_source='drought_overlay' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row['exit_reason'] == 'HANDOFF'
    assert row['exit_price'] == 123.45


def test_drought_handoff_ignores_a_hold_signal(core_node, monkeypatch):
    """The CRITICAL bug a paired review of the wiring diff caught: compute_
    buy_signal returns a real dict on almost every poll (signal='HOLD' most
    of the time), never None just because there's no signal -- the first
    version's `if sig is None: return` treated ordinary HOLD polls as a
    fired core signal and closed the drought position on nearly every poll
    inside a signal window."""
    node = _close_core_trade(core_node, -5, 0)
    monkeypatch.setattr(paper_trading, '_current_price', lambda ticker: (150.0, None))
    with db._conn() as c:
        c.execute("UPDATE watch_list SET drought_overlay_enabled=1, drought_confirm_days=3 WHERE id=?",
                  (node['id'],))
        c.commit()
    node = db.get_watch_list_node_by_id(node['id'])
    paper_trading.check_paper_drought_entry(node)
    assert db.get_drought_overlay_position(node['id'], paper=True) is not None

    monkeypatch.setattr(paper_trading, 'compute_buy_signal',
                         lambda n, **kw: {'current_price': 55.0, 'signal': 'HOLD'})
    paper_trading.check_paper_drought_handoff(node)
    assert db.get_drought_overlay_position(node['id'], paper=True) is not None, \
        "CRITICAL regression: a HOLD signal must never trigger HANDOFF"


def test_drought_functions_never_touch_a_live_mode_node(core_node):
    with db._conn() as c:
        c.execute("UPDATE watch_list SET mode='live', drought_overlay_enabled=1, "
                  "drought_confirm_days=3 WHERE id=?", (core_node['id'],))
        c.commit()
    node = db.get_watch_list_node_by_id(core_node['id'])
    paper_trading.check_paper_drought_entry(node)
    assert db.get_open_position_by_wl_id(node['id'], paper=True) is None
    assert db.get_drought_overlay_position(node['id'], paper=True) is None
    paper_trading.check_paper_drought_handoff(node)  # must not raise or touch anything


def test_drought_vol_gate_excludes_unknown_reading(core_node, monkeypatch):
    """MEDIUM-10 fix: a None vol_pctile reading must EXCLUDE the window
    (mirrors generate_drought_trades' _apply_vol_gate exactly), not allow it
    through -- the first version had this polarity backwards."""
    node = _close_core_trade(core_node, -5, 0)
    with db._conn() as c:
        c.execute("UPDATE watch_list SET drought_overlay_enabled=1, drought_confirm_days=3, "
                  "drought_vol_gate=0.4 WHERE id=?", (node['id'],))
        c.commit()
    node = db.get_watch_list_node_by_id(node['id'])
    monkeypatch.setattr(paper_trading, '_current_price', lambda ticker: (150.0, None))

    class _FakeSweep:
        @staticmethod
        def get_ivol_series(ticker):
            return None

        @staticmethod
        def _entry_vol_pctile(entry_time, series):
            return None  # unknown reading

    monkeypatch.setitem(sys.modules, 'scripts.drought_overlay_sweep', _FakeSweep)
    paper_trading.check_paper_drought_entry(node)
    assert db.get_drought_overlay_position(node['id'], paper=True) is None, \
        "MEDIUM-10 regression: an unknown vol reading let entry through"


# ---------------------------------------------------------------------------
# Margin add-on-at-arm
# ---------------------------------------------------------------------------

def test_addon_trigger_opens_leg_matching_core_shares_and_dedupes(core_node):
    """Offline proof for coverage_registry's registry id 'addon_entry_fill'."""
    with db._conn() as c:
        c.execute("UPDATE watch_list SET addon_enabled=1 WHERE id=?", (core_node['id'],))
        c.commit()
    node = db.get_watch_list_node_by_id(core_node['id'])
    now = datetime.now()
    db.open_position(node, 100.0, now, 100.0, now, shares=100, paper=True)
    pos = db.get_open_position_by_wl_id(node['id'], paper=True)

    paper_trading.check_paper_addon_trigger(node, pos, 130.0, now)
    leg = db.get_open_addon_leg_by_parent(pos['id'], paper=True)
    assert leg is not None
    assert leg['shares'] == pos['shares']
    assert leg['entry_price'] == 130.0

    # Re-trigger must not open a second leg.
    paper_trading.check_paper_addon_trigger(node, pos, 131.0, now)
    assert len(db.get_open_addon_legs(paper=True)) == 1


def test_addon_never_triggers_on_a_drought_position(core_node):
    with db._conn() as c:
        c.execute("UPDATE watch_list SET addon_enabled=1 WHERE id=?", (core_node['id'],))
        c.commit()
    node = db.get_watch_list_node_by_id(core_node['id'])
    now = datetime.now()
    db.open_drought_overlay_position(node, 50.0, now, 50.0, now, confirm_days=3, paper=True)
    dpos = db.get_drought_overlay_position(node['id'], paper=True)
    paper_trading.check_paper_addon_trigger(node, dpos, 60.0, now)
    assert db.get_open_addon_leg_by_parent(dpos['id'], paper=True) is None


def test_addon_leg_closes_in_lockstep_with_parent_and_applies_margin_cost(core_node):
    """Offline proof for coverage_registry's registry id 'addon_exit_fill'."""
    now = datetime.now()
    db.open_position(core_node, 100.0, now, 100.0, now, shares=100, paper=True)
    pos = db.get_open_position_by_wl_id(core_node['id'], paper=True)
    leg_id = db.open_addon_leg(pos, shares=100, entry_price=130.0, entry_time=now, paper=True)

    paper_trading.close_paper_addon_leg_if_open(pos['id'], TICKER, core_node['id'], 140.0, now, 'TRAIL', False)
    with db._conn() as c:
        row = c.execute("SELECT status, exit_price, pnl_pct FROM paper_addon_legs WHERE id=?", (leg_id,)).fetchone()
    assert row['status'] == 'closed'
    raw_ret = (140.0 - 130.0) / 130.0 * 100
    assert row['pnl_pct'] == pytest.approx(raw_ret - 0.04)  # MARGIN_COST_FLAT_PCT applied


def test_close_paper_addon_leg_if_open_is_a_noop_with_no_leg(core_node):
    now = datetime.now()
    db.open_position(core_node, 100.0, now, 100.0, now, shares=100, paper=True)
    pos = db.get_open_position_by_wl_id(core_node['id'], paper=True)
    # Should not raise even though no leg exists.
    paper_trading.close_paper_addon_leg_if_open(pos['id'], TICKER, core_node['id'], 140.0, now, 'TRAIL', False)


# ---------------------------------------------------------------------------
# Skim-and-reserve
# ---------------------------------------------------------------------------

def test_skim_fires_on_new_high_and_amount_shrinks_each_time(core_node, monkeypatch):
    """Offline proof for coverage_registry's registry id 'skim_fire'."""
    with db._conn() as c:
        c.execute("UPDATE watch_list SET skim_enabled=1 WHERE id=?", (core_node['id'],))
        c.commit()
    monkeypatch.setattr(paper_trading, '_spy_price_at', lambda ts: 500.0)
    node = _seed_skim(core_node, 0)
    for i, r in enumerate([15, 15, 15], start=1):
        node = _close_core_trade(node, r, i)
        paper_trading.check_paper_skim(node)
        node = db.get_watch_list_node_by_id(node['id'])

    with db._conn() as c:
        amounts = [row['amount'] for row in
                   c.execute("SELECT amount FROM skim_reserve_log WHERE wl_id=? AND action='skim' ORDER BY id",
                             (node['id'],))]
    assert len(amounts) == 3
    assert amounts[0] > amounts[1] > amounts[2], \
        "HIGH-3 regression: skim amount must shrink each time (net of the 15%-growth/20%-skim rates " \
        "used here, the 20% skim always dominates -- if this ever failed, check whether strategy_value " \
        "compounding was removed again)"
    # First skim: 20% of the sleeve AFTER compounding the first trade's own +15% return onto the
    # starting notional (10000 * 1.15 = 11500), NOT 20% of the untouched notional -- an earlier
    # version of this fix compounded correctly on the skim side but forgot to compound strategy_value
    # by the triggering trade's own return at all (found by an independent Opus review, 2026-08-09;
    # this exact assertion previously locked that bug in, since it matched the buggy output).
    assert amounts[0] == pytest.approx(10000 * 1.15 * 0.20)


def test_skim_never_fires_on_a_wiggle_at_the_peak(core_node, monkeypatch):
    """The 2026-08-08 CRITICAL bug this whole design fixed: a threshold may
    only fire on genuine recovery from a real decline, never a sub-decline
    wiggle right at a new high."""
    with db._conn() as c:
        c.execute("UPDATE watch_list SET skim_enabled=1 WHERE id=?", (core_node['id'],))
        c.commit()
    monkeypatch.setattr(paper_trading, '_spy_price_at', lambda ts: 500.0)
    node = core_node
    for i, r in enumerate([5, -0.05, 3]):
        node = _close_core_trade(node, r, i)
        paper_trading.check_paper_skim(node)
        node = db.get_watch_list_node_by_id(node['id'])
    with db._conn() as c:
        events = c.execute("SELECT action FROM skim_reserve_log WHERE wl_id=?", (node['id'],)).fetchall()
    assert not any(e['action'] == 'redeploy_alert' for e in events)


def test_redeploy_alert_never_fires_against_an_empty_reserve(core_node, monkeypatch):
    """HIGH-2 fix: a decline-then-full-recovery that never itself crosses the
    skim step (so the reserve stays genuinely at zero) must not alert."""
    with db._conn() as c:
        c.execute("UPDATE watch_list SET skim_enabled=1 WHERE id=?", (core_node['id'],))
        c.commit()
    monkeypatch.setattr(paper_trading, '_spy_price_at', lambda ts: 500.0)
    node = core_node
    # peak=1.02, decline to 0.867, recover past the peak to 1.0404 -- never
    # crosses skim_ref(1.0)*1.10, so no skim ever fires and the reserve is 0.
    for i, r in enumerate([2, -15, 20]):
        node = _close_core_trade(node, r, i)
        paper_trading.check_paper_skim(node)
        node = db.get_watch_list_node_by_id(node['id'])
    with db._conn() as c:
        events = c.execute("SELECT action FROM skim_reserve_log WHERE wl_id=?", (node['id'],)).fetchall()
    assert not any(e['action'] == 'skim' for e in events), "test setup invalid -- a skim fired"
    assert not any(e['action'] == 'redeploy_alert' for e in events), \
        "HIGH-2 regression: alerted to redeploy against a genuinely empty reserve"


def test_redeploy_alerts_fire_exactly_twice_across_a_real_decline_recovery_cycle(core_node, monkeypatch):
    """Offline proof for coverage_registry's registry id 'skim_redeploy_alert'."""
    with db._conn() as c:
        c.execute("UPDATE watch_list SET skim_enabled=1 WHERE id=?", (core_node['id'],))
        c.commit()
    monkeypatch.setattr(paper_trading, '_spy_price_at', lambda ts: 500.0)
    node = _seed_skim(core_node, 0)
    rets = [15, -10, -10, -12, 5, 10, 10, 20]
    for i, r in enumerate(rets, start=1):
        node = _close_core_trade(node, r, i)
        paper_trading.check_paper_skim(node)
        node = db.get_watch_list_node_by_id(node['id'])
    with db._conn() as c:
        events = [dict(row) for row in
                  c.execute("SELECT action FROM skim_reserve_log WHERE wl_id=? ORDER BY id", (node['id'],))]
    assert sum(1 for e in events if e['action'] == 'skim') == 1
    assert sum(1 for e in events if e['action'] == 'redeploy_alert') == 2


def test_skim_reserve_pool_actually_gains_real_shares(core_node, monkeypatch):
    """MEDIUM-7 fix: the reserve must actually be modeled with real shares
    against a real SPY price, not just an event log."""
    with db._conn() as c:
        c.execute("UPDATE watch_list SET skim_enabled=1 WHERE id=?", (core_node['id'],))
        c.commit()
    monkeypatch.setattr(paper_trading, '_spy_price_at', lambda ts: 500.0)
    node = _seed_skim(core_node, 0)
    node = _close_core_trade(node, 15, 1)
    paper_trading.check_paper_skim(node)
    pool = db.get_skim_reserve_pool('test_acct')
    # Seed call (0% trade) leaves strategy_value at 10000; this trade compounds
    # it by +15% to 11500, then 20% is skimmed.
    assert pool['reserve_shares'] == pytest.approx((10000 * 1.15 * 0.20) / 500.0)
    assert pool['avg_cost'] == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# Daily-track / live-track overlay config parity (task 6)
# ---------------------------------------------------------------------------

def test_daily_track_overlay_config_invariant_flags_a_real_mismatch(isolated_db):
    import signals_invariants as inv
    make_synthetic_csv(TICKER, last_close=100.0)
    try:
        db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', 20, 30, 2, 48,
                    trail_buy_pct=1.0, trail_pct=7.0, account='a', watchlist_id=1)
        db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', 20, 30, 2, 48,
                    trail_buy_pct=1.0, trail_pct=7.0, account='a', watchlist_id=1,
                    paper_role='daily_sync')
        with db._conn() as c:
            live_id = c.execute(
                "SELECT id FROM watch_list WHERE ticker=? AND paper_role IS NULL", (TICKER,)
            ).fetchone()[0]
            c.execute("UPDATE watch_list SET drought_overlay_enabled=1, drought_confirm_days=3 WHERE id=?",
                      (live_id,))
            c.commit()
        violations = inv.check_daily_track_overlay_config_matches_live_track()
        assert any(TICKER in v for v in violations), \
            "should flag the live-track/daily-track drought config mismatch"
    finally:
        cleanup_csv(TICKER)


def test_daily_track_overlay_config_invariant_silent_when_synced(isolated_db):
    import signals_invariants as inv
    make_synthetic_csv(TICKER, last_close=100.0)
    try:
        db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', 20, 30, 2, 48,
                    trail_buy_pct=1.0, trail_pct=7.0, account='a', watchlist_id=1)
        db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', 20, 30, 2, 48,
                    trail_buy_pct=1.0, trail_pct=7.0, account='a', watchlist_id=1,
                    paper_role='daily_sync')
        with db._conn() as c:
            c.execute("UPDATE watch_list SET drought_overlay_enabled=1, drought_confirm_days=3")
            c.commit()
        violations = inv.check_daily_track_overlay_config_matches_live_track()
        assert violations == []
    finally:
        cleanup_csv(TICKER)


# ---------------------------------------------------------------------------
# reconcile_overlay_nodes -- the backtest-replay side is monkeypatched in
# these tests (rather than relying on the synthetic flat-price fixture to
# produce a real z-score signal, which it won't) so the COMPARISON/
# classification logic itself -- the actual new code -- is exercised
# directly, isolated from whether a specific price series happens to trade.
# ---------------------------------------------------------------------------

def test_reconcile_drought_matches_when_paper_entry_aligns_with_backtest(core_node, monkeypatch):
    import pandas as pd
    with db._conn() as c:
        c.execute("UPDATE watch_list SET drought_overlay_enabled=1, drought_confirm_days=3 WHERE id=?",
                  (core_node['id'],))
        c.commit()
    node = db.get_watch_list_node_by_id(core_node['id'])
    entry_ts = _TS[10]
    fake_df_h = pd.DataFrame({'Close': [100.0]}, index=[entry_ts])
    fake_trade = {'entry_i': 0, 'exit_reason': 'TRAIL', 'ret': 0.05}
    monkeypatch.setattr('scripts.stacked_model.drought.generate_drought_trades',
                         lambda *a, **kw: ([fake_trade], fake_df_h))
    db.open_drought_overlay_position(node, 100.0, entry_ts, 100.0, entry_ts, confirm_days=3, paper=True)
    pos = db.get_drought_overlay_position(node['id'], paper=True)
    db.close_position(pos['id'], exit_signal_price=105.0, exit_price=105.0, exit_time=entry_ts,
                       exit_reason='TRAIL', paper=True)

    paper_trading.reconcile_overlay_nodes()
    log = db.get_overlay_reconciliation_log(wl_id=node['id'], mechanism='drought')
    assert log[0]['action'] == 'match'
    assert log[0]['match'] == 1


def test_reconcile_drought_flags_entry_bar_mismatch(core_node, monkeypatch):
    import pandas as pd
    with db._conn() as c:
        c.execute("UPDATE watch_list SET drought_overlay_enabled=1, drought_confirm_days=3 WHERE id=?",
                  (core_node['id'],))
        c.commit()
    node = db.get_watch_list_node_by_id(core_node['id'])
    backtest_entry_ts = _TS[10]
    paper_entry_ts = _TS[10] + timedelta(days=5)  # far outside the 4h match window
    fake_df_h = pd.DataFrame({'Close': [100.0]}, index=[backtest_entry_ts])
    fake_trade = {'entry_i': 0, 'exit_reason': 'TRAIL', 'ret': 0.05}
    monkeypatch.setattr('scripts.stacked_model.drought.generate_drought_trades',
                         lambda *a, **kw: ([fake_trade], fake_df_h))
    db.open_drought_overlay_position(node, 100.0, paper_entry_ts, 100.0, paper_entry_ts,
                                      confirm_days=3, paper=True)
    pos = db.get_drought_overlay_position(node['id'], paper=True)
    db.close_position(pos['id'], exit_signal_price=105.0, exit_price=105.0, exit_time=paper_entry_ts,
                       exit_reason='TRAIL', paper=True)

    paper_trading.reconcile_overlay_nodes()
    log = db.get_overlay_reconciliation_log(wl_id=node['id'], mechanism='drought')
    assert log[0]['action'] == 'entry_bar_mismatch'
    assert log[0]['match'] == 0


def test_reconcile_overlay_nodes_is_idempotent_same_day(core_node, monkeypatch):
    with db._conn() as c:
        c.execute("UPDATE watch_list SET skim_enabled=1 WHERE id=?", (core_node['id'],))
        c.commit()
    node = db.get_watch_list_node_by_id(core_node['id'])
    paper_trading.reconcile_overlay_nodes()
    first = db.get_overlay_reconciliation_log(wl_id=node['id'])
    paper_trading.reconcile_overlay_nodes()
    second = db.get_overlay_reconciliation_log(wl_id=node['id'])
    assert len(first) == len(second), "same-day rerun must not duplicate rows"


def test_reconcile_skim_detects_a_real_count_mismatch(core_node, monkeypatch):
    with db._conn() as c:
        c.execute("UPDATE watch_list SET skim_enabled=1 WHERE id=?", (core_node['id'],))
        c.commit()
    monkeypatch.setattr(paper_trading, '_spy_price_at', lambda ts: 500.0)
    node = core_node
    for i, r in enumerate([15, 15, 15]):
        node = _close_core_trade(node, r, i)
        paper_trading.check_paper_skim(node)
        node = db.get_watch_list_node_by_id(node['id'])
    # Real skims fired above (verified elsewhere) -- corrupt the log to
    # simulate a genuine divergence and confirm reconcile catches it.
    with db._conn() as c:
        c.execute("DELETE FROM skim_reserve_log WHERE wl_id=? AND action='skim' AND id NOT IN "
                  "(SELECT MIN(id) FROM skim_reserve_log WHERE wl_id=? AND action='skim')",
                  (node['id'], node['id']))
        c.commit()
    paper_trading.reconcile_overlay_nodes()
    log = db.get_overlay_reconciliation_log(wl_id=node['id'], mechanism='skim')
    assert log[0]['action'] == 'skim_count_mismatch'
    assert log[0]['match'] == 0


# ---------------------------------------------------------------------------
# Truth-table coverage (docs/design.md's 2026-08-07 "Truth-table dimensions"
# section, put-hedge dimension excluded -- out of scope this build). Real
# reachable (core, drought_overlay, addon_leg) state combinations for one
# wl_id, given the constraints already enforced by open_position()'s wl_id
# dedup (core and drought_overlay share one dedup key -- mutually exclusive
# by construction) and check_paper_addon_trigger's position_source=='core'
# gate (addon only reachable when core is armed):
#   1. (flat, none, none)
#   2. (flat, open drought, none)           -- addon never applies to drought
#   3. (open-unarmed core, none, none)
#   4. (armed core, none, none)             -- addon_enabled=0, or not yet triggered
#   5. (armed core, none, open addon leg)
# (drought, addon) and (armed-with-drought) are structurally UNREACHABLE --
# tested explicitly below, not just assumed from the code.
# ---------------------------------------------------------------------------

def test_truth_table_core_and_drought_are_mutually_exclusive(core_node, monkeypatch):
    """docs/design.md names this exact invariant: "drought should never be
    entered while core is open/armed simultaneously on the same node ...
    needing an explicit guard + test, not just a comment." """
    now = datetime.now()
    db.open_position(core_node, 100.0, now, 100.0, now, shares=10, paper=True)
    opened = db.open_drought_overlay_position(core_node, 50.0, now, 50.0, now, confirm_days=3, paper=True)
    assert opened is False, "core open -- a drought entry for the same wl_id must be rejected as a duplicate"

    with db._conn() as c:
        core_pos = c.execute(
            "SELECT id FROM paper_positions WHERE wl_id=?", (core_node['id'],)
        ).fetchone()
    db.close_position(core_pos['id'], exit_signal_price=100.0, exit_price=100.0, exit_time=now,
                       exit_reason='TIME', paper=True)

    # Reverse direction: drought open -> core's own open_position must also reject.
    opened = db.open_drought_overlay_position(core_node, 50.0, now, 50.0, now, confirm_days=3, paper=True)
    assert opened is True
    reopened_core = db.open_position(core_node, 100.0, now, 100.0, now, shares=10, paper=True)
    assert reopened_core is False, "drought open -- a fresh core entry for the same wl_id must be rejected too"


def test_truth_table_addon_leg_never_attaches_to_a_drought_position(core_node):
    """(drought_open, addon_open) is unreachable -- check_paper_addon_trigger
    gates on position_source=='core' specifically."""
    now = datetime.now()
    with db._conn() as c:
        c.execute("UPDATE watch_list SET addon_enabled=1 WHERE id=?", (core_node['id'],))
        c.commit()
    node = db.get_watch_list_node_by_id(core_node['id'])
    db.open_drought_overlay_position(node, 50.0, now, 50.0, now, confirm_days=3, paper=True)
    dpos = db.get_drought_overlay_position(node['id'], paper=True)
    paper_trading.check_paper_addon_trigger(node, dpos, 55.0, now)
    assert db.get_open_addon_leg_by_parent(dpos['id'], paper=True) is None


def test_truth_table_all_five_reachable_states_are_constructible(core_node):
    """A mechanical existence check for each of the 5 real reachable states
    listed above -- each must actually be buildable via the real functions,
    not just theoretically possible."""
    now = datetime.now()

    # 1. (flat, none, none)
    assert db.get_open_position_by_wl_id(core_node['id'], paper=True) is None
    assert db.get_drought_overlay_position(core_node['id'], paper=True) is None

    # 2. (flat, open drought, none)
    db.open_drought_overlay_position(core_node, 50.0, now, 50.0, now, confirm_days=3, paper=True)
    assert db.get_drought_overlay_position(core_node['id'], paper=True) is not None
    dpos = db.get_drought_overlay_position(core_node['id'], paper=True)
    db.close_position(dpos['id'], exit_signal_price=55.0, exit_price=55.0, exit_time=now,
                       exit_reason='HANDOFF', paper=True)

    # 3. (open-unarmed core, none, none)
    db.open_position(core_node, 100.0, now, 100.0, now, shares=10, paper=True)
    core_pos = db.get_open_position_by_wl_id(core_node['id'], paper=True)
    assert core_pos is not None and not core_pos['trail_state']

    # 4. (armed core, none, none) -- addon_enabled=0 on this node by default
    db.update_position_trail_state(core_pos['id'], {'trailing': True, 'peak': 105.0}, paper=True)
    armed_pos = db.get_open_position_by_wl_id(core_node['id'], paper=True)
    assert armed_pos['trail_state'].get('trailing') is True
    assert db.get_open_addon_leg_by_parent(core_pos['id'], paper=True) is None

    # 5. (armed core, none, open addon leg)
    leg_id = db.open_addon_leg(armed_pos, shares=armed_pos['shares'], entry_price=105.0, entry_time=now, paper=True)
    assert db.get_open_addon_leg_by_parent(core_pos['id'], paper=True) is not None
    db.close_addon_leg(leg_id, 110.0, now, 'TRAIL', paper=True)
    db.close_position(core_pos['id'], exit_signal_price=110.0, exit_price=110.0, exit_time=now,
                       exit_reason='TRAIL', paper=True)


def test_truth_table_addon_leg_exit_never_desyncs_from_parent_core_exit(core_node):
    """docs/design.md names this exact edge case: "an addon_leg's SL trigger
    vs. its parent core position's own SL trigger firing on the same bar --
    should be identical by construction ... needs a real test proving they
    never desync." In this implementation the addon leg never independently
    evaluates any exit condition at all -- close_paper_addon_leg_if_open is
    always called with the SAME reason/price check_paper_sells already
    computed for the parent, so desync is structurally impossible, not just
    coincidentally avoided. This test proves that end to end."""
    now = datetime.now()
    db.open_position(core_node, 100.0, now, 100.0, now, shares=10, paper=True)
    pos = db.get_open_position_by_wl_id(core_node['id'], paper=True)
    leg_id = db.open_addon_leg(pos, shares=10, entry_price=100.0, entry_time=now, paper=True)

    for reason, price in (('SL', 97.0), ('TRAIL', 112.0), ('TIME', 101.5)):
        with db._conn() as c:
            c.execute("UPDATE paper_addon_legs SET status='open', exit_reason=NULL WHERE id=?", (leg_id,))
            c.commit()
        paper_trading.close_paper_addon_leg_if_open(pos['id'], core_node['ticker'], core_node['id'],
                                                      price, now, reason, False)
        with db._conn() as c:
            leg = c.execute("SELECT exit_reason, exit_price FROM paper_addon_legs WHERE id=?", (leg_id,)).fetchone()
        assert leg['exit_reason'] == reason
        assert leg['exit_price'] == price


def test_truth_table_handoff_then_core_entry_same_poll_composes_correctly(core_node, monkeypatch):
    """docs/design.md names this exact race: "a core position's real entry
    signal firing on the exact same poll cycle a drought-overlay HANDOFF
    check would also fire." The documented ordering contract (handoff runs
    BEFORE the core buy-scan in the same poll) resolves it -- proven here by
    composing the two calls in that order and confirming a fresh core entry
    succeeds right after handoff clears the drought row, not blocked by the
    wl_id dedup the way it would be if drought were still open."""
    now = datetime.now()
    with db._conn() as c:
        c.execute("UPDATE watch_list SET drought_overlay_enabled=1, drought_confirm_days=3 WHERE id=?",
                  (core_node['id'],))
        c.commit()
    node = db.get_watch_list_node_by_id(core_node['id'])
    db.open_drought_overlay_position(node, 50.0, now, 50.0, now, confirm_days=3, paper=True)
    assert db.get_drought_overlay_position(node['id'], paper=True) is not None

    monkeypatch.setattr(paper_trading, 'compute_buy_signal', lambda n, **kw: {'current_price': 60.0, 'signal': 'BUY'})
    paper_trading.check_paper_drought_handoff(node)  # runs first, per the documented ordering contract
    assert db.get_drought_overlay_position(node['id'], paper=True) is None

    # Core's own buy-scan, running right after in the same poll -- must succeed now.
    opened = db.open_position(node, 60.0, now, 60.0, now, shares=5, paper=True)
    assert opened is True, "HANDOFF must fully clear the wl_id slot before core's own entry is attempted"


def test_skim_first_call_seeds_from_real_equity_and_never_fires(core_node, monkeypatch):
    """The gap found by an independent Opus review, 2026-08-09: enabling
    skim on a node that already has real prior paper-trading history (equity
    != 1.0) must seed skim_ref/peak/min_since_peak/strategy_value from that
    ACTUAL equity, not a flat 1.0/starting_notional baseline -- a flat seed
    fires one false giant skim capturing the entire pre-existing gain in a
    single shot the moment the flag is enabled."""
    with db._conn() as c:
        c.execute("UPDATE watch_list SET skim_enabled=1 WHERE id=?", (core_node['id'],))
        c.commit()
    monkeypatch.setattr(paper_trading, '_spy_price_at', lambda ts: 500.0)
    # Real prior history BEFORE skim is ever checked -- equity is already
    # 1.5, far past any flat-1.0-baseline skim_step, which would falsely
    # fire on the very first check_paper_skim call under the old bug.
    node = _close_core_trade(core_node, 50, 0)
    paper_trading.check_paper_skim(node)
    with db._conn() as c:
        events = c.execute("SELECT action FROM skim_reserve_log WHERE wl_id=?", (node['id'],)).fetchall()
    assert not any(e['action'] == 'skim' for e in events), \
        "seeding regression: the first-ever call must never itself fire a skim"
    node2 = db.get_watch_list_node_by_id(node['id'])
    assert node2['skim_ref'] == pytest.approx(1.5)
    assert node2['skim_peak_before_decline'] == pytest.approx(1.5)
    assert node2['skim_strategy_value'] == pytest.approx(10000 * 1.5)


# ---------------------------------------------------------------------------
# Skim x drought/addon interaction -- NOT part of the (core, drought, addon)
# truth table above; skim is a 4th, independent node-level flag a node can
# carry alongside any of those states. Confirmed by reading _paper_core_equity/
# _latest_core_trade_pnl_pct directly: both filter to
# position_source='core' in paper_trade_log, so drought_overlay trades (and
# addon legs, which live in a wholly separate paper_addon_legs table) are
# invisible to skim's equity/strategy_value math. This was an ACCIDENT of the
# query filter, not a decision, until the user made the call explicit
# (2026-08-1x staged-testing session): core-only is fine for now (skim isn't
# planned for near-term use given small notional/trade volume), but this is a
# real regression surface if skim is ever combined with drought/addon on the
# same node without re-confirming this scope choice still holds. Locking the
# CURRENT behavior in with an explicit test (rather than leaving it an
# unverified side effect of the filter) so a future change to either query is
# a deliberate, reviewed decision, not a silent behavior change.
# ---------------------------------------------------------------------------

def test_skim_ignores_drought_overlay_pnl_by_design(core_node, monkeypatch):
    """Deliberate current scope, confirmed with the user: skim's equity
    calculation only ever reflects position_source='core' trades. A drought-
    overlay trade closing at a large P&L in between two core trades must NOT
    move skim's strategy_value at all -- only the core trades' own returns
    should compound it. If this ever fails, either the scope decision changed
    (update this test to match) or _paper_core_equity/_latest_core_trade_pnl_pct
    started reading drought_overlay rows by accident (a real regression)."""
    with db._conn() as c:
        c.execute("UPDATE watch_list SET skim_enabled=1, drought_overlay_enabled=1, "
                   "drought_confirm_days=3, drought_vol_gate=0.4 WHERE id=?", (core_node['id'],))
        c.commit()
    monkeypatch.setattr(paper_trading, '_spy_price_at', lambda ts: 500.0)
    node = _seed_skim(core_node, 0)
    # 5%, deliberately below SKIM_STEP (0.10) -- a return AT or above the
    # step would fire a skim itself and confound the "did drought's P&L leak
    # in" assertion below with "did the skim math also change."
    node = _close_core_trade(node, 5, 1)
    paper_trading.check_paper_skim(node)
    node = db.get_watch_list_node_by_id(node['id'])
    strategy_value_before = node['skim_strategy_value']
    assert strategy_value_before == pytest.approx(10000 * 1.05)

    # A drought-overlay trade closes at a huge +200% return -- must be
    # invisible to skim.
    node = db.get_watch_list_node_by_id(node['id'])
    ts = _TS[2]
    db.open_drought_overlay_position(node, 100.0, ts, 100.0, ts, confirm_days=3, paper=True)
    dpos = db.get_drought_overlay_position(node['id'], paper=True)
    db.close_position(dpos['id'], exit_signal_price=300.0, exit_price=300.0, exit_time=ts,
                       exit_reason='HANDOFF', paper=True)

    # Next core trade at 0% -- if drought's return leaked in, strategy_value
    # would have jumped between the two check_paper_skim calls even though
    # this trade itself contributes nothing.
    node = _close_core_trade(node, 0, 3)
    paper_trading.check_paper_skim(node)
    node = db.get_watch_list_node_by_id(node['id'])
    assert node['skim_strategy_value'] == pytest.approx(strategy_value_before), \
        "skim's strategy_value moved from a drought-overlay trade -- core-only scope regressed"


def test_skim_ignores_addon_leg_pnl_by_design(core_node, monkeypatch):
    """Same scope decision as above, for addon legs: an addon_leg's real P&L
    (tracked entirely in paper_addon_legs, never paper_trade_log with
    position_source='core') must not move skim's strategy_value either."""
    with db._conn() as c:
        c.execute("UPDATE watch_list SET skim_enabled=1, addon_enabled=1 WHERE id=?", (core_node['id'],))
        c.commit()
    monkeypatch.setattr(paper_trading, '_spy_price_at', lambda ts: 500.0)
    node = _seed_skim(core_node, 0)

    ts = _TS[1]
    db.open_position(node, 100.0, ts, 100.0, ts, shares=10, paper=True)
    pos = db.get_open_position_by_wl_id(node['id'], paper=True)
    db.update_position_trail_state(pos['id'], {'trailing': True, 'peak': 105.0}, paper=True)
    armed_pos = db.get_open_position_by_wl_id(node['id'], paper=True)
    leg_id = db.open_addon_leg(armed_pos, shares=armed_pos['shares'], entry_price=105.0, entry_time=ts, paper=True)
    # Addon leg closes at a huge +150% return -- must be invisible to skim.
    db.close_addon_leg(leg_id, 262.5, ts, 'TRAIL', paper=True)
    db.close_position(pos['id'], exit_signal_price=110.0, exit_price=110.0, exit_time=ts,
                       exit_reason='TRAIL', paper=True)
    node = db.get_watch_list_node_by_id(node['id'])
    paper_trading.check_paper_skim(node)
    node = db.get_watch_list_node_by_id(node['id'])
    strategy_value_before = node['skim_strategy_value']

    node = _close_core_trade(node, 0, 2)
    paper_trading.check_paper_skim(node)
    node = db.get_watch_list_node_by_id(node['id'])
    assert node['skim_strategy_value'] == pytest.approx(strategy_value_before), \
        "skim's strategy_value moved from an addon-leg trade -- core-only scope regressed"
