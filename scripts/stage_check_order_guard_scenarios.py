"""Script-based test-plan staging for schwab_safety.check_order's guard-rejection
rows (docs/grid_ticker_coverage_promotion_process.md's "Testing philosophy" section,
test-plan kind 2) -- forces a real CANARY-tier coverage_events row for guard branches
that no steady-state node config can organically trip (see docs/backlog_cache.md's
2026-08-14 item and docs/design.md's 2026-08-15 entry for the full design writeup).

Covers 8 rows, all via ONE mechanism: a direct schwab_safety.check_order(...) call
(never schwab_client, never approve_and_record -- so this NEVER places a real order,
NEVER touches the broker, and NEVER increments any real order-count state file) against
a synthetic ticker (TICKER, never a real traded symbol) and the real 'sep' account,
which is genuinely trading_enabled=False in the live accounts table as of 2026-08-15
(confirmed via direct query -- the only such account right now; re-verify before
relying on this if accounts have changed since):
  - ticker_not_live_mode_block        (zero patching needed -- see below)
  - ticker_not_in_automation_scope_block
  - ticker_account_assignment_mismatch
  - ticker_level_automation_pause
  - buy_trading_day_block
  - buy_signal_window_block
  - hard_order_ceiling_block
  - notional_cap_block

The two "still needs a code check" rows from the backlog item (ticker_not_live_mode_
block / ticker_not_in_automation_scope_block) are included here too: tracing active_
signals.py's scan functions confirmed every real automated order-placement call site
already filters `ticker in AUTOMATION_ENABLED_TICKERS` before ever reaching schwab_
client (active_signals.py lines ~297/334/572/649/1252, plus every notify_*/signals_
notify.py gate) -- so ticker_not_in_automation_scope_block is structurally pre-empted
from firing organically and genuinely needs staging, same as the 6 backlog-named rows.
ticker_not_live_mode_block is NOT structurally pre-empted (check_order's own comment
documents a real, if rare, mid-poll-cycle race: a node demoted/removed between signal
computation and order placement) but forcing it deterministically is equally cheap via
the same mechanism, so it's included rather than left to wait on a lucky race.

Two more rows, account_disabled_block and global_burst_cap_block, are ALSO staged
here now (2026-08-16) -- overriding docs/grid_ticker_coverage_promotion_process.md's
prior not-prod-required guidance for those two rows, per explicit user sign-off
2026-08-16 (see docs/design.md's 2026-08-15/16 entries) to stage them the SAFE way
found in that session, described in each scenario's own comment below:
  - account_disabled_block: a synthetic account key (never a real alias) is inserted
    into the schwab_safety.ACCOUNTS singleton with enabled=False, called against, then
    removed in a finally block -- no real account row is ever read, mutated, or
    written to the DB; ACCOUNTS is a plain dict subclass and this script's own process
    is the only thing that ever sees the synthetic key.
  - global_burst_cap_block: check_order's own `counts` param (see its docstring) is
    used to hand it a synthetic in-memory dict that already looks like it's at the
    12-orders-per-minute threshold, instead of reading the real
    cache/live/schwab_order_counts.json state file -- the real file is never opened,
    read, or written by this scenario.

Mechanism per scenario (all self-reversing, all in-process only):
  - AUTOMATION_ENABLED_TICKERS: a plain mutable module-level set -- .add()/.discard()
    the synthetic TICKER around the calls that need it in scope.
  - schwab_safety._live_ticker_accounts: a plain function, patched via unittest.mock.
    patch to return a fixed {TICKER: {...}} dict instead of querying the real
    watch_list -- avoids creating any real watch_list row ("no node needed", per the
    backlog item's own framing).
  - schwab_safety._is_trading_day / _now: patched the same way the project's own
    tests/test_fake_broker_check_order_guards_phase3_scenario.py already does for
    these exact rows (both are documented "seam for tests to monkeypatch" points).
  - ticker_level_automation_pause: calls the REAL pause_ticker_automation/resume_
    ticker_automation functions (the actual Slack-button code path) against the
    synthetic TICKER only -- this DOES write to the real, shared schwab_ticker_
    automation.json state file the live daemon reads, but only ever under a ticker
    name no real node uses, and always resumed in a try/finally.

Every scenario asserts the real signals_db.get_coverage_events(scenario_key=...) row
actually landed with mode='dry_run' -- not just that SafetyViolation was raised --
matching the project's existing fake-venue test convention (assert the log call
fired, not just the exception).

Usage: .venv/bin/python scripts/stage_check_order_guard_scenarios.py
"""
import sys
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals_db as db
import schwab_safety
import schwab_client

ACCOUNT = "sep"  # real, trading_enabled=False as of 2026-08-15 -- reconfirm live if stale
TICKER = "STAGE_GUARD_TEST"  # synthetic -- never a real traded symbol, never in watch_list
IN_WINDOW_TIME = datetime(2026, 7, 30, 10, 30)  # a real NYSE trading day, in-window


def _recent_event(scenario_key):
    events = db.get_coverage_events(scenario_key=scenario_key)
    return any(e["ticker"] == TICKER and e["result"] == "blocked" and e["mode"] == "dry_run"
               for e in events)


def _run(grid_id, patches, kwargs, account=ACCOUNT):
    """patches: list of (target_obj, attr, value) for unittest.mock.patch.object.
    Runs check_order, expects SafetyViolation, confirms the real coverage_events row."""
    with ExitStack() as stack:
        for obj, attr, value in patches:
            stack.enter_context(patch.object(obj, attr, value))
        try:
            schwab_safety.check_order(account=account, ticker=TICKER, side="BUY",
                                       source='fixture:stage_check_order_guard_scenarios', **kwargs)
            print(f"  [FAIL] {grid_id}: check_order did NOT raise -- guard did not fire")
            return False
        except schwab_safety.SafetyViolation:
            pass
        except Exception as e:
            print(f"  [FAIL] {grid_id}: unexpected exception type {type(e).__name__}: {e}")
            return False
    if _recent_event(grid_id):
        print(f"  [PASS] {grid_id}: SafetyViolation raised + coverage_events(mode=dry_run) confirmed")
        return True
    print(f"  [FAIL] {grid_id}: SafetyViolation raised but no matching coverage_events row found")
    return False


def main():
    db.ensure_tables()
    if ACCOUNT not in schwab_safety.ACCOUNTS or schwab_safety.ACCOUNTS[ACCOUNT].trading_enabled:
        print(f"ABORT: '{ACCOUNT}' is not a real, currently trading_enabled=False account -- "
              f"re-check schwab_safety.ACCOUNTS before running (this script must never run "
              f"against a real-money account).")
        sys.exit(1)

    results = {}

    # 1. ticker_not_live_mode_block -- zero patching: TICKER is synthetic and never
    #    appears in the real watch_list, so the real _live_ticker_accounts() naturally
    #    excludes it.
    results["ticker_not_live_mode_block"] = _run(
        "ticker_not_live_mode_block", [], dict(quantity=10, price=10.0))

    happy_accounts = {TICKER: {ACCOUNT}}

    # 2. ticker_not_in_automation_scope_block -- ticker assigned to this account
    #    (patched), but deliberately NOT added to AUTOMATION_ENABLED_TICKERS.
    results["ticker_not_in_automation_scope_block"] = _run(
        "ticker_not_in_automation_scope_block",
        [(schwab_safety, "_live_ticker_accounts", lambda: happy_accounts)],
        dict(quantity=10, price=10.0))

    # 3. ticker_account_assignment_mismatch -- ticker assigned to a DIFFERENT account.
    mismatch_accounts = {TICKER: {"roth"}}
    results["ticker_account_assignment_mismatch"] = _run(
        "ticker_account_assignment_mismatch",
        [(schwab_safety, "_live_ticker_accounts", lambda: mismatch_accounts)],
        dict(quantity=10, price=10.0))

    # From here on: "happy path" preconditions (ticker assigned to ACCOUNT + in
    # automation scope) so the call reaches the specific guard under test.
    # _open_orders patched too (found only by actually running this script, not by
    # the earlier syntax-check pass): scenarios 4-8/10 below all reach the real BUY
    # path's dup-order guard, which calls schwab_safety._open_orders(ACCOUNT) --
    # 'sep' has no SCHWAB_ACCOUNT_SEP entry in .env, so schwab_client.
    # _resolve_account_hashes()['sep'] raises a real KeyError before any guard past
    # that point can even be reached. Returning [] here means no resting order for
    # the synthetic TICKER is ever claimed to exist -- correct for a ticker that
    # never appears in any real order book -- and, same as every other patch in
    # this script, means zero real broker calls, not just zero real order placement.
    happy_patches = [
        (schwab_safety, "_live_ticker_accounts", lambda: happy_accounts),
        (schwab_safety, "_open_orders", lambda account: []),
    ]
    schwab_safety.AUTOMATION_ENABLED_TICKERS.add(TICKER)
    try:
        # 4. ticker_level_automation_pause -- real pause/resume function calls.
        schwab_safety.pause_ticker_automation(TICKER, reason="stage_check_order_guard_scenarios.py")
        try:
            results["ticker_level_automation_pause"] = _run(
                "ticker_level_automation_pause", happy_patches, dict(quantity=10, price=10.0))
        finally:
            schwab_safety.resume_ticker_automation(TICKER)

        # 5. buy_trading_day_block -- force _is_trading_day False, independent of
        #    whatever real date the script happens to run on.
        results["buy_trading_day_block"] = _run(
            "buy_trading_day_block",
            happy_patches + [(schwab_safety, "_is_trading_day", lambda date_str: False)],
            dict(quantity=10, price=10.0))

        # 6. buy_signal_window_block -- force _is_trading_day True (isolate from #5)
        #    and _now to noon on a known trading day, outside every signal/open-check
        #    window.
        results["buy_signal_window_block"] = _run(
            "buy_signal_window_block",
            happy_patches + [
                (schwab_safety, "_is_trading_day", lambda date_str: True),
                (schwab_safety, "_now", lambda: IN_WINDOW_TIME.replace(hour=12, minute=0)),
            ],
            dict(quantity=10, price=10.0))

        # 7/8/10 all need the same in-window/trading-day preconditions, extracted once.
        in_window_patches = happy_patches + [
            (schwab_safety, "_is_trading_day", lambda date_str: True),
            (schwab_safety, "_now", lambda: IN_WINDOW_TIME),
        ]

        # 7. hard_order_ceiling_block -- notional > $100k (HARD_ORDER_CEILING),
        #    in-window/trading-day so the earlier BUY-only time gates don't shadow it.
        over_ceiling_qty = (schwab_safety.HARD_ORDER_CEILING // 10) + 1
        results["hard_order_ceiling_block"] = _run(
            "hard_order_ceiling_block", in_window_patches, dict(quantity=over_ceiling_qty, price=10.0))

        # 8. notional_cap_block -- notional above sep's real notional_cap ($10k as of
        #    2026-08-15) but below HARD_ORDER_CEILING, so #7 doesn't shadow it.
        cap = schwab_safety.ACCOUNTS[ACCOUNT].notional_cap
        over_cap_qty = int(cap // 10) + 10
        results["notional_cap_block"] = _run(
            "notional_cap_block", in_window_patches, dict(quantity=over_cap_qty, price=10.0))
    finally:
        schwab_safety.AUTOMATION_ENABLED_TICKERS.discard(TICKER)

    # 9. account_disabled_block -- a synthetic account key, never a real alias,
    #    inserted into the ACCOUNTS singleton with enabled=False and removed in a
    #    finally block. ACCOUNTS is a plain dict subclass (schwab_safety.py's
    #    _AccountsDict) -- assignment/deletion on it is an ordinary in-memory dict
    #    op, not a DB write, and nothing else in THIS process touches this key.
    #    This never mutates any real account row: no schwab_safety.reload_accounts()
    #    call happens in between, so the synthetic key can't leak into a fresh load,
    #    and the real 'sep'/etc. rows in ACCOUNTS are never read or written here.
    SYNTH_DISABLED_ACCOUNT = "STAGE_DISABLED_ACCOUNT_TEST"
    assert SYNTH_DISABLED_ACCOUNT not in schwab_safety.ACCOUNTS, (
        "synthetic account key already exists in ACCOUNTS -- refusing to overwrite, aborting")
    schwab_safety.ACCOUNTS[SYNTH_DISABLED_ACCOUNT] = schwab_safety.AccountLimits(
        enabled=False, notional_cap=100.0, daily_order_cap=1, trading_enabled=False,
        cash_settlement_type="cash",
    )
    try:
        results["account_disabled_block"] = _run(
            "account_disabled_block", [], dict(quantity=10, price=10.0), account=SYNTH_DISABLED_ACCOUNT)
    finally:
        del schwab_safety.ACCOUNTS[SYNTH_DISABLED_ACCOUNT]
    assert SYNTH_DISABLED_ACCOUNT not in schwab_safety.ACCOUNTS, (
        "FAILED TO RESTORE ACCOUNTS -- synthetic key still present after cleanup")

    # 10. global_burst_cap_block -- check_order's own `counts` param (see its
    #     docstring: "used for the daily-cap/burst-cap/duplicate checks instead of
    #     re-reading the state file") is handed a synthetic in-memory dict that
    #     already looks like it's at GLOBAL_ORDERS_PER_MINUTE -- the real
    #     cache/live/schwab_order_counts.json file is never opened (the `if counts
    #     is None:` read-the-file branch is skipped entirely). schwab_client.
    #     get_account_balance is patched to a large fixed value so this scenario's
    #     path to the burst-cap check (which sits after the real cash check in
    #     check_order's guard order) doesn't depend on sep's actual real cash
    #     balance -- no real balance-affecting call, just a same-process read
    #     that's now synthetic too, consistent with every other guard in this
    #     script never depending on real account financials.
    schwab_safety.AUTOMATION_ENABLED_TICKERS.add(TICKER)
    try:
        synthetic_counts = {"recent_order_timestamps": [time.time()] * schwab_safety.GLOBAL_ORDERS_PER_MINUTE}
        results["global_burst_cap_block"] = _run(
            "global_burst_cap_block",
            happy_patches + [
                (schwab_safety, "_is_trading_day", lambda date_str: True),
                (schwab_safety, "_now", lambda: IN_WINDOW_TIME),
                (schwab_client, "get_account_balance", lambda account: 1_000_000.0),
            ],
            dict(quantity=10, price=10.0, counts=synthetic_counts))
    finally:
        schwab_safety.AUTOMATION_ENABLED_TICKERS.discard(TICKER)

    print()
    n_pass = sum(results.values())
    print(f"{n_pass}/{len(results)} scenarios confirmed.")
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    sys.exit(0 if n_pass == len(results) else 1)


if __name__ == "__main__":
    main()
