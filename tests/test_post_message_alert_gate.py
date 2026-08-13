"""Pins signals_blocks._post_message's node_id noise-reduction gate
(2026-08-13) -- found missing by a 4-way paired review (cold Opus,
contextual Opus, Sonnet, Fable) that every existing test mocks
_post_message out entirely, so none of them ever exercise the real
suppress/fail-open logic. This file calls the REAL, un-mocked
_post_message directly (captured at module-import time, before
conftest.py's autouse _no_real_slack_posts fixture patches
signals_blocks._post_message to a noop for every other test) -- and
forces cfg.SOCKET_MODE/SLACK_HOOK off so the real send path can run
with zero real network calls (lands in the 'sim' log_mode branch, since
the env fixture also sets SIM_MODE=True -- SOCKET_MODE/SLACK_HOOK being
off is what actually guarantees no network call either way), instead
of also mocking _post_message itself, which would defeat the point.

Also pins the 2026-08-13 has_capital_at_stake fix (compares against the
REAL effective notional via _last_sale_recovery -- last closed trade's
proceeds -- not just the static starting_notional column), found by
the same review: a compounding soxl_ira node could grow its real
per-order size past the threshold while its static column stays small,
silently suppressing real-money order alerts."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_blocks
import signals_config
import signals_db
import schwab_safety

# Captured here, at collection time, before any fixture (including the
# autouse Slack-mock) has run -- a separate name bound to the real function
# object, unaffected by later monkeypatch.setattr(signals_blocks, ...).
_REAL_POST_MESSAGE = signals_blocks._post_message

TICKER = 'TEST_ALERT_GATE'


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    # Zero real network calls regardless of SIM_MODE -- SIM_MODE only
    # prefixes text, it does NOT stop a real chat_postMessage/webhook call
    # (see conftest.py's docstring). Forcing both off is what actually
    # guarantees no network call for a non-suppressed send (log_mode itself
    # reads 'sim', not 'console', since SIM_MODE=True below -- see _last_mode).
    monkeypatch.setattr(signals_config, 'SOCKET_MODE', False)
    monkeypatch.setattr(signals_config, 'SLACK_HOOK', '')
    monkeypatch.setattr(signals_config, 'SIM_MODE', True)
    signals_db.ensure_tables()
    with signals_db._conn() as c:
        c.execute("UPDATE accounts SET trading_enabled=1 WHERE alias='roth'")
        c.commit()
    yield
    Path(tmp_db.name).unlink(missing_ok=True)


def _add_node(ticker, starting_notional, state='live', account='roth'):
    signals_db.add_node(ticker, 'ZScoreBreakout', 'test', window=20, take_profit=10,
                         stop_loss=5, max_hold_hours=56, state=state,
                         starting_notional=starting_notional, account=account)
    with signals_db._conn() as c:
        row = c.execute("SELECT id FROM watch_list WHERE ticker=?", (ticker,)).fetchone()
        c.commit()
    return row[0]


def _last_mode():
    msgs = signals_db.get_slack_messages(limit=1)
    return msgs[0]['mode'] if msgs else None


def test_above_threshold_live_node_sends(env):
    node_id = _add_node(TICKER + '_HI', starting_notional=10_000)
    channel, ts = _REAL_POST_MESSAGE("test above threshold", node_id=node_id)
    assert (channel, ts) == (None, None)  # non-SOCKET_MODE send path always returns this
    assert _last_mode() != 'suppressed'  # NOT 'suppressed' -- it actually sent


def test_below_threshold_live_node_suppressed(env):
    node_id = _add_node(TICKER + '_LO', starting_notional=100)
    channel, ts = _REAL_POST_MESSAGE("test below threshold", node_id=node_id)
    assert (channel, ts) == (None, None)
    assert _last_mode() == 'suppressed'


def test_dry_run_node_suppressed_regardless_of_notional(env):
    # state='dry_run' -> effectively_dry_run=True -> has_capital_at_stake
    # short-circuits False before the dollar comparison even runs, however
    # large starting_notional is.
    node_id = _add_node(TICKER + '_DRYRUN', starting_notional=999_999, state='dry_run')
    _REAL_POST_MESSAGE("test dry_run", node_id=node_id)
    assert _last_mode() == 'suppressed'


def test_unresolvable_node_id_fails_open_and_sends(env):
    # No row for this id -- get_watch_list_node_by_id returns None -- the
    # gate's "node is not None and not should_alert_live(node)" is False,
    # so this must fall through to a real send, not silently vanish.
    _REAL_POST_MESSAGE("test missing node", node_id=999_999_999)
    assert _last_mode() != 'suppressed'


def test_no_node_id_always_sends(env):
    # System-wide messages (EOD/coverage reports, generic errors) were never
    # in scope for this gate -- omitting node_id must behave exactly as
    # before this feature existed.
    _REAL_POST_MESSAGE("test no node_id at all")
    assert _last_mode() != 'suppressed'


def test_effective_notional_below_static_still_alerts(env):
    """Pins the 2026-08-13 has_capital_at_stake fix: a node whose static
    starting_notional is BELOW threshold but whose last closed trade's real
    proceeds are ABOVE it must still alert -- the static column alone would
    have wrongly suppressed a real-money node here."""
    node_id = _add_node(TICKER + '_COMPOUNDED', starting_notional=100)
    node = signals_db.get_watch_list_node_by_id(node_id)
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with signals_db._conn() as c:
        c.execute(
            "INSERT INTO trade_log (ticker, strategy, version, window, account, "
            "take_profit, stop_loss, max_hold_hours, "
            "signal_price, signal_time, entry_price, entry_time, entry_drift_pct, "
            "exit_price, shares, exit_time, is_dry_run_sim, position_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'core')",
            (node['ticker'], node['strategy'], node['version'], node['window'], node['account'],
             node['take_profit'], node['stop_loss'], node['max_hold_hours'],
             100.0, now_str, 100.0, now_str, 0.0,
             100.0, 100,  # 100 * 100 = $10,000 real effective notional, well above static $100
             now_str),
        )
        c.commit()
    _REAL_POST_MESSAGE("test compounded effective notional", node_id=node_id)
    assert _last_mode() != 'suppressed'  # sent, not suppressed -- the old static-only check would fail this
