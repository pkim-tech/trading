"""Addendum to setup_2026_07_24_soxl_ira_live_test.py -- adds LABU as a
parallel backup to node 106 (LABD) for the real market-buy path test.
Mirrors LABD's node config exactly (same strategy/window/entry_timing/mode/
account), so whichever of the two actually triggers at the 9:30:02 pinned
check (or the 9:31-9:40 ambient fallback) exercises
_attempt_automated_market_buy. add_node uses INSERT OR IGNORE, so a rerun
is a harmless no-op.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_config as cfg
import signals_db as db

WATCHLIST_ID = 65
VERSION = "soxl_test"


def _set_account(ticker, version, window, account):
    with sqlite3.connect(cfg.DB_PATH) as conn:
        conn.execute(
            "UPDATE watch_list SET account=? WHERE ticker=? AND version=? AND window=?",
            (account, ticker, version, window),
        )
        conn.commit()


db.add_node(
    ticker="LABU", strategy="TrailingExitZScoreBreakout", version=VERSION, window=20,
    take_profit=10, stop_loss=0, max_hold_hours=24,
    label="soxl_ira live-buy test (market-buy path, LABD backup)",
    z_score_threshold=1.5, watchlist_id=WATCHLIST_ID, mode="live",
    trail_pct=0.5, entry_timing="open_check", starting_notional=22,
    fixed_sl_override=0.3,
)
_set_account("LABU", VERSION, 20, "soxl_ira")

rows = db.get_watchlist()
for r in rows:
    if r["ticker"] == "LABU" and r["version"] == VERSION and r["window"] == 20:
        print("Node created:", dict(r))
        break
else:
    raise RuntimeError("LABU node not found after insert")
