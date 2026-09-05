#!/usr/bin/env python
"""Phase 10 of the pipeline's phase numbering (2026-09-01, research-session dispatch,
number chosen deliberately non-sequential after Phase5 -- room reserved for future
phases 6-9): candidate report for a candidate_nodes-sourced (in-memory pipeline)
campaign. Combines candidate_full_review.py's --kernel gt --source candidate_nodes full
checklist machinery (Tab 1) with candidate_report_inmemory.py's curated top-N-per-category
picks (Tab 2) and the raw candidate_nodes population (Tab 3) into ONE xlsx, joined by
`node_id` (candidate_nodes.id, carried on all three tabs).

v2 (2026-09-01, same-day follow-up dispatch, user review of v1):
  - top_n widened from a hardcoded 2 to a --top-n CLI arg (default 5) -- both Tab 2 and
    Tab 1's scoping (Tab 1 = whatever Tab 2's curated set is) grow together.
  - New Tab 3 "All Candidates (raw)": every real candidate_nodes row for the version
    (thousands, e.g. 13,434 for v6.5), LEFT JOINed to candidate_verification_results for
    whatever lightweight metrics (core/addon/drought/core_both CAGR) already exist --
    deliberately NOT run through the full checklist (200x+ larger population than the
    curated set makes that infeasible, see Tab 1 scoping note below). Carries a
    `promoted_pick` bool marking node_ids in Tab 2's curated set.
  - --workers: optional ProcessPoolExecutor parallelization of Tab 1's checklist compute,
    throttled via campaign_registry.get_workers_budget(version) (same read-once, bounded-
    submission, fail-toward-unthrottled contract bench_phase1_phase2_inmemory.py's own
    _dispatch documents -- NOT imported from there, since that module is a gated backtest-
    kernel module; reimplemented standalone against just campaign_registry, which isn't
    gated). Default 1 (serial, original behavior) -- the caller decides when it's safe to
    ask for more (e.g. after confirming no other campaign is actively consuming CPU
    budget); this script does not check that itself.

v3 (2026-09-01, same-day follow-up after the v2 timing recalculation): --full-review-
population {curated, core_safe} -- 'core_safe' widens Tab 1 to the real full core_safe=
True population (phase4_results.core_safe=1 AND trades>=--min-trades, 9,212 rows for
v6.5) instead of the curated top-N set. Tab 2/Tab 3 are unchanged either way -- this only
widens Tab 1's population. See _core_safe_population_rows and build_report's
full_review_population param docstring.

NOTE on the v2 dispatch's real process gap (found 2026-09-01, see deep_backlog.md): the
poll used to gate the v2 real --workers>1 run checked only one ticker's pgrep
(bench_phase1_phase2_inmemory.py --ticker AGQ), which fired as soon as THAT ticker
finished, not when the whole (multi-ticker) v6.6 campaign did -- v6.6 moved on to another
ticker immediately after, causing ~2m21s of real, confirmed CPU contention between this
script's workers and v6.6's. Any caller reusing the poll-then-launch pattern against a
multi-ticker campaign MUST check the whole campaign (campaign_registry.status(campaign_id)
-- job_counts.get('running', 0) == 0 and job_counts.get('queued', 0) == 0), not a single
ticker's pgrep.

Tab 1 SCOPE (deliberate, measured 2026-09-01 -- see run_phase10_full_review_candidate_
nodes' own docstring warning): a real candidate_nodes campaign version can sweep MANY
(fixed_sl, window) scopes per ticker (64 for v6.5), each with a dozens-large candidate
population -- computing the full checklist (~11-13s/candidate) for ALL of them would run
tens of hours. Tab 1 is instead scoped to exactly the node_ids that land on Tab 2's
curated set -- this is the finalist-explaining use build_candidate_report_ground_truth's
own docstring already describes ("explains the finalists", not the full grid), just
applied one level higher (finalists-across-the-campaign instead of finalists-within-one-
scope). Each curated candidate gets one real full-checklist row via gt_full_review_rows
(candidates_override=[that exact candidate]), grouped by real scope first so candidates
sharing a scope don't redundantly re-derive it.

Kept as plain callable functions, not CLI-only (per a future-integration note from the
2026-09-01 dispatch: this report is expected to eventually get wired into the sweep
pipeline itself, auto-run after Phase5) -- main() is a thin CLI wrapper over
build_report(), which a future pipeline caller can import and call directly.

Neither tab's own per-row compute logic is reimplemented here -- this script only
discovers the curated node set, groups by real scope, and calls the existing report
builders. See:
  - scripts/candidate_full_review.py's gt_full_review_rows (Tab 1 per-candidate compute)
  - scripts/candidate_report_inmemory.py's fetch_rows/curate (Tab 2, and Tab 1's scoping)
  - scripts/phase4_candidate_nodes_resolver.py's derive_phase25_candidates_from_
    candidate_nodes(full_population=True) (per-scope candidate dict lookup)

Usage:
    .venv/bin/python scripts/candidate_full_review_two_tab.py --version VERSION \\
        [--tickers T1,T2,...] [--top-n 5] [--workers 1] [--xlsx NAME]

If --tickers is omitted, discovers every ticker with a candidate_nodes row for --version.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from candidate_full_review import (
    DB_PATH, DEFAULT_VOL_GATE, FIELDNAMES, COLUMN_DEFS, ensure_candidate_nodes_table,
    gt_full_review_rows, _build_output_row, GT_SKIP_COLUMNS, GT_SKIP_LABEL, _git_provenance_stamp,
    k1_status, _persist_snapshot, ticker_sector, underlier_info,
)
from phase4_candidate_nodes_resolver import derive_phase25_candidates_from_candidate_nodes
from build_v6_promotion_combined_report import CURATED_HEADERS
import candidate_report_inmemory as cri

LIVE_DB_PATH = "cache/live/trading_live.db"

RAW_COLUMNS = cri.COLUMNS[:14] + [
    "core_cagr_1m", "core_cagr_1s", "n_trades_1m", "n_trades_1s",
    "addon_cagr_1m", "addon_cagr_1s", "drought_cagr_1m", "drought_cagr_1s",
    "core_both_cagr_1m", "core_both_cagr_1s", "core_safe",
]

# phase4_results columns already real/computed for every core_safe=1 node (9,212/9,212 for
# v6.5-pv4) but never wired into any tab before -- item #2, 2026-09-02 research dispatch.
# All _pct-suffixed columns here follow this codebase's legacy convention (already stored
# x100, unlike candidate_verification_results' unsuffixed core_cagr_1m/1s etc -- see
# _pct100's own docstring on that distinct convention): confirmed directly against real
# phase4_results rows (e.g. addon_cagr_pct=49.6, not 0.496) before wiring in, precisely
# because _pct100's whole reason for existing was an identical scale mistake elsewhere.
PHASE4_EXTRA_COLUMNS = [
    "addon_cagr_pct", "drought_compounded_pct", "drought_combined_compounded_pct",
    "check8_compounded_pct", "check8_compounded_without_best_pct", "check8_best_trade_share_pct",
    "check11_max_drawdown_pct", "check13_worst_fold_cagr_pct", "check13_any_fold_fragile",
    "core_addon_disagreement", "check4_early_wr_pct", "check4_late_wr_pct",
]
PHASE4_EXTRA_HEADERS = [
    "Phase4 Addon Cagr %", "Phase4 Drought Compounded %", "Phase4 Drought Combined Compounded %",
    "Phase4 Check8 Compounded %", "Phase4 Check8 Compounded (no best) %", "Phase4 Check8 Best Trade Share %",
    "Phase4 Check11 Max Drawdown %", "Phase4 Check13 Worst Fold Cagr %", "Phase4 Check13 Any Fold Fragile",
    "Phase4 Core/Addon Disagreement", "Phase4 Check4 Early WR %", "Phase4 Check4 Late WR %",
]

# item #3 (2026-09-02): 1-minute-resolution CAGR siblings of Cagr/Cagr Add on/CAGR
# Drought/CAGR Both, for direct comparison against the primary 1s-resolution numbers
# (the two can diverge hugely -- confirmed real case, ETHU node 19460: core_both_cagr_1m
# =531.9% vs. core_both_cagr_1s=114.8%). Placed right after CURATED_HEADERS' 24 columns,
# LOCAL to this file rather than added to the shared build_v6_promotion_combined_report.
# CURATED_HEADERS/MANUAL_BLANK_COLS constants -- that module's own write_combined_xlsx
# independently builds a 24-element `curated` row list keyed to CURATED_HEADERS' current
# length; growing the shared constant would silently misalign ITS output columns (the
# 144-col checklist block would land 4 columns early) without touching its own code.
# TWO_TAB_MANUAL_BLANK_COLS (2, down from the shared MANUAL_BLANK_COLS' 6) keeps this
# file's own total column count unchanged by the reshuffle (24 + 4 + 2 = 24 + 6 = 30).
TWO_TAB_1M_HEADERS = ["Cagr (1m)", "Cagr Add on (1m)", "CAGR Drought (1m)", "CAGR Both (1m)"]
TWO_TAB_MANUAL_BLANK_COLS = 2  # was "U-Z"/6 (shared MANUAL_BLANK_COLS) before item #3; now Y-Z/2

# User's own standing manual-review highlight set (2026-09-04, confirmed against real
# column letters BD/BE/BO/BT/BU/BZ/CC/CD/DI in a live report) -- see _write_curated_tab's
# grouping/highlight block for how this drives both the highlight fill and the collapsed
# "gap" columns between them.
USER_HIGHLIGHTED_FIELDNAMES = [
    "resolution_spread_tranche", "years", "addon_n", "addon_early_wr_pct",
    "addon_late_wr_pct", "drought_n", "drought_win_rate_pct", "drought_early_wr_pct",
    "drought_late_wr_pct", "wf_positive_folds",
]

# wf_*_fold_alpha are real percentage-return values without a "_pct" suffix (legacy
# naming) -- everything else percentage-shaped is caught by the "_pct" suffix (raw
# FIELDNAMES/PHASE4_EXTRA_COLUMNS style), a literal "%" in a human-readable header
# (CURATED_HEADERS/TWO_TAB_1M_HEADERS/PHASE4_EXTRA_HEADERS style), or "cagr" in the
# header (this codebase's own CAGR columns are always percentages, never fractions,
# per _pct100's own docstring convention) -- excluding "tranche" columns, which use
# "cagr"/"%" only inside a bucket LABEL (e.g. "0-50%"), not as a real numeric value.
_ALPHA_PCT_FIELDNAMES = {"wf_min_fold_alpha", "wf_max_fold_alpha", "wf_mean_fold_alpha"}


def _is_pct_header(header):
    if not header or "tranche" in header.lower():
        return False
    h = header.lower()
    return h.endswith("_pct") or header in _ALPHA_PCT_FIELDNAMES or "%" in header or "cagr" in h


USER_PCT_NUMBER_FORMAT = "0.0"
USER_PCT_COLUMN_WIDTH = 6.43  # user's own convention (2026-09-04): ~50px in Excel's column-width units

# User's global category-color convention (2026-09-04): applied spreadsheet-wide, to
# every matching column regardless of whether it's also in USER_HIGHLIGHTED_FIELDNAMES
# -- separate layer from the pct number-format/width treatment above (same columns
# can carry both). Priority when a header could plausibly match more than one:
# CAGR > win-rate > number (checked in that order, first match wins) -- in practice
# these three patterns are mutually exclusive in this codebase's real column set.
USER_CAGR_FILL = "C6EFCE"    # light green
USER_WINRATE_FILL = "FFC7CE"  # light red
USER_NUMBER_FILL = "BDD7EE"   # light blue


def _is_cagr_header(header):
    if not header or "tranche" in header.lower():
        return False
    return "cagr" in header.lower()


def _is_winrate_header(header):
    if not header:
        return False
    h = header.lower()
    if "tranche" in h or "verdict" in h:
        return False
    return "win_rate" in h or "wr_pct" in h or "win_pct" in h or "wr %" in h


# Plain count/tally columns -- NOT IDs (Node ID excluded explicitly) or dates. Real
# names from this report's own column set: addon_n/drought_n, n_trades_1m/1s, trades/
# Trades, years/Years, wf_positive_folds/wf_total_folds, core_fluke_trades, bear_*_
# trades, crash25_*_trades, drought_ie_n_included/excluded, exit_fillacc_n, fillacc_n,
# underlier_count.
_NUMBER_HEADER_EXACT = {"trades", "years", "node id"}  # lowercased human-readable CURATED_HEADERS names


def _is_number_header(header):
    if not header:
        return False
    h = header.lower()
    if h == "node id" or "tranche" in h or "verdict" in h or "%" in h or h.endswith("_pct"):
        return False
    if h in _NUMBER_HEADER_EXACT - {"node id"}:
        return True
    return (h.endswith("_n") or h.endswith("_trades") or h.endswith("_folds")
            or h.endswith("_count") or h.startswith("n_trades"))

# User's front-block visibility convention (2026-09-04, confirmed against real column
# letters A-AD): 1-indexed positions within CURATED_HEADERS+TWO_TAB_1M_HEADERS+
# TWO_TAB_MANUAL_BLANK_COLS (30 columns, A:AD) to group/hide -- everything else in
# that range stays visible. Positional (not name-keyed) since 2 of the 30 are blank
# manual columns with no header text to key on -- relies on the user's own stated
# "assuming it doesn't change order" caveat, same as the rest of this block.
FRONT_BLOCK_HIDDEN_POSITIONS = [4, 5, 6, 9, 10, 14, 15, 16, 17, 18, 19, 20, 21]


def _phase4_extra_by_id(conn, node_ids):
    """Real phase4_results data for `node_ids`, appended as new columns (item #2) rather
    than overloaded onto any existing CURATED_HEADERS/FIELDNAMES slot: checked each
    existing front-block slot's own semantics against these phase4_results columns first
    (e.g. 'Add on %'/addon_compounded_pct is a raw compounded return of addon-only trades,
    computed by the full checklist -- NOT the same metric as phase4_results.addon_cagr_pct,
    an annualized CAGR; 'Drought %'/drought_compounded_pct DOES match phase4_results.
    drought_compounded_pct by name+semantics and is filled directly in the lightweight-
    fallback branch below). check8/check11/check13/core_addon_disagreement have no
    existing-column counterpart at all (full checklist tracks different, non-comparable
    per-check fields) -- inventing a plausible-sounding overlay here would repeat exactly
    the kind of silent-scale/semantic mismatch item #1 just fixed, so these get their own
    clearly-labeled block instead. Applied uniformly across ALL 4 tabs (independent of
    whether a row's front/checklist came from a Full Review match or the lightweight
    fallback) since phase4_results exists for the full core_safe population, not just raw-
    only rows."""
    ids = [i for i in node_ids if i is not None]
    out = {}
    if not ids:
        return out
    placeholders = ",".join("?" * len(ids))
    cols = ", ".join(PHASE4_EXTRA_COLUMNS)
    rows = conn.execute(
        f"SELECT candidate_id, {cols} FROM phase4_results WHERE candidate_id IN ({placeholders})",
        ids).fetchall()
    for r in rows:
        out[r[0]] = list(r[1:])
    return out

def _watch_list_semantic_axes(strategy, take_profit, stop_loss, trail_buy_pct, trail_sell_pct, arm_sell_pct):
    """The real (take_profit, stop_loss, trail_sell_pct) SEMANTIC triple build_params_dict/
    get_or_create_candidate_node expect -- i.e. the backtester's own axis meanings, NOT a
    literal column-name copy of watch_list's raw columns. v5 fix (2026-09-02, real bug
    found + confirmed against live data -- a prior version of this function used watch_
    list.take_profit unconditionally as the semantic take_profit/arm_pct input, which is
    WRONG for TrailingBothZScoreBreakout: that column is always NULL for Both (confirmed:
    every real live TrailingBoth watch_list row has take_profit=None), and an initial
    proposed fix (watch_list.stop_loss) was ALSO checked directly and found wrong -- a real
    query showed watch_list.stop_loss does NOT reliably equal anything meaningful for Both
    (one sample coincidentally equaled trail_buy_pct, a second real sample, ETHU id=237,
    did not: stop_loss=1 vs trail_buy_pct=2.0).

    Ground-truthed instead via `arm_sell_pct` (a real, distinct watch_list column) --
    confirmed by an actual end-to-end match: ETHU watch_list id=237 (TrailingBoth,
    arm_sell_pct=1.0, trail_buy_pct=2.0, trail_sell_pct=7.0) fed through build_params_dict
    produces a canonical params_json BYTE-IDENTICAL to real candidate_nodes.id=19460's own
    params_json (a genuine v6.5 TrailingBoth ETHU row with arm_pct=1.0/trail_buy_pct=2.0/
    trail_sell_pct=7.0) -- proof, not inference.

    TrailingBothZScoreBreakout: semantic take_profit = arm_sell_pct (-> candidate_nodes.
    arm_pct, ALWAYS a direct copy of the semantic take_profit field regardless of strategy
    -- see gt_full_review_rows' own node_id-assignment comment), semantic stop_loss =
    trail_buy_pct directly (-> params[sl_axis_col], sl_axis_col='trail_buy_pct' for Both),
    semantic trail_sell_pct = trail_sell_pct directly (-> params[fourth_axis_col],
    fourth_axis_col='trail_pct' for Both). watch_list.stop_loss is NOT used at all for Both
    -- confirmed unused/irrelevant for this strategy's real semantic mapping.

    Any other strategy (TrailingExitZScoreBreakout, the only other real strategy in this
    codebase): watch_list's own take_profit/stop_loss/trail_sell_pct columns are already
    the direct semantic values (confirmed: watch_list.stop_loss == watch_list.trail_sell_
    pct in every real sample, both feed the same sl_axis_col='trail_pct' slot)."""
    if strategy == "TrailingBothZScoreBreakout":
        return arm_sell_pct, trail_buy_pct, trail_sell_pct
    return take_profit, stop_loss, trail_sell_pct


def _promoted_node_ids(conn, version, tickers, live_db_path=LIVE_DB_PATH):
    """Real 'currently promoted' node_id per ticker, resolved against watch_list (cache/
    live/trading_live.db) -- v4 (2026-09-01), rebuilt v5 (2026-09-02, real bugs found +
    fixed, see deep_backlog.md). Deliberately NOT a hardcoded dict like the legacy
    build_v6_promotion_combined_report.py's PROMOTED_NODE_IDS (confirmed by reading that
    file directly: a one-time manual snapshot, no query at all).

    Two real fixes from v4:
    1. Correct per-strategy watch_list column resolution -- see _watch_list_semantic_axes'
       own docstring for the full incident (v4 used take_profit unconditionally, which is
       always NULL for TrailingBoth).
    2. NO version filter on the watch_list query -- a live node's watch_list.version can be
       an older/differently-labeled sweep generation string than the exact `version` this
       report targets, while still representing the IDENTICAL real trading parameter
       configuration (confirmed real case: ETHU watch_list id=237 is tagged version=
       'v6-massive-...', not this campaign's 'v6.5-...-pv4' string, yet its real param
       tuple matches v6.5 candidate_nodes.id=19460 exactly). 'Matches Promotion' answers
       'is this SPECIFIC real config currently live', not 'is this exact version string
       live' -- the candidate_nodes side of the match IS still scoped to `version` (only
       tickers actually being reported on).

    Cross-validates TWO independent resolution methods per live row and returns the
    params_json-based result (built via the SAME build_params_dict/resolve_axis_columns
    call the real production pipeline uses to write every candidate_nodes.params_json --
    proven correct via a real end-to-end match, see _watch_list_semantic_axes) -- the
    flat-column method (a pure read-only SELECT against candidate_nodes' own key_cols,
    mirroring get_or_create_candidate_node's lookup WITHOUT its insert-on-miss side effect,
    deliberately avoided here to keep this a read-only audit) is computed alongside purely
    as a disagreement check. Any disagreement between the two is printed explicitly, not
    silently resolved -- per 2026-09-02 dispatch: 'don't silently pick one if they differ,
    that itself would be a new finding worth surfacing'.

    Returns {} for any ticker with no non-archived watch_list row at all."""
    import json
    import strategies
    from node_key import build_params_dict

    if not tickers:
        return {}
    placeholders = ",".join("?" * len(tickers))
    live_conn = sqlite3.connect(live_db_path)
    try:
        rows = live_conn.execute(f"""
            SELECT ticker, strategy, window, z_score_threshold, fixed_sl,
                   take_profit, stop_loss, trail_buy_pct, trail_sell_pct, arm_sell_pct,
                   max_hold_hours, entry_timing
            FROM watch_list
            WHERE archived_at IS NULL AND ticker IN ({placeholders})
        """, tickers).fetchall()
    finally:
        live_conn.close()

    out = {}
    for (ticker, strategy, window, z, fixed_sl, tp, sl, tbp, tsp, asp, hold, entry_timing) in rows:
        sem_tp, sem_sl, sem_tsp = _watch_list_semantic_axes(strategy, tp, sl, tbp, tsp, asp)
        if sem_tp is None or sem_sl is None:
            continue  # a real row missing its own strategy's required axis -- skip, don't guess

        params = build_params_dict(strategy, ticker, fixed_sl or 0.0, window, z, hold,
                                    sem_tp, sem_sl, sem_tsp or 0.0, entry_timing,
                                    strategies.resolve_axis_columns)
        canonical = json.dumps(params, sort_keys=True)
        params_row = conn.execute(
            "SELECT id FROM candidate_nodes WHERE version=? AND ticker=? AND strategy=? AND params_json=?",
            (version, ticker, strategy, canonical)).fetchone()
        params_node_id = params_row[0] if params_row else None

        is_both = strategy == "TrailingBothZScoreBreakout"
        flat_trail_buy = sem_sl if is_both else 0.0
        flat_trail_sell = sem_tsp if is_both else sem_sl
        flat_row = conn.execute("""
            SELECT id FROM candidate_nodes
            WHERE ticker=? AND strategy=? AND version=? AND window=? AND z=? AND fixed_sl=?
                  AND arm_pct=? AND trail_buy_pct=? AND trail_sell_pct=? AND max_hold_hours=?
                  AND entry_timing=?
        """, (ticker, strategy, version, int(window), float(z), float(fixed_sl or 0.0),
              float(sem_tp), float(flat_trail_buy), float(flat_trail_sell), int(hold),
              entry_timing)).fetchone()
        flat_node_id = flat_row[0] if flat_row else None

        if params_node_id != flat_node_id:
            print(f"  DISAGREEMENT resolving promoted node for {ticker}/{strategy}: "
                  f"params_json method -> {params_node_id}, flat-column method -> {flat_node_id} "
                  f"(using params_json result).")
        if params_node_id is not None:
            out[ticker] = params_node_id
    return out


def _pct100(v):
    """candidate_verification_results' core_cagr_1m/1s, addon_cagr_1m/1s, drought_cagr_
    1m/1s, core_both_cagr_1m/1s are stored as raw fractions (0-1+ scale) -- every legacy
    _pct-suffixed column this report also renders (strategy_cagr_pct, addon_compounded_
    pct, etc.) is already stored x100 by the legacy pipeline's own convention. Found as a
    real regression, 2026-09-02 (research session, confirmed on real AGQ node_id=19026):
    substituting a candidate_verification_results value into a 'Cagr'/'%'-labeled column
    without this conversion renders e.g. 0.6978 instead of 69.78. Applied ONLY at render
    time -- the DB storage itself is untouched (still fractional, consistent with whatever
    convention wrote it)."""
    return None if v is None else v * 100


def _matches_promotion(ticker, node_id, promoted_ids):
    promoted_id = promoted_ids.get(ticker)
    if promoted_id is None:
        return "N/A (not yet promoted)"
    return "YES" if node_id == promoted_id else "no"


def _k1_tranche(k1_str):
    """Mirrors candidate_full_review.py's inline k1_tranche/brokerage_only derivation
    (build_candidate_report_ground_truth, right after ticker_sector/k1_status are read) --
    kept as a small standalone copy here rather than importing, since the original isn't
    factored out as its own function. Returns (k1_tranche, brokerage_only)."""
    k1_str = k1_str or "not checked"
    if k1_str.startswith("CONFIRMED K-1"):
        tranche = "K1_CONFIRMED"
    elif k1_str.startswith("confirmed clean"):
        tranche = "CLEAN_CONFIRMED"
    elif "ETN" in k1_str:
        tranche = "ETN_NOT_K1"
    else:
        tranche = "NOT_CHECKED"
    return tranche, tranche == "K1_CONFIRMED"


def _curated_front_and_checklist(node_id, promoted_ids, k1_fn, counter_formula,
                                  full_review_by_id, lightweight_row, winner, phase4_extra_by_id,
                                  version=None, sector_fn=None, underlier_fn=None):
    """Builds ONE row's (curated-front-24-cols, checklist-144-cols) pair -- v6 (2026-09-02,
    'every tab gets the exact same 174-column layout' user dispatch). Single source of
    truth for the CURATED_HEADERS-ordered front block across all 4 tabs (Full Review,
    Combined, Candidates, All Candidates (raw)) -- was 3 separately-hand-written, already-
    inconsistent field lists (a real cause of the format-drift complaint this rework
    responds to).

    If `node_id` has a real Full Review row (this candidate went through the full
    checklist), ALL 24 curated-front columns + the full 144-column checklist block are
    populated from that row's real data -- including Years/Cliff Safe/Add on and Drought
    detail columns/Cagr Add on/CAGR Drought/CAGR Both, which a lightweight-only row can
    never have. Cagr Add on/CAGR Drought/CAGR Both use core_addon_cagr_pct/core_drought_
    cagr_pct/core_both_cagr_pct (the same real annualized CAGR _curate_combined_rows now
    ranks on, v5 fix) -- NOT the lightweight candidate_verification_results numbers, even
    on a tab (Candidates) whose own native data source is lightweight, since a real,
    better number already exists for these particular ids (all 155 Candidates rows have a
    matching Full Review row -- confirmed 1:1 by v2/v5's own verification).

    Otherwise (no Full Review match -- the common case for All Candidates (raw)'s 13,434
    rows, only ~155 of which were ever run through the full checklist): falls back to
    `lightweight_row`'s own real fields (ticker/strategy/core_cagr_1s/worst_neighbor_cagr/
    trades/core_safe/addon_cagr_1s/drought_cagr_1s/core_both_cagr_1s, whichever the row's
    native source provides) for whatever IS real, and leaves every full-checklist-only
    column (Years, Add on/Drought detail columns, and the entire 144-col checklist block)
    genuinely blank -- never fabricated, never expensively backfilled (that's the whole
    reason the raw tab stays lightweight-only, see module docstring).

    Returns (front, front_1m, checklist, phase4_extra) -- front_1m (item #3, 2026-09-02)
    is the 1-minute-resolution sibling of Cagr/Cagr Add on/CAGR Drought/CAGR Both, shown
    for direct comparison against the primary (1s-resolution, more fill-realistic)
    columns -- confirmed real case, ETHU node 19460: core_both_cagr_1m=531.9% vs.
    core_both_cagr_1s=114.8%, a 417pp gap. Always _pct100()'d: both branches source these
    straight from candidate_verification_results' raw fractions, same convention as
    core_cagr_1s (unlike the legacy _pct-suffixed FIELDNAMES columns, already x100)."""
    p4 = phase4_extra_by_id.get(node_id) or [None] * len(PHASE4_EXTRA_COLUMNS)

    fr = full_review_by_id.get(node_id)
    if fr is not None:
        front = [
            fr["ticker"], counter_formula, node_id,
            _matches_promotion(fr["ticker"], node_id, promoted_ids), k1_fn(fr["ticker"]),
            fr["strategy"], winner, _pct100(fr.get("core_cagr_1s")), fr["worst_neighbor_pct"], None,
            fr["trades"], fr["years"], fr["status"],
            fr["addon_compounded_pct"], fr["addon_n"], fr["addon_tranche"], fr["addon_wr_tranche"],
            fr["drought_compounded_pct"], fr["drought_n"], fr["drought_tranche"], fr["drought_wr_verdict"],
            fr["core_addon_cagr_pct"], fr["core_drought_cagr_pct"], fr["core_both_cagr_pct"],
        ]
        front_1m = [_pct100(fr.get("core_cagr_1m")), _pct100(fr.get("addon_cagr_1m")),
                    _pct100(fr.get("drought_cagr_1m")), _pct100(fr.get("core_both_cagr_1m"))]
        checklist = [fr.get(h) for h in FIELDNAMES]
        return front, front_1m, checklist, p4

    r = lightweight_row or {}
    core_safe = r.get("core_safe")
    cliff_safe = None if core_safe is None else ("SAFE" if core_safe else "CLIFF")
    # "Drought %" (index 17) is the one existing slot with a real phase4_results
    # counterpart of matching name+semantics (drought_compounded_pct) -- filled here per
    # item #2. "Add on %" has no equivalent (phase4_results only has addon_cagr_pct, an
    # annualized CAGR, not the raw compounded return this column expects) -- stays blank.
    front = [
        r.get("ticker"), counter_formula, node_id,
        _matches_promotion(r.get("ticker"), node_id, promoted_ids), k1_fn(r.get("ticker")),
        r.get("strategy"), winner, _pct100(r.get("core_cagr_1s")), r.get("worst_neighbor_cagr"), None,
        r.get("trades"), None, cliff_safe,
        None, None, None, None,
        p4[1], None, None, None,
        _pct100(r.get("addon_cagr_1s")), _pct100(r.get("drought_cagr_1s")), _pct100(r.get("core_both_cagr_1s")),
    ]
    front_1m = [_pct100(r.get("core_cagr_1m")), _pct100(r.get("addon_cagr_1m")),
                _pct100(r.get("drought_cagr_1m")), _pct100(r.get("core_both_cagr_1m"))]
    # Identity/classification fields (v7, 2026-09-04): cheap ticker/version-level lookups
    # that don't require the expensive full-checklist compute -- were previously zeroed
    # out unconditionally along with the genuinely-expensive trade-resimulation columns
    # (walk-forward per-fold detail, addon/drought robustness verdicts, bear-market tests)
    # this block also carries. Filled here for every raw/lightweight row, not just rows
    # with a Full Review match -- real gap found 2026-09-04 (a raw-only row's sector/K1/
    # liquidity classification costs nothing, unlike the trade-stat checks it was
    # incorrectly gated alongside). candidate_type/also_matches/pick/comment/liquidity_*
    # genuinely don't apply to a non-curated row (no selection category, no cached avg-
    # volume lookup wired here) -- left blank rather than guessed.
    checklist_map = {h: None for h in FIELDNAMES}
    ticker_val = r.get("ticker")
    checklist_map["ticker"] = ticker_val
    checklist_map["node_id"] = node_id
    checklist_map["strategy"] = r.get("strategy")
    if version is not None:
        checklist_map["config_version"] = version
    if ticker_val is not None:
        if sector_fn is not None:
            checklist_map["sector"] = sector_fn(ticker_val)
        k1_str = k1_fn(ticker_val)
        checklist_map["k1_status"] = k1_str
        checklist_map["k1_tranche"], checklist_map["brokerage_only"] = _k1_tranche(k1_str)
        if underlier_fn is not None:
            checklist_map["underlier_count"], checklist_map["underlier_note"] = underlier_fn(ticker_val)
    checklist = [checklist_map[h] for h in FIELDNAMES]
    return front, front_1m, checklist, p4


def _write_curated_tab(ws, node_ids, promoted_ids, k1_fn, full_review_by_id,
                        lightweight_by_id, winner_by_id, phase4_extra_by_id,
                        version=None, sector_fn=None, underlier_fn=None):
    """Writes one CURATED_HEADERS(24) + TWO_TAB_1M_HEADERS(4) + TWO_TAB_MANUAL_BLANK_COLS(2)
    + FIELDNAMES(144) + PHASE4_EXTRA_HEADERS(10) = 184-column tab for the given `node_ids`
    in order -- shared by all 4 tabs, see _curated_front_and_checklist's own docstring for
    the per-row data-sourcing rule. The phase4 block (item #2, 2026-09-02) is appended
    after the existing 174 columns rather than overloaded onto any of them, and is
    populated uniformly (whichever branch -- Full Review match or lightweight fallback --
    supplied the rest of the row) since phase4_results covers the whole core_safe
    population, not just raw-only rows. The 1m block (item #3, same day) sits right after
    CURATED_HEADERS, consuming 4 of the original 6 manual-blank columns -- see
    TWO_TAB_1M_HEADERS' own module-level comment for why it's local to this file."""
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    headers = (CURATED_HEADERS + TWO_TAB_1M_HEADERS + [None] * TWO_TAB_MANUAL_BLANK_COLS
               + list(FIELDNAMES) + PHASE4_EXTRA_HEADERS)
    ws.append(headers)
    for cell in ws[1]:
        if cell.value:
            cell.font = Font(bold=True)
    for i, node_id in enumerate(node_ids, start=2):
        counter = f"=COUNTIF($A$2:A{i},A{i})"
        front, front_1m, checklist, phase4_extra = _curated_front_and_checklist(
            node_id, promoted_ids, k1_fn, counter, full_review_by_id,
            lightweight_by_id.get(node_id), winner_by_id.get(node_id), phase4_extra_by_id,
            version=version, sector_fn=sector_fn, underlier_fn=underlier_fn)
        ws.append(front + front_1m + [None] * TWO_TAB_MANUAL_BLANK_COLS + checklist + phase4_extra)
    for i, h in enumerate(CURATED_HEADERS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = max(10, min(len(h) + 2, 30))
    ws.freeze_panes = "B2"

    # User's own standing display convention (2026-09-04): every percentage-shaped
    # column, spreadsheet-wide (CURATED_HEADERS/1m/FIELDNAMES/PHASE4_EXTRA_HEADERS
    # alike -- see _is_pct_header), gets a fixed one-decimal DISPLAY format (never
    # rounds the underlying stored value, just how Excel renders it) and a narrow
    # fixed column width, since these are the columns actually scanned across many
    # rows at once and don't need CURATED_HEADERS' name-length-based width. Applied
    # AFTER the CURATED_HEADERS width loop above so it overrides those columns too.
    max_row = ws.max_row
    for col_idx, h in enumerate(headers, start=1):
        if _is_pct_header(h):
            letter = get_column_letter(col_idx)
            ws.column_dimensions[letter].width = USER_PCT_COLUMN_WIDTH
            for row in range(2, max_row + 1):
                ws.cell(row=row, column=col_idx).number_format = USER_PCT_NUMBER_FORMAT

    # User's own standing manual-review convention (2026-09-04): a fixed set of
    # FIELDNAMES columns get highlighted (the ones actually scanned); every OTHER
    # FIELDNAMES column from the very start of the block (AE) through the last
    # highlighted one gets grouped/collapsed (confirmed 2026-09-04: "AE through BN
    # needs to be hidden" -- the front of the block, before the first highlighted
    # column, collapses too, not just the internal gaps). Columns after the last
    # highlighted one are left alone (untouched, not grouped) -- not yet asked to
    # collapse those. Keyed off USER_HIGHLIGHTED_FIELDNAMES (names, not letters) so
    # this stays correct if FIELDNAMES' own order/length ever shifts -- only breaks
    # if one of these exact names is renamed or removed from COLUMN_DEFS entirely.
    fieldnames_start_col = len(CURATED_HEADERS) + len(TWO_TAB_1M_HEADERS) + TWO_TAB_MANUAL_BLANK_COLS + 1
    highlighted_cols = sorted(fieldnames_start_col + FIELDNAMES.index(name) for name in USER_HIGHLIGHTED_FIELDNAMES)
    for col in range(fieldnames_start_col, highlighted_cols[-1] + 1):
        if col not in highlighted_cols:
            ws.column_dimensions[get_column_letter(col)].outlineLevel = 1
            ws.column_dimensions[get_column_letter(col)].hidden = True
    # Front-block visibility (A:AD -- see FRONT_BLOCK_HIDDEN_POSITIONS' own comment).
    for pos in FRONT_BLOCK_HIDDEN_POSITIONS:
        ws.column_dimensions[get_column_letter(pos)].outlineLevel = 1
        ws.column_dimensions[get_column_letter(pos)].hidden = True
    # User's global category-color convention (2026-09-04): every CAGR/win-rate/
    # plain-count column, spreadsheet-wide, gets a fixed fill by category -- separate
    # layer from the pct number-format/width treatment above (a column can carry
    # both). Checked in priority order (CAGR > win-rate > number, first match wins)
    # since these three patterns are mutually exclusive in this report's real column
    # set. Columns in USER_HIGHLIGHTED_FIELDNAMES that match one of these categories
    # get the category color instead of the fallback yellow below -- only a
    # highlighted column matching NONE of the three (resolution_spread_tranche, the
    # one case today) keeps yellow.
    category_colored_cols = set()
    for col_idx, h in enumerate(headers, start=1):
        if _is_cagr_header(h):
            fill_color = USER_CAGR_FILL
        elif _is_winrate_header(h):
            fill_color = USER_WINRATE_FILL
        elif _is_number_header(h):
            fill_color = USER_NUMBER_FILL
        else:
            continue
        category_colored_cols.add(col_idx)
        letter = get_column_letter(col_idx)
        fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
        for row in range(1, ws.max_row + 1):
            ws[f"{letter}{row}"].fill = fill

    highlight_fill = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")
    for col in highlighted_cols:
        if col in category_colored_cols:
            continue
        letter = get_column_letter(col)
        for row in range(1, ws.max_row + 1):
            ws[f"{letter}{row}"].fill = highlight_fill


def _enrich_full_review_core_cagr(conn, csv_rows):
    """Attaches the real core_cagr_1s (candidate_verification_results, keyed by node_id)
    onto each Full Review csv_row -- v4 fix (2026-09-01, user-confirmed). Full Review's
    own strategy_cagr_pct is always None for candidate_nodes-sourced rows (candidate_nodes
    doesn't persist a cagr column, see phase4_candidate_nodes_resolver.py's documented
    deviation #1) -- core_cagr_1s is the real per-candidate CAGR that DOES exist for this
    data source, and is what _curate_combined_rows below uses for its Best-Both ranking
    and what the Combined tab's 'Cagr' column shows, per the standing project convention
    of CAGR over robust_alpha for reporting/ranking (feedback_cagr_over_robust_alpha)."""
    ids = [r["node_id"] for r in csv_rows if r.get("node_id") is not None]
    cagr_map = {}
    cagr_1m_map = {}
    if ids:
        placeholders = ",".join("?" * len(ids))
        cagr_map = dict(conn.execute(
            f"SELECT candidate_id, core_cagr_1s FROM candidate_verification_results "
            f"WHERE candidate_id IN ({placeholders})", ids))
        # item #3 (2026-09-02): the 1-minute-resolution siblings, never previously carried
        # onto the Full-Review-match branch (only core_cagr_1s was pulled here) -- these
        # ARE raw fractions straight from candidate_verification_results, same as
        # core_cagr_1s, so the front-row build below still needs _pct100() on them.
        cagr_1m_map = {row[0]: row[1:] for row in conn.execute(
            f"SELECT candidate_id, core_cagr_1m, addon_cagr_1m, drought_cagr_1m, core_both_cagr_1m "
            f"FROM candidate_verification_results WHERE candidate_id IN ({placeholders})", ids)}
    for r in csv_rows:
        r["core_cagr_1s"] = cagr_map.get(r.get("node_id"))
        m1 = cagr_1m_map.get(r.get("node_id")) or (None, None, None, None)
        r["core_cagr_1m"], r["addon_cagr_1m"], r["drought_cagr_1m"], r["core_both_cagr_1m"] = m1
    return csv_rows


def _recompute_stacked_cagr_with_1s_core(csv_rows):
    """Fixes a real inconsistency found 2026-09-04: core_addon_cagr_pct/core_drought_
    cagr_pct/core_both_cagr_pct (this checklist's own stacking math, run_optimization_
    sweep.py's build_candidate_report_ground_truth) multiply a core_factor derived from
    THIS function's own fresh GT-kernel re-simulation -- which, unlike Phase5's dedicated
    1s/1m comparison, always runs on MINUTE-resolution data (minute_df, confirmed at
    every run_backtest_ground_truth call site in run_optimization_sweep.py). Meanwhile
    the adjacent 'Cagr' column already got corrected to the trusted core_cagr_1s (v4 fix,
    _enrich_full_review_core_cagr, above) -- but that fix was never propagated into the
    3 stacking formulas, so they silently kept multiplying the OLD, less-trusted minute-
    resolution core factor. Not a resolution difference you should expect (a genuine
    'the same number computed twice can differ' case) -- it's one already-applied fix
    that didn't reach 3 downstream formulas sharing the same input.

    Re-derives the exact same gating logic build_candidate_report_ground_truth used
    (addon_robustness_verdict=='OK' gate, drought_robustness_verdict=='OK' gate,
    drought_ie_verdict=='REAL_SELECTION' override) from this row's own already-flattened
    checklist columns -- verified byte-for-byte against a real row (LABU node 43258,
    2026-09-04: reconstructed core_drought_cagr_pct=92.94676625259022 vs the real stored
    92.94676625259018, floating-point-only difference) before substituting the corrected
    core_cagr_1s-derived factor in place of the original re-simulated one.

    Only touches rows with a real core_cagr_1s (Phase5-verified -- every curated row
    qualifies) and a real 'years' (needed to convert an annualized CAGR back to a total-
    return factor and re-annualize) -- leaves everything else (raw-only/Phase4-only rows,
    which have no corrected core_cagr_1s to substitute) exactly as build_candidate_
    report_ground_truth originally computed it, same limitation the 'Cagr' display fix
    already has."""
    for r in csv_rows:
        core_cagr_1s = r.get("core_cagr_1s")
        years = r.get("years")
        if core_cagr_1s is None or not years:
            continue
        addon_ok = r.get("addon_robustness_verdict") == "OK"
        addon_compounded = r.get("addon_compounded_pct")
        addon_factor_gated = (1.0 + addon_compounded / 100.0) if (addon_ok and addon_compounded is not None) else 1.0

        drought_ie_real_selection = r.get("drought_ie_verdict") == "REAL_SELECTION"
        if drought_ie_real_selection and r.get("drought_ie_included_compounded_pct") is not None:
            drought_factor_gated = 1.0 + r["drought_ie_included_compounded_pct"] / 100.0
        else:
            drought_ok = r.get("drought_robustness_verdict") == "OK"
            drought_compounded = r.get("drought_compounded_pct")
            drought_factor_gated = (1.0 + drought_compounded / 100.0) if (drought_ok and drought_compounded is not None) else 1.0

        core_factor_corrected = (1.0 + core_cagr_1s) ** years
        r["core_addon_cagr_pct"] = ((core_factor_corrected * addon_factor_gated) ** (1.0 / years) - 1.0) * 100.0
        r["core_drought_cagr_pct"] = ((core_factor_corrected * drought_factor_gated) ** (1.0 / years) - 1.0) * 100.0
        r["core_both_cagr_pct"] = (
            (core_factor_corrected * addon_factor_gated * drought_factor_gated) ** (1.0 / years) - 1.0) * 100.0
    return csv_rows


def _cagr_sort_key(field):
    return lambda r: r.get(field) if r.get(field) is not None else float("-inf")


def _curate_combined_rows(csv_rows):
    """Local adaptation of build_v6_promotion_combined_report.curate_rows() for the
    Combined tab -- v4 fix (2026-09-01, real bug found + user-confirmed fix), extended v5
    (2026-09-02, ranking-metric unification + new category, both user-confirmed, see
    deep_backlog.md). Deliberately NOT calling that function directly (its own file isn't
    edited either).

    Changes from the original build_v6_promotion_combined_report.curate_rows():
    1. (v4) Dedup key is `node_id` instead of `(ticker, strategy, fixed_sl)` -- the
       original key assumed one campaign = one window; candidate_nodes rows here have no
       `fixed_sl` field in their csv_row shape at all (not in FIELDNAMES), so `r.get(
       'fixed_sl')` silently returned None for every row, collapsing ALL rows for a
       (ticker, strategy) pair onto one dedup key (confirmed on real ETHU data). node_id
       is a real, unique per-candidate identity for this data source.
    2. (v5) Add On/Drought sort keys are `core_addon_cagr_pct`/`core_drought_cagr_pct`
       (real annualized CAGR, computed by gt_full_review_rows against this candidate's OWN
       trades) instead of `addon_compounded_pct`/`drought_compounded_pct` (a raw, non-
       annualized per-leg return) -- found as a real Combined-vs-Candidates ranking-metric
       mismatch (Candidates tab's own cri.curate() always ranked on an annualized CAGR;
       Combined ranked the same categories on a non-annualized return, a genuinely
       different number that could disagree on which candidate wins). core_addon_cagr_pct/
       core_drought_cagr_pct is also now what gets DISPLAYED in the unified Cagr Add on/
       CAGR Drought columns for any row with real Full Review data (see _curated_front),
       so the ranking metric and the displayed metric are the same number everywhere.
    3. (v4) Best-Both sort key is `core_cagr_1s` instead of `strategy_cagr_pct` (always
       None for this data source, candidate_nodes doesn't persist cagr).
    4. (v5) New 'Best Core' category (any strategy, top-2 by core_cagr_1s) -- the existing
       'Best-Both' category is restricted to TrailingBothZScoreBreakout by design (it's
       specifically evaluating the live-default combined-strategy's own core performance),
       which meant a TrailingExitZScoreBreakout candidate (AGQ/ETHU/UGL/DPST/SOXL, etc.)
       could only ever appear via Add On/Drought/the last-resort fallback, never on pure
       core performance. 'Best Core' is strategy-unrestricted, same core_cagr_1s metric as
       Best-Both. Both categories are kept (not merged) -- Best-Both's own
       TrailingBoth-specific meaning is unchanged, this only ADDS visibility for the
       strategy it excludes."""
    by_ticker = {}
    for r in csv_rows:
        by_ticker.setdefault(r["ticker"], []).append(r)

    def key(r):
        return r["node_id"]

    out = []
    for ticker, rows in by_ticker.items():
        safe = [r for r in rows if r.get("status") == "SAFE"]

        addon = [r for r in safe if r.get("addon_tranche") != "FRAGILE"
                 and r.get("core_addon_cagr_pct") is not None]
        addon.sort(key=_cagr_sort_key("core_addon_cagr_pct"), reverse=True)

        # core_drought_cagr_pct is None unless drought was BOTH genuinely computed (this
        # candidate's strategy supports it, see strategies.uses_arm_trail_exit) AND
        # verified robust (drought_ok, OR the IE vol-gate's own separately-validated
        # REAL_SELECTION override) -- candidate_full_review.py's own real fix, 2026-09-02.
        # `is not None` alone is now the correct full gate; a separate `drought_tranche !=
        # "FRAGILE"` check would be WRONG here (and was, before this fix) -- a
        # REAL_SELECTION-verified row can have a real core_drought_cagr_pct while its base
        # drought_tranche is still "FRAGILE" (the IE challenge validates a DIFFERENT number
        # than the raw chrono-split check), so gating on tranche in addition would exclude
        # a genuinely verified row for the wrong reason.
        drought = [r for r in safe if r.get("core_drought_cagr_pct") is not None]
        drought.sort(key=_cagr_sort_key("core_drought_cagr_pct"), reverse=True)

        best_both = [r for r in safe if r.get("strategy") == "TrailingBothZScoreBreakout"]
        best_both.sort(key=_cagr_sort_key("core_cagr_1s"), reverse=True)

        best_core = list(safe)
        best_core.sort(key=_cagr_sort_key("core_cagr_1s"), reverse=True)

        winners = {}
        for label, group in (("Add On", addon[:2]), ("Drought", drought[:2]),
                              ("Best-Both", best_both[:2]), ("Best Core", best_core[:2])):
            for r in group:
                k = key(r)
                if k not in winners:
                    winners[k] = (r, [])
                winners[k][1].append(label)

        if winners:
            for r, cats in winners.values():
                r["_winner"] = ", ".join(cats)
                out.append(r)
        elif safe:
            best = max(safe, key=_cagr_sort_key("core_cagr_1s"))
            best["_winner"] = "Core (no category winner)"
            out.append(best)
    return out


def _tickers_for_version(conn, version):
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM candidate_nodes WHERE version=?", (version,)).fetchall()
    return sorted(r[0] for r in rows)


def _core_safe_population_rows(conn, version, min_trades=50):
    """Real core_safe=True population (v3, 2026-09-01 overnight full-checklist dispatch,
    user-confirmed via research session after the v2 timing recalculation) -- every
    candidate_nodes row with a real phase4_results.core_safe=1 verdict AND trades>=
    min_trades. Returns rows carrying exactly the scope-key fields (id/ticker/strategy/
    entry_timing/fixed_sl/window) _full_review_rows_for_curated needs -- same shape a
    curated row already has (that function only ever reads those 6 keys), so this is a
    drop-in alternate population for Tab 1 with no new grouping/dispatch logic required."""
    q = """
    SELECT n.id, n.ticker, n.strategy, n.window, n.entry_timing, n.fixed_sl
    FROM candidate_nodes n
    JOIN phase4_results p4 ON p4.candidate_id = n.id
    WHERE n.version=? AND p4.core_safe=1 AND n.trades>=?
    """
    cur = conn.execute(q, (version, min_trades))
    cols = ["id", "ticker", "strategy", "window", "entry_timing", "fixed_sl"]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _raw_population_rows(conn, version):
    """ALL candidate_nodes rows for `version` (v2 item 2) -- deliberately NOT run through
    the full checklist (see module docstring's Tab 1 scoping rationale -- the raw
    population is 200x+ larger than the curated set; full-checklist compute against it
    would be wildly infeasible). LEFT JOIN so an unverified row (candidate_verification_
    results has no row for it yet) still appears, with NULL lightweight-metric columns --
    the true raw population, verified or not (candidate_report_inmemory.fetch_rows' own
    INNER JOIN deliberately only wants verified rows for ITS purpose; this is a different
    purpose)."""
    cn_cols = cri.COLUMNS[:14]
    q = f"""
    SELECT n.{', n.'.join(cn_cols)},
           vr.core_cagr_1m, vr.core_cagr_1s, vr.n_trades_1m, vr.n_trades_1s,
           vr.addon_cagr_1m, vr.addon_cagr_1s, vr.drought_cagr_1m, vr.drought_cagr_1s,
           vr.core_both_cagr_1m, vr.core_both_cagr_1s, p4.core_safe
    FROM candidate_nodes n
    LEFT JOIN candidate_verification_results vr ON vr.candidate_id = n.id
    LEFT JOIN phase4_results p4 ON p4.candidate_id = n.id
    WHERE n.version = ?
    """
    cur = conn.execute(q, (version,))
    return [dict(zip(RAW_COLUMNS, r)) for r in cur.fetchall()]


def _full_review_worker(payload):
    """Top-level, picklable ProcessPoolExecutor worker (v2 item 4) -- computes one scope-
    group's full-checklist rows in a fresh process with its OWN sqlite connection (a live
    sqlite3.Connection can't cross a process boundary). Returns the same flattened,
    GT_SKIP_COLUMNS-labeled row shape the serial path returns, plus any missing node_ids,
    so the parent's result handling doesn't care which path ran."""
    db_path, version, vol_gate, ticker, strategy, entry_timing, fixed_sl, window, ids = payload
    conn = sqlite3.connect(db_path, timeout=60.0)
    try:
        ensure_candidate_nodes_table(conn)
        population = derive_phase25_candidates_from_candidate_nodes(
            ticker, strategy, version, fixed_sl=fixed_sl, entry_timing=entry_timing,
            window=window, full_population=True)
        override = [c for c in population if c["id"] in ids]
        missing = sorted(ids - {c["id"] for c in override})
        if not override:
            return [], missing
        out_rows = gt_full_review_rows(conn, ticker, strategy, version, entry_timing, fixed_sl,
                                        vol_gate=vol_gate, candidates_override=override)
    finally:
        conn.close()
    csv_rows = []
    for rec in out_rows:
        row = _build_output_row(rec)
        for col in GT_SKIP_COLUMNS:
            row[col] = GT_SKIP_LABEL
        csv_rows.append(row)
    return csv_rows, missing


def _full_review_rows_for_curated(curated_rows, version, vol_gate, db_path, max_workers=1):
    """Builds Tab 1's full-checklist rows scoped to exactly `curated_rows`' node_ids --
    see module docstring for why (unscoped is tens-of-hours infeasible for a real multi-
    scope campaign). Groups by real (ticker, strategy, entry_timing, fixed_sl, window)
    scope (all present directly on each curated row -- no re-discovery needed) so
    candidates sharing a scope share one derive_phase25_candidates_from_candidate_nodes
    (full_population=True) lookup rather than repeating it per candidate.

    max_workers<=1 (default): original serial path, one shared connection.
    max_workers>1: ProcessPoolExecutor, throttled via campaign_registry.get_workers_
    budget(version) -- read ONCE (not polled mid-run, same convention bench_phase1_
    phase2_inmemory.py's own _dispatch documents), bounded-submission when genuinely
    throttled (budget < max_workers), fails toward unthrottled (submit-everything) on any
    missing/invalid/unreadable budget -- never toward a hang or crash. Caller decides
    if/when parallelizing is safe (e.g. no other campaign actively consuming CPU budget);
    this function does not check that itself."""
    groups = {}
    for r in curated_rows:
        key = (r["ticker"], r["strategy"], r["entry_timing"], r["fixed_sl"], r["window"])
        groups.setdefault(key, set()).add(r["id"])
    tasks = [(db_path, version, vol_gate, ticker, strategy, entry_timing, fixed_sl, window, ids)
             for (ticker, strategy, entry_timing, fixed_sl, window), ids in groups.items()]

    def _report(ticker, strategy, entry_timing, fixed_sl, window, ids, rows, missing):
        if missing:
            print(f"  WARNING: {ticker}/{strategy}/entry_timing={entry_timing}/fixed_sl={fixed_sl}/"
                  f"window={window}: {len(missing)} curated node_id(s) not found in the real "
                  f"candidate_nodes population for this scope: {missing} -- skipping them.")

    if max_workers <= 1:
        conn = sqlite3.connect(db_path, timeout=60.0)
        ensure_candidate_nodes_table(conn)
        csv_rows = []
        try:
            for i, (_db, _v, _vg, ticker, strategy, entry_timing, fixed_sl, window, ids) in enumerate(tasks):
                print(f"\n{'#' * 100}\n[{i + 1}/{len(tasks)}] {ticker} / {strategy} / {version} "
                      f"/ entry_timing={entry_timing} / fixed_sl={fixed_sl} / window={window} "
                      f"-- {len(ids)} curated candidate(s)\n{'#' * 100}")
                population = derive_phase25_candidates_from_candidate_nodes(
                    ticker, strategy, version, fixed_sl=fixed_sl, entry_timing=entry_timing,
                    window=window, full_population=True)
                override = [c for c in population if c["id"] in ids]
                missing = sorted(ids - {c["id"] for c in override})
                _report(ticker, strategy, entry_timing, fixed_sl, window, ids, None, missing)
                if not override:
                    continue
                try:
                    out_rows = gt_full_review_rows(conn, ticker, strategy, version, entry_timing,
                                                     fixed_sl, vol_gate=vol_gate,
                                                     candidates_override=override)
                except Exception as e:
                    import traceback
                    print(f"  UNEXPECTED error on this scope, skipping: {e}\n{traceback.format_exc()}")
                    continue
                for rec in out_rows:
                    row = _build_output_row(rec)
                    for col in GT_SKIP_COLUMNS:
                        row[col] = GT_SKIP_LABEL
                    csv_rows.append(row)
        finally:
            conn.close()
        return csv_rows

    from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
    import campaign_registry
    b = campaign_registry.get_workers_budget(version)
    budget = max_workers if not isinstance(b, int) or b <= 0 else max(1, min(b, max_workers))
    print(f"Phase 10 parallel checklist compute: {len(tasks)} scope-groups, pool size "
          f"{max_workers}, throttled budget {budget} "
          f"(campaign_registry.get_workers_budget({version!r})={b!r})")

    csv_rows = []
    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        task_iter = iter(tasks)
        in_flight = {}

        def _submit_next():
            try:
                t = next(task_iter)
            except StopIteration:
                return False
            fut = pool.submit(_full_review_worker, t)
            in_flight[fut] = t
            return True

        while len(in_flight) < budget and _submit_next():
            pass
        completed = 0
        while in_flight:
            done, _pending = wait(in_flight.keys(), return_when=FIRST_COMPLETED)
            for fut in done:
                (_db, _v, _vg, ticker, strategy, entry_timing, fixed_sl, window, ids) = in_flight.pop(fut)
                completed += 1
                try:
                    rows, missing = fut.result()
                except Exception as e:
                    import traceback
                    print(f"  UNEXPECTED error on {ticker}/{strategy}/fixed_sl={fixed_sl}/"
                          f"window={window}, skipping: {e}\n{traceback.format_exc()}")
                    rows, missing = [], []
                _report(ticker, strategy, entry_timing, fixed_sl, window, ids, rows, missing)
                csv_rows.extend(rows)
                print(f"  [{completed}/{len(tasks)}] {ticker}/{strategy}/fixed_sl={fixed_sl}/"
                      f"window={window} done ({len(rows)} rows)")
            while len(in_flight) < budget and _submit_next():
                pass
    return csv_rows


def _write_report_xlsx(out_path, full_review_rows, curated_rows, raw_rows, conn, version, tickers):
    """v6 (2026-09-02, 'every tab gets the exact same 174-column layout' user dispatch):
    all 4 tabs (Full Review, Combined, Candidates, All Candidates (raw)) now share the
    IDENTICAL CURATED_HEADERS(24) + MANUAL_BLANK_COLS(6) + FIELDNAMES(144) = 174-column
    structure, via the single shared _write_curated_tab/_curated_front_and_checklist path
    -- was 3 separately hand-written, already-inconsistent header/field lists per tab
    (the real complaint this rework responds to: 'every one of the 4 tabs... should have
    the same format... i just want it to be consistent'). Row POPULATION (which node_ids
    appear on which tab) is unchanged from v5 -- only the column structure is unified.

    Item #2 (2026-09-02, same-day follow-up): +10 columns (PHASE4_EXTRA_HEADERS) appended
    after the 174, real phase4_results data joined by node_id -- see _phase4_extra_by_id's
    own docstring. Total is now 184 columns, still identical across all 4 tabs."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter

    promoted_ids = _promoted_node_ids(conn, version, tickers)
    k1_cache = {}

    def _k1(ticker):
        if ticker not in k1_cache:
            k1_cache[ticker] = k1_status(conn, ticker)
        return k1_cache[ticker]

    sector_cache, underlier_cache = {}, {}

    def _sector(ticker):
        if ticker not in sector_cache:
            sector_cache[ticker] = ticker_sector(conn, ticker)
        return sector_cache[ticker]

    def _underlier(ticker):
        if ticker not in underlier_cache:
            underlier_cache[ticker] = underlier_info(conn, ticker)
        return underlier_cache[ticker]

    full_review_rows = _enrich_full_review_core_cagr(conn, full_review_rows)
    full_review_rows = _recompute_stacked_cagr_with_1s_core(full_review_rows)
    full_review_by_id = {r["node_id"]: r for r in full_review_rows}
    combined_rows = _curate_combined_rows(full_review_rows)
    combined_rows.sort(key=lambda r: (r["ticker"], r["strategy"]))
    curated_rows = sorted(curated_rows, key=lambda r: (r["ticker"], -(r["core_cagr_1s"] or -1e9)))
    candidates_by_id = {r["id"]: r for r in curated_rows}
    raw_by_id = {r["id"]: r for r in raw_rows}
    winner_by_id = {r["id"]: r.get("_winner") for r in curated_rows}  # from cri.curate(), used on the raw tab

    all_node_ids = ({r["node_id"] for r in full_review_rows} | {r["node_id"] for r in combined_rows}
                    | {r["id"] for r in curated_rows} | {r["id"] for r in raw_rows})
    phase4_extra_by_id = _phase4_extra_by_id(conn, all_node_ids)

    wb = Workbook()

    review_ws = wb.active
    review_ws.title = "Full Review"
    _write_curated_tab(review_ws, [r["node_id"] for r in full_review_rows], promoted_ids, _k1,
                        full_review_by_id, {}, {}, phase4_extra_by_id,  # no lightweight fallback needed -- every id has a FR row
                        version=version, sector_fn=_sector, underlier_fn=_underlier)

    combined_ws = wb.create_sheet("Combined")
    combined_winner_by_id = {r["node_id"]: r.get("_winner") for r in combined_rows}
    _write_curated_tab(combined_ws, [r["node_id"] for r in combined_rows], promoted_ids, _k1,
                        full_review_by_id, {}, combined_winner_by_id, phase4_extra_by_id,
                        version=version, sector_fn=_sector, underlier_fn=_underlier)

    cand_ws = wb.create_sheet("Candidates")
    _write_curated_tab(cand_ws, [r["id"] for r in curated_rows], promoted_ids, _k1,
                        full_review_by_id, candidates_by_id, winner_by_id, phase4_extra_by_id,
                        version=version, sector_fn=_sector, underlier_fn=_underlier)

    raw_ws = wb.create_sheet("All Candidates (raw)")
    _write_curated_tab(raw_ws, [r["id"] for r in raw_rows], promoted_ids, _k1,
                        full_review_by_id, raw_by_id, winner_by_id, phase4_extra_by_id,
                        version=version, sector_fn=_sector, underlier_fn=_underlier)

    def_ws = wb.create_sheet("Column Definitions")
    def_ws.append(["Column", "Definition"])
    for cell in def_ws[1]:
        cell.font = Font(bold=True)
    for col, definition in COLUMN_DEFS.items():
        def_ws.append([col, definition])
        def_ws.cell(row=def_ws.max_row, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    def_ws.append(["node_id join key", "'Node ID' (or Full Review's 'node_id') == candidate_nodes.id on "
                                        "every one of the 4 data tabs -- all 4 share the identical column "
                                        "layout (CURATED_HEADERS + 1m-CAGR block + 2 blank manual + full "
                                        "checklist + phase4 block), so any row can be cross-referenced to "
                                        "the others directly."])
    def_ws.append(["Winner", "Full Review: always blank (this tab isn't curated -- it's the full scoped "
                              "population). Combined: real category label(s) from this report's own "
                              "curation (Add On/Drought/Best-Both/Best Core/Core fallback). Candidates/"
                              "All Candidates (raw): the Candidates tab's OWN curation label (candidate_"
                              "report_inmemory.curate(), a different real selection scheme than Combined's "
                              "-- Core/Add On/Drought top-N by candidate_verification_results CAGR) when "
                              "this node_id is in that curated set, else blank."])
    def_ws.append(["Matches Promotion", "YES/no if this node_id matches the real currently-promoted node "
                                         "for this ticker (resolved against watch_list, not hardcoded, "
                                         "matched on the real param tuple regardless of watch_list's own "
                                         "version label), or 'N/A (not yet promoted)' if no watch_list row "
                                         "matches this ticker at all."])
    def_ws.append(["Full-checklist-only columns (Years, Add on/Drought detail columns, Cagr Add on/CAGR "
                    "Drought/CAGR Both, and the entire checklist block starting after the 6 blank manual "
                    "columns)", "Real data only for a row that actually went through the full checklist "
                                 "compute (every Full Review/Combined row; Candidates rows too, since all "
                                 "155 happen to have a matching Full Review row this campaign) -- blank, "
                                 "never fabricated or expensively backfilled, for a raw-only row (~13,279 "
                                 "of All Candidates (raw)'s 13,434 rows) that was never run through it."])
    def_ws.append(["Phase4 Addon Cagr % / Phase4 Drought Compounded % / Phase4 Drought Combined Compounded % / "
                    "Phase4 Check8 Compounded % / Phase4 Check8 Compounded (no best) % / Phase4 Check8 Best "
                    "Trade Share % / Phase4 Check11 Max Drawdown % / Phase4 Check13 Worst Fold Cagr % / "
                    "Phase4 Check13 Any Fold Fragile / Phase4 Core/Addon Disagreement (appended after the "
                    "144-column checklist block, item #2 2026-09-02)",
                    "Real phase4_results data (candidate_verification_store's Phase4 table), joined by "
                    "node_id, populated for every row whose node_id has a phase4_results row (the full real "
                    "core_safe population, 9,212/9,212 for v6.5-pv4) regardless of which branch (Full Review "
                    "match or lightweight fallback) built the rest of that row -- cheap existing data, no new "
                    "compute. Deliberately NOT merged into any existing CURATED_HEADERS/checklist column: "
                    "checked each existing slot's own semantics first ('Add on %'/addon_compounded_pct is a "
                    "raw compounded return of addon-only trades computed by the full checklist, NOT the same "
                    "metric as phase4_results.addon_cagr_pct, an annualized CAGR -- overloading them would "
                    "repeat the exact silent scale/semantic mismatch this report's own Cagr-column fix just "
                    "caught) -- 'Drought %' is the one exception, filled directly since phase4_results."
                    "drought_compounded_pct matches its name+semantics exactly. check8/check11/check13/"
                    "core_addon_disagreement have no existing-column counterpart at all."])
    def_ws.append(["Cagr (1m) / Cagr Add on (1m) / CAGR Drought (1m) / CAGR Both (1m) (item #3, 2026-09-02)",
                    "The 1-minute-resolution sibling of the primary Cagr/Cagr Add on/CAGR Drought/CAGR Both "
                    "columns -- same core_cagr_1m/addon_cagr_1m/drought_cagr_1m/core_both_cagr_1m fields "
                    "phase5_second_level_overlay_check.py computes and candidate_verification_results "
                    "stores, at 1-minute bar resolution instead of the primary 1s (1-second) resolution. "
                    "1-minute is the COARSER, more fill-optimistic of the two (a whole extra minute of "
                    "intra-bar price movement to pick the most favorable fill inside) -- shown here for "
                    "direct comparison only; the existing 1s-resolution columns remain the primary/trusted "
                    "numbers for reporting/ranking, per feedback_cagr_over_robust_alpha's own convention. "
                    "The two can diverge hugely: confirmed real case, ETHU node 19460, core_both_cagr_1m="
                    "531.9% vs. core_both_cagr_1s=114.8%, a 417pp gap."])
    def_ws.append(["Generated", _git_provenance_stamp()])
    def_ws.column_dimensions["A"].width = 32
    def_ws.column_dimensions["B"].width = 110

    wb.save(out_path)
    _persist_snapshot(out_path)


def build_report(conn, version, tickers=None, top_n=5, vol_gate=DEFAULT_VOL_GATE,
                  workers=1, db_path=DB_PATH, full_review_population="curated",
                  min_trades=50):
    """Callable core of this script -- a future pipeline caller (e.g. auto-run after
    Phase5, noted as a planned-but-not-built follow-up in the 2026-09-01 v2 dispatch) can
    import and call this directly instead of shelling out to main(). Returns
    (full_review_rows, curated_rows, raw_rows) -- the same three row-sets _write_report_
    xlsx consumes, so a caller that wants the data without an xlsx can use this alone.

    full_review_population (v3, 2026-09-01 overnight dispatch): 'curated' (default,
    unchanged v1/v2 behavior) scopes Tab 1 to the curated top-N set. 'core_safe' scopes
    Tab 1 to the full real core_safe=True population instead (see
    _core_safe_population_rows) -- Tab 2 (Candidates) and Tab 3 (All Candidates raw) are
    UNCHANGED either way, this only widens Tab 1's population."""
    ensure_candidate_nodes_table(conn)
    all_tickers = tickers or _tickers_for_version(conn, version)
    if not all_tickers:
        raise SystemExit(f"No candidate_nodes rows for version={version!r}")
    print(f"Tickers ({len(all_tickers)}): {all_tickers}")

    print(f"\n--- Building Tab 2: Candidates (curated top-{top_n}-per-category) ---")
    cand_rows_raw = cri.fetch_rows(conn, version)
    if not cand_rows_raw:
        raise SystemExit(f"No verified candidate_nodes rows for version={version!r} "
                          f"(candidate_verification_results join found nothing)")
    curated_rows, safety_known = cri.curate(cand_rows_raw, top_n=top_n)
    if not safety_known:
        print("NOTE: worst_neighbor_cagr not populated for this version -- "
              "cliff-safety filter was skipped on the Candidates tab, not applied silently.")
    if tickers:
        wanted = set(all_tickers)
        curated_rows = [r for r in curated_rows if r["ticker"] in wanted]
    print(f"Curated: {len(curated_rows)} rows across {len(set(r['ticker'] for r in curated_rows))} tickers")

    if full_review_population == "core_safe":
        print(f"\n--- Building Tab 1: Full Review (scoped to the real core_safe=True "
              f"population, trades>={min_trades} -- see build_report's full_review_population "
              f"docstring) ---")
        tab1_population = _core_safe_population_rows(conn, version, min_trades=min_trades)
        if tickers:
            wanted = set(all_tickers)
            tab1_population = [r for r in tab1_population if r["ticker"] in wanted]
        print(f"core_safe population: {len(tab1_population)} rows")
    else:
        print("\n--- Building Tab 1: Full Review (scoped to Tab 2's curated node_ids -- see "
              "module docstring for why) ---")
        tab1_population = curated_rows
    full_review_rows = _full_review_rows_for_curated(tab1_population, version, vol_gate, db_path,
                                                       max_workers=workers)

    print("\n--- Building Tab 3: All Candidates (raw) ---")
    raw_rows = _raw_population_rows(conn, version)
    if tickers:
        wanted = set(all_tickers)
        raw_rows = [r for r in raw_rows if r["ticker"] in wanted]
    print(f"Raw population: {len(raw_rows)} rows")

    return full_review_rows, curated_rows, raw_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True,
                     help="real candidate_nodes.version string for the campaign (e.g. "
                          "'v6.5-bench-inmemory-...').")
    ap.add_argument("--tickers", default=None,
                     help="comma-separated ticker list. Default: every ticker with a "
                          "candidate_nodes row for --version.")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--vol-gate", type=float, default=DEFAULT_VOL_GATE)
    ap.add_argument("--top-n", type=int, default=5,
                     help="top-N per category (Core/Add On/Drought) for the curated set "
                          "(Tab 2 and Tab 1's scope both use this). Default 5.")
    ap.add_argument("--workers", type=int, default=1,
                     help="ProcessPoolExecutor pool size for Tab 1's checklist compute "
                          "(throttled via campaign_registry.get_workers_budget(--version)). "
                          "Default 1 (serial). Caller's responsibility to confirm no other "
                          "campaign is actively consuming CPU budget before raising this.")
    ap.add_argument("--full-review-population", choices=["curated", "core_safe"], default="curated",
                     help="'curated' (default): Tab 1 scoped to Tab 2's curated top-N set. "
                          "'core_safe': Tab 1 scoped to the full real core_safe=True population "
                          "instead (phase4_results.core_safe=1 AND trades>=--min-trades) -- "
                          "Tab 2/Tab 3 unchanged either way, this only widens Tab 1.")
    ap.add_argument("--min-trades", type=int, default=50,
                     help="trades floor for --full-review-population core_safe. Default 50.")
    ap.add_argument("--xlsx", default=None,
                     help="output/<name>.xlsx. Default: output/candidate_full_review_<version-slug>.xlsx")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    tickers = args.tickers.split(",") if args.tickers else None
    full_review_rows, curated_rows, raw_rows = build_report(
        conn, args.version, tickers=tickers, top_n=args.top_n, vol_gate=args.vol_gate,
        workers=args.workers, db_path=args.db, full_review_population=args.full_review_population,
        min_trades=args.min_trades)

    xlsx_name = args.xlsx or f"candidate_full_review_{args.version[:60]}"
    out_path = Path("output") / (xlsx_name if xlsx_name.endswith(".xlsx") else f"{xlsx_name}.xlsx")
    out_path.parent.mkdir(exist_ok=True)
    resolved_tickers = tickers or _tickers_for_version(conn, args.version)
    _write_report_xlsx(out_path, full_review_rows, curated_rows, raw_rows, conn, args.version, resolved_tickers)
    print(f"\nWrote {out_path} (Full Review: {len(full_review_rows)} rows, "
          f"Candidates: {len(curated_rows)} rows, All Candidates (raw): {len(raw_rows)} rows)")
    conn.close()


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
