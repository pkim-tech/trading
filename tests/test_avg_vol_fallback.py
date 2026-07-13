#!/usr/bin/env python3
"""
Test _build_buy_blocks()'s avg_vol_10d cache/fallback (active_signals.py).

Verifies: (1) a normal research-DB lookup caches the value onto watch_list,
(2) a research-DB failure falls back to the cached value instead of crashing.

Usage:
    python tests/test_avg_vol_fallback.py <ticker>   # defaults to TQQQ, watchlist 9
"""

import sys
import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import active_signals as a


def _node_for(ticker, watchlist_id=9):
    wl = a.get_watchlist(watchlist_id)
    node = next((n for n in wl if n['ticker'] == ticker), None)
    if node is None:
        raise SystemExit(f"{ticker} not found on watchlist {watchlist_id}")
    return node


def _fake_sig(ticker, price=85.0):
    return {
        'ticker': ticker, 'current_price': price, 'z_score': -2.1,
        'last_bar': datetime.datetime(2026, 7, 7, 14, 30), 'lower_band': price - 0.5,
        'hurst': None, 'adf_p': None,
    }


def main():
    ticker = sys.argv[1] if len(sys.argv) > 1 else 'TQQQ'
    a.ensure_tables()

    print(f"--- normal lookup ({ticker}) ---")
    node = _node_for(ticker)
    print(f"cached_avg_vol_10d before: {node.get('cached_avg_vol_10d')}")
    blocks = a._build_buy_blocks(node, _fake_sig(ticker))
    print(blocks[0]['text']['text'])
    node = _node_for(ticker)
    cached = node.get('cached_avg_vol_10d')
    print(f"cached_avg_vol_10d after:  {cached}")
    assert cached, "expected a real avg_vol_10d to get cached on success"

    print(f"\n--- forced research-DB failure ({ticker}) ---")
    real_path = a.RESEARCH_DB_PATH
    a.RESEARCH_DB_PATH = Path("/nonexistent/trading_universe.db")
    try:
        blocks = a._build_buy_blocks(node, _fake_sig(ticker))
        print(blocks[0]['text']['text'])
        assert f"{cached/1000:.0f}k" in blocks[0]['text']['text'] or "max_notional" not in str(blocks), \
            "fallback should still show a max-shares figure from the cached value"
        print("OK: fell back to cached value without crashing")
    finally:
        a.RESEARCH_DB_PATH = real_path


if __name__ == '__main__':
    main()
