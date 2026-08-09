---
name: weekend-cleanup
description: Reconfirm every "staged" live-trading backlog item (and other staged items) against real DB/broker state, since staged items are things "waiting on an organic signal" and the weekend is dead time to check whether that signal already fired without the backlog being updated. Also does a weekly audit of the week's commits for new features/behavior changes that landed with no proportional new test coverage. Use when the user says "weekend cleanup," "reconfirm the staged items," "weekly checkin," or asks to check the backlog is still accurate on a non-trading day.
---

# Weekend cleanup

Built 2026-08-08 (weekend) after reconfirming the live-trading "staged"
backlog items turned up a real drift: GDXU's node (id 108) had already
closed its position on 2026-08-07 with the exact predicted TIME exit, but
`backlog_cache.md` still listed "GDXU next exit-check" as an open item
waiting to happen. Nothing was wrong operationally -- the daemon did its
job correctly -- but the backlog itself had gone stale. A staged item
("waiting for X to happen") is exactly the kind of backlog entry that can
silently resolve itself while no session is open to notice, so this needs
a deliberate check, not passive trust that `go`'s summary is current.

Market is closed on weekends, so this is safe dead time to run without
worrying about racing a live trading window.

## When to run

- User explicitly asks for "weekend cleanup."
- It's a weekend/holiday (no trading day) and the user asks to reconfirm
  the backlog or check staged items are still accurate.

## Procedure

1. **Read `docs/backlog_cache.md` in full** and pull out every item that
   describes a *staged* state -- something built/deployed and "waiting on"
   an organic signal, a position to close, a node to trigger, or similar
   passive conditions. (See [[feedback_number_backlog_items.md]] convention
   -- present these numbered, grouped Staged/Todo/Accepted, when reporting
   back.)

2. **Baseline health first** -- run in this order (same as
   `daily-routine-check`, do NOT skip even though it's the weekend):
   - `scripts/daemon_status.py` -- daemon down is expected off-hours, not a
     problem, but confirms freshness of anything time-sensitive.
   - `.venv/bin/python signals_invariants.py` -- config-drift/invariant
     check; should read clean.

3. **For each staged item, query real state directly** -- never trust the
   backlog's own prose as still-true. Typical checks, adapt per item:
   - Node config: `SELECT ... FROM watch_list WHERE id=<wl_id>` -- confirm
     `state`, relevant flags (`drought_overlay_enabled`, `addon_enabled`,
     `trail_buy_pct`, etc.) still match what the backlog describes.
   - Open position: `SELECT * FROM open_positions WHERE wl_id=<wl_id>` --
     flat vs. open changes what "staged" even means for that item.
   - Recent closes: `SELECT ... FROM trade_log WHERE wl_id=<wl_id> ORDER BY
     exit_time DESC LIMIT 3` -- did the awaited event already happen?
   - Relevant `coverage_events` rows (e.g. `scenario_key LIKE '%top_up%'`)
     -- did the specific mechanism the item is trying to prove actually
     fire, even if the surrounding position already closed?
   - For an account-level claim (e.g. an `account_type` mislabel used as a
     deliberate test), re-grep the real source (`schwab_safety.py`) rather
     than trusting the backlog's memory of it -- code can drift too.

4. **Classify each staged item's outcome:**
   - **Still valid** -- real state matches the backlog description, item
     stays staged as-is.
   - **Resolved** -- the awaited event happened for real. Move it through
     the standard resolution workflow (CLAUDE.md's Research Log section):
     add/update `docs/deep_backlog.md`, prepend a one-liner to
     `docs/backlog_resolved_recent.md`, remove from `docs/backlog_cache.md`.
     If the resolution reveals something noteworthy (e.g. an awaited proof
     mechanism did NOT fire even though the surrounding trade closed),
     that's a real finding worth its own backlog line, not just silence.
   - **Blocker cleared, but not actually resolved** -- e.g. "waiting for
     position X to close" and it did, but the original test still hasn't
     produced its proof. Rewrite the item to reflect the real current state
     (now "ready for the next signal," not "waiting for the position to
     close") rather than leaving stale wording in place.

5. **Report back to the user** numbered and grouped
   (Staged/Todo/Accepted per [[feedback_number_backlog_items.md]]),
   flagging which items changed status and why, before making any edits to
   `backlog_cache.md`/`backlog_resolved_recent.md`/`deep_backlog.md` --
   confirm with the user before committing the doc updates, same as any
   other backlog-maintenance edit.

## Weekly commit/test-coverage audit

Added 2026-08-08 (evening), after the same-day canary Grid/restage/auto-
explain commit (`dcda025`) was found to have shipped 3 real behavior
changes -- `restage_canary_nodes.py` (new tool, 131 lines), price-action
auto-explain in `coverage_check.py`, and the `reason_by`-clobber guard the
session's own paired review had just found and fixed -- with **zero new
test functions**. The commit's "Full suite: 631 passed" was true but
misleading: that was the pre-existing count, unchanged by the diff (the
only test-file edits were mechanical 2-tuple -> 3-tuple signature fixes).
Nobody caught it until directly asked to review the backlog against recent
commits. This step exists so that check happens on a cadence, not only
when prompted.

1. **Walk every commit from the last 7 days** (`git log --oneline --since="7 days ago"`,
   then `git show --stat <sha>` per commit) and for each one that adds a new
   feature, new code path, or a real behavior change (not a pure doc/backlog/
   research-script/one-off-analysis commit):
   - Check whether the commit's own diff touched a test file, and if so,
     whether it added genuinely new `def test_*` functions -- not just
     signature/call-site fixes forced by an unrelated refactor (that's what
     the `dcda025` commit's diff looked like at a glance; only a real diff
     read caught that no new test logic existed).
   - If a new script was added (`scripts/*.py`) with no matching
     `tests/test_*.py`, that's a gap regardless of what the commit message
     claims about "full suite passed."
   - Cross-check any bug the commit's message claims was "verified with a
     regression test" -- grep for it. A claim in a commit message is not
     proof; find the actual test.
2. **Report gaps found**, grouped by commit, each with: what shipped, what's
   untested, and a concrete list of the missing test cases (mirroring how
   the existing code's own docstrings describe its edge cases/guards -- they
   usually already enumerate exactly what a test should cover, since the
   paired-review process demands explaining the "why" inline).
3. **Only write the missing tests after reporting** -- same confirm-before-
   editing posture as the staged-item backlog edits above, since writing
   tests touches committed files, not just backlog prose. (Exception: if the
   user has already said "yes, write them" in the same conversation, proceed
   directly -- don't re-ask for a decision already made.)

## Scope notes

- This is specifically about **staged** items (passive, waiting-on-a-
  trigger). Don't use this skill to re-litigate **TODO** items (those need
  design/decision work, not state verification) or to re-run the full
  `daily-routine-check` battery (that's about today's live trading
  activity, not backlog accuracy).
- Read-only by design -- this skill queries and reports, it does not place
  orders, restart the daemon, or otherwise touch live state. Doc edits
  (backlog files) still go through the user's confirmation per step 5.
