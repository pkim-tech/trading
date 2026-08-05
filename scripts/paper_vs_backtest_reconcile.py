"""
Reconciles real paper-trading activity (paper_trade_log/paper_positions) against what the
corrected backtest kernel would produce for the same ticker/date window, using the exact same
node config (mode='research' watch_list nodes, watchlist 65) and the same real signal-window
cadence (paper trading fires from active_signals._scan_buy_signals, the same two daily windows
the backtest mirrors -- not continuous polling, so a real divergence isn't explainable by check
frequency alone).

Built 2026-08-04 (very late) after a manual one-off comparison found HIBL/USD/KORU roughly
track their backtest for a recent window, but SOXL and YANG diverged (SOXL: backtest shows a
window-net-positive result with a real win, paper shows all losses; YANG: backtest shows zero
signals in the window, paper fired twice) -- this replaces that throwaway comparison with a
permanent, rerunnable tool instead of re-deriving it by hand next time.

Matches paper trades to backtest trades by entry date (paper's real fill time vs. backtest's
signal bar date) rather than exact timestamp, since paper's real fill price/time can drift
slightly from the idealized backtest fill -- the comparison is "did roughly the same signal
fire," not "did the exact same price land."

Usage: .venv/bin/python scripts/paper_vs_backtest_reconcile.py [--tickers ...] [--watchlist-id 65]
       [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--csv]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.drought_overlay_test import load_nodes, get_trades_and_bars

LIVE_DB = Path("cache/live/trading_live.db")
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def get_paper_trades(tickers, start, end):
    """end is a YYYY-MM-DD date; compared lexically against full timestamps, so it must be
    extended to end-of-day or every trade entered ON the end date is silently excluded
    ('2026-08-04 14:30:16' > '2026-08-04' lexically)."""
    con = sqlite3.connect(LIVE_DB)
    q = """
        SELECT ticker, entry_time, exit_time, pnl_pct, exit_reason
        FROM paper_trade_log
        WHERE entry_time >= ? AND entry_time <= ?
    """
    params = [start, f"{end} 23:59:59"]
    if tickers:
        q += f" AND ticker IN ({','.join('?' * len(tickers))})"
        params += tickers
    rows = con.execute(q, params).fetchall()
    con.close()
    return pd.DataFrame(rows, columns=["ticker", "entry_time", "exit_time", "pnl_pct", "exit_reason"])


def get_backtest_trades_in_window(node, start, end):
    """end is a YYYY-MM-DD date; pd.Timestamp(end) is midnight, so extend by a day or every
    trade signaling ON the end date is silently excluded -- same class of bug as
    get_paper_trades' date-string truncation."""
    trades, df_h = get_trades_and_bars(node)
    out = []
    end_bound = pd.Timestamp(end) + pd.Timedelta(days=1)
    for t in trades:
        if t["signal_i"] is None:
            continue
        et = df_h.index[t["signal_i"]]
        if pd.Timestamp(start) <= et <= end_bound:
            out.append({"entry_time": et, "ret": t["ret"], "result": t["result"]})
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", nargs="*", default=None)
    parser.add_argument("--watchlist-id", type=int, default=65)
    parser.add_argument("--start", default="2026-07-22")
    parser.add_argument("--end", default=None, help="Default: today")
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()
    end = args.end or pd.Timestamp.now().strftime("%Y-%m-%d")

    paper = get_paper_trades(args.tickers, args.start, end)
    if paper.empty:
        print("No paper trades in window.")
        return
    tickers = args.tickers or sorted(paper["ticker"].unique())

    nodes = {n["ticker"]: n for n in load_nodes(args.watchlist_id, tickers)}
    rows = []
    for t in tickers:
        if t not in nodes:
            print(f"{t}: no research-mode node on watchlist {args.watchlist_id}, skipping")
            continue
        bt_trades = get_backtest_trades_in_window(nodes[t], args.start, end)
        p = paper[paper["ticker"] == t]
        p_closed = p.dropna(subset=["pnl_pct"])

        bt_rets = [tr["ret"] for tr in bt_trades]
        p_rets = (p_closed["pnl_pct"] / 100.0).tolist()

        bt_win = sum(1 for r in bt_rets if r > 0)
        p_win = sum(1 for r in p_rets if r > 0)
        bt_comp = float(np.prod([1 + r for r in bt_rets]) - 1) if bt_rets else float("nan")
        p_comp = float(np.prod([1 + r for r in p_rets]) - 1) if p_rets else float("nan")

        rows.append({
            "ticker": t,
            "backtest_n": len(bt_rets), "backtest_win_rate": bt_win / len(bt_rets) if bt_rets else float("nan"),
            "backtest_compounded_pct": bt_comp * 100 if bt_rets else float("nan"),
            "paper_n": len(p_rets), "paper_win_rate": p_win / len(p_rets) if p_rets else float("nan"),
            "paper_compounded_pct": p_comp * 100 if p_rets else float("nan"),
            "paper_still_open": len(p) - len(p_closed),
        })

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 160)
    print(f"Reconciliation window: {args.start} to {end}\n")
    print(df.round(2).to_string(index=False))

    if args.csv:
        OUTPUT_DIR.mkdir(exist_ok=True)
        df.to_csv(OUTPUT_DIR / "paper_vs_backtest_reconcile.csv", index=False)
        print(f"\nWrote {OUTPUT_DIR / 'paper_vs_backtest_reconcile.csv'}")

    print("\n--- Divergence flags (backtest and paper disagree on direction, or trade-count gap >2) ---")
    print("(NOTE: backtest_n excludes a currently-open backtest position -- get_trades_and_bars filters "
          "result==OPEN upstream -- while paper_still_open counts paper's open position. Comparing "
          "backtest_n against paper_n+paper_still_open is asymmetric in the exact direction of 'paper "
          "fires more' (found by Opus review 2026-08-04 very late). Trade-count-gap flag below compares "
          "closed-vs-closed only; paper_still_open is reported for visibility, not counted in the gap.)")
    for _, r in df.iterrows():
        flags = []
        if pd.notna(r["backtest_compounded_pct"]) and pd.notna(r["paper_compounded_pct"]):
            if (r["backtest_compounded_pct"] > 0) != (r["paper_compounded_pct"] > 0):
                flags.append("direction mismatch (backtest and paper disagree on net win/loss for this window)")
        if abs(r["backtest_n"] - r["paper_n"]) > 2:
            flags.append(f"trade-count gap, closed trades only (backtest={r['backtest_n']}, paper={r['paper_n']}, "
                          f"paper also has {r['paper_still_open']} still open)")
        if r["backtest_n"] == 0 and r["paper_n"] > 0:
            flags.append("backtest shows zero closed signals in window but paper closed trades")
        if flags:
            print(f"{r['ticker']}: {'; '.join(flags)}")


if __name__ == "__main__":
    main()
