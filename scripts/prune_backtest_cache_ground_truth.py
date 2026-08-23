"""GT-kernel (v6) counterpart to prune_backtest_cache.py -- island-only retention for
`kernel_version='ground_truth_v6'` rows, but deliberately NOT a single-winner-per-group
prune. run_phase25_cliff_box_ground_truth (run_optimization_sweep.py) keeps up to
N_ISLANDS islands * 3 candidates (9 total) per real campaign scope, specifically so a
downstream overlay decision (add-on/drought, see scripts/gt_addon_winner_drought_eval.py
and run_optimization_sweep.run_addon_cliff_safety_ground_truth) can pick a different
winner than raw core-alpha would. A naive port of prune_backtest_cache.py's single-winner
query would silently collapse this back to one node per group -- destroying exactly the
information the 2026-08-22 multi-peak GT work was built to preserve. User-confirmed
requirements (2026-08-22): (1) preserve the FULL multi-candidate structure -- all up to 9
candidates' own cliff-boxes per scope, not just one; (2) the add-on/drought overlay
computation isn't persisted anywhere (verified directly -- run_addon_cliff_safety_ground_
truth and gt_addon_winner_drought_eval.py both compute trades fresh, in-process, via
run_backtest_ground_truth off raw price data every time; see run_optimization_sweep.py's
own comment above run_addon_cliff_safety_ground_truth: "does NOT dispatch anything ...
no backtest_cache writes"), so "the add-on/drought data survives pruning" reduces exactly
to "the core candidate rows it's derived from survive pruning" -- there's no separate
overlay table to protect.

Candidate/island selection is NOT reinvented here -- every real campaign scope's up-to-9
candidates come from run_optimization_sweep.derive_phase25_candidates_ground_truth
itself (the single source of truth for "what counts as a real candidate", already used by
the real Phase2.5-GT dispatch and the add-on-safety pass).

IMPORTANT, found by the 2026-08-22 4-way paired review (Opus independent-cold + Opus
contextual + Sonnet + Fable, all four independently converged on this as the top finding):
the FIRST version of this module treated "derive_phase25_candidates_ground_truth raised /
returned no candidates for this scope" as "nothing to keep -- drop 100% of this scope's
rows." That is correct ONLY for a genuinely-throwaway smoke-test scope (a *-Neighborhood
parity check with a handful of rows). Measured live against the real DB the same day: TWO
real, expensive, complete-Phase1 campaigns (KORU/TrailingBoth 5yr-Massive, 164,640 Phase1
rows; SOXL/TrailingExit, 11,760 Phase1 rows) were mid-Phase2 and would have had **176,400
real rows deleted outright**, with the validator reporting a clean pass (it only ever
cross-checks scopes that DO produce candidates, so a scope going from "N rows" to "0" is
invisible to every check). Fixed by treating "no candidates" as "not yet eligible for
island-only pruning" rather than "delete": compute_keep_manifest now emits a
'passthrough_scope' manifest entry (ALL of that scope's rows, verbatim) for every real GT
scope that isn't prunable -- whether because its own Phase1/Phase2 pipeline is genuinely
incomplete, its strategy has no known campaign_config.STRATEGIES grid, or its hp
reconstruction has drifted from what was actually swept. This also fixes the previously
separate "unknown-strategy GT rows silently deleted" gap the same review found -- both are
the same underlying bug shape (a lookup miss treated as "eligible to delete" instead of
"not eligible to prune yet"), so one fix covers both.

This module only adds, on top of derive_phase25_candidates_ground_truth: (a) discovering
real GT scopes and classifying each as prunable (real candidates found) or passthrough
(kept whole), (b) for prunable scopes, turning each candidate into a concrete rowid
keep-set via the SAME cliff-box shape run_phase25_cliff_box_ground_truth actually
dispatches (+/-CLIFF_RADIUS on tp/sl, +/-7h hold-time-cap neighbors, +/-1 trail_pct
neighbor -- constants/helpers imported, not re-hardcoded, so this can't silently drift
from the real dispatch the way the legacy tool's own CLIFF_RADIUS once did, see its own
header comment), (c) preserving each island's "center anchor" row too -- see
center_anchor_rowid's docstring for why a candidate's own cliff-box isn't sufficient on
its own to make island-center detection reproducible post-prune, and island_centers_for_
scope's docstring for why anchors must come from the FULL center list `pick_island_
centers` produces, not just the subset that happened to also clear the CAGR/non-empty-
region gate and produce candidates (a second real gap the same review found: an island
that lost the CAGR gate still occupies a min-separation "slot" among centers, and losing
its anchor row can shift which OTHER centers survive post-prune).

Note on production re-derivation after a swap (documented limitation, not a bug -- same
property the legacy prune_backtest_cache.py already has): once a prunable scope's rows
are reduced to its islands' cliff-boxes + anchors, re-running
derive_phase25_candidates_ground_truth for real (its own, un-patched completeness gates)
against the pruned file will correctly raise -- a deliberately-shrunk archive is no longer
a complete grid, and it shouldn't pretend to be. This module's own validator proves
reproducibility by temporarily bypassing those two gate calls (see
prune_backtest_cache_ground_truth_validate.py's _call_method_a_bypassing_completeness_
gates) -- that bypass is a validation-only technique with no production equivalent. The
intended workflow is: finish candidate derivation and any add-on/drought overlay decision
BEFORE pruning a scope (the full grid still exists then), and treat the pruned archive as
a permanent audit/cliff-safety reference afterward, not a live substrate to re-run
candidate search against.

Same build/dry-run/swap file-swap shape as prune_backtest_cache.py (fresh file + rename,
not DELETE+VACUUM in place) -- see that module's docstring for the full rationale
(167M-row NOT-IN scan / 65GB VACUUM rewrite, both avoided). DB_PATH/PRUNED_PATH/
VALIDATION_SENTINEL are plain module attributes (not CLI flags) so a validator/test
script can point this module at a scratch copy by reassigning them directly -- but note
candidate derivation itself runs through run_optimization_sweep's OWN `DB_PATH` global
(derive_phase25_candidates_ground_truth connects via `sqlite3.connect(DB_PATH)` inside
that module, not via any connection this module passes in). candidates_for_scope below
handles this by setting `ros.DB_PATH` to this module's own DB_PATH for the duration of
each call (try/finally) -- found by the same review: reassigning only this module's
DB_PATH without that sync silently derives candidates from whatever `ros.DB_PATH` still
pointed at (e.g. the real live DB) while resolving cliff-box rowids against a scratch
file, producing a keep-set with no relationship to either.

Usage:
  .venv/bin/python scripts/prune_backtest_cache_ground_truth.py --dry-run
  .venv/bin/python scripts/prune_backtest_cache_ground_truth.py --build
  .venv/bin/python scripts/prune_backtest_cache_ground_truth.py --swap
"""
import argparse
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

import run_optimization_sweep as ros
import strategies
import campaign_config
from run_ground_truth_phase1 import WINDOWS, Z_THRESHOLDS, HOLD_TIME_CAPS
from run_optimization_sweep import (
    CLIFF_RADIUS, derive_phase25_candidates_ground_truth, _campaign_scope_sql,
    _sl_axis_real_column, _trail_pcts_for_strategy, rebuild_indexes, pick_island_centers,
)

DB_PATH = Path("cache/research/trading_universe.db")
PRUNED_PATH = Path("cache/research/trading_universe_gt_pruned.db")
VALIDATION_SENTINEL = Path("cache/research/.prune_gt_validated")

KERNEL_VERSION = 'ground_truth_v6'


def _hp_for_strategy(strategy_name):
    """Full campaign hp dict for `strategy_name`, built from the SAME two sources every
    real GT sweep script uses (campaign_config.STRATEGIES for take_profits/stop_losses/
    trail_pcts, run_ground_truth_phase1's WINDOWS/Z_THRESHOLDS/HOLD_TIME_CAPS for the
    strategy-agnostic axes) -- not re-hardcoded here, so this can't drift from what a real
    Phase1-Coarse-GT run actually swept for this strategy."""
    grid = campaign_config.STRATEGIES[strategy_name]
    return {
        "windows": WINDOWS,
        "z_score_thresholds": Z_THRESHOLDS,
        "take_profits": grid["take_profits"],
        "stop_losses": grid["stop_losses"],
        "hold_time_caps": HOLD_TIME_CAPS,
        "trail_pcts": grid["trail_pcts"],
    }


def discover_all_gt_scopes(conn):
    """EVERY real (ticker, strategy, version, entry_timing, fixed_sl) scope present in
    this DB's ground_truth_v6 rows -- including a strategy with no known campaign_config
    grid (fixed_sl falls back to 0 for those, matching strategies.uses_fixed_sl's own
    graceful False-for-unknown-class default, which is also what _campaign_scope_sql
    would do). This is the FULL universe of GT scopes; discover_scopes() below is the
    subset with a known hp grid eligible for candidate-based pruning at all. Every scope
    in the full set gets EITHER pruned (if prunable) OR passed through whole -- see
    compute_keep_manifest -- so nothing here is ever silently dropped just for being
    discovered."""
    c = conn.cursor()
    c.execute(f"""SELECT DISTINCT ticker, strategy, version, entry_timing, stop_loss
                  FROM backtest_cache WHERE kernel_version='{KERNEL_VERSION}'""")
    scopes = set()
    for ticker, strategy, version, entry_timing, sl_col_val in c.fetchall():
        fixed_sl = sl_col_val if strategies.uses_fixed_sl(strategy) else 0
        scopes.add((ticker, strategy, version, entry_timing, fixed_sl))
    return sorted(scopes)


def discover_scopes(conn):
    """Real GT scopes whose strategy has a known campaign_config.STRATEGIES hp grid --
    the candidate subset of discover_all_gt_scopes() that candidate-based pruning can even
    attempt. Everything else is handled as a passthrough scope by compute_keep_manifest."""
    return [s for s in discover_all_gt_scopes(conn) if s[1] in campaign_config.STRATEGIES]


def candidates_for_scope(ticker, strategy_name, version, entry_timing, fixed_sl):
    """Real up-to-9 candidates for one scope, via derive_phase25_candidates_ground_truth
    itself (the single source of truth -- see module docstring). Returns [] (with a
    logged reason) for a scope whose Phase1/Phase2-GT pipeline isn't complete yet -- the
    caller (compute_keep_manifest) treats that as "not prunable yet", NOT "delete", see
    module docstring for the real incident this distinction fixes.

    Temporarily points run_optimization_sweep's own DB_PATH at this module's DB_PATH for
    the duration of the call (see module docstring's "production re-derivation" note) --
    derive_phase25_candidates_ground_truth connects via `sqlite3.connect(DB_PATH)` inside
    that module, ignoring any connection this module holds open."""
    hp = _hp_for_strategy(strategy_name)
    _orig_db_path = ros.DB_PATH
    try:
        ros.DB_PATH = DB_PATH
        candidates = derive_phase25_candidates_ground_truth(
            ticker, strategy_name, version, hp, fixed_sl=fixed_sl, entry_timing=entry_timing)
    except RuntimeError as e:
        print(f"[{ticker}/{strategy_name}/{version}/{entry_timing}] scope not prunable yet "
              f"(kept as passthrough, not deleted) -- ({e})")
        return []
    finally:
        ros.DB_PATH = _orig_db_path
    candidates.sort(key=lambda c: (c['island_tp'], c['island_sl'], -c['robust_alpha'],
                                    c['take_profit'], c['stop_loss'], c['max_hold_hours'], c['tpct']))
    return candidates


def island_centers_for_scope(conn, ticker, strategy_name, version, entry_timing, fixed_sl, hp):
    """The FULL up-to-N_ISLANDS center list derive_phase25_candidates_ground_truth's own
    pick_island_centers call would produce for this scope -- deliberately NOT limited to
    centers that went on to clear the CAGR/non-empty-region gate and produce candidates.

    Why this matters (found by the 2026-08-22 paired review, Opus independent-cold): a
    center that pick_island_centers picked but that then failed the CAGR gate (or had an
    empty +/-FINE_RADIUS region) still occupied a real "too close to me, don't pick
    another center here" slot during the GREEDY center-selection scan. If that center's
    own defining row isn't preserved, re-running center-detection post-prune sees a
    shorter candidate list with that slot's suppression effect gone -- which can let a
    DIFFERENT row get promoted into a center slot it was never actually entitled to
    pre-prune, silently shifting the whole rest of the center list. Anchoring every
    picked center (not just candidate-producing ones) closes this.

    Center detection unrestricted (2026-08-23 redesign): matches derive_phase25_
    candidates_ground_truth's own 2026-08-23 fix -- Phase1-grid-only center restriction
    removed there because it made a genuinely-discovered fine-mesh/multi-generation
    island invisible to candidate selection. This function anchors whatever centers
    THAT function actually picks, so it must query the same unrestricted scope or the
    anchor list silently stops matching the real candidate list -- exactly the bug this
    function's own anchoring logic exists to prevent, just one level up."""
    sl_axis_col, _ = strategies.resolve_axis_columns(strategy_name)
    sl_col = _sl_axis_real_column(sl_axis_col)
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    rows = conn.execute(f"""
        SELECT axis_tp, {sl_col}, {ros.ROBUST_ALPHA_SQL}, cagr
        FROM backtest_cache
        WHERE ticker=? AND strategy=? AND version=? AND trades > 0
          AND kernel_version='{KERNEL_VERSION}' {scope_sql}
    """, [ticker, strategy_name, version, *scope_params]).fetchall()
    if not rows:
        return []
    df_centers = pd.DataFrame(rows, columns=['take_profit', 'stop_loss', 'robust_alpha', 'cagr'])
    # rank_col='cagr' (2026-08-23, ground_truth_kernel_rebuild.md Step 4, paired-review
    # CRITICAL finding on the original CAGR-reranking commit): MUST match
    # derive_phase25_candidates_ground_truth's own pick_island_centers call exactly, or
    # this function's whole anchoring purpose (see docstring above) silently breaks --
    # it would anchor the alpha-picked centers while the real candidate list is now
    # cagr-picked, which can delete rows a cagr-picked center actually depends on.
    return pick_island_centers(df_centers, rank_col='cagr')


def cliff_box_rowids_for_candidate(conn, ticker, strategy_name, version, fixed_sl, entry_timing, cand, hp):
    """Rowids of the real cliff-box (+/-CLIFF_RADIUS tp/sl, +/-7h hold, +/-1 trail_pct
    neighbor) already sitting in backtest_cache around one candidate cell -- mirrors
    run_phase25_cliff_box_ground_truth's own box-generation loop exactly (same
    CLIFF_RADIUS import, same 1..30 tp/sl clamp, same +/-7h hold filter off hp's own
    hold_time_caps list, same trail_pct neighbor-index logic), just expressed as a
    rowid-selection query instead of a dispatch-task generator (nothing to dispatch here
    -- these cells are already computed, or they're not, either way there's nothing to
    resweep during a prune)."""
    sl_axis_col, fourth_axis_col = strategies.resolve_axis_columns(strategy_name)
    sl_col = _sl_axis_real_column(sl_axis_col)
    trail_pcts = _trail_pcts_for_strategy(strategy_name, hp)
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)

    tp_c2, sl_c2, hold_c = int(cand['take_profit']), int(cand['stop_loss']), int(cand['max_hold_hours'])
    w_c, z_c, tpct_c = int(cand['window']), float(cand['z_score_threshold']), float(cand['tpct'])

    if fourth_axis_col == 'trail_pct' and tpct_c in trail_pcts:
        idx = trail_pcts.index(tpct_c)
        tpct_neighbors = trail_pcts[max(0, idx - 1): idx + 2]
    else:
        tpct_neighbors = [tpct_c]

    hold_keep = [h for h in hp['hold_time_caps'] if abs(h - hold_c) <= 7]
    tp_range = list(range(max(1, tp_c2 - CLIFF_RADIUS), min(30, tp_c2 + CLIFF_RADIUS) + 1))
    sl_range = list(range(max(1, sl_c2 - CLIFF_RADIUS), min(30, sl_c2 + CLIFF_RADIUS) + 1))

    tp_ph = ','.join('?' * len(tp_range))
    sl_ph = ','.join('?' * len(sl_range))
    hold_ph = ','.join('?' * len(hold_keep))
    tpct_filter, tpct_params = "", []
    if fourth_axis_col == 'trail_pct':
        tpct_ph = ','.join('?' * len(tpct_neighbors))
        tpct_filter = f" AND trail_sell_pct IN ({tpct_ph})"
        tpct_params = [float(v) for v in tpct_neighbors]

    c = conn.cursor()
    c.execute(f"""
        SELECT rowid FROM backtest_cache
        WHERE ticker=? AND strategy=? AND version=? AND window=? AND z_score_threshold=?
          AND kernel_version='{KERNEL_VERSION}' {scope_sql}
          AND axis_tp IN ({tp_ph}) AND {sl_col} IN ({sl_ph})
          AND max_hold_hours IN ({hold_ph}) {tpct_filter}
    """, [ticker, strategy_name, version, w_c, z_c, *scope_params, *tp_range, *sl_range,
          *hold_keep, *tpct_params])
    return [r[0] for r in c.fetchall()]


def center_anchor_rowid(conn, ticker, strategy_name, version, fixed_sl, entry_timing, island_tp, island_sl):
    """The single row achieving MAX(robust_alpha) at exactly (island_tp, island_sl) across
    the full scope -- i.e. the row that actually made pick_island_centers pick this point
    as a center in the first place (derive_phase25_candidates_ground_truth's own
    df_centers query has no window/hold/z/tpct filter, so a center's own defining row can
    be ANY combo, not necessarily one of its island's own top-3 candidates -- confirmed
    live on SOXL: island center (21, 9) is a distinct point from all 3 of its own
    island's candidates (take_profit/stop_loss = 25/10, 24/10, 24/10), which sit up to 4
    units away, outside CLIFF_RADIUS=2 of the center itself).

    No tiebreak needed here (unlike the legacy tool's winner-selection query) -- any row
    tied for the max cagr value at this exact coordinate reproduces the same
    (island_tp, island_sl, cagr) triple pick_island_centers actually used; WHICH
    physical row achieves it doesn't matter for center reproduction, only the value.

    ORDER BY cagr, not robust_alpha (2026-08-23, ground_truth_kernel_rebuild.md Step 4,
    paired-review CRITICAL finding): must match island_centers_for_scope's own
    rank_col='cagr' pick_island_centers call -- same reasoning as that function's own
    comment."""
    sl_axis_col, _ = strategies.resolve_axis_columns(strategy_name)
    sl_col = _sl_axis_real_column(sl_axis_col)
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    c = conn.cursor()
    c.execute(f"""
        SELECT rowid FROM backtest_cache
        WHERE ticker=? AND strategy=? AND version=? AND kernel_version='{KERNEL_VERSION}' {scope_sql}
          AND trades > 0 AND axis_tp=? AND {sl_col}=?
        ORDER BY cagr DESC LIMIT 1
    """, [ticker, strategy_name, version, *scope_params, island_tp, island_sl])
    row = c.fetchone()
    return row[0] if row else None


def rowids_for_scope(conn, ticker, strategy_name, version, entry_timing, fixed_sl):
    """ALL ground_truth_v6 rowids for one real scope -- used for the passthrough-scope
    keep entry (a non-prunable scope's rows are kept whole, never deleted)."""
    scope_sql, scope_params = _campaign_scope_sql(strategy_name, fixed_sl, entry_timing)
    c = conn.cursor()
    c.execute(f"""SELECT rowid FROM backtest_cache
                  WHERE ticker=? AND strategy=? AND version=? AND kernel_version='{KERNEL_VERSION}' {scope_sql}""",
              [ticker, strategy_name, version, *scope_params])
    return [r[0] for r in c.fetchall()]


def compute_keep_manifest(conn):
    """[{kind, ticker, strategy, version, entry_timing, fixed_sl, ..., rowids}, ...] --
    for each real, PRUNABLE campaign scope: one 'candidate' entry per real candidate (up
    to 9), plus one 'center_anchor' entry per island `pick_island_centers` actually picked
    (up to N_ISLANDS, see island_centers_for_scope's docstring for why this must be the
    FULL center list, not just candidate-producing ones). For every OTHER real GT scope
    (unknown strategy, or a known strategy whose campaign isn't complete/prunable yet):
    one 'passthrough_scope' entry carrying ALL of that scope's rows -- kept whole, never
    deleted (see module docstring for the real incident -- 176,400 rows across two
    genuinely-in-progress campaigns -- this fixes).

    The prune's actual keep-set is the union of every entry's rowids; the validator uses
    the per-entry structure directly for fingerprinting."""
    manifest = []
    prunable_keys = set()
    for ticker, strategy_name, version, entry_timing, fixed_sl in discover_scopes(conn):
        hp = _hp_for_strategy(strategy_name)
        candidates = candidates_for_scope(ticker, strategy_name, version, entry_timing, fixed_sl)
        if not candidates:
            continue
        prunable_keys.add((ticker, strategy_name, version, entry_timing, fixed_sl))
        for idx, cand in enumerate(candidates):
            rowids = cliff_box_rowids_for_candidate(
                conn, ticker, strategy_name, version, fixed_sl, entry_timing, cand, hp)
            manifest.append({
                'kind': 'candidate',
                'ticker': ticker, 'strategy': strategy_name, 'version': version,
                'entry_timing': entry_timing, 'fixed_sl': fixed_sl,
                'candidate_idx': idx, 'candidate': cand, 'rowids': rowids,
            })
        centers = island_centers_for_scope(conn, ticker, strategy_name, version, entry_timing, fixed_sl, hp)
        for island_idx, (island_tp, island_sl) in enumerate(centers):
            anchor_rowid = center_anchor_rowid(
                conn, ticker, strategy_name, version, fixed_sl, entry_timing, island_tp, island_sl)
            manifest.append({
                'kind': 'center_anchor',
                'ticker': ticker, 'strategy': strategy_name, 'version': version,
                'entry_timing': entry_timing, 'fixed_sl': fixed_sl,
                'island_idx': island_idx, 'island_tp': island_tp, 'island_sl': island_sl,
                'rowids': [anchor_rowid] if anchor_rowid is not None else [],
            })

    for ticker, strategy_name, version, entry_timing, fixed_sl in discover_all_gt_scopes(conn):
        key = (ticker, strategy_name, version, entry_timing, fixed_sl)
        if key in prunable_keys:
            continue
        rowids = rowids_for_scope(conn, ticker, strategy_name, version, entry_timing, fixed_sl)
        manifest.append({
            'kind': 'passthrough_scope',
            'ticker': ticker, 'strategy': strategy_name, 'version': version,
            'entry_timing': entry_timing, 'fixed_sl': fixed_sl,
            'rowids': rowids,
        })
    return manifest


def compute_keep_rowids(conn):
    keep = set()
    for entry in compute_keep_manifest(conn):
        keep.update(entry['rowids'])
    return keep


def cmd_dry_run():
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    c = conn.cursor()
    c.execute(f"SELECT COUNT(*) FROM backtest_cache WHERE kernel_version='{KERNEL_VERSION}'")
    total_gt = c.fetchone()[0]
    manifest = compute_keep_manifest(conn)
    keep = set()
    for e in manifest:
        keep.update(e['rowids'])
    prunable_scopes = {(e['ticker'], e['strategy'], e['version'], e['entry_timing'], e['fixed_sl'])
                        for e in manifest if e['kind'] in ('candidate', 'center_anchor')}
    passthrough_entries = [e for e in manifest if e['kind'] == 'passthrough_scope']
    n_candidates = sum(1 for e in manifest if e['kind'] == 'candidate')
    n_anchors = sum(1 for e in manifest if e['kind'] == 'center_anchor')
    passthrough_rows = sum(len(e['rowids']) for e in passthrough_entries)
    print(f"Total ground_truth_v6 rows: {total_gt:,}")
    print(f"Prunable campaign scopes (real candidates found): {len(prunable_scopes)}")
    print(f"Passthrough scopes (kept whole, not eligible for island pruning yet): {len(passthrough_entries)}")
    for e in passthrough_entries:
        print(f"  passthrough: {e['ticker']}/{e['strategy']}/{e['version']}/{e['entry_timing']} "
              f"-- {len(e['rowids']):,} rows kept whole")
    print(f"Total candidates (up to 9/prunable scope): {n_candidates}")
    print(f"Total island center anchors (up to 3/prunable scope): {n_anchors}")
    print(f"Rows kept from passthrough scopes: {passthrough_rows:,}")
    print(f"Rows to keep total (prunable islands + passthrough): {len(keep):,} "
          f"({(len(keep) / total_gt * 100) if total_gt else 0:.4f}% of GT rows)")
    print(f"GT rows dropped (only from WITHIN prunable scopes' own non-island cells): "
          f"{total_gt - len(keep):,}")
    print("\n--dry-run only, nothing written. Re-run with --build to write the pruned DB.")
    conn.close()


def cmd_build():
    if PRUNED_PATH.exists():
        PRUNED_PATH.unlink()
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    keep = compute_keep_rowids(conn)
    print(f"Computed {len(keep):,} ground_truth_v6 rows to keep.")

    conn.execute(f"ATTACH DATABASE '{PRUNED_PATH}' AS pruned")
    c = conn.cursor()
    c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    for (create_sql,) in c.fetchall():
        conn.execute(create_sql.replace("CREATE TABLE", "CREATE TABLE pruned.", 1)
                     if create_sql.upper().startswith("CREATE TABLE")
                     else create_sql)

    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = [r[0] for r in c.fetchall()]
    for t in tables:
        if t == 'backtest_cache':
            continue
        print(f"Copying full table: {t}")
        conn.execute(f"INSERT INTO pruned.{t} SELECT * FROM main.{t}")

    print("Copying non-GT backtest_cache rows verbatim (this tool only prunes "
          f"kernel_version='{KERNEL_VERSION}' rows -- everything else, e.g. the legacy "
          "hourly-kernel rows prune_backtest_cache.py already governs, passes through "
          "unpruned so this tool can't silently interact with that separate prune)...")
    conn.execute(f"""INSERT INTO pruned.backtest_cache SELECT * FROM main.backtest_cache
                     WHERE kernel_version IS NULL OR kernel_version <> '{KERNEL_VERSION}'""")

    print("Copying kept ground_truth_v6 rows (prunable-scope islands + whole passthrough scopes)...")
    keep_list = list(keep)
    batch = 5000
    for i in range(0, len(keep_list), batch):
        chunk = keep_list[i:i + batch]
        ph = ','.join('?' * len(chunk))
        conn.execute(f"INSERT INTO pruned.backtest_cache SELECT * FROM main.backtest_cache WHERE rowid IN ({ph})", chunk)
    conn.commit()

    c.execute("SELECT COUNT(*) FROM pruned.backtest_cache")
    n = c.fetchone()[0]
    print(f"Pruned backtest_cache row count: {n:,}")

    # Copy EVERY index (on every table, not just backtest_cache) verbatim before the
    # standard rebuild_indexes() call below -- found by the 2026-08-22 paired review
    # (Opus independent-cold + Fable, both independently): the original version only
    # copied `type='table'` schema objects and then called rebuild_indexes(), which only
    # recreates the standard 5 backtest_cache indexes -- silently dropping any OTHER
    # real index (confirmed live: idx_candidate_overlay_ticker/idx_candidate_overlay_node
    # on candidate_overlay_results), a verbatim recurrence of the exact 2026-08-07 legacy
    # incident this module's own header comment already describes. SQLite's
    # schema-qualified CREATE INDEX puts the schema prefix on the INDEX name, not the
    # table name (`CREATE INDEX schema.idx ON table(...)`) -- same gotcha
    # prune_backtest_cache.py's own comment documents; handled here via regex on the
    # `CREATE [UNIQUE] INDEX <name> ON ...` prefix rather than a naive string replace,
    # since a naive replace of "CREATE INDEX" would insert the schema prefix in the
    # wrong place for a `CREATE UNIQUE INDEX` statement.
    print("Copying all real indexes (every table, not just backtest_cache)...")
    c.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")
    index_pattern = re.compile(r'^(CREATE\s+(?:UNIQUE\s+)?INDEX\s+)(\S+)(.*)$', re.IGNORECASE | re.DOTALL)
    for (index_sql,) in c.fetchall():
        m = index_pattern.match(index_sql.strip())
        if not m:
            print(f"WARNING: could not schema-qualify an index definition, skipping "
                  f"(inspect and copy by hand if needed): {index_sql[:120]!r}")
            continue
        conn.execute(f"{m.group(1)}pruned.{m.group(2)}{m.group(3)}")

    # rebuild_indexes() is run_optimization_sweep's own single source of truth for the
    # standard backtest_cache index set -- reused (not a second hand-rolled list), run
    # AFTER the generic copy above so it's a harmless no-op (CREATE INDEX IF NOT EXISTS)
    # if those 5 indexes were already copied verbatim, and a genuine safety net if any of
    # them didn't exist on the source DB yet. rebuild_indexes() operates on ros.DB_PATH,
    # so point it at the pruned file for this call only, then restore. Close `conn`'s own
    # handle on the pruned file first (via DETACH) -- rebuild_indexes() opens an
    # independent sqlite3.connect() against the same physical file, and holding both open
    # concurrently while a write transaction might still be pending is an avoidable risk.
    conn.commit()
    conn.execute("DETACH DATABASE pruned")
    conn.close()

    print("Rebuilding standard indexes on pruned.backtest_cache (via run_optimization_sweep.rebuild_indexes)...")
    _orig_db_path = ros.DB_PATH
    try:
        ros.DB_PATH = PRUNED_PATH
        rebuild_indexes()
    finally:
        ros.DB_PATH = _orig_db_path

    print(f"\nBuilt {PRUNED_PATH} -- original {DB_PATH} untouched. "
          f"Inspect it, then run --swap to replace the original.")


def pruned_fingerprint():
    st = PRUNED_PATH.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def live_db_fingerprint():
    st = DB_PATH.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def write_validation_sentinel():
    """Called only by the GT validator, only after every check passes -- cmd_swap
    refuses to run without a fresh, matching sentinel (same two-fingerprint binding as
    prune_backtest_cache.py's sentinel: PRUNED_PATH AND DB_PATH, so a sweep writing new
    rows into the live DB during a long validation run can't be silently discarded on
    swap, see that module's own live_db_fingerprint() comment for the full incident)."""
    VALIDATION_SENTINEL.write_text(f"{pruned_fingerprint()}|{live_db_fingerprint()}")


def cmd_swap():
    if not PRUNED_PATH.exists():
        print("No pruned DB found -- run --build first.")
        sys.exit(1)
    if not VALIDATION_SENTINEL.exists():
        print("No validation sentinel found -- run scripts/prune_backtest_cache_ground_truth_validate.py "
              "and confirm it passes before swapping. Refusing to swap an unvalidated build.")
        sys.exit(1)
    recorded = VALIDATION_SENTINEL.read_text().strip()
    recorded_pruned, _, recorded_live = recorded.partition("|")
    if recorded_pruned != pruned_fingerprint():
        print("Validation sentinel is STALE -- the pruned DB has changed (or been rebuilt) "
              "since the last passing validation. Refusing to swap.")
        sys.exit(1)
    if not recorded_live or recorded_live != live_db_fingerprint():
        print("Validation sentinel is STALE -- the LIVE DB has changed since the last "
              "passing validation (e.g. a sweep wrote new rows). Refusing to swap.")
        sys.exit(1)

    # Same WAL-checkpoint-then-rename safety as prune_backtest_cache.py's cmd_swap --
    # see that function's own comment for the full 2026-08-12 corruption incident this
    # guards against (a lingering open connection's -wal frames getting replayed onto
    # the pruned file's different page layout after a rename).
    check_conn = sqlite3.connect(DB_PATH, timeout=60.0)
    busy, log_frames, checkpointed = check_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    check_conn.close()
    if busy or log_frames > 0:
        print(f"Refusing to swap -- WAL checkpoint didn't fully clear "
              f"(busy={busy}, log_frames={log_frames}, checkpointed={checkpointed}). "
              f"Close any other connection to {DB_PATH} and re-run --swap.")
        sys.exit(1)
    for sidecar_suffix in ("-wal", "-shm"):
        p = DB_PATH.with_name(DB_PATH.name + sidecar_suffix)
        if p.exists():
            print(f"Refusing to swap -- {p} still present after a clean WAL checkpoint "
                  f"(unexpected). Investigate before retrying.")
            sys.exit(1)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    moved_aside = DB_PATH.with_name(f"trading_universe.db.pre_gt_prune_{ts}")
    shutil.move(str(DB_PATH), str(moved_aside))
    shutil.move(str(PRUNED_PATH), str(DB_PATH))
    for sidecar_suffix in ("-wal", "-shm"):
        p = PRUNED_PATH.with_name(PRUNED_PATH.name + sidecar_suffix)
        if p.exists():
            print(f"WARNING: unexpected {p} found for the pruned build -- moving "
                  f"alongside {DB_PATH.name} rather than leaving it orphaned.")
            shutil.move(str(p), str(DB_PATH.with_name(DB_PATH.name + sidecar_suffix)))
    VALIDATION_SENTINEL.unlink()
    print(f"Original moved to {moved_aside} (not deleted -- remove manually once confirmed).")
    print(f"{PRUNED_PATH.name} is now {DB_PATH.name}.")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--dry-run', action='store_true')
    g.add_argument('--build', action='store_true')
    g.add_argument('--swap', action='store_true')
    args = ap.parse_args()
    if args.dry_run:
        cmd_dry_run()
    elif args.build:
        cmd_build()
    elif args.swap:
        cmd_swap()


if __name__ == '__main__':
    main()
