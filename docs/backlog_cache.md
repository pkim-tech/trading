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

## Live/backtest parity gap — resolved on both entry and exit sides 2026-07-13, no broker fills needed
`TrailingBothZScoreBreakout`'s trailing-buy "wait for bounce" entry has no live-orchestration implementation (hands off to a broker-side trailing-buy order) — `scripts/verify_live_parity.py` deliberately can't compare it. Instead of waiting on real broker fill data, built `scripts/verify_trailing_buy_resolution.py`: re-detects every recent signal's bounce-entry using yfinance 5-min bars (real intra-hour tracking) and diffs against what the hourly-bar kernel (`_simulate_trail_both`) would catch for the same signal. After fixing a cutoff-time bug 2026-07-13 (see below), result across all 11 watchlist-9 tickers (130/130 signals matched, last ~58d): mean price diff +0.19%. **SOXL is still the real outlier**: +1.81% mean fill-price penalty (individual signals up to +15.5%), driven by its `trail_buy_pct=1%` being far tighter than its own ~3.65% median intra-hour swing (ratio 3.65) — volatile enough to cross/re-cross the trigger within an hour, so 5-min tracking locks in an earlier/worse fill than the hourly kernel models. TQQQ shows smaller +0.84% drift; everything else is at/near parity (AGQ actually skewed favorable, -2.01%). Formalized as a repeatable procedure in `docs/watchlist_candidate_checklist.md`.

Built the mirror-image check for the **exit** side 2026-07-13, `scripts/verify_trailing_sell_resolution.py`: same idea, but re-detects the peak/trail_stop crossing once trailing arms, using 5-min bars vs. the hourly kernel's trailing branch. Result: 21/21 exits matched, mean diff -0.17% — trailing-sell is already at parity across the whole watchlist (unlike entries, live trailing-sell is monitored continuously by `active_signals.py` itself, not handed off blind to a broker order, so this check mainly validates the *backtest's* hourly-bar exit modeling rather than a live-execution gap). LABU showed -4.6% on a single sample — not enough data to call a real outlier yet.

**Real bug found and fixed while building the sell-side script**: `max_hold_hours` counts hourly *bars* (~7/trading day), not calendar hours — the original buy-side script's cutoff-time math (`signal_time + timedelta(hours=max_hold_hours)`) was computing a cutoff days too early for any trade near its actual max-hold window, silently reporting fabricated "ran out of data" exits instead of real ones. Fixed in both scripts (now look up the real bar timestamp via `timestamps[entry_i + max_hold_hours]`). Rerunning the buy-side script after the fix confirmed the original SOXL finding wasn't an artifact of this bug — numbers moved only slightly (130/130 matched vs. 134/138 with the shorter truncated dataset before the fix).

**Not fully closed**: both checks validate the *price* assumption is broadly sound (except SOXL entries); they don't validate the broker's trailing-buy order mechanics themselves (whether Schwab's own trigger/fill logic matches the running-low model at all) — that piece still has no real-fill-time-vs-signal-time verification (see also the fill-price/drift accuracy item above).

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
- **Rename `cache/` → `data/`** (raised 2026-07-12): the folder holds `trading_live.db` (real trade record, not reproducible) and `trading_universe.db`/hourly CSVs (regenerable research data) — "cache" undersells what's actually in there, and gets more painful to fix the longer it's deferred (every new script/page adds another `DB_PATH`/`CACHE_DIR` reference). Real blast radius: `active_signals.py`, `data_manager.py`, `data_collector.py`, every `pages/*.py`, most `scripts/*.py`, `.gitignore`, `CLAUDE.md`'s "Runtime Artifacts" section, plus any backup cron/Task Scheduler jobs pointing at `cache/`. User explicitly OK waiting; not started, no urgency, but flagged as an increasing-cost-over-time item, not a someday-maybe.
