# Backlog Cache

Curated, current subset of `docs/deep_backlog.md` — read in full at session start (`go`). Full detail for every item lives in `deep_backlog.md`; this is just the active/relevant pointer list. Periodically re-triage.

## In progress
- **`trail_pct`→`trail_sell_pct` + `take_profit`→`arm_sell_pct` rename**: DB-side + `active_signals.py` + `run_optimization_sweep.py` done. `axis_tp` migration on `backtest_cache` (86.2M rows) confirmed complete and verified 2026-07-07 (row-for-row match vs. pre-migration backup, fresh AGQ backfill test passed once data-drift was accounted for). 4 stale duplicate tables (`open_positions`/`trade_log`/`watch_list`/`watchlists` in `trading_universe.db`, orphaned by the live/research DB split) dropped 2026-07-07, backed up first to `cache/stale_tables_backup_20260707.sql`.
  **Streamlit/script propagation — partial, still broken in places:**
  - `pages/0_Top_Pivot.py`: fixed — `load_best_nodes()`, cliff-safety query, watchlist-pivot join (was doing `b.take_profit = w.take_profit`, always `NULL=NULL` for 6 of 8 live `TrailingBoth` tickers — silently broke the "Watchlist — Alpha by Strategy" section).
  - `db_cache.py` (off-scope, not in the original file list, but shares the identical bug and runs nightly via cron): `CLIFF_GRID_SQL` and `refresh_best_nodes_cache()` both fixed (`take_profit`→`axis_tp`). **`refresh_best_nodes_cache()`'s fix is unverified** — was mid-verification-run when the session moved on; confirm it completes clean before trusting `cache/db_cache_daily.log` next run.
  - **Not yet started**: `pages/2_Node_Inspector.py`, `pages/3_Winners.py`, `pages/4_Portfolio.py`, `pages/10_Open_Positions.py`, `scripts/export_cliff_safety.py`, `scripts/verify_live_parity.py`, `scripts/fill_trail_pct_gaps.py`. Same pattern to apply: any `SELECT`/`WHERE`/`JOIN` against `backtest_cache.take_profit` → `axis_tp` (NULL for `TrailingBothZScoreBreakout` rows since the 2026-07-05 split), any `backtest_cache.trail_pct` → `trail_sell_pct` (column renamed, doesn't exist under the old name — hard `OperationalError`, not silent). **Exception**: `cache/watchlist_sweep.db` (used by "Watchlist Trade Pivot" section of `Top_Pivot.py`) is a separate, never-migrated snapshot DB — its `trail_pct`/`take_profit` columns are still correct as named; don't touch those.

## Live trading behaviors — big ask from 2026-07-07 night, not started
User asked about 6 related things in one message; sequencing agreed: (1) IRA settlement-delay check first, rest after.
- **Backtest re-entry behavior (answered)**: kernel (`backtester.py` `_simulate*` family, e.g. `_simulate_trail_both:582-633`) has **zero cooldown** after a stop-loss exit — if the z-score signal re-fires on the very next 10:25-10:40/15:25-15:40 check, it re-enters same day. This is intentional/backtested behavior, not a live-only bug.
- **SOXL 2026-07-07 specifics**: the SL exit (`trade_log id=3`, `-15.38%`, exited 07-07 09:30) was on watch_list node id 39, `TrailingExitZScoreBreakout v3.18`. The new BUY signal prompting re-entry is from a **different node**, id 45, `TrailingBothZScoreBreakout v3.35` (added to watchlist 07-07 06:26) — not literally the same signal re-firing, a different strategy variant on the same ticker. Didn't get further than this before session ended — user still needs an answer on what to actually do today.
- **IRA settlement-delay check (next up)**: backtest's compounding math (`(df_tr['Return']+1).prod()`) assumes capital is instantly reusable for the next trade — doesn't model T+1/T+2 IRA cash settlement. Need to check trade history for how often same-ticker (or same-account) trades are spaced closer than settlement allows; may need a re-sim with a capital-availability constraint. **This determines whether today's SOXL v3.35 signal and future position sizing are even valid for IRA.**
- **Account tracking schema**: column-on-watch_list vs. separate `accounts` table — user leaning simple (column) but said might change mind, needs a real decision pass. Allocations as of 2026-07-07 night (unverified against DB, from user's message):
  - Brokerage: AGQ $50k, TQQQ $50k (has live signal, user says trust the math despite ticker being flagged `research` mode — dividend timing not a brokerage concern since held in IRA-type accounts), GDXU (research, no capital)
  - SEP: EDC $32k (deliberately small — "safety" account)
  - Roth: none yet (may take an IRA ticker later)
  - IRA (risky account, 4 active): SOXL $50k, KORU $50k, HIBL $50k, LABU $50k (new ticker, not yet in watchlist/backtested — will move to Roth eventually)
  - Research/no capital: YANG, GDXU, DPST, TQQQ, NUGT
- **P&L tracking for compounding position sizing**: user wants trade sizing to compound realized P&L into the next trade per account — needs real capital/P&L tracking, not just $50k-flat notional.
- **win_twin_rate recalc**: one-off requested for AGQ and EDC (SOXL v3.18 already known-zero per existing backlog note below). YANG has 92 trades, user flagged as maybe too many to hand-verify — considering cutting the watchlist from 6 to 3 tickers if this gets unwieldy.
- **Slack messaging redesign**: user dislikes current action/reminder timing. Wants, for trailing-both tickers specifically: (1) on trailing-buy trigger, a message asking to confirm fill price/qty + input a defensive SL, plus the next arm-trigger price; (2) on arm trigger, a message to switch the SL to a trailing-sell at X%, with current order price and trigger price; (3) on forecast trailing-sell trigger (trading hours only), a confirm-price/qty message. Every trade-action message should also state estimated capital, account, and other trade details (user's explicit standing request going forward, not just for this redesign).

## Live-trading reliability gaps (real, not yet built)
- **Trailing-buy fill confirmation** — for `TrailingBothZScoreBreakout` pending trailing-buy orders (broker-side, state not simulated live), the daemon could compute per-bar whether the trailing-buy threshold would have triggered and Slack a confirmation prompt, instead of the user manually tracking/reporting the fill. Idea from TQQQ 700-share pending order, 2026-07-07.
- **Out-of-band heartbeat/watchdog** for `active_signals.py` — daemon crashed 2026-07-07 with no independent alert (Slack is posted *by* the daemon, can't alert on its own death). Needs a separate process checking a heartbeat.
- **Default rule**: every action-requiring state change in `active_signals.py` must have a Slack notification — current coverage (buy/sell/trailing-armed/limit-fill) is complete, but audit this against any new strategy/state added going forward.
- **"What's close" script** — no persisted/queryable signal-proximity state exists; had to hand-compute per-ticker distance-to-trigger this session. Wants a Slack-command-triggerable version.
- **Add account tracking** (Brokerage/SEP/IRA/Roth) — user wants DB-level tracking for portfolio performance, not a spreadsheet. Not started.

## Test coverage / historical notes
- **Automated round-trip DB test** for `active_signals.py` (`add_node`→`open_position`→`check_sell_condition`) — no test coverage exists for this path currently.
- `win_twin_rate` reads `0` for any `backtest_cache` row computed before that column existed (pre-commit `252b3bf`) — not a bug, old rows never recomputed retroactively. Affects SOXL's v3.18 rows specifically.

## Backup/storage policy (2026-07-07, current)
- `trading_live.db`: hourly cron backup, keep 30 days (`cache/live_backups/`).
- `trading_universe.db` (research, regenerable): daily + weekly rotating single-file backups (`trading_universe_daily.db.bak` / `_weekly.db.bak`), 2 copies total, no accumulation.

## Deferred, lower priority
- Slack slash-command interaction (`/positions`, `/watchlist`, `/status`) — needs a design pass, `SOCKET_MODE` already wired.
- Split `active_signals.py` into modules (1680+ lines, one file) — deferred, live-trading takes priority.
