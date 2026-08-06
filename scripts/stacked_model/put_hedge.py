"""Reusable put-hedge overlay: wraps any canonical trade list (scripts/stacked_model/
trade_schema.py) with a protective-put cost/payoff adjustment. Generalizes
scripts/sim_bear_market_stress_hedged.py's inline single-OTM-level hedge (validated
2026-08-06: SOXL benefits at every OTM level/crash tested, KORU's extreme IV makes it a
net cost in its two worst crashes) into a real, reusable module -- per the 2026-08-06/07
conversation, put-hedge has value beyond this one backtest, so it isn't built bespoke.

Liquidity is a real structural constraint, not a config choice (docs/backlog_cache.md's
put-hedge feasibility findings: GDXU has no options market at all; HIBL/UDOW/USD/YANG
have no near-term weekly expirations; HIBL's real spread was 104% of mid, unrollable;
liquidity is thin everywhere except KORU/SOXL). check_liquidity() gates on this directly
against options_snapshot rather than assuming a fixed ticker list, since the liquid set
could shift over time.

No historical options data exists anywhere (yfinance has none; options_snapshot only
started collecting 2026-08-06) -- trades before that date cannot be priced with real
IV. hedge_pnl() falls back to a realized-vol-derived BS proxy for those, which is a
documented approximation, never silently treated as equivalent to real market IV.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts.put_decay_forecast import bs_put_price
from scripts.put_hedge_cost_forecast import get_roll_contract

ROLL_T_DAYS = 20  # matches sim_bear_market_stress_hedged.py's real-world roll cadence
MAX_SPREAD_FRAC_OF_MID = 1.0  # HIBL's real 104%-of-mid spread is the disqualifying example


def check_liquidity(conn, ticker, otm_pct) -> bool:
    """Real gate: does `ticker` have a near-term, tradeable options market AT THE REAL
    STRIKE BEING HEDGED (otm_pct is not optional -- a 5%-OTM contract passing liquidity
    says nothing about a 50%-OTM contract, which can have zero bid on the same ticker;
    confirmed 2026-08-08 this was the actual bug behind AGQ 25/50% OTM and KORU 50% OTM
    showing liquid=True with no real bid). Requires a weekly-or-tighter roll cadence
    (<=10 real days to the next expiration) and a bid-ask spread no wider than
    MAX_SPREAD_FRAC_OF_MID of the mid price on the nearest contract in the latest
    snapshot. Returns False (not an exception) on missing data -- absence of an options
    market is a real, expected answer for several tickers."""
    contract = get_roll_contract(conn, ticker, otm_pct=otm_pct)
    if contract is None or contract["roll_interval_days"] > 10:
        return False
    mid = (contract["bid"] + contract["ask"]) / 2
    if mid <= 0:
        return False
    spread_frac = (contract["ask"] - contract["bid"]) / mid
    return spread_frac <= MAX_SPREAD_FRAC_OF_MID


def get_real_iv(conn, ticker):
    """Real ATM IV from the latest options_snapshot row, or None if no coverage at all
    for this ticker (e.g. GDXU, which has no options market)."""
    rows = conn.execute("""
        SELECT underlying_price, strike, implied_volatility FROM options_snapshot
        WHERE ticker=? AND snapshot_ts=(SELECT MAX(snapshot_ts) FROM options_snapshot WHERE ticker=?)
          AND implied_volatility IS NOT NULL AND implied_volatility > 0
        ORDER BY expiration LIMIT 500
    """, (ticker, ticker)).fetchall()
    if not rows:
        return None
    px = rows[0][0]
    atm = min(rows, key=lambda r: abs(r[1] - px))
    return atm[2]


def realized_vol_iv_proxy(df_h, entry_i, lookback_bars=130):
    """Fallback IV proxy for trades that predate real options_snapshot coverage.
    Annualized stdev of hourly log returns over the lookback window ending at entry --
    a rough proxy, not a substitute for real quoted IV (real IV typically runs above
    realized vol, so this likely understates true hedge cost for pre-coverage trades).
    lookback_bars=130 is roughly one trading month at 6-7 bars/day."""
    closes = df_h["Close"].values
    start = max(0, entry_i - lookback_bars)
    window = closes[start:entry_i + 1]
    if len(window) < 20:
        return None
    log_rets = np.diff(np.log(window))
    hourly_vol = np.std(log_rets)
    return float(hourly_vol * np.sqrt(6.5 * 252))  # ~6.5 trading hours/day, 252 trading days/year


def hedge_pnl(trade, ticker, otm_pct, conn, df_h=None, prefer_real_iv=False):
    """Prices the protective put at entry and exit via single-shot BS valuation (same
    method as sim_bear_market_stress_hedged.hedge_pnl -- not a day-by-day walk).
    Returns {'v_entry', 'v_exit', 'spread_dollars'} (all per-share dollars, matching
    entry_p/exit_p's own units) so the caller can combine them against the REAL total
    capital deployed (stock + premium), not just the stock's own entry_p -- buying a
    put is real, separate cash outlay on top of the position, not funded by the
    position's own capital (caught 2026-08-08: computing hedge P&L as a fraction of
    entry_p alone implicitly treats the premium as free, overstating the hedged
    return by roughly premium/entry_p -- ~1-2% per trade, which compounds across every
    hedged trade in a sequence the same way the add-on compounding bug did).

    options_snapshot only ever holds TODAY's IV (collection started 2026-08-06, no
    historical options data exists anywhere) -- there is no way to get a real
    point-in-time IV for an arbitrary past trade, so this is a caller-controlled
    preference, not a date cutover:
    - prefer_real_iv=True: always use today's real ATM IV, matching
      sim_bear_market_stress_hedged.py's original crash-reconstruction precedent (using
      current IV as the best available forward-looking proxy for a hypothetical/synthetic
      scenario -- appropriate when trades aren't really tied to a specific past calendar
      date, e.g. the crash-stress tool).
    - prefer_real_iv=False (default): use realized_vol_iv_proxy (requires df_h) --
      appropriate for a real multi-year historical trade sequence, where applying one
      fixed today-snapshot IV to a trade from years ago would be arbitrary. Falls back
      to real IV only if no df_h/proxy is available.
    Returns None if no IV (or no real spread quote) can be determined at all -- caller
    must decide how to treat an unpriceable trade (e.g. skip it from the hedged
    comparison), never silently treat as zero-cost.
    """
    entry_p, exit_p = trade["entry_p"], trade["exit_p"]
    held_days = trade["held_days"]

    iv = None
    if prefer_real_iv:
        iv = get_real_iv(conn, ticker)
    if iv is None and df_h is not None:
        iv = realized_vol_iv_proxy(df_h, trade["entry_i"])
    if iv is None:
        iv = get_real_iv(conn, ticker)
    if iv is None:
        return None

    # No historical options data exists anywhere (options_snapshot started
    # 2026-08-06) -- same documented-approximation status as the IV proxy above: use
    # TODAY's real quoted spread at this strike as the best available proxy for every
    # trade's round-trip transaction cost, rather than the prior silent zero. If this
    # ticker/strike has no contract in today's snapshot at all, the cost genuinely
    # can't be estimated -- unpriceable, not free.
    contract = get_roll_contract(conn, ticker, otm_pct)
    if contract is None or contract["underlying_price"] <= 0:
        return None
    # Today's quoted spread is a dollar amount scaled to TODAY's real underlying price
    # (e.g. ~$0.10 on a $135 stock) -- charging that flat dollar figure against a trade
    # at a wildly different price scale (crash-stress trades on a synthetic historical
    # series can be $2-5) massively overstates the real cost. Normalize to a fraction
    # of TODAY's price first (like IV, spread cost is properly a % figure, not a fixed
    # dollar one), then rescale to THIS trade's own entry_p -- caught 2026-08-08 when a
    # SOXL 2008 GFC crash-stress hedge compounded to -98% purely from this scale
    # mismatch, not a real cost finding.
    spread_frac_of_price = (contract["ask"] - contract["bid"]) / contract["underlying_price"]
    spread_dollars = spread_frac_of_price * entry_p

    strike = entry_p * (1 - otm_pct / 100.0)
    v_entry = bs_put_price(entry_p, strike, ROLL_T_DAYS / 365.0, iv)
    t_exit = max(ROLL_T_DAYS - held_days, 0) / 365.0
    v_exit = bs_put_price(exit_p, strike, t_exit, iv)
    return {"v_entry": v_entry, "v_exit": v_exit, "spread_dollars": spread_dollars}


def apply_hedge(trades, ticker, otm_pct, conn, df_h=None, prefer_real_iv=False):
    """Returns a new trade list with each trade's `ret` adjusted for hedge P&L, and
    downside floored near -otm_pct (the real "ruin protection" property found in the
    2026-08-06 crash-stress work -- a put caps loss at roughly its strike distance even
    if this BS model misprices the exact payoff during an extreme move). Trades that
    can't be priced (hedge_pnl returns None) are left unhedged and flagged via
    `hedge_priced=False`, not silently dropped or zeroed.

    check_liquidity(otm_pct) is a real HARD gate here, not a warning a caller can
    ignore (fixed 2026-08-08 -- previously only printed a message and every trade got
    hedged anyway, even at strikes with zero real bid): if this ticker/strike fails it,
    every trade in this call is left unhedged and flagged `hedge_priced=False`.

    prefer_real_iv: see hedge_pnl's docstring -- pass True for synthetic/crash-stress
    scenarios (use today's real IV for everything), leave False for a real historical
    trade sequence (use the per-trade realized-vol proxy instead)."""
    if not check_liquidity(conn, ticker, otm_pct):
        return [{**t, "unhedged_ret": t["ret"], "hedge_pnl": None, "hedge_priced": False} for t in trades]

    out = []
    floor = -(otm_pct / 100.0) * 1.05  # small slack for the hedge's own residual cost
    for t in trades:
        pd_ = hedge_pnl(t, ticker, otm_pct, conn, df_h=df_h, prefer_real_iv=prefer_real_iv)
        if pd_ is None:
            out.append({**t, "unhedged_ret": t["ret"], "hedge_pnl": None, "hedge_priced": False})
            continue
        entry_p, exit_p = t["entry_p"], t["exit_p"]
        v_entry, v_exit, spread_dollars = pd_["v_entry"], pd_["v_exit"], pd_["spread_dollars"]
        # Buying a put is real cash on top of the position, not funded by the
        # position's own capital -- the true return denominator is entry_p + v_entry
        # (stock + premium), not entry_p alone. Using entry_p alone (the pre-2026-08-08
        # version) implicitly treated the premium as free, overstating the hedged
        # return by roughly v_entry/entry_p (~1-2% per trade here) -- compounds across
        # every hedged trade the same way the add-on compounding bug did.
        raw_hedged_ret = ((exit_p - entry_p) + (v_exit - v_entry) - spread_dollars) / (entry_p + v_entry)
        raw_hedged_ret = max(raw_hedged_ret, floor)
        # compound_scale (add-on trades only; no-op =1.0 for core/drought) converts
        # this leg's own true return into the compounding-equivalent contribution
        # combine_sequential's cumprod needs -- see add_on.py.
        scale = t.get("compound_scale", 1.0)
        new_ret = raw_hedged_ret * scale
        hedge_pnl_frac = ((v_exit - v_entry) - spread_dollars) / entry_p
        out.append({**t, "ret": new_ret, "unhedged_ret": t["ret"],
                    "hedge_pnl": hedge_pnl_frac, "hedge_priced": True})
    return out
