"""Phase5 (new, 2026-08-28) -- second-level PRECISION check on Phase4's overlay-
adjusted CAGR, mirroring Phase3's own core-trades-only precision check
(scripts/phase3_second_level_check.py) but for the add-on/drought OVERLAY legs
Phase3 deliberately doesn't touch. Runs AFTER Phase4 (GT overlay report), against
that scope's real finalist candidates.

Real head start reused directly, not reimplemented: backtester.
apply_addon_overlay_ground_truth/simulate_drought_overlay_ground_truth (the
existing, already-validated GT overlay functions) run against BOTH a
1-minute and a 1-second trade list for a single node. This module is an
ORCHESTRATION wrapper: for every real GT Phase4 finalist in a scope (from
run_optimization_sweep.derive_phase25_candidates_ground_truth -- the exact same
function GT Phase4's own report calls, so the finalist set here is identical to
what Phase4 already reported, not re-derived independently), converts the
candidate into the node dict the kernel call expects (same axis-column
mapping run_optimization_sweep.build_candidate_report_ground_truth itself uses:
for TrailingBothZScoreBreakout, arm_pct=cand['take_profit'],
trail_buy_pct=cand['stop_loss'], trail_sell_pct=cand['tpct']; for
TrailingExitZScoreBreakout, arm_pct=cand['take_profit'],
trail_sell_pct=cand['stop_loss'], trail_buy_pct=0.0 -- see
run_optimization_sweep.build_candidate_report_ground_truth's own is_both branch),
runs the overlay CAGR at both granularities, and reports the delta -- same
reporting shape/thresholds as Phase3 (mean/median/worst/best delta across the
finalist population, not just a single hand-picked node).

TRADE-GENERATION SOURCE, changed 2026-08-29 (Task #2, planner dispatch/finding
-- deliberate tradeoff, read before touching this file again): core trades for
both granularities now come DIRECTLY from the real production kernel
(`backtester.run_backtest_ground_truth`, the same `@njit`-compiled function
every other GT phase uses), not from `sim_1s_vs_1m_groundtruth_overlays.simulate()`
(a from-scratch, hand-written independent reimplementation -- see that module's
own docstring for why it was originally built that way: to give genuine
independent evidence when cross-checking the kernel, not another patch on the
same lineage). Confirmed directly (`prep_minute_inputs`/`_simulate_trail_ground_
truth` are fully granularity-agnostic -- bucket whatever rows are in `minute_df`
by hourly bucket, no hardcoded minute-count assumption) that feeding real
1-second data straight into the kernel is not a hack: it reproduced candidate_
nodes id=851's real 123-trade/83.59% CAGR byte-identically in 2.96s, vs
`simulate()`'s ~120-270s/candidate. This makes Phase5 NO LONGER an independent
reimplementation cross-checking the kernel's own correctness -- it is now the
real kernel run twice at different resolutions, so a full core-trade agreement
here no longer says anything about whether the kernel itself is right, only
about the kernel's own sensitivity to bar granularity (which is Phase5's actual
stated purpose -- overlay CAGR sensitivity to data granularity, not kernel-
correctness verification; that's a separate, already-backlogged item: "no
independent-reimplementation verification exists for GT add-on/drought/vol-gate
overlays"). `sim_1s_vs_1m_groundtruth_overlays.py` itself is untouched and still
the real independent-reimplementation reference for anything that needs one.

Data (hourly + 1-second, resampled once to 1-minute) is loaded ONCE per ticker,
matching Phase3's own pattern -- not once per candidate, which would repeat the
"slow step" (loading the multi-GB 1-second file) N times for no reason.

Per-candidate checks run in a ProcessPoolExecutor (this project's existing
sweep-worker-pool convention, run_optimization_sweep.py) -- the loaded
DataFrames are set as module-level globals BEFORE the pool is created, so
Linux's default `fork` start method gives every worker copy-on-write access to
the already-loaded data with no per-worker reload (measured: reloading the full
1-second series alone costs ~456s; naive per-worker reloading would have made
parallelizing net-negative).

Scope decision (2026-08-28 design discussion, not written up elsewhere before
this script -- see this file's own docstring as the record, and docs/design.md's
matching entry): run against every real Phase4 finalist if the measured
per-candidate cost is cheap enough (comparable to Phase3's own per-node cost);
otherwise fall back to the top few winners and report the real measured cost so
a human can judge. The FIRST candidate of each scope is always run standalone
(to get a real, uncontended timing measurement) before the scope-wide decision
is made and the (possibly-truncated) remainder dispatched to the pool.
--limit lets a caller force the fallback explicitly.

Usage:
  .venv/bin/python scripts/phase5_second_level_overlay_check.py --ticker SOXL \\
      --version v6-massive-w2021-08-23_2026-08-21
  .venv/bin/python scripts/phase5_second_level_overlay_check.py --ticker SOXL \\
      --strategy TrailingBothZScoreBreakout --fixed-sl 1 --version <version>
  .venv/bin/python scripts/phase5_second_level_overlay_check.py --ticker SOXL \\
      --version <version> --limit 3   # top 3 finalists per scope only
"""
import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import pandas as pd

from run_optimization_sweep import DB_PATH, derive_phase25_candidates_ground_truth
from prune_backtest_cache_ground_truth import discover_all_gt_scopes, _hp_for_strategy
from phase4_candidate_nodes_resolver import (
    derive_phase25_candidates_from_candidate_nodes, discover_candidate_nodes_scopes,
)
from sim_1s_vs_1m_groundtruth_overlays import (
    load_hourly, load_seconds, resample_seconds_to_minutes, daily_indicators, cagr,
)
from backtester import (
    run_backtest_ground_truth, apply_addon_overlay_ground_truth,
    simulate_drought_overlay_ground_truth,
)
from candidate_verification_store import get_stored, upsert

# Above this measured per-candidate wall-clock cost (seconds), fall back to
# top-few-only instead of the full finalist set, absent an explicit --limit
# override. Kept as a safety net, not removed, even though the 2026-08-29
# kernel-direct trade generation (see module docstring) measures ~4s/candidate
# in practice -- far under this threshold, so it's not expected to trigger for
# SOXL-scale data anymore, but stays in place for any future ticker/candidate
# that's genuinely slower (e.g. a much longer history or a pathological trade
# count) rather than assuming the fast case always holds.
_CHEAP_ENOUGH_SECS_PER_CANDIDATE = 15.0
_FALLBACK_TOP_N = 3

# Set once in main() before the ProcessPoolExecutor is created -- fork (the
# Linux default multiprocessing start method) gives every worker copy-on-write
# access to these without a per-worker reload. Never reassigned inside a
# worker process.
_DFH = None
_DF_1M = None
_DF_1S = None


def _compounded(rets):
    """Vendored copy of scripts/candidate_full_review.py's own `compounded()` (2026-08-29,
    Task #5) -- NOT imported from that module: importing it here drags in that file's
    full heavy dependency chain (walk_forward_check -> candidate_checklist_report ->
    verify_trailing_buy_resolution), which has an unrelated, pre-existing broken import
    (`replay_five_min` missing) as of this task -- confirmed by trying the direct import
    first. Trivial, stable math (compound a return-fraction list into a total-return pct);
    vendoring it here is safer than pulling in that whole chain for one function."""
    prod = 1.0
    for r in rets:
        prod *= (1 + r)
    return (prod - 1) * 100


def _chrono_split_robustness_verdict(rets):
    """Vendored copy of scripts/candidate_full_review.py's own `_chrono_split_robustness`
    -- verdict computation ONLY (half1/half2 chronological split + single-biggest-trade
    removal, docs/overlay_parameter_robustness_process.md steps 1/3), not the full return
    dict (win-rate stability isn't needed here). See `_compounded`'s own docstring for why
    this is vendored rather than imported. `rets` MUST already be chronological (Entry
    Time ascending) -- true for both the addon leg's armed-trade list and the drought
    overlay's own window list, matching the original function's same precondition.
    Returns None if <2 rets (same as the original)."""
    if len(rets) < 2:
        return None
    mid = len(rets) // 2
    half1, half2 = rets[:mid], rets[mid:]
    half1_pct, half2_pct = _compounded(half1), _compounded(half2)
    comp_all = _compounded(rets)
    biggest_idx = max(range(len(rets)), key=lambda i: rets[i])
    without_biggest = rets[:biggest_idx] + rets[biggest_idx + 1:]
    comp_without = _compounded(without_biggest) if without_biggest else 0.0
    flips = (comp_all > 0) != (comp_without > 0)
    if flips:
        return "FRAGILE (sign flip)"
    if half1_pct < 0 or half2_pct < 0:
        return "FRAGILE (half negative)"
    return "OK"


def node_from_candidate(ticker, strategy_name, entry_timing, fixed_sl, cand):
    """Same axis-column mapping run_optimization_sweep.build_candidate_report_
    ground_truth uses internally (its own is_both branch, right before its
    run_backtest_ground_truth call) -- reused here verbatim, not re-derived,
    so a Phase5 node is guaranteed to mean the same real (arm_pct/trail_buy_pct/
    trail_sell_pct) triple Phase4's own report already computed core/addon/
    drought CAGR from."""
    is_both = strategy_name == "TrailingBothZScoreBreakout"
    if is_both:
        trail_buy_pct, trail_sell_pct = float(cand["stop_loss"]), float(cand["tpct"])
    else:
        trail_buy_pct, trail_sell_pct = 0.0, float(cand["stop_loss"])
    arm_pct = float(cand["take_profit"])
    return dict(
        ticker=ticker, strategy=strategy_name, window=cand["window"],
        z_score_threshold=cand["z_score_threshold"], fixed_sl=fixed_sl,
        arm_pct=arm_pct, arm_sell_pct=arm_pct, take_profit=None,
        trail_buy_pct=trail_buy_pct, trail_sell_pct=trail_sell_pct,
        max_hold_hours=cand["max_hold_hours"], entry_timing=entry_timing,
        # Real candidate_nodes.id (Task #4, 2026-08-29, planner dispatch) -- only
        # present for the candidate_nodes-fallback path (phase4_candidate_nodes_
        # resolver's own `cand` dicts carry a real `id`); the backtest_cache-sourced
        # path's `cand` dicts have no such key, so `.get` yields None here, and None
        # is the signal used everywhere below to skip verification-result persistence
        # for that path (no candidate_id exists to validate/insert against).
        id=cand.get("id"),
    )


def _run_gt_kernel(node, dfh, minute_df, start, end):
    """Real production kernel call, replacing sim_1s_vs_1m_groundtruth_overlays.
    simulate() as of 2026-08-29 (Task #2) -- see module docstring's trade-
    generation-source note. `run_backtest_ground_truth`'s need_times=True output
    is already in the exact gt_trades dict shape apply_addon_overlay_ground_truth/
    simulate_drought_overlay_ground_truth expect ('Entry Time'/'Entry Price'/
    'Exit Time'/'Exit Price'/'exit_reason'/'Return'/'armed'/'Arm Price'), so no
    to_gt_dict()-style adapter is needed here -- the old simulate()+Trade path
    needed that conversion, this one doesn't."""
    is_both = node["strategy"] == "TrailingBothZScoreBreakout"
    ind = daily_indicators(dfh, int(node["window"]))
    bars = dfh.loc[start:end + " 23:59:59"]
    return run_backtest_ground_truth(
        bars, ind, node["ticker"], minute_df,
        fixed_sl=node["fixed_sl"], arm_pct=node["arm_pct"], trail_buy_pct=node["trail_buy_pct"],
        trail_sell_pct=node["trail_sell_pct"], max_hours_to_hold=node["max_hold_hours"],
        z_score_threshold=node["z_score_threshold"], is_both=is_both,
        open_check_entry_timing=(node["entry_timing"] == "open_check"),
        same_bar_reentry=True, need_times=True,
    )


def overlay_cagrs(gt_trades, ticker, dfh, node, years):
    """Runs the SAME existing, already-validated GT overlay functions
    scripts/sim_1s_vs_1m_groundtruth_overlays.py's report_overlays() calls
    (apply_addon_overlay_ground_truth / simulate_drought_overlay_ground_truth),
    unmodified -- this differs from report_overlays only in RETURNING the
    computed CAGR numbers (for a delta table) instead of only printing them.
    `gt_trades` is already dict-shaped (see _run_gt_kernel), no conversion here.

    core_both_cagr (2026-08-29, Task #5): core+addon+drought triple-stacked CAGR,
    same formula scripts/candidate_full_review.py's own core_both_cagr_pct uses
    (core_factor * addon_factor_gated * drought_factor_gated) -- GATED the same way
    that reference does: addon/drought are only stacked in if their own chronological-
    split robustness verdict is 'OK' (docs/overlay_parameter_robustness_process.md
    steps 1/3), else they contribute a neutral 1.0 factor. Deliberately NOT matching
    Phase5's own addon_cagr/drought_cagr above, which stay ungated (unchanged, existing
    behavior) -- picked gating for core_both specifically because (a) the verification
    target for this task IS candidate_full_review.py's own gated number for node 851,
    so an ungated version would validate against the wrong definition, and (b) an
    ungated combined number risks a fragile single-trade addon/drought outlier
    dominating the 1m-vs-1s delta, which would read as a granularity-sensitivity
    finding when it's actually a robustness-fragility artifact -- exactly the
    conflation Phase5 exists to avoid. addon's own per-trade return for the gate
    uses the UNBLENDED (Exit-Arm)/Arm leg return, not apply_addon_overlay_ground_
    truth's blended 'Return' (which already contains core's own return -- see that
    function's own docstring; using the blended value here would double-count core
    exactly like candidate_full_review.py's own documented 2026-08-23 paired-review
    fix)."""
    if not gt_trades:
        return None, None, None, None
    addon_trades = apply_addon_overlay_ground_truth(gt_trades)
    core_bal = addon_bal = 1.0
    for t in addon_trades:
        core_bal *= (1 + t["Return_core"])
        addon_bal *= (1 + t["Return"])
    core_cagr = cagr(core_bal, years, start_bal=1.0)
    addon_cagr = cagr(addon_bal, years, start_bal=1.0)

    armed_trades = [t for t in addon_trades if t.get('armed')]
    addon_rets = [(t['Exit Price'] - t['Arm Price']) / t['Arm Price'] for t in armed_trades]
    addon_compounded_pct = _compounded(addon_rets) if addon_rets else None
    addon_ok = (len(addon_rets) >= 2 and
                _chrono_split_robustness_verdict(addon_rets) == 'OK')

    drought_cagr = None
    drought_compounded_pct = None
    drought_ok = False
    if node["strategy"] == "TrailingBothZScoreBreakout":
        drought = simulate_drought_overlay_ground_truth(
            gt_trades, dfh, ticker, fixed_sl=node["fixed_sl"], arm_pct=node["arm_pct"],
            trail_sell_pct=node["trail_sell_pct"])
        if drought is not None and drought.get("combined_compounded_pct") is not None:
            drought_cagr = cagr(1.0 + drought["combined_compounded_pct"] / 100.0, years, start_bal=1.0)
        if drought is not None:
            drought_compounded_pct = drought.get("drought_compounded_pct")
            drought_rets = drought.get("best_rets")
            drought_ok = (drought_rets is not None and len(drought_rets) >= 2 and
                          _chrono_split_robustness_verdict(drought_rets) == 'OK')

    addon_factor_gated = (1.0 + addon_compounded_pct / 100.0) if (
        addon_compounded_pct is not None and addon_ok) else 1.0
    drought_factor_gated = (1.0 + drought_compounded_pct / 100.0) if (
        drought_compounded_pct is not None and drought_ok) else 1.0
    core_both_bal = core_bal * addon_factor_gated * drought_factor_gated
    core_both_cagr = cagr(core_both_bal, years, start_bal=1.0)

    return core_cagr, addon_cagr, drought_cagr, core_both_cagr


def _check_candidate_core(node, dfh, df_1m, df_1s, start, end, years):
    """The actual compute -- shared by the standalone first-candidate timing
    call (main process) and the pool worker below (forked process, reading
    the module-level _DFH/_DF_1M/_DF_1S globals instead of taking them as
    arguments, so they're never re-pickled/re-sent per task)."""
    t0 = time.monotonic()
    trades_1m = _run_gt_kernel(node, dfh, df_1m, start, end)
    trades_1s = _run_gt_kernel(node, dfh, df_1s, start, end)
    core_1m, addon_1m, drought_1m, both_1m = overlay_cagrs(trades_1m, node["ticker"], dfh, node, years)
    core_1s, addon_1s, drought_1s, both_1s = overlay_cagrs(trades_1s, node["ticker"], dfh, node, years)
    elapsed = time.monotonic() - t0
    return {
        "n_trades_1m": len(trades_1m), "n_trades_1s": len(trades_1s),
        "core_cagr_1m": core_1m, "core_cagr_1s": core_1s,
        "addon_cagr_1m": addon_1m, "addon_cagr_1s": addon_1s,
        "drought_cagr_1m": drought_1m, "drought_cagr_1s": drought_1s,
        "core_both_cagr_1m": both_1m, "core_both_cagr_1s": both_1s,
        "core_delta_pp": None if (core_1m is None or core_1s is None) else (core_1m - core_1s) * 100,
        "addon_delta_pp": None if (addon_1m is None or addon_1s is None) else (addon_1m - addon_1s) * 100,
        "drought_delta_pp": None if (drought_1m is None or drought_1s is None) else (drought_1m - drought_1s) * 100,
        "core_both_delta_pp": None if (both_1m is None or both_1s is None) else (both_1m - both_1s) * 100,
        "elapsed_secs": elapsed,
    }


def _pool_worker(cand_idx, node, start, end, years):
    """Runs in a forked worker process -- reads _DFH/_DF_1M/_DF_1S as module-
    level globals (inherited via fork's copy-on-write at pool-creation time,
    set in main() before the ProcessPoolExecutor is created), not as function
    arguments, so the ~450MB+ DataFrames are never pickled/sent over IPC.

    The already-verified skip check happens in run_scope BEFORE dispatch (so an
    already-stored candidate never costs a pool round-trip at all, see run_scope) --
    this worker always does a genuine fresh kernel run."""
    row = _check_candidate_core(node, _DFH, _DF_1M, _DF_1S, start, end, years)
    row["candidate"] = cand_idx
    return row


def _stored_row_or_none(cid, cand_idx):
    """Query-first skip (Task #4, 2026-08-29, planner correction): if `cid` (real
    candidate_nodes.id) already has a stored phase5 result, return a row dict shaped
    like _check_candidate_core's own return value (elapsed_secs=0.0 -- genuinely
    near-zero real cost this run, which is also what the scope-wide cost-based
    fallback decision in run_scope should see). Returns None if `cid` is None (the
    backtest_cache-sourced path, no candidate_id to check) or nothing is stored yet."""
    if cid is None:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        stored = get_stored(conn, cid, "phase5")
    if stored is None:
        return None
    print(f"  candidate {cand_idx}: already verified at {stored['checked_at']}, using stored result")
    row = {k: v for k, v in stored.items() if k != "checked_at"}
    row["elapsed_secs"] = 0.0
    row["candidate"] = cand_idx
    return row


def _print_row(row, node):
    print(f"  candidate {row['candidate']}: TP={node['arm_pct']} SL={node['trail_buy_pct']} "
          f"tpct={node['trail_sell_pct']} hold={node['max_hold_hours']}h -- "
          f"core 1m/1s={_fmt(row['core_cagr_1m'])}/{_fmt(row['core_cagr_1s'])} "
          f"(delta {_fmt_pp(row['core_delta_pp'])})  "
          f"addon 1m/1s={_fmt(row['addon_cagr_1m'])}/{_fmt(row['addon_cagr_1s'])} "
          f"(delta {_fmt_pp(row['addon_delta_pp'])})  "
          f"drought 1m/1s={_fmt(row['drought_cagr_1m'])}/{_fmt(row['drought_cagr_1s'])} "
          f"(delta {_fmt_pp(row['drought_delta_pp'])})  "
          f"both 1m/1s={_fmt(row['core_both_cagr_1m'])}/{_fmt(row['core_both_cagr_1s'])} "
          f"(delta {_fmt_pp(row['core_both_delta_pp'])})  [{row['elapsed_secs']:.1f}s]")


def _fmt(x):
    return "N/A" if x is None else f"{x:.2%}"


def _fmt_pp(x):
    return "N/A" if x is None else f"{x:+.2f}pp"


def run_scope(ticker, strategy_name, version, entry_timing, fixed_sl, dfh, df_1m, df_1s,
              start, end, years, pool, limit=None, window=None):
    """`window` (Task #3, 2026-08-29): when set, this scope was discovered via
    candidate_nodes (not backtest_cache) -- see main()'s scope-discovery fallback and
    phase4_candidate_nodes_resolver.py's own docstring for why the version string alone
    isn't enough to disambiguate a real campaign in that case. `window=None` (default)
    keeps the original backtest_cache-based derive_phase25_candidates_ground_truth path
    exactly as before."""
    if window is not None:
        print(f"  (candidate_nodes fallback -- no backtest_cache rows for this version; "
              f"window={window} disambiguates which real batch)")
        candidates = derive_phase25_candidates_from_candidate_nodes(
            ticker, strategy_name, version, fixed_sl=fixed_sl, entry_timing=entry_timing,
            window=window)
    else:
        hp = _hp_for_strategy(strategy_name)
        candidates = derive_phase25_candidates_ground_truth(
            ticker, strategy_name, version, hp, fixed_sl=fixed_sl, entry_timing=entry_timing)
    if not candidates:
        print("  no Phase4 candidates for this scope -- skipping.")
        return []
    if limit is not None:
        candidates = candidates[:limit]

    # First candidate always runs standalone (main process, not the pool) --
    # gives a real, uncontended timing measurement to decide the rest of this
    # scope's scope, per this file's own docstring. Only meaningful when the
    # caller didn't already force --limit.
    # Task #4, 2026-08-29 (planner correction, point 4): the "always run first
    # candidate standalone for timing" logic goes through the same skip-check as
    # every other candidate -- an already-verified first candidate must not bypass
    # the stored-result reuse just because of this special case.
    first_node = node_from_candidate(ticker, strategy_name, entry_timing, fixed_sl, candidates[0])
    first_row = _stored_row_or_none(first_node.get("id"), 1)
    if first_row is None:
        first_row = _check_candidate_core(first_node, dfh, df_1m, df_1s, start, end, years)
        first_row["candidate"] = 1
        if first_node.get("id") is not None:
            with sqlite3.connect(DB_PATH) as conn:
                upsert(conn, first_node["id"], "phase5", first_row)
    _print_row(first_row, first_node)
    rows = [dict(first_row, ticker=ticker, strategy=strategy_name, fixed_sl=fixed_sl, window=window)]

    remaining = candidates[1:]
    if limit is None and len(candidates) > _FALLBACK_TOP_N:
        if first_row["elapsed_secs"] > _CHEAP_ENOUGH_SECS_PER_CANDIDATE:
            print(f"  first candidate took {first_row['elapsed_secs']:.1f}s (> "
                  f"{_CHEAP_ENOUGH_SECS_PER_CANDIDATE:.0f}s cheap-enough threshold) -- "
                  f"falling back to top {_FALLBACK_TOP_N} finalists only for this scope.")
            remaining = remaining[:_FALLBACK_TOP_N - 1]
        else:
            print(f"  first candidate took {first_row['elapsed_secs']:.1f}s (<= "
                  f"{_CHEAP_ENOUGH_SECS_PER_CANDIDATE:.0f}s) -- running all "
                  f"{len(candidates)} finalists for this scope.")

    if remaining:
        futures = {}
        extra_by_idx = {}
        for i, cand in enumerate(remaining, start=2):
            node = node_from_candidate(ticker, strategy_name, entry_timing, fixed_sl, cand)
            row = _stored_row_or_none(node.get("id"), i)
            if row is not None:
                _print_row(row, node)
                extra_by_idx[i] = dict(row, ticker=ticker, strategy=strategy_name,
                                        fixed_sl=fixed_sl, window=window)
                continue
            fut = pool.submit(_pool_worker, i, node, start, end, years)
            futures[fut] = node
        for fut in as_completed(futures):
            row = fut.result()
            node = futures[fut]
            _print_row(row, node)
            if node.get("id") is not None:
                with sqlite3.connect(DB_PATH) as conn:
                    upsert(conn, node["id"], "phase5", row)
            extra_by_idx[row["candidate"]] = dict(row, ticker=ticker, strategy=strategy_name,
                                                   fixed_sl=fixed_sl, window=window)
        # Combine in submission order (not completion order) so scope output
        # reads in the same TP/SL-descending order Phase4's own report does,
        # even though the underlying work ran in parallel.
        for i in sorted(extra_by_idx):
            rows.append(extra_by_idx[i])

    return rows


def _try_all_stored(scopes, limit):
    """Task #4 early-exit (2026-08-29, planner correction, point 1): if every scope
    in `scopes` is candidate_nodes-sourced (window is not None -- checked by the
    caller before this is invoked at all) AND every real candidate across all of
    them already has a stored phase5 result, build the full summary straight from
    candidate_verification_results and return it -- letting main() skip the
    multi-GB 1-second data load entirely on a pure rerun. Returns None the moment
    any candidate anywhere still needs a fresh kernel run (including a scope with
    zero real candidates -- nothing to report, not "fully stored")."""
    all_rows = []
    with sqlite3.connect(DB_PATH) as conn:
        for ticker, strategy_name, version, entry_timing, fixed_sl, window in scopes:
            candidates = derive_phase25_candidates_from_candidate_nodes(
                ticker, strategy_name, version, fixed_sl=fixed_sl,
                entry_timing=entry_timing, window=window)
            if limit is not None:
                candidates = candidates[:limit]
            if not candidates:
                continue
            for i, cand in enumerate(candidates, start=1):
                cid = cand.get("id")
                stored = get_stored(conn, cid, "phase5") if cid is not None else None
                if stored is None:
                    return None
                node = node_from_candidate(ticker, strategy_name, entry_timing, fixed_sl, cand)
                print(f"  candidate {i}: already verified at {stored['checked_at']}, using stored result")
                row = {k: v for k, v in stored.items() if k != "checked_at"}
                row["elapsed_secs"] = 0.0
                row["candidate"] = i
                _print_row(row, node)
                all_rows.append(dict(row, ticker=ticker, strategy=strategy_name,
                                      fixed_sl=fixed_sl, window=window))
    return all_rows


def main():
    global _DFH, _DF_1M, _DF_1S
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--strategy", default=None, help="omit to check every real GT strategy scope")
    ap.add_argument("--entry-timing", default=None, help="omit to check every real GT entry_timing scope")
    ap.add_argument("--fixed-sl", type=float, default=None, help="omit to check every real GT fixed_sl scope")
    ap.add_argument("--window", type=int, default=None,
                     help="filter to one real window value -- only meaningful for the "
                          "candidate_nodes fallback path (a version string can alias multiple "
                          "unrelated in-memory-pipeline batches, see phase4_candidate_nodes_"
                          "resolver.py); ignored for backtest_cache-sourced scopes, which "
                          "aggregate across windows exactly as derive_phase25_candidates_"
                          "ground_truth always has.")
    ap.add_argument("--data-source", choices=["yahoo", "massive"], default="massive")
    ap.add_argument("--limit", type=int, default=None,
                     help="force top-N finalists per scope instead of the auto cost-based decision")
    ap.add_argument("--workers", type=int, default=6,
                     help="ProcessPoolExecutor worker count (default 6, half of a 12-core box, "
                          "leaves headroom -- this is off-hours ad hoc research compute, not a "
                          "live-daemon-adjacent job, see long-job-launch skill's cap-to-headroom rule)")
    args = ap.parse_args()

    with sqlite3.connect(DB_PATH) as conn:
        all_scopes = discover_all_gt_scopes(conn)
    # 6-tuples (ticker, strategy, version, entry_timing, fixed_sl, window) -- window=None
    # for a backtest_cache-sourced scope (old path, aggregates across windows, matching
    # derive_phase25_candidates_ground_truth's own real cross-window semantics).
    scopes = [(s[0], s[1], s[2], s[3], s[4], None) for s in all_scopes
              if s[0] == args.ticker and s[2] == args.version
              and (args.strategy is None or s[1] == args.strategy)
              and (args.entry_timing is None or s[3] == args.entry_timing)
              and (args.fixed_sl is None or s[4] == args.fixed_sl)]
    covered = {(s[1], s[3], s[4]) for s in scopes}

    # candidate_nodes fallback (Task #3, 2026-08-29): a campaign the in-memory pipeline
    # ran has ZERO backtest_cache rows, so discover_all_gt_scopes can never find it --
    # only skip a (strategy, entry_timing, fixed_sl) already covered above, never a
    # window duplicate of it (see phase4_candidate_nodes_resolver.discover_candidate_
    # nodes_scopes' own docstring on why window is a real disambiguator here, not
    # redundant with the backtest_cache scope shape).
    cn_scopes = discover_candidate_nodes_scopes(args.ticker, args.version)
    for strategy, entry_timing, fixed_sl, window in cn_scopes:
        if (strategy, entry_timing, fixed_sl) in covered:
            continue
        if (args.strategy is not None and strategy != args.strategy):
            continue
        if (args.entry_timing is not None and entry_timing != args.entry_timing):
            continue
        if (args.fixed_sl is not None and fixed_sl != args.fixed_sl):
            continue
        if (args.window is not None and window != args.window):
            continue
        scopes.append((args.ticker, strategy, args.version, entry_timing, fixed_sl, window))

    if not scopes:
        raise SystemExit(f"No real GT scopes found (backtest_cache or candidate_nodes) for "
                          f"ticker={args.ticker} version={args.version} strategy={args.strategy} "
                          f"entry_timing={args.entry_timing} fixed_sl={args.fixed_sl} "
                          f"window={args.window}")
    print(f"Found {len(scopes)} real GT scope(s) to check "
          f"({sum(1 for s in scopes if s[5] is not None)} via candidate_nodes fallback).")

    # Early-exit-before-data-load (Task #4, 2026-08-29, planner correction, point 1):
    # if EVERY scope is candidate_nodes-sourced (window is not None -- a backtest_
    # cache-sourced scope always needs a fresh run, no candidate_id to check against)
    # AND every real candidate in every such scope already has a stored phase5 result,
    # skip the multi-GB 1-second data load entirely and build the summary straight
    # from candidate_verification_results.
    load_secs = 0.0
    run_secs = 0.0
    all_rows = _try_all_stored(scopes, args.limit) if all(s[5] is not None for s in scopes) else None

    if all_rows is not None:
        print("\nAll candidates in every scope already verified -- skipping data load entirely.")
    else:
        t_load = time.monotonic()
        print(f"Loading {args.ticker} hourly data...")
        _DFH = load_hourly(args.ticker, data_source=args.data_source)
        print(f"Loading {args.ticker} 1-second data (this is the slow step)...")
        _DF_1S = load_seconds(args.ticker)
        print(f"  {len(_DF_1S):,} 1-second rows, {_DF_1S.index.min()} -> {_DF_1S.index.max()}")
        _DF_1M = resample_seconds_to_minutes(_DF_1S)
        print(f"  {len(_DF_1M):,} 1-minute rows (resampled from the 1s series above)")
        load_secs = time.monotonic() - t_load
        print(f"Data load: {load_secs:.1f}s")

        start = "2021-08-23"
        end = "2026-08-21"
        bars_for_years = _DFH.loc[start:end + " 23:59:59"]
        years = (bars_for_years.index.max() - bars_for_years.index.min()).total_seconds() / (365.25 * 86400)

        t_run = time.monotonic()
        all_rows = []
        # Pool created AFTER _DFH/_DF_1M/_DF_1S are populated, so fork (Linux
        # default) gives every worker copy-on-write access with no reload.
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for ticker, strategy_name, version, entry_timing, fixed_sl, window in scopes:
                print(f"\n{'#' * 80}\n{ticker} / {strategy_name} / {version} / "
                      f"entry_timing={entry_timing} / fixed_sl={fixed_sl}"
                      f"{f' / window={window}' if window is not None else ''}\n{'#' * 80}")
                rows = run_scope(ticker, strategy_name, version, entry_timing, fixed_sl,
                                  _DFH, _DF_1M, _DF_1S, start, end, years, pool, limit=args.limit,
                                  window=window)
                all_rows.extend(rows)
        run_secs = time.monotonic() - t_run

    if not all_rows:
        print("\nNo candidates checked -- nothing to summarize.")
        return

    df_out = pd.DataFrame(all_rows)
    # Task #5, 2026-08-29 (planner dispatch): filename now includes version+window --
    # the old fixed ticker-only filename let two different version/window runs for
    # the same ticker silently overwrite each other's output. One CSV per real window
    # value (a per-scope split, since a single run can cover multiple windows) --
    # matches the output/phase4_<ticker>_<version>_w<window> convention used by
    # scripts/run_candidate_nodes_campaign_verification.py. A backtest_cache-sourced
    # scope (window=None) has no real window to key on -- its rows go to the
    # no-suffix filename, matching this script's original single-CSV-per-run shape
    # for that path (only one such group can exist per run either way).
    out_paths = []
    for window_val, group in df_out.groupby(df_out["window"], dropna=False):
        suffix = f"_w{int(window_val)}" if pd.notna(window_val) else ""
        out_path = os.path.join(
            ROOT, "output",
            f"phase5_second_level_overlay_check_{args.ticker.lower()}_{args.version}{suffix}.csv")
        group.to_csv(out_path, index=False)
        out_paths.append(out_path)

    print(f"\n=== Phase5 summary: {len(df_out)} candidate(s) checked across {len(scopes)} scope(s) ===")
    for label in ("core_delta_pp", "addon_delta_pp", "drought_delta_pp", "core_both_delta_pp"):
        s = df_out[label].dropna()
        if s.empty:
            print(f"  {label}: no candidates had both 1m/1s values -- N/A")
            continue
        print(f"  {label}: mean={s.mean():+.2f}pp median={s.median():+.2f}pp "
              f"worst={s.max():+.2f}pp best={s.min():+.2f}pp "
              f"({(s > 0).sum()}/{len(s)} show 1m OPTIMISM)")
    print(f"\nData load: {load_secs:.1f}s | Candidate checks: {run_secs:.1f}s total ({args.workers} workers), "
          f"{run_secs / len(df_out):.1f}s/candidate average (wall-clock, not CPU-time) | "
          f"Total: {load_secs + run_secs:.1f}s")
    print(f"Full results: {', '.join(out_paths)}")


if __name__ == "__main__":
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import script_usage
    script_usage.record_invocation()
    main()
