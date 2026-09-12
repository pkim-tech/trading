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
import json
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

# Pre-Phase4 trade-count floor (2026-08-31, planner dispatch): a real hard pipeline
# gate, not just a report-layer flag -- user's own ~10 trades/year sampling-confidence
# reasoning over the campaign's real ~5yr window (2021-08-23 to 2026-08-21). Resolves
# the earlier "not scoped -- hard filter vs soft/informational column, report vs
# pipeline" open question the same night's backlog raised. Skips Phase4's own
# expensive addon/drought computation entirely for a candidate below this floor (see
# gt_rows_for_scope's own filter, right after the candidate_nodes fallback resolves its
# candidate list) -- only meaningful for the candidate_nodes-sourced path, since that's
# the only one whose candidate dicts carry a real 'trades' count (phase4_candidate_
# nodes_resolver.py); the legacy backtest_cache path's own candidates_override is never
# read by build_candidate_report_ground_truth for that path (candidates_override=None),
# so filtering there would be a no-op, not a real gate -- deliberately left alone
# rather than touching run_optimization_sweep.py (gated) just to add one.
MIN_TRADES_FOR_PHASE4 = 50
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
                 "winner -- ranked by the SAME effective cagr this report's own cagr_pct column shows (row's "
                 "recomputed core_cagr when available, else the candidate's stale sweep-time cagr; 2026-09-07 fix "
                 "-- previously ranked on the stale sweep-time value alone, which silently disagreed with a "
                 "candidate_source='candidate_nodes' scope's own displayed cagr_pct). Falls back to "
                 "robust_alpha_pct only when EVERY candidate in the scope has no effective cagr at all.",
    "winner_metric": "Which column actually decided is_winner for this scope: 'cagr' (normal path, now includes "
                      "a candidate_nodes scope with a real recomputed core_cagr) or 'robust_alpha' (only when no "
                      "candidate in the scope has any effective cagr).",
    "candidate_source": "'backtest_cache' (normal GT path, derive_phase25_candidates_ground_truth) or "
                         "'candidate_nodes' (fallback for a campaign the in-memory sweep pipeline ran, which "
                         "writes zero backtest_cache rows -- scripts/phase4_candidate_nodes_resolver.py). A "
                         "candidate_nodes-sourced row's own candidate['cagr'] is always None (no sweep-time "
                         "CAGR is ever persisted there), but cagr_pct itself is NOT None here as of 2026-09-07 "
                         "(paired-review HIGH finding #1) -- it's recomputed from this row's real `trades` "
                         "(see trades_resolution), the same way every other row's cagr_pct is. "
                         "phase4_eligible always True (no CAGR-based gate exists without a real sweep-time "
                         "cagr -- see phase4_eligible's own note).",
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
    "cagr_pct": "Real annualized CAGR for this candidate's own cell -- as of 2026-09-07 (paired-review HIGH "
                "finding #1), recomputed from this row's own `trades` list (whatever resolution actually "
                "produced it -- see trades_resolution), NOT robust_alpha_pct's sweep-time source; falls back "
                "to the sweep-time value only when trades produced no computable CAGR.",
    "trades_resolution": "Which real trade source produced this row's `trades`/cagr_pct/checks 4-13: '1s' "
                          "(phase5_trades, trusted), 'second_cache'/'second_resim' (backtest_winner_trades or "
                          "a fresh resim, both true 1-second resolution), 'minute_cache'/'minute_resim' (minute "
                          "resolution), or None (legacy row/no trades). Added 2026-09-07 (paired-review HIGH "
                          "finding #3) so a post-1s-fix row is distinguishable from a pre-fix one.",
    "second_build_id": "The real db_cache.get_active_build_id(ticker, 'second') value active when "
                       "trades_resolution was decided for this row -- None when data_source != 'massive' or no "
                       "second build was active. Added 2026-09-07 so a scope re-verified after a NEWER second "
                       "build is promoted (see docs/deep_backlog.md's 2026-08-27 SOXL/DPST/DFEN incident) isn't "
                       "mistaken for already covered just because trades_resolution=='1s' from an OLDER build.",
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
    "core_addon_cagr_ungated_pct": "Core+add-on stacked CAGR (UNGATED -- the add-on leg contributes its raw number "
                            "whether or not it passed its own chrono-split robustness check; the `_ungated` suffix "
                            "distinguishes it from candidate_full_review.py's identically-shaped but GATED "
                            "core_addon_cagr_pct, a genuinely different number). Computed by Phase4 off this row's "
                            "own trade list (same list cagr_pct/trades_resolution describe). Phase4-side replacement "
                            "for Phase5's addon_cagr_1s -- see run_optimization_sweep._stacked_overlay_cagrs_gt. "
                            "Blank when phase4_eligible is False (skipped, not worth the compute).",
    "core_drought_cagr_ungated_pct": "Core+drought stacked CAGR (UNGATED, same suffix rationale as "
                              "core_addon_cagr_ungated_pct above), from the drought sweep's own combined compounded "
                              "return. Phase4-side replacement for Phase5's drought_cagr_1s. Blank when "
                              "phase4_eligible is False, and also blank whenever drought was never computed.",
    "core_both_cagr_pct": "Core+add-on+drought triple-stacked CAGR, each overlay independently GATED on its own "
                           "chronological-split robustness verdict (plus the drought included-vs-excluded "
                           "REAL_SELECTION vol-gate override). Phase4-side replacement for Phase5's "
                           "core_both_cagr_1s. Unlike its two ungated siblings above, this one IS the same "
                           "quantity candidate_full_review.py's core_both_cagr_pct means, so it keeps that name. "
                           "Blank when phase4_eligible is False.",
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
    "drought_skip_reason": "Set when drought was never computed for this scope at all (strategy fails "
                            "strategies.uses_arm_trail_exit()) -- see run_optimization_sweep.py's drought_skip_reason.",
    # Overlay-inclusive Check11/Check13 risk checks (2026-09-11 -- backlog item found
    # 2026-09-08: every check11/check13 above is CORE-only, so a promoted addon/drought
    # node had zero drawdown/fold-fragility verification on the overlay portion of its
    # equity curve). One (max_drawdown_pct, worst_fold_cagr_pct, any_fold_fragile,
    # n_folds_populated) quadruple per equity curve, computed by run_optimization_sweep.
    # _overlay_risk_checks_gt off this row's own `trades`/`drought`, folded against the
    # SAME (start, end) time axis (the real backtest span) for every combo -- see that
    # function's docstring for exactly what each curve is, why the 3 core-inclusive
    # curves carry `_ungated` (a real, different curve from this row's own gated
    # core_both_cagr_pct -- do not compare them directly), and why n_folds_populated
    # exists (an empty check13 fold reads as "not fragile", indistinguishable from a
    # genuinely healthy fold -- n_folds_populated < 5 means "too sparse to trust
    # any_fold_fragile"). All four blank together for a curve with zero trades (e.g.
    # drought_only when drought found no windows, or addon_only when no trade ever
    # armed) or floor-breach-poisoned (core_addon_ungated/core_both_ungated only, when
    # any blended trade's Return < -100%) -- and, same as addon_cagr_pct/
    # core_both_cagr_pct above, blank whenever phase4_eligible is False.
    **{f"{p}_max_drawdown_pct": f"Overlay-inclusive Check 11: max peak-to-trough compounded-equity "
                                 f"drawdown (<=0) on the {label} equity curve."
       for p, label in (("addon_only", "addon-only (armed-leg-only, unblended)"),
                        ("core_addon_ungated", "core+addon (blended, UNGATED)"),
                        ("drought_only", "drought-window-only"),
                        ("core_drought_ungated", "core+drought (UNGATED)"),
                        ("core_both_ungated", "core+addon+drought triple-stack (UNGATED)"))},
    **{f"{p}_worst_fold_cagr_pct": f"Overlay-inclusive Check 13: worst of the 5 equal-time-span folds' "
                                    f"own CAGR on the {label} equity curve, folded against the real "
                                    f"backtest span (not this curve's own, possibly sparse, extent)."
       for p, label in (("addon_only", "addon-only (armed-leg-only, unblended)"),
                        ("core_addon_ungated", "core+addon (blended, UNGATED)"),
                        ("drought_only", "drought-window-only"),
                        ("core_drought_ungated", "core+drought (UNGATED)"),
                        ("core_both_ungated", "core+addon+drought triple-stack (UNGATED)"))},
    **{f"{p}_any_fold_fragile": f"Overlay-inclusive Check 13: True if any of the 5 folds on the {label} "
                                 f"equity curve had CAGR<=GT_ROBUSTNESS_CAGR_MIN (20%) or was fully wiped "
                                 f"out. Check {p}_n_folds_populated before trusting a False here."
       for p, label in (("addon_only", "addon-only (armed-leg-only, unblended)"),
                        ("core_addon_ungated", "core+addon (blended, UNGATED)"),
                        ("drought_only", "drought-window-only"),
                        ("core_drought_ungated", "core+drought (UNGATED)"),
                        ("core_both_ungated", "core+addon+drought triple-stack (UNGATED)"))},
    **{f"{p}_n_folds_populated": f"Overlay-inclusive Check 13: how many of the 5 folds on the {label} "
                                  f"equity curve had >=1 real trade. <5 means any_fold_fragile=False may "
                                  f"just mean 'too sparse to evaluate', not 'genuinely robust'."
       for p, label in (("addon_only", "addon-only (armed-leg-only, unblended)"),
                        ("core_addon_ungated", "core+addon (blended, UNGATED)"),
                        ("drought_only", "drought-window-only"),
                        ("core_drought_ungated", "core+drought (UNGATED)"),
                        ("core_both_ungated", "core+addon+drought triple-stack (UNGATED)"))},
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


def gt_rows_for_scope(ticker, strategy, version, entry_timing, fixed_sl, grid_window=None,
                       addon_cliff_workers=6):
    """addon_cliff_workers (2026-09-11, paired-review HIGH finding -- contextual Opus
    review, both independent-cold and contextual converged on the underlying bug): the
    nesting-avoidance override belongs at the CALL SITE that's actually nested inside an
    outer pool (_run_one_gt_scope_worker, itself a run_gt_mode ProcessPoolExecutor
    worker), NOT hardcoded inside this shared function -- run_candidate_nodes_campaign_
    verification.py also calls this function, in a fully serial loop with NO outer pool
    at all, and a hardcoded workers=1 here would have silently starved that real,
    primary phase4_results-persisting consumer of the entire parallelization fix. Default
    6 (parallel ON) so every caller except the one that's actually nested gets the real
    speedup with zero code change required."""
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
            # Pre-Phase4 trade-count floor (2026-08-31, planner dispatch): filters BEFORE
            # candidates_override reaches build_candidate_report_ground_truth below, so a
            # too-few-trades candidate never gets its (expensive) addon/drought computed
            # at all -- not just excluded from the printed report afterward. See
            # MIN_TRADES_FOR_PHASE4's own module-level docstring for the real rationale.
            n_before_trades_filter = len(candidates)
            # c.get("trades") is not None (not the earlier `c.get("trades", 0)`, 2026-08-31
            # paired-review LOW finding): a missing `trades` key must fail OPEN (don't
            # filter), matching the sibling worst_neighbor_cagr filter's posture just below
            # -- the earlier `, 0` default would have SILENTLY EXCLUDED every candidate from
            # a future candidate source that omits this key, misreporting it as "below the
            # trade-count floor" when it's really "unknown." Not live today (the resolver
            # always sets `trades`), but the two adjacent filters must not disagree on this.
            candidates = [c for c in candidates
                          if c.get("trades") is None or c["trades"] >= MIN_TRADES_FOR_PHASE4]
            n_skipped_low_trades = n_before_trades_filter - len(candidates)
            if n_skipped_low_trades:
                print(f"  [GT candidate report] pre-Phase4 trade-count floor: skipping "
                      f"{n_skipped_low_trades} of {n_before_trades_filter} candidate(s) "
                      f"with < {MIN_TRADES_FOR_PHASE4} trades (Phase4 addon/drought never "
                      f"computed for them).")
            if not candidates:
                print("  [GT candidate report] SKIPPED -- every candidate_nodes candidate "
                      "for this scope was below the trade-count floor.")
                return [{**base, "error": "every candidate below MIN_TRADES_FOR_PHASE4"}]
            # Pre-Phase4 core-safety floor (2026-08-31, planner dispatch): same filtering
            # point/mechanism as the trade-count floor above -- skips Phase4's own addon/
            # drought computation entirely for a candidate bench_phase1_phase2_inmemory.py
            # already flagged core-CLIFF via its own worst_neighbor_cagr (persisted by
            # _insert_candidate_nodes_rows). Uses the SAME `< 0` threshold Phase4's own
            # independent run_addon_cliff_safety_ground_truth uses for core_cliff (see
            # that function's own docstring) -- the two computations are NOT structurally
            # coupled (an earlier attempt to restructure run_addon_cliff_safety_ground_
            # truth to trust this value directly was rejected: tracing its neighbor loop
            # showed the same per-cell simulations are needed regardless for the addon-
            # side computation, so restructuring wouldn't even save compute). This is NOT
            # just "matching thresholds by convention," though -- it's provably ONE-
            # DIRECTIONAL SAFE by construction (2026-08-31, paired-review finding, both
            # independent-cold and contextual review converged on this): bench's own
            # neighborhood (+-CLIFF_RADIUS tp/sl, hold/window/z/trail_pct all PINNED to
            # the candidate's own values, only cells already present in df_final) is a
            # STRICT SUBSET of Phase4's own neighborhood for the same candidate (same
            # +-CLIFF_RADIUS tp/sl, but ALSO sweeps hold +-7h and adjacent trail_pct
            # values, and always evaluates every cell in that box, not just whatever
            # happened to already exist in df_final) -- and both sides evaluate each cell
            # through the same underlying kernel convention (same_bar_reentry=True, same
            # massive/yahoo data_source, confirmed by reading both call sites). A strict
            # subset of cells can never have a LOWER minimum than the full box, so
            # worst_neighbor_cagr (bench's partial-box min) is always >= Phase4's own
            # worst_neighbor_core (its full-box min) for the same candidate -- meaning
            # this pre-filter can only ever exclude a candidate Phase4's OWN computation
            # would ALSO have flagged CLIFF; it can never falsely exclude a real winner.
            # THIS INVARIANT BREAKS if bench's own neighborhood is ever widened to vary
            # hold/window/z/trail_pct, or to search past CLIFF_RADIUS, or to include cells
            # NOT already in df_final -- any of those would make bench's box no longer a
            # subset of Phase4's, and the "can only under-promote, never over-exclude"
            # guarantee would silently stop holding. Anyone touching the "Cliff-safety
            # verdict per candidate" neighbor-selection logic in bench_phase1_phase2_
            # inmemory.py must re-check this invariant, not just assume it still holds.
            # Empirical validation to date is weak (2026-08-31, contextual review): the
            # only real sample (9 AGQ seed-mode candidates) all landed comfortably SAFE
            # (+30% to +58%), nowhere near the 0 boundary -- confirms agreement on the
            # SAFE side only, does NOT independently exercise the CLIFF/exclusion side.
            # The subset argument above is what actually makes this safe, not that
            # sample -- a real CLIFF-side sample is still worth running before fully
            # trusting this in a live campaign. Fails OPEN (does not filter) when
            # worst_neighbor_cagr is None (no real neighbor existed to judge from) --
            # "unknown" is never treated as "unsafe," matching Phase4's own core_cliff
            # None-handling convention (contrast with Phase5's OWN SAFE/SAFE gate
            # downstream, phase5_second_level_overlay_check.py's _filter_to_safe_
            # candidates, which does the OPPOSITE for its own core_safe=None case --
            # EXCLUDES an unverified candidate rather than passing it through, since that
            # gate's whole point is "don't verify anything nobody has judged yet." Two
            # different postures for two different purposes, not a contradiction.)
            n_before_core_safety_filter = len(candidates)
            candidates = [c for c in candidates
                          if c.get("worst_neighbor_cagr") is None or c["worst_neighbor_cagr"] >= 0]
            n_skipped_core_cliff = n_before_core_safety_filter - len(candidates)
            if n_skipped_core_cliff:
                print(f"  [GT candidate report] pre-Phase4 core-safety floor: skipping "
                      f"{n_skipped_core_cliff} of {n_before_core_safety_filter} candidate(s) "
                      f"with worst_neighbor_cagr < 0 (Phase4 addon/drought never computed "
                      f"for them).")
            if not candidates:
                print("  [GT candidate report] SKIPPED -- every candidate_nodes candidate "
                      "for this scope was core-CLIFF.")
                return [{**base, "error": "every candidate core-CLIFF (worst_neighbor_cagr < 0)"}]
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
                candidates_override=candidates if grid_window is not None else None,
                # addon_cliff_workers threaded straight through from gt_rows_for_scope's
                # own param (2026-09-11 paired-review HIGH finding fix -- the nesting-
                # avoidance override moved OUT of this shared function and into whichever
                # real call site is actually nested inside an outer pool; see that
                # param's own docstring).
                addon_cliff_workers=addon_cliff_workers)
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
            "robust_alpha_pct": c["robust_alpha"],
            # cagr_pct (2026-09-07, Review-Gate Persistence Rule item -- paired-review
            # HIGH finding #1): prefer row['core_cagr'], recomputed from the actual
            # `trades` list this row's checks 4/8/11/13 ran against (resolution-aware --
            # '1s'/'second_resim' when available, see trades_resolution below) -- over
            # c['cagr'], the stale Phase1/2 sweep-time value (always minute-resolution,
            # and always None for a candidate_nodes-sourced row, which is why this used
            # to print "cagr=N/A" for every such candidate regardless of real trades).
            # Falls back to c['cagr'] only if core_cagr itself is None (e.g. trades
            # empty) so a real, if stale, number is still better than a hard None.
            "cagr_pct": row.get("core_cagr") if row.get("core_cagr") is not None else c["cagr"],
            "n_trades": row["n_trades"],
            # Cache-hit/resim provenance (2026-08-29, paired-review HIGH finding "at
            # minimum" ask -- see run_optimization_sweep.build_candidate_report_ground_
            # truth's own trades-cache docstring for the staleness-invalidation design
            # this flag makes after-the-fact-detectable): True when this candidate's
            # trades came from backtest_winner_trades (kernel_version/build_id-matched),
            # False when freshly resimulated this call.
            "trades_from_cache": row.get("trades_from_cache", False),
            # Real per-row resolution marker (2026-09-07, Review-Gate Persistence Rule
            # item -- paired-review HIGH finding #3): '1s'/'minute_cache'/'minute_resim'/
            # 'second_resim' (see build_candidate_report_ground_truth's own trades_
            # resolution comment) -- persisted (phase4_results.trades_resolution below)
            # so a post-fix row is distinguishable from a pre-fix one after the fact.
            "trades_resolution": row.get("trades_resolution"),
            # second_build_id (2026-09-07, Review-Gate Persistence Rule item --
            # contextual paired-review HIGH finding): the specific massive_second_
            # derived build_id active when trades_resolution was decided -- see
            # run_optimization_sweep.build_candidate_report_ground_truth's own comment.
            "second_build_id": row.get("second_build_id"),
            "core_safe": row["core_safe"], "addon_safe": row["addon_safe"],
            "core_addon_disagreement": row["core_addon_disagreement"],
            "addon_cagr_pct": own["addon_cagr"] if own else None,
            # Phase4's own stacked overlay CAGRs (2026-09-08 Phase5-consolidation) --
            # distinct from "addon_cagr_pct" directly above, which is the add-on cliff-
            # safety pass's OWN-CELL number. These three are the per-candidate core+addon
            # / core+drought / gated triple-stack, computed off the same trade list as
            # cagr_pct -- see run_optimization_sweep._stacked_overlay_cagrs_gt. They are
            # the Phase4-side replacement for Phase5's addon_cagr_1s/drought_cagr_1s/
            # core_both_cagr_1s (real percentages here; Phase5 stored raw fractions).
            # `_ungated` suffix on the first two is load-bearing -- candidate_full_review.py
            # emits GATED numbers under the unsuffixed names (see GT_COLUMN_DEFS above).
            "core_addon_cagr_ungated_pct": row.get("core_addon_cagr_ungated"),
            "core_drought_cagr_ungated_pct": row.get("core_drought_cagr_ungated"),
            "core_both_cagr_pct": row.get("core_both_cagr"),
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
            # Overlay-inclusive Check11/Check13 risk checks (2026-09-11 backlog item) --
            # already aggregate-summarized by _overlay_risk_checks_gt, threaded straight
            # through from `row` (build_candidate_report_ground_truth's own output). The
            # 3 core-inclusive prefixes carry `_ungated` -- see that function's docstring.
            **{f"{p}_{s}": row.get(f"{p}_{s}")
               for p in ("addon_only", "core_addon_ungated", "drought_only",
                         "core_drought_ungated", "core_both_ungated")
               for s in ("max_drawdown_pct", "worst_fold_cagr_pct", "any_fold_fragile", "n_folds_populated")},
        }
        folds = row.get("check13_folds") or []
        for f in range(1, 6):
            fold = next((x for x in folds if x["fold"] == f), None)
            out[f"check13_fold{f}_n"] = fold["n"] if fold else None
            out[f"check13_fold{f}_cagr_pct"] = fold["cagr"] if fold else None
            out[f"check13_fold{f}_fragile"] = fold["fragile"] if fold else None
        rows.append(out)
    return rows


# Every other Phase4 checklist field gt_rows_for_scope already computes and prints but
# never persists (2026-08-30, planner dispatch, scope expansion on the same task): check4
# win-rate stability, check8 fluke check, check11 max drawdown, check13 walk-forward
# 5-fold, and the TrailingBoth-only drought overlay. Explicit key list (not "everything in
# `out`") so this blob doesn't accidentally sweep in fields that already have their own
# real columns/purpose (ticker/strategy/config_version/candidate_id/core_safe/addon_safe/
# take_profit/etc.) or scope-level fields that aren't really per-candidate (drought_
# skip_reason is scope-level, included anyway since it explains why every drought_* field
# below might be None for this candidate).
_PHASE4_CHECKLIST_KEYS = [
    "check4_early_wr_pct", "check4_late_wr_pct",
    "check8_compounded_pct", "check8_compounded_without_best_pct",
    "check8_best_trade_share_pct", "check8_too_few_trades",
    "check11_max_drawdown_pct", "check11_dd_peak_time", "check11_dd_trough_time",
    "drought_n_core_trades", "drought_core_compounded_pct", "drought_best_confirm_days",
    "drought_best_vol_gate", "drought_n_windows", "drought_n_simulated",
    "drought_compounded_pct", "drought_combined_compounded_pct", "drought_skip_reason",
] + [f"check13_fold{f}_{suffix}" for f in range(1, 6) for suffix in ("n", "cagr_pct", "fragile")] + [
    f"{p}_{s}" for p in ("addon_only", "core_addon_ungated", "drought_only",
                         "core_drought_ungated", "core_both_ungated")
    for s in ("max_drawdown_pct", "worst_fold_cagr_pct", "any_fold_fragile", "n_folds_populated")
]


def _persist_phase4_verdicts_and_checklist(rows):
    """Persist Phase4's per-candidate output onto candidate_nodes (2026-08-30, planner
    dispatch): Phase4 already computes all of this (build_candidate_report_ground_truth,
    in run_optimization_sweep.py -- a gated backtest-kernel module, deliberately NOT
    touched here; this just persists what gt_rows_for_scope already reads off its output
    and has always printed to console/CSV/xlsx) -- but never wrote any of it back onto the
    candidate_nodes row it came from, so a later session/script had no way to look it up
    without re-running Phase4. Additive UPDATE only -- never inserts a new candidate_nodes
    row, and a backtest_cache-sourced row (candidate_id is None, never promoted into
    candidate_nodes) has nothing to persist to and is silently skipped, same "no
    candidate_id to anchor against" posture this file's own trades_from_cache/candidate_id
    fields already document.

    Two real consumers, two different storage shapes:
    - core_safe/addon_safe: gates Phase5's expensive 1s-verification (phase5_second_
      level_overlay_check.py's SAFE/SAFE gate) -- confirmed real waste: 572 of 2,238
      Phase5-checked candidates (25.6%) were already CLIFF-flagged by Phase4 on the
      2026-08-29/30 campaign log. Persisted via the PRE-EXISTING phase4_results table
      (candidate_verification_store.py, real INTEGER columns, its own upsert_phase4/
      get_stored_phase4 API, already written by run_candidate_nodes_campaign_
      verification.py -- 9 real rows existed before this feature) rather than as new
      columns on candidate_nodes -- see the "Consolidated onto phase4_results" paragraph
      below for why an earlier version of this function did the latter and had to be
      corrected.
    - Every other checklist field (_PHASE4_CHECKLIST_KEYS): no real query/filter need on
      an individual sub-field today, just "look this candidate's checklist up later
      without re-running Phase4" -- one JSON blob column (`phase4_checklist_json`),
      matching the existing `params_json` column's own precedent for exactly this
      "structured, no per-field predicate" shape, rather than ~24 more individually
      ALTER-guarded columns for data nothing filters on.

    Consolidated onto phase4_results (2026-08-30, same day, paired-review MEDIUM
    finding): core_safe/addon_safe were originally two new TEXT columns on
    candidate_nodes -- but the PRE-EXISTING phase4_results table above already persists
    exactly these two fields (as real INTEGER columns) plus most of the rest of the
    checklist. Writing a second, unsynced representation directly on candidate_nodes was
    a real design smell, not a functional bug, but worth fixing rather than carrying two
    sources of truth forward. Now writes core_safe/addon_safe (+ the rest of
    _PHASE4_VALUE_COLUMNS phase4_results already tracks) through upsert_phase4, reusing
    run_candidate_nodes_campaign_verification._phase4_fields_from_row's exact field
    mapping (not re-derived here) so both writers stay byte-identical. Only
    phase4_checklist_json remains a candidate_nodes column -- it holds genuinely EXTRA
    detail phase4_results doesn't track (all 5 real check13 folds vs. phase4_results' own
    worst-fold summary, full drought detail, the two drawdown timestamps), so it isn't
    redundant with the consolidated table. NOTE: the OLD candidate_nodes.core_safe/
    addon_safe TEXT columns from the first version of this feature are no longer written
    or read by anything -- any real verdict a session persisted there before this
    consolidation landed needed a one-time migration into phase4_results (see
    scripts/migrate_core_safe_to_phase4_results.py, run once against the real DB the same
    day this consolidation landed, 636 real WEBL rows + others migrated) or it would have
    silently gone invisible to the SAFE/SAFE gate -- confirmed as a real, not
    hypothetical, paired-review HIGH finding against tonight's own already-completed
    campaign."""
    from candidate_verification_store import ensure_phase4_table, upsert_phase4
    from run_candidate_nodes_campaign_verification import _phase4_fields_from_row

    try:
        checklist_updates = []
        phase4_results_writes = []
        for r in rows:
            # error rows (candidate_id is None, built from `base` in gt_rows_for_scope)
            # and any row carrying its own 'error' key are both skipped -- matching
            # run_candidate_nodes_campaign_verification.py's own "skip both" convention
            # (2026-08-30, paired-review MEDIUM finding: this file only checked
            # candidate_id before, latent-harmless today since error rows never carry
            # one, but worth being explicit rather than relying on that coincidence).
            if r.get("candidate_id") is None or r.get("error"):
                continue
            # _json_safe (numpy scalar/pandas Timestamp -> JSON-serializable): check13_
            # foldN_fragile/check8_too_few_trades can be numpy.bool_, check4/check8/
            # check11/drought fields can be numpy.float64, and check11_dd_peak_time/
            # check11_dd_trough_time are real pandas Timestamps (_check11_max_drawdown_gt,
            # run_optimization_sweep.py, from backtester.py's resolve_time()) -- confirmed
            # 2026-08-30, paired-review HIGH finding: _native()'s plain `.item()` fallback
            # does NOT cover Timestamp (no .item() method), so json.dumps raised TypeError
            # on the very first real candidate with a drawdown before this fix.
            checklist_json = json.dumps(
                {k: _json_safe(r.get(k)) for k in _PHASE4_CHECKLIST_KEYS}, sort_keys=True)
            checklist_updates.append((checklist_json, r["candidate_id"]))
            phase4_results_writes.append((r["candidate_id"], _phase4_fields_from_row(r)))
        if not checklist_updates:
            return
        # args.db (2026-08-30, paired-review MEDIUM finding): this module-level DB_PATH is
        # the same one gt_rows_for_scope already syncs run_optimization_sweep's own
        # DB_PATH to (see that function's own "DB_PATH sync" docstring) -- consistent with
        # the rest of this file's pre-existing --db handling, not a new inconsistency.
        with sqlite3.connect(DB_PATH, timeout=60.0) as conn:
            # phase4_checklist_json written FIRST, in one executemany (2026-08-30,
            # paired-review MEDIUM finding): the per-candidate upsert_phase4 loop below
            # can legitimately raise (e.g. a stale candidate_id) -- doing that loop first
            # meant one bad candidate_id, via the outer except, silently discarded EVERY
            # row's checklist JSON too, not just that one candidate's phase4_results
            # write. Writing the checklist first means it survives even if the loop below
            # fails partway through.
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(candidate_nodes)")}
            if "phase4_checklist_json" not in existing_cols:
                conn.execute("ALTER TABLE candidate_nodes ADD COLUMN phase4_checklist_json TEXT")
            conn.executemany(
                "UPDATE candidate_nodes SET phase4_checklist_json=? WHERE id=?",
                checklist_updates)
            conn.commit()
            ensure_phase4_table(conn)
            n_phase4_written = 0
            for candidate_id, fields in phase4_results_writes:
                # Isolated per-candidate (2026-08-30, paired-review MEDIUM finding): a
                # single bad candidate_id must not abort phase4_results persistence for
                # every other candidate in this batch -- upsert_phase4 itself commits per
                # row, so a failure here only loses that one candidate's verdict, already
                # logged, not the whole scope's worth of work.
                try:
                    upsert_phase4(conn, candidate_id, fields)
                    n_phase4_written += 1
                except Exception as e:
                    print(f"  WARNING: failed to persist phase4_results for "
                          f"candidate_id={candidate_id} ({e}) -- skipping just this one.")
        print(f"\nPersisted core_safe/addon_safe (via phase4_results, {n_phase4_written} "
              f"row(s)) + full Phase4 checklist for {len(checklist_updates)} "
              f"candidate_nodes row(s).")
    except Exception as e:
        # Never let a persistence bug destroy an already-computed, expensive Phase4
        # report (2026-08-30, paired-review HIGH finding: an earlier version of this
        # function let a JSON-serialization TypeError propagate uncaught, which -- called
        # AFTER the scope loop but BEFORE the --csv/--xlsx write -- would have discarded
        # every row of a multi-hour run and exited non-zero, making run_inmemory_sweep_
        # queue.sh skip Phase5 entirely for that ticker). Persistence is a nice-to-have
        # on top of the report, never allowed to be worse than not persisting at all.
        print(f"\nWARNING: failed to persist core_safe/addon_safe/checklist to "
              f"candidate_nodes ({e}) -- report itself is unaffected, continuing.")


def _json_safe(v):
    """_native() (numpy scalar -> Python native) plus pandas Timestamp/datetime -> ISO
    string -- see _persist_phase4_verdicts_and_checklist's own comment for the real bug
    this fixes (check11_dd_peak_time/check11_dd_trough_time are real Timestamps, and
    _native's `.item()` fallback doesn't cover them)."""
    import datetime
    import pandas as pd
    v = _native(v)
    if isinstance(v, (pd.Timestamp, datetime.datetime, datetime.date)):
        return v.isoformat()
    return v


def _gt_scope_banner_text(ticker, strategy, version, entry_timing, fixed_sl, grid_window):
    """The ONE place this banner+timestamp text is built (2026-08-31, proactive fixup
    while fixing the paired-review CONFIRMED HIGH findings above) -- both
    _run_one_gt_scope_worker's success path and run_gt_mode's own error path (a scope
    whose worker raised, so there's no captured_output to reuse) need the IDENTICAL
    banner text, and hand-duplicating an f-string in two places is exactly the "two
    copies that can silently drift" shape this whole task exists to eliminate."""
    return (f"\n{'-'*100}\n{ticker} / {strategy} / {version} / entry_timing={entry_timing} "
            f"/ fixed_sl={fixed_sl}"
            f"{f' / grid_window={grid_window}' if grid_window is not None else ''}\n{'-'*100}"
            f"\n[{_datetime.now().strftime('%H:%M:%S')}]")


def _run_one_gt_scope_worker(db_path, ticker, strategy, version, entry_timing, fixed_sl,
                              grid_window, metric, min_alpha_arg):
    """Picklable, top-level worker body for ONE Phase4 scope (2026-08-31, Task #8 further
    follow-up -- real parallelism for a script that previously had ZERO, confirmed by grep
    before this). Extracted from run_gt_mode's own per-scope loop body so it can run inside
    a ProcessPoolExecutor worker via campaign_registry.run_throttled. Opens its own sqlite3
    connection -- a live Connection object can't cross a process boundary, matches this
    project's existing per-call `sqlite3.connect(...)` convention already used everywhere
    else in this file.

    Returns (captured_output, rows, error). captured_output is EVERY line this scope's
    real work printed -- the banner AND gt_rows_for_scope's own internal prints (its
    "[GT candidate report] ..." progress lines, and print_candidate_report_ground_truth's
    full rendered report, called from inside gt_rows_for_scope -- paired-review CONFIRMED
    HIGH, both independent reviewers, 2026-08-31: a first version of this function only
    deferred the 2-line banner and let gt_rows_for_scope print everything else straight to
    the worker's own inherited stdout fd, which under fork + a piped/tee'd parent stdout is
    block-buffered and interleaves/reorders across concurrent workers -- the actual
    candidate report, this script's primary human-readable output, became unattributable).
    Captured via contextlib.redirect_stdout so `_on_scope_result` (run_gt_mode, below) can
    print each scope's ENTIRE output as one atomic, correctly-ordered unit -- banner
    immediately followed by that same scope's real body, exactly matching the original
    serial loop's interleaving, just deferred to completion time instead of start time.

    `error` (round-2 fixup, same day, cold-review CONFIRMED MEDIUM): the real work is
    wrapped in its OWN try/except here, inside the redirect_stdout block, rather than
    letting an exception propagate out and lose whatever this scope had already printed
    before failing -- exactly the diagnostics (e.g. gt_rows_for_scope's own
    '[GT candidate report] ...' lines right before a crash) most useful for explaining
    the failure. `error` is the exception object (or None) for the caller to append its
    own '  UNEXPECTED error...' line after the (still-real) captured_output; rows is []
    when error is not None."""
    import contextlib
    import io

    buf = io.StringIO()
    error = None
    rows = []
    with contextlib.redirect_stdout(buf):
        conn = sqlite3.connect(db_path)
        try:
            print(_gt_scope_banner_text(ticker, strategy, version, entry_timing, fixed_sl, grid_window))
            if grid_window is None:
                node = gt_current_best_node(conn, ticker, strategy, version, entry_timing, fixed_sl,
                                             metric, min_alpha_arg)
                if node is None:
                    print(f"  [top_safe_nodes cross-check] no cliff-safe node found for {metric} floor")
                else:
                    print(f"  [top_safe_nodes cross-check] best {metric}: arm/tp={node['arm_pct']} "
                          f"sl={node['sl']} hold={node['hold']}h window={node['window']} "
                          f"z={node['z']} robust_alpha={node['alpha']:+.1f}% cagr={node['cagr']}")
            else:
                print("  [top_safe_nodes cross-check] skipped -- candidate_nodes-sourced scope, "
                      "no backtest_cache equivalent.")
            # addon_cliff_workers=1 (2026-09-11, real regression fix in
            # run_optimization_sweep.py -- paired-review HIGH finding fix, moved here
            # from inside gt_rows_for_scope itself after both an independent-cold and a
            # contextual Opus review independently converged on the same underlying bug:
            # a hardcoded override inside the shared function would have also silently
            # starved run_candidate_nodes_campaign_verification.py's own fully-serial
            # call to gt_rows_for_scope, the real primary phase4_results-persisting
            # consumer, of the whole fix). THIS worker function is the one actually
            # nested inside run_gt_mode's own ProcessPoolExecutor (--workers, default 4)
            # -- letting Phase4's new default (6) apply here too would nest a 6-worker
            # pool inside each outer worker (up to 24 processes at once, each
            # independently loading its own private copy of this ticker's hourly/
            # minute/second dataframes -- the exact per-worker memory duplication
            # bench_phase1_phase2_inmemory.py's own preload-before-fork fix eliminated
            # for the analogous Phase2.5 path). The real parallelism this fix restores
            # still applies -- just at the scope level this call site already has, not
            # a second nested level.
            rows = gt_rows_for_scope(ticker, strategy, version, entry_timing, fixed_sl,
                                      grid_window=grid_window, addon_cliff_workers=1)
        except Exception as e:
            error = e
        finally:
            conn.close()
    return buf.getvalue(), rows, error


GT_WORKERS_CAP_ROW_THRESHOLD = 15_000_000
GT_WORKERS_CAPPED_VALUE = 4


def resolve_effective_gt_workers(conn, massive_tickers, requested_workers):
    """Row-count-keyed effective-workers cap for run_gt_mode's outer per-scope pool
    (2026-09-11, scripts/calibrate_gt_workers.py dispatch). Real mechanism is per-worker
    cache/state growth (_SECOND_DF_CACHE etc.) ACROSS a long-lived worker's many
    sequential candidates, not a single call's peak memory -- confirmed via
    calibrate_gt_workers.py's --sustained mode, which showed per-worker PSS growth over
    15 candidates scaling with ticker row count (AGQ +0.24GB/worker, TNA +0.47GB/worker)
    and SOXL at workers=8 genuinely exceeding safe memory under that realistic sustained
    load (a real, non-instant trip -- distinct from a single-cell test, which showed no
    ceiling at all for any of the three tickers). Keyed on real active-build row count
    (not a hardcoded ticker name) so any other ticker that grows into SOXL's row-count
    range later is covered automatically, and so AGQ/TNA-scale tickers aren't penalized
    by a blanket low default. Threshold picked from the actual measured growth rates:
    TNA (11.4M rows) stayed safe under sustained load, SOXL (22.2M) did not --
    GT_WORKERS_CAP_ROW_THRESHOLD splits the two clusters.

    `massive_tickers`: real tickers in this run whose scopes use data_source='massive'
    (only those load second-resolution data at all -- a yahoo-sourced scope never hits
    this growth path). Below the threshold, `requested_workers` is returned as-is;
    above it, capped at GT_WORKERS_CAPPED_VALUE regardless of what was requested."""
    if not massive_tickers:
        return requested_workers
    placeholders = ",".join("?" for _ in massive_tickers)
    row_counts = dict(conn.execute(f"""
        SELECT m.ticker, COUNT(*) FROM massive_second_derived m
        JOIN active_builds ab ON ab.ticker = m.ticker AND ab.table_name = 'second'
                              AND ab.build_id = m.build_id
        WHERE m.ticker IN ({placeholders})
        GROUP BY m.ticker
    """, list(massive_tickers)).fetchall())
    if not row_counts:
        return requested_workers
    worst_ticker = max(row_counts, key=row_counts.get)
    worst_rows = row_counts[worst_ticker]
    if worst_rows > GT_WORKERS_CAP_ROW_THRESHOLD and requested_workers > GT_WORKERS_CAPPED_VALUE:
        print(f"[workers cap] {worst_ticker}: capped --workers {requested_workers} -> "
              f"{GT_WORKERS_CAPPED_VALUE} -- active massive_second_derived row count "
              f"{worst_rows:,} exceeds the sustained-load-safe threshold "
              f"({GT_WORKERS_CAP_ROW_THRESHOLD:,}); per-worker cache/state growth across "
              f"a long multi-candidate run scales with ticker row count (see "
              f"scripts/calibrate_gt_workers.py --sustained), not just a single call's "
              f"peak memory.")
        return GT_WORKERS_CAPPED_VALUE
    return requested_workers


def run_gt_mode(conn, tickers, metric, min_alpha_arg, csv_name, xlsx_name, grid_window_filter=None,
                 version_filter=None, db_path=None, workers=4):
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
    (Phase5 was already unaffected, since it's version-scoped).

    ALSO now filters the backtest_cache-sourced `covered` skip-set to `version_filter`
    (2026-08-30, root-cause fix, paired-review HIGH finding against the SAFE/SAFE-gate
    work: this used to be unconditionally version-unfiltered here, unlike Phase5's own
    `covered` set at phase5_second_level_overlay_check.py's main(), which has always
    filtered `all_scopes` to `s[2] == args.version` BEFORE building `covered`. Confirmed
    on real data: SOXL has 10 backtest_cache GT scopes at version
    'v6-massive-w2021-08-23_2026-08-21' -- with no filter here, EVERY one of SOXL's other
    24 candidate_nodes scopes (a totally different, unrelated 'bench-inmemory-...'
    campaign) got silently treated as "already covered by backtest_cache" and skipped
    from Phase4 entirely, so Phase4 never persisted core_safe/addon_safe for them at
    all -- which the Phase5 SAFE/SAFE gate then read as "unverified" for 100% of those
    scopes' candidates. `version_filter=None` (the default, when --version isn't passed
    -- multi-ticker/tranche ad hoc invocations) keeps the old, broader, version-agnostic
    behavior unchanged; only a real single-version campaign run (run_inmemory_sweep_
    queue.sh always passes --version) gets the tightened, Phase5-matching scoping."""
    from phase4_candidate_nodes_resolver import discover_all_candidate_nodes_scopes
    import run_optimization_sweep as ros

    _bc_scopes = gt_scopes_for_tickers(conn, tickers)
    if version_filter is not None:
        _bc_scopes = [s for s in _bc_scopes if s[2] == version_filter]
    scopes = [(s[0], s[1], s[2], s[3], s[4], None) for s in _bc_scopes]
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

    # Real parallelism (2026-08-31, Task #8 further follow-up) -- this loop previously ran
    # every scope fully serially (confirmed by grep before this: zero ProcessPoolExecutor/
    # ThreadPoolExecutor/multiprocessing anywhere in this file). Each scope's real work
    # (_run_one_gt_scope_worker) is independent -- its own sqlite3 connection, its own
    # gt_rows_for_scope call -- so scopes now run in a ProcessPoolExecutor, throttled via
    # campaign_registry.run_throttled (the SAME already-paired-reviewed logic bench_
    # phase1_phase2_inmemory.py._dispatch uses, factored out so this doesn't hand-roll a
    # 3rd copy).
    #
    # Results are buffered by scope INDEX and printed/accumulated in original `scopes`
    # order only after every scope has completed (2026-08-31, paired-review CONFIRMED
    # HIGH + 2 MEDIUM fixup, both independent reviewers): a first version printed/
    # extended in raw completion order, which (a) let two concurrent scopes' worker
    # output interleave/reorder in the tee'd campaign log (the worker itself now captures
    # its ENTIRE output including gt_rows_for_scope's own internal prints, not just the
    # banner -- see _run_one_gt_scope_worker's own docstring), (b) made all_rows' order --
    # and therefore the --csv/--xlsx deliverable's row order -- nondeterministic between
    # runs over the identical scopes, breaking reproducibility/diffability, and (c) broke
    # the exact convention Phase5's own code cites this file for ("Combine in submission
    # order (not completion order) so scope output reads in the same TP/SL-descending
    # order Phase4's own report does"). An exception still prints its OWN scope's banner
    # first (matching the original serial loop, which always printed the banner before
    # entering the try/except that could fail) so a failure is never an anonymous,
    # unattributable error line.
    #
    # workers_budget lookup uses the FIRST scope's version as the representative campaign
    # (2026-08-31) -- in the real shell-driven path (run_inmemory_sweep_queue.sh always
    # passes --version) every scope here shares the same version by construction, so this
    # is exact, not an approximation, for that path. A --tranche/--csv ad hoc run spanning
    # multiple distinct versions (no --version filter) would use the first scope's budget
    # for the whole batch -- an edge case, not the primary path this was built for.
    # Streams in scopes order via a reorder buffer (2026-08-31, same-day fixup, cold-
    # review CONFIRMED MEDIUM): an earlier version buffered EVERY scope until the whole
    # batch finished, then printed all at once -- correct order, but nothing appeared in
    # the tee'd campaign log for the entire Phase4 duration. run_inmemory_sweep_queue.sh's
    # own PYTHONUNBUFFERED=1 comment already documents this exact class of bug as real
    # ("nothing appeared to tail -f"), not cosmetic. This buffers only what's genuinely
    # out of order: scope idx prints/accumulates as soon as it's ready AND every scope
    # before it has already printed -- a scope that finishes early but is blocked behind
    # a still-running earlier scope waits; a scope that finishes exactly in order streams
    # immediately, same as the pre-parallelism serial loop always did.
    all_rows = []
    if scopes:
        from concurrent.futures import ProcessPoolExecutor
        from scripts import campaign_registry

        budget_version = scopes[0][2]
        _db_path = db_path or DB_PATH
        _indexed_scopes = list(enumerate(scopes))
        _pending = {}
        _next_to_emit = [0]

        def _scope_banner(scope):
            # Uses the SAME _gt_scope_banner_text function _run_one_gt_scope_worker's
            # success path calls -- not a hand-duplicated copy (see that function's own
            # docstring for why this specifically was worth a shared helper).
            ticker, strategy, version, entry_timing, fixed_sl, grid_window = scope
            return _gt_scope_banner_text(ticker, strategy, version, entry_timing, fixed_sl, grid_window)

        def _submit_scope(pool, indexed_scope):
            _idx, scope = indexed_scope
            ticker, strategy, version, entry_timing, fixed_sl, grid_window = scope
            return pool.submit(_run_one_gt_scope_worker, _db_path, ticker, strategy, version,
                                entry_timing, fixed_sl, grid_window, metric, min_alpha_arg)

        def _emit_ready():
            while _next_to_emit[0] in _pending:
                captured_output, rows = _pending.pop(_next_to_emit[0])
                print(captured_output, end="")
                all_rows.extend(rows)
                _next_to_emit[0] += 1

        def _on_scope_result(indexed_scope, result_or_exc):
            idx, scope = indexed_scope
            if isinstance(result_or_exc, Exception):
                # An exception here means something OUTSIDE _run_one_gt_scope_worker's own
                # try/except failed (e.g. the task itself couldn't be pickled/unpickled) --
                # that function catches its own real-work exceptions internally now (see
                # its own docstring) specifically so a genuine scope failure still returns
                # its captured partial output instead of losing it here.
                _pending[idx] = (
                    _scope_banner(scope) + f"\n  UNEXPECTED error on this scope, skipping: {result_or_exc}",
                    [])
                _emit_ready()
                return
            captured_output, rows, error = result_or_exc
            if error is not None:
                # Partial output (banner + whatever gt_rows_for_scope printed before
                # failing) is real and preserved -- append the error line after it rather
                # than discarding it, same "skip this scope, keep going" posture as before.
                captured_output += f"\n  UNEXPECTED error on this scope, skipping: {error}"
                rows = []
            _pending[idx] = (captured_output, rows)
            _emit_ready()

        # Preload-before-fork (2026-09-11, third real instance of this bug class this
        # session -- same pattern as bench_phase1_phase2_inmemory.py's main() and
        # run_optimization_sweep.py's run_addon_cliff_safety_ground_truth, both fixed
        # earlier tonight). Each _run_one_gt_scope_worker independently calls into
        # gt_rows_for_scope -> _load_node_inputs_ground_truth, which loads this scope's
        # ticker's hourly/minute/second dataframes into a plain module-level cache dict
        # PER PROCESS -- a worker that never inherited an already-populated cache loads
        # (and keeps) its own private copy for the rest of its life. Unlike the other two
        # fixes, this pool parallelizes across SCOPES, which can span multiple tickers
        # (and, per-scope, multiple data_source values -- data_source is derived from
        # each scope's own version string, see gt_rows_for_scope's own
        # `"massive" if "-massive" in version else "yahoo"` resolution a few lines above
        # this function). So the preload here is keyed per (ticker, data_source) pair
        # actually present in `scopes`, built in this (parent) process before the pool
        # is created -- every worker forked afterward inherits whichever of these pairs
        # its own scope needs via copy-on-write, instead of loading it itself.
        #
        # Bounded to _SECOND_DF_CACHE_MAX distinct pairs (paired-review CONFIRMED HIGH/
        # MEDIUM, both independent-cold and contextual Opus reviewers, same finding from
        # each): every one of these loader caches clears its ENTIRE dict on overflow
        # rather than evicting one entry (see _HOURLY_DF_CACHE_GT_MAX/_MINUTE_DF_CACHE_
        # MAX/_SECOND_DF_CACHE_MAX in run_optimization_sweep.py), so preloading more
        # pairs than the tightest cap (_SECOND_DF_CACHE_MAX=2) would have each new
        # pair's load evict the previous one -- only the last ~2 pairs would still be
        # cached by the time the pool forks, while every pair still paid its full
        # serial parent-side load cost (a real ~1GB second-resolution frame at SOXL
        # scale) for no COW-sharing benefit, plus startup latency that used to be
        # parallel across workers. A multi-ticker/--tranche run is a real path (see
        # this function's own `budget_version` comment above), so this isn't a
        # theoretical edge case. Above the cap, skip preloading entirely and let every
        # worker load its own scope's data independently -- exactly this function's
        # pre-fix behavior, not a regression.
        _preload_pairs = {(s[0], "massive" if "-massive" in s[2] else "yahoo") for s in scopes}
        if len(_preload_pairs) > ros._SECOND_DF_CACHE_MAX:
            print(f"[preload] skipping preload-before-fork: {len(_preload_pairs)} distinct "
                  f"(ticker, data_source) pair(s) exceeds _SECOND_DF_CACHE_MAX="
                  f"{ros._SECOND_DF_CACHE_MAX} -- preloading all of them would just evict each "
                  f"other before the pool forks. Each worker will load its own scope's data "
                  f"independently, same as before this fix.")
        else:
            for _pl_ticker, _pl_data_source in sorted(_preload_pairs):
                # Contained per-pair, same posture as _run_one_gt_scope_worker's own
                # per-scope containment (paired-review CONFIRMED HIGH, independent-cold
                # reviewer): a ticker with no active massive hourly/minute build, or no
                # yahoo CSV on disk, previously only failed THAT scope (caught inside
                # _run_one_gt_scope_worker's own try/except, returned as an error row
                # with output preserved) -- an uncaught preload failure here would abort
                # the whole run_gt_mode batch before a single scope even ran, for a
                # ticker that might not even be the one causing trouble. Skipping this
                # pair's preload on failure just falls back to the pre-fix behavior for
                # it (each worker needing it loads its own copy, and hits/handles the
                # same failure independently, contained to its own scope).
                try:
                    ros._load_hourly_df_ground_truth(_pl_ticker, data_source=_pl_data_source)
                    ros._load_minute_df(_pl_ticker, data_source=_pl_data_source)
                except (ValueError, FileNotFoundError) as e:
                    print(f"[preload] {_pl_ticker}/{_pl_data_source}: hourly/minute preload "
                          f"failed ({e}) -- skipping preload for this pair.")
                    continue
                # yahoo has no second-resolution equivalent -- _load_second_df always
                # raises ValueError for data_source != 'massive' (paired-review LOW,
                # both reviewers: an earlier version called this unconditionally,
                # printing a misleading "will fall back to minute" message for every
                # yahoo pair even though those scopes never request second resolution
                # at all -- see build_candidate_report_ground_truth's own
                # data_source == 'massive' gate on _resim_fill_resolution).
                if _pl_data_source == "massive":
                    try:
                        ros._load_second_df(_pl_ticker, data_source=_pl_data_source)
                    except ValueError as e:
                        print(f"[preload] {_pl_ticker}/{_pl_data_source}: {e} -- workers "
                              f"needing second-resolution data for this ticker will fall "
                              f"back to minute per-cell, same as _load_node_inputs_ground_"
                              f"truth's own fallback.")

        _massive_tickers = [t for t, ds in _preload_pairs if ds == "massive"]
        effective_workers = resolve_effective_gt_workers(conn, _massive_tickers, workers)

        with ProcessPoolExecutor(max_workers=effective_workers) as pool:
            campaign_registry.run_throttled(pool, _submit_scope, _indexed_scopes, budget_version,
                                             _on_scope_result)

        # Completeness guard (2026-08-31, paired review LOW, both independent reviewers):
        # _emit_ready only drains a CONTIGUOUS completed prefix, so a missing index would
        # silently strand it and every later scope out of stdout AND all_rows -- a quietly
        # short CSV/xlsx, not the loud KeyError the prior single-pass `for idx in range(...)`
        # would have raised. Not reachable today (run_throttled calls on_result exactly once
        # per submitted task, see its own docstring), but that guarantee lives in a different
        # file -- this converts a future violation of it into a loud failure here instead of
        # a silently truncated report.
        assert _next_to_emit[0] == len(scopes) and not _pending, (
            f"Phase4 scope-result bookkeeping incomplete: {len(scopes) - _next_to_emit[0]} "
            f"scope(s) never reported (expected run_throttled to call on_result exactly once "
            f"per task) -- refusing to write a silently truncated report.")

    _persist_phase4_verdicts_and_checklist(all_rows)

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
    ap.add_argument("--workers", type=int, default=4,
                     help="Requested ProcessPoolExecutor worker count for --kernel gt's "
                          "per-scope loop (2026-08-31, Task #8 further follow-up -- this loop "
                          "previously had zero parallelism at all). Actual in-flight concurrency "
                          "is additionally gated by the resolved campaign's workers_budget, if "
                          "any -- see scripts/campaign_registry.py. Automatically CAPPED at 4 "
                          "(regardless of this flag) for any ticker whose real active-build "
                          "massive_second_derived row count exceeds 15M (logged when this fires) "
                          "-- calibrate_gt_workers.py's --sustained mode (2026-09-11) found "
                          "per-worker cache/state growth ACROSS a long-lived worker's many "
                          "sequential candidates scales with ticker row count (not a single "
                          "call's peak memory, which showed no ceiling for any tested ticker), "
                          "and a real SOXL (~22M rows) sustained-load run genuinely exceeded "
                          "safe memory at workers=8 -- distinct from, and a better fit for the "
                          "real incident than, the original single-cell/per-worker-array-size "
                          "theory. AGQ/TNA-scale tickers are unaffected by the cap and use this "
                          "value as-is; default 4 here is unrelated to the cap and can be raised "
                          "for those tickers independently.")
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
                        grid_window_filter=args.grid_window, version_filter=args.version,
                        db_path=args.db, workers=args.workers)
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
