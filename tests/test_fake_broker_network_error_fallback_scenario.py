"""fake_broker regression for trading_incidents #10/#11 (2026-08-17 Schwab API
outage): a genuine transport-level exception raised mid-place_order (not a
broker-level REJECTED response -- that's force_reject_next_order's case)
exhausts schwab_client._submit_order_with_retry's retry budget,
_attempt_automated_buy falls back to manual (no order placed, no exception
propagates out of notify_buy_signal), and -- the actual gap the incidents
exposed -- no later poll cycle re-attempts the automated placement, because
_scan_buy_signals unconditionally skips any node with an existing pending_buys
row (added by notify_buy_signal's fallback path itself, order_id=None).

Uses fake_broker.force_network_error_next_order() (added alongside this test,
2026-09-01) rather than force_reject_next_order -- the real incident's
exception ("The read operation timed out") was raised inside place_order
before any order id existed, never reached the broker's async status poll at
all, an entirely different failure shape from a confirmed REJECTED response.

_ORDER_SUBMIT_RETRY_ATTEMPTS is monkeypatched to 1 (not the real 3) purely for
test determinism: force_network_error_next_order is one-shot by design (mirrors
the real fixture's other force_* methods), so reproducing a full 3-attempt
exhaustion would need three separate seeded errors with retry-loop internals
threaded through the test. A reduced-to-1 budget still exhausts on the single
seeded error and exercises the exact same fallback code path
(_attempt_automated_buy's `except Exception` branch) that a real 3rd-attempt
exhaustion does -- the retry COUNT isn't what this test is regression-covering,
the FALLBACK BEHAVIOR after exhaustion is."""
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
import schwab_client
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_NETWORK_ERROR_FALLBACK'
IN_WINDOW_TIME = datetime(2026, 7, 29, 10, 30)


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
    monkeypatch.setattr(schwab_safety, '_now', lambda: IN_WINDOW_TIME)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)
    monkeypatch.setattr(schwab_safety, '_open_orders', lambda account: [])
    monkeypatch.setattr(signals_notify, '_post_message', lambda *a, **kw: (None, None))
    monkeypatch.setattr(signals_notify, 'time', type('T', (), {'sleep': staticmethod(lambda *a: None)}))
    # The real daemon runs with Slack Bolt (INTERACTIVE=True) -- a non-interactive
    # (SIM_MODE/no-Bolt) run instead hits notify_buy_signal's terminal input() manual-confirm
    # prompt, a different code path (REPL-driven, not what a live poll-loop fallback uses).
    monkeypatch.setattr(signals_notify.cfg, 'INTERACTIVE', True)
    monkeypatch.setattr(signals_notify, '_chart_buy', lambda *a, **kw: None)
    # Determinism only -- see module docstring.
    monkeypatch.setattr(schwab_client, '_ORDER_SUBMIT_RETRY_ATTEMPTS', 1)
    monkeypatch.setattr(schwab_client, '_ORDER_SUBMIT_RETRY_INTERVAL_SECS', 0)

    signals_db.ensure_tables()
    signals_db.add_node(TICKER, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0)
    with signals_db._conn() as c:
        c.execute("UPDATE watch_list SET account='soxl_ira', starting_notional=800 WHERE ticker=?",
                   (TICKER,))
        c.commit()

    yield

    Path(tmp_db.name).unlink(missing_ok=True)


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def _sig(price):
    return {
        'ticker': TICKER, 'current_price': price, 'z_score': -2.4,
        'last_bar': IN_WINDOW_TIME, 'lower_band': price - 1.0,
        'sma': price + 2.0, 'std': 1.0, 'hurst': None, 'adf_p': None, 'window': 10,
    }


def test_network_error_mid_placement_falls_back_to_manual_no_order_no_crash(env, fake_broker):
    """A transport-level exception raised inside place_order (not a broker
    REJECTED response) must not propagate out of notify_buy_signal, must not
    result in any real order at the broker, and must fall back to the same
    manual pending_buys tracking a plain missed-automation case gets
    (order_id=None, order_placed=0) -- matching incident #10's real observed
    behavior ("automated order placement failed unexpectedly... falling back
    to manual")."""
    fake_broker.set_quote(TICKER, last=10.15, bid=10.14, ask=10.16)
    fake_broker.force_network_error_next_order()
    node = _node()
    sig = _sig(10.15)

    signals_notify.notify_buy_signal(node, sig)

    assert fake_broker.orders == {}, "a network error mid-placement must not leave a real order at the broker"

    pending = [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER]
    assert len(pending) == 1
    assert pending[0]['order_placed'] == 0
    assert pending[0]['order_id'] is None

    failed_events = [e for e in signals_db.get_coverage_events(scenario_key='automated_buy_execution')
                      if e['ticker'] == TICKER and e['result'] == 'failed_unexpectedly']
    assert len(failed_events) == 1
    assert 'timed out' in failed_events[0]['detail'].lower()


def test_no_auto_retry_fires_on_next_poll_cycle_after_network_error(env, fake_broker):
    """The real incident's actual gap: once the fallback above has created a
    pending_buys row, a LATER poll cycle's _scan_buy_signals must not
    re-attempt automated placement for this node -- it structurally can't,
    since pending_wl_ids (built fresh from get_pending_buys() every call)
    already contains this node's id and _scan_buy_signals' already_pending
    branch takes over instead of ever reaching notify_buy_signal again. This
    is exactly what incident #10 observed: "No further automatic retry
    occurred on later poll cycles" -- confirmed here as real code behavior,
    not just narrated in the incident log."""
    import active_signals

    fake_broker.set_quote(TICKER, last=10.15, bid=10.14, ask=10.16)
    fake_broker.force_network_error_next_order()
    node = _node()
    sig = _sig(10.15)
    signals_notify.notify_buy_signal(node, sig)
    assert fake_broker.orders == {}

    # A subsequent poll cycle would call compute_buy_signal (real yfinance/DB
    # lookup) -- not under test here, only whether _scan_buy_signals reaches
    # notify_buy_signal again for THIS node given a fresh BUY signal object.
    # already_pending short-circuits before that call, so no monkeypatch of
    # notify_buy_signal is even needed to observe "it wasn't re-attempted" --
    # fake_broker.orders staying empty after a second scan proves it directly.
    buy_alerted = set()
    open_position_keys = {'live': set(), 'paper': set()}
    active_signals._scan_buy_signals([node], buy_alerted, open_position_keys)

    assert fake_broker.orders == {}, "no auto-retry should fire on a later poll cycle for a node with a pending_buys row"
    pending = [p for p in signals_db.get_pending_buys() if p['ticker'] == TICKER]
    assert len(pending) == 1, "the original fallback pending_buys row must still be the only one -- no duplicate re-alert"
