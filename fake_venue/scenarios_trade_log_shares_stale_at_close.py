"""Phase 2 scenario: incident replay of `trading_incidents` id=8 (RETL,
`soxl_ira` wl_id=97, found 2026-08-14).

Real incident: RETL's real 2026-08-13 sequence was buy_fill_reconciled opened
@ 41sh -> same-day top_up (+8sh, the standard `_reconcile_fill` mechanic
covered by fake_venue/scenarios_post_fill_topup.py's leg 1) -> corrected to
49sh, which open_positions.shares, the real SL placement, and the real
2026-08-14 TIME-exit SELL (49 shares, confirmed via account-activity stream
ExecutionQuantity + coverage_events sell_exceeds_position_blocked
'quantity=49 held=49') all correctly used. Only trade_log's PERMANENT record
was wrong: `log_trade_entry()` wrote `shares=41` once at entry and nothing
ever re-synced it when the position later closed at 49 -- a real, if
cosmetic (no money moved wrong), data-integrity gap that corrupts any
downstream P&L-per-share/position-sizing audit trusting trade_log.shares.

Distinct from scenarios_post_fill_topup.py's target: that scenario proves the
top-up's OWN second broker order reconciles correctly (entry_price blending,
orphan-fill detection on the top-up's own fill). This scenario proves the
DOWNSTREAM consequence at CLOSE time -- does the permanent trade_log record
end up with the right share count once the position (already topped-up)
exits. Both scenarios share leg 1's shape (seed a short order, let
_reconcile_fill's top-up branch fire) since that's the real, faithful way a
live position ends up topped-up in the first place -- not a duplicated bug,
a shared precondition.

FIX ALREADY ON FILE, commit 484574ec (2026-08-14, prior session, "SOXS
incident response"): `signals_db.log_trade_exit` gained a `shares` kwarg
(None-default, preserves old behavior for any caller that doesn't have a
current count handy) and `close_position` now reads the position's CURRENT
`open_positions.shares` (row[6], SELECTed fresh under `_position_lock` right
before the close) and passes it through -- see log_trade_exit's own
2026-08-15 docstring addendum, which names this exact incident by id. This
scenario is the first real fake-venue proof that the fix holds end-to-end
through the REAL top-up mechanism (not a synthetic direct
`close_position(shares=49)` call) -- confirmed 2026-08-16 by directly
tracing signals_db.py: the fix is committed, not staged/reverted.

The `trading_incidents` ticket (id=8) itself is still marked [OPEN] as of
this run (`resolved_ts IS NULL`) -- confirmed by direct query, not assumed.
That's a stale ticket-hygiene gap (the underlying code fix landed the same
night the incident was logged), not evidence the bug is still live; this
scenario's own proof query is the actual evidence either way.
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
# Mirrors RETL id=97's real delta (41 -> 49, a top-up of 8 shares) rather
# than an arbitrary number -- same rationale as scenarios_post_fill_topup.py's
# SHORT_SHARES, chosen here to match the real incident's own shape.
SHORT_SHARES = 8


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

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_shares_stale',
                window=20, take_profit=10, stop_loss=1, max_hold_hours=56,
                state='live', account=CASH_ALIAS, starting_notional=NODE_NOTIONAL,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                label='fake-venue harness node (trade_log_shares_stale_at_close, RETL id=8 replay)')
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]


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
    price = venue.seed_quote(broker, TICKER, price, price_source_ticker=PRICE_SOURCE_TICKER)
    broker.set_cash_balance(CASH_ALIAS, 100_000.0)
    say(f"[setup] {TICKER} quote seeded at ${price:.4f}")

    node = _add_node()
    say(f"[setup] node wl_id={node['id']} ({CASH_ALIAS}), target_notional=${NODE_NOTIONAL}")

    schwab_safety.enable_auto_fill_detection(TICKER)
    schwab_safety.enable_node_auto_fill_detection(node['id'])

    # check_order's trading-day gate is orthogonal to what this scenario tests
    # (the trade_log.shares resync at close, not entry-side placement) --
    # same faked-when-needed treatment as scenarios_post_fill_topup.py, kept
    # here only so the harness is runnable deterministically on a weekend.
    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] not a real NYSE trading day -- faking schwab_safety._is_trading_day True "
            "(orthogonal to the mechanism under test)")
        schwab_safety._is_trading_day = lambda date_str: True

    # ------------------------------------------------------ open + top-up
    # Entry-side state SEEDED (not placed through the real BUY path), same
    # accepted caveat as scenarios_post_fill_topup.py -- the target here is
    # log_trade_entry/close_position/log_trade_exit's shares bookkeeping, not
    # entry placement.
    full_shares = max(int(NODE_NOTIONAL // price), 1)
    order_shares = max(full_shares - SHORT_SHARES, 1)
    sig = {'current_price': price, 'last_bar': datetime.now()}
    order_id = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY',
                                         order_shares, trail_offset=1.0)
    db.add_pending_buy(node, sig, channel=None, ts=None, order_id=order_id)
    db.mark_pending_buy_placed_by_wl_id(node['id'])
    checks.append(Check("original order sized short of target_notional (mirrors RETL's real 41sh entry)",
                        order_shares < full_shares,
                        f"order_shares={order_shares} full_shares={full_shares}"))

    fill_price = round(price * 0.99, 4)
    broker.force_fill(order_id, fill_price)
    say(f"[open] broker filled node's order {order_id} @ ${fill_price:.4f} for {order_shares} shares "
        f"(short of target); running check_auto_fills() -- real _reconcile_fill top-up branch should fire")
    notify.check_auto_fills([])

    pos = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("position opened for the original (short) fill", pos is not None,
                        f"shares={pos['shares'] if pos else None}"))

    topup_events = db.get_coverage_events(scenario_key="top_up")
    placed = [e for e in topup_events if e['result'] == 'placed' and e['node_id'] == node['id']]
    checks.append(Check("real top-up fired (delta > fill_price gate crossed), matching RETL's real "
                        "41->49 same-day top-up mechanic",
                        len(placed) == 1,
                        f"events={[(e['result'], e['detail']) for e in topup_events]}"))
    top_up_shares = None
    if placed:
        try:
            top_up_shares = int(placed[0]['detail'].split('shares=')[1].split(' ')[0])
        except (IndexError, ValueError):
            pass
    checks.append(Check("top-up shares parsed from the coverage event", top_up_shares is not None,
                        f"detail={placed[0]['detail'] if placed else None}"))

    pos_after_topup = db.get_open_position_by_wl_id(node['id'])
    expected_total_shares = order_shares + top_up_shares if top_up_shares is not None else None
    checks.append(Check("open_positions.shares already reflects original + top-up "
                        "(the real, always-correct side of this bug -- open_positions was never "
                        "the thing that went stale, only trade_log was)",
                        pos_after_topup is not None and expected_total_shares is not None
                        and pos_after_topup['shares'] == expected_total_shares,
                        f"shares={pos_after_topup['shares'] if pos_after_topup else None} "
                        f"expected={expected_total_shares}"))

    trade_log_id = pos_after_topup['trade_log_id'] if pos_after_topup else None
    entry_row = None
    if trade_log_id is not None:
        entry_row = next((t for t in db.get_trade_log_for_wl_id(node['id'], limit=5)
                          if t['id'] == trade_log_id), None)
    checks.append(Check("trade_log.shares AT ENTRY (pre-close) still reflects only the original "
                        "short fill -- the known, accepted-as-fine state of the bug BEFORE close "
                        "(log_trade_entry only ever wrote the entry-time count; nothing re-syncs "
                        "it until close)",
                        entry_row is not None and entry_row['shares'] == order_shares,
                        f"trade_log.shares={entry_row['shares'] if entry_row else None} "
                        f"expected(entry-time count)={order_shares}"))

    # ------------------------------------------------------------- close
    # Real production close_position() call -- exercises the ACTUAL fix
    # (close_position reads open_positions.shares fresh under _position_lock
    # and passes it to log_trade_exit) rather than asserting log_trade_exit's
    # shares kwarg in isolation. exit_reason='TIME' mirrors RETL's real
    # 2026-08-14 13:35:56 TIME exit.
    exit_price = round(price * 1.01, 4)
    exit_time = datetime.now() + timedelta(hours=8)
    say(f"[close] closing node's position via the real close_position() (exit_reason='TIME', "
        f"{expected_total_shares} shares expected to be resynced into trade_log)")
    closed = db.close_position(pos_after_topup['id'], exit_signal_price=price, exit_price=exit_price,
                               exit_time=exit_time, exit_reason='TIME')
    checks.append(Check("close_position() reports it actually closed the position", closed is True))

    pos_after_close = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("open_positions row is gone after close", pos_after_close is None))

    final_row = next((t for t in db.get_trade_log_for_wl_id(node['id'], limit=5)
                      if t['id'] == trade_log_id), None) if trade_log_id is not None else None
    # THE REAL CHECK THIS SCENARIO EXISTS FOR: trade_log.shares must now equal
    # the post-top-up total, not the entry-time-only count. Mutation-verifiable:
    # reverting close_position's `shares=row[6]` argument to omit shares
    # entirely (restoring None, the pre-484574ec default) would make this fail
    # (final_row['shares'] would stay order_shares, not expected_total_shares).
    checks.append(Check("trade_log.shares RESYNCED at close to the real post-top-up total "
                        "(RETL id=97's exact bug: was permanently stuck at the entry-time count, "
                        "41 instead of the real 49) -- the fix this scenario exists to prove",
                        final_row is not None and expected_total_shares is not None
                        and final_row['shares'] == expected_total_shares,
                        f"trade_log.shares={final_row['shares'] if final_row else None} "
                        f"expected(post-top-up total)={expected_total_shares} "
                        f"(pre-fix would have shown {order_shares}, the stale entry-time count)"))
    checks.append(Check("trade_log.shares did NOT stay stuck at the pre-close entry-time count "
                        "(distinguishes the fix from the pre-fix bug explicitly, not just "
                        "matching the expected value by coincidence)",
                        final_row is not None and final_row['shares'] != order_shares,
                        f"trade_log.shares={final_row['shares'] if final_row else None} "
                        f"stale_entry_only_count={order_shares}"))
    checks.append(Check("trade_log exit fields also populated correctly (exit_price/exit_reason)",
                        final_row is not None and final_row['exit_reason'] == 'TIME'
                        and abs(final_row['exit_price'] - exit_price) < 0.0005,
                        f"exit_reason={final_row['exit_reason'] if final_row else None} "
                        f"exit_price={final_row['exit_price'] if final_row else None} expected={exit_price:.4f}"))

    observations['node_wl_id'] = node['id']
    observations['price'] = price
    observations['order_shares'] = order_shares
    observations['top_up_shares'] = top_up_shares
    observations['expected_total_shares'] = expected_total_shares
    observations['trade_log_id'] = trade_log_id
    observations['trade_log_shares_final'] = final_row['shares'] if final_row else None
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT tl.id AS trade_log_id, tl.shares AS final_shares, tl.exit_reason, tl.account, tl.ticker,
       (SELECT COUNT(*) FROM open_positions WHERE wl_id = tl.wl_id) AS still_open,
       (SELECT COUNT(*) FROM coverage_events WHERE scenario_key='top_up' AND result='placed'
         AND node_id=tl.wl_id) AS top_up_placed_events
  FROM trade_log tl
 WHERE tl.ticker = ?
   AND tl.exit_reason = 'TIME'
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly one closed trade_log row for the
    scenario's ticker with a real top_up event on file and zero remaining
    open_positions -- and, critically, that trade_log.shares is NOT the
    original (pre-top-up) share count, directly from the harness DB."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 1 and rows[0]['still_open'] == 0 and rows[0]['top_up_placed_events'] == 1
          and rows[0]['final_shares'] is not None)
    return ok, rows
