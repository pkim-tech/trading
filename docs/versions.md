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
