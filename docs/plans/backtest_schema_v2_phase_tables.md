# Backtest schema v2: phase-staged tables + JSON node identity

Design conversation, 2026-08-25 (planner session). Supersedes the stale "Status, 2026-08-22:
schema rework..." note in `docs/plans/ground_truth_kernel_rebuild.md`'s Follow-on section (that
dispatch never landed — confirmed via `git log --all` across every branch, see
`docs/backlog_cache.md`'s 2026-08-23 entry). Not built. Not paired-reviewed. Discussion only,
captured here so it isn't lost.

## Problem this solves

Two previously-separate problems, both root-caused to the same thing: `backtest_cache`'s flat,
per-strategy-overloaded columns (`take_profit`/`trail_buy_pct`/etc. meaning different things
depending on strategy — 4 confirmed real bugs from this family, see `strategy_architecture.md`
and `docs/backlog_cache.md`'s 2026-08-07/2026-08-22 entries).

1. **Bloat/prune treadmill**: Phase1-Coarse alone produces the vast majority of `backtest_cache`'s
   60M+ raw GT rows, of which only ~0.36% (218,271) are ever real candidates. Today this gets
   fixed after the fact via `prune_backtest_cache_ground_truth.py` — a real, recurring, riskier-
   than-necessary operation (validation gate, WSL disk-space gotchas, swap risk) that has to be
   re-run every time bloat re-accumulates.
2. **Report-layer re-simulation**: `candidate_full_review.py` re-simulates every candidate's full
   trade list from scratch (`build_candidate_report_ground_truth`) because nothing ever persists
   the real per-candidate trade sequence computed during the sweep. This is the `[specced]`
   backlog item ("fold top-X trade-sequence retention into the sweep itself") — Part 2 of tonight's
   discussion, originally scoped standalone, now folded in here since it turned out to need the
   same identity/schema questions as Part 1.

## Node identity: `node_key`

```
node_key = sha256(strategy_name + canonical_sorted_json(params_dict))
```

`params_dict` is built **per-strategy**, using however many real axis columns that strategy
actually declares — no fixed arity anywhere. Confirmed via `strategies.py` that real arity
already varies today, not just hypothetically:

- `ZScoreBreakout`: 1 swept axis (`stop_loss`, and it's the real SL — not fixed)
- `TrailingExitZScoreBreakout` / `TrailingBuyZScoreBreakout`: 1 swept axis each, but mapped to a
  *different* real column (`trail_pct` vs `trail_buy_pct`), and SL is fixed (`uses_fixed_sl=True`)
- `TrailingBothZScoreBreakout(TrailingBuyZScoreBreakout)`: **2** swept axes — inherits
  `trail_buy_pct`, adds `fourth_axis='trail_pct'`. Higher arity than its own parent class.

A strategy that doesn't use a given axis contributes **no key at all** for it (not a 0/default
value) — this is what prevents an irrelevant always-constant column from polluting or colliding
with another strategy's hash. `resolve_axis_columns()` (`strategies.py:31`) is today's 2-slot
version of this lookup; `node_key` generalizes it to an arbitrary-length declared list
(`axis_columns` on each `BaseStrategy` subclass, or the fuller `PARAMS` contract from
`strategy_architecture.md` if that gets built first — `node_key` only needs the column-name list,
not the full type/min/max/UI metadata).

## Phase-table architecture

**Minimal-diff version, settled 2026-08-25**: only Phase1-Coarse's write target changes.
Phase2-Island, Phase2.5-CliffBox, Phase3, Phase4 already write into `backtest_cache` today and
keep doing exactly that — no new tables for them. Phase1 alone gets redirected to a new
ephemeral scratch table instead, since it's the actual source of the bloat (60M+ raw rows,
0.36% ever kept) — nothing else in the pipeline has that problem.

```
Phase1-Coarse  → backtest_phase1 (NEW, ephemeral/droppable per campaign — NOT a permanent
                  growing table; this is what kills the prune treadmill, not a relocation of
                  it). **Drop timing settled 2026-08-26/27**: immediately after Phase2-Island
                  finishes its read for that scope, not "once candidate_nodes is written" (the
                  earlier draft's leaning) -- backtest_phase1's only real consumer anywhere in
                  this design is Phase2-Island's own read step; nothing later (Phase2.5, the
                  trade-regeneration pass, candidate_nodes) ever reads it again, so there's no
                  reason to keep the full table alive past that point.
                  **Insurance kept before the drop**: copy the top 100 rows (by whatever ranking
                  metric, aggregate columns only -- no trade-level detail needed at this stage)
                  into `backtest_cache` itself, tagged `phase='Phase1-Coarse-GT'`, same permanent
                  table Phase2/2.5/3/4 already write into -- not a new table. Gives real
                  optionality to re-debug Phase2-Island's center-detection logic later without
                  re-running the full expensive Phase1-Coarse sweep, at negligible storage cost
                  (100 rows/scope is nothing next to the 60M+-row bloat problem this whole
                  redesign exists to fix).
                  Nothing gets copied from Phase1 into backtest_cache directly for the actual
                  island-detection input -- checked pick_island_centers() (run_optimization_
                  sweep.py:1880): it's a greedy multi-center picker that walks the FULL ranked
                  grid, skipping any coordinate within min_sep of an already-picked center. A
                  pre-filtered "winners" subset would break this (could pick 3 clustered points,
                  never seeing the excluded rows needed to know they're near-duplicates). Real
                  fix: Phase2-Island reads directly from backtest_phase1 (the full
                  per-(z,window,tpct) grid slice, before it's dropped) instead of backtest_cache,
                  same query shape as today just pointed at the new table. Phase2's own
                  fine-mesh sweep around the detected centers naturally re-covers the peak cell
                  when IT writes to backtest_cache -- nothing needs a separate promotion step.
                  Trade capture (below) is NOT seeded here -- Phase1-Coarse itself never
                  needs trade-level detail, only aggregate CAGR to find island centers.

Phase2-Island, Phase2.5-CliffBox, Phase3, Phase4 → unchanged from today except Phase2's read
  source (backtest_phase1 instead of backtest_cache for the island-center step, above) — all
  four still WRITE into backtest_cache directly, phase-tagged (`phase` column) exactly as now.
  Phase2.5's dense cliff-box (run_phase25_cliff_box()/_ground_truth(),
  run_optimization_sweep.py:2258) already writes real neighborhood data this way — nothing to
  build there.

backtest_winner_trades → **settled 2026-08-26/27: regenerate, don't inline-capture.** Original
  plan (struck) was a live min-heap cycled throughout Phase2/2.5, comparing each newly-computed
  cell's trades against current worst-of-top-3 -- rejected as too much write volume against too
  much of the grid (Phase2/2.5 still touch a very large cell count even after Phase1-Coarse is
  scoped down; capturing full trade sequences for all of them, then evicting most, wastes real
  writes for zero benefit over just re-deriving the few that actually survive). Settled instead:
  once Phase2.5-CliffBox finishes and each island's real top-3 is already decided (from
  backtest_cache, same as today), do ONE re-simulation pass -- just for those final top-3-per-
  island cells, not the full Phase2/2.5 grid -- and persist that result into
  backtest_winner_trades, keyed by node_key. This is a smaller, better-understood extension of
  the exact re-simulation `candidate_full_review.py` already does today (build_candidate_report_
  ground_truth) -- just moved to run ONCE right after Phase2.5, cached, instead of every single
  time a report gets generated. Solves the original `[specced]` backlog item's actual complaint
  (report-layer re-simulation is the real bottleneck) without touching Phase1/2/2.5's own
  already-tuned sweep loops at all -- zero risk to sweep throughput, all the risk is contained
  in a new, isolated, small re-simulation step. Phase4 gets its own sibling, backtest_overlay_
  trades, same regenerate-once approach, partitioned into separate results per overlay kind
  (none/drought/add-on/drought+add-on) — a drought winner's trades must never be conflated with
  a no-overlay winner's, since they're not the same underlying simulation.

candidate_nodes → promotion step, unchanged from today except: node_key already exists (minted
  at Phase1 generation time) — candidate_nodes references it, no re-keying at the promotion
  boundary. Trade data: add node_key as a column on backtest_winner_trades rather than copying
  rows (matches the "don't delete/duplicate data" convention already in use elsewhere).
```

Net result: `backtest_cache` stays the one permanent results table (same role it has today,
including holding all existing legacy v5/v6 rows untouched), it just never receives Phase1's
raw coarse-grid bulk again — that's the entire structural change.

## Scope decision: v5/v6 stay untouched

Backporting the ~60M+ existing `ground_truth_v6` rows (or v5's older data) into this scheme was
considered and rejected:

- v5 is fully sunset from live trading (all 14 real tickers promoted to v6, 2026-08-24/25).
- `strategy_architecture.md`'s own stated philosophy: "no urgency to migrate before [a new
  strategy is added]" — v5/v6 don't need the new shape, they already work.
- Backporting would force a real decision on the ~31 consumer scripts (dual-read logic, or a
  rewrite) — exactly the blast-radius problem that stalled the original 2026-08-22 dispatch,
  reimported for zero live benefit.
- Matches the standing "isolate new code from settled paths" convention — v5/v6 are settled,
  don't reach backward to unify them with new work for consistency's own sake.

**This means tonight's SOXL GT prune (in progress as of this doc) is completely unaffected by
this design** — it's about the existing flat-schema table; this design applies only to whatever's
built going forward (starting with the phase-table redesign itself, and any strategy after v6).

## Test plan

Draft, 2026-08-25. Not built. Ordered roughly build-order; each covers a different real risk
this design introduces. Kernel-adjacent items (marked) need the paired independent-cold +
contextual Opus review gate per CLAUDE.md before landing, same as any `run_optimization_sweep.py`
change.

**1. `node_key`/`params_dict` generalization** (pure function, no DB, cheapest to test first)
- Real-data regression: `TrailingExitZScoreBreakout` vs `TrailingBothZScoreBreakout`, real
  `backtest_cache` rows — round-trip fidelity (encode→decode reproduces original flat values),
  no collisions across the two strategies despite overlapping column names/values. Proves arity
  1-vs-2 (already real in production data) doesn't regress.
- Synthetic fixture strategy (`_TestManyAxisStrategy`, 20 fabricated params, never a real
  sweep): no hardcoded 2-slot assumption anywhere (the exact bug briefly introduced mid-design
  — a `sl_value`/`fourth_value` sketch that would've broken past arity 2), canonical-ordering
  independence, cross-arity non-collision. Needed because (1) alone can't test arity beyond 2 —
  no real 3+-axis strategy exists yet.

**2. `backtest_phase1` lifecycle** (ephemeral scratch table)
- A dropped/missing `backtest_phase1` for a ticker doesn't corrupt anything downstream — Phase2
  should just regenerate from scratch (see the "no coordination logic" decision above), not
  error or silently skip.
- **Multi-ticker concurrent-campaign collision: settled 2026-08-26/27 as a non-issue, no test
  needed.** Originally flagged as a real risk to guard against; walked back after checking real
  history didn't actually support it (the cited precedent turned out to be 8 workers on ONE
  ticker's own grid, not multiple tickers colliding) and confirming the actual operational
  pattern -- this system doesn't run tickers concurrently in practice, since a single ticker's
  own grid (164,640 cells for SOXL) already saturates the whole worker pool; there's no
  operational reason two tickers would ever sweep at the same time. Single shared
  `backtest_phase1` table is fine as originally designed -- no run-specific-table scheme, no
  scoped-delete mechanism, no extra design needed for a scenario rare enough not to be worth
  insuring against. This is a "for now" call, not a permanent constraint -- if the operational
  pattern ever changes (ticker concurrency becomes desired/needed), revisit rather than assume
  this reasoning still holds.

**3. `pick_island_centers` read-source migration** [kernel-adjacent]
- Simplified 2026-08-25: no synthetic byte-identical harness — just run the new pipeline for
  real on a ticker already swept under today's v6 pipeline, and diff the resulting candidates
  against v6's real existing output for that same ticker/scope. Real data, real comparison,
  same shape as #6 below — this is a storage-location change, not a logic change, so a real
  delta here is the actual signal to investigate, not a hypothetical one to construct.

**4. `backtest_winner_trades` regeneration pass** [kernel-adjacent] -- revised 2026-08-26/27
  for the settled regenerate-once design (min-heap eviction rejected, see above)
- Re-simulating a Phase2.5 top-3-per-island cell must reproduce the EXACT same trade sequence
  `backtest_cache`'s own row for that cell already implies (same params, same kernel version,
  same window) — real regression check: diff the regenerated trades against an independent
  from-scratch replay (`sim_minute_groundtruth_independent.py`-style) for a few real cells,
  same rigor already applied to the kernel itself.
- Tie-handling for "which cells are the real top-3" still reuses the deterministic-tiebreak
  lesson already learned the hard way this month (`GT_CANDIDATE_TIEBREAK`, `pick_island_
  centers`' 2026-08-23 fix, `prune_backtest_cache.py`'s `TIEBREAK_SQL`) — that selection logic
  is unchanged by this redesign, still needs to be correct before regeneration even starts.
- A dropped/incomplete Phase2.5 scope (regeneration never runs) must not leave
  `backtest_winner_trades` silently stale for that scope — report-layer consumers need a clear
  signal ("no trades captured for this node_key yet") rather than serving an old vintage.

**5. `backtest_overlay_trades` partitioning** [kernel-adjacent]
- Confirm the 4 overlay-kind results (none/drought/add-on/drought+add-on) are never conflated —
  a drought winner's regenerated trades must never be attributed to a no-overlay winner's slot
  or vice versa, since they're different simulations, not competing entries in one heap anymore.
  Direct regression test for the exact failure mode already named as the reason for
  partitioning them.

**6. End-to-end smoke test against real production shape**
- Per this project's existing schema-migration convention (`docs/deep_backlog.md`'s 2026-08-15
  note): run a real campaign end-to-end (Phase1→2→2.5→backtest_cache→candidate_nodes) against a
  copy of real production data, not synthetic — confirm the final `backtest_cache`/
  `candidate_nodes` output matches what today's un-migrated pipeline produces for the same
  ticker/scope, before trusting it on anything live-adjacent.

**7. Full-mesh periodic spot-check itself** (once built — separate from validating it)
- Needs its own pinned regression test: inject a known synthetic mismatch (a fabricated cell
  that should win but was excluded from Phase1/2/2.5's retained set) and confirm the spot-check
  actually flags it — a comparison tool that never fires on a real injected failure is worse
  than no tool at all.

## Requirement: one definition of "winner", not one per script

Raised 2026-08-25 (evening), after `prune_backtest_cache_ground_truth_validate.py` found a real
PRE-vs-POST candidate-selection discrepancy for AGQ (2 scopes) with no data drift and no
corruption -- root-caused to a near-tied cluster of adjacent grid cells (`axis_tp` 30/31/32,
CAGRs within ~1 point of each other) where the top-N-per-island tie-break isn't guaranteed
stable across two runs of the same selection code against identical data. Confirmed harmless
this time (the affected scope was `TrailingBothZScoreBreakout`, never the strategy actually
picked for AGQ's real live node), but it's the third distinct "definition of winner" bug found
this project's history (robust_alpha-vs-CAGR ranking confusion, `top_safe_nodes.py`'s hardcoded
`metric="robust_alpha"` with no GT-aware override, and now this tie-break instability) --
pattern is "pick the best row" logic reimplemented slightly differently in `run_optimization_
sweep.py`, `candidate_full_review.py`, `candidate_summary_report.py`, `prune_backtest_cache_
ground_truth.py`, and `top_safe_nodes.py`, each a chance to drift.

**Decided**: rather than auditing/fixing the current (soon-to-be-superseded) scripts one at a
time, this schema-v2 rebuild should define "winner" (ranking metric, full deterministic
tie-break column list, island/neighborhood grouping) exactly once, in one place, and have every
consumer (report, prune, sweep) call that single definition -- not re-derive it. Scope/design
of that single definition not yet started; flagged here so it's a stated requirement of the
rebuild, not an afterthought once the phase tables exist.

## Open, deferred out of this design

- **KORU "upswing-watch" as a real swept strategy** (own `sma_short_days`/`sma_long_days`/
  `vol_pctile_threshold` params, no z-score/trail axes at all) — raised as a candidate real
  (non-synthetic) stress test of a *third* distinct shape, and as the strategy that might
  naturally become "v7." Explicitly NOT picked up as part of this design — real backtest kernel
  work (new entry/exit signal class, sweep grid support, its own paired-review gate), decoupled
  from the schema question. The existing KORU backlog item (`backlog_cache.md`, revisit-when:
  user picks it back up) stays scoped as the original lightweight Slack-alert idea; if a swept-
  strategy version of it is ever wanted, that's a new, separate, bigger backlog item.
- Whether `candidate_nodes` keeps its own autoincrement `id` alongside `node_key`, or `node_key`
  becomes the PK directly — leaning toward `node_key` as PK (one identity, not two), not decided.
  **Settled 2026-08-26/27 for `backtest_winner_trades` specifically** (doesn't resolve the
  broader `candidate_nodes` question above, just this one table): a nullable `node_id` column,
  bridging old int-id consumers to the new node_key-first identity. Every regenerated Phase2.5
  top-3-per-island row gets a `node_key` always; `node_id` stays NULL for the (large majority)
  that never get promoted, and only gets backfilled at the moment a `node_key` actually gets
  promoted into `candidate_nodes` (which mints the real integer id). Lets old code still
  expecting an integer `node_id` join to trade data for a promoted candidate, without forcing
  every unpromoted top-3 row to carry one it doesn't need.
- Exact `axis_columns` declaration mechanism (minimal list-per-class vs. building the fuller
  `PARAMS` contract from `strategy_architecture.md` at the same time) — not decided; either
  works for `node_key`, the fuller contract just does more (sweep-grid auto-build, UI rendering)
  that isn't needed to unblock this design specifically.

## Periodic validation: full-mesh spot-check (raised 2026-08-25, settled shape)

Real question this answers: **does the generational walk (Phase1-Coarse → Phase2-Island →
Phase2.5-CliffBox) need to be adjusted to find a better base CAGR** — i.e., is the greedy/
generational search structurally capable of missing a better node than a full brute-force grid
would find?

**No fresh re-run needed** — Phase1/Phase2/Phase2.5 cells are already labeled from the real
sweep (`backtest_cache.phase`), so that's already the "what did the generational walk find"
data point. The only new work is the completion half: periodically (cadence TBD, doesn't need
to be frequent — the search algorithm itself doesn't change often) run the existing full-mesh
pass (today's `"Phase3-Full"` in `run_optimization_sweep.py` — already cache-skips whatever
Phase1/2/2.5 computed and fills in the rest of the grid) against a sampled ticker, then diff its
**top-3 set** (not just the single best value the existing log already compares) against what
Phase2.5 actually retained. Persist the result over time so drift is visible as a pattern, not
just a one-off. No renaming needed for this to work — naming (`Phase3-Full` vs. anything else)
is a separate, deferred decision, not a prerequisite.

Pick any ticker for the sampled check, independent of real campaign scheduling — don't build
coordination logic to run it before that ticker's `backtest_phase1` gets dropped. If the cache
is still there, the full-mesh pass reuses it for free; if `backtest_phase1` was already dropped
(it's ephemeral by design — see above), the pass just regenerates those cells. At a
weekly-or-less cadence the extra cost either way is a few minutes, not worth engineering
around.

Distinct from this doc's own testing plan above (that validates `node_key`/schema correctness;
this validates the generational search *methodology* against real per-ticker campaigns) — both
are periodic-confidence-check ideas but answer different questions, don't conflate them.

**First concrete experiment for this, raised 2026-08-25**: current defaults are
`N_ISLANDS=3` (Phase2's island search), and Phase2.5-CliffBox refines only the single best
cell overall (`ORDER BY robust_alpha DESC LIMIT 1`) — not even today's top-3. Proposed: widen
Phase2 to search top-10 islands, narrow back to top-3 into Phase2.5's cliff-box refinement.
Real compute cost either way (~3.3x Phase2's island-refinement cost for 10 vs 3), and it's
kernel-adjacent (`run_optimization_sweep.py`, review-gate applies once built) — **don't change
the constants directly; use the full-mesh spot-check once it exists to test whether 10→3 finds
anything 3→1 actually misses, and let that evidence decide.** Deferred, not urgent — "we can do
that later."

## Related finding: raw-data-layer "latest" resolution has the same failure shape (2026-08-26)

Real incident tonight, one layer below this doc's scope (`massive_hourly_derived_builds`/
`massive_minute_derived`, not `backtest_cache` itself): `get_massive_hourly_ohlcv`/
`get_massive_minute_ohlcv`'s no-`build_id` path resolves "latest" via `ORDER BY id DESC LIMIT 1`
— pure insertion order, no completeness/freshness check. A no-args `fetch_massive_minute_data.py`
refresh silently narrowed SOXL/DPST/DFEN's canonical minute CSVs from 5yr to 2yr, and a
subsequent `build_massive_hourly_derived.py` run created a newer, narrower build from that
truncated CSV — which then silently became "latest" over the prior, correct, full-5yr build.
Same failure shape as the overloaded-column bug family this doc exists to fix: something
silently resolves to a worse state with no check that it's actually complete/correct. Recovered
tonight via `scripts/reinject_derived_build.py` (copies a known-good build's rows into a fresh
build_id — see `docs/backlog_cache.md`'s 2026-08-26 entry for detail) and a new
never-overwrite-canonical-directly fetch/stage/promote convention (`scripts/promote_market_data_pull.py`).
Real fix specced but not built: an explicit `active_builds` promotion table (same "explicit
promote, not implicit ordering" pattern this doc's own phase-table design already uses for
`backtest_cache`'s winner-selection problem) — see the backlog entry for the full spec. Not
folded into this doc's own build-order since it's a distinct layer (raw derived market data,
not `backtest_cache`/`candidate_nodes`), but the same underlying principle applies: don't let an
implicit ordering rule stand in for an explicit correctness check.

## Build staging: independently testable pieces now, fused single-ticker pipeline later

Settled 2026-08-26/27. The eventual end state is a single-ticker wrapper: feed one ticker
through Phase1→Phase2→Phase2.5→the `backtest_winner_trades` regeneration pass→a reviewable
output file, then move to the next ticker — same simple per-ticker queue-loop shape
`scripts/run_ground_truth_sweep_queue.sh` already uses today, no new scheduling machinery
needed for that part.

**But not built that way first.** Each piece (node_key/params_dict, `backtest_phase1`
lifecycle, `pick_island_centers`'s read-source migration, the trade-regeneration pass, overlay
partitioning) gets built and validated standalone, in roughly the test-plan's numbered order,
before anything gets wired into the fused end-to-end flow. Fusing first would make it much
harder to tell which phase introduced a bug when something breaks — staying separated lets each
step's output be inspected and any error caught at the specific phase it happened in, not
discovered downstream after several phases have already run.

## Status

Discussion only. Not built, not scoped into tasks, not paired-reviewed. Next step if picked up:
scope into concrete build tasks (schema DDL, `node_key`/`axis_columns` implementation, the two
test suites above, then the phase-table sweep-loop changes to `run_optimization_sweep.py` —
kernel-adjacent, review-gate applies once real code exists).
