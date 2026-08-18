---
name: dispatch-to-peer
description: Send a scoped backlog item to a peer/background Claude session (e.g. the "research" session) to build, with the full checklist this project actually requires attached every time — not re-derived piecemeal per message. Use when handing off a build task to another session via SendMessage, queueing multiple backlog items to a peer, or checking in on a peer session's queue status.
---

# Dispatch to peer

Built 2026-08-17 after a real gap: dispatching 4 backlog items to the `research`
session took 5+ separate follow-up messages to arrive at a complete instruction set
(paired-review gate, then its exact CLAUDE.md phrasing, then background-vs-blocking,
then feature-wrap sequencing) — each piece was individually correct but had to be
re-derived from memory/CLAUDE.md in the moment instead of applied as one checklist.
This project's dispatch-agent workflow (see `docs/conversation_summary.md`'s
2026-08-17 entry) is a recurring pattern, not a one-off, so the checklist belongs
here.

## When to use

- Sending a scoped, buildable backlog item to a peer session (`research` or
  similar) via `SendMessage`.
- Queueing several items at once — state the priority order explicitly, every
  time; don't assume send-order implies priority.
- Checking in on a peer session's queue status when it's gone idle with items
  still outstanding.

## The checklist, every dispatch

1. **State the real scope precisely** — file paths, function names, the actual
   bug/gap traced (not "fix the X issue," the specific mechanism). A cold
   session (or one deep in unrelated context) can't re-derive what you already
   traced this session.

2. **Review-gate, in CLAUDE.md's exact phrasing, not a paraphrase.** If the
   diff will touch `active_signals.py`/any `signals_*.py` module/`schwab_*.py`/
   a backtest kernel module (`backtester.py`/`strategies.py`/
   `run_optimization_sweep.py`), say explicitly: **do not mark this done
   (commit it, close it, report it complete) until the paired independent-cold
   + contextual Opus review (with rebuttal exchange) has actually run and its
   outcome is recorded on file** (commit message or backlog/deep_backlog
   entry). "Needs review" as a stated fact is not the same instruction as "do
   not mark done until reviewed" — say the second one.

3. **Paired review + any resulting fixes = one complete feature cycle,
   closed with `feature wrap`.** Build → paired review → resolve CONFIRMED
   findings → `feature wrap` (update docs, review pre-commit checklist
   manually, commit) for that one item. State this sequencing explicitly per
   item, not once vaguely for the whole batch. Do NOT invoke the `verify`
   skill during feature wrap (standing project convention). State-only
   changes (a DB flag flip, an `archive_node()` call) skip the review gate
   entirely and go straight to feature wrap — say which items are which.

4. **Background, not blocking, by default.** Say explicitly: launch the
   paired-review agents (and any build sub-agents, if the peer dispatches
   further) with background execution, not blocking its own foreground
   thread — matches [[feedback_background_review_agents]] and
   [[feedback_default_background_long_commands]]. Don't assume a peer session
   applies this on its own; state it per dispatch.

5. **Priority order, explicit and numbered**, whenever more than one item is
   queued. State which one to start on first, not just "here's a list."

6. **Trading-hours check, if the peer's work could plausibly touch anything
   schwab-adjacent.** Per CLAUDE.md's Background-Agent Trading-Hours Rule —
   confirm off-hours/weekend before dispatching, or get explicit go-ahead if
   it's a live trading window. This applies to what the RECEIVING session
   might spawn, not just what you're spawning directly.

7. **Track the dispatch with `TaskCreate`/`TaskUpdate`, not a hand-rolled
   status file.** Built 2026-08-18 after a real incident: a session built a
   real fix (fixture filter + dedup for `check_intraday_risk_review`) fully
   passing tests, then ended (context/session boundary) before reaching
   review or commit — with zero trace anywhere of what was done, whether it
   was reviewed, or that it was even in progress. The next session had to
   reverse-engineer the status from git forensics (mtimes, reflog, diff
   content referencing a later commit hash) instead of just reading it. Two
   file-based fixes were considered and rejected: a gitignored file in the
   main tree doesn't help (worktree-isolated work is a separate checkout),
   and a committed shared-path status file guarantees merge conflicts on top
   of the real ones (two worktrees already collided on `signals_notify.py`
   itself once, see `docs/conversation_summary.md`'s 2026-08-17 entry).
   **Use the harness's own `TaskCreate` instead** — it's not a repo file, so
   it can't merge-conflict, doesn't need a worktree path looked up to find
   it, and already has the right shape: `description` holds the plan/scope
   (files touched, whether the review-gate applies), `status`
   (`pending`/`in_progress`/`completed`) tracks real progress instead of
   prose that goes stale, and `metadata` can record
   `review_status`/`reviewed_by` explicitly. Create the task before
   dispatching, update its status/metadata as the peer's work progresses
   (built → tested → reviewed → committed), and only mark `completed` once
   the CLAUDE.md review-gate (item 2 above) has actually been satisfied —
   never mark a task `completed` with a paired review still outstanding.

## Checking in on a stalled queue

If a peer session goes idle with items still outstanding (confirm via
`ListAgents`, and confirm nothing was actually built via `git log`/`git status`
— don't trust an idle status alone as evidence nothing happened), ask directly
whether the queue was received and what's blocking it, rather than re-sending
the same items cold. Restate the priority order in the check-in — don't make
the peer re-derive it from scrollback. **Also check `TaskList`/`TaskGet`
first** — the dispatched task's status/metadata may already answer "what's
the status" without needing a round-trip.

## What NOT to do

- Don't assume a peer session (even one with full project context) applies
  the review-gate/background/feature-wrap conventions automatically just
  because they're in CLAUDE.md — CLAUDE.md is advisory text, not a mechanical
  gate (same reasoning as the Review-Gate Persistence Rule itself: visible
  instructions don't mechanically stop a confident "done" from being written
  before the gate is actually checked). State it every dispatch.
- Don't bundle the review-gate instruction into a general "and also, remember
  to..." aside — put it in the same message as the scope, as an explicit,
  separately-readable instruction.
- Don't skip stating priority order just because it "seems obvious" from
  context — restate it, every batch.
