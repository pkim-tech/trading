# Backlog Cache

Curated, current subset of `docs/deep_backlog.md` — read in full at session start (`go`). Full detail for every item lives in `deep_backlog.md`; this is just the active/relevant pointer list. Periodically re-triage.

## In progress
- **`trail_pct`→`trail_sell_pct` + `take_profit`→`arm_sell_pct` rename**: DB-side + `active_signals.py` done 2026-07-07 (verified against live KORU/HIBL positions). `run_optimization_sweep.py` also fixed same day (was fully broken — `no such column: trail_pct` — since the DB rename had already landed): renamed SQL + added a new `axis_tp` PK-helper column (see `docs/design.md` "Addendum 2"). **DB migration for `axis_tp`/`arm_sell_pct` on the live 75.6M-row `backtest_cache` was still running as of session end — check `cache/axis_tp_migration.log` next session before trusting any fresh sweep run.** Planned follow-up once migration confirmed done: fresh AGQ `TrailingBothZScoreBreakout` backfill, compare against pre-migration cached numbers. **Still pending**: the 5 Streamlit pages (`Top_Pivot`, `Node_Inspector`, `Winners`, `Portfolio`, `Open_Positions`), and `scripts/export_cliff_safety.py`/`verify_live_parity.py`/`fill_trail_pct_gaps.py`. Also still pending: drop the 4 stale duplicate tables from `trading_universe.db` (backed up, just never executed — do once the `axis_tp` migration finishes, not concurrently).

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
