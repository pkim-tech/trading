# Design: Tick-to-Trade Latency Fix (active_signals.py / signals_notify.py / signals_compute.py)

**Status: revised 2026-09-17 after a pre-implementation Opus design review found real
problems in the original doc's causal model and in items A, B, C, D, E, F. This
version incorporates every correction. Treat this as the buildable spec for
follow-up work; the pre-review version is superseded.**

## Root cause of the ETHU miss (OUT OF SCOPE for this doc -- separate bug, being fixed in parallel)

The original version of this doc attributed the ETHU miss (2026-09-15, 14:30 BUY,
missed its own 10-minute `_OPEN_CHECK_WINDOWS` by ~90s) purely to the ~75s of
ambient housekeeping latency described below. That housekeeping latency is real
and worth fixing, but it is **not** the actual reason ETHU missed.

The real root cause: `_scan_pinned_entry` (`active_signals.py`) called
`schwab_client.get_session_open_price(ticker)` for both pinned "open" moments in
`_PINNED_OPEN_TIMES` -- which, prior to tonight's fix, was `{(9,30), (14,30)}`.
That function returns `quote["openPrice"]`, Schwab's fixed 9:30 session-open
print. It is only a genuine "current price" proxy *at* 9:30 -- calling it again
at 14:30 evaluated the pinned entry check against a price up to 5 hours stale.
Proven directly: ETHU and SOXL logged identical prices for their 9:30 and 14:30
pinned fetches on 2026-09-15 (`open_price_quality_log`), with a measured mean
1.811% drift vs. the real recorded bar over the prior week.

This is a data-correctness bug, not a latency problem, and is being fixed
separately (another agent, in parallel with this doc's revision) -- as of
tonight `_PINNED_OPEN_TIMES` has already been narrowed to `{(9, 30)}`
(`active_signals.py` ~line 172) and 14:30 now takes the same live-quote
(`get_current_price`) path as 10:30/15:30. **This doc does not propose or take
credit for that fix** -- it is noted here only because it is the real
explanation for why ETHU specifically missed its window, superseding this doc's
original claim. The ~75s latency finding below is independently real and still
worth fixing on its own merits: it widens the miss window for every other
real trade, and it is the reason several other trades that week (DPST, OILU,
ERY, HIBL, DFEN, NUGT, GDXU) landed 30-450s after their own pinned tick,
some inside their 10-minute window only by luck.

**A second, related drift bug to fix as part of any future retiming work (D/E/F
below), not by this doc itself**: `schwab_safety.py` carries its own separate
copy of the signal-window constants (`_SIGNAL_WINDOWS`/`_OPEN_CHECK_WINDOWS`,
~line 75-76), which has already drifted 1 minute from `active_signals.py`'s own
copy (~line 139/148): `active_signals.py`'s `_OPEN_CHECK_WINDOWS` starts at
`:31`, `schwab_safety.py`'s starts at `:30`. This second copy is the constant
that actually rejected ETHU's order via `buy_signal_window_block` -- it is not
a duplicate for redundancy's sake, `schwab_safety.py` has a documented
circular-import reason for keeping its own copy. Any future retiming work must
account for BOTH copies existing (grep both files, don't assume one source of
truth), and collapsing them into one shared source is worth doing as part of
that work rather than continuing to hand-sync two copies.

## Background / problem statement

Investigating the ETHU miss surfaced that the daemon's main loop
(`active_signals.run_loop`) spends a highly variable amount of time -- measured
median ~75s per cycle, some cycles 200s+ -- on ambient housekeeping before it
reaches the time-critical entry-check step for the 4 real reaction moments
(9:30/10:30/14:30/15:30 ET).

Findings traced and measured directly against the real running daemon
tonight (2026-09-17):

1. **A full exit/sell housekeeping sweep over the whole watchlist runs every
   cycle.** Of the ~16 distinct tickers involved in one measured cycle, only 7
   were real live tickers -- the other 9 (and most of the live-7's own node
   rows) were paper/dry_run/research test infrastructure competing for the
   same synchronous slot as real capital.

2. **`_current_price`/`_load_cache` re-reads and re-parses the ENTIRE
   multi-year hourly CSV on every single call, with zero per-cycle caching.**
   Measured ~45-62ms per call. Called independently from 4+ different
   functions with no sharing within a cycle -- one measured cycle made 36
   such calls for only 16 distinct tickers (AGQ alone: 9 times). Confirmed
   real, but a smaller contributor than originally estimated -- see the
   corrected cost model below.

3. **The years of history are never actually needed for a live entry
   decision.** `generate_daily_indicators` computes rolling stats over
   whatever `df_daily` it's handed (years, via `_load_cache`), but
   `compute_buy_signal` only ever reads the LAST row; `window` is at most 20
   (days). This cost exists because `_load_cache`/`generate_daily_indicators`
   are generic functions shared with backtest-replay callers that genuinely
   need full history -- nobody split the live (windowed) case from the replay
   (full history) case. **Correction (see item A below): `signals_compute.py`
   already has a per-process `_indicator_cache` keyed
   `(ticker, strategy, window)` that avoids recomputing this on every poll --
   this finding is real but partially already mitigated, not a raw gap.**

4. **Exit trigger PRICES need zero historical data.** SL/ARM/TRAIL stop
   prices are pure arithmetic off `entry_price` (in `open_positions`) and the
   node's own config (`fixed_sl`/`arm_pct`/`trail_sell_pct`) -- confirmed in
   `strategies.py`'s `check_exit` -- they never depend on the SMA/z-score
   band. **Correction (see item B below): the exit path as a whole still
   needs the hourly frame for reasons unrelated to trigger-price arithmetic
   (TIME exit's `hours_held`, bar-close bookkeeping, real OHLC for
   gap-through fills) -- "exit trigger prices" and "the exit path" are not
   the same scope, and the original doc conflated them.**

5. **`check_live_state_reconciliation` runs unconditionally every cycle,
   ahead of buy/sell actions, and makes 2 real sequential Schwab API calls
   PER POSITION, not per account.** `_open_orders(account)` is account-scoped
   data re-fetched once per position sharing that account (5x redundant for
   `ira`'s 5 positions, 6x for `brokerage`'s 6, etc.). **Correction (see item
   C below): this function is NOT pure detection-only as the original doc
   claimed -- it can auto-close a real position (`_reconcile_auto_close_flat_
   position`, added 2026-08-19) and is the live detector for a missing
   protective order. Batching its account-scoped API calls is still sound;
   reducing its cadence or skipping it near real trading windows is not,
   without an explicit user decision about that live-capital tradeoff.**

6. **Parallelizing the price-fetch step alone gives a real, measured ~7.5x
   speedup with zero correctness cost.** 17 real tickers, sequential Schwab
   quote calls: 4.34s. Same 17, threaded (`ThreadPoolExecutor`): 0.58s.
   Results matched exactly. A 170-call sustained stress test (17 tickers x
   10 rounds) hit zero rate-limit errors in 5.17s (~1973 calls/min) --
   Schwab's documented 120/min "Order Limit" appears specific to order
   placement (POST/PUT/DELETE), not quote GETs. Order-placement throughput
   itself has NOT been tested this way (can't safely stress-test real order
   placement) -- Schwab's own documented order-rate limit is 120/min, 10x
   this project's self-imposed `GLOBAL_ORDERS_PER_MINUTE=12`
   (`schwab_safety.py` line 360). **Correction (see item E below): this
   parallel-fetch finding is still solid, but the original doc's
   justification for skipping per-account order-placement serialization was
   empirically false -- see E.**

7. **Real machine constraint**: load average routinely 8-10/12 cores busy
   (the sweep pipeline routinely runs alongside the live daemon). Any
   parallel design here should use threads (`ThreadPoolExecutor`), not
   separate OS processes -- this work is I/O-bound (network calls, disk
   reads), and CPython releases the GIL during blocking I/O syscalls, so
   threads get the real overlap benefit without adding CPU contention. The
   GIL does NOT release for CPU-bound work (pandas/numpy computation), so
   threading helps the network/disk-wait portions of this pipeline, not the
   math portions -- relevant to item 3 above, not item 6.

## Corrected cost model

A real measured cycle (2026-09-17, ~58s total, from `logs/active_signals_
verbose.log`) breaks down as:

| Phase | ~Duration |
|---|---|
| Data refresh | ~2s |
| Gap before first exit check (`check_sl_order_fills` + `check_live_state_reconciliation` + pinned block) | ~14s |
| Exit-check loop over 13 positions | ~3s |
| Paper/dry-run sells | ~2s |
| Everything after (broker housekeeping: auto-fills, own-sell-fills, addon-leg reconciliation, fill-queue drain, paper/dry-run buy updates, entry-abandon/market-buy-rejected checks, trade-control sync) + ambient buy scan | ~31s |

Two corrections to the original doc's cost attribution:

- **CSV re-reads (`_load_cache`/`_current_price`) are only ~2-3s of the
  total** -- a real but much smaller contributor than the original doc
  implied. The bulk of the cost is the ~14s pre-exit reconciliation block and
  the ~31s of post-exit broker housekeeping, neither of which is CSV-bound.
- **The `_guarded` section-timing instrumentation** (already built and
  shipped in `active_signals.py` as of tonight) **has never actually run
  against a live trading day yet** as of this writing. The breakdown above is
  from manual log inspection of one cycle, not from that instrumentation's
  own aggregated output. Real per-section attribution data from
  `_guarded` should be collected from a live trading day before finalizing
  build priority among the remaining items below -- the relative sizing in
  this table is a reasonable starting point, not a substitute for that data.

## SLA / target

**Target: tick-to-trade under 1 second** (fetch price -> compare to precomputed
trigger -> send order), matching the fidelity of the 1-second-resolution GT
kernel this project's real live-vs-backtest fidelity is judged against
(user's explicit framing, 2026-09-17: 'get a price, is it past the trigger,
then send the trade'). **Acceptable fallback: under 10 seconds** if 1s proves
genuinely unreachable for a real reason (network round-trip floor, Schwab-side
latency) -- everything measured tonight (170-call quote stress test: ~150-300ms
per real Schwab round-trip) suggests 1s is achievable for the fetch+compare+
send critical path once the housekeeping ahead of it is addressed, not just
optimistic.

**How this gets verified, not just asserted**: `scripts/evening_status.py`'s
section 4b (tick-to-action delay) and `scripts/tick_to_trade_report.py` --
already built and paired-reviewed this same night -- are the standing
regression check. Re-run both before and after this design lands; the
required outcome is the real p50/p90 tick-to-trade numbers they report
dropping from tonight's measured baseline (8-573s, median well over 30s)
into the sub-1s (target) or sub-10s (fallback) range. This design does not
count as successful based on code review alone -- it needs the daemon to
actually run through real signal windows post-change and the telemetry to
show the real number.

## Proposed changes

### A. Cache `_load_cache`'s raw return per (ticker, CSV mtime) -- NOT a full daily-frozen-band precompute

**Original proposal (precompute `{ticker: (sma, std, band)}` once at daemon
start from `_load_cache`/`generate_daily_indicators` "as-is") is marked NEEDS
REWORK -- it would be a regression, not an optimization.**

Two problems found in review:

1. **Most of the caching this proposed already exists.** `signals_compute.
   py`'s `_indicator_cache` (keyed `(ticker, strategy, window)`) already
   avoids recomputing `generate_daily_indicators` on every poll. The
   remaining real cost here is narrower than the original doc assumed.
2. **Building the band from `_load_cache` "as-is" would silently revert a
   real 2026-08-28 correctness fix.** Live `compute_buy_signal` actually
   routes through `_daily_close_source` (`signals_compute.py` line ~181),
   which prefers a FRESH yfinance daily fetch over the resampled CSV,
   specifically to correct retroactive price adjustments. Precomputing from
   `_load_cache` alone bypasses that fresh-source preference, and also
   bypasses the `paper_role == 'daily_sync'` carve-out (`signals_compute.py`
   line ~306) that deliberately must NOT use the fresh source. A daemon-start
   precompute frozen from `_load_cache` would quietly feed BUY decisions from
   the pre-fix data source for every non-`daily_sync` node.

**Revised recommendation**: if the residual ~2-3s CSV-read cost (see cost
model above) is still worth removing, cache `_load_cache`'s raw return
keyed on `(ticker, csv mtime)`, invalidated whenever the CSV file changes --
not a frozen daily band, and not a bypass of `_daily_close_source`'s
fresh-fetch/`daily_sync` logic, which stays exactly as-is downstream of the
cache. This is a much smaller, correctness-preserving change than the
original proposal.

### B. Reuse one `_load_cache` result per ticker per cycle -- NOT "eliminate the CSV from the exit path"

**Original proposal ("exit trigger prices computed inline, never touch the
CSV") is marked NEEDS REWORK.** The narrow claim survives review: SL/ARM/
TRAIL trigger PRICES are pure arithmetic off `entry_price` + config
(confirmed in `strategies.py`'s `check_exit`) and should stay computed that
way. But "never touch the CSV" as a description of the exit path overall is
wrong -- the exit path genuinely needs the hourly frame for reasons that have
nothing to do with trigger-price arithmetic:

1. `hours_held`/the TIME exit needs `df_hourly` (via `_bars_held`, which
   counts trading-hour bars since signal) -- and this applies even to an
   ARMED (trailing) position, see item F below.
2. `last_bar_ts` keys the `sell_alerted` dedup set.
3. `resolve_at_bar_close`'s stateful comparison against `last_seen_bar`,
   which it mutates -- multiple code paths share this to avoid
   double-processing the same bar.
4. Real bar OHLC for the gap-through-trigger fill price
   (`strategies.py`'s `check_exit`, the Open-first gap check against
   `trail_stop_gap`/`stop_price`, ~lines 247-302) -- this is load-bearing for
   the actual reported exit price on a gap fill, not just display.

**Also correcting a factual error in the original doc**: the exit price on
the live mid-bar path already uses a real broker quote
(`resolve_live_exit_price`, fixed after a 2026-08-19 incident), NOT
`_current_price`/the CSV -- the original doc had this backwards. The
remaining `_current_price`/CSV callers on the exit side are mostly paper/
dry_run simulation infrastructure, not the real live fill-price path.

**Revised recommendation**: rescope to "reuse one `_load_cache` result per
ticker per cycle" -- matches A's revised scope (same underlying cache), and
removes the real redundant-re-read cost (36 calls for 16 tickers in one
measured cycle) without touching anything the exit path structurally needs
the CSV for.

### C. `check_live_state_reconciliation`: batch account calls now; cadence/grace-skip need a separate decision

**Split verdict, was a single 3-part recommendation in the original doc.**

- **Batching by account is SOUND and is DONE, not just planned.** Confirmed:
  `_open_orders(account)` really is called per-position redundantly today.
  Another agent built the batch-by-account version tonight, in parallel with
  this doc's revision -- fetch `_open_orders(account)` once per distinct
  account per run, reuse across that account's positions.
- **Reduce-cadence and grace-window-skip are NOT SAFE AS ORIGINALLY STATED.**
  The original doc's premise -- "a pure detection-only canary, never acts" --
  is FALSE as of a 2026-08-19 change: `check_live_state_reconciliation` now
  calls `_reconcile_auto_close_flat_position` and can genuinely auto-close a
  real position when the broker shows zero shares and the recorded SL order
  is FILLED/CANCELED. It is also the live detector for "missing protective
  order." Reducing its cadence widens the real window a position could sit
  unprotected and unnoticed; a grace-window skip would additionally blind it
  at exactly the moments new positions are opening and new stops are being
  placed -- the worst possible time to turn this check off.

**Revised recommendation**: ship the batching (already done). Hold
cadence-reduction and grace-window-skip pending an explicit, separate user
decision about that live-capital tradeoff -- do not bundle either into a
latency fix without that conversation happening first.

### D. Reorder the loop: move the AMBIENT buy scan up, not the pinned check

**Original proposal ("pinned entry-check runs before the general ambient
exit/housekeeping sweep") is marked premise wrong, real fix identified.**

Confirmed directly by reading `run_loop`'s real ordering (`active_signals.py`):
the pinned entry-check block already runs early -- right after
`check_sl_order_fills` (line 1270) and `check_live_state_reconciliation`
(line 1282), around line 1314-1378, well before the broker-housekeeping
block (lines ~1496-1572: paper/dry-run sells, reminders, auto-fills,
own-sell-fills, addon-leg reconciliation, fill-queue drain, pending-buy
updates, entry-abandon/market-buy-rejected checks). D as originally written
is a no-op against the current code.

**The real, valuable reorder is different**: the AMBIENT buy scan (not the
pinned check) sits at the very end of the loop body (`_ambient_buy_scan_
nodes`/`_scan_buy_signals` around lines 1605-1674), behind all ~31s of that
broker housekeeping. The ambient path is what ETHU actually fired through.
Once the 14:30 stale-price bug (see Root Cause section above) is fixed, the
pinned check should catch most real signals going forward -- but the ambient
scan is still the fallback for a failed/missed pinned check (e.g. a price
fetch exception in `_scan_pinned_entry`, which adds the ticker to
`failed_tickers` rather than firing at a degraded price) and deserves the
same reorder treatment on its own merits.

**Revised recommendation**: move the ambient buy scan to run immediately
after the pinned block, ahead of the broker-housekeeping block, not after
it. Two real ordering constraints must be preserved when doing this:

1. **Drought HANDOFF logic must still run before the ambient scan.**
   Confirmed in the code's own comments (~line 1605-1625): "drought-overlay
   HANDOFF -- MUST run BEFORE this poll's ... `check_paper_drought_handoff`'s
   own docstring states this ordering" -- HANDOFF initiates the exit before
   the ambient scan runs, or a core signal could fire on a node HANDOFF was
   about to hand back. Whatever new position the ambient scan moves to, the
   HANDOFF block that currently precedes it (immediately above, using the
   same `_ambient_buy_scan_nodes(watchlist, now)` eligibility set) must move
   with it, in the same relative order.
2. **The ambient scan's own `:25-:29` pre-pinned-check exclusion window**
   (inside `_ambient_buy_scan_nodes`, which has its own comment explaining
   the reasoning) must be preserved exactly -- this is what stops the
   ambient path from racing/double-firing against the pinned check just
   before it runs, not an artifact of the current ordering that can be
   dropped when the scan moves earlier in the loop.

### E. Parallel price-fetch burst, with per-account order-placement serialization (not dropped)

**Core threading idea for price-fetching is kept; the safety justification
for skipping order-placement serialization is marked NEEDS REWORK -- it was
empirically false.**

The original doc dropped per-account order/capital-check serialization as a
requirement, on the basis that "Schwab's own margin/buying-power enforcement
at order-placement time is the real backstop." This is contradicted directly
by an existing comment in `schwab_safety.py` (lines 113-114): Schwab **does
not** reserve or check buying power for a resting order at placement time --
confirmed by a real test where a $200k TRAILING_STOP order and a real limit
order both left buying power unchecked. Since the live default strategy
(`TrailingBothZScoreBreakout`) places resting trailing-buy orders, the
named backstop does not exist for the order type this system uses most. A
same-account race under E's proposed unserialized parallel burst could
therefore genuinely place two orders whose combined notional exceeds real
account capital, not just produce a harmless rejection.

**Revised recommendation**:
- Keep price-fetching parallelized -- that part's real, measured 7.5x
  speedup (4.34s -> 0.58s for 17 tickers) is solid and safe: it's
  threading-only, makes no state mutation, and each fetch is independent.
- Serialize order PLACEMENT per account. Reuse `schwab_safety.approve_and_
  record`'s existing flock-based locking (`schwab_safety.py` ~line 1073-1078,
  `fcntl.flock(f, fcntl.LOCK_EX)`) rather than inventing new locking --
  this already exists specifically to make order approval/recording atomic
  and is the right primitive to extend, not duplicate.
- **`GLOBAL_ORDERS_PER_MINUTE = 12` (`schwab_safety.py` line 360) needs
  deliberate resizing as part of building E, not discovery live.** This cap
  was sized for a much smaller (6-ticker) watchlist against today's much
  larger real universe. Parallelizing entries compresses what's currently a
  multi-minute spread-out fire into a period of seconds -- at the current
  watchlist size, a real simultaneous multi-ticker signal at one of the 4
  windows could hit this cap and get real orders rejected/delayed by the
  rate limiter itself, which would be a self-inflicted version of the exact
  problem this doc is trying to fix. Schwab's own documented order-rate
  limit is 120/min, giving real headroom to raise this deliberately once E is
  scoped.
- Must not double-fire against the existing ambient loop's own handling of
  the same nodes -- needs the same `buy_alerted`/`pending_wl_ids` dedup the
  ambient path already uses, shared state, not reinvented.

### F. State-based work filtering -- 4 states (IDLE / PENDING / HOLD / ARMED), not 3

**Original 3-state model (IDLE/HOLD/ARMED) is marked good idea, real safety
holes, needs a 4th state.** Three corrections to the per-state rules, plus a
missing state entirely.

1. **ARMED positions must NOT be dropped from the active poll entirely.**
   The original doc's claim -- "needs NOTHING in the active per-cycle poll...
   the only thing that matters once armed is FILL detection" -- is wrong on
   two counts, confirmed directly in `strategies.py`'s `check_exit`:
   - The real trailing-stop-PRICE trigger genuinely doesn't need polling
     (the broker's resting order tracks it). But the TIME-exit condition
     still fires even while armed/trailing: `check_exit`'s `if
     state.get('trailing')` branch (line 244) includes `ctx['low'] <=
     trail_stop or ctx['hours_held'] >= ctx['max_hours_to_hold']` (line
     276) -- hold-time expiring while still armed is a real, distinct exit
     path (`state['exit_forced_by_hold_time']`, line 294), with a real
     historical incident behind it: SH, 2026-07-29, held 28h against a 24h
     max while armed with a 50%-wide trail, stuck waiting on a resting order
     nowhere near its trigger because nothing was checking TIME while armed.
   - `check_live_state_reconciliation`'s "resting trailing-sell order
     actually exists" check also needs ARMED positions to remain visible in
     whatever population it scans -- it can't detect a missing protective
     order on a position it never looks at.
2. **HOLD's proposed arm-crossing check using a live mid-bar tick would
   create a live-vs-backtest divergence.** Confirmed in `strategies.py`:
   arm activation (`state['trailing'] = True`, line 308) sits behind `if not
   ctx.get('at_bar_close', True): return None, None, state` (line 304-305)
   -- arm is deliberately bar-close-only in the kernel, matching backtest
   semantics (confirmed identically in `backtester.py`'s
   `_simulate_trail_ground_truth`, the `STATE_HOLD` branch's `if c >=
   arm_price` check at line 1999, gated under the "bar-close-gated events"
   section, line 1994). Checking arm-crossing against a live intrabar tick
   would let live arm earlier than backtest ever would for the same data --
   drop the "live mid-bar arm check" part of HOLD's proposed rule; arm
   crossing stays a bar-close check like everything else in this state
   machine.
3. **Skipping IDLE nodes between windows would break `last_seen_bar`
   bookkeeping.** `resolve_at_bar_close` mutates `last_seen_bar`, and
   multiple code paths share it to avoid double-processing a bar. An IDLE
   node's bar bookkeeping needs to keep advancing regardless of which nodes
   are actively "checked" for entry signals -- "needs entry-check only at
   the 4 real windows" is fine for the SIGNAL check itself, but must not be
   read as "skip this node's bar-tracking entirely between windows."

**The missing state**: F's original model has no PENDING bucket -- a node
with a resting entry order placed, not yet filled, not yet a position.
Several real functions exist specifically to service this state and would be
silently broken by a 3-bucket model: `check_entry_abandon`,
`check_market_buy_rejected`, `update_real_pending_buys_running_low`, and the
limit-order fill-check path. These all need PENDING nodes in an actively
checked population every cycle, not just at the 4 windows -- a resting entry
order can fill, get rejected, or need abandoning at any time, not just at a
bar boundary.

**Revised model**:

- **IDLE (no open position, no pending buy)**: entry SIGNAL check only at
  the 4 real windows (via D/E above) -- z-score/band signals are only
  meaningful at those 4 discrete bar-reaction moments. Bar-tracking
  (`last_seen_bar` advancement) continues every cycle regardless.
- **PENDING (resting entry order, not yet filled)**: stays in the active
  per-cycle poll every cycle, not just at the 4 windows --
  `check_entry_abandon`, `check_market_buy_rejected`,
  `update_real_pending_buys_running_low`, and fill-checking all need this.
- **HOLD (position open, not yet armed)**: ARM-crossing check (bar-close
  only, cheap: current `open_positions` state vs. that bar's Close, no CSV,
  no band) plus an addon-trigger check, only while addon is enabled and no
  addon leg has been placed yet for this position.
- **ARMED (trailing-sell order resting at the broker)**: drops out of the
  trigger-PRICE polling (the broker tracks that), but stays in the active
  poll for (a) the TIME-exit check (`hours_held >= max_hours_to_hold`, real
  even while armed) and (b) `check_live_state_reconciliation`'s
  missing-protective-order detection. Fill detection is a separate
  (stream/async) concern layered on top, not a replacement for these two.

Net effect: the population needing FULL uniform per-cycle attention shrinks
to PENDING + HOLD; IDLE only needs bar-tracking + 4-window signal checks;
ARMED only needs the two narrow checks above, not full polling. This still
meaningfully reframes the original "36 `_current_price` calls for 16 distinct
tickers" finding -- a real portion of those were very likely for already-
ARMED positions whose trigger-price checks were unnecessary -- just not as
aggressively as "drop ARMED from the poll entirely" would have implied.

## Arm/trail-sell verification tooling (new, from tonight's follow-up work)

`scripts/verify_arm_timing.py` was started tonight as the arm-side sibling of
`verify_open_price_quality.py`. Current state:

- **Arm bar-close-crossing check: WORKING.** Ground truth = hourly Close
  crossing `arm_price`, matching the backtest's bar-close-only arm semantics
  confirmed in item F above (`strategies.py` line 304-308,
  `backtester.py`'s `_simulate_trail_ground_truth` line 1994-2002).
- **Trail-sell intrabar check: NOT YET CORRECT.** It needs to walk real
  second-resolution data (`db_cache.get_massive_second_ohlcv`, with minute
  fallback), replicating the exact per-tick sequencing the ground-truth
  kernel uses: check Open against the OLD peak-derived stop, THEN update the
  peak with this tick's High, THEN check Low against the NEW stop (see
  `backtester.py`'s `_simulate_trail_ground_truth`, the ARMED-state branch,
  for the exact order) -- not a naive running-max-High-then-check-Low loop,
  which would let a tick's own high extend the stop before that same tick's
  own low is checked against it, silently making the stop harder to breach
  than the real kernel's tick-by-tick semantics allow.

**Separate finding, worth its own backlog item**: `massive_second_derived`
(the real second-resolution ground-truth table) is currently STALE -- last
real data as of tonight's check was 2026-08-24, 24 days behind. Second-
resolution verification isn't actually possible for any recent trade right
now. This degrades ANY second-resolution work silently, not just this
script -- worth flagging and fixing independent of `verify_arm_timing.py`
itself.

## Open questions for review

1. Item A's `(ticker, csv mtime)` cache -- confirm the invalidation trigger
   (mtime check) is cheap enough to run every cycle without reintroducing a
   meaningful fraction of the cost it's meant to remove.
2. Any interaction between E's parallel burst and C's (currently on-hold)
   cadence-reduction/grace-window-skip -- deferred along with C until the
   separate user decision about that tradeoff happens; revisit once that
   decision is made.
3. Is there an existing pattern in this codebase for "a dict of frozen
   per-day state, periodically re-validated" worth reusing for A's narrower
   `(ticker, csv mtime)` cache, rather than inventing new machinery?
4. F's revised 4-state model -- confirm no other function beyond the four
   named (`check_entry_abandon`, `check_market_buy_rejected`,
   `update_real_pending_buys_running_low`, limit-order fill-check) depends
   on a PENDING node being in an every-cycle-checked population; a targeted
   grep across `active_signals.py` for `pending_buys`/`pending_wl_ids`
   consumers before implementation would close this out.
5. D's reorder -- once the ambient buy scan moves earlier in the loop
   (ahead of broker housekeeping), confirm nothing later in that
   housekeeping block (auto-fills, own-sell-fills, addon-leg reconciliation)
   was implicitly depending on the ambient scan having already run first in
   the same cycle (e.g. a `buy_alerted`/`pending_wl_ids` state it reads).
