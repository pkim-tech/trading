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
    k1_status,
)
from phase4_candidate_nodes_resolver import derive_phase25_candidates_from_candidate_nodes
from build_v6_promotion_combined_report import CURATED_HEADERS, MANUAL_BLANK_COLS
import candidate_report_inmemory as cri

LIVE_DB_PATH = "cache/live/trading_live.db"

RAW_COLUMNS = cri.COLUMNS[:14] + [
    "core_cagr_1m", "core_cagr_1s", "n_trades_1m", "n_trades_1s",
    "addon_cagr_1m", "addon_cagr_1s", "drought_cagr_1m", "drought_cagr_1s",
    "core_both_cagr_1m", "core_both_cagr_1s", "core_safe",
]

# CURATED_HEADERS-aligned column sets for the Candidates/raw tabs -- v4 (2026-09-01,
# same-day dispatch: "one consistent column vocabulary across the whole workbook").
# Only the CURATED_HEADERS columns with a real lightweight-data equivalent are included
# (Cagr/Cagr Add on/CAGR Drought/CAGR Both map onto the *_cagr_1s verification numbers,
# NOT the full-checklist strategy_cagr_pct/core_addon_cagr_pct -- a genuinely different,
# smaller-precision metric, same distinction candidate_report_inmemory.py's own docstring
# already draws). Columns with no lightweight equivalent (Add on %/Drought % as raw
# non-annualized per-leg returns, Add on/Drought trades/tranche, Drought WR Verdict,
# Years, Target-Manual) are left out entirely rather than rendered as fabricated/always-
# blank noise -- the real per-row full-checklist versions of those already exist on the
# Full Review/Combined tabs. Real sweep-parameter columns (Window/Z/...) are appended
# after the CURATED_HEADERS-aligned block since they're this tab's own real identity,
# not part of CURATED_HEADERS at all.
_CURATED_ALIGNED_PREFIX = ["ticker", "#", "Node ID", "Matches Promotion", "K1", "Strategy",
                           "Cagr", "Worst Neighbor", "Cliff Safe", "Trades",
                           "Cagr Add on", "CAGR Drought", "CAGR Both"]
_SWEEP_PARAM_SUFFIX = ["Window", "Z", "Fixed SL", "Arm %", "Trail Buy %", "Trail Sell %",
                       "Max Hold Hrs", "Entry Timing"]
CANDIDATES_TAB_HEADERS = _CURATED_ALIGNED_PREFIX[:6] + ["Winner"] + _CURATED_ALIGNED_PREFIX[6:] + _SWEEP_PARAM_SUFFIX
RAW_TAB_HEADERS = _CURATED_ALIGNED_PREFIX + ["promoted_pick"] + _SWEEP_PARAM_SUFFIX


def _promoted_node_ids(conn, version, tickers, live_db_path=LIVE_DB_PATH):
    """Real 'currently promoted' node_id per ticker for this exact version, resolved
    against watch_list (cache/live/trading_live.db) -- v4 (2026-09-01 dispatch),
    deliberately NOT a hardcoded dict like the legacy build_v6_promotion_combined_
    report.py's PROMOTED_NODE_IDS (confirmed by reading that file directly: it's a
    one-time manual snapshot of the 2026-08-23 promotion batch, no query at all).
    A watch_list row's real (window/z_score_threshold/fixed_sl/take_profit/trail_buy_pct/
    trail_sell_pct/max_hold_hours/entry_timing) already matches candidate_nodes' own
    storage encoding column-for-column (both are the strategy's real live param names,
    no is_both-style inversion needed) -- looked up via the exact same key_cols get_or_
    create_candidate_node uses, so a promoted node_id here always agrees with a Full
    Review row's own node_id for the identical param tuple. Returns {} if watch_list has
    no non-archived row under this version for any of `tickers` (e.g. a campaign that
    hasn't been promoted yet -- confirmed the real, current state for v6.5 as of
    2026-09-01)."""
    from locate_best_node import get_or_create_candidate_node
    if not tickers:
        return {}
    placeholders = ",".join("?" * len(tickers))
    live_conn = sqlite3.connect(live_db_path)
    try:
        rows = live_conn.execute(f"""
            SELECT ticker, strategy, version, window, z_score_threshold, fixed_sl,
                   take_profit, trail_buy_pct, trail_sell_pct, max_hold_hours, entry_timing
            FROM watch_list
            WHERE version=? AND archived_at IS NULL AND ticker IN ({placeholders})
        """, (version, *tickers)).fetchall()
    finally:
        live_conn.close()
    out = {}
    for (ticker, strategy, ver, window, z, fixed_sl, tp, tbp, tsp, hold, entry_timing) in rows:
        node_id = get_or_create_candidate_node(conn, {
            "ticker": ticker, "strategy": strategy, "version": ver, "window": window,
            "z": z, "fixed_sl": fixed_sl or 0.0, "arm_pct": float(tp or 0),
            "trail_buy_pct": float(tbp or 0), "trail_sell_pct": float(tsp or 0),
            "max_hold_hours": hold, "entry_timing": entry_timing,
            "robust_alpha": None, "trades": None, "sweep_run_id": None,
        })
        out[ticker] = node_id
    return out


def _matches_promotion(ticker, node_id, promoted_ids):
    promoted_id = promoted_ids.get(ticker)
    if promoted_id is None:
        return "N/A (not yet promoted)"
    return "YES" if node_id == promoted_id else "no"


def _enrich_curated_rows(conn, curated_rows):
    """Adds core_safe (-> Cliff Safe label) + core_both_cagr_1m/1s to already-curated
    (small, ~top-N-per-category) candidate dicts for the CURATED_HEADERS-aligned
    Candidates tab -- a small batched lookup against phase4_results/candidate_
    verification_results, NOT a change to candidate_report_inmemory.fetch_rows' own
    stable query (other callers of that function must not see new columns/behavior)."""
    ids = [r["id"] for r in curated_rows]
    if not ids:
        return curated_rows
    placeholders = ",".join("?" * len(ids))
    safe_map = {cid: (None if v is None else bool(v)) for cid, v in conn.execute(
        f"SELECT candidate_id, core_safe FROM phase4_results WHERE candidate_id IN ({placeholders})", ids)}
    both_map = {cid: (m, s) for cid, m, s in conn.execute(
        f"SELECT candidate_id, core_both_cagr_1m, core_both_cagr_1s FROM candidate_verification_results "
        f"WHERE candidate_id IN ({placeholders})", ids)}
    for r in curated_rows:
        core_safe = safe_map.get(r["id"])
        r["cliff_safe_label"] = None if core_safe is None else ("SAFE" if core_safe else "CLIFF")
        r["core_both_cagr_1m"], r["core_both_cagr_1s"] = both_map.get(r["id"], (None, None))
    return curated_rows


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
    if ids:
        placeholders = ",".join("?" * len(ids))
        cagr_map = dict(conn.execute(
            f"SELECT candidate_id, core_cagr_1s FROM candidate_verification_results "
            f"WHERE candidate_id IN ({placeholders})", ids))
    for r in csv_rows:
        r["core_cagr_1s"] = cagr_map.get(r.get("node_id"))
    return csv_rows


def _curate_combined_rows(csv_rows):
    """Local adaptation of build_v6_promotion_combined_report.curate_rows() for the
    Combined tab -- v4 fix (2026-09-01, real bug found + user-confirmed fix, see
    deep_backlog.md). Deliberately NOT calling that function directly (its own file isn't
    edited either) -- two changes from the original, everything else (top-2-per-category
    Add On/Drought/Best-Both + Core-fallback convention, category definitions, output
    shape/`_winner` labeling) is identical:

    1. Dedup key is `node_id` instead of `(ticker, strategy, fixed_sl)`. The original key
       assumed one campaign = one window (never needed window to disambiguate a real
       candidate); candidate_nodes rows (this data source) have no `fixed_sl` field at all
       in their csv_row shape (not in FIELDNAMES), so `r.get('fixed_sl')` silently returned
       None for every row, collapsing ALL rows for a (ticker, strategy) pair onto one
       dedup key -- confirmed on real ETHU data: 7 distinct node_ids collapsed into 1
       Combined row, with accumulated category labels misattributed to one arbitrary
       node_id. `node_id` is already a real, unique per-candidate identity for this data
       source (it isn't for the original backtest_cache-sourced use, which is why the
       original function doesn't use it) -- see _enrich_full_review_core_cagr's docstring
       for the sibling CAGR fix this pairs with.
    2. Best-Both sort key is `core_cagr_1s` instead of `strategy_cagr_pct` -- the latter is
       always None for this data source (see _enrich_full_review_core_cagr), which made
       every row's original sort key collapse to -inf (effectively unordered, first-seen-
       wins). core_cagr_1s (attached by _enrich_full_review_core_cagr, called before this)
       is the real per-candidate CAGR that does exist here."""
    by_ticker = {}
    for r in csv_rows:
        by_ticker.setdefault(r["ticker"], []).append(r)

    def key(r):
        return r["node_id"]

    out = []
    for ticker, rows in by_ticker.items():
        safe = [r for r in rows if r.get("status") == "SAFE"]

        addon = [r for r in safe if r.get("addon_tranche") != "FRAGILE"
                 and r.get("addon_compounded_pct") is not None]
        addon.sort(key=lambda r: r["addon_compounded_pct"], reverse=True)

        drought = [r for r in safe if r.get("drought_tranche") != "FRAGILE"
                   and r.get("drought_compounded_pct") is not None]
        drought.sort(key=lambda r: r["drought_compounded_pct"], reverse=True)

        best_both = [r for r in safe if r.get("strategy") == "TrailingBothZScoreBreakout"]
        best_both.sort(key=lambda r: r.get("core_cagr_1s") if r.get("core_cagr_1s") is not None else float("-inf"),
                        reverse=True)

        winners = {}
        for label, group in (("Add On", addon[:2]), ("Drought", drought[:2]), ("Best-Both", best_both[:2])):
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
            best = max(safe, key=lambda r: r.get("core_cagr_1s") if r.get("core_cagr_1s") is not None else float("-inf"))
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
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter

    promoted_ids = _promoted_node_ids(conn, version, tickers)
    k1_cache = {}

    def _k1(ticker):
        if ticker not in k1_cache:
            k1_cache[ticker] = k1_status(conn, ticker)
        return k1_cache[ticker]

    wb = Workbook()

    review_ws = wb.active
    review_ws.title = "Full Review"
    review_ws.append(FIELDNAMES)
    for cell in review_ws[1]:
        cell.font = Font(bold=True)
    for row in full_review_rows:
        review_ws.append([row.get(c) for c in FIELDNAMES])
    review_ws.freeze_panes = "B2"
    for i, col in enumerate(FIELDNAMES, start=1):
        review_ws.column_dimensions[get_column_letter(i)].width = max(12, min(len(col) + 2, 28))

    # Combined tab (v4, 2026-09-01 dispatch): exact CURATED_HEADERS + MANUAL_BLANK_COLS +
    # FIELDNAMES layout of the real precedent, build_v6_promotion_combined_report.py's
    # write_combined_xlsx -- reuses that file's own CURATED_HEADERS/MANUAL_BLANK_COLS
    # directly. Curation itself uses _curate_combined_rows (a local adaptation of that
    # file's curate_rows(), NOT called directly -- see that function's own docstring for
    # the two real, user-confirmed fixes: node_id dedup key instead of (ticker, strategy,
    # fixed_sl), core_cagr_1s instead of strategy_cagr_pct for Best-Both ranking/display).
    # curate_rows()-equivalent selection (top-2-per-category Add On/Drought/Best-Both +
    # Core-fallback) is what actually produces a real 'Winner' label per row -- an
    # uncurated full_review_rows row has no _winner at all, so the Combined tab is
    # deliberately the CURATED subset of full_review_rows, not every row in it.
    combined_ws = wb.create_sheet("Combined")
    full_review_rows = _enrich_full_review_core_cagr(conn, full_review_rows)
    combined_rows = _curate_combined_rows(full_review_rows)
    combined_rows.sort(key=lambda r: (r["ticker"], r["strategy"]))
    combined_headers = CURATED_HEADERS + [None] * MANUAL_BLANK_COLS + list(FIELDNAMES)
    combined_ws.append(combined_headers)
    for cell in combined_ws[1]:
        if cell.value:
            cell.font = Font(bold=True)
    for i, r in enumerate(combined_rows, start=2):
        matches_promotion = _matches_promotion(r["ticker"], r["node_id"], promoted_ids)
        curated = [
            r["ticker"], f"=COUNTIF($A$2:A{i},A{i})", r["node_id"], matches_promotion, r.get("k1_tranche"),
            r.get("strategy"), r.get("_winner"), r["core_cagr_1s"],
            r["worst_neighbor_pct"], None, r["trades"], r["years"], r["status"],
            r["addon_compounded_pct"], r["addon_n"], r["addon_tranche"], r["addon_wr_tranche"],
            r["drought_compounded_pct"], r["drought_n"], r["drought_tranche"], r["drought_wr_verdict"],
            r["core_addon_cagr_pct"], r["core_drought_cagr_pct"], r["core_both_cagr_pct"],
        ]
        raw = [r.get(h) for h in FIELDNAMES]
        combined_ws.append(curated + [None] * MANUAL_BLANK_COLS + raw)
    for i, h in enumerate(CURATED_HEADERS, start=1):
        combined_ws.column_dimensions[get_column_letter(i)].width = max(10, min(len(h) + 2, 30))
    combined_ws.freeze_panes = "B2"

    # Candidates tab -- v4: CURATED_HEADERS-aligned naming/order (see CANDIDATES_TAB_
    # HEADERS' own comment for which columns map and why some are left out).
    cand_ws = wb.create_sheet("Candidates")
    curated_rows = _enrich_curated_rows(conn, curated_rows)
    curated_rows = sorted(curated_rows, key=lambda r: (r["ticker"], -(r["core_cagr_1s"] or -1e9)))
    cand_ws.append(CANDIDATES_TAB_HEADERS)
    for cell in cand_ws[1]:
        cell.font = Font(bold=True)
    for i, r in enumerate(curated_rows, start=2):
        cand_ws.append([
            r["ticker"], f"=COUNTIF($A$2:A{i},A{i})", r["id"],
            _matches_promotion(r["ticker"], r["id"], promoted_ids), _k1(r["ticker"]), r["strategy"],
            r["_winner"], r["core_cagr_1s"], r["worst_neighbor_cagr"], r["cliff_safe_label"], r["trades"],
            r["addon_cagr_1s"], r["drought_cagr_1s"], r["core_both_cagr_1s"],
            r["window"], r["z"], r["fixed_sl"], r["arm_pct"], r["trail_buy_pct"],
            r["trail_sell_pct"], r["max_hold_hours"], r["entry_timing"],
        ])
    cand_ws.freeze_panes = "B2"
    for i, col in enumerate(CANDIDATES_TAB_HEADERS, start=1):
        cand_ws.column_dimensions[get_column_letter(i)].width = max(10, min(len(col) + 2, 22))

    # All Candidates (raw) tab -- v4: same CURATED_HEADERS-aligned naming/order, no
    # 'Winner' (curate()/curate_rows() were never run against the raw population, so no
    # real winner label exists per row -- keeping `promoted_pick` (curated top-N-set
    # membership, v2 semantics, unchanged) alongside the new 'Matches Promotion' (real
    # live-promotion match, v4) since the two are genuinely different concepts.
    curated_ids = {r["id"] for r in curated_rows}
    raw_ws = wb.create_sheet("All Candidates (raw)")
    raw_ws.append(RAW_TAB_HEADERS)
    for cell in raw_ws[1]:
        cell.font = Font(bold=True)
    for i, r in enumerate(raw_rows, start=2):
        core_safe = r.get("core_safe")
        cliff_safe_label = None if core_safe is None else ("SAFE" if core_safe else "CLIFF")
        raw_ws.append([
            r["ticker"], f"=COUNTIF($A$2:A{i},A{i})", r["id"],
            _matches_promotion(r["ticker"], r["id"], promoted_ids), _k1(r["ticker"]), r["strategy"],
            r["core_cagr_1s"], r["worst_neighbor_cagr"], cliff_safe_label, r["trades"],
            r["addon_cagr_1s"], r["drought_cagr_1s"], r["core_both_cagr_1s"], r["id"] in curated_ids,
            r["window"], r["z"], r["fixed_sl"], r["arm_pct"], r["trail_buy_pct"],
            r["trail_sell_pct"], r["max_hold_hours"], r["entry_timing"],
        ])
    raw_ws.freeze_panes = "B2"
    for i, col in enumerate(RAW_TAB_HEADERS, start=1):
        raw_ws.column_dimensions[get_column_letter(i)].width = max(10, min(len(col) + 2, 20))

    def_ws = wb.create_sheet("Column Definitions")
    def_ws.append(["Column", "Definition"])
    for cell in def_ws[1]:
        cell.font = Font(bold=True)
    for col, definition in COLUMN_DEFS.items():
        def_ws.append([col, definition])
        def_ws.cell(row=def_ws.max_row, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    def_ws.append(["node_id join key", "Full Review's 'node_id' column == Candidates'/All Candidates "
                                        "(raw)'s 'Node ID' column == Combined's 'Node ID' column == "
                                        "candidate_nodes.id -- use it to cross-reference a row on any "
                                        "tab back to the others."])
    def_ws.append(["promoted_pick", "All Candidates (raw) only: True if this node_id is in the "
                                     "Candidates tab's curated top-N-per-category set (v2 semantics -- "
                                     "distinct from 'Matches Promotion' below)."])
    def_ws.append(["Matches Promotion", "All tabs except Full Review: YES/no if this node_id matches "
                                         "the real currently-promoted node for this ticker (resolved "
                                         "against watch_list, not hardcoded), or 'N/A (not yet promoted)' "
                                         "if no watch_list row exists for this ticker under this version."])
    def_ws.append(["Combined tab", "Exact layout of the real precedent, build_v6_promotion_combined_"
                                    "report.py's write_combined_xlsx (CURATED_HEADERS + 6 blank manual "
                                    "columns + the full FIELDNAMES checklist), ported onto candidate_"
                                    "nodes-sourced data. Rows are the CURATED subset (curate_rows()'s "
                                    "top-2-per-category Add On/Drought/Best-Both + Core-fallback), not "
                                    "every Full Review row -- only a curated row has a real 'Winner'."])
    def_ws.append(["Generated", _git_provenance_stamp()])
    def_ws.column_dimensions["A"].width = 32
    def_ws.column_dimensions["B"].width = 110

    wb.save(out_path)


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
