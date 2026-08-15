"""Persistent fake-venue harness (Phase 1).

Runs the REAL live-trading response-handling code -- schwab_stream's
account-activity parser, signals_notify's fill reconciliation
(drain_fill_queue/check_auto_fills/_reconcile_buy_fill), schwab_safety's
check_order/approve_and_record -- against tests/fake_broker.py's FakeBroker,
inside a fully isolated process (own DB file, own schwab_safety state dir, no
Slack credentials, no real account hashes).

Design: docs/design.md's 2026-08-15 (later) / 2026-08-16 / 2026-08-16
(second pass) entries. Backlog pointer: docs/backlog_cache.md's
"persistent fake-venue harness" item.

Phase 1's job is the bug class this project keeps hitting, all of it in OUR
response-handling code rather than in broker behaviour: the ACCT_ACTIVITY
parser that silently failed on 100% of real messages for weeks, the
JNUG-shaped same-ticker/SAME-account node collision (JNUG 2026-08-10 was a
dry_run canary node and a live node both in `ira`; the deliberate live
reproduction, YINN wl_id 199 + 228, is likewise both in `soxl_ira`), and the
fill-attribution races around them. Two fake accounts (one cash-settlement,
one margin) share one FakeBroker instance, matching reality (Schwab is one
counterparty across accounts); the scenario puts two of its three nodes in the
SAME account so the real ambiguity is reproduced, not designed away.

The cash/margin split itself is scaffolding for future `same_day_block`
(cash-vs-margin settlement gating) coverage -- Phase 1's scenario does not
exercise it.

Nothing here imports at module scope from the modules whose behaviour depends
on env vars (signals_config.DB_PATH, schwab_safety._STATE_DIR,
AUTOMATION_ENABLED_TICKERS are all read once at import) -- the entrypoint
(scripts/fake_venue_harness.py) sets and asserts the environment BEFORE any
project import happens, and fake_venue.isolation.assert_isolation_took_effect()
re-checks afterwards that it actually took.

Explicitly NOT in Phase 1 (see the design entries):
  - replay mode / virtual clock (Phase 2) -- market data here is real/live.
  - TRAILING_STOP auto-trigger simulation inside FakeBroker (design item 3):
    fills are driven with force_fill(), since Phase 1 studies what happens
    AFTER a fill, not how the broker decides to fill. Needed for Phase 2
    soak-testing, not for this scenario.
  - Grid credit: scripts/coverage_registry.py:71 AND scripts/evening_status.py:51
    both still hardcode the real DB path, so the harness's own coverage_events
    are invisible to the Accountability Grid and to any evening report (design
    item 5, a later-milestone gate, left alone -- fixing them is the
    prerequisite for the Phase 2 "fake evening report").
  - Thread safety (design item 10): FakeBroker's internal dicts are still
    unprotected. Moot here -- the harness drives every leg from one thread,
    calling _handle_activity_message synchronously rather than from the
    stream's own callback thread -- but it must be resolved before any
    persistent/soak mode, where the poll loop and the stream thread run
    concurrently, which is exactly what "persistent" in the item name means.
  - The production-access tripwire (isolation.install_production_access_tripwire)
    is installed by the entrypoint only. The in-process pytest tests do not get
    it; they rely on running the harness as a subprocess instead.
"""
