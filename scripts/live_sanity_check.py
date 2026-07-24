"""
Standalone real-order sanity check -- deliberately places an order expected
to be REJECTED, to observe how a real account/Schwab actually responds.
Bypasses active_signals.py/signals_notify.py/schwab_safety entirely (this
account isn't wired into schwab_safety.ACCOUNTS yet, and per-ticker testing
would otherwise require a temporary live watch_list row per ticker) -- calls
the schwab-py client directly, the same pattern used for the manual $200k
buying-power test (2026-07-17, done by hand in Schwab's UI; this is the API
equivalent). This is a manual, supervised tool: it requires the operator to
type a confirmation for every single order, one ticker at a time, and never
retries or loops.

Two test kinds:
  oversized_buy -- BUY far more shares than the account can afford. Expected:
    Schwab rejects at placement (insufficient buying power). Tells us how
    Schwab actually surfaces that rejection (error code/message), since our
    own schwab_safety.check_order cash check isn't in this call path at all.
  naked_sell    -- SELL shares of a ticker you hold zero of. Expected: Schwab
    rejects (no margin/short entry) rather than opening an actual short
    position. Refuses to even attempt this if the account is confirmed to
    hold shares of the ticker already (that would be a real, unintended
    partial-close, not a test).

Usage:
  .venv/bin/python scripts/live_sanity_check.py --account-suffix 1234 \\
      --test oversized_buy --tickers AGQ HIBL SOXL
  .venv/bin/python scripts/live_sanity_check.py --account-suffix 1234 \\
      --test naked_sell --tickers AGQ HIBL SOXL

Every attempt (and its real outcome) is logged to signals_db.coverage_events
(scenario_key f"sanity_{test}", mode='live') so it shows up in
scripts/coverage_matrix.py alongside everything else.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import schwab.orders.equities as equity_orders
from schwab.utils import Utils

import schwab_auth
import signals_db as db
from schwab_client import get_current_price
from signals_blocks import _post_message

OVERSIZED_BUY_MULTIPLIER = 50  # request 50x more shares than the account can afford


def _resolve_hash_by_suffix(client, suffix):
    r = client.get_account_numbers()
    r.raise_for_status()
    matches = [a for a in r.json() if a["accountNumber"].endswith(suffix)]
    if not matches:
        raise SystemExit(f"no linked account ends with '{suffix}'")
    if len(matches) > 1:
        raise SystemExit(f"'{suffix}' matches {len(matches)} linked accounts -- use more digits")
    return matches[0]["hashValue"]


def _real_balance_and_position(client, account_hash, ticker):
    r = client.get_account(account_hash, fields=[client.Account.Fields.POSITIONS])
    r.raise_for_status()
    acct = r.json()["securitiesAccount"]
    balances = acct["currentBalances"]
    cash = float(balances["cashAvailableForTrading"]) if "cashAvailableForTrading" in balances else float(balances["availableFunds"])
    shares = 0.0
    for p in acct.get("positions", []):
        if p.get("instrument", {}).get("symbol") == ticker:
            shares = float(p.get("longQuantity", 0.0))
    return cash, shares


def _confirm(prompt):
    return input(f"{prompt}\nType the ticker again to confirm, anything else to skip: ").strip()


def run_one(client, account_hash, ticker, test, account_label):
    price = get_current_price(ticker)
    cash, shares = _real_balance_and_position(client, account_hash, ticker)
    print(f"\n=== {ticker} -- {test} ===")
    print(f"  current price: ${price:.4f}   real cash available: ${cash:,.2f}   real shares held: {shares:g}")

    if test == "oversized_buy":
        affordable = int(cash // price) if price else 0
        quantity = max(affordable * OVERSIZED_BUY_MULTIPLIER, affordable + 1000, 1000)
        side, order_fn = "BUY", equity_orders.equity_buy_market
        notional = quantity * price
        prompt = (f"About to submit BUY {quantity} {ticker} (~${notional:,.0f}) against real cash "
                  f"${cash:,.2f} -- expecting Schwab to REJECT for insufficient buying power.")
    elif test == "naked_sell":
        if shares > 0:
            print(f"  ABORTING: account actually holds {shares:g} real shares of {ticker} -- "
                  f"this would be a real partial/full close, not a naked-sell test. Skipping.")
            db.log_coverage_event("sanity_naked_sell", "live", ticker=ticker,
                                   result="aborted_real_position_held", detail=f"shares={shares:g}")
            return
        quantity = 1
        side, order_fn = "SELL", equity_orders.equity_sell_market
        prompt = (f"About to submit SELL {quantity} {ticker} while holding 0 real shares -- "
                  f"expecting Schwab to REJECT (no margin/short entry). If this account has a "
                  f"margin feature, a naked sell could theoretically open a real short instead -- "
                  f"confirm you accept that risk for this specific ticker before continuing.")
    else:
        raise SystemExit(f"unknown test '{test}'")

    print(f"  {prompt}")
    if _confirm(f"  >> {ticker}").upper() != ticker.upper():
        print(f"  skipped {ticker}")
        db.log_coverage_event(f"sanity_{test}", "live", ticker=ticker, result="skipped_by_operator")
        return

    try:
        _post_message(f"\U0001F9EA SANITY TEST — submitting {side} {quantity} {ticker} in "
                       f"{account_label} (expecting rejection, {test})")
        order = order_fn(ticker, quantity)
        r = client.place_order(account_hash, order)
        r.raise_for_status()
        order_id = Utils(client, account_hash).extract_order_id(r)
        msg = f"  UNEXPECTED: order was ACCEPTED (order_id={order_id}) -- check Schwab's UI NOW, consider cancelling."
        print(msg)
        _post_message(f"⚠️ SANITY TEST {ticker} — order ACCEPTED, not rejected as expected "
                       f"(order_id={order_id}) — check Schwab immediately")
        db.log_coverage_event(f"sanity_{test}", "live", ticker=ticker, result="unexpectedly_accepted",
                               detail=f"order_id={order_id} qty={quantity}")
    except Exception as e:
        print(f"  REJECTED as expected: {e}")
        _post_message(f"✅ SANITY TEST {ticker} — order rejected as expected ({test})")
        db.log_coverage_event(f"sanity_{test}", "live", ticker=ticker, result="rejected_as_expected",
                               detail=str(e)[:500])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-suffix", required=True, help="last 3-4 digits of the real account number, as shown in Schwab's masked UI")
    ap.add_argument("--account-label", default="test_account", help="label for Slack messages/coverage_events only")
    ap.add_argument("--test", required=True, choices=["oversized_buy", "naked_sell"])
    ap.add_argument("--tickers", required=True, nargs="+")
    args = ap.parse_args()

    client = schwab_auth.get_client()
    account_hash = _resolve_hash_by_suffix(client, args.account_suffix)

    print(f"Account resolved for suffix '{args.account_suffix}'. Running '{args.test}' one ticker at a "
          f"time -- you'll be asked to confirm each one individually before anything is submitted.")
    for ticker in args.tickers:
        run_one(client, account_hash, ticker, args.test, args.account_label)


if __name__ == "__main__":
    main()
