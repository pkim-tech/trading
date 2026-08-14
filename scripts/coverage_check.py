"""Daily expected-vs-actual coverage check -- piece #6 of the 2026-07-24
coverage-system reframe: every scenario in signals_db.scenario_expectations
has a documented expected outcome, and any deviation from it must get a
captured reason (signals_db.explain_deviation), not just a silent pass/fail.
An unexplained deviation (reason IS NULL) is itself the actionable finding --
"something looked off but nobody knows why" is never an acceptable end state.

Checks each active 'daily' scenario against the real trade_log/pending_buys
tables for today (or --date), records a coverage_deviations row when the
expectation isn't met, and prints a summary with every still-unexplained
deviation surfaced prominently.

Usage:
  .venv/bin/python scripts/coverage_check.py [--date YYYY-MM-DD]
  .venv/bin/python scripts/coverage_check.py --explain ID "reason text"
"""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pandas_market_calendars as mcal

import signals_db as db
import signals_compute as compute
import strategies

_NYSE_CAL = mcal.get_calendar('NYSE')


def _is_trading_day(check_date):
    """NYSE trading-day gate (weekends + market holidays) -- self-contained
    here rather than importing active_signals._is_trading_day, which would be
    a circular import (active_signals imports send_coverage_report, which
    calls this module). Same reasoning as the weekend-only guard this
    replaces (2026-07-25 Opus review): a market holiday is just as trivially,
    permanently false for a 'trade closed today' expectation as a weekend --
    checking it would manufacture real unexplained deviation tickets for
    every 'daily' scenario (8 as of 2026-07-30) with no real signal behind
    them (found by Opus review, 2026-07-30 -- the weekday()>=5 check alone
    doesn't catch a weekday market holiday)."""
    return not _NYSE_CAL.schedule(start_date=check_date, end_date=check_date).empty


def _entry_threshold_crossed(ticker, node, check_date):
    """Returns True/False if node's real entry (BUY) threshold provably did/didn't
    cross on check_date, using the SAME check_signal() the live daemon runs -- not a
    reimplementation of the z-score math -- against cached hourly price data (per
    docs/daily-routine-check's convention: recompute directly from cached data, never
    infer from a log grep). Returns None whenever this can't be determined with
    confidence (missing/unrecognized strategy, missing CSV, insufficient prior-day
    history, zero std) -- callers must only auto-explain a deviation on an explicit
    False, never on None, so a data gap fails toward leaving the ticket for a human
    to look at rather than silently excusing it (2026-08-08, built after confirming
    live against IVV: z stayed +0.8 to +2.8 across 4 straight days against a -0.1
    entry threshold -- the 'no closed trade' deviations really were just no signal).

    Restricted to the 2 bars the live daemon actually evaluates (hour=9 for the
    10:25-10:40 window, hour=14 for the 15:25-15:40 window -- active_signals.
    _SIGNAL_WINDOWS), not every hourly bar of the day. Fixed 2026-08-14: this
    previously checked hour.isin(range(9,15)) -- all 6 bars -- which is the
    BACKTEST kernel's target_hours=(9,14) continuous-evaluation scope, not the
    live daemon's actual twice-daily check. Found live: FAS/2026-08-13 showed a
    real crossing at the bar spanning 10:30-11:30 (Close reflects the price at
    11:30, an hour after the AM window already closed) while the real daemon
    log for that exact window shows z staying +2.4 to +4.9, never crossing --
    the daemon never had a chance to see it. The old scope made every such
    off-window dip look like a "confirmed missed signal," blocking auto-explain
    on what was actually a quiet day.

    Checks both bar Close AND bar Open (2026-08-08, paired-review finding) for a
    node with entry_timing='open_check' -- the live daemon's pinned entry scan
    (`active_signals._scan_pinned_entry`) evaluates the bar's Open, not just its
    Close, so checking Close alone could return False (auto-explain: no signal)
    for a node whose real Open leg actually crossed. A close-only node is
    unaffected -- the live daemon never evaluates its Open either."""
    strat_cls = getattr(strategies, node.get('strategy') or '', None)
    if strat_cls is None or not issubclass(strat_cls, strategies.BaseStrategy):
        return None
    df, df_daily = compute._load_cache(ticker)
    if df is None or df.empty:
        return None
    window = node.get('window')
    z_thresh = node.get('z_score_threshold')
    if not window or z_thresh is None:
        return None
    strat = strat_cls(window=window, z_score_threshold=z_thresh)
    ind = strat.generate_daily_indicators(df_daily)

    day = pd.Timestamp(check_date)
    daily_dates = ind.index.normalize()
    prior_days = daily_dates[daily_dates < day]
    if len(prior_days) == 0:
        return None
    prior_day = prior_days.max()
    sma, std = ind.loc[prior_day, "SMA"], ind.loc[prior_day, "Std"]
    if pd.isna(sma) or pd.isna(std) or std == 0:
        return None

    bars = df[(df.index.normalize() == day) & (df.index.hour.isin([9, 14]))]
    if bars.empty:
        return None
    check_open_too = node.get('entry_timing') == 'open_check'
    for _, bar in bars.iterrows():
        # Close (and Open, for open_check nodes) alone under-covers what the daemon's
        # continuous polling during the real 15-minute signal window could actually see --
        # the window spans the tail of this bar's formation, not just its final snapshot.
        # Low is the worst-case (most-likely-to-cross) price within the bar, so including
        # it keeps this failing toward "don't auto-explain away a real miss" (found by
        # Opus review, 2026-08-14: the prior Close/Open-only version could auto-explain a
        # genuine intra-window dip-and-recover the daemon actually saw and missed).
        prices = [bar["Close"]] + ([bar["Open"]] if check_open_too and "Open" in bar else [])
        if "Low" in bar:
            prices.append(bar["Low"])
        for price in prices:
            if strat.check_signal(dict(sma=sma, std=std, current_price=price)) == 'BUY':
                return True
    return False


def _check_trade_lifecycle(scenario, check_date):
    """Returns (met: bool, actual_summary: str, no_activity: bool). no_activity is
    True only when NOTHING happened at all (no pending_buys row, no closed trade) --
    False when a trade DID happen but with the wrong outcome (a real behavioral bug,
    never eligible for the price-action auto-explain below)."""
    params = json.loads(scenario['check_params'] or '{}')
    ticker = scenario['ticker']
    # ticker alone is ambiguous when a ticker has more than one real node (e.g.
    # GDXU: watch_list ids 88/108, different accounts/versions on the same
    # active watchlist) -- resolve the real node by its PK (node_id, if the
    # scenario carries one) and scope the trade_log/pending_buys lookup to it,
    # so a different node's trade can't silently satisfy this expectation
    # (found by Opus review, 2026-07-24).
    node_id = scenario.get('node_id')
    node = db.get_watch_list_node_by_id(node_id)
    if node_id is not None and node is None:
        # A scenario carries a node_id that no longer resolves (node deleted/
        # renamed) -- falling through to ticker-only scoping here would
        # silently reintroduce the exact ambiguity node_id exists to close,
        # with no signal that it happened. Surface it instead.
        print(f"  ! {scenario['scenario_key']:26s} {ticker or '':6s} node_id={node_id} no longer "
              f"resolves -- falling back to ticker-only scoping (ambiguous if ticker has >1 node)")
    # wl_id (2026-08-13): the strategy/version/window/account tuple alone stopped
    # being unique once the FAS/FAZ canary consolidation put multiple distinct
    # scenario nodes on the same ticker+strategy+version+window+account (found
    # by paired review of that same change) -- pass node_id through as an exact
    # disambiguator wherever a real node resolved, not just the tuple.
    disambig = dict(strategy=node.get('strategy'), version=node.get('version'),
                     window=node.get('window'), account=node.get('account'),
                     wl_id=node_id) if node else {}

    if params.get('expect_pending_carryover'):
        pending = db.get_pending_buys_for_ticker_on_date(ticker, check_date, **disambig)
        if pending:
            return True, f"pending_buys row present (order_placed={pending[0]['order_placed']})", False
        # Third real state (pending -> open -> closed) -- a same-day fill that
        # hasn't exited yet is real activity neither of the other two lookups
        # can see (2026-08-10, found via SDOW: filled same day, still open,
        # reported false 'no activity' -- see get_open_positions_for_ticker_on_date's
        # docstring for the full history of this bug shape recurring).
        open_pos = db.get_open_positions_for_ticker_on_date(ticker, check_date, **disambig)
        if open_pos:
            return True, f"same-day fill still open (shares={open_pos[0]['shares']}) instead of carryover -- not the designed scenario but real activity occurred", False
        trades = db.get_closed_trades_for_ticker_on_date(ticker, check_date, **disambig)
        if trades:
            return True, f"same-day fill instead of carryover (exit_reason={trades[0]['exit_reason']}) -- not the designed scenario but real activity occurred", False
        return False, f"no pending_buys row, no open position, and no closed trade found for {ticker} on {check_date}", True

    expect_reasons = params.get('expect_exit_reason', [])
    trades = db.get_closed_trades_for_ticker_on_date(ticker, check_date, **disambig)
    if not trades:
        # Same bug shape as the carryover branch above (2026-08-13, found via
        # VOO/QQQ/IVV: a trailing-buy fired and bounce-filled into a real open
        # position the next morning, but this branch only ever looked at
        # get_closed_trades_for_ticker_on_date for the exact check_date -- a
        # real, in-progress multi-day-hold trade read as "no activity" and
        # minted a false unexplained deviation every day until it closed.
        # Mirror the carryover branch's pending_buys -> open_positions
        # fallback: real activity in progress isn't the designed same-day
        # outcome, but it's not "nothing happened" either, so it must not be
        # eligible for the no_activity price-action auto-explain (which would
        # incorrectly reason about *today's* entry signal for a position that
        # already entered on an earlier day).
        pending = db.get_pending_buys_for_ticker_on_date(ticker, check_date, **disambig)
        if pending:
            return True, f"pending_buys row present (order_placed={pending[0]['order_placed']}) -- entry not yet resolved", False
        open_pos = db.get_open_positions_for_ticker_on_date(ticker, check_date, **disambig)
        if open_pos:
            return True, f"position still open (shares={open_pos[0]['shares']}) -- exit not yet resolved", False
        return False, f"no closed trade found for {ticker} on {check_date}", True
    reason = trades[0]['exit_reason']
    if reason in expect_reasons:
        return True, f"exit_reason={reason}", False
    return False, f"exit_reason={reason}, expected one of {expect_reasons}", False


def _check_coverage_event(scenario, check_date):
    """Returns (met: bool, actual_summary: str, no_activity: bool) for a
    'coverage_event' scenario
    -- the daily-firing counterpart to scenario_registry.compute_status(),
    scoped to a single day instead of all-time. scenario['scenario_key'] is
    the real coverage_events.scenario_key (reused directly, not a separate
    field) -- check_params optionally carries mode_filter/bad_results, same
    shape as scripts/coverage_registry.py's REGISTRY rows, so a row's
    daily-check definition never has to be kept in sync by hand with its
    REGISTRY entry (see seed_daily_coverage_expectations.py)."""
    params = json.loads(scenario['check_params'] or '{}')
    # mode_filter comes from the real scenario_expectations.mode column, not
    # check_params -- it must be part of add_scenario_expectation's dedup key
    # (scenario_key, node_id, ticker, mode) so two rows sharing one
    # coverage_events scenario_key under different modes (e.g. entry_fill
    # logged by both the dry_run-sim path and paper_trading.py) don't
    # silently collide/overwrite each other, same bug class as add_node's
    # take_profit=NULL dedup gap.
    mode_filter = scenario.get('mode')
    bad_results = set(params.get('bad_results', []))
    # coverage_events.ts defaults to SQLite datetime('now') -- UTC -- while
    # check_date (from _previous_trading_day) and trade_log's entry_time/
    # exit_time (used by _check_trade_lifecycle) are both real ET wall-clock
    # values written by application code. date(ts) alone would compare a UTC
    # date against an ET date, offsetting the checked window by ~4-5 hours --
    # confirmed against real data: 212 of 1799 existing coverage_events rows
    # have date(ts) != date(ts, 'localtime'). date(ts, 'localtime') converts
    # to the server's local zone (America/New_York) before comparing, keeping
    # this consistent with the other checker.
    q = "SELECT result, COUNT(*) n FROM coverage_events WHERE scenario_key = ? AND date(ts, 'localtime') = ?"
    args = [scenario['scenario_key'], check_date]
    if mode_filter:
        q += " AND mode = ?"
        args.append(mode_filter)
    # ticker/node_id scoping, same bug class already fixed for
    # _check_trade_lifecycle's node_id disambiguation above -- zero live
    # impact today since every seeded coverage_event scenario has ticker=
    # node_id=None, but a future per-ticker scenario would otherwise be
    # silently satisfied by a different ticker's/node's event under the same
    # scenario_key (caught by session-wrap Opus review before any such
    # scenario was actually seeded).
    if scenario.get('ticker'):
        q += " AND ticker = ?"
        args.append(scenario['ticker'])
    if scenario.get('node_id') is not None:
        q += " AND node_id = ?"
        args.append(scenario['node_id'])
    q += " GROUP BY result"
    with db._conn() as c:
        rows = c.execute(q, args).fetchall()
    if not rows:
        return False, f"no coverage_events for {scenario['scenario_key']!r} on {check_date}", False
    total = sum(r['n'] for r in rows)
    good = sum(r['n'] for r in rows if r['result'] not in bad_results)
    if good > 0:
        return True, f"{total}x event(s), {good} good, last result set {[r['result'] for r in rows]}", False
    return False, f"{total}x event(s), all bad_results {sorted(bad_results)}: {[r['result'] for r in rows]}", False


CHECKERS = {
    'trade_lifecycle': _check_trade_lifecycle,
    'coverage_event': _check_coverage_event,
}


# Mirrors scripts/audit_live_test_candidates.py's SCENARIO_ROLE_TO_GRID_IDS --
# duplicated rather than imported because that module pulls in active_signals
# (Slack Bolt app, daemon deps), which this script cannot afford: active_signals
# itself imports coverage_check for the EOD report, so importing back would be
# circular. Keep both copies in sync by hand when either changes (extend here
# whenever staging a new scenario_role there).
SCENARIO_ROLE_TO_GRID_IDS = {
    'time_exit_via_trail': ['time_exit_trigger_armed'],
    'time_exit_via_sl': ['time_exit_trigger_unarmed'],
    'gap_resize_and_topup': ['gap_resize', 'post_fill_topup'],
    # Added 2026-08-12 alongside the staged_test_config multi-role migration
    # -- RETL genuinely carries both roles simultaneously (drought_overlay_
    # enabled=1 + addon_enabled=1), previously untracked since one node could
    # only hold one scenario_role before.
    'drought_handoff': ['drought_entry', 'drought_handoff', 'drought_entry_placement',
                         'drought_handoff_cancel', 'drought_handoff_exit_placement',
                         'drought_handoff_alert_slot_preserved'],
    'addon': ['addon_entry_fill', 'addon_entry_placement', 'addon_exit_fill',
              'addon_exit_placement', 'addon_leg_independent_sl_fill_detection',
              'addon_leg_reconciliation', 'addon_second_ticker_buy_allowed',
              'addon_buying_power_check', 'addon_double_buy_exemption'],
}


def _last_hit_by_mode(scenario_key):
    """Returns {mode: {ticker, ts}} -- the most recent coverage_events row per
    mode for this scenario_key. SQLite's bare-column-with-MAX() extension
    (documented, reliable behavior, not ambiguous) guarantees ticker reflects
    the same row as the returned MAX(ts), not an arbitrary row sharing the
    group. Empty dict if this scenario_key has never fired via coverage_events
    (either check_mechanism='scenario_expectations', which never logs a
    coverage_events row -- see _last_hit_trade_lifecycle below -- or it
    genuinely never fired)."""
    with db._conn() as c:
        rows = c.execute(
            "SELECT mode, ticker, MAX(ts) as last_ts FROM coverage_events "
            "WHERE scenario_key=? GROUP BY mode", (scenario_key,)
        ).fetchall()
    return {r['mode']: dict(ticker=r['ticker'], ts=r['last_ts']) for r in rows}


def _last_hit_trade_lifecycle(scenario_key):
    """check_mechanism='scenario_expectations' rows (the canary_* scenarios)
    never log a coverage_events row -- their proof lives in trade_log instead,
    scoped by the real scenario_expectations.ticker/node_id. Returns the same
    shape as _last_hit_by_mode ({mode: {ticker, ts}}), mode always 'live'
    since canary nodes' scenario_expectations rows are all mode='live' (they
    exercise the real order-placement code path with dry_run=True at the
    account level, not a separate coverage_events mode)."""
    exps = [s for s in db.get_scenario_expectations() if s['scenario_key'] == scenario_key
            and s['check_method'] == 'trade_lifecycle']
    best = None
    for s in exps:
        node = db.get_watch_list_node_by_id(s.get('node_id')) if s.get('node_id') is not None else None
        disambig = dict(strategy=node.get('strategy'), version=node.get('version'),
                         window=node.get('window'), account=node.get('account')) if node else {}
        with db._conn() as c:
            q = "SELECT exit_time, ticker FROM trade_log WHERE ticker=?"
            args = [s['ticker']]
            for k, v in disambig.items():
                if v is not None:
                    q += f" AND {k}=?"
                    args.append(v)
            q += " ORDER BY exit_time DESC LIMIT 1"
            row = c.execute(q, args).fetchone()
        if row and row['exit_time'] and (best is None or row['exit_time'] > best['ts']):
            best = dict(ticker=row['ticker'], ts=row['exit_time'])
    return {'live': best} if best else {}


def _staged_coverage_for_rule(rule_id):
    """Returns a list of 'TICKER (account, role=scenario_role)' strings for
    every currently-staged watch_list node whose scenario_role maps (via
    SCENARIO_ROLE_TO_GRID_IDS) to this rule_id -- answers "what's staged to
    cover this" distinctly from "has it ever been covered". A staged node
    positioned for a rule but not yet fired shows up here even with an empty
    _last_hit_by_mode/_last_hit_trade_lifecycle result above."""
    out = []
    for role, grid_ids in SCENARIO_ROLE_TO_GRID_IDS.items():
        if rule_id not in grid_ids:
            continue
        for row in db.get_staged_test_configs():
            if row['scenario_role'] != role:
                continue
            node = db.get_watch_list_node_by_id(row['wl_id'])
            if node is None:
                continue
            out.append(f"{node['ticker']} ({node['account']}, role={role})")
    return out


def print_by_rule_report():
    """The coverage-rule-centric view: for every real logic branch in
    coverage_registry.REGISTRY, print (1) current status, (2) the last real
    hit per mode -- datetime + ticker, canary/live/paper/dry_run kept
    separate rather than collapsed into one number, and (3) any staged node
    currently positioned to hit it. Answers three distinct questions in one
    pass: did we cover everything (status), when/what last covered it
    (last-hit), and what's staged to cover it next (staged) -- deliberately
    NOT the same as run_check()'s per-ticker daily pass/fail; a rule here can
    span many tickers/nodes, and this view is all-time, not scoped to one day."""
    from scripts.coverage_registry import REGISTRY, compute_status
    db.ensure_tables()
    print("Coverage-rule report (all-time, not scoped to today)\n")
    for row in REGISTRY:
        status, status_detail = compute_status(row)
        print(f"[{row['id']}]  status={status}")
        print(f"    {status_detail}")
        if row['check_mechanism'] == 'coverage_events' and row.get('scenario_key'):
            hits = _last_hit_by_mode(row['scenario_key'])
        elif row['check_mechanism'] == 'scenario_expectations' and row.get('scenario_key'):
            hits = _last_hit_trade_lifecycle(row['scenario_key'])
        elif row['check_mechanism'] == 'open_price_quality_log':
            with db._conn() as c:
                r = c.execute("SELECT ticker, MAX(ts) as last_ts FROM open_price_quality_log").fetchone()
            hits = {'live': dict(ticker=r['ticker'], ts=r['last_ts'])} if r and r['last_ts'] else {}
        else:
            hits = {}
        if hits:
            for mode, h in sorted(hits.items()):
                print(f"    last hit ({mode}): {h['ticker']} @ {h['ts']}")
        else:
            print("    last hit: never")
        staged = _staged_coverage_for_rule(row['id'])
        if staged:
            print(f"    staged to cover: {', '.join(staged)}")
        print()


def run_check(check_date):
    """Returns a list of per-scenario result dicts (status: 'met'/'deviated'/
    'skipped', scenario_key, ticker, summary) in addition to printing/
    recording -- callers that need the live result (e.g.
    signals_notify.send_coverage_report) should use the return value rather
    than re-querying coverage_deviations afterward, since a scenario that
    deviated earlier the same day and is now met has its stale deviation row
    cleared here, not left behind as a contradiction."""
    db.ensure_tables()
    if not _is_trading_day(check_date):
        # A 'trade closed today' expectation is trivially, permanently false on
        # a day the market never opened -- checking it would just manufacture a
        # real, permanent unexplained deviation row for no reason (found live,
        # 2026-07-25 Opus review: exactly this happened via the Slack report
        # button; widened from weekend-only to the full NYSE calendar 2026-07-30
        # after a second Opus review noted a weekday market holiday has the
        # identical failure mode). Applies to both the CLI and any caller of
        # run_check.
        print(f"{check_date} is not a trading day (weekend or market holiday), nothing to check.")
        return []
    # 'informational' scenarios are checked and printed every day right
    # alongside 'daily' ones -- the difference is ticket-eligibility, not
    # visibility (2026-07-30: a scenario whose own trigger condition is
    # trade-conditional, e.g. canary_pinned_entry, shouldn't mint a ticket
    # just because the condition didn't fire today, but the user still wants
    # to see its status every day, not have it silently disappear).
    scenarios = db.get_scenario_expectations(expected_frequency=['daily', 'informational'])
    if not scenarios:
        print("No daily/informational scenario_expectations rows -- run scripts/seed_scenario_expectations.py first.")
        return []

    results = []
    print(f"Coverage check for {check_date}\n")
    for s in scenarios:
        # scenario_key alone is not a unique key -- two active rows can share
        # one scenario_key when disambiguated by node_id/mode (e.g. the same
        # designed scenario run against two different nodes on purpose), so
        # every result carries node_id/mode too -- a caller that dedups/looks
        # up by scenario_key alone would silently collapse two distinct rows
        # (found by Opus review, 2026-07-25).
        base = dict(scenario_key=s['scenario_key'], ticker=s['ticker'], node_id=s.get('node_id'), mode=s.get('mode'))
        # A scenario currently under an active coverage_snoozes row (see
        # signals_db.snooze_coverage) is treated as not-checked today, not
        # deviated -- a snooze can be scoped narrower than this expectation
        # (e.g. ticker='UDOW' vs a ticker-less global expectation), so this
        # deliberately checks for ANY active snooze on the scenario_key
        # rather than trying to match scope, matching the conservative
        # direction (skip, never silently mint a false ticket) found by
        # Opus review 2026-07-28: reconciliation_mismatch is the sole
        # DAILY_EXPECTED_IDS scenario and would otherwise mint an unexplained
        # coverage_deviations ticket every single day it's snoozed.
        # A scenario's node_id can be re-pointed to a node that didn't exist yet on
        # check_date (e.g. a repoint onto a brand-new node, or a manual --date backfill
        # run right after node creation) -- checking it anyway mints a permanent,
        # confidently-wrong deviation for a date the node had no real data at all.
        # Found 2026-08-13: the FAS/FAZ canary repoint produced exactly this shape, 16
        # coverage_deviations rows across 8 IDs, some auto-explained with a fabricated
        # "price never crossed threshold" reason. scripts/coverage_ticket_table.py's
        # timing-discrepancy check catches this generically now; this guard stops it
        # from recurring at the source.
        # added_at is stored via SQLite's datetime('now') -- UTC -- while check_date is
        # an ET trading-calendar date; comparing the raw UTC string's date substring
        # against check_date can be off by a day near midnight ET (e.g. 00:43 UTC on
        # the 13th is still the evening of the 12th ET). Converted via 'localtime' here
        # (system TZ confirmed ET), same fix pattern as the documented UTC-vs-local trap
        # in test_snooze_coverage_uses_local_time_not_utc. Found by Opus review 2026-08-13.
        node_id = s.get('node_id')
        if node_id is not None:
            with db._conn() as c:
                row = c.execute(
                    "SELECT date(added_at, 'localtime') AS d FROM watch_list WHERE id=?",
                    (node_id,)).fetchone()
            added_at = row['d'] if row and row['d'] else None
            if added_at and check_date < added_at:
                summary = f"node_id={node_id} did not exist yet on {check_date} (added {added_at})"
                print(f"  ~ {s['scenario_key']:26s} {s['ticker'] or '':6s} {summary}, skipped")
                results.append(dict(base, status='skipped', summary=summary))
                continue
        active_snoozes = db.get_active_snoozes(s['scenario_key'])
        if active_snoozes:
            snooze = active_snoozes[0]
            summary = f"snoozed until {snooze['snoozed_until']} ({snooze['reason']})"
            print(f"  ~ {s['scenario_key']:26s} {s['ticker'] or '':6s} {summary}")
            results.append(dict(base, status='skipped', summary=summary))
            continue
        checker = CHECKERS.get(s['check_method'])
        if checker is None:
            print(f"  ? {s['scenario_key']:26s} {s['ticker'] or '':6s} unknown check_method={s['check_method']!r}, skipped")
            results.append(dict(base, status='skipped', summary=f"unknown check_method={s['check_method']!r}"))
            continue
        met, actual_summary, no_activity = checker(s, check_date)
        informational = s['expected_frequency'] == 'informational'
        if met:
            db.clear_deviation_if_resolved(check_date, s['scenario_key'],
                                            ticker=s['ticker'], node_id=s.get('node_id'), mode=s.get('mode'))
            print(f"  ✓ {s['scenario_key']:26s} {s['ticker'] or '':6s} {actual_summary}")
            results.append(dict(base, status='met', summary=actual_summary))
        elif informational:
            # Never mints a coverage_deviations ticket -- the condition simply
            # not firing today is expected some days, not a failure. Still
            # printed with the same ✗ glyph so the report stays visually
            # honest about "didn't happen," just without the ticket teeth.
            print(f"  ✗ {s['scenario_key']:26s} {s['ticker'] or '':6s} {actual_summary}  (informational, no ticket)")
            results.append(dict(base, status='deviated', ticket_eligible=False, summary=actual_summary))
        else:
            dev_id = db.record_deviation(check_date, s['scenario_key'], s['expected_outcome'], actual_summary,
                                          ticker=s['ticker'], node_id=s.get('node_id'), mode=s.get('mode'))
            # Price-action auto-explain (2026-08-08): a 'no activity at all'
            # trade_lifecycle miss (no pending_buys row, no closed trade) is
            # NOT automatically a bug -- most of these are a hair-trigger
            # canary's entry threshold simply not crossing that day, real and
            # ordinary market behavior (confirmed empirically against IVV: z
            # stayed +0.8 to +2.8 for 4 straight days against a -0.1 entry
            # threshold). Only auto-explain when _entry_threshold_crossed
            # returns an explicit False -- None (data unavailable/unsupported
            # strategy) or True (it DID cross but still no trade -- a real
            # bug) both fall through to the normal unexplained ticket. A trade
            # that happened with the WRONG outcome (no_activity=False) is
            # never eligible here either -- that's a behavioral bug, not a
            # missing-signal question.
            # A row that already carries a HUMAN-authored reason (reason_by='user')
            # must never be silently overwritten by this auto-explain -- found by
            # paired Opus review 2026-08-08: record_deviation's own docstring
            # already enforces "never clobber a human reason" on re-record, but
            # explain_deviation itself has no such guard, so an unconditional call
            # here could replace real testimony (e.g. "confirmed real bug, SL
            # branch never fired") with the generic auto-verified string on a
            # later rerun for the same date. Checked fresh, not cached from
            # before record_deviation ran, since that call may have just touched
            # this exact row.
            auto_reason = None
            if no_activity:
                with db._conn() as c:
                    row = c.execute("SELECT reason_by FROM coverage_deviations WHERE id=?", (dev_id,)).fetchone()
                already_human_explained = row is not None and row['reason_by'] == 'user'
                if not already_human_explained:
                    node = db.get_watch_list_node_by_id(s.get('node_id')) if s.get('node_id') is not None else None
                    if node is not None and s.get('ticker'):
                        crossed = _entry_threshold_crossed(s['ticker'], node, check_date)
                        if crossed is False:
                            auto_reason = (f"Auto-verified: {s['ticker']}'s real cached price data never "
                                            f"crossed its entry threshold (z_score_threshold="
                                            f"{node.get('z_score_threshold')}) on {check_date} -- no real "
                                            f"signal to act on, not a code defect.")
            if auto_reason:
                db.explain_deviation(dev_id, auto_reason, reason_by='system')
                print(f"  ○ {s['scenario_key']:26s} {s['ticker'] or '':6s} {actual_summary}  "
                      f"(auto-explained: price never crossed entry threshold)")
                results.append(dict(base, status='deviated', auto_explained=True, summary=actual_summary))
            else:
                print(f"  ✗ {s['scenario_key']:26s} {s['ticker'] or '':6s} {actual_summary}")
                results.append(dict(base, status='deviated', summary=actual_summary))

    unexplained = db.get_deviations(unexplained_only=True)
    if unexplained:
        print(f"\n{len(unexplained)} UNEXPLAINED deviation(s) -- needs a reason via --explain:")
        for d in unexplained:
            print(f"  [{d['id']}] {d['check_date']} {d['scenario_key']} ({d['ticker']}): {d['actual_summary']}")
    else:
        print("\nNo unexplained deviations.")

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='YYYY-MM-DD, defaults to today')
    ap.add_argument('--explain', nargs=2, metavar=('ID', 'REASON'), default=None)
    ap.add_argument('--by-rule', action='store_true',
                     help="coverage-rule-centric view instead of the daily per-ticker check: "
                          "status + last hit (datetime/ticker/mode) + what's staged to cover it next")
    args = ap.parse_args()

    if args.explain:
        db.ensure_tables()
        dev_id, reason = args.explain
        db.explain_deviation(int(dev_id), reason)
        print(f"explained deviation {dev_id}: {reason}")
        return

    if args.by_rule:
        print_by_rule_report()
        return

    check_date = args.date or date.today().isoformat()
    run_check(check_date)


if __name__ == '__main__':
    main()
