"""Unit coverage for scripts/candidate_summary_report.py's Phase4 parallelism
(2026-08-31, Task #8 further follow-up -- this loop previously had ZERO
parallelism at all, confirmed by grep before this work started).

_run_one_gt_scope_worker is the picklable, top-level worker body a real
ProcessPoolExecutor dispatches -- tested directly here (in-process, not
through a real pool) since a real subprocess worker re-imports the module
fresh and can't observe a parent-process monkeypatch (same limitation noted
in tests/test_bench_dispatch_workers_budget.py). The generic throttle/
concurrency-cap logic this loop is wired through (campaign_registry.
run_throttled) already has its own dedicated tests in
tests/test_campaign_registry.py -- not re-duplicated here.

Return contract is (captured_output: str, rows, error: Exception|None) --
NOT (banner_lines, rows) -- paired-review CONFIRMED HIGH fixup (2026-08-31,
both independent reviewers): the worker's ENTIRE stdout (banner + gt_rows_
for_scope's own internal prints, including print_candidate_report_ground_
truth) must be captured as one atomic block, not just the 2-line banner.
`error` (same-day round-2 fixup, cold-review CONFIRMED MEDIUM): the real
work's own exceptions are now caught INSIDE the redirect_stdout block, so
whatever was printed before a failure survives instead of being discarded."""
import sqlite3

from scripts import candidate_summary_report as csr


def test_worker_backtest_cache_scope_calls_cross_check(monkeypatch, tmp_path):
    """grid_window=None -- the original backtest_cache-sourced path, includes the
    top_safe_nodes cross-check."""
    db_path = str(tmp_path / "scratch.db")
    sqlite3.connect(db_path).close()  # just needs to exist/be openable

    monkeypatch.setattr(csr, "gt_current_best_node", lambda conn, ticker, strategy, version,
                         entry_timing, fixed_sl, metric, min_alpha_arg: {
                             "arm_pct": 5, "sl": 3, "hold": 24, "window": 5, "z": 1.0,
                             "alpha": 12.3, "cagr": 45.6})
    monkeypatch.setattr(csr, "gt_rows_for_scope", lambda ticker, strategy, version, entry_timing,
                         fixed_sl, grid_window=None: [{"ticker": ticker}])

    output, rows, error = csr._run_one_gt_scope_worker(
        db_path, "GDXU", "TrailingBothZScoreBreakout", "v6.5-test", "open_check", 3.0, None,
        "robust_alpha", 200.0)

    assert isinstance(output, str)
    assert error is None
    assert "top_safe_nodes cross-check" in output and "robust_alpha=+12.3%" in output
    # banner (ticker/strategy/version identifying line) must precede the cross-check
    # line within the SAME captured block -- this is the actual point of capturing the
    # whole thing as one atomic unit instead of a deferred separate banner.
    assert output.index("GDXU / TrailingBothZScoreBreakout") < output.index("top_safe_nodes cross-check")
    assert rows == [{"ticker": "GDXU"}]


def test_worker_candidate_nodes_scope_skips_cross_check(monkeypatch, tmp_path):
    """grid_window set -- candidate_nodes fallback path, no backtest_cache cross-check
    equivalent (matches the original serial loop's own comment for why)."""
    db_path = str(tmp_path / "scratch.db")
    sqlite3.connect(db_path).close()

    called = {"gt_current_best_node": False}
    monkeypatch.setattr(csr, "gt_current_best_node",
                         lambda *a, **kw: called.__setitem__("gt_current_best_node", True))
    monkeypatch.setattr(csr, "gt_rows_for_scope", lambda ticker, strategy, version, entry_timing,
                         fixed_sl, grid_window=None: [{"ticker": ticker, "grid_window": grid_window}])

    output, rows, error = csr._run_one_gt_scope_worker(
        db_path, "GDXU", "TrailingBothZScoreBreakout", "v6.5-test", "open_check", 3.0, 15,
        "robust_alpha", 200.0)

    assert called["gt_current_best_node"] is False  # never called for a windowed scope
    assert error is None
    assert "skipped -- candidate_nodes-sourced scope" in output
    assert rows == [{"ticker": "GDXU", "grid_window": 15}]


def test_worker_no_cliff_safe_node_found(monkeypatch, tmp_path):
    db_path = str(tmp_path / "scratch.db")
    sqlite3.connect(db_path).close()

    monkeypatch.setattr(csr, "gt_current_best_node", lambda *a, **kw: None)
    monkeypatch.setattr(csr, "gt_rows_for_scope", lambda *a, **kw: [])

    output, rows, error = csr._run_one_gt_scope_worker(
        db_path, "GDXU", "TrailingBothZScoreBreakout", "v6.5-test", "open_check", 3.0, None,
        "robust_alpha", 200.0)

    assert error is None
    assert "no cliff-safe node found" in output
    assert rows == []


def test_worker_captures_gt_rows_for_scope_internal_prints(monkeypatch, tmp_path):
    """The actual regression guard for the CONFIRMED HIGH fix: gt_rows_for_scope's OWN
    internal print() calls (its '[GT candidate report] ...' progress lines) must land
    in the captured output too, not escape to the worker's raw stdout."""
    db_path = str(tmp_path / "scratch.db")
    sqlite3.connect(db_path).close()

    def fake_gt_rows_for_scope(ticker, strategy, version, entry_timing, fixed_sl, grid_window=None):
        print("  [GT candidate report] using candidate_nodes fallback (grid_window=15)")
        print("  [GT candidate report] pre-Phase4 trade-count floor: skipping 1 of 3 candidate(s)")
        return [{"ticker": ticker}]

    monkeypatch.setattr(csr, "gt_current_best_node", lambda *a, **kw: None)
    monkeypatch.setattr(csr, "gt_rows_for_scope", fake_gt_rows_for_scope)

    output, rows, error = csr._run_one_gt_scope_worker(
        db_path, "GDXU", "TrailingBothZScoreBreakout", "v6.5-test", "open_check", 3.0, 15,
        "robust_alpha", 200.0)

    assert error is None
    assert "using candidate_nodes fallback" in output
    assert "pre-Phase4 trade-count floor" in output
    assert rows == [{"ticker": "GDXU"}]


def test_worker_preserves_partial_output_on_real_error(monkeypatch, tmp_path):
    """The actual regression guard for the round-2 CONFIRMED MEDIUM fix: a scope that
    prints real diagnostics and THEN raises must still return everything it printed
    before the failure, not just the banner -- these diagnostics are often exactly
    what explains the crash."""
    db_path = str(tmp_path / "scratch.db")
    sqlite3.connect(db_path).close()

    def flaky_gt_rows_for_scope(ticker, strategy, version, entry_timing, fixed_sl, grid_window=None):
        print("  [GT candidate report] using candidate_nodes fallback (grid_window=15)")
        raise RuntimeError("simulated real-work failure")

    monkeypatch.setattr(csr, "gt_current_best_node", lambda *a, **kw: None)
    monkeypatch.setattr(csr, "gt_rows_for_scope", flaky_gt_rows_for_scope)

    output, rows, error = csr._run_one_gt_scope_worker(
        db_path, "GDXU", "TrailingBothZScoreBreakout", "v6.5-test", "open_check", 3.0, 15,
        "robust_alpha", 200.0)

    assert isinstance(error, RuntimeError)
    assert "simulated real-work failure" in str(error)
    # The diagnostic printed right before the crash is NOT lost.
    assert "using candidate_nodes fallback" in output
    assert rows == []
