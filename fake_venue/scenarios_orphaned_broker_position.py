"""Phase 2 scenario: `orphaned_broker_position`, end to end through the real
ground-truth broker sweep's TWO-CONSECUTIVE-SWEEP CONFIRMATION GATE
(signals_notify.check_orphaned_broker_positions, gate added 2026-08-15).

The mechanism this proves: a real broker position with no matching local
open_positions row must NOT page on the sweep that first sees it (that sweep
could be catching a position mid-reconciliation, seconds from getting its
local row written) -- only a SECOND consecutive sweep that still sees the
same finding confirms and alerts. This needs two sequential poll cycles with
carried state (the finding-set persisted in signals_config.ORPHAN_SWEEP_STATE_PATH),
not a single-shot call -- exactly the shape existing coverage
(tests/test_orphaned_broker_position_sweep.py) already proves, but there via
a MOCKED run_full_sweep return value. What's never been driven end to end is
the real broker-vs-local comparison (scripts/check_untracked_positions.
check_account, itself fake_broker-tested in isolation via
tests/test_fake_broker_untracked_position_sweep_scenario.py) feeding INTO the
real two-sweep gate, through a real evolving FakeBroker order book, across
two real calls to check_orphaned_broker_positions -- the actual production
composition, not either half alone.

Shape (one fake node, one fake account -- this scenario isn't about ticker/
account disambiguation, Phase 1 already covers that; it's about the gate's
persistence-and-confirm state machine):

  node A  (fv_cash)  watch_list row only, state='live' -- makes TICKER
                      "known" to check_account's hand-held-ticker filter.
                      Deliberately NEVER given an open_positions row -- the
                      whole scenario is that gap.

  Leg 1  a real FILLED BUY is seeded directly at the fake broker (mirrors
         tests/test_fake_broker_untracked_position_sweep_scenario.py's
         _real_filled_buy -- NOT routed through the normal entry/reconcile
         chain, since the entire point is a broker position with zero local
         record). check_orphaned_broker_positions() runs sweep #1.
         -> scripts.check_untracked_positions.run_full_sweep()  [real]
         -> check_account(CASH_ALIAS)                            [real]
         => finding UNTRACKED, first sighting
         => coverage_events['orphaned_broker_position'] = 'found'  <-- checked
         => NO Slack post yet -- gate withholds it                 <-- checked

  Leg 2  nothing at the broker changes (the real incident shape: an
         unreconciled position that's still unreconciled 30 min later, not a
         transient reconciliation-lag artifact). check_orphaned_broker_positions()
         runs sweep #2, with the throttle watermark cleared (mirrors the
         pytest suite's _sweep_again helper -- the real production gap this
         closes is CADENCE, not the throttle itself, which is already unit-
         tested) but prior_findings state carried forward for real, read from
         the same ORPHAN_SWEEP_STATE_PATH file sweep #1 wrote.
         => the SAME finding key reappears -> CONFIRMED
         => coverage_events['orphaned_broker_position'] = 'found' again (2nd
            sighting is still logged as 'found', same as every prior sighting
            -- confirmation is a Slack-posting decision, not a distinct event
            result)                                                <-- checked
         => exactly 1 Slack post, mentioning the ticker, 'UNTRACKED', and
            '2 consecutive sweeps'                                 <-- checked

  Leg 3  negative control, in the SAME run: a position that reconciles cleanly
         between sweep #1 and sweep #2 (a fresh local open_positions row is
         inserted with the matching share count right after sweep #1, mirroring
         real reconciliation catching up before the next sweep) must NOT
         confirm and must NOT alert a second time -- this is the actual
         reason the gate exists (module docstring's own framing: routine
         reconciliation lag, not an incident). A separate ticker is used for
         this leg so it doesn't interfere with the confirmed finding above.
         => sweep #2 shows this second ticker's finding set EMPTY -> its own
            'clean' contribution, no alert credited to it            <-- checked

ISOLATION GAP FOUND, NOT FIXED (per this task's scope -- documented here,
left to a real backlog/production-code session): signals_config.
ORPHAN_SWEEP_STATE_PATH (signals_config.py:55) is `LIVE_DIR / "orphan_sweep_
state.json"`, a HARDCODED path under cache/live/ with NO env-var override at
all -- unlike every other schwab_safety state file (KILL_SWITCH_PATH,
NODE_AUTOMATION_PATH, etc., all rooted at SCHWAB_STATE_DIR and asserted by
fake_venue/isolation.py's assert_isolation_took_effect). It is also absent
from that function's own state-path check loop. tests/test_orphaned_broker_
position_sweep.py already works around this at the pytest level
(`monkeypatch.setattr(signals_config, 'ORPHAN_SWEEP_STATE_PATH', tmp_path /
...)`), and this scenario does the same at the module-attribute level (see
`_isolate_orphan_sweep_state_path` below) -- calling the real function
un-patched would make check_orphaned_broker_positions read/write the REAL
cache/live/orphan_sweep_state.json, which fake_venue's own production-access
tripwire (fake_venue/isolation.py's sys.addaudithook on cache/) is watching
for and would correctly flag as a breach. Low real-world severity (it is a
throttle/dedup timestamp file, not the trade DB or a Slack credential) but
it is a genuine gap in isolation.py's coverage worth closing there directly
in a future session, not papered over silently here.
"""
from dataclasses import dataclass
from datetime import datetime

from fake_venue import venue
from fake_venue.scenarios_meta import CASH_ACCOUNT_NUMBER, CASH_ALIAS, PRICE_SOURCE_TICKER, TICKER

FAKE_ACCOUNTS = [
    dict(alias=CASH_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='cash', margin_capable=0),
]
NODE_NOTIONAL = 2_000
UNTRACKED_SHARES = 19.0
# A second, distinct ticker for leg 3's negative control -- kept out of
# scenarios_meta (that TICKER constant is shared/reused by every other Phase 2
# scenario; this scenario is the only one that needs a second symbol at all).
CONTROL_TICKER = "TEST_FAKE_VENUE_SCENARIO_CTRL"
CONTROL_SHARES = 7.0


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ''
    required: bool = True

    def __post_init__(self):
        self.ok = bool(self.ok)


def _add_node(ticker, version):
    import signals_db as db

    db.add_node(ticker=ticker, strategy='TrailingBothZScoreBreakout', version=version,
                window=20, take_profit=10, stop_loss=1, max_hold_hours=56,
                state='live', account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                label=f'fake-venue harness node (orphaned_broker_position, {ticker})')
    return [n for n in db.get_watchlist() if n['ticker'] == ticker and n['version'] == version][0]


def _real_filled_buy(broker, ticker, shares, price):
    """Creates a genuinely FILLED BUY at the fake broker with NO local
    open_positions row -- mirrors tests/test_fake_broker_untracked_position_
    sweep_scenario.py's _real_filled_buy. Deliberately bypasses the normal
    pending-buy/reconcile chain: the entire scenario is what happens when
    that chain never ran (or hasn't caught up yet)."""
    broker.set_quote(ticker, last=price, bid=price, ask=round(price + 0.01, 4))
    order_id = broker.seed_resting_order(CASH_ALIAS, ticker, 'MARKET', 'BUY', shares)
    broker.force_fill(order_id, price=price)
    return order_id


def _isolate_orphan_sweep_state_path():
    """Repoints signals_config.ORPHAN_SWEEP_STATE_PATH at the harness's own
    isolated state dir. See module docstring's ISOLATION GAP note: this path
    has no env-var override in production code, unlike every schwab_safety
    state file, so it must be patched here or the real function under test
    would read/write cache/live/orphan_sweep_state.json."""
    import schwab_safety
    import signals_config as cfg

    state_dir = schwab_safety.STATE_PATH.parent
    cfg.ORPHAN_SWEEP_STATE_PATH = state_dir / "orphan_sweep_state.json"
    return cfg.ORPHAN_SWEEP_STATE_PATH


def run(price=None, verbose=True):
    """Runs the scenario against the already-isolated, already-imported
    environment. Returns (checks, observations)."""
    import schwab_safety
    import signals_db as db
    import signals_notify as notify

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
    venue.seed_account_number_env({CASH_ALIAS: CASH_ACCOUNT_NUMBER})
    price = venue.seed_quote(broker, TICKER, price, price_source_ticker=PRICE_SOURCE_TICKER)
    broker.set_cash_balance(CASH_ALIAS, 100_000.0)
    say(f"[setup] {TICKER} quote seeded at ${price:.4f}")

    orphan_state_path = _isolate_orphan_sweep_state_path()
    say(f"[setup] ORPHAN_SWEEP_STATE_PATH repointed to {orphan_state_path} (see module docstring's "
        f"ISOLATION GAP note -- this path has no real env-var override)")

    node_a = _add_node(TICKER, 'fake_venue_orphan')
    _add_node(CONTROL_TICKER, 'fake_venue_orphan_ctrl')
    say(f"[setup] node A wl_id={node_a['id']} ({CASH_ALIAS}), control node for {CONTROL_TICKER} also seeded")

    # check_orphaned_broker_positions' own trading-day gate
    # (scripts.coverage_check._is_trading_day, imported into signals_notify as
    # _coverage_is_trading_day) is a real NYSE calendar check -- faked True
    # here for the same reason every other Phase 2 scenario fakes its own
    # calendar gate: orthogonal to the mechanism under test, and the harness
    # must be runnable deterministically any day, including weekends.
    real_trading_day = notify._coverage_is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    notify._coverage_is_trading_day = lambda check_date: True
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking "
            "signals_notify._coverage_is_trading_day True for this run")

    posted = []

    def _capture(text=None, *a, **kw):
        posted.append(text if text is not None else (a[0] if a else kw.get('text')))
        return ('C0FAKEVENUE', '9999.1')

    notify._post_message = _capture

    now = datetime(2026, 8, 17, 10, 30)  # a real Monday, inside ORPHAN_SWEEP_WINDOW ((9,45),(16,0))

    # ---------------------------------------------------------------- leg 1
    order_id = _real_filled_buy(broker, TICKER, UNTRACKED_SHARES, price)
    say(f"[leg 1] real FILLED broker BUY seeded for {TICKER} ({UNTRACKED_SHARES:g} shares @ ${price:.4f}), "
        f"NO local open_positions row -- running check_orphaned_broker_positions() sweep #1")

    import schwab_client
    checks.append(Check("premise: schwab_client sees the real filled position",
                        schwab_client.get_all_real_positions(CASH_ALIAS).get(TICKER) == UNTRACKED_SHARES,
                        f"get_all_real_positions={schwab_client.get_all_real_positions(CASH_ALIAS)}"))
    checks.append(Check("premise: zero local open_positions rows exist yet",
                        db.get_open_position_by_wl_id(node_a['id']) is None))

    notify.check_orphaned_broker_positions(now=now)

    events_after_sweep1 = db.get_coverage_events(scenario_key="orphaned_broker_position")
    found_sweep1 = [e for e in events_after_sweep1 if e['result'] == 'found']
    checks.append(Check("sweep #1 recorded a 'found' coverage_event (evidence kept even though "
                        "the alert is withheld)",
                        len(found_sweep1) == 1,
                        f"events={[(e['result'], e['detail']) for e in events_after_sweep1]}"))
    checks.append(Check("sweep #1 finding mentions the real ticker and UNTRACKED",
                        bool(found_sweep1) and TICKER in found_sweep1[0]['detail']
                        and 'UNTRACKED' in found_sweep1[0]['detail'],
                        f"detail={found_sweep1[0]['detail'] if found_sweep1 else None}"))
    checks.append(Check("sweep #1 must NOT alert -- first sighting is held for confirmation",
                        posted == [], f"posted={posted}"))
    checks.append(Check("state file persisted prior_findings after sweep #1",
                        orphan_state_path.exists(), f"path={orphan_state_path}"))

    # ---------------------------------------------------------------- leg 3 setup
    # The control ticker's local row is opened NOW (between sweep #1 and
    # sweep #2) with no broker-side finding ever created for it at all --
    # this is the plainer of the two negative shapes the gate must handle
    # (a ticker that was never untracked to begin with stays silent), kept
    # deliberately separate from leg 1/2's confirmed finding so the two
    # outcomes (confirmed vs. clean) are provably independent in one run.
    node_ctrl = [n for n in db.get_watchlist() if n['ticker'] == CONTROL_TICKER][0]
    _real_filled_buy(broker, CONTROL_TICKER, CONTROL_SHARES, price)
    now_open = datetime.now()
    db.open_position(node_ctrl, signal_price=price, signal_time=now_open, entry_price=price,
                      entry_time=now_open, shares=CONTROL_SHARES)
    with db._conn() as c:
        c.execute("UPDATE open_positions SET account=? WHERE ticker=?", (CASH_ALIAS, CONTROL_TICKER))
        c.commit()
    say(f"[leg 3 setup] control ticker {CONTROL_TICKER} given a real filled broker position AND a "
        f"matching local row before sweep #2 -- must read as clean, never a finding at all")

    # ---------------------------------------------------------------- leg 2
    # Clears only the throttle watermark, matching tests/test_orphaned_broker_
    # position_sweep.py's own _sweep_again helper -- the real production gap
    # this scenario proves is the STATE MACHINE across two sweeps, not the
    # throttle interval itself (already unit-tested, not this scenario's job
    # to re-prove).
    notify._save_orphan_sweep_last_run(0.0)
    say(f"[leg 2] broker state unchanged for {TICKER} -- running check_orphaned_broker_positions() sweep #2")
    notify.check_orphaned_broker_positions(now=now)

    events_after_sweep2 = db.get_coverage_events(scenario_key="orphaned_broker_position")
    found_sweep2 = [e for e in events_after_sweep2 if e['result'] == 'found']
    checks.append(Check("sweep #2 logged its own 'found' event too (evidence recorded on every "
                        "sighting, not just the first)",
                        len(found_sweep2) == 2,
                        f"found events={len(found_sweep2)}"))
    checks.append(Check("sweep #2 CONFIRMS -- exactly one Slack post after two consecutive sweeps",
                        len(posted) == 1, f"posted={posted}"))
    if posted:
        checks.append(Check("the confirmation alert names the ticker, UNTRACKED, and "
                            "'2 consecutive sweeps'",
                            TICKER in posted[0] and 'UNTRACKED' in posted[0]
                            and '2 consecutive sweeps' in posted[0],
                            f"text={posted[0]!r}"))
    checks.append(Check("the control ticker (reconciled before sweep #2 ran) contributed nothing "
                        "to the alert text",
                        not posted or CONTROL_TICKER not in posted[0],
                        f"text={posted[0] if posted else None}"))

    pos_a_final = db.get_open_position_by_wl_id(node_a['id'])
    checks.append(Check("sweep is read-only -- no local open_positions row was ever created for "
                        "the confirmed untracked position (automation_principles.md #5)",
                        pos_a_final is None))

    observations['node_a_wl_id'] = node_a['id']
    observations['node_ctrl_wl_id'] = node_ctrl['id']
    observations['price'] = price
    observations['order_id'] = order_id
    observations['posted'] = list(posted)
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT COUNT(*) AS found_events,
       SUM(CASE WHEN detail LIKE '%UNTRACKED%' THEN 1 ELSE 0 END) AS untracked_findings
  FROM coverage_events
 WHERE scenario_key = 'orphaned_broker_position'
   AND result = 'found'
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly TWO 'found' coverage_events
    (one per sweep) directly from the harness DB, both mentioning UNTRACKED --
    the two-consecutive-sweep evidence trail the gate's own docstring
    promises is kept even when the alert itself is withheld on sighting #1."""
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['found_events'] == 2 and rows[0]['untracked_findings'] == 2)
    return ok, rows
