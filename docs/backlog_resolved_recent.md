# Backlog — Recently Resolved

## [backtest][tooling] Resolved 2026-09-13 — Phase4/Phase5 consolidation never ported trade-level persistence; write step added to `gt_full_review_rows`, delete-non-winners cleanup script built (not yet run), 14 promoted + 11 superseded v6.5.2 candidates backfilled into `phase5_trades`/`phase5_drought_windows`. Full detail: `deep_backlog.md`.

## [live-trading] Resolved 2026-09-13 — AGQ id=526's $328.30 "sizing gap" was correct compounding (an addon leg's real -$284.87 loss), not a bug; fixed `trace_node_capital_chain.py` to fold in addon_legs so it stops false-flagging this. Full detail: `deep_backlog.md`.

## [backtest][tooling] Resolved 2026-09-13 — `candidate_full_review_two_tab.py --workers` now routed through `resolve_effective_gt_workers` (real SOXL OOM crash confirmed via dmesg); also fixed a second, more serious bug found along the way — tonight's `tickdata.db` split had silently broken this cap's default connection, meaning `run_gt_mode`'s/`bench_phase1_phase2_inmemory.py`'s existing SOXL protection was non-functional since the split landed. Full detail: `deep_backlog.md`.

## [backtest][tooling] Resolved 2026-09-13 — persist per-phase sweep timing via new `phase_timing.py` (modeled on `script_usage.py`), wired into Phase1/2/2.5/4/9/10 + `scripts/phase_timing_report.py` reader; gated-file diff paired-reviewed, no HIGH/CRITICAL. Full detail: `deep_backlog.md`.

## [backtest][tooling][HIGH] Resolved 2026-09-13 — `ATTACH_CAMPAIGN_ID` version-mismatch guard wired into `resolve_campaign()`: closes both 2026-09-12 real incidents (CAMPAIGN_LABEL and WINDOW_START/END omission), verified against real campaign_18/22 data. Full detail: `deep_backlog.md`.

## [tooling][data] Resolved 2026-09-13 — split `massive_*_derived` tick-data tables (+`massive_hourly_corrections`/`active_builds`) out of `trading_universe.db` into new `tickdata.db`: 42.3GB→14.6GB, verified row-count+checksum match, all consumers repointed (wider raw-SQL surface than originally scoped), `signals_invariants.py` change paired-reviewed (no HIGH/CRITICAL). Full detail: `deep_backlog.md`.

## [live-trading][coverage] Resolved 2026-09-12 (found stale during backlog review) — CURE/TMF/ERX `SCHWAB_AUTOMATION_TICKERS` gap: fix was already applied 2026-08-16 and ERX itself already entered/exited/archived 2026-08-27; backlog entry never closed out. New small follow-up filed separately: TMF has an unrelated unactioned `pending_buys` row from 2026-09-09. Full detail: `deep_backlog.md`.

## [backtest][tooling] Resolved-as-already-done 2026-09-12 (found stale during backlog review) — persist full-review-report data to a queryable DB table: already built as `candidate_full_review_snapshots` (202,908 rows, `node_id`/`source_file`/`sheet_name`/`captured_at`/`row_json`), wired via `_persist_snapshot()` inside Phase 9 (`candidate_full_review_two_tab.py:1289`) since the 2026-09-10 Phase 9 build; backlog entry just never got closed out. Full detail: `deep_backlog.md`.

## [backtest][tooling][HIGH] Resolved 2026-09-12 (found stale during backlog review) — pre-v6.5.1-rerun runlist: all 4 steps (params_json conversion, checkpoint provenance fix, resume-from-top100 save-guard fix, N_ISLANDS pooling fix) were done and v6.5.1 launched back on 2026-09-03; entry just never got closed out of `backlog_cache.md`. Full detail: `deep_backlog.md`.

## [live-trading] Resolved-as-decided-wont-fix 2026-09-11 — automated-buy retry-after-outage mechanism (trading_incidents #10/#11) closed per user's call: a missed entry is already covered by existing Monte Carlo missed-trade failure-mode simulation; not worth the duplicate-order-placement risk of blind retry against an ambiguous (fill-or-no-fill) outage-timeout outcome. Full detail: `deep_backlog.md`.

## [live-trading][coverage] Resolved-as-already-done 2026-09-11 — `fake_broker.force_network_error_next_order()` backlog entry was stale, work already shipped `5aeeea5` (2026-09-01). Full detail: `deep_backlog.md`.

## [backtest][tooling] Investigated-didn't-pan-out 2026-09-11 — concurrent multi-group dispatch for Phase4's addon-cliff-safety check: predicted ~4x, real measurement showed it's SLOWER (8.3s→10.3s) due to per-executor overhead multiplying with pool count; not committed, existing 1.75-1.8x fix stands. Full detail: `deep_backlog.md`.

## [backtest][tooling] Resolved 2026-09-11 — Phase4's addon-cliff-safety check parallelized (`5b74587`), restoring parallelism lost when Phase5 got folded in 2026-09-08; ~1.75-1.8x speedup (25.4s→14.5s, 27 real candidates), zero mismatches. Paired review found+fixed 3 real bugs (coordinate-key mismatch, missed 2nd nested-pool site, wrong-consumer force-serialization) + reverted a wrong-kernel numba warmup regression. Full detail: `deep_backlog.md`.

## [backtest][tooling] Resolved 2026-09-11 — Phase2.5-cliffbox dispatch grouped by (window,z) to stop per-process second-resolution mprep cache thrashing: ~78x throughput (151 cells/sec vs. live job's own 1.94 cells/sec baseline), ported from verify_v651_cliffsafety_1s_timing.py's existing fix (never applied to the real production path before). Paired review: no HIGH/MEDIUM findings. Full detail: `deep_backlog.md`.

## [backtest][tooling] Resolved 2026-09-11 — live fail_counts visibility added to Phase2.5 dispatch progress bar (`510883c`); paired review found the first-draft fix wouldn't have caught a fast-failing stage (mininterval too coarse), fixed to render on each status's first occurrence + a loud proportional failure-rate line. Full detail: `deep_backlog.md`.

## [backtest][live-trading][HIGH] Resolved 2026-09-11 — overlay-inclusive Check11/Check13 risk checks added (5 equity curves: addon-only, core+addon, drought-only, core+drought, core+addon+drought); paired review found+fixed 3 HIGH (fold-span annualization, silent empty-fold voting, missing return_below_floor exclusion). 20 new phase4_results columns. Full detail: `deep_backlog.md`.

## [backtest][tooling] Resolved 2026-09-11 — second-resolution/main-pool worker memory duplication fixed (preload-before-fork, matching phase5_second_level_overlay_check.py's own pattern): second-res workers ~2.85GB→~670MB RSS each, confirmed output-invariant (bit-identical trades/CAGR pre/post), re-validated at --workers 4 and 8 under real combined load with a live campaign job; SECOND_RESOLUTION_MAX_CONCURRENT independent cap removed (now matches --workers). Full detail: `deep_backlog.md`.

## [backtest][tooling][HIGH] Resolved 2026-09-10 — checkpoint identity replaced with content hash + manifest, sweep_run_log provenance added (`05f63b0`); paired review found+fixed 1 HIGH (`--checkpoint-file` + multi-fixed_sl collision) + 3 MEDIUM + 2 LOW. Full detail: `deep_backlog.md`.

## [backtest] Resolved-as-not-needed 2026-09-08 — island-pooling crowd-out fix + live-node force-seeding both dropped: sweep's actual picks already beat/match live on every ticker checked (HIBL/GDXU/KORU/NUGT/OILU). "Get better, not protect the incumbent." Full detail: `deep_backlog.md`.

## [backtest] Resolved 2026-09-06 — Phase2.5/Phase4 1s-fill-resolution kernel wiring committed: reverified on corrected data, 4 held findings + 2 round-2 paired-review findings (selection-bias, insufficient memory mitigation) fixed, real memory near-miss caught+fixed under a full end-to-end run. Full detail: `deep_backlog.md`.

## [backtest] Resolved 2026-09-06 — `massive_second_derived` double-dividend-adjustment bug root-caused+fixed; all 22 tickers rebuilt+verified clean (0.0000% ratio vs minute leg, was up to ~21% wrong for dividend-paying tickers). Kernel wiring that surfaced this stays paused separately. Full detail: `deep_backlog.md`.

## [live-trading] Resolved 2026-09-06 — add-on leg P&L now compounds into next-trade sizing (`_last_sale_recovery`); paired review caught+fixed a real proceeds-dropping bug in the first build. Full detail: `deep_backlog.md`.

