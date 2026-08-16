"""
Shared config/state for the active_signals split: paths, Slack tokens, the
Slack Bolt app singleton, and the SIM_MODE/INTERACTIVE flags.

Other signals_* modules read these via `import signals_config as cfg; cfg.X`
(attribute access), never `from signals_config import X` for anything mutable
(DB_PATH, SLACK_CHANNEL_ID) -- the latter would copy the value at import time
and silently stop tracking monkeypatches/runtime mutation (e.g.
_resolve_channel_id() setting SLACK_CHANNEL_ID after Socket Mode connects).
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DB_PATH          = Path(os.environ.get("TRADING_DB_PATH", "./cache/live/trading_live.db"))
RESEARCH_DB_PATH = Path("./cache/research/trading_universe.db")
CACHE_DIR        = Path("./cache")
LIVE_DIR         = CACHE_DIR / "live"
RESEARCH_DIR     = CACHE_DIR / "research"
CONFIG_PATH      = Path("./config.json")
POLL_SECS        = int(os.environ.get("SIGNAL_POLL_SECS", 300))
SLACK_HOOK       = os.environ.get("SLACK_WEBHOOK_URL", "")

# Dollar bar a live node's starting_notional must cross to count as genuine
# capital at stake (2026-08-08 user call) -- below this, Slack alerting
# (routine AND anomaly) is suppressed in favor of EOD-only review; the
# underlying event/incident logging is never suppressed regardless of this
# threshold. See signals_helpers.has_capital_at_stake. Lowered 10,000->5,000
# on 2026-08-13 (user's explicit call) so brokerage's 3 real $6,000 nodes
# (AGQ/ETHU/JNUG) cross it -- the $10k default put them on the wrong side,
# suppressing real-money alerts the user wanted visible while extending this
# same gate to a broader set of previously-ungated Slack call sites
# (canary/dry_run fills, reminder loops) as the general noise-reduction
# filter. soxl_ira's nodes (max $2,500) still stay under it either way.
CAPITAL_AT_STAKE_THRESHOLD = float(os.environ.get("CAPITAL_AT_STAKE_THRESHOLD", 5_000))

# Persisted marker for signals_notify.check_intraday_risk_review -- survives
# a daemon restart (unlike an in-memory set) so a restart mid-window doesn't
# either re-alert on an already-seen incident or silently skip a new one.
INTRADAY_RISK_REVIEW_STATE_PATH = LIVE_DIR / "intraday_risk_review_state.json"

# Persisted last-checked-date marker for signals_notify.check_addon_buying_power_drift
# -- once/day is enough (real broker balance calls), and surviving a restart
# avoids re-checking (and potentially re-alerting) on the same day.
ADDON_BUYING_POWER_DRIFT_STATE_PATH = LIVE_DIR / "addon_buying_power_drift_state.json"

# Persisted throttle for the intraday broker orphan sweep (Stage D, 2026-08-15).
# Same durability pattern as the two state paths above -- an in-memory-only
# throttle resets on every daemon restart, and this project restarts the daemon
# deliberately and often (the morning restart is a documented manual step), so a
# within-window restart would re-sweep immediately instead of honouring the
# 30-minute interval.
# Honors SCHWAB_STATE_DIR (2026-08-16, backlog item "ORPHAN_SWEEP_STATE_PATH
# has no env-var override") -- the same env var schwab_safety.py's own state
# paths (STATE_PATH, KILL_SWITCH_PATH, etc., see schwab_safety.py:36) already
# key off, even though this constant lives in signals_config.py rather than
# schwab_safety.py: conceptually it's the same "schwab safety state" family
# (a throttle for the broker orphan-position sweep), and reusing the existing
# var lets fake_venue's harness isolate it automatically via configure_env()
# instead of requiring a dedicated per-scenario monkeypatch.
ORPHAN_SWEEP_STATE_PATH = Path(os.environ.get("SCHWAB_STATE_DIR", str(LIVE_DIR))) / "orphan_sweep_state.json"

LOG_DIR = Path("./logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
HUMAN_LOG_PATH   = LOG_DIR / "active_signals.log"
VERBOSE_LOG_PATH = LOG_DIR / "active_signals_verbose.log"
HEARTBEAT_PATH   = LIVE_DIR / "active_signals_heartbeat.txt"


class _Tee:
    """Mirrors writes to multiple streams — used to log to a file without losing
    the live console output when running `active_signals.py run` interactively."""
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            s.write(data)

    def flush(self):
        for s in self._streams:
            s.flush()


SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
SLACK_CHANNEL   = os.environ.get("SLACK_CHANNEL", "")
SOCKET_MODE     = bool(SLACK_BOT_TOKEN and SLACK_APP_TOKEN and SLACK_CHANNEL)

SLACK_CHANNEL_ID = ""
# Fail-safe default, flipped 2026-08-01: SIM_MODE is ON unless explicitly set
# to "0" -- previously defaulted OFF (opt-in safety), which meant any ad hoc
# dev/test invocation of Slack-posting code that forgot to export SIM_MODE=1
# would post real, unprefixed messages to the live channel (found live: a
# one-off test call to build_eod_scenario_review posted a real EOD report
# outside its normal schedule). The real daemon (active_signals.py run_loop)
# must now explicitly set SIM_MODE=0 to go live -- see the "How to Run"
# section of CLAUDE.md for the exact launch command, and
# signals_invariants.check_sim_mode_off_for_real_daemon(), which alerts
# loudly if the real daemon ever actually starts with this still True
# (should never legitimately happen -- SIM_MODE is for scripts/live_sim.py's
# isolated-DB REPL, not the persistent production process).
SIM_MODE         = os.environ.get("SIM_MODE", "1") != "0"
SIM_SCENARIO     = os.environ.get("SIM_SCENARIO", "")
# Interactive buttons/reminders require the process's own Socket Mode connection to be
# the one Slack delivers the click to. The sim never starts a SocketModeHandler (only
# run_loop() does), so if it rendered real buttons, a click would be delivered to
# whichever *other* process (the live daemon) happens to be connected — using sim data
# against the live DB. SIM_MODE forces the plain-text/typed-input fallback instead.
INTERACTIVE      = SOCKET_MODE and not SIM_MODE

if SOCKET_MODE:
    from slack_bolt import App
    bolt_app = App(token=SLACK_BOT_TOKEN)
else:
    bolt_app = None


def _resolve_channel_id():
    global SLACK_CHANNEL_ID
    if SLACK_CHANNEL_ID or not SOCKET_MODE:
        return
    try:
        r = bolt_app.client.chat_postMessage(channel=SLACK_CHANNEL, text="Signal monitor online.")
        SLACK_CHANNEL_ID = r['channel']
    except Exception as e:
        print(f"  [slack] could not resolve channel ID: {e}")
