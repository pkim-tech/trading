"""Fake-venue harness (Phase 1) -- durable check that the scenario really does
reproduce `buy_fill_reconciles_correct_node`, and that its isolation gate
refuses to run against production state.

Deliberately drives the harness as a SUBPROCESS, the same way the user runs
it, rather than importing the scenario in-process: the whole point of the
isolation design is that the env vars are set before any project import, and
an in-process test (pytest has already imported signals_config/schwab_safety
by then) would test a different, weaker thing. It also keeps this test from
being able to touch the real DB even if the gate itself regressed.
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fake_venue import activity_stream, isolation  # noqa: E402
from fake_venue import scenarios_meta as meta  # noqa: E402

HARNESS = REPO_ROOT / "scripts" / "fake_venue_harness.py"
FIXED_PRICE = 250.0  # explicit override -> no network/yfinance dependency in the suite


@pytest.fixture(autouse=True)
def _restore_environ():
    """isolation.configure_env() writes straight to os.environ (it has to --
    the real entrypoint runs before any import). Inside pytest that would leak
    blanked SLACK_*/SCHWAB_ACCOUNT_*/TRADING_DB_PATH values into every later
    test in the session, so snapshot and restore around each test here."""
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _run_harness(tmp_path, extra_args=()):
    db_path = tmp_path / "fake_venue.db"
    state_dir = tmp_path / "state"
    proc = subprocess.run(
        [sys.executable, str(HARNESS), "--price", str(FIXED_PRICE),
         "--db-path", str(db_path), "--state-dir", str(state_dir), "--keep", *extra_args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600,
        env={**os.environ},
    )
    return proc, db_path


def test_harness_run_reproduces_buy_fill_reconciles_correct_node(tmp_path):
    proc, db_path = _run_harness(tmp_path)
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout[-4000:]}\n{proc.stderr[-4000:]}"
    assert db_path.exists()

    # Proof read straight out of the harness's own DB, not from the harness's
    # own report -- the report is what's being checked, so it can't be the
    # evidence for itself.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT ce.*, wl.account FROM coverage_events ce JOIN watch_list wl ON wl.id = ce.node_id "
            "WHERE ce.scenario_key = 'buy_fill_reconciles_correct_node'")]
        positions = [dict(r) for r in conn.execute(
            "SELECT wl_id, ticker, account, shares FROM open_positions ORDER BY wl_id")]
        parse_health = [dict(r) for r in conn.execute(
            "SELECT result, ticker FROM coverage_events WHERE scenario_key = 'stream_message_parsed'")]
    finally:
        conn.close()

    assert len(rows) == 1, rows
    assert rows[0]['result'] == 'resolved'
    assert rows[0]['mode'] == 'live'
    assert rows[0]['ticker'] == meta.TICKER
    assert rows[0]['account'] == meta.CASH_ALIAS
    assert '3 pending' in rows[0]['detail'], rows[0]['detail']

    # Both nodes end up with a position, each in its OWN account -- the
    # cross-account attribution the Grid row is actually about.
    assert len(positions) == 2, positions
    assert {p['account'] for p in positions} == {meta.CASH_ALIAS, meta.MARGIN_ALIAS}

    # The real stream parser ran (not a pre-parsed tuple pushed onto FILL_QUEUE).
    assert any(h['result'] == 'parsed' and h['ticker'] == meta.TICKER for h in parse_health)
    assert sum(1 for h in parse_health if h['result'] == 'received') >= 2


def test_harness_json_report_is_self_consistent(tmp_path):
    proc, _ = _run_harness(tmp_path, extra_args=("--json",))
    assert proc.returncode == 0, proc.stdout[-4000:]
    payload = json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith('{"passed"')][-1])
    assert payload['passed'] is True
    assert all(c['ok'] for c in payload['checks'] if c['required'])
    assert len(payload['proof_rows']) == 1
    assert payload['observations']['production_path_accesses'] == []

    # FIXED 2026-08-16 (docs/backlog_cache.md, both items closed): this used
    # to be a KNOWN-DEFECT TRIPWIRE tolerating either a KeyError (pre-fix) or
    # clean completion (post-fix) for the AccountNumber defect, and only
    # recorded (without asserting on) the SchwabOrderID defect. Both are now
    # fixed in production (schwab_client.resolve_account_alias_from_number,
    # signals_notify.drain_fill_queue's _order_id_int reuse) -- assert the
    # fixed behaviour directly rather than tolerating the old defect shape.
    acct_obs = payload['observations']['drain_with_real_account_number']
    assert acct_obs == 'completed without raising', acct_obs
    redelivery_obs = payload['observations']['drain_redelivery']
    assert redelivery_obs == 'completed without raising', redelivery_obs


# ---------------------------------------------------------------------------
# Isolation gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("db_path,state_dir,aliases,tickers,expect", [
    ("cache/live/trading_live.db", "/tmp/fv_state", ("fv_cash",), {meta.TICKER},
     "TRADING_DB_PATH points at (or inside) production state"),
    # Not the production DB by name, but INSIDE the production dir -- an
    # equality-only check used to let this through.
    ("cache/live/fv_harness.db", "/tmp/fv_state", ("fv_cash",), {meta.TICKER},
     "TRADING_DB_PATH points at (or inside) production state"),
    ("/tmp/fv.db", "cache/live", ("fv_cash",), {meta.TICKER}, "production state dir"),
    ("/tmp/fv.db", "cache/live/fv_state", ("fv_cash",), {meta.TICKER}, "production state dir"),
    ("/tmp/fv.db", "/tmp/fv_state", ("ira",), {meta.TICKER}, "collide with real nicknames"),
    ("/tmp/fv.db", "/tmp/fv_state", ("fv_cash",), {"SOXL"}, "real watchlist tickers"),
])
def test_isolation_gate_refuses_production_state(monkeypatch, db_path, state_dir, aliases, tickers, expect):
    for key in list(os.environ):
        if key.startswith(("SLACK_", "SCHWAB_", "TRADING_DB", "SIM_MODE")):
            monkeypatch.delenv(key, raising=False)
    isolation.configure_env(db_path, state_dir, tickers)
    with pytest.raises(isolation.IsolationError) as e:
        isolation.assert_env_isolated(aliases, tickers)
    assert expect in str(e.value)


def test_isolation_gate_refuses_unset_env(monkeypatch):
    monkeypatch.delenv("TRADING_DB_PATH", raising=False)
    monkeypatch.delenv("SCHWAB_STATE_DIR", raising=False)
    with pytest.raises(isolation.IsolationError) as e:
        isolation.assert_env_isolated(("fv_cash",), {"XLK"})
    assert "TRADING_DB_PATH is unset" in str(e.value)
    assert "SCHWAB_STATE_DIR is unset" in str(e.value)


def test_isolation_gate_refuses_leaked_slack_credentials(monkeypatch, tmp_path):
    isolation.configure_env(tmp_path / "fv.db", tmp_path / "state", {"XLK"})
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-not-a-real-token")
    with pytest.raises(isolation.IsolationError) as e:
        isolation.assert_env_isolated(("fv_cash",), {"XLK"})
    assert "SLACK_APP_TOKEN" in str(e.value)


def test_post_import_gate_catches_late_configuration(tmp_path):
    """The one failure assert_env_isolated structurally cannot see: env vars set
    AFTER signals_config/schwab_safety were imported are silently ignored (both
    read their paths once at import)."""
    script = tmp_path / "late.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import signals_config\n"                      # imported too early, on purpose
        "from fake_venue import isolation\n"
        f"isolation.configure_env({str(tmp_path / 'fv.db')!r}, {str(tmp_path / 'state')!r}, {{'XLK'}})\n"
        "try:\n"
        f"    isolation.assert_isolation_took_effect({str(tmp_path / 'fv.db')!r}, "
        f"{str(tmp_path / 'state')!r}, {{'XLK'}})\n"
        "except isolation.IsolationError as e:\n"
        "    print('CAUGHT:', e)\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit('gate did not fire')\n"
    )
    proc = subprocess.run([sys.executable, str(script)], cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=300, env={**os.environ})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "imported before configure_env" in proc.stdout


# ---------------------------------------------------------------------------
# Emitter fidelity -- the message must go through the REAL parser
# ---------------------------------------------------------------------------

def test_production_access_tripwire_catches_relative_paths(tmp_path):
    """Pinned because the first version of the hook compared only absolute
    paths -- and every hardcoded production path in this repo is written
    RELATIVE (signals_config.py's own './cache/live/trading_live.db' default
    included), so the exact breach the tripwire exists for would have printed
    '0 accesses'. Runs in a subprocess: sys.addaudithook can never be removed,
    so installing it in the pytest process would tax every later test."""
    script = tmp_path / "tripwire.py"
    script.write_text(
        "import sqlite3, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from fake_venue import isolation\n"
        "isolation.install_production_access_tripwire()\n"
        "open('cache/live/schwab_kill_switch.json').close()\n"          # relative
        "sqlite3.connect(b'cache/research/trading_universe.db').close()\n"  # relative + bytes
        "hits = isolation.production_accesses()\n"
        "assert len(hits) == 2, hits\n"
        "assert all(h.split(': ', 1)[1].startswith('/') for h in hits), hits\n"
        "print('OK', hits)\n"
    )
    proc = subprocess.run([sys.executable, str(script)], cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=120, env={**os.environ})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.startswith("OK")


def test_emitter_can_produce_a_realistic_partial_fill():
    """drain_fill_queue's whole contract is 'the stream event is only a
    wake-up signal, never trust its quantity' -- so the emitter has to be able
    to produce the partial shape that makes that matter. The scenario leg that
    drives one through reconciliation is deliberately not built yet."""
    import schwab_stream

    msg = activity_stream.build_activity_message("45111931", 42, "GDXU", "BUY", 10.0, 3.0,
                                                  leaves_quantity=7.0)
    data = json.loads(msg["content"][-1]["MESSAGE_DATA"])
    info = data["BaseEvent"]["OrderFillCompletedEventOrderLegQuantityInfo"]
    assert info["LegSubStatus"] == "LegSubStatusPartiallyFilled"
    assert info["LegStatus"] == "LegOpen"
    assert info["QuantityInfo"]["LeavesQuantity"] == {"lo": "7000000", "signScale": 12}
    # The ORDER is for 10 shares; this execution filled 3.
    assert info["OrderInfoForTransactionPosting"]["Quantity"] == {"lo": "10000000", "signScale": 12}
    events, _ = schwab_stream._parse_activity_message(msg)
    assert events == [("45111931", "GDXU", "BUY", 10.0, 3.0, "42")]


def test_emitted_message_is_decoded_by_the_real_parser():
    import schwab_stream

    msg = activity_stream.build_activity_message("45111931", 1007506544737, "GDXU", "SELL",
                                                  12.989, 2.0)
    events, health = schwab_stream._parse_activity_message(msg)
    assert events == [("45111931", "GDXU", "SELL", 12.989, 2.0, "1007506544737")]
    assert [h[0] for h in health] == ['received', 'received', 'parsed']


def test_emitted_message_uses_real_signscale_fixed_point_encoding():
    data = activity_stream.build_fill_message_data("45111931", 1, "GDXU", "BUY", 9.1813, 49.0)
    info = data["BaseEvent"]["OrderFillCompletedEventOrderLegQuantityInfo"]["ExecutionInfo"]
    # Exactly the real values quoted in _parse_activity_message's own docstring.
    assert info["ExecutionPrice"] == {"lo": "9181300", "signScale": 12}
    assert info["ExecutionQuantity"] == {"lo": "49000000", "signScale": 12}
    # signScale = scale*2 + sign_bit; a zero value omits `lo` (real-traffic shape).
    assert activity_stream.encode_decimal(-1.5) == {"lo": "1500000", "signScale": 13}
    assert activity_stream.encode_decimal(0) == {"signScale": 12}
