"""Option 4 -- "shadow-kernel replica gating" -- for the confirmed SOXL live-vs-backtest
divergence (2026-08-20, wl_id=92/ira; see scripts/sim_live_mimic_baseline.py's docstring for
the full incident writeup and the root cause).

RESEARCH SCRIPT ONLY. Touches no production module.

The idea under test: rather than hand-writing a specific rule that suppresses live's extra
same-bar re-derivation after a carried WAIT resolves (that's "Option 2"), run the REAL strict
backtest kernel's own state machine as an independent SHADOW replica in lockstep with the
live-mimic, and only let live open a fresh WAIT if the shadow -- which knows nothing about
live's extra behavior -- would ALSO be starting a fresh WAIT on that exact bar.

Mechanically:
  * ShadowKernel below is a faithful, bar-at-a-time restatement of the verified
    scripts/export_trades.py::simulate_trail_both_annotated loop body (open_check=True).
    It is verified at runtime (--verify-shadow, on by default) to emit a trade list
    byte-identical to that function's, so "the shadow is the real kernel" is proven, not
    claimed.
  * The shadow is stepped for EVERY bar, unconditionally, before the live-mimic processes
    that same bar. It never reads live-mimic state, so it cannot be contaminated by it; by
    the same token its state legitimately DIVERGES from live's once live takes a trade the
    kernel wouldn't (that divergence is the interesting part -- see the report).
  * gate='shadow' consults shadow.started_wait_this_bar at the one decision point.
    gate='suppress' is the Option-2-equivalent (never re-derive). gate='none' reproduces
    the shared live-mimic baseline exactly (asserted at runtime).

Scope note (deliberate, matches Option 2's scope so the comparison is apples-to-apples):
only the same-bar WAIT RE-DERIVATION is gated. The inline same-bar SL check on a carried
fill (the other live-mimic deviation) is left in place under every gate mode.

FINDINGS (2026-08-21 run, all 6 real TrailingBoth nodes, full cached hourly history):
  * The ShadowKernel reproduces simulate_trail_both_annotated byte-identically on all 6
    tickers (runtime assert), and gate='none' reproduces sim_live_mimic_baseline exactly.
  * Option 4 == plain suppression (Option 2) on 5 of 6 tickers. Across ~138 gate decision
    points there was exactly ONE ALLOW (LABU 2025-04-10 09:30) -- and it only differed
    because the shadow's agreeing WAIT came from the OPEN check while the gated action is a
    CLOSE-check re-derivation. Require same-origin agreement and Option 4 collapses onto
    Option 2 exactly, everywhere.
  * The "lockstep" framing is a misnomer worth stating plainly: the shadow is INDEPENDENT,
    not synchronized. From the first gate decision onward the shadow is holding a position
    the live-mimic already closed (the ungated inline same-bar SL), so the gate's verdict is
    supplied by a kernel running a DIFFERENT trade history. It cannot desync in the sense of
    being corrupted by live state (proven above), but it also cannot answer the question one
    actually wants ("is live's action representable by the kernel GIVEN live's history").

Usage:
    .venv/bin/python scripts/sim_option4_shadow_gate.py [--tickers T ...] [--audit TICKER]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategies
from backtester import prep_inputs, WIN, LOSS, TWIN, TLOSS, OPEN
from scripts.export_trades import load_hourly, simulate_trail_both_annotated
from scripts.sim_5min_whipsaw import NODES
from scripts.sim_live_mimic_baseline import simulate_live_mimic

TB_NODES = [n for n in NODES if n['strategy'] == 'TrailingBothZScoreBreakout']


class ShadowKernel:
    """Independent, bar-at-a-time replica of the strict kernel
    (export_trades.simulate_trail_both_annotated, open_check=True).

    Contract: step(i) must be called exactly once per bar, for every bar, in order, and the
    object must never be fed anything derived from the live-mimic's state. After step(i):
      .started_wait_this_bar  -- True iff this bar started a fresh WAIT from the IDLE branch
      .wait_origin_this_bar   -- 'open' | 'close' | None
      .state                  -- 'in_trade' | 'waiting' | 'idle' (after the bar)
    """

    def __init__(self, p, take_profit, stop_loss, max_hours_to_hold,
                 trail_buy_pct, trail_pct, target_h0, target_h1, z_thresh):
        self.p = p
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.max_hours_to_hold = max_hours_to_hold
        self.trail_buy_pct = trail_buy_pct
        self.trail_pct = trail_pct
        self.target_h0 = target_h0
        self.target_h1 = target_h1
        self.z_thresh = z_thresh

        self.trades = []
        self.in_trade = self.waiting = self.trailing = False
        self.entry_price = self.stop_price = self.tp_price = self.peak = 0.0
        self.entry_bar = self.held = 0
        self.running_low = 0.0
        self.wait_bars = 0
        self.signal_bar = None
        self.signal_z = None
        self.arm_bar = None

        self.started_wait_this_bar = False
        self.wait_origin_this_bar = None
        self._next_i = 0

    @property
    def state(self):
        if self.in_trade:
            return 'in_trade'
        if self.waiting:
            return 'waiting'
        return 'idle'

    def _close(self, i, exit_px, held, result, ret):
        self.trades.append(dict(signal_i=self.signal_bar, signal_z=self.signal_z,
                                entry_i=self.entry_bar, arm_i=self.arm_bar, exit_i=i,
                                entry_p=self.entry_price, exit_p=exit_px, held=held,
                                result=result, ret=ret))

    def step(self, i):
        assert i == self._next_i, f"ShadowKernel stepped out of order: got {i}, expected {self._next_i}"
        self._next_i = i + 1
        self.started_wait_this_bar = False
        self.wait_origin_this_bar = None

        p = self.p
        cp, high, low, op = p['prices'][i], p['highs'][i], p['lows'][i], p['opens'][i]

        if self.in_trade:
            self.held += 1
            if self.trailing:
                trail_stop_gap = self.peak * (1.0 - self.trail_pct)
                if op <= trail_stop_gap:
                    pc = (op - self.entry_price) / self.entry_price
                    self._close(i, op, self.held, WIN if pc > 0 else LOSS, pc)
                    self.in_trade = self.trailing = False
                    return
                if high > self.peak:
                    self.peak = high
                trail_stop = self.peak * (1.0 - self.trail_pct)
                if low <= trail_stop or self.held >= self.max_hours_to_hold:
                    exit_px = trail_stop if low <= trail_stop else cp
                    pc = (exit_px - self.entry_price) / self.entry_price
                    self._close(i, exit_px, self.held, WIN if pc > 0 else LOSS, pc)
                    self.in_trade = self.trailing = False
                return
            if op <= self.stop_price:
                pc = (op - self.entry_price) / self.entry_price
                self._close(i, op, self.held, LOSS, pc)
                self.in_trade = False
                return
            if low <= self.stop_price:
                pc = (self.stop_price - self.entry_price) / self.entry_price
                self._close(i, self.stop_price, self.held, LOSS, pc)
                self.in_trade = False
                return
            if cp >= self.tp_price:
                self.trailing = True
                self.peak = cp
                self.arm_bar = i
                return
            if self.held >= self.max_hours_to_hold:
                pc = (cp - self.entry_price) / self.entry_price
                self._close(i, cp, self.held, TWIN if pc > 0 else TLOSS, pc)
                self.in_trade = False
            return

        if self.waiting:
            self.wait_bars += 1
            buy_trigger_gap = self.running_low * (1.0 + self.trail_buy_pct)
            if op >= buy_trigger_gap:
                self._fill(i, op)
                return
            if low < self.running_low:
                self.running_low = low
            buy_trigger = self.running_low * (1.0 + self.trail_buy_pct)
            if high >= buy_trigger:
                self._fill(i, buy_trigger)
                return
            if self.wait_bars >= self.max_hours_to_hold:
                self.waiting = False
            return

        h = p['hours'][i]
        if h != self.target_h0 and h != self.target_h1:
            return
        di = p['daily_idx'][i]
        if di < 0:
            return
        sma, std = p['sma_arr'][di], p['std_arr'][di]
        if std == 0.0:
            return
        lower_band = sma - std * self.z_thresh
        trend_arr, has_trend = p['trend_arr'], p['has_trend']
        signal_open = (op <= lower_band) and (op > trend_arr[di]) if has_trend else op <= lower_band
        if signal_open:
            self._start_wait(i, op, sma, std, 'open')
            return
        signal_close = (cp <= lower_band) and (cp > trend_arr[di]) if has_trend else cp <= lower_band
        if signal_close:
            self._start_wait(i, cp, sma, std, 'close')

    def _start_wait(self, i, px, sma, std, origin):
        self.waiting = True
        self.running_low = px
        self.wait_bars = 0
        self.signal_bar = i
        self.signal_z = (px - sma) / std
        self.started_wait_this_bar = True
        self.wait_origin_this_bar = origin

    def _fill(self, i, entry_price):
        self.entry_price = entry_price
        self.tp_price = entry_price * (1.0 + self.take_profit)
        self.stop_price = entry_price * (1.0 - self.stop_loss)
        self.entry_bar = i
        self.held = 0
        self.arm_bar = None
        self.in_trade = True
        self.waiting = self.trailing = False

    def finalize(self):
        n = len(self.p['prices'])
        if self.in_trade:
            cp = self.p['prices'][n - 1]
            pc = (cp - self.entry_price) / self.entry_price
            self._close(n - 1, cp, self.held, OPEN, pc)
        return self.trades


# ---------------------------------------------------------------------------

_CMP_KEYS = ('signal_i', 'entry_i', 'arm_i', 'exit_i', 'entry_p', 'exit_p', 'held', 'result', 'ret')


def _cmp(trades):
    return [tuple(t[k] for k in _CMP_KEYS) for t in trades]


def simulate_gated(p, take_profit, stop_loss, trail_buy_pct, trail_pct,
                   max_hours_to_hold, target_h0, target_h1, z_thresh, gate='shadow'):
    """The live-mimic mechanism (identical to sim_live_mimic_baseline.simulate_live_mimic)
    with the same-bar carried re-derivation routed through `gate`:
        'none'     -- always re-derive (== the unfixed live-mimic baseline)
        'suppress' -- never re-derive (== Option 2's hand-written rule)
        'shadow'   -- re-derive only if the independent ShadowKernel also started a fresh
                      WAIT on this exact bar (Option 4)
    """
    prices, highs, lows, hours, opens = p['prices'], p['highs'], p['lows'], p['hours'], p['opens']
    daily_idx, sma_arr, std_arr = p['daily_idx'], p['sma_arr'], p['std_arr']
    trend_arr, has_trend = p['trend_arr'], p['has_trend']
    dates = p['timestamps'].date

    shadow = ShadowKernel(p, take_profit, stop_loss, max_hours_to_hold,
                          trail_buy_pct, trail_pct, target_h0, target_h1, z_thresh)

    trades = []
    in_trade = waiting = trailing = False
    entry_price = stop_price = tp_price = peak = 0.0
    entry_bar = held = 0
    running_low = 0.0
    wait_bars = 0
    signal_bar = signal_z = signal_date = None
    arm_bar = None
    carried_hits = 0          # carried fill + same-bar SL breach (gate decision points)
    gate_allowed = 0
    gate_vetoed = 0
    gate_log = []

    def check_signal(i, op, cp, allow_open=True):
        nonlocal waiting, running_low, wait_bars, signal_bar, signal_z, signal_date
        di = daily_idx[i]
        if di < 0:
            return False
        sma, std = sma_arr[di], std_arr[di]
        if std == 0.0:
            return False
        lower_band = sma - std * z_thresh
        if allow_open:
            signal_open = (op <= lower_band) and (op > trend_arr[di]) if has_trend else op <= lower_band
            if signal_open:
                waiting = True; running_low = op; wait_bars = 0
                signal_bar = i; signal_z = (op - sma) / std; signal_date = dates[i]
                return True
        signal_close = (cp <= lower_band) and (cp > trend_arr[di]) if has_trend else cp <= lower_band
        if signal_close:
            waiting = True; running_low = cp; wait_bars = 0
            signal_bar = i; signal_z = (cp - sma) / std; signal_date = dates[i]
            return True
        return False

    n = len(prices)
    for i in range(n):
        # Shadow steps first, unconditionally, every bar. Independent of everything below.
        shadow.step(i)

        cp, high, low, op = prices[i], highs[i], lows[i], opens[i]

        if in_trade:
            held += 1
            if trailing:
                trail_stop_gap = peak * (1.0 - trail_pct)
                if op <= trail_stop_gap:
                    pc = (op - entry_price) / entry_price
                    trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, arm_i=arm_bar, exit_i=i,
                                       entry_p=entry_price, exit_p=op, held=held,
                                       result=WIN if pc > 0 else LOSS, ret=pc, exit_reason='TRAIL'))
                    in_trade = trailing = False
                    continue
                if high > peak:
                    peak = high
                trail_stop = peak * (1.0 - trail_pct)
                if low <= trail_stop or held >= max_hours_to_hold:
                    exit_px = trail_stop if low <= trail_stop else cp
                    pc = (exit_px - entry_price) / entry_price
                    reason = 'TRAIL' if low <= trail_stop else 'TIME'
                    trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, arm_i=arm_bar, exit_i=i,
                                       entry_p=entry_price, exit_p=exit_px, held=held,
                                       result=WIN if pc > 0 else LOSS, ret=pc, exit_reason=reason))
                    in_trade = trailing = False
                continue
            if op <= stop_price:
                pc = (op - entry_price) / entry_price
                trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, arm_i=arm_bar, exit_i=i,
                                   entry_p=entry_price, exit_p=op, held=held, result=LOSS,
                                   ret=pc, exit_reason='SL'))
                in_trade = False
                continue
            if low <= stop_price:
                pc = (stop_price - entry_price) / entry_price
                trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, arm_i=arm_bar, exit_i=i,
                                   entry_p=entry_price, exit_p=stop_price, held=held, result=LOSS,
                                   ret=pc, exit_reason='SL'))
                in_trade = False
                continue
            if cp >= tp_price:
                trailing = True; peak = cp; arm_bar = i
                continue
            if held >= max_hours_to_hold:
                pc = (cp - entry_price) / entry_price
                trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, arm_i=arm_bar, exit_i=i,
                                   entry_p=entry_price, exit_p=cp, held=held,
                                   result=TWIN if pc > 0 else TLOSS, ret=pc, exit_reason='TIME'))
                in_trade = False
            continue

        if waiting:
            wait_bars += 1
            buy_trigger_gap = running_low * (1.0 + trail_buy_pct)
            filled = False
            if op >= buy_trigger_gap:
                entry_price = op; filled = True
            else:
                if low < running_low:
                    running_low = low
                buy_trigger = running_low * (1.0 + trail_buy_pct)
                if high >= buy_trigger:
                    entry_price = buy_trigger; filled = True
            if filled:
                is_carried = signal_date is not None and signal_date < dates[i]
                tp_price = entry_price * (1.0 + take_profit)
                stop_price = entry_price * (1.0 - stop_loss)
                entry_bar = i; held = 0; arm_bar = None
                in_trade = True; waiting = trailing = False
                if is_carried and low <= stop_price:
                    pc = (stop_price - entry_price) / entry_price
                    trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, arm_i=None, exit_i=i,
                                       entry_p=entry_price, exit_p=stop_price, held=0, result=LOSS,
                                       ret=pc, exit_reason='SL_SAME_BAR_CARRIED'))
                    in_trade = False
                    carried_hits += 1
                    allow = (gate == 'none') or (gate == 'shadow' and shadow.started_wait_this_bar)
                    if gate != 'none':
                        gate_log.append(dict(i=i, ts=str(p['timestamps'][i]),
                                             shadow_state_after=shadow.state,
                                             shadow_started_wait=shadow.started_wait_this_bar,
                                             shadow_origin=shadow.wait_origin_this_bar,
                                             allowed=allow))
                    if allow:
                        if check_signal(i, op, cp, allow_open=False):
                            gate_allowed += 1
                    else:
                        gate_vetoed += 1
                continue
            if wait_bars >= max_hours_to_hold:
                waiting = False
            continue

        h = hours[i]
        if h != target_h0 and h != target_h1:
            continue
        check_signal(i, op, cp, allow_open=True)

    if in_trade:
        cp = prices[n - 1]
        pc = (cp - entry_price) / entry_price
        trades.append(dict(signal_i=signal_bar, entry_i=entry_bar, arm_i=arm_bar, exit_i=n - 1,
                           entry_p=entry_price, exit_p=cp, held=held, result=OPEN,
                           ret=pc, exit_reason='OPEN'))

    return trades, dict(carried_hits=carried_hits, gate_allowed=gate_allowed,
                        gate_vetoed=gate_vetoed, gate_log=gate_log,
                        shadow_trades=shadow.finalize())


def _summarize(trades):
    compounded = 1.0
    for t in trades:
        compounded *= (1.0 + t['ret'])
    return dict(n=len(trades), pct=round((compounded - 1.0) * 100, 2))


def _load(node):
    df_h = load_hourly(node['ticker'])
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    strat = strategies.TrailingBothZScoreBreakout(window=node['window'], z_score_threshold=node['z'])
    ind = strat.generate_daily_indicators(df_daily)
    return prep_inputs(df_h, ind)


def _params(node):
    return dict(take_profit=node['arm_pct'] / 100.0,
                stop_loss=node['fixed_sl'] / 100.0,
                trail_buy_pct=node['trail_buy_pct'] / 100.0,
                trail_pct=node['trail_sell_pct'] / 100.0,
                max_hours_to_hold=node['max_hold_hours'],
                z_thresh=node['z'])


def _print_trades(label, trades, timestamps, date_filter=None):
    print(f"  {label}:")
    any_row = False
    for t in trades:
        et = timestamps[t['entry_i']]
        if date_filter and et.date().isoformat() not in date_filter:
            continue
        any_row = True
        print(f"    entry={et} exit={timestamps[t['exit_i']]} "
              f"entry_p={t['entry_p']:.4f} exit_p={t['exit_p']:.4f} "
              f"ret={t['ret'] * 100:+.2f}% {t.get('exit_reason', '')}")
    if not any_row:
        print("    (none)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', nargs='*', default=None)
    ap.add_argument('--audit', default=None,
                    help='ticker: dump every bar where option4 and live-mimic diverge')
    args = ap.parse_args()
    nodes = TB_NODES if not args.tickers else [n for n in TB_NODES if n['ticker'] in args.tickers]

    rows = []
    for node in nodes:
        ticker = node['ticker']
        p = _load(node)
        prm = _params(node)
        ts = p['timestamps']

        strict = simulate_trail_both_annotated(
            p, prm['take_profit'], prm['stop_loss'], prm['max_hours_to_hold'],
            prm['trail_buy_pct'], prm['trail_pct'], 9, 14, prm['z_thresh'], open_check=True)

        mimic_ref, ref_carried = simulate_live_mimic(
            p, prm['take_profit'], prm['stop_loss'], prm['trail_buy_pct'], prm['trail_pct'],
            prm['max_hours_to_hold'], 9, 14, prm['z_thresh'])

        mimic, m_meta = simulate_gated(p, **prm, target_h0=9, target_h1=14, gate='none')
        supp, s_meta = simulate_gated(p, **prm, target_h0=9, target_h1=14, gate='suppress')
        opt4, o_meta = simulate_gated(p, **prm, target_h0=9, target_h1=14, gate='shadow')

        # --- runtime verifications (fail loudly rather than silently reporting garbage) ---
        assert _cmp(o_meta['shadow_trades']) == _cmp(strict), \
            f"{ticker}: ShadowKernel does NOT reproduce the verified strict kernel"
        assert _cmp(mimic) == _cmp(mimic_ref), \
            f"{ticker}: gate='none' does NOT reproduce sim_live_mimic_baseline"
        assert ref_carried == m_meta['carried_hits'], f"{ticker}: carried-hit count mismatch vs baseline"

        b, m, s, o = _summarize(strict), _summarize(mimic), _summarize(supp), _summarize(opt4)
        identical = _cmp(opt4) == _cmp(supp)
        rows.append(dict(ticker=ticker, base_n=b['n'], base_pct=b['pct'],
                         mimic_n=m['n'], mimic_pct=m['pct'],
                         supp_n=s['n'], supp_pct=s['pct'],
                         o4_n=o['n'], o4_pct=o['pct'],
                         carried=m_meta['carried_hits'],
                         supp_carried=s_meta['carried_hits'], o4_carried=o_meta['carried_hits'],
                         o4_allowed=o_meta['gate_allowed'], o4_vetoed=o_meta['gate_vetoed'],
                         same_as_supp='YES' if identical else 'NO'))

        if ticker == 'SOXL':
            print("=== SOXL 2026-08-19/20 trade lists ===")
            df = ('2026-08-19', '2026-08-20')
            _print_trades('strict baseline', strict, ts, df)
            _print_trades('live-mimic (unfixed)', mimic, ts, df)
            _print_trades('option4 (shadow-gated)', opt4, ts, df)
            _print_trades('option2-equiv (suppress)', supp, ts, df)
            print()

        if args.audit == ticker:
            print(f"=== {ticker} gate decision log (option4) ===")
            for g in o_meta['gate_log']:
                print(f"  bar {g['i']} {g['ts']} shadow_after={g['shadow_state_after']:9} "
                      f"started_wait={g['shadow_started_wait']!s:5} origin={g['shadow_origin']} "
                      f"-> {'ALLOW' if g['allowed'] else 'VETO'}")
            print(f"\n=== {ticker} option4 vs suppress: first differing trades ===")
            co, cs = _cmp(opt4), _cmp(supp)
            for k in range(max(len(co), len(cs))):
                a = co[k] if k < len(co) else None
                bb = cs[k] if k < len(cs) else None
                if a != bb:
                    print(f"  idx {k}")
                    if a:
                        print(f"    o4 : entry={ts[opt4[k]['entry_i']]} exit={ts[opt4[k]['exit_i']]} "
                              f"{opt4[k]['entry_p']:.4f}->{opt4[k]['exit_p']:.4f} "
                              f"{opt4[k]['ret']*100:+.2f}% {opt4[k]['exit_reason']}")
                    if bb:
                        print(f"    sup: entry={ts[supp[k]['entry_i']]} exit={ts[supp[k]['exit_i']]} "
                              f"{supp[k]['entry_p']:.4f}->{supp[k]['exit_p']:.4f} "
                              f"{supp[k]['ret']*100:+.2f}% {supp[k]['exit_reason']}")
            print()

    hdr = (f"{'ticker':6} {'strict_n':>8} {'strict_%':>10} {'mimic_n':>8} {'mimic_%':>10} "
           f"{'supp_n':>7} {'supp_%':>10} {'o4_n':>6} {'o4_%':>10} "
           f"{'carried':>8} {'o4_allow':>9} {'o4_veto':>8} {'o4==supp':>9}")
    print(hdr)
    print('-' * len(hdr))
    for r in rows:
        print(f"{r['ticker']:6} {r['base_n']:>8} {r['base_pct']:>10} {r['mimic_n']:>8} {r['mimic_pct']:>10} "
              f"{r['supp_n']:>7} {r['supp_pct']:>10} {r['o4_n']:>6} {r['o4_pct']:>10} "
              f"{r['carried']:>8} {r['o4_allowed']:>9} {r['o4_vetoed']:>8} {r['same_as_supp']:>9}")


if __name__ == '__main__':
    main()
