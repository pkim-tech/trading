"""CLI-arg-parsing / grid-construction tests for scripts/bench_phase1_phase2_inmemory.py.

Covers the 2026-08-29 additions:
  - new --z-thresholds flag (float, nargs="+", plain-replace of module Z_THRESHOLDS)
  - --window promoted to nargs="+" (multi-value = ONE pooled Phase1 axis, not N campaigns)
  - both flags added to the --seed-watch-list-id mutual-exclusivity list

Deliberately NO broker / stream / kernel mechanics and NO real backtest run --
argparse + the extracted apply_grid_overrides() helper only.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts import bench_phase1_phase2_inmemory as bench


@pytest.fixture(autouse=True)
def _restore_module_grid():
    """apply_grid_overrides() mutates module-level globals; the module is imported
    once and shared, so snapshot/restore around every test."""
    win, z = list(bench.WINDOWS), list(bench.Z_THRESHOLDS)
    yield
    bench.WINDOWS, bench.Z_THRESHOLDS = win, z


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


def test_z_thresholds_conflicts_with_seed_mode():
    with pytest.raises(SystemExit) as ei:
        _run_main(["--seed-watch-list-id", "999", "--z-thresholds", "0.5", "1"])
    assert "--z-thresholds" in str(ei.value)


def test_multi_window_conflicts_with_seed_mode():
    with pytest.raises(SystemExit) as ei:
        _run_main(["--seed-watch-list-id", "999", "--window", "5", "10"])
    assert "--window" in str(ei.value)


def _run_main(argv):
    """Drive main() far enough to hit the seed-mode mutual-exclusion check, which
    raises SystemExit before any DB/backtest work."""
    old = sys.argv
    sys.argv = ["bench_phase1_phase2_inmemory.py"] + argv
    try:
        bench.main()
    finally:
        sys.argv = old
