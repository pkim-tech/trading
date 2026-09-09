"""Tests for signals_invariants.check_core_entry_notional_matches_sizing and
check_addon_leg_shares_matches_parent, added 2026-09-08 after trading_incident #17
(UGL core entry: 181sh @ $51.11 = $9,251.80 real notional vs a corrected
_last_sale_recovery target of $4,460.64 -- the exact bug fixed in commit 8b87283,
7.5h before this check existed to catch it)."""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import signals_invariants

TICKER = 'TEST_SIZING_FILL_CHECK'


@pytest.fixture
def env(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    signals_db.ensure_tables()
    signals_db.add_node(
        TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
        stop_loss=1, max_hold_hours=100, state='live', trail_buy_pct=1.0, trail_pct=1.0,
        fixed_sl_override=1.0, account='brokerage', starting_notional=5000)
    yield _node()['id']
    Path(tmp_db.name).unlink()


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _open_core_position(wl_id, entry_price, shares, signal_price=None):
    node = _node()
    now = datetime.now()
    signals_db.open_position(
        node, signal_price=signal_price or entry_price, signal_time=now,
        entry_price=entry_price, entry_time=now, shares=shares)


def test_clean_entry_at_starting_notional_not_flagged(env):
    """No prior closed trade -- _last_sale_recovery falls back to starting_notional
    ($5000). A real fill close to that target (within slippage) is clean."""
    _open_core_position(env, entry_price=100.0, shares=50)  # $5000 exactly
    assert signals_invariants.check_core_entry_notional_matches_sizing() == []


def test_ugl_shaped_incident_is_flagged(env):
    """Reproduces the real incident's shape: a fill roughly double the correct
    target is a real sizing bug, not slippage."""
    _open_core_position(env, entry_price=51.1149, shares=181)  # ~$9,251.80 vs $5000 target
    violations = signals_invariants.check_core_entry_notional_matches_sizing()
    assert len(violations) == 1
    assert TICKER in violations[0]
    assert 'sizing-calculation bug' in violations[0]


def test_small_notional_share_rounding_not_flagged(env):
    """A tiny-notional node (e.g. a canary/pilot) where whole-share flooring alone
    produces a large PERCENTAGE deviation must not false-positive -- e.g. a $500
    target at $178/share floors to 2 shares ($356, a real ~29% 'deviation' that
    is correct sizing, not a bug)."""
    signals_db.set_starting_notional_override(env, 500.0)
    _open_core_position(env, entry_price=178.80, shares=2)  # floor(500/178.80) = 2
    assert signals_invariants.check_core_entry_notional_matches_sizing() == []


def test_ordinary_slippage_within_tolerance_not_flagged(env):
    """A real fill within the tolerance band (worst-case trailing-buy padding, or
    a market-buy landing slightly off signal price) is expected, not a bug."""
    _open_core_position(env, entry_price=105.0, shares=55)  # $5775, +15.5% vs $5000
    assert signals_invariants.check_core_entry_notional_matches_sizing() == []


def test_starting_notional_override_once_consumed_is_used_as_expected_target(env):
    """open_position() unconditionally clears starting_notional_override_once in
    the same transaction as a real fill -- recomputing _last_sale_recovery
    AFTER that clear would always diverge from what real sizing used (a
    guaranteed false violation, paired-review HIGH finding, 2026-09-08). The
    real starting_notional_override_once_consumed coverage_event (logged by
    open_position itself) must be preferred instead."""
    signals_db.set_starting_notional_override_once(env, 8000.0)
    node = _node()
    entry_time = datetime.now()
    signals_db.open_position(node, signal_price=100.0, signal_time=entry_time,
                              entry_price=100.0, entry_time=entry_time, shares=80)  # $8000, matches the once value
    assert signals_db.get_watch_list_node_by_id(env)['starting_notional_override_once'] is None  # confirms it cleared
    assert signals_invariants.check_core_entry_notional_matches_sizing() == []


def test_drought_overlay_entry_is_checked_too(env):
    """A drought_overlay entry sizes through the SAME _last_sale_recovery call as
    core (real incident #15) -- excluding position_source='drought_overlay' would
    miss the identical bug class recurring there (paired-review HIGH finding)."""
    now = datetime.now()
    signals_db.open_position(
        _node(), signal_price=51.1149, signal_time=now, entry_price=51.1149, entry_time=now,
        shares=181, position_source='drought_overlay', drought_confirm_days=3, drought_vol_gate=0.4)
    violations = signals_invariants.check_core_entry_notional_matches_sizing()
    assert len(violations) == 1
    assert 'drought_overlay' in violations[0]


def test_high_price_low_share_count_oversizing_is_flagged_not_masked(env):
    """A flat share-count rounding floor doesn't scale down for a high-price/
    low-share-count node -- expected_shares=1.0 here, so a plain `abs(shares -
    expected_shares) <= 1.5` floor would have wrongly blessed a 2-share (100%,
    UGL-magnitude) oversize as 'just rounding' (independent-cold review finding,
    2026-09-08). The one-sided + absolute-dollar-bounded floor must not mask this."""
    signals_db.set_starting_notional_override(env, 100.0)
    _open_core_position(env, entry_price=100.0, shares=2)  # expected_shares=1.0, real=2 -> +$100, +100%
    violations = signals_invariants.check_core_entry_notional_matches_sizing()
    assert len(violations) == 1


def test_paper_node_not_checked(env):
    signals_db.set_node_state(env, 'paper')
    _open_core_position(env, entry_price=51.1149, shares=181)
    assert signals_invariants.check_core_entry_notional_matches_sizing() == []


def _open_addon_leg_row(wl_id, parent_position_id, parent_trade_log_id, shares, account='brokerage'):
    now = datetime.now()
    with signals_db._conn() as c:
        c.execute("""
            INSERT INTO addon_legs
                (wl_id, parent_position_id, parent_trade_log_id, ticker, account, shares,
                 entry_price, entry_time, status, is_dry_run_sim, entry_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', 0, 'filled')
        """, (wl_id, parent_position_id, parent_trade_log_id, TICKER, account, shares, 50.0,
              now.strftime('%Y-%m-%d %H:%M:%S')))
        c.commit()


def _open_position_row(wl_id):
    with signals_db._conn() as c:
        row = c.execute("SELECT id, trade_log_id FROM open_positions WHERE wl_id=?", (wl_id,)).fetchone()
    return row[0], row[1]


def test_addon_leg_matching_parent_shares_not_flagged(env):
    _open_core_position(env, entry_price=100.0, shares=50)
    pos_id, tl_id = _open_position_row(env)
    _open_addon_leg_row(env, pos_id, tl_id, shares=50)
    assert signals_invariants.check_addon_leg_shares_matches_parent() == []


def test_addon_leg_mismatched_parent_shares_flagged(env):
    """An add-on leg is supposed to mirror the parent's exact share count --
    any real deviation (e.g. a partial fill, a stale share-count read) is a
    genuine sizing mismatch, unlike the core check's percentage tolerance."""
    _open_core_position(env, entry_price=100.0, shares=50)
    pos_id, tl_id = _open_position_row(env)
    _open_addon_leg_row(env, pos_id, tl_id, shares=30)
    violations = signals_invariants.check_addon_leg_shares_matches_parent()
    assert len(violations) == 1
    assert '30' in violations[0] and "50" in violations[0]


def test_addon_leg_with_closed_parent_uses_trade_log_shares(env):
    """A leg can outlive its own parent's close (independent detection/
    reconciliation) -- must still resolve a real parent share count via
    parent_trade_log_id, not silently skip."""
    now = datetime.now()
    with signals_db._conn() as c:
        cur = c.execute("""
            INSERT INTO trade_log
                (ticker, strategy, version, window, stop_loss, max_hold_hours, account,
                 signal_price, signal_time, entry_price, entry_time, entry_drift_pct,
                 exit_price, exit_time, exit_reason, shares, is_dry_run_sim, position_source)
            VALUES (?, 'TrailingBothZScoreBreakout', 'test', 10, 1, 100, 'brokerage',
                    100.0, ?, 100.0, ?, 0.0, 110.0, ?, 'SL', 50, 0, 'core')
        """, (TICKER, now.strftime('%Y-%m-%d %H:%M:%S'), now.strftime('%Y-%m-%d %H:%M:%S'),
              now.strftime('%Y-%m-%d %H:%M:%S')))
        tl_id = cur.lastrowid
        c.execute("""
            INSERT INTO addon_legs
                (wl_id, parent_trade_log_id, ticker, account, shares, entry_price, entry_time,
                 status, is_dry_run_sim, entry_status)
            VALUES (?, ?, ?, 'brokerage', ?, 50.0, ?, 'closed', 0, 'filled')
        """, (env, tl_id, TICKER, 20, now.strftime('%Y-%m-%d %H:%M:%S')))
        c.commit()
    violations = signals_invariants.check_addon_leg_shares_matches_parent()
    assert len(violations) == 1
    assert '20' in violations[0] and '50' in violations[0]
