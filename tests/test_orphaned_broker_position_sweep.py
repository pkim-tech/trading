"""Stage D of the 2026-08-14 SOXS incident fix: the intraday cadence on the
ground-truth broker sweep (signals_notify.check_orphaned_broker_positions).

IMPORTANT framing, because the incident plan file got this wrong: the sweep
itself is NOT new. scripts/check_untracked_positions.run_full_sweep was built
2026-08-07 (after the GDXU incident -- same shape: a real unprotected broker
position with zero local record, undetected for a week) and has been wired into
active_signals.py's 07:00 readiness block since 2026-08-08. The real gap the
SOXS incident exposed is CADENCE, not existence: 07:00 is pre-market, so a fill
that went unreconciled at 09:30:05 would not have been swept for until 07:00 the
following morning -- ~22 hours. These tests pin the intraday wrapper's gating,
its reuse of the existing sweep (rather than a parallel implementation), and its
read-only contract.
"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import signals_config
import signals_db
import signals_notify


@pytest.fixture
def env(monkeypatch, tmp_path):
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    monkeypatch.setattr(signals_config, 'DB_PATH', Path(tmp_db.name))
    monkeypatch.setattr(signals_config, 'RESEARCH_DB_PATH', tmp_path / "no_such_research.db")
    posted = []
    monkeypatch.setattr(signals_notify, '_post_message',
                         lambda *a, **kw: posted.append(a[0] if a else kw.get('text')))
    # Throttle is persisted to a real file -- point it at tmp_path so tests
    # neither read nor write the daemon's live cache/live/ state.
    monkeypatch.setattr(signals_config, 'ORPHAN_SWEEP_STATE_PATH',
                         tmp_path / "orphan_sweep_state.json")
    signals_db.ensure_tables()
    yield posted
    Path(tmp_db.name).unlink(missing_ok=True)


def _patch_sweep(monkeypatch, findings, calls=None):
    """Patches run_full_sweep at its source module -- check_orphaned_broker_
    positions imports it inside the function body, so patching a name on
    signals_notify would not take effect."""
    import scripts.check_untracked_positions as sweep_mod

    def _fake(*a, **kw):
        if calls is not None:
            calls.append(1)
        if isinstance(findings, Exception):
            raise findings
        return findings

    monkeypatch.setattr(sweep_mod, 'run_full_sweep', _fake)


# A trading day (Friday) inside the sweep window.
IN_WINDOW = datetime(2026, 8, 14, 10, 30)


UNTRACKED = "  \U0001F6A8 UNTRACKED: broker holds 19 SOXS, NO open_positions/addon_legs row exists at all"
STALE = "  \u26a0\ufe0f  STALE: local says 19 SOXS, broker holds 0 long"
SHORT = "  \U0001F6A8 SHORT: broker holds a SHORT position of 19 SOXS -- verify this is expected, not an oversell"


def _sweep_again(now=None):
    """Clears only the throttle watermark so a second sweep runs in-test,
    leaving prior_findings intact -- that's what the confirmation gate reads."""
    signals_notify._save_orphan_sweep_last_run(0.0)
    signals_notify.check_orphaned_broker_positions(now=now or IN_WINDOW)


def test_finding_alerts_only_after_two_consecutive_sweeps(env, monkeypatch):
    """The gate: a real, persistent untracked position must still page, but on
    the SECOND sighting, not the first."""
    _patch_sweep(monkeypatch, {'ira': [UNTRACKED]})

    signals_notify.check_orphaned_broker_positions(now=IN_WINDOW)
    assert env == [], "first sighting must be held for confirmation"
    events = signals_db.get_coverage_events(scenario_key='orphaned_broker_position')
    assert len(events) == 1 and events[0]['result'] == 'found', (
        "but it must still be RECORDED on first sighting -- the gate withholds the "
        "alert, it must not erase the evidence")
    assert 'SOXS' in events[0]['detail']

    _sweep_again()
    assert len(env) == 1, env
    assert 'SOXS' in env[0] and 'UNTRACKED' in env[0], env[0]
    assert '2 consecutive sweeps' in env[0], env[0]


def test_transient_finding_that_clears_never_alerts(env, monkeypatch):
    """THE reason the gate exists. A real exit fill not yet locally reconciled
    shows as STALE for one sweep and is gone by the next -- ordinary
    reconciliation lag, not an incident. Alerting on it every 30 min is exactly
    the alert fatigue this sweep was built to avoid."""
    _patch_sweep(monkeypatch, {'ira': [STALE]})
    signals_notify.check_orphaned_broker_positions(now=IN_WINDOW)
    assert env == []

    _patch_sweep(monkeypatch, {})  # condition cleared on its own
    _sweep_again()
    assert env == [], f"a transient finding must never page anyone: {env}"


def test_a_changed_finding_does_not_count_as_confirmation(env, monkeypatch):
    """A share count still moving between sweeps is mid-reconciliation, not a
    stable condition -- the finding string embeds the count, so it correctly
    fails to match the prior sweep's key."""
    _patch_sweep(monkeypatch, {'ira': ["  MISMATCH: SOXS -- broker=19 shares, local=12 shares"]})
    signals_notify.check_orphaned_broker_positions(now=IN_WINDOW)
    _patch_sweep(monkeypatch, {'ira': ["  MISMATCH: SOXS -- broker=19 shares, local=17 shares"]})
    _sweep_again()
    assert env == [], f"a still-moving share count must not confirm: {env}"


def test_short_position_alerts_immediately_on_first_sighting(env, monkeypatch):
    """SHORT is exempt from the gate -- a naked/accidental short is worth
    tolerating a false positive for."""
    _patch_sweep(monkeypatch, {'ira': [SHORT]})
    signals_notify.check_orphaned_broker_positions(now=IN_WINDOW)
    assert len(env) == 1, env
    assert 'SHORT' in env[0], env[0]


def test_short_alerts_immediately_while_a_sibling_finding_is_still_held(env, monkeypatch):
    """Mixed sweep: the short pages now, the unconfirmed one waits, and the
    message says so rather than silently dropping it."""
    _patch_sweep(monkeypatch, {'ira': [SHORT, UNTRACKED]})
    signals_notify.check_orphaned_broker_positions(now=IN_WINDOW)
    assert len(env) == 1, env
    assert 'SHORT' in env[0], env[0]
    assert 'UNTRACKED' not in env[0], "the unconfirmed sibling must not ride along"
    assert 'awaiting confirmation' in env[0], env[0]


def test_persistent_finding_keeps_alerting_on_later_sweeps(env, monkeypatch):
    """Once confirmed, an unresolved condition stays visible -- the gate delays
    the first page, it does not one-shot it."""
    _patch_sweep(monkeypatch, {'ira': [UNTRACKED]})
    signals_notify.check_orphaned_broker_positions(now=IN_WINDOW)
    _sweep_again()
    _sweep_again()
    assert len(env) == 2, env


def test_clean_sweep_logs_proof_it_actually_ran_and_stays_silent(env, monkeypatch):
    """A 'clean' event matters: without it the Grid cannot tell "swept, nothing
    found" apart from "never swept at all" -- which is precisely the failure
    mode fast_path_fill_reconciliation sat in, invisible, for months."""
    _patch_sweep(monkeypatch, {})
    signals_notify.check_orphaned_broker_positions(now=IN_WINDOW)
    assert env == []
    events = signals_db.get_coverage_events(scenario_key='orphaned_broker_position')
    assert len(events) == 1 and events[0]['result'] == 'clean', events


def test_sweep_failure_is_recorded_not_silently_treated_as_clean(env, monkeypatch):
    _patch_sweep(monkeypatch, RuntimeError("token expired"))
    signals_notify.check_orphaned_broker_positions(now=IN_WINDOW)  # must not raise
    events = signals_db.get_coverage_events(scenario_key='orphaned_broker_position')
    assert len(events) == 1 and events[0]['result'] == 'sweep_failed', events
    assert 'token expired' in events[0]['detail']


def test_does_not_run_before_the_window_opens(env, monkeypatch):
    calls = []
    _patch_sweep(monkeypatch, {}, calls)
    signals_notify.check_orphaned_broker_positions(now=datetime(2026, 8, 14, 9, 30))
    assert calls == [], "9:30 is inside the reconcile-normally grace, before the 9:45 window"
    assert signals_db.get_coverage_events(scenario_key='orphaned_broker_position') == []


def test_does_not_run_after_the_window_closes(env, monkeypatch):
    calls = []
    _patch_sweep(monkeypatch, {}, calls)
    signals_notify.check_orphaned_broker_positions(now=datetime(2026, 8, 14, 16, 30))
    assert calls == []


def test_does_not_run_on_a_non_trading_day(env, monkeypatch):
    calls = []
    _patch_sweep(monkeypatch, {}, calls)
    # 2026-08-15 is a Saturday.
    signals_notify.check_orphaned_broker_positions(now=datetime(2026, 8, 15, 10, 30))
    assert calls == [], "a weekend sweep would burn real broker calls for nothing"


def test_throttled_to_one_real_sweep_per_interval(env, monkeypatch):
    """Each sweep costs 2 real broker calls per account across every linked
    account -- running it every POLL_SECS would hammer the API all day."""
    calls = []
    _patch_sweep(monkeypatch, {}, calls)
    for _ in range(5):
        signals_notify.check_orphaned_broker_positions(now=IN_WINDOW)
    assert len(calls) == 1, f"expected exactly one real sweep, got {len(calls)}"


def test_reuses_the_existing_sweep_rather_than_reimplementing_it(env, monkeypatch):
    """Guards the single-source-of-truth property directly: if someone later
    inlines a second broker-vs-local comparison here, patching the real sweep
    would stop suppressing the work and this test fails."""
    calls = []
    _patch_sweep(monkeypatch, {}, calls)
    signals_notify.check_orphaned_broker_positions(now=IN_WINDOW)
    assert calls == [1], "must delegate to scripts.check_untracked_positions.run_full_sweep"


def test_never_writes_a_position_row_to_fix_what_it_finds(env, monkeypatch):
    """Detect-only (automation_principles.md #5, and the explicit 2026-08-06
    user scoping call). Auto-creating the missing row would erase the exact
    signal this sweep exists to surface."""
    _patch_sweep(monkeypatch, {
        'ira': ["  🚨 UNTRACKED: broker holds 19 SOXS, NO open_positions/addon_legs row exists at all"],
    })
    signals_notify.check_orphaned_broker_positions(now=IN_WINDOW)
    with signals_db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM open_positions").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM addon_legs").fetchone()[0] == 0


def test_throttle_survives_a_daemon_restart(env, monkeypatch, tmp_path):
    """The throttle is persisted, not in-memory (2026-08-15 review finding).
    This project restarts the daemon deliberately and often, so an in-memory
    throttle would re-sweep on every restart inside the window."""
    calls = []
    _patch_sweep(monkeypatch, {}, calls)
    signals_notify.check_orphaned_broker_positions(now=IN_WINDOW)
    assert len(calls) == 1

    # Simulate a restart: nothing in memory carries over, only the state file.
    assert signals_notify._load_orphan_sweep_last_run() > 0
    signals_notify.check_orphaned_broker_positions(now=IN_WINDOW)
    assert len(calls) == 1, "a restart must not reset the throttle"


def test_throttle_is_stamped_before_the_sweep_so_a_failure_cannot_hammer_the_broker(env, monkeypatch):
    calls = []
    _patch_sweep(monkeypatch, RuntimeError("broker down"), calls)
    for _ in range(4):
        signals_notify.check_orphaned_broker_positions(now=IN_WINDOW)
    assert len(calls) == 1, "a failing sweep must still consume the throttle"


def test_missing_state_file_reads_as_sweep_now(env, monkeypatch):
    """Erring toward one extra read-only sweep is the safe direction."""
    assert signals_notify._load_orphan_sweep_last_run() == 0.0
    calls = []
    _patch_sweep(monkeypatch, {}, calls)
    signals_notify.check_orphaned_broker_positions(now=IN_WINDOW)
    assert len(calls) == 1
