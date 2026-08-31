"""Unit coverage for scripts/campaign_registry.py (Task #8, 2026-08-31):
single source of truth for the in-memory GT pipeline's version string, plus
the campaign_jobs queue (enqueue/claim_next/mark_finished/skip_remaining) and
status board. Every test uses an isolated tmp_path DB -- never the real
cache/research/trading_universe.db."""
import sqlite3

import pytest

from scripts import campaign_registry as reg


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_campaign_registry.db")


def test_build_version_string_matches_bench_phase1_phase2_inmemory_shape():
    """Same shape bench_phase1_phase2_inmemory.py's own tests assert on:
    -isl suffix right before -pv, absent by default, -pv always present."""
    v_default = reg.build_version_string("v6.5", 4, "massive", "2021-08-23", "2026-08-21")
    assert "-isl" not in v_default
    assert v_default.endswith("-pv4")
    assert v_default.startswith("v6.5-bench-inmemory-v6-massive")

    v_isl = reg.build_version_string("v6.5", 4, "massive", "2021-08-23", "2026-08-21",
                                      n_islands=10)
    assert "-isl10-pv4" in v_isl
    assert v_isl.endswith("-pv4")


def test_build_version_string_z_and_seed_suffixes():
    v = reg.build_version_string("v6.5", 4, "massive", "2021-08-23", "2026-08-21",
                                  z_thresholds=[0.5, 1.0, 1.5, 2.0], seed_watch_list_id=19)
    assert "-z0.5-1.0-1.5-2.0-seed19-pv4" in v


def test_build_version_string_no_label_omits_prefix():
    v = reg.build_version_string(None, 4, "yahoo", "2021-08-23", "2026-08-21")
    assert v.startswith("bench-inmemory-v6-")
    assert "-massive" not in v


def test_register_campaign_idempotent_on_version_string(db_path):
    v = reg.build_version_string("v6.5", 4, "massive", "2021-08-23", "2026-08-21")
    id1 = reg.register_campaign(v, "v6.5", 4, "massive", "2021-08-23", "2026-08-21",
                                 db_path=db_path)
    id2 = reg.register_campaign(v, "v6.5", 4, "massive", "2021-08-23", "2026-08-21",
                                 db_path=db_path)
    assert id1 == id2
    with sqlite3.connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    assert n == 1


def test_resolve_or_create_same_params_returns_same_id_and_version(db_path):
    """The actual split-brain fix under test: two independent callers with
    IDENTICAL real inputs (what bench_phase1_phase2_inmemory.py and
    run_inmemory_sweep_queue.sh each pass) must resolve to the exact same
    version string and campaign row."""
    id1, v1 = reg.resolve_or_create("v6.5", 4, "massive", "2021-08-23", "2026-08-21",
                                     z_thresholds=[0.5, 1.0, 1.5, 2.0], n_islands=3,
                                     db_path=db_path)
    id2, v2 = reg.resolve_or_create("v6.5", 4, "massive", "2021-08-23", "2026-08-21",
                                     z_thresholds=[0.5, 1.0, 1.5, 2.0], n_islands=3,
                                     db_path=db_path)
    assert id1 == id2
    assert v1 == v2


def test_resolve_or_create_different_params_get_different_versions(db_path):
    """The actual historical bug: a PROMOTION_ALGO_VERSION bump mid-campaign
    must produce a genuinely distinct version string, not silently collide."""
    id_pv3, v_pv3 = reg.resolve_or_create("v6.5", 3, "massive", "2021-08-23", "2026-08-21",
                                           n_islands=3, db_path=db_path)
    id_pv4, v_pv4 = reg.resolve_or_create("v6.5", 4, "massive", "2021-08-23", "2026-08-21",
                                           n_islands=3, db_path=db_path)
    assert id_pv3 != id_pv4
    assert v_pv3 != v_pv4
    assert v_pv3.endswith("-pv3")
    assert v_pv4.endswith("-pv4")


def test_enqueue_and_claim_next_fifo(db_path):
    cid, _ = reg.resolve_or_create("v6.5", 4, "massive", "2021-08-23", "2026-08-21",
                                    db_path=db_path)
    reg.enqueue(cid, "GDXU", "TrailingBothZScoreBreakout", "1,2,3,4,5,6,7,8", db_path=db_path)
    reg.enqueue(cid, "GDXU", "TrailingExitZScoreBreakout", "1,2,3,4,5,6,7,8", db_path=db_path)

    first = reg.claim_next(cid, db_path=db_path)
    assert first['ticker'] == "GDXU"
    assert first['strategy'] == "TrailingBothZScoreBreakout"

    second = reg.claim_next(cid, db_path=db_path)
    assert second['strategy'] == "TrailingExitZScoreBreakout"

    assert reg.claim_next(cid, db_path=db_path) is None


def test_claim_next_scoped_to_campaign(db_path):
    """A campaign_id filter must not claim another campaign's queued job."""
    cid_a, _ = reg.resolve_or_create("v6.5", 4, "massive", "2021-08-23", "2026-08-21",
                                      db_path=db_path)
    cid_b, _ = reg.resolve_or_create("v6.5", 5, "massive", "2021-08-23", "2026-08-21",
                                      db_path=db_path)
    reg.enqueue(cid_b, "SOXL", "TrailingBothZScoreBreakout", "1", db_path=db_path)
    assert reg.claim_next(cid_a, db_path=db_path) is None
    job = reg.claim_next(cid_b, db_path=db_path)
    assert job['ticker'] == "SOXL"


def test_claim_next_never_returns_same_job_twice(db_path):
    """Simulates the real concurrency guarantee: once claimed, a job is
    'running' and cannot be claimed again by a second caller."""
    cid, _ = reg.resolve_or_create("v6.5", 4, "massive", "2021-08-23", "2026-08-21",
                                    db_path=db_path)
    reg.enqueue(cid, "GDXU", "TrailingBothZScoreBreakout", "1", db_path=db_path)
    job = reg.claim_next(cid, db_path=db_path)
    assert job is not None
    assert reg.claim_next(cid, db_path=db_path) is None


def test_mark_finished_records_status_and_rc(db_path):
    cid, _ = reg.resolve_or_create("v6.5", 4, "massive", "2021-08-23", "2026-08-21",
                                    db_path=db_path)
    job_id = reg.enqueue(cid, "GDXU", "TrailingBothZScoreBreakout", "1", db_path=db_path)
    reg.claim_next(cid, db_path=db_path)
    reg.mark_finished(job_id, 0, db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT status, rc FROM campaign_jobs WHERE id=?", (job_id,)).fetchone()
    assert row == ('done', 0)

    job_id2 = reg.enqueue(cid, "GDXU", "TrailingExitZScoreBreakout", "1", db_path=db_path)
    reg.claim_next(cid, db_path=db_path)
    reg.mark_finished(job_id2, 1, db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        row2 = conn.execute("SELECT status, rc FROM campaign_jobs WHERE id=?", (job_id2,)).fetchone()
    assert row2 == ('failed', 1)


def test_skip_remaining_only_touches_queued_rows_for_that_ticker(db_path):
    """Matches run_inmemory_sweep_queue.sh's real behavior: a failed ticker
    abandons its OWN remaining queued jobs, but must not touch another
    ticker's queued jobs or an already-running/done job."""
    cid, _ = reg.resolve_or_create("v6.5", 4, "massive", "2021-08-23", "2026-08-21",
                                    db_path=db_path)
    j_gdxu_1 = reg.enqueue(cid, "GDXU", "TrailingBothZScoreBreakout", "1", db_path=db_path)
    j_gdxu_2 = reg.enqueue(cid, "GDXU", "TrailingExitZScoreBreakout", "1", db_path=db_path)
    j_soxl = reg.enqueue(cid, "SOXL", "TrailingBothZScoreBreakout", "1", db_path=db_path)

    claimed = reg.claim_next(cid, db_path=db_path)  # claims j_gdxu_1 -> running
    assert claimed['id'] == j_gdxu_1
    reg.mark_finished(j_gdxu_1, 1, db_path=db_path)  # failed

    n = reg.skip_remaining(cid, "GDXU", db_path=db_path)
    assert n == 1  # only j_gdxu_2 was still queued

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = {r['id']: r['status'] for r in conn.execute("SELECT id, status FROM campaign_jobs")}
    assert rows[j_gdxu_1] == 'failed'
    assert rows[j_gdxu_2] == 'skipped'
    assert rows[j_soxl] == 'queued'  # untouched


def test_status_reports_job_counts_and_running_jobs(db_path):
    cid, _ = reg.resolve_or_create("v6.5", 4, "massive", "2021-08-23", "2026-08-21",
                                    db_path=db_path)
    reg.enqueue(cid, "GDXU", "TrailingBothZScoreBreakout", "1", db_path=db_path)
    reg.enqueue(cid, "SOXL", "TrailingBothZScoreBreakout", "1", db_path=db_path)
    reg.claim_next(cid, db_path=db_path)  # GDXU now running

    rows = reg.status(campaign_id=cid, db_path=db_path)
    assert len(rows) == 1
    assert rows[0]['campaign']['id'] == cid
    assert rows[0]['job_counts'] == {'queued': 1, 'running': 1}
    assert len(rows[0]['running']) == 1
    assert rows[0]['running'][0]['ticker'] == 'GDXU'


def test_status_empty_for_unknown_campaign(db_path):
    reg.ensure_tables(db_path)
    assert reg.status(campaign_id=999, db_path=db_path) == []


def test_version_string_parity_with_bench_module(monkeypatch):
    """The actual regression guard for the incident this module exists to fix
    (paired review, 2026-08-31, cold reviewer's finding #10): if
    bench_phase1_phase2_inmemory.py's version-string INPUTS (CAMPAIGN_LABEL/
    PROMOTION_ALGO_VERSION/DATA_SOURCE/START/END) ever drift out of sync with
    what run_inmemory_sweep_queue.sh's resolve_campaign() reads via the same
    module import, this test catches it -- both sides must resolve to the
    EXACT same string for the exact same real production args."""
    from scripts import bench_phase1_phase2_inmemory as bench

    args = bench.build_arg_parser().parse_args(
        ["--strategy", "TrailingBothZScoreBreakout",
         "--z-thresholds", "0.5", "1.0", "1.5", "2.0", "--n-islands", "3"])
    bench.apply_grid_overrides(args)
    bench_version = bench._build_version_string(args)

    # Same inputs a real `resolve_campaign()` shell call would read off the
    # live bench module (CAMPAIGN_LABEL/PROMOTION_ALGO_VERSION/DATA_SOURCE/
    # START/END) and pass to `campaign_registry.py create`.
    registry_version = reg.build_version_string(
        label=bench.CAMPAIGN_LABEL, promotion_algo_version=bench.PROMOTION_ALGO_VERSION,
        data_source=bench.DATA_SOURCE, window_start=bench.START, window_end=bench.END,
        z_thresholds=bench.Z_THRESHOLDS, n_islands=args.n_islands)

    assert bench_version == registry_version


def test_claim_next_cli_exits_2_on_empty_queue_not_1(db_path, monkeypatch):
    """CLI-level regression guard (paired review, 2026-08-31, CONFIRMED HIGH):
    'queue empty' must be a DIFFERENT exit code from a real error, or the
    shell's `|| break` can't tell an unattended campaign finishing early (an
    empty queue) apart from claim-next crashing mid-drain."""
    import sys

    monkeypatch.setattr(sys, "argv", ["campaign_registry.py", "claim-next"])
    monkeypatch.setattr(reg, "DB_PATH", db_path)
    with pytest.raises(SystemExit) as exc_info:
        reg.main()
    assert exc_info.value.code == 2


def test_pause_resume_round_trip(db_path):
    cid, _v = reg.resolve_or_create("v6.5", 4, "massive", "2021-08-23", "2026-08-21",
                                     db_path=db_path)
    assert reg.is_paused(cid, db_path=db_path) is False

    assert reg.set_paused(cid, True, db_path=db_path) is True
    assert reg.is_paused(cid, db_path=db_path) is True

    assert reg.set_paused(cid, False, db_path=db_path) is True
    assert reg.is_paused(cid, db_path=db_path) is False

    assert reg.set_paused(999999, True, db_path=db_path) is False


def test_is_paused_fails_safe_on_unreadable_db(tmp_path):
    """Same fail-safe direction as get_workers_budget -- a broken pause check must
    never accidentally halt a real campaign."""
    assert reg.is_paused(1, db_path=str(tmp_path)) is False


def test_claim_next_refuses_when_paused_distinct_from_empty(db_path, monkeypatch, capsys):
    """The actual regression guard for the two-controls design: paused (exit 3) must
    NOT be conflated with genuinely empty (exit 2) even though a real job IS queued."""
    import sys

    cid, _v = reg.resolve_or_create("v6.5", 4, "massive", "2021-08-23", "2026-08-21",
                                     db_path=db_path)
    reg.enqueue(cid, "GDXU", "TrailingBothZScoreBreakout", "1,2,3,4,5,6,7,8", db_path=db_path)
    reg.set_paused(cid, True, db_path=db_path)
    monkeypatch.setattr(reg, "DB_PATH", db_path)

    monkeypatch.setattr(sys, "argv", ["campaign_registry.py", "claim-next", "--campaign-id", str(cid)])
    with pytest.raises(SystemExit) as exc_info:
        reg.main()
    assert exc_info.value.code == 3  # paused, NOT 2 (empty) -- the job is still really queued

    reg.set_paused(cid, False, db_path=db_path)
    reg.main()  # resumed -- claims successfully, no SystemExit at all
    assert "GDXU" in capsys.readouterr().out


def test_run_throttled_caps_in_flight_and_reports_results(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time as _time

    monkeypatch.setattr(reg, "get_workers_budget", lambda version: 2)
    state = {"cur": 0, "peak": 0}
    lock = threading.Lock()

    def work(x):
        with lock:
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
        _time.sleep(0.03)
        with lock:
            state["cur"] -= 1
        return x * 2

    results = []

    def on_result(task, res):
        results.append((task, res))

    with ThreadPoolExecutor(max_workers=6) as pool:
        reg.run_throttled(pool, lambda p, t: p.submit(work, t), range(10), "v-test", on_result)

    assert state["peak"] <= 2
    assert sorted(r for _t, r in results) == [x * 2 for x in range(10)]


def test_run_throttled_unthrottled_reports_exceptions(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(reg, "get_workers_budget", lambda version: None)

    def work(x):
        if x == 3:
            raise RuntimeError("boom")
        return x

    results = []

    def on_result(task, res):
        results.append((task, res))

    with ThreadPoolExecutor(max_workers=4) as pool:
        reg.run_throttled(pool, lambda p, t: p.submit(work, t), range(5), "v-test", on_result)

    outcomes = {t: r for t, r in results}
    assert isinstance(outcomes[3], RuntimeError)
    assert outcomes[0] == 0 and outcomes[4] == 4
