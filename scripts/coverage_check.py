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

    Restricted to target_hours (9,10,11,12,13,14 anchors) matching CLAUDE.md's real
    live signal-window scope -- the 15:30 bar is never checked live either.

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

    bars = df[(df.index.normalize() == day) & (df.index.hour.isin(range(9, 15)))]
    if bars.empty:
        return None
    check_open_too = node.get('entry_timing') == 'open_check'
    for _, bar in bars.iterrows():
        prices = [bar["Close"]] + ([bar["Open"]] if check_open_too and "Open" in bar else [])
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
    disambig = dict(strategy=node.get('strategy'), version=node.get('version'),
                     window=node.get('window'), account=node.get('account')) if node else {}

    if params.get('expect_pending_carryover'):
        pending = db.get_pending_buys_for_ticker_on_date(ticker, check_date, **disambig)
        if pending:
            return True, f"pending_buys row present (order_placed={pending[0]['order_placed']})", False
        trades = db.get_closed_trades_for_ticker_on_date(ticker, check_date, **disambig)
        if trades:
            return True, f"same-day fill instead of carryover (exit_reason={trades[0]['exit_reason']}) -- not the designed scenario but real activity occurred", False
        return False, f"no pending_buys row and no closed trade found for {ticker} on {check_date}", True

    expect_reasons = params.get('expect_exit_reason', [])
    trades = db.get_closed_trades_for_ticker_on_date(ticker, check_date, **disambig)
    if not trades:
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
    args = ap.parse_args()

    if args.explain:
        db.ensure_tables()
        dev_id, reason = args.explain
        db.explain_deviation(int(dev_id), reason)
        print(f"explained deviation {dev_id}: {reason}")
        return

    check_date = args.date or date.today().isoformat()
    run_check(check_date)


if __name__ == '__main__':
    main()
