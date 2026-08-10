---
name: live-test-node-setup
description: Stage a real live/paper watch_list node with deliberately-detuned parameters so it exercises a specific execution-code scenario (a Trade-Flow Accountability Grid row, or any real order-placement path) quickly and observably, with proper metadata so a future session doesn't have to reverse-engineer why the config looks abnormal. Also covers checking whether a previously "verified-live" scenario needs re-confirmation after execution-code changes, and running a full regression pass across staged test nodes after a batch of real trading-code changes lands. Use when the user wants to stage a live test node, detune a node to force a specific exit reason (SL/TRAIL/TIME), set up coverage-testing nodes, or asks to "regression test" live execution paths.
---

# Live test node setup

Built 2026-08-09 after a real cross-session confusion: RETL's live node
(`soxl_ira`, id 143) carried `fixed_sl=50%` -- a value nowhere on any real
SL sweep grid, clearly hand-set -- with no structured record of why. The
answer existed (`staged_test_config` row, `scenario_role='time_exit_via_sl'`,
full `notes`) but took several minutes of grepping `deep_backlog.md`/
`conversation_summary.md`/`live_test_coverage.md` to find, and the node's
own `label` field described a *different*, earlier test on the same node --
actively misleading. This skill exists so staging a test node always leaves
a findable trail, and so "was this already proven" gets checked against
real code-change history, not just a stale badge.

## Core principle: detune the EXIT side, not (just) the entry side

Entry signal checks only run in the two daily signal windows (10:25-10:40,
15:25-15:40 ET) -- loosening `z_score_threshold` doesn't make entries fire
more than ~2x/day/node, it just raises the odds a window actually produces
one. The real lever for fast, observable cycling is the **exit** side:
`fixed_sl` / `arm_sell_pct` / `trail_buy_pct` / `trail_sell_pct` /
`max_hold_hours` are checked continuously (every poll), so detuning those
is what actually compresses "wait weeks for a real TRAIL exit" into
"probably happens this week."

To force a **specific** exit reason (not just "any exit, fast"):
- **SL test**: keep `arm_sell_pct` unreachable (far from current price) so
  the position can't arm, tight `fixed_sl` so SL is the only path.
- **TRAIL test**: normal/tight `arm_sell_pct` so it arms quickly, then a
  tight `trail_sell_pct` so the trailing-stop breach fires fast.
- **TIME test**: unreachable `arm_sell_pct` (same as SL test) but a wide,
  deliberately-inert `fixed_sl` (e.g. 50% -- a real resting stop that just
  won't realistically hit) plus a short `max_hold_hours`, so neither SL nor
  TRAIL preempts the hold-time-forced exit. This was RETL's actual design.
- **Coverage-only (no target exit reason)**: tight everything -- fine when
  the goal is just cycling drought/addon/entry mechanics fast, not proving
  one specific exit branch.

Same-bar arm/TP/SL checks are skipped on the entry bar itself, in both
backtest and live (deliberate, closed 2026-08-08 -- see CLAUDE.md) --
detuning arm/trail as tight as you want still can't collapse straight to
"armed same bar as fill," so this isn't a real risk to account for.

## This spans multiple days -- track it as staged, not one-shot

A real fill can take until the next trading day's signal window to land
organically (never force/fake a fill -- see
[[feedback_prefer_organic_over_forced_live_tests]]), and the exit condition
being tested may take further days to trigger after that. Treat this as a
staged, multi-session item:
- While waiting for entry: backlog/session-cache note is "node X staged,
  watching for organic entry fill."
- Once filled: note flips to "node X filled @ price, watching for
  [SL/TRAIL/TIME] exit."
- Only after the target exit actually fires (confirmed via `trade_log`,
  `exit_reason` column) is the test complete.

Don't let a stale "watching for X" note sit past its actual resolution --
this is exactly the failure mode `weekend-cleanup` was built to catch
(check `trade_log`/`coverage_events` for the real outcome before assuming
the note is still accurate).

## Staging procedure

1. **Confirm with the user before creating/modifying any live node** --
   this touches real capital, even at small notional. Never do this
   unprompted.
2. **Set `watch_list.label`** to a short human-readable purpose string
   (e.g. `"2026-08-09 TIME-exit regression retest, post-drought/addon
   hardening"`). Overwrite a stale label from an earlier test on the same
   node rather than leaving it to mislead the next reader.
3. **Write a `staged_test_config` row**:
   - `scenario_role` -- match a real `coverage_registry.py` scenario_key
     where one exists (e.g. `time_exit_via_sl`, `gap_resize`,
     `post_fill_topup`), so the cross-reference in the next section works.
     Free text is fine if there's no matching Grid row.
   - `expected_config` -- JSON of every deliberately-abnormal field, e.g.
     `{"fixed_sl": {"test_value": 50, "revert_to": 2.0}, "max_hold_hours":
     {"test_value": 11, "revert_to": 100}}`. Always include `revert_to` --
     that's the piece that was missing for RETL and cost the most time to
     reconstruct after the fact.
   - `notes` -- plain-language why, written for a reader with zero session
     context (a future `go`/cold session, not you-right-now).
4. **Back up the live DB first** (`cp cache/live/trading_live.db
   cache/live/trading_live.db.bak_<label>_$(date +%Y%m%d_%H%M%S)`) before
   any write, per standing convention.
5. **Update `signals_invariants.py`'s baseline** if the node is
   `mode='live'`/`state='live'` and doesn't already have one
   (`scripts/seed_baseline_config.py`), so the deliberate deviation doesn't
   get flagged as unexplained drift on the next invariant check.

## Checking staleness before trusting a "verified-live" badge

`coverage_registry.py`'s `verified-live` status has no recency or
invalidation logic -- a single firing from weeks ago renders identically
to one from yesterday. Before treating a scenario as "already proven,
no need to re-test":

1. Pull the scenario's last-verified date:
   `.venv/bin/python scripts/coverage_registry.py | grep <scenario_key>`
2. Check what's changed in the code paths that scenario exercises since
   that date: `git log --since=<date> --oneline -- active_signals.py
   signals_*.py schwab_*.py` (narrow to the specific module(s) the
   scenario actually touches, not the whole live-trading surface).
3. If real execution-logic changes landed in the relevant files since the
   last verification (not just docs/comments), treat the badge as stale --
   the mechanism needs a fresh live confirmation under current code, not a
   pass based on old evidence.

## Running a full regression pass

Trigger: after a batch of real execution-code changes lands (the same
`active_signals.py`/`signals_*.py`/`schwab_*.py`/`backtester.py`/
`strategies.py` change scope that already triggers `session wrap`'s paired
Opus review, per CLAUDE.md) -- particularly before a bigger next step (a
new kernel-fix rollout, a rebalance-automation build) where you want firm
footing on what currently-live code actually does, not what it did before
the changes.

1. List every `staged_test_config` row and every `wired-never-fired` /
   older `verified-live` Grid row whose target code changed in the batch
   (via the staleness check above).
2. For each: either it's still actively staged (leave as-is, keep
   watching), or it needs to be re-staged (detune again per the procedure
   above) to get a fresh organic firing under current code.
3. Report a punch list to the user -- which scenarios are confirmed fresh,
   which are stale and re-staged, which are stale and NOT yet re-staged
   (and why, e.g. blocked on an organic entry signal). Don't silently
   revert or re-stage anything without the user's go-ahead per node.
