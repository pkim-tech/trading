"""Tests for _post_message's incident=True alert gate anchoring on the
POSITION's own recorded ground truth (open_positions.is_dry_run_sim, stamped at
entry time) rather than re-deriving effectively_dry_run from the node's CURRENT
state/account.

Backlog item (2026-08-17): a node demoted to paper/dry_run -- or an account
deliberately stopped -- while a real (is_dry_run_sim=0) position and real
resting broker orders still existed would have silenced exactly that position's
UNPROTECTED/reconciliation alerts, precisely when a human has just intervened
and most needs visibility. Also covers the same entry's residual (b): a
coverage_snoozes lever for alert_stale_price_exit_suppressed.
"""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import schwab_safety
import signals_blocks
import signals_config
import signals_db
import signals_notify

TICKER = 'TEST_INCIDENT_GATE'

# Captured at import time, BEFORE tests/conftest.py's autouse _no_real_slack_posts
# fixture swaps signals_blocks._post_message for a no-op -- these tests are about
# the gate INSIDE that function, so the no-op stand-in would test nothing. Safe:
# the fixture below forces SOCKET_MODE/SLACK_HOOK off, so the real function can
# only reach its console branch, never a live Slack send.
_real_post_message = signals_blocks._post_message


def _limits(trading_enabled):
    return schwab_safety.AccountLimits(enabled=True, notional_cap=100_000, daily_order_cap=100,
                                        trading_enabled=trading_enabled, cash_settlement_type='cash')


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    # SOCKET_MODE/SLACK_HOOK both off -> the real _post_message falls through to
    # the console branch, so we can exercise the genuine gate (not the autouse
    # conftest no-op) and read the suppress decision off slack_message_log.
    monkeypatch.setattr(signals_config, 'SOCKET_MODE', False)
    monkeypatch.setattr(signals_config, 'SLACK_HOOK', None)
    monkeypatch.setattr(signals_config, 'SIM_MODE', True)

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=7,
                         stop_loss=5, max_hold_hours=7, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='roth' WHERE ticker=?", (TICKER,))
        c.commit()
    node = [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]
    now = datetime.now()
    signals_db.open_position(node, signal_price=50.0, signal_time=now, entry_price=50.0,
                              entry_time=now, shares=100)
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET account='roth' WHERE ticker=?", (TICKER,))
        c.commit()

    # A real, trading-enabled account, so the node starts out NOT
    # effectively_dry_run -- the demotions below are what change that.
    monkeypatch.setitem(schwab_safety.ACCOUNTS, 'roth', _limits(trading_enabled=True))
    yield
    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _pos():
    return signals_db.get_open_position(TICKER)


def _sent(before_count):
    """True if the most recent slack_message_log row was actually sent rather
    than gate-suppressed. _post_message writes exactly one row either way."""
    with signals_db._conn() as c:
        rows = c.execute("SELECT mode FROM slack_message_log ORDER BY id").fetchall()
    assert len(rows) == before_count + 1, f"expected exactly 1 new log row, got {len(rows) - before_count}"
    return rows[-1]['mode'] != 'suppressed'


def _log_count():
    with signals_db._conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM slack_message_log").fetchone()['n']


def _demote_node(state):
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET state=? WHERE ticker=?", (state, TICKER))
        c.commit()


# --- the actual gap being closed -------------------------------------------

@pytest.mark.parametrize('demotion', ['paper', 'dry_run'])
def test_real_position_still_alerts_after_its_node_is_demoted(env, demotion):
    pos = _pos()
    assert pos['is_dry_run_sim'] == 0
    _demote_node(demotion)
    # Sanity: the OLD gate would have suppressed this.
    assert signals_blocks.effectively_dry_run(_node().get('account'), _node()) is True

    n = _log_count()
    _real_post_message("UNPROTECTED", node_id=pos['wl_id'], incident=True, pos=pos)
    assert _sent(n), "a real position's incident alert must survive its node being demoted"


def test_real_position_still_alerts_after_its_account_is_disabled(env, monkeypatch):
    pos = _pos()
    monkeypatch.setitem(schwab_safety.ACCOUNTS, 'roth', _limits(trading_enabled=False))
    assert signals_blocks.effectively_dry_run('roth', _node()) is True

    n = _log_count()
    _real_post_message("UNPROTECTED", node_id=pos['wl_id'], incident=True, pos=pos)
    assert _sent(n), "stopping an account must not silence an already-real position's incidents"


def test_genuinely_synthetic_position_stays_suppressed(env):
    """The other half of the contract: a position that was NEVER real (a
    canary/dry_run node's synthesized fill) must still be silenced, even though
    it reaches the exact same call sites."""
    with signals_db._conn() as c:
        c.execute("UPDATE open_positions SET is_dry_run_sim=1 WHERE ticker=?", (TICKER,))
        c.commit()
    pos = _pos()
    assert pos['is_dry_run_sim'] == 1

    n = _log_count()
    _real_post_message("UNPROTECTED", node_id=pos['wl_id'], incident=True, pos=pos)
    assert not _sent(n)

    # ...and stays suppressed even while its node is still nominally live and
    # its account enabled -- i.e. the decision really is the position's flag,
    # not the node's current state.
    assert _node()['state'] == 'live'


def test_missing_flag_fails_open(env):
    """A hand-built/legacy dict with no is_dry_run_sim key must SEND, matching
    the gate's documented fail-open contract."""
    pos = dict(_pos())
    pos.pop('is_dry_run_sim')
    _demote_node('paper')
    n = _log_count()
    _real_post_message("UNPROTECTED", node_id=pos['wl_id'], incident=True, pos=pos)
    assert _sent(n)


# --- node-keyed sites are deliberately unchanged ----------------------------

def test_node_keyed_incident_without_a_position_keeps_the_old_gate(env):
    """A failed automated buy has no position to anchor to, so the current
    node/account is the only available truth -- effectively_dry_run must still
    apply there."""
    node = _node()
    n = _log_count()
    _real_post_message("buy failed", node_id=node['id'], incident=True)
    assert _sent(n)  # live node, enabled account

    _demote_node('paper')
    n = _log_count()
    _real_post_message("buy failed", node_id=node['id'], incident=True)
    assert not _sent(n)


def test_routine_alerts_are_untouched_by_pos(env):
    """pos only ever participates when incident=True -- a routine (non-incident)
    alert keeps should_alert_live/$capital-at-stake, regardless of pos."""
    pos = _pos()
    # Drop below CAPITAL_AT_STAKE_THRESHOLD so should_alert_live is False --
    # if `pos` were (wrongly) consulted on the non-incident path, this real
    # is_dry_run_sim=0 position would force it through anyway.
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET starting_notional=100 WHERE ticker=?", (TICKER,))
        c.commit()
    n = _log_count()
    _real_post_message("BUY SIGNAL", node_id=pos['wl_id'], incident=False, pos=pos)
    assert not _sent(n)


# --- residual (b): the stale-price alert's snooze lever ---------------------

def test_stale_price_alert_respects_a_snooze(env, monkeypatch):
    posted = []
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda *a, **kw: posted.append((a[0] if a else kw.get('text'), kw)))
    signals_notify._STALE_PRICE_ALERTED.clear()
    pos = _pos()

    until = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
    signals_db.snooze_coverage('stale_price_exit_check_skipped', until, 'known stale test feed',
                                ticker=TICKER)

    signals_notify.alert_stale_price_exit_suppressed(pos)
    assert posted == []
    # The RECORD survives the snooze -- only the Slack alert is suppressed.
    # Tagged 'skipped_snoozed' (listed in the Grid row's bad_results) so an
    # acknowledged condition stops inflating counts without erasing the fact
    # that a real position genuinely went unmonitored (rebuttal round: an
    # earlier version dropped the row entirely, contradicting the
    # unconditional-logging comment in the same function).
    events = signals_db.get_coverage_events(scenario_key='stale_price_exit_check_skipped')
    assert len(events) == 1
    assert events[0]['result'] == 'skipped_snoozed'


def test_stale_price_alert_fires_when_snooze_scope_does_not_match(env, monkeypatch):
    posted = []
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda *a, **kw: posted.append((a[0] if a else kw.get('text'), kw)))
    signals_notify._STALE_PRICE_ALERTED.clear()
    pos = _pos()

    until = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
    signals_db.snooze_coverage('stale_price_exit_check_skipped', until, 'different ticker',
                                ticker='SOME_OTHER_TICKER')

    signals_notify.alert_stale_price_exit_suppressed(pos)
    assert len(posted) == 1
    # Assert the KWARGS, not just that something posted -- without this the test
    # passes identically with `pos=pos` deleted from the call site (both
    # reviewers, rebuttal round).
    _text, kw = posted[0]
    assert kw['incident'] is True
    assert kw['pos'] is pos
    assert len(signals_db.get_coverage_events(scenario_key='stale_price_exit_check_skipped')) == 1


def test_stale_price_alert_fires_after_the_snooze_expires(env, monkeypatch):
    posted = []
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda *a, **kw: posted.append((a[0] if a else kw.get('text'), kw)))
    signals_notify._STALE_PRICE_ALERTED.clear()
    pos = _pos()

    past = (datetime.now() - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
    signals_db.snooze_coverage('stale_price_exit_check_skipped', past, 'already expired',
                                ticker=TICKER)

    signals_notify.alert_stale_price_exit_suppressed(pos)
    assert len(posted) == 1


# --- pinned-ORDER anchoring (no position yet) -------------------------------

def test_real_order_bypasses_the_gate_entirely(env, monkeypatch):
    """check_market_buy_rejected's alerts concern a CONFIRMED real broker
    order. `node=` alone can't carry that: effectively_dry_run also reads the
    account's CURRENT trading_enabled, which nothing freezes into node_json --
    so stopping the account would still have gone dark on a `node=`-only fix."""
    pinned = dict(_node())  # snapshot taken while the node was live
    _demote_node('paper')
    monkeypatch.setitem(schwab_safety.ACCOUNTS, 'roth', _limits(trading_enabled=False))

    # Both halves of the staleness bug are present at once.
    assert signals_blocks.effectively_dry_run(pinned.get('account'), pinned) is True

    n = _log_count()
    _real_post_message("market-buy REJECTED after partial fill", node_id=pinned['id'],
                        incident=True, node=pinned, real_order=True)
    assert _sent(n), "a proven-real broker order's incident must never be suppressed"


def test_pinned_node_beats_a_live_relookup(env):
    """`node=` must be used INSTEAD of re-resolving node_id from the DB."""
    pinned = dict(_node())
    _demote_node('paper')
    n = _log_count()
    _real_post_message("placement failed", node_id=pinned['id'], incident=True, node=pinned)
    assert _sent(n), "a pinned live snapshot must win over the demoted live row"

    # ...and the converse: a pinned DRY-RUN snapshot stays suppressed even
    # though the DB row it would have re-resolved is unchanged -- so this is
    # genuinely "use the pinned value", not "always send".
    stale_dry = dict(pinned, state='dry_run')
    n = _log_count()
    _real_post_message("placement failed", node_id=pinned['id'], incident=True, node=stale_dry)
    assert not _sent(n)


# --- lint-style backstop over the real call sites ---------------------------

def test_every_incident_call_site_is_anchored():
    """Backstop (both reviewers): the gate itself is well covered, but the real
    risk surface is the plumbing across ~20 call sites. Every `incident=True`
    _post_message call must anchor to something pinned -- a position (`pos=`), a
    pinned/in-hand node (`node=`), or proven order realness (`real_order=`) --
    never a bare `node_id=`, which makes the gate re-resolve CURRENT config.

    Deliberately NOT an allowlist of exempt sites: an allowlist is precisely the
    thing a future session extends to silence this check rather than to fix a
    real miss."""
    import ast as _ast
    src = (Path(__file__).parent.parent / 'signals_notify.py').read_text()
    unanchored = []
    for n in _ast.walk(_ast.parse(src)):
        if not (isinstance(n, _ast.Call) and getattr(n.func, 'id', None) == '_post_message'):
            continue
        kw = {k.arg for k in n.keywords}
        if 'incident' not in kw:
            continue
        if not kw & {'pos', 'node', 'real_order'}:
            unanchored.append(n.lineno)
    assert not unanchored, (
        f"signals_notify.py lines {unanchored}: incident=True with no pinned anchor "
        f"(pos=/node=/real_order=) -- the gate would re-resolve current config, "
        f"the exact staleness bug closed 2026-08-17")
