# Backlog

## ✅ Backlog-hygiene pass, 2026-07-22 — five stale headings closed out, retroactively marked resolved
Found while doing a dedicated backlog-hygiene pass (flagged as a to-do at the end of the
prior session): these five `backlog_cache.md` headings described work that had actually
already been completed (sometimes by the item's own later progress notes, sometimes by a
separate later session), but the heading itself was never updated to say so — each one below
gets its own entry with the real resolution date and pointer.

## ✅ [live-trading][backtest] Resolved 2026-07-18 — v4 nodes (SL=1%, all 19 candidates) promoted into the real `watch_list` table, replacing v3.x SL=15% params
Heading previously read "High priority... not started, no `watch_list` rows changed yet" —
stale; its own later progress notes in the same entry already documented the real work: all
19 walk-forward-screened tickers' v4 winning nodes (`fixed_sl=1%`, pulled from
`backtest_cache` ranked by robust alpha) were inserted into watchlist 57 via direct SQL
(worked around the `add_node` `fixed_sl` bug, separately fixed the same session), each
annotated walk-forward-clean or flagged. All landed in `mode='research'`, not `mode='live'` —
"promoted into `watch_list`" meant the table, not live execution status; live/paused status is
tracked separately (see the watchlist-65 entry below).

## ✅ [live-trading] Superseded 2026-07-20 — watchlist 65 ("Live v5") is now the active watchlist, not watchlist 57 ("Live v4", GDXD+EDC only)
Heading previously described watchlist 57 (GDXD+EDC only) as "Active" — that was accurate as
of 2026-07-18 but is stale now. Watchlist 65 ("Live v5", 10 tickers: AGQ, DPST, GDXU, HIBL,
KORU, NUGT, SOXL, UDOW, USD, YANG) became the active watchlist 2026-07-20, selected from the
full 18-ticker v5 resweep by cliff-safe robust alpha (see the 2026-07-20 watchlist-65 entry
above). All 10 nodes are still `mode='research'` — nothing trades live yet. Watchlist 57's 18
v4 nodes are preserved, untouched, just no longer polled (same non-destructive
versioning pattern as every prior watchlist supersession). Always check `watchlists.is_active`
rather than trusting a hardcoded id anywhere in docs.

## ✅ [backtest] Resolved 2026-07-15/19/20 — trailing-buy fill logic kernel-correctness fix, executed in full across three sessions
Heading previously ended "Action needed: implement per the plan file. Not started" despite its
own later progress notes showing the opposite. Real timeline: kernel implemented 2026-07-15
(`_simulate_trail_both` computes possible/pessimistic/certain per node, island/cliff-safety
rank on `MIN` of the three, verified via `verify_v4_fill_bounds.py` across all 11 live nodes at
the time); `phase` column backfilled for the pre-tagging SOXL/KORU rows via
`scripts/backfill_v4_phase.py` (confirmed run clean, 0 rows left untagged — `docs/
conversation_summary.md` session ~12); gap-through-trigger entry-side fix 2026-07-19 and
exit-side fix 2026-07-20 (both in `CLAUDE.md`'s Key Files); full 18-ticker v4/v5 resweep under
the corrected kernel completed 2026-07-20. Nothing outstanding from the original plan remains.

## ✅ [backtest] Resolved 2026-07-17 — `entry_timing=open_check` live-actionable analog built
Heading previously said "Not started, not scoped beyond the shape above" — stale; the exact
proposed fix (a second poll window per signal time, ~9:31-9:40/14:31-14:40 alongside the
existing close-check windows, reusing `compute_buy_signal`, with `buy_alerted` dedup
preventing a double-fire) was built the same session as the GDXD promotion (`active_signals.
_OPEN_CHECK_WINDOWS`, `watch_list.entry_timing` column). Confirmed still live and in use today:
all 18 watchlist-65/57 v4 nodes use `entry_timing='open_check'` (`CLAUDE.md`'s Live Trading
section).

## ✅ [live-trading][security] Status corrected 2026-07-22 — one-account-per-ticker: infra built, rollout ongoing (not "not started")
Heading previously ended "Not started, no code changes yet" — stale; substantial
infrastructure now exists. `schwab_safety.ACCOUNTS` holds 4 account slots (brokerage/sep/
roth/ira) each with its own `AccountLimits` (`notional_cap`, `daily_order_cap`, `account_type`);
`watch_list.account` is a real per-node column; `_live_ticker_accounts()` derives the live
ticker→account mapping fresh from `mode='live'` rows (currently empty — no ticker is
`mode='live'` yet, so nothing is actually exercising per-ticker isolation live). The originally
floated "account nickname = ticker symbol" placeholder naming was not what shipped — actual
accounts are named by role (brokerage/sep/roth/ira), with `watch_list.account` doing the
ticker→account assignment instead. A 5th account (limited-margin IRA, funded 2026-07-22) is
queued to join this set once API token scope + compliance permission clear (see
`project_new_ira_account_status` memory) — rollout is incremental, not a single completed
migration, so this item stays open in spirit (tracking new-account onboarding) even though the
core mechanism is done.

## ✅ [live-trading][security] Resolved 2026-07-22 — `same_day_block` account-type-awareness
`schwab_safety.AccountLimits` gained `account_type` (`'cash'` or `'margin'` — regular and IRA
limited margin treated identically, since both lack the T+1 cash-settlement restriction the
same-day-rebuy check exists for). `ACCOUNTS`: brokerage is `'margin'`; sep/roth/ira are `'cash'`,
confirmed by the user. `check_order`'s same-day-rebuy check now only fires for `'cash'` accounts.
A blanket "real orders only in a confirmed margin account" gate was considered and explicitly
rejected mid-build (caused 2 real test failures before being reverted) — the user's account model
is one account per ticker, capital-capped and expanded by funding a new account rather than
liquidating an existing one (see `project_account_segregation_model` memory), so a hard
margin-only gate would've locked automation out of every existing account. 1 new test
(`test_same_day_rebuy_not_blocked_in_margin_account`). The new (5th) limited-margin IRA funded
2026-07-22 isn't in `ACCOUNTS` yet — blocked on Schwab API token scope + compliance trading
permission. Full suite: 177 passed.

## ✅ [backtest] Resolved 2026-07-22 — `auto_adjust`/split-guard reconciliation closed; data traceability (not full immutability) chosen as the design direction
Reconciled the open 2026-07-16 question (why did `data_manager.py`'s split-guard rescue still
fire for KORU if `yf.download()` already defaults to `auto_adjust=True`?): `auto_adjust=True`
only adjusts the window being fetched *right now* for corporate actions known as of today, not
rows already sitting in the local cache from a prior fetch — a new split therefore still produces
a scale cliff between stale-scale cached rows and new-scale freshly-fetched rows, which is exactly
what the split-guard detects and rescales the whole file to fix. Guard is real work, not dead
code. Since the rescale is one multiplicative factor across the whole series, downstream
%-based signals (z-score, SL/TP/arm, returns) are scale-invariant to it, so past `backtest_cache`
numbers should stay reproducible by re-running the same code. Corrected the stale in-code comment
at `data_manager.py:113-124` that had claimed the opposite (that yfinance's hourly interval isn't
retroactively split-adjusted at all). Full writeup: `docs/research_log.md`'s 2026-07-22 entry.
**Real, distinct gap raised in the same discussion, backlogged (medium priority, doesn't block
trading) rather than built**: no archived record of the exact cache-file data that fed any past
backtest — mutated in place on every split-guard rescale. User's explicit call: traceability (know
when/why/how data changed) over full immutable/versioned data linked to each `backtest_cache` row
— the latter would be real reproducibility but a much bigger lift (schema change across the whole
sweep engine, real storage growth), and isn't needed if the scale-invariance argument above holds.
Design agreed for later: a `data_mutation_log` table in `trading_universe.db` (via `db_cache.py`),
one row per split-guard rescale event with a `pre_mutation_snapshot` of the actual old data (not
just metadata about the change) — cheap since these events are rare. Not yet built.

## ✅ [live-trading][backtest] Resolved 2026-07-20 — watchlist 65 candidate testing complete; found+fixed a live/paper compliance gap and a `create_watchlist` id-burning bug
Full writeup in `docs/research_log.md` ("2026-07-20 (cont.) — Watchlist 65 candidate
testing: chaos-monkey, Stage 5 compliance gap found+fixed, watchlists.id gap
root-caused+fixed"). Short version: chaos-monkey execution-adherence extended to cover
`TrailingExitZScoreBreakout` and `open_check` (previously TB/close-only), repointed at
watchlist 65 — no node collapses even at 20% miss rate, USD's thin sample cleared. Stage
5 compliance recheck found `strategies.py::check_exit` (used by both real live positions
and paper trading) never received the 2026-07-20 exit-side gap-through-trigger kernel
fix — fixed by threading a real `open_price` through `check_sell_condition` from both
call sites and adding the Open-first gap check to `check_exit`. Verified via full
bar-by-bar parity against real historical trades (280 total across SOXL/AGQ, effectively
zero mismatches). Also found+fixed, while investigating the unexplained watchlist id
57->65 jump: `signals_db.create_watchlist`'s `INSERT OR IGNORE` silently burns an
AUTOINCREMENT id on any duplicate-name call even though no row is written (reproduced
directly) — fixed to check existence first. No node params changed; watchlist 65 is
candidate-tested and clear of blockers.

## ✅ [backtest] Resolved 2026-07-20 — last-window market-on-close vs trailing-buy comparison, MOC does not win
Full writeup in `docs/research_log.md` ("2026-07-20 — Last-window market-on-close vs
trailing-buy comparison"). Short version: for signals firing in the last daily window
(the 14:30 bar, no time left before the overnight gap), compared TB's real bounce-fill
against an MOC counterfactual entry at the same signal bar's Close, across all 4
`TrailingBothZScoreBreakout` tickers in the Live v5 watchlist (GDXU, HIBL, SOXL, USD) and
all 3 fill resolutions (possible/pessimistic/certain). TB won every ticker/resolution,
often by 5-20x on compounded return, driven by a fat right tail of large winners MOC's
earlier entry misses. Hand-verified 2 SOXL trades entry-to-exit against real OHLC bars —
every fill price traced exactly to a real Open/High/Low/Close. No 18-ticker backfill
run — the result was decisive enough on the current watchlist alone; treated as closed.
New tooling: `scripts/sim_close_vs_trail_buy.py` + `export_trades.
collect_last_window_comparisons`/`simulate_trail_both_signal_tracked`/
`_simulate_exit_from_entry`.

## ✅ Resolved 2026-07-20 — full 18-ticker v5 resweep completed; immediate-entry vs trailing-buy comparison; "Live v5" watchlist built

The interrupted resweep (started 2026-07-20, see prior entry below) was completed the same
day using the new per-ticker resumable queue tooling (`scripts/run_sweep_queue.sh` +
`scripts/campaign_config.py`) instead of the old batch `run_v4_full18_resweep.sh`/
`run_v5_full18_test.sh` scripts. Full writeup, including the immediate-entry
(`TrailingExitZScoreBreakout`) vs trailing-buy (`TrailingBothZScoreBreakout`) cliff-safety
comparison and two rounds of self-caught errors in that comparison (unfiltered-alpha
mistake, then a summary-sentence contradicting the very table it was drawn from), is in
`docs/research_log.md` ("2026-07-20 — Immediate-entry (TrailingExit) vs trailing-buy
(TrailingBoth) full 18-ticker v5 comparison").

New standing tool: `scripts/campaign_comparison_table.py` — prints the side-by-side
TB-vs-TE comparison table (best alpha, worst_neighbor/cliff status, full node params,
trades, win rate, liquidity) directly from `backtest_cache`, with a `--min-best` filter.
Built because this exact table got hand-rebuilt piece by piece several times in
conversation before being made a real script — use this going forward instead of
re-deriving it ad hoc.

**Outcome**: 10 tickers selected for a new "Live v5" watchlist (`watchlist_id=65`,
created+activated this session, superseding `watchlist_id=57` "Live v4"): AGQ, DPST,
GDXU, HIBL, KORU, NUGT, SOXL, UDOW, USD, YANG — split 4 `TrailingBothZScoreBreakout`
(GDXU/HIBL/SOXL/USD) and 6 `TrailingExitZScoreBreakout` (AGQ/DPST/KORU/NUGT/UDOW/YANG),
picked per-ticker by whichever strategy had the higher cliff-safe robust alpha at
`fixed_sl∈{1,2,3}`. TQQQ and LABU had a viable (cliff-safe) TE node but were dropped by
user call anyway. DUST/GDXD/NAIL/RETL/UVIX/ZSL excluded entirely — no cliff-safe node in
either strategy at this SL range. All 10 new nodes inserted via
`scripts/build_v5_watchlist.py` in `mode='research'` (not live, not automation-enabled) —
see the still-open candidate-testing item in `docs/backlog_cache.md` for what's needed
before any of these go further.

Also fixed while building the comparison table: `TrailingBothZScoreBreakout`'s swept
"stop_loss" grid axis is actually `trail_buy_pct` (real `stop_loss` column holds the
constant `fixed_sl`) — an ad hoc query that used the literal `stop_loss` column for the
cliff-neighborhood check produced numbers that disagreed with the real
`identify_full_mesh_candidates` log output until this was caught and fixed.

## ✅ [live-trading][security] Resolved 2026-07-21 — SELL-side automated-order attempt is now mode-gated, not just ticker-gated

Found 2026-07-19 while reviewing whether EDC (real open position, `research`-mode node) could
safely join the widened automation scope. `notify_trailing_activated` (`signals_notify.py`) is
called unconditionally from the real `open_positions` exit-check loop in `run_loop` and calls
`_attempt_automated_sell(pos, current_price)` with no check on the position's node `mode` —
only `_attempt_automated_sell`'s own `ticker not in AUTOMATION_ENABLED_TICKERS` gate stood
between a `research`-mode ticker's real open position and a real/dry-run automated sell
attempt. The BUY side didn't have this gap: `_scan_buy_signals` only reaches
`_attempt_automated_buy` when `node.get('mode')=='live'`. This is why EDC's node was removed
from `watch_list` rather than folded into the widened automation scope at the time — adding
its ticker to `AUTOMATION_ENABLED_TICKERS` while its real position's node was still `research`
would have exposed that position's exit to the automated-sell path.

**Fixed 2026-07-21**: `_attempt_automated_sell` now looks up the position's own `(ticker,
window)` node from `db.get_watchlist()` and only proceeds when a matching node exists with
`mode=='live'` — falling back to the manual flow (not KeyError) both when the mode doesn't
match and when no matching node exists at all (e.g. a since-removed node, mirroring EDC's
removal). Matches `automation_principles.md` #7 (new automation surfaces inherit the full
existing gating, not a subset). Two new tests in `tests/test_schwab_automation.py`:
`test_automated_sell_falls_back_when_node_mode_not_live`,
`test_automated_sell_falls_back_when_no_matching_node`. Full suite: 156 passed.

## [live-trading] Low priority, 2026-07-19 — paper-trading dedup is ticker-only, not `(ticker, window)`-aware

`paper_trading.start_paper_buy`'s dedup guard (`db.get_paper_pending_buy(ticker)` /
`db.get_open_position(ticker, paper=True)`) is a single-ticker lookup — unlike the real
`open_position()`, which dedups on `(ticker, window)` (`signals_db.py:737-738`, itself not
`node.id`-based, so two nodes sharing a `window` value would still collide there too). Fine
today since every automation-enabled ticker has exactly one node. Would need to become
`(ticker, window)`-aware before a ticker could ever run two nodes (e.g. a legacy v3.x node
alongside a new v4 node) through paper trading simultaneously — the second node's BUY
signal would otherwise be silently dropped as a false "already open" duplicate. Not
scoped, no ticker needs this today.

## Default rule: Slack-notify on every action-requiring state change (2026-07-07)

Established as a standing convention, not a one-off feature: any state transition in `active_signals.py` that requires the user to do something (place/cancel an order, confirm a fill, etc.) must have a corresponding Slack notification. Prompted by HIBL's arm-sell threshold crossing without an alert — but on investigation, the notification already exists (`notify_trailing_activated()`, fires on `just_activated_trailing` at `active_signals.py:1582-1583`) and today's miss was purely because the daemon was down the whole time since HIBL's buy signal (same root cause as the heartbeat/watchdog gap below), not a missing notification path.

Current coverage, for reference: buy signal (`notify_buy_signal`), sell signal/SL/TP/TIME/TRAIL (`notify_sell_signal`), trailing armed (`notify_trailing_activated`), limit fill (`notify_limit_filled`) all exist and are wired into `run_loop`. Action item going forward: any new state/strategy added to `active_signals.py` must include a matching Slack notification before being considered done — audit this against the rule at that time, don't assume coverage.

## Out-of-band heartbeat/watchdog for active_signals.py (2026-07-07)

Daemon crashed today mid-session (the `tickers`-table bug during the DB split) and there was no independent alert — Slack itself is posted *by* the daemon, so it can't reliably alert on its own death. Needs a separate standalone process: a lightweight watchdog (own process, cron or sleep-loop) that checks a heartbeat (daemon touches a file/DB row every poll cycle) and posts to Slack via its own independent webhook call if the heartbeat goes stale during market hours — so a bug in the main daemon can't take down its own alerting. Not started.

## ✅ Resolved 2026-07-12/13 — "What's close" proximity script

Covered by a different mechanism than originally scoped: the reference report's `build_reference_table` (`active_signals.py`) already shows per-ticker buy-trigger/arm-trigger distance (signed `Proximity` column) for every `mode='live'` node, and the 2026-07-12 "Resend Report" button lets the user trigger a fresh one on demand from Slack — no standalone script or slash command needed. Live-verified via multiple real sends.

## ✅ Resolved 2026-07-13 — Add account tracking (Brokerage/SEP/IRA/Roth) for portfolio performance

Built as originally scoped: `account` column added to both `open_positions` and `trade_log` (`trading_live.db`, migration in `ensure_tables()`, backup taken first — `cache/trading_live.db.bak_pre_account_migration_20260713`), threaded through `open_position()`/`log_trade_entry()` so the account is captured at execution time from the node's current `watch_list.account` value (survives later account reassignment, e.g. LABU's ira→roth move — old trades keep the account they were actually placed in). `pages/10_Open_Positions.py` shows an Account column. `pages/4_Portfolio.py` gained a new "Account Performance (live)" section: per-account realized trade count/win rate/compounded return from `trade_log`, plus open-position count/unrealized $ P&L from `open_positions` (current price via yfinance). Smoke-tested (HTTP 200, no traceback) and spot-checked against the real DB.

**Caveat**: only trades placed 2026-07-13 onward carry a real account value — the 4 positions open before the migration (AGQ/HIBL/EDC/SOXL) show as `unknown` since `account` was NULL at insert time; no historical backfill is possible (the value wasn't captured anywhere before now). The Portfolio page surfaces this as a caption when `unknown` rows exist.

## ✅ Done 2026-07-07 (evening, while user away) — Split trading_universe.db into live/research files; folded Watchlist Trade Pivot into Top Pivot; trail_pct rename still deferred

**DB split**: Built `cache/trading_live.db` as a **copy** of `watchlists`/`watch_list`/`open_positions`/`trade_log` (4/47/2/3 rows) — `cache/trading_universe.db` untouched, still holds `backtest_cache`/`hurst_cache`/`tickers` plus the original (now-redundant) copies of the live tables. Repointed `active_signals.py`'s `DB_PATH` to `trading_live.db`, added `RESEARCH_DB_PATH` for the one `hurst_cache` lookup it makes. Repointed `pages/10_Open_Positions.py`, and split `pages/4_Portfolio.py`/`pages/0_Top_Pivot.py`/`scripts/post_sweep_report.py` into dual-path (live table queries → `trading_live.db`, `backtest_cache` queries → `trading_universe.db`); `pages/0_Top_Pivot.py`'s watch_list/backtest_cache join now uses `ATTACH DATABASE` across the two files (verified working). `scripts/verify_live_parity.py` and `scripts/live_test.py` needed no changes (already DB-path-agnostic via monkeypatch / re-import).

**Correction to the original split plan**: `kv_cache` stays in the research file, not the live one — every `db_cache.py::refresh_*_cache()` function populates it from `backtest_cache` queries and it's consumed only by research-side Streamlit pages (pivot/cliff-grid/dropdown caches), not by `active_signals.py` at all.

**Not yet cut over**: the already-running `active_signals.py run` process (started 06:04 same day) still has the old code loaded and is reading/writing `trading_universe.db`'s original live tables — it was deliberately left untouched (user explicitly declined a restart mid-day). The code changes above only take effect on next restart. **Before trusting `trading_live.db` in production**: restart `active_signals.py`, confirm it comes up reading the new file, then drop the now-redundant `watchlists`/`watch_list`/`open_positions`/`trade_log` tables from `trading_universe.db` (not done — old tables currently still exist there too, in sync as of the split moment but will drift once the new daemon starts writing to `trading_live.db` only).

**Watchlist Trade Pivot fold**: absorbed `pages/12_Watchlist_Trade_Pivot.py`'s WIN/LOSS/TWIN/TLOSS/OPEN summary + drill-down into a new section at the bottom of `pages/0_Top_Pivot.py` (still reading from the `cache/watchlist_sweep.db` sandbox, unchanged) and deleted the standalone test page. Verified both pages return HTTP 200 with no tracebacks via a headless Streamlit smoke test.

**trail_pct → trail_sell_pct rename**: still deferred, now for a sharper reason than "wait for off-hours" — traced through `active_signals.py`'s `run_loop` and found `check_sell_condition` is called with no try/except around it (`active_signals.py:1522-1541`), so renaming the column out from under the running daemon would cause an uncaught `KeyError` on the very next poll cycle and kill the entire process (no monitoring on any open position until manually restarted). Do this only immediately before a planned restart, not mid-session.

## ✅ Resolved 2026-07-12 — `trail_pct`/`take_profit` rename propagation

All previously-listed files done: `pages/2_Node_Inspector.py`, `pages/3_Winners.py`, `pages/4_Portfolio.py`, `pages/10_Open_Positions.py` (`take_profit`→`axis_tp`, `trail_pct`→`trail_sell_pct` in SQL), `scripts/export_cliff_safety.py` (same rename, plus fixed a pre-existing `sl_label`/`sl_display` NameError left over from the original commit), `scripts/verify_live_parity.py` (node dict key `trail_pct`→`trail_sell_pct` to match `active_signals.py`'s expected key). `scripts/fill_trail_pct_gaps.py` needed no change — doesn't touch those columns. **Note**: other pages (`8_ADF_Filter.py`, `11_Universe_Scan.py`, `1_Spatial_Topology.py`, `7_Hurst_Filter.py`, `scripts/profile_dispatch.py`, `scripts/post_sweep_report.py`, `scripts/top_safe_nodes.py`) still query raw `take_profit` but were never in scope — they only look at non-v3.x strategies where the column is still populated; revisit only if they ever need to show `TrailingBothZScoreBreakout` rows. **Exception unchanged**: `cache/watchlist_sweep.db` is a separate, never-migrated snapshot DB.

## ✅ Mostly resolved 2026-07-13 — Live/backtest parity gap (trailing-buy/sell fill-timing)

`TrailingBothZScoreBreakout`'s trailing-buy "wait for bounce" entry has no live-orchestration implementation (hands off to a broker-side trailing-buy order) — `scripts/verify_live_parity.py` deliberately can't compare it. Instead of waiting on real broker fill data, built `scripts/verify_trailing_buy_resolution.py`: re-detects every recent signal's bounce-entry using yfinance 5-min bars (real intra-hour tracking) and diffs against what the hourly-bar kernel (`_simulate_trail_both`) would catch for the same signal. After fixing a cutoff-time bug 2026-07-13 (see below), result across all 11 watchlist-9 tickers (130/130 signals matched, last ~58d): mean price diff +0.19%. **SOXL is still the real outlier**: +1.81% mean fill-price penalty (individual signals up to +15.5%), driven by its `trail_buy_pct=1%` being far tighter than its own ~3.65% median intra-hour swing (ratio 3.65) — volatile enough to cross/re-cross the trigger within an hour, so 5-min tracking locks in an earlier/worse fill than the hourly kernel models. TQQQ shows smaller +0.84% drift; everything else is at/near parity (AGQ actually skewed favorable, -2.01%). Formalized as a repeatable procedure in `docs/watchlist_candidate_checklist.md` (checks 2/3).

Built the mirror-image check for the **exit** side 2026-07-13, `scripts/verify_trailing_sell_resolution.py`: same idea, but re-detects the peak/trail_stop crossing once trailing arms, using 5-min bars vs. the hourly kernel's trailing branch. Result: 21/21 exits matched, mean diff -0.17% — trailing-sell is already at parity across the whole watchlist (unlike entries, live trailing-sell is monitored continuously by `active_signals.py` itself, not handed off blind to a broker order, so this check mainly validates the *backtest's* hourly-bar exit modeling rather than a live-execution gap). LABU showed -4.6% on a single sample — not enough data to call a real outlier yet.

**Real bug found and fixed while building the sell-side script**: `max_hold_hours` counts hourly *bars* (~7/trading day), not calendar hours — the original buy-side script's cutoff-time math (`signal_time + timedelta(hours=max_hold_hours)`) was computing a cutoff days too early for any trade near its actual max-hold window, silently reporting fabricated "ran out of data" exits instead of real ones. Fixed in both scripts (now look up the real bar timestamp via `timestamps[entry_i + max_hold_hours]`). Rerunning the buy-side script after the fix confirmed the original SOXL finding wasn't an artifact of this bug — numbers moved only slightly (130/130 matched vs. 134/138 with the shorter truncated dataset before the fix).

**Still not fully closed** (residual, carried forward, no dedicated tracking item since — fold in if this area comes up again): both checks validate the *price* assumption is broadly sound (except SOXL entries); they don't validate the broker's trailing-buy order mechanics themselves (whether Schwab's own `TRAILING_STOP` trigger/fill logic matches the running-low model at all) — that piece still has no real-fill-time-vs-signal-time verification against actual Schwab fills.

## ✅ Resolved (db round-trip coverage built) / Dead (Task Scheduler piece dropped) 2026-07-13/14 — Test coverage & heartbeat watchdog

**Automated round-trip DB test** (`add_node`→`open_position`→`check_sell_condition`, no coverage existed as of 2026-07-13): built — `tests/test_db_roundtrip.py` and `tests/test_signal_and_notifications.py` now cover this path. Resolved.

**Heartbeat/Task Scheduler watchdog** (`scripts/check_heartbeat.py`, posts a Slack alert if `active_signals.py`'s heartbeat file goes stale — meant to catch the daemon going silent, e.g. host sleep/suspend, without relying on the daemon itself to notice its own death): scoped in full (Task Scheduler setup walkthrough, "run only when logged on" vs. password-store tradeoff, retry/missed-start behavior) then **deliberately dropped 2026-07-13** — for the failure modes it would catch (sleep/network/power), the user has no way to act on the alert remotely while at work, so the alert would be pure unactionable stress. Root cause (WSL sleep during market hours) fixed directly via a Windows power-plan change instead. `check_heartbeat.py` itself was left ~80% built and still works standalone if ever revisited — same-session fix wrapped `main()` so an unhandled crash in the check itself also posts a Slack alert, not just the two originally-expected stale/missing paths. See `docs/design.md`'s Heartbeat section for the current authoritative note.

## ✅ Resolved 2026-07-14 — trailing-buy re-entry timing after a same-day exit

No bug — same-day re-entry uses the exact same two daily signal windows as any other entry
(`_simulate_trail_both`'s new-signal detection has no calendar-day reset or first-entry-of-
day special-casing). Full experiment writeup in `docs/research_log.md` (2026-07-14 entry).

## ✅ Resolved 2026-07-15 — Phase 3 (full parameter mesh) adds no value over Phase 1/2/2.5

Phase 3 won 0/30 tagged SOXL+KORU v4 SL-sweep campaigns — Phase 1 or Phase 2 always held
the best robust-alpha node. `--max-phase` cap added to `run_optimization_sweep.py` (default
3, unchanged behavior) so future runs can skip it. Full experiment writeup in
`docs/research_log.md` (2026-07-15 entry).

## ✅ Resolved 2026-07-17 — same-day buy→sell block explored and deliberately not built

PDT rule eliminated by FINRA effective 2026-06-04 (Regulatory Notice 26-10); no broker or
regulatory reason for a same-day round-trip block remains. Real cost of blocking anyway is
high (GDXD retains only ~47% of edge if same-day exits are deferred to the next day).
Decision: proceed without a block. `schwab_safety.py`'s existing `same_day_block` (blocks
same-day *re-buy*, a different, unrelated direction) is untouched, still enforced live.
Full experiment writeup in `docs/research_log.md` (2026-07-17 entry).

## ✅ Done 2026-07-07 — Composite index `idx_bc_ticker_strategy_version` on `backtest_cache`

Added `CREATE INDEX idx_bc_ticker_strategy_version ON backtest_cache(ticker, strategy, version)` to both `cache/trading_universe.db` (86.2M rows, 430s to build) and the new `cache/watchlist_sweep.db` sandbox (34.7M rows, 49s). Found while an ad-hoc `ticker IN (...) AND strategy=? AND version LIKE 'v3.%'` query was scanning most of the table — `EXPLAIN QUERY PLAN` showed it falling back to the PK's autoindex (`strategy` is the PK's only usable leading column for that filter shape); none of the 4 existing secondary indexes had `ticker` paired with `strategy`/`version`. This index makes that exact filter shape (ticker-set + strategy + version-prefix) a pure index scan going forward.

## Watchlist-scoped trade-cache sandbox — new pattern, to formalize/absorb later (2026-07-07)

Built `cache/watchlist_sweep.db` as a disposable snapshot (not part of the live/research split below — a throwaway sandbox for prototyping) containing: a scoped `backtest_cache` subset (`TrailingBothZScoreBreakout`, v3.x, the 11 current watchlist tickers only — 34.7M rows), a copy of `watch_list` for `watchlist_id=9` ("Sweep v3 - Full"), and a new `trade_cache` table (one row per individual trade — entry/exit time/price, hours_held, Result WIN/LOSS/TWIN/TLOSS/OPEN, Return — keyed by full node identity) populated by running the actual kernel (`backtester.run_backtest_dispatch`) once per node rather than trusting `backtest_cache`'s aggregate `win_rate`/`win_twin_rate` columns (which can be stale — see finding below).

Built `pages/12_Watchlist_Trade_Pivot.py` as a **test page** against this sandbox: per-node WIN/LOSS/TWIN/TLOSS/OPEN breakdown + compounded return, with a drill-down into individual cached trades. User's explicit plan: test it standalone, then absorb into the existing `pages/0_Top_Pivot.py` later rather than keeping it as a separate permanent page.

**Related finding, not a new bug**: `backtest_cache.win_twin_rate` reads as `0.0` for any row computed before that column existed (added in commit `252b3bf`) — `run_optimization_sweep.py:66-72` explicitly documents old rows are never recomputed retroactively. SOXL's v3.18 rows are affected. The `trade_cache` sandbox sidesteps this entirely by computing fresh from the kernel instead of reading the cached aggregate.

**Open question**: should this sandbox pattern (scoped snapshot + trade_cache) become the permanent shape of the "watchlist" tier in the live/research split below, or stay a one-off testing tool? Not decided.

## ✅ Closed 2026-07-07 — SOXL/KORU live exit-strategy decision (overtaken by events)

Was an open question about whether to switch SOXL/KORU's live exit params to better-backtesting candidate configs (SOXL v3.35, KORU v3.34) — see prior analysis in git history if ever needed. Overtaken by the 2026-07-07 market swing: SOXL hit its real stop-loss and was closed out (-15.38%, logged in `trade_log`); KORU also breached its stop but the user chose to hold through it manually, working the position directly rather than via a config swap. No longer an open decision.

## Rename trail_pct → trail_sell_pct; also take_profit → trail_arm_pct for TrailingBoth; split buy/sell display columns (2026-07-06, updated 2026-07-06)

`trail_pct` (exit-side trailing %) and `trail_buy_pct` (entry-side bounce %) are asymmetrically named — the sell-side one kept its original name from `TrailingExitZScoreBreakout` (v1.8, back when there was only one trailing mechanism), while the buy-side one got a distinct name when `TrailingBothZScoreBreakout` added a second trailing axis. User wants `trail_pct` renamed to `trail_sell_pct` for symmetry/clarity, so buy-side and sell-side code/display never need to be parsed apart from a combined string.

**Added while walking through a full `TrailingBothZScoreBreakout` trade end-to-end (2026-07-06, investigating why SOXL v3.35 showed a 6837% alpha vs v3.18's 2894% — turned out to be real market data, a genuine +182% single trade during SOXL's documented April 2026 rally, not a bug)**: `take_profit` is also mis-named for this strategy — it doesn't exit the trade, it arms the trailing-sell mechanism once cleared (`check_exit` sets `state['trailing']=True` at that threshold, then rides `peak - trail_pct%` from there). Rename to `trail_arm_pct` for this strategy's semantics. Full sequence should read, in order: `trail_buy_pct` (bounce entry confirmation) → `trail_arm_pct` (threshold that arms the trailing sell, was `take_profit`) → `trail_sell_pct` (trailing-stop width, was `trail_pct`).

Decided explicitly **not** to physically reorder the DB columns to match this sequence — SQLite column order doesn't affect queries, only reordering would require a full rebuild of the 146M-row `backtest_cache` table (same risk class as the REINDEX incident earlier this session). Instead: rename only, and get the bounce→arm→trail-sell reading order via explicit column ordering in `SELECT` statements and the Cliff Safety CSV export, not the physical schema.

Scope (real column in 3 DB tables — **`open_positions` currently holds live KORU/SOXL positions**, so this must not be done carelessly mid-trade):
- DB: `ALTER TABLE ... RENAME COLUMN trail_pct TO trail_sell_pct` on `backtest_cache`, `watch_list`, `open_positions` (SQLite supports this natively, no full rebuild). Also consider renaming `take_profit` → `trail_arm_pct`, though note `take_profit` is a genuinely shared/generic axis column reused with real take-profit semantics by other strategies (`ZScoreBreakout`, `LimitExitZScoreBreakout`, etc.) — a straight column rename would mis-name it for those, so this likely needs to stay a per-strategy *display/label* rename rather than a DB column rename, unless a fuller audit says otherwise. **Back up the DB file first**, per established practice ([[feedback_backup_before_schema_migration]]).
- Code: rename the `trail_pct` param/column references across `strategies.py`, `backtester.py`, `run_optimization_sweep.py`, `active_signals.py`, `pages/0_Top_Pivot.py`, `pages/2_Node_Inspector.py`, `pages/3_Winners.py`, `pages/4_Portfolio.py`, `scripts/export_cliff_safety.py`, `scripts/fill_trail_pct_gaps.py`, `scripts/verify_live_parity.py`, `tests/test_TrailingBuyZScoreBreakout.py` — do NOT touch `trail_buy_pct` (no substring collision, but double check regex/replace scope).
- Also fix `scripts/export_cliff_safety.py`'s/`pages/0_Top_Pivot.py`'s combined `sl_display` ("Bounce % / Trail %" as one string) to output separate `trail_buy_pct`/`trail_sell_pct` numeric columns instead, ordered bounce→arm→trail-sell — this was the immediate trigger (Excel-facing CSV was mixing buy and sell values into one field).
- Verify KORU/SOXL still read back correctly from `open_positions` after the rename before considering it done.

Deferred until after live positions are settled or clearly off-hours — not done same-session as opening those two positions.

## ✅ Done (mostly) — Split trading_universe.db into separate live/research DB files (2026-07-06, cutover verified 2026-07-07)

`cache/trading_live.db` (watchlists, watch_list, open_positions, trade_log) and `cache/trading_universe.db` (backtest_cache, hurst_cache, tickers, kv_cache) now fully split, `active_signals.py` repointed and verified reading/writing the live file exclusively (confirmed via `ensure_tables()` + live position exit-check exercise). **Not yet done**: the 4 stale duplicate live tables (`watch_list`/`watchlists`/`open_positions`/`trade_log`) are still physically present in `trading_universe.db`, unused — a backup exists (`cache/trading_universe.db.bak_pre_table_drop_20260707`) but the actual `DROP TABLE` never ran. Safe to do once the `arm_sell_pct` migration (see rename item) finishes — don't run concurrent writes against the same file. Also cosmetic: the research file is still literally named `trading_universe.db`, not renamed to `trading_research.db`.

## Dead 2026-07-13 — Slack slash-command interaction for the live trading app (2026-07-06)

Originally scoped commands: `/positions` (check open positions), `/watchlist` (watchlist/buy-trigger status), `/status` (general daemon health). Superseded piecemeal by the button-based interaction pattern built across many sessions instead of a slash-command design: `/positions` ≈ `scripts/open_positions_status.py` + the manual open/close buttons (2026-07-12); `/watchlist` ≈ the reference report's Proximity column + on-demand resend button (2026-07-12, see the resolved proximity-script entry above). `/status` has no direct equivalent — resending the reference report and getting silence is a de-facto down-detector, but it's passive (relies on the user thinking to check) rather than a proactive alert. That gap is really the same problem as the still-open "Out-of-band heartbeat/watchdog" item above, not a slash command — closing this out as dead rather than carrying a stale command-design item forward.

## Split active_signals.py into modules (2026-07-06)

`active_signals.py` (1680+ lines) has grown to hold everything: DB connection/schema, watchlist CRUD, positions CRUD, signal computation, chart generation, Slack messaging, and the `run_loop` daemon + CLI, all in one file. Split in two passes, lowest-risk first:

1. **Pass 1 — watchlist + positions**: pull `get_watchlists`/`get_active_watchlist_id`/`create_watchlist`/`delete_watchlist`/`set_active_watchlist`/`get_watchlist`/`add_node`/`remove_node`/`set_node_mode`/`label_node` into `watchlist.py`, and `get_open_positions`/`update_position_trail_state`/`open_position`/`close_position`/`log_trade_entry`/`log_trade_exit` into `positions.py`. Both are pure DB CRUD, no coupling to signal logic — safest to extract first.
2. **Pass 2 — db + notify**: pull `_conn`/`ensure_tables` into `db.py` (shared base everything else imports), and the Slack/chart layer (`_post_message`, `_build_buy_blocks`, `_build_sell_blocks`, `notify_buy_signal`/`notify_sell_signal`/`notify_trailing_activated`/`notify_limit_fill`, `_chart_buy`/`_chart_sell`/`_upload_chart`) into `notify.py`.

Leaves `compute_buy_signal`/`check_sell_condition`/`run_loop`/CLI in `active_signals.py` (or a renamed daemon module) as the actual trading-logic core. Not started — deferred, live trading week takes priority.

## ✅ Done — Live-test the watchlist (2026-07-07)

Sweep 3 / "Sweep v3 - Full" watchlist is live and has been traded through a real market swing (SOXL stopped out, KORU held through a breach, HIBL entered and armed its trailing sell) — the priority this note flagged is complete.

**Milestone marker**: once the axis-schema cleanup (2026-07-05 — `strategies.py` class attributes + `validate_axis_values`, consolidating the 5 duplicated `_resolve_axis_columns`/`uses_fixed_sl` copies) and this live-test session both land, that's the end of `docs/operational_limits.md`'s Phase 1 (Manual Execution). Revisit then whether to relabel/split phases (e.g. current work retroactively "Phase 0" bootstrapping vs. a "Phase 1" that starts once Sweep 3 is the confirmed live watchlist) and define what Phase 2 actually covers (automation — see [[project_execution_automation_plan]]).

## Slack TEST MODE marker for manual notify_* testing (2026-07-05)

Found while manually testing `notify_buy_signal`/`notify_sell_signal` message rendering for SOXL/TQQQ: `SOCKET_MODE` is driven by real `.env` Slack credentials (bot/app token + `#trading` channel), so any ad-hoc script that calls a `notify_*` function posts to the real live channel with no indication it's a test — indistinguishable from an actual signal, including live Executed/Skipped buttons wired to real `open_position`/`close_position`. Add a `TEST_MODE` env var (or explicit param threaded through `_post_message`) that prefixes messages with something like "🧪 TEST MODE — " and swaps the action buttons for inert ones (or a plain-text context block) so manual testing can't be mistaken for — or accidentally trigger — a real trade.

## Automated round-trip test for active_signals.py (2026-07-05)

No test coverage exists for `active_signals.py`'s DB layer — `tests/` only exercises strategy kernels via fake dicts, never `add_node`/`open_position`/`check_sell_condition`. Found while quick-manually-testing the fixed_sl/trail_pct round-trip for a real v3.x trailing node (SOXL v3.18) and realizing there's no repeatable way to catch this axis-mapping bug class (already hit twice: the Portfolio page `load_watchlist_metrics` bug and the `dispatch_parallel_grid` `uses_fixed_sl` regression). Plan: pytest test monkeypatching `active_signals.DB_PATH` to a temp sqlite file, calling `ensure_tables()`, then driving `add_node()` with real v3.x trailing values → read back `watch_list` row → `open_position()` → read back `open_positions` row → `check_sell_condition()` with synthetic prices, asserting `trail_pct`/`fixed_sl`/`trail_buy_pct` survive each hop unmangled.

## 2026-07-05 (night) — win_twin_rate metric added; trail_pct sparse-then-fill extension; ALL53 ticker shorthand

- **Added `win_twin_rate` column to `backtest_cache`** (`run_optimization_sweep.py::init_idempotent_db`, simple `ALTER TABLE`, no PK rebuild needed): the existing `win_rate` only counts `Result=='WIN'` exactly, silently excluding profitable `TIME`-exit trades (`TWIN`) — found while investigating why a KORU node showed a 21% win rate despite yielding about the same alpha as a 71%-win-rate node (turned out 71% of its trades were actually profitable, just via `TWIN`, not counted). `win_twin_rate = (WIN+TWIN)/trades` is the real "did this trade make money" rate; old rows keep `win_twin_rate=0` (not recomputed retroactively — would require re-simulating all historical trades). Threaded through `dispatch_parallel_grid`'s cache-hit path, live-compute path, both INSERT statements, and displayed in `pages/0_Top_Pivot.py`'s Cliff Safety table alongside the original `win_rate` (kept, not replaced).
- **Extended `scripts/run_v3_backfill_sweep.sh`** with every single-percent trail_pct version 8-30% (`version = trail_pct% + 20` — same formula the existing 1-7% versions already followed, v3.21=1%...v3.27=7%) after finding `TrailingExitZScoreBreakout`'s (v3.18) full 1-30% sweep did much better at wide trail_pct (9-24%) than `TrailingBoth`'s tested 1-7% range. Sparse set (9/12/15/18/21/24/27/30% = v3.29/32/35/38/41/44/47/50) planned to run first; all in-between single-percent versions are wired and ready with no further script edits needed.
- **Built `scripts/fill_trail_pct_gaps.py`**: reads whatever sparse trail_pct data exists per ticker, finds each ticker's best value so far, and prints (doesn't execute) the `run_v3_backfill_sweep.sh` commands to backfill its immediate ±1% neighbors — mirrors the sweep engine's own coarse-then-island refinement, applied to the trail_pct axis. Already found (before any sparse run) that EDC/HIBL/SOXL's best trail_pct sits at the edge of the already-dense 1-7% range (7%), meaning 8% (v3.28) is worth checking once actually run.
- **Added an `ALL53` ticker-arg shorthand** to `run_v3_backfill_sweep.sh` (`./scripts/run_v3_backfill_sweep.sh <version> ALL53`) expanding to the full 53-ticker universe (same list as `run_v2_backfill_sweep.sh`), for running the v3.x strategy family against all 53 tickers instead of just Sweep 3's 11.

## ✅ Done 2026-07-05 — `backtest_cache` schema migration: real named columns + trail_pct as a true 4th axis (v3.x)

Built the "real named columns" option (see `docs/design.md`'s "v3.x reparameterization" section for full detail): `backtest_cache` schema rebuilt (60,364,303 rows verified carried over unchanged, no data migration needed — v1.x/v2.x rows keep their old overloaded meaning untouched), `stop_loss` now always means real SL going forward, `trail_buy_pct`/`trail_pct` are real columns. Went further than the minimal fix: `trail_pct` is now a genuine swept 4th grid axis for `TrailingBothZScoreBreakout` (`hyperparameters.trail_pcts`), replacing the old one-full-backfill-per-value pattern (v2.13-v2.17). Also fixed a pre-existing bug found along the way: Node Inspector/Portfolio only ever dispatched to `run_backtest_v17`-or-`run_backtest`, silently wrong for all 4 trailing strategies — now share one dispatch function (`backtester.py::run_backtest_dispatch`) with the sweep engine. `watch_list`/`open_positions` got a matching `trail_buy_pct` column; `add_node()` accepts real values for v3.x, falls back to legacy behavior for old nodes. v3.x backfill scope resorted and simplified same day (see `docs/design.md` "Version Changelog") — single combined tp/sl grid everywhere, TrailingBoth's trail_pct broken into per-value versions v3.21-27 instead of swept as a 4th axis in one run, restricted to Sweep 3's 11 tickers.

## ✅ Done 2026-07-05 — Fixed `add_node()`'s legacy fallback mis-mapping trail_buy_pct/trail_pct for `TrailingBothZScoreBreakout`

Found while asking "do any current watchlist winners sit in the 1/2/4/5 sl range" — the legacy fallback in `active_signals.py::add_node()` (added during the v3.x migration) always assumed the overloaded `stop_loss` value meant `trail_pct`, which is only true for `TrailingExitZScoreBreakout`/`LimitOrderTrailingExit`. For `TrailingBothZScoreBreakout` it actually means `trail_buy_pct`, with `trail_pct` a separate static per-version constant (v2.13=1%...v2.17=5%) that lived in `config.execution.trail_pct` at backfill time and can't be recovered from the row itself. All 8 Sweep 3 `TrailingBothZScoreBreakout` watch_list rows (AGQ/DPST/EDC/GDXU/HIBL/KORU/UVIX/YANG) had this backwards (`trail_buy_pct=0`, `trail_pct`=the real bounce value) — fixed in code (dispatches by strategy now, mirrors `run_optimization_sweep.py::_resolve_axis_columns`) and backfilled the 8 rows directly (backup at `cache/watch_list_backup_pre_trailfix_20260705.json`). No open position was affected (`open_positions` was empty throughout), but `check_sell_condition` would have used the wrong `trail_pct` for the first live trade on any of these 8 tickers. Also fixed `pages/0_Top_Pivot.py`'s "Cliff Safety" table, which displayed the raw overloaded `stop_loss` column with no indication of what it meant per strategy — now resolves and labels the real axis (SL / Bounce% / Trail% / Bounce%+Trail%) per row, and the neighbor-radius cliff query filters on the correct real column for v3.x rows instead of always `stop_loss`.

**Follow-up not done this session**: `pages/0_Top_Pivot.py`'s "Watchlist — Alpha by Strategy" section only queries `watchlist_id=1` and joins `backtest_cache`/`watch_list` on raw `b.stop_loss = w.stop_loss` — fine for that watchlist's real-SL strategies, but would break if it's ever extended to Sweep 3 (`watchlist_id=5`) or any future v3.x trailing-strategy node without the same axis-aware join fix.

## ✅ Done — Watchlist Repick (2026-07-04 → concluded 2026-07-07, Sweep 3/"Sweep v3 - Full" is live)

Was reviewing v2.x sweep results to repick the live watchlist (AGQ/EDC/FAS/HIBL). Entry/exit shorthand for reference: **Close** = bar-close confirmed entry, **Limit** = intrabar touch entry, **Trail** = trailing-buy bounce entry; **Fixed** = market exit at TP/SL, **Trail** = trailing-stop exit, **Limit** = limit-order exit. Existing versions: Close/Fixed=v1.5/2.5, Close/Trail=v1.8/2.8, Limit/Fixed=v1.7/2.7, Close/Limit=v2.12, Trail/Fixed=v1.9/2.9, Trail/Trail=v1.10/2.10. Concluded with the current v3.x, 10-ticker Sweep 3 watchlist now live. The "Research:" ideas below this point (post-loss cooldown, gap risk, seasonal patterns, etc.) are separate standalone research questions, still open.

Todo:
- **✅ Resolved 2026-07-05 — giving up on v2.4 (`TrendFilteredZScore`, 50-day SMA trend filter) for now**: didn't produce substantive signals. Not pursuing further.
- **✅ Resolved 2026-07-05 — giving up on limit-based order entry/exit variants for now**: v2.7 (`LimitOrderZScoreBreakout`) root-caused as structurally underperforming (see High Priority section), and unbuilt variants (Limit/Limit, Trail/Limit, v1.7-2, v1.7-3) aren't giving better signals than the close/trailing-based strategies already in place — same verdict as Hurst/ADF. Not pursuing further.
- **✅ Resolved 2026-07-05 — varying trailing-buy-entry % / trailing-sell-exit % already covered by v3.21-27**: `trail_buy_pct` is swept via the `sl` grid axis (combined grid incl. 1/2/4/5 low-end points) and `trail_pct` is swept 1-7% across the seven v3.21-27 versions (currently running backfill). No separate work needed.
- Revisit the design review list (`docs/code_review_findings.md`).

Research:
- **9:30am bar inclusion**: redoing charts/backtests to use the 9:30am bar (currently first bar is 10:30) is a big effort — changes every trading day from 7 bars to 8, requiring recalculation of max-hold-hours-to-bar-count conversions throughout `backtester.py`/sweep grids, plus re-fetching/re-caching hourly data with the new boundary. Flagged as research, not a quick todo.
- **Post-loss cooldown / trade freeze per ticker (2026-07-04)**: empirically checked the live watchlist's (AGQ/EDC/FAS/HIBL, v1.5 params) actual backtest trades — a large majority of losing exits are followed by a same-ticker re-entry within days (many same-day, in the next signal window), often at a *lower* price than the exit, not a recovered one. Mechanism: a stop-loss exit means price kept moving away from the entry, which against a lagging rolling SMA/std often makes the z-score deviation *larger*, so the same signal re-triggers almost immediately — the strategy has no memory of just having been stopped out. Proposed fix: block `check_signal` from returning BUY for N bars (e.g. 7) after that ticker's last exit. Caveat: this only delays re-entry, it doesn't prevent it if the ticker is still oversold once the freeze lifts — needs an actual kernel change (`_simulate`/`strategies.py`, new cooldown state) plus a backfill comparison against current results to know if it actually helps, not just a parameter tweak. (Side benefit: fewer close-in-time same-ticker round trips also reduces wash-sale frequency, though that's now less relevant since live testing is planned in IRA/Roth accounts — see `docs/operational_limits.md`.)
- Ways to avoid big losses, or strings of consecutive losses.
- Gap risk (overnight/weekend gaps through stop levels).
- Seasonal trade patterns.
- Tickers unrelated to a given ticker but highly correlated to its down-signals (cross-ticker signal).
- Split analysis into two halves — look for candidates whose edge holds in both halves (out-of-sample consistency) rather than just aggregate alpha.
- 3-month rolling window analysis — walk-forward validation instead of one static backtest window.
- Does higher liquidity correlate with the pattern holding up better?
- Why are some 3x leverage funds consistently more profitable (better alpha/win rate) than others across strategies/versions? Look for a common factor (underlying volatility/beta, decay from daily rebalancing, sector, liquidity, borrow cost) that predicts which leveraged ETFs are good candidates before running a full sweep on them.
- **KORU trail_pct=6-7% extension (2026-07-05)**: v2.13-17 (`TrailingBothZScoreBreakout`, trail_pct 1-5%) shows KORU's best node at 5% has 38 trades / 71% win rate, while 4 of the other 5 trail_pct values are stuck at exactly 25% win rate — likely a small-sample artifact (too few trades to mean anything at tight trail_pcts, not a real "gets worse" trend), not proof the pattern improves monotonically. Hypothesis: a tight trailing stop exits almost immediately on normal chop, so low trail_pct values barely get real trades at all; a wider stop gives the position room to actually ride the reversion, plausibly explaining both the trade-count jump and the win-rate jump together. Worth a single-ticker (KORU) rerun with `hyperparameters.trail_pcts=[1,2,3,4,5,6,7]` (now a real swept axis as of the v3.x reparameterization, see `docs/design.md`) to see whether 6-7% keeps improving, plateaus, or gives back — check trade counts at each value before trusting any win-rate comparison. Not built, one-off research item.

- **✅ Resolved 2026-07-04 — liquidity threshold (1% of 10-day avg $ volume) is fine as-is for lump-sum entry**: checked all four watchlist tickers' actual signal-window (10:25-10:40, 15:25-15:40) hourly $ volume against a $50k order, worst case HIBL's afternoon window at ~9.2% participation. Ran a square-root market-impact estimate (`impact ≈ k·σ·√participation`, using each ticker's own intrabar range as the volatility proxy) — worst-case estimated slippage ~0.4% (HIBL), everything else <0.3%. Negligible against 8-29% TP/SL bands and trade alpha in the tens-to-hundreds of percent. Tightening to 0.5% ADV would needlessly cut EDC/HIBL-type candidates for a cost that isn't real. No threshold change needed.

## High Priority

- **Trailing-buy entry fill assumes an optimistic (unproven) intrabar bar-path — needs a rerun of `_simulate_trail_buy`/`_simulate_trail_both` (v1.9/v1.10) with corrected fill logic (found 2026-07-10)**: `backtester.py`'s trailing-buy "waiting" loop (`_simulate_trail_both:602-616`, mirrored in `_simulate_trail_buy`) always updates `running_low` from the current bar's Low *before* checking whether the same bar's High clears the bounce trigger — i.e. it silently assumes the Low always happens before the High within every hourly bar, which is the best case for this strategy (lower running_low ⇒ easier/lower trigger to clear) and isn't knowable from OHLC bars alone. Built a side, read-only analysis in `scripts/export_trades.py` (`simulate_trail_both_ohlc_aware`, kept separate from the live kernel — nothing in `backtester.py`/`run_optimization_sweep.py` was touched) that resolves each bar's fill as **CERTAIN** wherever possible instead of guessing: (1) High clears the trigger from *prior* bars' already-confirmed low alone — certain regardless of this bar's own order; (2) after folding in this bar's own Low, the bar's **Close** itself clears the new trigger — certain, since Close is always chronologically last in the bar. Only when neither holds (a wick touches the trigger but Close pulls back before the bar ends, with no later bar ever confirming it) does it fall back to an Open/Close-direction heuristic — for SOXL that was just 1 of 61 entries (1.6%), so this is a solvable problem, not a fundamental blind spot.
  Result on SOXL (57-trade backtest, `TrailingBothZScoreBreakout`, current live watch_list params): 51/57 comparable trade entries shifted price/timing, and final compounded equity on $50k dropped from **$3.55M (7007% return) under the current optimistic kernel to $1.85M (3591%) under the certain-tiered logic** — a large, one-directional overstatement, not noise.
  **Action needed**: port the certain-tiered fill logic from `simulate_trail_both_ohlc_aware` back into the real numba kernels (`_simulate_trail_buy`, `_simulate_trail_both`) and rerun the full sweep for every `TrailingBuyZScoreBreakout`/`TrailingBothZScoreBreakout` node — this is the strategy family behind **all 11 tickers on the live watchlist (watchlist_id=9)**, so every live alpha/return number currently on file is inflated by an unquantified amount until this is redone. Related to (but a more precise, actionable version of) the existing "Live/backtest parity gap" P0 #3 note in `docs/backlog_cache.md` about the trailing-buy state machine having no live implementation to verify against — this finding is about the *backtest's own* fill assumption being optimistic, independent of live-parity. Not started.

- **✅ Look-ahead bias in every backtest's entry signal — fixed and tested**: Discovered 2026-07-03 — daily SMA/std indicators included the current day's own close, so intraday entry signals were scored against future information. Fixed in `backtester.py:24` (`prep_inputs`) to look up the *previous* day's indicator row, matching `active_signals.compute_buy_signal`'s live `today` cutoff. Verified via `scripts/verify_live_parity.py` (clean MATCH). Corrected data lives under the **v2.x** version namespace (v2.4-2.10 = same strategy mapping as v1.x); v1.x data left untouched for before/after comparison. Full details/quantified impact in git history if ever needed again.

- **Review P0 live-trading fixes one at a time**: 2026-07-03 follow-up session implemented fixes for code_review_findings.md P0 #1 (TIME exit wall-clock bug), #2 (fixed_sl/trail_pct round-trip), #4 (signal-window exact-minute bug), #5 (sell_alerted never cleared), #6 (app.py config corruption) — self-verified only (unit-level smoke tests + one real backfill), not yet walked through by the user. See "Fixed in follow-up session (2026-07-03, continued)" at the bottom of `docs/code_review_findings.md` for what changed and why. `active_signals.py` needs a restart once reviewed/accepted (live process won't pick up changes otherwise).

- **Manually test fixed_sl/trail_pct round-trip before promoting Sweep 3 (v3.18/v3.21-27) live**: P0 #2 fix (2026-07-03) adds `fixed_sl`/`trail_pct` columns and wires them through `add_node` → Slack BUY-button JSON → `open_position` → `check_sell_condition`, but this is currently **untested against a real trailing-strategy live position** — the current watchlist (AGQ/EDC/FAS/HIBL) is all v1.5, which doesn't exercise this code path at all. Superseded from v1.8/v1.9/v1.10 by v3.18 (`TrailingExitZScoreBreakout`)/v3.21-27 (`TrailingBothZScoreBreakout`) — needs thorough manual verification (add a node, confirm `watch_list`/`open_positions` get correct `fixed_sl`/`trail_pct` values, click through the actual Slack BUY button, verify `check_sell_condition` reads the right stop-loss) before trusting it with real money. **Planned as the focus of the next full session** — live-trading the watchlist end to end.

- **Dispatch overhead optimization**: Profiled 2026-07-03 (see `docs/dispatch_telemetry_results.md`) — the profiling script never actually measured DB-insert time; "88% result collection" is mostly just parallel kernel compute (~90% parallel efficiency), not IPC/pickling overhead. Batched the `backtest_cache` INSERT into `executemany()` (chunk size later bumped to 5000, running fine in production). No pre-change baseline was captured, so a before/after speed comparison isn't possible at this point — not worth chasing further unless a new perf question comes up.

- **Checkpoint 1 ticker-scoping bug — silently drops top candidates**: ✅ Fixed 2026-07-02. `identify_island_candidates()` (`run_optimization_sweep.py:350-360`) queried the entire historical `backtest_cache` for a version/strategy with no ticker filter, but `bh_cache` (gates Phase 2/3) is only built for the *current* run's `target_tickers`. Any run with a narrower ticker list than what's already cached from a prior broader run silently dropped legitimate top candidates at "No B&H data, skipping Phase 2" — no error, no warning surfaced anywhere except a log line. Confirmed it had happened across v1.6 (52x), v1.7 (27x), and v1.8 (3x) historically — including `MULL`, `VRTL`, `WULX`, `NBIZ`, `SMST`, the v1.6 top-5 alpha performers at the time. Fix: added `allowed_tickers` param to scope the Checkpoint 1 query to `ticker IN current tickers list`.

- **v1.9/v1.10 trailing buy strategies**: ✅ Built 2026-07-03. `TrailingBuyZScoreBreakout` (v1.9): after z-score signal, tracks running low and enters when price bounces `trail_buy_pct`% above it; fixed TP/SL exit. `TrailingBothZScoreBreakout` (v1.10): same trailing entry + trailing exit once TP activated (trail_pct=3% hardcoded). `sl` sweep axis → `trail_buy_pct` for both. Smoke test: AGQ/SOXL v1.10 beats v1.9; v1.8 beats v1.10 on AGQ but v1.10 beats v1.8 on SOXL — suggests ticker-dependent optimal. Sweep queued overnight.

- **Limit-order model variants (v1.7-2, v1.7-3) — ✅ abandoned 2026-07-05**: see Watchlist Repick section — giving up on limit-based order variants, not giving better signals.

- **Session cache two-file design**: Work account uses two files — one for session close handover, one (conversation_cache?) for top-10 session history loaded into conversation context. Need to check work account to reconstruct the design and port it here.

- **v1.6 coarse grid sweep**: ✅ Done. Step-3 [3,6,...,30] coarse + 3-island ±4 fine mesh + full mesh for cliff-safe top-10. Three-phase sweep engine built (`run_optimization_sweep.py`). v1.6 completed: 358 tickers coarse, 30 island mesh, 1 full mesh (WULX — only cliff-safe index/other candidate). SMST full mesh running separately.

- **Phase 2.5 bug — only sweeps best node's (w,z)**: Phase 2.5 runs a ±CLIFF_RADIUS TP/SL ±7h hold sweep around the true best node, but only for that node's (w, z) combo. Should sweep all 3 island centers across all (w, z) combos so cliff check has complete neighborhood data for every candidate.

- **Sweep run registry**: Add `sweep_runs` table to DB — one row per sweep execution with `run_id`, `version`, `timestamp`, `config_json` snapshot, `notes`, `phase_reached`. Lets you record why each version was run and reconstruct config if needed. Wire into sweep engine to auto-insert on start/finish. Concrete case for why this matters (2026-07-02): discovered v1.6 only partially copied v1.5.1's EDC/FAS data (AGQ fully copied — 72k/72k rows match exactly; EDC/FAS only 8k/72k copied) with no record anywhere of why, or that a copy even happened — took manual SQL archaeology to reconstruct. A run registry would have made this a one-row lookup instead.

- **Cliff check improvements**: Current `CLIFF_RADIUS=2`, `AND trades > 0` excludes NO_TRADES nodes. Consider: (1) include NO_TRADES as alpha=0 so cliff detection catches edges where signal disappears; (2) widen radius to 3 for coarse-only data where ±2 may miss real neighbors. v1.5 cliff check: 25/340 tickers safe — VRTL, WULX, CIFG, GEVX, CRDU are top safe candidates.

- **v1.7 limit order entry model**: ✅ Built. `LimitOrderZScoreBreakout` — fill on `Low <= lower_band` intrabar at `lower_band` price; intrabar stop loss checks `Low <= stop_price`; TP checks `Close >= tp_price` at bar close. New `_simulate_limit` Numba kernel + `run_backtest_v17`. Grid: w=[10,20], z=[1.0,1.5,2.0], TP/SL=[3,6,...,30], Hold=[7,14,...,140].

- **v1.8 trailing exit**: ✅ Sweep wired. `TrailingExitZScoreBreakout` — close-based entry (v1.5 style), trailing stop once TP% cleared. `trail_pct` replaces `stop_losses` in sweep grid ([2–10%]), `fixed_stop_loss=15` in config execution. `run_backtest_v18` dispatched in sweep engine. Config set to v1.8, ready to run. Pending: run sweep, review results.

- **v2.7 (`LimitOrderZScoreBreakout`) structurally underperforms v2.5/2.6 and v2.8/2.9/2.10 — root-caused 2026-07-04, not a bug**: Post-sweep review (`docs/post_sweep_report.md`) showed v2.7 averaging **negative** alpha across the full backfill (-61.5 avg vs +9 to +34 for other versions) and losing on every per-ticker best-node comparison checked (AGQ/EDC/FAS/HIBL/TQQQ/SOXL). Two structural causes, not overfitting or a data issue:
  - **Looser entry trigger**: `check_signal` requires only `Low <= band` (`strategies.py`, any wick counts) vs. `ZScoreBreakout`'s `Close <= band` (a confirmed close through the threshold). Many v2.7 entries are noise wicks that immediately revert — reflected in some sweep nodes' very low win rates (3–16%) despite high total return (a few outlier winners carrying the average).
  - **Capped fill price**: entry always fills at exactly `lower_band` (`backtester.py` `_simulate_limit`), so on days where price genuinely gaps/crashes well past the band, v2.7 gives up the deeper, more-extreme entry that `ZScoreBreakout` would have gotten from its Close-based fill.
  - Combined with the **fixed TP/SL exit**, v2.7 also caps its rare winners at a fixed % — on assets with fat-tailed moves (many backtest trades run 500–2000%+), this is the dominant driver of v2.7 losing to v2.8/2.9/2.10 (trailing exit / bounce-confirmation entry).
  - **Considered and rejected as a fix**: adding an entry "buffer" (`Low <= band × (1 - buffer%)`) — this is mathematically equivalent to raising `z_score_threshold`, a knob the sweep already searches (z=1.0/1.5/2.0); it doesn't add a new signal-quality filter, just requires a deeper (still potentially noisy) wick. A real noise filter needs something orthogonal to price depth (time/persistence confirmation, or a regime/trend filter) — but persistence confirmation is just `TrailingBuyZScoreBreakout`'s bounce-wait mechanism restated, and a regime filter is `TrendFilteredZScore` — i.e. there's no clean fix that doesn't collapse into an existing strategy variant. Concluded the entry noise is a real, unfixable-in-isolation cost of the touch-based entry model.

- **`verify_live_parity.py` reported a false MISMATCH for `LimitOrderZScoreBreakout` — fixed 2026-07-04, harness bug not a real live/backtest gap**: `replay()` checked the entry signal against bar Close instead of Low, unlike the kernel (`_simulate_limit`, which correctly uses Low). This made it look like live trading couldn't achieve the backtest's entries. Turned out backwards: production `active_signals.py`'s `notify_limit_fill` intrabar-fill-detection loop polls **all day**, every `POLL_SECS` (300s), unconditional on the `target_hours`/buy-window gate — so live genuinely does monitor continuously for limit-entry nodes, closely matching (in fact exceeding) the kernel's two-signal-window Low check. Fixed by making `replay()` use bar Low (not Close) as the price fed to `compute_buy_signal` for `LimitOrderZScoreBreakout` entries. Verified: TQQQ and HIBL nodes that previously reported MISMATCH now report clean MATCH.
  - **Bug found and fixed along the way**: `verify_live_parity.py`'s `replay()` mislabeled every trailing-stop-triggered exit as TWIN/TLOSS instead of WIN/LOSS, because `active_signals.check_sell_condition` (`active_signals.py:623-624`) collapses the strategy's `WIN`/`LOSS` reason into a generic `'TRAIL'` string for Slack messaging, and `replay()`'s label reconstruction didn't recognize `'TRAIL'` as a WIN/LOSS case. Fixed by treating `'TRAIL'` as WIN/LOSS-by-return-sign, distinct from `'TIME'` (which stays TWIN/TLOSS). Verified: AGQ v1.8 case that previously showed the labeling quirk now reports clean MATCH.

- **v1.9/v1.10 live execution — low priority, not sure we care**: since v1.x data predates the look-ahead bias fix, v1.9/v1.10 aren't trustworthy to trade as-is (would need a v2.9/v2.10-equivalent bias-corrected backfill first, which is already queued). Live-execution wiring itself is design-solved (Schwab trailing-stop-buy order, see `code_review_findings.md` finding #3 / `operational_limits.md`) — no state machine needed, just convert the staged order to a trailing-stop-buy at bar close. If `_STRATEGY_LABELS` entries for `TrailingBuyZScoreBreakout`/`TrailingBothZScoreBreakout` are a quick add, do it; otherwise don't prioritize.

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

- **Is the look-ahead bias itself an accidental (contrarian) selection signal? Worth a deliberate, non-cheating test.** Raised 2026-07-03 alongside the look-ahead bias finding above. Mechanism, traced precisely: including day D's own close in the rolling window both pulls the SMA down *and* inflates the STD (a large move that day increases that day's contribution to variance). Since `lower_band = SMA - z*STD`, both effects push the band *further down* — i.e. the bias makes entry *harder*, not easier, specifically on days when the move is already large. This matches the measured direction: the biased kernel produced *fewer* trades than the corrected replay (AGQ: 18 vs 31), not more. So the bias isn't just noise — it behaves like an implicit filter: "was today's drop large relative to the volatility that same drop just created?" That's a real (if backwards-arrived-at) selection criterion, and it's an open question whether it survives being reformulated honestly — e.g. gating entry on same-day realized intraday range/volatility (which *is* knowable in real time, unlike tomorrow's close-to-close std) instead of cheating with future daily closes. Mean-reversion caveat (the user's point): if the ticker is genuinely mean-reverting, next-day sigma should come back down after the spike, so a filter built on "elevated same-day vol" might just be selecting for noise-driven overshoots rather than a durable edge — not guaranteed to hold, worth testing rather than assuming either way. Contrarian in the sense that "fix the bug and the strategy gets worse" would be a surprising result — but the trade-count/alpha data so far (bias removed, alpha still positive for all 4 watchlist tickers) suggests the effect isn't load-bearing, just inflating magnitude. Test idea: build a same-day realized-vol-gated variant (no future information) and compare its alpha/trade-count against both the biased kernel and the corrected replay.

## Medium Priority

- **Long-running process consistency check**: Currently manually restarting `active_signals.py` periodically for confidence that it hasn't drifted into a bad state. Goal: stop needing manual restarts. Two options discussed: (1) run a second process that restarts fresh every morning alongside the long-running one, diff their signals to build confidence the long-running one hasn't drifted; (2) make the long-running process itself controllable via the Slack socket (already Socket Mode) so it can take a "revalidate/restart" command without killing the OS process. Not urgent — manual restart is fine for now — but risk is forgetting to restart it one day, so don't let this drop.

- **Half-day trading sessions not handled**: `_SIGNAL_WINDOWS` (10:25-10:40, 15:25-15:40 ET) assumes a full session. On early-close days (day before Thanksgiving, Christmas Eve, etc.) the market closes ~1pm, so the 15:25 window never happens — a TIME exit that would've fired then just doesn't trigger until the next real session. Low priority, but a real gap in `_in_buy_window`/exit-check timing.

- **Parameter selection workflow**: After enough sweeps, need a way to review results across tickers and select parameter sets to trade — currently manual via logs/heatmaps.
- **`trading_engine.py` cleanup**: Either retrofit or replace with Layer 3 implementation; currently points at legacy files.

## Low Priority / Ideas

- **Dropped index `idx_bc_version_ticker_z_return`** (2026-07-03): `backtest_cache(version, ticker, z_score_threshold, strategy_return DESC)`, removed from `init_idempotent_db` (`run_optimization_sweep.py`) — checked every page query against `backtest_cache` via `EXPLAIN QUERY PLAN` and found none matching this shape (real point lookups hit the PK directly; nearby queries partition/order by `alpha_vs_spy`, not `strategy_return`). Was pure insert-time overhead, especially costly in Phase 3's full-mesh insert volume. If a future query needs it, re-add the `CREATE INDEX` line — the two other extra indexes (`idx_bc_ticker`, `idx_bc_version_return`) were verified in-use and kept.

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

## [live-trading][tax] Deprioritized 2026-07-18 (raised 2026-07-17) — wash-sale/tax analysis needed before promoting any ticker into the taxable brokerage account
Distinct from the resolved cross-account (taxable-loss → IRA-repurchase) wash-sale item — this strategy re-enters losing tickers within 30 days as completely normal, routine behavior (not a one-time event), so any ticker live-traded in the taxable brokerage account will generate wash sales continuously, indefinitely. User's explicit call: **fine with this** — a wash sale only defers the loss into replacement-share cost basis, doesn't destroy it, and repeatedly sitting out 30 days after every loss to avoid it would likely cost more in missed re-entries than the deferral costs (matches the general "missed signals aren't free but aren't catastrophic either" theme from the chaos-monkey findings). Also discussed: splitting the brokerage account's capital, with part parked as a static buy-and-hold SPY sleeve alongside the actively-traded portion — orthogonal, low-risk, no design work needed.
**LABU floated as a candidate** for the first ticker promoted into the brokerage account, not decided.
**Year-end wrinkle flagged by user, not yet verified**: a wash sale is "just a deferral" in general, but if the 30-day window straddles the calendar year boundary (loss sold in December, replacement bought in the following January), the disallowed loss can't be claimed on that tax year's return at all — it rolls into the replacement shares' basis and isn't usable until eventually sold clean, effectively pushing the deduction a full tax year later rather than just 30 days. Relevant to year-end tax planning (offsetting current-year gains with current-year losses) specifically. Recalled from memory, not independently verified against current IRS rules yet — worth confirming precisely (which side of Dec 31 triggers it, exact mechanics) as part of the real analysis below rather than trusting an offhand recollection.
**Deprioritized 2026-07-18**: pushed behind the train/test split and v4-promotion work — no taxable-account promotion is imminent, so the tax analysis isn't blocking anything active right now. Revisit when a taxable-account promotion actually becomes near-term.
**Action needed**: user wants a real wash-sale/tax analysis done before promoting anything into the brokerage account (not scoped yet — e.g. quantifying deferred-vs-recognized loss timing impact across a tax year, and specifically verifying the year-end-straddle mechanic above). Not started.

## [live-trading][security] Phase 4 (deferred to cloud-infrastructure planning), 2026-07-18 — move order-placement/mutating Schwab calls behind a separate proxy this session can't write to
Raised by the user: their work architecture uses an API-proxy pattern (a separate service/codebase that holds real credentials and does the actual outbound call, so the calling app only ever sends intent) — nothing Schwab-specific exists yet, would need to be rebuilt from scratch in a separate folder/codebase here. Goal is two-fold, roughly equal weight: (1) safety boundary — a bug or bad edit made in *this* repo/session can never itself place/cancel/modify a real order, since the code with real trading authority would live somewhere this session doesn't write to; (2) credential isolation — the Schwab OAuth token/secrets would live only with the proxy, not on this machine/repo at all.
**Scope discussed**: only the write/mutating calls move (`place_trailing_buy`, `place_trailing_sell`, and any future cancel/modify-order calls) — read-only calls (`get_account`, `get_filled_order`, quotes) stay local in `schwab_client.py` since they can't themselves cause a trade. This server would call out to the proxy for anything that changes broker state; the proxy is the only thing holding a live, order-capable Schwab session.
**Not scoped at all yet**: what the proxy-side code actually looks like (that part is deliberately meant to live outside what an agent session writes), the request/response contract between this repo and the proxy, how the proxy would authenticate/hold the Schwab token, and how `schwab_safety.py`'s existing guardrails (allowlist, dry_run, kill switch, signal-window gate, duplicate-order window) would split across the local/proxy boundary — plausibly some checks stay local (fast pre-flight rejection) and some get re-validated proxy-side (defense in depth) but not decided.
**Deferred 2026-07-18**: user tagged this "phase 4" — deliberately pushed out until cloud infrastructure is actually being considered (the proxy is naturally a separately-hosted service, so the two decisions are linked). Not a priority for the current train/test-split + v4-promotion phase.
**Action needed**: design session, not started. Logged as backlog only per explicit 2026-07-17 instruction — no code changes, no client stubs, nothing wired.

## [live-trading][security] Phase 4 (deferred to cloud-infrastructure planning), 2026-07-18 — one brokerage account per live ticker, for blast-radius containment against a rogue algorithm
User plans to split into **one brokerage account per ticker** on the live watchlist. Originally raised as a PDT-avoidance idea (see the now-resolved same-day-block backlog item — that rationale turned out moot since the PDT rule itself was eliminated 2026-06-04). **Restated with a real, independent rationale**: capping how much capital a bug or malfunctioning algorithm on one ticker can ever touch. With separate accounts, a rogue order on ticker A structurally cannot reach ticker B's capital — no code enforcement needed, the account boundary itself is the guarantee.
**Directly relevant to an existing known gap**: the 2026-07-16 GDXD-promotion finding that Schwab does not reserve/check buying power against a resting order at placement time, combined with the 2026-07-17 finding that the new FINRA intraday-margin framework (replacing the old PDT rules, effective 2026-06-04) is a deficit-and-cure model, not a hard fill-time block — i.e. there's real reason to believe multiple simultaneously-resting orders across tickers *could* collectively commit more than available capital before anything stops them. That gap was previously flagged as needing an "aggregate-across-tickers cash exposure" guard (unbuilt). **One-account-per-ticker makes that guard structurally unnecessary** rather than something to build — each account's own balance is a hard ceiling on that ticker's exposure by construction.
**Complements, doesn't replace, the API-proxy idea above**: the proxy limits *what code can act* (safety boundary + credential isolation); account-splitting limits *how much money any single ticker's logic can ever reach* (blast-radius containment). Different layers of the same defense-in-depth goal.
**Real operational cost is small, confirmed by reading the code (2026-07-17)**: these are all IRA accounts, so there's no per-trade taxable event or capital-gains paperwork to multiply (just N annual 5498/1099-R-type forms, minor). And `schwab_client.py:29-59` already uses a single `schwab_auth.get_client()` OAuth session for the whole Schwab login, resolving every nickname (`brokerage`/`sep`/`roth`/`ira` today) to an account hash via one `get_account_numbers()` call plus a `SCHWAB_ACCOUNT_<NAME>` env-var suffix match — since all these accounts are linked under one login, scaling `NICKNAMES` from 4 to a dozen-plus needs zero new logins/tokens, just more list entries and env vars, plus updating whatever mapping decides which nickname a given ticker's trades route through.
**Deferred 2026-07-18, un-deferred same day**: briefly tagged "phase 4" alongside the API-proxy item, but the user reprioritized it as the active focus later the same session (after resolving the walk-forward negative-fold follow-up by sending DPST/NUGT/RETL/UDOW/UVIX to research instead of investigating further) — account-per-ticker setup is now the next real thread of work, not deferred infra planning. API-proxy item stays deferred on its own.
**Action needed**: design session on account-splitting scope (how many accounts, which nicknames, `NICKNAMES`/`SCHWAB_ACCOUNT_<NAME>` mapping, which tickers get their own account first). Not started. No code changes yet.

## ✅ [live-trading] Resolved 2026-07-18 — GDXD paper-trading layer built
`dry_run=True` (the state of every real Schwab account today) only posts a Slack "[DRY RUN]
would place..." message and produces zero simulated fill/P&L — no way to see whether the
automation engine (`schwab_client`/`schwab_safety`, wired 2026-07-17) actually catches signals
reliably before any ticker is flipped back to real execution. Built `paper_trading.py`
(new module) to close that gap for `schwab_safety.AUTOMATION_ENABLED_TICKERS` tickers
(currently `{"GDXD"}`) while they stay `research` mode:
- `start_paper_buy(node, sig)` — called from `active_signals._scan_buy_signals` on a BUY
  signal for a research-mode, automation-enabled ticker running a trailing-buy strategy
  (`db._is_trailing_buy(node)`). Inserts a `paper_pending_buys` row (`running_low` seeded at
  signal price); deduped against an existing paper position or pending row.
- `update_paper_buys()` — called every poll, unconditionally, same "not gated to a window"
  reasoning as `check_auto_fills` (a real trailing buy can fill any time after the signal).
  Tracks `running_low = min(running_low, current_price)`; once
  `current_price >= running_low * (1 + trail_buy_pct/100)`, sizes
  `shares = int(starting_notional // current_price)` — fully deployed at the real discovered
  fill price, the "correct" sizing approach identified in the trailing-buy capital-sizing
  backlog item (paper trading learns the true fill price directly instead of needing the
  live worst-case-conservative formula) — and opens a `paper_positions` row.
- `check_paper_sells(last_seen_bar, paper_sell_alerted, load_cache)` — mirrors the real
  `open_positions` exit-check block in `active_signals.run_loop` exactly (same `at_bar_close`
  detection, shares the `last_seen_bar` dict — safe since a ticker is never simultaneously
  `live` and `research`), calling `signals_compute.check_sell_condition(..., paper=True)`, the
  same exit state machine real positions use, writing to `paper_positions`/`paper_trade_log`
  instead. Uses its own dedup set (`paper_sell_alerted`) since paper position ids are
  independent of real `open_positions` ids.
- `signals_db.py`: new `paper_positions`/`paper_trade_log` tables, schema-identical mirrors of
  `open_positions`/`trade_log`. Existing CRUD (`get_open_positions`, `open_position`,
  `close_position`, `log_trade_entry`, `log_trade_exit`, `update_position_trail_state`) took a
  `paper=False` param rather than being duplicated (`_pos_tables(paper)` picks the table pair).
  New `paper_pending_buys` table — lighter than real `pending_buys` (no reminder machinery,
  since a paper fill is auto-detected every poll, never confirmed by a human click).
- `signals_compute.check_sell_condition` gained a `paper=False` param: threads to
  `db.update_position_trail_state(..., paper=paper)`, and skips the interactive
  "Apply Correction" corp-action Slack block when `paper=True` (that button's handler assumes
  a real `open_positions` id) — falls back to a plain freeze/print warning. Accepted gap since
  this is scoring infrastructure, not real capital.
**Deliberate deviation from the original framing** ("let `AUTOMATION_ENABLED_TICKERS` act
through the existing `_attempt_automated_buy`/`_attempt_automated_sell` path"): investigating
that path (`signals_notify.py`) found it would write real `pending_buys` rows that nothing
ever marks Filled (no human clicks the button for a research ticker, and auto-fill-detection
is opt-in/off) — those rows would sit forever and `check_buy_reminders` would nag every 15
minutes about a ticker that was never actually live. Built a fully separate simulation instead
— never calls `schwab_client`/`schwab_safety` at all, independent of `dry_run`, keeps real
live-trading state (reminders, `open_positions`, `_attempt_automated_sell`) completely
untouched by research-mode paper activity.
**Known limitation**: fills are sampled at `POLL_SECS` cadence, not tick-perfect against a
real broker's continuously-live `TRAILING_STOP` price — close enough to score signal-catching
reliability and get directionally-real fill data, not a tick-perfect replay.
`scripts/paper_trading_status.py` added (prints pending/open/closed paper state, matching
`scripts/open_positions_status.py`'s convention).
**Verified**: full `pytest tests/` suite, 92 passed (was 86 — 6 new tests in
`tests/test_paper_trading.py` covering pending-buy dedup, running-low tracking, bounce-fill
sizing, SL exit + `paper_trade_log` write, and that the real `open_positions`/`pending_buys`
tables are untouched by the paper flow). Confirmed against the real `trading_live.db`: real
`open_positions`(1)/`trade_log`(11) row counts unchanged after creating the new empty paper
tables via `scripts/paper_trading_status.py`.

## ✅ [backtest] Resolved 2026-07-18 — `signals_db.add_node`'s `fixed_sl` computation ignored the real per-node value for uses_fixed_sl strategies
`add_node` used to always compute `fixed_sl = _config_fixed_stop_loss()` (reads
`config.json`'s global `execution.fixed_stop_loss`) whenever `strategies.uses_fixed_sl(strategy)`
was true, regardless of what real per-node SL value the caller actually wanted. Found
2026-07-18 promoting 19 tickers' v4 (SL=1%) nodes: every row came out with `fixed_sl=15.0`
instead of the real `1.0`, silently wrong, no error — worked around that session via direct
SQL inserts. **Fixed**: `add_node` gained a `fixed_sl_override=None` parameter — when set,
used instead of `_config_fixed_stop_loss()`; `None` (the default) preserves the old
config-read behavior for legacy v3.x callers, so no existing call site needed to change.

## ✅ [live-trading] Resolved 2026-07-23 — canary watchlist nodes (A-F) built, sizing bug fixed, all live
Designed 2026-07-22, built 2026-07-23. `scripts/add_canary_nodes.py` adds six synthetic
`watch_list` nodes (SPY/QQQ/IWM/DIA/VOO/XLF — liquid, cached, unused by any real node) to the
active watchlist, all `mode='research'`, `version='canary'`, with deliberately extreme parameters
so each should complete its expected lifecycle every trading day:
- **A** (SPY, `TrailingBothZScoreBreakout`, `entry_timing='close'`): full happy path — ambient
  entry → bounce-fill → arm → trailing-sell, all same day. `fixed_sl=30` (unreachable),
  `arm_sell_pct`/`trail_buy_pct`/`trail_sell_pct`=0.1 (hair-trigger).
- **B** (QQQ, same shape): early-SL path — bounce-fill → immediate SL. `arm_sell_pct=10`
  (unreachable), `fixed_sl=0.1` (hair-trigger).
- **C** (IWM, same as A but `entry_timing='open_check'`): exercises `_scan_pinned_entry` +
  `open_price_quality_log` — the pinned intrabar-open scan, distinct from A's bar-close-only path.
- **D** (DIA, `trail_buy_pct=5.0`): large enough that the bounce-fill is unlikely to complete
  same day — a pending trailing-buy carried overnight into the next session's open is a direct
  daily regression check for the 2026-07-22 stale-cache fix (`_current_price`'s market-open
  staleness guard).
- **E** (VOO, `strategy='TrailingExitZScoreBreakout'`, not TrailingBoth): `signals_db.
  _is_trailing_buy` routes on the strategy's axis schema (`sl_axis` class attribute), not the
  `trail_buy_pct` value — only a real TrailingExit node reaches `paper_trading.
  start_paper_market_buy` (immediate market buy, no `pending_buys` row) instead of the
  bounce-fill path A-D exercise. A TrailingBoth node with `trail_buy_pct=0` would NOT have
  tested this (an earlier version of the plan got this wrong).
- **F** (XLF, `take_profit=50`/`fixed_sl_override=50`, `max_hold_hours=2`): arm and SL both
  practically unreachable, tiny hold cap — the only exit path left open is TIME.
**Sizing bug fixed**: `starting_notional=500` (the original A/B draft) sizes to
`int(500 // price) == 0` shares at SPY/QQQ's real price (~$700+) — both would have silently
never filled. Caught by independent Opus review before deployment; raised to `10000` for all six.
**Accepted limitation, not closed**: `_scan_pinned_exit_arm` only ever reads real (non-paper)
`open_positions` — no canary, however designed, can exercise it. Deferred to the planned
`live_sim.py` harness extension (`docs/backlog_cache.md`), which can call it directly against
synthetic positions instead.
**Verified**: all 6 present in `watch_list` (watchlist_id=65, 16 nodes total: 10 real v5 +
6 canary) via direct query. Deploying them is what surfaced the reference-report bugs — see
`docs/research_log.md`'s 2026-07-23 entry and `docs/backlog_cache.md`'s resolved entries for
that investigation.

## ✅ [live-trading] Resolved 2026-07-23 — `scripts/live_sim_harness.py` built: non-interactive coverage harness for `active_signals.py`/`signals_*.py`
Decided 2026-07-22 (see the entry directly above this one), built 2026-07-23. Six scenarios,
each calling the real orchestration function directly against an isolated sim DB
(`TRADING_DB_PATH` override, same mechanism `scripts/live_sim.py` already used) — full run takes
~2s: `scenario_pinned_entry_trailing_buy` (`_scan_pinned_entry`, the real open-check trailing-buy
entry path), `scenario_pinned_exit_arm` (`_scan_pinned_exit_arm` arming + `notify_trailing_
activated` persisting both `trailing` and `peak`, regression coverage for the 2026-07-22
stale-clobber bug), `scenario_reconcile_fill_topup` (`signals_notify._reconcile_fill` with a
deliberately forced shortfall), `scenario_gap_resize` (`signals_notify.check_gap_resize`,
asserting the actual replacement order's ticker/shares/price via a `wraps=` spy on
`place_equity_buy`, not just the Slack text), `scenario_time_exit` (`check_sell_condition`'s TIME
trigger via bars-held, not wall-clock hours), and `scenario_ambient_market_buy_entry` (the second
real entry path — an automated market buy via `_scan_buy_signals` → `notify_buy_signal` →
`_attempt_automated_market_buy` → synchronous fast-confirm → `_reconcile_buy_fill` → SL
placement). Real z-score math runs unmodified against a real synthetic CSV written to
`cache/research/` (same convention as `tests/conftest.py`'s `make_synthetic_csv`) — only the
`schwab_client` broker-network boundary is stubbed via `unittest.mock.patch.object`, since the
harness's job is verifying wiring (dedup, mode-gating, arm/re-arm state, sizing math, Slack
content), not re-verifying signal math (`backtest-change-rollout`'s job).

**Found and fixed along the way** (real bugs, not hypothetical):
1. `signals_db.get_open_position()` (singular ticker lookup) never coerced `trail_state` from
   `None` to `{}` the way `get_open_positions()`/`get_position_by_id()` already do — calling
   `check_sell_condition` against its result raised `TypeError: 'NoneType' object is not
   iterable` inside `strategies.py`'s `check_exit`. A pre-existing inconsistency (a stale comment
   in `tests/test_part4_entry_trigger.py` even documented it as expected), not previously hit
   because nothing in production called `check_sell_condition` against this function's raw
   result. Fixed to match its siblings; full suite unaffected (184 passed).
2. **Safety incident, found and remediated the same session**: `schwab_safety.py` has several
   real, hardcoded (`Path(__file__).parent / ...`) state files — order counts (`STATE_PATH`),
   kill switch, ticker-automation pause, auto-fill-detection toggle, automation-scope — none
   gated by `TRADING_DB_PATH` the way the DB is. An early version of this harness (before this
   was known) placed real dry-run BUY attempts that wrote straight into the real
   `cache/live/schwab_order_counts.json` across repeated debug runs, driving the real `ira`
   account's `daily_order_cap` counter to its actual limit (10/10) before it was caught (the
   harness's own scenarios started silently failing cross-contamination, which is what surfaced
   it). The file was reset (confirmed safe: its `recent_orders` contents were entirely the
   harness's own synthetic `ZHARN*` tickers, all counts are self-expiring/date-keyed, and the
   real kill switch — engaged since 2026-07-16 — meant no real order could have gone through
   regardless). **Structural fix, not just a harness workaround**: added `SCHWAB_STATE_DIR` env
   var to `schwab_safety.py` (mirrors `TRADING_DB_PATH` exactly — `_STATE_DIR` computed once at
   import time, all five real paths derive from it), so the harness now sets
   `SCHWAB_STATE_DIR` to a fresh `tempfile.mkdtemp()` before importing any project module,
   isolating all five files at once (kill-switch-engaged reads as `False` for a nonexistent
   file, no separate stub needed). This is the durable fix — any future test/sim script gets the
   same isolation automatically, not just this one harness.
3. Independent Opus review (requested mid-build) verified the `SCHWAB_STATE_DIR` remediation was
   complete (no other real file/OAuth-token path reachable given `dry_run=True` short-circuits
   before any real client call) and flagged two of the six scenarios' assertions as weaker than
   the regression they claimed to guard — both tightened: `scenario_pinned_exit_arm` now checks
   `peak` survived alongside `trailing` (the original clobber bug dropped both), and
   `scenario_gap_resize` now spies on the real `place_equity_buy` call args instead of asserting
   only the Slack message text.

Full suite: 184 passed (was 181; +3 from the stale-price-exit-alert item below, +0 net from this
item's `get_open_position` fix). Not yet adopted as a required step in any workflow (e.g.
`feature wrap`/`session wrap`) or documented in `docs/automation_principles.md` — still just a
tool that exists, per the original 2026-07-22 decision's "document as standing convention" being
left undone.

## ✅ [live-trading] Resolved 2026-07-23 — Slack alert for the stale-price-guard silent-suppression gap (2026-07-22 HIBL incident's residual finding)
The one gap the 2026-07-22 independent Opus review flagged as not-yet-fixed: when
`signals_compute._current_price()` returns `(None, None)` (its market-open staleness guard, or a
genuine same-day data-refresh failure), `active_signals._check_position_exit`'s mid-bar branch
silently `return`s — no SL/trailing-stop/TIME check runs against that real position for that
poll, previously with only a `log_poll` trace line, no Slack alert. Fixed:
`signals_notify.alert_stale_price_exit_suppressed(pos)`, rate-limited 15min per position id (same
cooldown pattern as `_alert_reconcile_mismatch`/`_guarded`'s per-section cooldown), wired into
`_check_position_exit` at the `cp is None` branch. 3 new tests
(`tests/test_stale_price_exit_alert.py`): fires with ticker/account/function name in the message,
rate-limited on a second call, and keyed per-position (not shared across positions). Full suite:
184 passed (was 181).

## ✅ [live-trading] Resolved 2026-07-23 — coverage_events fully wired; stale "~13 remaining" note corrected; `daemon_section_exception` added
Originally built 2026-07-22 (`signals_db.coverage_events` table + `log_coverage_event()`/
`get_coverage_events()`, `scripts/coverage_matrix.py` pivot view — see that session's entry
above) with 7 real control sites wired. A follow-up backlog note at the time listed ~13
scenarios as "not yet wired": SL placement (sync/async), top-up/`_reconcile_fill`, trailing-arm
re-read in `notify_trailing_activated`, `drain_fill_queue` fast-path, daemon-survives-exception,
open-price-quality, and (flagged as possibly-already-covered) second-live-ticker-BUY-blocked.
**2026-07-23 audit**: grepped every real `log_coverage_event(` call site in the codebase and
found the note was stale — `sl_placement` (+ `sl_placement_fast_confirm_timeout`), `top_up`,
`trailing_arm_state_reread`, `gap_resize`, `fast_path_fill_reconciliation`,
`reconciliation_mismatch`/`reconciliation_fetch_failed`, `cash_check`, `same_day_block`, and all
5 dup-order guards were already wired (in `signals_notify.py`/`schwab_safety.py`), evidently
done in a later session without this backlog entry ever being updated to reflect it.
`open_price_quality` was never meant to live in `coverage_events` in the first place — it already
has its own dedicated `open_price_quality_log` table (Part 4 Deliverable 2), a separate design
choice, not a gap.
**The one real gap found**: `active_signals._guarded()` (wraps every `run_loop` section in
try/except so one section's exception can't crash the daemon, automation_principles.md #3/#4)
caught and alerted on exceptions but never logged whether the daemon actually survived one — no
queryable record of which sections fail, how often, or when. Fixed: new `daemon_section_exception`
scenario key logged inside `_guarded`'s except block, `mode` via `signals_notify._coverage_mode(None)`
(safe fallback to `"dry_run"` since `_guarded` wraps whole daemon-loop sections, not one
account), `result=<section name>`, `detail=<exception text>`. Logs on every caught exception
(not cooldown-gated like the paired Slack alert) — an independent Opus review confirmed this is
an accepted non-issue given the table's diagnostic (count + most-recent) purpose, not a
correctness bug. New test `test_guarded_logs_coverage_event_on_failure`
(`tests/test_run_loop_fault_tolerance.py`), using the same `signals_config.DB_PATH` monkeypatch
isolation pattern as `test_db_roundtrip.py`. Full suite: 185 passed (was 184).
Prompted by discussing whether `coverage_events` captures enough state to support the kind of
manual cross-check the user did with the HIBL trailing-buy CSV a week prior — concluded
`detail` is free-text (real numbers like `shares=`/`price=`/`required=$`/`available=$` are
present, but not structured columns, and expected-vs-actual isn't stored side by side), so it
supports spot-checking a row but not query-level aggregation or automated verification yet.
Structured `detail` (human-readable short text + JSON, per the user's stated preference) was
discussed and deliberately deferred, not built this session.
`coverage_events` is now considered fully wired against every real control site identified so
far — treat as closed, not a standing backlog item; revisit only if a new control site is added.
