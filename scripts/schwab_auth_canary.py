"""Hourly cron target (NOT wired to crontab yet -- a separate, explicit activation
step): probes real Schwab auth end-to-end and persists stateful health, so
daemon_morning_check.py (a separate script) can answer "is Schwab healthy" WITHOUT
re-probing itself. Real motivating question (user's own observation): auths Tuesday
morning, seems to fail Tuesday morning the following week -- is the real refresh-token
window exactly 7 days, or slightly under? This canary's own persisted history is the
empirical data that answers it.

REAL SAFETY FINDING, confirmed before building (not assumed from schwab_auth.py's own
docstring, which is stale on this point): `schwab_auth.get_client(interactive=False)`
does NOT skip opening a browser the way that docstring claims -- read the actual
installed schwab-py source (schwab/auth.py): `interactive=False` only skips the
"press ENTER" prompt; `client_from_login_flow` still calls `webbrowser.get(...).open(...)`
and then BLOCKS for up to `callback_timeout` (default 300s = 5 minutes) waiting for an
OAuth callback that will never arrive in an unattended cron context, before finally
raising RedirectTimeoutError. In THIS environment `webbrowser.get()` itself raises
immediately (`Error: could not locate runnable browser`, confirmed live) -- but that is
an environment-dependent fact, not a schwab-py guarantee, and an hourly cron job that
could silently hang for 5 minutes on a bad day is not an acceptable risk. Additionally,
`easy_client`'s own `max_token_age` default (60*60*24*6.5 -- exactly 6.5 days, not 7)
PROACTIVELY DISCARDS a token file older than that and falls through to the same
browser-flow path regardless of whether a refresh would have actually succeeded --
this may well be the real mechanism behind the user's observed "~7 day" failure
window, not a Schwab-server-side cutoff. Both findings are logged into this canary's
own history for the empirical question, and the probe itself is wrapped in a
subprocess with a hard wall-clock timeout so this canary can NEVER hang a cron slot
regardless of environment quirks.

State persisted to cache/live/schwab_auth_canary_state.json (small, evolving record --
NOT an append log, this is the "current status" daemon_morning_check.py reads):
{last_checked_at, status (healthy/broken), last_known_good_at,
first_detected_broken_at, last_reactive_alert_at, last_proactive_warning_for_creation_ts}.

Full poll history appended to logs/canaries/schwab_auth_canary_log.jsonl
(gitignored runtime artifact, moved 2026-09-08 from docs/ -- same append-only
convention as logs/canaries/corp_action_canary_log.jsonl) -- one line per poll:
{poll_timestamp, status, creation_timestamp, token_age_days, error}. This is the real
data that answers the 7-vs-6.5-day question once enough real failures accumulate.

Alert logic (Slack, gated by SIM_MODE like every other Slack-posting script in this
codebase -- defaults ON, an accidental dev/test invocation never leaks to the real
channel):
  - REACTIVE: healthy->broken = immediate alert. Stays broken on a later hourly run =
    one follow-up nudge after >=4h since the last alert, then quiet until resolved.
    broken->healthy = quiet confirmation.
  - PROACTIVE (added same day, separate ask): reads cache/live/schwab_token.json's
    top-level `creation_timestamp` (epoch seconds, set at interactive login -- NOT the
    token sub-dict's `expires_at`, which is just the 30-min access-token expiry and
    gets rewritten on every refresh). If >=5 days old and no proactive warning has
    been sent yet for THIS creation_timestamp value (dedup on the timestamp itself, so
    a fresh reauth naturally resets it), sends one one-shot "reauth when convenient"
    warning -- in ADDITION to, not instead of, the reactive alert above.

Usage:
  .venv/bin/python scripts/schwab_auth_canary.py
  SIM_MODE=1 .venv/bin/python scripts/schwab_auth_canary.py   # test: prints instead of posting
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # cron runs from an unknown cwd

import requests
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

TOKEN_PATH = ROOT / "cache" / "live" / "schwab_token.json"
STATE_PATH = ROOT / "cache" / "live" / "schwab_auth_canary_state.json"
LOG_PATH = ROOT / "logs" / "canaries" / "schwab_auth_canary_log.jsonl"

PROBE_TIMEOUT_SECS = 30      # hard wall-clock cap -- never lets a cron slot hang
FOLLOWUP_NUDGE_GAP_HOURS = 4
PROACTIVE_WARNING_AGE_DAYS = 5

# Same fail-safe convention as signals_config.py's own SIM_MODE (defaults ON unless
# explicitly set to "0") -- replicated directly rather than importing signals_config
# (which would pull in the whole Bolt app singleton just for a boolean flag).
SIM_MODE = os.environ.get("SIM_MODE", "1") != "0"
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "")

# The probe runs in a SEPARATE subprocess (not in-process) specifically so
# PROBE_TIMEOUT_SECS is a real, unconditional wall-clock cap regardless of what
# schwab-py/webbrowser does internally (see module docstring's safety finding) --
# subprocess.run's own timeout=... kills the child outright, it doesn't rely on the
# child code cooperating with any internal timeout.
_PROBE_CODE = """
import sys
sys.path.insert(0, {root!r})
import schwab_auth
c = schwab_auth.get_client(interactive=False)
# A constructed client doesn't itself prove the token refreshed successfully --
# make one cheap, real, read-only call (same call schwab_client.py's own account-list
# helper already makes elsewhere in this codebase) to confirm actual server round-trip.
r = c.get_account_numbers()
if r.status_code != 200:
    print(f"HTTP {{r.status_code}}: {{r.text[:500]}}", file=sys.stderr)
    sys.exit(1)
print("OK")
""".format(root=str(ROOT))


def _load_state():
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _token_creation_timestamp():
    """Real epoch seconds from cache/live/schwab_token.json's top-level
    creation_timestamp field, or None if the token file doesn't exist/is malformed.
    Deliberately NOT the token sub-dict's expires_at (30-min access-token expiry,
    rewritten every refresh -- irrelevant to the ~7-day refresh-token question)."""
    if not TOKEN_PATH.exists():
        return None
    try:
        d = json.loads(TOKEN_PATH.read_text())
        return d.get("creation_timestamp")
    except (json.JSONDecodeError, OSError):
        return None


def probe():
    """Returns (status, error_or_None). Runs the real get_client + get_account_numbers
    round-trip in a subprocess with a hard PROBE_TIMEOUT_SECS wall-clock cap."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", _PROBE_CODE],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        return "broken", f"probe subprocess exceeded {PROBE_TIMEOUT_SECS}s timeout"
    if result.returncode == 0 and result.stdout.strip() == "OK":
        return "healthy", None
    return "broken", (result.stderr.strip() or result.stdout.strip() or
                       f"probe subprocess exit code {result.returncode}")


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


def main():
    now = datetime.now()
    now_iso = now.isoformat(timespec="seconds")
    state = _load_state()
    prev_status = state.get("status")

    status, error = probe()
    creation_ts = _token_creation_timestamp()
    token_age_days = ((now.timestamp() - creation_ts) / 86400.0) if creation_ts else None

    # --- reactive alert logic ---
    if status == "healthy":
        if prev_status == "broken":
            _slack(f"✅ Schwab auth canary: back to healthy (was broken since "
                   f"{state.get('first_detected_broken_at', 'unknown')}).")
        state["last_known_good_at"] = now_iso
        state["first_detected_broken_at"] = None
        state["last_reactive_alert_at"] = None
    else:
        if prev_status != "broken":
            state["first_detected_broken_at"] = now_iso
            state["last_reactive_alert_at"] = now_iso
            _slack(f"🔴 Schwab auth canary: FLIPPED to broken. {error}\n"
                   f"Reauth needed (browser login) before the daemon can trade.")
        else:
            last_alert = state.get("last_reactive_alert_at")
            gap_hours = (
                (now - datetime.fromisoformat(last_alert)).total_seconds() / 3600.0
                if last_alert else 999
            )
            if gap_hours >= FOLLOWUP_NUDGE_GAP_HOURS:
                broken_since = state.get("first_detected_broken_at", "unknown")
                _slack(f"🔴 Schwab auth canary: still broken (since {broken_since}). {error}")
                state["last_reactive_alert_at"] = now_iso

    # --- proactive early-warning (independent of the reactive status above) ---
    if creation_ts is not None and token_age_days is not None and token_age_days >= PROACTIVE_WARNING_AGE_DAYS:
        already_warned = state.get("last_proactive_warning_for_creation_ts") == creation_ts
        if not already_warned:
            created_str = datetime.fromtimestamp(creation_ts).strftime("%Y-%m-%d %H:%M")
            _slack(f"⏰ Schwab token created {created_str} ({token_age_days:.1f} days ago) -- "
                   f"expiry likely in ~2 days, reauth when convenient.")
            state["last_proactive_warning_for_creation_ts"] = creation_ts

    state["last_checked_at"] = now_iso
    state["status"] = status
    _save_state(state)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps({
            "poll_timestamp": now_iso, "status": status,
            "creation_timestamp": creation_ts, "token_age_days": token_age_days,
            "error": error,
        }) + "\n")

    print(f"{now_iso}: status={status} token_age_days={token_age_days} error={error}")


if __name__ == "__main__":
    import script_usage
    script_usage.record_invocation()
    main()
