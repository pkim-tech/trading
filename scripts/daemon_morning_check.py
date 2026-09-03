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
     directly (cache/live/schwab_auth_canary_state.json) -- does NOT re-probe Schwab
     itself (the canary already ran within the last hour; a second probe here would
     be redundant real-API-call load for no new information).
     - State missing, unreadable, or stale (last_checked_at more than
       STALE_STATE_MAX_HOURS old): CANNOT CONFIRM healthy -- never starts the daemon
       regardless of --live, alerts distinctly from "confirmed broken" so a human
       knows this is a "no information" case, not a known-bad one.
     - status == "healthy": WOULD start the daemon. Only actually does so if --live
       was passed (explicit env, see safety layer 2 above); otherwise reports the
       decision and takes no action. When it does start for real, verifies it's
       actually alive a few seconds later (not an immediate crash) before declaring
       success.
     - status == "broken": never starts, regardless of --live -- posts "daemon down
       AND Schwab needs reauth, cron can't fix this, log in manually."

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

STALE_STATE_MAX_HOURS = 2.0   # canary runs hourly -- >2h old means it's not keeping up
DAEMON_START_VERIFY_DELAY_SECS = 5  # brief pause before re-checking it's actually alive

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
    """Returns (status_or_None, reason_if_none) -- None status means "cannot confirm,"
    with a human-readable reason (missing/unreadable/stale), never fabricated."""
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

    status, reason = _read_canary_state()

    if status is None:
        _slack(f"⚠️ Daemon morning check: active_signals.py NOT running, and Schwab "
                f"auth status can't be confirmed ({reason}) -- NOT starting the "
                f"daemon automatically. Check manually.")
        return

    if status == "broken":
        _slack("🔴 Daemon morning check: active_signals.py NOT running AND Schwab "
               "needs reauth -- cron can't fix this, log in manually.")
        return

    # status == "healthy" -- safe to start, but only actually acts under --live.
    if not args.live:
        print("[dry-run, no --live] would start active_signals.py now "
              "(daemon down, Schwab healthy) -- pass --live to actually do it.")
        _slack("ℹ️ Daemon morning check (dry-run): active_signals.py is down, Schwab "
               "is healthy -- would auto-start, but --live wasn't passed, so nothing "
               "was started.")
        return

    pid = _start_daemon()
    time.sleep(DAEMON_START_VERIFY_DELAY_SECS)
    if _daemon_running():
        _slack(f"🟡 Daemon morning check: you forgot but the cron saved you -- "
                f"active_signals.py was down, Schwab was healthy, started it "
                f"automatically (pid {pid}). All clean.")
    else:
        _slack(f"🔴 Daemon morning check: attempted to start active_signals.py "
                f"(pid {pid}) but it's not running {DAEMON_START_VERIFY_DELAY_SECS}s "
                f"later -- may have crashed immediately, check "
                f"{DAEMON_STDOUT_LOG} manually.")


if __name__ == "__main__":
    import script_usage
    script_usage.record_invocation()
    main()
