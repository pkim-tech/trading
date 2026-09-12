"""Unit coverage for scripts/compute_expected_bench_version.py, built 2026-09-12
after a real live incident: launching run_inmemory_sweep_queue.sh in
ATTACH_CAMPAIGN_ID mode without also setting WINDOW_START/WINDOW_END silently
computed a DIFFERENT campaign's version string (matching an unrelated,
already-finished campaign's window by coincidence), false-"already done"-
skipping all 16 real fixed_sl runs in ~90 seconds with zero real work done.

These tests pin the exact reproduction of that incident (test_missing_window_
override_silently_matches_a_different_campaign) plus the corrected case, so a
future change to this comparison logic can't reintroduce the same silent
collision without a test failing."""
from scripts.compute_expected_bench_version import compute_expected_version


def test_missing_window_override_silently_matches_a_different_campaign():
    """Reproduces the real 2026-09-12 incident exactly: campaign_22's real
    window is 2021-08-23..2026-05-23, but omitting window_start/window_end
    (as the buggy launch command did) silently falls back to bench's module
    default window (2021-08-23..2026-08-21) -- which happens to be campaign_
    18's real, already-fully-swept window. The computed version is therefore
    IDENTICAL to campaign_18's, not campaign_22's -- this is the actual
    mechanism of the false "already done" skip, not a hypothetical."""
    computed = compute_expected_version(
        z_thresholds="0.5 1.0 1.5 2.0", n_islands=3, campaign_label="v6.5.2",
    )
    assert computed == (
        "v6.5.2-bench-inmemory-v6-massive-w2021-08-23_2026-08-21-"
        "z0.5-1.0-1.5-2.0-isl3-pv4"
    )


def test_explicit_window_override_produces_the_real_campaign_22_version():
    """The corrected launch (WINDOW_START/WINDOW_END both set) produces
    campaign_22's real registered version -- confirmed byte-identical against
    `campaign_registry.py status --campaign-id 22`'s own output the night of
    the incident."""
    computed = compute_expected_version(
        z_thresholds="0.5 1.0 1.5 2.0", n_islands=3, campaign_label="v6.5.2",
        window_start="2021-08-23", window_end="2026-05-23",
    )
    assert computed == (
        "v6.5.2-bench-inmemory-v6-massive-w2021-08-23_2026-05-23-"
        "z0.5-1.0-1.5-2.0-isl3-pv4"
    )


def test_only_one_of_window_start_or_end_still_falls_back_for_the_other():
    """Both-or-neither is validated elsewhere (run_inmemory_sweep_queue.sh's
    own script-start check) -- this function itself has no opinion, it just
    mirrors bench's per-field fallback exactly, so a caller bug that only sets
    one of the two doesn't get masked by this function silently doing
    something smarter than bench itself would."""
    only_start = compute_expected_version(
        z_thresholds="0.5 1.0 1.5 2.0", n_islands=3, window_start="2021-08-23",
    )
    only_end = compute_expected_version(
        z_thresholds="0.5 1.0 1.5 2.0", n_islands=3, window_end="2026-05-23",
    )
    assert "2026-08-21" in only_start  # END fell back to bench's module default
    assert "2021-08-23" in only_end    # START fell back to bench's module default


def test_campaign_label_falls_back_to_bench_module_default_when_unset():
    from scripts import bench_phase1_phase2_inmemory as bench
    computed = compute_expected_version(z_thresholds="0.5 1.0 1.5 2.0", n_islands=3)
    assert computed.startswith(bench.CAMPAIGN_LABEL + "-")


def test_n_islands_and_z_thresholds_always_affect_the_version():
    """Mirrors run_inmemory_sweep_queue.sh's own per-job dispatch: --z-thresholds
    and --n-islands are ALWAYS passed explicitly (never conditionally omitted
    like --campaign-label/--entry-timing/window args), so they always show up
    in the version string -- never silently defaulted the way the window flags
    were in the real incident."""
    v3 = compute_expected_version(z_thresholds="1.0 2.0", n_islands=3)
    v10 = compute_expected_version(z_thresholds="1.0 2.0", n_islands=10)
    assert "-isl3-" in v3
    assert "-isl10-" in v10
    assert v3 != v10

    v_z1 = compute_expected_version(z_thresholds="1.0", n_islands=3)
    v_z2 = compute_expected_version(z_thresholds="1.0 2.0", n_islands=3)
    assert v_z1 != v_z2


def test_entry_timing_override_changes_the_version():
    default_v = compute_expected_version(z_thresholds="1.0", n_islands=3)
    close_v = compute_expected_version(z_thresholds="1.0", n_islands=3, entry_timing="close")
    assert default_v != close_v
