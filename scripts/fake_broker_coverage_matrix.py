"""Accountability matrix: every real order-lifecycle USE CASE that mutates a
real broker order (not just every schwab_client function -- several of those
are called from 2-3 genuinely different production scenarios, e.g.
place_equity_buy is entry, top-up, AND gap-resize fallback, each a distinct
decision path worth testing separately) -- and whether an actual
tests/test_fake_broker_*.py scenario drives real production code through it,
asserting on the fake broker's own resulting order state.

Built 2026-07-31, per explicit user request after finding fake_broker.py
under-utilized (only 1 of 8 same-session fixes had a fake_broker scenario)
and a real latent bug in the fixture itself (cancel_order's swapped argument
order) that no existing test had ever surfaced -- prompted a "what ELSE has
never actually been driven through fake_broker" audit instead of assuming
the rest is fine.

Same philosophy as scripts/coverage_registry.py: never hand-typed opinion
about what's covered. USE_CASES below is real, stable domain knowledge (the
call graph: which production entrypoint a test would call, and which
schwab_client function that entrypoint reaches for this specific scenario --
this doesn't change on every session) but whether a fake_broker test file
actually calls that entrypoint is derived fresh by grepping
tests/test_fake_broker_*.py and tests/test_node_circuit_breaker.py every
run, so this can't silently go stale.

A use case reads COVERED only if some fake_broker test file's source
contains a real call to its entrypoint -- necessary but not sufficient (the
call could be gated behind an earlier guard that returns before reaching the
schwab_client call this row is about); treat COVERED as "a real test drives
this entrypoint, worth reading to confirm it asserts the right thing," not
proof of an assertion on broker state by itself.

Run directly: .venv/bin/python scripts/fake_broker_coverage_matrix.py
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"

FAKE_BROKER_TEST_FILES = sorted(TESTS_DIR.glob("test_fake_broker_*.py")) + \
    [TESTS_DIR / "test_node_circuit_breaker.py"]

# One row per real, distinct decision path that ends in a broker mutation --
# NOT one row per schwab_client function (several are called from 2-3
# genuinely different scenarios). `entrypoint` is the real signals_notify
# function a test would call to exercise this path; `schwab_fn` is the
# schwab_client function it reaches for THIS specific scenario.
USE_CASES = [
    dict(id="trailing_buy_entry",
         desc="Automated trailing-buy entry placement (the live-default strategy's entry mechanism)",
         entrypoint="notify_buy_signal", via="_attempt_automated_buy", schwab_fn="place_trailing_buy"),
    dict(id="market_buy_entry",
         desc="Automated market-buy entry placement",
         entrypoint="notify_buy_signal", via="_attempt_automated_market_buy", schwab_fn="place_equity_buy"),
    dict(id="post_fill_topup",
         desc="Post-fill top-up buy (underspent notional corrected with a 2nd real order)",
         entrypoint="_reconcile_buy_fill", via="_reconcile_fill", schwab_fn="place_equity_buy"),
    dict(id="sl_placement_post_fill",
         desc="Real protective STOP placed right after a fill",
         entrypoint="_reconcile_buy_fill", via="_place_stop_loss_for_position", schwab_fn="place_stop_loss"),
    dict(id="trailing_sell_arm_replace",
         desc="Arm transition: resting SL replaced with a real TRAILING_STOP sell",
         entrypoint="notify_trailing_activated", via="_attempt_automated_sell",
         schwab_fn="replace_order_with_trailing_sell"),
    dict(id="trailing_sell_arm_fresh",
         desc="Arm transition with no resting SL to replace -- fresh trailing-sell placement",
         entrypoint="notify_trailing_activated", via="_attempt_automated_sell", schwab_fn="place_trailing_sell"),
    dict(id="exit_replace_resting",
         desc="TP/SL/TIME/hold-time-forced-TRAIL exit: resting order replaced with a real market sell",
         entrypoint="notify_sell_signal", via="_attempt_automated_exit_sell",
         schwab_fn="replace_equity_order_with_market"),
    dict(id="exit_fresh_market_sell",
         desc="Exit with no resting order to replace -- fresh market sell placement",
         entrypoint="notify_sell_signal", via="_attempt_automated_exit_sell", schwab_fn="place_equity_sell",
         # Was a grep false-positive until 2026-07-31 -- every scenario that
         # called notify_sell_signal always seeded sl_order_id first, so the
         # fresh-placement branch was never actually reached despite the
         # entrypoint matching. Closed by a dedicated test file
         # (test_fake_broker_exit_fresh_scenario.py) that deliberately does
         # NOT seed one, so the real grep-based detection below is trustworthy
         # again -- no manual override needed.
         ),
    dict(id="gap_resize_replace",
         desc="Overnight gap-resize: resting trailing-buy replaced with a real market buy",
         entrypoint="check_gap_resize", via="check_gap_resize", schwab_fn="replace_equity_order_with_market"),
    dict(id="gap_resize_fresh",
         desc="Gap-resize with no order_id on file -- fresh market buy placement",
         entrypoint="check_gap_resize", via="check_gap_resize", schwab_fn="place_equity_buy",
         # Was a grep false-positive until 2026-07-31 -- the only existing
         # scenario always seeded a resting order first. Closed with a new
         # test in the same file (test_gap_resize_places_a_fresh_market_buy_
         # when_no_order_id_on_file) that deliberately doesn't -- the
         # entrypoint-only grep check can no longer distinguish the two, so
         # this row is trustworthy again without a manual override.
         ),
    dict(id="entry_abandon_cancel",
         desc="Entry-abandon timeout: real resting trailing-buy cancelled outright",
         entrypoint="check_entry_abandon", via="check_entry_abandon", schwab_fn="cancel_order"),
]


def _files_calling(entrypoint):
    pattern = re.compile(rf"\b{re.escape(entrypoint)}\(")
    hits = []
    for f in FAKE_BROKER_TEST_FILES:
        if not f.exists():
            continue
        if pattern.search(f.read_text()):
            hits.append(f.name)
    return hits


def main():
    print("=" * 100)
    print("fake_broker use-case accountability matrix")
    print("=" * 100)
    covered = 0
    for uc in USE_CASES:
        hits = _files_calling(uc["entrypoint"])
        manually_overridden = uc.get("verified_gap_despite_entrypoint_match", False)
        is_covered = bool(hits) and not manually_overridden
        status = "COVERED" if is_covered else "GAP"
        if is_covered:
            covered += 1
        print(f"\n[{uc['id']}] {status}")
        print(f"  {uc['desc']}")
        print(f"  entrypoint: {uc['entrypoint']} -> {uc['via']} -> schwab_client.{uc['schwab_fn']}")
        if hits and manually_overridden:
            print(f"  entrypoint IS called by: {', '.join(sorted(hits))}, but manually verified "
                  f"this specific branch is never actually reached (see comment above)")
        elif hits:
            print(f"  via test file(s): {', '.join(sorted(hits))}")

    print("\n" + "-" * 100)
    print(f"{covered}/{len(USE_CASES)} real broker-mutating use cases have a fake_broker scenario "
          f"reaching their entrypoint.")
    if covered < len(USE_CASES):
        sys.exit(1)


if __name__ == "__main__":
    main()
