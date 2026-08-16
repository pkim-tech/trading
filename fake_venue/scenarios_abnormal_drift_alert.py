"""Phase 2 scenario: `abnormal_drift_alert`, real fills through the single
chokepoint check_abnormal_drift claims to be invoked from -- signals_db.
open_position()/close_position() -- deterministically forcing the drift
threshold breach and the 2/day escalation cap instead of waiting for a real
trade to drift organically (docs/backlog_cache.md's 2026-08-14 'abnormal-drift
liquidity-signal alert' item; docs/deep_backlog.md's 2026-08-15 build entry;
CLAUDE.md's Grid row still `wired-never-fired` as of this scenario's build --
tests/test_fake_broker_abnormal_drift_scenario.py already covers the function
in isolation via pytest, but nothing before this exercised it end-to-end
against a real, persisted, subprocess-isolated DB with proof-by-fresh-query,
the same distinction fake_venue's other scenarios draw against fake_broker).

Shape (one fake node, one fake account -- like the other single-ticker Phase 2
scenarios, this isn't about ticker/account disambiguation):

  node A  (fv_cash)  starting_notional pushed comfortably above
                      signals_config.CAPITAL_AT_STAKE_THRESHOLD (read live,
                      not hardcoded, so this stays correct under an env
                      override) -- has_capital_at_stake's dollar gate is a
                      real precondition of the mechanism under test, not
                      incidental scaffolding, so this scenario deliberately
                      proves the node clears it rather than assuming it does.

  Leg 1  (ENTRY, real broker fill, driven through the real slow-poll chain --
         schwab_client.get_filled_order -> notify._reconcile_buy_fill ->
         db.open_position_from_pending -> db.open_position()) a resting BUY
         order is seeded at the signal price, then filled at a price offset
         by 2x ABNORMAL_DRIFT_THRESHOLD_PCT (read live from signals_db, same
         reasoning as the notional above) so the drift is unambiguously past
         threshold regardless of calibration drift. This is the one leg that
         exercises fake_venue's actual differentiator over the pytest
         suite -- a real FakeBroker fill reconciled through the real
         production poll path, not a direct unit call.
         => coverage_events['abnormal_drift_alert'] = 'alerted', side=entry,
            count_today=0 (this ticker's 1st breach today)     <-- checked
         => exactly 1 Slack post, mentioning 'entry' and '1/2'  <-- checked

  Leg 2  (EXIT, direct db.close_position() call) check_abnormal_drift's own
         docstring is explicit that it fires from open_position()/
         close_position() "regardless of caller module" -- unlike the BUY
         side, there is no single real SELL-fill chokepoint function in this
         codebase to drive (SL sync-poll, trail sync-poll, and the manual
         Slack "Exited" handler each call close_position() directly with
         their own already-resolved exit_signal_price/exit_price, mirroring
         _reconcile_buy_fill's BUY-side role split across several sites, not
         one). Calling db.close_position() directly here is not a bypass of
         the mechanism under test -- close_position() *is* the mechanism's
         other real chokepoint, the exact function named in its own
         docstring, so this is the most faithful way to exercise it without
         inventing a 6th caller shape this project's real code doesn't have.
         exit_signal_price/exit_price are offset the same way leg 1's were.
         => coverage_events['abnormal_drift_alert'] = 'alerted', side=exit,
            count_today=1 (this ticker's 2nd breach today, exhausts the cap)
                                                                  <-- checked
         => a 2nd Slack post, mentioning 'exit' and '2/2'        <-- checked

  Leg 3  (2nd ENTRY, direct db.open_position() call, same day) the escalation
         cap itself: a 3rd same-day breach for this ticker must still be
         detected and logged, just not re-posted to Slack.
         => coverage_events['abnormal_drift_alert'] = 'suppressed_daily_cap',
            side=entry, count_today=2                            <-- checked
         => Slack post count UNCHANGED at 2 -- no 3rd nag         <-- checked
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from fake_venue import venue
from fake_venue.scenarios_meta import CASH_ACCOUNT_NUMBER, CASH_ALIAS, PRICE_SOURCE_TICKER, TICKER


FAKE_ACCOUNTS = [
    dict(alias=CASH_ALIAS, notional_cap=50_000, daily_order_cap=100,
         cash_settlement_type='cash', margin_capable=0),
]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ''
    required: bool = True

    def __post_init__(self):
        self.ok = bool(self.ok)


def _add_node(starting_notional):
    import signals_db as db

    db.add_node(ticker=TICKER, strategy='TrailingBothZScoreBreakout', version='fake_venue_drift',
                window=20, take_profit=10, stop_loss=1, max_hold_hours=56,
                state='live', account=CASH_ALIAS, starting_notional=starting_notional,
                trail_buy_pct=1.0, trail_pct=1.0, fixed_sl_override=1.0,
                label='fake-venue harness node (abnormal_drift_alert)')
    return [n for n in db.get_watchlist() if n['ticker'] == TICKER][0]


def run(price=None, verbose=True):
    """Runs the scenario against the already-isolated, already-imported
    environment. Returns (checks, observations)."""
    import signals_config as cfg
    import schwab_client
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

    # Read live rather than hardcode -- both are env-overridable
    # (CAPITAL_AT_STAKE_THRESHOLD, ABNORMAL_DRIFT_THRESHOLD_PCT), and this
    # scenario's whole point is to prove against the REAL, currently-active
    # gates, not a value that might silently drift out of sync with them.
    node_notional = cfg.CAPITAL_AT_STAKE_THRESHOLD + 2_000
    threshold_pct = db.ABNORMAL_DRIFT_THRESHOLD_PCT
    max_alerts = db.ABNORMAL_DRIFT_MAX_ALERTS_PER_TICKER_PER_DAY
    drift_offset_pct = threshold_pct * 2  # unambiguously past threshold, whatever it's calibrated to
    observations['node_notional'] = node_notional
    observations['threshold_pct'] = threshold_pct
    observations['max_alerts_per_day'] = max_alerts

    node = _add_node(node_notional)
    say(f"[setup] node wl_id={node['id']} ({CASH_ALIAS}), starting_notional=${node_notional} "
        f"(threshold=${cfg.CAPITAL_AT_STAKE_THRESHOLD}), drift_offset={drift_offset_pct:.2f}% "
        f"(2x ABNORMAL_DRIFT_THRESHOLD_PCT={threshold_pct}%)")

    checks.append(Check("node's starting_notional clears CAPITAL_AT_STAKE_THRESHOLD "
                        "(has_capital_at_stake's real dollar gate)",
                        node['starting_notional'] >= cfg.CAPITAL_AT_STAKE_THRESHOLD,
                        f"starting_notional={node['starting_notional']} "
                        f"threshold={cfg.CAPITAL_AT_STAKE_THRESHOLD}"))

    schwab_safety.enable_auto_fill_detection(TICKER)
    schwab_safety.enable_node_auto_fill_detection(node['id'])

    # Same environmental fake as every other Phase 2 scenario -- orthogonal to
    # the mechanism under test (the drift-alert chokepoint, not the calendar
    # gate), needed so the harness is runnable deterministically any day.
    real_trading_day = schwab_safety._is_trading_day(datetime.now().strftime('%Y-%m-%d'))
    observations['real_trading_day'] = real_trading_day
    if not real_trading_day:
        say("[setup] today is not a real NYSE trading day -- faking schwab_safety._is_trading_day "
            "True for this run (see module docstring: orthogonal to the mechanism under test)")
        schwab_safety._is_trading_day = lambda date_str: True

    posted = []

    def _capture(text=None, *a, **kw):
        posted.append(text if text is not None else (a[0] if a else kw.get('text')))
        return ('C0FAKEVENUE', '9999.1')

    def _drift_posts():
        # check_auto_fills' leg-1 call also triggers _place_stop_loss_for_position
        # (the ticker is in AUTOMATION_ENABLED_TICKERS -- same real behavior
        # post_fill_topup's module docstring calls out for its own broker-order
        # count), which posts its own unrelated "STOP LOSS submitted" message --
        # filtered out here so the drift-alert-specific checks below aren't
        # coupled to that separate mechanism's own message count.
        return [p for p in posted if 'drift' in p and 'threshold' in p]

    # check_abnormal_drift posts via `import schwab_client; schwab_client._post_message(...)`
    # (signals_db.py) -- patching the schwab_client binding is the one that
    # actually reaches it, same reasoning replace_target_mismatch's module
    # docstring gives for its own dual binding.
    schwab_client._post_message = _capture

    # ---------------------------------------------------------------- leg 1
    # ENTRY: real broker fill, reconciled through the real slow-poll chain.
    entry_fill_price = round(price * (1 + drift_offset_pct / 100), 4)
    sig = {'current_price': price, 'last_bar': datetime.now()}
    order_id = broker.seed_resting_order(CASH_ALIAS, TICKER, 'TRAILING_STOP', 'BUY',
                                         max(int(node_notional // price), 1), trail_offset=1.0)
    db.add_pending_buy(node, sig, channel=None, ts=None, order_id=order_id)
    db.mark_pending_buy_placed_by_wl_id(node['id'])
    say(f"[leg 1] seeded resting BUY {order_id} @ signal=${price:.4f}; broker fills @ "
        f"${entry_fill_price:.4f} ({drift_offset_pct:+.2f}% drift); running check_auto_fills()")
    broker.force_fill(order_id, entry_fill_price)
    notify.check_auto_fills([])

    pos = db.get_open_position_by_wl_id(node['id'])
    checks.append(Check("position opened for the real (drift-heavy) entry fill", pos is not None,
                        f"shares={pos['shares'] if pos else None} entry_price={pos['entry_price'] if pos else None}"))

    events = db.get_coverage_events(scenario_key='abnormal_drift_alert', limit=1000)
    my_events = [e for e in events if e['ticker'] == TICKER]
    entry_alerts = [e for e in my_events if e['result'] == 'alerted' and 'side=entry' in (e['detail'] or '')]
    checks.append(Check("check_abnormal_drift fired 'alerted' for the entry breach "
                        f"(drift {drift_offset_pct:+.2f}% > threshold {threshold_pct}%)",
                        len(entry_alerts) == 1,
                        f"events={[(e['result'], e['detail']) for e in my_events]}"))
    checks.append(Check("entry alert reports count_today=0 (this ticker's 1st breach today)",
                        len(entry_alerts) == 1 and 'count_today=0' in entry_alerts[0]['detail'],
                        f"detail={entry_alerts[0]['detail'] if entry_alerts else None}"))
    checks.append(Check("exactly 1 drift-alert Slack post after leg 1, tagged 1/{} and 'entry'"
                        .format(max_alerts),
                        len(_drift_posts()) == 1 and TICKER in _drift_posts()[0]
                        and 'entry' in _drift_posts()[0] and f"1/{max_alerts}" in _drift_posts()[0],
                        f"drift_posts={_drift_posts()}"))

    # ---------------------------------------------------------------- leg 2
    # EXIT: direct db.close_position() -- the mechanism's own OTHER real
    # chokepoint (see module docstring for why this is the faithful call, not
    # a bypass, for the SELL side specifically).
    exit_signal_price = entry_fill_price  # any baseline works; what matters is the offset below
    exit_fill_price = round(exit_signal_price * (1 - drift_offset_pct / 100), 4)
    say(f"[leg 2] closing wl_id={node['id']}'s position directly: exit_signal=${exit_signal_price:.4f} "
        f"exit_fill=${exit_fill_price:.4f} ({-drift_offset_pct:+.2f}% drift)")
    closed = db.close_position(pos['id'], exit_signal_price=exit_signal_price, exit_price=exit_fill_price,
                               exit_time=datetime.now(), exit_reason='SL')
    checks.append(Check("close_position() reported a real close", closed))

    events = db.get_coverage_events(scenario_key='abnormal_drift_alert', limit=1000)
    my_events = [e for e in events if e['ticker'] == TICKER]
    exit_alerts = [e for e in my_events if e['result'] == 'alerted' and 'side=exit' in (e['detail'] or '')]
    checks.append(Check("check_abnormal_drift fired 'alerted' for the exit breach",
                        len(exit_alerts) == 1,
                        f"events={[(e['result'], e['detail']) for e in my_events]}"))
    checks.append(Check("exit alert reports count_today=1 (this ticker's 2nd breach today, "
                        "exhausting the cap)",
                        len(exit_alerts) == 1 and 'count_today=1' in exit_alerts[0]['detail'],
                        f"detail={exit_alerts[0]['detail'] if exit_alerts else None}"))
    checks.append(Check("exactly 2 drift-alert Slack posts after leg 2 (1 new, tagged 2/{} and 'exit')"
                        .format(max_alerts),
                        len(_drift_posts()) == 2 and 'exit' in _drift_posts()[1]
                        and f"2/{max_alerts}" in _drift_posts()[1],
                        f"drift_posts={_drift_posts()}"))

    # ---------------------------------------------------------------- leg 3
    # 2nd ENTRY, same day: the cap itself. A 3rd breach must still be
    # detected and logged, just not re-posted -- direct db.open_position()
    # call for the same reason leg 2 used close_position() directly.
    entry_time3 = datetime.now() + timedelta(seconds=1)
    entry_fill_price3 = round(price * (1 + drift_offset_pct / 100), 4)
    say(f"[leg 3] re-opening wl_id={node['id']} directly for a 3rd same-day breach: "
        f"signal=${price:.4f} entry=${entry_fill_price3:.4f}")
    opened3 = db.open_position(node, signal_price=price, signal_time=entry_time3,
                               entry_price=entry_fill_price3, entry_time=entry_time3, shares=10)
    checks.append(Check("db.open_position() reported a real (re-)open for the 3rd breach", opened3))

    events = db.get_coverage_events(scenario_key='abnormal_drift_alert', limit=1000)
    my_events = [e for e in events if e['ticker'] == TICKER]
    checks.append(Check("exactly 3 abnormal_drift_alert events total for this ticker today",
                        len(my_events) == 3,
                        f"events={[(e['result'], e['detail']) for e in my_events]}"))
    suppressed = [e for e in my_events if e['result'] == 'suppressed_daily_cap']
    checks.append(Check(f"the 3rd breach logged 'suppressed_daily_cap' (cap={max_alerts}/day already "
                        "exhausted by legs 1+2), not a 3rd 'alerted'",
                        len(suppressed) == 1 and 'side=entry' in suppressed[0]['detail']
                        and 'count_today=2' in suppressed[0]['detail'],
                        f"detail={suppressed[0]['detail'] if suppressed else None}"))
    checks.append(Check("drift-alert Slack post count UNCHANGED at 2 -- the 3rd breach did NOT re-nag Slack",
                        len(_drift_posts()) == 2, f"drift_posts={_drift_posts()}"))

    observations['node_wl_id'] = node['id']
    observations['price'] = price
    observations['entry_fill_price'] = entry_fill_price
    observations['exit_fill_price'] = exit_fill_price
    observations['posted_count'] = len(posted)
    observations['drift_posted_count'] = len(_drift_posts())
    return checks, observations


# ---------------------------------------------------------------------------
# Proof-by-query: deliberately re-opens the harness DB with a plain sqlite3
# connection and asserts on the rows themselves, rather than trusting the
# in-process API calls above (or a log line) as evidence.
# ---------------------------------------------------------------------------

PROOF_SQL = """
SELECT ce.scenario_key, ce.mode, ce.ticker, ce.node_id, ce.result, ce.detail, ce.strategy_type,
       ce.id, wl.account, wl.state
  FROM coverage_events ce
  JOIN watch_list wl ON wl.id = ce.node_id
 WHERE ce.scenario_key = 'abnormal_drift_alert'
   AND ce.ticker = ?
 ORDER BY ce.id ASC
"""


def verify_proof(db_path):
    """Returns (ok, rows). ok requires exactly 3 rows in order (alerted/entry,
    alerted/exit, suppressed_daily_cap/entry), all mode='live', attached to
    the same real watch_list node, directly from the harness DB."""
    import sqlite3

    from fake_venue.scenarios_meta import TICKER as _ticker

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(PROOF_SQL, (_ticker,)).fetchall()]
    finally:
        conn.close()
    ok = (len(rows) == 3
          and rows[0]['result'] == 'alerted' and 'side=entry' in (rows[0]['detail'] or '')
          and rows[1]['result'] == 'alerted' and 'side=exit' in (rows[1]['detail'] or '')
          and rows[2]['result'] == 'suppressed_daily_cap' and 'side=entry' in (rows[2]['detail'] or '')
          and all(r['mode'] == 'live' for r in rows)
          and len({r['node_id'] for r in rows}) == 1
          and all(r['account'] == CASH_ALIAS for r in rows)
          and all(r['state'] == 'live' for r in rows))
    return ok, rows
