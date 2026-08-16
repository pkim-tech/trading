"""Phase 2 scenario: `node_circuit_breaker` -- schwab_safety.record_node_streak
(schwab_safety.py:502), the node-level, monitor-only circuit breaker, driven
through this harness's real wire-format/subprocess-isolated boundary rather
than a mocked function call or a pytest-tmp_path-monkeypatched state file.

Lower marginal value per the backlog: tests/test_node_circuit_breaker.py
already proves the CORE fix (the reset-before-genuine-submission-attempt bug,
2026-07-29 Sonnet review) against tests/fake_broker.py in-process, including
both the trip-accumulates and the trip-resets-on-success paths. This scenario
does NOT re-derive that -- it adds the one axis that tier can't: proof that
the mechanism holds up driven through place_equity_buy in the harness's own
OS process, against its own isolated NODE_BREAKER_PATH state file
(schwab_safety.NODE_BREAKER_PATH, resolved via SCHWAB_STATE_DIR -- see
fake_venue/isolation.py's assert_isolation_took_effect, which already proves
this path lives in the harness's state dir, not production's), read back with
a raw json.loads() over the real file on disk -- not the in-process dict the
pytest tier's monkeypatch-redirected-but-still-python-native Path object
would otherwise let a bug in the read-back path hide behind.

Also covers one thing the pytest tier's two tests don't: that a 4th
consecutive failure past the trip does NOT re-fire the alert (node_state.get(
tripped_key) suppression, schwab_safety.py:551) -- untested there, and a real
risk shape on its own (a monitor that pages repeatedly for the same ongoing
condition trains the human to ignore it).

Real code under test: schwab_safety.record_node_streak, called from
schwab_client._place_equity_order's real BUY placement try/except
(schwab_client.py:691/727/729) -- the exact chokepoint
test_real_broker_submission_failure_accumulates_the_order_failures_streak
exercises, here reached via schwab_client.place_equity_buy instead of a
direct _place_equity_order call, matching how every real production call
site actually reaches it.

One fake node/account (this scenario isn't about ticker/account
disambiguation -- record_node_streak's node_id resolution is already
node_id-precise via the caller-supplied node_id, not the ambiguous
ticker+account fallback Phase 1 covers).

  Legs 1-3  three separate place_equity_buy calls, broker.place_order patched
            to always raise (mirrors the pytest tier's `_always_fail`) --
            each call's own internal retry loop (schwab_client.
            _ORDER_SUBMIT_RETRY_ATTEMPTS attempts, interval zeroed for speed)
            exhausts and re-raises, so each of the 3 calls contributes
            exactly one hit=True to the streak.
            => after leg 3: coverage_events['node_circuit_breaker_tripped']
               = 'tripped', streak=3                            <-- checked
            => NODE_BREAKER_PATH's real on-disk JSON, read back fresh,
               shows order_failures_streak=3 / order_failures_tripped=True
               for this node's id                               <-- checked

  Leg 4     a 4th failure past the trip (same always-raising broker) must NOT
            re-fire the alert -- exactly one 'tripped' event total, streak
            keeps counting past the threshold in the state file (=4) but
            just_tripped stays False (schwab_safety.py:548-553's `not
            node_state.get(tripped_key)` guard).
            => coverage_events['node_circuit_breaker_tripped'] still has
               exactly 1 row                                    <-- checked

DUPLICATE_ORDER_WINDOW_SECS is zeroed for the duration -- 3-4 same-ticker/
side/quantity BUY attempts in quick succession would otherwise trip the
UNRELATED local duplicate-order fingerprint guard (schwab_safety.check_order)
before ever reaching the broker, which would exercise record_node_streak via
the SafetyViolation branch (schwab_client.py:691) instead of the genuine
broker-submission-failure branch (line 727) this scenario targets -- same
override every other Phase 2 scenario placing repeat same-shape orders uses.
"""
from dataclasses import dataclass
from datetime import datetime

from fake_venue import venue
from fake_venue.scenarios_meta import CASH_ALIAS, PRICE_SOURCE_TICKER, TICKER

FAKE_ACCOUNTS = [
    dict(alias=CASH_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='cash', margin_capable=0),
]
NODE_NOTIONAL = 2_000


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ''
    required: bool = True

    def __post_init__(self):
        self.ok = bool(self.ok)


def _add_node():
    import signals_db as db

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_breaker',
                window=20, take_profit=10, stop_loss=1, max_hold_hours=56,
                state='live', account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                label='fake-venue harness node (node_circuit_breaker)')
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]


def run(price=None, verbose=True):
    """Runs the scenario against the already-isolated, already-imported
    environment. Returns (checks, observations)."""
    import json

    import schwab_client
    import schwab_safety
    import signals_db as db

    def say(msg):
        if verbose:
            print(msg)

    checks, observations = [], {}

    db.ensure_tables()
    if [n for n in db.get_watchlist() if n['ticker'] == TICKER]:
        raise RuntimeError("this harness DB has already been used -- point --db-path at a fresh "
                           "file (the default temp dir is fresh every run)")
    venue.seed_fake_accounts(FAKE_ACCOUNTS)
    broker = venue.install_fake_broker([CASH_ALIAS])
    price = venue.seed_quote(broker, TICKER, price, price_source_ticker=PRICE_SOURCE_TICKER)
    broker.set_cash_balance(CASH_ALIAS, 100_000.0)
    say(f"[setup] {TICKER} quote seeded at ${price:.4f}")

    node = _add_node()
    say(f"[setup] node wl_id={node['id']} ({CASH_ALIAS}), target_notional=${NODE_NOTIONAL}")

    # Same environmental overrides every other Phase 2 scenario placing a real
    # BUY uses -- both orthogonal to the mechanism under test (see each
    # sibling scenario's docstring for the individual rationale).
    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (orthogonal to the mechanism under test)")
        schwab_safety._is_trading_day = lambda date_str: True

    orig_now = schwab_safety._now
    schwab_safety._now = lambda: datetime(2026, 7, 29, 10, 30)

    orig_retry_interval = schwab_client._ORDER_SUBMIT_RETRY_INTERVAL_SECS
    schwab_client._ORDER_SUBMIT_RETRY_INTERVAL_SECS = 0

    orig_dup_window = schwab_safety.DUPLICATE_ORDER_WINDOW_SECS
    schwab_safety.DUPLICATE_ORDER_WINDOW_SECS = 0

    shares = max(int(NODE_NOTIONAL // price), 1)
    real_place_order = broker.place_order

    def _always_fail(account_hash, order):
        raise ConnectionError("simulated real broker rejection (place_order itself fails)")

    try:
        # ==================================================== legs 1-3
        say("[legs 1-3] 3 consecutive place_equity_buy calls, each hitting a broker that always "
            "raises -- each call's own internal retry loop exhausts and re-raises once")
        broker.place_order = _always_fail
        raised_count = 0
        for i in range(3):
            try:
                schwab_client.place_equity_buy(CASH_ALIAS, TICKER, shares, price, node_id=node['id'])
            except Exception:
                raised_count += 1
        broker.place_order = real_place_order
        checks.append(Check("all 3 place_equity_buy attempts raised (broker never actually accepted)",
                            raised_count == 3, f"raised_count={raised_count}"))

        tripped = [e for e in db.get_coverage_events(scenario_key='node_circuit_breaker_tripped')
                  if e['ticker'] == TICKER and e['node_id'] == node['id']]
        checks.append(Check("node_circuit_breaker_tripped fired exactly once after the 3rd "
                            "consecutive real broker-submission failure",
                            len(tripped) == 1 and tripped[0]['result'] == 'tripped'
                            and 'order_failures' in tripped[0]['detail'] and 'streak=3' in tripped[0]['detail'],
                            f"events={[(e['result'], e['detail']) for e in tripped]}"))

        # Real on-disk file, re-read fresh -- not the in-process attribute the
        # calls above already mutated, so this actually proves the write/read
        # round-trip through this harness's own isolated state dir, not just
        # that the in-memory call succeeded.
        breaker_state = json.loads(schwab_safety.NODE_BREAKER_PATH.read_text())
        node_state = breaker_state.get(str(node['id']), {})
        checks.append(Check("NODE_BREAKER_PATH's real on-disk JSON (re-read fresh from the harness's "
                            "own isolated state dir) shows the streak/tripped flag for this node",
                            node_state.get('order_failures_streak') == 3
                            and node_state.get('order_failures_tripped') is True,
                            f"path={schwab_safety.NODE_BREAKER_PATH} node_state={node_state}"))

        # ==================================================== leg 4
        say("[leg 4] a 4th consecutive failure past the trip -- must NOT re-fire the alert")
        broker.place_order = _always_fail
        try:
            schwab_client.place_equity_buy(CASH_ALIAS, TICKER, shares, price, node_id=node['id'])
            leg4_raised = False
        except Exception:
            leg4_raised = True
        broker.place_order = real_place_order
        checks.append(Check("leg 4's attempt also raised", leg4_raised))

        tripped_after_leg4 = [e for e in db.get_coverage_events(scenario_key='node_circuit_breaker_tripped')
                              if e['ticker'] == TICKER and e['node_id'] == node['id']]
        checks.append(Check("still exactly ONE node_circuit_breaker_tripped event after the 4th "
                            "failure -- an already-tripped breaker doesn't re-alert on every "
                            "subsequent failure of the same ongoing condition",
                            len(tripped_after_leg4) == 1,
                            f"count={len(tripped_after_leg4)}"))

        breaker_state_after_leg4 = json.loads(schwab_safety.NODE_BREAKER_PATH.read_text())
        node_state_after_leg4 = breaker_state_after_leg4.get(str(node['id']), {})
        checks.append(Check("streak keeps counting past the threshold in the state file even "
                            "though the alert itself doesn't re-fire",
                            node_state_after_leg4.get('order_failures_streak') == 4,
                            f"node_state={node_state_after_leg4}"))

        observations['node_wl_id'] = node['id']
        observations['price'] = price
        observations['shares'] = shares
        observations['final_streak'] = node_state_after_leg4.get('order_failures_streak')
    finally:
        broker.place_order = real_place_order
        schwab_client._ORDER_SUBMIT_RETRY_INTERVAL_SECS = orig_retry_interval
        schwab_safety.DUPLICATE_ORDER_WINDOW_SECS = orig_dup_window
        schwab_safety._now = orig_now

    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT wl.id AS wl_id, wl.account,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='node_circuit_breaker_tripped'
         AND result='tripped' AND node_id=wl.id) AS tripped_events
  FROM watch_list wl
 WHERE wl.ticker = ?
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one node, with exactly 1
    'tripped' event (not 2 -- the leg 4 no-re-alert check), directly from the
    harness DB."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['tripped_events'] == 1)
    return ok, rows
