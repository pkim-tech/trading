# Backlog

## High Priority

- **Review P0 live-trading fixes one at a time**: 2026-07-03 follow-up session implemented fixes for code_review_findings.md P0 #1 (TIME exit wall-clock bug), #2 (fixed_sl/trail_pct round-trip), #4 (signal-window exact-minute bug), #5 (sell_alerted never cleared), #6 (app.py config corruption) — self-verified only (unit-level smoke tests + one real backfill), not yet walked through by the user. See "Fixed in follow-up session (2026-07-03, continued)" at the bottom of `docs/code_review_findings.md` for what changed and why. `active_signals.py` needs a restart once reviewed/accepted (live process won't pick up changes otherwise).

- **Dispatch overhead optimization**: Profiled 2026-07-03 (see `docs/dispatch_telemetry_results.md`) — **correction same day**: the profiling script never actually measured DB-insert time (no `INSERT` in its instrumented path); "88% result collection" is mostly just parallel kernel compute (~90% parallel efficiency), not IPC/pickling overhead as originally written up. Batched the `backtest_cache` INSERT into `executemany()` chunks of 50 with named columns anyway (worth having independently — caught a real instance of positional-insert fragility, code_review_findings.md #15). **Not yet measured**: whether batching actually improved sweep speed. Next session: instrument the real INSERT step in isolation (old per-row `execute()` vs new `executemany()`) to get real before/after numbers.

- **Checkpoint 1 ticker-scoping bug — silently drops top candidates**: ✅ Fixed 2026-07-02. `identify_island_candidates()` (`run_optimization_sweep.py:350-360`) queried the entire historical `backtest_cache` for a version/strategy with no ticker filter, but `bh_cache` (gates Phase 2/3) is only built for the *current* run's `target_tickers`. Any run with a narrower ticker list than what's already cached from a prior broader run silently dropped legitimate top candidates at "No B&H data, skipping Phase 2" — no error, no warning surfaced anywhere except a log line. Confirmed it had happened across v1.6 (52x), v1.7 (27x), and v1.8 (3x) historically — including `MULL`, `VRTL`, `WULX`, `NBIZ`, `SMST`, the v1.6 top-5 alpha performers at the time. Fix: added `allowed_tickers` param to scope the Checkpoint 1 query to `ticker IN current tickers list`.

- **v1.9/v1.10 trailing buy strategies**: ✅ Built 2026-07-03. `TrailingBuyZScoreBreakout` (v1.9): after z-score signal, tracks running low and enters when price bounces `trail_buy_pct`% above it; fixed TP/SL exit. `TrailingBothZScoreBreakout` (v1.10): same trailing entry + trailing exit once TP activated (trail_pct=3% hardcoded). `sl` sweep axis → `trail_buy_pct` for both. Smoke test: AGQ/SOXL v1.10 beats v1.9; v1.8 beats v1.10 on AGQ but v1.10 beats v1.8 on SOXL — suggests ticker-dependent optimal. Sweep queued overnight.

- **Limit-order model variants (v1.7-2, v1.7-3)**: `LimitOrderZScoreBreakout` (v1.7) only implements entry-side limit fill (`_simulate_limit`: `low <= lower_band` intrabar, but TP still checks bar-close `cp >= tp_price`). Two unbuilt variants of the same model: v1.7-2 = exit-limit only (bar-close market entry like v1.5/v1.6, but TP fills as a real resting limit order intrabar — `high >= tp_price`, mirroring the existing SL check structure); v1.7-3 = both-limit (entry AND TP both real resting limit orders, closest to fully hands-off — only SL/TIME still need a live decision). `TrailingExitZScoreBreakout` (v1.8) likely has the same 3-way split (entry could be limit-based or bar-close, independent of exit mechanics) — same variation space, not three separate strategies. Use a dash suffix (v1.7-1/v1.7-2/v1.7-3) rather than the existing dot convention, since `v1.5.1`-style dots already mean "different z-threshold sweep of the same strategy" — don't want the two meanings colliding.

- **Session cache two-file design**: Work account uses two files — one for session close handover, one (conversation_cache?) for top-10 session history loaded into conversation context. Need to check work account to reconstruct the design and port it here.

- **v1.6 coarse grid sweep**: ✅ Done. Step-3 [3,6,...,30] coarse + 3-island ±4 fine mesh + full mesh for cliff-safe top-10. Three-phase sweep engine built (`run_optimization_sweep.py`). v1.6 completed: 358 tickers coarse, 30 island mesh, 1 full mesh (WULX — only cliff-safe index/other candidate). SMST full mesh running separately.

- **Phase 2.5 bug — only sweeps best node's (w,z)**: Phase 2.5 runs a ±CLIFF_RADIUS TP/SL ±7h hold sweep around the true best node, but only for that node's (w, z) combo. Should sweep all 3 island centers across all (w, z) combos so cliff check has complete neighborhood data for every candidate.

- **Sweep run registry**: Add `sweep_runs` table to DB — one row per sweep execution with `run_id`, `version`, `timestamp`, `config_json` snapshot, `notes`, `phase_reached`. Lets you record why each version was run and reconstruct config if needed. Wire into sweep engine to auto-insert on start/finish. Concrete case for why this matters (2026-07-02): discovered v1.6 only partially copied v1.5.1's EDC/FAS data (AGQ fully copied — 72k/72k rows match exactly; EDC/FAS only 8k/72k copied) with no record anywhere of why, or that a copy even happened — took manual SQL archaeology to reconstruct. A run registry would have made this a one-row lookup instead.

- **Cliff check improvements**: Current `CLIFF_RADIUS=2`, `AND trades > 0` excludes NO_TRADES nodes. Consider: (1) include NO_TRADES as alpha=0 so cliff detection catches edges where signal disappears; (2) widen radius to 3 for coarse-only data where ±2 may miss real neighbors. v1.5 cliff check: 25/340 tickers safe — VRTL, WULX, CIFG, GEVX, CRDU are top safe candidates.

- **v1.7 limit order entry model**: ✅ Built. `LimitOrderZScoreBreakout` — fill on `Low <= lower_band` intrabar at `lower_band` price; intrabar stop loss checks `Low <= stop_price`; TP checks `Close >= tp_price` at bar close. New `_simulate_limit` Numba kernel + `run_backtest_v17`. Grid: w=[10,20], z=[1.0,1.5,2.0], TP/SL=[3,6,...,30], Hold=[7,14,...,140].

- **v1.8 trailing exit**: ✅ Sweep wired. `TrailingExitZScoreBreakout` — close-based entry (v1.5 style), trailing stop once TP% cleared. `trail_pct` replaces `stop_losses` in sweep grid ([2–10%]), `fixed_stop_loss=15` in config execution. `run_backtest_v18` dispatched in sweep engine. Config set to v1.8, ready to run. Pending: run sweep, review results.

- **Trade log UI**: DB table and schema exist (`trade_log` in `trading_universe.db`). Pending: Socket Mode modal to record entry/exit from Slack interactions.

- **Screener → sweep**: Re-export leveraged ETF screener with Underlying Index + Total Assets columns, re-import, then use Screener page to select candidates and add to config.json for sweep. Current import (Results 7.csv) is missing those columns so underlier classification is incomplete.

## Visualization Pages (Streamlit)

- **Island view (Portfolio page)**: Click a watchlist node → show its neighborhood (±2 TP/SL, ±1 day hold — ~50 nodes total) with the selected node highlighted in the center. Visual version of the existing cliff/island safety check.

- **Open Positions page**: ✅ Built (`pages/10_Open_Positions.py`) — entry/signal price, drift %, current price, P&L %, TP/SL prices, hours held/left.

- **Universe Scan page** (`pages/11_Universe_Scan.py`): ✅ Built — coarse alpha ranking, liquidity (max notional), underlier type, TOP_IDX/TOP_STK/LOW_LIQ/REFINE flags, neighborhood safety score. Pending: (1) switch safety score to worst-neighbor min (currently count of positive neighbors); (2) color-code green/yellow/red; (3) fine mesh trigger button for top-25 only.

- **Two-phase UX rethink**: The current pages reflect two distinct workflows that aren't made explicit: (1) **Discovery** — sweep → Winners → find candidate tickers/nodes; (2) **Optimization** — Spatial Topology + Node Inspector → refine a candidate into a tradeable config. Consider shared "active ticker" context across Topology and Node Inspector, or restructuring so the two optimization pages feel like sub-views of a single ticker analysis flow.

- **Trade chart page**: ✅ Built as Node Inspector (`pages/2_Node_Inspector.py`) — price + bands at z=2.0/2.5/3.0, trade markers, optional Hurst/ADF (opt-in).

- **Topology page — collapsible controls**: Pickers and dropdowns consume too much vertical space. Add collapse/expand toggle to maximize chart real estate. — Medium

- **Topology page — node selection rework**: Bottom section for picking and researching nodes is hard to use. Needs rework — easier node selection, clearer node details, path to launch Node Inspector from selected node. — Medium

- **SPY trend / VIX level as entry filter**: Next research direction after ruling out Hurst/ADF. Hypothesis: avoid entries when SPY is in a downtrend (price < 200d SMA) or VIX is elevated (VIX > 25). Macro regime signals rather than ticker-level — may address the lag problem that killed Hurst/ADF.

## Medium Priority

- **Long-running process consistency check**: Currently manually restarting `active_signals.py` periodically for confidence that it hasn't drifted into a bad state. Goal: stop needing manual restarts. Two options discussed: (1) run a second process that restarts fresh every morning alongside the long-running one, diff their signals to build confidence the long-running one hasn't drifted; (2) make the long-running process itself controllable via the Slack socket (already Socket Mode) so it can take a "revalidate/restart" command without killing the OS process. Not urgent — manual restart is fine for now — but risk is forgetting to restart it one day, so don't let this drop.

- **Half-day trading sessions not handled**: `_SIGNAL_WINDOWS` (10:25-10:40, 15:25-15:40 ET) assumes a full session. On early-close days (day before Thanksgiving, Christmas Eve, etc.) the market closes ~1pm, so the 15:25 window never happens — a TIME exit that would've fired then just doesn't trigger until the next real session. Low priority, but a real gap in `_in_buy_window`/exit-check timing.

- **Parameter selection workflow**: After enough sweeps, need a way to review results across tickers and select parameter sets to trade — currently manual via logs/heatmaps.
- **`trading_engine.py` cleanup**: Either retrofit or replace with Layer 3 implementation; currently points at legacy files.

## Low Priority / Ideas

- **Node version-change reminder in Slack alerts**: When a live watchlist node's version/params change day-over-day (e.g. AGQ v1.5→v1.6 swap on 2026-07-01), flag it explicitly in the alert instead of relying on the version tag alone. Low priority now — version tag already shown, gap was just not connecting it in the moment. Revisit if watchlist grows large enough that version swaps become frequent/hard to track manually.

- **Alternative trading windows**: Explore hourly bar closes beyond 9:30 and 14:30 (e.g. 11:30, 12:30, 1:30) — requires expanding `target_hours` and re-sweeping.
- **Chaos monkey / floor alpha**: Re-run backtest with worst-case execution — entry at highest price in N bars after signal, exit at lowest price in N bars after exit signal. Produces `floor_alpha` metric. Nodes with positive floor alpha are robust to real-world execution delays. Store alongside `alpha_vs_spy` in DB and surface in Winners page.
- **Alpha robustness — drop top N trades**: Re-run backtest dropping the top 3 best-performing trades and recalculate alpha. Tests whether alpha is structural or lucky.
- **Automated exits**: Exits (TP/SL/TIME) are deterministic and the primary candidate for brokerage API automation. Manual exit of multiple simultaneous positions is operationally risky. Entries stay manual.
- **Broader ticker universe**: `results.csv` (999 rows, mixed leveraged/non-leveraged) has liquidity data for non-leveraged ETFs. Import and sweep to increase signal frequency.
- **Half-Life of Reversion**: Fit an Ornstein-Uhlenbeck process per ticker to estimate mean-reversion speed. Use to inform `max_hold_hours` sweep range per ticker. One offline computation per ticker, surface in Screener.
- **Hurst/ADF as entry filter**: ✅ Researched (`pages/7_Hurst_Filter.py`, `pages/8_ADF_Filter.py`). Verdict: not actionable on current dataset — see `docs/research.md`. Revisit for v1.6 if dataset changes.
- **Advanced indicators**: Dataset size allows pre-computing Bollinger Bands, ATR, MACD etc. instantly via TA-Lib or pandas-ta.
- **Basic ML experimentation**: Dataset small enough to train Random Forests or XGBoost on CPU in seconds.
- **Multi-ticker signal dashboard**: View current z-score signals across the full ticker universe in one place.
- **Position sizing model**: Layer 3 will eventually need a position sizing model.
