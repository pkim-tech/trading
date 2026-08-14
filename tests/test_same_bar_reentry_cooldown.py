"""Real incident, 2026-08-13: RETL/soxl_ira exited via TIME at 09:30:54, a fresh
trailing-buy order was placed at 09:31:54 -- the same hourly bar. The backtest kernel's
per-bar loop (_simulate_trail_both) structurally cannot produce this (an exit processed
on bar i only reaches the entry-check branch on bar i+1), so live needed an equivalent
minimum-1-bar gap. This exercises the real _scan_buy_signals cooldown gate
(active_signals.py) and its supporting DB plumbing (get_last_exit_bar_time,
close_position's exit_bar_time auto-derivation from trail_state['exit_decision_bar'],
_stash_exit_decision_bar) end to end, not just the gate's own boolean condition."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import active_signals
import signals_config
import signals_db as db

TICKER = 'TEST_COOLDOWN'


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    """autouse=True (not just requested per-test) -- a test in this file that forgets
    to declare `env` as a parameter still gets the DB_PATH isolation applied, closing
    off the exact class of mistake that wrote 7 rows into the real live
    cache/live/trading_live.db during this fix's own development (found by a
    contextual Opus review 2026-08-14, cleaned up by hand)."""
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    db.ensure_tables()
    posted = []
    monkeypatch.setattr(active_signals, '_post_message', lambda *a, **kw: (posted.append(a), (None, None))[1])
    monkeypatch.setattr(active_signals, 'notify_buy_signal', lambda node, sig: posted.append(('BUY', node['ticker'])))
    return posted


def _add_node(account='soxl_ira'):
    # take_profit actually stores arm_sell_pct for TrailingBothZScoreBreakout (see
    # add_node's own comment) -- 30.0 mirrors a typical real node's arm threshold.
    # state='live' -- default is 'paper', which routes _scan_buy_signals to the paper
    # branch entirely, never reaching the real/dry_run cooldown gate under test here.
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v4', window=10, take_profit=30.0,
                stop_loss=1.0, max_hold_hours=11, account=account, fixed_sl_override=50.0,
                state='live')
    node = db.get_watch_list_node(TICKER, account=account, watchlist_id=False)
    return node


def _close_with_exit_bar(node, exit_bar_time):
    """Mirrors the real flow: open a position, decide a SELL on `exit_bar_time`
    (_stash_exit_decision_bar), then close -- exactly the sequence
    _scan_pinned_exit_arm/_check_position_exit follow."""
    db.open_position(node, signal_price=10.0, signal_time=exit_bar_time,
                      entry_price=10.0, entry_time=exit_bar_time, shares=10)
    pos = db.get_open_position_by_wl_id(node['id'])
    active_signals._stash_exit_decision_bar(pos, exit_bar_time)
    db.close_position(pos['id'], exit_signal_price=9.5, exit_price=9.5,
                       exit_time=exit_bar_time, exit_reason='TIME')


def test_entry_on_same_bar_as_last_exit_is_suppressed(env, monkeypatch):
    node = _add_node()
    exit_bar = datetime(2026, 8, 13, 9, 30, 0)
    _close_with_exit_bar(node, exit_bar)

    # A fresh BUY signal computed for the SAME bar the exit was decided on -- the
    # exact RETL shape (exit and re-entry both landing in the 09:30 bar).
    monkeypatch.setattr(active_signals, 'compute_buy_signal',
                         lambda n, price_override=None: {
                             'ticker': TICKER, 'window': n['window'], 'signal': 'BUY',
                             'z_score': -2.0, 'last_bar': exit_bar,
                         })
    active_signals._scan_buy_signals([node], set(), {})

    assert not any(p[0] == 'BUY' for p in env), "BUY should have been suppressed by the same-bar cooldown"
    events = db.get_coverage_events(scenario_key='same_bar_reentry_cooldown')
    assert any(e['ticker'] == TICKER and e['result'] == 'suppressed' for e in events)


def test_entry_on_a_strictly_newer_bar_proceeds(env, monkeypatch):
    node = _add_node()
    exit_bar = datetime(2026, 8, 13, 9, 30, 0)
    _close_with_exit_bar(node, exit_bar)

    later_bar = datetime(2026, 8, 13, 10, 30, 0)
    monkeypatch.setattr(active_signals, 'compute_buy_signal',
                         lambda n, price_override=None: {
                             'ticker': TICKER, 'window': n['window'], 'signal': 'BUY',
                             'z_score': -2.0, 'last_bar': later_bar,
                         })
    active_signals._scan_buy_signals([node], set(), {})

    assert any(p[0] == 'BUY' and p[1] == TICKER for p in env), "BUY on a genuinely newer bar must not be blocked"
    events = db.get_coverage_events(scenario_key='same_bar_reentry_cooldown')
    assert not any(e['ticker'] == TICKER and e['result'] == 'suppressed' for e in events)


def test_no_prior_exit_never_blocks_entry(env, monkeypatch):
    """A node that has never closed a real trade has no exit_bar_time to compare
    against -- get_last_exit_bar_time returns None, and the gate must fail open."""
    node = _add_node()
    bar = datetime(2026, 8, 13, 9, 30, 0)
    monkeypatch.setattr(active_signals, 'compute_buy_signal',
                         lambda n, price_override=None: {
                             'ticker': TICKER, 'window': n['window'], 'signal': 'BUY',
                             'z_score': -2.0, 'last_bar': bar,
                         })
    active_signals._scan_buy_signals([node], set(), {})
    assert any(p[0] == 'BUY' and p[1] == TICKER for p in env)


def test_drought_leg_closing_after_core_does_not_mask_the_core_exit_bar(env):
    """Real bug found by contextual Opus review 2026-08-14, fixed before this shipped:
    get_last_exit_bar_time originally had no position_source filter. RETL (the incident
    node) runs both drought and add-on -- a drought-overlay leg sharing the same wl_id
    can close AFTER the core position, through call sites this fix's
    _stash_exit_decision_bar was never added to (close_addon_leg_real_if_open, the
    drought HANDOFF close), leaving exit_bar_time NULL on that later row. Without the
    position_source='core' filter, that NULL row would win "most recent by exit_time"
    and silently disarm the cooldown on exactly the node it exists to protect."""
    node = _add_node()
    core_exit_bar = datetime(2026, 8, 13, 9, 30, 0)
    _close_with_exit_bar(node, core_exit_bar)

    # A drought-overlay leg for the SAME node, closing LATER, with no exit_bar_time --
    # mirrors close_addon_leg_real_if_open/the HANDOFF close path, neither of which
    # stashes exit_decision_bar.
    db.open_position(node, signal_price=10.0, signal_time=datetime(2026, 8, 13, 11, 30, 0),
                      entry_price=10.0, entry_time=datetime(2026, 8, 13, 11, 30, 0), shares=5,
                      position_source='drought_overlay')
    drought_pos = db.get_open_position_by_wl_id(node['id'])
    db.close_position(drought_pos['id'], exit_signal_price=9.5, exit_price=9.5,
                       exit_time=datetime(2026, 8, 13, 12, 0, 0), exit_reason='HANDOFF')

    assert db.get_last_exit_bar_time(node['id']) == str(core_exit_bar), \
        "the later, exit_bar_time-less drought leg must not mask the real core exit's bar"


def test_unparseable_exit_bar_time_fails_open_without_crashing_the_scan(env, monkeypatch):
    """Real bug found by cold Opus review 2026-08-14, fixed before this shipped: the
    original write side used str() (not .strftime), so a tz-aware or sub-second-precision
    bar could write a format datetime.strptime's fixed '%Y-%m-%d %H:%M:%S' pattern can't
    parse -- an uncaught ValueError there would abort _scan_buy_signals entirely (every
    node in the batch, not just this one). Writes a corrupt exit_bar_time directly
    (bypassing _stash_exit_decision_bar, which is now fixed) to exercise the read side's
    defensive handling on its own."""
    node = _add_node()
    db.open_position(node, signal_price=10.0, signal_time=datetime(2026, 8, 13, 9, 30, 0),
                      entry_price=10.0, entry_time=datetime(2026, 8, 13, 9, 30, 0), shares=10)
    pos = db.get_open_position_by_wl_id(node['id'])
    db.update_position_trail_state(pos['id'], {'exit_decision_bar': '2026-08-13T09:30:00-04:00'})
    db.close_position(pos['id'], exit_signal_price=9.5, exit_price=9.5,
                       exit_time=datetime(2026, 8, 13, 9, 30, 0), exit_reason='TIME')

    bar = datetime(2026, 8, 13, 9, 30, 0)
    monkeypatch.setattr(active_signals, 'compute_buy_signal',
                         lambda n, price_override=None: {
                             'ticker': TICKER, 'window': n['window'], 'signal': 'BUY',
                             'z_score': -2.0, 'last_bar': bar,
                         })
    # Must not raise -- this is the whole point of the test.
    active_signals._scan_buy_signals([node], set(), {})
    assert any(p[0] == 'BUY' and p[1] == TICKER for p in env), \
        "unparseable exit_bar_time must fail OPEN (allow the entry), not block it or crash"
    events = db.get_coverage_events(scenario_key='same_bar_reentry_cooldown')
    assert any(e['ticker'] == TICKER and e['result'] == 'unparseable_exit_bar' for e in events)


def test_get_last_exit_bar_time_reflects_the_real_exit_path(env):
    """Direct DB-layer check: close_position's auto-derivation from
    trail_state['exit_decision_bar'] actually lands in trade_log.exit_bar_time --
    this is the column that was silently never populated for real exits before
    2026-08-14 (only paper_trading.py wrote it).

    env is required here even though this test never touches it directly -- omitting
    it (as an earlier version of this file did) skips the signals_config.DB_PATH
    monkeypatch entirely, silently writing test rows into the REAL live
    cache/live/trading_live.db instead of the isolated tmp_db (found by a contextual
    Opus review 2026-08-14, after it had already happened once -- 7 rows had to be
    cleaned up by hand). Every test in this module must take `env`, whether or not
    it reads the fixture's return value, or pytest will not apply the patch."""
    node = _add_node()
    exit_bar = datetime(2026, 8, 13, 14, 30, 0)
    _close_with_exit_bar(node, exit_bar)
    assert db.get_last_exit_bar_time(node['id']) == str(exit_bar)
