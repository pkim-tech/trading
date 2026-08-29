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

    # Phase1 task construction pools z x window into a single flat list (mirrors the
    # `for z in Z_THRESHOLDS for w in WINDOWS` cross-product at ~line 916). With the
    # AGQ widened grid that is 4 z x 4 w = 16 distinct cells, all in one list.
    bench.Z_THRESHOLDS = [0.5, 1.0, 1.5, 2.0]
    cells = [(w, z) for z in bench.Z_THRESHOLDS for w in bench.WINDOWS]
    assert len(cells) == 16
    assert len(set(cells)) == 16
    assert {w for w, _ in cells} == {5, 10, 15, 20}
    assert {z for _, z in cells} == {0.5, 1.0, 1.5, 2.0}


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
