"""fake_broker scenario for the ground-truth broker sweep itself
(scripts/check_untracked_positions.check_account/run_full_sweep) -- the exact
6-step scenario the 2026-08-14 incident plan specified for "Stage D."

This closes a real pre-existing gap, not just a new one: the sweep was built
2026-08-07 after the GDXU incident and has run automatically against the real
broker every morning at 07:00 since 2026-08-08, with ZERO test coverage of any
kind. Its wrapper's gating/throttle is covered by
test_orphaned_broker_position_sweep.py; this file covers the sweep's actual
broker-vs-local comparison, driven through a real evolving order book rather
than a mocked return value.

Step 1 (a real filled position with no local open_positions row) reproduces the
literal SOXS/2026-08-14 and GDXU/2026-08-07 condition directly.
"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import signals_config
import signals_db
import schwab_safety
import schwab_client
from scripts.check_untracked_positions import check_account

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_SWEEP_SCENARIO'
ACCOUNT = 'soxl_ira'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    monkeypatch.setattr(schwab_safety, 'STATE_PATH', tmp_path / "schwab_order_counts.json")
    monkeypatch.setattr(schwab_safety, 'KILL_SWITCH_PATH', tmp_path / "schwab_kill_switch.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account=?, starting_notional=800 WHERE ticker=?",
                   (ACCOUNT, TICKER))
        c.commit()
    yield
    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _real_filled_buy(fake_broker, shares, price=10.0):
    """Creates a genuinely FILLED BUY at the fake broker -- fake_broker's
    get_account nets FILLED BUY/SELL legs into the positions list that
    schwab_client.get_all_real_positions really reads, so this produces a real
    broker position by the same route production does."""
    fake_broker.set_quote(TICKER, last=price, bid=price, ask=price + 0.01)
    order_id = fake_broker.seed_resting_order(ACCOUNT, TICKER, 'MARKET', 'BUY', shares)
    fake_broker.force_fill(order_id, price=price)
    return order_id


def _open_local_position(shares, price=10.0):
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=price, signal_time=now, entry_price=price,
                              entry_time=now, shares=shares)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account=? WHERE ticker=?", (ACCOUNT, TICKER))
        c.commit()


def test_real_broker_position_with_no_local_row_is_flagged_untracked(env, fake_broker):
    # Steps 1-3: a real filled position, zero local rows, run the sweep.
    _real_filled_buy(fake_broker, shares=19.0)
    assert schwab_client.get_all_real_positions(ACCOUNT) == {TICKER: 19.0}, (
        "test premise: the fake broker really reports this as a held position")

    findings = check_account(ACCOUNT)

    assert len(findings) == 1, findings
    assert 'UNTRACKED' in findings[0], findings[0]
    assert TICKER in findings[0] and '19' in findings[0], findings[0]


def test_real_position_with_a_matching_local_row_is_clean(env, fake_broker):
    # Step 4: negative case.
    _real_filled_buy(fake_broker, shares=19.0)
    _open_local_position(shares=19.0)
    assert check_account(ACCOUNT) == []


def test_share_count_drift_between_broker_and_local_is_flagged_mismatch(env, fake_broker):
    _real_filled_buy(fake_broker, shares=19.0)
    _open_local_position(shares=12.0)
    findings = check_account(ACCOUNT)
    assert len(findings) == 1 and 'MISMATCH' in findings[0], findings
    assert '19' in findings[0] and '12' in findings[0], findings[0]


def test_local_row_the_broker_no_longer_backs_is_flagged_stale(env, fake_broker):
    """Mirror image of UNTRACKED: the position closed at the broker (or never
    really existed) while the local record still claims it's open."""
    _open_local_position(shares=19.0)
    findings = check_account(ACCOUNT)
    assert len(findings) == 1 and 'STALE' in findings[0], findings


def test_sweep_is_read_only_and_never_creates_the_missing_row(env, fake_broker):
    # Step 5: automation_principles.md #5 -- detection only.
    _real_filled_buy(fake_broker, shares=19.0)
    check_account(ACCOUNT)
    with signals_db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM open_positions").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM addon_legs").fetchone()[0] == 0
    assert schwab_client.get_all_real_positions(ACCOUNT) == {TICKER: 19.0}, (
        "and it must not have touched the broker either")


def test_dry_run_sim_local_row_does_not_mask_a_real_untracked_broker_position(env, fake_broker):
    """Step 6 regression. is_dry_run_sim positions are synthesized locally and
    have no broker counterpart by design, so the sweep's local-side query
    excludes them (is_dry_run_sim=0). This pins that a synthetic row can't
    accidentally satisfy the check for a genuinely untracked REAL position --
    which would silently reintroduce the whole gap."""
    _real_filled_buy(fake_broker, shares=19.0)
    node = _node()
    now = datetime.now()
    signals_db.open_position(node, signal_price=10.0, signal_time=now, entry_price=10.0,
                              entry_time=now, shares=19.0, is_dry_run_sim=True)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account=? WHERE ticker=?", (ACCOUNT, TICKER))
        c.commit()

    findings = check_account(ACCOUNT)
    assert len(findings) == 1 and 'UNTRACKED' in findings[0], findings


def test_hand_held_ticker_never_in_the_watchlist_is_informational_not_a_finding(env, fake_broker):
    """The user's own non-algo holdings must not render as permanent red --
    an earlier version of this sweep did exactly that (caught by paired Opus
    review) and would have trained the operator to ignore it."""
    fake_broker.set_quote('TEST_HANDHELD', last=10.0, bid=10.0, ask=10.01)
    oid = fake_broker.seed_resting_order(ACCOUNT, 'TEST_HANDHELD', 'MARKET', 'BUY', 100.0)
    fake_broker.force_fill(oid, price=10.0)
    assert check_account(ACCOUNT) == []
