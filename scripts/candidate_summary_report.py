"""Consolidated liquidity-screen candidate summary -- for each ticker, THREE
candidate rows (best safe node / best unsafe node / 5min best possible node,
see CANDIDATE_LABELS below), each with core alpha, raw return, ANNUALIZED
excess return (CAGR-based, fair across tickers with different cached-history
lengths), 5-min real-fill accuracy, worst neighbor (cliff-safety), liquidity,
and drought/add-on overlay results (only shown when that exact row's params
match a registered candidate_nodes entry -- overlay backtests are only ever
run against ONE specific node's params, not all three candidate types, so
showing the same overlay numbers on every row would misrepresent them as
validated for configs they were never tested against). Built 2026-08-08
after repeatedly rebuilding pieces of this table by hand in conversation --
consolidates locate_best_node.py's winner pick, top_safe_nodes.py's
neighbor-check logic, candidate_5min_report.py's 3-way candidate split,
annualized_alpha_report.py's CAGR calc, verify_fill_resolution_accuracy.py's
5-min replay, and candidate_overlay_results into one script. The canonical
"everything at a glance" candidate report -- keep adding to this rather than
spinning up another standalone comparison script, per the user's explicit
call 2026-08-08 (later). Row structure (3 rows/ticker, not 1) is also the
user's explicit call, same session -- "i would look at best unsafe first to
just make sure we're not missing anything."

Usage:
  .venv/bin/python scripts/candidate_summary_report.py TNA URTY SQQQ ...
  .venv/bin/python scripts/candidate_summary_report.py --all-swept
  .venv/bin/python scripts/candidate_summary_report.py TNA --skip-5min  # faster, skip yfinance calls
  .venv/bin/python scripts/candidate_summary_report.py TNA --xlsx report  # output/report.xlsx, 2 sheets

GT-kernel mode (2026-08-23, GT Phase 4): --kernel gt switches the ticker loop over to the
v6 GT pipeline (run_optimization_sweep.derive_phase25_candidates_ground_truth ->
build_candidate_report_ground_truth -> print_candidate_report_ground_truth) instead of the
legacy candidate_nodes/run_overlay_shim machinery above -- a genuinely different candidate
shape (up-to-9 island-derived candidates per real GT scope, not this file's fixed 3-5 named
candidate types), so it gets its own row schema/columns (GT_COLUMN_DEFS) rather than being
forced into COLUMN_DEFS's legacy-only column meanings. Still the same script/CLI/--csv/--xlsx
plumbing, per the standing "canonical report, don't spin up a new script" convention above.
  .venv/bin/python scripts/candidate_summary_report.py --kernel gt AGQ GDXU UGL WEBL
  .venv/bin/python scripts/candidate_summary_report.py --kernel gt --tranche 1
  .venv/bin/python scripts/candidate_summary_report.py --kernel gt --tranche 1 --xlsx gt_t1
"""
import argparse
import csv
import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from top_safe_nodes import CLIFF_RADIUS, best_safe_node
from annualized_alpha_report import calendar_days, cagr
from locate_best_node import resolve_version
# verify_fill_resolution_accuracy (deliberately NOT imported at module level,
# found 2026-08-28): it imports replay_five_min/FIVE_MIN_LOOKBACK_DAYS from
# scripts/verify_trailing_buy_resolution.py, which no longer exist there --
# renamed to replay_one_min in commit 2a9f3d3 ("Full v6 promotion"), and this
# file's own import was never updated to match. A real, pre-existing bug
# (unrelated to this session's own work, filed to docs/backlog_cache.md, not
# fixed here -- fixing it properly means deciding new FIVE_MIN_LOOKBACK_DAYS-
# equivalent semantics for 1-min-granularity data, a real judgment call
# beyond this file's scope). Was a hard, unconditional import-time crash for
# EVERY invocation of this script, including --skip-5min runs that never
# actually call fill_accuracy_for_node -- moved to a lazy import inside
# fill_accuracy_summary (its only call site) so --skip-5min's own documented
# purpose ("skip yfinance calls") actually works again until that deeper
# drift gets a real fix.
from candidate_5min_report import find_candidates
from run_overlay_shim import (
    run_for_node as run_overlay_for_node, ensure_candidate_nodes_table, ensure_table as ensure_overlay_table,
)
from datetime import datetime as _datetime

# run_optimization_sweep/campaign_config/prune_backtest_cache_ground_truth (GT-mode-only
# deps) are deliberately NOT imported at module level -- run_optimization_sweep.py runs
# logging.basicConfig(handlers=[FileHandler(...), StreamHandler(sys.stdout)]) at import
# time (its own module top), which would otherwise fire on every invocation of THIS file,
# including plain --kernel legacy runs that never touch GT at all, and could interleave
# root INFO logging with the legacy fixed-width terminal table. Imported lazily inside
# run_gt_mode() instead (paired-review finding, 2026-08-23, against commit 7ec4663).

DB_PATH = "cache/research/trading_universe.db"
ROBUST_ALPHA_SQL = ("MIN(alpha_vs_spy, COALESCE(alpha_vs_spy_pessimistic, alpha_vs_spy), "
                     "COALESCE(alpha_vs_spy_certain, alpha_vs_spy))")
GT_TRANCHES_PATH = Path(__file__).resolve().parent / "gt_tranches.txt"

# Column schema for --kernel gt output -- deliberately separate from COLUMN_DEFS (see
# module docstring): a GT candidate row's 'robust_alpha_pct'/'cagr_pct' are NOT the same
# thing as the legacy schema's core_alpha_pct/ann_excess_pct (different derivation,
# different fill-resolution/reentry conventions -- see run_optimization_sweep.py's own
# derive_phase25_candidates_ground_truth/build_candidate_report_ground_truth docstrings),
# so reusing those column names here would misrepresent one kernel's numbers as the other's.
GT_COLUMN_DEFS = {
    "ticker": "The symbol.",
    "strategy": "TrailingBothZScoreBreakout or TrailingExitZScoreBreakout -- this scope's real GT campaign strategy.",
    "config_version": "The real backtest_cache `version` string this scope's GT rows were computed under.",
    "entry_timing": "'open_check' or 'close' -- this scope's real GT campaign entry timing.",
    "fixed_sl": "Fixed stop-loss % for strategies that use one (TrailingExitZScoreBreakout); 0 otherwise.",
    "candidate_rank": "1-based position in derive_phase25_candidates_ground_truth's own returned candidate list "
                       "for this scope (up to 9 -- top-3-per-island across up to 3 islands).",
    "is_winner": "True for the single candidate build_candidate_report_ground_truth picked as the scope's overall "
                 "winner -- ranked by cagr_pct (2026-08-23, ground_truth_kernel_rebuild.md Step 4, CAGR is the "
                 "GT selection metric) EXCEPT for a candidate_source='candidate_nodes' scope, where cagr_pct is "
                 "never available and the winner is ranked by robust_alpha_pct instead -- see winner_metric.",
    "winner_metric": "Which column actually decided is_winner for this scope: 'cagr' (normal path) or "
                      "'robust_alpha' (candidate_source='candidate_nodes' scope, no real cagr available).",
    "candidate_source": "'backtest_cache' (normal GT path, derive_phase25_candidates_ground_truth) or "
                         "'candidate_nodes' (fallback for a campaign the in-memory sweep pipeline ran, which "
                         "writes zero backtest_cache rows -- scripts/phase4_candidate_nodes_resolver.py). A "
                         "candidate_nodes-sourced row always has cagr_pct=None and phase4_eligible=True "
                         "(no CAGR-based gate exists without a real cagr -- see phase4_eligible's own note).",
    "candidate_id": "The real candidate_nodes.id this row came from (candidate_source='candidate_nodes' only) "
                     "-- None for a backtest_cache-sourced row, which was never promoted into candidate_nodes. "
                     "This is the real anchor key scripts/candidate_verification_store.py's phase4_results "
                     "table persists against (Task #2, 2026-08-29 planner dispatch).",
    "take_profit": "Candidate's take_profit/arm_sell_pct cell value (see run_optimization_sweep.py's take_profit "
                    "column meaning per strategy).",
    "stop_loss": "Candidate's stop_loss/trail_buy_pct cell value.",
    "max_hold_hours": "Candidate's max hold time cell value.",
    "window": "Candidate's z-score lookback window.",
    "z_score_threshold": "Candidate's entry z-score threshold.",
    "tpct": "Candidate's 4th-axis trail_sell_pct (TrailingBoth) or 0 (TrailingExit).",
    "robust_alpha_pct": "MIN(possible,pessimistic,certain) alpha vs SPY for this candidate's own cell, as computed "
                         "by the real Phase1/2-GT sweep rows this scope's candidates were derived from.",
    "cagr_pct": "Real annualized CAGR for this candidate's own cell (same source as robust_alpha_pct).",
    "n_trades": "Real trade count from this candidate's own same_bar_reentry=True trade list (build_candidate_"
                "report_ground_truth's own re-simulation, matching the real live dispatch convention).",
    "trades_from_cache": "True when this candidate's trade list came from backtest_winner_trades (Phase2.5's "
                          "persisted cache, kernel_version/build_id-matched -- see build_candidate_report_"
                          "ground_truth's own trades-cache docstring), False when freshly resimulated this "
                          "call. Informational/provenance only -- a stale cache-hit should already be "
                          "unreachable (kernel_version/build_id mismatch falls through to resim), this "
                          "column exists so that's independently verifiable after the fact.",
    "core_safe": "True/False/None(unknown) -- cliff-safety verdict on the CORE (unlevered) CAGR (2026-08-23, "
                 "ground_truth_kernel_rebuild.md Step 4 -- alpha replaced by CAGR for GT), same worst-neighbor<0 "
                 "convention this project uses everywhere else. NOTE units: for GT this threshold means 'a "
                 "nearby parameter nudge lost money outright', a looser bar than legacy's 'underperformed SPY'.",
    "addon_safe": "Same cliff-safety verdict, computed on the ADD-ON-adjusted CAGR instead (see "
                  "run_addon_cliff_safety_ground_truth's docstring for its known limitations before treating "
                  "this as an absolute go/no-go signal).",
    "core_addon_disagreement": "True when core_safe and addon_safe disagree for this candidate.",
    "addon_cagr_pct": "Add-on-adjusted CAGR at this candidate's own cell -- computed regardless of the ticker's "
                       "current account (see addon_eligible/addon_eligibility_reason) since it's a real input to "
                       "a future account-assignment decision, not just a property of today's account. Blank when "
                       "phase4_eligible is False (skipped, not worth the compute) -- check that column first "
                       "before reading a blank here as a real compute failure.",
    "addon_eligible": "Whether this ticker is CURRENTLY on a margin-capable account (schwab_safety's real "
                       "margin_capable gate) -- annotation only, does not gate addon_cagr_pct's computation.",
    "addon_eligibility_reason": "Human-readable reason for addon_eligible's value.",
    "phase4_eligible": "Whether this candidate's own coarse island cell cleared PHASE25_ISLAND_CAGR_MIN -- "
                        "False means core_safe/addon_safe/addon_cagr_pct were deliberately never computed "
                        "(skipped, not worth the overlay compute), not a failure. Every candidate still appears "
                        "in this report and still has real robust_alpha_pct/cagr_pct/n_trades regardless. "
                        "ALWAYS True for a candidate_source='candidate_nodes' row -- that gate is cagr-based and "
                        "no real cagr exists there, so this column means 'gate not applicable' for those rows, "
                        "not 'gate applied and passed' -- check candidate_source before reading this as a real "
                        "CAGR-floor verdict.",
    "error": "Set instead of the above when this scope/candidate couldn't be evaluated (e.g. Phase1/2-GT campaign "
             "not complete yet, or a build_candidate_report_ground_truth failure) -- see the message for why.",
    "check4_early_wr_pct": "Check 4 (70/30 win-rate stability): win rate over the earlier 70% of this candidate's "
                            "own trades by time.",
    "check4_late_wr_pct": "Check 4: win rate over the later 30% of trades. A big early-vs-late gap flags a "
                           "candidate whose edge may be decaying, not stable.",
    "check8_compounded_pct": "Check 8 (trade-count fluke): full compounded return across this candidate's own "
                              "trades (same_bar_reentry=True).",
    "check8_compounded_without_best_pct": "Check 8: compounded return with the single best trade removed.",
    "check8_best_trade_share_pct": "Check 8: percentage-point share of compounded return contributed by the "
                                    "single best trade -- large values flag a one-trade fluke.",
    "check8_too_few_trades": "Check 8: True when n_trades < GT_FLUKE_MIN_TRADES (run_optimization_sweep.py) -- "
                              "the fluke check is unreliable below this count.",
    "check11_max_drawdown_pct": "Check 11: max peak-to-trough compounded-equity drawdown (<=0) across this "
                                 "candidate's own trades.",
    "check11_dd_peak_time": "Check 11: timestamp of the equity peak the max drawdown fell from.",
    "check11_dd_trough_time": "Check 11: timestamp of the equity trough the max drawdown bottomed at.",
    # Real per-fold keys (paired-review CRITICAL finding, 2026-08-23, both independent-cold
    # and contextual Opus review converged on this independently): this used to be 3 literal
    # "{N}" template-string keys, which don't match the real check13_fold1_n..check13_fold5_
    # fragile keys gt_rows_for_scope() actually emits below -- _write_csv's DictWriter (default
    # extrasaction='raise') crashed outright on any real row, and _write_xlsx silently wrote 3
    # blank template-named columns while dropping all 15 real fold values (exactly the
    # "blank looks like a compute failure" trap this whole GT column schema exists to avoid).
    **{k: v for n in range(1, 6) for k, v in {
        f"check13_fold{n}_n": f"Check 13 (walk-forward 5-fold): trade count in fold {n} "
                               f"(1-5, equal-time-span slices).",
        f"check13_fold{n}_cagr_pct": f"Check 13: fold {n}'s own annualized CAGR.",
        f"check13_fold{n}_fragile": f"Check 13: True when fold {n}'s CAGR <= GT_ROBUSTNESS_CAGR_MIN (20%) -- "
                                     f"None means the fold was empty (no trades), not evaluated.",
    }.items()},
    "drought_n_core_trades": "Drought overlay (TrailingBoth only): core trade count fed into the drought sim.",
    "drought_core_compounded_pct": "Drought overlay: core-only compounded return over the same window.",
    "drought_best_confirm_days": "Drought overlay: winning confirm_days from the swept grid "
                                  "(drought_overlay_sweep.CONFIRM_DAYS_GRID).",
    "drought_best_vol_gate": "Drought overlay: winning vol_gate from the swept grid "
                              "(drought_overlay_sweep.VOL_GATE_GRID), None if ungated.",
    "drought_n_windows": "Drought overlay: number of real drought windows found (confirm_days no-signal gaps).",
    "drought_n_simulated": "Drought overlay: number of those windows actually simulated (some may be skipped, "
                            "e.g. missing vol data).",
    "drought_compounded_pct": "Drought overlay: compounded return from drought-window trades alone. None (not "
                               "NaN) when zero real drought windows existed at the winning grid cell.",
    "drought_combined_compounded_pct": "Drought overlay: core+drought combined compounded return.",
    "drought_skip_reason": "Set when drought was never computed for this scope at all (e.g. strategy is not "
                            "TrailingBothZScoreBreakout) -- see run_optimization_sweep.py's drought_skip_reason.",
}

# Relabels find_candidates()'s internal keys to the user's requested wording
# 2026-08-08 (later) -- kept as a separate map (not renamed at the source in
# candidate_5min_report.py) since that script's own terminal output is aimed
# at a different, more technical audience/context.
CANDIDATE_LABELS = {
    'cliff-safe (current convention)': 'best safe node',
    'CAGR-first (possible-resolution, cliff-checked on the same metric)': 'best CAGR-safe node',
    'best robust_alpha (ignoring cliff-safety)': 'best unsafe node',
    'best possible (raw alpha_vs_spy)': '5min best possible',
    'best certain (alpha_vs_spy_certain, no-guessing resolution)': 'best certain',
}
CANDIDATE_TYPE_ORDER = ['best safe node', 'best CAGR-safe node', 'best unsafe node', '5min best possible',
                         'best certain']

# Single source of truth for what each output column means -- used for both
# the xlsx glossary sheet and (imported directly) the Streamlit Candidates
# page's glossary expander, so the two never drift apart.
COLUMN_DEFS = {
    "ticker": "The symbol.",
    "candidate_type": "Which of the 4 candidate-selection methods produced this row: 'best safe node' "
                       "(robust_alpha=MIN(possible,pessimistic,certain), required to pass the CLIFF_RADIUS=3 "
                       "neighbor-safety check -- this project's real selection convention), 'best CAGR-safe "
                       "node' (same cliff-safety check but ranked on alpha_vs_spy/CAGR-equivalent instead of "
                       "robust_alpha -- your actual real-world selection preference), 'best unsafe node' "
                       "(top robust_alpha row regardless of whether it clears the safety check -- look here "
                       "first to sanity-check the safe pick isn't missing something), or '5min best possible' "
                       "(top raw 'possible'-resolution alpha_vs_spy row, ignoring robustness entirely -- named "
                       "for the 5-min fill-accuracy finding that 'possible' is empirically the most accurate "
                       "single resolution, see docs/research_log.md's 2026-08-08 entries). Rows are dropped, "
                       "not duplicated, when two of the three methods pick the identical node.",
    "strategy": "Which strategy class this ticker's rows use (TrailingBothZScoreBreakout = trailing-buy entry, "
                "TrailingExitZScoreBreakout = market-buy entry) -- always the strategy of the ticker's single "
                "best robust_alpha row across all strategies; all 3 candidate rows for a ticker share it.",
    "core_alpha_pct": "robust_alpha for this row's node: MIN(possible, pessimistic, certain) fill-resolution "
                       "alpha vs SPY, over the ticker's full cached-data window. This project's standard "
                       "selection metric -- WITH SPY subtracted (see abs_return_pct for the raw number).",
    "abs_return_pct": "This row's node's own RAW compounded return (SPY NOT subtracted) over the same window. "
                       "Read this next to core_alpha_pct when weighing a 'safe but small' pick against a "
                       "'ridiculous but risky' one -- core_alpha_pct/worst_neighbor_pct are SPY-adjusted, this "
                       "one isn't.",
    "years": "Real calendar span of this ticker's cached hourly data (days between the earliest and latest "
             "cached bar, /365.25). Tickers vary a lot here (e.g. SOXL ~3.0y vs SPCL ~0.31y) -- this is why "
             "core_alpha_pct/abs_return_pct alone aren't fairly comparable across tickers.",
    "trades": "Number of completed trades in this row's node's backtest.",
    "ann_excess_pct": "CAGR-based excess return over SPY (SPY-adjusted, like core_alpha_pct), annualized over "
                       "the full calendar window (years column) -- fixes the cross-ticker horizon-mismatch "
                       "problem above. A large value backed by a short 'years'/low 'trades' is weak evidence "
                       "(e.g. SPCL's ~3000%+ off 0.31y/3 trades is an annualization artifact, not a real "
                       "signal) -- always read this next to years/trades, never alone.",
    "fillacc_possible_win_pct": "Of this row's node's real trailing-buy entry signals in the last ~58 days "
                                 "(yfinance's 5-min history cap), the % where the 'possible' fill resolution "
                                 "(the kernel's default, optimistic-but-unmodified bounce-fill assumption) was "
                                 "closest to the REAL 5-minute-bar fill price, vs the 'pessimistic'/'certain' "
                                 "alternatives. Blank for TrailingExitZScoreBreakout rows (market-buy entry has "
                                 "no bounce-fill resolution to check) or if trail_buy_pct=0.",
    "fillacc_possible_mean_err_pct": "Mean absolute price error (%) of the 'possible' resolution vs the real "
                                      "5-min fill, across those same signals. Lower is better/more trustworthy. "
                                      "Same blank-condition as fillacc_possible_win_pct.",
    "fillacc_n": "How many real signals the fill-accuracy check above is actually based on -- often single "
                 "digits in a 58-day window, so treat a 100% win rate on n=1-2 with real caution.",
    "worst_neighbor_pct": "Cliff-safety check: the worst robust_alpha found among nearby take_profit/stop_loss "
                           "grid values (CLIFF_RADIUS=3 steps) AND max_hold_hours +/-7 around this row's node "
                           "params, holding every other axis fixed -- deliberately matches "
                           "top_safe_nodes.best_safe_node()'s own tolerance (found 2026-08-08: a wider +/-24 "
                           "hold tolerance, prune_backtest_cache.py's own convention, could make a node "
                           "certified 'safe' by best_safe_node() show CLIFF here, purely from a hold-time "
                           "window inconsistency, not a real disagreement about safety). Negative means a "
                           "nearby parameter nudge would have lost money -- see "
                           "docs/cliff_safety_query_checklist.md.",
    "status": "CLIFF (worst_neighbor_pct < 0, fragile to small parameter changes) or SAFE (worst_neighbor_pct "
              ">= 0), computed fresh for THIS row's own node -- so 'best unsafe node'/'5min best possible' rows "
              "can (and often do) show CLIFF even though 'best safe node' always shows SAFE by construction.",
    "addon_n": "Number of backtested add-on-leg trades for THIS exact row's own node params -- computed on "
               "demand (run_overlay_shim.py) the first time a candidate is seen, then reused from "
               "candidate_overlay_results on later runs. All 3 candidate rows for a ticker get their own real "
               "run against their own params, not a shared/repeated number. Blank only if the node has <2 "
               "real trades to evaluate an overlay against, or --skip-overlay was passed.",
    "addon_compounded_pct": "Compounded return of just the add-on overlay's own trades (not combined with core). "
                             "Same blank-condition as addon_n.",
    "addon_win_rate_pct": "% of add-on trades that were profitable. Same blank-condition as addon_n.",
    "drought_n": "Number of backtested drought-overlay trades, same on-demand-per-row-node computation and "
                 "blank-condition as addon_n.",
    "drought_compounded_pct": "Compounded return of just the drought overlay's own trades (not combined with core).",
    "drought_win_rate_pct": "% of drought trades that were profitable.",
    "x_addon_pct": "NAIVE estimate of core+add-on combined: (1+core_return)*(1+addon_return)-1. Add-on capital "
                   "runs CONCURRENTLY with an open core position (not sequentially), so this OVERSTATES the "
                   "real combined effect -- v5_stacked_backtest.py's parallel-return model is the rigorous "
                   "version. Treat this column as a rough upper bound, not a real number.",
    "x_drought_pct": "NAIVE estimate of core+drought combined, same (1+core)*(1+overlay)-1 formula. Drought "
                      "fills core's own idle-time gaps SEQUENTIALLY, so this approximation is more defensible "
                      "than x_addon_pct, but still not the rigorous stacked model.",
    "liquidity_dollars_per_day": "avg_vol_10d * last_price * 0.01 from the tickers table -- this project's "
                                  "standard real dollar-liquidity estimate (confirmed against CLAUDE.md's cited "
                                  "figures, e.g. AGQ ~$2.02M, HIBL ~$89.9K). Ticker-level, same across all 3 "
                                  "candidate rows for a ticker. Supposed to be the FIRST-pass filter before "
                                  "spending validation effort on a candidate (see the 2026-08-07 'liquidity was "
                                  "never the limiting filter' finding) -- a great alpha number on an illiquid "
                                  "name is untradeable regardless of the rest of this row.",
}


def best_node_strategy(conn, ticker, version="v5"):
    """Which strategy has this ticker's single best robust_alpha row, across
    ALL strategies -- used to scope the 3-way candidate split to one strategy
    (mixing TrailingBoth/TrailingExit params in a neighbor search wouldn't be
    meaningful, same convention top_safe_nodes.py already uses). Also returns
    spy_bh (ticker-level, identical across every row for this ticker/version,
    so fetched once here rather than per candidate)."""
    c = conn.cursor()
    c.execute(f"""
        SELECT strategy, spy_bh FROM backtest_cache
        WHERE ticker=? AND version=? AND trades>0
        ORDER BY {ROBUST_ALPHA_SQL} DESC LIMIT 1
    """, (ticker, version))
    return c.fetchone()


def load_ticker_df(conn, ticker, version, strategy):
    import pandas as pd
    df = pd.read_sql("""
        SELECT ticker, COALESCE(take_profit, arm_sell_pct) AS take_profit, stop_loss, max_hold_hours, window,
               z_score_threshold, trail_buy_pct, trail_sell_pct, entry_timing,
               alpha_vs_spy, alpha_vs_spy_pessimistic, alpha_vs_spy_certain,
               strategy_return, trades, win_rate, sweep_run_id
        FROM backtest_cache
        WHERE ticker=? AND version=? AND strategy=? AND trades>0
    """, conn, params=(ticker, version, strategy))
    pess = df["alpha_vs_spy_pessimistic"].fillna(df["alpha_vs_spy"])
    cert = df["alpha_vs_spy_certain"].fillna(df["alpha_vs_spy"])
    df["robust_alpha"] = pd.concat([df["alpha_vs_spy"], pess, cert], axis=1).min(axis=1)
    return df


def _window_dates_from_version(version):
    """Real (start_date, end_date) strings encoded in a windowed version string (see
    run_optimization_sweep.window_version_suffix -- `-w{start}_{end}`, e.g.
    'v6-massive-w2022-05-20_2026-05-20'), or (None, None) if `version` isn't windowed.
    Inverse of window_version_suffix. Uses the LAST match so a double-suffixed version
    (the run_quarterly_soxl_sweep.sh bug, 2026-08-15 -- --version already windowed AND
    --start-date/--end-date passed, so run_optimization_sweep.py appends its own suffix
    on top) still parses the correct real window, not a truncated partial match. Fix,
    2026-08-23 (sibling to the data_source fix, same night): gt_rows_for_scope/
    gt_full_review_rows previously called build_candidate_report_ground_truth with
    start_date=None, end_date=None unconditionally, even for a version string that
    encodes a real narrower window -- silently re-deriving GT report legs (spy_bh,
    years, check1 macro, addon/drought re-simulation) over the ticker's FULL history
    instead of the campaign's real window. This is the parsing half of that fix; see
    gt_rows_for_scope/gt_full_review_rows for where the result is actually threaded
    through."""
    if not version:
        return None, None
    matches = re.findall(r"-w(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})", version)
    if not matches:
        return None, None
    start, end = matches[-1]
    return start, end


def _window_days_from_version(version):
    """Real calendar-day span encoded in a windowed version string, or None if
    `version` isn't windowed. See _window_dates_from_version for the underlying parse."""
    start, end = _window_dates_from_version(version)
    if start is None:
        return None
    return (pd.Timestamp(end) - pd.Timestamp(start)).days


def annualized_excess(ticker, strategy_return, spy_bh_full_window, version=None):
    """CAGR-based excess return over the real calendar span the backtest
    actually ran over: the windowed span encoded in `version` when this is a
    date-windowed campaign (run_quarterly_soxl_sweep.sh etc.), otherwise the
    ticker's full cached-data calendar span -- see annualized_alpha_report.py's
    docstring for why raw alpha_vs_spy isn't cross-ticker comparable and why
    this annualizes over the full window rather than invested-only time
    (user's explicit call, 2026-08-08). Bug fixed 2026-08-16: before this,
    every windowed campaign's CAGR/ann_excess were silently wrong -- always
    divided by the ticker's full cached-history span (e.g. SOXL's ~3.06y)
    regardless of the actual (e.g. 1y) window the return was computed over,
    understating CAGR by ~3x for a 1-year window on a 3-year-history ticker."""
    days = _window_days_from_version(version) or calendar_days(ticker)
    strat_cagr = cagr(strategy_return, days)
    spy_cagr = cagr(spy_bh_full_window, days)
    if strat_cagr is None or spy_cagr is None:
        return days, None
    return days, strat_cagr - spy_cagr


def fill_accuracy_summary(ticker, strategy, window, z, trail_buy_pct, hold):
    """(possible_win_rate_pct, possible_mean_abs_err_pct, n) from the 5-min
    real-fill replay, or None if this node's entry mechanism has no bounce-
    fill resolution to check (TrailingExitZScoreBreakout's market-buy entry,
    or a TrailingBoth row with trail_buy_pct=0)."""
    if strategy != "TrailingBothZScoreBreakout" or not trail_buy_pct:
        return None
    from verify_fill_resolution_accuracy import fill_accuracy_for_node
    df = fill_accuracy_for_node(ticker, window, z, trail_buy_pct, hold)
    if df.empty:
        return None
    diff_cols = ['possible_diff_pct', 'pessimistic_diff_pct', 'certain_diff_pct']
    abs_diffs = df[diff_cols].abs()
    closest = abs_diffs.idxmin(axis=1)
    win_rate = (closest == 'possible_diff_pct').mean() * 100
    mean_abs_err = df['possible_diff_pct'].dropna().abs().mean()
    return win_rate, mean_abs_err, len(df)


def worst_neighbor(conn, ticker, version, strategy, window, z, entry_timing,
                    sl, tp, hold, tb, ts, metric="robust_alpha"):
    """Worst `metric` among the CLIFF_RADIUS-step neighborhood on
    take_profit/stop_loss (index-based nearest-distinct-values), holding
    every other axis fixed. max_hold_hours tolerance intentionally set to
    +/-7 (not prune_backtest_cache.py's own +/-24) to MATCH
    top_safe_nodes.best_safe_node()'s tolerance -- found 2026-08-08 that a
    node top_safe_nodes.py certified 'safe' (+2.7% worst neighbor at +/-7)
    came back CLIFF here at +/-24 (-9.8%) for TNA, purely because +/-24
    happened to span this ticker's ENTIRE swept hold-time range (6 values,
    7 apart) rather than just nearby ones. Since this report's 'best safe
    node'/'best CAGR-safe node' labels come directly from best_safe_node()'s
    own check, this function must use the SAME tolerance AND the SAME
    metric that check used, or the label and Status column can visibly
    disagree -- found 2026-08-11 for 'best CAGR-safe node': this function
    defaulted to robust_alpha unconditionally, so a node genuinely verified
    safe under alpha_vs_spy (its own selection metric) printed a
    contradictory CLIFF status computed against a DIFFERENT metric than the
    one that vetted it. prune_backtest_cache.py's +/-24 is left untouched --
    that's a real, separate, deliberate convention for island selection, not
    something to change as a side effect of this report."""
    metric_sql = ROBUST_ALPHA_SQL if metric == "robust_alpha" else metric
    c = conn.cursor()

    def nearest_values(col_sql, center):
        c.execute(f"""
            SELECT DISTINCT {col_sql} FROM backtest_cache
            WHERE ticker=? AND version=? AND strategy=? AND window=? AND z_score_threshold=?
                  AND entry_timing=? AND trail_buy_pct=? AND trail_sell_pct=?
        """, (ticker, version, strategy, window, z, entry_timing, tb, ts))
        vals = sorted(set(r[0] for r in c.fetchall() if r[0] is not None))
        if center not in vals:
            return {center}
        idx = vals.index(center)
        return set(vals[max(0, idx - CLIFF_RADIUS):min(len(vals), idx + CLIFF_RADIUS + 1)])

    tp_keep = list(nearest_values("COALESCE(take_profit, arm_sell_pct)", tp))
    sl_keep = list(nearest_values("stop_loss", sl))
    tp_ph = ",".join("?" * len(tp_keep))
    sl_ph = ",".join("?" * len(sl_keep))
    c.execute(f"""
        SELECT MIN({metric_sql}) FROM backtest_cache
        WHERE ticker=? AND version=? AND strategy=? AND window=? AND z_score_threshold=?
              AND entry_timing=? AND trail_buy_pct=? AND trail_sell_pct=?
              AND COALESCE(take_profit, arm_sell_pct) IN ({tp_ph})
              AND stop_loss IN ({sl_ph})
              AND ABS(max_hold_hours - ?) <= 7 AND trades > 0
    """, [ticker, version, strategy, window, z, entry_timing, tb, ts] + tp_keep + sl_keep + [hold])
    return c.fetchone()[0]


def compounded(rets):
    prod = 1.0
    for r in rets:
        prod *= (1 + r)
    return (prod - 1) * 100


def _native(v):
    """numpy.int64/float64 -> native Python type. sqlite3 has no adapter for
    numpy scalars and silently binds them as a BLOB instead of a number --
    same bug/fix as candidate_full_review.py's overlay_robustness() (see its
    docstring), found 2026-08-19 in this function's sibling query, which is
    what actually feeds addon_n/drought_n (overlay_robustness feeds a
    different set of columns -- the verdict/robustness ones -- so fixing
    only that one left this one still silently blank)."""
    return v.item() if hasattr(v, "item") else v


def liquidity_dollars_per_day(conn, ticker):
    """Real dollar liquidity, this project's standard formula (see
    campaign_comparison_table.py) -- avg_vol_10d*last_price*0.01, confirmed
    against CLAUDE.md's cited figures (AGQ ~$2.02M/day, HIBL ~$89.9K/day)."""
    c = conn.cursor()
    c.execute("SELECT avg_vol_10d * last_price * 0.01 FROM tickers WHERE symbol=?", (ticker,))
    row = c.fetchone()
    return row[0] if row else None


def overlay_summary_for_node(conn, ticker, strategy, version, mechanism, node):
    """Overlay backtests (candidate_overlay_results) are only ever run
    against ONE specific node's exact params (via candidate_nodes/
    run_overlay_shim.py), not against all 3 candidate types -- so this
    matches on the full param tuple, not just ticker, and returns None
    (blank) for a candidate row that was never actually run through the
    overlay shim, rather than showing another row's numbers as if they
    applied here too."""
    c = conn.cursor()
    # Real gap found in paired review 2026-08-09 (candidate_full_review.py):
    # run_overlay_shim.py's INSERT has no dedup and can be re-run for the
    # same node -- 22+ real candidate_node_ids have 2-5 duplicate
    # run_timestamps for the same mechanism, silently inflating n/compounded
    # here. Scoped to the latest run_timestamp per (candidate_node_id,
    # mechanism), matching the same fix in candidate_full_review.overlay_robustness.
    c.execute("""
        SELECT cor.ret FROM candidate_overlay_results cor
        JOIN candidate_nodes cn ON cn.id = cor.candidate_node_id
        WHERE cn.ticker=? AND cor.mechanism=? AND cn.strategy=? AND cn.version=?
              AND cn.window=? AND cn.z=? AND cn.fixed_sl=? AND cn.arm_pct=?
              AND cn.trail_buy_pct=? AND cn.trail_sell_pct=? AND cn.max_hold_hours=? AND cn.entry_timing=?
              AND cor.run_timestamp = (
                  SELECT MAX(cor2.run_timestamp) FROM candidate_overlay_results cor2
                  WHERE cor2.candidate_node_id = cor.candidate_node_id AND cor2.mechanism = cor.mechanism
              )
    """, (ticker, mechanism, strategy, version, _native(node['window']), _native(node['z']),
          _native(node['sl']), _native(node['arm_pct']), _native(node['trail_buy_pct']),
          _native(node['trail_sell_pct']), _native(node['hold']), node['entry_timing']))
    rets = [r[0] for r in c.fetchall()]
    if not rets:
        return None
    wr = sum(1 for r in rets if r > 0) / len(rets) * 100
    return len(rets), compounded(rets), wr


def ensure_overlay_for_node(conn, ticker, strategy, version, node, confirm_days=10):
    """Computes drought/addon overlay results on demand for THIS exact node
    if they're not already in candidate_overlay_results -- added 2026-08-08
    (later) per the user's explicit call: 'all three candidates should get
    the same overlay treatment', not just whichever one happened to already
    be registered from an earlier locate_best_node.py/run_overlay_shim.py
    run. Only computes the missing mechanism(s); commits immediately so a
    later run's overlay_summary_for_node lookup (and other tools reading
    candidate_overlay_results) see it too."""
    missing = {m for m in ("drought", "addon")
               if overlay_summary_for_node(conn, ticker, strategy, version, m, node) is None}
    if not missing:
        return
    shim_node = {
        'ticker': ticker, 'strategy': strategy, 'version': version,
        'window': node['window'], 'z': node['z'], 'fixed_sl': node['sl'],
        'arm_pct': node['arm_pct'], 'trail_buy_pct': node['trail_buy_pct'],
        'trail_sell_pct': node['trail_sell_pct'], 'max_hold_hours': node['hold'],
        'entry_timing': node['entry_timing'],
        'robust_alpha': node['robust_alpha'], 'trades': node['trades'],
    }
    rows = run_overlay_for_node(conn, ticker, shim_node, confirm_days, mechanisms=missing)
    if not rows:
        return
    run_ts = _datetime.now().isoformat(timespec="seconds")
    conn.executemany("""
        INSERT INTO candidate_overlay_results
            (run_timestamp, mechanism, ticker, candidate_node_id,
             confirm_days, entry_time, exit_time, exit_reason, ret)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [(run_ts, r["mechanism"], r["ticker"], r["candidate_node_id"],
           r["confirm_days"], r["entry_time"], r["exit_time"], r["exit_reason"], r["ret"])
          for r in rows])
    conn.commit()


def _row_to_record(row):
    """Converts one internal row tuple to a {column_name: value} dict keyed
    exactly like COLUMN_DEFS, so CSV/xlsx/terminal output and the glossary
    all agree on column names in one place."""
    ticker = row[0]
    if row[1] is None:
        return {"ticker": ticker}
    (ticker, candidate_type, strategy, ralpha, sret, years, trades, ann_excess, fill_acc, wn, cliff,
     addon, drought, addon_mult, drought_mult, liquidity) = row
    return {
        "ticker": ticker, "candidate_type": candidate_type, "strategy": strategy,
        "core_alpha_pct": ralpha, "abs_return_pct": sret,
        "years": years, "trades": trades, "ann_excess_pct": ann_excess,
        "fillacc_possible_win_pct": fill_acc[0] if fill_acc else None,
        "fillacc_possible_mean_err_pct": fill_acc[1] if fill_acc else None,
        "fillacc_n": fill_acc[2] if fill_acc else None,
        "worst_neighbor_pct": wn, "status": cliff,
        "addon_n": addon[0] if addon else None,
        "addon_compounded_pct": addon[1] if addon else None,
        "addon_win_rate_pct": addon[2] if addon else None,
        "drought_n": drought[0] if drought else None,
        "drought_compounded_pct": drought[1] if drought else None,
        "drought_win_rate_pct": drought[2] if drought else None,
        "x_addon_pct": addon_mult, "x_drought_pct": drought_mult,
        "liquidity_dollars_per_day": liquidity,
    }


def _git_provenance_stamp():
    """Best-effort commit/dirty/timestamp stamp for report provenance -- filed
    against the real 2026-08-23 incident (docs/backlog_cache.md) where a
    generated report was trusted as current for over an hour after two
    commits changed the numbers in it, with nothing flagging staleness.
    Lazy-imported per this file's existing run_optimization_sweep import
    convention (see module docstring above -- avoids its import-time
    logging.basicConfig side effect on every plain legacy-mode run)."""
    from run_optimization_sweep import _current_kernel_git_state, _REPORT_GEN_FILES
    commit, dirty = _current_kernel_git_state(_REPORT_GEN_FILES)
    ts = _datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    if commit is None:
        return f"commit=unknown at {ts}"
    return f"commit={commit[:12]}{'*dirty*' if dirty else ''} at {ts}"


def _write_csv(name, rows, col_defs=COLUMN_DEFS, to_record=_row_to_record):
    out_path = Path("output") / (name if name.endswith(".csv") else f"{name}.csv")
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w", newline="") as f:
        f.write(f"# Generated: {_git_provenance_stamp()}\n")
        w = csv.DictWriter(f, fieldnames=list(col_defs.keys()))
        w.writeheader()
        for row in rows:
            w.writerow(to_record(row))
    print(f"Wrote {out_path} ({len(rows)} rows)")


def _write_xlsx(name, rows, col_defs=COLUMN_DEFS, to_record=_row_to_record):
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter

    out_path = Path("output") / (name if name.endswith(".xlsx") else f"{name}.xlsx")
    out_path.parent.mkdir(exist_ok=True)

    wb = Workbook()
    data_ws = wb.active
    data_ws.title = "Candidates"

    cols = list(col_defs.keys())
    data_ws.append(cols)
    for cell in data_ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        rec = to_record(row)
        data_ws.append([rec.get(c) for c in cols])
    data_ws.freeze_panes = "A2"
    for i, col in enumerate(cols, start=1):
        data_ws.column_dimensions[get_column_letter(i)].width = max(12, min(len(col) + 2, 28))

    def_ws = wb.create_sheet("Column Definitions")
    def_ws.append(["Column", "Definition"])
    for cell in def_ws[1]:
        cell.font = Font(bold=True)
    for col, definition in col_defs.items():
        def_ws.append([col, definition])
        def_ws.cell(row=def_ws.max_row, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    def_ws.append(["Generated", _git_provenance_stamp()])
    def_ws.column_dimensions["A"].width = 32
    def_ws.column_dimensions["B"].width = 110

    wb.save(out_path)
    print(f"Wrote {out_path} ({len(rows)} rows, 2 sheets)")


def build_rows_for_ticker(conn, ticker, version, min_alpha, skip_5min, skip_overlay=False):
    """Returns a list of internal row tuples, one per candidate type (up to
    3, deduped -- see find_candidates()), or [(ticker, None)] if there's no
    data/no candidates at all."""
    best = best_node_strategy(conn, ticker, version)
    if best is None:
        return [(ticker, None)]
    strategy, spy_bh = best

    df_t = load_ticker_df(conn, ticker, version, strategy)
    candidates = find_candidates(df_t, min_alpha)
    if not candidates:
        return [(ticker, None)]

    # Only 'CAGR-first' gets a non-default metric here -- it's the one new
    # type whose own selection check (inside best_safe_node) used
    # alpha_vs_spy, not robust_alpha, so its Status/WorstNb must be
    # recomputed on that SAME metric or it visibly contradicts its own
    # label (found 2026-08-11, see worst_neighbor's docstring). The other
    # 4 types keep their existing, unchanged robust_alpha-based Status --
    # deliberately not widened to '5min best possible'/'best certain' too,
    # which have their own separate, longstanding, deliberate "show the
    # robust_alpha view even though a different metric picked this node"
    # framing that isn't part of this fix's scope.
    WN_METRIC_BY_LABEL = {
        'CAGR-first (possible-resolution, cliff-checked on the same metric)': 'alpha_vs_spy',
    }

    rows = []
    for raw_label, node in candidates.items():
        label = CANDIDATE_LABELS.get(raw_label, raw_label)
        wn = worst_neighbor(conn, ticker, version, strategy, node['window'], node['z'],
                             node['entry_timing'], node['sl'], node['arm_pct'], node['hold'],
                             node['trail_buy_pct'], node['trail_sell_pct'],
                             metric=WN_METRIC_BY_LABEL.get(raw_label, "robust_alpha"))
        cliff = "CLIFF" if (wn is not None and wn < 0) else ("SAFE" if wn is not None else "?")
        days, ann_excess = annualized_excess(ticker, node['return'], spy_bh, version=version)
        years = round(days / 365.25, 2) if days else None
        fill_acc = None if skip_5min else fill_accuracy_summary(
            ticker, strategy, node['window'], node['z'], node['trail_buy_pct'], node['hold'])
        if not skip_overlay:
            ensure_overlay_for_node(conn, ticker, strategy, version, node)
        addon = overlay_summary_for_node(conn, ticker, strategy, version, "addon", node)
        drought = overlay_summary_for_node(conn, ticker, strategy, version, "drought", node)
        # NAIVE multiplicative combination -- (1 + core) * (1 + overlay) - 1.
        # This is an APPROXIMATION, not real stacked-model math: drought fills
        # core's own time gaps sequentially (so multiplying compounded
        # multipliers is roughly defensible), but add-on runs CONCURRENTLY
        # with an open core position (parallel capital, not sequential), so
        # multiplying it against core here overstates/misrepresents the real
        # combined effect -- v5_stacked_backtest.py's proper parallel-return
        # model is the rigorous version of this, not this quick estimate.
        core_mult = 1 + node['return'] / 100
        addon_mult = (core_mult * (1 + addon[1] / 100) - 1) * 100 if addon else None
        drought_mult = (core_mult * (1 + drought[1] / 100) - 1) * 100 if drought else None
        liquidity = liquidity_dollars_per_day(conn, ticker)
        rows.append((ticker, label, strategy, node['robust_alpha'], node['return'], years, node['trades'],
                     ann_excess, fill_acc, wn, cliff, addon, drought, addon_mult, drought_mult, liquidity))
    return rows


def load_gt_tranche(n):
    """Parses gt_tranches.txt's own "<number> <space-separated tickers>" format,
    skipping comment/blank lines."""
    with open(GT_TRANCHES_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            num, *tickers = line.split()
            if num == str(n):
                return tickers
    raise ValueError(f"Tranche {n} not found in {GT_TRANCHES_PATH}")


def gt_scopes_for_tickers(conn, tickers):
    """Real (ticker, strategy, version, entry_timing, fixed_sl) GT scopes for exactly
    these tickers, restricted to strategies with a known campaign_config.STRATEGIES hp
    grid (a scope with no known grid can't be passed to derive_phase25_candidates_
    ground_truth at all) -- reuses prune_backtest_cache_ground_truth's own scope
    discovery/hp-grid-construction rather than re-deriving either. A (ticker, strategy)
    scope with no known grid is dropped, not silently -- logged explicitly (paired-review
    finding, 2026-08-23: the caller's own "no scope at all" notice only fires when a
    ticker has ZERO surviving scopes, so a ticker with one known-grid scope plus one
    unknown-strategy scope would otherwise lose the latter with no output at all)."""
    import campaign_config
    from prune_backtest_cache_ground_truth import discover_all_gt_scopes
    wanted = set(tickers)
    scopes = [s for s in discover_all_gt_scopes(conn) if s[0] in wanted]
    kept, dropped = [], []
    for s in scopes:
        (kept if s[1] in campaign_config.STRATEGIES else dropped).append(s)
    for ticker, strategy, version, entry_timing, fixed_sl in dropped:
        print(f"  {ticker}/{strategy}/{version}: dropped -- no known campaign_config.STRATEGIES "
              f"hp grid for strategy {strategy!r}.")
    return kept


def gt_current_best_node(conn, ticker, strategy, version, entry_timing, fixed_sl, metric, min_alpha_arg):
    """Independent second opinion via top_safe_nodes.best_safe_node, scoped to
    kernel_version='ground_truth_v6' rows for this EXACT scope (version/strategy/ticker/
    entry_timing, plus stop_loss=fixed_sl for strategies that use a fixed SL) -- mirrors
    top_safe_nodes.py's own --kernel-version ground_truth_v6 / --metric CLI scoping (CLI-
    only there, reproduced here for inline/structured use).

    entry_timing/fixed_sl scoping added 2026-08-23 (paired-review finding against the
    original version of this function, confirmed against real backtest_cache rows): a
    version+strategy pair can have MULTIPLE distinct GT scopes differing only in
    entry_timing or fixed_sl (run_optimization_sweep._campaign_scope_sql's own real
    dispatch scoping) -- without this filter, this cross-check could silently mix rows
    from a different campaign than the one whose candidates are printed alongside it.

    KNOWN RESIDUAL LIMITATION (not fixed here, same review): for TrailingBothZScoreBreakout
    GT rows, the real swept "SL-like" axis is trail_buy_pct (strategies.resolve_axis_columns'
    sl_axis_col) -- `stop_loss` is just this scope's fixed_sl constant, not a swept value
    (confirmed via real DB query: it's identical across every row once scoped as above).
    best_safe_node()'s neighbor mask holds trail_buy_pct/trail_sell_pct exactly fixed and
    perturbs take_profit/stop_loss +/-CLIFF_RADIUS -- so for TrailingBoth scopes this cross-
    check's cliff-safety search never actually varies the real 2nd axis, degenerating to a
    TP x hold-time-only neighbor check. This does NOT produce a wrong verdict (core_safe/
    addon_safe on the real GT candidate report rows are computed correctly, via
    run_addon_cliff_safety_ground_truth's own resolve_axis_columns-aware neighbor search) --
    it only means THIS SECONDARY informational cross-check is weaker than intended for
    TrailingBoth. A proper fix needs a GT-aware neighbor function that honors
    resolve_axis_columns, not a reuse of best_safe_node's legacy-schema assumption; left as
    a known gap rather than half-fixed under this pass."""
    import strategies
    sql_kernel_scope = "AND kernel_version='ground_truth_v6' AND entry_timing=?"
    params = [version, strategy, ticker, entry_timing]
    if strategies.uses_fixed_sl(strategy):
        sql_kernel_scope += " AND stop_loss=?"
        params.append(fixed_sl)
    df = pd.read_sql(f"""
        SELECT ticker, COALESCE(take_profit, arm_sell_pct) AS take_profit,
               stop_loss, max_hold_hours, window,
               z_score_threshold, trail_buy_pct, trail_sell_pct, entry_timing,
               alpha_vs_spy, alpha_vs_spy_pessimistic, alpha_vs_spy_certain,
               strategy_return, trades, win_rate, cagr
        FROM backtest_cache
        WHERE version=? AND strategy=? AND ticker=? AND trades > 0 {sql_kernel_scope}
    """, conn, params=params)
    if df.empty:
        return None
    pess = df["alpha_vs_spy_pessimistic"].fillna(df["alpha_vs_spy"])
    cert = df["alpha_vs_spy_certain"].fillna(df["alpha_vs_spy"])
    df["robust_alpha"] = pd.concat([df["alpha_vs_spy"], pess, cert], axis=1).min(axis=1)
    # Same "user didn't override --min-alpha" default-substitution top_safe_nodes.py's own
    # CLI applies (its --min-alpha default is None, not a fixed number) -- 200 is this
    # file's --min-alpha argparse default for the (unrelated) legacy cliff-safety search,
    # reused here as the "not explicitly overridden" sentinel.
    min_alpha = 50 if (metric == "cagr" and min_alpha_arg == 200) else min_alpha_arg
    return best_safe_node(df, min_alpha=min_alpha, metric=metric)


def gt_rows_for_scope(ticker, strategy, version, entry_timing, fixed_sl, grid_window=None):
    """Real per-candidate GT rows for one scope, matching GT_COLUMN_DEFS. A
    scope whose Phase1/2-GT campaign isn't complete yet, or whose
    build_candidate_report_ground_truth call fails (e.g. the in-progress
    backtester.py drought-overlay dependency as of 2026-08-23), returns row(s)
    with only 'error' populated (plus raw candidate cells when derive_phase25_
    candidates_ground_truth itself succeeded) rather than raising -- callers
    loop over many scopes and must not have one bad scope kill the batch.

    `grid_window` (2026-08-29, Task #3, additive -- named to avoid colliding with this
    file's own unrelated "window" concept, the date-range pair `_window_dates_from_
    version` resolves into `win_start`/`win_end` below): when set, skips the backtest_
    cache-based derive_phase25_candidates_ground_truth path entirely and uses scripts/
    phase4_candidate_nodes_resolver.derive_phase25_candidates_from_candidate_nodes
    instead (its own `window` param is the sweep-grid window count, e.g. 10/15/20 --
    matches candidate_nodes.window/every candidate dict's own 'window' key), via
    build_candidate_report_ground_truth's candidates_override param -- for a campaign
    the in-memory sweep pipeline ran (zero backtest_cache rows, invisible to the default
    path). `grid_window=None` (default) is byte-identical to this function's behavior
    before this param existed -- no existing caller is affected. Required (not optional)
    whenever the caller already knows `version` aliases multiple unrelated batches (see
    that resolver's own `window` param docstring) -- this function does not try to
    detect that on its own.

    DB_PATH sync (paired-review finding, 2026-08-23): derive_phase25_candidates_
    ground_truth/build_candidate_report_ground_truth connect via run_optimization_
    sweep's OWN module-level DB_PATH, not any connection this file holds -- so a
    caller running against a non-default --db would otherwise silently derive
    candidates from the wrong file. Same try/finally sync prune_backtest_cache_
    ground_truth.candidates_for_scope already uses (see that function's own
    docstring for the "production re-derivation" incident this guards against)."""
    import run_optimization_sweep as ros
    from run_optimization_sweep import (
        derive_phase25_candidates_ground_truth,
        build_candidate_report_ground_truth,
        print_candidate_report_ground_truth,
    )
    from prune_backtest_cache_ground_truth import _hp_for_strategy
    from phase4_candidate_nodes_resolver import derive_phase25_candidates_from_candidate_nodes

    base = {"ticker": ticker, "strategy": strategy, "config_version": version,
            "entry_timing": entry_timing, "fixed_sl": fixed_sl}
    hp = _hp_for_strategy(strategy)
    # data_source resolution (fix, 2026-08-23, backlog item logged same day): mirrors
    # scripts/run_ground_truth_phase1.py's own version-string convention (line ~217,
    # `version = "v6" + ("-massive" if data_source == "massive" else "") + ...`) and
    # scripts/paper_vs_backtest_reconcile.py's identical inline resolution -- a version
    # carrying the '-massive' marker was always swept with data_source='massive'
    # (dispatch_parallel_grid_ground_truth/run_addon_cliff_safety_ground_truth both
    # hard-require the marker whenever data_source='massive', see run_optimization_
    # sweep.py ~line 1263/2900), so the marker is a reliable, already-enforced signal,
    # not a new heuristic. Without this, build_candidate_report_ground_truth silently
    # defaulted to data_source='yahoo' regardless of version, evaluating a massive-
    # tagged campaign against Yahoo's shorter cached history (confirmed materially
    # wrong on AGQ node_id=436: 138 vs real 240 trades).
    data_source = "massive" if "-massive" in version else "yahoo"
    # window resolution (fix, 2026-08-23, sibling to the data_source fix above): a version
    # carrying the '-w{start}_{end}' suffix (window_version_suffix, run_optimization_
    # sweep.py:691) was swept over a real narrower date window -- see _window_dates_
    # from_version's own docstring for the full incident. (None, None) for a non-windowed
    # version, which build_candidate_report_ground_truth already treats as full-history.
    win_start, win_end = _window_dates_from_version(version)
    _orig_db_path = ros.DB_PATH
    try:
        ros.DB_PATH = DB_PATH
        if grid_window is not None:
            # candidate_nodes fallback (Task #3, 2026-08-29) -- see this function's own
            # grid_window docstring. Bypasses derive_phase25_candidates_ground_truth
            # entirely (it would just raise/return [] for an in-memory-only campaign).
            print(f"  [GT candidate report] using candidate_nodes fallback "
                  f"(grid_window={grid_window}) -- no backtest_cache rows expected for this scope.")
            candidates = derive_phase25_candidates_from_candidate_nodes(
                ticker, strategy, version, fixed_sl=fixed_sl, entry_timing=entry_timing,
                window=grid_window)
            if not candidates:
                print("  [GT candidate report] SKIPPED -- no candidate_nodes candidates for this scope.")
                return [{**base, "error": "no candidate_nodes candidates for this scope"}]
        else:
            try:
                candidates = derive_phase25_candidates_ground_truth(
                    ticker, strategy, version, hp, fixed_sl=fixed_sl, entry_timing=entry_timing)
            except RuntimeError as e:
                print(f"  [GT candidate report] SKIPPED -- derive_phase25_candidates_ground_truth raised: {e}")
                return [{**base, "error": f"derive_phase25_candidates_ground_truth: {e}"}]
            if not candidates:
                print("  [GT candidate report] SKIPPED -- no Phase2.5-GT candidates for this scope.")
                return [{**base, "error": "no Phase2.5-GT candidates for this scope"}]

        try:
            report = build_candidate_report_ground_truth(
                ticker, strategy, version, hp, start_date=win_start, end_date=win_end,
                fixed_sl=fixed_sl, entry_timing=entry_timing, data_source=data_source,
                candidates_override=candidates if grid_window is not None else None)
        except Exception as e:
            # Broad on purpose -- the in-progress backtester.py drought-overlay fix could
            # legitimately fail with any exception shape while it's mid-fix, not just
            # RuntimeError, and one bad scope must not kill the whole batch. But this must
            # NOT read as "known, expected, nothing to see" -- a real NEW bug in this path
            # would raise exactly the same way, so the full traceback is surfaced (not just
            # str(e)) and the wording doesn't assert the cause (paired-review finding,
            # 2026-08-23: an earlier version of this message asserted "(expected while the
            # kernel fix is in flight)" unconditionally, which would mask a genuine new
            # defect behind a reassuring label).
            import traceback
            tb = traceback.format_exc()
            print(f"  [GT candidate report] build_candidate_report_ground_truth RAISED for "
                  f"{len(candidates)} successfully-derived raw candidates (may be the known "
                  f"in-flight backtester.py dependency, or may be a new bug -- see traceback):\n{tb}")
            return [{**base, "candidate_rank": i + 1, "take_profit": c['take_profit'],
                     "stop_loss": c['stop_loss'], "max_hold_hours": c['max_hold_hours'],
                     "window": c['window'], "z_score_threshold": c['z_score_threshold'],
                     "tpct": c['tpct'], "robust_alpha_pct": c['robust_alpha'], "cagr_pct": c['cagr'],
                     "error": f"build_candidate_report_ground_truth failed: {e}"}
                    for i, c in enumerate(candidates)]
    finally:
        ros.DB_PATH = _orig_db_path

    if report.get("error"):
        print(f"  [GT candidate report] {report['error']}")
        return [{**base, "error": report["error"]}]

    print_candidate_report_ground_truth(report)
    rows = []
    for i, row in enumerate(report["candidates"]):
        c = row["candidate"]
        own = row["addon_detail"]["own_cell"] if row.get("addon_detail") else None
        d = row.get("drought")
        out = {
            **base, "candidate_rank": i + 1, "is_winner": (i == report["winner_index"]),
            "winner_metric": report.get("winner_metric"),
            "candidate_source": "candidate_nodes" if grid_window is not None else "backtest_cache",
            # real candidate_nodes.id when this row came from the candidate_nodes-sourced
            # path (phase4_candidate_nodes_resolver's `id` key, see that module's own
            # docstring) -- None for a backtest_cache-sourced row, which was never
            # promoted into candidate_nodes and so has no real candidate_id to anchor a
            # phase4_results row against (Task #2, 2026-08-29 planner dispatch, PERF).
            "candidate_id": c.get("id"),
            "take_profit": c["take_profit"], "stop_loss": c["stop_loss"],
            "max_hold_hours": c["max_hold_hours"], "window": c["window"],
            "z_score_threshold": c["z_score_threshold"], "tpct": c["tpct"],
            "robust_alpha_pct": c["robust_alpha"], "cagr_pct": c["cagr"],
            "n_trades": row["n_trades"],
            # Cache-hit/resim provenance (2026-08-29, paired-review HIGH finding "at
            # minimum" ask -- see run_optimization_sweep.build_candidate_report_ground_
            # truth's own trades-cache docstring for the staleness-invalidation design
            # this flag makes after-the-fact-detectable): True when this candidate's
            # trades came from backtest_winner_trades (kernel_version/build_id-matched),
            # False when freshly resimulated this call.
            "trades_from_cache": row.get("trades_from_cache", False),
            "core_safe": row["core_safe"], "addon_safe": row["addon_safe"],
            "core_addon_disagreement": row["core_addon_disagreement"],
            "addon_cagr_pct": own["addon_cagr"] if own else None,
            "addon_eligible": report["addon_eligible"],
            "addon_eligibility_reason": report["addon_eligibility_reason"],
            "phase4_eligible": row.get("phase4_eligible", True),
            "check4_early_wr_pct": row.get("check4_early_wr_pct"),
            "check4_late_wr_pct": row.get("check4_late_wr_pct"),
            "check8_compounded_pct": row["check8_fluke"].get("compounded_pct") if row.get("check8_fluke") else None,
            "check8_compounded_without_best_pct": row["check8_fluke"].get("compounded_without_best_pct") if row.get("check8_fluke") else None,
            "check8_best_trade_share_pct": row["check8_fluke"].get("best_trade_share_pct") if row.get("check8_fluke") else None,
            "check8_too_few_trades": row["check8_fluke"].get("too_few_trades") if row.get("check8_fluke") else None,
            "check11_max_drawdown_pct": row.get("check11_max_drawdown_pct"),
            "check11_dd_peak_time": row.get("check11_dd_peak_time"),
            "check11_dd_trough_time": row.get("check11_dd_trough_time"),
            "drought_n_core_trades": d.get("n_core_trades") if d else None,
            "drought_core_compounded_pct": d.get("core_compounded_pct") if d else None,
            "drought_best_confirm_days": d.get("best_confirm_days") if d else None,
            "drought_best_vol_gate": d.get("best_vol_gate") if d else None,
            "drought_n_windows": d.get("n_drought_windows") if d else None,
            "drought_n_simulated": d.get("n_drought_simulated") if d else None,
            "drought_compounded_pct": d.get("drought_compounded_pct") if d else None,
            "drought_combined_compounded_pct": d.get("combined_compounded_pct") if d else None,
            "drought_skip_reason": report.get("drought_skip_reason"),
        }
        folds = row.get("check13_folds") or []
        for f in range(1, 6):
            fold = next((x for x in folds if x["fold"] == f), None)
            out[f"check13_fold{f}_n"] = fold["n"] if fold else None
            out[f"check13_fold{f}_cagr_pct"] = fold["cagr"] if fold else None
            out[f"check13_fold{f}_fragile"] = fold["fragile"] if fold else None
        rows.append(out)
    return rows


def run_gt_mode(conn, tickers, metric, min_alpha_arg, csv_name, xlsx_name, grid_window_filter=None,
                 version_filter=None):
    """--kernel gt entry point: loops every real GT scope for `tickers`, printing
    each scope's full candidate report (print_candidate_report_ground_truth) plus
    a top_safe_nodes cross-check to the terminal, and returns the flat GT_COLUMN_
    DEFS row list for optional --csv/--xlsx export. gt_rows_for_scope itself never
    raises (it catches its own real failure modes and returns error rows instead),
    but this loop's own per-scope work (the cross-check call, row accumulation) is
    ALSO wrapped per-scope -- a scope-level exception here logs and continues rather
    than losing every already-accumulated row before a --csv/--xlsx write (paired-
    review finding, 2026-08-23, against an earlier version with no such guard).

    candidate_nodes fallback (2026-08-29, Task #3, additive): gt_scopes_for_tickers
    only reads backtest_cache, so a campaign the in-memory sweep pipeline ran (zero
    backtest_cache rows) is otherwise invisible here -- without this, gt_rows_for_
    scope's own grid_window param has no reachable caller (paired-review HIGH finding,
    confirmed on rebuttal: 2026-08-29). Adds every real candidate_nodes scope for
    `tickers` not already covered by a backtest_cache scope (same (strategy,
    entry_timing, fixed_sl) match convention scripts/phase5_second_level_overlay_
    check.py's own main() already uses), each carrying its own real window value.
    `grid_window_filter`, when set, additionally restricts the candidate_nodes
    fallback to that one window -- use it whenever a version is known to alias
    multiple unrelated batches (see phase4_candidate_nodes_resolver.py's own
    `window` param docstring); has no effect on backtest_cache-sourced scopes,
    which don't carry this ambiguity.

    `version_filter` (added 2026-08-30, planner dispatch): when set, restricts the
    candidate_nodes fallback to that one exact version string, instead of every
    version discover_all_candidate_nodes_scopes finds for the ticker. Real gap this
    closes: with no filter, a ticker that has ever been swept under N historical
    campaigns re-processes ALL N every Phase4 run, including long-stale orphan rows
    from pre-GT sweeps (confirmed on real data 2026-08-29: SOXL alone has 20 distinct
    candidate_nodes versions) -- wasted compute and noisy logs, not a correctness bug
    (Phase5 was already unaffected, since it's version-scoped). Has no effect on
    backtest_cache-sourced scopes (gt_scopes_for_tickers itself is not version-
    filtered here -- out of scope for this fix, matches grid_window_filter's own
    backtest_cache-scopes-unaffected precedent)."""
    from phase4_candidate_nodes_resolver import discover_all_candidate_nodes_scopes

    scopes = [(s[0], s[1], s[2], s[3], s[4], None) for s in gt_scopes_for_tickers(conn, tickers)]
    covered = {(s[0], s[1], s[3], s[4]) for s in scopes}  # (ticker, strategy, entry_timing, fixed_sl)
    for ticker in tickers:
        for strategy, version, entry_timing, fixed_sl, window in discover_all_candidate_nodes_scopes(ticker):
            if (ticker, strategy, entry_timing, fixed_sl) in covered:
                continue
            if grid_window_filter is not None and window != grid_window_filter:
                continue
            if version_filter is not None and version != version_filter:
                continue
            scopes.append((ticker, strategy, version, entry_timing, fixed_sl, window))

    found = {s[0] for s in scopes}
    for ticker in tickers:
        if ticker not in found:
            print(f"\n{ticker}: no GT (kernel_version='ground_truth_v6') scope found in backtest_cache "
                  f"or candidate_nodes -- skipping.")

    all_rows = []
    for ticker, strategy, version, entry_timing, fixed_sl, grid_window in scopes:
        print(f"\n{'#'*100}\n{ticker} / {strategy} / {version} / entry_timing={entry_timing} "
              f"/ fixed_sl={fixed_sl}"
              f"{f' / grid_window={grid_window}' if grid_window is not None else ''}\n{'#'*100}")
        try:
            if grid_window is None:
                node = gt_current_best_node(conn, ticker, strategy, version, entry_timing, fixed_sl,
                                             metric, min_alpha_arg)
                if node is None:
                    print(f"  [top_safe_nodes cross-check] no cliff-safe node found for {metric} floor")
                else:
                    print(f"  [top_safe_nodes cross-check] best {metric}: arm/tp={node['arm_pct']} sl={node['sl']} "
                          f"hold={node['hold']}h window={node['window']} z={node['z']} "
                          f"robust_alpha={node['alpha']:+.1f}% cagr={node['cagr']}")
            else:
                # top_safe_nodes cross-check is backtest_cache-only (best_row/gt_current_
                # best_node) -- no equivalent exists for a candidate_nodes-sourced scope,
                # skip rather than print a misleading "no cliff-safe node found".
                print("  [top_safe_nodes cross-check] skipped -- candidate_nodes-sourced scope, "
                      "no backtest_cache equivalent.")
            all_rows.extend(gt_rows_for_scope(ticker, strategy, version, entry_timing, fixed_sl,
                                               grid_window=grid_window))
        except Exception as e:
            print(f"  UNEXPECTED error on this scope, skipping: {e}")

    if csv_name:
        _write_csv(csv_name, all_rows, col_defs=GT_COLUMN_DEFS, to_record=lambda r: r)
    if xlsx_name:
        _write_xlsx(xlsx_name, all_rows, col_defs=GT_COLUMN_DEFS, to_record=lambda r: r)

    # Greppable per-ticker completion marker (2026-08-29), matching bench_phase1_
    # phase2_inmemory.py's own "PROGRESS: <Phase> done ticker=..." print convention --
    # so `grep "PROGRESS:" logfile` shows real progress through Phase4/5 too, not just
    # Phase1-2.5, across a long unattended multi-ticker queue run.
    for ticker in tickers:
        n_rows = sum(1 for r in all_rows if r.get("ticker") == ticker)
        n_scopes = sum(1 for s in scopes if s[0] == ticker)
        print(f"PROGRESS: Phase4 done ticker={ticker}: {n_rows} candidate rows "
              f"across {n_scopes} scope(s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--kernel", choices=["legacy", "gt"], default="legacy",
                     help="'legacy' (default, unchanged behavior): candidate_nodes/run_overlay_shim-based "
                          "3-5-row-per-ticker report. 'gt': v6 GT-kernel pipeline (derive_phase25_candidates_"
                          "ground_truth -> build_candidate_report_ground_truth), up-to-9 rows/scope -- see "
                          "GT_COLUMN_DEFS, a separate column schema from the legacy report's COLUMN_DEFS.")
    ap.add_argument("--tranche", type=int, default=None,
                     help="--kernel gt only: source tickers from scripts/gt_tranches.txt's tranche N "
                          "instead of positional args.")
    ap.add_argument("--grid-window", type=int, default=None,
                     help="--kernel gt only: restrict the candidate_nodes fallback (a campaign the "
                          "in-memory sweep pipeline ran, invisible to the normal backtest_cache path) "
                          "to one real sweep-grid window value -- REQUIRED whenever a version string "
                          "aliases multiple unrelated in-memory batches (see phase4_candidate_nodes_"
                          "resolver.py's own `window` param docstring). Has no effect on backtest_cache-"
                          "sourced scopes. Omit to include every candidate_nodes window found.")
    ap.add_argument("--metric", choices=["robust_alpha", "cagr"], default=None,
                     help="--kernel gt only: metric for the top_safe_nodes cross-check (see that script's "
                          "own --metric help). Default: cagr under --kernel gt (2026-08-23, "
                          "ground_truth_kernel_rebuild.md Step 4 -- CAGR is the sole GT selection metric); "
                          "robust_alpha under --kernel legacy (unchanged).")
    ap.add_argument("--version", default=None,
                     help="--kernel legacy: force a single version for every ticker (old behavior). "
                          "Default: auto-resolve per ticker via resolve_version() -- v5.1 when the ticker "
                          "has it, else v5. --kernel gt: restrict the candidate_nodes fallback to this one "
                          "exact version string instead of auto-discovering every version ever swept for "
                          "the ticker (added 2026-08-30 -- see run_gt_mode's version_filter docstring). Has "
                          "no effect on backtest_cache-sourced GT scopes either way.")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--min-alpha", type=float, default=200,
                     help="Alpha floor for the 'best safe node' cliff-safety search (default 200%%, matching "
                          "top_safe_nodes.py's convention). 'best unsafe node'/'5min best possible' are always "
                          "shown regardless of this floor.")
    ap.add_argument("--skip-5min", action="store_true",
                     help="skip the 5-min fill-accuracy replay (saves a yfinance call per ticker)")
    ap.add_argument("--skip-overlay", action="store_true",
                     help="skip computing drought/addon overlay for candidates that don't have it yet "
                          "(faster, but addon/drought columns stay blank for un-registered candidates)")
    ap.add_argument("--csv", default=None, help="write output/<name>.csv instead of the wide terminal table")
    ap.add_argument("--xlsx", default=None,
                     help="write output/<name>.xlsx (Candidates sheet + Column Definitions glossary sheet) "
                          "instead of the wide terminal table")
    args = ap.parse_args()
    if args.metric is None:
        # GT default is cagr (2026-08-23, ground_truth_kernel_rebuild.md Step 4);
        # legacy default stays robust_alpha -- unchanged.
        args.metric = "cagr" if args.kernel == "gt" else "robust_alpha"

    if args.kernel == "gt":
        # Resolve tickers (may raise ValueError for an unknown --tranche) BEFORE opening
        # conn -- paired-review finding, 2026-08-23: the earlier version opened conn first,
        # so an unknown --tranche's ValueError left it unclosed (harmless at process exit,
        # but the only GT exit path without a close()).
        tickers = load_gt_tranche(args.tranche) if args.tranche is not None else args.tickers
        if not tickers:
            print("--kernel gt requires --tranche N or an explicit ticker list.")
            return
        print(f"Tickers: {' '.join(tickers)}")
        conn = sqlite3.connect(args.db)
        try:
            run_gt_mode(conn, tickers, args.metric, args.min_alpha, args.csv, args.xlsx,
                        grid_window_filter=args.grid_window, version_filter=args.version)
        finally:
            conn.close()
        return

    conn = sqlite3.connect(args.db)

    ensure_candidate_nodes_table(conn)
    ensure_overlay_table(conn)
    tickers = args.tickers
    if not tickers:
        c = conn.cursor()
        c.execute("SELECT DISTINCT ticker FROM candidate_nodes")
        tickers = [r[0] for r in c.fetchall()]

    rows = []
    for ticker in tickers:
        try:
            version = args.version or resolve_version(conn, ticker)
        except RuntimeError as e:
            # resolve_version() refuses tickers with real GT (ground_truth_v6) data rather
            # than silently falling back to stale v5/v5.1 -- added 2026-08-23. Skip just
            # this ticker instead of aborting the whole default (all-tickers) run -- same
            # fix as candidate_full_review.py's Task #5 (found via a post-hoc review of
            # this file's own --kernel gt addition asking whether this loop shared that
            # all-tickers-abort bug shape; it did, pre-existing, unrelated to --kernel gt).
            print(f"Skipping {ticker}: {e}")
            continue
        rows.extend(build_rows_for_ticker(conn, ticker, version, args.min_alpha, args.skip_5min,
                                           args.skip_overlay))

    conn.close()

    if args.xlsx:
        _write_xlsx(args.xlsx, rows)
        return
    if args.csv:
        _write_csv(args.csv, rows)
        return

    hdr = "%-8s %-20s %12s %9s %9s %6s %6s %10s %14s %9s %6s %20s %20s %12s %12s" % (
        "Ticker", "Candidate", "Liquidity$/d", "CoreA%", "AbsRet%", "Years", "Trades", "AnnExcess%",
        "FillAcc(win%,err%)", "WorstNb%", "Status", "Addon(n,comp%,WR%)", "Drought(n,comp%,WR%)",
        "x Addon%", "x Drought%")
    print(hdr)
    print("(x columns are a NAIVE multiplicative estimate, not real stacked-model math -- see docstring)")
    print("(AnnExcess%/CoreA% are SPY-adjusted; AbsRet% is not -- see COLUMN_DEFS. Years/Trades sit next to "
          "AnnExcess%: a big number backed by a short history / few trades is weaker evidence.)")
    print("(Addon/Drought columns are blank unless THIS row's exact params match a registered candidate_nodes "
          "entry that was actually run through the overlay shim -- not repeated across a ticker's 3 rows.)")

    ticker_best = {}
    for row in rows:
        if row[1] is None:
            continue
        ticker_best[row[0]] = max(ticker_best.get(row[0], -1e9), row[3])

    def sort_key(row):
        ticker = row[0]
        best = ticker_best.get(ticker, -1e9) if row[1] is not None else -1e9
        type_rank = CANDIDATE_TYPE_ORDER.index(row[1]) if row[1] in CANDIDATE_TYPE_ORDER else 99
        return (-best, ticker, type_rank)

    for row in sorted(rows, key=sort_key):
        rec = _row_to_record(row)
        if rec.get("core_alpha_pct") is None:
            print(f"{rec['ticker']:8} NO_DATA")
            continue
        wn_str = f"{rec['worst_neighbor_pct']:>9.1f}" if rec['worst_neighbor_pct'] is not None else "      n/a"
        ae_str = f"{rec['ann_excess_pct']:+.1f}" if rec['ann_excess_pct'] is not None else "-"
        fa_str = (f"{rec['fillacc_possible_win_pct']:.0f}%,{rec['fillacc_possible_mean_err_pct']:.2f}%,"
                  f"n={rec['fillacc_n']}") if rec['fillacc_possible_win_pct'] is not None else "-"
        ao_str = (f"{rec['addon_n']},{rec['addon_compounded_pct']:+.2f}%,{rec['addon_win_rate_pct']:.0f}%"
                  if rec['addon_n'] is not None else "-")
        dr_str = (f"{rec['drought_n']},{rec['drought_compounded_pct']:+.2f}%,{rec['drought_win_rate_pct']:.0f}%"
                  if rec['drought_n'] is not None else "-")
        am_str = f"{rec['x_addon_pct']:+.1f}" if rec['x_addon_pct'] is not None else "-"
        dm_str = f"{rec['x_drought_pct']:+.1f}" if rec['x_drought_pct'] is not None else "-"
        liq = rec['liquidity_dollars_per_day']
        liq_str = f"${liq:,.0f}" if liq is not None else "n/a"
        print(f"{rec['ticker']:8} {rec['candidate_type']:<20} {liq_str:>12} {rec['core_alpha_pct']:>9.1f} "
              f"{rec['abs_return_pct']:>9.1f} {rec['years']!s:>6} {rec['trades']:>6} {ae_str:>10} {fa_str:>14} "
              f"{wn_str} {rec['status']:>6} {ao_str:>20} {dr_str:>20} {am_str:>12} {dm_str:>12}")


if __name__ == "__main__":
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
