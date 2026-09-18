"""Read-only, broker-verified portfolio performance check -- pulls real cash
balance, buying power, and per-position market value/P&L straight from
Schwab's own account response for every resolvable account, instead of
trusting open_positions/trade_log. Closes the "broker-verified not
DB-trusted" gap flagged in docs/backlog_cache.md (raised 2026-08-26). Makes
no order/mutating calls.

Usage: .venv/bin/python scripts/portfolio_performance_check.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import schwab_client
import schwab_safety

total_equity = 0.0
total_cash = 0.0
total_unrealized = 0.0

for nickname in schwab_safety.ACCOUNTS:
    try:
        hashes = schwab_client._resolve_account_hashes()
        if nickname not in hashes:
            print(f"\n{nickname}: NOT LINKED (no real account hash resolved)")
            continue

        cash = schwab_client.get_account_balance(nickname)
        bp = schwab_client.get_account_buying_power(nickname)
        raw_positions = schwab_client._get_raw_real_positions(nickname)

        print(f"\n{nickname}  (cash ${cash:,.2f}, buying power ${bp:,.2f})")
        total_cash += cash

        if not raw_positions:
            print("  no open positions")
            continue

        acct_mv = 0.0
        acct_pl = 0.0
        for p in raw_positions:
            symbol = p.get("instrument", {}).get("symbol", "?")
            qty = float(p.get("longQuantity", 0.0)) or float(p.get("shortQuantity", 0.0))
            if not qty:
                continue
            mv = float(p.get("marketValue", 0.0))
            day_pl = float(p.get("currentDayProfitLoss", 0.0))
            open_pl = float(p.get("longOpenProfitLoss", p.get("shortOpenProfitLoss", 0.0)))
            avg_price = float(p.get("averagePrice", 0.0))
            acct_mv += mv
            acct_pl += open_pl
            print(f"  {symbol:6s} {qty:g}sh @ avg ${avg_price:.2f}  mv=${mv:,.2f}  "
                  f"day_pl=${day_pl:+,.2f}  open_pl=${open_pl:+,.2f}")

        total_equity += acct_mv
        total_unrealized += acct_pl
        print(f"  -- account totals: mv=${acct_mv:,.2f}  open_pl=${acct_pl:+,.2f}")

    except Exception as e:
        print(f"\n{nickname}: ERROR -- {e}")

print(f"\n=== Portfolio: cash=${total_cash:,.2f}  positions_mv=${total_equity:,.2f}  "
      f"unrealized_pl=${total_unrealized:+,.2f} ===")
