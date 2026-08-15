#!/usr/bin/env python3
"""
Persistent fake-venue harness, Phase 1 entrypoint (backlog item:
"persistent fake-venue harness", docs/backlog_cache.md; design:
docs/design.md's 2026-08-15 (later) / 2026-08-16 / 2026-08-16 (second pass)
entries).

Runs the real live-trading response-handling code -- schwab_stream's
ACCT_ACTIVITY parser, signals_notify's fill reconciliation, schwab_safety's
check_order -- against tests/fake_broker.py's FakeBroker, in its own OS
process with its own DB file and its own schwab_safety state dir, and proves
`buy_fill_reconciles_correct_node` (the Grid row with no live proof, blocked
for months on "no two live nodes share a ticker") end to end.

Isolation is asserted BEFORE any project module is imported and re-verified
after (signals_config.DB_PATH / schwab_safety._STATE_DIR /
AUTOMATION_ENABLED_TICKERS are all import-time reads, so a late env var is
silently ignored -- that failure mode is what the second assert catches).

Usage:
    .venv/bin/python scripts/fake_venue_harness.py                 # real live price
    .venv/bin/python scripts/fake_venue_harness.py --price 250.0   # offline/deterministic
    .venv/bin/python scripts/fake_venue_harness.py --db-path ... --state-dir ... --keep

Exit code 0 = every required check passed AND the proof query found the real
coverage_events row; 1 otherwise.
"""
import argparse
import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fake_venue import isolation  # noqa: E402  (stdlib-only, safe before configure_env)
from fake_venue import scenarios_meta  # noqa: E402  (constants only, no project imports)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-path", default=None,
                    help="harness DB file (default: a fresh temp dir, removed on exit)")
    ap.add_argument("--state-dir", default=None,
                    help="harness schwab_safety state dir (default: a fresh temp dir)")
    ap.add_argument("--price", type=float, default=None,
                    help="quote override; omit to pull a real live price (yfinance)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the harness DB/state dir after the run (for inspection)")
    ap.add_argument("--json", action="store_true", help="machine-readable result on stdout")
    args = ap.parse_args()

    tmp_root = None
    if args.db_path is None or args.state_dir is None:
        tmp_root = Path(tempfile.mkdtemp(prefix="fake_venue_"))
    db_path = Path(args.db_path) if args.db_path else tmp_root / "fake_venue.db"
    state_dir = Path(args.state_dir) if args.state_dir else tmp_root / "state"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    tickers = {scenarios_meta.TICKER}
    aliases = scenarios_meta.ALIASES

    # --- isolation gate, before ANY project import ------------------------
    isolation.install_production_access_tripwire()
    isolation.configure_env(db_path, state_dir, tickers)
    isolation.assert_env_isolated(aliases, tickers)
    isolation.assert_isolation_took_effect(db_path, state_dir, tickers)  # imports happen here

    from fake_venue import scenarios  # noqa: E402  (must follow configure_env)

    print(f"[isolation] DB        {db_path}")
    print(f"[isolation] state dir {state_dir}")
    print(f"[isolation] scope     {sorted(tickers)}  accounts {sorted(aliases)}")

    failed = 0
    checks, observations, proof_rows = [], {}, []
    scenario_error = None
    try:
        checks, observations = scenarios.run(price=args.price)
        proof_ok, proof_rows = scenarios.verify_proof(db_path)

        print("\n--- checks " + "-" * 60)
        for c in checks:
            tag = "PASS" if c.ok else ("FAIL" if c.required else "note")
            print(f"  [{tag}] {c.name}" + (f"\n         {c.detail}" if c.detail else ""))
            if not c.ok and c.required:
                failed += 1

        print("\n--- proof query (fresh read-only connection to the harness DB) ---")
        print(scenarios.PROOF_SQL.strip())
        for row in proof_rows:
            print(f"  -> {row}")
        if not proof_ok:
            print("  -> PROOF FAILED: no single 'resolved' buy_fill_reconciles_correct_node row")
            failed += 1

    except Exception as e:
        # Caught, not propagated: a scenario that blows up is exactly when the
        # isolation verdict below matters most, so it must still be reported
        # (and still fail the run) rather than being skipped by an unwinding
        # stack.
        scenario_error = traceback.format_exc()
        failed += 1
        print(f"\n!! scenario raised: {type(e).__name__}: {e}")
    finally:
        # ALWAYS reported, breach or not, scenario error or not.
        prod_hits = isolation.production_accesses()
        observations['production_path_accesses'] = prod_hits
        print("\n--- isolation tripwire (audit hook on every file/sqlite open under cache/) ---")
        if prod_hits:
            for hit in prod_hits[:20]:
                print(f"  !! {hit}")
            print(f"  -> {len(prod_hits)} production-path access(es) — ISOLATION BREACH")
            failed += 1
        else:
            print("  -> 0 accesses to cache/live/ or cache/research/ during the entire run")

        print("\n--- observations " + "-" * 54)
        for k, v in observations.items():
            print(f"  {k}: {v}")
        if scenario_error:
            print("\n--- traceback " + "-" * 57)
            print(scenario_error)

        if args.json:
            print(json.dumps({
                'passed': failed == 0,
                'checks': [{'name': c.name, 'ok': c.ok, 'required': c.required} for c in checks],
                'proof_rows': proof_rows,
                'observations': observations,
                'error': scenario_error,
            }, default=str))

        if tmp_root is not None and not args.keep:
            shutil.rmtree(tmp_root, ignore_errors=True)
        elif args.keep:
            print(f"\n[kept] {db_path}  |  {state_dir}")

    print(f"\n{'PASS' if failed == 0 else 'FAIL'} — {failed} failing check(s)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
