"""Isolation gate for the fake-venue harness -- fail loud BEFORE anything runs.

Two real incidents motivate every check here, neither hypothetical:
  - 2026-08-14: tests/test_corporate_action_detection.py had no DB isolation
    and wrote real TEST_CORP_ACTION rows into production trading_live.db all
    week, undetected.
  - 2026-07-23: an unisolated harness wrote real dry-run BUY attempts into the
    real schwab_order_counts.json, driving the live `ira` account's
    daily_order_cap counter toward its actual limit. Same category bit again
    during this session's abnormal-drift-alert build (schwab_safety.
    NODE_AUTOMATION_PATH).

Deliberately stdlib-only and project-import-free: assert_env_isolated() must
be callable before signals_config/schwab_safety are imported, since both read
their paths once at import time.
"""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_DB = REPO_ROOT / "cache" / "live" / "trading_live.db"
PRODUCTION_STATE_DIR = REPO_ROOT / "cache" / "live"

# Every real account alias (signals_db.ensure_tables' seeded `accounts` rows).
# A fake alias colliding with one of these is the single remaining way a fake
# order could resolve to a real Schwab account hash -- see docs/design.md's
# 2026-08-16 entry (the rebuttal that narrowed "real order placement is
# exposed" down to exactly this).
REAL_ACCOUNT_ALIASES = {"brokerage", "ira", "roth", "soxl_ira", "sep"}

_SLACK_ENV_VARS = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL", "SLACK_WEBHOOK_URL")


class IsolationError(RuntimeError):
    """Raised when the harness environment could touch production state."""


def _is_within(path, parent):
    try:
        return Path(path).resolve().is_relative_to(Path(parent).resolve())
    except (AttributeError, ValueError):  # py<3.9 / cross-device
        return str(Path(path).resolve()).startswith(str(Path(parent).resolve()) + os.sep)


def _env_file_account_aliases():
    """Account aliases named by SCHWAB_ACCOUNT_<ALIAS> lines in .env.

    Parsed directly (stdlib, no dotenv import) because assert_env_isolated
    runs BEFORE signals_config's load_dotenv() -- so a suffix that lives only
    in .env is invisible to os.environ at gate time, and blanking only the
    hardcoded 5 aliases would silently miss a 6th real account added later."""
    aliases = set()
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return aliases
    try:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("SCHWAB_ACCOUNT_") and "=" in line:
                aliases.add(line.split("=", 1)[0][len("SCHWAB_ACCOUNT_"):].strip().lower())
    except OSError:
        pass
    return aliases


def configure_env(db_path, state_dir, automation_tickers):
    """Sets every env var the harness depends on. MUST be called before the
    first project import -- signals_config.DB_PATH, schwab_safety._STATE_DIR
    and schwab_safety.AUTOMATION_ENABLED_TICKERS are all import-time reads.

    Slack vars are set to "" rather than deleted: load_dotenv() (called at
    signals_config import) only fills keys ABSENT from os.environ, so an
    empty string here beats the real .env value and forces SOCKET_MODE=False
    with no code change (design 2026-08-16 second pass, item 6).

    SCHWAB_ACCOUNT_<ALIAS> vars are blanked for the same reason: with no
    suffix set, schwab_client._resolve_account_hashes() cannot map any alias
    to a real account hash even if the preset _account_hashes cache were ever
    reset mid-run.
    """
    os.environ["TRADING_DB_PATH"] = str(db_path)
    os.environ["SCHWAB_STATE_DIR"] = str(state_dir)
    os.environ["SIM_MODE"] = "1"
    os.environ["SCHWAB_AUTOMATION_TICKERS"] = ",".join(sorted(automation_tickers))
    for var in _SLACK_ENV_VARS:
        os.environ[var] = ""
    for key in list(os.environ):
        if key.startswith("SCHWAB_ACCOUNT_"):
            os.environ[key] = ""
    for alias in REAL_ACCOUNT_ALIASES | _env_file_account_aliases():
        os.environ[f"SCHWAB_ACCOUNT_{alias.upper()}"] = ""


def assert_env_isolated(fake_aliases, automation_tickers):
    """Raises IsolationError unless the process environment is provably
    incapable of touching production DB/state/Slack/accounts."""
    problems = []

    db_env = os.environ.get("TRADING_DB_PATH", "")
    if not db_env:
        problems.append("TRADING_DB_PATH is unset -- signals_config would default to the real trading_live.db")
    else:
        db = Path(db_env).resolve()
        # Containment, not equality: `cache/live/anything.db` would otherwise
        # pass the gate and scatter harness state through the production dir.
        if db == PRODUCTION_DB.resolve() or db.name == PRODUCTION_DB.name \
                or _is_within(db, PRODUCTION_STATE_DIR):
            problems.append(f"TRADING_DB_PATH points at (or inside) production state ({db})")

    state_env = os.environ.get("SCHWAB_STATE_DIR", "")
    if not state_env:
        problems.append("SCHWAB_STATE_DIR is unset -- schwab_safety would use the real cache/live state files")
    else:
        state_dir = Path(state_env).resolve()
        if state_dir == PRODUCTION_STATE_DIR.resolve() or _is_within(state_dir, PRODUCTION_STATE_DIR):
            problems.append(f"SCHWAB_STATE_DIR points at (or inside) the production state dir "
                            f"({state_dir})")

    for var in _SLACK_ENV_VARS:
        if os.environ.get(var):
            problems.append(f"{var} is set -- the harness must not open a second Socket Mode connection "
                            f"or post to the real channel")

    if os.environ.get("SIM_MODE", "1") == "0":
        problems.append("SIM_MODE=0 -- the harness must never run in real-alert mode")

    real_scope = set(automation_tickers) & _REAL_TICKERS_HINT
    if real_scope:
        problems.append(f"automation scope includes real watchlist tickers: {sorted(real_scope)}")
    env_scope = {t.strip() for t in os.environ.get("SCHWAB_AUTOMATION_TICKERS", "").split(",") if t.strip()}
    if env_scope != set(automation_tickers):
        problems.append(f"SCHWAB_AUTOMATION_TICKERS={sorted(env_scope)} does not match the harness's own "
                        f"scope {sorted(automation_tickers)} -- the real .env scope must never leak in")

    collisions = set(fake_aliases) & (REAL_ACCOUNT_ALIASES | _env_file_account_aliases())
    if collisions:
        problems.append(f"fake account aliases collide with real nicknames: {sorted(collisions)}")

    leaked_suffixes = sorted(k for k in os.environ
                             if k.startswith("SCHWAB_ACCOUNT_") and os.environ[k])
    if leaked_suffixes:
        problems.append(f"real account-number suffixes still set: {leaked_suffixes}")

    if problems:
        raise IsolationError("fake-venue harness isolation check FAILED:\n  - " + "\n  - ".join(problems))


# Not a gate on correctness, just a loud tripwire: a harness scenario should
# never name a ticker that a real node trades, so a stray un-isolated run can't
# be confused with real activity in any log or DB.
_REAL_TICKERS_HINT = {
    'AGQ', 'CURE', 'DFEN', 'DIA', 'DPST', 'DUST', 'EDC', 'ERX', 'ERY', 'ETHU', 'FAS', 'FAZ',
    'GDXD', 'GDXU', 'HIBL', 'IVV', 'IWM', 'JDST', 'JNUG', 'KORU', 'LABD', 'LABU', 'MULL',
    'NAIL', 'NUGT', 'QID', 'QQQ', 'RETL', 'SDOW', 'SH', 'SOXL', 'SOXS', 'SPXU', 'SPY', 'TMF',
    'TQQQ', 'TWM', 'UDOW', 'USD', 'UVIX', 'VOO', 'VRTL', 'XLF', 'YANG', 'YINN', 'ZSL',
}


_PRODUCTION_ACCESSES = []


def install_production_access_tripwire():
    """Records every attempt to open a file (or sqlite DB) under the real
    cache/live directory, via sys.addaudithook.

    This is the empirical half of the isolation story: assert_env_isolated()
    proves the ENV is right, this proves nothing actually reached production
    anyway -- covering the failure modes an env check structurally can't
    (a module that hardcodes a path instead of reading TRADING_DB_PATH, a
    lazily-cached global captured before the override, a real schwab_auth
    token read). Records rather than raises: an exception thrown from inside
    an audit hook lands in arbitrary library code with no useful stack, and
    the harness fails the run on a non-empty record anyway.

    Audit hooks cannot be uninstalled once added -- fine for the harness's
    own short-lived process, which is why this is called from the entrypoint
    and not from library code."""
    import os
    import sys

    # REPO_ROOT/cache, not just cache/live: cache/research/trading_universe.db
    # (signals_config.py:18) has NO env override at all, so if a harness code
    # path ever reached it there would be no other way to notice.
    watched = str((REPO_ROOT / "cache").resolve())

    def _hook(event, args):
        if event not in ("open", "sqlite3.connect") or not args:
            return
        path = args[0]
        if isinstance(path, int):    # open() on an existing fd
            return
        try:
            # os.fsdecode handles the bytes paths sqlite3.connect passes
            # through (str(b'/x') is "b'/x'", which matched nothing), and
            # abspath resolves the RELATIVE form -- which is the form every
            # hardcoded path in this repo actually uses, including
            # signals_config.py's own "./cache/live/trading_live.db" default.
            # A relative-path miss would print "0 accesses" for precisely the
            # breach this hook exists to catch. abspath is pure string math
            # plus os.getcwd(), neither of which raises an audit event, so
            # there's no re-entrancy hazard here.
            text = os.path.abspath(os.fsdecode(path))
        except Exception:
            return
        if text.startswith(watched):
            _PRODUCTION_ACCESSES.append(f"{event}: {text}")

    sys.addaudithook(_hook)


def production_accesses():
    """Every production-path access seen since the tripwire was installed."""
    return list(_PRODUCTION_ACCESSES)


def assert_isolation_took_effect(db_path, state_dir, automation_tickers):
    """Post-import verification that the env vars were actually read by the
    real modules -- catches the one mistake assert_env_isolated() structurally
    cannot: importing a project module BEFORE configure_env() ran (all three
    of these are import-time reads, so a late env var is silently ignored)."""
    import signals_config as cfg
    import schwab_safety

    problems = []
    if Path(cfg.DB_PATH).resolve() != Path(db_path).resolve():
        problems.append(f"signals_config.DB_PATH is {cfg.DB_PATH} (expected {db_path}) -- a project module "
                        f"was imported before configure_env() ran")
    if Path(schwab_safety.STATE_PATH).parent.resolve() != Path(state_dir).resolve():
        problems.append(f"schwab_safety state files live in {schwab_safety.STATE_PATH.parent} "
                        f"(expected {state_dir})")
    for name in ("KILL_SWITCH_PATH", "TICKER_AUTOMATION_PATH", "NODE_AUTOMATION_PATH",
                 "AUTO_FILL_DETECTION_PATH", "NODE_AUTO_FILL_DETECTION_PATH", "NODE_BREAKER_PATH",
                 "AUTOMATION_SCOPE_STATE_PATH"):
        path = Path(getattr(schwab_safety, name)).resolve()
        if path.parent != Path(state_dir).resolve():
            problems.append(f"schwab_safety.{name} is {path} -- outside the harness state dir")
    # signals_config.ORPHAN_SWEEP_STATE_PATH -- not a schwab_safety.py constant,
    # but keys off the same SCHWAB_STATE_DIR var (2026-08-16 fix), so it belongs
    # in this same tripwire loop rather than going unchecked (the gap that let
    # an unisolated orphan-sweep test almost read/write real production state).
    orphan_path = Path(cfg.ORPHAN_SWEEP_STATE_PATH).resolve()
    if orphan_path.parent != Path(state_dir).resolve():
        problems.append(f"signals_config.ORPHAN_SWEEP_STATE_PATH is {orphan_path} -- outside the harness state dir")
    if cfg.SOCKET_MODE:
        problems.append("signals_config.SOCKET_MODE is True -- real Slack credentials leaked into the harness")
    if cfg.SLACK_HOOK:
        # SIM_MODE only prefixes the text; _post_message still posts to a
        # webhook whenever SLACK_HOOK is truthy (signals_blocks.py).
        problems.append("signals_config.SLACK_HOOK is set -- the harness would post to a real webhook")
    leaked_suffixes = sorted(k for k in os.environ
                             if k.startswith("SCHWAB_ACCOUNT_") and os.environ[k])
    if leaked_suffixes:
        # Re-checked here, post-load_dotenv: the pre-import check can only see
        # os.environ, and .env values don't land there until signals_config
        # imports.
        problems.append(f"real account-number suffixes present after load_dotenv(): {leaked_suffixes}")
    if not cfg.SIM_MODE:
        problems.append("signals_config.SIM_MODE is False")
    if schwab_safety.AUTOMATION_ENABLED_TICKERS != set(automation_tickers):
        problems.append(f"schwab_safety.AUTOMATION_ENABLED_TICKERS is "
                        f"{sorted(schwab_safety.AUTOMATION_ENABLED_TICKERS)} "
                        f"(expected {sorted(automation_tickers)})")
    if problems:
        raise IsolationError("fake-venue harness post-import isolation check FAILED:\n  - "
                             + "\n  - ".join(problems))
