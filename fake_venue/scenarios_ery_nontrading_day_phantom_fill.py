"""Phase 2 scenario: incident replay of `trading_incidents` id=1 (ERY,
`soxl_ira` wl_id=105, 2026-07-26 -- the FIRST incident on file). Ticket
confirmed [OPEN] (`resolved_ts IS NULL`) by direct query as of this run --
NOT assumed resolved just because it's old.

Real incident, 3 distinct sub-failures, all reproduced here as 3 legs against
REAL code (not asserted from reading docstrings alone):

  1. `active_signals._in_window()`'s open_check window ran on a Sunday
     against stale Friday cache; the z-score signal still read BUY, and a
     real TRAILING_STOP BUY order for ERY (1 sh) was placed at the broker.
  2. A fill-detection path then recorded it as FILLED without confirming
     against the broker, opening a phantom `open_positions` row (2 sh @
     $10.14 -- note the share count didn't even match the 1-share order,
     consistent with `get_filled_order`'s pre-fix fuzzy "most-recent-FILLED-
     order-for-this-ticker+side" fallback matching a stale, unrelated prior
     fill instead of the actual (never-filled, market-closed) Sunday order).
  3. The follow-on stop-loss placement was correctly REJECTED by Schwab
     ("oversold/overbought" -- no real position existed) but that rejection
     only fired a coverage_events row, never a Slack alert -- found by the
     user manually checking, not by anything paging them.

Root cause (leg 1) already fixed and this scenario's own leg 1 is the direct
proof: `docs/deep_backlog.md`'s 2026-07-26 "NYSE trading-day gate" entry adds
an unconditional BUY-side check to `schwab_safety.check_order` (the real
chokepoint every placement path routes through) -- confirmed live in the
current code at schwab_safety.py's `if side == "BUY": ... if not
_is_trading_day(...): raise SafetyViolation(...)` block, logging
`buy_trading_day_block`.

Leg 2's specific mechanism (a fuzzy, order-id-less get_filled_order match
returning a stale unrelated fill) was independently found and fixed the
NEXT day, 2026-07-27 (GDXU incident, same defect class) --
`get_filled_order(order_id=...)` now looks up that EXACT order only and
returns None if it isn't FILLED, never substituting a different, older
FILLED order. This scenario's leg 2 recreates the exact hazard shape (a
genuinely stale prior FILLED order sitting in the broker's order book for
the same ticker+side) and confirms the exact-order-id path does NOT get
confused by it -- the real question this incident leaves open, since the
deep_backlog entry never says which fill-detection call site involved in the
real 2026-07-26 event as a matter of separate proof.

Leg 3 (SL-rejection Slack silence) -- `schwab_client._post_order_confirmation`
(built 2026-07-24, TWO DAYS BEFORE this incident) already polls the real
order status after placement and posts a distinct 'REJECTED by Schwab' alert
+ raises OrderRejected for exactly this shape. Since the fix predates the
incident in commit history but the incident still happened with no alert,
the most likely explanation is a stale running daemon process (a repeatedly-
documented failure mode in this project -- see CLAUDE.md's daemon-restart
notes) rather than a code gap; this scenario's leg 3 is the direct proof that
CURRENT code, actually invoked, does alert.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

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

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_ery_replay',
                window=20, take_profit=10, stop_loss=1, max_hold_hours=56,
                state='live', account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                label='fake-venue harness node (ERY id=1 non-trading-day replay)')
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]


def run(price=None, verbose=True):
    """Runs the scenario against the already-isolated, already-imported
    environment. Returns (checks, observations)."""
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
    say(f"[setup] node wl_id={node['id']} ({CASH_ALIAS})")

    posted = []

    def _capture(*a, **kw):
        posted.append(a[0] if a else kw.get('text'))
        return (None, None)

    schwab_client._post_message = _capture

    # ------------------------------------------------------------- leg 1
    # A real Sunday, the actual incident date -- not just "not today". A
    # date-string check like _is_trading_day's is date-driven, not
    # relative-to-now, so faking today's actual weekday would be a weaker
    # proof than using the real incident's own calendar date.
    real_sunday = '2026-07-26'
    say(f"[leg 1] faking schwab_safety._now() to {real_sunday} (the real ERY incident's Sunday) "
        f"and attempting the SAME real order type ERY's real BUY used (TRAILING_STOP)")
    schwab_safety._now = lambda: datetime.strptime(real_sunday, '%Y-%m-%d')

    raised = None
    try:
        schwab_client.place_trailing_buy(CASH_ALIAS, TICKER, 1, price, 1.0, node_id=node['id'])
    except schwab_safety.SafetyViolation as e:
        raised = e
    checks.append(Check("place_trailing_buy raises SafetyViolation on the real incident's Sunday date "
                        "(the exact order type/flow ERY's real order used)",
                        raised is not None, f"raised={raised}"))

    gate_events = db.get_coverage_events(scenario_key="buy_trading_day_block")
    blocked = [e for e in gate_events if e['result'] == 'blocked' and e['ticker'] == TICKER]
    checks.append(Check("buy_trading_day_block coverage event fired", len(blocked) == 1,
                        f"events={[(e['result'], e['detail']) for e in gate_events]}"))
    checks.append(Check("BLOCKED alert posted to Slack for the blocked attempt",
                        any('BLOCKED' in (m or '') for m in posted), f"posted={posted}"))
    checks.append(Check("zero broker orders exist -- the phantom order never reached the broker "
                        "(the real 2026-07-26 order, order 1007336072120, should never have existed "
                        "under current code)",
                        len(broker.orders) == 0, f"orders={list(broker.orders)}"))

    # restore real _now() before leg 3's poll-based status checks, which use
    # wall-clock-relative internals elsewhere in schwab_client/schwab_safety
    # that must not run against a frozen 2026-07-26.
    schwab_safety._now = datetime.now

    # ------------------------------------------------------------- leg 2
    # Recreate the fuzzy-match hazard directly: a genuinely stale, unrelated
    # FILLED order sits in the broker's order book for this exact ticker+side
    # (mirrors "2 sh @ $10.14" not matching the real 1-share Sunday order --
    # a stale prior fill is exactly what a NO-order_id fallback would have
    # matched instead of correctly seeing "not filled yet"). The REAL Sunday
    # order (never actually fillable -- market closed) is seeded WORKING,
    # not FILLED.
    stale_order_id = broker.seed_resting_order(CASH_ALIAS, TICKER, 'MARKET', 'BUY', 2)
    stale_fill_price = round(price * 0.81, 4)  # deliberately distinct from the real quote
    broker.force_fill(stale_order_id, stale_fill_price)  # populates real executionLegs, matching a genuine FILLED order
    say(f"[leg 2] seeded+filled a genuinely stale unrelated FILLED order {stale_order_id} "
        f"(2 sh @ ${stale_fill_price:.4f}) for the same ticker+side, then a fresh Sunday order that "
        f"never actually filled (still WORKING)")
    real_order_id = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY', 1,
                                              trail_offset=1.0, status='WORKING')

    fill = schwab_client.get_filled_order(CASH_ALIAS, TICKER, 'BUY', order_id=real_order_id)
    checks.append(Check("get_filled_order(order_id=<the real Sunday order>) returns None -- does NOT "
                        "substitute the stale unrelated FILLED order (the exact 2026-07-27 GDXU-class "
                        "fix; a pre-fix order_id=None fallback would have wrongly matched the stale "
                        "2-share fill here)",
                        fill is None, f"fill={fill}"))

    fill_fuzzy = schwab_client.get_filled_order(CASH_ALIAS, TICKER, 'BUY', order_id=None)
    checks.append(Check("the OLD fuzzy order_id=None fallback WOULD have matched the stale fill "
                        "(demonstrates the hazard is real, not hypothetical -- this mode still exists "
                        "for call sites with no order to check, it's just no longer used for a "
                        "known/attributable order)",
                        fill_fuzzy is not None and fill_fuzzy['quantity'] == 2,
                        f"fill_fuzzy={fill_fuzzy}"))

    # ------------------------------------------------------------- leg 3
    # Phantom position seeded directly (mirrors what leg 2's now-fixed hazard
    # would have wrongly created) -- this leg's target is the SL-rejection
    # alert path, not the position-creation mechanism itself.
    pos_opened = db.open_position(node, signal_price=price, signal_time=datetime.now(),
                                  entry_price=price, entry_time=datetime.now(), shares=2)
    checks.append(Check("phantom position seeded (mirrors the real 2 sh @ $10.14 phantom row)",
                        pos_opened is True))

    broker.force_reject_next_order('REJECTED')
    say("[leg 3] priming the broker to REJECT the follow-on stop-loss placement "
        "(mirrors Schwab's real 'oversold/overbought' rejection -- no real position existed)")
    posted_before_sl = len(posted)
    sl_raised = None
    try:
        schwab_client.place_stop_loss(CASH_ALIAS, TICKER, 2, round(price * 0.99, 2), node_id=node['id'])
    except schwab_client.OrderRejected as e:
        sl_raised = e
    checks.append(Check("place_stop_loss raises OrderRejected on a confirmed broker REJECTED status",
                        sl_raised is not None, f"raised={sl_raised}"))
    new_posts = posted[posted_before_sl:]
    checks.append(Check("a Slack alert WAS posted for the rejected SL -- current code closes the exact "
                        "2026-07-26 gap (rejection fired coverage_events but never alerted) -- "
                        "if this fails, the incident's leg-3 gap is genuinely still open, not "
                        "just a stale ticket",
                        any('REJECTED' in (m or '') for m in new_posts), f"new_posts={new_posts}"))

    observations['node_wl_id'] = node['id']
    observations['price'] = price
    observations['posted_all'] = posted
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT ce.scenario_key, ce.mode, ce.ticker, ce.node_id, ce.result, ce.detail, wl.account, wl.state
  FROM coverage_events ce
  JOIN watch_list wl ON wl.id = ce.node_id
 WHERE ce.scenario_key = 'buy_trading_day_block'
   AND ce.result = 'blocked'
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one 'blocked' buy_trading_day_block
    event, attached to a real watch_list node, directly from the harness DB --
    the root-cause fix's own direct evidence."""
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['result'] == 'blocked' and rows[0]['state'] == 'live')
    return ok, rows
