"""Full candidate review in one pass: everything candidate_summary_report.py
already computes (cliff-safety, AnnExcess%, 5-min fill accuracy, liquidity,
addon/drought overlay summary) PLUS two things that previously needed a
separate manual invocation per ticker/mechanism:

1. Raw strategy CAGR (not just AnnExcess% over SPY) -- same cagr() formula
   annualized_alpha_report.py uses, applied to the node's own abs_return_pct
   instead of discarding it after computing the SPY-relative excess.
2. Overlay robustness (docs/overlay_parameter_robustness_process.md steps 1
   and 3, same logic as candidate_overlay_robustness_check.py, reused
   inline instead of requiring a separate per-ticker/per-mechanism run):
   chronological split (are both halves positive?) and single-trade-removal
   (does the sign flip without the single biggest winner?).
3. CORE node out-of-sample walk-forward (5-fold by default, reusing
   walk_forward_check.py's own walk_forward()/summarize() and
   train_test_split_check.py's period_spy_bh() directly against THIS row's
   exact node params via run_backtest_dispatch, not just the old v4/
   TrailingBoth-only best_v4_node() path) -- previously only available as a
   separate per-ticker script run, and only ever checked the v4 winning
   node, never an arbitrary v5 safe/unsafe/possible/certain candidate.
   TrailingExitZScoreBreakout nodes only get the single 'possible'
   resolution (run_backtest_v18 has no pessimistic/certain bounds support,
   unlike TrailingBoth's run_backtest_v110) -- reported fold alpha for
   those rows is possible-only, not the 3-way robust minimum.

Built 2026-08-09 after repeatedly being asked to run candidate_summary_report.py,
then annualized_alpha_report.py for raw CAGR, then candidate_overlay_robustness_check.py
per ticker/mechanism as separate steps -- one script, one pass, per the user's
explicit "make 1 script and put it all together" call.

Usage:
  .venv/bin/python scripts/candidate_full_review.py                # all registered candidate_nodes tickers
  .venv/bin/python scripts/candidate_full_review.py SOXL AGQ FAS    # specific tickers
  .venv/bin/python scripts/candidate_full_review.py --skip-5min     # faster, skip yfinance calls
  .venv/bin/python scripts/candidate_full_review.py --csv full_review
"""
import argparse
import csv
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import yfinance as yf

from annualized_alpha_report import cagr
from candidate_summary_report import (
    DB_PATH, build_rows_for_ticker, _row_to_record, best_node_strategy, load_ticker_df,
    CANDIDATE_LABELS,
)
from candidate_5min_report import best_safe_node, _node_from_row, _node_key
from run_overlay_shim import ensure_candidate_nodes_table, ensure_table as ensure_overlay_table
from walk_forward_check import walk_forward, summarize as summarize_folds
from train_test_split_check import period_spy_bh
from run_optimization_sweep import _load_node_inputs, CACHE_DIR
from backtester import run_backtest_dispatch, run_backtest_v110
from check_stock_splits import check_ticker as _check_splits
from v4_max_drawdown import max_drawdown
from run_optimization_sweep import _summarize_trades
from verify_trailing_sell_resolution import find_hourly_trailing_exits, replay_five_min, FIVE_MIN_LOOKBACK_DAYS
import strategies as _strategies
from datetime import datetime as _dt, timedelta as _timedelta

STRATEGY_CLASSES = {
    "TrailingBothZScoreBreakout": _strategies.TrailingBothZScoreBreakout,
    "TrailingExitZScoreBreakout": _strategies.TrailingExitZScoreBreakout,
}
DEFAULT_FOLDS = 5


def exit_fill_accuracy_summary(ticker, strategy, node):
    """Checklist item 3: trailing-SELL resolution check, the exit-side mirror
    of item 2's entry-side fillacc columns (already reused as-is from
    candidate_summary_report.py). TrailingBothZScoreBreakout only -- exit
    logic for TrailingExitZScoreBreakout is a plain market-sell, no trailing-
    stop resolution to check. Real yfinance 5-min replay, same
    FIVE_MIN_LOOKBACK_DAYS window as the entry-side check, so this only ever
    covers recently-armed trades -- can legitimately be (win%, err%, n=0) for
    a node that hasn't armed in the last ~58 days."""
    if strategy != "TrailingBothZScoreBreakout":
        return None
    cutoff = _dt.now() - _timedelta(days=FIVE_MIN_LOOKBACK_DAYS)
    try:
        events = find_hourly_trailing_exits(
            ticker, node["window"], node["z"], node["trail_buy_pct"] / 100.0,
            node["trail_sell_pct"] / 100.0, node["arm_pct"] / 100.0, node["hold"], cutoff)
    except Exception:
        return None
    if not events:
        return (None, None, 0)
    try:
        df_5m = yf.Ticker(ticker).history(period=f"{FIVE_MIN_LOOKBACK_DAYS}d", interval="5m")
        if df_5m.index.tz is not None:
            df_5m.index = df_5m.index.tz_localize(None)
    except Exception:
        return None
    diffs = []
    for ev in events:
        real = replay_five_min(df_5m, ev["arm_time"], ev["peak_at_arm"], node["trail_sell_pct"] / 100.0,
                                ev["cutoff_time"])
        if real is None:
            continue
        diffs.append((real["five_min_exit_price"] - ev["hourly_exit_price"]) / ev["hourly_exit_price"] * 100)
    if not diffs:
        return (None, None, 0)
    mean_abs_err = sum(abs(d) for d in diffs) / len(diffs)
    win_rate = sum(1 for d in diffs if abs(d) < 0.5) / len(diffs) * 100  # within noise, same convention as item 2
    return (win_rate, mean_abs_err, len(diffs))

_TREND_CACHE = {}
_SPLIT_CACHE = {}


def ticker_trend(ticker):
    """Checklist item 1: 30d/90d return, independent of the backtest's mean-
    reversion assumption -- a large sustained move means recent signals are
    fighting a real trend, not just chopping around a stable mean. Cached
    per-ticker (cheap CSV read, but no need to redo it per candidate row)."""
    if ticker in _TREND_CACHE:
        return _TREND_CACHE[ticker]
    path = CACHE_DIR / f"{ticker}_1h.csv"
    if not path.exists():
        _TREND_CACHE[ticker] = (None, None)
        return None, None
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    daily = df.resample("D").last().dropna(subset=[close_col])
    if len(daily) < 64:
        result = (None, None)
    else:
        r30 = (daily[close_col].iloc[-1] / daily[close_col].iloc[-21] - 1) * 100
        r90 = (daily[close_col].iloc[-1] / daily[close_col].iloc[-63] - 1) * 100
        result = (float(r30), float(r90))
    _TREND_CACHE[ticker] = result
    return result


def ticker_split_flag(ticker):
    """Checklist item 6: real yfinance-confirmed stock splits landing inside
    the ticker's cached date range -- a missed split silently corrupts the
    cached price series. Cached per-ticker (real network call)."""
    if ticker in _SPLIT_CACHE:
        return _SPLIT_CACHE[ticker]
    path = CACHE_DIR / f"{ticker}_1h.csv"
    if not path.exists():
        _SPLIT_CACHE[ticker] = None
        return None
    try:
        splits = _check_splits(path)
        result = "; ".join(f"{s['split_date']} ({s['ratio']}x)" for s in splits) if splits else "none"
    except Exception as e:
        result = f"check failed: {e}"
    _SPLIT_CACHE[ticker] = result
    return result


def candidate_type_membership(df_t, min_alpha):
    """Independently computes the four raw candidate-selection node keys
    (not deduped, unlike find_candidates()) so a collapsed row can be
    labeled with EVERY type that landed on it, not just whichever label
    find_candidates() happened to keep. Answers "which ones overlapped?"
    for any row with fewer than 4 distinct candidates for its ticker.

    Also returns raw_label -> full node dict (window/z/sl/arm_pct/
    trail_buy_pct/trail_sell_pct/hold/entry_timing, plus the real
    possible/pessimistic/certain alphas) for every candidate computed here --
    the SAME node objects `find_candidates()` itself would produce, so a row
    can look its own params up directly instead of round-tripping through a
    `WHERE robust_alpha=?` float-equality DB query (found 2026-08-09, paired
    review: that query could silently return no match after any
    backtest_cache recompute, or match the WRONG node on a robust_alpha tie
    between two grid plateaus -- this makes that whole lookup unnecessary)."""
    membership = {}  # node_key -> [label, ...]
    label_to_node = {}  # raw_label -> node dict

    cliff_safe = best_safe_node(df_t, min_alpha=min_alpha, metric="robust_alpha")
    if cliff_safe:
        cliff_safe['robust_alpha'] = cliff_safe.pop('alpha')
        membership.setdefault(_node_key(cliff_safe), []).append('best safe node')
        label_to_node['cliff-safe (current convention)'] = cliff_safe

    top_robust_row = df_t.sort_values("robust_alpha", ascending=False).iloc[0]
    top_robust = _node_from_row(top_robust_row)
    membership.setdefault(_node_key(top_robust), []).append('best unsafe node')
    label_to_node['best robust_alpha (ignoring cliff-safety)'] = top_robust

    top_possible_row = df_t.sort_values("alpha_vs_spy", ascending=False).iloc[0]
    top_possible = _node_from_row(top_possible_row)
    membership.setdefault(_node_key(top_possible), []).append('5min best possible')
    label_to_node['best possible (raw alpha_vs_spy)'] = top_possible

    df_cert = df_t.copy()
    df_cert["_alpha_certain_filled"] = df_cert["alpha_vs_spy_certain"].fillna(df_cert["alpha_vs_spy"])
    top_certain_row = df_cert.sort_values("_alpha_certain_filled", ascending=False).iloc[0]
    top_certain = _node_from_row(top_certain_row)
    membership.setdefault(_node_key(top_certain), []).append('best certain')
    label_to_node['best certain (alpha_vs_spy_certain, no-guessing resolution)'] = top_certain

    return membership, label_to_node


def compounded(rets):
    prod = 1.0
    for r in rets:
        prod *= (1 + r)
    return (prod - 1) * 100


def win_rate(rets):
    if not rets:
        return 0.0
    return sum(1 for r in rets if r > 0) / len(rets) * 100


def overlay_robustness(conn, ticker, strategy, version, mechanism, node):
    """Looks up the exact candidate_node_id for this row's params (same match
    as candidate_summary_report.overlay_summary_for_node), pulls its
    chronologically-ordered trades, and runs the two mechanical robustness
    checks from docs/overlay_parameter_robustness_process.md that apply to
    an already-generated trade list (steps 1 and 3 -- no fit-half parameter
    search here, since drought/addon use a fixed generic config via
    run_overlay_shim.py, not a per-candidate search).

    Returns None if <2 trades (nothing to split/stress-test), else a dict:
      {half1_pct, half2_pct, flips_sign, verdict}
    verdict is one of: 'OK', 'FRAGILE (half negative)', 'FRAGILE (sign flip)'.
    """
    c = conn.cursor()
    c.execute("""
        SELECT cor.entry_time, cor.ret
        FROM candidate_overlay_results cor
        JOIN candidate_nodes cn ON cn.id = cor.candidate_node_id
        WHERE cn.ticker=? AND cor.mechanism=? AND cn.strategy=? AND cn.version=?
              AND cn.window=? AND cn.z=? AND cn.fixed_sl=? AND cn.arm_pct=?
              AND cn.trail_buy_pct=? AND cn.trail_sell_pct=? AND cn.max_hold_hours=? AND cn.entry_timing=?
        ORDER BY cor.entry_time
    """, (ticker, mechanism, strategy, version, node['window'], node['z'], node['sl'], node['arm_pct'],
          node['trail_buy_pct'], node['trail_sell_pct'], node['hold'], node['entry_timing']))
    trades = c.fetchall()
    if len(trades) < 2:
        return None

    rets = [t[1] for t in trades]
    mid = len(rets) // 2
    half1, half2 = rets[:mid], rets[mid:]
    half1_pct, half2_pct = compounded(half1), compounded(half2)

    comp_all = compounded(rets)
    biggest_idx = max(range(len(rets)), key=lambda i: rets[i])
    without_biggest = rets[:biggest_idx] + rets[biggest_idx + 1:]
    comp_without = compounded(without_biggest) if without_biggest else 0.0
    flips = (comp_all > 0) != (comp_without > 0)

    if flips:
        verdict = "FRAGILE (sign flip)"
    elif half1_pct < 0 or half2_pct < 0:
        verdict = "FRAGILE (half negative)"
    else:
        verdict = "OK"

    return {"half1_pct": half1_pct, "half2_pct": half2_pct, "flips_sign": flips, "verdict": verdict}


_INPUTS_CACHE = {}


def core_walk_forward(ticker, strategy, node, n_folds=DEFAULT_FOLDS):
    """N-fold out-of-time walk-forward for THIS row's exact core node, reusing
    walk_forward_check.py's walk_forward()/summarize() directly instead of
    reimplementing the fold-slicing/dispersion math. Returns None if this
    ticker/strategy has no cached hourly data. TrailingExitZScoreBreakout
    nodes only get the 'possible' resolution (see module docstring) -- passed
    to walk_forward() as pessimistic=None/certain=None, matching how it
    already handles a missing bound (only includes non-empty lists in the
    per-fold MIN)."""
    strategy_class = STRATEGY_CLASSES.get(strategy)
    if strategy_class is None:
        return None

    # window matters -- prep embeds the daily SMA/Std arrays built with THIS
    # window, so a cache keyed without it silently reuses one candidate's
    # indicators for another's walk-forward/drawdown/same-day-block checks
    # (found 2026-08-09, paired review: 15+ tickers have candidate rows
    # spanning window=10 and window=20, e.g. GDXD -- confirmed materially
    # different real numbers, not just noise).
    cache_key = (ticker, strategy, node["window"])
    inputs = _INPUTS_CACHE.get(cache_key)
    if inputs is None:
        loaded = _load_node_inputs(ticker, strategy_class, strategy, node["window"], node["z"])
        if loaded is None:
            _INPUTS_CACHE[cache_key] = False
            return None
        _, _, prep = loaded
        hourly_path = CACHE_DIR / f"{ticker}_1h.csv"
        df_t = pd.read_csv(hourly_path, index_col=0, parse_dates=True).sort_index()
        if df_t.index.tz is not None:
            df_t.index = df_t.index.tz_localize(None)
        inputs = (prep, df_t.index.min(), df_t.index.max())
        _INPUTS_CACHE[cache_key] = inputs
    if inputs is False:
        return None
    prep, data_start, data_end = inputs

    return_bounds = strategy == "TrailingBothZScoreBreakout"
    result = run_backtest_dispatch(
        strategy_class, df_hourly=None, df_daily_indicators=None, ticker=ticker,
        take_profit=node["arm_pct"], sl_raw=node["trail_buy_pct"] if return_bounds else node["trail_sell_pct"],
        max_hours_to_hold=node["hold"], z_score_threshold=node["z"],
        fixed_sl=node["sl"], trail_pct_pct=node["trail_sell_pct"],
        entry_timing=node["entry_timing"], return_bounds=return_bounds, prep=prep,
    )
    if return_bounds:
        t_base, p_base, c_base = result
    else:
        t_base, p_base, c_base = result, None, None

    fold_rows = walk_forward(t_base, p_base, c_base, data_start, data_end, n_folds)
    if fold_rows is None:
        return None
    summary = summarize_folds(fold_rows)
    if summary.get("folds_with_trades", 0) == 0:
        return None
    positive_folds = summary["folds_with_trades"] - summary["negative_folds"]
    fwt = summary["folds_with_trades"]
    # Indicative only, not a hard gate -- PASS/MARGINAL/FAIL describe fold
    # consistency the same plain-language way `status` (SAFE/CLIFF) already
    # does for cliff-safety, per the user's explicit "these are indicative,
    # some are harder stops than others" framing (2026-08-09). A ticker with
    # fewer folds actually carrying trades than requested (fwt < n_folds) is
    # flagged THIN regardless of the win/loss split -- too little data in
    # some windows to trust the split either way.
    if fwt < n_folds:
        verdict = "THIN (some folds had no trades)"
    elif positive_folds == fwt:
        verdict = "PASS (all folds positive)"
    elif positive_folds >= (fwt / 2):
        verdict = f"MARGINAL ({positive_folds}/{fwt} folds positive)"
    else:
        verdict = f"FAIL ({positive_folds}/{fwt} folds positive)"
    # Checklist items 11/12: true peak-to-trough max drawdown across the
    # node's full compounded equity curve, plus where it sits RIGHT NOW
    # relative to that worst case -- reuses v4_max_drawdown.py's own
    # max_drawdown() against the SAME trade list walk_forward already
    # computed above, no extra kernel run.
    closed = [t for t in t_base if t["Result"] in ("WIN", "LOSS", "TWIN", "TLOSS")]
    max_dd_pct, _, _ = max_drawdown(closed) if closed else (None, None, None)

    # Checklist items 9/10: how much of this node's edge depends on capital a
    # real cash-account same-day-re-buy rule (schwab_safety.py's same-day-
    # block, enforced live) would actually block. TrailingBoth-only -- the
    # checklist itself scopes this there, and run_backtest_v18 (TrailingExit)
    # has no same_day_block parameter to even test. One extra kernel run
    # (blocked variant), reusing the same prep/node params as the unblocked
    # run above; #10 (stability) reuses the SAME two trade lists, chronologically
    # split, rather than a third kernel run.
    same_day_block_check = None
    if strategy == "TrailingBothZScoreBreakout" and closed:
        # return_bounds=True so this compares ROBUST alpha (MIN of possible/
        # pessimistic/certain) on both sides, matching the checklist's own
        # explicit instruction ("compare both trade count and robust alpha,
        # not just one or the other") -- an earlier version compared
        # possible-only, found wrong in paired review 2026-08-09.
        t_block, p_block, c_block = run_backtest_v110(
            df_hourly=None, df_daily_indicators=None, ticker=ticker,
            take_profit=node["arm_pct"] / 100.0, stop_loss=node["sl"] / 100.0,
            max_hours_to_hold=node["hold"], z_score_threshold=node["z"],
            trail_buy_pct=node["trail_buy_pct"] / 100.0, trail_pct=node["trail_sell_pct"] / 100.0,
            entry_timing=node["entry_timing"], return_bounds=True, prep=prep, same_day_block=True,
        )
        closed_block = [t for t in t_block if t["Result"] in ("WIN", "LOSS", "TWIN", "TLOSS")]
        if closed_block:
            closed_pess = [t for t in p_base if t["Result"] in ("WIN", "LOSS", "TWIN", "TLOSS")] if p_base else []
            closed_cert = [t for t in c_base if t["Result"] in ("WIN", "LOSS", "TWIN", "TLOSS")] if c_base else []
            closed_block_pess = [t for t in p_block if t["Result"] in ("WIN", "LOSS", "TWIN", "TLOSS")] if p_block else []
            closed_block_cert = [t for t in c_block if t["Result"] in ("WIN", "LOSS", "TWIN", "TLOSS")] if c_block else []

            def _robust_alpha(base, pess, cert, spy_bh):
                if not base:
                    return None
                alphas = [_summarize_trades(base, spy_bh)[0]]
                if pess:
                    alphas.append(_summarize_trades(pess, spy_bh)[0])
                if cert:
                    alphas.append(_summarize_trades(cert, spy_bh)[0])
                return min(alphas)

            spy_bh_full = period_spy_bh(data_start, data_end)
            alpha_unblocked = _robust_alpha(closed, closed_pess, closed_cert, spy_bh_full)
            alpha_blocked = _robust_alpha(closed_block, closed_block_pess, closed_block_cert, spy_bh_full)
            n_unblocked, n_blocked = len(closed), len(closed_block)
            trade_retention_pct = (n_blocked / n_unblocked * 100) if n_unblocked else None
            # Only a meaningful ratio when the unblocked baseline is a real
            # positive edge -- with a negative denominator the ratio's sign
            # inverts (a blocked variant that's LESS bad would read as "low
            # retention", the opposite of what the number is meant to convey).
            # Raw before/after values are always exposed too so the sign is
            # visible even when the ratio itself is withheld (found in review).
            alpha_retention_pct = ((alpha_blocked / alpha_unblocked * 100)
                                    if (alpha_unblocked is not None and alpha_unblocked > 0
                                        and alpha_blocked is not None) else None)

            # Stability (#10): split at ONE shared calendar date (the
            # unblocked run's own 70th-percentile trade's entry time) so both
            # variants are compared over the SAME real time window -- an
            # earlier version cut each list at its own 70%-of-COUNT index,
            # which lands at two different dates once same-day-block removes
            # trades non-uniformly in time (found in review). Per-half SPY
            # benchmark, not the full-window constant, matching
            # train_test_split_check.py's own established convention.
            def _filter_period(trades, start, end):
                return [t for t in trades if start <= t["Entry Time"] < end]

            cut_idx = max(0, min(len(closed) - 1, int(len(closed) * 0.7)))
            cut_time = closed[cut_idx]["Entry Time"] if closed else None
            retention_early = retention_late = None
            if cut_time is not None:
                spy_early = period_spy_bh(data_start, cut_time)
                spy_late = period_spy_bh(cut_time, data_end)
                a_u_early = _robust_alpha(_filter_period(closed, data_start, cut_time),
                                           _filter_period(closed_pess, data_start, cut_time),
                                           _filter_period(closed_cert, data_start, cut_time), spy_early)
                a_b_early = _robust_alpha(_filter_period(closed_block, data_start, cut_time),
                                           _filter_period(closed_block_pess, data_start, cut_time),
                                           _filter_period(closed_block_cert, data_start, cut_time), spy_early)
                a_u_late = _robust_alpha(_filter_period(closed, cut_time, data_end),
                                          _filter_period(closed_pess, cut_time, data_end),
                                          _filter_period(closed_cert, cut_time, data_end), spy_late)
                a_b_late = _robust_alpha(_filter_period(closed_block, cut_time, data_end),
                                          _filter_period(closed_block_pess, cut_time, data_end),
                                          _filter_period(closed_block_cert, cut_time, data_end), spy_late)
                if a_u_early is not None and a_u_early > 0:
                    retention_early = (a_b_early / a_u_early * 100) if a_b_early is not None else 0.0
                if a_u_late is not None and a_u_late > 0:
                    retention_late = (a_b_late / a_u_late * 100) if a_b_late is not None else 0.0

            same_day_block_check = {
                "trade_retention_pct": trade_retention_pct,
                "alpha_retention_pct": alpha_retention_pct,
                "alpha_unblocked_pct": alpha_unblocked,
                "alpha_blocked_pct": alpha_blocked,
                "retention_early_pct": retention_early,
                "retention_late_pct": retention_late,
            }

    current_dd_pct = None
    if closed:
        equity, peak = 100.0, 100.0
        for t in closed:
            equity *= (1.0 + t["Return"])
            peak = max(peak, equity)
        current_dd_pct = (equity - peak) / peak * 100.0

    return {
        "folds_with_trades": fwt,
        "positive_folds": positive_folds,
        "total_folds": n_folds,
        "min_fold_alpha": summary["min_fold_alpha"],
        "max_fold_alpha": summary["max_fold_alpha"],
        "mean_fold_alpha": summary["mean_fold_alpha"],
        "verdict": verdict,
        "max_drawdown_pct": max_dd_pct,
        "current_drawdown_pct": current_dd_pct,
        "same_day_block_check": same_day_block_check,
    }


# K-1/UBTI tax-filer status -- ONLY reflects tickers actually confirmed via a
# real source check (issuer tax-document page), per this project's standing
# "confirmed via real search, not assumed" convention (see docs/research_log.md's
# 2026-08-07 tax/compliance entry, TQQQ 2026-08-04 entry). Every ticker not
# listed here is genuinely unchecked -- default to "not checked", never guess
# from structural resemblance to a confirmed ticker. Add a row here only after
# a real source check, with the source noted.
K1_STATUS = {
    "AGQ": "CONFIRMED K-1 (proshares.com tax-and-filing-documents)",
    "USO": "CONFIRMED K-1 (uscfinvestments.com/tax-information)",
    "UCO": "CONFIRMED K-1 (same ProShares commodity-pool family as USO)",
    "SCO": "CONFIRMED K-1 (same ProShares commodity-pool family as USO)",
    "ZSL": "CONFIRMED K-1 (same ProShares silver-futures family as AGQ)",
    "UVIX": "CONFIRMED K-1 (VIX-futures-based, taxpackagesupport.com/volatility_uvix)",
    "SOXL": "confirmed clean, standard 1099 (direxion.com FAQ)",
    "NUGT": "confirmed clean, standard 1099 (direxion.com FAQ)",
    "TQQQ": "confirmed clean, standard 1099 (RIC, not a commodity/currency/volatility product)",
    "GDXU": "not K-1, but a BMO-issued ETN (unsecured issuer/counterparty credit risk) -- bmoetns.com",
    "GDXD": "not K-1, but a BMO-issued ETN (unsecured issuer/counterparty credit risk) -- bmoetns.com",
}


def k1_status(ticker):
    return K1_STATUS.get(ticker, "not checked")


def ticker_sector(conn, ticker):
    """Real description/leverage/inverse straight from the tickers table --
    not a hand-typed lookup."""
    c = conn.cursor()
    c.execute("SELECT description, leverage, inverse FROM tickers WHERE symbol=?", (ticker,))
    row = c.fetchone()
    if not row:
        return None
    desc, leverage, inverse = row
    lev_str = f"{leverage:g}x" if leverage else ""
    inv_str = " inverse" if inverse else ""
    return f"{desc} ({lev_str}{inv_str})" if desc else None


def _bucket(value, edges, labels):
    """edges is a sorted list of upper bounds; labels has len(edges)+1 entries."""
    if value is None:
        return None
    for edge, label in zip(edges, labels):
        if value < edge:
            return label
    return labels[-1]


def add_tranches(rec):
    """Adds clean, pivot-friendly categorical columns for every continuous/
    free-text criterion already in `rec` -- per the user's explicit 2026-08-09
    call: 'give me a summary column for each criteria I can use in a pivot
    table... create tranches for anything not immediately pass/fail.' Mutates
    and returns rec. Every _tranche value is indicative only, not a hard gate
    (some of these are harder stops than others -- left to the user to decide
    which, via filtering the sheet themselves)."""
    rec["years_tranche"] = _bucket(rec.get("years"), [1, 2, 3], ["<1y", "1-2y", "2-3y", "3y+"])
    rec["trades_tranche"] = _bucket(rec.get("trades"), [10, 25, 50, 100],
                                     ["<10", "10-25", "25-50", "50-100", "100+"])
    rec["liquidity_tranche"] = _bucket(rec.get("liquidity_dollars_per_day"), [50_000, 100_000, 500_000, 1_000_000],
                                        ["<$50k", "$50k-100k", "$100k-500k", "$500k-1M", "$1M+"])
    cagr_val = rec.get("strategy_cagr_pct")
    rec["cagr_tranche"] = ("negative" if cagr_val is not None and cagr_val < 0 else
                            _bucket(cagr_val, [50, 100, 200], ["0-50%", "50-100%", "100-200%", "200%+"]))
    rec["cliff_tranche"] = rec.get("status")  # SAFE/CLIFF -- clean alias, consistent _tranche naming

    wf = rec.get("walk_forward")
    if not wf:
        rec["wf_tranche"] = "NONE"
    elif wf["verdict"].startswith("THIN"):
        rec["wf_tranche"] = "THIN"
    elif wf["verdict"].startswith("PASS"):
        rec["wf_tranche"] = "PASS"
    elif wf["verdict"].startswith("MARGINAL"):
        rec["wf_tranche"] = "MARGINAL"
    else:
        rec["wf_tranche"] = "FAIL"

    def _overlay_tranche(key):
        r = rec.get(key)
        if not r:
            return "NONE"
        return "OK" if r["verdict"] == "OK" else "FRAGILE"
    rec["addon_tranche"] = _overlay_tranche("addon_robustness")
    rec["drought_tranche"] = _overlay_tranche("drought_robustness")

    k1 = rec.get("k1_status", "not checked")
    if k1.startswith("CONFIRMED K-1"):
        rec["k1_tranche"] = "K1_CONFIRMED"
    elif k1.startswith("confirmed clean"):
        rec["k1_tranche"] = "CLEAN_CONFIRMED"
    elif "ETN" in k1:
        rec["k1_tranche"] = "ETN_NOT_K1"
    else:
        rec["k1_tranche"] = "NOT_CHECKED"

    poss = rec.get("alpha_possible_pct")
    cert = rec.get("alpha_certain_pct")
    if poss is None or cert is None or poss == 0:
        rec["resolution_spread_tranche"] = None
    else:
        spread_pct = abs(cert - poss) / abs(poss) * 100
        rec["resolution_spread_tranche"] = _bucket(spread_pct, [20, 100], ["TIGHT", "MODERATE", "WIDE"])

    wf_rec = rec.get("walk_forward")

    dd = wf_rec.get("max_drawdown_pct") if wf_rec else None
    rec["drawdown_tranche"] = _bucket(-dd if dd is not None else None, [10, 25, 50],
                                       ["<10%", "10-25%", "25-50%", "50%+"]) if dd is not None else None

    t30 = rec.get("trend_30d_pct")
    if t30 is None:
        rec["trend_tranche"] = None
    elif t30 >= 20:
        rec["trend_tranche"] = "STRONG_UP"
    elif t30 >= 5:
        rec["trend_tranche"] = "MILD_UP"
    elif t30 > -5:
        rec["trend_tranche"] = "FLAT"
    elif t30 > -20:
        rec["trend_tranche"] = "MILD_DOWN"
    else:
        rec["trend_tranche"] = "STRONG_DOWN"

    sf = rec.get("split_flag")
    rec["split_tranche"] = None if sf is None else ("NONE" if sf == "none" else "FLAGGED")

    sdb_rec = wf_rec.get("same_day_block_check") if wf_rec else None
    sdb_alpha_ret = sdb_rec.get("alpha_retention_pct") if sdb_rec else None
    if sdb_alpha_ret is None:
        rec["sdb_tranche"] = "N/A (TrailingExit)" if rec.get("strategy") == "TrailingExitZScoreBreakout" else None
    else:
        rec["sdb_tranche"] = _bucket(sdb_alpha_ret, [25, 75], ["LOW_RETENTION", "MODERATE_RETENTION",
                                                                "HIGH_RETENTION"])
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--version", default="v5")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--min-alpha", type=float, default=0,
                     help="Alpha floor for the 'best safe node' cliff-safety search (default 0 -- show "
                          "every cliff-safe node regardless of magnitude, not just the >=200%% convention).")
    ap.add_argument("--skip-5min", action="store_true")
    ap.add_argument("--skip-overlay", action="store_true")
    ap.add_argument("--skip-walkforward", action="store_true",
                     help="skip walk-forward AND everything computed alongside it in the same kernel "
                          "run -- max/current drawdown, same-day-block sensitivity/stability (checklist "
                          "items 9-13 all go blank, not just 13; slower otherwise -- runs the real numba "
                          "kernel per row, not just a DB lookup)")
    ap.add_argument("--folds", type=int, default=DEFAULT_FOLDS)
    ap.add_argument("--skip-trend", action="store_true", help="skip the 30d/90d macro-trend check")
    ap.add_argument("--skip-splits", action="store_true",
                     help="skip the stock-split check (yfinance network call per ticker)")
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    ensure_candidate_nodes_table(conn)
    ensure_overlay_table(conn)
    tickers = args.tickers
    if not tickers:
        c = conn.cursor()
        c.execute("SELECT DISTINCT ticker FROM candidate_nodes")
        tickers = [r[0] for r in c.fetchall()]

    label_to_clean = CANDIDATE_LABELS  # raw find_candidates() label -> clean UI label
    clean_to_label = {v: k for k, v in label_to_clean.items()}

    out_rows = []
    for ticker in tickers:
        best = best_node_strategy(conn, ticker, args.version)
        membership = {}
        label_to_node = {}
        if best is not None:
            strategy0, _ = best
            df_t = load_ticker_df(conn, ticker, args.version, strategy0)
            if not df_t.empty:
                membership, label_to_node = candidate_type_membership(df_t, args.min_alpha)

        trend_30d, trend_90d = (None, None) if args.skip_trend else ticker_trend(ticker)
        split_flag = None if args.skip_splits else ticker_split_flag(ticker)

        for row in build_rows_for_ticker(conn, ticker, args.version, args.min_alpha, args.skip_5min,
                                          args.skip_overlay):
            if row[1] is None:
                out_rows.append({"ticker": ticker, "no_data": True})
                continue
            rec = _row_to_record(row)
            years = rec["years"]
            strat_cagr = cagr(rec["abs_return_pct"], years * 365.25) if years else None
            rec["strategy_cagr_pct"] = strat_cagr
            rec["sector"] = ticker_sector(conn, ticker)
            rec["k1_status"] = k1_status(ticker)
            rec["trend_30d_pct"] = trend_30d
            rec["trend_90d_pct"] = trend_90d
            rec["split_flag"] = split_flag

            (t, candidate_type, strategy, ralpha, sret, yrs, trades, ann_excess, fill_acc, wn, cliff,
             addon, drought, addon_mult, drought_mult, liquidity) = row
            # Look this row's exact node params up directly from label_to_node
            # (computed fresh, same session, same function find_candidates()
            # itself uses) instead of round-tripping through a DB query keyed
            # on `robust_alpha` float equality -- that query could silently
            # return no match after any backtest_cache recompute, or match a
            # DIFFERENT node on a robust_alpha tie between two grid plateaus
            # (found 2026-08-09, paired review). Also means the three raw
            # fill-resolution alphas (alpha_raw/alpha_pessimistic/alpha_certain
            # -- already present on every node dict find_candidates() produces)
            # no longer need a second DB query either.
            raw_label = clean_to_label.get(candidate_type)
            node = label_to_node.get(raw_label) if raw_label else None
            if node:
                rec["addon_robustness"] = overlay_robustness(conn, ticker, strategy, args.version, "addon", node)
                rec["drought_robustness"] = overlay_robustness(conn, ticker, strategy, args.version, "drought", node)
                node_key = _node_key(node)
                rec["also_matches"] = membership.get(node_key, [candidate_type])
                rec["alpha_possible_pct"] = node["alpha_raw"]
                rec["alpha_pessimistic_pct"] = (node["alpha_pessimistic"] if node["alpha_pessimistic"] is not None
                                                 else node["alpha_raw"])
                rec["alpha_certain_pct"] = (node["alpha_certain"] if node["alpha_certain"] is not None
                                             else node["alpha_raw"])
                rec["walk_forward"] = None if args.skip_walkforward else core_walk_forward(
                    ticker, strategy, node, args.folds)
                rec["exit_fill_acc"] = None if args.skip_5min else exit_fill_accuracy_summary(
                    ticker, strategy, node)
            else:
                rec["addon_robustness"] = None
                rec["drought_robustness"] = None
                rec["also_matches"] = [candidate_type]
                rec["alpha_possible_pct"] = rec["alpha_pessimistic_pct"] = rec["alpha_certain_pct"] = None
                rec["walk_forward"] = None
                rec["exit_fill_acc"] = None
            add_tranches(rec)
            out_rows.append(rec)

    conn.close()

    if args.csv:
        out_path = Path("output") / (args.csv if args.csv.endswith(".csv") else f"{args.csv}.csv")
        out_path.parent.mkdir(exist_ok=True)
        fieldnames = ["ticker", "sector", "k1_status", "k1_tranche", "candidate_type", "also_matches", "strategy",
                      "liquidity_dollars_per_day", "liquidity_tranche",
                      "core_alpha_pct", "abs_return_pct", "strategy_cagr_pct", "cagr_tranche", "ann_excess_pct",
                      "alpha_possible_pct", "alpha_pessimistic_pct", "alpha_certain_pct", "resolution_spread_tranche",
                      "years", "years_tranche", "trades", "trades_tranche",
                      "worst_neighbor_pct", "status", "cliff_tranche",
                      "fillacc_possible_win_pct", "fillacc_possible_mean_err_pct", "fillacc_n",
                      "addon_n", "addon_compounded_pct", "addon_win_rate_pct", "addon_robustness_verdict",
                      "addon_tranche",
                      "drought_n", "drought_compounded_pct", "drought_win_rate_pct", "drought_robustness_verdict",
                      "drought_tranche",
                      "wf_verdict", "wf_tranche", "wf_positive_folds", "wf_total_folds", "wf_min_fold_alpha",
                      "wf_max_fold_alpha", "wf_mean_fold_alpha",
                      "max_drawdown_pct", "current_drawdown_pct", "trend_30d_pct", "trend_90d_pct", "split_flag",
                      "sdb_trade_retention_pct", "sdb_alpha_retention_pct",
                      "sdb_alpha_unblocked_pct", "sdb_alpha_blocked_pct",
                      "sdb_retention_early_pct", "sdb_retention_late_pct",
                      "drawdown_tranche", "trend_tranche", "split_tranche", "sdb_tranche",
                      "x_addon_pct", "x_drought_pct",
                      "exit_fillacc_win_pct", "exit_fillacc_mean_err_pct", "exit_fillacc_n"]
        with open(out_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for rec in out_rows:
                if rec.get("no_data"):
                    w.writerow({"ticker": rec["ticker"]})
                    continue
                row = {k: rec.get(k) for k in fieldnames if k in rec}
                row["ticker"] = rec["ticker"]
                row["also_matches"] = " = ".join(rec.get("also_matches", [rec["candidate_type"]]))
                ar = rec.get("addon_robustness")
                dr = rec.get("drought_robustness")
                row["addon_robustness_verdict"] = ar["verdict"] if ar else None
                row["drought_robustness_verdict"] = dr["verdict"] if dr else None
                wf = rec.get("walk_forward")
                row["wf_verdict"] = wf["verdict"] if wf else None
                row["wf_positive_folds"] = wf["positive_folds"] if wf else None
                row["wf_total_folds"] = wf["total_folds"] if wf else None
                row["wf_min_fold_alpha"] = wf["min_fold_alpha"] if wf else None
                row["wf_max_fold_alpha"] = wf["max_fold_alpha"] if wf else None
                row["wf_mean_fold_alpha"] = wf["mean_fold_alpha"] if wf else None
                row["max_drawdown_pct"] = wf["max_drawdown_pct"] if wf else None
                row["current_drawdown_pct"] = wf["current_drawdown_pct"] if wf else None
                sdb = wf["same_day_block_check"] if wf else None
                row["sdb_trade_retention_pct"] = sdb["trade_retention_pct"] if sdb else None
                row["sdb_alpha_retention_pct"] = sdb["alpha_retention_pct"] if sdb else None
                row["sdb_alpha_unblocked_pct"] = sdb["alpha_unblocked_pct"] if sdb else None
                row["sdb_alpha_blocked_pct"] = sdb["alpha_blocked_pct"] if sdb else None
                row["sdb_retention_early_pct"] = sdb["retention_early_pct"] if sdb else None
                row["sdb_retention_late_pct"] = sdb["retention_late_pct"] if sdb else None
                efa = rec.get("exit_fill_acc")
                row["exit_fillacc_win_pct"] = efa[0] if efa else None
                row["exit_fillacc_mean_err_pct"] = efa[1] if efa else None
                row["exit_fillacc_n"] = efa[2] if efa else None
                w.writerow(row)
        print(f"Wrote {out_path} ({len(out_rows)} rows)")
        return

    hdr = ("%-8s %-20s %12s %8s %9s %9s %10s %6s %6s %6s %6s | %-28s | %-28s" % (
        "Ticker", "Candidate", "Liquidity$/d", "CAGR%", "AbsRet%", "AnnExcess%", "WorstNb%",
        "Years", "Trades", "Status", "Fill", "Addon(n,comp%,WR%,robust)", "Drought(n,comp%,WR%,robust)"))
    print(hdr)
    print("-" * len(hdr))
    for rec in out_rows:
        if rec.get("no_data"):
            print(f"{rec['ticker']:8} NO_DATA")
            continue
        liq = rec['liquidity_dollars_per_day']
        liq_str = f"${liq:,.0f}" if liq is not None else "n/a"
        cagr_str = f"{rec['strategy_cagr_pct']:+.1f}" if rec['strategy_cagr_pct'] is not None else "-"
        ae_str = f"{rec['ann_excess_pct']:+.1f}" if rec['ann_excess_pct'] is not None else "-"
        wn_str = f"{rec['worst_neighbor_pct']:+.1f}" if rec['worst_neighbor_pct'] is not None else "n/a"
        fill_str = "Y" if rec['fillacc_possible_win_pct'] is not None else "-"

        def overlay_str(n_key, comp_key, wr_key, robust_key):
            n = rec.get(n_key)
            if n is None:
                return "-"
            robust = rec.get(robust_key)
            v = robust["verdict"] if robust else "n/a"
            return f"{n},{rec[comp_key]:+.1f}%,{rec[wr_key]:.0f}%,{v}"

        ao_str = overlay_str("addon_n", "addon_compounded_pct", "addon_win_rate_pct", "addon_robustness")
        dr_str = overlay_str("drought_n", "drought_compounded_pct", "drought_win_rate_pct", "drought_robustness")
        also = rec.get("also_matches", [rec["candidate_type"]])
        type_label = rec['candidate_type'] if len(also) < 2 else " = ".join(also)

        print(f"{rec['ticker']:8} {type_label:<20} {liq_str:>12} {cagr_str:>8} "
              f"{rec['abs_return_pct']:>9.1f} {ae_str:>10} {wn_str:>10} {rec['years']!s:>6} {rec['trades']:>6} "
              f"{rec['status']:>6} {fill_str:>6} | {ao_str:<28} | {dr_str:<28}")


if __name__ == "__main__":
    main()
