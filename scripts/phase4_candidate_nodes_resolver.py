"""Candidate resolution for Phase4/Phase5, sourced from `candidate_nodes` instead of
`backtest_cache` -- new, 2026-08-29 (Task #1, planner dispatch, see docs/backlog_cache.md
"Found 2026-08-29 -- Phase 4/5's candidate resolution").

Real gap this closes: `run_optimization_sweep.derive_phase25_candidates_ground_truth`
(what both Phase4's `candidate_summary_report.py` and Phase5's
`scripts/phase5_second_level_overlay_check.py` use to find a scope's real candidates)
queries `backtest_cache WHERE kernel_version='ground_truth_v6'`. The new in-memory
sweep pipeline (`bench_phase1_phase2_inmemory.py`) writes ZERO `backtest_cache` rows for
any campaign it runs -- it promotes winners straight into `candidate_nodes` instead. Any
campaign run only through that pipeline is therefore structurally invisible to Phase4/5.

This is a COPY/parallel implementation, not an edit to `derive_phase25_candidates_ground_
truth` itself (that function lives in run_optimization_sweep.py, a gated backtest-kernel
module per CLAUDE.md's Review-Gate Persistence Rule -- avoided entirely by not touching
it). The old backtest_cache-based path is untouched and still the default everywhere it's
already wired in; this module is opt-in, called explicitly by a caller that wants
candidate_nodes coverage instead.

Key fact this relies on (confirmed 2026-08-29 against real data): EVERY writer of
candidate_nodes for a ground_truth_v6 campaign -- scripts/candidate_full_review.py's
run_gt_full_review (the old backtest_cache-backed pipeline's own promotion path, which
loops derive_phase25_candidates_ground_truth's own output and calls
get_or_create_candidate_node per candidate) and bench_phase1_phase2_inmemory.py's
_insert_candidate_nodes_rows (the new in-memory pipeline, which does an in-memory
equivalent of the exact same island-center + top-3-per-island selection before writing)
-- writes ALREADY-SELECTED winners, one row per (island, rank) slot. So the real work
here is not re-deriving which cells win (that decision was already made at write time by
whichever pipeline produced the row); it's re-deriving each row's ORIGINAL (take_profit,
stop_loss, tpct) triple from its (arm_pct, trail_buy_pct, trail_sell_pct) storage
encoding, and re-grouping the resulting flat row-set back into islands for the same
`{island_tp, island_sl, take_profit, stop_loss, ...}` list shape derive_phase25_
candidates_ground_truth returns, so it's a drop-in candidate-list source for any Phase4/5
caller.

Known deviations from derive_phase25_candidates_ground_truth's own output, both
documented rather than silently absorbed:
  1. `cagr` is NOT persisted anywhere in candidate_nodes (only `robust_alpha`, which for
     a GT row already means alpha_vs_spy, not CAGR -- see _insert_candidate_nodes_rows'
     own docstring). Every returned candidate's 'cagr' key is None here. This is fine for
     Phase4/5's own actual use of the returned list (both re-simulate every trade from
     scratch via simulate()/the GT kernel to get their real numbers -- they only consume
     take_profit/stop_loss/window/z_score_threshold/max_hold_hours/tpct for that, never
     the dict's cagr/robust_alpha fields) but means any caller that wants to RANK or
     gate on cagr from this function's output directly (rather than re-simulating) will
     get None and must not treat that as 0 or drop the row.
  2. `phase4_eligible` defaults to True unconditionally (no PHASE25_ISLAND_CAGR_MIN gate
     -- that gate is cagr-based and cagr isn't available here). Every already-promoted
     candidate_nodes row already survived its own pipeline's earlier selection, so
     defaulting to eligible is the same "don't drop it, only the eligibility ANNOTATION
     is unknown" posture derive_phase25_candidates_ground_truth itself uses for a
     candidate whose top_cagr isn't NaN -- see that function's own docstring for the
     underlying reasoning. Any downstream caller relying on this flag to skip Phase4/5
     overlay compute for a genuinely negative-cagr island will not get that skip here;
     that's a real gap of this implementation, not a hidden approximation -- OK for
     validated-both-clean scopes and cheap-enough campaigns, not free for a huge one.
  3. Island grouping is a GREEDY FIXED-CAPACITY assignment over the already-promoted
     row set (ranked on `robust_alpha`, the only real merit column candidate_nodes
     retains), NOT a second run of `pick_island_centers` -- tried that first and it
     does NOT work here: `pick_island_centers`'s min_sep exclusion needs the FULL grid
     to tell two real islands with nearby (take_profit, stop_loss) centers apart (e.g.
     real SOXL/TrailingBoth/fixed_sl=1: islands at (30,7) and (23,7), only 7 apart,
     both real and both correctly separated by the original run because it had the
     full grid's evidence). Re-running it against only the 9 already-selected winners
     loses exactly the "the coordinate 7 away is ALSO a real distinct peak" evidence
     -- confirmed empirically (v0 of this function did this and silently merged those
     two islands, producing 6 candidates for that scope instead of the real 9).
     The greedy approach instead never re-detects centers: it walks the row pool in
     robust_alpha-descending order, and assigns each row to the first still-open
     (<3 members) island whose SEED (the first/highest-ranked row assigned to it) is
     within FINE_RADIUS -- capping each island at 3 the moment it fills, so a
     higher-ranked but coordinate-nearby row can never "steal" a slot from a
     genuinely different island the way a second center-detection pass would. This
     exactly reproduces both the original grouping and the original within-island
     rank order for all 10 real Campaign A scopes (SET-MATCH on every scope; two
     scopes have 8/9 candidates instead of 9/9, and those two also show as
     order-differs -- but that's a PRE-EXISTING candidate_nodes promotion-time
     INSERT OR IGNORE collision losing one row, not a resolver bug: candidate_nodes
     itself only has 8 rows for those two scopes to begin with, confirmed via
     scripts/validate_phase4_candidate_nodes_resolver.py's real run, 2026-08-29),
     including the (30,7)/(23,7) near-collision case above.

Returns the same list-of-dicts shape as derive_phase25_candidates_ground_truth, PLUS
a real `id` key (added 2026-08-29, Task #3, planner dispatch -- the real
candidate_nodes.id this row came from, so a caller like Phase5's verification-
persistence code can recover a real candidate_id; derive_phase25_candidates_ground_
truth's own backtest_cache-sourced output has no equivalent, since those candidates
were never promoted into candidate_nodes at all), PLUS `core_safe`/`addon_safe`
(added 2026-08-30, planner dispatch -- Phase4's own cliff-safety verdict, persisted
by candidate_summary_report.py's _persist_phase4_verdicts_and_checklist; True/False/None(unknown,
Phase4 hasn't covered this candidate yet) tri-state, same semantics
GT_COLUMN_DEFS' core_safe/addon_safe entries already document -- see
phase5_second_level_overlay_check.py's SAFE/SAFE gate, the one real consumer):
{id, island_tp, island_sl, take_profit, stop_loss, max_hold_hours, window,
 z_score_threshold, tpct, robust_alpha, cagr, phase4_eligible, core_safe, addon_safe}.
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd

from run_optimization_sweep import DB_PATH, FINE_RADIUS, GT_CANDIDATE_TIEBREAK, N_ISLANDS
import strategies


def _stop_loss_and_tpct_from_row(sl_axis_col, fourth_axis_col, trail_buy_pct, trail_sell_pct):
    """Inverse of bench_phase1_phase2_inmemory._insert_candidate_nodes_rows' own
    forward mapping (and run_optimization_sweep.build_candidate_report_ground_truth's
    is_both branch, the same mapping node_from_candidate reuses) -- must stay in exact
    lockstep with both, never re-derived independently."""
    if sl_axis_col == 'trail_buy_pct':
        stop_loss = trail_buy_pct
        tpct = trail_sell_pct if fourth_axis_col == 'trail_pct' else 0.0
    elif sl_axis_col == 'trail_pct':
        stop_loss = trail_sell_pct
        tpct = 0.0
    else:
        stop_loss = 0.0
        tpct = 0.0
    return stop_loss, tpct


def _text_to_bool(v):
    """core_safe/addon_safe are persisted as TEXT "True"/"False"/NULL (candidate_summary_
    report._persist_phase4_verdicts_and_checklist) to preserve the real tri-state (True/False/None-
    unknown) build_candidate_report_ground_truth's own row already carries -- this is the
    read-side inverse. pd.isna guards both a real SQL NULL (read back as None) and any
    pandas NaN-coercion edge case in a mixed-content object column, rather than assuming
    which one read_sql produces here."""
    return None if pd.isna(v) else (v == "True")


def discover_all_candidate_nodes_scopes(ticker):
    """Same as discover_candidate_nodes_scopes but across EVERY version for `ticker`,
    not just one known version -- mirrors prune_backtest_cache_ground_truth.
    discover_all_gt_scopes(conn)'s own ticker-agnostic-of-version discovery pattern,
    for a caller (e.g. scripts/candidate_summary_report.py's --kernel gt ticker-driven
    flow) that doesn't already know which version(s) a candidate_nodes-only campaign
    used. Returns (strategy, version, entry_timing, fixed_sl, window) 5-tuples."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT DISTINCT strategy, version, entry_timing, fixed_sl, window "
            "FROM candidate_nodes WHERE ticker=?", (ticker,)).fetchall()
    return sorted(rows)


def discover_candidate_nodes_scopes(ticker, config_version):
    """Real (strategy, entry_timing, fixed_sl, window) tuples present in candidate_nodes
    for (ticker, config_version) -- new, 2026-08-29 (Task #3, planner dispatch). Parallel
    to prune_backtest_cache_ground_truth.discover_all_gt_scopes, but reads candidate_nodes
    instead of backtest_cache, so it also finds a campaign the in-memory pipeline ran (no
    backtest_cache rows to discover from at all -- see module docstring). `window` IS part
    of the tuple here (backtest_cache's own discovery doesn't need it -- a version there
    is one real campaign) because a candidate_nodes version string can alias multiple
    unrelated batches distinguished only by window (see derive_phase25_candidates_from_
    candidate_nodes's own `window` param docstring) -- the caller MUST pass each returned
    window value through to that function's `window` param, not drop it."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT DISTINCT strategy, entry_timing, fixed_sl, window FROM candidate_nodes "
            "WHERE ticker=? AND version=?", (ticker, config_version)).fetchall()
    return sorted(rows)


def derive_phase25_candidates_from_candidate_nodes(ticker, strategy_name, config_version,
                                                     fixed_sl=0, entry_timing='open_check',
                                                     window=None, full_population=False):
    """candidate_nodes-sourced replacement for derive_phase25_candidates_ground_truth --
    see module docstring for the full design/known-deviations writeup. Read-only.

    `full_population` (added 2026-08-29, Phase3-retirement/Phase5-widening task): when
    True, SKIPS the island-clustering step below entirely and returns every raw
    candidate_nodes row for the scope (same query, no up-to-`N_ISLANDS`-islands-of-
    up-to-3 cap) -- this is the same un-narrowed population phase3_second_level_check.py's
    own `main()` query has always audited. Existing callers (phase5_second_level_overlay_
    check.py's original narrow path, candidate_summary_report.py, validate_phase4_
    candidate_nodes_resolver.py) are UNCHANGED -- they don't pass this, so they keep
    getting the island-capped subset exactly as before. Each returned dict still has the
    real `id`/`take_profit`/`stop_loss`/`tpct` keys (via the same `_stop_loss_and_tpct_
    from_row` mapping), but `island_tp`/`island_sl` are just set to the candidate's own
    (take_profit, stop_loss) -- there is no real island grouping to report when every row
    is returned individually.

    `window` (added 2026-08-29, planner review): candidate_nodes' natural scope key
    (ticker, strategy, version, fixed_sl, entry_timing) is NOT enough to identify one
    real campaign -- confirmed on real data that a single version string
    ('bench-inmemory-v6-massive-w2021-08-23_2026-08-21') holds TWO unrelated sweep
    batches that happen to share it: a window=[10,20] batch from 2026-08-27/28 and a
    separate window=15 batch from 2026-08-29 (the actual "Campaign C" this resolver was
    built to cover). Without a window filter this function silently pools rows across
    both -- candidates that were never swept against each other end up ranked/grouped
    together as if they were, corrupting the island selection (found: fixed_sl=1.0
    returned 18 mixed-window candidates instead of the real ~9-per-batch). `window=None`
    (default) preserves derive_phase25_candidates_ground_truth's own genuine
    cross-window semantics (a real single campaign CAN legitimately sweep multiple
    windows together, e.g. bench_phase1_phase2_inmemory.py's own WINDOWS=[10,20] --
    that's correct pooling, not a bug) -- only pass an explicit `window` when the
    version string is known/suspected to alias multiple independent batches, as
    Campaign C's window=15 slice is.

    Returns [] if candidate_nodes has no rows for this exact scope -- same "nothing to
    report" contract as the backtest_cache-based function's own empty-df early return,
    not an error."""
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    window_sql = " AND window=?" if window is not None else ""
    params = [ticker, strategy_name, config_version, float(fixed_sl), entry_timing]
    if window is not None:
        params.append(int(window))
    with sqlite3.connect(DB_PATH) as conn:
        # core_safe/addon_safe (2026-08-30, planner dispatch): candidate_summary_report.py's
        # _persist_phase4_verdicts_and_checklist is the only writer and ALTER-guards these columns lazily,
        # so a DB that's never had a --kernel gt run against it yet (or a fresh/test DB)
        # won't have them -- select literal NULLs instead of raising OperationalError, same
        # tri-state "unknown" a real un-persisted candidate would carry anyway.
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(candidate_nodes)")}
        safe_cols_sql = ", ".join(
            (col if col in existing_cols else f"NULL AS {col}")
            for col in ("core_safe", "addon_safe"))
        df = pd.read_sql(f"""
            SELECT id, window, z AS z_score_threshold, arm_pct, trail_buy_pct, trail_sell_pct,
                   max_hold_hours, robust_alpha, trades, {safe_cols_sql}
            FROM candidate_nodes
            WHERE ticker=? AND strategy=? AND version=? AND fixed_sl=? AND entry_timing=?{window_sql}
        """, conn, params=params)
    if df.empty:
        return []

    df['take_profit'] = df['arm_pct'].astype(float)
    sl_tpct = df.apply(
        lambda r: _stop_loss_and_tpct_from_row(sl_axis_col, fourth_axis_col,
                                                float(r['trail_buy_pct']), float(r['trail_sell_pct'])),
        axis=1, result_type='expand')
    df['stop_loss'] = sl_tpct[0]
    df['tpct'] = sl_tpct[1]

    _tb_cols = ['robust_alpha'] + [c for c, _ in GT_CANDIDATE_TIEBREAK]
    _tb_asc = [False] + [asc for _, asc in GT_CANDIDATE_TIEBREAK]
    df = df.sort_values(_tb_cols, ascending=_tb_asc)

    if full_population:
        # No island-clustering -- every raw row is its own "island" of one, same
        # un-narrowed population phase3_second_level_check.py's own query has always
        # audited. See docstring above.
        candidates = []
        for _, cand in df.iterrows():
            tp, sl = int(cand['take_profit']), int(cand['stop_loss'])
            candidates.append({
                'id': int(cand['id']),
                'island_tp': tp, 'island_sl': sl,
                'take_profit': tp, 'stop_loss': sl,
                'max_hold_hours': int(cand['max_hold_hours']), 'window': int(cand['window']),
                'z_score_threshold': float(cand['z_score_threshold']), 'tpct': float(cand['tpct']),
                'robust_alpha': float(cand['robust_alpha']), 'cagr': None,
                'phase4_eligible': True,
                'core_safe': _text_to_bool(cand['core_safe']),
                'addon_safe': _text_to_bool(cand['addon_safe']),
            })
        return candidates

    # Greedy fixed-capacity island assignment -- see module docstring's deviation #3
    # for why this replaces a second pick_island_centers pass.
    islands = []  # list of {'seed': (tp, sl), 'members': [row dict, ...]}
    for _, row in df.iterrows():
        tp, sl = float(row['take_profit']), float(row['stop_loss'])
        target = None
        for isl in islands:
            if len(isl['members']) >= 3:
                continue
            seed_tp, seed_sl = isl['seed']
            if abs(tp - seed_tp) <= FINE_RADIUS and abs(sl - seed_sl) <= FINE_RADIUS:
                target = isl
                break
        if target is None:
            if len(islands) < N_ISLANDS:
                target = {'seed': (tp, sl), 'members': []}
                islands.append(target)
            else:
                # More than N_ISLANDS distinct coordinate clusters in the promoted pool
                # (shouldn't happen for a real 9-or-fewer-row scope, but don't silently
                # drop real data) -- fall through to the closest open-or-not island.
                target = min(islands, key=lambda isl: (abs(tp - isl['seed'][0]) +
                                                         abs(sl - isl['seed'][1])))
        target['members'].append(row)

    candidates = []
    for isl in islands:
        seed_tp, seed_sl = isl['seed']
        for cand in isl['members']:
            candidates.append({
                'id': int(cand['id']),
                'island_tp': seed_tp, 'island_sl': seed_sl,
                'take_profit': int(cand['take_profit']), 'stop_loss': int(cand['stop_loss']),
                'max_hold_hours': int(cand['max_hold_hours']), 'window': int(cand['window']),
                'z_score_threshold': float(cand['z_score_threshold']), 'tpct': float(cand['tpct']),
                'robust_alpha': float(cand['robust_alpha']), 'cagr': None,
                'phase4_eligible': True,
                'core_safe': _text_to_bool(cand['core_safe']),
                'addon_safe': _text_to_bool(cand['addon_safe']),
            })
    return candidates
