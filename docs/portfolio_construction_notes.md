# Portfolio Construction — Walkthrough Notes

Source: `output/candidate_full_review_20260809_192051.xlsx` (rebuilding lost mobaxterm work).

## Steps
1. CSV → Excel.
2. Freeze panes after col A → moving to after col I (Keys section, see below).
3. AutoFilter on header row.
4. `liquidity_dollars_per_day`: `#,##0`.
5. All `*_pct` cols: `#,##0"%"` (values already raw percent, not fractions).
6. Keys (A-I), reusing existing `pick`/`comment` cols:
   - A `node_id`, B `ticker`, C `pick`, D `comment`, E `liquidity_dollars_per_day`
   - F `account_mod` (new): core/drought/addon/both/low_vol/same_day_block
   - G `trades_per_year` (new) = trades/years
   - H `target_cagr` (new): maps to strategy_cagr_pct / core_drought_cagr_pct / core_addon_cagr_pct / core_both_cagr_pct / drought_ie_included_compounded_pct / core_sdb_cagr_pct per F
   - I `strategy_cagr_pct`
   - Also add `years`, `trades` to Keys (left of freeze) — Keys now A-K, freeze moves to L2.
   - Blocked on fresh report run (for `core_sdb_cagr_pct`) — running in background.
7. Add underliers lookup — not yet scoped.
8. Replace `k1_tranche` with two split columns (not yet built):
   - tax/legal status: K1 / ETN / Clean (renamed from k1_tranche's K1_CONFIRMED/ETN_NOT_K1/CLEAN_CONFIRMED/NOT_CHECKED)
   - structural type: Commodity / Treasury / Crypto / <20 / Clean (simplified — equity underliers just get <20 if concentrated, Clean otherwise; no separate "Leverage" tag)
   Priority when multiple structural types match: Commodity > Treasury > Crypto > <20 > Clean (default).
   ETN tag is informational only (counterparty risk noted, not a decision factor).
   Commodity/K1/Crypto tags = account-routing info, not disqualifying (all allowed).
   K1-research background agent launched for the 2 unconfirmed tickers (BTCZ, MQQQ).
   K1-research agent result: neither confirmed (stays `not checked`, no citable source found). Structural type confirmed though: BTCZ (T-Rex/Tuttle, swap-based -2x spot Bitcoin, single underlier) = Crypto. MQQQ (Tradr, swap-based 2x monthly Nasdaq-100/QQQ, ~100 constituents) = Equity/Clean.
   Leverage factor = user's own risk read, not a structural-type tag. New `leverage_factor` column (not yet built): parsed from underlier_note's leading pattern (e.g. "-2x"/"3x"/"1.5x"), values seen so far: 1x/2x/3x (+ 1.5x for UVXY-type).
9. Loosened cliff-safety criteria: `status`/`cliff_tranche` (currently binary SAFE >=0 / CLIFF <0 on `worst_neighbor_pct`) becomes 3-way: SAFE (>=0) / SEMI_SAFE (-20 <= x < 0) / CLIFF (< -20). Not yet built.
   Separate, tighter "seriously consider" filter (not the same as the label above): worst_neighbor_pct >= -10 AND target_cagr "ridiculously good" (threshold not yet quantified) — this is what actually gates real candidates, -20 is just the display/label cutoff.
10. `account_mod` restructured (supersedes item 6/F's flat 6-value list) — sort was originally by CAGR desc, later by target_cagr instead. Primary mod + stackable modifiers, not one flat categorical:
    - Core (+/- SDB)
    - Add On (+/- SDB)
    - Drought (+/- LowVol, +/- SDB)
    - Overlay = addon + drought together (+/- LowVol, +/- SDB)
    Known gap, not yet resolved: report only has single-combo CAGR columns (core_addon_cagr_pct, core_drought_cagr_pct, core_both_cagr_pct, core_sdb_cagr_pct once the fresh report lands) — no fully-stacked column exists (e.g. Drought+LowVol+SDB together). User flagged as "getting ahead of myself," not finalized.
    Practical default so far: target_cagr = MAX(Core, Add On, Drought, Overlay); LowVol/SDB are unquantified "icing" flags, not combined into the number (no way to before). Idea raised, not decided/built: compute the full stacked permutation matrix (multiplicative, per the existing overlay-stacking convention) instead — would give real numbers for every combo, could simplify node picking.
11. Account-routing gate for `brokerage` specifically: only 3 nodes picked there, admitted if K1-tagged (forced routing per the 2026-08-12 K1-restricts-to-brokerage decision) OR add-on-boosted CAGR was too strong to pass up (tax cost accepted anyway).
    Correction: not a hard cap of 3 nodes for brokerage — could be more. $20k planned per node there for now (sizing choice, not a count limit).
    $20k/node is a starting point, not a ceiling — scale up later if it works, no rush.
12. Safety check per mod, not just the base node: relies on each mod's own robustness verdict (addon_robustness_verdict for Add On, drought_robustness_verdict for Drought, both OK required for Overlay) — separate from status/cliff_tranche (SAFE/SEMI_SAFE/CLIFF), which is the base core node's own worst_neighbor_pct check.
13. Column grouping (Excel outline, collapsible sections) for addon_*/drought_* detail. Kept always-visible per section: compounded return (converted to CAGR: (1+compounded_pct/100)^(1/years)-1, using addon_compounded_pct/drought_compounded_pct + years -- new addon_cagr_pct/drought_cagr_pct columns needed) + the existing tranche column (addon_tranche/drought_tranche). Rest of each section's detail columns collapse under it. Not yet built.
14. trades_per_year < 20 -> flagged "Thin" (most likely unreliable). Ties to the trades_per_year column (item 6/G).
    (Applied in a second pass, not the initial one.)

## Overall process phases
- **Phase 1 — Revenue organization**: Keys section (items 6, 10), addon/drought CAGR conversion + grouping (item 13), tax/structural split (item 8), leverage_factor (item on 1x/2x/3x).
- **Phase 2 — Node selection / node mods (flags) / account selection**: account routing can change node selection itself, not just tag it (item 11, brokerage K1-or-big-addon gate). Resorted by target_cagr sometime after this phase (before Phase 3).
- **Phase 3 — Other filters**: thin trades (<20/yr, item 14), frail cliff-safety flips (SEMI_SAFE/-10 threshold, item 9), unreliable walk-forward (wf_verdict), bad fluke trades (core_fluke_verdict) -- CAGR > 300% overrides any of these (exception, not automatic); below 300%, plenty of other 100-200%+ nodes exist without taking the risk, so no override.
  Filters deliberately come AFTER revenue organization, not before -- filtering too early risks losing a genuinely great node before you can see it.
- **Phase 4 — Circle back**: another round of account allocation and possibly node flags on whatever survives Phase 3.

## Note on thresholds
All numeric thresholds above (300% CAGR override, -10/-20 cliff bands, 20 trades/yr thin cutoff, $20k/node) are this quarter's working heuristics, not permanently locked rules -- expect them to shift each quarter as the picture changes. Don't hardcode these as fixed constants in any tooling; keep them adjustable.
15. Column grouping confirmed keyed by header-name prefix (matches the earlier addon_*/drought_*/wf_*/sdb_*/bear_*/crash25_*/etc. proposal).
16. All column widths set narrower than their header text (deliberately truncated, not auto-fit) -- minimizes whitespace so more columns are visible across the screen at once. Not yet applied.
17. Conditional highlighting: if addon_tranche == OK, highlight the addon CAGR column; if drought_tranche == OK, highlight the drought CAGR column; if BOTH OK, highlight the overlay CAGR column.
18. target_cagr is a formula, not a static value: dynamically references whichever CAGR cell matches the chosen account_mod for that row (e.g. account_mod="Core" -> target_cagr = that row's core CAGR cell).
19. Yellow-highlighted the cell for whatever node/mod was actually picked, for future visibility of the decision.
20. `node_id` renamed to `c_id` (candidate_nodes.id) in the Keys section -- distinct from the live watch_list.id (203 for AGQ etc.), avoids confusion between the two ID spaces.
21. Fixed: green conditional-highlighting was on the wrong CAGR columns (my own new addon_cagr_pct/drought_cagr_pct instead of core_addon_cagr_pct/core_drought_cagr_pct, the ones target_cagr's formula actually uses). Corrected.
22. Fixed: target_cagr now defaults to MAX(Core,AddOn,Drought,Overlay) when account_mod is blank instead of showing empty -- manual pick still overrides when filled in.
23. trades_per_year rounded to whole number (was 1 decimal).
24. Confirmed: report correctly auto-resolves v5.1-vs-v5 per ticker. AGQ/DPST/KORU/SOXL show v5 because they genuinely have no v5.1 data (untouched by the 2026-08-11 backfill that triggered the partial v5.1 resweep) -- not a version-selection bug.
25. account_mod now auto-picked (best of Core/Add On/Drought by CAGR, Overlay excluded from auto-pick per user's call -- still a valid manual override in the dropdown/formula). target_cagr's blank-fallback MAX also drops Overlay to match.
26. New `account` column added to Keys (real live watch_list account per ticker: brokerage/ira/roth/soxl_ira), distinct from account_mod.
27. Keys order: swapped trades/years/strategy_cagr_pct position (now c_id, ticker, account, pick, comment, liquidity_dollars_per_day, account_mod, trades_per_year, target_cagr, trades, years, strategy_cagr_pct).
28. Fixed: account column was wrongly auto-pulled from the live watch_list lookup (circular -- mirrors current reality instead of being an actual Phase 2 decision). Now blank/manual with a brokerage/ira/roth/soxl_ira dropdown, same pattern as account_mod.
29. Real open discrepancy found, not resolved: AGQ's candidate_nodes row (id=106) has pick='no'/comment='' right now, but docs show it was explicitly promoted to a real live node (id=203, $231 initial) during the 2026-08-10 promotion pass and called "the one deliberate exception" in the K1 screening pass -- doesn't square with pick='no'. No recorded reason for the flip. Not chased further, flagged for a closer look if it matters for account routing.
30. strategy_cagr_pct: static light-red fill (all rows). Whichever CAGR column account_mod (G) actually picks (Core->strategy_cagr_pct, Add On->core_addon_cagr_pct, Drought->core_drought_cagr_pct) turns yellow instead, overriding red/green for that cell (stopIfTrue priority).
31. Real gap found and corrected mid-session: drought_ie_confirm_days=10 for most tickers was real recorded sweep data (run_overlay_shim.py's most-recent run per node), NOT a code fallback default as I first claimed -- user caught this. Only SOXL/AGQ had the specifically-validated confirm_days=3; most others just had a generic confirm_days=10 pass, not a per-ticker-optimized one. Built scripts/sweep_drought_confirm_days.py (real, reusable, in scripts/ not scratch) to run a genuine confirm_days 1-15 grid per ticker and pick each ticker's best (min 5 trades). Running for the 10 real live tickers. Caveat: full-data best-pick only, not the fit/test-half-split + single-trade-removal stress test docs/overlay_parameter_robustness_process.md requires before trusting it as robust.
32. Real bug found and fixed: run_overlay_shim.py's node_dict() defaulted to hardcoded version="v5", never auto-resolving to v5.1 the way candidate_full_review.py has since 2026-08-12. Confirmed live on ETHU: was silently running against a stale v5 node (robust_alpha=180.9, id=75) instead of the real current v5.1 node (robust_alpha=320.0). Fixed: moved resolve_version() from candidate_summary_report.py to locate_best_node.py (fixes a circular import too -- candidate_summary_report.py already imports FROM run_overlay_shim.py), run_overlay_shim.py's --version now defaults to None and auto-resolves per ticker. Verified: 4 tests pass, imports clean. Re-running the confirm_days sweep with the fix.
33. User's forward-looking idea, not built now: eventually want to query/compare nodes across BOTH v5 and v5.1 (not just auto-pick one) -- ties into the existing quarterly-resweep-cadence backlog item. resolve_version() deliberately doesn't blend versions in one query (cliff-safety neighbor search assumes one consistent grid) so this needs real design work, not a quick change. Revisit when it matters (~2 months per user).
34. Column grouping simplified: dropped the full-file per-section grouping, replaced with a single group AO (fillacc_possible_mean_err_pct) through BJ (one before BK), hidden -- BK (core_addon_cagr_pct) stays visible/unhidden.
35. Freeze panes extended past Keys through the full core CAGR score block (strategy_cagr_pct + core_addon/drought/both/sdb_cagr_pct), not just Keys alone -- whole CAGR picture stays visible while scrolling right.
36. Fixed: run_overlay_shim.py gained a --node-id option (via new locate_best_node.node_from_candidate_id()) to run against an exact candidate_nodes row instead of re-deriving "best" -- needed because best_row()'s own selection can legitimately disagree with candidate_full_review.py's per-candidate-type node picks (found on ETHU: kept generating new nodes that matched none of the report's displayed rows). ETHU's real confirm_days=7 (weak, n=7) now correctly lands on its actual "best safe node" report row.
37. target_cagr formatted as whole-number % (was showing decimals from the underlying formula).
38. Real layout fix: stacked CAGR columns (core_addon/drought/both/sdb_cagr_pct) moved to sit immediately after Keys, not out past ~50 unrelated detail columns -- freeze_panes was dragging along everything in between (ended up 50+ columns wide). Freeze now Q2 (Keys + CAGR scores only).
39. Hidden group boundary corrected to AN (worst_neighbor_pct, start of the cliff-safety block) through drought_wr_tranche -- covers cliff+fillacc+addon+drought detail as one collapsible section.
40. Sort by target_cagr descending applied as the final build step, after account_mod picks are set (matches Phase 2 sequencing -- sort on what was picked, not raw alpha before mods are chosen).
41. "Certain-only" rows (candidate_type='best certain' AND also_matches is exactly 'best certain', not also matching safe/unsafe/CAGR-safe/5min) filtered out: pick='no', comment='certain'. Applied directly to candidate_nodes DB (persists across re-runs, matches pick/comment's designed behavior) -- 5 rows: DPST(132), ETHU(237), KORU(133), SOXL(130), SOXS(317).
42. Real bug fixed: openpyxl's column_dimensions.group() only sets outlineLevel/hidden on the FIRST column of a range, not the whole span (confirmed via a minimal repro) -- explains why grouping "looked wrong" repeatedly. Replaced with an explicit loop setting each column's dimension individually. AN:BN now correctly all hidden/grouped.

## Side investigation: JNUG same_day_block real-node check (2026-08-13)
Triggered by reviewing sdb columns in the prototype -- found neither report row for JNUG
matched the real live node exactly (live node = candidate_nodes id=45, v5, not the v5.1-resolved
rows the report shows by default). Checked id=45 directly:
- Full history (2.92yr): unblocked compounded=2374.7%, blocked=590.7% -- blocking costs ~4x in absolute terms.
- Trailing 1.11yr (matching the original 2026-08-11 decision's methodology): unblocked=415.4%, blocked=562.6% -- blocking wins recently.
- 5-way split of the trailing window: diffs bounce -44pp to +48pp, too few trades per slice (3-23) to call a trend either way.
- One specific losing swing traced to a real trade: unblocked would have been +30.3% (Feb 5-11 2026), blocked shifted the entry a day and turned it into -1.4%.

**Decision (user, 2026-08-13)**: leave force_same_day_block=1 on JNUG (wl_id=205, brokerage) for now -- acknowledged the full-history cost is real and large, but wants to keep it running a bit longer as a live production test rather than reverting immediately.
Also found separately: 8 of 10 real live tickers have an exact candidate_nodes match (SOXL/DPST/GDXU/ETHU/KORU/DFEN/JNUG/SOXS); NUGT and AGQ have no exact match at all -- not investigated further this session.
Refined: plan is to kill force_same_day_block=1 on JNUG after the first real live trade closes under it -- one real data point, not an open-ended test.
43. AGQ's pick='no' (item 29) confirmed wrong -- fixed to pick='yes', comment='promoted to real live brokerage node id=203, 2026-08-10' (matches the real, already-documented promotion).
44. Correction to item 43: AGQ's pick/comment blanked out (NULL/NULL) instead of set to 'yes' -- this is a fresh portfolio-construction pass, so the old stale value shouldn't be replaced with another asserted claim, just reset to undecided for a real decision to be made in this process.
45. Real persistence built (per user's explicit call, after losing the original mobaxterm file): candidate_nodes gained account_mod/account columns. build_portfolio_prototype.py now reads existing persisted values first (a manual edit always wins), and writes back any freshly-computed default immediately -- nothing built in the Keys section only lives in the xlsx anymore. Sync direction going forward: user pastes/tells values, agent writes to DB directly (no automated Excel-read-back parser needed).
46. Seeded account_mod/account for the 10 real live tickers from the REAL watch_list_candidate_link table (their actual 2026-08-11 historical picks, not auto-computed): NUGT=Drought/ira, SOXL=Drought/ira, DPST=Core/ira, GDXU=Drought/roth, ETHU=Add On/brokerage, KORU=Drought/roth, AGQ=Add On/brokerage, DFEN=Core/roth, JNUG=Overlay/brokerage, SOXS=Drought/ira.

## Forward intent (2026-08-13, for ~3 months out)
User's stated future need: join today's node picks (account_mod/account/pick, now persisted on candidate_nodes.id) to a fresh 1-year-forward backtest run -- i.e. take today's real decisions and compare/rotate against fresh data ~3 months from now. Ties directly to the existing backlog item "quarterly resweep cadence + dual-window (max-year/1yr) CAGR + candidate rotation" (docs/deep_backlog.md, 2026-08-11 entry) -- this is a concrete use case for that item, not a new one. The join key is candidate_nodes.id, which is why persisting account_mod/account against that exact id (not just a spreadsheet row) matters for this to actually work later.

## Provenance persistence built (2026-08-13, "your mission to populate the dates")
Real gap found: candidate_nodes had zero date-range provenance -- all 10 real live nodes' sweep_run_id was NULL (never tracked, created before that mechanism existed 2026-08-11), so there was no way to know from the DB alone what data window any stored robust_alpha reflected. Confirmed real drift risk: 6 of 10 tickers (JNUG/DFEN/GDXU/ETHU/NUGT/SOXS) had years=1.11 at actual decision time (pulled from the original 2026-08-09 snapshot files, real evidence) vs ~2.9-3+ years available today.

Built:
- candidate_nodes gained 4 new columns: data_start, data_end, years_at_computation, robust_alpha_computed_at.
- Backfilled robust_alpha_computed_at for 340/341 existing rows via direct join to backtest_cache.run_timestamp (100% reliable, no guessing) -- 1 orphaned row (TZA, id=1) has no matching backtest_cache row left (likely pruned).
- Backfilled years_at_computation for the 10 real live nodes ONLY, from real values found in the original 2026-08-09/08-08 snapshot xlsx files (JNUG/DFEN/GDXU/ETHU/NUGT/SOXS=1.11yr, AGQ/SOXL/KORU=3.04yr, DPST=3.02yr) -- explicitly did NOT backfill/guess this for the other ~330 rows (no real record exists, left NULL per the project's "never fabricate provenance" lesson).
- get_or_create_candidate_node() (locate_best_node.py) now stamps real data_start/data_end/years_at_computation/robust_alpha_computed_at on EVERY touch going forward (both new-row insert and existing-row refresh, including when alpha/trades are unchanged -- "confirmed current as of now" is itself real information). Fixed a bug in the first version where provenance only got stamped on an actual alpha/trades drift, silently skipping unchanged rows.

Explored but deliberately not relied on: reconstructing historical alpha by truncating today's CSV to an old node's [data_end-years, data_end] window and re-simulating -- worked closely for some tickers (DFEN +0.4pp, DPST -1.8pp) but was wildly off for others (AGQ -948.9pp, NUGT -228.3pp, KORU -106.4pp), likely because today's CSV has since had corrections (bad-tick fixes, split-guard rescales) a naive truncation doesn't undo. Not scaled to the full 100+ row file -- flagged as unreliable, not built out further.

## 1-year window sweep tooling built (2026-08-13)
Confirmed directly: the sweep/kernel has no native date-window parameter -- always reads the full cached CSV. Discussed and settled on a safe approach: temporarily truncate the target ticker's CSV in place (backup first, restore via trap on exit, matching this project's config.json-patch-then-restore pattern), run the sweep, restore. Real accepted risk (user's call): a concurrent research process touching the same ticker during the truncation window would see wrong data -- user's own discipline is to only run one research process at a time. Live daemon is not at risk (only reads recent bars, never old history).
Built `scripts/run_1y_window_sweep.sh TICKER YEARS END_DATE VERSION_TAG [--skip-cache-refresh]`. END_DATE must be explicit/fixed (not "today", which would silently drift). VERSION_TAG must be a real, distinct, newly-minted version string (e.g. v6-1y) -- confirmed this needs no schema change, `version` already supports arbitrary distinct campaign tags the same way v5.1 already is its own value from v5. Not run yet -- per standing convention, user runs sweeps themselves.
