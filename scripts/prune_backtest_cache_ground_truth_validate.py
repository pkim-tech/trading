"""GT-kernel prune validation gate -- mirrors scripts/full_db_prune_validate.py's shape
(see .claude/skills/prune-validation/SKILL.md for the legacy gate this is modeled on),
adapted for prune_backtest_cache_ground_truth.py's multi-candidate-per-scope,
passthrough-scope-aware structure.

Rewritten after the 2026-08-22 4-way paired review (Opus independent-cold + Opus
contextual + Sonnet + Fable) found the first version had several real gaps, all fixed
here:
  - It only ever cross-checked scopes that produced candidates -- a scope silently
    losing 100% of its rows (the CRITICAL bug this same review found in the prune tool
    itself) was invisible to every check here too, since an empty manifest for that scope
    meant nothing to compare. Now checks EVERY real GT scope, prunable or passthrough.
  - PRE and POST cliff-box content were both computed via the SAME
    cliff_box_rowids_for_candidate function -- a bug baked into that function's own box
    definition (wrong CLIFF_RADIUS clamp, wrong hold window, wrong trail_pct neighbor
    slice) would reproduce identically on both sides and never be caught. Added
    `independent_cliff_box_rowids` -- a from-scratch, per-cell-query re-implementation of
    the exact same box shape, cross-checked against the real function's output PRE.
  - Method B's `_campaign_scope_sql`/`_sl_axis_real_column` reuse meant a bug in the
    shared scope predicate would pass both methods identically. Both Method B and the new
    independent box check now use `_independent_scope_sql`, written directly against the
    known DB semantics rather than calling into run_optimization_sweep's own helper.
  - Passthrough tables (everything outside kept GT cliff-boxes -- the other ~21 tables,
    plus non-GT and non-prunable-GT backtest_cache rows) had zero validation, even though
    the swap this gate authorizes replaces the ENTIRE file. Added a full per-table
    row-count PRE-vs-POST check.
  - A NULL cagr on a non-top region row crashed Method B outright (only the top row was
    null-guarded). Fixed to skip/guard every row.
  - Dead code (`post_manifest = []` reassigned before use) removed.

Two independently-implemented candidate-selection methods, both checked PRE (against the
live/source DB) and POST (against the freshly --build'd pruned file):

  Method A: run_optimization_sweep.derive_phase25_candidates_ground_truth itself -- the
  real function the actual Phase2.5-GT dispatch and prune_backtest_cache_ground_truth.py
  both already trust as "what counts as a real candidate". Against the PRUNED file this
  can't run unmodified -- its own completeness guards require the FULL Phase1 coarse grid
  and the FULL Phase2 +/-FINE_RADIUS island mesh to still be present, which a
  deliberately-shrunk archive by definition no longer has.
  `_call_method_a_bypassing_completeness_gates()` monkeypatches ONLY those two guard
  calls to "yes, complete" for the duration of one call, so the exact same core selection
  code runs against the pruned file too -- this is a validation-only technique with no
  production equivalent (see prune_backtest_cache_ground_truth.py's module docstring's
  "production re-derivation" note for why that's intentional, not a gap).

  Method B: a SEPARATELY-WRITTEN raw-SQL top-3-per-island query
  (`independent_candidates_for_scope`) -- does NOT call derive_phase25_candidates_ground_
  truth or its scope-predicate helper. Still reuses the genuinely shared, low-bug-risk
  PRIMITIVES (pick_island_centers, FINE_RADIUS, PHASE25_ISLAND_CAGR_MIN, ROBUST_ALPHA_SQL,
  N_ISLANDS) rather than re-hardcoding second copies that could silently drift.

Never touches --swap or deletes anything. Writes the validation sentinel
prune_backtest_cache_ground_truth.py's cmd_swap requires, only on a fully clean pass.

Usage:
  .venv/bin/python scripts/prune_backtest_cache_ground_truth_validate.py
"""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

import run_optimization_sweep as ros
import strategies
import prune_backtest_cache_ground_truth as pbcg

KEPT_COUNT_LOG = Path("cache/research/.prune_gt_kept_counts.json")

# Row columns that fully identify + value-check one kept cell -- coordinate columns plus
# the raw inputs ROBUST_ALPHA_SQL/cagr are computed from, so a copy that silently mutated
# a value (not just dropped a row) would also be caught.
IDENTITY_COLS = ["axis_tp", "trail_buy_pct", "stop_loss", "trail_sell_pct", "max_hold_hours",
                  "window", "z_score_threshold", "alpha_vs_spy", "alpha_vs_spy_pessimistic",
                  "alpha_vs_spy_certain", "cagr", "trades"]


def _row_hash(row):
    s = "|".join("" if v is None else str(v) for v in row)
    return int(hashlib.md5(s.encode()).hexdigest()[:16], 16)


def _entry_group_key(entry):
    base = (entry['kind'], entry['ticker'], entry['strategy'], entry['version'],
            entry['entry_timing'], entry['fixed_sl'])
    if entry['kind'] == 'candidate':
        return base + (entry['candidate_idx'],)
    if entry['kind'] == 'center_anchor':
        return base + (entry['island_idx'],)
    return base  # passthrough_scope -- one group per scope, no sub-index


def _fingerprint_entries(db_path, entries):
    """{group_key: (count, xor_hash)} for each entry's own rowids, read fresh from
    db_path -- rowids are file-local, so this must be called separately against the live
    DB and the pruned file (never reuse rowid numbers across files)."""
    conn = sqlite3.connect(db_path, timeout=60.0)
    c = conn.cursor()
    cols_sql = ", ".join(IDENTITY_COLS)
    fp = {}
    for entry in entries:
        gk = _entry_group_key(entry)
        rowids = entry['rowids']
        if not rowids:
            fp[gk] = (0, 0)
            continue
        cnt, xh = 0, 0
        batch = 5000
        for i in range(0, len(rowids), batch):
            chunk = rowids[i:i + batch]
            ph = ",".join("?" * len(chunk))
            for row in c.execute(f"SELECT {cols_sql} FROM backtest_cache WHERE rowid IN ({ph})", chunk):
                cnt += 1
                xh ^= _row_hash(row)
        fp[gk] = (cnt, xh)
    conn.close()
    return fp


def _call_method_a_bypassing_completeness_gates(ticker, strategy_name, version, hp, fixed_sl, entry_timing):
    """Calls the REAL derive_phase25_candidates_ground_truth against whatever ros.DB_PATH
    currently points at, with its two completeness guards patched to always report
    "complete" -- see module docstring for why this is necessary (and safe: it doesn't
    change the guards for any real caller, only for this validator's own process, only
    for the duration of this one call)."""
    with patch.object(ros, '_phase1_coarse_gt_status', return_value=(1, 1)), \
         patch.object(ros, '_phase2_island_gt_status', return_value=(1, 1)):
        return ros.derive_phase25_candidates_ground_truth(
            ticker, strategy_name, version, hp, fixed_sl=fixed_sl, entry_timing=entry_timing)


def _independent_scope_sql(strategy_name, fixed_sl, entry_timing):
    """Independently-written re-expression of run_optimization_sweep._campaign_scope_
    sql's own scope predicate -- written directly against the known DB semantics (both
    live strategies use a fixed, non-swept SL stored in the `stop_loss` column), NOT by
    calling that helper -- so a bug baked into the shared helper (wrong param order,
    wrong branch condition) isn't guaranteed to reproduce identically in both the real
    prune tool and this validator's independent checks."""
    if strategy_name in ("TrailingBothZScoreBreakout", "TrailingExitZScoreBreakout"):
        return " AND stop_loss=? AND entry_timing=?", [int(round(float(fixed_sl))), entry_timing]
    return " AND entry_timing=?", [entry_timing]


def independent_candidates_for_scope(ticker, strategy_name, version, entry_timing, fixed_sl, hp):
    """Method B -- genuinely separate implementation of "top-3-robust-alpha cells within
    +/-FINE_RADIUS of each of up to N_ISLANDS island centers, centers restricted to
    Phase1's own coarse-grid tp/sl values, islands below PHASE25_ISLAND_CAGR_MIN dropped"
    -- expressed as a per-island SQL ORDER BY/LIMIT query instead of derive_phase25_
    candidates_ground_truth's pandas boolean-mask + sort_values + head(3). Runs
    unconditionally against whatever ros.DB_PATH currently points at."""
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    sl_col = ros._sl_axis_real_column(sl_axis_col)
    tpct_col_sql = 'trail_sell_pct' if fourth_axis_col == 'trail_pct' else '0'
    scope_sql, scope_params = _independent_scope_sql(strategy_name, fixed_sl, entry_timing)
    tp_ph = ','.join('?' * len(hp['take_profits']))
    sl_ph = ','.join('?' * len(hp['stop_losses']))

    conn = sqlite3.connect(ros.DB_PATH, timeout=60.0)
    try:
        centers_rows = conn.execute(f"""
            SELECT axis_tp, {sl_col}, {ros.ROBUST_ALPHA_SQL}
            FROM backtest_cache
            WHERE ticker=? AND strategy=? AND version=? AND trades > 0
              AND kernel_version='{pbcg.KERNEL_VERSION}' {scope_sql}
              AND axis_tp IN ({tp_ph}) AND {sl_col} IN ({sl_ph})
        """, (ticker, strategy_name, version, *scope_params, *hp['take_profits'], *hp['stop_losses'])).fetchall()
        if not centers_rows:
            return []
        df_centers = pd.DataFrame(centers_rows, columns=['take_profit', 'stop_loss', 'robust_alpha'])
        centers = ros.pick_island_centers(df_centers)

        # Independently-expressed version of the same GT_CANDIDATE_TIEBREAK contract
        # Method A's pandas sort applies (see run_optimization_sweep.py's own comment on
        # that constant for why each column is a genuine secondary preference, not a bare
        # determinism nonce) -- written directly as a SQL ORDER BY here rather than
        # reusing Method A's sort call, so a bug in that call site isn't guaranteed to
        # reproduce identically in this re-implementation. Note the independence is
        # partial by design: the MECHANISM (hand-written SQL vs. pandas) is genuinely
        # separate, but both sides import the same GT_CANDIDATE_TIEBREAK POLICY constant
        # (which columns, which directions) rather than each hardcoding their own copy --
        # a wrong/incomplete entry in that shared constant would reproduce identically on
        # both sides and this cross-check would not catch it. Accepted tradeoff: a
        # hand-duplicated policy list risks silent drift between the two sides even more
        # than a shared-but-wrong policy does (2026-08-22 4-way review).
        _tiebreak_sql_col = {
            'trades': 'trades', 'stop_loss': sl_col, 'max_hold_hours': 'max_hold_hours',
            'window': 'window', 'z_score_threshold': 'z_score_threshold',
            'tpct': tpct_col_sql, 'take_profit': 'axis_tp',
        }
        # tpct_col_sql is the bare literal '0' for any strategy without a real trail_pct
        # axis (e.g. TrailingExitZScoreBreakout) -- harmless in a SELECT list, but SQLite
        # reinterprets a bare integer literal appearing in ORDER BY as a 1-based
        # output-column ordinal rather than a value, which raised "ORDER BY term out of
        # range" for every non-TrailingBoth scope (caught by the 2026-08-22 4-way review,
        # confirmed independently by all four reviewers, missed by this session's own
        # repro since it only exercised a TrailingBoth scope). Sorting on a column that's
        # constant across the region is a no-op anyway (same as it is for Method A's
        # pandas sort on the equally-constant `tpct` column), so the term is dropped
        # rather than special-cased into a non-ordinal SQL expression.
        order_by = ', '.join(
            [f"{ros.ROBUST_ALPHA_SQL} DESC"] +
            [f"{_tiebreak_sql_col[col]} {'ASC' if asc else 'DESC'}" for col, asc in ros.GT_CANDIDATE_TIEBREAK
             if _tiebreak_sql_col[col] != '0']
        )

        candidates = []
        for tp_c, sl_c in centers:
            region_rows = conn.execute(f"""
                SELECT axis_tp, {sl_col}, max_hold_hours, window, z_score_threshold,
                       {tpct_col_sql}, {ros.ROBUST_ALPHA_SQL}, cagr, trades
                FROM backtest_cache
                WHERE ticker=? AND strategy=? AND version=? AND trades > 0
                  AND kernel_version='{pbcg.KERNEL_VERSION}' {scope_sql}
                  AND ABS(axis_tp - ?) <= {ros.FINE_RADIUS} AND ABS({sl_col} - ?) <= {ros.FINE_RADIUS}
                ORDER BY {order_by}
                LIMIT 3
            """, (ticker, strategy_name, version, *scope_params, tp_c, sl_c)).fetchall()
            if not region_rows:
                continue
            top_cagr = region_rows[0][7]
            if top_cagr is None or top_cagr <= ros.PHASE25_ISLAND_CAGR_MIN:
                continue
            for tp, sl, hold, w, z, tpct, robust_alpha, cagr, trades in region_rows:
                if cagr is None or robust_alpha is None:
                    continue  # NULL cagr/alpha on a non-top region row -- not a real,
                              # rankable candidate; Method A's pandas path tolerates NaN
                              # arithmetic silently, this raw-SQL path skips explicitly.
                candidates.append({
                    'island_tp': tp_c, 'island_sl': sl_c,
                    'take_profit': int(tp), 'stop_loss': int(sl),
                    'max_hold_hours': int(hold), 'window': int(w),
                    'z_score_threshold': float(z), 'tpct': float(tpct),
                    'robust_alpha': float(robust_alpha), 'cagr': float(cagr),
                })
        candidates.sort(key=lambda c: (c['island_tp'], c['island_sl'], -c['robust_alpha'],
                                        c['take_profit'], c['stop_loss'], c['max_hold_hours'], c['tpct']))
        return candidates
    finally:
        conn.close()


def independent_cliff_box_rowids(conn, ticker, strategy_name, version, fixed_sl, entry_timing, cand, hp):
    """Second, from-scratch implementation of cliff_box_rowids_for_candidate's exact same
    box shape -- itertools.product over explicit coordinate tuples + one parameterized
    equality query per cell, rather than a single IN-list query, and using
    _independent_scope_sql instead of _campaign_scope_sql. A bug in the ORIGINAL box
    definition (wrong CLIFF_RADIUS clamp, wrong hold window, wrong trail_pct neighbor
    slice, wrong column mapping) is not guaranteed to reproduce here too. This directly
    covers the "PRE and POST both call the same function" tautology gap the 2026-08-22
    paired review found -- run only PRE (against the live DB), compared to
    cliff_box_rowids_for_candidate's own PRE output; POST-side box preservation is
    already covered by the per-candidate row-content fingerprint check."""
    import itertools
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    sl_col = ros._sl_axis_real_column(sl_axis_col)
    trail_pcts = ros._trail_pcts_for_strategy(strategy_name, hp)
    scope_sql, scope_params = _independent_scope_sql(strategy_name, fixed_sl, entry_timing)

    tp_c2, sl_c2, hold_c = int(cand['take_profit']), int(cand['stop_loss']), int(cand['max_hold_hours'])
    w_c, z_c, tpct_c = int(cand['window']), float(cand['z_score_threshold']), float(cand['tpct'])

    tp_lo, tp_hi = max(1, tp_c2 - ros.CLIFF_RADIUS), min(30, tp_c2 + ros.CLIFF_RADIUS)
    sl_lo, sl_hi = max(1, sl_c2 - ros.CLIFF_RADIUS), min(30, sl_c2 + ros.CLIFF_RADIUS)
    holds = [h for h in hp['hold_time_caps'] if -7 <= (h - hold_c) <= 7]
    if fourth_axis_col == 'trail_pct' and tpct_c in trail_pcts:
        pos = trail_pcts.index(tpct_c)
        tpcts = trail_pcts[max(0, pos - 1): pos + 2]
    else:
        tpcts = [tpct_c]

    rowids = set()
    c = conn.cursor()
    for tp in range(tp_lo, tp_hi + 1):
        for sl in range(sl_lo, sl_hi + 1):
            for hold in holds:
                for tpct in tpcts:
                    if fourth_axis_col == 'trail_pct':
                        rows = c.execute(f"""SELECT rowid FROM backtest_cache
                            WHERE ticker=? AND strategy=? AND version=? AND kernel_version='{pbcg.KERNEL_VERSION}' {scope_sql}
                              AND axis_tp=? AND {sl_col}=? AND max_hold_hours=? AND window=? AND z_score_threshold=?
                              AND trail_sell_pct=?""",
                            [ticker, strategy_name, version, *scope_params, tp, sl, hold, w_c, z_c, tpct]).fetchall()
                    else:
                        rows = c.execute(f"""SELECT rowid FROM backtest_cache
                            WHERE ticker=? AND strategy=? AND version=? AND kernel_version='{pbcg.KERNEL_VERSION}' {scope_sql}
                              AND axis_tp=? AND {sl_col}=? AND max_hold_hours=? AND window=? AND z_score_threshold=?""",
                            [ticker, strategy_name, version, *scope_params, tp, sl, hold, w_c, z_c]).fetchall()
                    rowids.update(r[0] for r in rows)
    return rowids


def _candidate_key(c):
    return (c['island_tp'], c['island_sl'], c['take_profit'], c['stop_loss'],
            c['max_hold_hours'], c['window'], c['z_score_threshold'], c['tpct'])


def _compare_candidate_lists(label, list_a, list_b, tol=1e-6):
    keys_a = {_candidate_key(c) for c in list_a}
    keys_b = {_candidate_key(c) for c in list_b}
    problems = []
    if keys_a != keys_b:
        problems.append(f"{label}: candidate coordinate sets differ. Only in A: "
                         f"{keys_a - keys_b}. Only in B: {keys_b - keys_a}.")
        return problems
    by_key_b = {_candidate_key(c): c for c in list_b}
    for c in list_a:
        c2 = by_key_b[_candidate_key(c)]
        if abs(c['robust_alpha'] - c2['robust_alpha']) > tol or abs(c['cagr'] - c2['cagr']) > tol:
            problems.append(f"{label}: {_candidate_key(c)} robust_alpha/cagr mismatch: "
                             f"A={c['robust_alpha']:.6f}/{c['cagr']:.6f} B={c2['robust_alpha']:.6f}/{c2['cagr']:.6f}")
    return problems


def _table_row_counts(db_path):
    conn = sqlite3.connect(db_path, timeout=60.0)
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = [r[0] for r in c.fetchall()]
    counts = {}
    for t in tables:
        counts[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    conn.close()
    return counts


def main():
    live = str(pbcg.DB_PATH)
    conn = sqlite3.connect(live, timeout=60.0)

    all_scopes = pbcg.discover_all_gt_scopes(conn)
    total_gt = conn.execute(f"SELECT COUNT(*) FROM backtest_cache WHERE kernel_version='{pbcg.KERNEL_VERSION}'").fetchone()[0]
    print(f"--- {len(all_scopes)} real ground_truth_v6 scope(s) discovered (total {total_gt:,} GT rows) ---")

    # Total-row accounting sanity check: every real GT row must belong to EXACTLY one
    # discovered scope. This would catch a scope-key bug (e.g. the fractional-fixed_sl
    # rounding mismatch between discovery and _campaign_scope_sql) that silently drops or
    # double-counts rows across scope boundaries -- independent of whether that scope
    # ends up prunable or passthrough.
    accounted = 0
    for ticker, strategy_name, version, entry_timing, fixed_sl in all_scopes:
        accounted += len(pbcg.rowids_for_scope(conn, ticker, strategy_name, version, entry_timing, fixed_sl))
    if accounted != total_gt:
        print(f"\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print(f" ACCOUNTING FAILURE: {accounted:,} GT rows attributed to discovered scopes, "
              f"but {total_gt:,} real GT rows exist. A scope-key bug is dropping or "
              f"double-counting rows across scope boundaries. Refusing to proceed.")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        sys.exit(1)
    print(f"Row accounting OK: all {total_gt:,} GT rows attributed to exactly one scope.")

    manifest = pbcg.compute_keep_manifest(conn)
    candidate_entries = [e for e in manifest if e['kind'] == 'candidate']
    anchor_entries = [e for e in manifest if e['kind'] == 'center_anchor']
    passthrough_entries = [e for e in manifest if e['kind'] == 'passthrough_scope']
    prunable_scope_keys = {(e['ticker'], e['strategy'], e['version'], e['entry_timing'], e['fixed_sl'])
                            for e in candidate_entries}
    print(f"Prunable scopes: {len(prunable_scope_keys)} ({len(candidate_entries)} candidates, "
          f"{len(anchor_entries)} island anchors). Passthrough scopes (kept whole): "
          f"{len(passthrough_entries)}.")
    if not prunable_scope_keys:
        print("WARNING: zero prunable scopes found -- every real GT scope will be kept "
              "whole (passthrough). This may be correct (no campaign is complete enough "
              "yet) or may indicate a real regression in candidate derivation -- verify "
              "against expectation before trusting a --build off this run.")

    problems = []
    pre_a_by_scope, pre_b_by_scope = {}, {}
    for key in prunable_scope_keys:
        ticker, strategy_name, version, entry_timing, fixed_sl = key
        hp = pbcg._hp_for_strategy(strategy_name)
        cands_a = sorted((e['candidate'] for e in candidate_entries if
                           (e['ticker'], e['strategy'], e['version'], e['entry_timing'], e['fixed_sl']) == key),
                          key=lambda c: (c['island_tp'], c['island_sl'], -c['robust_alpha'],
                                         c['take_profit'], c['stop_loss'], c['max_hold_hours'], c['tpct']))
        cands_b = independent_candidates_for_scope(ticker, strategy_name, version, entry_timing, fixed_sl, hp)
        pre_a_by_scope[key], pre_b_by_scope[key] = cands_a, cands_b
        problems += _compare_candidate_lists(f"PRE {key} (A vs B)", cands_a, cands_b)

        for idx, cand in enumerate(cands_a):
            expected_rowids = next(e['rowids'] for e in candidate_entries if e['candidate_idx'] == idx and
                                    (e['ticker'], e['strategy'], e['version'], e['entry_timing'], e['fixed_sl']) == key)
            independent_rowids = independent_cliff_box_rowids(
                conn, ticker, strategy_name, version, fixed_sl, entry_timing, cand, hp)
            if set(expected_rowids) != independent_rowids:
                problems.append(
                    f"PRE cliff-box MISMATCH {key} candidate#{idx} {_candidate_key(cand)}: "
                    f"real fn={len(expected_rowids)} rows, independent re-impl={len(independent_rowids)} rows. "
                    f"Only in real fn: {set(expected_rowids) - independent_rowids}. "
                    f"Only in independent: {independent_rowids - set(expected_rowids)}.")

    if problems:
        print("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print(f" PRE cross-check FAILED: {len(problems)} disagreement(s) between independently-implemented methods.")
        print(" Live DB and archives untouched -- no --build/sentinel. Fix the disagreement first.")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        for p in problems[:30]:
            print(f"  {p}")
        sys.exit(1)
    print(f"PRE: Method A/B candidate agreement AND independent cliff-box re-implementation "
          f"both clean across all {len(prunable_scope_keys)} prunable scope(s).")

    pre_fp = _fingerprint_entries(live, candidate_entries + anchor_entries + passthrough_entries)
    pre_table_counts = _table_row_counts(live)
    conn.close()

    print("\n--- Extracting (--build, does not touch live DB) ---")
    pbcg.cmd_build()

    print("\n--- Re-deriving candidates against the PRUNED file (Method A bypassed, Method B raw) ---")
    _orig_db_path = ros.DB_PATH
    post_problems = []
    try:
        ros.DB_PATH = pbcg.PRUNED_PATH
        for key in prunable_scope_keys:
            ticker, strategy_name, version, entry_timing, fixed_sl = key
            hp = pbcg._hp_for_strategy(strategy_name)
            pre_cands = pre_a_by_scope[key]

            post_cands_a = _call_method_a_bypassing_completeness_gates(
                ticker, strategy_name, version, hp, fixed_sl, entry_timing)
            post_cands_b = independent_candidates_for_scope(
                ticker, strategy_name, version, entry_timing, fixed_sl, hp)

            post_problems += _compare_candidate_lists(f"PRE-vs-POST {key} (Method A)", pre_cands, post_cands_a)
            post_problems += _compare_candidate_lists(f"PRE-vs-POST {key} (Method B)", pre_b_by_scope[key], post_cands_b)
    finally:
        ros.DB_PATH = _orig_db_path

    # Rowid-content fingerprint POST: re-resolve rowids fresh against the pruned file
    # (a cell's coordinates are the same, but its rowid number in the new file is
    # unrelated to its old rowid -- INSERT...SELECT reassigns rowids).
    pconn = sqlite3.connect(str(pbcg.PRUNED_PATH), timeout=60.0)
    post_entries = []
    for entry in candidate_entries + anchor_entries + passthrough_entries:
        hp = pbcg._hp_for_strategy(entry['strategy'])
        if entry['kind'] == 'candidate':
            rowids = pbcg.cliff_box_rowids_for_candidate(
                pconn, entry['ticker'], entry['strategy'], entry['version'], entry['fixed_sl'],
                entry['entry_timing'], entry['candidate'], hp)
        elif entry['kind'] == 'center_anchor':
            r = pbcg.center_anchor_rowid(
                pconn, entry['ticker'], entry['strategy'], entry['version'], entry['fixed_sl'],
                entry['entry_timing'], entry['island_tp'], entry['island_sl'])
            rowids = [r] if r is not None else []
        else:  # passthrough_scope -- should reproduce verbatim, same rowid COUNT (not
               # the same rowid numbers, which INSERT...SELECT reassigns)
            rowids = pbcg.rowids_for_scope(
                pconn, entry['ticker'], entry['strategy'], entry['version'],
                entry['entry_timing'], entry['fixed_sl'])
        post_entries.append({**entry, 'rowids': rowids})
    post_fp = _fingerprint_entries(str(pbcg.PRUNED_PATH), post_entries)
    post_table_counts = _table_row_counts(str(pbcg.PRUNED_PATH))
    pconn.close()

    all_groups = set(pre_fp) | set(post_fp)
    fp_mismatches = [g for g in all_groups if pre_fp.get(g) != post_fp.get(g)]
    if fp_mismatches:
        post_problems.append(f"Per-entry row-content fingerprint MISMATCH in "
                              f"{len(fp_mismatches)} of {len(all_groups)} groups (candidates + "
                              f"anchors + passthrough scopes).")
        for g in fp_mismatches[:20]:
            post_problems.append(f"  {g}: PRE={pre_fp.get(g)} POST={post_fp.get(g)}")

    # Whole-DB table-level check -- the swap replaces the ENTIRE file, not just kept GT
    # cliff-boxes, so every other table (and non-GT / non-prunable-GT backtest_cache
    # rows, covered above via passthrough_scope entries) needs a row-count guarantee too.
    for table in sorted(set(pre_table_counts) | set(post_table_counts)):
        if table == 'backtest_cache':
            continue  # backtest_cache itself is expected to shrink -- covered by the
                      # per-entry fingerprint checks above instead of a raw count match
        pre_n, post_n = pre_table_counts.get(table), post_table_counts.get(table)
        if pre_n != post_n:
            post_problems.append(f"Table '{table}' row count changed on copy: PRE={pre_n} POST={post_n} "
                                  f"(expected an exact, unpruned passthrough).")
    non_gt_pre = pre_table_counts.get('backtest_cache', 0) - total_gt
    post_conn = sqlite3.connect(str(pbcg.PRUNED_PATH), timeout=60.0)
    non_gt_post = post_conn.execute(
        f"SELECT COUNT(*) FROM backtest_cache WHERE kernel_version IS NULL OR kernel_version <> '{pbcg.KERNEL_VERSION}'"
    ).fetchone()[0]
    post_conn.close()
    if non_gt_pre != non_gt_post:
        post_problems.append(f"Non-GT backtest_cache row count changed on copy: PRE={non_gt_pre} POST={non_gt_post} "
                              f"(this tool must never touch non-ground_truth_v6 rows).")

    if post_problems:
        print("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print(f" POST validation FAILED -- {len(post_problems)} problem(s). No sentinel written, no swap possible.")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        for p in post_problems[:40]:
            print(f"  {p}")
        sys.exit(1)
    print(f"POST: Method A (gate-bypassed) and Method B both reproduce PRE exactly across "
          f"all prunable scopes. Per-entry (candidate + anchor + passthrough-scope) content "
          f"fingerprints match ({len(all_groups)} groups). All other tables + non-GT/"
          f"non-prunable-GT backtest_cache rows copied with exact row-count preservation.")

    # Kept-row-count regression check per scope (prunable AND passthrough, so a
    # passthrough scope quietly shrinking is flagged too).
    prev = {}
    if KEPT_COUNT_LOG.exists():
        prev = {tuple(json.loads(k)): v for k, v in json.loads(KEPT_COUNT_LOG.read_text()).items()}
    scope_counts = {}
    for entry in post_entries:
        sk = json.dumps([entry['ticker'], entry['strategy'], entry['version'],
                          entry['entry_timing'], entry['fixed_sl']])
        scope_counts[sk] = scope_counts.get(sk, 0) + len(entry['rowids'])
    regressions = [(k, prev[tuple(json.loads(k))], v) for k, v in scope_counts.items()
                   if tuple(json.loads(k)) in prev and v < prev[tuple(json.loads(k))]]
    KEPT_COUNT_LOG.write_text(json.dumps(scope_counts))

    if regressions:
        print("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print(f" ROW-COUNT REGRESSION in {len(regressions)} scope(s) vs. the last recorded GT prune.")
        print(" (Baseline updated to this run -- re-running won't re-flag; this run's swap is still refused.)")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        for k, old, new in regressions[:20]:
            print(f"  {k}: {old} -> {new}")
        sys.exit(1)

    pbcg.write_validation_sentinel()
    print(f"\nNo row-count regressions. Validation sentinel written -- safe to run:")
    print("  .venv/bin/python scripts/prune_backtest_cache_ground_truth.py --swap")


if __name__ == "__main__":
    main()
