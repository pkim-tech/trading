"""9am ET weekday cron target (NOT wired to crontab yet -- a separate, explicit
activation step). Pure read-only status check: is active_signals.py running
(daemon_status.py's own pgrep-based check) + what's the last known Schwab auth canary
state (read from scripts/schwab_auth_canary.py's persisted state file, NOT re-probed)
-- reports both via Slack. NEVER starts, stops, or otherwise touches the daemon.

Rewritten 2026-09-03 (real incident, same day): the original version of this script
auto-started active_signals.py when it found the daemon down and the Schwab canary
healthy. A test invocation of that logic (`SIM_MODE=1 .venv/bin/python
scripts/daemon_morning_check.py`) found the daemon genuinely NOT running (pre-existing
process had already exited for an unrelated reason) and, exactly as designed, started
a REAL active_signals.py process -- inheriting the test invocation's own SIM_MODE=1
from the shell via subprocess.Popen's default environment inheritance (no `env=`
override). That real process ran with SIM_MODE=1 (confirmed both via
/proc/<pid>/environ and the daemon's own check_sim_mode_off_for_real_daemon()
invariant firing at its own startup), which only affects Slack formatting/interactive
buttons -- NOT whether real broker orders get placed (confirmed directly: no SIM_MODE
gate exists anywhere in schwab_safety.py/schwab_client.py). User's decision in direct
response: remove the auto-start capability entirely, this script only ever reports
status. If the daemon is down, a human decides what to do -- this script's only job is
to make sure a human finds out.

(The SIM_MODE-doesn't-gate-broker-orders finding above is a separate, real, standing
production-safety question -- flagged, not investigated further here, needs its own
explicit scoping before anyone touches it.)

Logic:
  1. Is active_signals.py running (daemon_status.py's own check, reused as a
     subprocess, not reimplemented)?
  2. Read scripts/schwab_auth_canary.py's persisted state (cache/live/
     schwab_auth_canary_state.json) -- does NOT re-probe Schwab itself (the canary
     already ran within the last hour; a second probe here would be redundant
     real-API-call load for no new information). Reports "can't confirm" distinctly
     from "confirmed healthy"/"confirmed broken" if the state file is missing,
     unreadable, or stale (>STALE_STATE_MAX_HOURS old).
  3. Posts ONE Slack message summarizing both -- daemon up/down, Schwab
     healthy/broken/unknown. Never conditional on daemon state for whether it posts;
     always reports, so a human always gets the morning status regardless of outcome.

Usage:
  .venv/bin/python scripts/daemon_morning_check.py
  SIM_MODE=1 .venv/bin/python scripts/daemon_morning_check.py   # test: prints instead of posting
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import requests
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

CANARY_STATE_PATH = ROOT / "cache" / "live" / "schwab_auth_canary_state.json"
STALE_STATE_MAX_HOURS = 2.0   # canary runs hourly -- >2h old means it's not keeping up

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
    what that script itself considers "running." Purely a status read -- no side
    effect either way."""
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


def main():
    daemon_up = _daemon_running()
    schwab_status, schwab_reason = _read_canary_state()

    daemon_word = "UP" if daemon_up else "DOWN"
    if schwab_status == "healthy":
        schwab_word = "healthy"
    elif schwab_status == "broken":
        schwab_word = "BROKEN (needs reauth)"
    else:
        schwab_word = f"unknown ({schwab_reason})"

    if daemon_up:
        icon = "✅"
    elif schwab_status == "healthy":
        icon = "⚠️"   # down but fixable by a human in seconds (schwab is fine)
    else:
        icon = "🔴"   # down AND schwab isn't confirmed healthy -- worse morning

    _slack(f"{icon} Daemon morning check: active_signals.py is {daemon_word}. "
           f"Schwab auth: {schwab_word}.")


if __name__ == "__main__":
    import script_usage
    script_usage.record_invocation()
    main()
