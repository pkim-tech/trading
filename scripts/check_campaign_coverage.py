#!/usr/bin/env python
"""Verify a sweep campaign's queued (ticker, strategy) jobs actually cover the
real capital-at-stake live tickers, both strategies each, by default.

Built after a real gap (2026-09-01): campaign_id=1 ("v6.5") was assembled from
several separate `TICKERS=...` launches and silently ended up missing DFEN
entirely and DPST's TrailingExit job (DPST's own real live strategy) -- nobody
checked coverage until asked directly. Same shape as scripts/check_cron_drift.py
(documented/intended state vs. real state), applied to campaign_jobs instead of
crontab.

Usage:
    .venv/bin/python scripts/check_campaign_coverage.py --campaign-id 1
    .venv/bin/python scripts/check_campaign_coverage.py --campaign-id 1 --tickers AGQ UGL
"""
import argparse
import sqlite3
import sys

sys.path.insert(0, ".")
import signals_config as cfg

RESEARCH_DB = "cache/research/trading_universe.db"
LIVE_DB = "cache/live/trading_live.db"
DEFAULT_STRATEGIES = ("TrailingBothZScoreBreakout", "TrailingExitZScoreBreakout")


def real_live_tickers():
    """Real capital-at-stake live tickers -- state='live', not archived, sized at
    or above CAPITAL_AT_STAKE_THRESHOLD (matches signals_helpers.has_capital_at_stake's
    own gate, so this doesn't drift from the live daemon's own definition of 'real')."""
    conn = sqlite3.connect(LIVE_DB)
    cur = conn.execute(
        "SELECT DISTINCT ticker FROM watch_list WHERE state='live' AND archived_at IS NULL "
        "AND starting_notional >= ? ORDER BY ticker",
        (cfg.CAPITAL_AT_STAKE_THRESHOLD,),
    )
    tickers = [r[0] for r in cur.fetchall()]
    conn.close()
    return tickers


def queued_pairs(campaign_id):
    conn = sqlite3.connect(RESEARCH_DB)
    cur = conn.execute(
        "SELECT ticker, strategy FROM campaign_jobs WHERE campaign_id=?", (campaign_id,)
    )
    pairs = set(cur.fetchall())
    conn.close()
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign-id", type=int, required=True)
    ap.add_argument("--tickers", nargs="+", default=None,
                     help="override the expected ticker list (default: real capital-at-stake live tickers)")
    ap.add_argument("--strategies", nargs="+", default=list(DEFAULT_STRATEGIES))
    args = ap.parse_args()

    expected_tickers = args.tickers or real_live_tickers()
    expected = {(t, s) for t in expected_tickers for s in args.strategies}
    actual = queued_pairs(args.campaign_id)

    missing = sorted(expected - actual)
    extra = sorted(actual - expected)

    print(f"Expected {len(expected_tickers)} tickers x {len(args.strategies)} strategies "
          f"= {len(expected)} jobs; campaign_id={args.campaign_id} has {len(actual)} queued.")

    if not missing and not extra:
        print("OK: full coverage, no gaps.")
        return 0

    if missing:
        print(f"MISSING ({len(missing)}):")
        for t, s in missing:
            print(f"  - {t} / {s}")
    if extra:
        print(f"UNEXPECTED, not in the expected ticker/strategy set ({len(extra)}):")
        for t, s in extra:
            print(f"  + {t} / {s}")
    return 1


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    sys.exit(main())
