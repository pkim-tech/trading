"""Mutation testing for signals_notify.check_entry_abandon -- answers "would
the test suite actually catch this bug" deterministically, instead of trusting
that a new test looks like it would. Reverts one real, specific fix at a time
(temporarily rewriting signals_notify.py in place), runs the exact regression
test that fix is paired with, asserts it FAILS (the mutant is "killed"), then
restores the original text and re-confirms the test passes again.

Built 2026-07-31 after a HIGH real-money bug (an earlier version of
check_entry_abandon could orphan a real resting order while claiming it was
cancelled) was found by independent code review, not by the test suite --
this checks whether the regression test written for that bug (and four other
historical/current bugs in the same function) actually kills a reintroduction
of it, not just whether the code currently looks right.

Distinct from tests/fake_broker.py (drives real order-placement code against
a simulated broker state machine) and sim_chaos_monkey.py (simulates a human
missing real signals to measure strategy robustness) -- this operates on the
source code itself via a known-bad text substitution, then reruns the
unmodified test suite.

Usage: .venv/bin/python scripts/mutation_test_entry_abandon.py
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "signals_notify.py"

# Each mutation: (description, fixed text as it exists today, buggy text to
# substitute, pytest node id of the test that must fail against the mutant).
# `fixed` must match signals_notify.py's real current text EXACTLY ONCE.
MUTATIONS = [
    (
        "HIGH real-money: the manual-placement (order_placed, no order_id) "
        "guard is removed -- falls through to clear-and-claim-cancelled "
        "while a real order may still be resting with zero local tracking.",
        "        if not limits.dry_run and pb.get('order_placed') and not order_id:",
        "        if False:  # MUTATED: removed the no-order-id-on-file guard",
        "tests/test_entry_abandon.py::"
        "test_real_account_manual_placement_with_no_order_id_does_not_clear_or_false_claim",
    ),
    (
        "Reads the live watch_list node instead of the pinned pending-buy "
        "snapshot -- a later account edit could retarget a real cancel_order "
        "call at the wrong account.",
        "        node = pb['node']",
        "        node = db.get_watch_list_node_by_id(wl_id)  "
        "# MUTATED: live re-fetch instead of pinned snapshot",
        "tests/test_entry_abandon.py::"
        "test_account_uses_pinned_node_snapshot_not_live_watch_list_edit",
    ),
    (
        "Unrecognized-account fail-closed guard removed -- would crash on "
        "`limits.dry_run` (None has no such attribute) instead of alerting "
        "and leaving the row untouched.",
        "        if limits is None:",
        "        if False:  # MUTATED: removed fail-closed unrecognized-account guard",
        "tests/test_entry_abandon.py::test_unrecognized_account_fails_closed_and_alerts",
    ),
    (
        "A real bounce-fill racing the cancel is no longer reconciled -- "
        "the FILLED status branch is skipped, so a genuine fill would be "
        "abandoned instead of opening the real position.",
        "            if status == 'FILLED':",
        "            if False:  # MUTATED: FILLED race no longer reconciled",
        "tests/test_entry_abandon.py::"
        "test_real_account_cancel_racing_a_fill_reconciles_instead_of_abandoning",
    ),
    (
        "Unconfirmed-cancel fail-closed retry removed -- clears the row "
        "(and posts an 'entry abandoned, resting order cancelled' claim) "
        "even though the cancel was never actually confirmed.",
        "            if status != 'CANCELED':",
        "            if False:  # MUTATED: unconfirmed-cancel fail-closed retry removed",
        "tests/test_entry_abandon.py::test_real_account_unconfirmed_cancel_leaves_row_for_retry",
    ),
]


def _run_test(*node_ids):
    result = subprocess.run(
        [".venv/bin/python", "-m", "pytest", *node_ids, "-q"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    return result.returncode == 0, result.stdout + result.stderr


def main():
    original = TARGET.read_text()
    results = []
    try:
        for desc, fixed, buggy, test_id in MUTATIONS:
            count = original.count(fixed)
            if count != 1:
                results.append((desc, test_id, None,
                                 f"SKIPPED -- fixed text matched {count} times, expected 1 (source drift?)"))
                continue

            mutated = original.replace(fixed, buggy, 1)
            TARGET.write_text(mutated)
            try:
                mutant_passed, output = _run_test(test_id)
            finally:
                TARGET.write_text(original)  # always restore before the next mutation, even on error

            if mutant_passed:
                results.append((desc, test_id, False,
                                 "MUTANT SURVIVED -- test did not catch this bug"))
            else:
                results.append((desc, test_id, True, "mutant killed"))

        # Final sanity: confirm the restored (real, fixed) file passes the
        # whole entry-abandon test file -- proves the restore loop above
        # didn't leave the source in a broken state.
        restore_ok, restore_output = _run_test("tests/test_entry_abandon.py")
    finally:
        TARGET.write_text(original)  # belt-and-suspenders, even on exception

    print("=" * 78)
    print("Mutation test results -- signals_notify.check_entry_abandon")
    print("=" * 78)
    killed = 0
    for desc, test_id, ok, note in results:
        status = "KILLED" if ok else ("SKIPPED" if ok is None else "SURVIVED")
        print(f"[{status:8s}] {test_id}")
        print(f"           {desc}")
        print(f"           {note}")
        if ok:
            killed += 1
    print("-" * 78)
    print(f"{killed}/{len(results)} mutants killed.")
    print(f"Restore sanity check: {'OK' if restore_ok else 'FAILED -- see output below'}")
    if not restore_ok:
        print(restore_output)

    if killed != len(results) or not restore_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
