"""
Research script: overlay idea for strategy "droughts" (extended no-trade stretches, the real
mechanism behind SOXL's weak 2026 -- docs/research_log.md's 2026-08-04 late entries). Corrects an
earlier, cruder version (drought_buy_hold_test.py, superseded) that just held through the entire
gap to the next real signal -- unrealistic, since that could ride a large uncontrolled drawdown.

Real design (per conversation): once a drought is confirmed (N trading days with no new entry
signal -- realistically knowable live, not day-0 foreknowledge), buy the underlying and manage
the position with the SAME risk primitives the core strategy already uses and has already been
tested with -- a fixed stop-loss (node's fixed_sl%) from entry, and/or a trailing stop
(trail_sell_pct%) from the running peak, active immediately (no arm-threshold gate, unlike the
core strategy's TP-then-trail state machine -- this position doesn't need one since it's already
long). Whichever exit fires first wins; if neither fires before the strategy's own next real
signal, exit there (hand off -- the strategy takes over from its own trigger).

Tests both variants (fixed-SL-only, trailing-stop-only) plus the combination (whichever is
tighter fires first) against every real historical drought, all 10 v5 tickers.

Usage: .venv/bin/python scripts/drought_overlay_test.py [--tickers ...] [--watchlist-id 65]
       [--confirm-days 10] [--csv]
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtester import prep_inputs, OPEN
import strategies
from scripts.export_trades import (
    load_hourly,
    simulate_trail_both_annotated,
    simulate_trail_exit_chaos,
)
from scripts.drought_detection_test import load_nodes

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
TARGET_H0, TARGET_H1 = 9, 14


def build_indicators(strategy_name, df_daily, window):
    strat_cls = getattr(strategies, strategy_name)
    return strat_cls(window=window).generate_daily_indicators(df_daily)


def get_trades_and_bars(node):
    df_h = load_hourly(node["ticker"])
    df_daily = df_h.resample("D").last().dropna(subset=["Close"])
    ind = build_indicators(node["strategy"], df_daily, node["window"])
    p = prep_inputs(df_h, ind)
    open_check = node["entry_timing"] == "open_check"

    if node["strategy"] == "TrailingBothZScoreBreakout":
        trades = simulate_trail_both_annotated(
            p, node["arm_pct"] / 100.0, node["fixed_sl"] / 100.0, node["max_hold_hours"],
            node["trail_buy_pct"] / 100.0, node["trail_sell_pct"] / 100.0,
            TARGET_H0, TARGET_H1, node["z"], open_check=open_check,
        )
    elif node["strategy"] == "TrailingExitZScoreBreakout":
        rng = np.random.default_rng(0)
        trades = simulate_trail_exit_chaos(
            p, node["arm_pct"] / 100.0, node["fixed_sl"] / 100.0, node["max_hold_hours"],
            node["trail_sell_pct"] / 100.0, TARGET_H0, TARGET_H1, node["z"],
            rng, "drop", 0.0, "drop", 0.0, open_check=open_check,
        )
    else:
        raise ValueError(f"unhandled strategy {node['strategy']}")

    real_trades = [t for t in trades if t["signal_i"] is not None and t["result"] != OPEN]
    return real_trades, df_h


def find_drought_windows(trades, df_h, confirm_days):
    """Given a node's real signal trades (from get_trades_and_bars) and confirm_days
    (how many no-signal trading days before the overlay enters), returns the list of
    (entry_i, gap_end_i) windows eligible for a drought-overlay entry -- one per gap
    between consecutive real signals long enough to actually confirm a drought.
    gap_end_i is the next real signal bar, the overlay's backstop/handoff point.
    Extracted 2026-08-05 from main()'s inline loop so scripts/drought_overlay_sweep.py
    can reuse the identical window-finding logic across a confirm_days grid instead of
    re-deriving it (this logic, not simulate_overlay, is what actually depends on
    confirm_days -- simulate_overlay only needs a window's entry/backstop bars)."""
    signal_bars = sorted(t["signal_i"] for t in trades)
    exit_bars = {t["signal_i"]: t["exit_i"] for t in trades}
    hours = df_h.index.hour
    checkpoint_mask = (hours == TARGET_H0) | (hours == TARGET_H1)
    checkpoint_bars = np.where(checkpoint_mask)[0]

    windows = []
    for k in range(len(signal_bars) - 1):
        gap_start = exit_bars[signal_bars[k]]  # position closes, drought can begin
        gap_end = signal_bars[k + 1]            # next real signal = backstop
        eligible = checkpoint_bars[(checkpoint_bars > gap_start) & (checkpoint_bars < gap_end)]
        if len(eligible) < confirm_days * 2:  # ~2 checkpoints/day
            continue
        entry_i = int(eligible[confirm_days * 2 - 1])
        if entry_i + 1 >= gap_end:
            continue
        windows.append((entry_i, gap_end))
    return windows


def simulate_overlay(df_h, entry_i, backstop_i, fixed_sl_pct, arm_pct, trail_sell_pct,
                      exit_vol_gate=None, bar_vol_pctile=None):
    """Bar-by-bar exit simulation using the CORE STRATEGY'S OWN exit state machine
    (TrailingBothZScoreBreakout's arm-then-trail mechanic, mirrored from
    export_trades.simulate_trail_both_annotated's in_trade branch) -- not a new design.
    Fixed SL protects immediately from entry; once price runs up arm_pct%, the trailing
    stop arms and starts protecting the peak by trail_sell_pct% instead. Whichever fires
    first wins. Same Open-first gap-through-trigger fill logic as the real kernel.

    exit_vol_gate/bar_vol_pctile (added 2026-08-06, per-ticker exit-vol-spike research --
    see scripts/drought_overlay_sweep.py's run_ticker_sweep_exit_vol_gated): when both are
    given, forces an early close at that bar's own Close (reason 'VOLSPIKE') the first bar
    where the position is underwater (cp < entry_price) AND bar_vol_pctile[i] >= the gate.
    Both default None -- zero behavior change for every existing caller."""
    opens, highs, lows, closes = (df_h["Open"].values, df_h["High"].values,
                                   df_h["Low"].values, df_h["Close"].values)
    entry_price = opens[entry_i + 1] if entry_i + 1 < len(opens) else closes[entry_i]
    entry_bar = entry_i + 1 if entry_i + 1 < len(opens) else entry_i
    sl_price = entry_price * (1 - fixed_sl_pct / 100.0)
    tp_price = entry_price * (1 + arm_pct / 100.0)
    trailing = False
    peak = entry_price

    for i in range(entry_bar + 1, backstop_i + 1):
        op, high, low, cp = opens[i], highs[i], lows[i], closes[i]
        if exit_vol_gate is not None and cp < entry_price:
            pct = bar_vol_pctile[i]
            if not np.isnan(pct) and pct >= exit_vol_gate:
                return {"exit_i": i, "exit_reason": "VOLSPIKE", "ret": cp / entry_price - 1.0}
        if trailing:
            trail_price = peak * (1 - trail_sell_pct / 100.0)
            if op <= trail_price:
                return {"exit_i": i, "exit_reason": "TRAIL", "ret": op / entry_price - 1.0}
            if high > peak:
                peak = high
            trail_price = peak * (1 - trail_sell_pct / 100.0)
            if low <= trail_price:
                return {"exit_i": i, "exit_reason": "TRAIL", "ret": trail_price / entry_price - 1.0}
            continue
        if op <= sl_price:
            return {"exit_i": i, "exit_reason": "SL", "ret": op / entry_price - 1.0}
        if low <= sl_price:
            return {"exit_i": i, "exit_reason": "SL", "ret": sl_price / entry_price - 1.0}
        if cp >= tp_price:
            trailing = True
            peak = cp

    # handoff -- neither SL nor an armed trailing exit fired before the strategy's own
    # next real signal
    exit_price = opens[backstop_i] if backstop_i < len(opens) else closes[-1]
    ret = exit_price / entry_price - 1.0
    return {"exit_i": backstop_i, "exit_reason": "HANDOFF", "ret": ret}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=None)
    parser.add_argument("--watchlist-id", type=int, default=65)
    parser.add_argument("--confirm-days", type=int, default=10)
    parser.add_argument("--sl-pct", type=float, default=None,
                         help="Override the SL %% (default: reuse each node's fixed_sl).")
    parser.add_argument("--arm-pct", type=float, default=None,
                         help="Override the arm threshold %% (default: reuse each node's real arm_pct).")
    parser.add_argument("--trail-pct", type=float, default=None,
                         help="Override the trailing-stop %% (default: reuse trail_sell_pct).")
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()

    nodes = load_nodes(args.watchlist_id, args.tickers)
    all_rows = []
    for node in nodes:
        try:
            trades, df_h = get_trades_and_bars(node)
        except Exception as e:
            print(f"{node['ticker']}: failed ({e})")
            continue
        if len(trades) < 2:
            continue

        for entry_i, gap_end in find_drought_windows(trades, df_h, args.confirm_days):
            sl_pct = args.sl_pct if args.sl_pct is not None else node["fixed_sl"]
            arm_pct = args.arm_pct if args.arm_pct is not None else node["arm_pct"]
            trail_pct = args.trail_pct if args.trail_pct is not None else node["trail_sell_pct"]
            result = simulate_overlay(df_h, entry_i, gap_end, sl_pct, arm_pct, trail_pct)
            all_rows.append({
                    "ticker": node["ticker"],
                    "entry_time": str(df_h.index[entry_i + 1]),
                    "exit_time": str(df_h.index[result["exit_i"]]),
                    "exit_reason": result["exit_reason"], "ret": result["ret"],
                })

    df = pd.DataFrame(all_rows)
    if df.empty:
        print("No droughts found.")
        return
    pd.set_option("display.width", 160)

    if args.csv:
        OUTPUT_DIR.mkdir(exist_ok=True)
        df.to_csv(OUTPUT_DIR / "drought_overlay_test.csv", index=False)
        print(f"Wrote {OUTPUT_DIR / 'drought_overlay_test.csv'}\n")

    print(f"--- Pooled (n={len(df)} drought overlay trades across {df['ticker'].nunique()} tickers) ---")
    print(f"mean_ret={df['ret'].mean()*100:.2f}%  median_ret={df['ret'].median()*100:.2f}%  "
          f"win_rate={(df['ret'] > 0).mean():.3f}  "
          f"compounded={(np.prod(1 + df['ret']) - 1)*100:.1f}%")

    print("\n--- Exit reason breakdown ---")
    print(df["exit_reason"].value_counts().to_string())
    print(df.groupby("exit_reason")["ret"].mean().mul(100).round(2).to_string())
    wins = df[df["ret"] > 0]["ret"]
    losses = df[df["ret"] <= 0]["ret"]
    if len(wins) and len(losses):
        print(f"win/loss magnitude ratio: {abs(wins.mean() / losses.mean()):.2f}x "
              f"(mean_win={wins.mean()*100:.2f}%  mean_loss={losses.mean()*100:.2f}%)")

    print("\n--- Per-ticker ---")
    per_ticker = df.groupby("ticker").agg(
        n=("ret", "size"), mean_ret=("ret", "mean"),
        win_rate=("ret", lambda s: (s > 0).mean()),
        compounded=("ret", lambda s: float(np.prod(1 + s) - 1)),
    ).round(4)
    print(per_ticker.to_string())


if __name__ == "__main__":
    main()
