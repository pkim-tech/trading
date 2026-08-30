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
