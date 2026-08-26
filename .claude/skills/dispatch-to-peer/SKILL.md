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

   **Recurred 2026-08-25**: stating this once in the initial dispatch prompt
   is not sufficient — `ListAgents` showed the dispatched peer sitting `busy`
   (foreground-blocking) on its own build/test steps minutes into the same
   dispatch this exact instruction was sent with. The instruction as written
   only explicitly names "the paired-review agents (and any build
   sub-agents)" — it doesn't say anything about the peer's own direct,
   long-running tool calls (running the full test suite itself, a build
   script, `git` operations) that never spawn a sub-agent at all and so
   never trigger the "background it" framing in the peer's own head. Say
   BOTH explicitly, every dispatch: (a) any sub-agent it spawns (reviewers,
   further build dispatches) must run backgrounded, AND (b) its own
   long-running Bash/tool calls (test runs, builds, anything that isn't a
   quick command) should also run with `run_in_background: true` rather than
   blocking its own turn — don't rely on "background execution" alone to be
   read as covering both. **Check `ListAgents` a few minutes after any
   dispatch** — a peer showing `busy` (not `idle`) while you'd expect it to
   be waiting on a backgrounded job is the live signal this is happening
   again; send a direct one-line reminder (referencing this same
   instruction) rather than assuming it'll self-correct.

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

8. **Collision check on shared surface, before writing the dispatch prompt.**
   Added 2026-08-22 after a real near-miss: this session, `coder`, and
   `backtester-kernel-audit` all edited `db_cache.py`/`backtest_cache`
   schema/`docs/backlog_cache.md` concurrently in the same evening, only
   caught by accident when staging a commit (a `git status` showed another
   session's uncommitted diff already sitting in the working tree). Before
   dispatching work that touches a specific file/table with real collision
   risk (schema files, `run_optimization_sweep.py`, shared docs like
   `backlog_cache.md`/`deep_backlog.md`), run `git status`/`git diff` on that
   path first to see if another session already has uncommitted changes
   there, and check `TaskList` for whether another dispatched task already
   claims that scope. A 2-command check before writing the prompt, not a new
   process to maintain — agents do exactly what they're dispatched to do,
   so the actual fix is upstream, at dispatch time.

## Clearing a peer session

Two safe-to-clear states — don't conflate them:

- **Task complete**: the feature-wrap commit is on `main` (check
  `TaskGet`/`git log`, not memory or the peer's last message alone). Every
  feature is built, reviewed, and committed as one complete unit (item 3) —
  never partial/incremental commits mid-build — so there's no in-between
  state to preserve.
- **Task blocked on a question**: the peer raised an open question it can't
  resolve itself and is now idling, waiting on a reply. Idle-with-a-pending-
  question is NOT a reason to keep the session alive — a session sitting
  parked accumulates nothing useful, its context just ages (stale cache,
  bigger re-brief cost whenever it does resume). Blocked is a reason TO
  clear, not to wait. Before clearing in this state: write the open question
  (and, once answered, the decision) into the Task's metadata via
  `TaskUpdate`, and into `docs/backlog_cache.md` too if it's a standing
  design question that might outlive this exchange. Once that's durably
  captured, clear it — resuming always means a full re-brief per item 1
  regardless of whether the session was cleared or not, so nothing is
  actually lost by clearing while blocked.

The one state that's NOT safe to clear: **actively building** (`ListAgents`
shows `busy`, `TaskGet` shows `in_progress` with no open question posted).
That's real in-flight work with nothing durable yet — clearing there loses
it. Check both `ListAgents` and `TaskGet` before ever clearing a peer.

## Checking in on a stalled queue

If a peer session goes idle with items still outstanding (confirm via
`ListAgents`, and confirm nothing was actually built via `git log`/`git status`
— don't trust an idle status alone as evidence nothing happened), ask directly
whether the queue was received and what's blocking it, rather than re-sending
the same items cold. Restate the priority order in the check-in — don't make
the peer re-derive it from scrollback. **Also check `TaskList`/`TaskGet`
first** — the dispatched task's status/metadata may already answer "what's
the status" without needing a round-trip.

## Multi-tier Agent-tool dispatch (coordinator → subagent → subagent's own reviewers)

Added 2026-08-22 after a real session that dispatched via the `Agent` tool (not
`SendMessage` to a peer session) turned far more interactive and expensive than
intended — the user's own framing: "usually I'd dispatch tasks from another
session and you'd orchestrate the background processes, but this got
challenging." Same underlying checklist above still applies (review-gate,
feature-wrap sequencing, collision checks), but `Agent`-tool subagents add a
real second layer: a dispatched builder often spawns its OWN 4-way review
round, so "I launched 2 things" can silently mean 2 + 8 = 10 concurrent agents
actually running. Lessons from that session:

- **Never use `subagent_type: "fork"` for this.** It inherits the entire
  conversation and re-processes it as real input tokens on every spawn — at a
  cache discount, not for free. On a long session this is unpredictable and
  large (one fork alone hit ~420k tokens; could as easily have been 800k).
  Default to a fresh `general-purpose` (or more specific) agent with a
  complete, self-contained prompt instead — write out whatever context the
  task actually needs explicitly. See `feedback_no_fork_subagents` memory.
  Reserve fork-like reasoning only for something genuinely inseparable from
  live in-turn conversation state, not just "it saves me writing the spec."

- **State the full close-out convention in the FIRST prompt, every time**:
  build → run its own paired/adversarial review round → self-commit if clean
  or CONFIRMED findings are fixed (this project's `feature wrap` steps,
  self-applied — no `session close`/`session wrap`, since a single-feature
  dispatch has no conversation to log) → if genuinely stuck, raise back to
  the coordinator or file a `docs/backlog_cache.md` entry, don't force
  through uncertainty. Don't default to "don't commit, just report" — that
  just adds a manual commit round-trip for the coordinator to do later for
  no benefit, once the review has already cleared. See
  `feedback_dispatched_agent_wrap_convention` memory.

- **Get the full spec right in the initial dispatch, not via mid-flight
  follow-up messages.** A same-session correction (e.g. "also add a CAGR>50
  filter") sent via `SendMessage` to an already-running agent can get lost or
  misattributed — in one real case, the agent's own final report claimed it
  had "fabricated" a requirement that was actually relayed by the
  coordinator, and discarded the whole feature rather than fixing bugs its
  reviewers found in the implementation. If a follow-up correction is
  unavoidable, make it unmistakably an explicit coordinator instruction, not
  a soft suggestion, and re-verify after completion that it actually landed
  (don't assume delivery = incorporation).

- **Track real concurrency with `ListAgents`, not memory of what you
  dispatched.** A dispatched builder's own spawned review round doesn't show
  up until you check — in one real instance the coordinator believed 2
  agents were running when `ListAgents` showed 7 (2 directly dispatched + 4
  from one task's internal review swarm + 1 more). Check `ListAgents`
  whenever asked "what's running" or before dispatching anything new — don't
  answer from what you remember launching.

- **Sequence dispatches that touch the same file; don't parallelize by
  default.** Beyond the collision check in item 8 above (checking for
  ALREADY-uncommitted changes before dispatching), two NEW dispatches that
  will both edit the same kernel file concurrently is a live risk even when
  each is individually well-scoped — one real session saw an unrelated
  accidental bundled fix from one concurrent task's edits show up in
  another's diff. If two queued tasks share a file, run them one after the
  other rather than in parallel, even if their described scopes don't
  obviously overlap.

- **This is an orchestrator duty, not something to leave to the agents to
  self-report.** Before writing multiple dispatch prompts (whether launching
  them together or queuing them), explicitly reason through — for EACH
  pair of planned/in-flight tasks — which functions/files each will realistically
  touch, based on their stated scope, not just whether they're topically
  related. If two plausibly overlap, decide up front: serialize them, or
  scope one narrowly enough to provably avoid the other's area, and say so
  in the prompt. Don't wait for `git status` to reveal a collision after the
  fact, and don't assume a clean `git diff` at dispatch time means no
  collision risk — that only rules out conflict with ALREADY-committed
  state, not with another task about to start. After a batch of concurrent
  or sequential dispatches lands, also plan the retest step explicitly: if
  task B's diff touches code task A's tests exercised, rerun A's tests
  (parity suite, etc.) against the merged result, don't assume A's
  once-passing result still holds once B's changes are stacked on top.

- **Instruct agents explicitly not to end a turn on an unverified "waiting" —
  and explain WHY backgrounding-then-stopping doesn't work for a subagent.**
  Seen repeatedly in one session: an agent ran something via `run_in_
  background: true` (or launched a bash job with `&`) and then ended its
  turn saying "I'll wait for the batch to complete" or "pausing until the
  notification arrives." The real mechanism: task-notifications on job
  completion are specific to the coordinator's own `Agent`-tool parent→child
  relationship — a subagent backgrounding its OWN `Bash` call and ending its
  turn has no guarantee anything wakes it back up when that job finishes.
  "I'll wait" is a dead end there, not just bad practice; the job may finish
  with nobody ever checking it again. State directly in every dispatch
  prompt: **never background your own verification/build steps and then end
  your turn** — either run it in the foreground (blocking, same tool call,
  wait for the real result before proceeding) or, if a step is genuinely
  long, explicitly poll it to completion (a real wait-loop checking `ps`/a
  result file) before ending the turn. Don't end a turn on a bare "waiting"
  with nothing concrete backing it — either produce a real result or report
  a concrete, specific failure.

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
