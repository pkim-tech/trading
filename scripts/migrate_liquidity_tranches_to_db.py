"""One-time migration: seeds db_cache.sweep_tranches from the real history of
scripts/liquidity_tranches.txt as it stood through 2026-08-12's session --
original tranche membership, the K-1/concentration/diversification
disqualifications applied that night (with their real reasons), and the new
tranche 15 (GDXU/NUGT/SOXS) added at the tail. Run once; re-running is safe
(add_tranche_ticker/remove_tranche_ticker are both idempotent upserts), but
there's no reason to run it again once scripts/render_liquidity_tranches.py
is the normal way to regenerate the .txt file going forward.

Usage: .venv/bin/python scripts/migrate_liquidity_tranches_to_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db_cache

CAMPAIGN = 'liquidity_screen'

# Original tranche membership as it stood before tonight's K-1 policy pass.
ORIGINAL_TRANCHES = {
    1: ['SQQQ'],
    2: ['TZA', 'SPXL', 'TNA', 'SPXU', 'SPXS'],
    3: ['TECL', 'ETHU'],
    4: ['JNUG', 'BULZ', 'DFEN', 'OILU', 'BOIL'],
    5: ['FNGU', 'WEBL', 'ERX', 'GUSH', 'YINN'],
    6: ['SOLT', 'ROM', 'SSG', 'MQQQ', 'TECS'],
    7: ['UGL', 'SRTY', 'TMV', 'FAZ', 'LABD'],
    8: ['SDOW', 'CWEB', 'UVXY', 'TWM', 'TBT'],
    9: ['DXD'],
    10: ['KOLD', 'QPUX', 'URTY', 'FAS', 'CURE'],
    11: ['SPCL', 'DRN', 'DDM', 'UWM', 'JDST'],
    12: ['EDC'],
    13: ['FNGD', 'ERY', 'TMF', 'GLL'],
    14: ['BTCZ', 'BITU', 'BITX'],
}

# ticker -> (tranche_num, removal reason), applied 2026-08-12
DISQUALIFICATIONS = {
    'BULZ': (4, 'concentration -- 15 holdings, below the 20-security minimum'),
    'FNGU': (5, 'concentration -- 10 holdings, below the 20-security minimum'),
    'TMV': (7, 'single-bond, non-K-1 -- diversification rule stands (not covered by the K-1 generalization)'),
    'UVXY': (8, 'K-1/brokerage-eligible but weak CAGR -- not worth the sweep time'),
    'TBT': (8, 'single-bond, non-K-1 -- diversification rule stands (not covered by the K-1 generalization)'),
    'QPUX': (10, 'concentration -- 4 actively-held holdings, extremely concentrated'),
    'SPCL': (11, 'concentration -- 10 holdings, single-stock SpaceX contamination + mandate-change history'),
    'FNGD': (13, 'concentration -- 10 holdings, below the 20-security minimum'),
    'TMF': (13, 'single-bond, non-K-1 -- diversification rule stands (not covered by the K-1 generalization)'),
    'GLL': (13, 'K-1/brokerage-eligible but weak CAGR -- not worth the sweep time'),
}

# New tranche 15, added 2026-08-12 -- real watchlist tickers still missing
# v5.1 that weren't otherwise anywhere in this campaign (DFEN already in
# tranche 4).
NEW_TRANCHE_15 = ['GDXU', 'NUGT', 'SOXS']


def main():
    db_cache.set_sweep_campaign_config(
        CAMPAIGN, version='v5.1', fixed_sls='1 2 3',
        strategies='TrailingBothZScoreBreakout TrailingExitZScoreBreakout',
        entry_timing='open_check')

    for tranche_num, tickers in ORIGINAL_TRANCHES.items():
        for ticker in tickers:
            db_cache.add_tranche_ticker(CAMPAIGN, tranche_num, ticker)

    for ticker, (tranche_num, reason) in DISQUALIFICATIONS.items():
        db_cache.remove_tranche_ticker(CAMPAIGN, tranche_num, ticker, reason)

    for ticker in NEW_TRANCHE_15:
        db_cache.add_tranche_ticker(CAMPAIGN, 15, ticker,
                                     reason='real watchlist ticker missing v5.1, no other slot in this campaign')

    tranches = db_cache.get_tranches(CAMPAIGN)
    print(f"Seeded {sum(len(v) for v in tranches.values())} active tickers across {len(tranches)} tranches.")
    for n in sorted(tranches):
        print(f"  {n}: {' '.join(tranches[n])}")


if __name__ == '__main__':
    main()
