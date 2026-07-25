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

import signals_db as db


def _check_trade_lifecycle(scenario, check_date):
    """Returns (met: bool, actual_summary: str)."""
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
            return True, f"pending_buys row present (order_placed={pending[0]['order_placed']})"
        trades = db.get_closed_trades_for_ticker_on_date(ticker, check_date, **disambig)
        if trades:
            return True, f"same-day fill instead of carryover (exit_reason={trades[0]['exit_reason']}) -- not the designed scenario but real activity occurred"
        return False, f"no pending_buys row and no closed trade found for {ticker} on {check_date}"

    expect_reasons = params.get('expect_exit_reason', [])
    trades = db.get_closed_trades_for_ticker_on_date(ticker, check_date, **disambig)
    if not trades:
        return False, f"no closed trade found for {ticker} on {check_date}"
    reason = trades[0]['exit_reason']
    if reason in expect_reasons:
        return True, f"exit_reason={reason}"
    return False, f"exit_reason={reason}, expected one of {expect_reasons}"


CHECKERS = {
    'trade_lifecycle': _check_trade_lifecycle,
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
    if datetime.strptime(check_date, '%Y-%m-%d').weekday() >= 5:
        # A 'trade closed today' expectation is trivially, permanently false on
        # a day the market never opened -- checking it would just manufacture a
        # real, permanent unexplained deviation row for no reason (found live,
        # 2026-07-25 Opus review: exactly this happened via the Slack report
        # button). Applies to both the CLI and any caller of run_check.
        print(f"{check_date} is a weekend, no trading day to check.")
        return []
    scenarios = db.get_scenario_expectations(expected_frequency='daily')
    if not scenarios:
        print("No daily scenario_expectations rows -- run scripts/seed_scenario_expectations.py first.")
        return []

    results = []
    print(f"Coverage check for {check_date}\n")
    for s in scenarios:
        checker = CHECKERS.get(s['check_method'])
        if checker is None:
            print(f"  ? {s['scenario_key']:26s} {s['ticker'] or '':6s} unknown check_method={s['check_method']!r}, skipped")
            results.append(dict(status='skipped', scenario_key=s['scenario_key'], ticker=s['ticker'],
                                 summary=f"unknown check_method={s['check_method']!r}"))
            continue
        met, actual_summary = checker(s, check_date)
        if met:
            db.clear_deviation_if_resolved(check_date, s['scenario_key'],
                                            ticker=s['ticker'], node_id=s.get('node_id'), mode=s.get('mode'))
            print(f"  ✓ {s['scenario_key']:26s} {s['ticker'] or '':6s} {actual_summary}")
            results.append(dict(status='met', scenario_key=s['scenario_key'], ticker=s['ticker'], summary=actual_summary))
        else:
            db.record_deviation(check_date, s['scenario_key'], s['expected_outcome'], actual_summary,
                                 ticker=s['ticker'], node_id=s.get('node_id'), mode=s.get('mode'))
            print(f"  ✗ {s['scenario_key']:26s} {s['ticker'] or '':6s} {actual_summary}")
            results.append(dict(status='deviated', scenario_key=s['scenario_key'], ticker=s['ticker'], summary=actual_summary))

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
