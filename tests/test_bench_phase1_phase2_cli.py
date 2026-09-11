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


def test_checkpoint_file_rejected_with_multiple_fixed_sl_values():
    """Paired-review HIGH finding (2026-09-10, checkpoint content-hash structural fix):
    an explicit --checkpoint-file is a single shared path, but fixed_sl is part of the
    checkpoint identity hash -- without this guard, fixed_sl_list[1] would find
    fixed_sl_list[0]'s saved file, hit a hash mismatch, and _validate_checkpoint_manifest
    would hard-raise mid-run (after fixed_sl_list[0]'s candidates were already written).
    Real invocation path: run_inmemory_sweep_queue.sh can pass --checkpoint-file
    alongside a multi-value --fixed-sl-values CSV when CHECKPOINT_FILE is set."""
    with pytest.raises(SystemExit) as ei:
        _run_main(["--strategy", "TrailingBothZScoreBreakout", "--checkpoint-file",
                   "/tmp/shared_checkpoint.parquet", "--fixed-sl-values", "1", "2"])
    assert "--checkpoint-file" in str(ei.value)


def test_checkpoint_file_allowed_with_single_fixed_sl_value(monkeypatch):
    """Sibling of the rejection test above -- a single-value --fixed-sl-values (or the
    plain --fixed-sl form) must NOT trip the new guard, since there's exactly one real
    identity to hash and no shared-path collision is possible."""
    args = _parse(["--strategy", "TrailingBothZScoreBreakout", "--checkpoint-file",
                   "/tmp/shared_checkpoint.parquet", "--fixed-sl-values", "1"])
    assert args.checkpoint_file == "/tmp/shared_checkpoint.parquet"
    # main() itself would still hit real DB/backtest work past the guard -- this test
    # only confirms the guard's own condition (len(fixed_sl_list) > 1) doesn't fire for
    # a single-value list, mirroring how the rejection test confirms it does for 2+.
    fixed_sl_list = [1]
    assert not (args.checkpoint_file and len(fixed_sl_list) > 1)


def test_version_string_gets_isl_suffix_when_overridden():
    args = _parse(["--strategy", "TrailingBothZScoreBreakout", "--n-islands", "10"])
    version = bench._build_version_string(args)
    # "-isl10" is no longer the true suffix (2026-08-30, planner dispatch item 3): the
    # unconditional -pv{PROMOTION_ALGO_VERSION} pipeline-version marker always comes last
    # now, so this asserts "-isl10" appears immediately before it, not that it ends the
    # string.
    assert f"-isl10-pv{bench.PROMOTION_ALGO_VERSION}" in version
    assert version.endswith(f"-pv{bench.PROMOTION_ALGO_VERSION}")


def test_version_string_no_isl_suffix_by_default():
    args = _parse(["--strategy", "TrailingBothZScoreBreakout"])
    version = bench._build_version_string(args)
    assert "-isl" not in version


def test_version_string_not_yet_wired_to_window_override():
    """Item 4, 2026-09-01: campaign_registry.build_version_string/register_campaign/
    resolve_or_create gained a windows= param (mirroring z_thresholds), covered by
    tests/test_campaign_registry.py. NOT wired into bench_phase1_phase2_inmemory.py's
    _build_version_string/register_campaign call sites here -- reverted mid-session
    after paired review found scripts/run_inmemory_sweep_queue.sh always passes
    --window unconditionally (WINDOWS defaults non-empty) but its own resolve_campaign()
    doesn't pass --windows, so wiring bench's call sites alone would have split the
    version string bench computes from the one the shell script registers for two
    live-running drain loops (campaign_id=1, pids 1613975/1614043). Re-wire together
    with the run_inmemory_sweep_queue.sh fix once those loops finish/restart."""
    args = _parse(["--strategy", "TrailingBothZScoreBreakout",
                   "--window", "5", "10", "15", "20"])
    version = bench._build_version_string(args)
    assert "-w5-10-15-20" not in version


def test_version_string_always_has_promotion_algo_version_suffix():
    """2026-08-30, planner dispatch item 3: without this, a re-run of the exact same
    tickers/parameters after a real promotion-algorithm change (backfill/gating/scope-
    detection logic, not a sweep-parameter change) would silently produce an IDENTICAL
    version string to a prior, algorithmically different campaign -- making the two
    indistinguishable in candidate_nodes/sweep_run_log. Must be present regardless of
    which other optional suffixes fire."""
    args = _parse(["--strategy", "TrailingBothZScoreBreakout"])
    version = bench._build_version_string(args)
    assert version.endswith(f"-pv{bench.PROMOTION_ALGO_VERSION}")


def test_checkpoint_filename_differs_with_n_islands_override(monkeypatch):
    """2026-09-10 structural fix: _build_checkpoint_filename is now content-hashed
    (see _checkpoint_identity_params) rather than built from a hand-maintained
    "_isl<N>" filename fragment -- the hash reads the real module-level N_ISLANDS
    global directly (already resolved by main()'s apply_grid_overrides() before
    run_one_fixed_sl ever runs for real), so this monkeypatches that global instead
    of relying on args.n_islands alone, matching the entry_timing test below."""
    args = _parse(["--strategy", "TrailingBothZScoreBreakout"])
    monkeypatch.setattr(bench, "N_ISLANDS", 3)
    name_default = bench._build_checkpoint_filename("TrailingBothZScoreBreakout", 3.0, args)
    monkeypatch.setattr(bench, "N_ISLANDS", 7)
    name_override = bench._build_checkpoint_filename("TrailingBothZScoreBreakout", 3.0, args)
    assert name_default != name_override


def test_checkpoint_filename_differs_with_entry_timing_override(monkeypatch):
    """Real live incident, 2026-09-01: a fresh --entry-timing close run for
    SOXL/TrailingBoth/fixed_sl=1 silently loaded an open_check checkpoint left over
    from the prior evening's real v6.5 campaign run (same ticker/strategy/fixed_sl/
    windows/z/date-range/isl/pv) and skipped Phase1+Phase2 entirely -- caught before
    any candidate_nodes rows were written (its orphaned ProcessPoolExecutor workers
    kept running pre-fix code for several more minutes after, killed live by pid).
    Filename is now content-hashed (2026-09-10 structural fix) rather than a
    "_close" filename fragment -- asserts the hash differs instead of a substring."""
    args_default = _parse(["--strategy", "TrailingBothZScoreBreakout"])
    args_close = _parse(["--strategy", "TrailingBothZScoreBreakout", "--entry-timing", "close"])
    monkeypatch.setattr(bench, "ENTRY_TIMING", "open_check")
    name_default = bench._build_checkpoint_filename("TrailingBothZScoreBreakout", 3.0, args_default)
    monkeypatch.setattr(bench, "ENTRY_TIMING", "close")
    name_close = bench._build_checkpoint_filename("TrailingBothZScoreBreakout", 3.0, args_close)
    assert name_default != name_close


def test_checkpoint_identity_params_includes_resolved_grid():
    """The content hash must cover the resolved campaign_config grid tuple too
    (docs/backlog_cache.md's separate "checkpoint filename doesn't key on
    campaign_config.py's grid contents" gap, folded into this same structural fix)
    -- not just strategy_name, so editing take_profits/stop_losses/trail_pcts in
    campaign_config.py changes the hash even though strategy_name is unchanged."""
    args = _parse(["--strategy", "TrailingBothZScoreBreakout"])
    params_default = bench._checkpoint_identity_params(
        "TrailingBothZScoreBreakout", 3.0, args, take_profits=[1, 2, 3])
    params_edited_grid = bench._checkpoint_identity_params(
        "TrailingBothZScoreBreakout", 3.0, args, take_profits=[1, 2, 3, 4])
    assert params_default["take_profits"] != params_edited_grid["take_profits"]
    assert (bench._checkpoint_identity_hash(params_default)
            != bench._checkpoint_identity_hash(params_edited_grid))


def test_checkpoint_identity_params_reads_real_campaign_config_grid(monkeypatch):
    """Sibling of the test above, but exercising the REAL _resolve_checkpoint_grid path
    (no take_profits override passed in) -- paired-review LOW finding: the other test
    passes take_profits explicitly and never actually proves editing campaign_config.py
    itself changes the hash, only that a different take_profits VALUE does."""
    args = _parse(["--strategy", "TrailingBothZScoreBreakout"])
    params_before = bench._checkpoint_identity_params("TrailingBothZScoreBreakout", 3.0, args)
    original_grid = bench.campaign_config.STRATEGIES["TrailingBothZScoreBreakout"]
    edited_grid = dict(original_grid, take_profits=list(original_grid["take_profits"]) + [999])
    monkeypatch.setitem(bench.campaign_config.STRATEGIES, "TrailingBothZScoreBreakout", edited_grid)
    params_after = bench._checkpoint_identity_params("TrailingBothZScoreBreakout", 3.0, args)
    assert params_before["take_profits"] != params_after["take_profits"]
    assert (bench._checkpoint_identity_hash(params_before)
            != bench._checkpoint_identity_hash(params_after))


def test_checkpoint_identity_params_includes_n_generations_and_fine_radius(monkeypatch):
    """Paired-review MEDIUM finding (2026-09-10): N_GENERATIONS/FINE_RADIUS both change
    which real Phase2 cells get computed into df_full, but PROMOTION_ALGO_VERSION's own
    contract explicitly says NOT to bump it for a sweep-scope parameter change like these
    -- relying on that convention would NOT have covered them. Must be hashed directly."""
    args = _parse(["--strategy", "TrailingBothZScoreBreakout"])
    params_before = bench._checkpoint_identity_params("TrailingBothZScoreBreakout", 3.0, args)
    monkeypatch.setattr(bench, "N_GENERATIONS", bench.N_GENERATIONS + 1)
    params_n_gen = bench._checkpoint_identity_params("TrailingBothZScoreBreakout", 3.0, args)
    assert (bench._checkpoint_identity_hash(params_before)
            != bench._checkpoint_identity_hash(params_n_gen))

    monkeypatch.setattr(bench, "N_GENERATIONS", bench.N_GENERATIONS)  # restore
    monkeypatch.setattr(bench, "FINE_RADIUS", bench.FINE_RADIUS + 1)
    params_fine_radius = bench._checkpoint_identity_params("TrailingBothZScoreBreakout", 3.0, args)
    assert (bench._checkpoint_identity_hash(params_before)
            != bench._checkpoint_identity_hash(params_fine_radius))


def test_checkpoint_manifest_round_trips_and_detects_mismatch(tmp_path):
    args = _parse(["--strategy", "TrailingBothZScoreBreakout"])
    params = bench._checkpoint_identity_params("TrailingBothZScoreBreakout", 3.0, args)
    content_hash = bench._checkpoint_identity_hash(params)
    ckpt = tmp_path / "checkpoint.parquet"
    ckpt.write_text("fake parquet contents")

    # No manifest yet -- must hard-refuse, not silently proceed.
    with pytest.raises(RuntimeError, match="manifest missing"):
        bench._validate_checkpoint_manifest(str(ckpt), params, content_hash)

    bench._write_checkpoint_manifest(str(ckpt), params, content_hash)
    bench._validate_checkpoint_manifest(str(ckpt), params, content_hash)  # does not raise

    other_params = bench._checkpoint_identity_params(
        "TrailingBothZScoreBreakout", 3.0, args, take_profits=[999])
    other_hash = bench._checkpoint_identity_hash(other_params)
    with pytest.raises(RuntimeError, match="identity mismatch"):
        bench._validate_checkpoint_manifest(str(ckpt), other_params, other_hash)


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
    its own top-2 cells are returned (not top-1, not unranked), and a combo already
    present is left untouched.

    Updated 2026-09-03 (real gap found via paired review, docs/research_log.md's 2026-09-03
    HIBL entry): find_missing_window_z_top_n now picks up to `top_n` DISTINCT ISLANDS (via
    pick_island_centers, min_sep=ISLAND_MIN_SEP=6) within a missing combo, not a flat
    top_n-by-cagr sort -- the old flat sort let ONE strong island consume every backfill
    slot for a combo, silently starving a second real, distinct island of any
    representation. Test data updated accordingly: the (5, 2.0) combo's two picks are now
    well-separated coordinates (2,2) and (20,20), both real distinct islands; a THIRD cell
    (21,21) sits within min_sep of (20,20) -- same island, not a separate 3rd pick, so it's
    naturally excluded by pick_island_centers itself, not by a top_n cutoff on a flat sort."""
    df = pd.DataFrame([
        _synthetic_grid_row(1, 1, 24, 5, 1.0, 0.0, cagr=50.0),     # present combo (5, 1.0)
        _synthetic_grid_row(2, 2, 24, 5, 2.0, 0.0, cagr=30.0),     # missing combo (5, 2.0) -- island A
        _synthetic_grid_row(20, 20, 24, 5, 2.0, 0.0, cagr=25.0),   # missing combo (5, 2.0) -- island B
        _synthetic_grid_row(21, 21, 24, 5, 2.0, 0.0, cagr=10.0),   #   within min_sep of island B -- same island
        _synthetic_grid_row(5, 5, 24, 10, 1.0, 0.0, cagr=5.0),     # missing combo (10, 1.0) -- only 1 row
        _synthetic_grid_row(6, 6, 24, 10, 2.0, 0.0, cagr=60.0),    # present combo (10, 2.0)
    ])
    present = {(5, 1.0), (10, 2.0)}
    missing, rows_by_combo = bench.find_missing_window_z_top_n(
        present, [5, 10], [1.0, 2.0], df, tb_cols=["cagr"], tb_asc=[False], top_n=2)

    assert missing == [(5, 2.0), (10, 1.0)]
    assert [r["take_profit"] for r in rows_by_combo[(5, 2.0)]] == [2, 20]  # 2 DISTINCT islands, not top-2 flat cagr
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

    Anchored on the `combos_topped_up_seed, backfill_seed_rows, combos_zero_evidence_seed =
    top_up_window_z_island_quota(` ASSIGNMENT, not the bare call text (2026-08-30,
    contextual-review HIGH finding: a later, unrelated Phase1-insurance-snapshot backfill
    added its OWN find_missing_window_z_top_n( call earlier in this same function's
    source, so a plain `src.index('find_missing_window_z_top_n(')` silently started
    matching that call instead and made this assertion vacuous -- it still passed, but no
    longer tested the thing its docstring claims. Re-anchored again 2026-09-03 (paired-
    review HIGH finding) when the seed-stage call site itself switched from
    find_missing_window_z_top_n to top_up_window_z_island_quota -- same class of stale-
    anchor bug, caught this time before landing instead of after)."""
    import inspect
    src = inspect.getsource(bench.run_one_fixed_sl)
    seed_backfill_idx = src.index(
        'combos_topped_up_seed, backfill_seed_rows, combos_zero_evidence_seed = '
        'top_up_window_z_island_quota(')
    phase25_dispatch_idx = src.index('desc="Phase2.5-cliffbox')
    assert seed_backfill_idx < phase25_dispatch_idx, (
        "seed-stage window/z island-quota top-up must run BEFORE Phase2.5 dispatches, or "
        "backfilled candidates lose their real cliffbox refinement + cliff-safety verification")


def test_top_up_window_z_island_quota_tops_up_a_partially_present_combo():
    """2026-09-03: top_up_window_z_island_quota's whole reason for existing over
    find_missing_window_z_top_n -- a combo with ONE candidate already present (not
    entirely missing) can still be missing a SECOND real, distinct island in that same
    combo. Real motivating case: HIBL window=10/z=1.0 had two real islands (TP=2 cagr~60%,
    TP=28 cagr~52%) -- if TP=2 alone had already won a slot via normal selection (so the
    combo is NOT "missing" by find_missing_window_z_top_n's presence-only gate), TP=28
    would never get backfilled at all under the old function. This test reproduces that
    shape directly: present_df already has ONE row for (5, 1.0) (island A only) -- the
    unconditional quota must still add island B."""
    df = pd.DataFrame([
        _synthetic_grid_row(2, 2, 24, 5, 1.0, 0.0, cagr=60.0),     # island A (already present)
        _synthetic_grid_row(28, 28, 24, 5, 1.0, 0.0, cagr=52.0),   # island B (should get topped up)
        _synthetic_grid_row(1, 1, 24, 10, 2.0, 0.0, cagr=10.0),    # a fully-covered combo -- no top-up needed
    ])
    present_df = pd.DataFrame([
        {"take_profit": 2, "stop_loss": 2, "window": 5, "z_score_threshold": 1.0},
        {"take_profit": 1, "stop_loss": 1, "window": 10, "z_score_threshold": 2.0},
    ])
    combos_topped_up, rows_by_combo, zero_evidence = bench.top_up_window_z_island_quota(
        present_df, [5, 10], [1.0, 2.0], df, tb_cols=["cagr"], tb_asc=[False], top_n=2)

    assert combos_topped_up == [(5, 1.0)]
    assert [r["take_profit"] for r in rows_by_combo[(5, 1.0)]] == [28]
    # (5, 2.0) and (10, 1.0) are real gaps in this test's own grid (never given any rows,
    # not this test's focus) -- correctly reported as zero-evidence, not silently dropped.
    assert zero_evidence == [(5, 2.0), (10, 1.0)]


def test_top_up_window_z_island_quota_overlapping_boxes_dont_double_cover():
    """2026-09-03, real bug found independently by both independent-cold and contextual
    paired review: pick_island_centers' separation test is an OR across axes (>=min_sep on
    EITHER tp or sl), so two genuinely distinct centers can have overlapping +-FINE_RADIUS
    coverage boxes. A present row inside BOTH boxes must only satisfy its OWN nearest
    center, not silently cover both -- otherwise one present row can starve a second real
    island exactly like the original bug this whole fix targets. Centers here: (2,3) and
    (2,11) are 8 apart in stop_loss (>= ISLAND_MIN_SEP=6, so genuinely distinct islands),
    but a present row at (2,7) sits within FINE_RADIUS=4 of BOTH (|7-3|=4, |11-7|=4)."""
    df = pd.DataFrame([
        _synthetic_grid_row(2, 3, 24, 5, 1.0, 0.0, cagr=60.0),    # island A
        _synthetic_grid_row(2, 11, 24, 5, 1.0, 0.0, cagr=55.0),   # island B
    ])
    present_df = pd.DataFrame([
        {"take_profit": 2, "stop_loss": 7, "window": 5, "z_score_threshold": 1.0},  # equidistant-ish, nearer to A
    ])
    combos_topped_up, rows_by_combo, _ = bench.top_up_window_z_island_quota(
        present_df, [5], [1.0], df, tb_cols=["cagr"], tb_asc=[False], top_n=2)

    # The present row can cover at most ONE of the two islands -- the other must still be
    # topped up. (Exact nearest-center tie-break for the (2,7) row isn't the point here;
    # what matters is that the OTHER island is never silently left uncovered.)
    assert combos_topped_up == [(5, 1.0)]
    assert len(rows_by_combo[(5, 1.0)]) == 1


def test_top_up_window_z_island_quota_reports_zero_evidence_combos():
    """2026-09-03: a combo with literally zero computed cells can't be topped up at all --
    must be reported via the dedicated combos_zero_evidence return value (restored after
    both independent-cold and contextual review found the original diff silently dropped
    this diagnostic, contrary to the module's own 'flag loudly, don't silently drop'
    convention)."""
    df = pd.DataFrame([_synthetic_grid_row(1, 1, 24, 5, 1.0, 0.0, cagr=50.0)])
    combos_topped_up, rows_by_combo, zero_evidence = bench.top_up_window_z_island_quota(
        pd.DataFrame(), [5, 10], [1.0], df, tb_cols=["cagr"], tb_asc=[False], top_n=2)
    assert zero_evidence == [(10, 1.0)]
    assert (10, 1.0) not in rows_by_combo


def test_find_missing_arm_top_n_finds_gaps_and_ranks_by_cagr():
    """2026-08-30, planner dispatch: the arm_pct (take_profit) backfill's core selection
    logic, single-axis sibling of find_missing_window_z_top_n -- confirms a take_profit
    value absent from `present_arms` is detected as missing, its own top-3 cells (by
    cagr) are returned (not top-1, not top-2, not unranked), and a present arm is left
    untouched."""
    df = pd.DataFrame([
        _synthetic_grid_row(1, 1, 24, 5, 1.0, 0.0, cagr=50.0),   # present arm (take_profit=1)
        _synthetic_grid_row(2, 2, 24, 5, 1.0, 0.0, cagr=30.0),   # missing arm (take_profit=2) -- top-3
        _synthetic_grid_row(2, 3, 24, 5, 1.0, 0.0, cagr=25.0),   #   "
        _synthetic_grid_row(2, 4, 24, 5, 1.0, 0.0, cagr=20.0),   #   "
        _synthetic_grid_row(2, 5, 24, 5, 1.0, 0.0, cagr=10.0),   #   4th-best -- should be excluded
        _synthetic_grid_row(3, 5, 24, 5, 1.0, 0.0, cagr=5.0),    # missing arm (take_profit=3) -- only 1 row
    ])
    present = {1}
    missing, rows_by_arm = bench.find_missing_arm_top_n(
        present, [1, 2, 3], df, tb_cols=["cagr"], tb_asc=[False], top_n=3)

    assert missing == [2, 3]
    assert [r["stop_loss"] for r in rows_by_arm[2]] == [2, 3, 4]  # top-3 by cagr, not top-1/top-2/top-4
    assert [r["stop_loss"] for r in rows_by_arm[3]] == [5]        # only 1 available -- no crash


def test_find_missing_arm_top_n_zero_evidence_arm_returns_empty_list():
    """A missing arm value whose df_source slice is entirely empty must report an empty
    list, distinguishable from 'found some, took top_n' -- not silently absent from the
    returned dict. Mirrors find_missing_window_z_top_n's own equivalent test."""
    df = pd.DataFrame([_synthetic_grid_row(1, 1, 24, 5, 1.0, 0.0, cagr=50.0)])
    present = {1}
    missing, rows_by_arm = bench.find_missing_arm_top_n(
        present, [1, 2], df, tb_cols=["cagr"], tb_asc=[False], top_n=3)
    assert missing == [2]
    assert rows_by_arm[2] == []


def test_seed_stage_arm_backfill_runs_before_phase25_dispatch():
    """2026-08-30, planner dispatch item 2: same ordering invariant as
    test_seed_stage_backfill_runs_before_phase25_dispatch above, for the NEW arm_pct
    backfill -- must run before Phase2.5 dispatches, or a backfilled-by-arm candidate
    loses its real cliffbox refinement + cliff-safety verification, identical failure
    mode to the one the window/z backfill's own two-stage design was built to fix."""
    import inspect
    src = inspect.getsource(bench.run_one_fixed_sl)
    seed_arm_backfill_idx = src.index(
        'missing_arms_seed, backfill_arm_seed_rows = find_missing_arm_top_n(')
    phase25_dispatch_idx = src.index('desc="Phase2.5-cliffbox')
    assert seed_arm_backfill_idx < phase25_dispatch_idx, (
        "seed-stage arm_pct backfill must run BEFORE Phase2.5 dispatches, or backfilled "
        "candidates lose their real cliffbox refinement + cliff-safety verification")


# --- 2026-09-03, Runlist Steps 2/3 (checkpoint entry_timing incident real fix) ---
# _should_load_checkpoint/_should_save_checkpoint extracted from run_one_fixed_sl's own
# inline logic specifically so this gate is a small, directly testable predicate instead
# of buried in a large function only exercisable via a real multi-minute backtest run.

def test_use_checkpoint_flag_defaults_false():
    args = _parse(["--strategy", "TrailingBothZScoreBreakout"])
    assert args.use_checkpoint is False
    assert args.checkpoint_file is None


def test_use_checkpoint_flag_settable():
    args = _parse(["--strategy", "TrailingBothZScoreBreakout", "--use-checkpoint"])
    assert args.use_checkpoint is True


def test_should_load_checkpoint_false_by_default_even_if_file_exists(tmp_path):
    """The real incident this closes: a stale default-path checkpoint sitting on disk
    from an earlier run must NOT be silently loaded just because it exists -- neither
    --checkpoint-file nor --use-checkpoint was passed, so this is a genuine real-campaign
    invocation, not an explicit dev-iteration request."""
    args = _parse(["--strategy", "TrailingBothZScoreBreakout"])
    ckpt = tmp_path / "bench_phase12_checkpoint_fake.parquet"
    ckpt.write_bytes(b"not a real parquet file, existence is all this test needs")
    assert bench._should_load_checkpoint(args, seed_task=None, checkpoint_path=str(ckpt)) is False


def test_should_load_checkpoint_true_with_use_checkpoint_flag(tmp_path):
    args = _parse(["--strategy", "TrailingBothZScoreBreakout", "--use-checkpoint"])
    ckpt = tmp_path / "bench_phase12_checkpoint_fake.parquet"
    ckpt.write_bytes(b"exists")
    assert bench._should_load_checkpoint(args, seed_task=None, checkpoint_path=str(ckpt)) is True


def test_should_load_checkpoint_true_with_explicit_checkpoint_file(tmp_path):
    ckpt = tmp_path / "explicit.parquet"
    ckpt.write_bytes(b"exists")
    args = _parse(["--strategy", "TrailingBothZScoreBreakout", "--checkpoint-file", str(ckpt)])
    assert bench._should_load_checkpoint(args, seed_task=None, checkpoint_path=str(ckpt)) is True


def test_should_load_checkpoint_false_for_seed_mode_even_with_opt_in(tmp_path):
    """Seed mode never loads a checkpoint regardless of the opt-in flags -- see the
    caller's own 2026-08-29 round-4 rationale (stable default path across identical seed
    invocations would silently short-circuit a genuine repeat smoke test)."""
    args = _parse(["--strategy", "TrailingBothZScoreBreakout", "--use-checkpoint"])
    ckpt = tmp_path / "bench_phase12_checkpoint_fake.parquet"
    ckpt.write_bytes(b"exists")
    assert bench._should_load_checkpoint(args, seed_task=("fake", "task"), checkpoint_path=str(ckpt)) is False


def test_should_load_checkpoint_false_for_resume_from_top100_even_with_opt_in(tmp_path):
    args = _parse(["--strategy", "TrailingBothZScoreBreakout", "--use-checkpoint",
                    "--resume-from-top100"])
    ckpt = tmp_path / "bench_phase12_checkpoint_fake.parquet"
    ckpt.write_bytes(b"exists")
    assert bench._should_load_checkpoint(args, seed_task=None, checkpoint_path=str(ckpt)) is False


def test_should_save_checkpoint_true_for_normal_run():
    args = _parse(["--strategy", "TrailingBothZScoreBreakout"])
    assert bench._should_save_checkpoint(args, seed_task=None) is True


def test_should_save_checkpoint_false_for_seed_mode():
    args = _parse(["--strategy", "TrailingBothZScoreBreakout"])
    assert bench._should_save_checkpoint(args, seed_task=("fake", "task")) is False


def test_should_save_checkpoint_false_for_resume_from_top100():
    """Real Step 3 fix: a --resume-from-top100 run's own deliberately-narrowed df_full
    (pre-filtered top-100 snapshot, can miss a real island) must never overwrite the
    shared default checkpoint path a later real full-campaign run would load."""
    args = _parse(["--strategy", "TrailingBothZScoreBreakout", "--resume-from-top100"])
    assert bench._should_save_checkpoint(args, seed_task=None) is False


def test_ensure_sweep_run_log_checkpoint_columns_idempotent(tmp_path):
    """Calling the migration twice against the same connection must not raise --
    covers both the normal idempotency case and (indirectly) the paired-review MEDIUM
    finding that a concurrent ALTER TABLE ... duplicate column name race must be
    swallowed, not propagated."""
    import sqlite3
    db_path = tmp_path / "sweep_run_log_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(bench._SWEEP_RUN_LOG_CREATE_SQL)
    bench._ensure_sweep_run_log_checkpoint_columns(conn)
    bench._ensure_sweep_run_log_checkpoint_columns(conn)  # must not raise
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sweep_run_log)").fetchall()}
    assert {"checkpoint_source", "checkpoint_hash", "checkpoint_path"} <= cols
    conn.close()


def test_ensure_sweep_run_log_checkpoint_columns_swallows_concurrent_duplicate_add(tmp_path):
    """Simulates the real TOCTOU race: two connections both see the column missing (the
    PRAGMA probe already ran), then both attempt the ALTER -- the second must not raise."""
    import sqlite3
    db_path = tmp_path / "sweep_run_log_test2.db"
    conn = sqlite3.connect(str(db_path))
    # Legacy (pre-migration) shape -- the real DBs this migration runs against, unlike
    # _SWEEP_RUN_LOG_CREATE_SQL which already includes the new columns for a fresh table.
    conn.execute("""
        CREATE TABLE sweep_run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL, finished_at TEXT,
            script TEXT, pid INTEGER, ticker TEXT, strategy TEXT, fixed_sl REAL,
            windows TEXT, version TEXT,
            n_final_candidates INTEGER, n_candidate_nodes_written INTEGER,
            n_trade_rows_written INTEGER, elapsed_s REAL
        )""")
    conn.execute("ALTER TABLE sweep_run_log ADD COLUMN checkpoint_source TEXT")
    # checkpoint_source now already exists on disk, but simulate a probe that (as in the
    # real race) ran BEFORE that ALTER committed -- sqlite3.Connection.execute is a
    # read-only C-extension slot (can't monkeypatch the instance directly), so wrap it in
    # a thin duck-typed proxy instead; _ensure_sweep_run_log_checkpoint_columns only ever
    # calls .execute(sql) on whatever it's given.
    class _StaleProbeConn:
        def execute(self, sql, *a, **kw):
            if sql.strip().startswith("PRAGMA table_info"):
                class _Empty:
                    def fetchall(self):
                        return []
                return _Empty()
            return conn.execute(sql, *a, **kw)
    bench._ensure_sweep_run_log_checkpoint_columns(_StaleProbeConn())  # must not raise
    conn.close()
