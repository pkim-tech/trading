# Backlog Cache

Curated, current subset of `docs/deep_backlog.md` — read in full at session start (`go`). Full detail for every item lives in `deep_backlog.md`; this is just the active/relevant pointer list. Periodically re-triage. Resolved/dead items are pruned here once closed out — see git history or `docs/conversation_summary.md` if the old writeup is ever needed.

## Medium priority, 2026-07-10 — same-bar arm/take-profit trigger not checked at entry
`simulate_trail_both_annotated`/`_simulate_trail_both` skip arm/TP/SL checks on the entry bar itself (fill sets `in_trade=True` then `continue`s, so trailing-arm logic starts evaluating the *next* bar). Checked across the 6 live tickers whether the entry bar's own High already cleared the arm threshold: SOXL 0/57, EDC 0/32, KORU 0/30, AGQ 2/36, LABU 1/38, but **HIBL 20/54 (37%)** — over a third of HIBL trades could arm the trailing-sell an hour earlier than the backtest credits. Direction of the return bias not yet determined — unprovable from hourly OHLC (same intrabar-order problem as the fill-timing item). Explicit user call: leave the kernel as-is since live trading has the same delayed-until-next-bar behavior — fixing backtest without fixing live would create a live/backtest divergence. Not started.

## Backlogged, 2026-07-09 — fill-price/drift accuracy
Fills often don't land exactly at the expected trigger, and the current typed-price-entry flow (Executed button → manual price entry) doesn't do anything with that drift beyond logging it. Scope not yet defined (better fill capture? drift-vs-expected alerting? something else).

## Open question, 2026-07-09 — is the Schwab catastrophic-stop +1% buffer the right size?
Stop order placed at `(stop_loss + 1)%` below trigger (flat +1% buffer, hardcoded, `schwab_sl_pct = node['stop_loss'] + 1`) so ordinary intraday noise doesn't trip it before the real Slack SELL signal fires. Not empirically grounded — if the goal is avoiding noise-driven stop-outs, the buffer should be backtested/varied rather than assumed.

## Open question, 2026-07-09 — trailing-buy re-entry timing after a same-day exit
If a same-day re-entry trigger hits (ticker sold, then dislocates again same day), does the live trailing-buy order need to be placed relative to the **9:30 open** or the **10:30 normal bar time**? Not tested yet. Worth checking against `active_signals.py`'s actual signal-window/bar-labeling logic (hourly bars are labeled by start time) before assuming either answer.

## Resolved, 2026-07-12 — `trail_pct`/`take_profit` rename propagation
All previously-listed files done: `pages/2_Node_Inspector.py`, `pages/3_Winners.py`, `pages/4_Portfolio.py`, `pages/10_Open_Positions.py` (`take_profit`→`axis_tp`, `trail_pct`→`trail_sell_pct` in SQL), `scripts/export_cliff_safety.py` (same rename, plus fixed a pre-existing `sl_label`/`sl_display` NameError left over from the original commit), `scripts/verify_live_parity.py` (node dict key `trail_pct`→`trail_sell_pct` to match `active_signals.py`'s expected key). `scripts/fill_trail_pct_gaps.py` needed no change — doesn't touch those columns. **Note**: other pages (`8_ADF_Filter.py`, `11_Universe_Scan.py`, `1_Spatial_Topology.py`, `7_Hurst_Filter.py`, `scripts/profile_dispatch.py`, `scripts/post_sweep_report.py`, `scripts/top_safe_nodes.py`) still query raw `take_profit` but were never in scope — they only look at non-v3.x strategies where the column is still populated; revisit only if they ever need to show `TrailingBothZScoreBreakout` rows. **Exception unchanged**: `cache/watchlist_sweep.db` is a separate, never-migrated snapshot DB.

## Live trading — open items
- **`win_twin_rate` recalc / watchlist size** — not started.

## Live/backtest parity gap — real, unresolved (found 2026-07-08)
`TrailingBothZScoreBreakout` (100% of watchlist 9's live tickers) is deliberately excluded from `scripts/verify_live_parity.py`'s comparison — its own docstring says the trailing-buy "wait for bounce" entry state machine has no live implementation. Live only detects "z-score crossed trigger" and hands off bounce-timing to a **broker-side trailing-buy order** — nobody has verified the broker's real trailing-buy behavior actually resembles what the backtest kernel assumed. Single biggest unverified assumption behind every currently-live trade. (Backtest kernel itself confirmed correct on re-entry blocking — not the source of any bug found so far.)

## WSL/Windows sleep incident, 2026-07-08 — heartbeat mitigation incomplete
`active_signals.py` writes a heartbeat timestamp every loop iteration; `scripts/check_heartbeat.py` posts a Slack alert if it goes stale. **Incomplete**: the piece that makes this actually useful — a Windows Task Scheduler job (host-level, survives WSL suspension, fires on resume/unlock) invoking the checker — was never built. Nothing currently calls `check_heartbeat.py`.

## Live-trading reliability gaps (real, not yet built)
- **SMA/Std recalculated from scratch every poll** — `compute_buy_signal`'s `generate_daily_indicators()` recomputes the full rolling mean/std over the entire daily-close history on every 5-min poll, per node, even though it only depends on prior days. Backtest kernel already caches this correctly (`sma_arr`/`std_arr` computed once). Proposed, not implemented.
- **Default rule**: every action-requiring state change in `active_signals.py` must have a Slack notification — audit this against any new strategy/state added going forward.

## Test coverage
- **Automated round-trip DB test** for `active_signals.py` (`add_node`→`open_position`→`check_sell_condition`) — no test coverage exists for this path currently.

## Reference — backup/storage policy (2026-07-07, current)
- `trading_live.db`: hourly cron backup, keep 30 days (`cache/live_backups/`).
- `trading_universe.db` (research, regenerable): daily + weekly rotating single-file backups, 2 copies total, no accumulation.

## Deferred, lower priority
- Split `active_signals.py` into modules (1680+ lines, one file) — deferred, live-trading takes priority.
