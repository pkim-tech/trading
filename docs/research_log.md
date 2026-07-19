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
