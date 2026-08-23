# Sweep Engine v7 — Lessons From the GT (v6) Permutation Campaign

Real issues found running the first full GT-kernel overnight permutation campaign
(2026-08-23), each costing real wall-clock or real candidate quality. Originally written
as forward-looking "what v7 should do differently" reference material — but items 1-3
below were actually FIXED same-day in v6 (not deferred), once the root cause was traced
to "why does v6 diverge from v5 here" rather than treated as a new design problem. See
`docs/deep_backlog.md`'s 2026-08-23 entry for the full incident writeup and final
resolution. This doc's value now is mostly the ROOT CAUSE framing (a parallel
`_ground_truth`-suffixed rebuild diverging from proven v5 logic with no real
justification), which v7 (or any future kernel variant) should avoid by construction:
parameterize the existing correct implementation, don't duplicate it.

## 1. Multi-generation Phase2 search must be a first-class part of the engine, not bolted on later

Legacy (`run_optimization_sweep.py` `main()`) ran Phase2-island in a real
`for gen in range(max_generations)` loop (project convention: at least 3
generations), each generation re-ranking off the CURRENT `backtest_cache` state so a
later generation could discover a real island near — but not exactly on — an earlier
generation's coarse grid. When GT generalized Phase1/Phase2/Phase2.5 (2026-08-22),
this loop was never ported. Root cause wasn't an oversight in isolation — it was a
**deliberate correctness fix elsewhere silently disabling it**: `_phase2_island_gt_tasks`
was restricted to only tp/sl values literally in Phase1's own coarse grid, to fix a
real bug in the Phase2 completeness check (reading centers off Phase2's OWN fine-mesh
rows there would false-block a genuinely-finished run). That fix was correct for the
completeness check but got reused for actual mesh generation too — so even a naive
generation loop would have been a no-op, since every generation would recompute
identical restricted centers.

**v7 design implication**: island-center detection for "is this scope done" and
island-center detection for "what should I search next" are fundamentally different
queries with different correctness requirements, and must be two separate functions
from day one, not one function with a boolean flag bolted on after the fact. The
completeness-style query needs a STATIC target (computable before the search runs,
so a rerun can check itself against a fixed baseline) — this is only well-defined for
a fixed grid, never for an iterative search. The search-style query needs to see its
own prior output. Conflating them is what silently killed multi-generation search
this time.

**Also**: a genuine iterative search's own size is NOT knowable in advance (chicken-
and-egg — expected cell count depends on where the search leads, which depends on
running it). A hard done/expected completeness GATE, the pattern GT introduced for
Phase1→Phase2 (see #2 below), cannot be extended to gate a real multi-generation
Phase2 without abandoning either the gate or the generations. v7's answer, carried
over from what actually got built this session: no completeness gate inside a
generation loop at all — resumability comes entirely from cache-hit idempotency
(a generation that finds nothing new costs one cheap fully-cached pass, not a
false-blocked exception).

## 2. Hard completeness gates are new to GT and exist for a real but narrow reason — v7 should keep the reason, generalize the mechanism

Legacy's Phase1→Phase2 transition never checked completeness at all — safe only
because legacy's `main()` runs Phase1 and Phase2 synchronously in one process call
chain, so by construction Phase1 for that exact scope had just finished moments
earlier. GT introduced independent per-ticker invocation (`run_ground_truth_phase1.py
--ticker X`, dispatched separately per ticker/combo by a queue script) plus a REAL
new failure mode legacy never had: individual grid cells can genuinely error out
(missing `massive_minute_derived` data, e.g. a concurrent backfill build racing the
sweep) without the batch dispatch aborting — `dispatch_parallel_grid_ground_truth`
logs a failed cell once and `continue`s, it never raises. So "the call returned"
and "the grid is actually complete" became two different facts for the first time,
and the hard gate (`_phase1_coarse_gt_status`, raises `RuntimeError` if
`done < expected`) exists to close that gap — not a manufactured check, a real one.

It bit hard in practice: CURE hit it TWICE (0/164,640 cells, ~43 wasted minutes per
attempt) before its data existed, and RETL hit it once (146,169/164,640) mid-build.
Both are the SAME root cause (data build racing the sweep, no dependency ordering
between them) surfacing as a hard failure instead of a silent wrong result — which is
the design working as intended, just expensive when it fires routinely rather than
rarely.

**v7 design implication**: keep the "never silently mesh around incomplete data"
guarantee, but don't let the failure mode be "burn 40+ minutes re-attempting every
cell of a ticker with zero available data, twice, before finally raising." A cheap
up-front existence check (does this ticker have ANY `massive_minute_derived` rows at
all, not "compute all 164,640 cells and count how many errored") would turn a
43-minute wasted grind into an instant, clear failure. This is deliberately NOT a
general "detect repeated failures" heuristic — user's explicit call: "we don't
optimize for everything, we just need to confirm the data IS there" — i.e. a targeted
precondition check, not a smarter retry/backoff system.

## 3. Data backfill and sweep consumption order must share one source of truth

`coder3`'s `massive_minute_derived` backfill ran alphabetically through the full
69-ticker universe while the GT tranche sweep consumed tickers in
`gt_tranches.txt`'s priority order (also mostly alphabetical within each tranche,
coincidentally) — two independent orderings, no dependency gate between the two
jobs, racing on the same DB. Caused both CURE's full-block and RETL's partial
failure. Fixed reactively mid-session by re-prioritizing the backfill to match
`gt_tranches.txt`.

**v7 design implication**: a sweep campaign and a data-backfill job that share a
target-ticker set should read that ordering from the SAME file/config, not have it
independently re-derived (alphabetically, by whichever agent happens to be doing the
backfill that day). Whichever job runs second should refuse to start on a ticker the
first job hasn't reached yet, rather than attempting-and-failing.

## 4. The Phase2.5 candidate-quality gate (`PHASE25_ISLAND_CAGR_MIN=50`) has no v5 precedent and should be re-justified on its own, not by pointing at another new feature

Legacy's Phase2.5 (`run_phase25_cliff_box`) has no gate at all — no island concept,
cliff-boxes the single global top-`robust_alpha` row unconditionally. The 50% CAGR
pre-filter is pure v6 invention, added when Phase2.5 got extended to multi-island.
It was initially justified (in this session) as "it also bounds overlay compute
cost" — true, but circular: drought/add-on overlay (`apply_addon_overlay_ground_truth`,
`simulate_drought_overlay_ground_truth`) is ITSELF new-to-v6 (dated
2026-08-22/08-23), so "protects a new feature's compute cost" says nothing about
whether the gate's actual threshold/placement is correct. Real risk it carries: a
coarse island top cell of e.g. 33% CAGR never gets cliff-boxed at all, even if a
genuinely cliff-safe 55%+ node sits one cell away — the gate evaluates the coarse
(un-refined) cell, not the refined result.

Left in place this session (user's call, real compute-cost concern), now partially
mitigated by the generation-loop fix (a later generation's own coarse-equivalent
center gets an independent shot at the gate). Not fully resolved.

**v7 design implication**: if a candidate-quality pre-filter is kept, it should be
justified by its own false-negative rate against real data (how often does a
sub-threshold coarse cell sit next to a real cliff-safe candidate that clears the
bar), not by pointing at a second unrelated feature's compute cost. Worth an actual
empirical check before v7 locks in a number.

## 5a. The generation-loop walk was invisible to actual candidate selection until this was caught

Found the same session the generation loop was built, before it shipped as "done":
both `run_phase25_cliff_box_ground_truth` (the real Phase2.5 execution) and
`derive_phase25_candidates_ground_truth` (used for both that execution AND
`candidate_summary_report.py`'s output) restrict island-CENTER detection to tp/sl
values literally in Phase1's own coarse grid — same rerun-safety rationale as
`_phase2_island_gt_status`'s completeness check (a rerun must not shift centers
based on its own prior fine-mesh output), but applied to the SELECTION layer, not
just a completeness check. Result: the generation loop can spend real compute
(gen2/gen3 alone added 92,660 and 106,740 new cells for one AGQ scope) discovering
genuinely new islands via `exclude_centers`-forced hopping, and NONE of it can ever
become a selected candidate — Phase2.5 and the report always re-derive the same
gen1-equivalent 3 islands, blind to anything gen2-5 found elsewhere.

**Confirmed NOT a legacy problem**: v5's `run_phase25_cliff_box` queries the single
global best `robust_alpha` row from the ENTIRE current `backtest_cache`, no tp/sl
restriction at all — whatever any generation found, it would see. This restriction
is new to GT's multi-island design (added 2026-08-22 for a narrow, legitimate
rerun-determinism reason that legacy's single-winner design never needed).

**v7 design implication**: a multi-stage search pipeline (explore → select → refine
→ report) needs every stage after "explore" to be built against the ACTUAL widened
search space from day one, not bolted on generation-loop-first with selection logic
verified separately later. This was caught by luck (user asking "so are we tuning
this" prompted a check of the actual selection call path) — it should have been the
FIRST thing checked once generation-loop search was proposed, not something
surfaced while building a reporting feature two steps downstream. Any future
"widen the search" change must be reviewed end-to-end through selection/reporting
in the same pass, not just validated at the mesh-generation layer.

## 5. Phase3 (full-mesh fallback) is confirmed dead, don't resurrect it

Legacy's Checkpoint-2 → Phase3 fallback (routes cliff/fragile Phase2.5 candidates to
a full brute-force mesh) has no GT equivalent, and per explicit user confirmation
this session, it never once produced a better node than Phase2 historically — not
"almost never," never. v7 should not carry this forward as a "missing gap"; it's a
settled non-feature.

## Process note

None of gaps #1/#2/#3 were found by the 2026-08-22 paired review that touched the
exact code involved (the coarse-grid restriction fix) — they surfaced via user
pushback on an unrelated question during this session, not systematic review. A
review pass optimizing for "found something to flag" over "found something that
actually matters" can introduce real regressions under the guise of a fix — see
`feedback_only_high_critical_review_findings` in agent memory, added the same day
after this was found.
