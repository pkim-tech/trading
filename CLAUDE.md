# Trading Alpha Engine

## Session Commands
- `go` — session start: read the first ~60 lines of `docs/session_cache.md` and the full `docs/backlog_cache.md`, and give a brief summary of where we left off, what's next, and any current backlog items worth flagging.
- `session close` — session end: write the session summary entry (starting with `## <date> — <title>`, no surrounding `---`) to a file and run `python scripts/session_cache_update.py <file>` (or pipe via stdin) — this mechanically prepends to `docs/session_cache.md` (cap 10, drop oldest, gitignored) and appends to `docs/conversation_summary.md` (permanent, uncapped, committed) in one shot, no need to read either file first. Commit only `docs/conversation_summary.md`. No tests, no skills.
- `feature wrap` — mid-session feature complete: update relevant docs, review pre-commit checklist (`docs/pre_commit_checklist.md`) manually, and commit. Does not trigger session close. **Do not invoke any skills (especially verify) during this command.**
- `session wrap` — in order: (1) update relevant docs, (2) review pre-commit checklist manually, (3) write the session summary entry via `python scripts/session_cache_update.py` (see `session close` above) to update both `docs/session_cache.md` and `docs/conversation_summary.md`, (4) commit everything including both files. **Do not invoke any skills.**

## Project Overview
A z-score mean reversion backtesting and optimization system targeting leveraged ETFs. Runs parallel grid sweeps over take profit, stop loss, and hold time parameters to find optimal strategy configurations with positive alpha vs SPY. Now in live trading phase with manual execution via Schwab.

## Live Trading — Current State
- **Watchlist**: `watchlist_id=9` ("Sweep v3 - Full", `is_active=1` in the `watchlists` table) is the actual live/active watchlist — `get_watchlist()` with no argument resolves via `get_active_watchlist_id()`, which reads this flag. **`watchlist_id=7` ("Sweep 3 (v3.x)") is stale/inactive**, superseded by 9 on 2026-07-07 06:26; do not treat it as live going forward. (Correction 2026-07-08: prior docs and a prior session's `watch_list.account` backfill incorrectly targeted watchlist 7 after 9 was already active — always check `watchlists.is_active` before assuming which watchlist_id is live.)
  - 11 tickers total on watchlist 9, all `TrailingBothZScoreBreakout` (trailing entry — place a broker trailing-buy order at `trail_buy_pct`%, broker handles fill timing — + trailing exit): AGQ, DPST, EDC, GDXU, HIBL, KORU, LABU, NUGT, SOXL, TQQQ, YANG. Versions vary per ticker (v3.24–v3.49) — see `watch_list` table for exact per-ticker window/arm_sell_pct/trail_buy_pct/trail_sell_pct.
  - **Per-ticker `live`/`research` mode changes over time** (last known split as of 2026-07-13: live = DPST/EDC/HIBL/KORU/LABU/SOXL, research = AGQ/GDXU/NUGT/TQQQ/YANG — AGQ moved to research this session after a sustained decline plus a wash-sale-adjacent cash-account constraint on adding to a losing position) — don't trust a hardcoded list here, run `python scripts/watchlist_status.py` for the current mode/trigger-distance table straight from the DB.
  - All use `fixed_sl=15%` (real stop loss, config-driven constant, not swept) and z-score thresholds 1.0-1.5 per ticker.
- **Signal windows**: 10:25–10:40 AM ET and 15:25–15:40 PM ET (matches backtest target_hours=(9,14)). Hourly bars are labeled by **start** time (e.g. the "14:30" bar spans 14:30–15:30) — the bar-close signal actually being checked in the 15:25–15:40 window is the **14:30** bar (last one fully closed by then), not the 15:30 bar. `target_hours=(9,14)` reflects this: only hours 9-14 (9:30-14:30 anchors) are in the backtested grid: the 15:30 partial bar is excluded entirely.
- **Open positions**: change constantly — run `python scripts/open_positions_status.py` for the current live list straight from `open_positions`, don't trust a hardcoded snapshot here. Note: `open_position()`'s `signal_time`/`entry_time` split lets a late/manual entry be backdated to the real signal bar, so `max_hold_hours` counts from the actual dislocation rather than the late entry — used at least once (KORU/SOXL, 2026-07-06 catch-up entries off a missed 2026-07-02 signal).
- **Execution workflow**:
  - `TrailingExitZScoreBreakout`: Stage limit order pre-market at absurd low price → Slack fires at bar close if price confirmed below lower_band → edit order to market and submit (~5 seconds).
  - `TrailingBothZScoreBreakout`: same bar-close signal fires the alert, but place a **trailing buy order at `trail_buy_pct`%** instead of a market order — the broker tracks the bounce-above-running-low entry itself (live code does not simulate this state machine). Three-step Slack confirmation (2026-07-10): BUY alert → tap **"Trailing Buy Order Placed"** once the order is resting at the broker (no position opens yet, no price asked — fill price isn't known at this point) → tap **"Filled"** separately once it actually executes, entering the real fill price (this is what opens the position and starts arm/SL/trail triggers). Reminders nag every 15 min through both phases until Filled/Skipped resolves it.
  - 🔶 in morning report = set phone alarm for 10:28 and 15:28.
- **Stop loss**: Schwab stop order at lower_band × (1 - (SL%+1%)) — 1% buffer over backtest SL for intraday protection. Real exit is triggered by Slack SELL signal at bar close, not the Schwab stop (which is catastrophic insurance only).
- **Position sizing**: $50k notional per trade. Share count shown in BUY Slack message.
- **Entry price**: Real-time via `yfinance fast_info.last_price` at signal check time.

## Key Files
- `app.py` — Streamlit UI for configuring and launching optimization sweeps
- `run_optimization_sweep.py` — main parallel optimization engine (brute force + generational refinement)
- `backtester.py` — core backtest simulation logic (`run_backtest`)
- `strategies.py` — strategy classes: `ZScoreBreakout`, `TrailingExitZScoreBreakout`, `LimitOrderZScoreBreakout`, `LimitOrderTrailingExit`, `TrailingBuyZScoreBreakout`, `TrailingBothZScoreBreakout` (live default), `LimitExitZScoreBreakout`, `TrendFilteredZScore`
- `active_signals.py` — live trading daemon: polls signal windows, manages `open_positions`/`watch_list`, fires Slack alerts. Delegates to (and re-exports, for backward compat): `signals_config.py` (config/tokens/Bolt app singleton), `signals_db.py` (DB layer), `signals_compute.py` (signal computation), `signals_charts.py` (chart PNGs), `signals_blocks.py` (Slack message posting/block builders), `signals_helpers.py` (small shared helpers), `signals_handlers.py` (Bolt interactive button/modal handlers), `signals_notify.py` (notify_*/reminder loops/reference-table/report — the core, formerly the whole Slack-facing layer, split out 2026-07-14 for length).
- `data_collector.py` — fetches and caches hourly price data
- `data_manager.py` — data fetching and cache management
- `config.json` — runtime config: tickers, hyperparameters, strategy selection
- `pages/` — Streamlit multipage app views (Spatial Topology, Node Inspector)
- `scripts/daemon_status.py` — checks whether `active_signals.py` is running and whether its process-start time is older than the newest edit among the live-trading source files (i.e. stale vs. already restarted) — run instead of manually comparing `ps`/mtimes whenever a restart is pending.
- `scripts/run_v4_backfill_sweep.sh` — v4 kernel-correctness backfill wrapper (fill-optimism resolution bounds, `fixed_sl` sweep, Open-check entry timing — see `docs/backlog_cache.md`). `backtester._simulate_trail_both` now computes three parallel trailing-buy bounce-fill resolutions per node, since none of hourly OHLC proves the true intrabar path: **possible** (existing/unchanged, assumes Low-before-High), **pessimistic** (new, mirror-image Low-after-High assumption — always same-bar-or-later and same-or-worse price than `possible`, a real bracket partner), **certain** (new, only resolves a fill when provable regardless of ordering — can occasionally beat `possible` since deferring lets `running_low` fall further first, so it is *not* a pessimistic-price bound, just a no-guessing one). Island search and cliff-safety rank/filter on `MIN(possible, pessimistic, certain)` (`run_optimization_sweep.ROBUST_ALPHA_SQL`), not `possible` alone, so a node is only selected when it holds up under every resolution. Loops 10 `stop_loss` values x 2 `entry_timing` values (20 campaigns, `TrailingBothZScoreBreakout`, 11 live tickers only) through `run_optimization_sweep.py --version v4 --entry-timing <close|open_check>`, each a full phase1→2→2.5 run. Unlike `run_v3_backfill_sweep.sh`, every campaign shares one version string (`v4`) — the real `stop_loss`/`entry_timing` `backtest_cache` columns disambiguate campaigns instead, so island/cliff-safety queries filter on those columns too (`run_optimization_sweep.py::_campaign_scope_sql`). New `sl_sweep_summary` table holds one rollup row per completed campaign for slice/trend queries without re-deriving island stats from raw `backtest_cache`.
- `scripts/live_sim.py` — manual-step live-sim REPL: drives the real `compute_buy_signal`/`check_sell_condition`/`notify_*` functions against an isolated `cache/trading_sim.db` (via `TRADING_DB_PATH` env override) so the full BUY→placed→filled→arm→trailing-sell Slack sequence can be exercised bar-by-bar without touching the live daemon or `trading_live.db`. `SIM_MODE=1` forces plain-text/typed-input Slack messages (prefixed with a `🧪 SIM MODE`/`🧪 SIM MODE END` header/footer context block, optionally labeled via `SIM_SCENARIO` env var) instead of interactive buttons — the sim never opens its own Socket Mode connection, so real buttons would risk being delivered to the live daemon's connection instead. Real interactive buttons genuinely cannot be tested this way (Slack routes all clicks to whichever process holds the socket, i.e. the live daemon, not the sim) — button *layout* can still be previewed by manually appending a dummy-`action_id` actions block (safe no-op if tapped). `buy`/`sell` REPL commands drive signal checks directly; `pending`/`placed TICKER`/`fill TICKER PRICE`/`remind_buy [--stale]` drive the three-state buy lifecycle (signal → order placed → filled) added 2026-07-10. Interactively tested end-to-end with the user 2026-07-09/10 — found and fixed several real bugs in the process (see `docs/conversation_summary.md`).

## Runtime Artifacts (not committed)
- `cache/live/` — the real trade record: `trading_live.db` plus pre-migration `.bak` snapshots, `trading_sim.db` (live-sim testing), `active_signals_heartbeat.txt`
- `cache/research/` — regenerable research data: hourly CSV per ticker, `trading_universe.db` (+ daily/weekly `.bak` rotations), `watchlist_sweep.db`, `dismissed_tickers.json`
- `logs/` — optimization output PNGs, CSVs, text reports
- `output/` — script outputs/exports/reports (not cache): `*_trades.xlsx`, `live_backups/` (hourly `trading_live.db` snapshots), archived/legacy files
- `active_phase_grid.json` — live progress state written during sweep runs
- `current_test.json` — temp telemetry file, deleted on sweep exit

## How to Run
```bash
# Install dependencies
pip install -r requirements.txt

# Launch Streamlit UI
streamlit run app.py

# Or run optimization sweep directly
python run_optimization_sweep.py
```

## Architecture Notes
- Optimization uses `ProcessPoolExecutor` with up to 10 workers
- Results cached in SQLite to avoid re-running completed nodes
- Multi-generation refinement: macro grid scan → frontier detection → fine mesh around top performers
- `active_phase_grid.json` is written by the sweep and read by the Streamlit UI for live progress display
