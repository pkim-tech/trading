"""signals_helpers.get_full_position_state (2026-08-10): merges
db.get_real_position_state's local view (pending_buys/open_positions/
trade_log) with a fresh broker-side read (real shares, resting orders, cash)
-- the "check both sides" ground-truth call this project was missing (see
its own docstring for the full history/motivation). schwab_client calls are
monkeypatched here rather than driven through tests/fake_broker.py -- this
function only ever calls 3 read-only schwab_client functions directly
(get_real_position/get_real_orders/get_account_balance), it never places an
order, so fake_broker's full stateful order-book machinery isn't needed.

A paired Opus review (independent-cold + contextual) of the first version
found 3 real false-positive sources -- state=='live' alone doesn't mean a
real broker order exists (roth/brokerage carry live nodes at
trading_enabled=False, which fill via the dry-run-sim synthesis path), a
pending_buys row with order_placed=0 is the normal pre-placement window not
a mismatch, and add-on leg shares live outside open_positions.shares -- plus
a docstring claim about the UNIQUE constraint that was simply false. The
tests below cover each finding directly, not just the happy path the first
version's tests were scoped to."""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import schwab_client
import schwab_safety
import signals_config
import signals_db as db
from signals_helpers import get_full_position_state

TICKER = 'TEST_FULL_STATE'


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    db.ensure_tables()
    yield db
    os.unlink(tmp_db.name)


@pytest.fixture(autouse=True)
def _real_account(monkeypatch):
    """effectively_dry_run(account, node) checks schwab_safety.ACCOUNTS[account]
    .trading_enabled, not just node.state -- give every test a real account by
    default so 'live' actually means real (a test relying on real ACCOUNTS
    config, like the true first version's bug, would silently pass/fail
    depending on today's live-trading rollout state instead of what's being
    tested)."""
    fake_limits = type('L', (), {'trading_enabled': True})()
    monkeypatch.setitem(schwab_safety.ACCOUNTS, 'TEST_REAL_ACCT', fake_limits)
    fake_dry_limits = type('L', (), {'trading_enabled': False})()
    monkeypatch.setitem(schwab_safety.ACCOUNTS, 'TEST_DRYRUN_ACCT', fake_dry_limits)


def _set_account_and_state(node_id, account, state):
    with db._conn() as c:
        c.execute("UPDATE watch_list SET account = ?, state = ? WHERE id = ?", (account, state, node_id))
        c.commit()


def _make_node(account='TEST_REAL_ACCT', state='live'):
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', window=5,
                take_profit=5.0, stop_loss=1, max_hold_hours=48)
    n = [x for x in db.get_watchlist() if x['ticker'] == TICKER][0]
    _set_account_and_state(n['id'], account, state)
    return db.get_watch_list_node_by_id(n['id'])


def test_no_account_short_circuits_to_local_only(isolated_db):
    db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'v5', window=5,
                take_profit=5.0, stop_loss=1, max_hold_hours=48)
    n = [x for x in db.get_watchlist() if x['ticker'] == TICKER][0]
    state = get_full_position_state(n['id'])
    assert state['broker_shares'] is None
    assert state['broker_fetch_error'] is None
    assert state['mismatches'] == []


def test_broker_fetch_failure_recorded_not_raised(isolated_db, monkeypatch):
    node = _make_node()

    def _boom(account, ticker):
        raise RuntimeError('API down')
    monkeypatch.setattr(schwab_client, 'get_real_position', _boom)
    state = get_full_position_state(node['id'])
    assert state['broker_fetch_error'] == 'API down'
    assert state['mismatches'] == []


def test_dry_run_account_never_flags_mismatch_despite_state_live(isolated_db, monkeypatch):
    """Direct regression for the HIGH finding both review passes independently
    caught: a node can be state='live' while its ACCOUNT is
    trading_enabled=False (roth/brokerage carry real state='live' nodes like
    this today) -- fills for it are synthesized via
    signals_notify.update_dry_run_buys, never touch the broker, so comparing
    against a broker read must never happen for it. Before the fix, this
    exact setup produced a permanent false 'broker holds 0' mismatch."""
    node = _make_node(account='TEST_DRYRUN_ACCT', state='live')
    now = datetime.now()
    db.open_position(node, signal_price=100.0, signal_time=now, entry_price=101.0,
                      entry_time=now, shares=10)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda a, t: 0.0)
    monkeypatch.setattr(schwab_client, 'get_real_orders', lambda a, t: [])
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: 5000.0)
    state = get_full_position_state(node['id'])
    assert state['broker_shares'] == 0.0  # raw fact still returned
    assert state['mismatches'] == []  # but never asserted as a finding


def test_holding_share_count_matches_no_mismatch(isolated_db, monkeypatch):
    node = _make_node()
    now = datetime.now()
    db.open_position(node, signal_price=100.0, signal_time=now, entry_price=101.0,
                      entry_time=now, shares=10)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda a, t: 10.0)
    monkeypatch.setattr(schwab_client, 'get_real_orders', lambda a, t: [])
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: 5000.0)
    state = get_full_position_state(node['id'])
    assert state['status'] == 'holding'
    assert state['broker_cash'] == 5000.0
    assert state['mismatches'] == []


def test_holding_share_count_mismatch_flagged(isolated_db, monkeypatch):
    node = _make_node()
    now = datetime.now()
    db.open_position(node, signal_price=100.0, signal_time=now, entry_price=101.0,
                      entry_time=now, shares=10)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda a, t: 7.0)
    monkeypatch.setattr(schwab_client, 'get_real_orders', lambda a, t: [])
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: 5000.0)
    state = get_full_position_state(node['id'])
    assert any('share-count mismatch' in m for m in state['mismatches'])


def test_holding_broker_shows_zero_flagged(isolated_db, monkeypatch):
    node = _make_node()
    now = datetime.now()
    db.open_position(node, signal_price=100.0, signal_time=now, entry_price=101.0,
                      entry_time=now, shares=10)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda a, t: 0.0)
    monkeypatch.setattr(schwab_client, 'get_real_orders', lambda a, t: [])
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: 5000.0)
    state = get_full_position_state(node['id'])
    assert any('broker holds 0' in m for m in state['mismatches'])


def test_holding_addon_leg_shares_included_in_comparison(isolated_db, monkeypatch):
    """Direct regression for the HIGH finding both review passes independently
    caught: addon_legs is a deliberately separate table from open_positions
    (9 real soxl_ira nodes carry addon_enabled=1 today), so the broker's real
    total is core+leg while open_positions.shares alone is core-only. Before
    the fix, a healthy core(10)+leg(10)=20 position would have false-flagged
    as 'local=10 broker=20'."""
    node = _make_node()
    now = datetime.now()
    db.open_position(node, signal_price=100.0, signal_time=now, entry_price=101.0,
                      entry_time=now, shares=10)
    parent = db.get_open_position_by_wl_id(node['id'])
    db.open_addon_leg(parent, shares=10, entry_price=102.0, entry_time=now, paper=False)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda a, t: 20.0)
    monkeypatch.setattr(schwab_client, 'get_real_orders', lambda a, t: [])
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: 5000.0)
    state = get_full_position_state(node['id'])
    assert state['mismatches'] == []


def test_pending_entry_order_not_yet_placed_is_not_a_mismatch(isolated_db, monkeypatch):
    """Direct regression for the MEDIUM-HIGH/HIGH finding both review passes
    independently caught: order_placed=0 is the NORMAL window between a BUY
    signal firing and the order actually being placed (the manual 3-step
    Slack flow, or automation catching up) -- not evidence anything is
    wrong. Before the fix, every live BUY signal would false-flag during
    this window."""
    node = _make_node()
    now = datetime.now()
    sig = dict(current_price=100.0, last_bar=now)
    db.add_pending_buy(node, sig, channel='C1', ts='1.0')
    pb = db.get_pending_buy_by_wl_id(node['id'])
    assert pb['order_placed'] == 0  # sanity: this is the real default
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda a, t: 0.0)
    monkeypatch.setattr(schwab_client, 'get_real_orders', lambda a, t: [])
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: 5000.0)
    state = get_full_position_state(node['id'])
    assert state['mismatches'] == []


def test_pending_entry_placed_but_no_resting_broker_order_flagged(isolated_db, monkeypatch):
    node = _make_node()
    now = datetime.now()
    sig = dict(current_price=100.0, last_bar=now)
    db.add_pending_buy(node, sig, channel='C1', ts='1.0')
    db.mark_pending_buy_placed_by_wl_id(node['id'])
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda a, t: 0.0)
    monkeypatch.setattr(schwab_client, 'get_real_orders', lambda a, t: [])
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: 5000.0)
    state = get_full_position_state(node['id'])
    assert any('no matching resting BUY order' in m for m in state['mismatches'])


def test_pending_entry_placed_with_resting_buy_order_no_mismatch(isolated_db, monkeypatch):
    node = _make_node()
    now = datetime.now()
    sig = dict(current_price=100.0, last_bar=now)
    db.add_pending_buy(node, sig, channel='C1', ts='1.0')
    db.mark_pending_buy_placed_by_wl_id(node['id'])
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda a, t: 0.0)
    monkeypatch.setattr(schwab_client, 'get_real_orders',
                         lambda a, t: [{'status': 'WORKING', 'orderType': 'TRAILING_STOP', 'instruction': 'BUY'}])
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: 5000.0)
    state = get_full_position_state(node['id'])
    assert state['mismatches'] == []


def test_pending_entry_placed_resting_sell_order_does_not_satisfy(isolated_db, monkeypatch):
    """A leftover protective SELL/stop resting for the ticker must not mask a
    genuinely missing entry BUY order -- the two review passes both flagged
    the unfiltered any-resting-order check as a false negative."""
    node = _make_node()
    now = datetime.now()
    sig = dict(current_price=100.0, last_bar=now)
    db.add_pending_buy(node, sig, channel='C1', ts='1.0')
    db.mark_pending_buy_placed_by_wl_id(node['id'])
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda a, t: 0.0)
    monkeypatch.setattr(schwab_client, 'get_real_orders',
                         lambda a, t: [{'status': 'WORKING', 'orderType': 'STOP', 'instruction': 'SELL'}])
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: 5000.0)
    state = get_full_position_state(node['id'])
    assert any('no matching resting BUY order' in m for m in state['mismatches'])


def test_flat_orphaned_broker_position_flagged(isolated_db, monkeypatch):
    node = _make_node()
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda a, t: 25.0)
    monkeypatch.setattr(schwab_client, 'get_real_orders', lambda a, t: [])
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: 5000.0)
    state = get_full_position_state(node['id'])
    assert state['status'] == 'flat'
    assert any('orphaned real position' in m for m in state['mismatches'])


def test_holding_paper_with_real_orphan_still_flagged(isolated_db, monkeypatch):
    """Direct regression for a finding on the first version: branching on the
    merged `status` string (which collapses to 'holding_paper' when only a
    paper position exists) would skip real-orphan detection entirely for a
    live node that also happens to carry a paper position. Branching on the
    real local fields directly (real_position/pending_buy) instead of
    `status` closes this."""
    node = _make_node()
    now = datetime.now()
    db.open_position(node, signal_price=100.0, signal_time=now, entry_price=101.0,
                      entry_time=now, shares=5, paper=True)
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda a, t: 25.0)
    monkeypatch.setattr(schwab_client, 'get_real_orders', lambda a, t: [])
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: 5000.0)
    state = get_full_position_state(node['id'])
    assert state['status'] == 'holding_paper'  # paper axis unaffected
    assert any('orphaned real position' in m for m in state['mismatches'])


def test_non_live_node_never_flags_mismatch_even_with_broker_shares(isolated_db, monkeypatch):
    """A ticker+account is routinely shared by one 'live' node plus several
    'paper'/'research' clones -- the broker has no wl_id concept, so a real
    position at that (ticker, account) could legitimately belong to a
    DIFFERENT live sibling node. Comparing a non-live node's local 'flat'
    status against the shared account's broker position would misattribute
    that sibling's real position as this node's own orphaned position."""
    node = _make_node(state='paper')
    monkeypatch.setattr(schwab_client, 'get_real_position', lambda a, t: 25.0)
    monkeypatch.setattr(schwab_client, 'get_real_orders',
                         lambda a, t: [{'status': 'WORKING', 'orderType': 'TRAILING_STOP', 'instruction': 'BUY'}])
    monkeypatch.setattr(schwab_client, 'get_account_balance', lambda a: 5000.0)
    state = get_full_position_state(node['id'])
    assert state['broker_shares'] == 25.0  # raw facts still returned
    assert state['mismatches'] == []  # but never asserted as a finding for a non-live node
