"""
Backfills the `phase` column for v4 backtest_cache rows written before phase
tagging was added mid-session (2026-07-15) — SOXL/KORU stop_loss=3 (both
entry_timings) and the partial SOXL stop_loss=6/close campaign.

Deterministically replays phase1 -> phase2 -> phase2.5 -> phase3 grid
generation in historical order (same logic as run_optimization_sweep.py),
tagging each row with whichever phase's task set first covers it and is
still untagged. No backtests are recomputed — only the `phase` column is
written.

Usage: .venv/bin/python scripts/backfill_v4_phase.py
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd
from run_optimization_sweep import pick_island_centers, FINE_RADIUS, CLIFF_RADIUS, ROBUST_ALPHA_SQL

DB_PATH = Path(__file__).resolve().parent.parent / "cache" / "research" / "trading_universe.db"
STRATEGY = "TrailingBothZScoreBreakout"
VERSION = "v4"

Z_THRESHOLDS = [1.0, 1.5, 2.0]
WINDOWS = [10, 20]
TAKE_PROFITS = [1, 2, 3, 4, 5, 6, 9, 12, 15, 18, 21, 24, 27, 30]
STOP_LOSSES = TAKE_PROFITS  # raw grid values -> trail_buy_pct axis for this strategy
HOLD_TIME_CAPS = list(range(7, 147, 7))
TRAIL_PCTS = [1, 2, 3, 4, 5, 6, 7]  # -> trail_sell_pct axis

CAMPAIGNS = [
    ("SOXL", 3, "close"),
    ("SOXL", 3, "open_check"),
    ("KORU", 3, "close"),
    ("KORU", 3, "open_check"),
    ("SOXL", 6, "close"),
]


def phase1_set():
    return {
        (tp, sl, hold, w, z, tpct)
        for z in Z_THRESHOLDS
        for w in WINDOWS
        for tp in TAKE_PROFITS
        for sl in STOP_LOSSES
        for hold in HOLD_TIME_CAPS
        for tpct in TRAIL_PCTS
    }


def backfill_campaign(conn, ticker, stop_loss, entry_timing):
    cur = conn.cursor()
    scope = (ticker, VERSION, STRATEGY, stop_loss, entry_timing)

    total_null = cur.execute(
        "SELECT COUNT(*) FROM backtest_cache WHERE ticker=? AND version=? AND strategy=? "
        "AND stop_loss=? AND entry_timing=? AND phase IS NULL", scope).fetchone()[0]
    if total_null == 0:
        print(f"[{ticker} sl={stop_loss} {entry_timing}] nothing to backfill (already tagged)")
        return

    print(f"[{ticker} sl={stop_loss} {entry_timing}] {total_null} untagged rows")

    # Phase 1
    p1 = phase1_set()
    cur.executemany(
        """UPDATE backtest_cache SET phase='Phase1-Coarse'
           WHERE ticker=? AND version=? AND strategy=? AND stop_loss=? AND entry_timing=?
             AND axis_tp=? AND trail_buy_pct=? AND max_hold_hours=? AND window=?
             AND z_score_threshold=? AND trail_sell_pct=? AND phase IS NULL""",
        [(ticker, VERSION, STRATEGY, stop_loss, entry_timing, tp, sl, hold, w, z, tpct)
         for (tp, sl, hold, w, z, tpct) in p1]
    )
    conn.commit()
    n1 = cur.execute(
        "SELECT COUNT(*) FROM backtest_cache WHERE ticker=? AND version=? AND strategy=? "
        "AND stop_loss=? AND entry_timing=? AND phase='Phase1-Coarse'", scope).fetchone()[0]
    print(f"  Phase1-Coarse: {n1} tagged")

    # Phase 2 — island centers computed from phase1-only data, per (z, w, tpct)
    p2 = set()
    for z in Z_THRESHOLDS:
        for w in WINDOWS:
            for tpct in TRAIL_PCTS:
                df_wz = pd.read_sql(f"""
                    SELECT axis_tp AS take_profit, trail_buy_pct AS stop_loss, max_hold_hours,
                           alpha_vs_spy, {ROBUST_ALPHA_SQL} AS robust_alpha
                    FROM backtest_cache
                    WHERE ticker=? AND version=? AND strategy=? AND stop_loss=? AND entry_timing=?
                      AND phase='Phase1-Coarse' AND z_score_threshold=? AND window=?
                      AND trail_sell_pct=? AND trades > 0
                """, conn, params=[*scope, z, w, tpct])
                if df_wz.empty:
                    continue
                centers = pick_island_centers(df_wz)
                for (tp_c, sl_c) in centers:
                    for tp in range(max(1, tp_c - FINE_RADIUS), min(30, tp_c + FINE_RADIUS) + 1):
                        for sl in range(max(1, sl_c - FINE_RADIUS), min(30, sl_c + FINE_RADIUS) + 1):
                            for hold in HOLD_TIME_CAPS:
                                p2.add((tp, sl, hold, w, z, tpct))
    if p2:
        cur.executemany(
            """UPDATE backtest_cache SET phase='Phase2-Island'
               WHERE ticker=? AND version=? AND strategy=? AND stop_loss=? AND entry_timing=?
                 AND axis_tp=? AND trail_buy_pct=? AND max_hold_hours=? AND window=?
                 AND z_score_threshold=? AND trail_sell_pct=? AND phase IS NULL""",
            [(ticker, VERSION, STRATEGY, stop_loss, entry_timing, tp, sl, hold, w, z, tpct)
             for (tp, sl, hold, w, z, tpct) in p2]
        )
        conn.commit()
    n2 = cur.execute(
        "SELECT COUNT(*) FROM backtest_cache WHERE ticker=? AND version=? AND strategy=? "
        "AND stop_loss=? AND entry_timing=? AND phase='Phase2-Island'", scope).fetchone()[0]
    print(f"  Phase2-Island: {n2} tagged")

    # Phase 2.5 — cliff box around the single best node among phase1+phase2 data
    row = cur.execute(f"""
        SELECT axis_tp, trail_buy_pct, max_hold_hours, window, z_score_threshold, trail_sell_pct
        FROM backtest_cache
        WHERE ticker=? AND version=? AND strategy=? AND stop_loss=? AND entry_timing=?
          AND phase IN ('Phase1-Coarse','Phase2-Island') AND trades > 0
        ORDER BY {ROBUST_ALPHA_SQL} DESC LIMIT 1
    """, scope).fetchone()

    p25 = set()
    if row:
        tp_c, sl_c, hold_c, w_c, z_c, tpct_c = int(row[0]), int(row[1]), int(row[2]), int(row[3]), float(row[4]), float(row[5])
        idx = TRAIL_PCTS.index(tpct_c) if tpct_c in TRAIL_PCTS else None
        tpct_neighbors = TRAIL_PCTS[max(0, idx - 1): idx + 2] if idx is not None else [tpct_c]
        for tp in range(max(1, tp_c - CLIFF_RADIUS), min(30, tp_c + CLIFF_RADIUS) + 1):
            for sl in range(max(1, sl_c - CLIFF_RADIUS), min(30, sl_c + CLIFF_RADIUS) + 1):
                for hold in [h for h in HOLD_TIME_CAPS if abs(h - hold_c) <= 7]:
                    for tpct in tpct_neighbors:
                        p25.add((tp, sl, hold, w_c, z_c, tpct))
    if p25:
        cur.executemany(
            """UPDATE backtest_cache SET phase='Phase2.5-CliffBox'
               WHERE ticker=? AND version=? AND strategy=? AND stop_loss=? AND entry_timing=?
                 AND axis_tp=? AND trail_buy_pct=? AND max_hold_hours=? AND window=?
                 AND z_score_threshold=? AND trail_sell_pct=? AND phase IS NULL""",
            [(ticker, VERSION, STRATEGY, stop_loss, entry_timing, tp, sl, hold, w, z, tpct)
             for (tp, sl, hold, w, z, tpct) in p25]
        )
        conn.commit()
    n25 = cur.execute(
        "SELECT COUNT(*) FROM backtest_cache WHERE ticker=? AND version=? AND strategy=? "
        "AND stop_loss=? AND entry_timing=? AND phase='Phase2.5-CliffBox'", scope).fetchone()[0]
    print(f"  Phase2.5-CliffBox: {n25} tagged")

    # Phase 3 — whatever's left over (full 1-30x1-30 mesh superset of everything above)
    remaining = cur.execute(
        "SELECT COUNT(*) FROM backtest_cache WHERE ticker=? AND version=? AND strategy=? "
        "AND stop_loss=? AND entry_timing=? AND phase IS NULL", scope).fetchone()[0]
    if remaining:
        cur.execute(
            "UPDATE backtest_cache SET phase='Phase3-Full' WHERE ticker=? AND version=? "
            "AND strategy=? AND stop_loss=? AND entry_timing=? AND phase IS NULL", scope)
        conn.commit()
    print(f"  Phase3-Full: {remaining} tagged")

    still_null = cur.execute(
        "SELECT COUNT(*) FROM backtest_cache WHERE ticker=? AND version=? AND strategy=? "
        "AND stop_loss=? AND entry_timing=? AND phase IS NULL", scope).fetchone()[0]
    print(f"  [{'OK' if still_null == 0 else 'GAP'}] remaining untagged: {still_null}")


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    for ticker, stop_loss, entry_timing in CAMPAIGNS:
        backfill_campaign(conn, ticker, stop_loss, entry_timing)
    conn.close()
