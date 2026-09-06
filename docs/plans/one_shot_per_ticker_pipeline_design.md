# One-shot per-ticker pipeline design (Phase1→2→2.5→4→5→10 off one loaded dataset)

Status: **design only, not built, not reviewed**. Raised 2026-09-05/06 (research session),
following directly from that session's investigation into a real resolution-mismatch bug
in Phase 10 (see `docs/backlog_cache.md`/commit `47616b1` and its paired-review trail).
Companion doc to `docs/plans/continuous_island_walk_search_design.md` (that doc addresses
search-quality/island-cutoff; this one addresses data-loading/resolution architecture) --
not competing proposals, could land independently or together.

## Motivation: the real failure chain this session traced

All of the following, found and (mostly) fixed in one session, share ONE root cause —
different pipeline phases independently load/re-derive price data at different
resolutions, with no single canonical trade list per candidate:

1. **Phase4** (`checklist_v65.py` via `bench_phase1_phase2_inmemory.py`) always computes
   check4/8/11/13 + addon/drought at MINUTE resolution (`minute_df`, confirmed at every
   `run_backtest_ground_truth` call site in `run_optimization_sweep.py`).
2. **Phase5** (`phase5_second_level_overlay_check.py`) separately re-simulates the SAME
   candidate at both 1-minute and 1-second resolution, specifically because Phase4's
   minute-only number can be meaningfully wrong (confirmed concretely: ETHU node 38604's
   minute-vs-second fill timing moved one trade's arm price ~8%, swinging that node's
   annualized CAGR ~130pp). Phase5 stores both trade lists in `phase5_trades`.
3. **Phase 10** (`candidate_full_review.py`'s `build_candidate_report_ground_truth`, the
   deep-checklist report generator) never read Phase5's stored trades at all — it
   re-simulated a THIRD time, at minute resolution again, silently reproducing the same
   drift Phase5 already resolved. Fixed 2026-09-04, commit `47616b1`: Phase 10 now reads
   `phase5_trades` first (see `candidate_verification_store.get_phase5_1s_trades`),
   falling back to resimulation only when no stored 1s trades exist. Paired review (real,
   both independent-cold + contextual Opus agents) caught a real CRITICAL window-scoping
   bug and a HIGH staleness gap in the first version of that fix — both documented in the
   commit message and `docs/backlog_cache.md`.
4. **Phase4's own dispatch** (`candidate_summary_report.py`'s `ProcessPoolExecutor`,
   `--kernel gt` path) parallelizes per-SCOPE, not per-ticker, with a plain per-process
   dict cache (`_MINUTE_DF_CACHE`/`_NODE_INPUT_CACHE_GT`, `run_optimization_sweep.py:987-
   1091`) — no cross-worker sharing. A ticker with many scopes (e.g. NUGT: 16 distinct
   strategy/entry_timing/fixed_sl combos, confirmed via real DB query) can have its price
   data loaded independently by every worker handling one of its scopes, not once
   globally. Not yet fixed.
5. **Phase2.5's cliffbox** (`worst_neighbor_cagr`) is a `min()` over cells already
   computed during Phase1/2, at their same minute-resolution — so a genuine cliff-safety
   verdict can be corrupted by the exact same per-trade resolution noise item 2 describes,
   for the NEIGHBOR cells, not just the candidate's own cell. Not yet addressed anywhere.

Every one of items 1-5 is a symptom of the same design gap: **no single, canonical,
best-available-resolution trade list computed once per candidate and reused by every
downstream consumer.**

## Real cost data gathered this session (2026-09-05/06, live benchmarks, not estimates)

Two real per-ticker benchmarks (`_load_node_inputs_ground_truth` for the minute path,
direct `load_seconds`/`prep_minute_inputs`/`run_backtest_ground_truth` calls for 1s),
run cold-vs-warm-cache caveats noted:

| Ticker | 1s rows | 1s load time | mprep (hourly bucketing) | Per-cell kernel throughput (1s, single process, cache warm) |
|---|---|---|---|---|
| GDXU (low volume) | 1.26M | 3.74s | 0.81s | 139 cells/sec |
| SOXL (high volume, longest-history/most-traded live node) | 22.2M | 105.8s | 2.84s | 58.3 cells/sec |

Key findings from this data:
- **Per-cell kernel cost does NOT scale with row-count ratio** between minute and second
  data (already established 2026-08-30, `docs/research_log.md`'s N_ISLANDS entry: 5.40s
  vs 5.71s minute-vs-second, only ~1.1x despite 3.4x more rows) — the kernel's outer loop
  iterates the HOURLY bar array (`backtester.py`'s `_simulate_trail_ground_truth`, fixed
  length regardless of sub-hourly resolution); fine-grained data is only touched via
  per-bar slices during fill-timing checks, not scanned wholesale. Reconfirmed here: SOXL
  and GDXU's per-cell costs (58-139/sec) are the same order of magnitude as minute-
  resolution's own measured per-worker throughput (~103/sec, from 823/sec @ 8 workers).
- **Both load time and per-cell cost scale with ticker trading VOLUME** (SOXL ~18x more
  1s rows than GDXU, ~28x slower load, ~2.4x slower per-cell) — a flat per-ticker cost
  assumption is wrong; SOXL-class high-turnover tickers dominate total cost, low-volume
  tickers barely register.
- The much-quoted "1.6s/cell" figure (`scripts/run_ground_truth_neighborhood.py`'s own
  docstring) is STALE — it predates a 2026-08-22 caching fix to
  `_load_node_inputs_ground_truth` that memoizes the expensive prep step per `(ticker,
  strategy, window, start_date, end_date, data_source)`, not per-candidate. Real
  post-fix throughput (`docs/conversation_summary.md:8273`): 823 cells/sec @ 8 workers,
  when many cells share that cache key (Phase1's own coarse sweep: ~2 distinct windows
  across 164,640 cells). The 1.6s/cell (~4 nodes/s) figure describes the UNCACHED,
  pre-fix cost — still real for any dispatch pattern that doesn't share the cache key
  across cells (which is exactly item 4 above, Phase4's per-scope dispatch).

**Conclusion**: 1-second resolution is NOT prohibitively expensive per se — the real
cost driver identified this session is architectural (per-scope-not-per-ticker dispatch,
no load-once-before-fork), not resolution. Fixing the dispatch pattern (matching Phase5's
own already-correct "load once in the parent process, fork workers after" convention)
is the actual lever, independent of which resolution gets used.

## Proposed design (sketch, NOT scoped/reviewed)

For a given ticker, in ONE process (or one process per ticker, matching Phase5's
existing fork-after-load pattern):

1. Load hourly + 1-second data ONCE (the ~4-106s cost, ticker-dependent, paid exactly
   once for the whole pipeline run, not once per phase and not once per scope).
2. Compute `mprep`/`prep` ONCE off that loaded 1s data.
3. Run Phase1 (coarse grid) off this same loaded data.
4. Run Phase2 (island refine) — reuse.
5. Run Phase2.5 (cliffbox neighbor check) — reuse; genuinely addresses the item-5 gap
   above (neighbor cells get the same resolution as the candidate's own cell).
6. Run Phase4 (checklist stats) on the Phase2.5 survivors — reuse; no redundant load.
7. Phase5's role shrinks or changes: if nothing upstream is minute-resolution anymore,
   its core "is minute lying to us" precision-check purpose is largely satisfied by
   construction. Open question (see below) whether it becomes an optional regression
   sanity-check (compare against a cheap minute-only shadow run) or is retired.
8. Phase 10 (report) reads the SAME in-memory trade list already computed in step 3-6 —
   no DB round-trip, no re-simulation, by construction (this is the natural end-state of
   the 47616b1 fix, taken to its logical conclusion: not "read from a table," but "never
   left memory in the first place").

## Open design questions (not resolved, need real discussion before scoping)

1. **Does Phase1 itself move to 1-second, or stay minute with only Phase2.5+ upgraded?**
   Given per-cell cost is confirmed similar order-of-magnitude, an all-1s Phase1 may be
   affordable — but Phase1's population is 400K-1.4M cells vs Phase2.5's much smaller
   survivor-neighborhood population. Needs real per-ticker full-grid cost projection
   using the volume-scaling relationship found above (SOXL-class tickers will dominate),
   not a flat estimate.
2. **What does Phase5 become?** Retired entirely (redundant once nothing is minute-only),
   demoted to an occasional regression check, or kept as-is for candidates that still
   route through the legacy `backtest_cache`-sourced path (which this one-shot design
   may not cover)?
3. **Per-process vs per-ticker parallelism model**: does one-shot-per-ticker mean literally
   one process per ticker (14 concurrent processes for a full-universe run, each with its
   own loaded dataset), or a smarter shared-memory scheme? Interacts with the existing
   `campaign_registry.get_workers_budget` throttling convention.
4. **Legacy `backtest_cache`-sourced candidates** (pre-dates the bench pipeline) have no
   `phase5_trades`/1s-trade equivalent at all — does this design apply only to
   `candidate_nodes`-sourced (bench pipeline) campaigns, leaving the legacy path
   untouched (same scoping the 47616b1 fix already uses), or does it require a parallel
   fix there too?
5. **Real memory footprint**: holding both hourly AND 1-second data resident for a
   ticker across an entire Phase1→10 run, times however many tickers run concurrently —
   not measured this session, needs a real check before assuming this is free.
6. **Rollout**: this is a `backtester.py`/`run_optimization_sweep.py`/
   `bench_phase1_phase2_inmemory.py` change — squarely the staged `backtest-change-
   rollout` skill's territory (single-node verify → biased single-ticker → wider), with
   the paired-review gate at the end, same as every other kernel change this session
   went through.

## Suggested next step (not started)

Single-ticker proof of concept on GDXU (cheap, ~4s load) end-to-end through Phase1-2.5
at 1-second resolution, verified against the existing minute-resolution result for the
same ticker/version, before committing to anything wider. Then repeat on a SOXL-class
high-volume ticker to get a real worst-case cost data point before projecting full-
universe cost.
