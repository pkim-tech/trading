"""
First-pass liquidity screen for the 2026-08-07 backlog finding: the 18-ticker
v4/v5 watchlist was never actually liquidity-limited -- hundreds of 2x/3x
leveraged tickers with real, tradable liquidity were never even considered.

Ranks every leveraged ticker NOT already backtested (v4/v5) by 1% ADV
notional (avg_vol_10d * last_price * 0.01, same formula as
run_optimization_sweep.py / pages/11_Universe_Scan.py / liquidity_notional_yearago.py),
so validation effort (backtest sweeps) goes to the most liquid untested
candidates first, per the user's explicit framing: liquidity is the
first-pass filter on this pool, not an afterthought.

Flags candidates whose index_underlier suggests a commodity-futures index
(oil/gas/crude/commodity) as K-1/UBTI caution -- same category as the
already-confirmed USO/AGQ K-1 exposure -- for manual confirmation before
considering for any IRA/Roth account, not a hard exclusion.

Usage:
    .venv/bin/python scripts/liquidity_screen_candidates.py [--leverage 2,3] [--top N]
"""
import argparse
import csv
import sqlite3
import sys

DB_PATH = "cache/research/trading_universe.db"

COMMODITY_KEYWORDS = ("crude", "oil", "gas", "commodity", "gold", "silver", "metals")


def already_tested_tickers(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute("SELECT DISTINCT ticker FROM backtest_cache WHERE version IN ('v4','v5')")
    return {row[0] for row in cur.fetchall()}


def screen(conn: sqlite3.Connection, leverages: list[int], top_n: int | None) -> list[dict]:
    tested = already_tested_tickers(conn)
    placeholders = ",".join("?" for _ in leverages)
    query = f"""
        SELECT symbol, description, index_underlier, inverse, leverage,
               avg_vol_10d, last_price
        FROM tickers
        WHERE ABS(leverage) IN ({placeholders})
          AND has_data = 1
          AND avg_vol_10d IS NOT NULL
          AND last_price IS NOT NULL
    """
    cur = conn.execute(query, leverages)
    rows = []
    for symbol, desc, underlier, inverse, leverage, avg_vol, last_price in cur.fetchall():
        if symbol in tested:
            continue
        notional = avg_vol * last_price * 0.01
        underlier_l = (underlier or "").lower()
        commodity_flag = any(kw in underlier_l for kw in COMMODITY_KEYWORDS)
        rows.append({
            "symbol": symbol,
            "description": desc or "",
            "underlier": underlier or "",
            "leverage": leverage,
            "inverse": bool(inverse),
            "avg_vol_10d": avg_vol,
            "last_price": last_price,
            "notional_1pct_adv": notional,
            "commodity_k1_caution": commodity_flag,
        })
    rows.sort(key=lambda r: r["notional_1pct_adv"], reverse=True)
    if top_n:
        rows = rows[:top_n]
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--leverage", default="2,3", help="comma-separated leverage magnitudes to include")
    parser.add_argument("--top", type=int, default=50)
    parser.add_argument("--out", default="output/liquidity_screen_candidates.csv")
    parser.add_argument("--no-single-stock", action="store_true",
                         help="exclude tickers whose underlier is a single stock (stock_underlier set)")
    args = parser.parse_args()

    leverages = [int(x) for x in args.leverage.split(",")]
    conn = sqlite3.connect(DB_PATH)
    rows = screen(conn, leverages, None)
    if args.no_single_stock:
        single_stock = set()
        cur = conn.execute("SELECT symbol FROM tickers WHERE stock_underlier IS NOT NULL")
        single_stock = {r[0] for r in cur.fetchall()}
        rows = [r for r in rows if r["symbol"] not in single_stock]
    if args.top:
        rows = rows[:args.top]

    print(f"{'Ticker':6s} {'Lev':>4s} {'Inv':>3s} {'1% ADV Notional':>18s} {'K1?':>4s}  Underlier")
    for r in rows:
        print(f"{r['symbol']:6s} {r['leverage']:>4.0f} {'Y' if r['inverse'] else '':>3s} "
              f"${r['notional_1pct_adv']:>16,.0f} {'!' if r['commodity_k1_caution'] else '':>4s}  {r['underlier']}")

    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)
    print(f"\n{len(rows)} candidates written to {args.out}")


if __name__ == "__main__":
    main()
