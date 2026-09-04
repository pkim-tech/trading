"""9am ET weekday cron target (NOT wired to crontab yet -- a separate, explicit
activation step). A deliberate, narrow exception to the standing "user starts the
daemon" rule (feedback_user_runs_daemon_lifecycle memory) -- confirmed with the user,
backup-only safety net for the case where the daemon was never started for the day,
not a general-purpose auto-restart policy and not a green light to touch the daemon
in any other context.

REAL INCIDENT, same day, twice-revised design -- read this before touching this file
again:
  v1 (auto-start, no safety rail): a test invocation (`SIM_MODE=1 .venv/bin/python
  scripts/daemon_morning_check.py`) found the daemon genuinely down (pre-existing
  process had already exited for an unrelated reason) and, exactly as designed,
  called subprocess.Popen to start a REAL active_signals.py process -- inheriting the
  test's own SIM_MODE=1 from the shell (Popen's default env inheritance, no `env=`
  override). Confirmed directly that SIM_MODE does NOT gate real broker order
  placement anywhere in schwab_safety.py/schwab_client.py -- only Slack formatting/
  interactive buttons -- so this was a real, live daemon with no functional safety
  net, just an accident of testing.
  v2 (read-only, no auto-start at all): removed the capability entirely as an
  immediate mitigation. User's follow-up call: the auto-start CAPABILITY wasn't the
  problem -- the problem was that TESTING could accidentally trigger a real start
  with no safety rail between "checking what this script would do" and "it actually
  doing it." Restored in v3 (this version) with two real safety layers instead of
  removing the feature:
    1. **--live is required to ever act.** Without it, this script only ever prints/
       logs its decision (dry-run) -- it NEVER calls subprocess.Popen regardless of
       daemon/Schwab state. The real crontab entry (once activated) passes --live
       explicitly; any ad hoc/manual/test invocation without it is now
       STRUCTURALLY incapable of starting anything, regardless of what SIM_MODE
       happens to be set to in the caller's shell -- this is the actual fix for
       what went wrong, not a SIM_MODE convention (which this script's own testing
       already proved is too easy to get inherited by accident).
    2. **Explicit subprocess environment, never inherited.** When --live actually
       spawns the daemon, the child's env is built via `os.environ.copy()` then
       `env['SIM_MODE'] = '0'` set EXPLICITLY -- not left to Popen's default
       inheritance + active_signals.py's own `os.environ.setdefault('SIM_MODE','0')`
       (which only fires when nothing already set it, exactly the gap this morning's
       incident exploited). Real mode is now explicit at the call site, never
       implicit-via-inheritance.

Logic:
  1. Is active_signals.py already running (daemon_status.py's own pgrep-based check,
     reused as a subprocess -- not reimplemented)? If yes: quiet Slack confirmation,
     done. Nothing else in this script runs.
  2. If NOT running: read scripts/schwab_auth_canary.py's persisted state file
     directly (cache/live/schwab_auth_canary_state.json) first -- if it's fresh
     (last_checked_at within STALE_STATE_MAX_HOURS), trust it as-is, no re-probe (the
     canary already ran within the last hour; a second probe here would be redundant
     real-API-call load for no new information).

     REVISED 2026-09-03 (real bug found in testing): a stale/missing/unreadable
     canary state used to mean "give up, never start, alert as unconfirmed" -- even
     under --live. That's wrong: staleness means the canary hasn't told us anything
     RECENTLY, not that Schwab is actually broken. When the state is stale/missing/
     unreadable, this script now falls back to running the exact same direct probe
     the canary itself uses (schwab_auth_canary.probe() -- imported and called
     in-process, not reimplemented; same subprocess + PROBE_TIMEOUT_SECS wall-clock
     cap as the canary's own safety design) to verify Schwab health itself, right
     now, before deciding. Every Slack message this script posts says explicitly
     whether the healthy/broken verdict came from the canary's own recent state or
     from this script's own fallback probe, so a human reading the alert always
     knows which one produced it.
     - Status resolved (canary-fresh OR fallback-probe), healthy: WOULD start the
       daemon. Only actually does so if --live was passed (explicit env, see safety
       layer 2 above); otherwise reports the decision and takes no action. When it
       does start for real, verifies the REAL observed outcome (added 2026-09-03) --
       not just "is the process still alive," but whether its own heartbeat file
       (cache/live/active_signals_heartbeat.txt, written on every poll-loop
       iteration) actually updated since the start attempt, and whether a traceback
       appeared in its stdout log since then. Polls up to
       DAEMON_START_VERIFY_MAX_WAIT_SECS (not a single early snapshot) -- confirmed
       live the same day that a real, healthy start can take ~17s of startup work
       (invariants checks, EOD reports) before its first heartbeat write, so a
       single 5s check produced a false "hung during init" on a daemon that was
       actually fine. A dead process or an actual traceback still fail fast, no
       need to wait out the rest of the window for those. Reports which of these
       failed (or "heartbeat confirmed fresh, no traceback") in the Slack message,
       rather than a bare pid.
     - Status resolved (canary-fresh OR fallback-probe), broken: never starts,
       regardless of --live -- posts "daemon down AND Schwab needs reauth, cron
       can't fix this, log in manually," including the fallback probe's real error
       when that's the source.

Usage:
  .venv/bin/python scripts/daemon_morning_check.py            # dry-run, never acts
  .venv/bin/python scripts/daemon_morning_check.py --live      # real crontab invocation
  SIM_MODE=1 .venv/bin/python scripts/daemon_morning_check.py --live  # still dry-run-SAFE:
      even with --live, the spawned child's SIM_MODE is set EXPLICITLY to '0' by this
      script, never inherited from the caller's shell -- SIM_MODE in this shell only
      gates THIS script's own Slack posting now, exactly like every sibling script.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import requests
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

CANARY_STATE_PATH = ROOT / "cache" / "live" / "schwab_auth_canary_state.json"
DAEMON_STDOUT_LOG = ROOT / "logs" / "active_signals_stdout.log"
HEARTBEAT_PATH = ROOT / "cache" / "live" / "active_signals_heartbeat.txt"  # signals_config.HEARTBEAT_PATH

STALE_STATE_MAX_HOURS = 2.0   # canary runs hourly -- >2h old means it's not keeping up
DAEMON_START_VERIFY_DELAY_SECS = 5   # brief initial pause before the first check
# Confirmed live 2026-09-03: active_signals.py does real startup work (invariants
# checks, EOD scenario review, morning report) BEFORE reaching its main loop's first
# heartbeat write -- took ~17s in one real run. A single early check flagged this as
# "hung during init" when it was actually a normal, healthy start still in progress.
# Poll up to this ceiling instead of one early snapshot.
DAEMON_START_VERIFY_MAX_WAIT_SECS = 90
DAEMON_START_VERIFY_POLL_SECS = 5

SIM_MODE = os.environ.get("SIM_MODE", "1") != "0"
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "")


def _slack(text):
    if SIM_MODE:
        print(f"[SIM_MODE, not posted] {text}")
        return
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL:
        print(f"[no slack config] {text}")
        return
    try:
        requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            json={"channel": SLACK_CHANNEL, "text": text},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[alert failed: {e}] {text}")


def _daemon_running():
    """Reuses scripts/daemon_status.py's own pgrep-based check as a subprocess (exit
    0 = running, 1 = not) -- not reimplemented, so this can never silently drift from
    what that script itself considers "running." Read-only, no side effect."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "daemon_status.py")],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _read_canary_state():
    """Returns (status_or_None, staleness_reason_or_None) -- status is the canary's
    last-known status if the state file is fresh; staleness_reason is set (and status
    is None) when the state can't be trusted as-is (missing/unreadable/stale), which
    triggers the direct-probe fallback in _resolve_schwab_status() below. Never
    fabricates a status."""
    if not CANARY_STATE_PATH.exists():
        return None, "canary state file doesn't exist (canary may not be wired up yet)"
    try:
        state = json.loads(CANARY_STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return None, f"canary state file unreadable: {e}"

    last_checked = state.get("last_checked_at")
    if not last_checked:
        return None, "canary state file has no last_checked_at"
    age_hours = (datetime.now() - datetime.fromisoformat(last_checked)).total_seconds() / 3600.0
    if age_hours > STALE_STATE_MAX_HOURS:
        return None, f"canary state is {age_hours:.1f}h stale (last checked {last_checked})"

    return state.get("status"), None


def _resolve_schwab_status():
    """Returns (status, source, detail). status is "healthy"/"broken"/None (None only
    if the fallback probe itself couldn't run -- doesn't happen in practice since
    probe() always returns healthy/broken, but kept honest rather than assumed).
    source is "canary_state" or "direct_probe", so every caller/Slack message can say
    which one produced the verdict. detail is the staleness reason when source is
    direct_probe, or the probe's own error string when the probe found it broken."""
    status, staleness_reason = _read_canary_state()
    if staleness_reason is None:
        return status, "canary_state", None

    # Canary state stale/missing/unreadable -- staleness means "verify it yourself
    # right now," not "give up." Reuse the canary's own probe (same subprocess +
    # wall-clock-timeout safety design) rather than reimplementing it.
    import schwab_auth_canary
    probe_status, probe_error = schwab_auth_canary.probe()
    detail = f"canary state unusable ({staleness_reason}); ran direct probe instead"
    if probe_error:
        detail += f": {probe_error}"
    return probe_status, "direct_probe", detail


def _start_daemon():
    """The one real side-effecting action in this script -- ONLY ever called when
    --live was passed (see main()). Launches active_signals.py detached
    (start_new_session=True, Python's equivalent of setsid, so it survives this
    script's own process exiting), stdout/stderr redirected to the same log file the
    user's own manual invocation would use.

    env is built EXPLICITLY (os.environ.copy() + a forced SIM_MODE='0'), never left
    to Popen's default inheritance -- this is the real fix for this morning's
    incident: a caller's own SIM_MODE=1 (however it got there -- an exported shell
    var, a test harness, anything) can no longer silently propagate into a REAL
    daemon start. Real mode is explicit at this call site, not implicit."""
    env = os.environ.copy()
    env["SIM_MODE"] = "0"
    log_f = open(DAEMON_STDOUT_LOG, "a")
    proc = subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "python"), "active_signals.py", "run"],
        cwd=str(ROOT), stdout=log_f, stderr=subprocess.STDOUT,
        start_new_session=True, env=env,
    )
    return proc.pid


def _verify_daemon_started_once(start_time, pre_start_log_size):
    """One snapshot check of the real observed outcome, not a prediction from old
    logs: checks the process is actually alive, then whether its own heartbeat file
    was written to since start_time (proves the main loop reached its first
    iteration, not just that the process spawned -- active_signals.py writes
    HEARTBEAT_PATH on entry to every poll iteration, active_signals.py:955) and
    whether any traceback appeared in its stdout log since the start attempt
    (DAEMON_STDOUT_LOG carries whatever an uncaught exception prints, since Popen
    redirected stderr there). Returns (ok, detail, retryable) -- retryable is True
    only for "still waiting on the first heartbeat, nothing wrong seen yet" so the
    caller can keep polling; a dead process or a real traceback are fail-fast
    (retryable=False), no point waiting out the rest of the window for those."""
    if not _daemon_running():
        return False, (f"it's not running {DAEMON_START_VERIFY_DELAY_SECS}s later -- "
                        f"may have crashed immediately, check {DAEMON_STDOUT_LOG} manually."), False

    new_log_text = ""
    if DAEMON_STDOUT_LOG.exists():
        with DAEMON_STDOUT_LOG.open() as f:
            f.seek(pre_start_log_size)
            new_log_text = f.read()
    if "Traceback (most recent call last)" in new_log_text:
        tail = "\n".join(new_log_text.strip().splitlines()[-15:])
        return False, ("process is running but its own log shows a traceback since "
                        f"the start attempt -- check manually:\n{tail}"), False

    if not HEARTBEAT_PATH.exists():
        return False, "process is running, no heartbeat file yet, still waiting.", True
    try:
        hb_mtime = HEARTBEAT_PATH.stat().st_mtime
    except OSError:
        return False, "process is running but heartbeat file couldn't be read -- check manually.", False
    if hb_mtime < start_time.timestamp() - 1:  # 1s tolerance for filesystem mtime granularity
        return False, "process is running, heartbeat hasn't updated yet, still waiting.", True

    return True, "heartbeat confirmed fresh, no traceback in its log.", False


def _verify_daemon_started(start_time, pre_start_log_size):
    """Polls _verify_daemon_started_once up to DAEMON_START_VERIFY_MAX_WAIT_SECS --
    startup does real work (invariants, EOD reports) before its first heartbeat
    write, so a single early snapshot isn't enough (confirmed live 2026-09-03).
    Stops immediately on a fail-fast result (dead process, real traceback) or once
    the process reports healthy; only the "still waiting, nothing wrong seen"
    result keeps polling until the deadline."""
    deadline = time.time() + DAEMON_START_VERIFY_MAX_WAIT_SECS
    time.sleep(DAEMON_START_VERIFY_DELAY_SECS)
    while True:
        ok, detail, retryable = _verify_daemon_started_once(start_time, pre_start_log_size)
        if ok or not retryable or time.time() >= deadline:
            if not ok and retryable:
                detail = (f"heartbeat still hasn't updated after "
                           f"{DAEMON_START_VERIFY_MAX_WAIT_SECS}s -- may be hung during "
                           f"init, check manually.")
            return ok, detail
        time.sleep(DAEMON_START_VERIFY_POLL_SECS)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true",
                     help="required to actually start the daemon when it's found down "
                          "and Schwab is healthy. Without this flag, the script only "
                          "reports what it WOULD do -- it never calls subprocess.Popen. "
                          "The real crontab entry passes this explicitly; a manual/test "
                          "invocation without it can never start anything.")
    args = ap.parse_args(argv)

    if _daemon_running():
        _slack("✅ Daemon morning check: active_signals.py already running, all good.")
        return

    status, source, detail = _resolve_schwab_status()
    source_note = ("Schwab canary's own recent state" if source == "canary_state"
                    else f"this script's own direct probe ({detail})")

    if status is None:
        # Only possible if the direct probe itself couldn't produce a verdict --
        # kept as an explicit branch rather than assumed unreachable.
        _slack(f"⚠️ Daemon morning check: active_signals.py NOT running, and Schwab "
                f"auth status can't be confirmed via {source_note} -- NOT starting "
                f"the daemon automatically. Check manually.")
        return

    if status == "broken":
        _slack(f"🔴 Daemon morning check: active_signals.py NOT running AND Schwab "
               f"needs reauth (per {source_note}) -- cron can't fix this, log in "
               f"manually.")
        return

    # status == "healthy" -- safe to start, but only actually acts under --live.
    if not args.live:
        print(f"[dry-run, no --live] would start active_signals.py now "
              f"(daemon down, Schwab healthy per {source_note}) -- pass --live to "
              f"actually do it.")
        _slack(f"ℹ️ Daemon morning check (dry-run): active_signals.py is down, Schwab "
               f"is healthy (per {source_note}) -- would auto-start, but --live "
               f"wasn't passed, so nothing was started.")
        return

    start_time = datetime.now()
    pre_start_log_size = DAEMON_STDOUT_LOG.stat().st_size if DAEMON_STDOUT_LOG.exists() else 0
    pid = _start_daemon()
    time.sleep(DAEMON_START_VERIFY_DELAY_SECS)
    ok, detail = _verify_daemon_started(start_time, pre_start_log_size)
    if ok:
        _slack(f"🟡 Daemon morning check: you forgot but the cron saved you -- "
                f"active_signals.py was down, Schwab was healthy, started it "
                f"automatically (pid {pid}), {detail}")
    else:
        _slack(f"🔴 Daemon morning check: attempted to start active_signals.py "
                f"(pid {pid}) but {detail}")


if __name__ == "__main__":
    import script_usage
    script_usage.record_invocation()
    main()
