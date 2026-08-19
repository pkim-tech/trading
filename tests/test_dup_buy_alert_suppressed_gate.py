"""Confirmed-live noise bug, 2026-08-18: the `dup_buy_alert_suppressed` Slack
message in active_signals.py::_scan_buy_signals (already_pending branch) was
missing `node_id=` on its `_post_message` call, so it completely bypassed
signals_blocks._post_message's should_alert_live/has_capital_at_stake noise
gate -- unlike every other alert of this shape, gated in the 2026-08-17 sweep
(see tests/test_post_message_gate_call_sites.py, scoped to signals_notify.py
only -- this call site lives in active_signals.py, which that sweep never
scanned). Confirmed real: FAS (soxl_ira, dry_run, $500) and DIA (ira,
dry_run, $10k) both pushed this message to live Slack on 2026-08-18.

Mirrors tests/test_post_message_alert_gate.py's technique: capture the REAL
signals_blocks._post_message at import time (before conftest.py's autouse
noop-patch fixture replaces it, including on active_signals' own imported
name -- see conftest.py's docstring on why every module holding its own
`from signals_blocks import _post_message` reference needs patching
individually), then re-point active_signals._post_message at it for this
test only, so _scan_buy_signals' real call actually exercises the gate
instead of a mock recording args.
"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))

import active_signals
import signals_blocks
import signals_config
import signals_db as db

_REAL_POST_MESSAGE = signals_blocks._post_message

TICKER = 'TEST_DUPBUY_GATE'


@pytest.fixture
def env(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'SOCKET_MODE', False)
    monkeypatch.setattr(signals_config, 'SLACK_HOOK', '')
    monkeypatch.setattr(signals_config, 'SIM_MODE', True)
    db.ensure_tables()
    # Undo the autouse noop-patch just for active_signals' own reference --
    # this is the exact call site under test, and the real gate logic must run.
    monkeypatch.setattr(active_signals, '_post_message', _REAL_POST_MESSAGE)
    yield
    Path(tmp_db.name).unlink(missing_ok=True)


def _add_node_and_pend(ticker, state, starting_notional, account='soxl_ira'):
    db.add_node(ticker, 'TrailingBothZScoreBreakout', 'test', window=20, take_profit=30.0,
                stop_loss=1.0, max_hold_hours=56, state=state,
                starting_notional=starting_notional, account=account, fixed_sl_override=1.0)
    node = db.get_watch_list_node(ticker, account=account, watchlist_id=False)
    sig = {
        'ticker': ticker, 'window': node['window'], 'signal': 'BUY',
        'z_score': -2.0, 'last_bar': datetime(2026, 8, 18, 14, 30),
        'current_price': 10.0,
    }
    # Puts node['id'] in pending_wl_ids -> _scan_buy_signals' already_pending
    # branch -> the dup_buy_alert_suppressed message under test.
    db.add_pending_buy(node, sig, channel=None, ts=None)
    return node, sig


def test_high_notional_live_node_dup_buy_alert_still_reaches_slack(env, monkeypatch):
    """Non-regression check: a real capital-at-stake node's dup-suppression
    notice must still reach Slack after the node_id= fix -- the gate should
    only silence sub-threshold/dry-run noise, not real-money nodes. On its
    own this doesn't prove node_id= is actually wired through (an
    unresolvable/missing node_id also fails OPEN and always sends) -- the
    dry-run test below is the one that proves the gate is really engaged."""
    node, sig = _add_node_and_pend(TICKER + '_HI', state='live', starting_notional=999_999)
    monkeypatch.setattr(active_signals, 'compute_buy_signal', lambda n, price_override=None: sig)

    active_signals._scan_buy_signals([node], set(), {'live': set(), 'paper': set()})

    msgs = db.get_slack_messages(limit=5)
    dup_msgs = [m for m in msgs if 'BUY signal suppressed' in (m.get('text') or '')]
    assert dup_msgs, "expected a dup_buy_alert_suppressed message to be logged"
    assert dup_msgs[0]['mode'] != 'suppressed'


def test_dry_run_node_dup_buy_alert_does_not_reach_slack(env, monkeypatch):
    """The real live incident: a dry_run/canary node's routine dup-suppression
    noise (FAS/soxl_ira, DIA/ira, both 2026-08-18) must stay off real Slack
    regardless of its starting_notional."""
    node, sig = _add_node_and_pend(TICKER + '_DRYRUN', state='dry_run', starting_notional=10_000)
    monkeypatch.setattr(active_signals, 'compute_buy_signal', lambda n, price_override=None: sig)

    active_signals._scan_buy_signals([node], set(), {'live': set(), 'paper': set()})

    msgs = db.get_slack_messages(limit=5)
    dup_msgs = [m for m in msgs if 'BUY signal suppressed' in (m.get('text') or '')]
    assert dup_msgs, "expected the message to still be logged (suppressed != silently dropped)"
    assert dup_msgs[0]['mode'] == 'suppressed', \
        "dry_run node's dup-buy-suppressed alert must not reach real Slack"


def test_no_ungated_dup_buy_alert_left_in_scan_buy_signals():
    """Structural backstop: reuses scripts/audit_post_message_gating.py so a
    future edit to this call site (or a new ungated one added to the same
    function) fails here instead of silently reopening this incident."""
    import audit_post_message_gating as audit

    rows = audit.audit(str(Path(__file__).parent.parent / 'active_signals.py'))
    offenders = [f"{r['func']}:{r['line']}" for r in rows
                 if r['func'] == '_scan_buy_signals' and not r['gated'] and not r['deliberate']]
    assert not offenders, f"ungated per-position _post_message call site(s): {offenders}"
