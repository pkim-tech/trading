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

## Step 3 progress addendum (2026-08-21/22 night, real-time handoff note)

Written directly into this doc rather than left in conversation, specifically so a
`coder2` context-clear doesn't lose it — none of this is in git history yet since most of
it is decided-but-not-yet-built.

**Built and committed**: `_load_node_inputs_ground_truth`/`run_single_backtest_node_ground_truth_isolated`/
`dispatch_parallel_grid_ground_truth` (Step 3 core dispatch, `d864b55`), Phase2/2.5-GT
wrapper functions (`9ab807a`, `cf810ba` — corrected a bogus fork-reported smoke test caused
by a missing `trail_pcts` key silently falling back to config.json's default), and a
Phase1-must-finish-before-Phase2-reads ordering guard (`aac8ae8`, after 5 real HIGH/MEDIUM
bugs across 2 review rounds — see that commit for the full list; the guard itself needed as
much scrutiny as the thing it guards).

**Running now (background, detached, survives session-clear)**: SOXL Phase1-coarse-GT full
grid (164,640 cells, 8 workers, `logs/ground_truth_phase1_soxl.log`) — this is the
reference/proof-of-concept full-density run, deliberately left untouched. A neighborhood-check
batch (1 worker, `logs/ground_truth_neighborhood_batch.log`) sequentially covering the other
11 Tranche-1 tickers, user-authorized 2026-08-22.

**Real finding so far (SOXL, from the early neighborhood check, before the full sweep
result exists)**: live config (arm=30%/trail_buy=3%/hold=70h) shows ~58.5% ground-truth
CAGR, but worst-neighbor CAGR in its immediate ±2 box is only ~12.0% — fails the plan's
20%-worst-neighbor-CAGR bar. Diagonal-specific: trail_buy_pct alone or hold alone both hold
up fine (~29-31%); only the combination together craters it. Next-best swept config
(arm=30/trail_buy=5/hold=77h, 81.6%) sits at the edge of the swept box — real island search
needed to know if something better exists just outside it. **Not yet resolved by a real
Phase2/2.5-GT run** — the 574.1% "interior peak" number reported earlier was retracted, it
was computed against a Phase1 dataset that was only ~3% complete (a bug in the ordering
guard's first draft let it through; fixed in `aac8ae8`).

**Decided but NOT yet built** (this is the part that would be lost on a clear):
- **Timeline problem**: full Phase1-coarse-GT at current density is ~9-15h/ticker
  (observed rate 3-4.3 nodes/s @ 8 workers, not the isolated 1.6s/cell benchmark — real
  contention from running 3 concurrent CPU-bound processes on a 12-core box) — ~3 weeks
  serial for 21 tickers, not viable.
- **Reduced-density grid, spec'd, not yet coded**: halve SL axis (14→7), TP axis (14→7),
  and `max_hold_hours` axis (20→10 values, 7h→14h step — confirmed this is **hourly bars**,
  not calendar days, via `config.json`'s `hold_time_caps`). Combined: 164,640→20,580 cells,
  exactly 8.0x fewer. ~1.86h @ 8 workers at the real observed rate. `trail_pct` axis (7
  values) left untouched.
- **Known risk of the reduced grid, accepted not solved**: `pick_island_centers` is a pure
  greedy rank-and-separate with no interpolation — halving the coarse sample count doesn't
  hurt Phase2's coverage near a chosen center (mesh is always a fixed ±4 unit-density box
  regardless of Phase1 density), but does raise variance in the ranking itself, risking a
  center-selection miss with no compute-side signal anything went wrong. This is the SAME
  risk category as the ordering-guard bugs just found — a silent-wrong-number risk, not a
  crash risk.
- **Extra Phase2 generation, decided as a partial mitigation**: helps the "true peak just
  outside the ±4 window, reachable by hopping" failure mode; does NOT help "true peak never
  sampled densely enough to seed as a candidate center at all" (structural — Phase2 never
  globally re-explores the original coarse space). Cost: ~1-3h extra per ticker on the
  reduced grid. **Decision: add it as standard** for the reduced-density runs (not yet
  wired into code).
- **Window-shortening for the discovery pass, spec'd, not yet coded**: use a shorter window
  (6mo-1yr vs the validated 2024-08-21..2026-08-20 ~2yr) ONLY for Phase1-coarse-GT
  discovery/triage, then re-run Phase2/2.5-GT refinement on the FULL validated 2yr window
  for whichever islands survive triage — avoids the regime-bias risk of trusting a short
  window's ranking as final. Day-skipping/subsampling (as opposed to window-shortening) was
  evaluated and **rejected outright** — breaks rolling-indicator computation and
  hold-time/trailing-peak state continuity, not just noisier.
- **Net plan once built**: reduced-density-grid + shortened-window Phase1-coarse triage
  across all 21 tickers (~1-2h each, likely doable same-night), full-density
  Phase2/2.5-GT-with-extra-generation confirmation only for whichever tickers/islands the
  triage flags as promising or fragile — not a blanket full-density campaign.
- **Not yet authorized**: which tickers beyond SOXL get treated as "flagged, needs full
  confirmation" — that's a call for once real triage numbers exist, not decided in advance.
- **2yr vs 5yr window, flagged 2026-08-22 (early AM)**: all current GT campaigns/benchmarks
  are scoped to the 2yr window validated by the Step 2a parity gate
  (2024-08-21..2026-08-20), NOT the full 5yr minute data already purchased/fetched for all
  82 tickers (see the 2026-08-21 "very late" session_cache entry). Whatever real
  nodes/s rate gets confirmed for the 2yr window will drop ~2.5x (proportional to total
  bars) once/if campaigns expand to the full 5yr window — a real, expected scaling factor,
  not a bug. Not deciding now which tickers/campaigns need 5yr vs 2yr — flagging so it's not
  forgotten when that question comes up.

**Process note**: the silent-process-restart scare earlier tonight was NOT an unexplained
external event — it was `coder2`'s own deliberate `kill <parent_pid>` (moving logs out of
job tmp/ into `logs/`) not cascading to `ProcessPoolExecutor` worker children as expected
(POSIX SIGTERM doesn't propagate to a process pool by default), briefly orphaning the old
workers for about a minute before being caught and killed. Already fully explained at the
time; re-raised as a concern later due to a relay error, then re-confirmed closed (checked
dmesg/journalctl/crontab directly — nothing external).

## v6 scope decision, 2026-08-22 — narrow sunset, signal mechanics unchanged

Real goal clarified: v6 sunsets v5/v5.1 entirely (not a parallel option), and doing so is
also a deliberate forcing function to exercise/refactor the candidate-promotion pipeline
(Phase 4 tooling: candidate reports, `top_safe_nodes.py`, promotion checklist) rather than
just plumbing to view GT numbers.

**Scope decided narrow, not broad**: v6 keeps v5.0's trade **signal mechanics** byte-for-byte
— GT only fixes **fill/execution modeling**, not when a decision fires. Precisely:

- **Stays v5.0-identical (trigger timing)**: buy (2 daily windows, hourly z-score,
  `entry_timing` open_check/close semantics), arm (`strategies.py`'s `at_bar_close` gate),
  and TIME (`hours_held >= max_hours_to_hold`, same gate — though TIME isn't really a
  choice, it's definitional: `max_hold_hours` is hour-denominated by construction, so it
  rides along regardless of bar resolution elsewhere).
- **Goes full GT (fill/execution modeling)**: entry fill price/timing (continuous
  broker-tracked trailing-buy, not bar-sampled), SL (continuous resting-stop with real
  overnight gap-through), TRAIL (continuous peak/stop tracking once armed — note: for the
  live `TrailingBothZScoreBreakout` strategy, reaching `tp_price` does NOT return a `'TP'`
  exit, `strategies.py:439-442` — it only sets `state['trailing']=True` and arms the
  trailing mechanism; the actual exit is TRAIL, continuous, not bar-gated. Other strategy
  classes, e.g. `TrailingExitZScoreBreakout`, do have a real bar-close-gated TP exit —
  irrelevant here since TrailingBoth is the live default).
- Real worked example confirming this split against actual live data: SOXL `ira`/wl_id=92,
  signal window 2026-08-18 15:25-15:40, actual fill 15:59:35 @ $128.755 (continuous
  trailing-buy tracking — the ~34min signal-to-fill gap is exactly what GT models),
  overnight hold, SL exit 2026-08-19 @ $125.18 (below the nominal ~$126.18 stop — consistent
  with a real gap-through at the open, which GT's continuous SL modeling captures and the
  old kernel's bar-sampled assumption could not).

**Explicit non-goal for v6, deferred to v6.1 below**: a discomfort with higher trading
frequency (user's direct call) rules out any timing-architecture direction that means
checking/trading more often — this bounds what v6.1 can even explore, not just what v6
does.

## v6.1 parking lot — timing-architecture variations (separate future backtest, not v6)

Explicitly NOT part of the v6 sunset — folding the sunset (bounded, has a finish line: match
v5's candidates under real fills) together with an open-ended timing-parameter search would
turn v6 into a project with no natural stopping point ("we could get stuck in v6.1 for days
until we even finish a backtest" — user's own framing). Candidates to explore once v6 ships
and proves itself, each requiring its own real backtest validation, not a config tweak:

1. **Arm: bar-close gate → continuous.** Currently only re-checked once/hour at bar close
   (`strategies.py:439`); a continuous check (matching how SL/TRAIL already work) could arm
   on a better intrabar peak instead of missing a spike that reverts before close. Real
   backtest question, not a fill-modeling fix — changes which trades even get armed.
2. **Buy: frequency-as-risk-control, independent of the original manual-trading reason.**
   The 2-window structure was originally motivated by wanting to limit *manual* trading
   effort (`project_immediate_entry_motivation` memory) — automation removes that
   justification, but throttling entry frequency may still be a real, deliberately-kept risk
   control on its own merits (caps capital committed per day), not just a leftover
   constraint to remove now that it's technically unnecessary.

Both require a genuinely new backtest (trade selection/count changes, not just re-priced
fills on the same trade list) — same category as this plan's other deferred
`[new-strategy]` ideas, not something GT absorbs. **Bounded by the trading-frequency
constraint above**: any v6.1 direction that means trading more often is off the table
regardless of what a parameter search might otherwise suggest.

## Full game-plan shape, 2026-08-22 (session discussion, not yet started beyond Phase 1)

1. **Data foundation** (in progress, `coder`): fixing windows + new data pipeline for
   minute/hourly derived data, ahead of a longer backfill (4-4.75yr lookback leaning,
   preserving at least one held-out quarter for rebalance testing — user's call, not
   swept).
2. **Sweep methodology**: throughput confirmed fine (the 3.66/s vs 823/s scare was a
   pre-fix log, real cause was uncached repeat-compute, already fixed) — reduced-density
   grid + extra Phase2 generation stay optional fallbacks, not required.
2.5. **Independent-simulator cross-validation, decided 2026-08-22 — runs BEFORE Phase 3,
   not after.** Extend the from-scratch independent reimplementation
   (`sim_minute_groundtruth_independent.py`, already validated SOXL/HIBL to <0.06% against
   real `trade_log` fills) to the other 10 Tranche-1 tickers with real live trade history.
   Cheap (replays real trades, not a full grid sweep) and it's the strongest available
   evidence GT's fill modeling matches reality, not just that the numba port matches the
   Python prototype (Step 2a's parity gate already proved that, separately). De-risks
   trusting Phase 3's full sweep output before spending real compute on it.
3. **Run the real triage sweep** across the universe once 1, 2, and 2.5 settle.
4. **Build the GT-aware tooling**: candidate-report pipeline, `top_safe_nodes.py`
   kernel-version-aware selection, promotion checklist's 3 GT-dependent checks (9/10/13).
   **Alpha removed entirely** (not just de-prioritized) from schema/reports/ranking —
   CAGR (or GT worst-neighbor-CAGR) is the sole metric going forward. **Core+overlay
   joint optimization folded in here** (moved from the old Follow-on bucket, see below) —
   `ensure_overlay_for_node`/`run_overlay_shim.run_for_node` confirmed to only compute one
   fixed drought variant (confirm_days=10, vol_gate=off) + one addon variant, automatically
   but only for whichever core node already won on core-only CAGR, never searched jointly.
   Real deployability constraint: add-on needs margin-borrowed capital (`brokerage`-only,
   Reg-T margin), not available in `ira`/`roth`/`sep` (cash accounts) — the joint sweep
   scopes add-on to `brokerage`-bound nodes only; drought has no such constraint and
   applies universe-wide. `kernel_version` added as a real schema column (future-proofs
   the data model for eventual multi-strategy use) but no general pluggable-kernel
   selector built — no second kernel needs one yet. **Review-gate applies**: alpha
   removal touches `ROBUST_ALPHA_SQL`/`run_optimization_sweep.py`, a backtest kernel
   module under CLAUDE.md's mandatory paired independent-cold + contextual Opus review —
   flag explicitly here rather than relying on session-wrap to catch it after the fact,
   per this project's own `908a6f0` incident (shipped once without review because
   attention was on a narrower sub-problem, same risk shape as this megaproject).
5. **Pick new v6 candidates, sunset v5/v5.1** — no re-derivation of old picks, straight
   fresh selection via Phase 4's tooling against Phase 3's sweep output. **Rollback
   posture, decided 2026-08-22**: if a v6 candidate turns out wrong post-promotion,
   disable trading first (safe stop, not a config revert) — v5 configs stay recoverable
   by candidate node id if truly needed, but treated as a last-resort stopgap, not a real
   "safe" fallback, since the whole premise of v6 is that v5 probably wasn't the best node
   to begin with.
6. **Promote + harness/script cleanup**: run the real promotion process
   (`docs/watchlist_candidate_checklist.md` etc.) for each new v6 candidate; triage-then-fix
   the real surface found by direct grep — 22 non-test source files + 20 test files with
   hardcoded `'v5'` string references, 6 of 7 candidate/promotion pipeline files with
   baked-in hourly-only trade generation (`get_trades_and_bars`/
   `simulate_trail_both_annotated`) — most of the pipeline files get fixed as a byproduct
   of Phase 4, several of the source files are dead one-off research scripts that should
   be archived (ties into the existing `[tooling]` scripts/-cleanup backlog item) rather
   than "fixed." **Biggest single item**: `evening_status.py` Part 3's real-vs-kernel
   divergence check (`compute_divergence`/`get_backtest_trades_in_window`, writes to the
   persisted `divergence_check_log` table) is the literal decision mechanism the
   already-resolved quarterly-rebalance item said to watch (30d trailing, 10pp threshold,
   "hold current config, watch this instead"). Swapping the replay to GT changes what
   "predicted" means, so the threshold needs real recalibration, not just a code swap, or
   the daemon's own drift detection goes silent or false-alarms on normal GT-vs-real noise.
   **Review-gate applies**: promotion logic and any `signals_*.py`/`schwab_*.py` touch
   here needs the same paired review — flag explicitly, same reasoning as Phase 4 above.
   **Wash-sale risk, flagged 2026-08-22 (adversarial review)**: sunsetting a v5 position
   and opening a v6 position on the same ticker is precisely the GDXU incident's shape
   (`trading_incident #2`) — `docs/watchlist_candidate_checklist.md` check #16 (commit
   `3a88c1d`) already covers taxable-loss-then-IRA-repurchase-within-30-days at promotion
   time, but it's a manual step with no code enforcement (see the still-open
   `[live-trading][tax]` backlog item). **Decided: run check #16 explicitly for every
   v5→v6 sunset transition**, not just new-ticker promotions — the checklist already
   covers this, the sunset just needs to actually invoke it every time, not skip it
   because "this ticker's already live, it's not really a new promotion."
   **CLAUDE.md staleness, flagged 2026-08-22 (adversarial review, agreed)**: the "Live
   Trading — Current State" section's strategy-version conventions (references to v5/v5.1
   as live defaults) will go stale the moment v6 replaces them — capturing here explicitly
   so session-wrap's "reconcile older bullets on the same topic" step doesn't miss it the
   way the 2026-08-04 USO K-1 bullet went stale/self-contradicting before someone caught it.
7. **Test-harness alignment** (paper/dry_run/harness, "the backtest is still our
   principle" — user's framing): checked directly, 2026-08-22 — paper trading
   (`paper_trading.py`'s `update_paper_buys()`) and dry_run
   (`signals_notify.py`'s buy-update loop /`_fill_dry_run_buy`) already independently
   implement continuous every-poll trailing-buy tracking (`compute._current_price` +
   running-low/trigger), matching GT's core premise (continuous, not bar-sampled) —
   **not a structural gap**, just needs fine-grained calibration against GT (poll
   cadence, running-low definition edge cases), which drift metrics can catch. The real
   gap is `tests/fake_broker.py`: zero autonomous fill-timing model at all — `MARKET`
   fills instantly at whatever quote is set, `STOP`/`TRAILING_STOP` orders sit `WORKING`
   until the test itself calls `advance_price()`/`force_fill()`, entirely hand-scripted
   per test. **Decided scope, not a harness rewrite**: audit existing
   `advance_price()`/`force_fill()` sequences across the test suite against what GT
   considers realistic (e.g. a trailing-buy scenario should script a continuous
   multi-tick convergence, not a single-jump fill), and require new scenarios follow that
   convention going forward. Explicitly rejected as out of scope: having the harness
   itself validate backtest predictions (a different question, already owned by the
   evening_status divergence checks) or blurring canary/fake_broker's existing role as
   execution-mechanics proof (kernel-independent by design, confirmed unaffected by GT
   earlier this session) into candidate-quality proof.
8. **Live-trading execution backlog** (was "Phase 6" before this got renumbered): runs in
   parallel throughout, unaffected — dispatched via `research`/`coder2` as normal,
   doesn't wait on any of the above.

**v6.1 boundary applies throughout**: no direction in any phase above should mean trading
*more often* — the user's explicit discomfort with higher frequency bounds what's even
worth exploring, separate from what a parameter search might suggest.

## Follow-on (separate future project, not this plan)

Folding together two previously-separate deferred backlog items, all sharing the same
real trigger condition ("a new strategy variant/kernel architecture is actually being
designed"): the `backtest_cache` overloaded-columns schema definition (2026-08-07,
deferred), kernel versioning (`project_kernel_versioning_idea` memory, 2026-07-20, a
`KERNEL_VERSION` column so a cached row self-documents which kernel logic produced it).

**Status, 2026-08-22: schema rework now IN PROGRESS** (dispatched to the `backtester`
session) — refactoring `backtest_cache` to a JSON parameter-definition column instead of
adding more overloaded columns per strategy. Triggered by yet another instance of the same
root cause (a column meaning different things per strategy causing a real interpretation
bug) — same failure family as the `take_profit` overload (arm-sell-pct vs. real take-profit
vs. drought-arm-override, 4 confirmed instances now) that motivated deferring this in the
first place. Same dispatch also covers a query-optimization pass over everything reading
`backtest_cache`, to optimize real table usage once the JSON-parameter-definition shape
lands (not a separate ask — same underlying schema change, same session).

**Core+overlay joint kernel optimization has moved OUT of this Follow-on bucket** —
folded into the main v6 sunset scope instead (see the "v6 scope decision" section above),
per 2026-08-22 discussion: `ensure_overlay_for_node`/`run_overlay_shim.run_for_node`
confirmed to only compute a single fixed drought variant (confirm_days=10, vol_gate=off)
+ single addon variant, automatically but only for whichever core node already won on
core-only CAGR — never searched jointly with core params. Real deployability constraint
found in the same discussion: add-on requires margin-borrowed capital (`brokerage`-only,
Reg-T margin), not available in `ira`/`roth`/`sep` (cash accounts) — the joint sweep must
scope add-on consideration to `brokerage`-bound nodes only; drought has no such constraint
and applies universe-wide.
