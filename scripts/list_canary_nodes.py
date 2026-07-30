"""Lists every real watch_list node with version='canary', across all
watchlists -- so "how many canaries are there" is answered by query, not
recalled from memory.

Includes a 'pair' / 'testing' column: the original 6 canaries (IVV, QQQ, IWM,
DIA, VOO, XLF) were each deliberately paired with an inverse-exposure
counterpart added 2026-07-29 (FAZ/SPXU/TWM/QID/SDOW), plus a standalone
long/short pair on gold miners (JNUG/JDST) -- so regardless of which
direction the market moves on a given day, at least one side of each pair
should get real signal activity. This mapping is deliberate design intent,
not derived from data, so it's a hand-maintained table here (like
seed_scenario_expectations.py's SCENARIOS) -- update it if the pairing ever
changes.

Usage: .venv/bin/python scripts/list_canary_nodes.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db

# ticker -> (pairs_with, what_its_testing). The 5 A-F letters are
# docs/deep_backlog.md's 2026-07-23 original design -- each new inverse
# ticker was fixed 2026-07-29 (scripts/mirror_canary_pair_config.py) to carry
# the EXACT same hair-trigger config as its counterpart, so both sides of
# each pair exercise the same mechanism regardless of market direction.
PAIRS = {
    'IVV':  ('SPXU', 'A: full happy path (entry->bounce-fill->arm->trail-sell)'),
    'VOO':  ('SPXU', 'E: TrailingExit immediate market-buy path (2nd proxy for A)'),
    'SPXU': ('IVV/VOO', 'A mirrored: full happy path, inverse side'),
    'QQQ':  ('QID', 'B: early-SL path'),
    'QID':  ('QQQ', 'B mirrored: early-SL path, inverse side'),
    'IWM':  ('TWM', 'C: pinned/open_check entry mechanism'),
    'TWM':  ('IWM', 'C mirrored: pinned/open_check entry, inverse side'),
    'DIA':  ('SDOW', 'D: overnight carry (wide trail_buy_pct, unlikely same-day fill)'),
    'SDOW': ('DIA', 'D mirrored: overnight carry, inverse side'),
    'XLF':  ('FAZ', 'F: TIME-only exit (arm+SL both unreachable)'),
    'FAZ':  ('XLF', 'F mirrored: TIME-only exit, inverse side'),
    'JNUG': ('VOO', 'E mirrored: TrailingExit immediate market-buy path (2026-07-29 fix -- '
                     'was the missing 6th A-F mirror, converted from generic TrailingBoth)'),
    'JDST': ('?', 'no longer paired -- JNUG became the E-mirror instead of JDST\'s original '
                  'gold-miner-inverse role; purpose still open'),
}

with db._conn() as c:
    rows = [dict(r) for r in c.execute(
        "SELECT id, ticker, account, mode, watchlist_id, strategy FROM watch_list "
        "WHERE version='canary' ORDER BY ticker"
    ).fetchall()]

for r in rows:
    pairs_with, testing = PAIRS.get(r['ticker'], ('?', 'not in PAIRS mapping -- update this script'))
    print(f"  {r['ticker']:6s} wl_id={r['id']:4d}  account={r['account']:10s} mode={r['mode']:8s} "
          f"watchlist_id={r['watchlist_id']}  strategy={r['strategy']:28s} "
          f"pairs_with={pairs_with:8s} testing={testing}")
print(f"\n{len(rows)} canary node(s) total.")

unmapped = [r['ticker'] for r in rows if r['ticker'] not in PAIRS]
if unmapped:
    print(f"[warn] {len(unmapped)} canary ticker(s) not in PAIRS: {unmapped} -- add them to this script")
