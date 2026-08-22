# Ground-Truth-Native Kernel Rebuild

Drafted 2026-08-21 (night), for Opus + Sonnet review before any build starts.

## Why

A single-session investigation (2026-08-21, see `docs/research_log.md`'s "SOXL/HIBL
real-execution CAGR correction" entry and `docs/backlog_cache.md`'s two HIGH items from
that night) found that the backtest kernel's per-bar OHLC resolution (`possible` /
`pessimistic` / `certain` — a guess at intrabar Low-vs-High ordering, since hourly bars
don't record it) is systematically optimistic, and that optimism varies by ticker/config.
Real 1-minute price data (Massive.com, now covering the full 82-ticker v5/v5.1
candidate universe, 5 years back) resolves that ambiguity directly instead of guessing.

**Correction (caught in the 2026-08-21 review round)**: an earlier draft of this plan cited
a specific number (SOXL -5.1%/HIBL -2.9% CAGR) as the motivating example. That number was
itself later found to be a bug artifact and was refuted by an independent clean-room
re-implementation the same night (SOXL +58.7%, HIBL +54.0%, all 12 tickers positive). The
real motivation for this plan is not any specific number — **it's that this project
currently cannot reliably reproduce its own backtest results at all.** Two independently
built minute-resolution scripts, both claiming to answer the same question against the same
real data, disagreed by ~90 percentage points of CAGR before their bugs were found. That's
the actual problem this plan exists to fix: a trustworthy, single, validated way to check
whether the kernel's numbers reflect real execution — not a specific correction to any one
ticker's result.

This plan is deliberately scoped to avoid repeating that disagreement: build once,
correctly, reusing the validated logic from `scripts/sim_minute_groundtruth_independent.py`
(cross-checked against real `trade_log` to <0.06% on SOXL's actual 2026-08-20 trades)
rather than another parallel one-off script. This also directly answers the standing
`docs/deep_backlog.md` 2026-08-14 item ("fragmented live-vs-backtest verification
tooling doesn't answer the real question; needs consolidation, not another one-off
script") — this plan IS that consolidation, for the backtest-accuracy side of that gap.

## Reconciliation with the same-night SOXL/LABU same-bar-SL fix (flagged by all 3 reviewers)

`docs/backlog_cache.md`'s same-bar-SL item (resolved-direction 2026-08-21, not yet built)
decided: suppress live's fill-bar SL enforcement so live matches the OLD hourly kernel's
structural blind spot (it never evaluates a fill bar's own exit). This plan's new kernel
does the opposite — SL continuous from the instant of real fill, matching live's actual
current behavior. These are not two independently-valid choices; only one can be the real
target.

**Resolution**: the old fix was a workaround for the OLD kernel's limitation, not a
statement that continuous fill-bar SL is wrong. Once the new ground-truth kernel exists and
is validated, it has no fill-bar blind spot to work around — live's real continuous SL
(what it already does today) is the behavior the new kernel confirms as correct. **The
old same-bar-SL fix should NOT be built** if this plan proceeds; it would be actively
wrong once the reference kernel changes. This is the first, concrete instance of Step 7's
"audit live-side workarounds built for the old kernel's limitations" — surfaced now
because a reviewer caught it, not deferred to Step 7's later audit.

## What supersedes what

**Replaced/superseded by this work:**
- `possible`/`pessimistic`/`certain` recompute item (2026-08-09, tabled)
- `possible`/`pessimistic`/`certain` overlay-tooling-is-possible-only item (2026-08-09, tabled)
- Per-trade worst-execution-path resolution idea (2026-08-21) — this plan IS that idea, built
- Trailing-stop peak-update ordering optimism (2026-08-21) — resolved directly, no more guessing
- v5/v5.1 candidate selection HIGH item (2026-08-21 night) — this plan is the fix
- SOXL/HIBL negative-CAGR HIGH item (2026-08-21 night) — folds in as the first re-verified tickers

**Explicitly NOT in scope for this plan:**
- lowvol/same_day_block as standalone overlay axes
- Full drought/add-on resweep (confirm_days × vol_gate grid) — sample sizes are too thin at
  2yr coverage to trust a sweep result (see research log: 1-9 trades per cell); add-on runs
  in full (no confirm_days/vol_gate axis to sweep), drought runs ONCE at confirm_days=3 as a
  directional indicator only, not a validated selection input
- Core+overlay jointly-optimized kernel (drought/add-on state machines built into the sweep's
  hot loop) — a larger, separate future architecture project
- 10-year historical data purchase
- SOXS `drought_confirm_days=1` fix — real, already recommended (see research log's SOXS
  entry), deliberately not bundled into this plan
- Ground-truth resolution for periods outside real minute-data coverage (falls back to
  today's hourly kernel for those dates — a hybrid, not a full historical replacement)

## Architecture

**What goes into the new kernel path** (verified logic, ported from
`scripts/sim_minute_groundtruth_independent.py`, not redesigned):
- Entry/WAIT resolution: continuous real minute-level running-low tracking, firing the
  instant the real bounce trigger crosses. Replaces the 3-way guess entirely.
- SL monitoring: continuous from the moment of real fill (matches live's actual behavior —
  a real resting protective stop order, `schwab_client.py::place_stop_loss` (~line 1130,
  `OrderType.STOP`) — tracks continuously, not bar-sampled. Corrected citation: the original
  draft cited `place_trailing_buy`/`OrderType.TRAILING_STOP` (~line 864), which is the real
  mechanism for the trailing-buy ENTRY, not the protective SL — same underlying
  continuous-tracking conclusion, wrong function name).
- Trailing-stop peak tracking: continuous minute-level, no Low-before-High/High-before-Low
  guessing.
- Missing-minute policy: skip absent minutes (a Polygon-style feed means "no trade
  printed," not "no data" — confirmed via a coverage-report showing full [Low,High] range
  coverage even at low per-minute fill %, e.g. HIBL ~25% fill still 100% range-covered).
  Explicitly NOT the old "hourly backstop" fallback (proven tonight to silently re-inject
  the kernel's own optimism).
- Fallback to today's existing hourly kernel for any date outside real minute coverage.

**What stays exactly as today** (confirmed deliberate live/kernel behavior, not a
resolution artifact — do not touch):
- Signal detection (idle→waiting): still anchored to the two daily checkpoint windows
  (open_check/close_check), daily SMA/Std computed the same way (`prep_inputs`'s
  `daily_lookup = {d: i-1}`, prior-day-only).
- Arm/TP check: still bar-close gated, once/hour — verified in `strategies.py`'s
  `check_exit` (`at_bar_close` early-return before the `tp_price` test), not continuous.

**Open variable, not a pre-decided default (resolved 2026-08-21 review round)**: whether
close_check can fire the same bar as an already-resolved open_check (`--no-same-bar-reentry`
in the prototype). All three review rounds (Opus/Sonnet/Fable) independently flagged this as
materially changing results (SOXL 58.7%→77.9%, HIBL 54.0%→28.2% with it off) and unresolved
in the original draft. **Decision**: don't pre-decide it — run Tranche 1's first ticker with
it as an explicit on/off variable (same spirit as possible/pessimistic/certain running in
parallel rather than being argued about upfront). If it doesn't materially move later
tickers' results, drop it as a fixed toggle in subsequent runs instead of carrying it as a
permanent axis.

**Performance — corrected, was wrong in the original draft**: computing one real resolution
instead of three parallel guesses is a real per-*guess* saving, but all 3 reviewers pointed
out this ignores that the loop now iterates real minutes instead of hourly bars — roughly
20-60x more data points per hour of trading. Additionally, the naive "1 instead of 3" framing
overstates the real saving: continuous SL monitoring effectively runs as its own pass (closer
to "running another node" than a free byproduct of the single resolution), eating a large
chunk of the theoretical 3x-to-1x reduction — real savings are probably closer to 1/3 of the
naive estimate, not the full 2/3, and get eroded further once overlay calculations
(add-on/drought) run on top. Net effect could plausibly be no saving at all, or a genuine
slowdown. Actual performance is unknown until benchmarked — Step 2's numba port should
include a real timing measurement (single ticker, single cell, core-only AND with overlays)
before any full-grid campaign is sized or scheduled.

**Search strategy: unchanged.** The existing sweep engine's phased approach (Phase 1
coarse grid → Phase 2 island refinement → Phase 2.5 fine mesh) stays as-is — it's an
orthogonal efficiency mechanism (which cells get computed) from what changed here (what
each cell computes). No reason to abandon a working search strategy.

**Scope**: `TrailingBothZScoreBreakout` and `TrailingExitZScoreBreakout` only — the two
strategies actually live. Not the older, unused strategy classes.

## Cliff-safety redefinition

Today: `robust_alpha = MIN(possible, pessimistic, certain)` across a config's grid
neighborhood — doing two jobs at once: (1) parameter-neighborhood robustness, (2) hedging
against resolution-ambiguity uncertainty.

With ground truth, job (2) disappears (one real number, not three guesses). Cliff-safety
becomes: is ground truth's own value robust across the parameter neighborhood — same
concept, computed from a single trustworthy number instead of a MIN() over three guesses.
Real behavior-change risk to flag in review: the old MIN() was implicitly *more*
conservative than pure parameter-robustness alone (it was also hedging ambiguity), so some
previously-rejected "not cliff-safe" configs could newly pass, or vice versa — worth the
reviewers' explicit attention.

**New selection bar**: worst-neighbor ground-truth CAGR > 20% (replaces the earlier
alpha-over-SPY framing — simpler, and would have caught SOXL directly).

## Order of operations

**Tranche 1 — current live watchlist first** (real capital, most urgent to know about):
all real `state='live'` nodes, ground-truth-verified with the new kernel.

**Tranche 2+ — full 82-ticker v5/v5.1 candidate universe** (lower urgency, no real capital
at risk yet). Minute data for all 82 tickers is now fetched (5 years, 2021-08-2x through
2026-08-21, confirmed via file coverage check).

**Prerequisite, before trusting any of the above**: two data-quality checks not yet run at
full scope —
1. Corporate-action discontinuity check: cross-reference known real splits (the
   `corporate_actions` table, e.g. SOXS's confirmed 2026-07-14 split) against the raw
   5-year minute data for a spurious price discontinuity Massive's `adjusted=true` might
   not have handled consistently with the hourly CSVs' own split-guard rescale.
2. Full-window coverage/population check: extend tonight's `--coverage-report` (verified
   2yr/6-ticker) to the full 5yr window across all 82 tickers.

## `backtest_cache` write policy (prerequisite, not follow-on — all 3 reviewers flagged this)

The original draft deferred `KERNEL_VERSION` to "follow-on, separate future project." All
three reviewers independently found this unsafe: `ROBUST_ALPHA_SQL` = `MIN(alpha_vs_spy,
COALESCE(pessimistic...), COALESCE(certain...))` — writing ground-truth rows into the
existing columns would either get silently `MIN()`'d against unrelated hourly-kernel
sibling rows, or (if pessimistic/certain are left NULL) silently `COALESCE` to a
non-comparable value. `dispatch_parallel_grid`'s per-node cache lookup would also silently
serve old hourly-kernel rows back out as if they were ground truth — the same stale-cache
bug shape that already hit this project twice (2026-07-20).

**Decision**: new `version` tag for ground-truth campaigns — `v6` (freed up 2026-08-21 by
renaming the old, never-materialized "idle-capital-parking" backlog nickname that
previously used it; confirmed via a live-DB check that `backtest_cache` has zero existing
`v6` rows, no data collision). Not overwriting v5/v5.1 — never delete backtest_cache data
per standing convention. Note: "v6" has carried multiple informal, never-shipped meanings in
this project's history (idle-capital-parking, a separate closed "momentum-exhaustion"
strategy idea, an example string in `docs/portfolio_construction_notes.md`) — none of them
ever wrote a real `backtest_cache.version` value, so this is the first real claim on the
tag. Stand up the `KERNEL_VERSION` column now (the
already-designed-but-unbuilt idea from `project_kernel_versioning_idea` memory), not as a
separate future project — this plan is exactly the trigger condition ("a new kernel
architecture is actually being designed") that idea was waiting for. Every `v6` row
self-documents which kernel logic (numba-ported ground-truth vs. hourly `possible`) produced
it. Check `prune_backtest_cache.py`/the `prune-validation` skill before this ships — both
assume one kernel per version.

## Step-by-step

1. Run the two data-quality checks above across the full 82-ticker/5yr dataset.
2. Port the validated minute-resolution logic from `sim_minute_groundtruth_independent.py`
   into a numba-compatible kernel function (new, alongside the existing kernel functions in
   `backtester.py` — not replacing them, since the hourly kernel is still needed for
   pre-coverage dates and as the historical baseline).
2a. **Mandatory parity gate before the port is trusted for anything else** (all 3 reviewers
   flagged this, given two prior implementations disagreed by 90pp tonight): the numba port
   must reproduce `sim_minute_groundtruth_independent.py`'s per-trade output byte-identically
   on all 12 real live tickers, at both same-bar-reentry settings, as a pinned test — same
   pattern this project already uses for kernel parity (`simulate_trail_both_annotated`,
   `tests/test_certain_resolution_fix.py`). No sweep output is trusted until this passes.
3. Wire the new kernel into the existing sweep engine's per-cell dispatch (Phase
   1/2/2.5 unchanged), scoped to Tranche 1 (live watchlist) first.
4. Recompute cliff-safety from the new kernel's ground-truth output directly; apply the
   new 20%-worst-neighbor-CAGR bar.
5. Compare Tranche 1's ground-truth-based selection against the actual current live
   watchlist. Flag every divergence explicitly. **Do not auto-action any divergence** —
   report only, matching tonight's explicit "don't touch live state" call.
6. Once Tranche 1 is trusted and reviewed, repeat for Tranche 2 (full candidate universe).
7. Separately, once the new kernel exists: audit every live-side workaround that was built
   specifically to compensate for the OLD hourly kernel's known structural limitations
   (e.g., the same-bar-reentry cooldown, built 2026-08-14 specifically because the old
   kernel can't represent same-bar re-entry) — these may need to change once the kernel
   they were compensating for no longer has that limitation. Not a separate project; falls
   out naturally once Tranche 1's core ground-truth results exist.

## Follow-on (separate future project, not this plan)

Folding together three previously-separate deferred backlog items, all sharing the same
real trigger condition ("a new strategy variant/kernel architecture is actually being
designed"): the `backtest_cache` overloaded-columns schema definition (2026-08-07,
deferred), kernel versioning (`project_kernel_versioning_idea` memory, 2026-07-20, a
`KERNEL_VERSION` column so a cached row self-documents which kernel logic produced it),
and core+overlay joint kernel optimization (this session, 2026-08-21).
