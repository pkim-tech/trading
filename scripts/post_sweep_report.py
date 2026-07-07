"""
Run after the v2.x backfill sweep (scripts/run_v2_backfill_sweep.sh, no-arg full run)
finishes. Reports on every node in watch_list: live-parity check against today's params,
plus the best-alpha v2.x replacement candidate (same strategy) with its own parity check.

Usage: .venv/bin/python scripts/post_sweep_report.py
"""
import sys
import sqlite3
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.verify_live_parity import compare, kernel_trades

DB_PATH = Path(__file__).resolve().parent.parent / "cache" / "trading_universe.db"
LIVE_DB_PATH = Path(__file__).resolve().parent.parent / "cache" / "trading_live.db"
OUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "post_sweep_report.md"

STRATEGY_TO_V2 = {
    'TrendFilteredZScore': 'v2.4',
    'ZScoreBreakout': 'v2.5',
    'LimitOrderZScoreBreakout': 'v2.7',
    'TrailingExitZScoreBreakout': 'v2.8',
    'TrailingBuyZScoreBreakout': 'v2.9',
    'TrailingBothZScoreBreakout': 'v2.10',
}


def get_watch_list():
    with sqlite3.connect(LIVE_DB_PATH) as conn:
        return conn.execute("""
            SELECT ticker, strategy, version, window, z_score_threshold,
                   take_profit, stop_loss, max_hold_hours
            FROM watch_list
        """).fetchall()


def best_v2_node(conn, ticker, v2_version, min_trades=15):
    row = conn.execute("""
        SELECT window, z_score_threshold, take_profit, stop_loss, max_hold_hours,
               trades, win_rate, strategy_return, alpha_vs_spy
        FROM backtest_cache
        WHERE ticker=? AND version=? AND trades>=?
        ORDER BY alpha_vs_spy DESC LIMIT 1
    """, (ticker, v2_version, min_trades)).fetchone()
    return row


def node_summary(kt):
    closed = [t for t in kt if t['Result'] in ('WIN', 'LOSS', 'TWIN', 'TLOSS')]
    wins = sum(1 for t in closed if t['Result'] in ('WIN', 'TWIN'))
    ret = 1.0
    for t in closed:
        ret *= (1 + t['Return'])
    n = len(closed)
    return n, (100 * wins / n if n else 0.0), 100 * (ret - 1)


def main():
    print(f"[{datetime.now()}] Generating report...")
    watch_list = get_watch_list()
    lines = [f"# Post-Sweep Report — generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]

    with sqlite3.connect(DB_PATH) as conn:
        for ticker, strategy, version, window, z, tp, sl, hold in watch_list:
            lines.append(f"## {ticker} — {strategy} (current: {version} w={window} z={z} "
                          f"tp={tp} sl={sl} hold={hold})")

            # Current node: live-parity + fresh kernel numbers
            try:
                kt = kernel_trades(ticker, strategy, window, z, tp, sl, hold)
                n, wr, ret = node_summary(kt)
                match = compare(ticker, strategy, window, z, tp, sl, hold)
                lines.append(f"- Current node (fixed kernel): trades={n} win_rate={wr:.1f}% "
                              f"return={ret:.1f}% | parity: {'MATCH' if match else 'MISMATCH'}")
            except Exception as e:
                lines.append(f"- Current node: ERROR ({e})")

            # Best v2.x replacement candidate, same strategy
            v2_version = STRATEGY_TO_V2.get(strategy)
            if v2_version:
                best = best_v2_node(conn, ticker, v2_version)
                if best:
                    bw, bz, btp, bsl, bhold, btrades, bwr, bret, balpha = best
                    lines.append(f"- Best {v2_version} candidate: w={bw} z={bz} tp={btp} sl={bsl} "
                                  f"hold={bhold} | trades={btrades} win_rate={bwr:.1f}% "
                                  f"return={bret:.1f}% alpha={balpha:.1f}%")
                    try:
                        cand_match = compare(ticker, strategy, int(bw), float(bz), int(btp),
                                             int(bsl), int(bhold))
                        lines.append(f"  parity: {'MATCH' if cand_match else 'MISMATCH'}")
                    except Exception as e:
                        lines.append(f"  parity: ERROR ({e})")
                else:
                    lines.append(f"- No {v2_version} candidate found (trades>=15 filter, or not yet swept)")
            lines.append("")

    OUT_PATH.write_text("\n".join(lines))
    print(f"[{datetime.now()}] Report written to {OUT_PATH}")


if __name__ == "__main__":
    main()
