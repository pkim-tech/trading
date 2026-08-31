"""Real functional coverage for scripts/bench_phase1_phase2_inmemory.py's _dispatch
mid-job workers_budget throttling (2026-08-31, Task #8 follow-up -- folded in while
the Task #8 commit was already on hold, per the user's own ask).

Uses a ThreadPoolExecutor stand-in for `pool` (not a real ProcessPoolExecutor) --
_dispatch only calls .submit()/._max_workers on it, and a real subprocess pool can't
observe a monkeypatched fake backtest function (each worker re-imports the module
fresh in its own interpreter). A ThreadPoolExecutor runs in-process, so the
concurrency-tracking fake function below sees the real, enforced in-flight cap."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from scripts import bench_phase1_phase2_inmemory as bench
from scripts import campaign_registry as reg


def _make_tasks(n):
    # (take_profit, stop_loss, max_hold_hours, window, z, trail_sell_pct)
    return [(i + 1, i + 1, 24, 5, 1.0, 2) for i in range(n)]


def _concurrency_tracking_fake_backtest(sleep_s=0.05):
    state = {"cur": 0, "peak": 0}
    lock = threading.Lock()

    def fake(args_tuple):
        with lock:
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
        time.sleep(sleep_s)
        with lock:
            state["cur"] -= 1
        return {"status": "SUCCESS", "payload": (0.0, 1, 1.0, 0.0, 0.0, 0.0)}

    return fake, state


def test_dispatch_caps_in_flight_at_workers_budget(monkeypatch):
    fake, state = _concurrency_tracking_fake_backtest()
    monkeypatch.setattr(bench, "run_single_backtest_node_ground_truth_isolated", fake)
    monkeypatch.setattr(reg, "get_workers_budget", lambda version: 2)

    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = bench._dispatch(pool, _make_tasks(12), "TICK", "TrailingBothZScoreBreakout",
                                "v-test", 3, 100.0, desc="test")

    assert len(rows) == 12  # every task still completed
    assert state["peak"] <= 2  # never exceeded the budget, despite max_workers=6


def test_dispatch_unthrottled_when_no_budget_set(monkeypatch):
    """None (no campaign row / workers_budget never set) falls back to the pool's own
    max_workers -- the ORIGINAL submit-everything-upfront throughput, not a hang."""
    fake, state = _concurrency_tracking_fake_backtest()
    monkeypatch.setattr(bench, "run_single_backtest_node_ground_truth_isolated", fake)
    monkeypatch.setattr(reg, "get_workers_budget", lambda version: None)

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = bench._dispatch(pool, _make_tasks(12), "TICK", "TrailingBothZScoreBreakout",
                                "v-test", 3, 100.0, desc="test")

    assert len(rows) == 12
    assert state["peak"] == 4  # used every available worker


def test_dispatch_budget_of_zero_does_not_hang(monkeypatch):
    """A non-positive budget must fail toward 'unthrottled', never toward 'submit
    nothing forever' -- see _dispatch's own docstring on this fail-safe direction."""
    fake, state = _concurrency_tracking_fake_backtest(sleep_s=0.01)
    monkeypatch.setattr(bench, "run_single_backtest_node_ground_truth_isolated", fake)
    monkeypatch.setattr(reg, "get_workers_budget", lambda version: 0)

    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = bench._dispatch(pool, _make_tasks(5), "TICK", "TrailingBothZScoreBreakout",
                                "v-test", 3, 100.0, desc="test")

    assert len(rows) == 5


def test_dispatch_recovers_failures_and_crashes_throttled(monkeypatch):
    """Non-SUCCESS statuses and raised exceptions must still be counted/skipped
    (not silently dropped or hung) under the bounded (throttled) submission path."""
    lock = threading.Lock()
    calls = {"n": 0}

    def flaky(args_tuple):
        with lock:
            calls["n"] += 1
            n = calls["n"]
        if n % 3 == 0:
            raise RuntimeError("simulated crash")
        if n % 3 == 1:
            return {"status": "REJECTED"}
        return {"status": "SUCCESS", "payload": (0.0, 1, 1.0, 0.0, 0.0, 0.0)}

    monkeypatch.setattr(bench, "run_single_backtest_node_ground_truth_isolated", flaky)
    monkeypatch.setattr(reg, "get_workers_budget", lambda version: 2)  # < max_workers=3 -> throttled

    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = bench._dispatch(pool, _make_tasks(9), "TICK", "TrailingBothZScoreBreakout",
                                "v-test", 3, 100.0, desc="test")

    assert len(rows) == 3  # only the SUCCESS third


def test_dispatch_recovers_failures_and_crashes_unthrottled(monkeypatch):
    """Same contract on the unthrottled (budget >= max_workers) fast path -- this is
    the ORIGINAL submit-everything-upfront code path, must still behave identically."""
    lock = threading.Lock()
    calls = {"n": 0}

    def flaky(args_tuple):
        with lock:
            calls["n"] += 1
            n = calls["n"]
        if n % 3 == 0:
            raise RuntimeError("simulated crash")
        if n % 3 == 1:
            return {"status": "REJECTED"}
        return {"status": "SUCCESS", "payload": (0.0, 1, 1.0, 0.0, 0.0, 0.0)}

    monkeypatch.setattr(bench, "run_single_backtest_node_ground_truth_isolated", flaky)
    monkeypatch.setattr(reg, "get_workers_budget", lambda version: None)  # unthrottled

    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = bench._dispatch(pool, _make_tasks(9), "TICK", "TrailingBothZScoreBreakout",
                                "v-test", 3, 100.0, desc="test")

    assert len(rows) == 3


def test_dispatch_exactly_at_max_workers_takes_unthrottled_fast_path(monkeypatch):
    """budget == max_workers (the real default: the shell passes --workers-budget equal
    to --workers) must take the unthrottled SUBMIT-ALL-UPFRONT path -- paired-review
    CONFIRMED HIGH, 2026-08-31: a real ProcessPoolExecutor benchmark measured the
    bounded-submission path as a 1.2-1.5x throughput regression versus submit-all when
    there's no actual throttling to do.

    Deliberately does NOT just assert peak concurrency == max_workers (paired-review LOW,
    2026-08-31: the throttled path reaches the same peak when budget == max_workers, so
    that alone can't tell the two branches apart -- a regression back to bounded
    submission here would pass silently). Instead blocks every task on a threading.Event
    and asserts ALL 12 tasks were actually SUBMITTED to the pool before any one of them is
    allowed to complete -- true only for submit-everything-upfront; the throttled path
    would submit only `budget` tasks and wait for a completion before submitting more."""
    release = threading.Event()

    def fake(args_tuple):
        release.wait(timeout=5)
        return {"status": "SUCCESS", "payload": (0.0, 1, 1.0, 0.0, 0.0, 0.0)}

    monkeypatch.setattr(bench, "run_single_backtest_node_ground_truth_isolated", fake)
    monkeypatch.setattr(reg, "get_workers_budget", lambda version: 4)

    result = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        # Wrap submit() itself -- this is what actually distinguishes the two branches.
        # Counting calls to `fake()` (task EXECUTION start) doesn't work: with
        # max_workers=4, at most 4 tasks can be RUNNING at once regardless of how many
        # were SUBMITTED to the pool's internal queue, so that signal caps at 4 either way.
        submit_calls = []
        real_submit = pool.submit
        monkeypatch.setattr(pool, "submit",
                             lambda *a, **kw: (submit_calls.append(1), real_submit(*a, **kw))[1])

        t = threading.Thread(target=lambda: result.__setitem__(
            "rows", bench._dispatch(pool, _make_tasks(12), "TICK", "TrailingBothZScoreBreakout",
                                     "v-test", 3, 100.0, desc="test")))
        t.start()
        # All 4 real workers are blocked inside fake() on release.wait() -- give _dispatch
        # time to finish whatever it's going to submit before any task can complete, then
        # check submission count BEFORE unblocking anything.
        time.sleep(0.3)
        submitted_before_any_completion = len(submit_calls)
        release.set()
        t.join(timeout=5)

    assert submitted_before_any_completion == 12  # every task submitted upfront
    assert len(result["rows"]) == 12


def test_dispatch_budget_change_between_calls_takes_effect(monkeypatch):
    """The real point of set-workers-budget: budget is read ONCE per _dispatch call (not
    polled mid-call, see _dispatch's own docstring for why), so a change between two
    separate _dispatch calls -- e.g. between Phase2-island generations, the real shape of
    a single bench_phase1_phase2_inmemory.py process's actual call pattern -- must be
    picked up on the very next call."""
    fake, state = _concurrency_tracking_fake_backtest()
    monkeypatch.setattr(bench, "run_single_backtest_node_ground_truth_isolated", fake)

    budget_holder = {"value": 4}
    monkeypatch.setattr(reg, "get_workers_budget", lambda version: budget_holder["value"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        bench._dispatch(pool, _make_tasks(8), "TICK", "TrailingBothZScoreBreakout",
                         "v-test", 3, 100.0, desc="call1")
        assert state["peak"] == 4  # first call: unthrottled at budget=4

        state["peak"] = 0  # reset between calls
        budget_holder["value"] = 2  # simulate a live `set-workers-budget --workers-budget 2`
        bench._dispatch(pool, _make_tasks(8), "TICK", "TrailingBothZScoreBreakout",
                         "v-test", 3, 100.0, desc="call2")
        assert state["peak"] <= 2  # second call: picked up the lowered budget


def test_get_workers_budget_fails_safe_on_unreadable_db(tmp_path):
    """paired-review CONFIRMED HIGH, 2026-08-31: get_workers_budget is called from inside
    _dispatch, the hot path of a real multi-hour sweep, on a DB concurrently written by the
    sweep's own tables plus enqueue/claim-next/mark-finished/status -- an unhandled
    sqlite3 error here used to propagate straight out of _dispatch and kill the bench
    process mid-run, discarding hours of completed in-memory work. Real (not monkeypatched)
    failure: point at a path that can never be a valid SQLite DB."""
    unreadable = tmp_path  # a directory, not a file -- sqlite3.connect + execute raises
    assert reg.get_workers_budget("v-test", db_path=str(unreadable)) is None


def test_dispatch_end_to_end_survives_unreadable_budget_db(monkeypatch, tmp_path):
    """End-to-end version of the above: _dispatch itself, using the REAL get_workers_budget
    (not monkeypatched) pointed at a DB path that can't be read, completes successfully
    instead of crashing -- the actual fix, not just the unit-level guarantee."""
    fake, state = _concurrency_tracking_fake_backtest(sleep_s=0.01)
    monkeypatch.setattr(bench, "run_single_backtest_node_ground_truth_isolated", fake)
    monkeypatch.setattr(reg, "DB_PATH", str(tmp_path))  # a directory -- every read fails

    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = bench._dispatch(pool, _make_tasks(5), "TICK", "TrailingBothZScoreBreakout",
                                "v-test", 3, 100.0, desc="test")

    assert len(rows) == 5  # completed anyway -- fell back to unthrottled


def test_get_and_set_workers_budget_round_trip(tmp_path):
    db_path = str(tmp_path / "test_workers_budget.db")
    cid, version = reg.resolve_or_create("v6.5", 4, "massive", "2021-08-23", "2026-08-21",
                                          workers_budget=8, db_path=db_path)
    assert reg.get_workers_budget(version, db_path=db_path) == 8

    assert reg.set_workers_budget(cid, 3, db_path=db_path) is True
    assert reg.get_workers_budget(version, db_path=db_path) == 3

    assert reg.set_workers_budget(999999, 5, db_path=db_path) is False


def test_get_workers_budget_none_for_unset_or_unknown(tmp_path):
    db_path = str(tmp_path / "test_workers_budget2.db")
    _cid, version = reg.resolve_or_create("v6.5", 4, "massive", "2021-08-23", "2026-08-21",
                                           db_path=db_path)  # no workers_budget passed
    assert reg.get_workers_budget(version, db_path=db_path) is None
    assert reg.get_workers_budget("no-such-version", db_path=db_path) is None
