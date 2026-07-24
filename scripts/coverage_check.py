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
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db


def _check_trade_lifecycle(scenario, check_date):
    """Returns (met: bool, actual_summary: str)."""
    params = json.loads(scenario['check_params'] or '{}')
    ticker = scenario['ticker']

    if params.get('expect_pending_carryover'):
        pending = db.get_pending_buys_for_ticker_on_date(ticker, check_date)
        if pending:
            return True, f"pending_buys row present (order_placed={pending[0]['order_placed']})"
        trades = db.get_closed_trades_for_ticker_on_date(ticker, check_date)
        if trades:
            return True, f"same-day fill instead of carryover (exit_reason={trades[0]['exit_reason']}) -- not the designed scenario but real activity occurred"
        return False, f"no pending_buys row and no closed trade found for {ticker} on {check_date}"

    expect_reasons = params.get('expect_exit_reason', [])
    trades = db.get_closed_trades_for_ticker_on_date(ticker, check_date)
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
    db.ensure_tables()
    scenarios = db.get_scenario_expectations(expected_frequency='daily')
    if not scenarios:
        print("No daily scenario_expectations rows -- run scripts/seed_scenario_expectations.py first.")
        return

    print(f"Coverage check for {check_date}\n")
    for s in scenarios:
        checker = CHECKERS.get(s['check_method'])
        if checker is None:
            print(f"  ? {s['scenario_key']:26s} {s['ticker'] or '':6s} unknown check_method={s['check_method']!r}, skipped")
            continue
        met, actual_summary = checker(s, check_date)
        if met:
            print(f"  ✓ {s['scenario_key']:26s} {s['ticker'] or '':6s} {actual_summary}")
        else:
            db.record_deviation(check_date, s['scenario_key'], s['expected_outcome'], actual_summary, ticker=s['ticker'])
            print(f"  ✗ {s['scenario_key']:26s} {s['ticker'] or '':6s} {actual_summary}")

    unexplained = db.get_deviations(unexplained_only=True)
    if unexplained:
        print(f"\n{len(unexplained)} UNEXPLAINED deviation(s) -- needs a reason via --explain:")
        for d in unexplained:
            print(f"  [{d['id']}] {d['check_date']} {d['scenario_key']} ({d['ticker']}): {d['actual_summary']}")
    else:
        print("\nNo unexplained deviations.")


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
