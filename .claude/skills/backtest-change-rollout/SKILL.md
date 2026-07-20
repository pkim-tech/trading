---
name: backtest-change-rollout
description: Workflow for validating and rolling out a change to the backtest kernel or strategy logic (a bug fix, a new fill-optimism resolution, a new strategy variant, etc.) -- from single-trade manual verification through a full multi-ticker resweep. Use when the user wants to verify a kernel/backtester change is correct before trusting sweep numbers off it, or wants to plan/prepare (not necessarily launch) a resweep/backfill campaign. Triggers on "resweep", "backfill", "verify the kernel change", "recheck one node", or planning discussion around re-running the optimization grid.
---

# Backtest change rollout

A staged process for trusting a change to `backtester.py`/`strategies.py`
before it drives real sweep numbers, ending in a full campaign -- but each
stage is a checkpoint, not something to blow through automatically.

## Stage 1 -- single-node manual trade audit

**This stage produces evidence for the user to re-verify, not a verification
Claude performs and signs off on.** Don't call a change "verified" on the
strength of this stage -- the actual verifying happens when the user reviews
the trade-by-trade output. The reason this stage exists at all: Claude
hallucinates, so a claim of "verified" isn't trustworthy on its own no
matter how it's phrased -- the fix is producing checkable evidence by
default, not asserting confidence more carefully.

Before trusting any aggregate number off the changed kernel, write a script
that produces **human-reviewable, trade-by-trade detail** for one ticker/one
parameter combo -- entry bar, exit bar, the specific trigger that fired, the
OHLC values involved, old-code fill vs new-code fill -- so a person can
actually follow the logic by hand, not just compare a before/after summary
stat. A synthetic unit test proves the mechanism works on fabricated data; an
aggregate before/after flip (e.g. "alpha went from +1442% to -37.8%") proves
*something* changed but not *that* it changed correctly on real trades.
Neither is a substitute for this stage (see the exit-side gap-through-
trigger fix, 2026-07-20, where this step was skipped and the change was
presented as fully verified anyway).

Concretely: pull the real historical bars for one ticker, find actual
instances where the changed code path fires (e.g. real gap-through-trigger
exits), and print/tabulate the specific before/after fill decision for each
one next to the raw OHLC that justifies it -- then hand that output to the
user instead of a "this is verified" claim.

## Stage 2 -- expand to one full ticker, biased parameters

Once Stage 1's manual spot-check looks right, run one ticker through the
full Phase1->2->2.5 pipeline -- but **default the parameter ranges toward
where alpha has historically been found**, not the full swept grid. E.g. the
existing `stop_loss<=9%` cap (`docs/backlog_cache.md`, 2026-07-16: robust
alpha declined consistently above 9% across every ticker checked) is the
precedent for this bias. State the biased default explicitly and ask before
narrowing -- **always offer the full parameter space too** if the user wants
it; the bias is a default, not a restriction.

## Stage 3 -- storage sizing check, before committing to a full campaign

Before launching a multi-ticker campaign, check:
- Current `backtest_cache` size on disk and row count.
- The new campaign's expected node count (Phase1 formula: `z_thresholds x
  windows x take_profits x stop_losses x hold_time_caps x trail_pcts`, per
  `(ticker, fixed_sl, entry_timing)` combo -- see `run_phase1_coarse`'s
  `expected` calc in `run_optimization_sweep.py`) x number of combos in
  scope, plus Phase2/2.5 (smaller, targeted).
- Available disk headroom.

Surface this as a real forecast (rows, approx bytes/row from existing data,
projected growth) so the user can make an informed storage-strategy call
before compute starts, not after. Note DB reads can time out or run very
slowly while a sweep is actively writing (WAL lock contention) -- don't try
to force a query through a live write storm; wait or use a read-only
connection with a generous timeout, and don't treat a slow/timing-out query
as safe to retry aggressively. `COUNT(*)` on a large table can itself time
out (full scan) -- `SELECT MAX(rowid)` is a fast approximate row count.

**Don't trust `df -h`'s free-space number at face value on WSL2.** It
reports the virtual disk's (vhdx) filesystem capacity, not real free space
on the Windows host backing it -- the vhdx can be thin-provisioned and
report far more headroom than actually exists (confirmed 2026-07-20: `df`
said 826GB free, real available was 113GB). Ask the user for their real
available space rather than presenting the `df` number as the answer.

## Why the staging order matters, not just the checklist

Stages 1-3 are cheap (minutes to a single-ticker run) and Stage 4 is not
(hours, per the throughput math in Stage 3). A kernel bug found *after*
Stage 4 is expensive in a specific way: the per-node cache dedup
(`dispatch_parallel_grid`) keys on `version`, not on "which exact kernel
code produced this row" -- so every row computed under the buggy version is
contaminated regardless of ticker/param combo, and the fix is bump-version-
and-rerun, not a partial patch. The old wrong rows aren't deleted (never
delete `backtest_cache` data), but the compute time is genuinely sunk. This
is the concrete reason Stages 1-2 exist as real gates, not busywork before
the "actual" work -- an unfavorable-but-correct Stage 4 result (e.g. a
ticker flipping to CLIFF post-fix) is a valid finding, not a loss; a bug
caught only at Stage 4 is the loss Stages 1-2 are there to prevent.

## Stage 4 -- the full campaign

Mechanics: `scripts/run_sweep_queue.sh` (with `scripts/campaign_config.py`)
runs one `(ticker, fixed_sl, strategy, entry_timing)` combo per
`run_optimization_sweep.py` invocation, looped in bash, so each ticker's
Phase1->2->2.5 completes independently and is inspectable as soon as it's
done -- **do not** batch all tickers into a single `--tickers A B C ...`
invocation of `run_optimization_sweep.py` directly; that forces every
ticker through one shared Phase1 + cross-ticker Checkpoint1 before any of
them reach Phase2+, which defeats per-ticker inspectability and is not the
pattern the user wants.

**No campaign-level skip/resumability shortcut** -- a `campaign_config.py
done` presence check (does a `Phase2-Island` row already exist for this
combo?) was tried and removed the same session it was added (2026-07-20): it
has the identical blind spot as the row-count check disabled in Stage 3's
warning above -- it trusts a cache hit without confirming the cached rows
reflect the *current* code, and a kernel edit mid-campaign would make it
silently serve stale rows again. Confirmed once by luck (a recomputed node
matched exactly), but luck isn't the mechanism to rely on. Every combo is
always invoked now; `dispatch_parallel_grid`'s own per-node cache lookup
(exact param-tuple match) is the one trusted resumability mechanism -- a
combo that's already fully done just submits zero new tasks and returns
fast, so resuming is still cheap, just without the false confidence of a
campaign-level shortcut.

**Backtests should run per ticker** as a general rule, not batched, per the
above.

## Stage 5 -- reconfirm live-sim/paper-trading compliance

After a kernel change, check whether `signals_compute.py` (real-time SELL
condition checks) or `paper_trading.py` (paper-trading simulation) implement
their own independent copy of the changed logic, rather than assuming a
backtest-kernel fix is backtest-only. Note the likely (but unconfirmed
per-change) reason this class of bug is less probable live: the daemon polls
real continuous(ish) prices at `POLL_SECS` cadence, not discrete hourly OHLC
bars, so a bug that's specifically an artifact of bar-level approximation
(like gap-through-trigger) may not have a live-side equivalent -- but that's
a hypothesis to verify per change, not something to assume without checking.
Distinct from `verify_trailing_buy_resolution.py`/
`verify_trailing_sell_resolution.py`, which check the hourly kernel's
approximation quality against finer-grained (5-min bar) data -- not whether
the live/paper code paths independently reimplement the changed logic.

## Critical: never launch the campaign yourself

**Preparing the queue script/config and running it are two different asks.**
Building `run_sweep_queue.sh`, computing the storage forecast, or discussing
scope is not authorization to execute it -- confirming scope/design in
conversation is not authorization either. **The user runs these campaigns
themselves, in their own shell, always** -- this mirrors the existing
`run_backfill_queue.sh` convention ("meant to be run directly by the user in
their own terminal ... not launched by an agent, to avoid config.json races
with any other in-flight run"). Present the exact command plus the storage/
time forecast, then stop. Do not run it in the background "to save the user
a step" even after a scoping discussion looks settled -- wait for an
explicit "run it" / "go ahead" on the actual execution, separate from
agreeing on scope. If a campaign is already running and needs to change, say
so and let the user decide whether to interrupt it -- don't kill or relaunch
sweep processes unilaterally either.

## Gotchas already found and fixed

- `TrailingBothZScoreBreakout`'s swept "stop_loss" grid axis actually
  populates the real `trail_buy_pct` column, not SL -- the real SL is
  `fixed_sl` (`config.execution.fixed_stop_loss`), a single value per
  campaign. Don't confuse the two when reasoning about scope.
- `TrailingExitZScoreBreakout`'s kernel (`run_backtest_v18`) ignores
  `entry_timing` -- `close` only, no `open_check` variant. Encoded in
  `campaign_config.py`'s `entry_timings` list per strategy so the queue
  script doesn't waste a redundant pass.
- `TrailingBothZScoreBreakout` defaults to `open_check` only (2026-07-20) --
  matches the real live `watch_list` config for all 18 tickers; `close` was
  only ever an experimental comparison, not the default resweep scope.
  Re-add `close` explicitly if a future comparison needs it.
- Never blanket-`DELETE` existing `backtest_cache` rows before a resweep
  "just to be safe" -- `dispatch_parallel_grid`'s own per-node cache lookup
  (keyed on the full param tuple) already only recomputes genuinely-missing
  nodes; a manual delete throws away real, already-correct data for nothing
  (this happened once, was regretted, see `docs/backlog_cache.md` 2026-07-20).
