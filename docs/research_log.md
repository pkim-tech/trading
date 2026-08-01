# Research Log

Permanent, append-only record of experiments run against this system — a lab notebook,
not a to-do list. Distinct from `docs/backlog_cache.md`/`docs/deep_backlog.md` (open
action items) and `docs/conversation_summary.md` (session narrative): an entry here
exists whenever a real hypothesis was tested, whether or not it produced code changes or
a new backlog item. Oldest first, new entries appended at the end. Committed (not
gitignored, not capped) — old findings should stay greppable indefinitely.

Infrastructure findings that change how future checks should be run (e.g. a new
watchlist-candidate-checklist check) get logged here too, but the actual reusable
procedure/script documentation lives in `docs/watchlist_candidate_checklist.md` (or the
relevant script's docstring) — don't duplicate it here, just note that it changed and
why.

Each entry:
```
## YYYY-MM-DD — <ticker/topic> — <one-line finding>
**Hypothesis/question:**
**Method:**
**Result:**
**Verdict:** Confirmed / Refuted / Inconclusive / Resolved-no-action
**Follow-up:** backlog item name, or "none"
```

---

## 2026-07-14 — KORU/same-day re-entry — trailing-buy re-entry timing after a same-day exit needs no fix
**Hypothesis/question:** If a same-day re-entry trigger hits (ticker sold, then dislocates
again same day), does the live trailing-buy order need to be placed relative to the 9:30
open or the 10:30 normal bar time?
**Method:** Read `_simulate_trail_both`'s actual loop (`backtester.py:538-643`) to check
whether new-signal detection has any calendar-day reset or first-entry-of-day special-casing.
**Result:** New-signal detection is already restricted to the same two configured hours
every day (`target_h0`/`target_h1`, matching the two live Slack windows), with no
day-boundary logic at all. A same-day re-entry is scanned for using the exact same two
windows as any other entry, walk-forward — nothing day-of special about it. The original
"10:30" framing in the question was itself a misunderstanding: 10:30 was never a
signal-check hour, backtest or live.
**Verdict:** Resolved-no-action — no bug, no fix needed.
**Follow-up:** none.

---

## 2026-07-15 — SOXL/KORU — Phase 3 (full parameter mesh) adds no value over Phase 1/2/2.5
**Hypothesis/question:** Does the v4 sweep's Phase 3 (full mesh refinement) ever actually
improve on the best node Phase 1 (coarse) + Phase 2 (island search) + Phase 2.5
(cliff-box) already found?
**Method:** Added `phase`/`generation`-level tracking and a `--max-phase` CLI cap
(`run_optimization_sweep.py`), then compared each phase's own best
`MIN(possible,pessimistic,certain)` alpha node across all 30 tagged SOXL+KORU SL-sweep
campaigns run so far.
**Result:** Phase 3 won 0/30 campaigns — Phase 1 or Phase 2 always held the best robust-alpha
node; Phase 2.5 won a few. Separately confirmed island/cliff-safety selection (Checkpoint 2)
already only depends on Phase1+2+2.5 data, so Phase 3 was never part of that calculation
anyway.
**Verdict:** Confirmed (Phase 3 is dead weight for this sweep's actual selection criterion).
**Follow-up:** `--max-phase {1,2,2.5,3}` added (default 3, unchanged pipeline behavior) so
future runs can skip it for compute/disk savings; `generation` column added to similarly
test whether island search's extra generational passes earn their cost (not yet analyzed).

---

## 2026-07-17 — watchlist-wide — same-day buy→sell block explored and deliberately not built
**Hypothesis/question:** Should a same-day buy-then-sell round trip on the same security be
blocked, to avoid being flagged/classified as a day trader (a compliance-optics concern, not
a broker-side rule)?
**Method:** (1) Confirmed the live account is a limited margin account (no PDT-style
broker-side count limit, no cash-account good-faith-violation risk). (2) Live FINRA lookup:
the classic PDT $25k-minimum/4-trades-in-5-days rule was eliminated entirely effective
2026-06-04 (Regulatory Notice 26-10), replaced by an intraday-margin-deficit framework that
doesn't restrict day trading by count at all. (3) Quantified the real cost of avoiding
same-day round trips anyway, via a new gap-aware deferred-sell simulator
(`scripts/sim_delayed_sell.py`/`export_trades.simulate_trail_both_deferred_sell`) on GDXD's
real production node.
**Result:** No broker-side or (post-2026-06-04) regulatory reason to block same-day round
trips remains. Employer-side: no official written rule, only an unstated soft preference.
Real cost of blocking anyway is high — GDXD's SL=1% node retains only ~47% of its edge
(+22,402% → +10,473%) if forced to defer same-day exits to the next day (first-pass number
of +25,793% was a bug: deferred stop-loss fills were pinned to the nominal `stop_price`
regardless of overnight gap risk; fixed to charge the worse of `stop_price` or the resolving
day's `Open`).
**Verdict:** Resolved-no-action — proceed without a same-day buy→sell block. Worst case if
compliance ever objects is being told to stop, a reversible outcome not worth giving up real
edge for a rule that may not even apply. `schwab_safety.py`'s existing `same_day_block`
(blocks same-day *re-buy* after an exit — the original cash-account-era behavior, a
different direction than this question) was left untouched, still enforced live.
**Follow-up:** none on this question. Revisit only if compliance actually raises it.

---

## 2026-07-18 — watchlist candidates — checklist checks 4/9/10 run for 19 tickers
**Hypothesis/question:** Of the 19 tickers screened by the fast checklist pass (1/2/3/6/7/8)
this session and last, which ones show real win-rate decay (check 4) or real exposure to
the live, still-enforced `schwab_safety.py` same-day-block (checks 9/10) that the fast
screen doesn't catch?
**Method:** New `scripts/checklist_deep_checks.py` — reuses `run_backtest_v110`
(`same_day_block=True` vs `False`), `_summarize_trades`, and `compute_bh_returns` directly
(no reimplemented kernel/alpha math). For each of AGQ/DPST/EDC/GDXD/GDXU/HIBL/KORU/LABU/
NUGT/SOXL/TQQQ/YANG/UDOW/USD/UVIX/ZSL/NAIL/DUST/RETL: pulled the real v4 SL=1%/open_check
winning node, ran it blocked and unblocked, computed robust alpha
(`MIN(possible,pessimistic,certain)`) for each, and split both trade lists chronologically
70/30 to check stability of the retention ratio over time.
**Result:** Check 4: GDXU (25.6%→5.6% win rate) and RETL (15.7%→7.7%) show real late-window
decay; everyone else holds flat or improves late. Check 9: same-day-block retention of
robust alpha ranges from severe (ZSL 3.5%, YANG 3.7%, GDXD 6.9%, UDOW 14.7%) to fine-to-improving
(DPST 111.9%, UVIX 162.7%). Check 10: several tickers' late-window retention ratio goes
negative or blows up (TQQQ, YANG, UDOW, USD, UVIX, ZSL) — traced to a thin/near-zero late-window
baseline robust alpha making the ratio unstable, a methodology artifact of small late samples
rather than a real reversal; treat those specific late-window numbers as inconclusive, not as
"gets worse." Full numbers in `logs/checklist_deep_checks.csv`.
**Verdict:** Confirmed real flags for GDXU/RETL (check 4) and ZSL/YANG/GDXD/UDOW (check 9);
inconclusive for check 10's negative-ratio cases specifically.
**Follow-up:** feeds directly into the open "promote other 11 watchlist tickers' v4 nodes"
backlog decision — none of this is a hard blocker (same-day-block isn't being newly
enforced beyond what already exists) but it's real information about how concentrated each
node's edge is in trades a live constraint could someday clip.

---

## 2026-07-18 — DPST/SOXL — chaos-monkey "improves under misses" divergence explained
**Hypothesis/question:** Session 18's chaos-monkey run found DPST/SOXL's mean compounded
return holds up or improves as signal miss rate increases, unlike the rest of the
watchlist (which decays, e.g. KORU -31% at 20% miss). Is this a real, understood
mechanism, or noise/small-sample artifact?
**Method:** New `scripts/investigate_chaos_divergence.py` — reuses
`export_trades.simulate_trail_both_chaos` directly, decomposing the combined miss_rate
into entry-only and exit-only runs (2000 trials each, `drop` mode, 20% miss) for
DPST/SOXL/KORU/HIBL (KORU/HIBL as tickers that decay normally, for contrast). Also pulled
each ticker's real live node params and baseline per-trade return distribution
(win rate, mean/median return per trade).
**Result:** Two separate effects, not one: (1) **Exit-side misses are structurally
benign-to-beneficial for every ticker tested** (ratio 0.98–1.22, never a real drag) — the
SL check (`export_trades.py:341`, `if low <= stop_price`) is re-evaluated fresh every bar
with no memory of a prior touch, so a missed check on a spike-down bar that recovers by
the next bar simply never re-fires; the position survives a stop-out the
perfect-adherence baseline would have taken. This is a property of the current
kernel/live-monitoring design, not sampling luck. (2) **The real ticker-specific
divergence is on the entry side**: KORU (ratio 0.70) and HIBL (0.72) lose heavily to
missed entries because their winning trades are infrequent and high-quality (KORU: 68.8%
win rate, 11.0% median per-trade return — missing one is a real lost opportunity), while
DPST (1.06, improves) and SOXL (0.95) have much thinner, closer-to-coinflip edge per trade
(DPST: 52.9% win rate, 0.9% median per-trade return) — missing some of DPST's many
marginal-quality entries costs little and occasionally dodges a loser, netting positive.
SOXL's exit-side ratio (1.22) is the largest of the four and not fully explained —
plausibly tied to its already-known high intrabar volatility relative to `trail_sell_pct`
(fill-drift ratio 3.47, an earlier checklist finding) but not confirmed.
**Verdict:** Confirmed — a real, understood structural mechanism (exit-side SL-check
statelessness) plus a real per-ticker entry-quality effect, not noise or a small-sample
artifact.
**Follow-up:** SOXL's unusually large exit-side ratio is still open, worth a closer look
if SOXL comes up again. No code changes needed — this explains existing behavior, doesn't
require a fix (the SL-check statelessness matches live behavior already, not a
backtest/live divergence).

---

## 2026-07-18 — SOXL/AGQ/KORU/watchlist-wide — v3-vs-v4 trade overlap, watchlist-wide max drawdown, live selloff comparison
**Hypothesis/question:** Prompted by a real, live semiconductor selloff hitting SOXL/KORU:
(1) is v4 (SL=1%, open_check) really "the same trades as v3 with a tighter stop," or a
different strategy? (2) What's the worst-case historical drawdown for every watchlist
ticker's v4 node, and does any of it come close to a stated ~50% risk tolerance? (3) Right
now, mid-selloff, how does v4's live drawdown compare to what the actual live v3.x
`watch_list` params would be showing for the same tickers?
**Method:** New `scripts/v4_max_drawdown.py` (true equity-curve peak-to-trough max
drawdown, not just consecutive-loss-streak length) run across all 12 watchlist tickers'
v4 nodes. Separately compared each v4 node's trades against its real live v3.x
`watch_list` node's trades (same underlying kernel, different params) by matching entry
times within ±1 day. Also computed each node's *current* (as-of-now) drawdown from its
own running peak, for both v4 and the real live v3.x params, using latest cached data
mid-selloff.
**Result:** (1) v3/v4 are meaningfully different strategies, not the same trades with a
different stop — v4's `trail_buy_pct=1%` catches 3-5x more (smaller, more frequent)
entries than v3's wider thresholds; only 10-33% of v4's trades correspond to a real v3
trade around the same time (SOXL 33%, AGQ 22%, KORU 10%). (2) Every ticker's v4 worst-case
historical drawdown falls between -5.9% (YANG) and -23.8% (SOXL) — all well under a stated
~50% tolerance (which itself would take ~69 consecutive -1% losses to reach; SOXL's actual
worst streak was 27, i.e. -23.8%). SOXL/DPST/HIBL share the same real Aug-Oct 2023
drawdown window — a shared macro event across tickers, not independent risk. (3) Mid the
real, live 2026-07 semiconductor selloff: v4's current drawdown is minor everywhere (SOXL
-3.0%, KORU -2.0%, worst is GDXU -6.8%) while the real live v3.x params are taking much
larger current hits on the same tickers right now (SOXL -33.8%, KORU -38.6%, GDXU -55.7% —
GDXU's live v3.x node is at its all-time-worst drawdown point at this exact moment).
**Verdict:** Confirmed — v4's tight stop meaningfully changes real-time risk exposure, not
just backtest cosmetics; live-validated during an actual adverse event, not just
historical replay. v3-vs-v4 trade-overlap finding is a real caveat for the promotion
decision: v4 isn't a strict safety upgrade to the same strategy, it's a different
(busier, lower per-trade win-rate) strategy that also happens to have a much tighter stop.
**Follow-up:** Added checks 11 (max drawdown) and 12 (current-drawdown-vs-worst-case
calibration) to `docs/watchlist_candidate_checklist.md`. Feeds directly into the still-open
v4-promotion backlog decision.

---

## 2026-07-18 — 19 tickers (watchlist + screened candidates) — train/test split + walk-forward out-of-sample validation, resolving the "Train/test split" deferred backlog item
**Hypothesis/question:** Every v4 node on file (island search, cliff-safety, robust-alpha
ranking) is selected against the full historical range with no held-out data — is any of
it overfit to the in-sample period? This was the stated real blocker on the
watchlist-wide v4-promotion decision (see backlog).
**Method:** Two scripts built, both reusing the existing backtest kernel/trade-list rather
than reimplementing anything, and both avoiding a costly re-sweep by exploiting that the
strategy's SMA/std indicators are already backward-looking (no lookahead) — running the
backtest once over full history and slicing the resulting trade list chronologically is
numerically equivalent to running separate backtests on split date ranges:
1. `scripts/train_test_split_check.py` — single 70/30 chronological split per ticker
   (per-ticker 70th-percentile entry-time cutoff), robust-alpha (`MIN` of
   possible/pessimistic/certain) computed separately for each half, each against SPY's
   *own* return over that half's specific date range (`period_spy_bh`) — not one blended
   full-history SPY number, which was a real bug caught and fixed mid-session (the first
   version used `compute_bh_returns()`'s single full-history SPY return for both halves,
   which understated test-period alpha and even showed spurious negative retention for
   RETL/UVIX before the fix).
2. `scripts/walk_forward_check.py` — generalizes the single split into N=5 equal
   chronological calendar windows, each independently evaluated (not cumulative
   compounding across windows) against its own SPY benchmark. Built specifically because
   the single-split method showed a real interpretability weakness on KORU (see below).
Run across all 19 tickers cleared by the 2026-07-18 checklist screen (12 live watchlist +
UDOW/USD/UVIX/ZSL/NAIL/DUST/RETL).
**Result:** Single 70/30 split: median out-of-sample retention well under 50% (e.g. AGQ
28.5%, HIBL 23.1%, YANG 7.7%, TQQQ 4.9%), with only LABU/DPST/ZSL/DUST/KORU holding up
strongly. GDXD and SOXL landed at a near-exact tie on absolute out-of-sample numbers
(test robust-alpha 818.9 vs 815.1, test compounded 832.1% vs 844.9%) despite very
different retention ratios (43.0% vs 29.5%) — the ratio difference was mostly an artifact
of SOXL's training number starting higher, not a real difference in OOS quality. KORU's
70/30 result (test retention 518%, an apparent *improvement*) was traced to a single
outlier trade (+91.7% over a ~28-calendar-day hold, legitimately within the node's own
`max_hold_hours=126` trading-hour cap, not a bug) that alone accounted for roughly half
the entire test-period compounded return — a red flag that a single split's result can be
dominated by one trade and isn't trustworthy in isolation, in either direction.
Walk-forward (5 folds) gave a materially more useful picture: **14/19 tickers had zero
negative-alpha folds across all 5 windows** (AGQ, DUST, EDC, GDXD, GDXU, HIBL, KORU, LABU,
NAIL, SOXL, TQQQ, USD, YANG, ZSL); 5 had exactly one negative fold (DPST, NUGT, RETL,
UDOW, UVIX). Critically, KORU's walk-forward folds showed positive, real alpha (51-238%)
across all four windows *before* the standout recent-selloff window (900%) — the edge
isn't purely a fluke confined to one lucky test period, contrary to what the single split
alone suggested. In one fold (fold 3, 2024-10 to 2025-05) SPY itself returned -1.3% while
KORU/GDXD/SOXL all still posted large positive absolute returns (50-430%), real evidence
these aren't just riding a rising SPY tide.
**Verdict:** Confirmed with a real caveat — a single 70/30 split is not reliable enough to
trust alone (demonstrated concretely by KORU's outlier-driven, direction-ambiguous
result); the walk-forward version is the more trustworthy check going forward. Most
watchlist tickers show real, broadly consistent out-of-sample edge (14/19 zero-negative-
fold), though absolute out-of-sample magnitude is much smaller than the in-sample
headline numbers suggest across the board — none of this should be read as "the in-sample
numbers are right," only as "the edge is real, just smaller and noisier than in-sample
suggested."
**Follow-up:** Added as checklist check 13 (`docs/watchlist_candidate_checklist.md`).
DPST/NUGT/RETL/UDOW/UVIX's single negative fold each needs a closer look (which window,
how large) before treating them as validated. Directly informs, but does not fully
resolve, the still-open watchlist-wide v4-promotion decision (see backlog) — this
resolves the "is any of this in-sample-only" concern with real evidence, but the
promotion decision itself is still open.

## 2026-07-20 — Immediate-entry (TrailingExit) vs trailing-buy (TrailingBoth) full 18-ticker v5 comparison
**Hypothesis:** does `trail_buy_pct` (waiting for a bounce off the running low before entering) actually earn its cost, given how often overnight gaps blow past the trigger — and separately, is it worth dropping since trailing-buy orders are operationally hard to execute manually right now (the reason all 18 tickers are paused in `research` mode)?
**Method:** `scripts/run_sweep_queue.sh` (both strategies, `entry_timing=open_check`, `fixed_sl ∈ {1,2,3}`, all 18 watchlist tickers) run to completion on the corrected (gap-through-trigger-fixed) `v5` kernel — 230 `backtest_cache` phase rows confirmed present across all 36 (ticker × strategy) combos × up to 3 SLs. All numbers below are `ROBUST_ALPHA_SQL` (`MIN(alpha_vs_spy, pessimistic, certain)`, alpha vs SPY, not raw strategy return) — the same convention the live sweep engine's own Checkpoint2 cliff check uses.
**First pass was wrong**: initially compared raw best-alpha per ticker per strategy without checking cliff-safety (`worst_neighbor` in the real Phase2.5 cliff-box log), and reported TB winning 11/18 vs TE 7/18. The user caught this by pointing at the actual `sweep_queue_v5_20260720_092857.log` cliff-check lines, which show many of those "TB wins" nodes are CLIFF (unstable neighbors), not legitimate candidates.
**Corrected result (filtered to `worst_neighbor >= 0`, i.e. cliff-safe only, per ticker/strategy/fixed_sl combo from the real log)**:
- TrailingExit has a cliff-safe node on 12/18 tickers; TrailingBoth on only 7/18.
- On 5 tickers — DPST, KORU, LABU, TQQQ, YANG — TrailingBoth has **no cliff-safe node at all** across `fixed_sl ∈ {1,2,3}`, while TrailingExit does. No ticker shows the reverse (TB safe, TE not).
- Where both have a safe candidate: TB wins GDXU (267.2 vs 245.8), HIBL (466.2 vs 440.8), SOXL (1212.1 vs 947.0), USD (198.9 vs 195.6); TE wins AGQ (1114.9 vs 923.7), NUGT (384.8 vs 216.8), UDOW (300.4 vs 214.0).
- Neither strategy has a safe node at all for DUST, GDXD, NAIL, RETL, UVIX, ZSL (in this SL range).
**Verdict:** Mixed, corrected a second time — an earlier draft of this entry claimed TE "is never worse where both are viable," which is wrong: TB beats TE on 4 of the 7 tickers where both have a safe node (GDXU, HIBL, SOXL, USD). The only one-sided part of the result is *existence*: TB has zero safe candidates on 5 tickers where TE has one, never the reverse. So TE is the safer default where you need *a* viable node at all, but where both are viable, it's a real per-ticker split (TB wins 4, TE wins 3), not a blanket TE advantage. Combined with the real motivation (trailing-buy is hard to execute manually right now), TE is still the more practical default, but not because it backtests strictly better.
**Follow-up:** This used the log's per-combo Phase2.5 cliff-box output directly, not a fresh SQL cliff query against `backtest_cache` — worth a proper `CLIFF_RADIUS`-neighborhood SQL cliff check across the full grid (not just the best-per-combo candidate already promoted to Phase2.5) before treating this as final. No node changes made; this is a research finding only.

## 2026-07-20 — Last-window market-on-close vs trailing-buy comparison
**Hypothesis:** for a signal firing in the last daily signal window (the 14:30-labeled bar, checked in the 15:25-15:40 window — no bars left same-day before the overnight gap), does waiting for a trailing-buy bounce (TB) actually earn its cost, or would a plain market-on-close (MOC) entry at that signal bar's own Close do as well or better?
**Method:** new `scripts/sim_close_vs_trail_buy.py` + `export_trades.collect_last_window_comparisons`/`simulate_trail_both_signal_tracked`/`_simulate_exit_from_entry` — pure-Python mirrors of `backtester._simulate_trail_both`, extended to track `signal_i` and share the SL/TP/trailing/TIME exit state machine between the real TB fill and an MOC counterfactual entered at the same signal bar's Close. Run against the real Live v5 watchlist (id=65)'s 4 `TrailingBothZScoreBreakout` tickers (GDXU, HIBL, SOXL, USD), all 3 fill resolutions (possible/pessimistic/certain).
**Result:** TB beat MOC on every ticker and every resolution, not close — compounded return e.g. SOXL 1001-9603% (TB) vs 168-465% (MOC), GDXU 434% vs 81-118%, HIBL 339-430% vs 2.5-83%, USD 122-132% vs 25-42%. MOC actually "wins" more individual trades head-to-head on 3 of 4 tickers (higher count where moc_ret > tb_ret), but TB's rarer wins are much larger (fat right tail), so it dominates on mean/compounded return regardless. Hand-verified 2 SOXL trades (a simple 1-bar SL loss, and the largest TWIN winner, held to the 70h time limit) entry-to-exit against real OHLC bars in `SOXL_1h.csv` — every fill price traced exactly, to full float precision, to a real Open/High/Low/Close, confirming the result isn't a computation artifact.
**Verdict:** Resolved — MOC does not beat TB for last-window signals on the current watchlist; no ticker came close enough to justify an 18-ticker backfill or a live entry-timing change. Waiting for the trailing-buy bounce is still worth its cost even on the operationally-hardest signal window.
**Follow-up:** None planned, treated as closed. `scripts/sim_close_vs_trail_buy.py` is reusable if the question resurfaces on a different watchlist/ticker set.

## 2026-07-20 (cont.) — Watchlist 65 candidate testing: chaos-monkey, Stage 5 compliance gap found+fixed, watchlists.id gap root-caused+fixed
**Chaos-monkey execution-adherence, repointed at watchlist 65**: `sim_chaos_monkey.py` previously only supported `TrailingBothZScoreBreakout` and `entry_timing='close'` — extended with a new `export_trades.simulate_trail_exit_chaos` mirror (for the 6 `TrailingExitZScoreBreakout` tickers) and `open_check` support in both chaos mirrors (every node in watchlist 65 runs `open_check` live). Baseline switched to the real production kernel (`run_backtest_v110`/`run_backtest_v18`) instead of a pure-Python mirror. Verified all 10 nodes reproduce the real kernel exactly at 0% miss rate. Result: no node collapses even at a 20%-per-check miss rate (`drop` mode) — retention ranges 57.8% (HIBL) to 90.1% (DPST) of baseline compounded return; USD's thin sample (12 trades, previously flagged) held up fine (87.2%). Full detail: `output/chaos_monkey_summary.csv`.
**Stage 5 compliance recheck found a real, unfixed gap**: `strategies.py`'s `check_exit` (used by both real live `signals_compute.check_sell_condition` and `paper_trading.check_paper_sells`) never received the 2026-07-20 exit-side gap-through-trigger fix that's in `backtester.py`'s kernels — it only checked `low <= stop_price`/`low <= trail_stop`, no Open-first check, and the call site (`active_signals.py`) never even extracted the bar's Open to pass through. Practical effect: a live position gapping through its SL/trailing-stop overnight would report the stale theoretical price in the Slack SELL alert and in paper-trading's simulated exit, not the real gapped fill — same bug class as the kernel fix, never propagated to live-facing code.
**Fixed**: threaded a new `open_price` param through `check_sell_condition` from both call sites (`active_signals.py`, `paper_trading.py`, both now extract `bar['Open']` at bar-close); `strategies.py::check_exit` for `TrailingBothZScoreBreakout`, `TrailingExitZScoreBreakout`, and `LimitOrderTrailingExit` (identical duplicated block, fixed for consistency even though not currently live) now checks Open before falling to Low, mirroring the kernel exactly.
**Verified**: full `pytest tests/` (98 passed, unchanged — no existing test exercised the missing param, which is exactly the gap that was found). 3 synthetic cases (SL gap, trailing-stop gap, no-gap sanity check) confirmed correct behavior. Full bar-by-bar parity check against real historical data: forward-simulated `check_exit` from every real entry against the corrected kernel's actual trades — 151/151 SOXL (TB) trades matched exactly (exit bar, price, reason); 128/129 AGQ (TE) trades matched exactly, the 1 "mismatch" being an artifact of the test harness (a still-open position at the very end of the dataset, not a real divergence).
**Separately found and fixed a real `watchlists.id` bug while investigating the 57->65 id jump**: `signals_db.create_watchlist` used `INSERT OR IGNORE` keyed on the UNIQUE `name` column — reproduced directly in an isolated test DB that SQLite silently burns an AUTOINCREMENT id on a name conflict even though no row is written (5 duplicate-name attempts advanced the sequence by 5 with zero new rows). This explains the unexplained 57->65 gap (6 ids burned, zero trace in `watch_list_audit` since no row/error was ever produced). **Fixed**: check for an existing name first, only INSERT when genuinely new — verified duplicate calls now return the existing id with zero ids consumed, while genuinely new names still increment normally (1-by-1, no gaps).
**Verdict**: watchlist 65's 10 nodes are now fully candidate-tested (execution-adherence + live/paper compliance) with no blockers found; the compliance gap found along the way was real and is now fixed, verified against real trade history. `create_watchlist` bug fixed as a bonus finding, unrelated to the node params themselves.

## 2026-07-20 (cont.) — Full 13-check candidate checklist run on watchlist 65
**Method**: `scripts/checklist_v65.py` (new) runs checks 1/4/8/9/10/11/12/13 against all 10 watchlist-65 nodes' real live params (not a re-derived "best" node); checks 2/3/6 via existing scripts (`verify_trailing_buy/sell_resolution.py`, `check_stock_splits.py`), which already default to the active watchlist/all cached tickers.
**Findings**:
- Check 9 (same-day-block sensitivity): GDXU/SOXL showed severe alpha loss under the block (12.2%/5.2% retention) — **reinterpreted same session, not a real concern**: the guardrail (`signals_db.closed_today`, docstring: "IRA/SEP cash accounts can't reuse that capital until T+1 settlement") only applies to cash accounts; user confirmed watchlist 65 tickers will trade in limited margin accounts, which don't have this settlement restriction. GDXU/SOXL's real relevant number is their unblocked robust alpha (267.0%/1209.0%), not the same-day-blocked figure. TE strategy couldn't be checked at all here — `_simulate_trail`'s kernel has no `same_day_block` parameter.
- Check 13 (walk-forward, 5-fold): only UDOW/USD have zero negative folds; DPST/KORU/NUGT/SOXL/YANG have 2/5 negative — a real step down from the earlier v4 cohort's 14/19 clean record (2026-07-18).
- Check 11 (max drawdown): DPST -65.5% exceeds the previously-stated ~50% risk tolerance; YANG/SOXL/KORU (-54.3%/-47.9%/-46.0%) sit at the line. Deeper than v4's cohort (-5.9% to -23.8%), expected given v5's wider fixed_sl (2-3% vs v4's 1%).
- Check 12 (current drawdown): AGQ/KORU/SOXL sitting in a real drawdown right now (-21.1%/-34.7%/-33.6%), not at peak.
- Check 4: GDXU and NUGT show real win-rate decay early-to-late (22.7%→10.5%, 34.8%→20.0%).
- Check 6 (splits): 5 real splits found in-range (YANG, KORU×2, UDOW, USD) — spot-checked all against raw price series (±3-day max single-bar move, all under 10%), no unadjusted-jump artifacts, cache is correctly adjusted.
- Check 7 (fill-optimism): 90-94% of TB entries are "certain" fills, not dependent on the Low/High-ordering guess. The compounded-return-delta side of this check is stale — `simulate_trail_both_ohlc_aware` predates the 2026-07-19/20 gap-through-trigger fixes, so a direct comparison against the current kernel's return would be misleading; not reported.
- Check 8: no pure trade-count fluke on any node — all stay solidly profitable even excluding the single best trade.
- Check 2/3 (5-min resolution): SOXL shows the most drift (mean -0.53% entry-side, ratio 3.9 exit-side, consistent with its historical flag); check 3's sample is thin (3 exits total across the watchlist).
**Verdict**: no node is blocked from anything today (all still `mode='research'`). Real, structural findings worth remembering: DPST's drawdown exceeds stated risk tolerance; SOXL/KORU/YANG close behind; walk-forward consistency is noticeably weaker than v4's cohort across half the tickers. Full detail: `output/checklist_v65_summary.csv`.
**Follow-up**: `schwab_safety.py`'s `same_day_block` needs account-type awareness before it's ever wired into the live loop — logged to `docs/backlog_cache.md`.

## 2026-07-21 — Trailing-buy budget-adherence design (Part 3): four empirical checks that each changed the design mid-session
**Hypothesis/question:** designing the live order-placement infra for the "trailing-buy needs re-sizing"/"gap-through-trigger" backlog items (`docs/backlog_cache.md`) — several sub-questions came up that had real, checkable answers instead of being assumed.
**Method/Result, one per question:**
- *Does a trailing-buy order even need to rest overnight, or could placement be deferred to a single next-morning check?* Checked real signal-to-fill delay (`simulate_trail_both_signal_tracked`, `possible` resolution) across watchlist-65's 4 `TrailingBothZScoreBreakout` tickers: same-day fill rate is GDXU 40.6%, HIBL 82.0%, SOXL 53.0%, USD 100.0% — same-day fills are the majority, not the exception, so any design has to handle same-day sizing drift, not just overnight.
- *Is a flat sizing-pad percentage enough to bound overnight gap-through risk?* Computed the real overnight upward-gap distribution (close→next-open) per ticker against each node's real `trail_buy_pct`: p99 gaps run 8-19%, max observed 20.6% (GDXU). Even a +5-point pad still leaves 4-15% of gap days exceeding it — no finite fixed pad closes the risk, only bounds it probabilistically. Conclusion: overnight handling needs an active pre-open cancel/resize checkpoint, not a bigger static pad.
- *How stale is the pre-market quote available at a 9:15-9:29 pre-open check?* Pulled real 1-minute yfinance bars (small sample, yfinance caps 1m history at ~30 days): last pre-market print averages 16 minutes before the 9:30 open for GDXU/SOXL/USD, but **41 minutes for HIBL**, whose thin pre-market feed produced up to 13.4% drift to the real open in-sample. Led to checking Schwab's own quote endpoint (`Client.get_quote`) directly — confirmed live (`realtime: true`, `extended.lastPrice`/`quoteTime` fields populated) via a real call, materially fresher than yfinance's feed. Adopted as primary price source for the gap-check, with yfinance kept as fallback only.
- *What's the actual regulatory consequence of a limited-margin-account fill exceeding its budgeted notional?* Initial read (good-faith-violation/90-day-restriction rules) was the wrong mechanism — those govern selling before settlement, not a single oversized buy, and a true limited-margin (non-debit) account likely rejects/partially-fills an order it can't cover rather than completing it and creating a violation. Found the actually-relevant, very recent mechanism instead: FINRA Regulatory Notice 26-10 (Rule 4210 amendments, effective 2026-06-04, replacing the PDT rule) — an "intraday margin deficit" must be cured by close of business on the 5th business day before repeated-failure risk accrues, with a 90-day freeze only after a *pattern* of failures, not one incident. Whether this framework covers a limited-margin IRA (which doesn't support real debit) specifically is **not resolved** — FINRA's own notice says interpretive guidance was still forthcoming as of publication.
**Verdict:** Design finalized on the strength of these checks, not assumption — see the full design at `docs/backlog_cache.md`'s pointer entry and the session's plan file. Real conclusion of the regulatory check: stay conservative (pad + fast reconciliation) regardless, since account-type applicability of the cure window is still genuinely unconfirmed.
**Follow-up:** Confirm with Schwab directly whether the limited-margin IRA is in scope for the Rule 4210 intraday-margin cure mechanism. Once `schwab_client.get_current_price` is live, rerun the pre-market-to-open drift check against Schwab's own quote feed (not yfinance) to confirm the flat 5% branch-B pad is still well-calibrated with a live source instead of yfinance's staler one.

## 2026-07-21 — Real intraday drift across the four live signal-reaction windows, for automated bar-close BUY order sizing
**Hypothesis/question:** raised while discussing automating bar-close order placement for `TrailingExitZScoreBreakout` (currently a fully manual "Executed" Slack flow, `signals_notify.notify_buy_signal`'s non-trailing branch, `signals_handlers.py:193`'s `entry_price_submit` handler) — what sizing pad does an automated market order need, given real intraday price movement rather than an assumed near-instant fill? A live observation (watching HIBL fill across several partial orders at the open, some legs 0.25%+ apart) contradicted the initial "market order fills in ~5 seconds, pad can be tiny" assumption.
**Correction found mid-discussion, before any data was pulled:** the live daemon's `entry_timing='open_check'` mechanism (`active_signals._OPEN_CHECK_WINDOWS = [(9,31,9,40),(14,31,14,40)]`) doesn't read a literal exchange Open tick — `signals_compute.compute_buy_signal` always fetches the current real-time price and compares it to `lower_band` (from strictly-prior daily data), regardless of which window is polling. So "open_check" live means "poll earlier in the day, using whatever the live price is at that moment" — an approximation of the backtest kernel's literal `Open` column check (`backtester._simulate_trail`/`_simulate_trail_both`, `open_check_entry_timing`), which does read the true OHLC Open. Confirmed the two live windows correctly mirror the backtest, which evaluates `open_check_entry_timing` at both `target_h0=9` and `target_h1=14` (`backtester.py:806-822`) — the midday 14:31-14:40 window is intentional, not a bug.
**Also found:** `_OPEN_CHECK_WINDOWS`' ~9-10 minute width isn't arbitrary — `signals_config.POLL_SECS=300` (5-minute default polling cadence) means the daemon isn't phase-aligned to the market clock (`active_signals.py:274-275` comment: "POLL_SECS=300 means we rarely land on the exact opening minute"); the window needs to be wide enough that at least one poll reliably lands inside it regardless of phase.
**Method:** built `scripts/sim_open_window_volatility.py` (real yfinance 1-minute bars, ~7-8 trading days available) measuring, per ticker per day, how far price drifts from the specific print each of the 4 live windows reacts to: `morning_open` (drift from the 9:30:00 print, over 9:30-9:40), `midday_open` (drift from the 14:30:00 print, over 14:30-14:40), `morning_close` (drift from the 9:30-10:30 bar's Close/~10:30:00 print, over 10:25-10:40), `afternoon_close` (drift from the 14:30-15:30 bar's Close/~15:30:00 print, over 15:25-15:40). Run across all 10 real watchlist-65 nodes.
**Result — window volatility (mean of `max_dev_pct`, averaged across the 6 `TrailingExitZScoreBreakout` tickers: AGQ, DPST, KORU, NUGT, UDOW, YANG):**
| window | avg mean_dev | worst single day (avg across tickers) |
|---|---|---|
| morning_open | 1.78% | up to 5.1% (KORU) |
| morning_close | 1.16% | up to 4.6% (KORU) |
| afternoon_close | 0.51% | up to 2.3% (SOXL) |
| midday_open | 0.44% | up to 1.8% (SOXL) |

The 9:30 open is 3-4x more volatile than the midday/afternoon windows across every ticker checked — expected (opening-auction price discovery vs. a routine mid-session hourly boundary), but not previously quantified. Per-ticker spread is large: KORU's morning-open (3.49% mean dev) is ~8x YANG's midday-open (0.16%), so a single flat pad across tickers/windows is the wrong shape.
**Cross-checked against real entry-resolution mix** (separate quick analysis, real production kernel via `run_backtest_dispatch`, all 6 `TrailingExit` tickers' actual live nodes): 90.6% of entries resolve via the open-check branch (451/498), only 9.4% fall through to the close-window fallback. Of the open-check fires, 72% (324/451) are the *afternoon* (14:30) open-check — which is also the calmest window measured (0.44% mean dev). So the dominant real execution path is also the safest one; the volatile 9:30-morning-open path is both the minority (127/451 of open-check fires) and needs the biggest pad.
**Drift-accumulation profile** (added after the question "is the 1.78% morning-open drift instantaneous or does it build up?" — extended the same script to sample dev_pct at fixed minute-offsets from the 9:30:00 print): averaged across the 6 tickers, dev grows 0.94% (1 min) → 1.05% (2m) → 1.22% (3m) → 1.49% (5m) → 1.78% (10m). Roughly half the eventual 10-minute drift is present within the first minute (opening-print noise), but it keeps building meaningfully afterward, especially for the choppier tickers (KORU 2.59%→3.49% just between minute 5 and 10; SOXL 2.85%→3.92%). Not purely instantaneous noise — faster polling during the open_check windows specifically (vs. the default 5-minute `POLL_SECS`) would materially cut typical exposure, roughly in half if catching the signal at minute 1-2 instead of minute 8-9, though not eliminate it.
**Verdict:** a single flat sizing pad for automated bar-close BUY orders is the wrong design — needs to be per-ticker and per-window at minimum (morning-open vs. everything-else is the sharpest split). Poll frequency during the open_check windows is a real, separate lever against the same problem, not just pad size. Full data: `output/open_window_volatility_summary.csv`, `output/open_drift_profile.csv`.
**Follow-up:** (1) design the actual per-ticker/per-window buy/fill sizing budget (pad table + top-up) for the bar-close automation, using this data as the calibration input — not started. (2) Check whether the 5-minute poll cadence can cause a real signal to be missed at an open_check window's own boundary (e.g. a signal only true starting ~9:38 might not be caught until a poll lands at 9:41+, just past the window's 9:40 close) — raised, not yet checked.

## 2026-07-21 — Part 4 implementation: backtest-replay verification (Deliverable 1) surfaced a real `prev_close` bug
**Hypothesis/question:** while implementing Part 4 (pinned entry-check automation for the 6 `TrailingExitZScoreBreakout` watchlist-65 tickers), built `scripts/verify_pinned_entry_vs_backtest.py` to check that `signals_compute.compute_buy_signal(node, as_of=..., price_override=...)` reproduces the real backtest kernel's own BUY decisions when fed the exact historical Open/Close price the kernel used for each real trade entry.
**Method:** for each ticker's real watchlist node, ran the real backtest kernel (`backtester.run_backtest_dispatch`) to get every trade's Entry Time/Entry Price, determined which branch fired (Open at the 9:30/14:30 target hour, or Close otherwise) by matching Entry Price against the bar's real Open/Close, then replayed `compute_buy_signal` at that historical date/price and asserted `signal == 'BUY'`.
**Result — first run, before any fix:** every ticker failed on the large majority of trades (e.g. AGQ 109/129, DPST 104/126) — `⚠️ Possible corporate action ... freezing new signals` fired on almost every replayed entry. Root cause: `compute_buy_signal`'s `prev_close` was computed from the unsliced `df_daily['Close'].iloc[-1]` — the most recent row in the *entire* cached daily frame — instead of `df_daily_prior` (already correctly sliced to `< as_of`/today, the same frame the SMA/Std indicators are computed from). Historical replay was comparing e.g. a 2026-02-02 entry price against 2026-07-21's real close, years of drift away, spuriously tripping the corporate-action discontinuity guard (`detect_price_discontinuity`) on nearly every trade.
**This isn't replay-only — a real latent live-mode bug too:** `df_daily = df.resample('D').last().dropna()` includes today's own partial/in-progress bar if any intraday data already exists for today by the time `compute_buy_signal` runs. So in live use (`as_of=None`), `df_daily.iloc[-1]` could silently be *today's own latest print* mislabeled as "previous close," not yesterday's real close — feeding both the corporate-action guard and `build_reference_table`'s `Overnight %` display a wrong reference on any day this triggers. Not confirmed to have caused a real false-positive freeze live (the corporate-action guard is coarse — needs to land near a known split ratio — so this may have gone unnoticed), but the mechanism is real and was silently present before this fix.
**Fix:** switched `prev_close` to derive from `df_daily_prior['Close']` (correctly sliced) instead of the unsliced `df_daily['Close']`. Also affects two other existing `as_of`-based scripts that were silently exposed to the same bug: `scripts/verify_live_parity.py` and `scripts/live_sim.py`.
**Re-run after fix:** 5/6 tickers 100% clean (DPST 126/126, KORU 77/77, NUGT 34/34, UDOW 72/72, YANG 60/60). AGQ: 128/129 — the one remaining mismatch is a real ~2:1-ratio price discontinuity on 2026-02-02 (prev_close=$285.99 → current=$145.00), almost certainly a genuine AGQ stock split. This is *expected*, not a defect: the backtest kernel has no corporate-action awareness at all (operates on raw, presumably split-adjusted-at-collection-time cached prices), while the live-side guard correctly does — a live/backtest disagreement on a real split day is the guard working as designed, not something to chase further.
**Verdict:** the pinned-entry replay mechanism is verified correct on 5/6 tickers and the 1 remaining "mismatch" is explained, not a defect. Full `pytest tests/` (131 passed) confirms no regression from the `prev_close` fix.
**Follow-up:** none required for Part 4 to proceed to paper-trading/Deliverable 2. If a future session investigates a live corporate-action false-positive/negative, check whether this `prev_close` mechanism (today's partial bar leaking in) played a role.

## 2026-07-22 — Reconciling the 2026-07-16 `auto_adjust=True`/split-guard open question, and a data-traceability discussion
**Hypothesis/question:** raised while discussing whether Yahoo Finance historical data can silently change out from under a past backtest. The 2026-07-16 GDXD entry above found `yf.download()` actually defaults to `auto_adjust=True` (confirmed via `inspect.signature`) and neither `data_manager.py` call site overrides it — contradicting the in-code comment claiming hourly bars aren't retroactively split-adjusted — but left open why the split-guard rescue (built for the KORU incident) was ever needed if Yahoo already adjusts everything.
**Method:** re-confirmed the installed version (`yfinance==1.4.1`) and default (`auto_adjust=True`) directly; reasoned through the fetch/merge flow in `data_manager.py` (`STEP 3`: only the recent `safe_days_to_fetch`-day delta window is re-fetched from Yahoo on each incremental update, then merged with the existing local CSV).
**Result:** `auto_adjust=True` only adjusts *the window being fetched right now* for corporate actions known as of today — it does not reach back and re-adjust rows already sitting in the local cache from a prior fetch. So when a new split happens, the next incremental delta fetch comes back on the new adjusted scale while the old cached rows are still on the prior scale, producing exactly the discontinuity the split-guard (`detect_price_discontinuity`, wired into `data_manager.py`'s merge step) detects and rescales the whole local file to fix.
**Verdict:** the split-guard is not dead code / not solving a fake problem — it's correctly compensating for fetch-boundary staleness, not for Yahoo failing to adjust at all. The 2026-07-16 item's "why didn't auto_adjust prevent KORU" question is resolved: auto_adjust and the split-guard are two different layers (per-fetch adjustment vs. cross-fetch reconciliation), both needed. Because the guard's rescale is a single multiplicative factor applied to the whole series, downstream signals (z-scores, %-based SL/TP/arm triggers, returns) are scale-invariant to it, so past `backtest_cache` alpha numbers should remain reproducible by re-running the same code.
**Real, distinct gap surfaced in the same discussion (not resolved, deliberately backlogged)**: there is no archived/immutable snapshot of the exact cache file bytes that fed any given past backtest — the on-disk CSV is mutated in place on every incremental merge and every split-guard rescale. So "re-run the same code, get the same number" should hold (per the scale-invariance argument above), but literal forensic reproducibility ("what data, byte-for-byte, produced this specific historical result") is not possible today. Discussed two shapes: (a) full immutable/versioned data linked to each `backtest_cache` row (real reproducibility, but a meaningfully bigger lift — schema change touching the whole sweep engine, plus real storage growth), vs. (b) a lightweight append-only mutation-event log (timestamp/ticker/factor/before-after checksum) giving an audit trail without redesigning the pipeline. User's read: doesn't want to over-build now, but flagged this as a "we'll pay for this later" concern rather than a solved one — see `docs/backlog_cache.md`.
**Follow-up:** none scheduled yet; backlogged at low priority pending the user deciding between (a)/(b) above, or a real incident that makes the tradeoff concrete.

## 2026-07-22 — "v6": does parking idle capital in SPY (or any other vehicle) between an EOD exit and the next entry actually beat sitting in cash?
**Hypothesis/question:** raised mid-session as a new strategy variant idea — when a position exits on the trading day's last bar, instead of letting the freed capital sit idle in cash until the next signal, buy SPY at the exit price, hold it continuously (however many days the gap lasts), and market-sell it the instant the next real trailing-buy signal fires (SPY's liquidity means this adds only seconds of friction, modeled as zero slippage). Generalized mid-discussion to: don't assume SPY is the right vehicle — extract every real such gap window across all 10 watchlist-65 tickers and scan the whole cached ticker universe for what would have actually made money parked there ("we had spare capital across ~2 years of real gaps — what would have made money with it?").
**Method:** `scripts/sim_v6_spy_parking.py` (single-ticker) and `scripts/sim_v6_parking_vehicle_sweep.py` (universe sweep). "EOD" = an exit landing on the genuinely last bar on file for its calendar date (not a fixed hour — the exit-check loop can still fire on the 15:30 bar even though new entries stop being checked at 14:30). Used `backtester.run_backtest_dispatch` (the same strategy-aware dispatcher `run_optimization_sweep.py` uses) against each ticker's real active watchlist-65 v5 node — correctly handling that 6 of the 10 nodes are actually `TrailingExitZScoreBreakout`, not `TrailingBothZScoreBreakout` (an assumption the first draft of the script got wrong and had to fix). Extracted 62 real EOD-exit→next-entry gap windows across all 10 tickers (AGQ 9, DPST 15, GDXU 3, HIBL 7, KORU 9, NUGT 3, SOXL 4, UDOW 10, USD 1, YANG 1), then scanned ~1,448 cached tickers (the full existing research universe, including broad-market and crypto-linked leveraged ETFs), pricing each window independently via nearest Close at-or-before the window's start/end timestamp, and compounding each candidate's per-window return across all 62 windows chronologically (a simplification — doesn't model overlapping windows across tickers sharing one real capital pool).
**Result — the core premise does NOT hold**: SPY itself would have *lost* money parked across these exact 62 windows (-5.4% compounded, mean per-window -0.07%, win rate exactly 50%). QQQ also loses (-11.2%). Broad-market alternatives (DIA, IWM, VOO) are all close to flat. A T-bill-like cash proxy (BIL) is also close to flat/slightly negative; SHY near-zero — i.e., real risk-free yield over these specific short windows is indistinguishable from doing nothing, which is itself informative (the current baseline's implicit 0%-during-the-gap assumption isn't obviously wrong for a cash-equivalent). On SOXL specifically (single-ticker check, 4 EOD windows), SPY-parking made SOXL's own compounded return slightly *worse* (614.5% baseline → 590.6% v6, ratio 0.9666).
**The "top 30 by return" leaderboard is very likely overfitting noise, not a real finding**: scanning ~600 tickers with sufficient data against only 62 (correlated, not independent — multiple tickers' windows can span the same real calendar dislocation days) return windows is a classic multiple-comparisons trap. The top hits (TSLQ +141.7%, SARK +131.7%, WEBS +115.0%, BERZ +93.6%, SCO +85.1%, ...) have win rates clustered at 50-61% — not a robust edge, just noise that happened to compound well over a small, correlated sample. None of this should be read as "buy TSLQ between trades."
**Verdict:** the v6 idea as originally framed (SPY beats cash) is not supported by the real extracted windows — if anything, SPY parking would have been a net drag on this exact sample. The generalized vehicle-sweep didn't surface a credible replacement either; its "winners" are a sampling artifact, not alpha. Recommend not pursuing this direction further without either (a) a much larger/independent sample of gap windows (would need many more tickers/years, and de-correlating overlapping windows), or (b) a principled reason to prefer one specific vehicle (e.g., a genuine inverse-hedge rationale) rather than mining the full universe for whatever happened to win.
**Out-of-sample split-check, same session** (`scripts/sim_v6_split_check.py`): split the 62 windows chronologically into two 31-window halves and re-scored SPY/QQQ and the 6 direct SPY/QQQ short-or-inverse candidates (SH/SDS/SPXU/SPXS/PSQ/SQQQ/QID) plus BIL independently in each half. **Every single candidate reverses sign between the two halves** — including SQQQ, which looked best in the full sample (+16.4% compounded) but was +47.4% in the first half and -21.0% in the second, a complete reversal, not a stable edge. SPY itself: -9.3% (h1) vs. +4.3% (h2). Only BIL is close to consistent across halves, and that's because it's near-zero in both (i.e., "doing nothing" is the only thing that held up). **This is a clean, direct demonstration that 62 correlated windows is not enough sample to trust any vehicle pick here** — not just the leaderboard noise flagged above, but the two economically-motivated candidates (SQQQ/QID as a "short the market on bad days" hedge) specifically.
**Verdict (final):** don't act on any vehicle choice from this analysis. The idea needs a much larger, less-correlated sample (many more real gap windows, likely requiring years more live data or a much broader ticker set feeding the gap-extraction step) before it's worth revisiting, and even then the "pick the best backtested vehicle" framing invites exactly this overfitting trap — a future attempt should pre-commit to a candidate with an independent rationale before looking at the numbers, not choose after.
**Follow-up:** none planned. Full per-window detail: `output/v6_gap_windows.csv`; per-ticker detail: `output/v6_spy_parking_SOXL.csv`; full universe sweep: `output/v6_vehicle_sweep_results.csv`; split-check: `scripts/sim_v6_split_check.py` (rerunnable if more gap windows accumulate later).

## 2026-07-22 — HIBL paper trade entered and stopped out in 31 seconds: stale-cache race at market open
**Hypothesis/question:** user noticed a HIBL paper-trading position enter at $104.09 and exit via SL 31 seconds later at $100.68 (-3.28%) — asked "why did it trade, and SL out in under a minute."
**Method:** reconstructed the sequence from `paper_trade_log`, `coverage_events`, and the raw cached CSV (`cache/research/HIBL_1h.csv`). Entry price ($104.09) matched exactly the *prior day's* 15:30 bar Close, not any value in the actual 09:30 session bar (Open=Low=$100.37, Close=$103.69, High=$105.16).
**Result:** `signals_compute._current_price(ticker)` reads `df['Close'].iloc[-1]` from the locally cached CSV with no staleness check at all. The pending trailing-buy's `running_low` was set to $100.37 (the real live price at the moment yesterday's 15:25-15:40 signal window fired, mid-bar, ahead of that bar's eventual $104.09 close). This morning at 09:30:12, `paper_trading.update_paper_buys()` called `_current_price()` before that ticker's first same-day data refresh had landed — the CSV's last row was still yesterday's stale $104.09 close, which cleared the $101.37 bounce trigger ($100.37 × 1.01) and "filled" the paper buy at a price that was never actually tradable at that moment. The next poll's fresh data (real $100.37 open) then immediately tripped the 1% SL.
**Verdict:** real bug, paper-trading-only in actual impact (real trailing-buy orders execute against Schwab's live order book, not this cache, so no real-money exposure existed) — but it also affects two **real (non-paper)** call sites sharing the same function: `active_signals._check_position_exit`'s mid-bar branch (a real position's SL/trailing-sell continuous check) and `_check_limit_fill` (live limit-order fill detection). Neither had ever hit the race live (every account still `dry_run=True`, and no real ticker signal has landed in that exact narrow open-market window yet), but the mechanism was real and reachable.
**Fix:** `_current_price()` now returns `(None, None)` if the cache's last row predates today's date, it's a weekday, and `now.time() >= 9:30` — every one of the 6 call sites already handled `None` gracefully. Independent Opus review confirmed the fix logic is correct and strictly safer than the prior behavior, but flagged a residual gap: on a live trading day, if a *real* position's data refresh genuinely fails (not just the narrow open-race), the guard now silently suppresses that position's intrabar exit check with only a `log_poll` trace, no Slack alert — worth a follow-up alert for that specific case. Full suite: 181 passed.
**Also built this session, prompted by "we need logs of what we're sending to Slack" (there was previously zero persistent record of _post_message content) and "track each price/bar the pollers are hitting":** `signals_db.slack_message_log`/`log_slack_message`/`get_slack_messages` (full text + mode, wired into `signals_blocks._post_message`), and a shared `log_poll()` helper (`signals_helpers.py`) writing `[poll]`-prefixed trace lines to the existing `VERBOSE_LOG_PATH`, wired into every price/bar-consuming decision point identified during this investigation (`_current_price`, `_check_position_exit`, `_scan_pinned_exit_arm`, `_scan_pinned_entry`, `_check_limit_fill`, `paper_trading.update_paper_buys`/`check_paper_sells`).
**Follow-up (not yet built):** (1) Slack alert for the real-position stale-guard suppression case Opus flagged. (2) A canary-node plan was designed and independently reviewed but found to have a real sizing bug (`starting_notional=500` sizes to 0 shares at SPY/QQQ's real price — canaries would silently never fill) — not yet fixed or deployed. (3) Separately, confirmed `active_signals._scan_pinned_exit_arm` only ever reads real (non-paper) `open_positions` — no paper canary, however designed, can exercise the pinned exit-arm path; this is either a real coverage gap worth closing or an accepted limitation, not yet decided. (4) User wants a scriptable extension of `scripts/live_sim.py` to drive `active_signals.py`'s real functions directly (pinned entry/exit, top-up, gap-resize, TIME exit, both entry paths) as a fast, repeatable coverage harness — proposed as the new standard verification step for any future `active_signals.py`/`signals_*.py` change, not yet built.

## 2026-07-23 — Morning Report investigation: a wrong initial diagnosis, two real bugs found, one root cause
**Hypothesis/question:** user reported the daemon restart didn't send a Morning Report to Slack ("no report"). Initial hypothesis floated by the user: maybe the report is gated to skip when no tickers are `mode='live'`.
**Method, and how the diagnosis went wrong before it went right:** checked `send_reference_report` (`signals_notify.py`) directly for mode-gating — found none, so ruled out the user's hypothesis. Found `slack_message_log` (built the prior session) showed a row for the missing report with no caught error, and manually re-invoking the same function from a standalone script succeeded and was independently confirmed via `chat.getPermalink` — leading to a wrong intermediate conclusion that delivery was fine and the issue was on the user's viewing side. This was wrong on two counts: (1) `slack_message_log` was logging *intent* (written before the send attempt), not delivery, so a row's existence never actually proved success; (2) `logs/active_signals.log` (the human-readable daemon log) had no explicit `.flush()` call and was block-buffered, so real evidence from the failed attempt (if any) was sitting unflushed and unobservable on disk the whole time — confirmed by the file's mtime being frozen for 10+ minutes while the daemon's heartbeat proved it was still actively looping.
**Fixed those two observability gaps first** (`buffering=1` on the human log; moved `log_slack_message` to fire after the attempt with a real `error` column) — which paid off immediately: the next real Slack send failed with a confirmable, visible error (`invalid_blocks`, see below) instead of another round of blind guessing.
**Real root cause, once observability actually worked:** `build_reference_table` (`signals_notify.py`) filtered its input to `mode == 'live'` nodes only. Every watchlist node has been `mode='research'` since the 2026-07-20 v5 promotion — so the report had been posting successfully (header, kill-switch status, "No open positions", "Buy Candidates" header) with **zero candidate rows** underneath, for weeks, with no error at any layer. This was exactly the user's original hypothesis, which got wrongly ruled out earlier in the same conversation by checking the wrong function.
**Second bug, exposed immediately by fixing the first:** once all 16 nodes rendered (10 real + 6 newly-added canaries), the message hit 53 Slack blocks — over the hard 50-block-per-message API limit — and was rejected outright (`invalid_blocks`, confirmed live via the now-working error logging). Fixed by collapsing each row's up-to-3 separate `actions` blocks into one (Slack allows up to 5 elements per `actions` block).
**Verdict:** the empty-report bug was real, silent, and had been live for the entire life of the all-research watchlist (2026-07-20 onward) — a genuinely useful report was returning structurally-valid-but-content-free messages the whole time, and nothing in the system would have caught it without this investigation. The block-limit bug would have blocked the fix from working at all had the observability gaps not been fixed first — worth noting as a case where fixing "boring" logging infrastructure was a hard prerequisite for diagnosing the "real" bug, not a side quest.
**Follow-up:** none required — both bugs fixed and independently verified via a real permalink lookup, not just trusting `_post_message`'s return value. Full suite: 181 passed throughout. See `docs/backlog_cache.md`'s 2026-07-23 entries for the mechanical fix detail.

## 2026-08-01 — Does the buy-sizing safety pad create a real compounding drag over many trades? No — already solved by an existing same-day top-up.
**Hypothesis/question:** while sizing 3 new small "real-world coverage pilot" live nodes (HIBL/USD/YANG, `soxl_ira`), noticed `signals_helpers.buy_order_sizing` deliberately under-deploys capital every buy (a 1% safety pad, plus `trail_buy_pct` itself for trailing-buy strategies) to cover fill-price uncertainty, and that real/dry_run sizing compounds off its own realized proceeds (`_last_sale_recovery`). Since a compounding loop multiplicatively reapplies that same ~1-2%/trade shortfall, does this silently erode real returns far below what the backtest assumes, for tickers that trade often?
**Method:** built `scripts/sim_pilot_notional_drag.py`, replaying each of the 10 real v5 nodes' actual historical trade sequence (`backtester.run_backtest_dispatch` against cached OHLC) through the exact production sizing formula (`int(notional // (price × (1 + pad%)))`, next-cycle notional = `shares × exit_price`), compared against the backtest's own idealized full-reinvestment compounded return (`((Return+1).prod()-1)`).
**Initial result (WRONG, see correction below):** at real $50k sizing, several tickers appeared to flip from large backtest winners to real losses purely from this compounding pad — SOXL +794.2% idealized vs a simulated -99.8% (near-total wipeout), HIBL +673.7% vs -58.6%, GDXU +436.3% vs -62.5%, DPST +295.8% vs -5.7%. Two real script bugs were also found and fixed en route (using `trail_buy_pct` as the swept axis for `TrailingExitZScoreBreakout`, which actually sweeps `trail_sell_pct` — silently zeroed YANG's real 17% trailing-exit width; and a hardcoded `z_score_threshold=2.0` that didn't match HIBL's real node value of 1.0) — both real bugs, correctly fixed, unrelated to the eventual wrong conclusion below.
**Correction — the simulation omitted an already-built production mechanism:** `signals_notify._reconcile_fill` (built 2026-07-21, called unconditionally by `_reconcile_buy_fill` on every real fill) already does a same-day post-fill top-up — buys the shortfall between `target_notional` and the real fill notional immediately, at the now-known real fill price, with no pad needed (the pad only exists to cover price *uncertainty*, which is gone once the fill is confirmed). This was missed entirely in the original simulation, which modeled every trade as if the pad's shortfall were never recovered.
**Result (corrected, with top-up modeled):** the "drag" nearly vanishes for every v5 ticker — SOXL 794.2% vs 781.4% (12.8pp gap), HIBL 673.7% vs 649.6% (24.1pp), GDXU 436.3% vs 413.1% (23.2pp), DPST 295.8% vs 260.6% (35.2pp), the rest within 1-15pp. The residual is mathematically bounded to under one share's worth of dollars per trade (the top-up's own floor-division remainder) — consistent with ordinary rounding noise, not a systemic compounding failure.
**Verdict:** no real gap exists; no code change needed. The design change that was about to be built in response (persist an unused-notional "reserve" and fold it into the next trade, or replay full trade history from `starting_notional` each time to derive it statelessly) is unnecessary — `_reconcile_fill`'s existing same-day top-up already achieves the same effect, and does it better (same-day at the known real price, vs. next-cycle at an estimate).
**Process note:** this cost real, avoidable investigation time — the mistake was modeling "what does the current sizing formula do" from first principles (`signals_helpers.py`) without first checking whether a downstream reconciliation step (`signals_notify.py`) already closes the gap. Before building a simulation of current production behavior, grep the full real code path (entry sizing *and* post-fill reconciliation), not just the sizing function in isolation.
**Follow-up:** none needed. `scripts/sim_pilot_notional_drag.py` is committed and reusable (`--watch-id ID... [--top-up]`) if this question ever needs revisiting for a new node.

## 2026-08-01 — Do "gap/no-dip" trailing-buy entries (paying near-worst-case padded price, little/no benefit from a running-low dip) differ in quality from entries that got a discount from waiting? Explored, no consistent pattern, not pursued further.
**Hypothesis/question:** raised while investigating the sizing-pad compounding question above — since `running_low` only ever falls before a trailing-buy bounce-fill, some entries end up paying close to the padded worst-case price (little/no dip captured) while others get a real discount. Does "wait ≤1 bar before fill" (a proxy for no meaningful dip) correlate with worse trade quality, and would a mechanical rule ("after 3 consecutive losses, only re-enter on a gap trade until a win") improve returns?
**Method:** `scripts/export_trades.py::simulate_trail_both_signal_tracked` (extended pure-Python mirror of `_simulate_trail_both` that also tracks `signal_i` per trade, not exposed by the real numba kernel) run against the 4 real v5 tickers that actually use `TrailingBothZScoreBreakout` (GDXU, HIBL, SOXL, USD — the other 6 v5 tickers run `TrailingExitZScoreBreakout`, a direct market-buy strategy with no waiting/bounce phase at all, so this question doesn't apply to them). Computed win rate and return magnitude (mean/compounded) split by gap (wait≤1 bar) vs. normal (dipped first) buckets, a Wald-Wolfowitz runs test on each ticker's full win/loss sequence to check for streak clustering beyond chance, and a direct simulation of the "3-strikes" rule (skip non-gap trades once 3 consecutive losses have occurred, reset on a win).
**Result:** no consistent cross-ticker pattern. Win rate alone was often misleading — HIBL's gap trades win slightly *more* (21.6% vs 20.0%) and have comparable/better mean return (+1.87% vs +1.48%), while GDXU's gap trades win less (15.4% vs 23.7%) *and* are much weaker in magnitude (+0.62% mean vs +5.07%, contributing only +7.6% of GDXU's +436.3%+7.6% total despite being 41% of trades). SOXL is a milder version of GDXU's pattern; USD has no comparison group (100% gap trades). The runs test found no ticker with statistically significant streak clustering (all |z| < 1.96) — the long losing streaks that look dramatic in isolation (DPST 23 straight, KORU 16, HIBL 14) are consistent with plain random ordering given each ticker's low win rate (12-32%), not evidence of a distinct "bad regime" by trade-sequence alone (calendar-time clustering was spot-checked for DPST's 23-loss streak — dense, 2025-02-20 to 2025-04-11 — but not investigated further). The 3-strikes rule, simulated directly against real trade sequences, was net harmful on 2 of 4 tickers (GDXU +436.3%→+48.8%, SOXL +794.2%→+35.8%) and only helped HIBL (+673.7%→+732.6%) — and only because HIBL happens to be the one ticker where gap-trade quality isn't worse to begin with, not because the rule itself is sound.
**Verdict:** not pursued further. The rule doesn't generalize across even this small 4-ticker sample, and picking it based on which ticker it happened to help would be overfitting on n=4. No code change.
**Follow-up:** none planned now ("another research permutation" for later, per the user). If revisited: calendar-time clustering of long streaks (does DPST's dense 2025-02-20/04-11 streak line up with a real SPY/VIX regime?) is the one thread that was flagged but not run.
