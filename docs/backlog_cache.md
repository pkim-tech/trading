# Backlog Cache

> **Open items only.** Resolved items live permanently in `docs/deep_backlog.md`; a rolling
> window of recently-resolved one-liners (for session-handoff context) lives in
> `docs/backlog_resolved_recent.md`, pruned to entries from roughly the last week — anything
> older is fully covered by `deep_backlog.md` and doesn't need re-reading here. `go` reads this
> file plus `backlog_resolved_recent.md` in full each session; keep both lean.
>
> **Maintenance**: when an item here resolves — (1) add/update the full-detail entry in
> `deep_backlog.md` (unchanged convention), (2) prepend a one-liner to
> `backlog_resolved_recent.md`, (3) drop entries from `backlog_resolved_recent.md` older than
> ~7 days (already permanent in `deep_backlog.md`), (4) remove the item from this file. When a
> genuinely new backlog item is raised, add a 1-2 line entry here directly (no full-detail
> writeup needed unless/until it resolves).

## [live-trading] Found 2026-08-04 very late — broader diagnosis still open (all 3 point-fixes now resolved, see `backlog_resolved_recent.md`)
**Broader diagnosis (still open)**: (1) and (2) (now fixed, see `backlog_resolved_recent.md`) weren't coincidence -- multiple scripts (`status_check.py`, `coverage_check.py`, `watchlist_status.py`, `paper_trading_status.py`) each reimplement their own ad hoc view of overlapping state (open positions, pending buys, node config) instead of sharing one canonical "real current state" function, so the same class of bug can independently exist in each. Real fix candidates, not yet decided: (a) narrow -- point-fix the next instance when found; (b) broad -- one shared `signals_db` function (e.g. `get_real_position_state(ticker, account=None)`) that all these scripts call instead of hand-rolling SQL. Either touches `signals_db.py`/coverage logic and needs the standard Opus review gate. See `.claude/skills/daily-routine-check/SKILL.md` for the full context.

## [backtest] Built 2026-08-05, real gap found — drought-overlay sweep's cliff-safety catches parameter instability but not ticker-concentration risk; best pooled cell (confirm_days=20/sl=15/arm=15/trail=5) is KORU-dominated, same failure shape as the original SOXL-only result
`scripts/drought_overlay_sweep.py` built and run (see `docs/research_log.md`'s 2026-08-05 (late) entry for full result). Needs a concentration screen (e.g. max single-ticker share of pooled compounded return, or a minimum-net-positive-tickers-at-n>=X requirement) before any pooled cell can be trusted as a real candidate. Put-hedge option-selection methodology (below) stays blocked on this landing first.

## [portfolio] Planned, not yet built, 2026-08-04 very late — put-hedge option-selection methodology (strike/expiry rule, real IV via yfinance live chains, expected-value cost-benefit)
Full design: `docs/design.md`'s "Put-hedge option-selection methodology" section. Still gated on the SOXL sweep above landing on real risk parameters -- the sweep now exists but hasn't cleared the concentration-risk gap yet.

## [backtest] Research idea, found 2026-08-03, narrowed same day — the 14:30 daily signal window fires ~3x more entry signals than the 9:30 window, across nearly every v5 watchlist ticker
Side finding from the entry-timing-seasonality check (`docs/research_log.md`'s 2026-08-03 entry). Directional intraday drift is now ruled out as the explanation (same-day follow-up entry: no real 9:30-to-14:30 price drift on any of the 10 tickers). Leading unverified explanation: cumulative opportunity — more elapsed trading time by the 6th bar than the 1st for any large-enough move (either direction) to breach the lower band, not a directional bias. Not yet tested directly (would need e.g. comparing intraday range/volatility by bar number).

## [backtest] Research idea, raised 2026-08-02 — FFT/wavelet-based trend-extraction as a distinct strategy paradigm (not an enhancement to the existing z-score mean-reversion family)
Evaluated a Gemini-proposed live spectral-filtering pipeline (STFT/CWT low-pass denoising → slope/crossing signal → VWAP/TWAP execution). Confirmed this describes a fundamentally different strategy (trend-following off a denoised price line, sub-minute tick-level execution) than what this system runs (hourly-bar z-score mean reversion, 2 fixed daily signal windows, manual/bridge-automation execution) — most of the doc's cold-start/microstructure/execution-routing content doesn't apply here at all. The one applicable piece (offline FFT cycle-period detection on historical hourly data) is already captured separately — see the existing FFT cycle-detection item below. This entry is specifically the "build a new denoised-trend strategy variant" idea — unscoped, would need its own backtest kernel path distinct from `strategies.py`'s current classes. Not just a stray idea: user confirmed 2026-08-02 the system may eventually run several distinct strategy paradigms in parallel, not just mean-reversion variants — so this is a real candidate for that, not automatically low priority.

## [backtest][live-trading] Research idea, raised 2026-08-02 — ATR/volatility-scaled position sizing, evaluated against a Gemini-proposed risk-architecture doc
Real blocker, not just unscoped: `starting_notional` is already capital-constrained per account (one-account-per-ticker model, sized to available capital), not a free sizing knob — needs a design for what shrinks when vol is high (idle capital? fewer shares?) before this is even a backtest question. Two other modules from the same doc (watchdog auto-flatten, loss-streak circuit breaker) were evaluated and rejected/backlogged in the same conversation — see below and `docs/conversation_summary.md`.

## [live-trading] Research idea, raised 2026-08-02 — loss-streak circuit breaker (same-day or cross-day), evaluated against the same Gemini risk-architecture doc
User flagged real overfitting risk: current live footprint is only 4 nodes (SPY/SH/GDXU/DPST) across 2 signal windows/day, too little same-day trade volume for a same-day streak counter to mean anything, and a cross-day threshold would be tuned on a small live sample. Needs a "watch it, don't automate it" framing (alert-only) rather than an auto-pause, if pursued at all.

## [live-trading][coverage] Investigated 2026-08-02 — the 3 suspicious wired-never-fired rows explained; 2 real open items filed; remaining 14 triaged by staging feasibility, none actionable today
Full detail: `docs/deep_backlog.md`'s two 2026-08-02 entries. Of the 14 `wired-never-fired` rows: 4 need staging during market hours (blocked while not WFH), 4 need a 2nd live node on a different account (blocked, only one real account), 1 deferred to the planned Slack rearchitecture, 1 (`market_buy_placement`) just needs DPST to hit a real signal, 4 aren't realistically stageable.

## [backtest][live-trading] Open, raised 2026-08-01 — formalize/write down the pattern separating manual-fill execution reality from automated/backtest-assumed fills
Currently scattered across `docs/operational_limits.md`'s Phase 1/2 marker and `sim_chaos_monkey.py`; not yet scoped where it should actually live.

## [backtest] Open (bear-market + regime items) — from the 2026-08-01 research tangent's independent Opus challenge
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by title: "independent Opus challenge of the 2026-08-01 research tangent"). Compounding-drag item itself is resolved.

## [live-trading][coverage] Open, raised 2026-07-30 — should canary scenario_expectations tests appear on the Trade-Flow Accountability Grid?
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Accepted residual risk, 2026-07-27 — `_submit_replace_with_retry`'s retry can fire a second `replace_order` against an already-replaced order_id
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Partially resolved 2026-07-27 (later) — `.env`'s `SCHWAB_AUTOMATION_TICKERS` trimmed from 29 to the 12 tickers on watchlist 65. Research universe/`trading_universe.db` scope not touched — still open if that broader trim is wanted.

## [live-trading] Open, raised 2026-07-26 (evening), staged 2026-07-27 — 3 of 3 planned real-order tests now progressing; daemon currently down, blocking the remaining 2 from resolving
Full detail: `docs/deep_backlog.md`'s 2026-07-27 entry. Status of the remaining 2 unconfirmed as of last check — verify before assuming still blocked.

## [live-trading][coverage] Open, raised 2026-07-27 — Trade-Flow Test Accountability Grid (`pages/14_Coverage.py`, backed by `scripts/coverage_registry.py`) needs filters + direct links to underlying tests
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Idea, raised 2026-07-26, explicitly gated on the wl_id refactor landing and being observed correct first (that landed 2026-07-25/26) — per-node `dry_run` override, additive/OR-logic only, never replacing the account-level flag
`docs/deep_backlog.md:3642` — deferred, user judged it disproportionate effort even with the gate satisfied. Full detail there.

## [live-trading][compliance] Open, raised 2026-07-25 (3rd time this has come up in conversation) — FINRA eliminated the PDT rule/$25k threshold in 2026; re-evaluate daily_order_cap's purpose and confirm Schwab's rollout status
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest][live-trading] Research idea, raised 2026-07-25 — monthly universe rescreen (recurring cadence) + a possible "v6" momentum-exhaustion-bounce strategy variant
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Latent, found by Opus review round 6, 2026-07-25 — stale-pending-buys guard could silently discard a real manual fill confirmation if a ticker is ever outside SCHWAB_AUTOMATION_TICKERS
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title). `signals_invariants.py` monitors the config state that would trigger it but doesn't fix the underlying handler.

## [live-trading][security] Deferred, not currently reachable, found by Opus review 2026-07-24 evening — manual-"Filled" SL call is ticker-gated but not mode-gated
Add a `node.get('mode', 'live') == 'live'` guard alongside the ticker check if/when that routing changes — see `docs/deep_backlog.md`'s 2026-07-24 evening batch-fix entry. Distinct from the 2026-08-01 `handle_entry_price` SL-placement fix (different code path) — still open.

## [live-trading][coverage] Minor, found 2026-07-24 ~evening via Opus review — record_deviation doesn't refresh expected_outcome on rerun
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Idea, raised 2026-07-24 ~15:05 ET — open a new margin account strictly dedicated to one real production ticker; keep soxl_ira as the standing multi-ticker test account
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][tax] Idea, raised 2026-07-24 ~15:10 ET — run SOXL in both `roth` and `soxl_ira`, but `roth` needs to become limited-margin first
`roth` is still `dry_run=True`/cash-type as of last check — precondition still unmet.

## [live-trading] Open, raised 2026-07-24 ~10:55 ET — retry `check_gap_resize`'s cancel+replace test properly pre-market on a future day, not mid-day
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Open, found 2026-07-24 ~10:35 ET — real BUY/SELL Slack alerts carry no canary tag, unlike the Reference Report or paper-trading's console tag
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title). Unconfirmed whether still true — verify before assuming.

## [live-trading] Far-backlog, raised 2026-07-25, deprioritized same day — v5 watchlist skews long-only, consider adding inverse counterparts
Distinct from the canary `ira` account's 2026-07-29 inverse-pair additions (FAZ/SPXU/TWM/QID/SDOW/JNUG/JDST) — those are canary-only, the real v5 watchlist 65 skew is untouched. Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Open, raised 2026-07-24 morning — signal/reminder alerts don't show dry_run status, only real order-placement messages do
Distinct from `mode_tag(account)` (2026-07-26), which only tags order-placement alerts. Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Open, raised 2026-07-24 morning, CORRECTED 2026-07-24 ~10:15 ET — split paper-trading/dry-run/live notifications into separate channels (or otherwise reduce chattiness)
Explicitly still backlogged per CLAUDE.md's `paper_alert_verbose` note — deliberately not bundled into smaller fixes. Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][security] Open, raised by session-wrap Opus review 2026-07-23 night — `availableFunds` is leverage-inclusive for a real margin account, unverified whether that's safe for `brokerage`
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title). Monitored (not fixed) via one of `signals_invariants.py`'s 4 checks.

## [live-trading] Open, raised 2026-07-23 night — Schwab auth flow is "clunky", no cleaner unattended path designed yet
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title). Distinct from the already-fixed `interactive=True` hang bug.

## [backtest] Open, paused 2026-07-22 — "v6" idle-capital parking idea; inconclusive, downturn-specific follow-up queued
Tested (`docs/research_log.md`'s 2026-07-22 entry): no robust fold-consistent edge found. Follow-up idea (check specifically during real SPY drawdown episodes — dates listed in CLAUDE.md) raised but not yet run.

## [live-trading] Idea, raised 2026-07-22, not designed — small (10-share) real EDC pilot position, held ~1 month to shake out issues before scaling
Note: EDC's node was fully removed from `watch_list` 2026-07-19 (its one open position is now hand-tracked via spreadsheet) — re-confirm this idea's premise still holds before acting on it.

## [live-trading][tax] Open question, raised 2026-07-22 — which ticker (SOXL vs AGQ) goes to the taxable brokerage account first; wash-sale cross-account mechanic needs one more confirmation
Gated on the broader wash-sale/tax analysis item below, also still open.

## [live-trading][security] Not started, discussed at length 2026-07-22 — max cumulative BUY notional per ticker per day (backstop against a repeat-buy/runaway bug)
User wants to think it over before picking a final multiplier. Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Low priority, idea raised 2026-07-21 — should `CASH_SAFETY_BUFFER` scale with order size instead of staying a flat $200?
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Implemented 2026-07-21, unclear if live-tested — Entry Trigger/Fill/SL-Placement/Arm-latency automation for TrailingExitZScoreBreakout (Part 4)
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title). Extensive automation now exists and runs live (DPST, canary E-node) — verify whether this specific code path has been directly confirmed before treating as still-open.

## [live-trading] Implemented 2026-07-21, unclear if live-tested — trailing-buy budget adherence (Part 3: padded sizing + overnight gap guard + post-fill top-up)
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title). CLAUDE.md's Position Sizing section describes the padded-sizing + top-up design as working correctly at real notional (mathematically forced) — may be de facto resolved, verify before further work here.

## [live-trading][tax] Active hold, set 2026-07-20 — don't buy GDXU/AGQ in any IRA-type account before their wash-sale clearance dates
**Still genuinely active**: clearance dates are 2026-08-05/2026-08-06 (confirmed via `docs/conversation_summary.md`) — today is 2026-08-02. Distinct from the unrelated, already-cleared 2026-07-07 wash-sale question.

## [backtest] Research idea, not started, 2026-07-20 — is overnight gap frequency/magnitude asymmetric (up-gap vs down-gap)?
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Low priority, 2026-07-19 — paper-trading dedup is ticker-only, not `(ticker, window)`-aware
Full detail in `docs/deep_backlog.md`.

## [live-trading][security] Phase 4 (deferred to cloud-infrastructure planning), 2026-07-18 — move order-placement/mutating Schwab calls behind a separate proxy this session can't write to
Full detail in `docs/deep_backlog.md`.

## [live-trading][security] High priority, active focus as of 2026-07-18 — one brokerage account per live ticker, for blast-radius containment against a rogue algorithm
Full detail in `docs/deep_backlog.md`. Confirmed still unbuilt: `ACCOUNTS` has only one non-dry-run account (`soxl_ira`), shared across many live tickers.

## [live-trading][tax] Deprioritized 2026-07-18, 2026-07-17 — wash-sale/tax analysis needed before promoting any ticker into the taxable brokerage account
Full detail moved to `docs/deep_backlog.md` ("Deprioritized 2026-07-18" entry). No ticker promoted to taxable brokerage yet — precondition still unmet.

## [live-trading][backtest] High priority (raised 2026-07-18) — dividend cash isn't credited into P&L/SL/arm tracking; material for DPST specifically
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest] Medium priority, designed-not-built 2026-07-22 — data mutation log (traceability, not full immutability/versioning) for historical price cache rescales
Note: `db_cache.log_data_mutation`/`get_data_mutations` was built 2026-07-22 per CLAUDE.md's Key Files — this item may already be resolved and just never closed out here; verify before treating as open.

## [backtest] Idea, not scoped, 2026-07-15 — eventually delete v3.x once v4 is a confirmed superset
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest] Medium priority, 2026-07-15 (revised) — v4 sweep disk footprint: 11-ticker watchlist is fine, full 53-ticker universe is not
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title). Note: the 2026-08-02 `backtest_cache` pruning (65GB→256MB, island-only) changes storage economics but addresses post-hoc bloat, not necessarily peak-disk-during-a-full-universe-sweep — unclear if this fully resolves the original concern.

## [backtest] Medium priority, 2026-07-10 — same-bar arm/take-profit trigger not checked at entry
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title). Distinct from the unrelated `at_bar_close` bookkeeping bug (fixed 2026-07-27).

## [backtest] Open question, 2026-07-09 (rescoped 2026-07-13, buffer question resolved 2026-07-14) — fixed_sl=15% itself still needs a real sweep
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest] High priority, 2026-07-16 — need a proper delayed-entry simulation for the same-day-re-buy constraint, not just a trade-list filter
`docs/deep_backlog.md:4601-4602` confirms still unbuilt — only the admittedly-wrong post-hoc filter method exists. `sim_delayed_sell.py` (2026-07-17) covers the exit-side mirror only, not this entry-side ask.

## [execution] Research question, 2026-07-15 — when would TWAP/VWAP order execution become worth considering
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [backtest] Idea, not scoped, 2026-07-18 — daily-bar strategy variant, for much longer backtest history / real bear-market regime coverage
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading] Idea, not built, 2026-08-01 — real correlation-verification logic for the new JNUG/JDST "G" canary pair
Today's fix (`docs/deep_backlog.md`'s 2026-08-01 entry) only restored their real config/labels/scenario_expectations row (`canary_bull_bear_pair`) — monitored the same simple same-day-trade-happened way as every other canary, no real correlation check built yet.

## [backtest] Idea, not scoped, re-opened 2026-08-03 — rolling/time-varying FFT spectral power as a regime signal (alongside the 2026-08-01 SPY-trend/VIX finding)
Split off from the now-closed "FFT cycle detection to inform window selection" item — that half was tested and refuted (`docs/research_log.md`'s 2026-08-03 entry: no significant fixed dominant cycle in any v5 ticker's returns). This half — whether *time-varying* spectral structure (not a single fixed cycle) says anything about regime — was never tested and is still open.

## [data] Idea, not scoped, 2026-08-01 — start recording our own 1-minute bars now, so a future "we need historical data" request never hits an expired retention window again
Full detail: `docs/deep_backlog.md`'s bulk-migrated 2026-08-01 entry (search by this header's title).

## [live-trading][tax] Idea, raised 2026-08-04 evening — live wash-sale risk monitor (detection/alerting only, not prevention)
Distinct from the backtest-only simulator below (`sim_wash_sale_impact.py`, retrospective/aggregate) — this would run against real, current positions and flag which ones are at wash-sale risk right now (e.g. a same/substantially-identical security bought in one real account within 30 days of a loss-sale in another, the cross-account gridlock mechanism already found for SOXL). Explicitly not meant to block or auto-cancel a trade — just surface the risk so the user can decide (same "detection-only, decide policy later" pattern as `_log_pre_action_state_verification`/`record_node_streak`, 2026-07-29). **Two specific tickers named as priority scope, 2026-08-04**: SPY (will be bought/sold across multiple accounts via the skim-and-reserve overlay's skim/redeploy mechanism above — real multi-account wash-sale exposure by design, not a hypothetical) and TQQQ (already held elsewhere per the standing watchlist note; a real IRA node may get added if liquidity ever becomes a problem for the existing brokerage-held position — see the 2026-08-04 TQQQ-cleared-for-IRA item above, same conditional-risk shape). Not scoped: where it hooks in (real-time during `check_order`? a daily batch report?), what "at risk" means precisely per instrument type (standard 1099 vs AGQ's Section-1256 wash-sale-exempt status needs to be excluded from triggering false positives), and whether it needs real cost-basis/lot data from Schwab or can work off local `trade_log`/`open_positions`.

## [portfolio][tax] Idea, raised 2026-08-03 evening — real wash-sale/cost-basis-tracking + tax simulator, prototype only
`scripts/sim_wash_sale_impact.py` (real AGQ/SOXL trades) found near-total wash-sale gridlock at this trade cadence in a taxable account — SOXL's result stands (owed tax despite a net loss, standard '40-Act fund, normal wash-sale rules apply). **AGQ's result is wrong and superseded, 2026-08-04**: AGQ is a Section 1256 commodity-pool/K-1 instrument, which is exempt from the wash-sale rule entirely (overrides normal broker 1099-B wash-sale treatment even at the share level) and gets 60/40 blended-rate tax treatment in a taxable account instead of the flat ordinary rate the script assumed — the reverse of what was concluded. See the new AGQ K-1/UBTI entry below for the fuller correction (AGQ may now be *better* suited to brokerage than IRA, opposite of the original recommendation). Full rebuild (Section 1256 60/40 + no-wash-sale modeling) not done — this prototype's AGQ numbers should not be used for decisions.

## [portfolio][tax] Found 2026-08-04 — AGQ is a K-1/UBTI risk, same category as the now-blocked USO; account-placement recommendation reversed
Confirmed via research (ProShares' own K-1 documentation): AGQ (ProShares Ultra Silver) is a Section 1256 commodity pool taxed as a partnership, not a standard '40-Act fund like SOXL. Real IRA/Roth risk: if K-1-allocated gross UBTI crosses $1,000/year (plausible given real trading activity — 46 trades, real gains in the 2026 backtest), the IRA itself must file Form 990-T and pay UBIT at compressed trust tax rates (37% bracket starts ~$14,451), debited directly from IRA assets by the custodian (not a taxable distribution, but real value erosion, happening automatically). This directly reverses tonight's earlier IRA-placement recommendation for AGQ — brokerage (Section 1256's 60/40 blended rate, no wash-sale issue) may now be the *better* fit for AGQ specifically, opposite of SOXL, which should stay in IRA/Roth per its own (still-valid) wash-sale-gridlock finding. **Not yet decided or acted on** — same-night finding, surfaced too late in the session to re-derive the full account-placement plan. `docs/operational_limits.md:86` had already flagged this as an open "verify before funding" item as far back as project history goes; it was never actually checked until tonight.

## [live-trading][tax] Idea, raised 2026-08-04 — TQQQ cleared for IRA trading (no K-1, wash-sale risk conditional on brokerage leg staying unsold-at-a-loss); not yet acted on
Full detail: `docs/research_log.md`'s 2026-08-04 entry. Best v5 node identified (`TrailingBothZScoreBreakout`, fixed_sl=2, open_check, 120 trades, 20.8% win rate, +242.8% robust alpha) but no IRA `watch_list` node added yet — TQQQ's existing nodes are all stale pre-v4 versions or research-mode.

## [portfolio][design] Decided-in-principle 2026-08-04 — skim-and-reserve "insurance policy" overlay: design finalized (SPY reserve, manual redeploy), not yet implemented
Full detail: `docs/deep_backlog.md`'s 2026-08-04 entry (top). Includes the untested multi-bounce/redeploy-fakeout risk and the user's "skim regardless" call.

## [backtest] Research thread resolved-with-caveats 2026-08-04 late — SOXL-weak/AGQ-strong 2026 YTD observation confirmed real after fixing 2 real bugs in tonight's research scripts; underlying cause still unexplained
User's original observation (AGQ strategy +68% YTD 2026 despite price -58%, SOXL strategy weak despite price +197%) was initially contradicted by tonight's 4 research passes (velocity, chop-cluster, regime regression, config cross-apply) — traced to two real bugs shared across all 5 scripts written tonight: (1) `TrailingExitZScoreBreakout` nodes (AGQ/DPST/KORU/NUGT/UDOW/YANG) store their arm-sell/take-profit value in the `take_profit` column, not `arm_sell_pct` — scripts only read the latter and silently disabled TP for these 6 tickers; (2) none of the 5 scripts passed `open_check=True` even though every real v5 node uses `entry_timing='open_check'`. A cold Opus review (blind read) independently confirmed bug 1's fix and independently found bug 2, plus 3 smaller issues (a still-open mark-to-market trade counted as completed in 3 scripts; a stale docstring; dead code) — all fixed. Corrected result: AGQ 2026 YTD +52.3%, SOXL 2026 YTD -30.8% — confirms the original observation. But the corrected pooled cross-ticker/cross-time analysis (105 periods, 10 tickers) shows strategy return generally moves *with* price trend, not against it (rho=+0.539, p<0.0001, survives ticker fixed effects) — so 2026 is a genuine **outlier year for both tickers relative to their own historical pattern**, not an example of a general rule. Cause of the 2026 anomaly itself is still open. Two committed scripts had the same bug pattern — both now resolved: `scripts/multiday_cycle_entry_test.py`'s buggy path turned out to never have been exercised (its published finding was independent of node config, confirmed unaffected on inspection); `scripts/entry_timing_seasonality.py` was fixed and its 2026-08-03 finding rerun — confirmed, if anything more solidly null (win rates now nearly identical between windows, p-values higher than before). All 5 of tonight's scripts also rerun post-fix; conclusions unchanged except the chop-cluster scan's "closest to significant" ticker flipped from SOXL to KORU (neither survives multiple-comparisons scrutiny). Full detail: `docs/research_log.md`'s 2026-08-04 (late) entries.

## [portfolio] Idea, raised 2026-08-04 evening — options-based alternatives to the reserve/insurance idea, not modeled
Four candidate uses, confirmed real account capability (Schwab, researched 2026-08-04): (1) protective puts on SPY/QQQ as literal crash insurance instead of the static skim reserve — works in any IRA, even without limited margin (Level 1). (2) covered calls on the idle reserve for extra yield — also Level 1. (3) vertical spreads for cheaper-structured insurance — needs limited margin on the IRA (already a separate open item). (4) SPX (broad-based index) options get the same Section 1256 60/40 + no-wash-sale tax treatment just confirmed for AGQ — a natural fit for brokerage specifically. None modeled yet — muni-bond-as-reserve test was also started same session (`cache/research/MUB_1h.csv` fetched) and abandoned mid-calculation (NaN bug, unfixed) when this tangent came up.

## [backtest][portfolio] Open, raised/deeply explored 2026-08-04 very late — drought overlay (buy underlying + reuse the core strategy's own arm-then-trail exit mechanics, entered on a reactive "10 days since last signal" confirmation) — mechanistically sound, not yet validated as deployable
Full chain: `scripts/drought_detection_test.py` (predictive vol-percentile detection — refuted, ~zero advance warning) → `scripts/drought_buy_hold_test.py` (naive hold-to-next-signal — superseded, open-ended risk) → `scripts/drought_overlay_test.py` (current: reuses the real TrailingBoth arm/SL/trail state machine, reactive entry). Result: SOXL +30.2% compounded but driven by just 2 of 13 trades (fragile); TQQQ negligible (+0.81pp on an already-497.7%-compounded core); pooled across all 10 v5 tickers still net negative (10% win rate below breakeven for the 5.4x payoff ratio). **Real finding**: gap-through-stop-loss risk is heavily concentrated in volatile/idiosyncratic leveraged sector-commodity names (KORU/AGQ/YANG, max excess 30.6%) vs. broad-index leveraged funds (SPY/QQQ family, max excess 2.0%) — this overlay is structurally safer on the broad-index names even though their core edge is smaller, a tradeoff not yet weighed. Put-hedge identified as the structural fix for gap risk (stops can be gapped through, puts can't) but unbacktested — no historical options data available (yfinance is live-only). Real side-insight: droughts are low-realized-vol periods, and IV tends to track realized vol with a lag, so put purchases timed to a detected drought should tend to be cheap. Needs: much larger drought sample before trusting SOXL's result, options-data infrastructure (or a rough Black-Scholes approximation) for the put-hedge economics. Full detail: `docs/research_log.md`'s 2026-08-04 (very late) entry.
