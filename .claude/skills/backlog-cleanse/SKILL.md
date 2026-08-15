---
name: backlog-cleanse
description: Systematic, read-only triage of docs/backlog_cache.md in small batches — verify each open item against real live state (DB queries, code greps, coverage_registry, subagent research), never edit code, and hand off both confirmed-resolved items AND buildable-but-unscoped items (with a drafted scope) to whichever peer session owns write access to the backlog docs. Use when the user says "backlog cleanse," "clean up the backlog," "triage the backlog," or asks to continue a batch-by-batch backlog review that was already in progress.
---

# Backlog cleanse

Built 2026-08-16 (planner session) after a ~50-item triage pass the night
before found several real staleness cases (a duplicate item, a stale
ticker list, two "downgraded" items that were actually fully decided) —
this skill formalizes that pass into a repeatable procedure instead of an
ad hoc conversation.

**Core constraint this skill exists to encode**: this session does research
and verification only. It never edits `docs/backlog_cache.md`,
`docs/deep_backlog.md`, `docs/backlog_resolved_recent.md`, or any code —
even for an item confirmed 100% resolved. Per
[[feedback_message_vs_direct_edit_concurrent_docs]], those files are
actively owned by whichever session is running the live-trading engineering
work (referred to as the "coder"/"research" peer session) — message that
session with the confirmed finding and let it perform the actual doc
mutation, rather than risk a concurrent-edit collision. Real code changes
follow the same rule for the same reason: this is a triage/verification
role, not an implementation one.

## When to run

- User says "backlog cleanse" or asks to continue a batch backlog review.
- Mid-cleanse: user says "next 10," "keep going," or similar — resume from
  the next unreviewed item, don't restart from the top.

## Procedure

1. **Read `docs/backlog_cache.md` in full** (and `backlog_resolved_recent.md`
   if recent-resolution context is needed to avoid re-litigating something
   already closed). Present items in **file order** (top to bottom is
   already roughly priority order per the file's own convention — see
   CLAUDE.md's `docs/roadmap.md` triage note) — do not reorder, and never
   physically restructure the file itself for presentation purposes
   ([[feedback_clump_backlog_by_tag]] — grouping is a read-time/report-time
   thing only).

2. **Work in batches of ~10 items** (or whatever count the user specifies).
   Track which items have been reviewed this cleanse pass so "next 10"
   resumes correctly rather than re-checking already-cleared items —
   a TaskCreate/TaskUpdate checklist is a reasonable way to hold this
   across turns if the pass spans multiple messages.

3. **For each item in the batch, verify against real live state** — never
   trust the backlog's own prose as still-true
   ([[feedback_verify_backlog_against_live_state]]). Typical checks, adapt
   per item:
   - A claimed code fix: grep the real source for the described
     behavior/guard, don't trust the backlog's memory of it.
   - A claimed DB/node state: query `cache/live/trading_live.db` directly
     (`watch_list`, `open_positions`, `trade_log`, `coverage_events`,
     `pending_buys`, etc.) — the same pattern `weekend-cleanup` uses for
     staged items.
   - A "waiting on X" item: check whether X already happened (a trade
     closed, a scenario fired, a signal triggered) even if nobody updated
     the backlog.
   - Anything time-boxed (`(revisit ~date)`) whose date has passed: treat
     as due for a real look, not an automatic close — first check the
     `scripts/check_backlog_stale_dates.py` output already run at session
     start.
   - Anything genuinely ambiguous, cross-cutting, or history-dependent
     (e.g. "was this decided or just discussed?") — check `git log`/
     `watch_list_audit` per [[feedback_check_history_not_just_current_state]]
     rather than guessing from current state alone.
   - For research-heavy or multi-step verification, **spawning a
     subagent is fine and encouraged** (this is exactly the pattern last
     session's ~50-item triage used) — but the subagent must stay
     read-only too (DB reads, greps, `git log`, script runs that don't
     mutate state). Don't hand a subagent doc-edit or code-edit work.

4. **Classify each item:**
   - **Resolved** — real state confirms the described outcome already
     happened (or the underlying premise is now moot/superseded). Flag for
     handoff (step 6).
   - **Still open, verified accurate** — backlog description still matches
     reality, no change needed.
   - **Stale wording, not resolved** — the situation has moved (e.g. a
     blocker cleared but the actual ask isn't done) — note the correction
     for the peer session to apply, don't silently let it stand.
   - **Needs a user decision** — genuinely ambiguous or requires a call
     only the user can make (keep, drop, defer, reframe). Surface it, don't
     guess.
   - **Buildable, not yet scoped** — a real, still-open item where the
     actual blocker is "nobody's written down what to build," not a pending
     decision or missing verification. Draft a scope with the user in this
     conversation (design Q&A, read-only research to fill in facts a
     subagent can pull — current schema, existing helper functions, related
     items already on file) and hand the drafted scope to the peer session
     in the same batch handoff (step 6) so it can formalize it into
     `docs/design.md` and build from it. This is still research/drafting,
     not implementation — no code, no `docs/design.md` edit from this
     session; the peer session owns turning a drafted scope into a real
     spec doc.

5. **Report the batch back to the user, numbered** (matching
   [[feedback_number_backlog_items]] so they can say "close #3, keep #7")
   before any handoff message goes out — the user gets to veto a
   resolved-classification before it's acted on, same as any other
   backlog-maintenance step. Keep it concise
   ([[feedback_conciseness]]/[[feedback_less_agreeable_less_verbose]]):
   item number, one-line verdict, the concrete evidence (query result,
   grep hit, commit sha), not a re-summary of the item's own prose.

6. **Send one batched `SendMessage`** to the peer session that owns
   `docs/backlog_cache.md` write access (check `ListAgents` — the CLAUDE.md
   convention calls this session "research"/"coder" depending on what it's
   mid-task on) covering everything the user confirmed this batch, at its
   next natural break — don't demand it interrupt active work:
   - **Resolved** items: list each with its evidence, ask it to run the
     standard 4-step resolution (deep_backlog.md entry,
     backlog_resolved_recent.md prepend, backlog_cache.md removal).
   - **Stale wording** items: include the correction in the same message.
   - **Buildable, not yet scoped** items: include the drafted scope from
     step 4 so the peer session can turn it into a real `docs/design.md`
     entry and start building — this is the actual point of the triage
     pass, not just cleanup: anything genuinely ready to build should flow
     to the coder, not just sit reclassified in the backlog.

7. **Move to the next batch only when the user says so** — this is
   explicitly paced by the user (matches how the original triage
   conversation ran), not something to blow through in one shot even
   though nothing here is destructive.

## Scope notes

- Read-only and no-code-changes are hard constraints, not defaults to
  override under Auto Mode — even a one-line "obviously correct" fix found
  along the way gets reported, not applied, from this session.
- This skill is about **backlog accuracy and flow to the coder** — verify,
  filter out what's stale/resolved, and hand off what's ready to build with
  a real scope attached. It is not about doing the underlying work: draft
  the scope, don't implement it, even for something that looks trivial.
- Distinct from `weekend-cleanup`: that skill is specifically about
  *staged* (waiting-on-a-trigger) items plus a weekly commit/test-coverage
  audit, and can run standalone on a weekend. This skill covers the whole
  backlog, batch-paced, and doesn't care what day it is.
