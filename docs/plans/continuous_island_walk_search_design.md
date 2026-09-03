# Continuous/exhaustive island-walking search — design proposal (not implemented)

Status: **design doc for user review, not built.** Written 2026-09-03, per
`docs/backlog_cache.md`'s "Written up 2026-09-02, awaiting user review before dispatch"
entry. Do not implement from this doc without an explicit go-ahead — the backlog item's
own status line already says this is exactly the state it should stay in until reviewed.

## This is NOT the same problem tonight's N_ISLANDS fix solved

Easy to conflate, so stated up front: commit `686a098` (same session, same investigation
arc) fixed a **selection/promotion bug** — `find_missing_window_z_top_n`'s backfill let one
strong island's cells consume both of a (window, z) combo's backfill slots, silently
dropping a second, real, distinct island's candidate (HIBL's live-promoted node,
`candidate_nodes` id=907, arm=28/trail_buy=3, was the concrete case). That fix, plus the new
unconditional `top_up_window_z_island_quota` and the `_check_live_node_regression` guard,
all operate on **islands that Phase2's mesh generation already found** — they ensure found
islands survive into `final_candidates`, nothing more.

This doc is about a **different, earlier stage**: whether Phase2's mesh generation finds
enough islands *in the first place*. Today it's capped at a fixed `N_ISLANDS=3` per
(window, z, trail_pct) combo, walked for a fixed `N_GENERATIONS=3` — an arbitrary cutoff
regardless of whether a given ticker/scope's landscape is sparse (3 islands overkill) or
rich (3 islands leaves real regions unexplored). Fixing tonight's selection bug does not
change how many islands Phase2 discovers; this doc's proposal changes that discovery
process itself. The two are complementary, not overlapping — this design should assume
tonight's selection-stage fix is already in place and not attempt to re-solve any part of
it.

## Real motivating finding (2026-09-02, HIBL/KORU/NUGT/GDXU investigation)

All 4 "missing" live-promoted nodes turned out to be genuine, strong, live-validated local
optima under current data (seed-diag reproduction matched real historical CAGR almost
exactly for all 4 — HIBL 52.40%, KORU 74.43%, NUGT 114.53%, GDXU 90.86%) that Phase2's
island search never walked at all — not because they were found-then-dropped (that's
tonight's separate bug), but because a fixed `N_ISLANDS=3` per combo, in a real 16-combo
grid (windows=[5,10,15,20] × z=[0.5,1.0,1.5,2.0]), only ever explores a small, arbitrarily-
sized slice of what the coarse Phase1 grid already suggests is out there.

## User's proposed design (2026-09-02, relayed via the research session)

> Rather than a fixed top-N_ISLANDS cutoff, run island exploration continuously/
> sequentially — walk ONE island (3-generation fine-mesh) to convergence, then move to the
> next-best remaining unexplored region, repeat until either no islands remain or a
> stopping criterion is hit (e.g. K consecutive islands fail to beat the current
> best-found candidate).

Explicitly framed as reordering coverage and replacing a fixed N with an adaptive stopping
rule — not reducing the total permutation space ("there are just too many permutations,"
per the user's own words). A rich ticker/scope goes as deep as it needs; a sparse one stops
early instead of paying for the same fixed N regardless of landscape.

## Today's real structure (bench_phase1_phase2_inmemory.py, as of commit 686a098)

The generation loop (`run_one_fixed_sl`, ~line 2017-2064) is the concrete thing this
proposal replaces. Per fixed_sl:

```
for gen in range(1, N_GENERATIONS + 1):          # N_GENERATIONS = 3, fixed
    for z in Z_THRESHOLDS:                       # 4 values (real v6.5/v6.5.1 grid)
        for w in WINDOWS:                        # 4 values
            for tpct in TRAIL_PCTS:               # 7 (TrailingBoth) or 1 (TrailingExit)
                df_wz = df_gen_pool[... filtered to this (w, z, tpct) ...]
                centers = pick_island_centers(df_wz, n=N_ISLANDS, rank_col="cagr")  # N_ISLANDS = 3, fixed
                for (tp_c, sl_c) in centers:
                    # ±FINE_RADIUS box around each center -> phase2_tasks
    new_tasks = phase2_tasks - explored_tasks
    if not new_tasks:
        continue   # THIS combo's islands converged to already-explored territory
    # dispatch new_tasks, accumulate into df_gen_pool for next generation
```

Two things worth being precise about, since the proposal reuses both:

1. **The `if not new_tasks: continue` line (~line 2046) is already exactly the
   convergence signal the proposal wants** — a combo whose freshly-picked centers mesh
   entirely inside `explored_tasks` has converged; no separate detection logic is needed,
   this is a real, already-working mechanism. The gap isn't detecting convergence, it's
   that ALL combos share one global `gen` counter capped at 3, so every combo gets
   the same fixed budget regardless of whether it converged in 1 generation or was still
   finding new territory at generation 3.

2. **`N_ISLANDS=3` is the per-combo island COUNT, separate from the generation CAP.**
   Today, each of the 16 combos always gets exactly 3 islands (whatever `pick_island_
   centers` returns, up to 3), each walked for up to 3 generations. The proposal
   replaces the fixed island count (not just the generation cap) with "keep pulling the
   next-best island until a stopping rule fires" — a bigger change than just raising
   `N_GENERATIONS`.

## Proposed design

### 1. A global priority queue of (combo, island) exploration units, not islands-per-combo-in-parallel

Today's loop iterates all 16 combos' N_ISLANDS=3 picks together, one shared generation at
a time. The proposal wants ONE island explored to its own convergence before moving to
"the next-best remaining unexplored region" — implying a single ordering across ALL
combos, not 16 independent per-combo loops. Concretely: seed a priority queue from Phase1's
own coarse grid (`df1`, already computed and in-memory — no new compute needed for this
step), ranked descending by `cagr` (matching `pick_island_centers`'s own `rank_col`
convention). Each pop pulls the next `pick_island_centers`-style center **not yet explored**
in ANY combo — i.e. repeatedly call `pick_island_centers(df1_filtered_to_combo, n=<current
count + 1>, rank_col="cagr")` and take the newest entry, rather than fixing `n=3` upfront.
This preserves the exact same per-combo scoping and min-separation logic already in place
(`ISLAND_MIN_SEP`, `FINE_RADIUS`) — the only change is that "how many islands to pull from
this combo" becomes open-ended instead of fixed at 3.

### 2. Walking one island to its own convergence

For the popped (combo, island-center) pair: run the existing ±`FINE_RADIUS` box expansion →
dispatch → check `new_tasks` against `explored_tasks`, generation by generation, **for this
island alone** — reusing the exact same `if not new_tasks` signal, just scoped to one
island's own walk instead of the whole grid's shared generation counter. Needs its own
per-island generation cap as a safety valve (a genuinely pathological landscape could keep
finding "new" cells indefinitely if `FINE_RADIUS`'s box keeps sliding into fresh territory
each generation) — **open question for the user**: what's a reasonable hard ceiling here?
Today's fixed 3 is itself untested against "how many generations does a real island
actually need to converge" beyond the empirical observation already on file (a real run
this session: gen 1 found 19,880/19,880 new cells, gen 2 found 560/20,440 new, gen 3 found
0/20,440 new — converged by generation 3 in that case, but that's ONE combo's aggregate
across the whole grid, not a single island's own number).

### 3. The stopping rule: K consecutive non-improving islands

Pop the next island off the priority queue, walk it to convergence, compare its best cell's
`cagr` against a running best-so-far. If it doesn't beat the running best, increment a
non-improvement counter; if it does, reset the counter to 0 and update the running best.
Stop when the counter reaches K, or the queue is exhausted.

**Open question, needs a real user decision, not a default I should silently pick**: is
"the running best" a single GLOBAL best across all 16 combos, or a PER-COMBO best? These
produce genuinely different behavior:

- **Global best**: matches the user's own wording ("K consecutive islands fail to beat the
  current best-found candidate") most literally. Risk: once one combo (e.g. window=5,
  which real v6.5.1 data already shows can post CAGR 3-4x higher than other windows for
  some tickers — see the HIBL investigation's own numbers) establishes a strong global
  best, EVERY other combo's islands look like non-improvements even if they're genuinely
  strong, DISTINCT regions worth keeping — the exact failure shape tonight's diversity fix
  was built to prevent, just moved from the selection stage into the search stage instead.
- **Per-combo best**: keeps exploring a combo as long as it keeps finding genuinely new
  per-combo bests, independent of how strong other combos are — matches tonight's
  selection-stage philosophy (every combo deserves its own real chance, not just whichever
  one dominates globally) more closely, but changes "K consecutive" from a single counter
  into 16 independent counters, which could cost significantly more (a weak combo could
  still burn K islands failing to beat its OWN modest best, even after the strong combos
  are long since exhausted).

Recommend per-combo (consistent with tonight's fix's own reasoning), but this is
explicitly the user's call, not mine to default silently.

### 4. Compute cost — real numbers, not a guess

Today's Phase2 cost (measured, this session, real v6.5.1 launch, AGQ/TrailingBoth/
fixed_sl=1, full 16-combo grid): 268,180 rows across 3 generations, 213.8s. That's the
number to beat/bound against.

The continuous design's cost is fundamentally **data-dependent**, not a fixed formula —
that's the whole point (adaptive depth). Two bounds worth stating explicitly:

- **Worst case (K never reached, e.g. a landscape with many genuinely-improving weak
  islands)**: unbounded without an explicit hard ceiling — could walk the ENTIRE
  Phase1-coarse-ranked list before ever hitting K consecutive failures, dramatically more
  expensive than today's fixed grid. **A hard ceiling on total islands walked per (ticker,
  strategy, fixed_sl) scope is a required safety valve**, not optional — open question:
  what number, or should it be wall-clock-based instead of island-count-based (matching
  the existing `workers_budget`/`paused` throttle pattern in `campaign_registry.py` rather
  than a fixed island cap)?
- **Best case (a genuinely sparse ticker/scope)**: could be CHEAPER than today — if the
  first 1-2 islands per combo already represent everything real, K consecutive failures
  fires fast and the remaining coarse-grid regions never get walked at all, unlike today's
  fixed N_ISLANDS=3 × N_GENERATIONS=3 which pays the same cost regardless.

No real number can be given for "average" cost without either (a) a hard ceiling decided
first, or (b) running this against real data once built — this section should not be read
as a firm estimate, only as the two bounds that exist without one.

## What this does NOT change

- Tonight's selection-stage fix (`find_missing_window_z_top_n` diversity,
  `top_up_window_z_island_quota`, `_check_live_node_regression`) stays exactly as-is —
  whatever islands this new search process finds still need to survive final selection,
  and that mechanism is already correct as of `686a098`.
- Phase1-coarse itself is unchanged — this only touches Phase2's mesh-generation loop and
  (depending on the stopping-rule answer above) potentially how many generations a given
  island gets before its own convergence check fires.
- `campaign_registry.py`'s job-queue/version-string model is unaffected — this changes
  what happens INSIDE one `run_one_fixed_sl` call, not how campaigns/jobs are tracked.

## Open questions for user review (not decided here)

1. Per-island generation cap — hard ceiling, or uncapped with only the K-consecutive rule
   as the stop? (Section 2)
2. Running-best definition for the K-consecutive rule — global or per-combo? (Section 3)
3. K's actual value — no proposal here; likely needs empirical tuning against a few real
   tickers once built, not a number picked in the abstract.
4. Total-cost safety valve — hard island-count ceiling, wall-clock budget (matching
   `campaign_registry.py`'s existing throttle idioms), or both? (Section 4)
5. Does this replace `N_GENERATIONS`/`N_ISLANDS` as module constants entirely, or become an
   opt-in mode (e.g. a CLI flag) alongside the existing fixed-N behavior, at least for an
   initial validation period against a few known-rich/known-sparse tickers before becoming
   the default?
