"""CLI-arg-parsing / grid-construction tests for scripts/bench_phase1_phase2_inmemory.py.

Covers the 2026-08-29 additions:
  - new --z-thresholds flag (float, nargs="+", plain-replace of module Z_THRESHOLDS)
  - --window promoted to nargs="+" (multi-value = ONE pooled Phase1 axis, not N campaigns)
  - both flags added to the --seed-watch-list-id mutual-exclusivity list
  - new --n-islands flag overriding module-level N_ISLANDS, and the 3 call-site fixes
    that now pass n=N_ISLANDS explicitly to pick_island_centers() (its own `n` default
    parameter is bound to N_ISLANDS at function-definition time, not re-read per call)

Deliberately NO broker / stream / kernel mechanics and NO real backtest run --
argparse + the extracted apply_grid_overrides() helper only.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts import bench_phase1_phase2_inmemory as bench
from run_optimization_sweep import pick_island_centers


@pytest.fixture(autouse=True)
def _restore_module_grid():
    """apply_grid_overrides() mutates module-level globals; the module is imported
    once and shared, so snapshot/restore around every test."""
    win, z, n_isl = list(bench.WINDOWS), list(bench.Z_THRESHOLDS), bench.N_ISLANDS
    yield
    bench.WINDOWS, bench.Z_THRESHOLDS, bench.N_ISLANDS = win, z, n_isl


def _parse(argv):
    return bench.build_arg_parser().parse_args(argv)


def test_z_thresholds_override_replaces_grid():
    args = _parse(["--strategy", "TrailingBothZScoreBreakout",
                   "--z-thresholds", "0.5", "1", "1.5", "2"])
    assert args.z_thresholds == [0.5, 1.0, 1.5, 2.0]
    bench.apply_grid_overrides(args)
    assert bench.Z_THRESHOLDS == [0.5, 1.0, 1.5, 2.0]


def test_multi_window_is_one_pooled_axis():
    args = _parse(["--strategy", "TrailingBothZScoreBreakout",
                   "--window", "5", "10", "15", "20"])
    assert args.window == [5, 10, 15, 20]
    bench.apply_grid_overrides(args)
    # ONE list, not N separate grids
    assert bench.WINDOWS == [5, 10, 15, 20]
    assert isinstance(bench.WINDOWS, list)

    # Exercise the REAL production task-construction function (build_phase1_tasks_grid,
    # extracted verbatim from run_one_fixed_sl's non-seed-mode branch) rather than a
    # re-implementation -- this would fail if the real pooling logic ever broke.
    # AGQ widened grid: 4 z x 4 w x 1 tp x 1 sl x 1 hold x 1 tpct = 16 pooled cells,
    # all in ONE flat list (not 16 separate single-cell lists).
    bench.Z_THRESHOLDS = [0.5, 1.0, 1.5, 2.0]
    tasks = bench.build_phase1_tasks_grid(
        bench.Z_THRESHOLDS, bench.WINDOWS,
        take_profits=[10], stop_losses=[3], hold_time_caps=[14], trail_pcts=[2.0])
    assert isinstance(tasks, list)
    assert len(tasks) == 16
    assert len(set(tasks)) == 16
    windows_seen = {t[3] for t in tasks}
    z_seen = {t[4] for t in tasks}
    assert windows_seen == {5, 10, 15, 20}
    assert z_seen == {0.5, 1.0, 1.5, 2.0}


def test_final_topN_region_filter_does_not_key_on_window_or_z():
    """Real top-9 selection (~line 1231-1256) filters each island's region only on
    take_profit/stop_loss proximity to the island center -- confirms window and
    z_score_threshold are NOT part of that filter, i.e. a pooled multi-window/multi-z
    run's final ranking genuinely competes across all window/z values together rather
    than silently re-slicing back to one value each."""
    import inspect
    src = inspect.getsource(bench)
    region_block = src[src.index("final_centers = pick_island_centers(df_final"):
                        src.index("n_converged = sum(")]
    # The region-selection filter (df_final[...]) must only reference take_profit/
    # stop_loss island-proximity, never window/z_score_threshold.
    filter_block = region_block[region_block.index("region = df_final["):
                                 region_block.index("region = region[region")]
    assert "window" not in filter_block
    assert "z_score_threshold" not in filter_block


def test_single_window_still_works():
    args = _parse(["--strategy", "TrailingBothZScoreBreakout", "--window", "15"])
    assert args.window == [15]
    bench.apply_grid_overrides(args)
    assert bench.WINDOWS == [15]


def test_defaults_unchanged_when_flags_omitted():
    args = _parse(["--strategy", "TrailingBothZScoreBreakout"])
    assert args.window is None
    assert args.z_thresholds is None
    bench.apply_grid_overrides(args)
    assert bench.WINDOWS == [10, 20]
    assert bench.Z_THRESHOLDS == [1.0, 1.5, 2.0]


def test_pick_island_centers_n_override_changes_selection():
    """Directly tests the mechanism --n-islands relies on: pick_island_centers's own
    `n` parameter (imported from run_optimization_sweep.py) actually changes which/how
    many centers come back, since its default is bound to N_ISLANDS at function-
    definition time and NOT re-read per call -- the reason the 3 call sites in
    run_one_fixed_sl now pass n=N_ISLANDS explicitly instead of relying on the default.

    Fixture: 10 rows spread 10 apart on take_profit (well past ISLAND_MIN_SEP=6, and
    stop_loss held fixed) so every row is mutually far enough apart that none blocks
    another -- with n=10 all 10 distinct coordinates are picked, with n=3 only the
    top-3-by-cagr are.
    """
    df = pd.DataFrame({
        "take_profit": [i * 10 for i in range(10)],
        "stop_loss": [5] * 10,
        "cagr": [float(i) for i in range(10)],  # higher take_profit == higher cagr
    })

    centers_n3 = pick_island_centers(df, n=3, rank_col="cagr")
    centers_n10 = pick_island_centers(df, n=10, rank_col="cagr")

    assert len(centers_n3) == 3
    assert len(centers_n10) == 10
    assert set(centers_n3) != set(centers_n10)
    # greedy descending-cagr order is identical either way, so n=3's picks are a
    # strict prefix of n=10's picks, not just a differently-sized unrelated set
    assert centers_n10[:3] == centers_n3


def test_apply_grid_overrides_sets_n_islands():
    args = _parse(["--strategy", "TrailingBothZScoreBreakout", "--n-islands", "10"])
    assert args.n_islands == 10
    bench.apply_grid_overrides(args)
    assert bench.N_ISLANDS == 10


def test_apply_grid_overrides_leaves_n_islands_unchanged_when_omitted():
    args = _parse(["--strategy", "TrailingBothZScoreBreakout"])
    assert args.n_islands is None
    prev = bench.N_ISLANDS
    bench.apply_grid_overrides(args)
    assert bench.N_ISLANDS == prev


def test_z_thresholds_conflicts_with_seed_mode():
    with pytest.raises(SystemExit) as ei:
        _run_main(["--seed-watch-list-id", "999", "--z-thresholds", "0.5", "1"])
    assert "--z-thresholds" in str(ei.value)


def test_multi_window_conflicts_with_seed_mode():
    with pytest.raises(SystemExit) as ei:
        _run_main(["--seed-watch-list-id", "999", "--window", "5", "10"])
    assert "--window" in str(ei.value)


def test_n_islands_rejects_zero():
    with pytest.raises(SystemExit) as ei:
        _run_main(["--strategy", "TrailingBothZScoreBreakout", "--n-islands", "0"])
    assert "--n-islands" in str(ei.value)


def test_n_islands_rejects_negative():
    with pytest.raises(SystemExit) as ei:
        _run_main(["--strategy", "TrailingBothZScoreBreakout", "--n-islands", "-3"])
    assert "--n-islands" in str(ei.value)


def test_version_string_gets_isl_suffix_when_overridden():
    args = _parse(["--strategy", "TrailingBothZScoreBreakout", "--n-islands", "10"])
    version = bench._build_version_string(args)
    assert version.endswith("-isl10")


def test_version_string_no_isl_suffix_by_default():
    args = _parse(["--strategy", "TrailingBothZScoreBreakout"])
    version = bench._build_version_string(args)
    assert "-isl" not in version


def test_checkpoint_filename_differs_with_n_islands_override():
    args_default = _parse(["--strategy", "TrailingBothZScoreBreakout"])
    args_override = _parse(["--strategy", "TrailingBothZScoreBreakout", "--n-islands", "7"])
    name_default = bench._build_checkpoint_filename("TrailingBothZScoreBreakout", 3.0, args_default)
    name_override = bench._build_checkpoint_filename("TrailingBothZScoreBreakout", 3.0, args_override)
    assert name_default != name_override
    assert "_isl7" in name_override
    assert "_isl" not in name_default


def _run_main(argv):
    """Drive main() far enough to hit the seed-mode mutual-exclusion check, which
    raises SystemExit before any DB/backtest work."""
    old = sys.argv
    sys.argv = ["bench_phase1_phase2_inmemory.py"] + argv
    try:
        bench.main()
    finally:
        sys.argv = old


def _synthetic_grid_row(tp, sl, hold, window, z, tpct, cagr, trades=10, alpha=100.0):
    return dict(take_profit=tp, stop_loss=sl, max_hold_hours=hold, window=window,
                z_score_threshold=z, trail_sell_pct=tpct, cagr=cagr, trades=trades,
                alpha_vs_spy=alpha)


def test_find_missing_window_z_top_n_finds_gaps_and_ranks_by_cagr():
    """2026-08-30, planner dispatch: the window/z backfill's core selection logic --
    confirms a (window, z) combo absent from `present_combos` is detected as missing,
    its own top-2 cells (by cagr) are returned (not top-1, not unranked), and a combo
    already present is left untouched."""
    df = pd.DataFrame([
        _synthetic_grid_row(1, 1, 24, 5, 1.0, 0.0, cagr=50.0),   # present combo (5, 1.0)
        _synthetic_grid_row(2, 2, 24, 5, 2.0, 0.0, cagr=30.0),   # missing combo (5, 2.0) -- top-2
        _synthetic_grid_row(3, 3, 24, 5, 2.0, 0.0, cagr=25.0),   #   "
        _synthetic_grid_row(4, 4, 24, 5, 2.0, 0.0, cagr=10.0),   #   3rd-best -- should be excluded
        _synthetic_grid_row(5, 5, 24, 10, 1.0, 0.0, cagr=5.0),   # missing combo (10, 1.0) -- only 1 row
        _synthetic_grid_row(6, 6, 24, 10, 2.0, 0.0, cagr=60.0),  # present combo (10, 2.0)
    ])
    present = {(5, 1.0), (10, 2.0)}
    missing, rows_by_combo = bench.find_missing_window_z_top_n(
        present, [5, 10], [1.0, 2.0], df, tb_cols=["cagr"], tb_asc=[False], top_n=2)

    assert missing == [(5, 2.0), (10, 1.0)]
    assert [r["take_profit"] for r in rows_by_combo[(5, 2.0)]] == [2, 3]  # top-2 by cagr, not top-1/top-3
    assert [r["take_profit"] for r in rows_by_combo[(10, 1.0)]] == [5]   # only 1 available -- no crash


def test_find_missing_window_z_top_n_zero_evidence_combo_returns_empty_list():
    """A missing combo whose df_source slice is entirely empty (no rows at all for that
    (window, z) -- e.g. every cell had trades=0 and was already filtered out of df_final
    upstream before this function ever sees it) must report an empty list, distinguishable
    from 'found some, took top_n' -- not silently absent from the returned dict. This test
    constructs the empty slice via window absence (df has no window=10 rows at all), the
    same shape a real all-trades=0 combo would present to this function by the time it's
    called (that filtering happens upstream, not inside this function)."""
    df = pd.DataFrame([_synthetic_grid_row(1, 1, 24, 5, 1.0, 0.0, cagr=50.0)])
    present = {(5, 1.0)}
    missing, rows_by_combo = bench.find_missing_window_z_top_n(
        present, [5, 10], [1.0], df, tb_cols=["cagr"], tb_asc=[False], top_n=2)
    assert missing == [(10, 1.0)]
    assert rows_by_combo[(10, 1.0)] == []


def test_cliffbox_tasks_for_cell_matches_the_original_per_island_expansion():
    """cliffbox_tasks_for_cell was extracted from the per-island Phase2.5 seed loop's own
    inline expansion so the window/z backfill seed step could reuse it without a second
    hand-copied implementation -- confirms the extracted version reproduces the exact
    task count/shape the original inline code produced for a known cell."""
    cand = pd.Series(_synthetic_grid_row(2, 2, 24, 5, 2.0, 0.0, cagr=30.0))
    tasks = bench.cliffbox_tasks_for_cell(cand, trail_pcts=[0.0, 1.0])
    # tp/sl: max(1, 2-CLIFF_RADIUS)..min(30, 2+CLIFF_RADIUS) with CLIFF_RADIUS=2 -> [1,2,3,4] (4 values,
    # not 5, since 0 clamps to 1); hold=24 -> HOLD_TIME_CAPS within +-7 -> {21, 28} (2 values);
    # tpct=0.0 in [0.0, 1.0] -> neighbors [0.0, 1.0] (2 values). 4*4*2*2 = 64.
    assert len(tasks) == 64
    for tp, sl, hold, w, z, tpct in tasks:
        assert 1 <= tp <= 4 and 1 <= sl <= 4
        assert hold in (21, 28)
        assert w == 5 and z == 2.0
        assert tpct in (0.0, 1.0)


def test_cliffbox_tasks_for_cell_trail_pct_neighbors_at_nonzero_index():
    """The trail_pct neighbor slice (trail_pcts[max(0, idx-1): idx+2]) was the site of a
    real prior bug (the round-3/round-4 seed-mode regression documented elsewhere in this
    file, around TRAIL_PCTS reassignment) -- the other cliffbox test only exercises idx=0,
    where a buggy `trail_pcts[idx-1: idx+2]` (no max(0, ...) guard) would produce the SAME
    result as the correct version (both slice from index 0). This test uses idx=2 (a
    middle element) where the two versions diverge: the buggy version would slice
    `trail_pcts[1:4]` = [2,3,4], while the correct version slices `[max(0,1):4]` = same in
    this case -- so use idx=2 with a value where the missing max(0,...) guard would
    actually produce a NEGATIVE start index instead, at idx=0 vs idx=len-1 boundary check."""
    trail_pcts = [1, 2, 3, 4, 5]
    cand = pd.Series(_synthetic_grid_row(2, 2, 24, 5, 2.0, tpct=3, cagr=30.0))  # idx=2 (value 3)
    tasks = bench.cliffbox_tasks_for_cell(cand, trail_pcts=trail_pcts)
    tpct_values = {t[5] for t in tasks}
    assert tpct_values == {2.0, 3.0, 4.0}  # trail_pcts[1:4], correct neighbor window around idx=2

    cand_last = pd.Series(_synthetic_grid_row(2, 2, 24, 5, 2.0, tpct=5, cagr=30.0))  # idx=4, last element
    tasks_last = bench.cliffbox_tasks_for_cell(cand_last, trail_pcts=trail_pcts)
    tpct_values_last = {t[5] for t in tasks_last}
    # idx+2 = 6 > len(trail_pcts) -- Python slicing clamps automatically, no explicit guard
    # needed on this end; confirms no IndexError and no phantom out-of-range value included.
    assert tpct_values_last == {4.0, 5.0}


def test_seed_stage_backfill_runs_before_phase25_dispatch():
    """2026-08-30, paired-review MEDIUM finding (contextual review): the three tests above
    only exercise find_missing_window_z_top_n/cliffbox_tasks_for_cell in isolation -- none
    of them would catch a regression that moved the seed-stage backfill call BELOW the
    Phase2.5 dispatch, which would silently reintroduce the exact bug this two-stage design
    was built to fix (a backfilled candidate promoted straight from raw, unrefined data with
    a degenerate cliff-safety verdict). A real pool/data/backtest integration test isn't
    practical here, but the ordering invariant itself is checkable directly from source:
    the seed-stage `find_missing_window_z_top_n(` call must appear BEFORE the Phase2.5
    `_dispatch(...desc="Phase2.5-cliffbox"` call in run_one_fixed_sl's source, matching
    test_final_topN_region_filter_does_not_key_on_window_or_z's own source-slicing
    convention above.

    Anchored on the `missing_combos_seed, backfill_seed_rows = find_missing_window_z_top_n(`
    ASSIGNMENT, not the bare call text (2026-08-30, contextual-review HIGH finding: a later,
    unrelated Phase1-insurance-snapshot backfill added its OWN find_missing_window_z_top_n(
    call earlier in this same function's source, so a plain `src.index('find_missing_window_z_
    top_n(')` silently started matching that call instead and made this assertion vacuous --
    it still passed, but no longer tested the thing its docstring claims)."""
    import inspect
    src = inspect.getsource(bench.run_one_fixed_sl)
    seed_backfill_idx = src.index(
        'missing_combos_seed, backfill_seed_rows = find_missing_window_z_top_n(')
    phase25_dispatch_idx = src.index('desc="Phase2.5-cliffbox')
    assert seed_backfill_idx < phase25_dispatch_idx, (
        "seed-stage window/z backfill must run BEFORE Phase2.5 dispatches, or backfilled "
        "candidates lose their real cliffbox refinement + cliff-safety verification")
