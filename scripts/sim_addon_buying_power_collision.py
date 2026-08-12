"""How often would AGQ/ETHU/JNUG's real add-on legs collide on `brokerage`'s
shared equity -- raised 2026-08-12 after building the leverage-aware
buying-power check (schwab_client.get_leveraged_buying_power). The account's
$20,000 equity is one shared pool across all 3 tickers; this replays each
ticker's real historical trade sequence under its exact live node config and
checks, at every real arm event (the moment add-on would fire), whether the
account's leveraged buying power was enough to cover that add-on plus
whatever the OTHER 2 tickers already had open at that moment.

Real add-on order-time reservation logic (schwab_safety.check_order) only
reserves capital for tickers with a currently-RESTING BUY order -- a narrow,
sub-hour window this hourly-bar backtest can't see. This simulation instead
reserves for any ticker with an OPEN POSITION (entry through exit) at the
arm moment -- a broader, more conservative stand-in for "capital already
committed," answering the practically useful question ("how often would 3
tickers sharing one account's equity actually run short") rather than the
narrow one the real code checks moment-to-moment.

Static $20,000 equity assumed throughout (today's real brokerage cash) --
does NOT model real P&L drift over the backtest window, so this is an
approximation of "if the account had stayed at today's size the whole time,"
not a claim about what actually would have happened with compounding.

STALE / N/A as of 2026-08-12 (later same session): get_leveraged_buying_power
was reverted out of the real order-check path (paired Opus review found it
materially overstates real buying power on soxl_ira -- see that function's
docstring), so this script's premise (simulating collisions against it) has
no current real-world counterpart. NODES' notional values are also now stale
(real starting_notional was raised $2,000->$6,000 for all 3 the same
session, after this script was written). Kept for whenever
get_leveraged_buying_power is redesigned and safely re-wired -- re-verify
both the notional values and EQUITY against real state before trusting a
rerun's numbers.

Usage: .venv/bin/python scripts/sim_addon_buying_power_collision.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import schwab_client
from scripts.drought_overlay_test import get_trades_and_bars

EQUITY = 20_000.0
HEADROOM_MULT = 2.0

NODES = {
    'AGQ': dict(ticker='AGQ', strategy='TrailingExitZScoreBreakout', window=10, z=1.0,
                fixed_sl=2.0, arm_pct=8.0, trail_buy_pct=0.0, trail_sell_pct=7.0,
                max_hold_hours=84, entry_timing='open_check', notional=6000.0),
    'ETHU': dict(ticker='ETHU', strategy='TrailingBothZScoreBreakout', window=10, z=2.0,
                 fixed_sl=2.0, arm_pct=30.0, trail_buy_pct=2.0, trail_sell_pct=2.0,
                 max_hold_hours=98, entry_timing='open_check', notional=6000.0),
    'JNUG': dict(ticker='JNUG', strategy='TrailingBothZScoreBreakout', window=10, z=1.0,
                 fixed_sl=1.0, arm_pct=29.0, trail_buy_pct=1.0, trail_sell_pct=1.0,
                 max_hold_hours=112, entry_timing='open_check', notional=6000.0),
}


def main():
    margin_req = {t: schwab_client.get_account_margin_requirement(t) for t in NODES}
    print("Real margin requirements:", margin_req)

    # events: list of (timestamp, ticker, kind) where kind in {'open','arm','close'}
    events = []
    trades_by_ticker = {}
    for ticker, node in NODES.items():
        trades, df_h = get_trades_and_bars(node)
        trades_by_ticker[ticker] = (trades, df_h)
        for t in trades:
            open_ts = df_h.index[t['entry_i']]
            close_ts = df_h.index[t['exit_i']]
            events.append((open_ts, ticker, 'open', t))
            events.append((close_ts, ticker, 'close', t))
            if t.get('arm_i') is not None:
                arm_ts = df_h.index[t['arm_i']]
                events.append((arm_ts, ticker, 'arm', t))
    events.sort(key=lambda e: e[0])

    open_positions = {}  # ticker -> trade dict, while a position is open
    total_arms = 0
    collisions = []
    for ts, ticker, kind, t in events:
        if kind == 'open':
            open_positions[ticker] = t
        elif kind == 'close':
            open_positions.pop(ticker, None)
        elif kind == 'arm':
            total_arms += 1
            notional = NODES[ticker]['notional']
            buying_power = EQUITY / margin_req[ticker]
            reserved_other = sum(
                NODES[other]['notional'] for other in open_positions if other != ticker
            )
            required = notional * HEADROOM_MULT + reserved_other
            if required > buying_power:
                collisions.append((ts, ticker, required, buying_power,
                                    [o for o in open_positions if o != ticker]))

    print(f"\nTotal real arm (add-on trigger) events across AGQ/ETHU/JNUG: {total_arms}")
    print(f"Would-collide (required > available leveraged buying power): {len(collisions)}")
    if collisions:
        print("\nCollisions:")
        for ts, ticker, required, buying_power, others in collisions:
            print(f"  {ts}  {ticker}  required=${required:,.0f}  available=${buying_power:,.0f}  "
                  f"concurrent_open={others}")


if __name__ == '__main__':
    main()
