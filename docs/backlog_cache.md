# Backlog Cache

## [live-trading][coverage] Resolved 2026-07-27 — `coverage_deviations` rows are now permanent record ("ticket model"), never deleted
Per user's explicit framing: an exception/deviation is an artifact of something that happened, like a
Jira ticket — once created, it should never vanish, only ever be explained. `clear_deviation_if_resolved`
(`signals_db.py`) changed from `DELETE` to auto-`UPDATE` (`reason='Auto-resolved: ...'`,
`reason_by='system'`) for a same-day unexplained deviation that a later re-check finds met — the row
stays, tagged as system-resolved rather than human-explained. Also cleared the 4 stale unexplained
XLF/VOO/IWM/QQQ rows (ids 7-10, open since 2026-07-24, root-caused by the dry_run fill synthesis gap
already fixed 2026-07-26) by explaining them directly.
**Regression found+fixed same session** (session-wrap Opus review): the auto-resolve change alone would
have let a genuine NEW same-day deviation silently inherit the stale system reason and vanish from
`get_deviations(unexplained_only=True)` — `record_deviation` now clears a `reason_by='system'` reason
(never a human one) whenever it refreshes an existing row, so a system auto-resolution yields to real
new evidence instead of masking it. New regression test
(`test_record_deviation_clears_system_reason_on_new_deviation`).

## [live-trading][coverage] Open, raised 2026-07-27 — Trade-Flow Test Accountability Grid (`pages/14_Coverage.py`, backed by `scripts/coverage_registry.py`) needs filters + direct links to underlying tests
Built 2026-07-27: a 32-row registry of real trade-flow logic branches with status computed live
from `coverage_events`/`coverage_deviations` (never hand-typed) — see `docs/conversation_summary.md`'s
2026-07-27 entry for the full build/rationale (why two competing coverage systems already existed,
why Opus review alone isn't sufficient accountability). Real result at build time: 25 of 32 branches
(not-instrumented + wired-never-fired + attempt-failed) have zero live proof of working.
**Not yet built, raised same session**: the grid is a flat 32-row table with no way to narrow it down
or jump to the actual evidence:
1. **Filters** — by status (e.g. show only `not-instrumented`/`wired-never-fired`, the real gaps),
   by `check_mechanism`, or by free-text search over `code_path`/`scenario`.
2. **Links to underlying tests** — each row's `offline_coverage` field is free text naming a test file/
   function (e.g. `test_schwab_safety.py`) or a script (`live_sim_harness.py::scenario_x`), but there's
   no clickable/copyable path to actually go run or open it. Should resolve to something actionable —
   at minimum the real file path, ideally a button that runs the specific pytest node id or harness
   scenario and shows pass/fail inline.
Not scoped: exact filter UI (Streamlit multiselect/selectbox), whether test-links resolve to file paths
only or something more interactive. Registry itself (`scripts/coverage_registry.py`) is stable and
correct as of this session (250 tests passing) — this item is purely about surfacing/navigating it
better, not fixing its computation logic. **Its computation logic did need real fixing, twice, before
reaching that state**: two accuracy bugs caught during initial build (a `scenario_key` collision
between `paper_trading.py` and the dry_run-sim code sharing `entry_fill`/`exit_fill`; a blocked-vs-
succeeded conflation for `sl_placement`/`top_up`), then a required session-wrap Opus review of the
same-session `signals_db.py` change found 3 more CONFIRMED bugs, most severe being **inverted logic**:
the `scenario_expectations` check-mechanism branch fed unexplained-deviation rows (failures) into the
same good/bad bucketing as `coverage_events`, so an unexplained (currently failing) scenario rendered
green "verified-live" and a clean day with zero rows rendered red — backwards for an accountability
tool. Also found: the same-session `clear_deviation_if_resolved` DELETE→UPDATE change (see the
sticky-deviation entry below) let a genuine new same-day deviation inherit a stale system-authored
"auto-resolved" reason and vanish from the unexplained-deviations report; and `bad_results` only
listed one of several real failure result strings per scenario (`sl_placement`/`top_up`/`gap_resize`
each emit 2-4 distinct failure results, not one). All 3 fixed same session, 6 new regression tests
added, full suite 250 passed (was 244), harness 7/7, `signals_invariants.py` clean (1 known accepted
UDOW violation, unchanged). This is exactly the failure mode the grid was built to guard against in
the first place — a real, live example of why the user wanted evidence-based accountability instead
of trusting AI review alone.
3. **Row grouping** — raised same session, after adding heatmap row-coloring (`STATUS_COLOR`/`_heat_row`
   in `pages/14_Coverage.py`): the 32 rows currently render as one flat list sorted worst-status-first,
   with no grouping by feature area (entry/exit/guards/reconciliation/multi-node) or by which real module
   implements them (`schwab_safety.py` vs `signals_notify.py` vs `active_signals.py`). A logical grouping
   would make the heatmap easier to scan for "which whole area is weak" rather than reading 32
   individual rows. Not scoped: grouping key (by `code_path`'s module, by a new manually-tagged
   `feature_area` field on each `REGISTRY` row, or by `check_mechanism`), whether groups are collapsible
   sections or just a sort/header break within the same table.

## [live-trading][coverage] Resolved 2026-07-26 — `live_sim_harness.py` gained `scenario_dry_run_sim_cycle`, closing a coverage gap in testing the dry_run fill-synthesis logic itself (neither `live_sim.py`'s REPL nor the unit tests drove it end-to-end through the real daemon wiring). Full detail: `docs/deep_backlog.md`'s 2026-07-26 entry (top).

## [live-trading][coverage] Resolved 2026-07-26 — dry_run fill synthesis built: a dry_run account's trailing/market-buy order now closes the loop against real price data instead of stalling forever. Full detail: `docs/deep_backlog.md`'s 2026-07-26 entry.
Fixes the canary/dry_run "no closed trade found" false-positive `coverage_deviations` (XLF/VOO/
IWM/QQQ, ids 7-10, unexplained since 2026-07-24) — root cause: a `dry_run=True` account's real
broker order never gets a real fill event (schwab_client short-circuits before the broker call),
so the `pending_buys` row just sits forever and no `open_positions`/`trade_log` row is ever
written. New `signals_notify.update_dry_run_buys`/`check_dry_run_sim_sells` mirror
`paper_trading.py`'s bounce-fill/immediate-close simulation but against the real
`open_positions`/`trade_log` tables (user's explicit call, not the paper tables), tagged
`is_dry_run_sim=1`. Opus review found 6 CONFIRMED + 2 PLAUSIBLE issues, all fixed same session:
(1) most severe — `active_signals._scan_pinned_exit_arm` was missed by the initial skip guard,
which would have corrupted the new function's own bar-close detection (shared `last_seen_bar`)
and fired real `notify_trailing_activated`/`notify_sell_signal` Slack flows on a synthetic
position; (2) `_fill_dry_run_buy` didn't check `open_position()`'s return value, so a duplicate
fill attempt would re-post a false "would have filled" alert forever; (3) a `wl_id`-less
`pending_buys` row would never clear and re-fire forever — now fails closed; (4) `closed_today`/
`_last_sale_recovery` read `trade_log` with no filter, so a simulated exit could block a real
same-day re-buy or size a real order off fake proceeds — both now exclude `is_dry_run_sim` rows;
(5) synthetic positions were indistinguishable from real ones in the Morning Report/`cmd_positions`/
`open_positions_status.py` — now tagged `🧪DRY-RUN-SIM`, "Manually Close" button suppressed; (6) no
mode gate on the new buy-side function — added defensively. 6 new tests target these exact failure
modes. Full suite: 244 passed (was 238). `live_sim_harness.py`: 6/6. `signals_invariants.py`: 1
known accepted violation (UDOW), unchanged.

## [live-trading][security] Resolved 2026-07-26 — `signals_invariants.py` built: startup + pre-commit config-invariant checks, 4 checks live. Full detail: `CLAUDE.md`'s Key Files entry.
Mitigates the Opus round-6 "stale-pending-buys guard" finding (still open, still needs live-Slack
testing before the handler itself is fixed) by guarding the config-state precondition instead —
plus 3 more checks from already-known backlog gaps (research-mode ticker still automation-scoped
with a real open position; `mode='live'` node with `account=None`; `brokerage.dry_run` flipping
False while the leverage-inclusive-cash gap is open). 1 known violation currently fires (UDOW's
seeded 2026-07-23 test position) — accepted, not a new bug, safe only because `ira` stays
`dry_run=True`.
Session-wrap Opus review of the diff found 2 CONFIRMED issues, both fixed same session: (1)
MEDIUM-HIGH — the new `run_loop` startup block was unguarded (outside any try/except, before
`_guarded`'s own per-section isolation kicks in), so a transient DB lock or a `log_slack_message`
exception could have prevented the live daemon from starting at all; fixed by routing through
`_guarded("invariants", ...)` and wrapping the `_post_message` call in its own try/except. (2)
MEDIUM — the research-mode-with-open-position check used a ticker-only `get_open_position` lookup,
which would false-positive on the deliberate DPST/GDXU live+research node pairs whenever the
*live* node held the real position; fixed to use `get_open_position_by_wl_id(node['id'])`. Also
noted, not acted on: the UDOW violation re-alerts on every daemon restart with no cooldown/dedup —
acceptable per user's call (it's a known, accepted condition), revisit if it becomes noisy.
`scripts/live_sim_harness.py`: 6/6 scenarios passed, both before and after the fixes.

## [live-trading] Resolved 2026-07-26 — "run both" real+paper restructure dropped, two-node pattern already covers it. Full detail: `docs/deep_backlog.md`'s 2026-07-26 entry.

## [live-trading][security] Resolved 2026-07-26 — session-wrap Opus review found+fixed an orphan-table forward hazard in the watch_list rebuild migration. Full detail: `docs/deep_backlog.md`'s 2026-07-26 entry.

## [live-trading][security] Resolved 2026-07-26 — Opus review of paper_alert_verbose/account-dedup diff: HIGH duplicate-node idempotency bug in 2 live-setup scripts, plus a rebuild forward-hazard and a stale-snapshot alert gate, all fixed. Full detail: `docs/deep_backlog.md`'s 2026-07-26 entry.

## [live-trading][security] Resolved 2026-07-25/26 — wl_id-keyed refactor implemented, reviewed (2 Opus rounds), landed. Full detail: `docs/deep_backlog.md`'s 2026-07-25/26 entry.

## [live-trading][security] Idea, raised 2026-07-26, explicitly gated on the wl_id refactor above landing and being observed correct first — per-node `dry_run` override, additive/OR-logic only, never replacing the account-level flag
Grew directly out of the wl_id-refactor design conversation above: once `wl_id`
gives real per-node identity, a node-level `dry_run` parameter becomes
possible where it wasn't safely before. **Deliberately additive, not a
replacement** for the existing account-level `dry_run` (`schwab_safety.
ACCOUNTS[account].dry_run`) — a node's flag can only make things *more*
conservative, never less:
`real_order_allowed = (account.dry_run == False) AND (node.dry_run != True)`
A node-level `dry_run=True` can force a specific node into simulation even
inside an otherwise-live account (e.g. keep a new/unproven node dry even once
its account goes real); it can never force real execution in a `dry_run=True`
account. This bounds the failure mode of a wl_id-resolution bug to "wrongly
forces a node into dry_run," never "wrongly executes a real order" — much
lower stakes than a naive node-replaces-account design, which was rejected
specifically because it would have multiplied the blast radius of the exact
class of ticker-vs-node bug the wl_id refactor above exists to fix.
**Explicitly not to be built alongside or immediately after the wl_id
refactor** — needs the refactor landed and observed correct in the field
first, since this idea's safety case depends entirely on `wl_id` being
correctly threaded through real order placement already.

## [live-trading][security] Resolved 2026-07-25 — second Opus review round on the daily_order_cap change, 2 findings
Full-diff review of the final `schwab_safety.py` state (increment + check both BUY-only, `soxl_ira`
cap 3→100), after the first round's SELL-blocked-by-BUY-exhausted-cap bug was already fixed.
Mutation-tested the new regression test (`test_sell_not_blocked_by_buy_exhausted_daily_cap`) by
reverting the guard — confirmed it actually fails without the fix, not passing coincidentally.
Two findings:
1. **CONFIRMED, fixed same session**: `is_protective`'s docstrings (`check_order`/
   `approve_and_record`) still advertised it as covering "a stop-loss placement or post-fill
   top-up" — stale now that SELL (including stop-loss placement) is unconditionally exempt from
   `daily_order_cap` regardless of `is_protective`. The flag is dead on that path now, only
   meaningful for the BUY-side top-up. Docstrings corrected to say so explicitly.
2. **PLAUSIBLE, not fixed, user's known tradeoff**: bumping `soxl_ira`'s cap 3→100 with
   `notional_cap` unchanged at $800 removes the de-facto cumulative same-day BUY-notional bound
   the low cap was incidentally providing (~$2.4k/day → ~$80k/day). Nothing else in the codebase
   reads the counter for a second purpose (checked — `STATE_PATH` is read only in `check_order`,
   written only in `approve_and_record`), so this isn't a hidden second-order bug, just the
   explicit consequence of the bump the user already asked for. Ties into the existing
   still-open "max cumulative BUY notional per ticker per day" backlog item.
Full suite: 232 passed. `scripts/live_sim_harness.py`: 6/6 scenarios passed.

## [live-trading][coverage] Resolved 2026-07-25 — pytest was polluting the real coverage_events table with fake "daemon_section_exception" rows
Found while cross-checking `docs/live_test_coverage.md` against real `coverage_matrix.py` output
for the Monday live-test replan: `daemon_section_exception` showed 360 real-looking rows
(detail=`boom`), which turned out to be pytest fault-injection noise, not real daemon failures.
Root cause: 5 of 6 test functions in `tests/test_run_loop_fault_tolerance.py` called
`active_signals._guarded` with a real raising exception but never requested the file's own
`isolated_db` fixture — `signals_db.log_coverage_event` is fire-and-forget (never raises into the
caller), so each test passed while silently writing a real row into
`cache/live/trading_live.db`'s `coverage_events` table. Only one test out of six had opted into
isolation. Fixed: made `isolated_db` `autouse=True` for the whole file. Backed up
`trading_live.db` (`cache/live/trading_live.db.bak_20260725_114349_pre_coverage_events_test_pollution_cleanup`)
then deleted the 360 polluted rows (`scenario_key='daemon_section_exception' AND detail='boom'`).
Checked `tests/test_coverage_check.py` (the only other file touching `log_coverage_event`) — every
one of its 32 tests already explicitly requests `isolated_db`, no gap there. Full suite: 232 passed.

## [live-trading][compliance] Open, raised 2026-07-25 (3rd time this has come up in conversation, not previously backlogged) — FINRA eliminated the PDT rule/$25k threshold in 2026; re-evaluate daily_order_cap's purpose and confirm Schwab's rollout status
Came up while discussing whether `daily_order_cap` (currently per-*account*, not per-ticker —
`schwab_safety.py:643`/`714`) should exempt SELL orders from *incrementing* the counter (currently
only `is_protective` SL/top-up orders are exempted from being *blocked* by it, not from counting
toward it — a real, separate gap from the already-fixed `is_protective` bypass, found this session,
not yet fixed or scoped). That question was originally framed as a PDT-compliance concern (does
Schwab/FINRA care how many same-day round trips — buy+sell same ticker same day — we do), which
prompted a real web-research check since the user suspected FINRA had changed something here and
this exact topic had already come up twice before without ever getting checked or backlogged.
**Confirmed via research (WebSearch, 2026-07-25)**: FINRA Rule 4210 was amended, SEC-approved
2026-04-14, effective 2026-06-04 (`Regulatory Notice 26-10`) — **eliminates the Pattern Day Trader
designation and the $25,000 minimum equity requirement entirely**, replacing the old "4+ day trades
in 5 rolling business days locks a sub-$25k account out" rule with a risk-based intraday-margin
framework (equity requirements now scale with actual intraday exposure; accounts with as little as
$2,000 can day trade without frequency restrictions under the new rule). **Caveat, not yet
confirmed**: brokers have until 2027-10-20 to fully implement — unconfirmed whether Schwab has
actually rolled this out on our specific accounts as of today (~7 weeks post-effective-date).
**Practical implications, not yet acted on**:
1. If Schwab has already implemented this, the original PDT round-trip-counting concern that started
   this whole discussion may be moot for our accounts — worth confirming directly with Schwab (account
   PDT-flag status) rather than assuming either way.
2. `daily_order_cap`'s design purpose should be revisited regardless of PDT: it was never actually a
   day-trade counter (it counts raw orders, not buy+sell round trips, and it's per-account not
   per-ticker — see the still-undecided per-ticker-vs-per-account item below) — it's a bug-safety
   backstop against a runaway/repeat-order bug, a distinct concern from PDT compliance either way.
3. **The SELL-increment gap found this session is still open regardless of the PDT question** — SELL
   orders (including protective ones) currently still consume the same shared daily counter as BUYs
   (`approve_and_record`'s `today[account] += 1` runs unconditionally for every order/side). Candidate
   fix, following the same precedent as `notional_cap` being made BUY-only (2026-07-24): stop SELL
   orders from incrementing `daily_order_cap` at all, not just exempt protective ones from the check.
   Not yet built — user was mid-decision on this when the PDT tangent came up.
**Action needed**: (a) confirm Schwab's real PDT-framework rollout status on the actual live accounts,
(b) decide the SELL-increment fix independent of that, (c) decide the actual bumped `daily_order_cap`
number for `soxl_ira` (was mid-discussion, not resolved this session either).

## [backtest][live-trading] Research idea, raised 2026-07-25 — monthly universe rescreen (recurring cadence) + a possible "v6" momentum-exhaustion-bounce strategy variant
Two related but distinct ideas from the same discussion:
1. **Recurring monthly rescreen, not a one-time backlog item**: rerun the same kind of full-universe
   resweep/screen used to originally find v5 candidates on a monthly cadence, watching for tickers
   trending positively that aren't on the current watchlist yet. User's framing: this is literally how
   AGQ was found the first time, before its later results soured (`[[project_watchlist_selection_rationale]]`)
   — a standing process, not a single research task. Not scoped: which script/cadence mechanism (cron?
   manual monthly reminder?), what "trending positively" threshold triggers a closer look, whether this
   folds into the existing per-ticker resumable sweep tooling (`scripts/run_sweep_queue.sh`,
   `scripts/campaign_comparison_table.py`) or needs its own lighter-weight screen.
2. **Possible "v6" idea, momentum-exhaustion bounce plays**: distinct from the current z-score
   mean-reversion-on-dislocation strategy family. Observation: a ticker that's had a lot of momentum,
   as it starts losing steam, often gets a technical-analysis-driven bounce (people keep trying to buy
   the dip/breakout continuation even as the real trend is rolling over) — a different signal shape
   than the existing "dislocation from a stable mean" setup. **Core risk-management idea**: if the big
   down days (the real trend continuation, not the bounce) can be avoided via a tight stop (~1%), the
   small bounce attempts might be clippable for alpha even in a name whose longer trend is turning
   negative. Not designed: what defines "losing steam" (momentum indicator, volume pattern, RSI/MACD
   divergence?), how this differs mechanically from the existing z-score entry logic, backtest period
   selection (same overfitting concern already flagged for the "v6" idle-capital-parking idea —
   momentum-exhaustion regimes are likely just as period-dependent). **Not yet named/versioned for
   real** — "v6" is already tentatively claimed by the idle-capital-parking research idea (see the
   2026-07-22 resolved/paused entry below); if both ideas proceed, one will need a different version
   tag. Pure research idea, not scoped or started.

## [live-trading] Resolved 2026-07-25 — coverage-system "compass" v2: node_id/mode identity migration + six-round Opus review chain. Full detail: `docs/deep_backlog.md`'s 2026-07-25 entry.

## [live-trading][security] Latent, found by Opus review round 6, 2026-07-25 -- stale-pending-buys guard could silently discard a real manual fill confirmation if a ticker is ever outside SCHWAB_AUTOMATION_TICKERS
Full detail in the round-6 entry above. `handle_entry_price`/`handle_trail_buy_fill_price` (added
round 4, `signals_handlers.py`) both check `any(p['ticker'] == ticker for p in db.get_pending_buys())`
before opening a position, on the assumption that every rendered Executed/Filled button implies a real
pending_buys row exists. That assumption breaks for a live-mode `TrailingExitZScoreBreakout` ticker
NOT in `SCHWAB_AUTOMATION_TICKERS` (`notify_buy_signal` only calls `db.add_pending_buy` when
`trailing_buy or market_buy_eligible`, and `market_buy_eligible` requires automation-scope
membership) -- such a ticker still renders a normal "Executed" button (per `_build_buy_blocks`'s
non-interactive `else` branch), but tapping it would hit the guard and be silently discarded as
"already resolved," with no position opened and no protective stop placed for a real fill. **Not
currently reachable** -- all 9 live TrailingExit tickers are confirmed in `.env`'s automation scope as
of 2026-07-25. **Action needed before this can bite**: either (a) fall back to
`db.get_open_position(ticker)` when no pending_buys row is found, treating "already open" as the only
real "already resolved" signal, or (b) always create a pending_buys row for every alert that renders
an Executed button, regardless of automation-scope membership. Needs live-Slack testing before
shipping either fix -- this session couldn't test the button/modal/handler chain end-to-end.

## [live-trading][security] Deferred, not currently reachable, found by Opus review 2026-07-24 evening — manual-"Filled" SL call is ticker-gated but not mode-gated
`signals_handlers.handle_trail_buy_fill_price`'s new `_place_stop_loss_for_position` call gates
only on `AUTOMATION_ENABLED_TICKERS`, not `node.mode == 'live'`, unlike the BUY-side routing in
`_scan_buy_signals`. Traced: not live-reachable today — the real `pending_buys` table (what makes
the "Filled" button exist at all) is only ever populated from `notify_buy_signal`
(`signals_notify.py:410`), which `_scan_buy_signals` only calls for `mode=='live'` nodes; a
research-mode ticker's BUY routes to `paper_trading.start_paper_buy` instead, which has no
"Filled" button. Would become reachable if the BUY-routing if/elif is ever restructured (e.g. the
"run both real+paper regardless of mode" idea already on file, or the Monday mode-scoping
decision). Add a `node.get('mode', 'live') == 'live'` guard alongside the ticker check if/when
that routing changes — see `docs/deep_backlog.md`'s 2026-07-24 evening batch-fix entry for full
context.

## [live-trading][coverage] Resolved 2026-07-26 — dry_run fill synthesis built (see the 2026-07-26 resolved entry above), closing this gap: dry_run does NOT auto-complete without it, which is exactly why the synthesis exists.
`_check_trade_lifecycle` (`scripts/coverage_check.py`) reads `trade_log`/`pending_buys` via
`signals_db.get_closed_trades_for_ticker_on_date`/`get_pending_buys_for_ticker_on_date` — correct
tables given all 6 canaries are currently `mode='live'`, `account='ira'` (not `research`/paper, an
initial reviewer premise that was checked against the live DB and found wrong). But for the 5
TrailingBoth canaries (IVV/QQQ/IWM/DIA/XLF — not VOO, which is `TrailingExitZScoreBreakout`), a real
trade only lands in `trade_log` after the manual "Trailing Buy Order Placed" → "Filled" Slack button
sequence, or via whatever automated fill-reconciliation path exists (`drain_fill_queue` et al.) — **not
yet confirmed whether `dry_run=True` ever auto-completes that sequence without a human tap**. If it
doesn't, the daily coverage check could show a "deviation" (no closed trade) for those 5 canaries most
days even when nothing is actually wrong — a false-positive-generating design gap in the very system
built to eliminate false "something looked off" ambiguity. **Action needed**: trace whether dry_run
auto-produces a fill event for TrailingBoth orders (check `signals_notify.py`'s fill-reconciliation path
against a dry_run account) before trusting `coverage_check.py`'s daily output for these 5 tickers.
Not investigated further same session — user deliberately deferred, heading out.
**User's framing, next session (2026-07-24 evening)**: called this "an important idea" in its own
right, not just a coverage-check caveat — this is really about **how dry_run tests completion** at
all, a question the user had already been independently thinking about (on the train the day before,
re: "how dry_run was going to handle no positions"). A candidate design raised: handle it the same way
paper trading does — **stage some positions as if they'd filled** (rather than requiring dry_run to
wait on a real fill event that may never come from a broker it never actually calls). Not the only
option discussed, just the one floated — pick this up as the first thing next session, per user's
explicit sequencing call.

## [live-trading][coverage] Minor, found 2026-07-24 ~evening via Opus review — record_deviation doesn't refresh expected_outcome on rerun
`signals_db.record_deviation`'s `ON CONFLICT` `UPDATE SET` refreshes `actual_summary`/`ts` but not
`expected_outcome` — if a `scenario_expectations` row's `expected_outcome` text is edited and the same
`(check_date, scenario_key, ticker)` deviation is re-recorded same day, the deviation row keeps the
stale `expected_outcome` value. Low-risk (informational field only, doesn't affect the met/not-met
pass/fail logic or any real trading decision) — backlogged, not fixed same session per user's call.

## [live-trading] Design note, raised 2026-07-24 ~16:20 ET — paper trading and dry_run test genuinely different things, not redundant with each other
`dry_run=True` runs the real order-placement code path (`schwab_safety.check_order` and every real
guard — cash check, `notional_cap`, `daily_order_cap`, duplicate-order guards, automation scope) all
the way to the real broker call, then stops — proves "would this order be correctly placed/blocked,"
never simulates a fill or tracks P&L. Paper trading (`paper_trading.py`) bypasses
`schwab_client`/`schwab_safety` entirely — never exercises the real guard code at all — and instead
simulates the full fill-to-exit lifecycle (bounce-fill timing, arm/trail/SL state machine, realized
P&L) against real price data. **Confirmed by today's actual results**: every single bug found today
(`notional_cap` blocking SELL, `daily_order_cap` starving SL placement, the `brokerage` account-hash
crash, the `add_node` dedup bug) came from dry_run/live activity going through the real guard code —
paper trading, even running, would never have caught any of them. Conversely dry_run never validates
whether the strategy's actual exit logic behaves realistically against real price movement, since it
never simulates a fill at all. **Conclusion**: complementary, not redundant — dry_run validates the
safety/guard layer, paper trading validates the strategy/execution-realism layer. Reinforces why
paper trading being fully dormant (see the entry below) is a real, separate gap, not something
today's dry_run-driven bug hunt already covers.

## [live-trading] Idea, raised 2026-07-24 ~15:05 ET — open a new margin account strictly dedicated to one real production ticker; keep soxl_ira as the standing multi-ticker test account
Today's `daily_order_cap` exhaustion (see entry below) traces to a real account-role mismatch:
`soxl_ira` ran 4 different tickers (GDXD/GDXU/ERY/LABU) through it today for setup convenience, which
doesn't match the real one-account-per-ticker model (`[[project_account_segregation_model]]`) —
`daily_order_cap=3` was sized for one ticker's real daily order volume, not four tickers sharing a
quota. **User's plan**: keep `soxl_ira` as a standing multi-ticker live-test account (not meant to
carry real production trading), and open a **new, separate margin account strictly dedicated to one
ticker** for actual real production trading, matching the intended model properly. Not scoped/actioned
yet — a real account-opening + `schwab_safety.ACCOUNTS` addition to do later, not today.
**Alternative/complementary feature idea, same discussion**: instead of (or alongside) the new
dedicated account, make `daily_order_cap` per-**ticker** rather than per-**account** — explicitly not
framed as a bug fix, but as a real feature needed *if* multi-ticker-per-account usage (like today's
`soxl_ira` test setup) continues rather than being fully replaced by one-account-per-ticker. A
per-ticker cap would let each ticker draw from its own quota, so one ticker's entries/exits/SL-
placements can't starve another's, without requiring a new account per ticker. Not scoped (schema
change to `AccountLimits`, how `check_order`/`schwab_order_counts.json` would need to key by
ticker+account instead of just account) — a real alternative design, worth weighing against the
new-account plan above rather than assuming one supersedes the other.
**User's leaning, same discussion**: if the real discipline is strict one-ticker-per-account
isolation (blast-radius containment, per `[[project_account_segregation_model]]`), then deliberately
blending tickers into one account's "swimlane" — even to solve the order-cap starvation problem —
undermines the reason that discipline exists in the first place. Leaning toward the new-dedicated-
account plan as the real fix, and treating the per-ticker-cap feature as a lower-priority fallback
only worth building if the team ever deliberately chooses to relax single-ticker-per-account
isolation, not as a way to avoid opening new accounts.

## [live-trading][tax] Idea, raised 2026-07-24 ~15:10 ET — run SOXL in both `roth` and `soxl_ira`, but `roth` needs to become limited-margin first
Grew out of the same-day-re-entry discussion (SOXL: ~29% of real historical trades involve a same-day
re-buy — see the `buy_alerted` day-lockout entry above) plus the account-segregation planning above.
`roth` is currently `account_type="cash"` (`schwab_safety.py:141`), so `same_day_block` would
structurally prevent it from ever capturing that same-day-re-entry slice of SOXL's edge — only a
margin/limited-margin account (like `soxl_ira`, `account_type="margin"`) can. **User's plan**: open a
**new Roth IRA with limited margin** (the existing `roth` account is a plain cash IRA) before SOXL
could meaningfully run there alongside `soxl_ira`/a future dedicated SOXL account. Both existing
`roth`/`soxl_ira` accounts are IRA-type, so no wash-sale cross-account concern between them (per the
already-resolved 2026-07-07 finding that IRA-realized losses never trigger wash-sale disallowance).
Not scoped/actioned — real account-opening step for later, tied to the broader account-segregation
plan above rather than a separate initiative.

## [live-trading][security] Resolved 2026-07-24 evening — `daily_order_cap` no longer starves SL-placement/top-up. Full detail: `docs/deep_backlog.md`'s 2026-07-24 evening batch-fix entry.

## [live-trading][security] Resolved 2026-07-24 evening — `notional_cap` now BUY-only, real position-size check added for SELL. Full detail: `docs/deep_backlog.md`'s 2026-07-24 evening batch-fix entry.

## [live-trading] Resolved 2026-07-25 — coverage-system reframe complete: Streamlit dashboard, strategy_type axis, structured expected-vs-actual, drill-down, Slack-callable report (all 7 pieces). Full detail: `docs/deep_backlog.md`'s 2026-07-25 entry.

## [live-trading][security] Resolved 2026-07-24 evening — `last_seen_bar` now seeded from real current bar at startup. Full detail: `docs/deep_backlog.md`'s 2026-07-24 evening batch-fix entry. Residual: `sell_alerted`/`window_alerted`/`limit_fill_alerted` still restart-unsafe, deliberately not fixed (no clean persisted-state reconstruction) — see that entry.

## [live-trading] Open, raised 2026-07-24 ~10:55 ET — retry `check_gap_resize`'s cancel+replace test properly pre-market on a future day, not mid-day
Today's GDXD/GDXU gap-resize test (docs/live_test_plan_2026-07-24.md) missed its actual goal — neither
ticker had genuinely gapped past its trigger overnight, so `check_gap_resize` correctly did nothing
and the real cancel+replace-with-MARKET code path remained unexercised. Considered manufacturing the
scenario mid-day (place a real trailing-buy now, seed `pending_buys.signal_price` artificially low so
the current price already "clears" the trigger, then call `check_gap_resize()` directly) — **rejected
by the user**: `check_gap_resize` is specifically a pre-market/before-open mechanic (correcting a
resting order based on real overnight price movement before the 9:30 open), and forcing it mid-day
wouldn't test that same thing — the underlying mechanics (cancel-order confirmation, market-buy
placement) are already separately proven via today's other real tests anyway, so a mid-day manufactured
version would add nothing new. **Real action for next time**: retry this properly on a future pre-market
morning — same setup pattern as today (seed a resting trailing-buy + `pending_buys` before market open),
but this time pick a ticker already showing real overnight gap movement (per the KORU 8.26% gap lesson
from today) instead of picking blind and hoping.

## [live-trading] Open, found 2026-07-24 ~10:35 ET — real BUY/SELL Slack alerts carry no canary tag, unlike the Reference Report or paper-trading's console tag
`notify_buy_signal`/`_build_buy_blocks` (`signals_notify.py:359`/`signals_blocks.py:91`, the actual
per-signal alert with buttons) have zero `version=='canary'` awareness — no tag, no visual distinction
from a genuine live candidate signal. Contrast with two places that got this right: (1) the Reference
Report table (`signals_notify.py:1235`) tags canary rows 🧪CANARY, a 2026-07-23 fix specifically
because canary rows becoming visible without a tag was flagged as unsafe; (2) the paper-trading print
path (`active_signals.py:203`) explicitly labels `(paper-trading)`. The per-signal live alert — the
one that actually posts to Slack with real buttons in real time — has neither. **Demonstrated live
today**: IVV's canary BUY signal (10:34 ET, after today's SPY-canary-swap) posted as a plain
"BUY SIGNAL — IVV $742.21 z=-1.23" message, visually identical to a real candidate alert, no
indication anywhere in the message that it's a canary. Action needed: add the same 🧪CANARY-style tag
to `notify_buy_signal`'s console print and `_build_buy_blocks`' Slack message (and check the SELL
alert path, `_build_sell_blocks`, for the same gap). Not fixed same day — found mid test day, low
urgency since canary real-trade buttons are already suppressed separately, but a real "is this real?"
ambiguity risk during any future live session with canaries mixed into the same channel.
**User's broader ask, raised same discussion, taxonomy corrected after discussion**: don't just fix
canary tagging specifically — tag *every* message with its real mode as a standing policy, regardless
of which channel it lands in. **There are only 3 real modes: Live / Dry-run / Paper — Canary is not a
4th mode, it's a deliberately-extreme parameterization of Paper trading** (same underlying mechanism —
`paper_trading.py`'s simulation — just absurd thresholds for fast proof-of-life, per the original
canary design). This reframes the "paper trading fully dormant" entry above too: today's canaries
running as `mode='live'` isn't a separate, unrelated gap — it's the *same* 2026-07-23 mode-flip
collision also breaking canary's intended design specifically, since canaries were meant to run as
paper trades (with extreme params), not live-mode nodes. Rationale for universal tagging: the
channel-routing idea (separate entry above, "split paper-trading/dry-run/live notifications into
separate channels") is the primary fix for noise/ambiguity, but if a message ever gets posted to the
wrong channel (misconfigured routing, a future code path that forgets to route correctly, etc.), a
universal mode tag on the message itself is a defense-in-depth backstop — self-identifying regardless
of where it ends up, rather than trusting channel placement alone. Fold into the same design
conversation as the channel-routing item and the Monday mode-scoping decision; every `_post_message`
call site would need this tag threaded through alongside whatever destination-routing logic gets built.

## [live-trading][backtest] Resolved 2026-07-24 evening — `buy_alerted` now unlocks after a genuine same-day close (real or paper), gated against resting pending buys. Full detail: `docs/deep_backlog.md`'s 2026-07-24 evening batch-fix entry.

## [live-trading] Open, found 2026-07-24 ~10:15 ET — paper trading is currently fully dormant system-wide (side effect of the 2026-07-23 all-mode='live' decision, not a deliberate choice); revisit Monday
`active_signals.py:199-203`'s BUY-signal routing is a strict if/elif: `mode='live'` → real/dry_run
alert path (`notify_buy_signal`); `mode!='live'` (i.e. `research`) + in `AUTOMATION_ENABLED_TICKERS`
→ `paper_trading.start_paper_buy`. Since every node in the watchlist has been `mode='live'` since the
2026-07-23 "revert live-fire dry-run test state... confirmed intentional" decision (see the superseded
entry below), the paper-trading branch is currently unreachable for every ticker, not just today's
soxl_ira test scope — a system-wide side effect the user hadn't realized until asking today "why am I
not getting anything in paper trading."
**Root cause is two good decisions from different sessions silently canceling each other out**, not a
single mistake: `docs/conversation_summary.md:2214` (~2026-07-18) records the *original* decision —
"Research mode now doubles as the target state for automation-engine dry-run ('paper trading')" —
i.e. keep everything in `mode='research'` as the simplified default, with paper trading as the
standing dry-run layer. The 2026-07-23 decision to flip all 16 nodes to `mode='live'` (justified on
its own terms — safe because `dry_run=True` protects every account except `soxl_ira`) never got
reconciled against that earlier research-mode-as-default decision, so nobody noticed it would also
kill paper trading system-wide as an unintended side effect — **including the 6 canary nodes**, which
per their original design are meant to run as a deliberately-extreme *parameterization* of paper
trading (same mechanism, absurd thresholds for fast proof-of-life), not as a separate concept. There
are only 3 real modes — Live / Dry-run / Paper — canary is not a 4th; today's canaries running as
`mode='live'` is the same root-cause collision misfiring on canary's design specifically, not a
separate gap (see the "tag every message with its mode" entry below for the corrected taxonomy).
Follow-ups user wants to revisit **Monday (2026-07-27)**, not this week:
1. Decide whether some/all research-mode-eligible tickers should go back to `mode='research'` now
   that today's live test day is done, to restore paper-trading coverage.
2. **Real feature idea, bigger than a quick fix**: restructure the if/elif into "do both" — run the
   real/dry_run alert path AND a parallel paper-trading simulation for the same node regardless of
   `mode`, so paper trading runs continuously as a standing regression signal (ties into the
   2026-07-24 "permanent canary tickers in ira" idea above) instead of being mutually exclusive with
   live/dry_run alerting. Would also need `paper_trading.py`'s dedup (currently ticker-only) to
   handle a ticker being tracked both ways simultaneously without colliding.
3. **Compounding gap found same investigation**: 15 of the 24 `mode='live'` nodes (all v5 tickers +
   4 of 6 canaries) have `account=None` — never assigned a real account at all. `check_order` requires
   a real account before it can even evaluate `dry_run` vs `live`, so these fail-closed as `BLOCKED
   ... unknown account 'None'` (confirmed live: the HIBL/IWM "BLOCKED TRAILING BUY" messages this
   morning) instead of producing a useful dry_run simulation. Net effect: with today's `mode='live'`-
   everywhere state, almost nothing produces an informative dry_run walkthrough at all — only UDOW
   has a real account (`ira`), and it's dedup-blocked from new BUYs by its existing position. Fold
   into the Monday mode-scoping decision above — deciding `mode='research'` vs `'live'` per ticker
   should go hand-in-hand with deciding whether it needs a real account assigned.
4. **Process note**: this whole chain (two colliding decisions + the account-mapping gap) slipped
   past the 2026-07-23 session-wrap Opus review, even though that review covered the very session
   where the mode='live' flip happened. Not a failure of that review specifically — a diff-scoped
   review checks the day's actual changes, not a 7-session-old decision it was never shown alongside
   the new one. Worth remembering as a class of gap that kind of review structurally can't catch:
   cross-session decision reconciliation, not single-diff correctness.

**Item 1 resolved 2026-07-25, ahead of the planned Monday date**: all 10 v5-version nodes (watchlist
65, excluding the 6 canary + `soxl_test` nodes) flipped back `mode='live'` → `research` via a direct
`watch_list` update (backfilled into `watch_list_audit`) — restores continuous paper-trading coverage
on the actual live tickers/strategy, matching the original research-mode-as-default design. Canaries
deliberately left as-is; user's read is they don't need coverage diversity the way the real watchlist
does (already non-overlapping ticker set, distinct proof-of-life purpose).

**Item 2 (run-both restructure) dropped, 2026-07-26**: the wl_id refactor already makes the
two-node pattern (one `research` node for continuous paper coverage, a second `live` node in the
real account, same ticker/strategy/params — exactly the DPST pairing added last session) a safe,
zero-code-change way to get both simultaneously. Verified before dropping: `paper_trading.py`'s
dedup is wl_id-keyed, not ticker-keyed (already fixed by the refactor), and `open_position_keys`
(`active_signals.py:530-531`) already merges real + paper positions — so two nodes for one ticker
don't collide. Restructuring the if/elif to run both paths off one node would have duplicated what
adding a second node already gives you for free; not worth building.

**Item 3 (`account=None` gap) reconfirmed resolved, corrected 2026-07-26**: re-checked directly
against the live DB — `get_watchlist()` (`signals_db.py:733`, filters to the active watchlist only,
`watchlist_id=65`, which is all `active_signals.py` ever polls) shows 0 of the current 15
`mode='live'` nodes have `account=None` today. Initially mischaracterized this as "moot because the
affected nodes are gone" — corrected: it was a real, deliberate fix, not an accident of the mode
flips. `watch_list_audit` (ids 168-182, all `2026-07-24 14:15:06`) shows all 16 nodes that had
`account=None` that day (10 v5 + 6 canary) were explicitly backfilled `account None -> ira` via
direct `edit_node` updates — an operational patch applied after the gap was found live, same day,
not a code-level guard. **Residual, not yet closed**: `add_node` still has no validation preventing
a *future* `mode='live'` call from omitting `account` again — the fix closed the specific instances,
not the class of bug. Low priority (hasn't recurred since; every live-node-creation script since has
passed `account` explicitly) — worth a cheap guard (warn or reject `add_node(mode='live',
account=None)`) if it's ever worth the effort, not urgent enough to block anything.

## [live-trading] Far-backlog, raised 2026-07-25, deprioritized same day — v5 watchlist skews long-only, consider adding inverse counterparts
All 10 v5 tickers are one-directional leveraged longs (SOXL, GDXU, NUGT, HIBL, KORU, DPST, UDOW, USD,
AGQ) except YANG (the only inverse ticker) — the whole book loses together in a broad leveraged-long
selloff, unlike the old v4 set which had some hedge-like pairing (EDC/AGQ as SOXL/KORU hedges, per
`[[project_watchlist_selection_rationale]]`). That portfolio-balance logic didn't carry over into the
v5 selection, which picked per-ticker on cliff-safe robust alpha only.
**Original motivation, clarified same day**: the real point of pairing isn't just directional balance
in the abstract — it's two concrete operational goals once manual live trading resumes: (1) a genuine
hedge (a down move in one leg is offset by the paired inverse leg), and (2) **guaranteed fill
activity** — pairing a long with its inverse means at least one leg of the pair is likely to be
signaling/filling at any given time, addressing the same sparse-signal problem that motivated flipping
v5 back to research mode this session (too few live orders to spread tests/validation across).
**User's call, same day**: pushed to far-backlog, not near-term. Two reasons: (1) the mean-reversion
strategy itself already makes money in both up and down periods (z-score breakout on a dislocation,
not a directional bet), so long-only isn't the raw exposure risk it looks like at first glance — a
50/50 long/short allocation would be a seismic reallocation shift, not a quick tweak; (2) there isn't
a good backtest period to properly optimize/validate a bear-pair selection without overfitting to
whatever the sample period happened to contain (same class of problem as the "v6" idle-capital-parking
work's overfitting issue, `[[project ... v6 parking]]`/see the resolved 2026-07-22 entry). Worth
researching eventually, but needs a real design conversation (allocation split, which counterparts,
how to validate without overfitting) before scoping, not just grafting existing v4 inverse nodes on.

## [live-trading][security] Resolved 2026-07-24 evening — `_trailing_buy_status` now anchors to the real `signal_price` trigger instead of a cache-derived one. Full detail: `docs/deep_backlog.md`'s 2026-07-24 evening batch-fix entry.

## [live-trading] Resolved 2026-07-24 evening — `TrailingBothZScoreBreakout` fills now get a real automated stop-loss (both auto-fill and manual-Filled paths). Full detail: `docs/deep_backlog.md`'s 2026-07-24 evening batch-fix entry. Live verification still planned for Monday 2026-07-27 per the original plan.

## [live-trading] Open, found 2026-07-24 ~08:53 ET — SL Slack alert falls back to a generic "should have auto-filled" guess instead of checking the real broker order book; user's call is this needs two code paths, not one query fix
Hit live: SPY's real SL alert (manually-seeded soxl_ira position, no `broker_stop_price` on file)
showed "Check account — Stop Loss order should have auto-filled @ $735.97" — a generic fallback in
`signals_blocks._build_sell_blocks` (`signals_blocks.py:225-231`) that branches only on our own DB's
`broker_stop_price` field, never the real broker order book. Confirmed manually via
`schwab_safety._open_orders("soxl_ira")` + `_has_open_sell_order` (already exist, used elsewhere,
e.g. `check_order`'s duplicate-order guard) that SPY genuinely has no resting stop order right now —
harmless this time (user's read: it's just an old generic code block firing, not a real risk), but
the alert could have said this definitively instead of guessing.
**User's framing, broader than a single query fix**: this is really two different kinds of position
wearing the same alert path — (1) a position the automation itself opened/placed the stop for
(`broker_stop_price` genuinely on file, trustworthy) vs. (2) a manually-seeded/manually-traded
position where we never placed anything and have no real broker linkage recorded at all (today's
SPY/SH test positions, and potentially any future manually-tracked position). Rather than patching
`_build_sell_blocks`'s single fallback string, worth auditing for other call sites that assume
`broker_stop_price`/our own DB state accurately reflects broker reality, and giving automated vs.
manual positions two explicit, clearly-labeled code paths: automated path trusts/reports the known
`broker_stop_price`; manual path always queries the live broker order book
(`_has_open_sell_order`/`_open_orders`) rather than guessing, since no DB field can be trusted for it.
**Action needed**: scope which other alert/decision sites have this same DB-assumed-true-for-broker-
state pattern before designing the two-path split — not just this one line. Backlogged, not fixed
same day per user's call (close to market open, low urgency since manual account-check is always the
safe fallback either way today).

## [live-trading][security] Resolved 2026-07-24 evening — `add_node`'s NULL-unsafe dedup fixed (explicit check-then-skip, includes `arm_sell_pct`/trail axes), existing duplicate rows cleaned up. Full detail: `docs/deep_backlog.md`'s 2026-07-24 evening batch-fix entry.

## [live-trading][security] Elevated priority 2026-07-24 morning — real evidence that neither notional_cap nor the cash check would catch a same-account double-buy
Confirmed empirically today, not just theoretical: placing two real resting `TRAILING_STOP` BUY orders
(GDXD 5sh, GDXU 3sh) left `get_account_balance('soxl_ira')` completely unchanged ($1,110.43 before and
after) — contradicts the assumption in `_has_open_buy_order_in_account`'s own docstring that Schwab
reserves buying power for a resting order (that finding was based on a bounded-price order the night
before, not a `TRAILING_STOP`). Practical consequence, walked through in detail this morning: in a
same-ticker double-buy scenario (e.g. `check_gap_resize` placing a replacement order while the original
somehow also still resolves for real — see the `cancel_order` confirmation fix above), **neither**
`notional_cap` (checked per-order, not cumulative) **nor** the real cash-availability check (sees the
undecremented balance, since the resting/just-filled original doesn't seem to move it) would catch it —
both individual orders are small enough to independently pass. This is the same structural gap already
flagged 2026-07-22 for the *multi-ticker-sharing-one-account* case
(`_has_open_buy_order_in_account`'s docstring), now confirmed to apply to the *same-ticker* double-buy
case too, and confirmed with real data instead of just reasoning about it. **User's call: "not good
enough"** — this needs an actual fix, not just a documented gap, before scaling beyond today's
single-ticker-per-scenario test. Candidate fix (not designed yet): a per-ticker cumulative same-day BUY
notional cap (ties into the already-backlogged 2026-07-22 "max cumulative BUY notional per ticker per
day" item, which was scoped for a different but related runaway-repeat-buy scenario — worth designing
both together).
**Existing partial mitigation, confirmed insufficient**: `signals_helpers._last_sale_recovery` already
sizes each order off the ticker's last-closed-trade proceeds (or `starting_notional` if none yet) — a
real soft ceiling against any *one* order being wildly oversized, used by both `buy_order_sizing` and
`check_gap_resize`'s replacement sizing. But it's per-order, not cross-order: for a ticker with no trade
history (like today's GDXD/GDXU), it falls back to the same `starting_notional` target for *any* order
attempt, so two independent, individually-reasonable-sized orders for the same ticker (the double-buy
case) would each pass it and still double real exposure together. Doesn't close the gap.

## [live-trading] Idea, raised 2026-07-24 morning — permanent canary tickers in the dormant `ira` account as a standing delayed-regression test
Insight from today's real `soxl_ira` test day: the canary-node pattern (deliberately inert
proof-of-life tickers, absurd thresholds, real Slack alerts but suppressed real-trade buttons) is
genuinely valuable beyond one-off testing. Since the existing `ira` account isn't supposed to carry
real trading activity anyway (manual live trading paused since 2026-07-18), it could permanently host
a small canary set — a standing, always-on regression signal that the full pipeline (signal
detection, Slack alerts, order-construction code paths) still works, without needing a dedicated test
day each time. Not scoped yet — which tickers, how many, whether `dry_run` stays True forever for
this account or gets a narrower always-real canary lane. Design conversation for later, not
blocking today's test plan.

## [live-trading] Open, raised 2026-07-24 morning — Morning Report block-limit failure regressed (23 nodes now), fix via ticker-scope reduction not another patch
The 2026-07-23 fix (collapsing each row's actions blocks) worked for 16 nodes; adding 7 more
`soxl_test` nodes this morning (SPY/SH/ERX/ERY/LABD/GDXD/GDXU) pushed the reference table back over
Slack's 50-block cap — `invalid_blocks` again, report failed to send entirely at today's restart.
**User's call**: don't patch the block count again — the real fix is trimming the live/canary ticker
list down to the realistic ~3-4 tickers actually trading at once, which naturally keeps the report
under the limit. Revisit once the `soxl_test`/canary sprawl is cleaned up post-test-day, not before.

## [live-trading] Open, raised 2026-07-24 morning — signal/reminder alerts don't show dry_run status, only real order-placement messages do
Found live: a stale (pre-restart) daemon fired a real `SELL SIGNAL — STOP LOSS` alert for the
newly-seeded SPY test position and sat "Waiting for Slack response (Exited/Skipped)" — required a
manual real-order-book/position check (`schwab_safety._open_orders`, `schwab_client.get_real_position`)
to confirm nothing was actually placed, purely because the alert itself gave no indication of the
account's `dry_run` state. `[DRY RUN]` is already prefixed on the real order-placement messages
(`_place_equity_order`/`_place_trailing_order`/`place_stop_loss` in `schwab_client.py`), but the
earlier signal/reminder alerts (`notify_buy_signal`, `notify_sell_signal`, trailing-buy/reminder
messages in `signals_notify.py`) fire before any placement attempt and say nothing about it.
**Action needed**: surface the account's real `dry_run` flag (or "🧪 TEST" style tag for a
non-production node/account) directly on these earlier alerts too, not just the placement
confirmation — cheap, avoids exactly this kind of "is this real?" ambiguity during real trading days.

## [live-trading] Open, raised 2026-07-24 morning, CORRECTED 2026-07-24 ~10:15 ET — split paper-trading/dry-run/live notifications into separate channels (or otherwise reduce chattiness)
User's real complaint: the single trading Slack channel is getting noisy alongside today's real
`soxl_ira` dry_run=False test activity and the stale-daemon SPY/SH signals from this morning — hard
to tell what's actually real at a glance (compounds the dry_run-visibility gap above). **Correction**:
this entry originally assumed "10 real v5 + 6 canary nodes, all research-mode" were the noise source
via paper trading "running continuously" — that assumption is wrong. Confirmed today (see the
"paper trading fully dormant" entry above) that every node in the watchlist is actually `mode='live'`,
not `research`, so paper trading isn't running at all right now — the actual noise source today is
real/dry_run alerts across many simultaneously-live nodes (soxl_test + v5 + canary, all `mode='live'`),
not a paper-trading/live mix. The channel-routing need still stands (still hard to tell what's real
at a glance across dry_run vs. live vs. eventually-paper messages), but the framing of *why* was
wrong. **Action needed**: design a channel-routing scheme (e.g. separate channels/webhooks per mode,
or a consistent filterable tag prefix per message) so the real trading channel isn't drowned out —
still needs a design conversation, not a quick fix, since every `_post_message` call site would need
a mode-aware destination. Revisit once the Monday paper-trading-mode decision (see entry above) is
made, since that'll add a third real message source back into the mix.
**Further refined 2026-07-24 ~16:15 ET**: today's chattiness is a **shakeout-phase** artifact, not
the permanent target state — many tickers/canaries/tests all live at once, actively being debugged.
Once the real watchlist narrows to its actual production size (1-3 tickers genuinely live-trading),
the **live channel specifically** should get quiet/focused again — real trading shouldn't be noisy.
Other channels (paper/canary/ongoing-test activity) can stay as chatty as needed, since that's where
exploratory shakeout work belongs. So the channel-routing design should explicitly account for this
trajectory (loud-now/quiet-later on the live channel specifically) rather than just splitting by mode
as a static end-state — ties directly into the coverage-system reframe above, which is itself meant
to be the shakeout-phase tool that eventually lets the live channel go quiet with confidence.

## [live-trading][security] Open, raised by session-wrap Opus review 2026-07-23 night — `availableFunds` is leverage-inclusive for a real margin account, unverified whether that's safe for `brokerage`
`schwab_client.get_account_balance`'s new fallback (`cashAvailableForTrading` → `availableFunds`,
see the balance-bug fix below) is confirmed correct/fail-closed for `soxl_ira` (a *limited-margin*
IRA — no leverage, `availableFunds ≈ real cash`). Flagged, not fixed: for the real taxable
**`brokerage`** account (`schwab_safety.py:139`, genuine Reg-T margin), `availableFunds` reflects
cash plus loan value of held marginable positions minus margin requirement — i.e. it can exceed
settled cash. `check_order`'s cash-availability check is meant to be a conservative real-cash gate;
against `brokerage` it could currently pass a BUY partly funded by borrowing rather than settled
cash. Bounded today by `dry_run=True` + `notional_cap`/`CASH_SAFETY_BUFFER`, so not urgent — but
should be resolved (e.g. prefer a settled-cash-specific field for `account_type=="margin"`) before
ever flipping `brokerage` to real trading. Also flagged: `scripts/live_sanity_check.py`'s own copy of
the same fallback logic (line 67) is duplicated inline rather than calling `get_account_balance` —
justified (the script fetches balance+positions in one call, deliberately bypasses
`schwab_client`/`schwab_safety`), but a real future drift risk if the field-name fallback ever needs
to change again. A shared small helper (e.g. `_extract_available_cash(balances)`) would remove the
duplication cheaply.

## [live-trading][security] Open, real findings from 2026-07-23 night live-order testing on soxl_ira — fix + retest before trusting the automation path
Real orders placed directly against the new `soxl_ira` account (bypassing `schwab_safety` via raw
`schwab_client`/`schwab.orders` calls, same pattern as `scripts/live_sanity_check.py`) to sanity-check
real Schwab behavior before Friday. Findings, most-important first:
1. **Real bug, fixed same night**: `schwab_client.get_account_balance` read
   `cashAvailableForTrading`, which doesn't exist on a MARGIN-type account (confirmed against both
   `soxl_ira` and the existing `ira` — both report `type: MARGIN` from Schwab's real API, `ira` despite
   being configured `account_type="cash"` in `schwab_safety.ACCOUNTS`, a separate discrepancy worth a
   sentence in the weekend review). Correct field: `availableFunds`. Fixed with a fallback (try
   `cashAvailableForTrading`, else `availableFunds`) since cash-type accounts (sep/roth) have never
   been confirmed either way. `scripts/live_sanity_check.py`'s own separate balance read had the
   identical bug, fixed too. This had been fail-closed (blocked every real BUY on any margin account
   via an uncaught `KeyError` → `SafetyViolation`), not a silent bypass, but would have blocked
   legitimate BUYs indefinitely if not caught tonight.
2. **Not yet fixed — order placement/cancellation are asynchronous, and both the sanity script and
   the real production path (`schwab_client._place_equity_order`/`_place_trailing_order`) treat the
   initial HTTP response as the final verdict.** Confirmed live: an oversized BUY (1007 SOXL, ~$153k
   against $1,110 real cash) returned HTTP 201 (`place_order` succeeded, no exception) — the sanity
   script read that as "UNEXPECTED: accepted" and alerted — but a follow-up `get_order` call (made
   ~0.3-0.7s later, not instant) showed the *real* verdict: `status: "REJECTED"`,
   `statusDescription: "You do not have enough available cash/buying power for this order."` Same
   shape confirmed for `cancel_order` (HTTP 200 doesn't mean cancelled — confirmed via a fast, ~0.3s
   follow-up poll showing `CANCELED`). Production code currently posts `✅ ... submitted to Schwab`
   right after the HTTP response with no follow-up status check — a real rejection would look
   identical to a real success in Slack, with the only symptom being the existing fill-reminder loop
   nagging forever with no rejection reason ever surfacing. **Action needed**: add a follow-up
   `get_order` poll (short retry loop, since resolution time isn't instant/guaranteed) after every
   real placement/cancel in `schwab_client.py`, surface a real rejection distinctly from
   "still pending," and fix the same blind spot in `live_sanity_check.py`. Retest after fixing.
3. **Interesting, not necessarily a bug**: a second live-fire finding contradicts an assumption
   baked into existing code. `schwab_safety.py`'s `_has_open_buy_order_in_account` guard (built
   2026-07-22) was justified by "Schwab doesn't reserve buying power for a resting order." Tonight's
   real test showed the opposite for a same-account resting BUY: placing a second $738 trailing BUY
   while a first $738 trailing BUY was still resting (`AWAITING_STOP_CONDITION`) against $1,110.43
   cash got a real `REJECTED`, `"You do not have enough available cash/buying power for this order."`
   — meaning Schwab *did* hold the first order's funds. The existing guard is still reasonable
   defense-in-depth (untested: whether this holds across *different* tickers in the same account, the
   actual scenario the guard was built for — only same-ticker was tested tonight), but the stated
   rationale may not be accurate. Worth revisiting this weekend, not urgent.
4. **All clean/expected results, no incidents**: naked_sell (SOXL, 0 held) → `REJECTED`
   ("oversold/overbought"); oversell (SPY, sell 4 vs 3 held) → `REJECTED`, same reason, even with a
   resting order already partially reserving shares; a stacked-orders sequence (sell 1 regular +
   trailing sell 1, both resting, reserving 2 of 3 real shares) correctly allowed a 3rd resting sell
   of 1 more share (exactly at the 3-share boundary) then rejected a 4th; a resting *unfilled* BUY did
   **not** count toward sellable shares for a subsequent oversell attempt (correctly still rejected).
   Trailing buy/sell both construct real `TRAILING_STOP` orders via schwab-py's actual `OrderBuilder`
   (not custom-built), confirmed via real placement (`AWAITING_STOP_CONDITION` status, a real status
   value not seen before, distinct from `PENDING_ACTIVATION` for plain market orders).
5. **Test B done tonight via direct bypass (didn't actually need `check_order`/window-gate wiring —
   it's purely Schwab's real cash check, not ours)**: BUY 2 SPY (~$1,476.52) against real cash
   $1,110.43 — only $366 over, not a 50x-oversized case like the first test — still a clean
   `REJECTED`, `"You do not have enough available cash/buying power for this order."` Confirms the
   rejection holds right at a tight margin, not just when wildly oversized.
   **Still not yet run — needs real signal-window testing (BUY-side `check_order` only runs
   10:25-10:40/15:25-15:40 ET), planned for tomorrow (2026-07-24)**:
   - **Test A**: set `soxl_ira.notional_cap=$1`, attempt any real BUY (ticker wired to the account) —
     expect `SafetyViolation` for exceeding `notional_cap`, blocked before Schwab. This one genuinely
     needs the full `check_order` path (our own static cap, not Schwab's dynamic cash check), so it
     couldn't be shortcut via bypass the way Test B was.
All real test orders placed tonight were fully resolved (filled: none: cancelled: all) — no open
resting orders left in `soxl_ira` as of this entry. **User wants this whole night's findings
reviewed together this weekend** — treat this entry as the source material for that review.
All tonight's ad hoc tests (plus the two already-logged `sanity_*` events, corrected for the
false-positive read above) were backfilled into `signals_db.coverage_events` after the fact —
`scripts/coverage_matrix.py` now reflects them (`manual_buy_trailing`, `manual_buy_trailing_stacked`,
`manual_sell_regular`, `manual_sell_oversell`, `manual_sell_trailing`,
`manual_sell_trailing_oversell`, `manual_sell_stacked_boundary`, `manual_naked_sell_via_bypass`,
`manual_penny_stock_restriction`, `manual_buy_market_buffer`, `manual_order_status_async`).
Also tested after that: the fixed `STOP` order type (`place_stop_loss`'s real order shape, not the
production wrapper) — a real STOP SELL was accepted and rested as `PENDING_ACTIVATION`, **the same
status label a plain MARKET order uses**, unlike `TRAILING_STOP` which shows the distinct
`AWAITING_STOP_CONDITION` — worth knowing if any reconciliation logic ever branches on status
strings expecting STOP and TRAILING_STOP to look different while resting. Placing it also surfaced a
real, actionable warning from schwab-py: `passing floats to set_price and set_stop_price is
deprecated and will be removed soon` — `schwab_client.place_stop_loss` (line 309,
`order.set_stop_price(stop_price)`) passes a float directly, matching the deprecated pattern
exactly. Not urgent (still works today), but will break on a future schwab-py upgrade if not
migrated to passing prices as strings. Cancelled cleanly after (`CANCELED`).
`schwab_safety._broker_confirms_order`/`_all_orders` (the real duplicate-order guard's broker-truth
check) were sanity-checked against the real order history this produced (36 orders: 16 canceled, 18
rejected, 2 `FILLED` — the two `FILLED` ones were confirmed to be the original pre-staged SPY/SH
positions from earlier in the day, not anything from tonight's after-hours testing) — correctly
returns `False` for every resolved order, and a follow-up live check confirmed it also correctly
returns `True` for a genuinely still-resting order. Both directions now confirmed working; treat as
closed, not a gap.

**Biggest remaining gap, not yet tested at all**: every real order tonight went through a direct
bypass (`schwab_client`/`schwab.orders` calls made ad hoc, same pattern as
`scripts/live_sanity_check.py`) — none of it touched `schwab_safety.check_order`'s real guard chain,
and **none of it exercised the actual production/Slack workflow** (`notify_trailing_activated`, the
"Trailing Buy Order Placed" → "Filled" two-step Slack confirmation, `_place_equity_order`/
`_place_trailing_order`'s real Slack messaging). That's the literal path you'll be relying on live
tomorrow, and it has zero real-order confirmation behind it yet — everything tested tonight confirms
Schwab's own behavior and our bypass scripts, not the actual daemon-driven flow a human will interact
with. Also still untested: a real fill (nothing filled tonight, market was closed — `get_filled_order`
field parsing remains unverified against a real fill response, and it's unconfirmed whether the
account-activity stream fast path pushes a fill event or only the poll fallback catches it), and the
real Part-3 gap-correction cancel+replace sequence (`check_gap_resize`). **Action needed**: at least
one real order tomorrow should go through
the actual production wrapper (not the bypass) during a real signal window, to close this gap before
trusting the automated path for anything beyond ad hoc API-level confirmation.

## [live-trading] Open, planning in progress 2026-07-23 night — Friday 2026-07-24 real-account test plan on the new limited-margin IRA
Superseding the "revert live-fire state" framing below: user confirmed 2026-07-23 night that
`mode='live'` on all 16 `watchlist_id=65` nodes and the disengaged kill switch are **intentional**,
not leftover test state — safe because every account in `schwab_safety.ACCOUNTS`
(brokerage/sep/roth/ira) stays `dry_run=True`. The fake UDOW position (740 sh @ $67.55,
`account='ira'`, armed `trailing=True/peak=$69.57`) is also being left in place — that account
isn't part of tomorrow's plan and stays dry_run=True, so no real order can result from it.
**Real plan for tomorrow**: the user manually pre-staged two *real* positions in the new
limited-margin IRA today (outside our system) — SPY and SH (inverse S&P 500, -1x) — specifically
so arm/trailing-sell logic can be exercised against real live positions. Wants to test "as much as
possible" given the account only holds ~$5k. **Not yet done, blocking everything else**: the new
IRA account has **not been reauthenticated/reconnected** to the Schwab API yet — user hasn't
confirmed whether its token scope + compliance trading permission have actually cleared (last
known status 2026-07-22: still blocked on both). Given `scripts/schwab_oauth_setup.py` to run
themselves in their own terminal tonight; it'll show how many accounts are linked, which is the way
to confirm whether the new IRA is visible yet. **Still needed before Friday, once reauthed confirms
the account is visible**: (1) add the new IRA to `schwab_safety.ACCOUNTS` (account_type='margin' per
the existing "limited margin IRA = no same-day cash-settlement restriction" convention, dry_run
decision TBD), (2) real account-number suffix, (3) seed `open_positions` rows for the real SPY/SH
positions (real share counts/entry prices, pulled from the account or user-provided) with a node
config appropriate for exercising arm/trailing-sell, since neither ticker's existing watchlist-65
node fits as-is (SPY's is a deliberately-inert `canary` node; SH isn't on the watchlist at all),
(4) decide dry_run scope precisely — leaning only the new IRA goes `dry_run=False`, nothing else.
**Not fully planned yet** — user wants to think through this further before anything is built;
treat as a design conversation to finish, not a go-ahead to implement.

## [live-trading] Open, raised 2026-07-23 night — Schwab auth flow is "clunky", no cleaner unattended path designed yet
User's real complaint: when the 7-day token goes stale, there's no interactive capability inside
`active_signals.py` to complete the OAuth login (correctly, by design — headless daemon can't
answer a browser prompt) — the only path is running `scripts/schwab_oauth_setup.py` manually in a
separate terminal, and since `schwab_client._get_client()` caches the client in a module-level
global (`_client`, set once, never invalidated), even a successful manual reauth doesn't take
effect until the daemon process is **restarted** — an easy thing to forget mid-trading-day. Not
scoped yet: whether the fix is a Slack alert reminding to restart after any reauth, a way to force
`_client` to reload without a full daemon restart, a pre-market token-expiry check/warning, or
something else. **Action needed**: design conversation before building anything.

## [live-trading][security] Superseded 2026-07-23 night — "revert live-fire dry-run test state" (see item above)
Original framing (2026-07-23 morning): all 16 `watchlist_id=65` nodes were `mode='live'` (should be
`research`), the kill switch was disengaged (should be engaged), and UDOW had a fake open position
(740 sh @ $67.55, `account='ira'`) seeded directly into the real `open_positions` table.
`dry_run=True` protected this throughout so no real order was ever possible. **User confirmed
2026-07-23 night this state is intentional, not leftover** — see the item above for current plan
and reasoning. Full incident detail still in `docs/deep_backlog.md`'s 2026-07-23 "Live-fire
dry-run test state left in place" entry.

## [live-trading] Resolved 2026-07-23 — Schwab OAuth `interactive=True` default hung the daemon (main client + stream thread), both fixed; alert-spam gap also closed
Full detail: `docs/deep_backlog.md`'s 2026-07-23 entry. Short version: `schwab_client._get_client()`
and `schwab_stream._run_stream_once()` both defaulted to `interactive=True`, contra
`schwab_auth.py`'s own documented unattended-context intent — fixed to `interactive=False` at both
call sites. Real nuance: schwab-py's `easy_client()` still attempts (and fails) the login flow
even with `interactive=False` when the token's stale/missing — converts a hang into a clean
exception, not a no-op — so `schwab_stream`'s existing reconnect loop will still retry-and-fail
indefinitely on a persistently stale token (not yet fixed, see deep_backlog). Opus review of the
diff also caught the reconnect loop's failure-alert Slack-posting the real trading channel on
every retry (not just logging) — fixed with a 15-min cooldown on the alert specifically (matches
`active_signals._SECTION_ALERT_COOLDOWN_SECS`'s existing convention; console/log stays
unthrottled). Full suite: 185 passed.

## [live-trading][security] Resolved 2026-07-22 — stale-cache race at market open (HIBL paper trade entered and SL'd in 31 seconds)
Full writeup: `docs/research_log.md`'s 2026-07-22 "HIBL paper trade" entry. Short version:
`signals_compute._current_price()` read the local CSV cache with zero staleness check, so a poll
landing between market open and that ticker's first same-day refresh could silently hand back
*yesterday's* closing price as if it were live — the mechanism behind HIBL's paper entry filling
at a stale $104.09 (matching the prior day's close exactly) and immediately SL'ing once fresh data
landed. Fixed: `_current_price()` now returns `(None, None)` if the cache predates today and the
market's open; all 6 call sites already handled `None`. Real (non-paper) exposure existed too —
`_check_position_exit`'s mid-bar branch and `_check_limit_fill` share the same function — but
never fired live (`dry_run=True` everywhere). Independent Opus review confirmed the fix and flagged
one residual gap: on a live day, a genuine (non-open-race) data-refresh failure now silently
suppresses a real position's intrabar exit check via only a `log_poll` trace, no Slack alert.
**Resolved 2026-07-23** — see `docs/deep_backlog.md`'s 2026-07-23 "Slack alert for the
stale-price-guard silent-suppression gap" entry.
Also built this session: `signals_db.slack_message_log`/`log_slack_message`/`get_slack_messages`
(full text + mode of every real `_post_message` call — previously zero persistent record existed,
which is why the user's morning reference report was unrecoverable after scrolling past it in
Slack) and a shared `log_poll()` helper (`signals_helpers.py`) writing `[poll]`-prefixed trace
lines to `VERBOSE_LOG_PATH`, wired into every price/bar-consuming decision point found during this
investigation. Full suite: 181 passed. Not yet committed as of this backlog entry — see session
close commit.

## [live-trading] Resolved 2026-07-22/23 — canary watchlist nodes for daily paper-trading proof-of-life, all six built and live
Full design writeup moved to `docs/deep_backlog.md`'s 2026-07-23 entry. Short version: `scripts/
add_canary_nodes.py` now adds all six (A-F) to the active watchlist, `starting_notional` raised
500→10000 (the sizing bug that would've sized to 0 shares at SPY/QQQ prices, caught by Opus review),
and run — all 6 confirmed present in `watch_list` (watchlist_id=65, 16 nodes total now: 10 real v5 +
6 canary). `_scan_pinned_exit_arm`'s paper-blind-spot (no canary can exercise it, real positions only)
left as an accepted limitation, not closed — deferred to the `live_sim.py` harness item below, which
can call it directly against synthetic positions.
**Immediate follow-on discovery, same session**: adding these 6 research-mode rows to the watchlist
is what surfaced the reference-report bugs below — they'd been invisible for weeks because the
report had been silently rendering zero candidate rows the whole time.

## [live-trading][security] Resolved 2026-07-23 — Morning Report silently rendered empty for weeks (mode filter), then broke outright once fixed (Slack block-limit)
Full incident writeup in `docs/research_log.md`'s 2026-07-23 entry (the debugging path is worth
keeping — it's a case study in an observability gap producing a wrong initial diagnosis). Two
real, separate bugs, found while chasing "the restart didn't send a report":
1. **`build_reference_table` (`signals_notify.py`) filtered to `mode == 'live'` only.** Every
   watchlist node has been `mode='research'` since the 2026-07-20 v5 promotion, so the report has
   been posting successfully (header/kill-switch/context blocks) with **zero candidate rows**
   underneath — no error, no indication anything was wrong, just silently useless. This is
   exactly what the user originally suspected ("send it even if not live") and what got wrongly
   ruled out mid-session (checked `send_reference_report` for mode-gating, found none, missed
   that the function it calls has its own filter). Fixed: removed the filter, all nodes render
   now, each row carries `Mode` for display.
2. **Immediately exposed a second bug**: with all 16 rows rendering, the message hit 53 Slack
   blocks — over the hard 50-block-per-message limit — and Slack rejected it outright
   (`invalid_blocks`). Fixed: collapsed each row's up-to-3 separate `actions` blocks (manual
   open/close, automation pause/resume, auto-fill toggle) into one (Slack allows up to 5 elements
   per actions block), cutting per-row block count enough to fit again.
3. **Safety fix alongside #1**: research-mode rows becoming visible meant canary nodes (deliberately
   absurd parameters, never meant to be traded) would get the same "Manually Open {ticker}" button
   as any real candidate for the first time. Suppressed for any node with `version == 'canary'`
   (automation_principles.md #0/#7 — a newly-exposed surface must not silently inherit an action
   that was previously unreachable). Also added a `🧪CANARY`/`(research)` tag to non-live rows so
   they're never visually confused with an actionable live trigger.
Verified: fresh report sent and independently confirmed via `chat.getPermalink` (not just trusting
the API response) — real permalink, all 16 rows present, canaries tagged. Full suite: 181 passed.

## [live-trading] Resolved 2026-07-23 — two logging/observability gaps found while chasing the above, both fixed
Found because the above incident was genuinely undiagnosable in real time — worth fixing on its
own merits, not just as a side effect:
1. **`logs/active_signals.log` never flushed.** `human_fh = open(HUMAN_LOG_PATH, "a")`
   (`active_signals.py`) had no explicit `.flush()` anywhere, unlike the verbose log — since it's
   not a tty, Python fully block-buffers it, so console output (including any `[slack error]`
   line) could sit invisible on disk for the buffer's lifetime. Confirmed live: the file's mtime
   was frozen for 10+ minutes while the daemon was demonstrably still looping (heartbeat proved
   it). Fixed: `open(HUMAN_LOG_PATH, "a", buffering=1)` (line-buffered). Takes effect on next
   restart — done, daemon already restarted with the fix live.
2. **`slack_message_log` recorded intent, not delivery.** `db.log_slack_message(mode, text)` was
   called *before* the real `chat_postMessage`/webhook attempt in `signals_blocks._post_message`
   — a row's existence was wrongly read as proof of a successful send mid-incident (a real mistake
   made in this session, not just a hypothetical risk). Fixed: new nullable `error` column
   (migration applied to the live DB, backed up first as
   `trading_live.db.bak_20260722_233127_pre_slack_log_migration`), and the log call moved to
   *after* the attempt, populated with the caught exception/HTTP-status string (`None` = no error
   caught). `_post_message` also now returns `(channel, ts)` reliably from a single code path.
   `send_reference_report` and `active_signals.py`'s two call sites (startup + scheduled) now
   print/capture that return value instead of discarding it, so a future incident has a real
   `ts` to check via `chat.getPermalink` instead of nothing.
Both root-caused entirely by inspection/live testing, no unit tests added yet for either (the
flush behavior and the error-column plumbing are both straightforward enough that a live restart
was the real verification — see `docs/live_test_coverage.md` if this should get a synthetic test
later). Full suite: 181 passed throughout.

## [live-trading] Resolved 2026-07-23 — `scripts/live_sim_harness.py` built (non-interactive coverage harness)
Full detail: `docs/deep_backlog.md`'s 2026-07-23 entry — 6 scenarios, ~2s full run, plus a real
`get_open_position` trail_state bug and a real safety incident (harness polluted the live
`schwab_order_counts.json`, remediated with a new `SCHWAB_STATE_DIR` env override in
`schwab_safety.py`) found and fixed along the way. **Resolved 2026-07-23**: wired into `CLAUDE.md`'s
`session wrap` (runs when `active_signals.py`/`signals_*.py`/`schwab_*.py` changed) and documented
as `docs/automation_principles.md` #11.

## [backtest] Open, paused 2026-07-22 — "v6" idle-capital parking idea; inconclusive, downturn-specific follow-up queued
Full detail in `docs/research_log.md`'s 2026-07-22 entry. Short version, in the order the
analysis actually evolved this session:
1. **First pass (restricted to EOD-exit gaps only, 62 windows across all 10 watchlist-65
   tickers)**: no vehicle showed a robust edge. SPY itself lost money (-5.4% compounded);
   the "top 30" leaderboard (TSLQ, SARK, WEBS, BERZ, SQQQ, ...) reversed sign completely in an
   out-of-sample 50/50 split check — clean overfitting, not a real effect.
2. **Broadened to every exit→next-entry gap, not just EOD ones** (776 windows) — the EOD-only
   restriction was itself the flaw (idle capital exists between any two trades, not just
   overnight ones). Re-scanned; had to exclude the 10 source tickers from the candidate pool
   (scoring e.g. KORU against KORU's own gap windows measures "should we not have exited," a
   different question, not the parking one — this had been silently inflating the first
   broadened run's top result to a nonsensical +68,113%).
3. **Corrected broadened result**: SPY-long is now sign-consistent positive across a 50/50
   split (+65.9%/+123.2%, both halves positive) — better than the first pass, but still ~50%
   win rate, so the compounded number is carried by a few big windows, not a consistent
   per-window edge. Short/inverse SPY (SH/SDS/SPXU/SPXS) all lost consistently in both
   halves — the mirror image, as expected.
4. **Not yet run**: check whether SPY-long's apparent edge is really just "the market went up
   most of this period" by scoring separately during real identified SPY drawdown episodes
   (`>=5%` from trailing peak) vs. everywhere else — 11 episodes identified in the 2023-08 to
   2026-07 window, notably 2023-09-21→2023-10-11 (-7.9%), 2023-10-18→2023-11-06 (-10.3%),
   2024-08-02→2024-08-13 (-8.4%), 2025-03-06→2025-05-12 (-19.0%, the big one), 2026-03-19→
   2026-04-08 (-9.1%). If the "hedge" thesis (our exits cluster around bad market days) is
   real, SPY-long should do *worse* and inverse should do *better* specifically inside these
   windows than outside them — not yet checked.
**Action needed**: run the downturn-episode-specific scoring (candidates already narrowed to
SPY/QQQ/SH/SDS/SPXU/SPXS/SQQQ/QID/BIL from this session's discussion) before drawing any
further conclusion. Scripts ready: `scripts/sim_v6_parking_vehicle_sweep.py` (windows already
cached in `output/v6_gap_windows.csv` with the broadened 776-window definition),
`scripts/sim_v6_split_check.py --split 50-50`/`70-30`/`5fold` (supports arbitrary fold configs).

## [live-trading] Resolved 2026-07-23 — coverage_events fully wired (stale "~13 remaining" note corrected, `daemon_section_exception` added)
Full detail: `docs/deep_backlog.md`'s 2026-07-23 entry. Short version: nearly everything on the
old "not yet wired" list turned out to already be wired (the note was stale); the one real gap
(`_guarded()` not logging whether the daemon survives a section exception) is now fixed via a
new `daemon_section_exception` scenario key. Full suite: 185 passed. Treat as closed — revisit
only if a new control site is added.

## [live-trading][security] Planned for Friday (2026-07-24 WFH day) — real-account sanity tests: oversized BUY + naked SELL across several tickers, on the new limited-margin IRA only
Built this session (uncommitted): `scripts/live_sanity_check.py` — standalone, bypasses
`active_signals.py`/`signals_notify.py`/`schwab_safety` entirely (calls the schwab-py client
directly via account-suffix resolution, not nickname, since the new IRA isn't in
`schwab_safety.ACCOUNTS`/`NICKNAMES` yet) — same pattern as the manual $200k buying-power test
(2026-07-17). Two tests: `oversized_buy` (BUY 50x+ more shares than real cash affords, expect
Schwab rejection at placement) and `naked_sell` (SELL 1 share of a ticker held at 0, expect
rejection, not an accidental short — script hard-aborts if real shares are actually held).
Requires per-ticker typed confirmation (type the ticker again), never loops/retries, logs every
outcome to `coverage_events` (`scenario_key=sanity_oversized_buy`/`sanity_naked_sell`, `mode='live'`).
**User confirmed 2026-07-22**: only the new limited-margin IRA is going live (not
brokerage/sep/roth/ira yet) — but multiple tickers can be sanity-tested in sequence against it
since each test's notional/quantity is small/rejected by design. **Scope decision, same day**: only
run the two guaranteed-safe rejection tests for now; hold off on the deeper small-real-fill test
menu (tiny real BUY + SL placement + trailing-stop + `get_real_position` field check + sell, ~5
more tests, each closing a specific "unverified against a real account response" gap in
`docs/live_test_coverage.md`) until after seeing how the rejection tests land.
**Blocker**: user needs a WFH day to be present for this (can't run during market hours while at
work) — **planned for Friday 2026-07-24**. Also still needs: the new IRA's real account-number
suffix, and confirmation that its API token scope + compliance trading permission have actually
cleared (last known status 2026-07-22: still blocked on both) — check before Friday, not after
showing up to run the script.

## [live-trading] Idea, raised 2026-07-22, not designed — small (10-share) real EDC pilot position, held ~1 month to shake out issues before scaling
Distinct from the sanity-check tests above (those are one-shot, expect-rejection tests; this
would be a real, sustained, small live position). Not yet scoped: which account (the new limited-
margin IRA, presumably, matching "only going live on 1" above, but not confirmed for this
specifically), which node/strategy config (EDC's `watch_list` row was removed entirely 2026-07-19 —
would need a fresh node, not a revival of the old v3.27 one, since that's tied to the existing
manual 423-share position being unwound separately), and whether it runs through the real
automation path (`mode='live'`, in `AUTOMATION_ENABLED_TICKERS`, real Slack BUY/SL/trailing flow)
or is a one-off manually-placed position just monitored for drift. **Action needed**: design
session before building anything — account, node config, sizing override (10 shares fixed, not
the usual `starting_notional`-driven formula), and automation scope.

## [live-trading][tax] Open question, raised 2026-07-22 — which ticker (SOXL vs AGQ) goes to the taxable brokerage account first; wash-sale cross-account mechanic needs one more confirmation
Real comparison pulled this session (v5, `open_check`, `scripts/campaign_comparison_table.py
--tickers AGQ SOXL`): AGQ's winner is TE (`TrailingExitZScoreBreakout`, best alpha 1114.9%,
worst_neighbor 285.2, win rate 31.2%, 128 trades, no trailing-buy order to catch manually,
no dividends — cleaner tax treatment); SOXL's winner is TB (`TrailingBothZScoreBreakout`, best
alpha 1212.1%, worst_neighbor 153.9, win rate 9.9%, 151 trades, pays small dividends). **Real,
practical gap**: AGQ's liquidity (1% ADV ≈ $2.0M/day) is drastically thinner than SOXL's (≈
$136.0M/day) — a $50k AGQ order is a much bigger fraction of daily volume, closer to the
HIBL/EDC thin-liquidity fragmentation case than to SOXL. Leaning AGQ for the tax/win-rate/cliff-
safety reasons, capped by how large it can scale; not a final decision.
**Wash-sale mechanic reconfirmed this session, not fully resolved**: an IRA/SEP/Roth-realized loss
never triggers a wash sale at all (no recognized loss to disallow — resolved 2026-07-07). Only a
loss realized in a real taxable account matters, and it's direction-specific: rebuying in the
*same or another taxable account* just defers the loss (recoverable via basis); rebuying in an
*IRA-type account* within 30 days permanently disallows it (Rev. Rul. 2008-5). The one AGQ loss on
file (2026-07-07, in brokerage, clears 2026-08-06) is the safe/deferred case if AGQ is bought back
in brokerage itself — **not yet confirmed**: whether there's an AGQ loss in some *other taxable*
account (not sep/roth/ira, which don't count) that isn't in this file. **Action needed**: user to
confirm whether any such other-taxable-account AGQ loss exists before finalizing the brokerage
ticker decision.

## [live-trading][security] Not started, discussed at length 2026-07-22 — max cumulative BUY notional per ticker per day (backstop against a repeat-buy/runaway bug)
Raised while reviewing this session's order-submission retry mechanism: none
of the existing guards actually bound total same-day BUY exposure to a
single ticker. Walked through each one and confirmed the gap is real:
`notional_cap`/`HARD_ORDER_CEILING` are per-*order* only; `daily_order_cap`
counts orders per *account*, not per-ticker notional; the resting-order
guard (`_has_open_order`) offers no protection against repeated **market**
buys specifically, since a market order fills almost instantly — by the
time a second buy attempt fires, the first is already `FILLED` (terminal,
excluded from the open-orders check), so nothing stays resting for the
guard to catch; the duplicate-order guard only catches a retry within
`DUPLICATE_ORDER_WINDOW_SECS` (60s) and `DUPLICATE_ORDER_QUANTITY_TOLERANCE_PCT`
(5%) of the same quantity — a bug that re-sizes each attempt or fires more
than 60s apart slips past it entirely. The cash-balance check doesn't cover
it either: it bounds total *account* capital, not intended *trade size* —
an account can (and with compounding, is expected to) hold materially more
cash than one ticker's `starting_notional`, so a 4x-sized repeat-buy bug
could still pass the cash check every time.

**Design converged on, not yet built**: a flat multiplier on
`watch_list.starting_notional` (not a fixed dollar figure, so it scales per
ticker automatically) — leaning **1.1x** for the normal case. **Dynamic
case**: if a same-day sell already closed the position for this ticker
(this strategy always exits fully, so "the most recent same-day sell" is
unambiguous, no need to sum multiple), base the cap on that sell's real
notional (shares × exit price) instead of the static `starting_notional` —
tracks recycled capital rather than a plan-time number that may be stale
relative to actual account growth. Leaning **~1.0-1.05x** the sell notional
for that case specifically (not stacked on top of the normal 1.1x
allowance) — 1.0x exactly is workable but would occasionally trip on the
existing same-day sizing pad (`buy_order_sizing`'s ~1% `pad_pct`/
`market_pad_pct` overshoot) for a perfectly legitimate re-buy, requiring a
rare manual override; 1.05x absorbs that pad without ever blocking a normal
re-buy. **User wants to think about this further before committing to
a number or building it** — explicitly asked to backlog rather than
implement now. On breach: block + Slack alert (not also tripping the kill
switch — that idea was floated and dropped in favor of the simpler
per-order block).

## [live-trading] Low priority, idea raised 2026-07-21 — should `CASH_SAFETY_BUFFER` scale with order size instead of staying a flat $200?
Raised while fixing the buffer amount itself (see the resolved cash-check item
below — flipped from an accidental $1,000 hard requirement to the intended
$200 per-order overage cushion, with the user's real ~$1,000 reserve habit
kept separate and unenforced). Open question, not scoped: a flat $200 covers
fee/price-tick overage fine for a modest order, but may be too small (as a
fraction of the order) for a large notional, or unnecessarily conservative
for a tiny one. Possible shape: buffer as a percentage of `notional` with a
floor, or keyed off the ticker's typical bid-ask spread/volatility rather
than a single constant. Per `automation_principles.md` #7a (prefer simple
over precise when precision doesn't buy much), don't build this unless a
real case shows the flat $200 actually causing a problem — no evidence of
that yet, every account still `dry_run=True`.

## [live-trading][security] Resolved 2026-07-21 — account cash/buying-power check built: `get_account_balance` + `check_order` wiring, quantity-aware, fail-closed
Full design context (why this was needed) preserved below the line. Built this
session: `schwab_client.get_account_balance(account)` (`Client.get_account`,
parses `securitiesAccount.currentBalances.cashAvailableForTrading` — field
names follow Schwab's documented schema but are **unverified against a real
account response**, flagged for live confirmation, see `docs/live_test_coverage.md`).
Wired into `schwab_safety.check_order`'s single BUY chokepoint — every BUY-
placing path (`place_equity_buy`, `place_trailing_buy`, the top-up, the
gap-correction replacement) goes through it; confirmed by an independent Opus
review (2026-07-21) that no BUY path bypasses it. Requires
`cash_available >= notional + CASH_SAFETY_BUFFER` (`CASH_SAFETY_BUFFER = 200`,
a deliberate small flat cushion for per-order overage — fees/a quote-to-fill
price tick — instead of precise real-time accounting. Deliberately *not* a
restatement of the user's own much larger cash reserve habit (~$1,000 kept
in each account); `notional` is sized independently off `starting_notional`/
compounding logic, not off real cash, which is exactly why this check exists
at all — see `docs/automation_principles.md` #7a). Fails closed: any exception from
the balance fetch itself raises `SafetyViolation`, blocking the order rather
than letting it through unchecked. Four existing test files' fixtures
monkeypatch `get_account_balance` (large default) so existing BUY-path tests
don't hit a real API; 5 new dedicated tests in `test_schwab_safety.py`
(insufficient cash, buffer-on-top-of-notional, sufficient-cash passes,
balance-fetch-failure fails closed, SELL exempt). Full suite: 142 passed
(was 137).
**`CASH_SAFETY_BUFFER` was initially built as `1_000` — a bug, not the
intended design**: caught by the user immediately after review — the $1,000
was meant to be the user's own operational cash-reserve habit, not something
`check_order` enforces as a blocking requirement. Fixed same session:
`CASH_SAFETY_BUFFER` is `200` (a small per-order overage cushion, the only
thing actually enforced as a block), and a new, separate, **non-blocking**
`CASH_RESERVE_WATERMARK = 1_000` check posts an informational Slack warning
(💰 emoji) whenever `cash_available < CASH_RESERVE_WATERMARK` on a BUY check
— lets the user know to add cash before the reserve actually runs out,
without blocking the order. Deliberately checks the raw balance, not
"balance after this trade" (simpler, per user's own call — `automation_principles.md`
#7a). 2 new tests cover the warning firing and not firing. Full suite after
both fixes: 153 passed.
**Second real gap found by the Opus review and fixed same session**: Schwab
does not reserve buying power for a resting order (already known from the
existing same-ticker duplicate-order guard's own docstring), so **two
different live tickers sharing one account could each pass the cash check
against the same undecremented balance** — the flat $200 buffer alone
doesn't cover a second simultaneous BUY. Not reachable today (every live
ticker has its own account, per `_live_ticker_accounts`) but not structurally
enforced. Fixed with a real-order-book check (not a local heuristic, per
`automation_principles.md` #1): `schwab_safety._has_open_buy_order_in_account`
blocks a second concurrent BUY into an account that already has *any* other
ticker's resting BUY order, reusing the same `_open_orders()` fetch the
existing same-ticker guard (`_has_open_order`, refactored to share one API
call instead of two) already makes — no extra network cost. 3 new tests
cover this. Also tightened test hygiene as a side effect: the existing
same-ticker duplicate-order-book check was previously **hitting the real
Schwab API unmocked** in every BUY-path test (a pre-existing gap, not
introduced this session) — now mocked (`_open_orders` returns `[]` by
default) in all four fixtures, per `automation_principles.md` #8. Full
suite after this fix: 145 passed.
**Not yet fixed, two smaller residual findings from the same review, logged
as their own items below**: (1) the balance-fetch network call now happens
while `approve_and_record`'s cross-account file lock is held — a slow/hung
fetch would stall order processing for every account, not just the one being
checked; (2) `cashAvailableForTrading`'s exact field semantics (e.g. whether
it's margin-inclusive) are still unverified against a real account response.
**User's proposed empirical validation, still not built**: fund a real
(margin-enabled) account with a small amount (~$100), flip that one account's
`dry_run` to `False`, and place a single ~$100.50 order via a minimal
standalone script (bypassing `active_signals.py`/`signals_notify.py`/
`schwab_safety` entirely, calling `schwab_client.place_equity_buy` directly)
to observe Schwab's real behavior — this would be the first real (non-dry_run)
order this system has ever placed, so treat deliberately. Not scripted yet.
Related but distinct real-world note (not actionable code, confirm with
Schwab directly rather than assume): a short position from an oversell is
likely self-limiting in no-margin (IRA-type) accounts (order rejected, not
executed as a short), but could actually execute in the margin-enabled
account; unsure of Schwab's specific margin-call/PDT-flag policy for a brief
accidental short — don't assume benign without confirming.
Original framing (2026-07-21, before this fix): `schwab_client.py` had no
function at all to read an account's real cash balance, and
`schwab_safety.check_order` only enforced a fixed per-account `notional_cap`
(a static dollar ceiling), never actual available cash — so any BUY order
(including Part 3's top-up, which places a real order) exceeding real
available cash would either get rejected (no-margin accounts) or silently
draw on margin (margin-enabled accounts), with zero check on our side.

## [live-trading][security] Resolved 2026-07-21 — cash-balance network call held inside `approve_and_record`'s cross-account file lock
Found during the Opus review of the cash-balance check (resolved item
above). `check_order`'s cash-availability check (and the order-book fetch
for the duplicate-BUY guard) run while `approve_and_record` holds
`_open_locked()` — a global lock shared across every account. A slow or
hung Schwab call would stall order processing for every account's order,
not just the current one, for as long as schwab-py's 30s default httpx
timeout takes to fire. Fixed per the proportionate option flagged at the
time (`automation_principles.md` #7a): `schwab_client._get_client()` now
calls `client.set_timeout(_CLIENT_TIMEOUT_SECS)` (10.0s) once at client
creation, bounding every Schwab HTTP call — not just the two under the
lock — to 10s instead of 30s. Simpler than restructuring lock ordering,
same failure mode either way (fails closed once the call errors out).
New `tests/test_schwab_client.py::test_get_client_applies_short_timeout`.
Not fully closed: still bounded by 10s, not eliminated — restructuring the
lock so only the count/cap bookkeeping (not the network calls) runs under
it remains a further option if 10s is ever observed to matter in practice.

## [live-trading][security] Resolved 2026-07-21 — pre-existing test hygiene gap: some `schwab_safety` tests were silently hitting the real Schwab API
Found during the Opus review of the cash-balance check above. The same-ticker
duplicate-order-book check (`_has_open_order`, now refactored to share a
fetch with `_has_open_buy_order_in_account`) was being exercised in every
BUY-path test via a real, unmocked `get_orders_for_account` call against
whatever the actual `ira` account's real order book happened to contain at
test-run time. Ran the follow-up sweep this item asked for: every test file
that imports `schwab_safety`/`schwab_client`
(`test_schwab_safety.py`/`test_part3_gap_resize.py`/`test_part4_entry_trigger.py`/
`test_schwab_automation.py`/the new `test_schwab_client.py`) already mocks
`schwab_safety._open_orders` and `schwab_client.get_filled_order` in its
fixture — no stragglers found, nothing left to fix.

## [live-trading][security] Resolved 2026-07-21 — `active_signals.run_loop` fault tolerance built: per-section isolation + outer last-resort net
Full original context preserved below the line. Fixed this session, using
the granularity decision the item left open: **both** approaches, not one or
the other. A new `_guarded(section, fn, *args, **kwargs)` helper
(`active_signals.py`, just above `run_loop`) wraps every previously-unguarded
loop-body section (reference report, gap-resize check, window alerts,
pinned exit-arm/entry scans, the per-position exit-check loop — now also
per-position isolated via a `_check_position_exit` helper so one bad
position doesn't stop the rest — paper-sell checks, reminders, auto-fill
checks, fill-queue drain, paper-buy updates, the per-node limit-fill loop —
similarly per-node isolated — and both buy-signal scans): catches and logs
any exception, posts a rate-limited Slack alert (15min cooldown per section,
so a persistent failure doesn't spam every poll) rather than silently
swallowing it (`automation_principles.md` #4), and lets the loop continue to
its next section/iteration. On top of that, the whole loop-body block is
still wrapped in one outer try/except as a last-resort net, catching
anything that slips through an individual guard (e.g. a bug in the glue code
between sections) so the daemon can never die from an unhandled exception in
`run_loop` — logs and posts a 🔴 Slack alert, then proceeds to the next poll.
**Not yet done**: no fault-injection tests exist yet exercising any of these
paths (each guard's behavior is straightforward enough to reason about, but
per `automation_principles.md` #8, everything should be testable — add tests
that inject a raising stub into a couple of the wrapped sections and confirm
the loop survives and alerts, rather than trusting the wrapping by
inspection alone). Not yet observed live either — see
`docs/live_test_coverage.md`.
Original framing (2026-07-21, before this fix): found while discussing how to
chaos-test resilience to real-world failures (bar/data timeouts, real-time
price request timeouts, position/order-book request timeouts). Two spots
already isolated failures correctly (`_refresh`'s per-ticker timeout+catch,
`_scan_pinned_entry`'s per-ticker try/except) but everything else in
`run_loop`'s `while True:` body had no exception handling at all, and there
was no top-level try/except around the loop body either — a single
unexpected exception anywhere would propagate all the way up and kill the
entire daemon process, stopping monitoring/protection for every open
position on every ticker, not just skipping the one thing that failed.

## [live-trading][security] Resolved 2026-07-22 — `schwab_safety`'s duplicate-order guard now confirms against Schwab's real order book, not just a local pre-flight record
Full original context preserved below the line. `approve_and_record` still
writes the order into its local `recent_orders` dedup list before the real
`place_order` call happens, but `check_order`'s duplicate check now only
blocks a matching retry for a **real (non-dry_run) account** if a new
`_broker_confirms_order` cross-check finds a genuinely-accepted (WORKING/
QUEUED/FILLED, not CANCELED/EXPIRED/REJECTED/REPLACED) order for that exact
ticker/side/quantity in Schwab's real order book (`_all_orders`, a new
unfiltered sibling of `_open_orders`) — so a failed/rejected/errored prior
attempt no longer wrongly blocks a legitimate retry. Dry-run accounts keep
the old pure-local-heuristic behavior unchanged (nothing real to verify
against). 2 new tests in `test_schwab_safety.py`. Full suite after this fix
and the critical trail_state fix below: 172 passed (was 164).
Original framing (2026-07-21): found while fixing Part 3's top-up to place a
real broker order. `approve_and_record` writes the order into its local
`recent_orders` dedup list *before* the real `place_order` call happens
(`schwab_client._place_equity_order`) — if that broker call then fails/times
out/is rejected, the guard still believes it succeeded, so a legitimate
retry within `DUPLICATE_ORDER_WINDOW_SECS` (60s) was wrongly blocked as a
duplicate. Not urgent at the time — no real order had been placed yet
(`dry_run=True` everywhere, still true).

## [live-trading][security] Resolved 2026-07-22 — CRITICAL: trailing-arm state clobber caused re-arming and duplicate live trailing-sell orders (oversell risk); found by a full-stack Opus review
Found via a scoped independent Opus review of the whole automation stack
(`schwab_client.py`, `schwab_safety.py`, `active_signals.py`,
`signals_notify.py`, `signals_compute.py`, `signals_db.py`,
`paper_trading.py`), requested to look at seams between features each
already individually reviewed in prior sessions. `check_sell_condition`
(`signals_compute.py`) persists the newly-armed state (`trailing=True,
peak=P`) to the DB correctly, but `notify_trailing_activated`
(`signals_notify.py`) then merged its reminder fields onto the **stale**
in-memory `pos['trail_state']` (the pre-arm copy the caller passed in) and
overwrote the DB with it — silently losing `trailing`/`peak` right after
arming. On the next bar close the position looked unarmed again, `check_exit`
re-armed it, and (since a TrailingBoth position entered via trailing-buy has
no `sl_order_id` to cancel) `_attempt_automated_sell` placed a **second**
live trailing-sell order for the same shares — an oversell risk if both
filled. **Fixed**: new `signals_db.get_position_by_id(position_id)` (fresh
single-row lookup by primary key); `notify_trailing_activated` now re-reads
the position before merging, so it starts from the real just-armed state,
not the stale one. 1 new regression test.

**Two more findings from the same review, fixed alongside it, per explicit
user direction on scope**:
- SELL-side order placement had **no resting-order duplicate guard at all**
  (only BUY did) — this is what let the state-clobber bug above actually
  stack two live orders. Fixed: new `schwab_safety._has_open_sell_order`,
  wired into `check_order` as a same-ticker (not account-wide, unlike the
  BUY guard — an unrelated resting BUY must not block closing a position)
  SELL-side check. 2 new tests.
- `_attempt_automated_sell` cancels a resting stop-loss *before* confirming
  the trailing-sell placed; if placement then fails, the position is left
  with zero protection and no automated way to recover it. **User's explicit
  call**: don't restructure the cancel/place ordering (no real alternative
  that avoids some window of risk) — instead just make sure the user is
  told what to do. Now posts a 🚨 UNPROTECTED Slack alert with the SL price
  to manually re-place. 1 new test.

Also fixed as its own item (poll-loop/Slack-handler race, user: "yeah i
think loop poll double open/close needs a fix"): `open_position`/
`close_position` (`signals_db.py`) each did a SELECT-then-act with no lock
spanning both statements — the poll loop thread and the Socket Mode Slack
handler thread (same process, `active_signals.py` starts both) could each
pass their own existence check before either committed, risking a duplicate
open or a racing double-close silently overwriting `trade_log`'s exit
fields with the losing thread's values. Fixed with a plain
`threading.Lock()` (`signals_db._position_lock`) — single-process/
multi-thread daemon, not the cross-process concern `schwab_safety`'s file
lock guards. `close_position` now also returns `True`/`False` (was
implicit `None`) and is a safe no-op if the row is already gone. 3 new
tests.

**Not fixed, explicit user call**: kill switch / per-ticker pause also
blocks *protective* sell orders, not just new entries (found by the same
review) — this is existing, understood behavior (turning the algo off means
accepting the exposure), not a bug to patch.

Full suite: 172 passed (was 164). `verify_trailing_buy_resolution.py`/
`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` clean post-fixes.
Nothing here has been observed live — every account still `dry_run=True`.
## [live-trading][security] Resolved 2026-07-22 — live-state reconciliation check built: detection + text-only proposed remediation, never auto-executes
Related idea from the item above, split out once actually built. Originally
scoped as: does `open_positions.shares` match the broker's real position
size, and does a resting protective order (SL or trailing-sell, matching the
position's current phase) actually exist at the broker for that exact
quantity? **User explicitly flagged the danger**: this must stay detection/
alert-only (Slack notify on mismatch) — an auto-correcting version would be
a new automated-trading decision layer on top of the ones this project has
already found real bugs in, and a false-positive mismatch (legitimate
slippage, a deliberate manual override, a timing lag) would trigger a real,
wrong, automated trade to "fix" something that wasn't actually broken.
**Refined at the time**: a hybrid middle ground — on a detected mismatch,
compute and post a *proposed* remediation (e.g. "top up N shares" / "place
missing SL at $X") to Slack, rather than either silent auto-correction or a
bare alert with no suggested fix.

**Built 2026-07-22, scoped down to text-only** (confirmed with the user
before building: a clickable approve-button version would be a much bigger
build — new Slack handler, new execution path through `schwab_safety`,
needing its own dedicated safety review — deferred as a possible v2, not
built now). New `schwab_client.get_real_position(account, ticker)` (real
share quantity via `Client.get_account(fields=[Account.Fields.POSITIONS])`,
field names unverified against a real response, same caveat pattern as
`get_account_balance`). New `signals_notify.check_live_state_reconciliation`
(called every `run_loop` poll cycle via `_guarded`, automation-scope tickers
only — matches where the real order-placement risk actually is today):
compares broker-real shares vs. `open_positions.shares`, and whether the
expected resting order (SL pre-arm, trailing-sell post-arm) is actually
resting, via the existing `schwab_safety._open_orders` order-book fetch.
Posts a text-only Slack alert with the suggested fix on any mismatch —
never places an order itself. Broker treated as ground truth (automation_
principles.md #1); alerts rate-limited 15min per (position, mismatch-kind)
to avoid repeat-alert spam. 8 new tests (`tests/test_live_state_reconciliation.py`).
Full suite: 164 passed. `verify_trailing_buy_resolution.py`/
`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` clean (required
since `active_signals.py`/`signals_notify.py` changed).
Not yet done: no real mismatch has ever been observed live (every account
still `dry_run=True`) — tracked in `docs/live_test_coverage.md`.

## [live-trading] Implemented 2026-07-21, not yet live-tested — Entry Trigger/Fill/SL-Placement/Arm-latency automation for TrailingExitZScoreBreakout (Part 4)
Full design at `/home/pkim/.claude/plans/replicated-gliding-quasar.md` (not
git-tracked); implementation summary in `docs/design.md` (Layer 3, "Part 4"
entry). Automates the 6 `TrailingExitZScoreBreakout` watchlist-65 tickers'
(AGQ, DPST, KORU, NUGT, UDOW, YANG) previously fully-manual BUY flow: pinned
single-shot entry/exit-arm checks (`active_signals._PINNED_BAR_TIMES`,
`:30:02` each hourly bar boundary) replacing ambient 5-min polling,
`schwab_client.get_session_open_price()` (Schwab's real `openPrice`) for
detection-drift-free open-check entries, `_attempt_automated_market_buy`/
`notify_buy_signal`'s new branch for real market-order placement, a genuine
resting STOP order (`schwab_client.place_stop_loss`, new `open_positions.
sl_order_id` column) placed via a synchronous fast-confirm step immediately
after fill (real gap, not previously automated for any ticker — ~70-80% of
this strategy's trades exit via SL), and `_scan_pinned_exit_arm` collapsing
the trailing-sell arm-detection latency from up to 5 min to ~2s. Also found
and fixed while implementing: `schwab_safety._OPEN_CHECK_WINDOWS` was one
minute too late for the new pinned checks (`(9,31,...)`→`(9,30,...)`), and a
real `signals_compute.compute_buy_signal` bug where `prev_close` read the
unsliced `df_daily` (today's/latest close) instead of `df_daily_prior` — found
via the new backtest-replay verification script, see `docs/research_log.md`'s
2026-07-21 "Part 4 implementation" entry for the full writeup.
**A phase-by-phase scenario review the same day (continuing session) found and
fixed three more real bugs**, see `docs/design.md`'s Part 3 entry for full
detail: (1) the SL price was anchored to the real fill price instead of the
trigger/signal price, diverging from the backtest's stop formula whenever a
market order slipped; (2) the async fallback fill-detection paths
(`check_auto_fills`/`drain_fill_queue`/`check_gap_resize`) never actually
placed the SL if the synchronous fast-confirm path timed out, contradicting
the timeout alert's own claim; (3) Part 3's post-fill top-up never placed a
real broker order at all, only DB bookkeeping — fixed alongside a
`schwab_safety` duplicate-order-guard tightening (now quantity-aware, ±5%
tolerance) needed once the top-up started actually submitting orders.
Verified: full `pytest tests/` (137 passed, was 107 pre-session);
`scripts/verify_pinned_entry_vs_backtest.py` (Deliverable 1) — 5/6 tickers
100% clean, AGQ's one mismatch is a real, expected stock-split-day divergence
(see research log entry), not a defect; `verify_trailing_buy_resolution.py`/
`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` clean post-fixes.
**Not yet done**: Deliverable 2 (`scripts/verify_open_price_quality.py` +
`open_price_quality_log` table) needs real trading-day data to populate — the
daemon hasn't run live with this code yet; no real order tested end-to-end
(every account still `dry_run=True`); `dry_run=False` flip pending both.

## [live-trading] Implemented 2026-07-21, not yet live-tested — trailing-buy budget adherence (Part 3: padded sizing + overnight gap guard + post-fill top-up)
Full writeup in `docs/design.md` (Layer 3, "Part 3" entry) and `docs/research_log.md`'s
2026-07-21 design entry. Resolves both the "trailing-buy needs re-sizing" and
"gap-through-trigger fill optimism" backlog items with one unified design:
same-day sizing pad (`buy_order_sizing(node, sig, pad_pct=1.0)`), a pre-open gap
guard (`signals_notify.check_gap_resize`, fired once daily from `active_signals.
run_loop` at `_GAP_CHECK_WINDOW=(9,15,9,29)` — cancels+replaces a resting trailing
buy with a plain MARKET order only if the overnight gap already cleared the
trigger), and a post-fill top-up (`_reconcile_buy_fill`/`_reconcile_fill`, fed by
both the existing `check_auto_fills` poll and a new account-activity websocket,
`schwab_stream.py`). All 8 implementation tasks done; full `pytest tests/` green
(107 passed, 9 new), `verify_trailing_buy_resolution.py`/
`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` clean, `pending_buys.order_id`
migration confirmed additive-only against a `trading_live.db` backup.
**Not yet done**: no real order has been tested end-to-end (every account is still
`dry_run=True`); `schwab_stream.py`'s account-activity payload parsing is unverified
against a real fill event — confirm both before trusting this beyond dry-run.
Also open: confirm with Schwab whether the limited-margin IRA falls under FINRA
Rule 4210's new intraday-margin cure mechanism (user will confirm directly, or a
test run can help — Schwab typically emails a notification for this); once
confirmed, rerun the pre-market-to-open drift check against Schwab's own quote
feed (not yfinance) to make sure the flat 5% gap-guard pad is still well-calibrated.

## [live-trading][backtest] Resolved 2026-07-21 — SOXL's watchlist-65 node stays TrailingBoth
Real comparison (v5, `open_check`, robust alpha): TB (sl2 tp30 trail_buy3.0 h70 w10 z1.0)
1212.1% best alpha, worst_neighbor 153.9; TE (sl2 tp26 trail_sell8.0 h119 w10 z1.0) 947.0%
best alpha, worst_neighbor 28.0. TE's edge was operational ease (no trailing-buy order to
catch manually) at the cost of ~22% alpha and a much thinner cliff margin. **User decision:
keep TB** — that operational-ease motivation is exactly what Part 3 (trailing-buy budget
adherence automation, above) resolves directly, so there's no reason to give up the extra
alpha. No watchlist/`signals_db` change made.

## [live-trading][tax] Active hold, set 2026-07-20 — don't buy GDXU/AGQ in any IRA-type account before their wash-sale clearance dates
Real taxable-brokerage-account losses found while discussing account mapping for
watchlist 65: GDXU lost in brokerage on 2026-07-06 (clears **2026-08-05**), AGQ lost
in brokerage on 2026-07-07 (clears **2026-08-06**). Per IRS Rev. Rul. 2008-5 (the
same mechanic resolved 2026-07-07 — see `project_wash_sale_holds.md`), buying either
ticker in any IRA-type account (ira/sep/roth, or the new limited-margin IRA) before
its clearance date permanently disallows that brokerage loss. Not the same situation
as the 2026-07-07 resolution (that was an IRA-internal loss, a non-issue) — this is a
real taxable-account loss, the actually-dangerous direction. Both tickers are fine to
trade in the brokerage account itself, or in IRA accounts after clearance. **Action
needed**: don't execute or recommend a GDXU/AGQ buy into any IRA-type account until
the respective date above.

## [live-trading][security] Resolved 2026-07-22 — `same_day_block` is now account-type-aware
Full original framing preserved below the line. Built this session: `schwab_safety.AccountLimits`
gained an `account_type` field (`'cash'` or `'margin'` — covers both regular margin and IRA
limited margin identically, since both lack the T+1 cash-settlement restriction). `ACCOUNTS`:
brokerage is `'margin'` (ordinary taxable brokerage accounts have it by default); sep/roth/ira are
`'cash'`, confirmed by the user. `check_order`'s same-day-rebuy check now only fires when
`limits.account_type == 'cash'`. 1 new test (`test_same_day_rebuy_not_blocked_in_margin_account`).
**Deliberately NOT built, considered and rejected**: a blanket "real orders only allowed in a
confirmed margin account" gate — the user's actual account model is one account per ticker,
growing over time as capital/liquidity needs it (fund a new trade in a new account rather than
liquidating an existing one), so a hard margin-only gate would have locked automation out of
every existing cash account (brokerage/sep/roth/ira all already hold real, non-algorithmic
positions). `account_type` is metadata for `same_day_block` specifically, not a live/no-live gate.
The new (5th) limited-margin IRA funded 2026-07-22 isn't in `ACCOUNTS` yet — still blocked on API
token scope + compliance trading permission, see the `project_new_ira_account_status` memory.
Full suite: 177 passed.

### Original framing (2026-07-20), before resolution
Found while reinterpreting the watchlist-65 checklist's check 9 (same-day-block
sensitivity). The guardrail (`signals_db.closed_today`, enforced in
`schwab_safety.py`'s BUY-side safety check) refuses a same-day re-buy on any
ticker unconditionally — but its own docstring frames the risk as cash-account
settlement-specific ("IRA/SEP cash accounts can't reuse that capital until T+1
settlement"). User confirmed 2026-07-20: watchlist 65's tickers will trade in
**limited margin accounts**, not IRA/SEP/Roth — limited margin doesn't have this
same-day-reuse settlement restriction, so the block as currently written would
reject valid, safe trades once live. Not urgent — `schwab_client`/`schwab_safety`
aren't wired into `active_signals.py`'s real loop yet, so nothing is actually
enforced live right now. Depends loosely on the still-undecided one-account-per-ticker
plan (`docs/deep_backlog.md`) for how account type gets tracked per ticker.

## [backtest] Resolved 2026-07-20 — last-window MOC vs trailing-buy; MOC does not win, see `docs/deep_backlog.md`/`docs/research_log.md`

## [backtest] Research idea, not started, 2026-07-20 — is overnight gap frequency/magnitude asymmetric (up-gap vs down-gap)?
Raised while confirming the gap-through-trigger fix is symmetric across the
trailing-buy trigger (adverse = up-gap) and the trailing-stop/SL trigger
(adverse = down-gap) -- it is (both fixed, 2026-07-19 entry-side / 2026-07-20
exit-side). Open question, not yet checked: do these leveraged/inverse ETFs
actually gap up vs. down with different frequency or magnitude? If so, the
trailing-buy side and trailing-stop side wouldn't be equally exposed to the
fill-optimism risk in practice even though the kernel treats them
symmetrically. **User: this should be checked across all three fill
resolutions (possible/pessimistic/certain), not just `possible` alone** --
same robust-alpha convention as everywhere else in the sweep engine, since
possible/pessimistic/certain can genuinely differ on when trailing activates
and where the peak sits, not just on the final number. Framed as
research/backlog, not scheduled.

## [backtest] Resolved 2026-07-20 — full 18-ticker v5 resweep completed; see `docs/deep_backlog.md` and `docs/research_log.md`
One open sub-item carried forward, not yet done: the observed sweep-throughput question (~125-335 nodes/sec, unclear if a real regression from the exit-gap kernel edit or something else) was never benchmarked before the original interruption and still isn't — low priority, revisit if throughput looks slow again.

## [live-trading][backtest] Resolved 2026-07-20 — watchlist 65 candidate testing complete, found+fixed 2 real bugs, see `docs/deep_backlog.md`/`docs/research_log.md`

## [live-trading] Resolved 2026-07-19 — `AUTOMATION_ENABLED_TICKERS` moved to `.env`, widened to all 18 v4 tickers; EDC's v3.27 node removed
Full writeup in `docs/design.md` (Layer 3). Short version: `AUTOMATION_ENABLED_TICKERS`
moved from a hardcoded Python set in `schwab_safety.py` to `SCHWAB_AUTOMATION_TICKERS` in
`.env` (gitignored, deployment-specific — same reasoning as `SCHWAB_ACCOUNT_<NAME>`), with
`schwab_safety.sync_automation_scope()` (called once at `run_loop` startup) logging any
scope change to `watch_list_audit` as the replacement for the git-history audit trail that
move gave up. Widened from GDXD-only to all 18 v4 research-mode tickers (paper-trading
only, no accounts/dry_run/live involved), each set to `starting_notional=5000`. EDC's
v3.27 node was removed from `watch_list` entirely (not just excluded from the widened
scope) — its real open position (423sh @ $73.57) is unaffected (SELL alerts key off
`open_positions`, not `watch_list`), user is tracking its unwind manually going forward.
Verified: full `pytest tests/` (92 passed), `verify_trailing_buy_resolution.py`/
`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` (required since `active_signals.py`
changed) — both clean, only already-documented drift.
**Two real gaps found while doing this, not yet fixed** (their own item below):
paper-trading's dedup is ticker-only (not `(ticker, window)`-aware like the real
`open_position()`), and SELL-side automation (`_attempt_automated_sell`) is gated by
ticker membership only, not by the position's node `mode` — unlike the BUY side.

## [live-trading][security] Resolved 2026-07-21 — SELL-side automated-order attempt is now mode-gated, not just ticker-gated
Full original context in `docs/deep_backlog.md`. `_attempt_automated_sell` (`signals_notify.py`)
was gated only by `ticker in AUTOMATION_ENABLED_TICKERS`, not by the position's node `mode` —
unlike the BUY side (`_scan_buy_signals` requires `mode=='live'`), per `automation_principles.md`
#7 (new automation surfaces inherit the full existing gating, not a subset). Fixed: looks up
the position's own `(ticker, window)` node from `db.get_watchlist()` and falls back to manual
(returns `False`) unless a matching node exists with `mode=='live'` — also fail-closed if no
matching node is found at all (e.g. the node was later removed, mirroring EDC's 2026-07-19
removal). Two new tests in `tests/test_schwab_automation.py`
(`test_automated_sell_falls_back_when_node_mode_not_live`,
`test_automated_sell_falls_back_when_no_matching_node`).

## [live-trading] Low priority, 2026-07-19 — paper-trading dedup is ticker-only, not `(ticker, window)`-aware
Full detail in `docs/deep_backlog.md`. Short version: `paper_trading.start_paper_buy`'s
dedup is single-ticker, unlike the real `open_position()`'s `(ticker, window)` dedup. Fine
today since every automation-enabled ticker has one node; not scoped.

Curated, current subset of `docs/deep_backlog.md` — read in full at session start (`go`). Full detail for every item lives in `deep_backlog.md`; this is just the active/relevant pointer list. Periodically re-triage. Resolved/dead items are pruned here once closed out — see git history or `docs/conversation_summary.md` if the old writeup is ever needed.

## [live-trading] Resolved 2026-07-18 — GDXD paper-trading layer built, `add_node` fixed_sl bug fixed
Full writeup in `docs/deep_backlog.md`. Short version: `paper_trading.py` (new module) gives
`schwab_safety.AUTOMATION_ENABLED_TICKERS` tickers (currently `{"GDXD"}`) a real paper-trading
simulation while they stay `research` mode — `active_signals._scan_buy_signals` routes their
BUY signals to `paper_trading.start_paper_buy` instead of the silent research print;
`update_paper_buys()` (every poll, unconditional) tracks a continuous running-low and
simulates the trailing-buy bounce-fill; `check_paper_sells()` runs the real
`signals_compute.check_sell_condition` exit state machine against the simulated position.
Writes to new `paper_positions`/`paper_trade_log`/`paper_pending_buys` tables — never the real
`open_positions`/`trade_log`/`pending_buys` — and never calls `schwab_client`/`schwab_safety`
at all (pure simulation, independent of `dry_run`, which alone still produces zero fill/P&L).
**Deliberate deviation from the original framing** (routing through the real
`_attempt_automated_buy`/`_attempt_automated_sell` path): investigating that path found it
would write real `pending_buys` rows nothing ever marks Filled, causing indefinite
`check_buy_reminders` nagging for a ticker that was never actually live — the fully separate
simulation avoids that. `scripts/paper_trading_status.py` shows current state. **Known
limitation**: fills sampled at `POLL_SECS` cadence, not tick-perfect against a real broker's
continuously-live `TRAILING_STOP`. Verified: full `pytest tests/` suite (92 passed, was 86 —
6 new tests in `tests/test_paper_trading.py`), confirmed real `open_positions`/`trade_log`/
`pending_buys` row counts unchanged after running the new code path.
Separately, `signals_db.add_node` gained a `fixed_sl_override=None` param (falls back to old
config-read behavior when omitted) — fixes the bug where it silently pulled `config.json`'s
stale global default instead of the real per-node SL for any future SL-swept promotion.

## ✅ Resolved 2026-07-18 — v4 nodes (SL=1%) promoted into the real `watch_list`, replacing v3.x SL=15% params; see `docs/deep_backlog.md`

## [backtest] Resolved 2026-07-18 — 5 tickers with a negative walk-forward fold (DPST, NUGT, RETL, UDOW, UVIX) sent to research, no per-ticker investigation
Follow-up from the walk-forward check (`docs/research_log.md` 2026-07-18 entry,
`logs/walk_forward_check.csv`). Each had exactly one mild negative-alpha fold out of 5
(DPST -11.9%, NUGT -7.2%, UDOW -3.6%, UVIX -4.0%, RETL -8.4%). **User decision: skip the
per-ticker regime investigation and just keep all 5 in research mode** rather than promoting
any of them further. NUGT/AGQ/GDXU/TQQQ/YANG were already `research`; RETL/UDOW/UVIX were
never in the live `watch_list` to begin with (screened candidates only). **DPST flipped
`live`→`research` this session** (`signals_db.set_node_mode(53, 'research')`, watchlist_id=9)
— it had been the leading candidate for the first taxable-brokerage-account ticker, but this
walk-forward flag plus its already-known thin trade count (chaos-monkey item, 2026-07-17)
was enough to deprioritize it without further digging. No live capital was at risk (DPST had
no open position at the time of the mode change).

## [live-trading][security] Phase 4 (deferred to cloud-infrastructure planning), 2026-07-18 — move order-placement/mutating Schwab calls behind a separate proxy this session can't write to
Full detail in `docs/deep_backlog.md`. Short version: API-proxy pattern for safety
boundary + credential isolation on Schwab order-placement calls, not scoped. Deferred
until cloud infrastructure is actually being considered (proxy is naturally a
separately-hosted service).

## ✅ Superseded 2026-07-20 — watchlist 65 ("Live v5") is now active, not watchlist 57; see `docs/deep_backlog.md`

### Original entry (2026-07-18), preserved for the watchlist-57-build history below
new active watchlist (id=57, "Live v4") holds only GDXD+EDC; 10 v3.x tickers archived in watchlist 9, not polled
User decided v4's `trail_buy_pct` (much tighter than v3.x) is too tight to catch manually —
manual live trading is paused until the Schwab automation engine actually drives entries.
Correction mid-session: initially just flipped all 12 nodes to `research` mode within
watchlist 9, but the real ask was to stop the daemon polling the 10 stale v3.x tickers
entirely (AGQ, DPST, GDXU, HIBL, KORU, LABU, NUGT, SOXL, TQQQ, YANG — user tracks these in
a separate v3 spreadsheet, so no risk of losing track). **Resolved via watchlist versioning**
(same pattern as the earlier watchlist 7→9 supersession), not row deletion: cloned GDXD (v4)
and EDC (v3.27, has the one open position) into a new watchlist (`signals_db.create_watchlist`
→ id=57, "Live v4"), set it active (`set_active_watchlist`). Watchlist 9 ("Sweep v3 - Full")
is untouched and inactive — all 10 archived nodes' full config is still there, not deleted,
just not polled (`active_signals.py` re-queries `get_watchlist()` — the active one — fresh
every loop iteration, so this took effect without a daemon restart). Can be re-cloned into
the active watchlist later once v4 versions exist for them.
**EDC has one open live position** (423 shares @ $73.57, opened 2026-07-16, v3.27) — stays
open, user will monitor manually. Confirmed `check_sell_condition`/`notify_sell_signal`
(`active_signals.py:263-289`) key off `open_positions`, not `watch_list`/`watchlist_id` at
all, so EDC's SELL alert still fires normally regardless of which watchlist is active.
**"Research" now doubles as "paper trading"**: both GDXD and EDC are `research` mode within
watchlist 57. The plan is to wire `schwab_client`/`schwab_safety` into the daemon's real loop
(still disconnected) and run it `dry_run=True` on these research-mode nodes to validate the
engine catches v4 signals reliably, before flipping any ticker back to real execution —
likely paired with the one-account-per-ticker rollout below so each ticker gets its own
account as it's proven out.
**New: `watch_list_audit` table added** (`signals_db.py`, `ensure_tables`) — append-only log
of every `create_watchlist`/`delete_watchlist`/`set_active_watchlist`/`add_node`/
`remove_node`/`set_node_mode`/`label_node` call, going forward. Built after discovering
`watchlists.id` jumped straight to 57 with zero explanation available (47 prior watchlists
created-then-deleted via the Streamlit UI's Create/Delete buttons, `pages/3_Winners.py`/
`pages/4_Portfolio.py` — legitimate manual usage, not a bug, but genuinely unreconstructable
after the fact since `AUTOINCREMENT` never reuses ids and nothing was logging the *why*).
`signals_db.get_watchlist_audit(limit=200)` reads it back, newest first. Verified via
`pytest tests/ -k watch` (2 passed) plus a live round-trip test.
**New: `watch_list.annotation` column added** (freeform human-readable "why", distinct from
`label` — a short display tag — and from `watch_list_audit` — the mechanical what-changed
log). `signals_db.annotate_node(watch_id, text)` setter, also writes to `watch_list_audit`.
Backfilled on both current watchlist-57 rows: EDC ("carried into Live v4 to keep it
monitored, not yet promoted to v4 params"), GDXD ("v4 pilot... dry_run automation target").
**Done, 2026-07-18**: all 19 tickers from the full walk-forward screen added to watchlist 57
as `research`, not just the 12-ticker watchlist. Each ticker's v4 winning node pulled directly
from `backtest_cache` (`WHERE version='v4' AND stop_loss=1`, ranked by robust alpha —
`MIN(alpha_vs_spy, pessimistic, certain)`, same selection logic as `run_optimization_sweep.
ROBUST_ALPHA_SQL`) and inserted via **direct SQL, not `signals_db.add_node`** — found and
worked around a real bug: `add_node`'s `fixed_sl` computation reads `config.json`'s global
`execution.fixed_stop_loss` (15%) for any `uses_fixed_sl` strategy, ignoring the real
per-node SL value entirely, so the first insertion attempt silently wrote `fixed_sl=15.0`
onto every new row instead of the intended `1.0`. Caught by inspecting the rows after
insert, not by any test — **`add_node` still has this bug for any future v4/SL-swept
promotion**, logged as a new follow-up below. Deleted the 10 wrong rows and re-inserted
correctly via raw SQL (same pattern used earlier for the GDXD/EDC clone).
14 tickers (AGQ, DUST, EDC*, GDXD, GDXU, HIBL, KORU, LABU, NAIL, SOXL, TQQQ, USD, YANG, ZSL —
*EDC kept its existing v3.27 node, not re-promoted to v4) annotated "walk-forward clean, zero
negative folds." 5 tickers (DPST, NUGT, RETL, UDOW, UVIX) annotated with their specific
negative-fold detail, added anyway per user decision but flagged for closer look. All in
`research` mode — no live signals fire, this is the automation-engine dry-run staging set.
Verified: `pytest tests/` full suite (86 passed).

## [backtest] Resolved 2026-07-18 — `signals_db.add_node`'s `fixed_sl` computation ignored the real per-node value for uses_fixed_sl strategies
`add_node` (`signals_db.py`) used to always compute `fixed_sl = _config_fixed_stop_loss()`
(reads `config.json`'s global `execution.fixed_stop_loss`) whenever
`strategies.uses_fixed_sl(strategy)` was true, regardless of what real per-node SL value the
caller actually wanted — no parameter existed to override it. Found 2026-07-18 promoting 19
tickers' v4 (SL=1%) nodes: every row came out with `fixed_sl=15.0` (the stale global default)
instead of the real `1.0`, silently wrong, no error; worked around that session by inserting
via direct SQL instead of `add_node`. **Fixed**: `add_node` gained a
`fixed_sl_override=None` parameter — when set, used instead of `_config_fixed_stop_loss()`.
`None` (the default) preserves old behavior for legacy v3.x callers.

## [live-trading][security] High priority, active focus as of 2026-07-18 — one brokerage account per live ticker, for blast-radius containment against a rogue algorithm
Full detail in `docs/deep_backlog.md`. Short version: split into one brokerage account
per ticker so a rogue order on one ticker structurally can't reach another's capital —
each account's own balance becomes a hard ceiling by construction, making the previously-
flagged "aggregate resting-order cash exposure" gap structurally unnecessary rather than
something to build. **User declared this the next thread of work, 2026-07-18** (after
sending the 5 negative-walk-forward-fold tickers to research instead of investigating
further). `schwab_client.py`'s `NICKNAMES`/`SCHWAB_ACCOUNT_<NAME>` pattern already supports
scaling from 4 accounts to a dozen-plus with just more entries (single shared OAuth login).
**Naming, 2026-07-18**: account nicknames will just be the ticker symbol itself (e.g.
`SOXL`, `KORU`) as a placeholder — not worth bikeshedding before the real design session.
**Also reframed same session**: the account-per-ticker rollout is now tied to the
automation-engine/paper-trading plan (v4's `trail_buy_pct` is too tight to trade manually —
"research" mode tickers are meant to run through the Schwab automation engine in
`dry_run=True` as the paper-trading validation phase, then flip to real execution once
proven, likely per-ticker-account by account as each is set up).
**Status corrected 2026-07-22 (was stale — see `docs/deep_backlog.md`)**: infra is built, not
"not started" — `schwab_safety.ACCOUNTS` holds 4 account slots (brokerage/sep/roth/ira), each
with its own `AccountLimits` (`notional_cap`/`daily_order_cap`/`account_type`);
`watch_list.account` is a real per-node column; `_live_ticker_accounts()` derives the live
ticker→account mapping fresh (currently empty — no ticker is `mode='live'` yet). Rollout is
incremental (a 5th limited-margin IRA funded 2026-07-22, still blocked on API scope +
compliance) — this item stays open for tracking new-account onboarding, not because the
mechanism itself is unbuilt.
**Sequencing confirmed 2026-07-19**: ticker selection for first-funded-account isn't picked
up front — wait for real paper-trading results (once the daemon exercises the widened
`AUTOMATION_ENABLED_TICKERS` set through a live signal window) to inform which tickers earn
an account first, rather than guessing now.

## [live-trading][tax] Deprioritized 2026-07-18, 2026-07-17 — wash-sale/tax analysis needed before promoting any ticker into the taxable brokerage account
Full detail moved to `docs/deep_backlog.md` ("Deprioritized 2026-07-18" entry). Short version:
strategy will generate continuous wash sales in a taxable account, user is fine with the
deferral mechanic in principle (LABU floated as first candidate) but wants a real analysis
(incl. year-end 30-day-straddle mechanic, unverified) before promoting. Pushed behind the
train/test split and v4-promotion work — not blocking anything active right now.

## [live-trading][security] Resolved 2026-07-17 — same-day buy→sell block explored and deliberately NOT built
Full writeup moved to `docs/research_log.md` (2026-07-17 entry). Short version: PDT rule
eliminated by FINRA 2026-06-04, no broker/regulatory reason for a block remains; real cost
of blocking anyway is high (GDXD retains only ~47% of edge if same-day exits are deferred).
Decision: proceed without a block. `schwab_safety.py`'s existing `same_day_block` (blocks
same-day *re-buy*, unrelated direction) is untouched, still enforced live.

## [live-trading][backtest] High priority (raised 2026-07-18) — dividend cash isn't credited into P&L/SL/arm tracking; material for DPST specifically
**2026-07-18: user confirmed this needs to happen** (along with the trailing-buy re-sizing item below) — no longer just a medium-priority idea.
Raised while investigating trailing-buy capital utilization. Checked real dividend history via `yfinance`: SOXL, DPST, EDC, HIBL, KORU, LABU, TQQQ, NUGT, YANG all pay small quarterly distributions (largest recent: HIBL $1.41/share, DPST $0.671/share — all well under ~2% of share price); AGQ, GDXD, GDXU pay none (commodity-linked structure, no dividend history on file).
**Confirmed via a real fetch (`auto_adjust=False` vs `auto_adjust=True` around DPST's 2026-06-23 ex-div date)**: `data_manager.py`'s `yf.download(auto_adjust=True)` (already relied on for the KORU split fix, session 15) only retroactively rescales *cached historical bars from before* an ex-div date (DPST 6/18 close: $122.94 raw → $122.29 once fetched after 6/23) — it does **not** touch live current price or a position's recorded `entry_price` in `open_positions`, which is correct: unlike a split, a dividend doesn't change what you actually paid, so `entry_price` should stay exactly as recorded. **No data-corruption bug found** — grepped the whole codebase for "dividend": zero hits, confirming there's also no dividend-crediting logic anywhere.
**The real gap**: the underlying market price genuinely drops by ~the dividend amount on the ex-date (true price action) — but the strategy only tracks price return (`current_price` vs `entry_price`), never adding back the real dividend cash actually received. So a position held across an ex-div date shows a P&L understated by roughly the dividend %, even though the account is economically whole (cash landed in settled cash). Same gap applies to SL/arm comparisons (`check_sell_condition`), which use the same unadjusted `entry_price` vs. live-price comparison.
**Material for DPST specifically**: last dividend was 0.51% of share price against `arm_sell_pct=1.0%` (over half its entire arm threshold) — a position near its arm trigger right around an ex-div date could show materially less gain than its true total return, delaying (or in a rare edge case, falsely triggering) arm/SL timing by a meaningful fraction of the threshold. HIBL/LABU (bigger $ dividends but wider 2%/21% arm thresholds) are less exposed proportionally; AGQ/GDXD/GDXU unaffected (no dividends).
**Action needed**: not scoped. Possible shape: track ex-div dates/amounts per held ticker (`yf.Ticker(t).dividends`) and add the per-share amount back into the live P&L/SL/arm comparison for any position that was open through the ex-date — needs design discussion on where this plugs into `check_sell_condition` without disturbing the existing corporate-action (split) freeze logic it sits next to.

## [backtest][live-trading] High priority, found+partially fixed 2026-07-19 — gap-through-trigger fill optimism: neither the backtest kernel nor live sizing ever modeled overnight gaps past trail_buy_pct
**Live-side design finalized 2026-07-21**: the Part 3 live infra referenced at the
bottom of this item is now fully designed — see the consolidated Part 3 entry at the
top of this file. Kernel fix (Part 1, below) remains resolved/unaffected.
Found while scoping the trailing-buy re-sizing item below. Empirically checked overnight-gap frequency (`.venv/bin/python scripts/sim_gap_policy.py` output, or the one-off `overnight_gap_check.py` scratch script) against every active v4 watchlist ticker's real `trail_buy_pct`: **a next-day gap exceeding the trigger happens on 19-44% of all trading days**, most tickers 30-40% — not a rare tail event, routine. Two independent places assumed this never happens:
1. **Backtest kernel** (`backtester.py::_simulate_trail_both`/`_simulate_trail_buy`): every trailing-buy fill used the theoretical `running_low × (1 + trail_buy_pct)` trigger price even when the bar's own `Open` had already proven it was blown through — silently overstating every v4 node's on-file return, a distinct fill-optimism source from the already-fixed Low/High-ordering one (closer in spirit to the deferred-SL gap bug fixed 2026-07-17, but on the entry side). **Fixed 2026-07-19**: all three resolutions now fill at the real `Open` once it's proven to have crossed the trigger, before falling through to the existing Low/High logic; `_simulate_trail_buy` gained a new `opens` parameter it previously lacked. Verified via a new synthetic-gap unit test (confirmed to fail pre-fix at the stale price, pass post-fix at the real Open) and byte-identical parity between the fixed kernel and the fixed `export_trades.simulate_trail_both_annotated` mirror on real SOXL/KORU/AGQ data. Full `pytest tests/` (94 passed) + required regression scripts (`verify_trailing_buy_resolution.py`/`verify_trailing_sell_resolution.py --tickers AGQ,SOXL`) both clean, only already-documented drift.
2. **Live worst-case sizing** (`signals_blocks.py`, 2026-07-17 fix): sizes off `signal_price × (1 + trail_buy_pct)`, which only bounds the fill under continuous intraday movement — a real `TRAILING_STOP` order left resting overnight becomes a market order the instant it triggers, so a gap can blow past that ceiling. No `TRAILING_STOP_LIMIT` order type exists at Schwab to cap this at the broker level (confirmed). **Not yet fixed** — this is Part 3 of the plan below, not yet built.
**Not yet done**: resweep of the v4 campaigns (needed since the kernel fix changes `possible`/`pessimistic`/`certain`/robust-alpha for every trailing-buy/-both node — same treatment as the original fill-optimism backfill, via `scripts/run_v4_backfill_sweep.sh`/`run_backfill_queue.sh`).
**Policy simulation built+run 2026-07-19**: `scripts/sim_gap_policy.py` + `export_trades.simulate_trail_both_gap_policy` (see `CLAUDE.md` Key Files) — decided, empirically, whether the live fix should always resize-and-enter on a gap or skip the trade above some gap-size threshold, matching the `sim_chaos_monkey.py` pattern rather than guessing. Result across all 18 v4 tickers (`output/gap_policy_summary.csv`): gap-through trades have consistently low win rates (8.5%-41.8%, most 10-30%, confirming they're genuinely worse setups) but skipping them moves total compounded return only modestly with no consistent direction per ticker — no threshold shows a clear universal edge, no ticker shows a dramatic blowup avoided by skipping. Leaning toward "always resize and enter" (simplest, matches the corrected kernel default) pending final confirmation.
**Action needed**: (1) resweep v4 campaigns with the fixed kernel; (2) build Part 3 (live infra: `schwab_client.cancel_order`, order-id capture, `pending_buys.order_id`, `signals_notify.check_gap_risk()`, a daily pre-open scheduled window in `active_signals.py`) — folds together with the trailing-buy re-sizing item's top-up design below, since both need the same cancel/resize plumbing. Full plan: `/home/pkim/.claude/plans/imperative-noodling-dream.md`.

## [live-trading] High priority, confirmed 2026-07-18 — trailing-buy order needs re-sizing as the trigger price moves, to actually use all budgeted capital
**Superseded 2026-07-21**: design finalized, folded into the consolidated Part 3 entry
at the top of this file (padded sizing + overnight gap guard + post-fill top-up) —
see that entry for the current plan, this one kept for historical context only.
**2026-07-18: user confirmed this needs to happen** (along with the dividend-tracking item above).
Raised by the user, separate from today's schwab_client wiring work. `signals_blocks._build_buy_blocks`/`signals_helpers.buy_order_sizing` size a trailing-buy order's *quantity* once, at signal time, off the worst-case bounce-trigger price (`price × (1 + trail_buy_pct/100)`, fixed 2026-07-17 to guarantee the order never costs more than `target_notional`). But a real Schwab `TRAILING_STOP` order's linked trigger price keeps moving as the broker's own running-low updates after the order is placed — if the real running low falls further before the bounce (the common case, same asymmetry already documented in the sizing-formula item below), the order fills at a lower price than the quantity was sized for, and the fixed share count now under-deploys the budgeted capital (real dollars left idle) rather than the over-deploy risk the worst-case formula was built to prevent.
**Confirmed real order type, 2026-07-17**: `schwab_client.place_trailing_buy`/`place_trailing_sell` build a real `TRAILING_STOP` order (`OrderType.TRAILING_STOP`, `schwab_client.py:124`) — not a market order. `place_equity_buy`/`place_equity_sell` (market) exist in the same file but nothing calls them for GDXD (`TrailingBothZScoreBreakout` always routes through the trailing functions), so the under-deployment gap described here is specifically about the trailing order's fixed quantity vs. its live-moving broker-side trigger.
**Second, smaller contributor, same direction**: `signals_helpers.buy_order_sizing` truncates to a whole share via `int(target_notional // (price * (1 + trail_buy_pct/100)))` — even at the exact worst-case fill price, integer rounding alone leaves up to ~1 share's worth of budgeted cash unused every trade. Worth folding into whichever fix below is chosen (e.g. a top-up order could also mop up the rounding remainder, not just the trigger-price gap).
**Proposed shape, decided 2026-07-19**: (b), a top-up order once the real fill price is known, chosen over (a) cancel/replace — reasoning: (a) would require polling/approximating the broker's own hidden running-low state (the same blind modeling that caused the gap-through-trigger bug below), plus new cancel/replace machinery and a cancel-vs-fill race risk; (b) only adds a plain market order on an existing, already-tested code path (`schwab_client.place_equity_buy`). Chosen despite (b)'s "no averaging" tradeoff (a top-up at a different price than the original fill needs a shares-weighted blended `entry_price`) since that's a contained, one-time write, not a standing failure surface.
**Real complication found while designing this, 2026-07-19**: chasing the top-up naively (buying at whatever price prevails when the fill is confirmed) risks buying into a further price run since the top-up has no worst-case ceiling of its own — this led to investigating how often price gaps at all, which surfaced a much bigger, separate bug (see the new "gap-through-trigger" item below): a real overnight gap past `trail_buy_pct` happens on 19-44% of trading days across the watchlist, not a rare edge case, and neither the backtest kernel nor the live worst-case sizing formula ever modeled it. That item is now the higher-priority blocker — **this item's live order-placement infra (Part 3: `schwab_client.cancel_order`, `pending_buys.order_id`, `signals_notify.check_gap_risk()`, a daily pre-open scheduled window) folds into and should be built alongside the gap-through-trigger fix's live-side piece**, not as a separate follow-on. See that item for current status.
**Action needed**: build Part 3 (live order-placement infra) — not started. Full plan in `/home/pkim/.claude/plans/imperative-noodling-dream.md`.

## [backtest][live-trading] High priority, 2026-07-16 — trailing-buy sizing formula spends money that isn't guaranteed to be there; backtest's compounding formula assumes it too
Found while manually reconstructing KORU's real live trailing-buy fills: `signals_blocks.py:97-98` computes `shares = target_notional // price`, where `price` is the *signal-time* price — but the order is a real trailing buy, which only fills once price bounces `trail_buy_pct`% above a running low that can only fall further after that. So the real fill price can be higher OR lower than the price used to size the order, and the resulting real dollar cost (`shares × actual_fill_price`) can exceed `target_notional` — i.e. the order can need more cash than was actually budgeted for. Real principle, stated by the user: **we can't spend money we don't have.**
**Backtest side**: the exact same unrealistic assumption is baked into every number in `backtest_cache` — `run_optimization_sweep.py:382`, `compounded = ((df_tr['Return'] + 1).prod() - 1) * 100`, assumes every trade can deploy an *exact* dollar notional with no share-count rounding and no sizing-price/fill-price mismatch. Reconstructed KORU's real 31-trade live-node history three ways: (1) naive compounding as currently computed — 17.4x; (2) capped at affordable shares when the real fill price makes the full requested quantity too expensive, but leaving cash idle when the fill is cheaper than expected — 16.4x; (3) correct version, sized directly off the real (already-known-after-the-fact) fill price so every trade is always fully deployed with zero shortfall and near-zero idle cash — 17.0x, the honest number. Same check on AGQ (37 trades, 5% trail_buy_pct, the second-highest on the watchlist after KORU's 12%): 20.1x (fixed) vs 19.8x (naive) — both real, measured examples show only a few percent divergence in practice (overshoot and undershoot trades roughly offset each other over enough trades), not the runaway compounding a naive "always overshoots by the full trail_buy_pct%" worst-case would suggest — but this is only two tickers' worth of real data, not proof it always washes out elsewhere.
**Scope not started**: fixing `_summarize_trades`'s compounding formula to properly walk trades with real share-count rounding (shares = floor(equity/entry_price), fully deployed, no shortfall) would change what `strategy_return`/`alpha_vs_spy` mean for every future run — making all of `backtest_cache`'s existing v3.x/v4 history incomparable to anything computed after the fix, a real backfill decision, not a quick patch. **Live side fix, principle confirmed but not implemented**: since `entry_price` genuinely isn't knowable at order-placement time (broker determines the real fill), the live formula can't just switch to `entry_price` the way the backtest fix can — needs to size *conservatively* for the worst case instead: `shares = target_notional // (price × (1 + trail_buy_pct))`, guaranteeing the worst-case fill (no further drop in running_low) never costs more than `target_notional`. Not yet implemented in `signals_blocks.py`.
**Action plan — all 5 items done, 2026-07-17:**
1. **Done.** Live sizing formula fixed: `signals_blocks.py:96-107` now sizes trailing-buy orders as `shares = target_notional // (price × (1 + trail_buy_pct/100))` when `db._is_trailing_buy(node)`, guaranteeing the worst-case fill never exceeds `target_notional`. Non-trailing-buy strategies unchanged. Verified with a standalone test (`trail_buy_pct=12%`, $50k target, $100 price: old formula → 500 shares/$56k worst-case cost; new formula → 446 shares/$49,952 worst-case cost).
2. **Done.** GDXD ran through the full checklist (see the entry above) — real flags found (macro trend, fill-drift ratio 2.51, late-window win-rate decline, and critically **check 9: only 7.2% of robust alpha survives the real same-day-block constraint**) but accepted given the deliberately small $5k pilot size. Checklist itself extended with checks 9 (same-day-block sensitivity) and 10 (same-day-collision 70/30 stability split) in `docs/watchlist_candidate_checklist.md`.
   **Blocker found and fixed**: GDXD's only campaigns on file used `entry_timing=open_check`, which had no live-actionable equivalent — the earlier-poll-window design from the `entry_timing=open_check` backlog item (below) was actually built this session: `active_signals._OPEN_CHECK_WINDOWS = [(9,31,9,40),(14,31,14,40)]`, `signals_db.watch_list.entry_timing` column (default `'close'`), `_scan_buy_signals()` shared helper. Verified: an open_check node fires once from the early window and doesn't double-fire at the later close window (existing `buy_alerted` dedup handles it for free); a close-only node is untouched by the early window.
3. **Done.** `schwab_safety.AUTOMATION_ENABLED_TICKERS` swapped `{"KORU"}` → `{"GDXD"}` (KORU was flat, no handoff risk either way). `schwab_safety._SIGNAL_WINDOWS` widened to include `_OPEN_CHECK_WINDOWS` so the automated BUY gate doesn't reject GDXD's real open-check-window orders. GDXD inserted into the real live `watch_list` (id=56): `mode='live'`, `account='ira'`, `entry_timing='open_check'`, `fixed_sl=1%` (a deliberate, user-confirmed first-of-its-kind divergence from the watchlist's flat 15% — every other ticker still runs the unswept global default, a still-open question), `trail_buy_pct=1%`, `arm_sell_pct=7%`, `trail_sell_pct=1%`, `max_hold_hours=7`, `window=20`. `dry_run=True` left untouched on the `ira` account — nothing places a real order yet, and separately, `schwab_client`/`schwab_safety` are still not called anywhere in `active_signals.py`'s actual loop (that wiring itself remains unbuilt).
   **Also added, beyond the original plan**: a per-ticker automation pause/resume Slack toggle (`schwab_safety.ticker_automation_enabled/pause_ticker_automation/resume_ticker_automation`, persisted to `cache/live/schwab_ticker_automation.json`, mirrors the existing global kill-switch pattern), with buttons on the reference report shown only for tickers in `AUTOMATION_ENABLED_TICKERS`. Requested mid-session once GDXD went live — lets automation be paused per-ticker from a phone without a code change.
4. **Done.** `_last_sale_recovery(ticker, starting_notional)` now requires the caller to pass `starting_notional` explicitly (raises `ValueError` if both trade history and `starting_notional` are missing) — no more hidden flat-$50k fallback. New `watch_list.starting_notional` column (`REAL NOT NULL DEFAULT 50000`, backfilled to 50000 for every existing row to preserve current behavior); GDXD's row set to `5000`. All 6 call sites (`signals_blocks.py`, `signals_notify.py` x2, `signals_handlers.py` x2) updated to pass `node.get('starting_notional')`; the two Slack-value round-tripped `node_fields` tuples (`signals_blocks.py`, `signals_notify.py`) extended to include it so it survives the button-click round trip.
5. **Done, with a real and more serious finding than the original hypothesis.** Live-tested directly against the real IRA account (real settled cash $271,662.09, confirmed via `client.get_account()` before testing — already well above both `HARD_ORDER_CEILING=$100k` and the IRA's own `$75k` cap, so no order within our own limits could ever test "beyond available cash" on its own). User placed a real $200k `TRAILING_STOP` buy order directly in Schwab's UI: **buying power was unaffected** — not reduced at all by the resting order. Re-tested with a limit order instead of a trailing-stop: **same result, buying power unaffected**. **Conclusion: Schwab does not reserve/check buying power against either order type at placement time** — whatever check exists (if any) only happens at actual fill/execution, not when a resting order is accepted. This is the opposite of the original hypothesis ("a cash account may already provide a hard backstop") — **no such backstop exists at placement time**. Both test orders were cancelled by the user afterward. **Real implication**: `schwab_safety`'s own per-order notional caps are the *only* protection that exists today — nothing on Schwab's side stops multiple legitimate-looking resting orders (e.g. several tickers' trailing buys, each individually under cap) from collectively committing far more than real available cash. Not an immediate risk at the current single-ticker $5k GDXD scope, but a real gap to close (e.g. an aggregate-resting-orders check, not just a per-order one) before meaningfully widening `AUTOMATION_ENABLED_TICKERS`. What happens when an order actually fires past available cash is still unknown — deliberately not tested (would require an uncontrolled real execution, not a placement-time check).

## [backtest] Resolved 2026-07-22 — split-guard/`auto_adjust` reconciliation closed; GDXD numbers verified clean
Full original writeup preserved below the line. **Reconciled 2026-07-22**: `auto_adjust=True`
only adjusts the window being fetched *right now* for corporate actions known as of today — it
doesn't reach back and re-adjust rows already sitting in the local cache from a prior fetch. A
new split therefore makes the next incremental delta fetch land on the new scale while old cached
rows stay on the prior scale, which is exactly the discontinuity the split-guard detects and
rescales the whole file to fix. Guard is real work, not dead code; auto_adjust and the guard are
two different layers (per-fetch adjustment vs. cross-fetch reconciliation). Since the guard's
rescale is one multiplicative factor across the whole series, downstream %-based signals (z-score,
SL/TP/arm, returns) are scale-invariant to it, so past `backtest_cache` numbers should stay
reproducible by re-running the same code. Full writeup: `docs/research_log.md`'s 2026-07-22 entry.
**Real, distinct gap surfaced in the same discussion, backlogged separately below (not resolved)**:
no archived/immutable snapshot of the exact cache-file bytes used for any past backtest exists —
literal byte-for-byte forensic reproducibility isn't possible, only "same code produces the same
number." See the new data-traceability item below.

### Original framing (2026-07-16), before reconciliation
While reviewing a v4 sweep summary CSV, GDXD (non-watchlist, cleared the Step 4 >=300%-v3.x-alpha screen) showed implausible numbers (best_certain up to ~91,000% alpha on some campaigns). Its local `cache/research/GDXD_1h.csv` price fell ~200x over 2.5 years ($9990 in 2023 -> ~$51 now), and `yf.Ticker('GDXD').splits` confirms 3 real reverse splits in that window (2024-04-29 0.10, 2025-10-22 0.05, 2026-02-09 0.10 — cumulative ~1-for-2000). First hypothesis was the KORU-style bug (splits never retroactively applied to local cache, corrupting any trade whose entry/exit straddles a split date) — **checked and disproven**: local Close prices flow smoothly through all 3 split dates with no discontinuity (e.g. 2024-04-29: $4862->$4842->$5345, no ~10x jump). Root cause of the smooth data: `yf.download()` defaults to `auto_adjust=True` (confirmed via `inspect.signature`), and neither call site in `data_manager.py:47,94` overrides it — so every fetch (initial 730-day pull and every incremental delta) already comes back split-adjusted and continuous. **This means the comment at `data_manager.py:113-115` ("yfinance's hourly interval does NOT retroactively split-adjust historical bars") is wrong or stale**, and the whole `detect_price_discontinuity` split-guard machinery (`signals_helpers.py`, wired into `data_manager.py`'s merge step, `compute_buy_signal`, and `check_sell_condition`) may be solving a problem that doesn't actually exist under current yfinance behavior — yet it was empirically triggered for real during the KORU incident (2026-07-15). Needs reconciling: was KORU's raw discontinuity coming from some other path (e.g. `fast_info`/live 1-min tick fetch, which is a separate code path from this `yf.download()` history call), not this one? If so the guard may be correctly placed for that path but the comment/reasoning documenting *why* is wrong. **Action needed**: audit why `auto_adjust=True` didn't prevent the KORU incident, and whether the split-guard is still doing real work or is now dead code for the `data_manager.py` merge path specifically.
**GDXD trade-level check done, 2026-07-16 — verified clean, no bug.** Called `backtester.run_backtest_v110` directly (not a reimplementation) on GDXD's winning node (window=10, z=1.0, tb=1%, arm=7%, fixed_sl=2%, ts=1%, max_hold=7h, open_check). All three resolutions show sane per-trade numbers: 247-279 trades, win rate 37-47%, avg win ~+7.5%, avg loss capped exactly at the -2% stop, max single trade only +35%, no outliers. Expected per-trade log-return compounded over ~250-280 trades works out to almost exactly the reported ~40-400x multiple — the huge alpha is real multiplicative compounding of a genuinely favorable per-trade edge repeated hundreds of times, not corrupted data or a stray outlier trade. Caveat (not GDXD-specific, applies to every ticker's on-file number): this assumes full reinvestment every trade, which is how `strategy_return`/`alpha_vs_spy` are always computed — not how live trading actually works yet ($50k fixed notional, no compounding).

## [backtest] Medium priority, designed-not-built 2026-07-22 — data mutation log (traceability, not full immutability/versioning) for historical price cache rescales
Raised while discussing the split-guard/`auto_adjust` reconciliation above. Real gap: no
archived record of the exact `cache/research/*_1h.csv` data that fed any given past
`backtest_cache` row — the file is mutated in place on every split-guard rescale (ordinary
incremental fetches only append new rows, never mutate old ones). Two shapes discussed: (a) full
immutable/versioned data with each `backtest_cache` row linked to a specific data version — real
byte-for-byte reproducibility, but a meaningfully bigger lift (schema change touching the whole
sweep engine, real storage growth); (b) a lightweight mutation-event log — cheap, no
reproducibility guarantee on its own. **User's decision**: (b) over (a) — traceability (know
when/why/how data changed) matters more than full immutability, since the split-guard's rescale
is scale-invariant to the %-based signals every strategy actually trades on, so re-running the
same code against today's cache *should* reproduce a past alpha number without needing the exact
old bytes.
**Design agreed, not yet built**: new `data_mutation_log` table in `trading_universe.db` (via
`db_cache.py`, the existing shared research DB) — one row per split-guard rescale event:
`ticker`, `factor`, `detected_at`, `overlap_bar_time`, `price_before`/`price_after`, `notes`, plus
a `pre_mutation_snapshot` column holding the full pre-rescale `df_local` (CSV-serialized) so the
actual old data is recoverable, not just the fact that it changed. Cheap since rescale events are
rare (a handful of real splits a year across the whole watchlist), captured only at the moment
`data_manager.py`'s existing rescale branch fires, not on every routine fetch.
**User's priority call**: medium — doesn't block getting trades out, no live urgency, but worth
doing before it's needed rather than after an incident. Full discussion: `docs/research_log.md`'s
2026-07-22 entry.

## [backtest] High priority, 2026-07-16 — execution-adherence robustness ("chaos monkey"), distinct from island/robust-alpha
Raised in discussion: island search (parameter-neighborhood robustness) and the possible/pessimistic/certain robust-alpha ranking (intrabar fill-timing robustness) both assume perfect adherence to the strategy over the full trade sequence. Neither models a human missing, mistiming, or skipping a real signal — the actual live-trading risk right now, since execution is fully manual. Because these are single-position-per-ticker, sequentially compounding strategies, a single missed/late/early trade doesn't just cost that trade's return: it can leave the position flat/in when the backtest assumes the opposite for every subsequent signal, so the divergence propagates forward through the rest of the compounding run rather than averaging out. Not the same axis as the existing train/test split item (regime/noise overfitting) — this is about tolerance to *deviation from the assumed execution path*, not about the historical sample.
**Proposed shape, not yet designed**: a Monte Carlo-style replay that randomly drops/delays a fraction of signals per run and reports the resulting compounded-return distribution vs. the "perfect adherence" number currently on file for each node — would quantify how much of the backtest's headline return is contingent on catching every single signal exactly on time. Prioritized above train/test split for now since it's directly relevant to the manual-execution phase being lived in right now, whereas train/test split matters more once automation reduces execution risk.
**Built and run, 2026-07-17**: `export_trades.simulate_trail_both_chaos`/`_resolve_miss` (pure-Python mirror of `simulate_trail_both_annotated`, verified byte-identical to baseline at `miss_rate=0`) + CLI driver `scripts/sim_chaos_monkey.py`. Entry signals missable only at the two daily signal windows (matching real Slack cadence); exit triggers (SL/trailing-stop/TIME) missable every bar (matching continuous live monitoring); TP-arming never missable (internal state change, not a clickable action). Two modes: `drop` (unbounded per-check miss, opportunity can vanish for good) and `delay` (same coin flip, but capped — forces action after `max_delay_checks-1`=2 consecutive misses of the same still-true condition). Ran all 12 watchlist_id=9 nodes × 2 modes × {1,5,10,20}% miss rates × 1000 trials (225s wall time) — results in `output/chaos_monkey_summary.csv`/`chart.png` (gitignored runtime artifacts, not committed).
**Real finding**: at a 20% miss rate (not an extreme assumption for manual execution), 9/12 tickers lose roughly 15–31% of mean compounded return vs. perfect adherence — worst is **KORU (ratio 0.69, i.e. -31%)**, also EDC/TQQQ/YANG/HIBL/LABU/GDXU/NUGT/GDXD (GDXD is `entry_timing=open_check`, indicative only — mirror is close-only) in the -21% to -24% range at 20% miss. **Two notable exceptions**: SOXL stays flat-to-slightly-positive even at 20% miss (ratio ~1.01–1.05), and **DPST actually improves with higher miss rates** (ratio 1.07–1.08 at 20% miss) — unexplained direction, worth a closer look before trusting it as "safe to miss DPST signals." `drop` vs `delay` modes track each other closely across every ticker — the 3-check delay cap barely changes outcomes vs. unbounded drop, suggesting persistent multi-window setups (where the cap would bind) are rare. Percentile spread (p10-p90) widens sharply with miss rate on most tickers even where the mean holds up (e.g. KORU p10 falls from +1566% at 1% miss to +669% at 20% miss) — tail risk degrades faster than the mean.
**Not yet investigated**: why DPST/SOXL diverge from the rest of the watchlist (real edge that benefits from occasional misses, or a small-sample/trade-count artifact — DPST's baseline is only a handful of trades relative to KORU/HIBL). **Action needed**: dig into the DPST/SOXL divergence before treating "some tickers tolerate misses fine" as a real result rather than noise.

## ✅ Resolved 2026-07-17 — `entry_timing=open_check` live-actionable analog built; see `docs/deep_backlog.md`

## [backtest] Idea, not scoped, 2026-07-15 — eventually delete v3.x once v4 is a confirmed superset
Floated as a future disk-relief idea, explicitly not something to act on yet. Real prerequisites before it's safe, raised and agreed in discussion: (1) v4 needs to actually be run for all 11 live-watchlist tickers, not just SOXL/KORU — the other 9 tickers' live `watch_list` config is currently backed entirely by v3.x data, so deleting v3 broadly now would leave them unsupported; (2) needs an explicit per-ticker check that each ticker's *currently live* winning node (the exact window/arm_sell_pct/trail_buy_pct/trail_sell_pct combo in `watch_list`) actually exists in v4's grid — v4's island search runs independently and isn't guaranteed to land on the identical node v3.x did, so "v4 covers v3" is a plausible but unverified claim, not a confirmed one. `possible`-value byte-matching was only spot-checked on one node per ticker last session (see v4 verification note), not proof of full grid coverage. Don't treat this as decided — revisit once both prerequisites are actually met.

## [backtest] Medium priority, 2026-07-15 (revised) — v4 sweep disk footprint: 11-ticker watchlist is fine, full 53-ticker universe is not
Real disk constraint found this session: WSL's own `df` free-space number (874GB) is misleading — it's against the vhdx's *nominal* max, not real disk. The vhdx is a dynamically-growing file on the Windows C: drive, which has only ~114GB actually free. Added `--max-phase` (see below) cuts each ticker's `backtest_cache` footprint to ~2.1GB (Phase1+2+2.5 only, no Phase3) × 20 campaigns. At that rate: **11-ticker live watchlist ≈ 23GB total — comfortably affordable.** **Full 53-ticker research universe ≈ 112GB — would consume essentially all remaining Windows-side headroom**, confirmed "tight" by the user. Decision: stay scoped to the 11-ticker live watchlist for this v4 SL-sweep (matches `run_v4_backfill_sweep.sh`'s existing documented scope). Extending to the full 53-ticker universe is a real future decision, not a default — would need either more disk (external drive, vhdx relocation — discussed 2026-07-15, not started) or further data reduction (downsample/summarize into `sl_sweep_summary`, drop raw rows post-summary) before attempting it.

## [backtest] Resolved 2026-07-15 — Phase 3 (full mesh) adds no value in every campaign tested so far
Full writeup moved to `docs/research_log.md` (2026-07-15 entry). Short version: Phase 3
won 0/30 tagged SOXL+KORU campaigns; `--max-phase` cap added (default 3, unchanged
behavior) so future runs can skip it. `generation` column added for a similar
not-yet-analyzed question about island-search generation count.

## ✅ Resolved 2026-07-15/19/20 — trailing-buy fill logic kernel-correctness fix, executed in full; see `docs/deep_backlog.md`

## [live-trading] Mostly resolved 2026-07-15 — corporate-action (stock split) defense
KORU did an unannounced ~1-for-20 stock split effective pre-market 2026-07-15 (entry $460.976 → live price ~$23.44). Discovered because the daemon's live 1-min price fetch (`signals_compute.py:115`) picked it up immediately while `yfinance`'s slower endpoints (`fast_info`, hourly `history()`) and the `.splits`/`.actions` metadata all lagged and still showed the pre-split ~$481 level.
**Fixed same day**: `cache/research/KORU_1h.csv` rescaled (pre-split rows ÷20, confirmed exact via `yf.Ticker('KORU').splits`), original backed up to `KORU_1h.csv.pre_split_fix.bak`. `data_manager.fetch_live_data_smart`'s merge step now detects a likely split automatically (`signals_helpers.detect_price_discontinuity` — round-number ratio match against known factors, e.g. 2/3/5/10/20, not a bare magnitude threshold, since a 3x leveraged ETF can plausibly crash >66% in one real extreme day) and rescales the whole local cache before merging, so this specific corruption mode can't silently recur for any ticker.
**Corporate-action detection built and live**: `signals_helpers.detect_price_discontinuity` wired into both `compute_buy_signal` (freezes new-signal generation on a stale `prev_close` — self-heals once the CSV merge-guard above refreshes it) and `check_sell_condition` (freezes SL/arm checks on a stale `entry_price` — the exact false-SL mechanism this KORU incident exposed). The held-position case sends one Slack alert per detection (`cache/live/corporate_action_alerts.json` tracks "already alerted" so it doesn't spam every ~30s poll) with a proposed correction and an "Apply Correction" button — applying it directly fixes `entry_price` via `signals_db.correct_entry_price`, which is what clears the freeze (no separate frozen-flag to toggle).
**Real data corrected this session** (via Schwab's real `get_transactions` API, not guessed ratios): `trade_log` id=9 (KORU's since-closed position) — was showing a bogus -95.75% pnl_pct from comparing pre/post-split prices directly; real fills showed 112 shares → 2240 post-split, entry $23.0488/share, exit $19.5911/share weighted avg → corrected to **-15.00%**, a clean stop-loss exit at the real trigger, not a catastrophic loss. Also found and fixed a 1-share discrepancy in SOXL's `open_positions` (307 recorded vs. 308 real broker fills) the same way.
**Still open**: the research-sweep/live-daemon shared-cache structural gap (`run_optimization_sweep.py`/`data_manager.py` both read/write the same `cache/research/{ticker}_1h.csv`) is mitigated by the new split-guard but not truly decoupled — a live corporate action can still transiently affect an in-flight research run before the next merge cycle rescales it. Giving the research sweep its own snapshot, decoupled from the live feed, is still the real fix, not done.

## [live-trading] Resolved (was stale) 2026-07-15 — HIBL trailing-buy order
`pending_buys` id=4 from 2026-07-14 was already resolved by the time this was checked 2026-07-15 (a fresh position was opened and properly marked Filled — `open_positions` shows 482 shares @ $114.31, verified against real broker fills of 375+107). No action needed; the backlog note was outdated.

## [live-trading] In progress, wiring done 2026-07-17 — Schwab API automation, dry-run cutover not started
First real OAuth login completed against the IRA account (masked-suffix matching in `.env`, never a full account number). Real guardrails built into `schwab_safety.py` beyond the 2026-07-14 skeleton: ticker allowlist + account-consistency (sourced live from `watch_list`), duplicate-order window, same-day-re-buy block (real cash-account good-faith-violation risk — same-day-*sell* deliberately left unblocked, a soft employer preference not a broker rule), a BUY-only signal-window time gate (mirrors `active_signals._in_buy_window`; SELL isn't gated since exit checks run continuously), and `AUTOMATION_ENABLED_TICKERS = {"GDXD"}` (swapped from KORU 2026-07-17, see the GDXD-promotion item above). `schwab_client.py` gained `place_trailing_buy`/`place_trailing_sell` (real broker-native `TRAILING_STOP` orders) and, this session, `get_filled_order` (polls the order book for a real fill). Kill switch persists across daemon restarts with Slack "Stop Engine"/"Start Engine" buttons.
**Done 2026-07-17**: `schwab_client` is now actually wired into `active_signals.py`'s real BUY/SELL loop (`signals_notify.py`) — see `docs/design.md`'s 2026-07-17b addendum for the full breakdown. Automated placement (`notify_buy_signal`/`notify_trailing_activated` call `schwab_client.place_trailing_buy`/`place_trailing_sell` directly for GDXD, falling back to the manual button flow on any `SafetyViolation`) is live and exercisable today since every account is still `dry_run=True`. A separate, opt-in fill-detection capability (`check_auto_fills`, per-ticker toggle `schwab_safety.auto_fill_detection_enabled`, defaults **off**) auto-records a real fill instead of waiting for the Filled/Exited click, once turned on — held back by default since `get_filled_order`'s field parsing hasn't been confirmed against a real fill response yet. 9 new tests in `tests/test_schwab_automation.py`.
**Not done**: real (non-placeholder) notional/daily cap values in `schwab_safety.py:52-55` still need review. Dry-run cutover (flipping `dry_run=False` for `ira`) not started — next step is watching GDXD's automated placement through a real live signal window in dry-run first. Auto-fill-detection not yet enabled for GDXD. Aggregate-across-tickers cash exposure (flagged 2026-07-17, see the GDXD-promotion item above) still unguarded, relevant whenever `AUTOMATION_ENABLED_TICKERS` widens beyond one ticker.
**Blocked 2026-07-18**: dry-run cutover is on hold waiting on a limited margin account (not yet in place) — external dependency, not a code/design gap. Revisit once that account is available.

## [backtest] Medium priority, 2026-07-10 — same-bar arm/take-profit trigger not checked at entry
`simulate_trail_both_annotated`/`_simulate_trail_both` skip arm/TP/SL checks on the entry bar itself (fill sets `in_trade=True` then `continue`s, so trailing-arm logic starts evaluating the *next* bar). Checked across the 6 live tickers whether the entry bar's own High already cleared the arm threshold: SOXL 0/57, EDC 0/32, KORU 0/30, AGQ 2/36, LABU 1/38, but **HIBL 20/54 (37%)** — over a third of HIBL trades could arm the trailing-sell an hour earlier than the backtest credits. Direction of the return bias not yet determined — unprovable from hourly OHLC (same intrabar-order problem as the fill-timing item). Explicit user call: leave the kernel as-is since live trading has the same delayed-until-next-bar behavior — fixing backtest without fixing live would create a live/backtest divergence. Not started.

## Backlogged, 2026-07-13 — fill-price/drift accuracy (scoped down)
Fills often don't land exactly at the expected trigger, and the current typed-price-entry flow (Executed button → manual price entry) doesn't do anything with that drift beyond logging it. **Scoped down 2026-07-13**: user is planning to hook the strategy up to a real broker API eventually, which makes the whole manual-execution phase (and its fill-drift error) temporary — not worth building a dashboard or feeding drift back into the strategy. If built at all, keep it to a simple large-drift warning (e.g. flag any single fill >3% off the expected trigger) — nothing more. See [[project_execution_automation_plan]].

## [backtest] Open question, 2026-07-09 (rescoped 2026-07-13, buffer question resolved 2026-07-14) — fixed_sl=15% itself still needs a real sweep
Originally framed as "is the Schwab catastrophic-stop +1% buffer the right size" — stop order placed at `(stop_loss + 1)%` below trigger (flat +1% buffer, hardcoded, `schwab_sl_pct = node['stop_loss'] + 1`), on the theory that ordinary intraday noise shouldn't trip the broker stop before the "real" Slack SELL signal fires. **Rescoped 2026-07-13**: `fixed_sl=15%` itself was also picked arbitrarily (flat across the whole watchlist, not derived) — tuning the +1% buffer on top of an unvalidated base number didn't make sense in isolation.
**Buffer question resolved 2026-07-14, AGQ live incident**: the premise behind the +1% buffer was wrong. The algo's own SL check (`strategies.py` `TrailingBothZScoreBreakout.check_exit`: `if ctx['low'] <= stop_price: return 'SL'`) already fires on intrabar low breach with no bar-close confirmation — it's mechanically identical to a real broker stop order, not a smoothed/confirmed "real signal" that the buffer needs to protect against noise for. There's no meaningfully separate signal for the buffer to guard. Concretely: AGQ's algo SL fired 2026-07-13 15:29:42 ($63.58 target), user missed/skipped the Slack alert (live trading is fully manual — no broker API automation yet, so the alert is just a notification, not an executed order), and the position rode down toward the broker's actual stop at $62.83 (the padded 16%) with no protection in between. Decision: **set broker stop-loss orders at the algo's exact fixed_sl price going forward (no +1% padding)** — this both matches backtest behavior exactly and removes dependence on catching/acting on the Slack alert in time. `open_positions.broker_stop_price` column added this session to track the real broker order price per position for future reference/reminder-message context.
**Still open**: is 15% `fixed_sl` itself justified? Sweep fixed_sl across a range per ticker, see where compounded return/alpha actually peaks/plateaus vs. the current flat 15%, and whether it should be flat across the watchlist or per-ticker like `trail_buy_pct` already is. **Folded into the v4 kernel-correctness plan 2026-07-14** — see the fill-optimism item above, `/home/pkim/.claude/plans/rustling-bubbling-hennessy.md`. Not started.
**Progress 2026-07-16, SOXL review**: `robust_alpha` (`MIN(possible,pessimistic,certain)`) declines consistently as `stop_loss` loosens across the full 1-30% grid on every ticker checked so far (SOXL/KORU/EDC/GDXU) — SL=1% best (27,673% on SOXL's winning node), SL=30% worst. `STOP_LOSSES`/`DENSE_SLS` capped at 9% in `run_v4_backfill_sweep.sh`/`run_backfill_queue.sh` going forward — no value above 9% has come close to the low-SL region, not worth the compute. **Caveat, not yet resolved**: SOXL's SL=1% winning node's edge is undercut by real fill-drift — `scripts/verify_trailing_buy_resolution.py` shows SOXL's entry fills drift a mean **+1.81%** from the hourly-kernel assumption (intrahour-range/trail_buy_pct ratio 3.47, the worst on the watchlist), nearly double a 1%-wide stop's whole margin — plus the `entry_timing=open_check` live-actionability gap (separate item above) and the general execution-adherence question (chaos-monkey item above). The declining-SL *trend* is trusted; the specific SL=1% magnitude is not, pending those three caveats.

## [backtest] High priority, 2026-07-16 — need a proper delayed-entry simulation for the same-day-re-buy constraint, not just a trade-list filter
While reviewing SOXL SL=1%, tried a quick same-day-re-buy-blocked simulation (mirrors the real `schwab_safety.py` cash-account rule) by post-hoc filtering the already-generated trade list: any entry falling on the same calendar day as a prior exit was simply dropped. Result was a real, large effect (`possible` resolution: 176 trades/27,738% baseline → 105 trades/4,787% blocked, roughly matching current live SL=15%'s number) — but the method is wrong. **Dropping a blocked trade assumes the opportunity just vanishes and capital sits idle; in reality the setup would still be checked (and likely taken) the next eligible day, just later and at a different price.** A dropped-trade filter and a delayed-entry simulation are not the same thing and can diverge a lot, especially for a strategy this trade-frequent.
**What's actually needed**: a bar-by-bar Python-level replay (not achievable as a flag on the existing `_simulate_trail_both` numba kernel, which has no same-day-block state) that walks the two daily signal windows, reuses the strategy's existing entry-check logic, and — while blocked — keeps re-evaluating the entry condition on subsequent days instead of discarding it outright. Entry price/time change under delay, which can cascade (a delayed entry may exit differently, shifting when the *next* block would apply), so this can't be approximated by simple filtering.
**Not started.** Relevant to the same-day-re-buy question (which affects any tight-SL, high-frequency node, not just SOXL SL=1%) and adjacent to the execution-adherence/chaos-monkey item above, though distinct: chaos-monkey models *missed* signals, this models a *known, already-enforced* real constraint that predictably reshapes the trade sequence rather than randomly perturbing it.
**Fixed** (commit `cf96c56`, "Drop stale +1% SL buffer..."): the `+ 1` padding removed from all `schwab_sl_pct` call sites in `signals_notify.py`; `pos.get('broker_stop_price')` now checked first for held positions where a real tracked value exists.

## [backtest] Resolved 2026-07-14 — trailing-buy re-entry timing after a same-day exit
Full writeup moved to `docs/research_log.md` (2026-07-14 entry). Short version: no bug —
same-day re-entry uses the exact same two daily signal windows as any other entry, no
special-casing needed.

## [execution] Research question, 2026-07-15 — when would TWAP/VWAP order execution become worth considering
Raised as a forward-looking research question, not tied to a current incident. All live sizing is currently a single-shot market/trailing order at $50k-ish notional per trade (see [[project_execution_automation_plan]] — full API automation is planned but not yet built). Worth scoping once real API automation is underway: at what position size / ticker liquidity / order-notional-vs-avg-volume ratio does slicing an entry or exit via TWAP or VWAP start to reduce market-impact or slippage cost meaningfully vs. the current single fill? Relevant existing context: `run_optimization_sweep.py:893`/`pages/11_Universe_Scan.py:99` already compute a `max_notional = avg_vol_10d * last_price * 0.01` liquidity cap per ticker (1% of daily dollar volume) for candidate screening — the same ratio is probably the right starting lens for a TWAP/VWAP threshold. Not scoped, no design started — likely blocked on the API automation work landing first, since TWAP/VWAP only matters once orders are placed programmatically rather than manually through Schwab's UI.
**First real evidence, 2026-07-15**: user's HIBL buy filled in ~20 separate pieces at the broker — HIBL's 1% ADV liquidity cap is only ~$90k (EDC ~$113k, similarly thin), so a $50k order is already a meaningful fraction of the day's volume and fragments naturally. SOXL/TQQQ (~$60M-$136M caps) are nowhere close to this by comparison — confirms HIBL/EDC are the tickers where slicing would matter first, if it ever does.

## Live trading — open items
- **Watchlist size** (originally 2026-07-07: "cut watchlist from 6 to 3 if unwieldy" — stale, superseded, watchlist is now 11 tickers). **Rescoped 2026-07-13**: still a real open question, but reframed. Current constraint is deliberately human — user is balancing diversification across accounts and keeping single tickers isolated to cash-vs-margin accounts, which is manual-execution capacity, not a statistical one (the candidate-checklist work already confirmed several of these are "safe islands" on their own merits — this isn't a quality gate, it's a bandwidth one). Explicit ordering from the user: **don't debate watchlist size until (1) the remaining `[backtest]`-tagged items are resolved (9:30-bar re-entry timing, fill-price optimism/drift) and (2) the API automation question is settled**, since API automation is what actually unlocks capacity for more tickers — sizing the watchlist before that is premature.

## Live-trading reliability
- **Default rule**: every action-requiring state change in `active_signals.py` must have a Slack notification — audit this against any new strategy/state added going forward.
- **Race condition: cache CSV reads vs. the daemon's own background refresh** — found 2026-07-13 when "🔄 Resend Report" silently did nothing. Root cause confirmed in `logs/active_signals_verbose.log`: the daemon's main loop refreshes every watchlist/open-position ticker's `cache/{ticker}_1h.csv` roughly every 30s via `fetch_live_data_smart` (non-atomic write-in-place, no lock), and a concurrent Slack button handler's `_load_cache()` read landed mid-write on GDXU's file at the exact same second, hitting it truncated/empty → `pandas.errors.EmptyDataError` → handler crashed, logged, no error surfaced to Slack. Same click again worked (pure timing fluke). Any button click has a small window to hit whichever ticker happens to be mid-refresh. **Fix**: make `data_manager.py`'s cache write atomic (write to temp file, then rename) so readers never see a truncated file. Not started.

## Reference — backup/storage policy (2026-07-07, current; weekly removed 2026-07-18)
- `trading_live.db`: hourly cron backup, keep 30 days (`cache/live_backups/`).
- `trading_universe.db` (research, regenerable): daily rotating single-file backup only (`trading_universe_daily.db.bak`). **Weekly backup permanently removed from crontab 2026-07-18** (had been commented out since 2026-07-15 to free disk headroom for the SL sweep, `trading_universe_weekly.db.bak` 46GB deleted at the time) — user decided not to bring it back rather than just leaving it disabled.

## Deferred, lower priority
- **Train/test (e.g. 70/30) split for backtest date range** — ✅ Resolved 2026-07-18, see the walk-forward item near the top of this file and `docs/research_log.md`'s 2026-07-18 entry.

## [backtest] Idea, not scoped, 2026-07-18 — daily-bar strategy variant, for much longer backtest history / real bear-market regime coverage
Raised while discussing today's walk-forward results: the current strategy's cached
hourly data only goes back ~3 years (2023-07, or ~1 year for GDXU/NUGT/USD) — no real
adverse-regime coverage (no 2020 COVID crash, no 2022 bear market) — which is the deeper
issue behind any overfitting/thin-sample concern, not just something a train/test split on
the existing window can fully address. `yfinance` **daily** bars have no such limit:
SOXL back to 2010-03-11 (inception), AGQ back to 2008-12-04 (inception), SPY back to
1993-01-29 — 10x+ more history and real regime diversity if usable.
**Real constraint, not a quick extension of the existing strategy**: the live strategy's
trailing-buy/trailing-sell mechanics (running-low tracking, arm-then-trail state,
intrabar Low/High checks) genuinely need hourly granularity — a daily-bar version would be
a different strategy variant (new strategy class, new backtest kernel path), not a
longer-history version of the same `TrailingBothZScoreBreakout` node. Wouldn't directly
validate what's currently live; would need its own design/build/validation cycle.
**Action needed**: not scoped, not started — logged as a real idea worth a future focused
session, not urgent.
