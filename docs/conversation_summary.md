# Session Cache

Handover notes between Claude sessions. Append a new entry on session close. Most recent first.

---

## 2026-07-01 — Multi-watchlist, live/research modes, MULL corporate action investigation

### What we did

- **Multi-watchlist support** (committed `6d4ab1c`): `watchlists` table with named profiles; one is_active drives signal loop. `watch_list` gains `watchlist_id` + `mode` (live/research). Migration runs automatically on next `ensure_tables()` call — existing 12 nodes moved to 'main'. Active signals suppresses Slack for `mode='research'` nodes (console-only).
- **Winners page**: sidebar watchlist picker, create/delete/set-active controls, Mode selectbox in data editor.
- **Portfolio page**: sidebar watchlist picker.
- **Watchlist backup**: `cache/watchlist_backup_20260701.json` — 12-node snapshot before trimming.
- **MULL corporate action investigation**: MULL had a 25:1 forward split on 2026-06-26. yfinance daily and cache are correctly split-adjusted (÷25); the "97% drop" on Google Finance is Google showing unadjusted prices. yfinance hourly has a bug where it returns 25x higher prices for pre-split dates vs daily — cache is on the correct scale and internally consistent. Backtesting results valid.

### Current State

- Watchlist: 12 nodes in 'main' (all mode=live). User was trimming to 7 — not yet done.
- run_optimization_sweep.py has uncommitted changes from prior session (Phase 2.5, island check fix).
- `docs/session_cache_addendum.md` still untracked — contains prior session notes, can be deleted or committed.

### Next Session

1. Trim watchlist to 7 nodes (use Winners page → uncheck Watch, or sidebar picker)
2. Decide between AGQ v1.5 (w=10 z=2.0 TP=19 SL=8 hold=133h) vs v1.6 (w=20 z=1.0 TP=28 SL=11 hold=140h)
3. Mark some nodes as 'research' vs 'live' once list is trimmed
4. Commit run_optimization_sweep.py (Phase 2.5 + island check fix)
5. Fix Phase 2.5 to sweep all 3 island centers × all (w,z) combos (backlog item added)
6. Review MULL/VRTL/NBIZ single-stock nodes before trading

---

## 2026-07-01 (addendum) — v1.6 Sweep Execution + Cliff Analysis

### What we did

- **v1.6 sweep completed overnight**: 358 tickers coarse, 30 island mesh (top 25 index + 5 other), cliff check at checkpoint 2. Only 1 ticker (WULX) survived cliff check for full mesh. No index-underlier tickers passed. WULX full mesh done, heatmap at `logs/topology_WULX_ZScoreBreakout.png`.
- **SMST full mesh**: Launched separately (`run_smst_full.py`, PID 102868, log: `logs/smst_full.log`). SMST had best_alpha +2188% but worst_neighbor -97.7% (cliff) — full mesh running to see the complete topology.
- **v1.5 cliff check**: Ran against full 1-30 data. 25/340 tickers safe. Top safe: VRTL (+721%), WULX (+515%), CIFG (+458%), GEVX (+284%), CRDU (+261%). Watchlist tickers (AGQ/EDC/FAS/HIBL) not in safe list.
- **Cliff check design note**: `CLIFF_RADIUS=2` = ±2 integer steps in TP and SL = 5×5=25-node box. `AND trades > 0` excludes NO_TRADES nodes from cliff detection — may miss edges where signal disappears.
- **Sweep run registry**: Discussed — `sweep_runs` DB table to snapshot config + notes per run. Not yet built.
- **Backlog updated**: v1.6 marked done, sweep registry + cliff improvements + Universe Scan pending items added.

### Key Findings

- Most high-alpha tickers are single-node spikes, not plateaus — only 7% pass cliff check on v1.5 full data
- WULX and VRTL are the strongest cliff-safe candidates from v1.5
- Cliff check brutality is a feature: protects against overfitting to lucky single nodes
- v1.6 coarse+island is fast enough (2.5h for 358 tickers at 10 workers) to run regularly

### Next Session

1. Check SMST full mesh result (`tail logs/smst_full.log`) — see if its topology is wide or spiky
2. Run full mesh on v1.5 cliff-safe tickers (VRTL, CIFG, GEVX, CRDU, HUTG) at z=[1.0, 1.5]
3. Build sweep run registry (`sweep_runs` DB table + sweep engine integration)
4. Update Universe Scan safety score to worst-neighbor min + color coding
5. Commit all session changes

---

## 2026-07-01 — v1.6 Sweep Design + Universe Scan Page

### What we did

- **Feature wrap**: committed Open Positions (signal price + drift %), Portfolio research nodes, Node Inspector Hurst opt-in, Top Pivot z-threshold expansion, sweep cache fix.
- **v1.6 sweep design**: Validated coarse-odd approach against v1.5 full data. Step-3 grid [3,6,...,30] recovers true peaks with Δ=0% in 10/10 test cases at ~28% of full node count.
- **Three-phase sweep architecture** designed:
  - **Phase 1 — Coarse** (all 358 tickers): TP/SL [3,6,...,30], all hold/window/z → 2,864,000 nodes
  - **Phase 2 — Island mesh** (top 25 index + top 5 non-index by coarse alpha): 3 islands ± 4 radius per ticker → 343,200 net new nodes
  - **Phase 3 — Full mesh** (top 5 index + top 5 non-index): complete 1-30 TP/SL grid → 525,600 net new nodes
  - **Total: 3,732,800 nodes** — fits in 4,320,000 capacity (200/s × 6h) with 14% headroom
- **Alpha gate**: rank-based — if not in top 50 by coarse alpha, skip island mesh. Top 5+5 for full mesh.
- **Cliff detection gate** (CRITICAL): Phase 3 selection is NOT just top-5 by coarse alpha. After Phase 2, compute worst-neighbor alpha from island mesh data. Filter out tickers where worst_neighbor < 0 (cliff risk). Top 5 index + 5 non-index selected from cliff-free survivors only.
- **Universe Scan page** (`pages/11_Universe_Scan.py`): built — shows coarse scan results for all tickers, liquidity (max notional = avg_vol × price × 1%), underlier type (index/stock), flags (LOW_LIQ / TOP_IDX / TOP_STK / REFINE), safety score (positive-neighbor count), full universe table with toggle.
- **config.json → v1.6**: 358 tickers (full v1.5 universe), z=[1.0, 1.5], TP/SL coarse [3,6,...,30], max_workers=10, max_generations=0.
- **run_optimization_sweep.py**: max_workers now reads from config instead of hardcoded 6.

### Key Decisions

- Coarse step-3 (not step-2/odds) — 11% of nodes vs 25%, same recovery quality
- 25 index + 5 non-index for island mesh; 5+5 for full mesh — fits budget
- Phase 3 requires cliff check from Phase 2 — sweep must checkpoint between phases, not run linearly
- z=2.0 NOT in v1.6 config — already fully swept in v1.5

### Next Session

1. **Build multi-phase sweep logic** in `run_optimization_sweep.py`:
   - Phase 1: coarse scan, save to DB as v1.6
   - Checkpoint: rank by coarse alpha, identify top 30 (25 index + 5 non-index) from `tickers` table underlier classification
   - Phase 2: island mesh for top 30
   - Checkpoint: compute worst-neighbor safety score, filter cliff tickers, pick top 10 survivors
   - Phase 3: full mesh for top 10
2. **Update Universe Scan page** safety score to use worst-neighbor (min neighbor alpha) rather than count, and color-code (green plateau / red cliff)
3. **Fine mesh trigger button** in Universe Scan page (top 25 only)
4. Commit `pages/11_Universe_Scan.py`, `config.json`, `run_optimization_sweep.py`

---

## 2026-07-01 — z=1.0/1.5 Sweep, Portfolio Rework, Coarse Grid Validation

### What we did

- **Hurst/ADF cleanup**: Removed pre-warm from Node Inspector; Hurst analysis now opt-in behind checkbox. Removed dead Hurst/ADF columns from Portfolio (were always NaN). Backlog updated.
- **Backlog cleanup**: Removed completed items, removed "full leveraged universe" item (already have 300+), consolidated Hurst/ADF into single done item.
- **Open Positions page**: Added `Signal $` and `Drift %` columns (entry price vs signal price).
- **v1.5.1 sweep (z=1.0/1.5, watchlist tickers)**: Ran full fine grid (TP/SL 1-30, w=10/20) for AGQ/EDC/FAS/HIBL. Fixed cache check bug (was counting all z-thresholds together). Renamed z=1.0/1.5 rows to v1.5.1. Accidentally deleted w=30 rows — lesson: fix the query, not the data. Cache check now also scopes by window.
- **Portfolio rework**: Supports mix-and-match node selection. Watchlist toggle + Research expander (version picker, filters, top nodes table, multiselect). Nodes from any version can be combined in one Gantt view.
- **Exit window analysis**: Exits can fire at any bar close, not just signal windows. Quick model showed restricted exits costs 0-22% compounded return. Not worth operationalizing.
- **Coarse grid validation (v1.5)**: Even-number grid (2,4,6,...,30) finds islands reliably. Every-3 missed EDC entirely (TP=17 sits between 15 and 18). Decision: even numbers for discovery, full fine grid for confirmed winners only.
- **Versioning convention**: 1.x = strategy family, 1.x.y = run variation. v1.5 = z=2.0 fine grid. v1.5.1 = z=1.0/1.5 fine grid (watchlist 4).
- **Z=1.0/1.5 findings**: HIBL z=1.0 w=20 TP=8 SL=6 strong (75 trades, 60% win rate, 629% alpha). AGQ z=1.0 dangerous (sustained downtrend). Cherry-picking watchlist tickers for z=1.0 isn't fair — need to sweep broader universe.
- **Top Pivot fix**: Added z=1.0/1.5 to `Z_THRESHOLDS`. Requires Streamlit cache clear after DB refresh.

### Key Decisions

- Even-number coarse grid is the standard for discovery sweeps going forward
- w=30 in coarse pass only; w=10/20 for fine grid
- Non-single-stock universe ≈ 78 tickers (filter criteria to rediscover next session)
- Capital deployment strategy: explore z=1.0/1.5 on broader universe when z=2.0 signals are quiet

### Next Session

1. Rediscover the ~78-ticker filter criteria
2. Set up even-number coarse grid sweep for non-single-stock universe at z=1.0/1.5
3. Validate coarse vs fine for v1.5.1 once sweep completes
4. Check sweep status (was still running: HIBL z=1.5 in progress)
5. Refresh Streamlit cache to see v1.5.1 in UI

---

## 2026-06-30 (session 3) — Cache/Index Sweep, DB Pruning, Top Pivot Navigation

### What we did

- **Top Pivot overhaul**: Replaced selectbox + buttons with HTML table (`st.html`) where each backtest cell (w=10/20 z=2.0) is a clickable link directly to Node Inspector. Links encode full node params (ticker, version, window, z, TP, SL, hold) via URL query params. Node Inspector reads `st.query_params` as fallback when session state is empty.
- **`_load_dropdown_opts` cache fix**: Was missing `@st.cache_data` — hit DB on every rerun. Added `ttl=86400`.
- **Double decorator bug**: Node Inspector had two stacked `@st.cache_data` decorators on `_load_dropdown_opts` and `run_cached_backtest`. Fixed to single decorator each.
- **Cache sweep**: Added `@st.cache_data` to 9 uncached DB/file functions across 6 pages (Spatial Topology `load_dropdown_options`, Winners `load_versions`/`load_ticker_strategy_options`/`load_results`, Portfolio `load_watchlist`/`load_hourly`, Sweep Status `load_versions`/`get_data_date`, Open Positions `load_positions`).
- **TTL sweep**: Changed all version-keyed backtest data functions to `ttl=86400` across all pages. Price/computation functions left at shorter TTLs.
- **DB indexes**: Added `(version, ticker)`, `(version, window)`, `(version, ticker, strategy)` indexes on `backtest_cache`. Applied to live DB.
- **`load_best_nodes` KV persistence**: Added `refresh_best_nodes_cache()` to `db_cache.py`. `load_best_nodes` now checks KV store first — survives Streamlit restarts without re-running window function query. Populated for all versions.
- **DB pruning**: Deleted z=2.5, z=3.0, w=30 rows + all of v1.2/v1.3/v1.4. 76M → 13M rows, 21GB → 3.5GB after vacuum. Archive saved at `cache/trading_universe_archive_20260630.db`.
- **WINDOWS/Z_THRESHOLDS**: Updated to `[10, 20]` and `[2.0]` in Top Pivot constants and pivot query.
- **Strategy discussion**: Decided `LimitOrderZScoreBreakout` will be a separate class (not inherited from ZScoreBreakout) — entry price is fundamental to P&L chain. Will share band/signal calculation via utility. Gets its own v1.6 sweep.

### Key Findings

- `on_select="rerun"` for `st.dataframe` does not work in this environment — clicking cells does nothing. Workaround: HTML table with `<a href>` links using `target="_top"`.
- `@st.cache_data` is in-memory only — clears on Streamlit restart. Version-keyed backtest data should also be persisted in KV store (SQLite) for restart resilience.
- Pruning z=2.5/3.0 and w=30 removed 83% of rows — confirmed those params almost never yield good signals.

### Current State

- DB: v1.5 only, 13M rows, 3.5GB. Archive at `cache/trading_universe_archive_20260630.db`.
- Watchlist: AGQ w=10 TP=19 SL=8, EDC w=10 TP=17 SL=17, FAS w=10 TP=25 SL=10, HIBL w=10 TP=29 SL=21

### Next Session

1. Design and implement `LimitOrderZScoreBreakout` strategy + v1.6 sweep
2. Revisit Top Pivot sort/filter (HTML table has JS sort on column headers; may want more)
3. Apply DB indexes when app is idle (was locked during session)

---

## 2026-06-30 (session 2) — Morning Report, Sweep Fixes, Open Positions Page

### What we did

- **Node Inspector commit**: Watchlist table now shows Return%, Alpha%, Asset B&H, SPY B&H, B&H Mult, Trades, Win% inline. Height auto-sizes. (`pages/2_Node_Inspector.py`)
- **Open Positions page** (`pages/10_Open_Positions.py`): Reads `open_positions` DB table, fetches current price via yfinance, shows entry/current price, unrealized P&L%, TP/SL prices, hours held/remaining.
- **Sweep skip optimization**: `run_master_evolutionary_suite` now does a single COUNT query before CSV reads to skip fully-cached tickers — eliminated ~15 min waste at sweep start.
- **max_workers**: Changed 6→10→6 (10 caused no improvement due to SQLite WAL issue; back to 6 to leave cores for active_signals).
- **WAL incident**: Stale process (PID 256511, python3 -c inline SQLite query from prior session) held a read lock for 592 hours, blocking WAL autocheckpoint. WAL grew to 30GB. Fixed by killing stale process + db_cache.py (also stuck for 3h). WAL flushed on next connection. Root cause: correlated subquery ran without index before idx_bc_version_ticker_z_return existed.
- **config.json**: Stripped ~200 completed tickers. Only incomplete tickers remain (~83 → now fewer after sweep continued).
- **Data collector cron**: Moved from 8 AM to 6:30 AM so daily bars are fresh before 7 AM morning report.
- **Morning report — daily 7 AM**: `send_startup_report` now fires daily at 7 AM ET via poll loop (tracks `last_morning_report_date`). No restart needed.
- **Morning report — overnight change**: Shows `now $X.XX (+Y.Y% O/N)  close $Z.ZZ` using `yfinance history(prepost=True)` for current price and `df_daily.iloc[-1]` for prev close (no date filter — picks up most recent completed session).
- **Morning report — data date**: Shows `data MM/DD` = last daily bar date used for bands so you can confirm freshness after 6:30 AM cron.
- **BUY message**: Added `max $Xk / Y shares @ 1% vol` from `avg_vol_10d` in tickers table (liquidity ceiling).

### Key Findings

- `fast_info.last_price` = regular session close only — misses pre/post-market. Use `history(prepost=True)` instead.
- `fast_info.previous_close` is inconsistent post-market (sometimes returns today's close, sometimes yesterday's). Use `df_daily.iloc[-1]` instead.
- `df_daily[df_daily.index < today]` excludes today's bar because resample index = midnight. Use `df_daily.iloc[-1]` for prev_close (no filter).
- WAL autocheckpoint works correctly but is blocked by any long-lived reader. Kill stale python3 -c processes after sessions.
- SQLite correlated subquery without index is O(n²) — will hang indefinitely on 20M+ row table. Index exists now (`idx_bc_version_ticker_z_return`) but needs a covering index on `(version, ticker, strategy_return DESC)` for that specific pattern.

### Current State

- Watchlist: AGQ, EDC, FAS, HIBL — no open positions
- FAS at +2.2% from trigger (🔶) — set alarm for 10:28 and 15:28 tomorrow
- Sweep running on remaining ~83 tickers; active_signals.py running (PID 269226)
- Morning report will auto-fire at 7 AM ET

### Next Session

1. Verify 7 AM morning report fires correctly with fresh bands (check `data` date = 06/30 after 6:30 AM cron)
2. Add covering index `(version, ticker, strategy_return DESC)` to prevent O(n²) query hangs
3. Run db_cache.py after sweep completes
4. Check sweep completion status

---

## 2026-06-30 (addendum) — Live Execution Design, Slack Redesign, Watchlist Trim

### What we did

- **Open-fill analysis**: Ran across all 17 watchlist tickers. Open-fill (9:30 bar open as entry) is consistently worse than 10:30 close — selection bias (bars only selected when close <= lower_band). Conclusion: market order at 10:30 matches backtest entry best.
- **Real-time price**: `compute_buy_signal` now uses `yfinance fast_info.last_price` instead of last cached hourly close. Fallback to cache on failure.
- **Signal time-gating**: Buy and sell signals only evaluated in windows 10:25–10:40 AM and 15:25–15:40 PM ET, matching backtest `target_hours=(9,14)` (9:30 bar close at 10:30, 14:30 bar close at 15:30). Outside windows, loop idles.
- **Execution workflow documented** (`docs/operational_limits.md`): Stage limit order pre-market at absurd price, edit to market at 10:30/15:30 when Slack fires. No overnight limit orders at lower_band (open-fill analysis showed this is worse).
- **Startup report redesigned**: Block Kit with 🔶/🟡/⚪ proximity emoji, sorted by % to trigger, open positions section with P&L, reconfirm reminder for hot tickers (< 5% away).
- **BUY message redesigned**: Two-line action card — `🟢 FAS — BUY — Market — $148.12 — 337 shares (~$50k)` / `🔴 FAS — SELL ALL — Stop Loss — $128.12 (-11% from trigger)`. Stop loss at lower_band × (1 - (SL% + 1%)) — 1% buffer over backtest SL for intraday noise protection. Intrabar false trigger rate confirmed very low (0.0–0.3% of bars).
- **SELL messages redesigned**: TP → cancel stop loss, sell market. SL → check account, should have auto-filled. TIME → change stop loss to market close order.
- **Portfolio page**: Ticker multiselect to toggle tickers on/off, full watchlist expander, TQQQ normalized price overlay alongside SPY, Hurst/ADF computation commented out for speed.
- **pages/9_Entry_Delay.py**: Entry delay analysis across all watchlist tickers.
- **open_fill_analysis.py**: Standalone open-fill vs backtest return script.
- **Watchlist trimmed** to AGQ, EDC, FAS, HIBL (top 4 by alpha/island quality). Others remain in DB.
- **Alternative trading windows** added to backlog.

### Key Findings

- Open-fill is always worse than 10:30 close — not a bug, just selection bias
- Intrabar SL false trigger rate: 0.0–0.3% across all 4 tickers — tight Schwab stop is fine
- Portfolio peak concurrent positions: up to 14 (all correlated — same macro event). Trimmed to 4 tickers to manage.
- 🔶 in morning report = set phone alarm for 10:28 and 15:28

### Current State

- Watchlist: AGQ w=10 TP=19 SL=8, EDC w=10 TP=17 SL=17, FAS w=10 TP=25 SL=10, HIBL w=10 TP=29 SL=21
- FAS at +2.9% from trigger — 🔶 tomorrow morning
- No open positions

### Next Session

1. Build Streamlit open positions page
2. Commit `pages/2_Node_Inspector.py` changes (not staged this session)
3. Check sweep status
## 2026-06-29 (addendum 2) — Watchlist Expansion, DB Indexes, Entry Delay Analysis

### What we did

- **docs/research.md**: Created. Captured Hurst/ADF filter findings and sweep parameter conclusions (was left in session_cache by predecessor).
- **Watchlist expanded to 17 tickers**: Added KORU, HIBL, SOXL, TQQQ, NAIL (top 5 by return), then corrected via Top Pivot download — KORU/SOXL/NAIL don't beat B&H (B&H mult < 1.0x, filtered by Top Pivot). Left them on watchlist anyway (user curious about signals). Added URTY, DUSL, TNA, DRN, OILU, CURE, MIDU from Top Pivot list (user removed TQQQ, GDXU, JNUG from download).
- **Watchlist versions**: Updated all v1.4 → v1.5.
- **DB index added**: `idx_bc_version_ticker_z_return` on `(version, ticker, z_score_threshold, strategy_return DESC)`. Took 220s to build on 45M rows.
- **PK fix in Node Inspector**: Watchlist metrics query now includes `strategy='ZScoreBreakout'` to hit PK instead of falling back to `idx_bc_ticker` scan.
- **Node Inspector watchlist table**: Now shows Return%, Alpha%, Asset B&H, SPY B&H, B&H Mult, Trades, Win% inline. Height auto-sizes to row count. Metrics cached via `load_watchlist_metrics()`.
- **active_signals.py startup report**: `send_startup_report()` fires at startup, posts Slack table with current price, buy trigger (lower_band), z-score, TP price, SL price per ticker.
- **pages/9_Entry_Delay.py**: New page. For each watchlist node, runs backtest then replays each trade with entry delayed 1-4 hours. Shows compounded return and missed trade count per delay. Finding: delayed entry is consistently terrible — strategy selects against fast mean-reversions.
- **Limit order analysis (AGQ)**: 9 of 18 AGQ trades fire at 9:30 bar. Open fill (using 9:30 open as entry) gives 311% vs 372% backtest — about 62% lower compounded return. Limit order fills between open and close (at lower_band), so real performance is between 311-372%. Interesting stat to run across all tickers.
- **backlog**: Added v1.6 open-price entry model. Removed FAS watchlist removal item (user decided to keep). SPY/VIX filter added as next research direction.
- **docs/design.md**: Updated with pages 7/8, shared hurst.py module, max_workers=6, sweep auto-cache, cron.

### Key Findings

- Delayed entry is bad: selecting for trades that didn't bounce fast = selecting losers
- Earlier entry (limit order at open) is better than waiting for 10:30 close
- But open fill still ~17% worse than backtested return for AGQ (compounded)
- Limit order at lower_band placed night before is valid execution approach
- 9:30 bar open is typically 1-3% above the 10:30 close (entry price in backtest)
- Most 9:30 trades gap through the limit price — fill at open, not exactly at lower_band

### Next Session

1. Run open-fill analysis across all 17 watchlist tickers (AGQ showed -62% compounded vs backtest — is this typical?)
2. Commit pending changes (active_signals.py, Node Inspector, pages/9, docs/)
3. Check sweep status — 30 U-Z tickers remaining
4. Run db_cache.py after sweep completes

---

## 2026-06-29 (addendum) — Hurst/ADF Research, Node Inspector Perf, Sweep Config

### What we did

- **Hurst bug fixed**: `np.var` → `np.mean(x**2)` in `_hurst_vectorized`. Was stripping trend component, making everything look mean-reverting. Values now correctly go above 0.5 during trending periods.
- **hurst.py**: Extracted `_hurst_vectorized` + `ROLLING_WINDOW=200` to shared module. Node Inspector imports from there. `active_signals.py` ADF window also updated to 200.
- **test_hurst.py**: Synthetic fBm sanity checks — random walk ≈0.5, trending >0.55, mean-reverting <0.45. All pass.
- **Node Inspector perf**: Fixed 7-13s load time caused by uncached `DISTINCT ticker` query on 45M rows. Added `_load_dropdown_opts()` with `@st.cache_data`. Added `@st.cache_data` to `run_cached_backtest`. Pre-warm limited to watchlist-only (was 314 tickers). Added `get_kv` import. Suppressed trades now show grey vrects.
- **Top Pivot**: Added "Exclude index" toggle (98 tickers tagged in `index_underlier`).
- **db_cache.py**: `DB_PATH` now uses `__file__`-relative path so cron job works from any directory.
- **run_optimization_sweep.py**: Auto-runs `refresh_dropdown_cache()` + `refresh_pivot_cache()` on sweep completion. `max_workers=6`. Cron job added at 4:15am daily.
- **config.json**: Expanded to 357 tickers for next sweep. Sweep ran to ~328/357 before kill (stopped at NFLU). Remaining: USAX→ZSL (30 tickers, all U-Z).
- **pages/7_Hurst_Filter.py**: Sweep Hurst filter (MR vs MO) across all qualifying nodes. Result: MO (momentum, H≥cutoff) helps 43/87 nodes vs MR 31/87. Weak signal.
- **pages/8_ADF_Filter.py**: Same for ADF p-value. Non-stationary filter (p≥cutoff) showed benefit on AGQ, DPST, EDC, FAS but not LABU. Fixed-cutoff test on FAS showed cherry-picking — all fixed cutoffs worse than base.

### Key Findings
- Hurst/ADF as entry filters: not actionable. Lag problem — can't detect regime change in time. At-entry regime is backward-looking and doesn't predict trade outcome reliably.
- Slight lean toward momentum entries (H≥0.5, non-stationary) but sample sizes too small (18-24 trades) to be confident.
- w10 z3.0 maxes at 4 trades over 2 years — too rare to trade. z2.0 is the real edge.
- w30 has no qualifying nodes for non-single-stock — likely trend drift kills mean reversion at that timescale.
- SVXY (inverse VIX): 93% return, 3.6× B&H, 18 trades at w20 z2.5 — marginal, too volatile.

### Current State
- Watchlist: AGQ, DPST, EDC, FAS, LABU (all v1.4 — needs update to v1.5)
- active_signals.py running
- 30 tickers remaining in v1.5 sweep (U-Z)
- Uncommitted changes staged, commit pending

### Next Session
1. Commit pending changes
2. Update watchlist versions v1.4 → v1.5
3. Run remaining 30 tickers: `.venv/bin/python3 run_optimization_sweep.py 2>&1 | tee -a logs/sweep_v15_full.log`
4. Run `db_cache.py` after sweep
5. SPY trend / VIX level as entry filter — next research direction

---

## 2026-06-29 — Portfolio Page, Pivot Cache, Signal Improvements

### What we did

- **Portfolio page** (`pages/4_Portfolio.py`): Gantt chart of all watchlist node trades on a shared x-axis with SPY price overlay and concurrent-positions step chart. Hurst + ADF sliders filter trades by regime at entry time — lets you see if regime filtering improves per-trade avg return. Summary metrics bar (trades, win rate, avg return, avg win, avg loss, avg hold, max concurrent) + per-node table with unfiltered vs filtered columns side-by-side.
- **Pivot cache** (`db_cache.py` `refresh_pivot_cache()`): Pre-aggregates Top Pivot data per (ticker, window, z, trades) into `kv_cache`. Page load now hits a key lookup instead of scanning 49M rows. Fallback to SQL if cache miss. Run `.venv/bin/python3 db_cache.py` after each sweep to refresh both dropdown and pivot caches.
- **Top Pivot cache integration** (`pages/0_Top_Pivot.py`): `load_pivot()` checks `kv_cache` first; `min_trades` filter applied in pandas on cached cell data.
- **z_score_threshold bug fixed** (`active_signals.py` line 311): `compute_buy_signal()` was hardcoding `2.0` in strategy constructor and `lower_band` calculation. Now uses `node['z_score_threshold']`.
- **Hurst + ADF in BUY signal**: At signal time, pulls latest Hurst from `hurst_cache` and computes ADF fresh on last 420 hourly bars. Both shown in console print and Slack `_fields_block`.
- **Removed `docs/handover.md`**: Was stale and duplicating DB state. `go` now reads last ~60 lines of `session_cache.md`. `session close` appends here only.
- **venv fix**: All python commands need `.venv/bin/python3`, not bare `python3`.

### Key Decisions

- Portfolio Hurst/ADF sliders show regime filter effect vs baseline — useful for deciding whether to use regime filter in live trading
- ADF computed fresh at signal time (fast enough for one ticker); no dedicated cache needed yet
- Hurst/ADF screener columns: will hook into data download pipeline rather than single scalar (regime-dependent); pending design decision on aggregation
- Pivot cache stores per-(ticker, window, z, trades) granularity so any min_trades value can be filtered in Python

### Current State

- Watch list: AGQ w=10 TP=19 SL=8 hold=133h, DPST w=10 TP=21 SL=12 hold=126h, EDC w=10 TP=17 SL=17 hold=112h, FAS w=10 TP=25 SL=10 hold=133h, LABU w=20 TP=21 SL=18 hold=84h
- Sweep v1.5: z=2.0 done, z=2.5 done, z=3.0 ~50% (33 tickers missing)
- No open positions. Ready to go live tomorrow with `active_signals.py`.

### Next Session

1. Start live signal monitoring: `.venv/bin/python3 active_signals.py`
2. Restart z=3.0 sweep: `.venv/bin/python3 run_optimization_sweep.py 2>&1 | tee -a logs/sweep_v15_full.log`
3. Run `.venv/bin/python3 db_cache.py` after sweep completes
4. Hurst/ADF screener column design: hook into data download, decide on aggregation approach
5. Position sizing in Slack BUY signal (data already in `tickers` table)

---

## 2026-06-28 (session 3) — Performance, Top Pivot, Config Cleanup, Hurst 60d

### What we did

- **Hurst vectorized**: Replaced Python loop in `rolling_hurst` with `sliding_window_view` + batch `lstsq`. Now computes every bar (step=1) instead of every 12. Off-by-one fixed (`[:-1]` on windows). ~10-50× faster on first load.
- **Hurst caching**: `hurst_cache` DB table stores full rolling series for watchlist tickers, persists across sessions. Non-watchlist tickers use `st.session_state` only. Staleness check vs CSV max timestamp. Pre-warm on Node Inspector load: watchlist from DB, >200% return tickers from session_state.
- **Hurst window changed to 60d** (was 30d). `ROLLING_WINDOW = 60 * 7 = 420`. Old 30d cached rows cleared and recomputed for all 5 watchlist tickers.
- **Node Inspector `@st.fragment`**: Slider + chart + metrics wrapped in fragment. Only the fragment reruns on slider drag — no data loading, backtest, or Hurst recomputation. H_at_entry pre-computed outside fragment (depends only on h_series, not cutoff).
- **Hurst filter finding**: Only 1 watchlist ticker showed improvement from the filter. Hurst may be better as a screener (ticker quality gate) than a per-trade filter. 60d window recomputed to recheck.
- **Winners perf**: Added `idx_bc_version_return` index on `(version, strategy_return)`. WAL mode enabled. Dropdown options moved to `kv_cache` DB table (persistent across server restarts). `load_results` TTL kept at 60s (filtered differently each call).
- **Spatial Topology perf**: `load_dropdown_options` replaced — now reads from `kv_cache` instead of `SELECT DISTINCT version, ticker, strategy FROM backtest_cache` (full 48M row scan). `load_slice` TTL raised to 3600s.
- **`db_cache.py`**: New module with `get_kv`/`set_kv` (JSON key-value in `kv_cache` table) and `refresh_dropdown_cache()`. Run as script to populate. Covers versions, tickers, strategies for Winners + Topology. Cache populated.
- **Top Pivot page** (`0_Top_Pivot.py`): New page. Pivot of best return per (ticker, window=10/20/30, z=2.0/2.5/3.0) = 9 cells + max + alpha + bh_mult. SQL-side GROUP BY (not Python). Filters: min trades, min return, min alpha, min B&H mult, exclude single-stock toggle. Editable "Underlier" text column — type stock symbol to mark as single-stock, saves to `tickers.stock_underlier`. Row selection → "View in Winners" (pre-filters ticker) or "Open in Node Inspector".
- **Single-stock filtering**: `tickers.stock_underlier` column used as quality gate. 233 of 357 original config tickers flagged as single-stock (mostly single-stock leveraged ETFs like NVDL, TSLL, AMDL etc).
- **config.json rebuilt**: 71 tickers ordered: watchlist (5) → top returners non-single-stock (42) → everything else non-inverse non-single-stock (24). Inverse/bear ETFs removed (34 flagged in `tickers.inverse=1`). VOO removed. Crypto (BITU, BITX, BTCL, ETHU, ETHT) kept — Bitcoin did well in sweep.
- **Backlog updated**: Two-phase UX rethink added (discovery vs optimization, node-centric vs ticker-centric).

### Key Decisions
- Single-stock leveraged ETFs excluded from sweep for now — optimize non-single-stock first.
- Inverse/bear ETFs excluded — strategy is long-only mean reversion.
- Crypto kept — historical results were reasonable.
- Hurst as per-trade filter has limited value for most watchlist tickers. More useful as screener column.
- `kv_cache` pattern for expensive dropdown queries; `hurst_cache` pattern for expensive per-ticker time series.

### Pending
- Hurst filter reassessment with 60d window — re-check which watchlist ticker improved
- FAS watchlist removal still pending decision
- LABU SL=9 vs SL=18 — wait for more z=2.5/3.0 data
- `refresh_dropdown_cache()` not yet scheduled (manual for now)

---

## 2026-06-28 (session 2) — DB PK Fix, Node Inspector Rebuild, Winners Fixes

### What we did

- **backtest_cache PK bug found and fixed**: `z_score_threshold` was missing from the PRIMARY KEY. `INSERT OR REPLACE` for z=2.5 silently overwrote z=2.0 rows (same PK). Sweep was generating 162k total nodes but finding only 54k cached (1 z-threshold worth) every restart. Root cause: table predates the column — `CREATE TABLE IF NOT EXISTS` never re-ran to update PK; `ALTER TABLE ADD COLUMN` can't change PKs.
- **DB migration**: Killed sweep + Streamlit. Rebuilt `backtest_cache` with correct PK (includes `z_score_threshold`). Copied all existing data. Replaced all v1.5 z=2.0 rows with v1.4 z=2.0 data (valid: the hardcoded z=2.0 bug means v1.4 z=2.0 = correct v1.5 z=2.0). Result: 17.9M z=2.0 rows, 36.6k z=2.5 (AGQ partial), 9.0M z=3.0 (141 tickers complete).
- **watch_list schema**: Added `z_score_threshold REAL DEFAULT 2.0` column. `ensure_tables()` now auto-migrates. `add_node()` accepts and stores it. Winners page passes it when adding to watchlist. Watchlist display now shows Z Thresh column.
- **Winners page**: Fixed Return/Alpha/etc columns sorting as strings — replaced string-formatting lambdas with `st.column_config.NumberColumn(format=...)` so underlying values stay numeric.
- **Node Inspector full rebuild**: Watchlist at top (clickable to pre-fill params). Price chart with Bollinger bands at z=2.0/2.5/3.0. Trade entry/exit markers + win/loss shading. Rolling Hurst (30d) subplot. Rolling ADF p-value (checkbox-gated). Hurst filter slider with suppressed trade markers and side-by-side metrics. All heavy computation cached by ticker+params.
- **Alembic discussion**: Decided not to adopt — SQLite + single-dev, additive-only schema changes, git history is sufficient audit trail.
- **Sweep restarted** by user after migration. Resume point: AGQ ~94k unvisited (down from 108k pre-fix).

### Key Decisions
- Node Inspector = optimization/validation view. Spatial Topology = island finding. Portfolio view (future) = tradability / capital requirements.
- Hurst stays as dynamic post-filter (slider), not a sweep dimension — they answer different questions.
- ADF gated behind checkbox (slow on first load, fast after cache warms).

### Current State
- Sweep running (user's terminal), resuming z=2.5 pass across all tickers
- Streamlit running PID 222878
- DB: PK correct, z=2.0 complete (v1.4-filled), z=2.5 in progress, z=3.0 done for 141 tickers

---

## 2026-06-28 — Backtester Bug Fix, Hurst Analysis, Overnight Sweep

### What we did
- **Critical bug fixed**: `backtester.py` Numba kernel had `sma - std * 2.0` hardcoded — `z_score_threshold` was stored in the DB tag but never affected the simulation. All v1.5 z=2.5 and z=3.0 nodes were identical to z=2.0 results. Fixed `_simulate` to accept `z_thresh` parameter; `run_backtest` now accepts `z_score_threshold=2.0`; sweep and Node Inspector both pass it through. Verified: same (w=20, TP=28, SL=9, hold=140) node gives 21 trades at z=2.0, 5 at z=2.5, 1 at z=3.0 for LABU.
- **DB cleanup**: Deleted 108k corrupt rows for AGQ z=2.5/3.0 and LABU z=3.0 (v1.5). Those nodes ran with the hardcoded 2.0 threshold regardless of tag.
- **Spatial Topology fix**: `load_slice` now includes `z_score_threshold` column. Dropdown selector appears when multiple thresholds exist — previously all thresholds were blended into one 3D scatter causing duplicate coordinate points and apparent "same profile" across thresholds.
- **Winners page**: Changed `groupby('ticker')` → `groupby(['ticker', 'z_score_threshold'])`. Now shows top N per ticker per threshold side by side for direct comparison.
- **LABU z=2.0 fix**: v1.5 only had 18k/72k rows (partial copy). Copied missing 54k rows from v1.4. LABU now shows in Winners at 210% alpha, 271% return, 2.54× B&H (w=20, TP=21, SL=18, hold=84h) — note SL=18 differs from watchlist param SL=9.
- **FAS sweeps**: z=3.0 — zero positive alpha across 54k nodes. z=2.5 — max 85.4% alpha, 1.43× B&H (only 13 trades). v1.4 559% return node was correct but FAS is structurally momentum.
- **Hurst exponent computed** for all watchlist tickers (R/S method on daily prices). All show H>0.5: AGQ=0.663, DPST=0.654, EDC=0.594, FAS=0.574, LABU=0.522 (6mo). LABU is the only one showing any mean-reversion at 1yr (H=0.454). ADF tests on FAS all non-stationary (p>0.35).
- **Hurst discussion**: Strategy is not pure mean reversion — it captures "recovery from extreme short-term dip in a trending asset." H>0.5 on price level is consistent with snap-back working. Short hold times avoid volatility decay, not momentum.
- **Overnight sweep started**: tmux `sweep_v15_full`, z=[2.0, 2.5, 3.0] for 357 tickers ordered by watchlist first (AGQ, DPST, EDC, FAS, LABU, CRMX) then descending v1.4 max alpha. Currently on DPST. z=2.0 nodes are cached for most tickers so overhead is minimal — mainly catches missed tickers like FAS.
- **statsmodels installed** (for ADF test).
- **v1.6 grid discussion**: Coarse grid (every-3 integers: [3,6,9,...,30] = 6k nodes vs 54k) proposed for v1.6 to validate island consistency. Decided to keep sequential integers for v1.5 and evaluate after results.

### Key Decisions
- FAS should be removed from watchlist: Hurst, ADF, z=3.0 sweep all point to momentum. The v1.4 559% return node is real but untrustworthy structurally.
- LABU SL=9 (original watchlist param) may be wrong — best v1.5 node at z=2.0 has SL=18. Worth revisiting once z=2.5/3.0 data is in.
- The "same return profile" bug was purely the hardcoded 2.0 in the Numba kernel — not a data copy issue.

### Current State
- Overnight sweep running: tmux `sweep_v15_full`, currently on DPST (~50% through), z=[2.0, 2.5, 3.0]
- DB: ~35.8M rows total. v1.5 clean z=2.0 data for all tickers except FAS (being swept now). z=2.5/3.0 for AGQ partially done, FAS z=2.5 done (54k), FAS z=3.0 done (54k, zero positive alpha)
- LABU v1.5 z=2.0: 72k rows complete, shows in Winners
- Watchlist: AGQ, DPST, EDC, FAS (candidate for removal), LABU, CRMX
- No open positions

### Next Session Should
- Check sweep completion — compare z=2.0 vs z=2.5 vs z=3.0 best nodes per watchlist ticker in Winners (now grouped by threshold)
- Decide whether to remove FAS from watchlist based on full v1.5 results
- Revisit LABU watchlist params — SL=18 at z=2.0 beats SL=9; check if z=2.5/3.0 changes this
- Run Hurst + ADF as batch computation across all 357 tickers → add to screener columns (backlog: high priority)
- Plan v1.6 coarse grid sweep design before building

---

## 2026-06-28 — v1.5 Winners/Status UI, Config Finalized, DB Copy

### What we did (append)
- **Winners page**: `z_score_threshold` added as display column, filter multiselect, and passed through session state to Node Inspector and Topology jumps. Min trades floored at 1 (was 0 — caused 17M row load and crash).
- **Sweep Status**: `expected_per_ticker` now multiplies by `z_score_thresholds` count; version-aware so v1.4 still shows 54k and v1.5 shows correct count.
- **v1.4 → v1.5 copy**: All 17.7M v1.4 rows copied into v1.5 with `z_score_threshold=2.0`. Overnight run now only needs to sweep 2.5 and 3.0 thresholds (108k nodes/ticker instead of 162k).
- **config.json finalized**: watchlist tickers (AGQ, DPST, EDC, FAS, LABU, CRMX) at top; `z_score_thresholds: [2.0, 2.5, 3.0]`; 357 tickers total.
- **CRMX added**: 6th in queue for v1.5 overnight sweep. No watchlist params yet — need v1.5 results first.
- **LABU v1.5 sweep**: running in tmux `sweep_v15` (z=3.0 threshold, 54k nodes complete).

### Next Session Should (updated)
- Check LABU v1.5 results — compare z=2.5 and z=3.0 nodes vs z=2.0 (v1.4). Is SL sensitivity improved at higher threshold?
- Start overnight full v1.5 sweep: `tmux new-session -d -s sweep_v15_full ".venv/bin/python run_optimization_sweep.py 2>&1 | tee logs/sweep_v15_full.log"`
- Add CRMX to watchlist once v1.5 results are in
- Position sizing in Slack BUY message (high priority backlog)

---

## 2026-06-28 — v1.5 Sweep, DB Memory Fixes, Risk Discussion, Watchlist Updates

### What we did
- **Risk discussion (Gemini follow-up)** — reviewed mean reversion risks: regime detection, bull market bias, leveraged ETF volatility decay, correlation risk. Concluded long-only + time exit + SL is structurally sound; key gap is regime detection (Hurst filter).
- **Backlog additions** — Half-Life of Reversion, ADF test, Hurst filter (6-month rolling window, DFA method), regime transition stress test (synthetic), H threshold slider (real ticker), rolling 1-year window re-sweep convention.
- **v1.5 sweep** — added `z_score_threshold` as a sweep parameter (2.5, 3.0 in addition to implicit 2.0 from v1.4). Motivated by LABU having wide SL nodes — hypothesis: higher threshold = deeper dips = tighter SL viable. Changes: `strategies.py`, `run_optimization_sweep.py`, `config.json`, `backtest_cache` schema (new column, ALTER TABLE migration).
- **DB memory fix** — all three main pages (Spatial Topology, Node Inspector, Winners) were loading 18M rows on startup (~6GB). Fixed: targeted queries per (version, ticker, strategy), numeric filters pushed into SQL for Winners, watchlist stats scoped to watchlist tickers. Added DB indexes on `version` and `(version, ticker)`.
- **Spatial Topology** — "View in Topology" button added to Winners page; session state jump lands on correct ticker/strategy/version.
- **Node Inspector** — z_score_threshold dropdown added; rewritten to use targeted slice query.
- **Watchlist** — added DPST (w=10, TP=21, SL=12, hold=126h) and FAS (w=10, TP=25, SL=10, hold=133h). AGQ and EDC already on list. LABU already on list. FAS noted as low win rate (22%) — intentional nerve test with smaller position.
- **v1.4 sweep** — was 90% complete at session start; completed during session (323→357 tickers).
- **v1.5 sweep** — started for watchlist tickers only (AGQ, DPST, EDC, FAS, LABU) in tmux session `sweep_v15`. ~108k nodes per ticker.

### Key Decisions
- v1.5 uses same DB, new version tag — threshold is just another parameter, not a new strategy
- FAS spatial grid is all-green (island warning) — likely momentum, not mean reversion. Added anyway for small position nerve test.
- QCML and CRWL are single-stock underliers (QCOM, CRDO) — excluded from watchlist consideration
- Bitcoin ETFs tested poorly (max 42% return) — not worth pursuing
- Hurst/ADF/Half-life are screener metrics (per-ticker, offline), not sweep parameters

### Current State
- v1.4: 357/357 tickers complete
- v1.5: running in tmux `sweep_v15` for watchlist tickers; config.json restored to full 357 tickers for overnight full run
- Watchlist: AGQ, EDC, DPST, FAS, LABU
- No open positions
- Streamlit running (PID ~5733)

### Next Session Should
- Check v1.5 watchlist sweep results — compare 2.5 and 3.0 threshold nodes vs 2.0 for LABU specifically (SL sensitivity)
- Start overnight v1.5 full sweep: `tmux new-session -d -s sweep_v15_full ".venv/bin/python run_optimization_sweep.py 2>&1 | tee logs/sweep_v15_full.log"`
- Review Winners page under v1.5 once data is available
- Consider position sizing in Slack BUY message (still high priority backlog)

---

## 2026-06-27 — Sweep Universe Expansion, Bug Fixes, Strategy Docs

### What we did
- **Results (8).csv imported** — 682 leveraged ETPs with Underlying Index + Total Assets; 673 have price data
- **508 new tickers fetched** — merged into tickers.json (now 1515); 506 CSVs downloaded, 2 failed (AGATF, DEE)
- **Timezone cleanup** — new yfinance (1.4.1) returns tz-aware timestamps; fixed 380 CSVs by stripping tz offset, fixed `data_manager.py` and `run_optimization_sweep.py` to strip tz on load
- **357 tickers loaded into config.json** — leveraged ETPs, has data, $1M liquidity floor (avg_vol_10d × last_price), includes inverse
- **app.py strategy name fix** — multiselect options updated from `ZScore_Original` → `ZScoreBreakout`; default fallback also fixed
- **NO_TRADES caching fix** — nodes returning no trades were never written to DB, causing them to rerun on every sweep restart. Now cached with trades=0 — massive speedup for thin tickers
- **Heatmap duplicate fix** — `pivot()` replaced with `groupby().mean().unstack()` to handle duplicate TP/SL cells
- **Trade log** — new `trade_log` table in DB; `log_trade_entry` on BUY execution, `log_trade_exit` on SELL; `open_positions` gets `trade_log_id` FK; auto-migrated via ALTER TABLE guard on startup
- **`pages/5_Sweep_Status.py`** — new page: per-ticker progress (nodes cached vs expected, % complete, ASCII bar), SUCCESS vs NO_TRADES counts, data freshness (last date in CSV), version filter, auto-refresh 30s
- **`pages/6_Strategy.py`** — renders `docs/strategy.md` in app
- **`docs/strategy.md`** — strategy reference: signal logic, params, live trading assumptions, edge cases
- **`docs/operational_limits.md`** — Phase 1 trading rules: risk-first principle, position limits, execution rules, travel policy, no early exits
- **Winners page filters** — added Min return % (default 100%), Min B&H multiplier (default 2x), B&H Mult column
- **Sweep running in tmux** — ~1.4M nodes/hr, ETA ~10h from session start (~10 tickers done at session close)

### Key Decisions
- NO_TRADES nodes are now cached but excluded from Winners/results — holes in grid are swept, not missed
- $1M liquidity floor = `avg_vol_10d × last_price ≥ $1M` — same as `$50k × 20`, 357 tickers pass
- Sweep runs via `tmux new -s sweep` to survive terminal drops
- Phase 2 (automated exits) closer than expected — Schwab API likely target; exits are deterministic and low-risk to automate

### Backlog additions
- Position sizing in Slack BUY message (high priority)
- Chaos monkey / floor alpha: worst-case entry/exit delay (1d and 2wk), missed TIME_EXIT, drop top N trades
- Portfolio backtest page: concurrent positions over time, capital utilization
- Automated exits (Phase 2): Schwab API, TP/SL/TIME submitted as market orders
- Broader ticker universe: results.csv (999 rows, mixed) for non-leveraged expansion

### Current State
- Sweep running in tmux, ~12% complete, ETA ~10h
- 3 tickers on watch list: AGQ, EDC, LABU
- Trade log built, not yet tested end-to-end (needs a real trade)
- DB has v1.4 data for 40 tickers

### Next Session Should
- Check sweep completion, review Winners page with new filters
- Pick next watchlist candidates from v1.4 results (filter: >100% return, >2x B&H mult, beat SPY)
- Build position sizing into Slack BUY message (`avg_vol_10d × last_price` from screener DB)
- Discuss chaos monkey sweep design before building

---

## 2026-06-27 — Screener, Ticker Universe, Data Collection

### What we did
- **Winners → Node Inspector fix** — now passes window/TP/SL/hold in session state so all dropdowns auto-select on arrival
- **Node Inspector strategy bug fix** — removed stale `strategy_mapping` dict, replaced with `getattr(strategies, name)` — was causing `NoneType is not callable` error
- **Data refresh wiring** — `active_signals.py` now fetches fresh price data for all watched tickers at the start of each poll cycle; no longer needs `data_collector.py` running alongside
- **`tickers.json`** — created as single source of truth for data collection universe; `data_collector.py` now reads from it instead of hardcoded list
- **Daily cron** — `scripts/run_data_collector.sh` runs `data_collector.py --once` at 8 AM daily, logs to `logs/data_collector_daily.log`
- **Full history pull** — ran `data_collector.py --once` for all ~1000 tickers in tickers.json; 926 CSVs cached, 74 failed (delisted/no data)
- **`tickers` DB table** — `scripts/import_tickers.py` imports screener CSV into `cache/trading_universe.db` with derived columns: `leverage` (parsed from description), `inverse`, `has_data` (CSV exists), `stock_underlier` / `index_underlier` (classified from underlying index + description), `last_price`, `total_assets`, performance columns
- **`pages/4_Screener.py`** — filter leveraged ETF universe before deciding what to sweep; filters: AUM, dollar-volume liquidity (investment × multiplier), leverage (2x/3x), inverse, single-stock underlier, has-data, underlying index search, performance; "Add to config.json" button populates `target_tickers`
- **Screener CSV exploration** — imported `results.csv` (mixed leveraged/non-leveraged), then `Results (7).csv` (682 leveraged-only but missing Underlying Index + Total Assets columns)

### Key Decisions
- `tickers.json` = data collection universe (all candidates); `config.json` target_tickers = sweep candidates (curated subset)
- Dollar-volume liquidity filter: `avg_vol_10d × last_price ≥ investment × multiplier` (default $50k × 20 = $1M)
- `leveraged_etp` field from screener is reliable — 1x inverse ETFs correctly marked "No"
- `stock_underlier` / `index_underlier` split is cleaner than a boolean flag; crypto/commodity/currency leave both NULL
- Single-stock underlier detection uses company suffixes on `underlying_index` + description patterns ("2X Long TSLA Daily", "ADRhedged") for "No Underlying Index" cases

### Current State
- 926 tickers with hourly price history cached
- Screener page working; current import (Results 7.csv) missing Underlying Index + Total Assets — underlier classification is description-only and incomplete
- No open positions

### Next Session Should
- **Re-export screener** with Underlying Index + Total Assets columns, re-run `python scripts/import_tickers.py <new_file.csv>`
- **Use Screener to select sweep candidates** — filter to 2x/3x, exclude single-stock, apply liquidity filter, add to config.json
- **Run sweep on leveraged universe** — ~130 tickers with data at 2x/3x; at 20 min/ticker ~45 hours with current grid. Consider coarsening TP/SL to every 2% (→ ~3 days)
- **Trade log** — new DB table for executed trades (signal price, exec price, exit price, drift), triggered from Socket Mode modal submissions

---

## 2026-06-27 — Socket Mode, Winners Page, Live Test

### What we did
- **Slack Socket Mode** — upgraded from webhook to `slack_bolt` + bot token + app token. BUY/SELL messages now have interactive Executed/Skipped/Exited buttons. Clicking opens a price entry modal; submission writes to `open_positions` and updates the original message. Falls back to webhook if bot tokens not set.
- **Chart upload** — `matplotlib` chart generated on each signal (price, SMA, ±2σ bands, signal marker for BUY; entry/TP/SL lines for SELL), uploaded via `files_upload_v2`. Channel ID resolved at startup via `_resolve_channel_id()`.
- **`compute_buy_signal` fix** — now excludes today's intraday close from the daily rolling window. Prior version made BUY signals mathematically impossible (lowering price also lowered the lower band). Fix: `df_daily[df_daily.index < today]`.
- **`--ticker` filter** — `python active_signals.py run --ticker TEST` limits poll loop to specific tickers without removing others from watch list.
- **Single-line poll summary** — one line per poll cycle instead of one line per node.
- **`scripts/live_test.py`** — synthetic TEST ticker driver: `setup` (writes CSV + adds to watch list), `sell` (pumps price above TP), `status`, `cleanup`. Verified full BUY→SELL→close flow via Slack.
- **`pages/3_Winners.py`** — leaderboard of top nodes per ticker. Filters: version, ticker, strategy, min trades, min alpha, beat asset B&H toggle, top N. Dismiss per `(ticker, strategy, version)` persisted to `cache/dismissed_tickers.json`. Click row → Watch / Dismiss / Open in Node Inspector. Watch list table at bottom with inline label editing, remove by uncheck, and backtest stats joined from DB. Last price + daily volume columns from cache CSVs.
- **Deleted `v_perf_test`** — 18,090 rows removed from `backtest_cache`.
- **`.env` cleanup** — removed stale `Code snippet` header and unused `OPENAI_API_KEY`, `FINNHUB_API_KEY`, `SMA_PERIOD`, `Z_SCORE_THRESHOLD` lines.

### Key Decisions
- Socket Mode runs in a daemon thread; poll loop continues immediately after posting signal (non-blocking)
- Dismiss scope: `(ticker, strategy, version)` — new version resets dismissals, different strategy on same ticker stays visible
- `strategy_return` and `alpha_vs_spy` stored as percentage points (e.g. 729.2 = 729.2%) — do not multiply by 100 in display
- `compute_buy_signal` uses prior closed day's indicators — matches live trading semantics (today's daily bar hasn't closed yet)

### Current State
- Layer 3: Socket Mode live and tested end-to-end. AGQ on watch list (ID=1).
- Winners page: working. Dismiss file at `cache/dismissed_tickers.json`.
- No open positions.

### Next Session Should
- **Winners → Node Inspector jump**: window/TP/SL/hold dropdowns in Node Inspector need auto-selection when navigating from Winners (session state currently only passes ticker/strategy/version)
- **Data refresh wiring**: call `fetch_live_data_smart(ticker)` inside poll loop before checking signals
- **Trade log**: new DB table for executed trades — signal price, exec price, exit price, drift. Triggered from Socket Mode modal submissions.

---

## 2026-06-26 — Layer 3 Active Signals + Strategy Architecture

### What we did
- **Built `active_signals.py`** — poll loop, BUY/SELL signal detection, Slack Block Kit notifications, open position tracking, execution prompt. CLI commands: `run`, `list`, `add`, `remove`, `positions`
- **Refactored `strategies.py`** — added `check_exit` to `BaseStrategy`; `active_signals` now delegates all entry/exit logic to the strategy class, no signal math duplicated
- **Renamed strategy DB values** to match class names (`ZScore_Original` → `ZScoreBreakout`, `ZScore_TrendFiltered` → `TrendFilteredZScore`). `run_optimization_sweep.py` now uses `getattr(strategies, name)` — no hardcoded map
- **Added `tests/`** — `test_ZScoreBreakout.py` (11 cases), `test_TrendFilteredZScore.py` (4 cases), shared helpers in `conftest.py`. All passing.
- **Wrote `docs/strategy_architecture.md`** — target data model where node = `(strategy, params as JSON)`, ticker is a param not a field, strategy declares its own parameter schema. Migration deferred until second strategy added.
- **Added AGQ to watch list** — `v1.4 AGQ top 20w 140h` (window=20, TP=28, SL=9, hold=140h)
- **`python-dotenv`** added; `.env` file needed for `SLACK_WEBHOOK_URL`

### Key Decisions
- Strategy class is the single source of truth for entry/exit logic — `active_signals.py` knows nothing about signal math
- Node identity is `(strategy, params as JSON)` — ticker is a param, not a first-class field. Hardcoded columns in `backtest_cache` are acceptable until a second strategy is added.
- Exit conditions (TP/SL/TIME) live on `BaseStrategy.check_exit` — subclasses can override for custom exit rules
- No unit tests by choice — sanity checking via running known nodes through the backtester is more meaningful
- `active_signals.py` requires `data_collector.py` running simultaneously — documented in readme.md

### Current State
- Layer 3: `active_signals.py` built and tested. Watch list has AGQ. No open positions.
- Slack webhook works (incoming webhook). Interactive buttons (Socket Mode) planned but not built.
- `active_signals.py` has no data refresh — reads stale cache unless `data_collector.py` is running
- DB strategy names now match class names; v1.2/v1.3 rows still have old names but those are dirty data anyway

### Next Session Should
- **Slack Socket Mode** — upgrade from webhook to bot token + app token; enable interactive buttons (Executed/Skipped) with modal for execution price entry and chart image upload
- **Chart image in Slack** — price history + SMA/bands + signal marker, generated on signal fire and uploaded via bot token
- **Live simulation test** — inject synthetic BUY data for TEST ticker, fire real Slack message, confirm trade, pump TP/SL price, confirm exit
- **Winners page** — Streamlit leaderboard showing top nodes per ticker for current version, with "Add to Watch List" button (imports `add_node` from `active_signals.py`)
- **Data refresh wiring** — call `fetch_live_data_smart(ticker)` inside the poll loop before checking signals

---

## 2026-06-26 — Performance & UI Session

### What we did
- **Numba optimization**: `backtester.py` rewritten with `@njit` kernel — 4s → 11ms per node (~360x). Full 18k node sweep now takes ~5 minutes on 10 workers vs overnight
- **SQLite batch commits**: writes now batch every 100 nodes — eliminates write contention with 10 workers
- **Fixed TIME_EXIT bug**: filter in `run_optimization_sweep.py` now correctly includes `TWIN`/`TLOSS` — this was causing thousands of nodes to go missing from DB
- **Fixed `max_hold_days` → `max_hold_hours`** in `active_phase_grid.json` output
- **Restored blue dots** (planned nodes) in Spatial Topology page, filtered against already-completed nodes
- **Topology UI fixes**: alpha filter defaults to absolute floor, leaderboard respects 4th-dimension slice, version picker is now first and cascades to ticker/strategy
- **Config simplified**: removed dual DB/file config — `config.json` is now single source of truth. Fixed generations `min_value=0`
- **Deleted legacy files**: `trading_engine.py`, `visualize_results.py`, `plot_3d_growth.py`, old `strategy_optimizer.py`
- **Renamed**: `strategy_optimizer.py` → `backtester.py`, `run_backtest_simulation` → `run_backtest`
- **tqdm throttled**: postfix updates every 2 seconds, display intervals 15-30s
- **Nightly DB backup cron** set up at 2am, keeps last 7 backups
- **Verified**: new backtester output matches DB on spot-check (mismatches are expected — DB was populated with buggy TIME_EXIT code)

### Current State
- Layer 1 (data collection): working
- Layer 2 (optimization): working, fast. Currently running sweep with new version tag to get clean data
- Layer 3 (live trading): not started
- DB has mixed versions — v1.2/v1.3 computed with old buggy code, new version being computed now with fixed code
- `requirements.txt` needs `numba` added

### Key Decisions Made
- Brute force full sweep is now fast enough that generations are redundant — left in code as escape hatch for future larger grids
- `config.json` single source of truth — DB copy of config removed
- numpy/Numba over GPU — dataset too small to benefit from GPU

### Next Session Should
- Add `numba` to `requirements.txt`
- Build Layer 3 live trading engine (`live_trading.py`)
- Build trade chart Streamlit page (price/bands/markers, launchable from Node Inspector)
- Review sweep results once current run completes — pick parameter sets for live trading

---

## 2026-06-25 — Repo Setup & Documentation

### What we did
- Set up SSH key (ed25519) and connected to GitHub
- Created repo `pkim-tech/trading`, renamed branch from `master` to `main`
- Cleaned up project structure: moved legacy files to `output/`, set up `.gitignore` and `.claudeignore`
- Created `~/.claude/CLAUDE.md` (global prefs) and `trading/CLAUDE.md` (project context)
- Rewrote `readme.md` with three-layer architecture
- Created `docs/` with `design.md`, `backlog.md`, `session_cache.md`
- Initial commit pushed to GitHub

### Current State
- Layer 1 (data collection): complete and working
- Layer 2 (optimization): complete, last run was ~18k nodes per ticker overnight
- Layer 3 (live trading): not started, `trading_engine.py` is legacy placeholder
- No pytest / unit tests — intentional for now

### Key Decisions Made
- Brute force over smart search for parameter optimization (floating point issues with fine-mesh approach)
- SQLite caching means nodes are never re-evaluated — safe to re-run sweeps
- L3 cache optimization is a known future performance improvement (Gemini suggestion, not yet implemented)

### Next Session Should
- Review `trading_engine.py` and decide whether to retrofit or replace for Layer 3
- Investigate L3 cache optimization for node evaluation speed
