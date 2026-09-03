# Sweep/Campaign Version Labels

Sister document to `docs/research_log.md`: research_log is the idea/hypothesis/experiment
side (why we tried something, what we found); this file is the pipeline-mechanics side —
what each real sweep-pipeline generation (`campaign_registry.py`'s `label` field, e.g.
`v6.5`) actually consists of, and which real campaigns/best-nodes/candidate promotions
came out of it. research_log answers "what did we learn"; this file answers "which
pipeline generation produced the nodes we're actually running live."

Human-readable index for `campaign_registry.py`'s `label` field (the `--label` value
passed at campaign creation, embedded as the version string's prefix). Distinct from
the sweep-manager mechanics (registry/queue/throttle/pause, `docs/plans/
campaign_registry_design.md`) — that infrastructure just needs to reconcile correctly,
it doesn't need its own version identity. This file tracks what a *label* means
strategically, so a label stays meaningful across many campaign_ids/runs over time.

## v6

Full 14-ticker promotion batch, 2026-08-25 ~03:33-03:55 ET (see `docs/deep_backlog.md`'s
"full v6 promotion" entry). All 14 tickers were genuinely promoted together in the same
session -- but `watch_list.version` carries TWO different label strings for the batch,
confirmed live (2026-09-03) still the current state:

- **`v6`** (plain label): AGQ, DFEN, DPST, GDXU, HIBL, JNUG, KORU, LABU, NUGT, SOXL, UGL,
  WEBL (12 tickers) -- promoted via `promote_v6_2026_08_23_batch1.py`, a batch script
  written the prior night and finally run this session, which stamps the shorter label.
- **`v6-massive-w2021-08-23_2026-08-21`** (fully-qualified): ETHU, OILU (2 tickers) --
  promoted via a manual checklist pass that named the exact GT campaign scope instead.

Both labels represent the SAME real campaign generation and the SAME promotion event --
this is a labeling inconsistency in how each was promoted, not two different vintages or
two different levels of vetting. Documented here explicitly (2026-09-03) precisely so a
future read of two different version strings across this one batch reads as "known,
intentional-by-omission naming split," not as a signal that ETHU/OILU are less-vetted or
were promoted separately -- this exact false read already cost real investigation time
once (see the backlog entry this note resolves). `watch_list.version` itself is left
as-is (not backfilled to a single label) -- that's a separate, not-yet-decided question
(touches live rows) from documenting the split.

## v6.5

Multi-axis top-N candidate selection (window/z-threshold/arm_pct backfill, generalized
2026-08-29/30) + fixed_sl swept 1-8 + N-generation Phase2 island search (`N_GENERATIONS`,
built 2026-08-31, `PROMOTION_ALGO_VERSION=4`). This is the mechanics generation currently
running live campaigns under — real campaign history, kept here as it accumulates:

- **campaign_id=1** (created 2026-08-31): `pv4`, `massive`, window `2021-08-23..2026-08-21`,
  `z=[0.5,1.0,1.5,2.0]`, `n_islands=3`. Covers the `brokerage` full rerun (AGQ/ETHU/OILU/
  GDXU/UGL/WEBL, picking up multi-gen after the earlier single-gen pv3/pv4 split-brain
  incident) + the `ira`/`roth` "other accounts" batch (DFEN/DPST/HIBL/JNUG/KORU/LABU/
  NUGT/SOXL). Real queue, not a fixed batch — jobs get appended live via `campaign_registry.py
  enqueue`, doesn't need to be relaunched to add tickers.
