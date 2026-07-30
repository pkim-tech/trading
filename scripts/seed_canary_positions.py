"""One-off: directly seed dry_run_sim open_positions rows for canary nodes so
their designed exit lifecycle (SL/TIME/TRAIL/TP) can be tested without waiting
for a real z-score entry signal. Mirrors what a real dry-run bounce-fill would
have written (see signals_notify._fill_dry_run_buy), just skipping the
pending_buys/bounce-fill step and entering directly at current price.

Usage: .venv/bin/python scripts/seed_canary_positions.py [wl_id ...]
Defaults to the canary tickers that still need a real lifecycle test: IVV,
IWM, DIA, VOO, XLF (137, 139, 140, 141, 142) -- QQQ (138) already completed
its designed SL lifecycle naturally on 2026-07-26 and doesn't need reseeding.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db
from signals_compute import _load_cache

DEFAULT_WL_IDS = [137, 139, 140, 141, 142]


def seed(wl_id):
    node = db.get_watch_list_node_by_id(wl_id)
    if node is None:
        print(f"  [skip] wl_id={wl_id} not found")
        return
    ticker = node['ticker']
    df, _ = _load_cache(ticker)
    if df is None or df.empty:
        print(f"  [skip] {ticker} — no cached price data")
        return
    price = float(df['Close'].dropna().iloc[-1])
    now = datetime.now()
    opened = db.open_position(node, signal_price=price, signal_time=now,
                               entry_price=price, entry_time=now,
                               shares=1, is_dry_run_sim=True)
    if opened:
        print(f"  seeded {ticker} (wl_id={wl_id}) @ ${price:.4f}")
    else:
        print(f"  [skip] {ticker} (wl_id={wl_id}) already has an open position")


if __name__ == '__main__':
    wl_ids = [int(x) for x in sys.argv[1:]] or DEFAULT_WL_IDS
    for wl_id in wl_ids:
        seed(wl_id)
