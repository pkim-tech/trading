"""Fake-venue proof for the 2026-08-15 retry/duplicate-order fix
(schwab_client._submit_order_with_retry / _submit_replace_with_retry): under
a REPEATED flapping connection (up-down-up mid-order-placement, not just the
single clean drop _submit_replace_with_retry's older docstring already
accepted as residual risk), the retry loop used to resubmit blind on every
local exception with zero re-check of whether a prior attempt's request had
actually landed at the broker -- N consecutive local "failures" could mean N
real orders resting, none confirmed locally.

Drives the real schwab_client.place_equity_buy -> _submit_order_with_retry
chain against tests/fake_broker.py, with place_order itself patched to
simulate the broker genuinely receiving and processing an order while the
local call still raises (the "response leg eaten" case a plain retry can't
tell apart from "nothing happened").

Two scenarios:
  1. Repeated flapping where the broker actually received the 2nd attempt --
     the retry loop must detect the already-placed order before firing a
     3rd, real, duplicate order.
  2. Ambiguous broker state (more than one order could be the missing
     attempt) -- must fail safe (raise, no further resubmission) rather than
     guess which one is real."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import signals_config
import signals_db
import schwab_client
import schwab_safety

from fake_broker import fake_broker  # noqa: F401

TICKER = 'TEST_RETRY_FLAP_SCENARIO'
_IN_WINDOW_TIME = datetime(2026, 7, 29, 10, 30)


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
    monkeypatch.setattr(schwab_safety, 'NODE_BREAKER_PATH', tmp_path / "schwab_node_breaker_state.json")
    monkeypatch.setattr(schwab_safety, 'AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'NODE_AUTO_FILL_DETECTION_PATH', tmp_path / "schwab_node_auto_fill_detection.json")
    monkeypatch.setattr(schwab_safety, 'AUTOMATION_ENABLED_TICKERS', {TICKER})
    monkeypatch.setattr(schwab_safety, '_now', lambda: _IN_WINDOW_TIME)
    monkeypatch.setattr(schwab_client, '_ORDER_SUBMIT_RETRY_INTERVAL_SECS', 0)
    monkeypatch.setattr(schwab_client, '_ORDER_CONFIRM_POLL_INTERVAL_SECS', 0)
    monkeypatch.delenv('SCHWAB_KILL_SWITCH', raising=False)

    signals_db.ensure_tables()
    yield
    schwab_safety.disengage_kill_switch()
    Path(tmp_db.name).unlink(missing_ok=True)


def _add_node(ticker, account, notional=50_000):
    signals_db.add_node(ticker, 'TrailingBothZScoreBreakout', 'test', window=10, take_profit=16.0,
                         stop_loss=1, max_hold_hours=105, state='live',
                         trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                         account=account, starting_notional=notional)
    return [n for n in signals_db.get_watchlist()
            if n['ticker'] == ticker and n['account'] == account][0]


def _real_placed_orders(fake_broker_, ticker):
    return [o for o in fake_broker_.orders.values()
            if o['orderLegCollection'][0]['instrument']['symbol'] == ticker]


def _node():
    return [n for n in signals_db.get_watchlist() if n['ticker'] == TICKER][0]


def test_repeated_flap_where_broker_received_the_order_prevents_a_duplicate_resubmit(env, fake_broker, monkeypatch):
    """Attempt 1: request never reaches the broker at all (a clean drop --
    nothing to find). Attempt 2: the broker genuinely receives and creates
    the order, but the local call still raises (response leg eaten by the
    next flap). Without the fix, attempt 3 would blindly resubmit and a
    SECOND real order would land. With the fix, the pre-attempt-3 broker
    check finds attempt 2's real order and returns it instead."""
    _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    real_place_order = fake_broker.place_order
    call_log = []

    def flaky_place_order(account_hash, order):
        call_log.append(1)
        n = len(call_log)
        if n == 1:
            raise ConnectionError("simulated flap: request never reached the broker")
        if n == 2:
            real_place_order(account_hash, order)  # broker genuinely creates the order...
            raise ConnectionError("simulated flap: broker received it, response lost")
        # A 3rd real placement call means the fix failed to detect attempt 2's order.
        return real_place_order(account_hash, order)

    monkeypatch.setattr(fake_broker, 'place_order', flaky_place_order)

    r, order_id = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)

    assert order_id is not None
    assert len(call_log) == 2, (
        f"expected exactly 2 real place_order calls (drop, then landed-but-lost-response) -- "
        f"a 3rd call means the retry resubmitted blind instead of detecting attempt 2's order, got {len(call_log)}"
    )

    placed = _real_placed_orders(fake_broker, TICKER)
    assert len(placed) == 1, f"expected exactly 1 real order at the broker, got {len(placed)}: {placed}"
    assert placed[0]['orderId'] == order_id

    events = signals_db.get_coverage_events(scenario_key='order_retry_duplicate_prevented')
    matches = [e for e in events if e['ticker'] == TICKER]
    assert any(e['result'] == 'prevented' for e in matches), (
        f"expected a 'prevented' coverage_events row logging the avoided duplicate, got {matches}"
    )


def test_ambiguous_broker_state_halts_retry_instead_of_guessing(env, fake_broker, monkeypatch):
    """The fix's own baseline-order-id snapshot (taken once, before attempt 1)
    means the retry loop can never manufacture ambiguity purely by its own
    actions -- the very next check after any landed-but-lost attempt finds
    that ONE order and returns it before a second real placement ever fires
    (see the test above). Genuine ambiguity can only come from a real
    concurrent, independent order matching the exact same shape -- e.g. a
    fast manual resubmission racing the automated retry -- landing in the
    same window as our own lost-response attempt. This test simulates
    exactly that: attempt 1 creates our own real order AND a concurrent
    independent one lands at the same moment, then raises (response lost).
    The pre-attempt-2 check must find both and refuse to guess, rather than
    silently pick one (or, worse, ignore both and fire a 3rd real order)."""
    _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    real_place_order = fake_broker.place_order
    call_log = []

    def flaky_place_order_with_concurrent_order(account_hash, order):
        call_log.append(1)
        # Our own attempt's real order...
        real_place_order(account_hash, order)
        # ...plus a genuinely separate, concurrent real order landing in the
        # exact same window (e.g. a human's fast manual resubmit) -- not
        # something our own retry loop created, so the baseline-id snapshot
        # (taken before attempt 1) can't have excluded it either.
        fake_broker.seed_resting_order('soxl_ira', TICKER, 'MARKET', 'BUY', 10, status='FILLED')
        raise ConnectionError("simulated flap: broker received it, response lost")

    monkeypatch.setattr(fake_broker, 'place_order', flaky_place_order_with_concurrent_order)

    with pytest.raises(schwab_client._AmbiguousBrokerState):
        schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)

    # Only 1 real placement call should have happened -- the ambiguity check
    # fires before attempt 2 and halts, no further resubmission.
    assert len(call_log) == 1, f"expected the retry loop to stop at the ambiguity check, got {len(call_log)} calls"

    placed = _real_placed_orders(fake_broker, TICKER)
    assert len(placed) == 2, f"expected exactly the 2 real orders from attempt 1's window, got {len(placed)}: {placed}"

    events = signals_db.get_coverage_events(scenario_key='order_retry_duplicate_prevented')
    matches = [e for e in events if e['ticker'] == TICKER]
    assert any(e['result'] == 'ambiguous' for e in matches), (
        f"expected an 'ambiguous' coverage_events row, got {matches}"
    )


def test_final_attempt_landing_is_caught_by_the_post_loop_check(env, fake_broker, monkeypatch):
    """Every attempt before the last one raises with nothing reaching the
    broker; the LAST attempt (_ORDER_SUBMIT_RETRY_ATTEMPTS'th) genuinely
    lands but the response is still lost. There is no further retry after
    the last attempt to gate a pre-check on -- proves the post-loop final
    check (run once after the loop exhausts, before raising last_exc) is
    what catches this, not the ordinary "before attempt N>0" gate alone."""
    _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    real_place_order = fake_broker.place_order
    call_log = []

    def flaky_place_order(account_hash, order):
        call_log.append(1)
        if len(call_log) < schwab_client._ORDER_SUBMIT_RETRY_ATTEMPTS:
            raise ConnectionError("simulated flap: request never reached the broker")
        real_place_order(account_hash, order)  # the final attempt genuinely lands...
        raise ConnectionError("simulated flap: broker received it, response lost")

    monkeypatch.setattr(fake_broker, 'place_order', flaky_place_order)

    r, order_id = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)

    assert order_id is not None, (
        "the post-loop final check must catch the last attempt landing even though "
        "every real placement call raised locally"
    )
    assert len(call_log) == schwab_client._ORDER_SUBMIT_RETRY_ATTEMPTS
    placed = _real_placed_orders(fake_broker, TICKER)
    assert len(placed) == 1, f"expected exactly 1 real order, got {len(placed)}: {placed}"
    assert placed[0]['orderId'] == order_id


def test_stale_preexisting_order_does_not_block_a_genuinely_needed_retry(env, fake_broker, monkeypatch):
    """A stale order sharing the exact same ticker/side/quantity/orderType,
    already resting/filled BEFORE this call's own retry loop even starts,
    must never be mistaken for a landed retry attempt -- proves the
    baseline-order-id snapshot (captured once, before attempt 1) correctly
    excludes it, so a genuinely-needed retry still fires and succeeds with
    its own new, distinct order."""
    _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    # Stale order from earlier -- same exact shape as what's about to be placed.
    fake_broker.seed_resting_order('soxl_ira', TICKER, 'MARKET', 'BUY', 10, status='FILLED')

    real_place_order = fake_broker.place_order
    call_log = []

    def flaky_place_order(account_hash, order):
        call_log.append(1)
        if len(call_log) == 1:
            raise ConnectionError("simulated flap: request never reached the broker")
        return real_place_order(account_hash, order)

    monkeypatch.setattr(fake_broker, 'place_order', flaky_place_order)

    r, order_id = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)

    assert order_id is not None
    assert len(call_log) == 2, "the stale pre-existing order must not short-circuit a genuinely needed retry"
    placed = _real_placed_orders(fake_broker, TICKER)
    assert len(placed) == 2, (
        f"expected the pre-seeded stale order plus this call's own real new order, got {len(placed)}: {placed}"
    )
    assert order_id in [o['orderId'] for o in placed]


def test_rejected_prior_order_is_not_mistaken_for_a_successful_duplicate(env, fake_broker, monkeypatch):
    """The exact incident shape this whole fix exists to prevent (the real
    LABD live incident: Schwab REJECTED a real stop after a late fill) --
    found in independent review as an untested gap. Attempt 1's order
    genuinely lands at the broker but is REJECTED, not filled/resting, then
    the local call raises (response lost). The pre-attempt-2 check must NOT
    treat a REJECTED order as evidence of a successful prior placement -- it
    must let a fresh retry fire and place a genuinely new, successful order,
    exactly like schwab_safety._DUPLICATE_NOT_CONFIRMED_STATUSES already
    does for the ordinary (non-retry) duplicate-order guard."""
    _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    real_place_order = fake_broker.place_order
    call_log = []

    def flaky_then_rejected(account_hash, order):
        call_log.append(1)
        if len(call_log) == 1:
            resp = real_place_order(account_hash, order)
            # Broker actually rejected this order (not filled) -- mutate the
            # real ledger directly, same technique
            # test_fake_broker_order_rejected_scenario.py uses to simulate a
            # genuine REJECTED status.
            fake_broker.orders[resp.order_id]['status'] = 'REJECTED'
            raise ConnectionError("simulated flap: broker received it, response lost")
        return real_place_order(account_hash, order)

    monkeypatch.setattr(fake_broker, 'place_order', flaky_then_rejected)

    r, order_id = schwab_client.place_equity_buy('soxl_ira', TICKER, 10, 10.0)

    assert order_id is not None
    assert len(call_log) == 2, (
        "a REJECTED prior order must not be mistaken for success -- the retry loop "
        "must still fire a fresh 2nd real placement"
    )
    placed = _real_placed_orders(fake_broker, TICKER)
    statuses = sorted(o['status'] for o in placed)
    assert statuses == ['FILLED', 'REJECTED'], f"expected 1 rejected + 1 successful order, got {statuses}"
    rejected_id = [o['orderId'] for o in placed if o['status'] == 'REJECTED'][0]
    assert order_id != rejected_id, "the returned order_id must be the successful order, never the rejected one"

    events = signals_db.get_coverage_events(scenario_key='order_retry_duplicate_prevented')
    matches = [e for e in events if e['ticker'] == TICKER]
    assert not any(e['result'] == 'prevented' for e in matches), (
        f"a REJECTED order must never be logged as a 'prevented' duplicate, got {matches}"
    )


def test_replace_path_also_prevents_a_duplicate_on_a_landed_but_lost_response(env, fake_broker, monkeypatch):
    """Same flapping-duplicate-prevention proof as the first test above, but
    through _submit_replace_with_retry (replace_order_with_stop_loss) --
    found in independent review as an untested path despite the docstring
    there claiming the gap is closed for every attempt, not just fresh
    placements."""
    node = _add_node(TICKER, 'soxl_ira')
    fake_broker.set_quote(TICKER, last=10.0, bid=10.0, ask=10.01)
    fake_broker.set_cash_balance('soxl_ira', 1_000_000.0)

    # A real open position -- place_stop_loss (SELL, is_protective) requires
    # one on file (schwab_safety's oversell guard).
    signals_db.open_position(node, 10.0, _IN_WINDOW_TIME, 10.0, _IN_WINDOW_TIME, shares=10)

    # A real resting STOP to replace -- placed normally, no flakiness yet.
    r, old_order_id = schwab_client.place_stop_loss('soxl_ira', TICKER, 10, 9.0)
    assert old_order_id is not None

    # The replace below is also a real SELL of the same ticker/account/
    # quantity within schwab_safety's DUPLICATE_ORDER_WINDOW_SECS -- a
    # separate, upstream guard from the fix under test here. Real
    # replace_order_with_stop_loss callers pass replacing_order_id (exempts
    # the resting-order guards), but this specific window-based local-record
    # check isn't scoped by replacing_order_id at all; disabling it here
    # keeps this test focused on the retry/dedup logic being proven, not on
    # reproducing that separate guard's own real-world timing.
    monkeypatch.setattr(schwab_safety, 'DUPLICATE_ORDER_WINDOW_SECS', 0)

    real_replace_order = fake_broker.replace_order
    call_log = []

    def flaky_replace_order(account_hash, order_id_arg, order):
        call_log.append(1)
        if len(call_log) == 1:
            real_replace_order(account_hash, order_id_arg, order)  # broker genuinely replaces...
            raise ConnectionError("simulated flap: broker received it, response lost")
        return real_replace_order(account_hash, order_id_arg, order)

    monkeypatch.setattr(fake_broker, 'replace_order', flaky_replace_order)

    r, new_order_id = schwab_client.replace_order_with_stop_loss('soxl_ira', TICKER, old_order_id, 10, 8.5)

    assert new_order_id is not None
    assert len(call_log) == 1, (
        f"expected exactly 1 real replace_order call -- the pre-attempt-2 check should have found "
        f"the landed replacement and skipped a 2nd, got {len(call_log)}"
    )

    resting = [o for o in fake_broker.orders.values()
               if o['orderLegCollection'][0]['instrument']['symbol'] == TICKER
               and o['status'] not in ('REPLACED', 'CANCELED', 'REJECTED')]
    assert len(resting) == 1, f"expected exactly 1 live STOP order after the replace, got {len(resting)}: {resting}"
    assert resting[0]['orderId'] == new_order_id

    events = signals_db.get_coverage_events(scenario_key='order_retry_duplicate_prevented')
    matches = [e for e in events if e['ticker'] == TICKER]
    assert any(e['result'] == 'prevented' for e in matches), (
        f"expected a 'prevented' coverage_events row from the replace path, got {matches}"
    )
