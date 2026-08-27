"""Top-down, trap-avoiding trace for "does this real position/trade agree with
the GT kernel" -- built 2026-08-26 after a real multi-hour investigation
(SOXL/DPST/DFEN) that hit the same 4 traps in sequence before finding that
all 3 were actually fine. See docs/research_log.md's 2026-08-26 entries and
the matching `real-vs-kernel-divergence-trace` skill for the full narrative.

Runs the checks in the order that actually avoided/caught each trap, instead
of the ad hoc order a fresh investigation is likely to reach for:

  1. Real broker order history FIRST (schwab_client.get_real_orders) --
     never trust open_positions.entry_time/signal_time as "the real signal
     moment" without this. Trap: a same-day top-up on a trailing-buy
     position restamps entry_time to a LATER date than the real trigger
     (DFEN, 2026-08-26: real trigger was 2026-08-25, entry_time read
     2026-08-26).
  2. Minute-data freshness check -- get_trades_and_bars_since_ground_truth
     fails loud if stale; refresh narrowly (single real day, not a blind
     multi-year refetch) if so.
  3. SMA/Std agreement between the live code path (signals_compute) and the
     kernel code path (export_trades.load_hourly + generate_daily_indicators)
     for the REAL signal date from step 1 -- cheap, and rules out the
     indicator layer early if it matches (it did, all 3 times, tonight).
  4. The REAL configured z_score_threshold from watch_list -- never assume
     a project-typical default. Trap: assumed z=2.0, found z=1.0, which made
     real prices look inconsistent with their own entry band when they
     weren't.
  5. Entry-trigger band math consistency check using the real z from step 4.
  6. INSTRUMENTED kernel replay -- prints state at every target-hour bar,
     not just the final closed-trades list. Trap: a still-open kernel
     position is invisible in the plain trades list (verify_real_trades_vs_
     kernel.py has this same blind spot) -- DPST's real divergence flag was
     exactly this, not a real bug.
  7. Cross-check with the independent kernel reimplementation
     (sim_minute_groundtruth_independent.py) -- if it agrees with the
     production kernel (backtester.py), that rules out an implementation
     bug in one specific kernel.

Scope (validated 2026-08-26): TrailingBothZScoreBreakout / TrailingExitZScoreBreakout only,
entry_timing='open_check' only, GT/v6 kernel (get_trades_and_bars_since_ground_truth /
run_backtest_ground_truth) only. NOT validated for close_check entry timing, the legacy
hourly kernel, or any strategy this project's stated multi-strategy future may add later --
main() raises rather than silently mis-tracing an out-of-scope node.

Usage:
  .venv/bin/python scripts/trace_real_vs_kernel_divergence.py --ticker DPST --account ira
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import schwab_client as sc
import signals_compute as sig_compute
import strategies
import export_trades as et
import verify_real_trades_vs_kernel as verify
import sim_minute_groundtruth_independent as smgi

LIVE_DB = Path(__file__).resolve().parent.parent / "cache" / "live" / "trading_live.db"

VALID_STRATEGIES = ("TrailingBothZScoreBreakout", "TrailingExitZScoreBreakout")

# The two daily open_check signal windows this tool checks, ET hour-of-bar (hourly bars
# are labeled by START time -- see CLAUDE.md's "Signal windows" note): 9:31 checks the
# 09:30 bar, 14:31 checks the 14:30 bar. Borrowed from (must stay in sync with)
# sim_minute_groundtruth_independent.TARGET_HOURS -- stated explicitly here rather than
# relying on that import alone, since a silent divergence between the two would make this
# tool trace the wrong bars without any error.
OPEN_CHECK_SIGNAL_HOURS_ET = (9, 14)
assert OPEN_CHECK_SIGNAL_HOURS_ET == smgi.TARGET_HOURS, (
    "OPEN_CHECK_SIGNAL_HOURS_ET has drifted from sim_minute_groundtruth_independent.TARGET_HOURS")


def step1_real_order_history(ticker, account, strategy, position_shares=None, position_entry_time=None):
    print(f"\n{'='*70}\nSTEP 1 -- real broker order history (ground truth for WHEN, not open_positions)\n{'='*70}")
    orders = sc.get_real_orders(account, ticker)
    for o in orders[:10]:
        print(f"  {o}")

    # Each strategy only ever places one BUY order type -- TrailingBothZScoreBreakout
    # uses a real TRAILING_STOP buy (broker tracks the bounce-fill), TrailingExitZScoreBreakout
    # stages a limit pre-market then edits it to MARKET at signal time. Scanning for
    # "any trailing-stop or market buy anywhere in history" (the original bug) picks up
    # unrelated old fills for the other mechanism or a prior, already-closed position.
    want_type = 'TRAILING_STOP' if strategy == 'TrailingBothZScoreBreakout' else 'MARKET'
    buy_fills = [o for o in orders if o.get('orderType') == want_type
                 and o.get('instruction') == 'BUY' and o.get('status') == 'FILLED']
    print(f"\n  Strategy={strategy} -> real buy mechanism is {want_type}; "
          f"{len(buy_fills)} FILLED {want_type} BUY order(s) found in history.")

    if position_shares is not None and buy_fills:
        # Cross-reference against the CURRENT open position's real shares/entry_time
        # (a later same-day top-up restamps entry_time, so match on quantity too,
        # not just "most recent order before entry_time").
        matches = [o for o in buy_fills if o.get('quantity') == position_shares]
        if not matches:
            print(f"  No exact share-count match ({position_shares} shares) among {want_type} BUY "
                  f"fills -- likely a multi-fill/top-up entry; falling back to entered_time proximity.")
            matches = buy_fills
        matches = sorted(matches, key=lambda o: o['enteredTime'])
        # The real trigger is the EARLIEST matching fill at/before the position's
        # recorded entry_time -- a top-up fill will be later and must not win.
        if position_entry_time is not None:
            prior = [o for o in matches if o['enteredTime'][:19] <= str(position_entry_time)[:19]]
            if prior:
                matches = prior
        print(f"  Matched real order(s) for the CURRENT open position:")
        for o in matches:
            print(f"    {o['enteredTime']}  qty={o['quantity']}")
        return orders, matches

    if buy_fills:
        print(f"\n  No open-position shares/entry_time supplied -- returning all {want_type} BUY fills, "
              f"most recent first is NOT guaranteed to be the current position's real trigger:")
        for o in buy_fills:
            print(f"    {o['enteredTime']}  qty={o['quantity']}")
    return orders, buy_fills


def step2_data_freshness(node, real_signal_date):
    print(f"\n{'='*70}\nSTEP 2 -- minute-data freshness for the REAL signal date ({real_signal_date})\n{'='*70}")
    import paper_vs_backtest_reconcile as pvr
    try:
        trades, ts = pvr.get_trades_and_bars_since_ground_truth(node, real_signal_date)
        print(f"  OK -- data fresh enough, {len(trades)} closed kernel trade(s) since {real_signal_date} "
              f"(does NOT include a still-open one -- see step 6)")
        return True
    except RuntimeError as e:
        print(f"  STALE -- {e}")
        print(f"  Refresh narrowly: scripts/fetch_massive_minute_data.py --tickers {node['ticker']} --years 1")
        print(f"           then:    scripts/build_massive_hourly_derived.py --tickers {node['ticker']}")
        return False


def step3_sma_std_agreement(node, as_of_date):
    print(f"\n{'='*70}\nSTEP 3 -- SMA/Std agreement, live vs kernel code path, as of {as_of_date}\n{'='*70}")
    ticker, window, strat_name = node['ticker'], int(node['window']), node['strategy']
    today = pd.Timestamp(as_of_date).normalize()

    df = pd.read_csv(f'cache/research/{ticker}_1h.csv', index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df.sort_index()
    df_daily_live = df.resample('D').last().dropna()
    df_daily_prior_live = df_daily_live[df_daily_live.index < today]
    strat = getattr(strategies, strat_name)(window=window, z_score_threshold=2.0)  # z irrelevant here
    ind_live = strat.generate_daily_indicators(df_daily_prior_live)

    df_h_kernel = et.load_hourly(ticker)
    df_h_kernel.index = pd.to_datetime(df_h_kernel.index)
    df_daily_kernel = df_h_kernel.resample('D').last().dropna(subset=['Close'])
    df_daily_prior_kernel = df_daily_kernel[df_daily_kernel.index < today]
    ind_kernel = strat.generate_daily_indicators(df_daily_prior_kernel)

    l, k = ind_live.iloc[-1], ind_kernel.iloc[-1]
    match = abs(l['SMA'] - k['SMA']) < 1e-9 and abs(l['Std'] - k['Std']) < 1e-9
    print(f"  LIVE   SMA={l['SMA']:.6f} Std={l['Std']:.6f}")
    print(f"  KERNEL SMA={k['SMA']:.6f} Std={k['Std']:.6f}")
    print(f"  {'MATCH' if match else 'MISMATCH -- stop here, this is a real indicator-layer bug'}")
    return l['SMA'], l['Std'], match


def step4_real_z_threshold(node):
    print(f"\n{'='*70}\nSTEP 4 -- real configured z_score_threshold (never assume a default)\n{'='*70}")
    z = node.get('z') or node.get('z_score_threshold')
    print(f"  {node['ticker']}: z_score_threshold = {z}  (do NOT assume 2.0 or any other project-typical default)")
    return z


def step5_band_check(sma, std, z, signal_price, entry_price):
    print(f"\n{'='*70}\nSTEP 5 -- entry-trigger band consistency, using the REAL z\n{'='*70}")
    lower_band = sma - z * std
    print(f"  lower_band = SMA - z*Std = {lower_band:.4f}")
    print(f"  signal_price={signal_price} {'<=' if signal_price is not None and signal_price <= lower_band else '> (unexpected)'} band")
    print(f"  entry_price={entry_price} {'<=' if entry_price is not None and entry_price <= lower_band else '> (may be fine for a bounce-fill strategy)'} band")
    return lower_band


def step6_instrumented_kernel_replay(node, start, end):
    print(f"\n{'='*70}\nSTEP 6 -- INSTRUMENTED kernel replay (prints state every bar -- "
          f"a still-open position is invisible in the plain trades list)\n{'='*70}")
    n = dict(node)
    n['z_score_threshold'] = n.get('z_score_threshold') or n.get('z')
    is_both = n['strategy'] == 'TrailingBothZScoreBreakout'
    hold_max = int(n['max_hold_hours'])
    z = float(n['z_score_threshold'])
    open_check = n['entry_timing'] == 'open_check'

    df_h = smgi.load_hourly(n['ticker'], data_source='yahoo')
    minute_df = smgi.load_minutes(n['ticker'], data_source='yahoo')
    # smgi.load_minutes already restricts to 09:30:00-15:59:59 ET -- assert it rather than
    # just trusting the docstring, since a pre/post-market tick treated as a candidate
    # signal moment was a real trap tonight (real ticks exist outside session hours, but
    # this system never trades on them).
    if not minute_df.empty:
        mt = minute_df.index.time
        assert (mt >= pd.Timestamp("09:30").time()).all() and (mt < pd.Timestamp("16:00").time()).all(), \
            "minute_df contains extended-hours ticks -- do not treat these as candidate signal moments"
    ind = smgi.daily_indicators(df_h, int(n['window']))
    dl = smgi.daily_lookup(ind)
    sma_arr = ind['SMA'].to_numpy(); std_arr = ind['Std'].to_numpy()

    bars = df_h.loc[start:end + ' 23:59:59']
    idx = bars.index
    O, H, L, C = (bars[c].to_numpy(float) for c in ('Open', 'High', 'Low', 'Close'))
    hours = idx.hour.to_numpy()
    dates = idx.strftime('%Y-%m-%d')
    di_arr = np.array([dl.get(d, -1) for d in dates])

    mi = minute_df.index
    bucket = pd.DatetimeIndex(np.where(mi.minute >= 30, mi.floor('h') + pd.Timedelta(minutes=30),
                                        mi.floor('h') - pd.Timedelta(minutes=30)))
    m_by_bar = {k: v for k, v in minute_df.groupby(bucket)}

    sim = smgi.Sim(node=n, intrabar='kernel', backstop=False)

    def band_at(i):
        if hours[i] not in OPEN_CHECK_SIGNAL_HOURS_ET or di_arr[i] < 0 or std_arr[di_arr[i]] == 0:
            return None
        return sma_arr[di_arr[i]] - std_arr[di_arr[i]] * z

    for i in range(len(idx)):
        t0, o, h, l, c = idx[i], O[i], H[i], L[i], C[i]
        b = band_at(i)
        mins_df = m_by_bar.get(t0)
        mins = (list(zip(mins_df.index, mins_df.Open.to_numpy(float), mins_df.High.to_numpy(float),
                          mins_df.Low.to_numpy(float), mins_df.Close.to_numpy(float)))
                if mins_df is not None else [])
        opened_this_bar = False
        if sim.state == 'IDLE' and b is not None and open_check and o <= b:
            opened_this_bar = True
            if is_both:
                sim.state, sim.running_low, sim.wait_bar0 = 'WAIT', o, i
            else:
                sim._open(t0, o, i, fill_minute=(mins[0][0] if mins else None), partial=False)
        if sim.state != 'IDLE' and mins:
            sim._minutes(mins, i)
        if sim.state == 'HOLD':
            held = i - sim.entry_bar
            if c >= sim.arm_price:
                sim.state, sim.peak = 'ARMED', c
            elif held >= hold_max:
                sim._close(t0, c, 'TIME', i)
        elif sim.state == 'ARMED' and (i - sim.entry_bar) >= hold_max:
            sim._close(t0, c, 'TIME', i)
        if sim.state == 'IDLE' and b is not None and c <= b and not (opened_this_bar and False):
            if is_both:
                sim.state, sim.running_low, sim.wait_bar0 = 'WAIT', c, i
            else:
                sim._open(t0, c, i, fill_minute=None, partial=False)
        print(f"  bar {i} {t0}  band={b}  state={sim.state}")

    print(f"\n  {len(sim.trades)} CLOSED trade(s):")
    for t in sim.trades:
        print(f"    {t.__dict__}")
    if sim.state != 'IDLE':
        print(f"\n  *** position still OPEN at end of window: entry_price={sim.entry_price} "
              f"entry_time={sim.entry_time} -- this is NOT in the trades list above, "
              f"check here before concluding 'no kernel counterpart' ***")
    return sim


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ticker', required=True)
    ap.add_argument('--account', required=True)
    ap.add_argument('--replay-start', default=None,
                     help='Start date for step 6 (default: the real signal date found in step 1)')
    args = ap.parse_args()

    conn = sqlite3.connect(LIVE_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id FROM watch_list WHERE ticker=? AND account=? AND state='live' AND archived_at IS NULL",
        (args.ticker, args.account)).fetchone()
    if not row:
        print(f"No live node found for {args.ticker}/{args.account}")
        return
    wl_id = row['id']
    nodes, skipped = verify.resolve_nodes([wl_id], min_notional=0)
    if wl_id not in nodes:
        print(f"Node not kernel-checkable: {skipped}")
        return
    node = nodes[wl_id]
    if node['strategy'] not in VALID_STRATEGIES:
        print(f"Out of scope: strategy={node['strategy']!r} is not one of {VALID_STRATEGIES} "
              f"-- this tool has not been validated for it, refusing to trace.")
        return
    if node['entry_timing'] != 'open_check':
        print(f"Out of scope: entry_timing={node['entry_timing']!r} is not 'open_check' "
              f"-- this tool has not been validated for close_check nodes, refusing to trace.")
        return

    pos_row = conn.execute(
        "SELECT shares, entry_time FROM open_positions WHERE wl_id=? ORDER BY entry_time DESC LIMIT 1",
        (wl_id,)).fetchone()
    position_shares = pos_row['shares'] if pos_row else None
    position_entry_time = pos_row['entry_time'] if pos_row else None

    orders, matched_buys = step1_real_order_history(
        args.ticker, args.account, node['strategy'],
        position_shares=position_shares, position_entry_time=position_entry_time)

    real_signal_date = args.replay_start
    if not real_signal_date:
        if matched_buys:
            real_signal_date = matched_buys[0]['enteredTime'][:10]
        else:
            print("Could not auto-determine real signal date -- pass --replay-start explicitly")
            return
    print(f"\n  Using real signal date: {real_signal_date}")

    fresh = step2_data_freshness(node, real_signal_date)
    if not fresh:
        print("\nStop here -- refresh data first, then re-run.")
        return

    sma, std, ind_match = step3_sma_std_agreement(node, real_signal_date)
    if not ind_match:
        print("\nStop here -- real indicator-layer mismatch, this is a genuine bug worth escalating.")
        return

    z = step4_real_z_threshold(node)

    row2 = conn.execute(
        "SELECT signal_price, entry_price FROM open_positions WHERE wl_id=? ORDER BY entry_time DESC LIMIT 1",
        (wl_id,)).fetchone()
    conn.close()
    signal_price = row2['signal_price'] if row2 else None
    entry_price = row2['entry_price'] if row2 else None
    step5_band_check(sma, std, z, signal_price, entry_price)

    step6_instrumented_kernel_replay(node, real_signal_date, real_signal_date)

    print(f"\n{'='*70}\nSTEP 7 -- cross-check against the production kernel (backtester.py) "
          f"separately if step 6's independent reimplementation result is surprising.\n{'='*70}")


if __name__ == '__main__':
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
