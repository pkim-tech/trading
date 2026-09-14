# Watchlist Candidate Checklist

Run this on any ticker before promoting it to `live` (new candidate, or re-checking an
existing one after a macro/regime concern is raised). All checks use only cached hourly
data + yfinance 5-min bars — no broker/live data needed.

## 0. Live-vs-candidate screening check (added 2026-09-08 — mechanical pre-check, run first)
Before spending effort on checks 1-18 below: pull the candidate's and the current live
node's already-EXISTING stored numbers only (`candidate_verification_results`/
`phase4_results` — never trigger a fresh Phase4 scope recompute for this step, that's
real, avoidable cost; see `docs/research_log.md`'s 2026-09-08 entry on why a scope-wide
Phase4 rerun is much more expensive than a single-candidate lookup) — core/addon/drought/
core_both CAGR + worst_neighbor_cagr, side by side. If the candidate doesn't clearly beat
live on the numbers already on file, stop here — don't run the rest of the checklist on a
candidate that isn't actually better. This is a cheap, mechanical screening gate, not a
judgment call each time — a real script for this should exist under `scripts/` (check
`scripts/list_scripts.py --grep` first) rather than being re-derived ad hoc per promotion
review. Numbers used here may be pre-`10f1945`-fix stale for drought specifically (see
that commit) — note explicitly if that caveat applies to a given comparison rather than
silently trusting a stale number, but don't block this cheap step on a full refresh.

## 1. Macro/trend check
Is the underlying trending hard in one direction right now, independent of the backtest's
mean-reversion assumption?
```
.venv/bin/python -c "
import pandas as pd
df = pd.read_csv('cache/<TICKER>_1h.csv', index_col=0, parse_dates=True)
df.index = pd.to_datetime(df.index).tz_localize(None)
daily = df.resample('D').last().dropna()
print('30d return:', (daily['Close'].iloc[-1]/daily['Close'].iloc[-21]-1)*100)
print('90d return:', (daily['Close'].iloc[-1]/daily['Close'].iloc[-63]-1)*100)
"
```
A large sustained move (either direction) in the last 30-90d means every recent "buy the
dip" signal is fighting a real trend, not just chopping around a stable mean — worth
checking trade-level win-rate clustering (early vs. recent trades) before trusting the
full-history win rate as-is.

## 2. Trailing-buy resolution check (`TrailingBothZScoreBreakout` only)
`scripts/verify_trailing_buy_resolution.py` — re-detects every recent live-watchlist
bounce-entry signal using 5-min bars (real, continuous-ish tracking) and compares against
what the hourly-bar backtest kernel (`_simulate_trail_both`) would have caught. Run across
the whole active watchlist (live + research) in one shot:
```
.venv/bin/python scripts/verify_trailing_buy_resolution.py
```
Read the per-ticker summary at the bottom:
- **`mean` price diff** — how much worse (+) or better (-) a continuously-tracked fill
  would land vs. the backtest's hourly assumption. Within ~±0.5% is noise.
- **`median_intrahour_range_pct_of_trigger`** — the ticker's typical intra-hour High-Low
  swing divided by its `trail_buy_pct`. A ratio near/below 1 means the trigger is wide
  enough that hourly resolution barely matters. **Ratio > ~1.5-2 is a real flag** — the
  ticker is volatile enough relative to its own trigger that intra-hour swings can cause a
  premature/worse fill the hourly kernel doesn't model. (Confirmed 2026-07-12: SOXL at
  ratio 3.57 showed a real +1.81% mean fill-price penalty; TQQQ/NUGT in the 1.5-1.75 range
  showed +0.37-0.84%; everything under ~1.0 was within noise of 0%.)

If a candidate's ratio is high, either accept the known drift, or consider whether its
`trail_buy_pct` should be widened to better match its actual volatility.

## 3. Trailing-sell resolution check (`TrailingBothZScoreBreakout` only)
`scripts/verify_trailing_sell_resolution.py` — same idea as check 2, but for the exit
side: once the trailing-sell arms (price clears `arm_sell_pct`), re-detects the
peak/trail_stop crossing using 5-min bars and compares against what the hourly-bar
kernel's trailing branch (`_simulate_trail_both`) would have caught. Also run across the
whole active watchlist in one shot:
```
.venv/bin/python scripts/verify_trailing_sell_resolution.py
```
Same reading as check 2 (`mean` price diff, `median_intrahour_range_pct_of_trigger` —
here measured against `trail_sell_pct` instead of `trail_buy_pct`). Sign convention
differs from the buy check: negative mean diff means the 5-min exit fills *lower* (worse)
than the hourly kernel assumed. (Built 2026-07-13: all 11 watchlist tickers at parity,
mean diff -0.17% across 21 matched exits — unlike the buy side, live trailing-sell is
already monitored continuously by `active_signals.py` itself rather than handed off blind
to a broker order, so this check mainly validates the *backtest's* hourly-bar exit
modeling, not a live-execution gap. LABU showed -4.6% on a single sample — not enough
data yet to call it a real outlier, worth re-checking as more trades accumulate.)

**Note on both trailing-buy/sell resolution scripts**: `max_hold_hours` counts hourly
*bars*, not calendar hours (bars only exist ~7/trading day), so any cutoff-time math for
the 5-min replay must look up the real bar timestamp (`timestamps[entry_i + max_hold_hours]`)
rather than adding `timedelta(hours=max_hold_hours)` — the latter cuts the replay off
days early for longer holds and produces fabricated "ran out of data" results. Both
scripts had this bug until fixed 2026-07-13.

## 4. Win-rate stability check (train/live split)
**GT/v6 candidates**: a GT-native equivalent of this check now exists —
`run_optimization_sweep.build_candidate_report_ground_truth`'s check 4 (added `ff8372e`,
2026-08-22), run automatically for `derive_phase25_candidates_ground_truth`'s shortlisted
candidates. Use that instead of the `run_backtest_v110` replay below for a v6 candidate;
the manual steps below still apply as-is for v5/v5.1.

Is the backtested win rate real, or an artifact of the older (training) portion of the
history — i.e., would a strategy that stopped working recently still show a good
full-history win rate? Replay the node's trades (same params as the live watchlist entry,
via `run_backtest_v110`/etc.), split chronologically 70/30, and compare:
```
tdf = <dataframe of replayed trades, oldest first>
n = len(tdf); cut = int(n * 0.7)
early, late = tdf.iloc[:cut], tdf.iloc[cut:]
print('early 70% win rate:', early['Result'].isin(['WIN','TWIN']).mean() * 100)
print('late 30% win rate:', late['Result'].isin(['WIN','TWIN']).mean() * 100)
```
A late win rate close to the early one means the edge isn't fading. Also eyeball the last
handful of trades directly (not just the aggregate) — a run of full stop-losses clustered
at the end is a real warning sign even if the aggregate late-window win rate still looks
fine (found for AGQ, 2026-07-12: 84% early vs. 81.8% late overall, but 2 of the last 4
trades were full -15% SL hits, both landing in the same recent downtrend window).

## 5. Live position hold-% / P&L check
For anything currently open, not just candidates: `python scripts/open_positions_status.py`
for entry price/time/shares, cross-referenced against the reference report's current
price and `hold=Xh/Yh` (hours held vs. `max_hold_hours`) and arm-% distance. Gives real
unrealized P&L per position and how close each is to arming/timing out — cheap gut-check
before deciding whether a "this looks bad" worry is about one ticker or the whole book
(found 2026-07-12: AGQ was the only red position out of four open; HIBL/EDC/SOXL were all
solidly positive).

## 6. Data-integrity check (stock splits)
`scripts/check_stock_splits.py` — queries yfinance's authoritative split history and flags
any split landing inside the ticker's cached date range. A missed split silently corrupts
the cached price series (huge fake gap/spike), producing phantom outlier trades and
inflated alpha. Caught real cases historically (UVIX, NBIZ). Cheap, run before trusting
any candidate's backtest numbers, not just at promotion time.

## 7. Fill-logic optimism check (`TrailingBothZScoreBreakout`/`TrailingBuyZScoreBreakout`)
`scripts/export_trades.py`'s `simulate_trail_both_ohlc_aware` — re-simulates entries
without assuming the best-case Low-before-High ordering within each hourly bar (the
standard kernel can't know which came first within an hour and picks the favorable
order). Quantifies how much a node's on-file return is overstated by that optimism.
Found historically to matter a lot for some tickers (SOXL's on-file return was ~2x
overstated, 7007%→3591%) — worth a spot-check on any candidate with an unusually strong
number before trusting it at face value.

## 8. Trade-count fluke check
**GT/v6 candidates**: GT-native equivalent exists — `build_candidate_report_ground_truth`'s
check 8 (`ff8372e`), run automatically for shortlisted candidates. Use that for a v6
candidate; the manual check below still applies as-is for v5/v5.1.

Before trusting a "best alpha" node, check whether it's actually driven by a single
outlier trade (`trades` column at or near 1 for the winning grid cell) rather than a
real repeatable edge. Recurring failure mode in sweep results (e.g. UVIX had thousands of
`trades=1` rows driving misleadingly high headline alpha).

## 9. Same-day-block sensitivity check
**GT/v6 candidates**: confirmed genuinely out of scope for now — `run_backtest_ground_truth`/
`_simulate_trail_ground_truth` have no `same_day_block` parameter at all (confirmed while
building the GT candidate-report, `ff8372e`). Porting it is deferred as low priority (user's
call, 2026-08-22 — `same_day_block` has never actually produced a useful signal
historically). This check does not run for v6 candidates; only applies to v5/v5.1 below.

How much of a node's edge depends on capital that the real cash-account same-day-re-buy
rule (`schwab_safety.py`'s same-day-block, enforced live) would actually block — i.e. a
fresh signal on the same calendar day as that node's own prior exit? Run
`backtester.run_backtest_v110` with `same_day_block=True` vs `False` on the same node
and compare both **trade count** and **robust alpha** (`MIN(possible,pessimistic,certain)`),
not just one or the other — the two can diverge sharply (a small trade-count hit can
still gut alpha if the blocked trades happen to be the strongest ones). See
`scripts/run_v4_backfill_sweep.sh`'s `same_day_block` kernel note for the underlying
feature. Found 2026-07-16: GDXD's best SL=1% node lost only ~18% of trades (308→251)
under blocking but **retained only 7.2% of robust alpha** — most of the edge lived in
exactly the trades the real same-day rule would block.

## 10. Same-day-collision stability check (70/30 split)
**GT/v6 candidates**: unresolved — depends on check 9, which doesn't run for GT (see
above). Not scoped whether this check should be dropped for v6 or given its own GT-aware
form; don't run this check for a v6 candidate until that's decided.

A same-day-block sensitivity number (check 9) is a single aggregate ratio — this checks
whether that vulnerability itself is stable over time or concentrated in one window.
Chronologically split the node's trades 70/30 (same method as check 4) and compare each
half's *own* same-day-block retention independently, not just the full-history number.
A node that's fine early but collapses late (or vice versa) means the aggregate ratio
from check 9 is hiding a regime-dependent effect, not a stable structural property of
the ticker.

## 11. Max drawdown check
**GT/v6 candidates**: GT-native equivalent exists — `build_candidate_report_ground_truth`'s
check 11 (`ff8372e`), run automatically for shortlisted candidates. Use that for a v6
candidate; the manual `v4_max_drawdown.py` check below still applies as-is for v5/v5.1.

True peak-to-trough max drawdown across the node's full compounded equity curve
(`scripts/v4_max_drawdown.py`) — not just the longest consecutive-loss streak, since a
drawdown can also build from a mix of wins-that-don't-recover-the-prior-peak and losses.
Exists to put a real, calibratable number on "can I actually stomach this" — a gut-check
against your own known risk tolerance (e.g. a real portfolio drawdown you've lived
through), not just an abstract backtest stat. Found 2026-07-18: every watchlist ticker's
v4 (SL=1%, open_check) node has a worst-case historical drawdown between -5.9% (YANG) and
-23.8% (SOXL) — all comfortably under a stated -50% tolerance, with SOXL/DPST/HIBL sharing
the same real Aug-Oct 2023 drawdown window (a shared macro event, not independent bad luck
per ticker — worth remembering these aren't fully diversified from each other).

## 12. Current-drawdown-vs-worst-case calibration check
Where does the node sit *right now* relative to its own worst-case history
(`scripts/v4_max_drawdown.py`'s equity-curve-peak tracking, evaluated at the latest trade
instead of just the historical max)? Cheap real-time gut-check, especially valuable during
a live stressful stretch (e.g. a real sector selloff) when it's easy to conflate "this
feels bad" with "this is actually bad by the numbers." Also useful to run the same
calculation against the *currently live* node (whatever `watch_list` is actually running,
e.g. old v3.x params) for direct comparison — found 2026-07-18, mid semiconductor selloff:
SOXL's v4 (SL=1%) node was sitting at only -3.0% current drawdown while the actual live
v3.x node (SL=15%) was at -33.8% (and GDXU's live v3.x node was at -55.7%, its all-time
worst point, at that exact moment) — the same real-world price action producing wildly
different strategy-level pain depending on stop width.

## 13. Walk-forward (N-fold out-of-time) consistency check
**GT/v6 candidates**: GT-native equivalent exists — `build_candidate_report_ground_truth`'s
check 13 (`ff8372e`), run automatically for shortlisted candidates, using
`GT_ROBUSTNESS_CAGR_MIN` (20%) as its fragility bar rather than robust-alpha. Use that for
a v6 candidate; `walk_forward_check.py` below still applies as-is for v5/v5.1.

Generalizes check 4's single 70/30 split into N (default 5) equal chronological calendar
windows across the ticker's full cached history (`scripts/walk_forward_check.py`), and
reports robust-alpha (`MIN(possible,pessimistic,certain)`) independently per window, each
against SPY's *own* return over that specific window (not one blended full-history
benchmark). Exists because a single train/test split can't distinguish "real robust edge"
from "got lucky on whichever window landed after the cut" — found 2026-07-18: KORU's v4
node showed a >5x out-of-sample *improvement* on a single 70/30 split, traced to one
outlier trade plus a favorable recent-selloff test window; a single split has no way to
flag that as fragile. Splitting into 5 windows instead showed the fuller picture: KORU's
alpha was positive and real (51-238%) across every one of the four earlier windows too,
with the 900%-alpha recent window as a genuine standout rather than the *only* good one —
stronger, more legible evidence than the single split gave either way.
Run: `.venv/bin/python scripts/walk_forward_check.py TICKER [TICKER ...] [--folds N]`.
**What to look for**: any negative-alpha fold at all (a real regime where the node lost to
SPY, not just underperformed its own average), and how much the min/max fold alpha spread
(dispersion) — a node with all-positive, low-dispersion folds is real evidence of a
repeatable edge; a node with high dispersion or any negative fold needs more scrutiny
before promotion. First full 19-ticker run (2026-07-18, all watchlist + screened
candidates, all against v4 SL=1%/open_check nodes): **14/19 had zero negative folds**
(AGQ, DUST, EDC, GDXD, GDXU, HIBL, KORU, LABU, NAIL, SOXL, TQQQ, USD, YANG, ZSL); 5 had
exactly one negative fold (DPST, NUGT, RETL, UDOW, UVIX) — worth a closer look at which
specific window went negative for those five before treating them as validated.
No re-sweep, no `backtest_cache` schema change — same "run the backtest once, slice the
already-computed trade list" approach as check 4/10, just generalized from 2 windows to N.

## 14. Config-drift baseline (REQUIRED ACTION, not just a check — do this at the moment of promotion, not before)
Unlike checks 1-13 (analysis to decide *whether* to promote), this is a mandatory step
*at* promotion time, immediately after flipping a node's `mode` to `live`: verify it has
a `staged_test_config` row (`SELECT wl_id, ticker, scenario_role FROM staged_test_config
WHERE wl_id=?`). If not, run `.venv/bin/python scripts/seed_baseline_config.py` (seeds
every `mode='live'` node missing one — safe, skips nodes already staged, won't overwrite).

Without this row, the node is invisible to `signals_invariants.py`'s daily config-drift
check (`check_staged_config_matches_expected`/`print_all_live_node_state`) — the tripwire
that catches a live node's real risk parameters (fixed_sl, z_score_threshold,
trail_sell_pct, etc.) silently diverging from what was intended (a bad manual DB edit, a
migration side-effect, a stale copy-paste value). A node with real capital and no baseline
row isn't "less protected" — it has **zero** config-drift protection from the moment it
goes live until someone happens to notice. **Found 2026-08-04 (very late)**: HIBL/USD/YANG
(wl_id 154/155/156, flipped live 2026-08-03, ~$6,000 combined real notional) went a full
day+ with no baseline row — `signals_invariants.py` ran clean the whole time because it
only checks nodes that already have one, not "every live node." Don't rely on a clean
`signals_invariants.py` run alone to mean full live coverage.

## 15. Corporate-action / reverse-split sponsor check (REQUIRED ACTION, do this at promotion)
Identify the candidate's ETF/ETN sponsor/issuer (Direxion, ProShares, Volatility Shares,
Bank of Montreal/MicroSectors, or other) and check that sponsor's own press-release page
for this ticker's reverse-split history — leveraged/inverse products decay and split far
more often than plain-vanilla equity ETFs, and each sponsor publishes structured, dated
advance-notice announcements (confirmed 2026-08-14 for all 4 sponsors covering the then-
current real-capital universe: Direxion `direxion.com/press-releases`, ProShares, Volatility
Shares `volatilityshares.com/news/`, BMO/MicroSectors `microsectors.com/insights` +
`newsroom.bmo.com` — real example: ETHU's 2025-03-26 announcement gave 14 days' notice
before the 2025-04-09 effective date). This groundwork feeds the live-trading corporate-
action monitoring design (found via a real false-positive freeze on NUGT, 2026-08-14 —
`signals_compute.detect_price_discontinuity`'s price-ratio heuristic mistook an ordinary
46% rally for a split; the fix in design uses each sponsor's real announcement instead of
a price guess). A candidate whose sponsor isn't yet a known, checked source has **zero**
advance-warning coverage for a real split until that sponsor's page is added to the
monitoring list — record the sponsor at promotion time even before the monitoring
mechanism itself is built, so the gap is visible and trackable rather than silent.

## 16. Wash-sale check (REQUIRED ACTION, do this whenever a promotion moves a ticker between accounts of different tax status)
Wash-sale rules apply at the SECURITY level across every account the taxpayer holds, not
by this project's internal node/wl_id bookkeeping — a loss on the same ticker in a
*different* watch_list node/account still counts if it was a real taxable sale. The risky
direction specifically is a **taxable loss (brokerage) followed by a same-security
repurchase in an IRA/Roth within 30 days** — that permanently disallows the loss (worse
than a normal wash sale, which just defers it). The reverse direction (an IRA/Roth-side
loss followed by a taxable repurchase) has no tax consequence, since an IRA/Roth sale
never generates a reportable capital loss in the first place — nothing to disallow.
Procedure: for any ticker whose promotion account differs from its current live account's
tax status (taxable `brokerage` vs. tax-advantaged `ira`/`roth`/`soxl_ira`/`sep`), pull
`trade_log` for that ticker across ALL accounts (not just the one node being replaced) for
recent closes, and check whether a real taxable-side loss sits within 30 days of the move.
Found 2026-08-19: a promotion pass initially checked only the exact wl_id being replaced
and concluded "different node, doesn't matter" — wrong; the correct check is ticker-wide.

## 17. Open positions / open orders check (REQUIRED ACTION, do this immediately before any live flip)
Query `open_positions` AND `pending_buys` for the exact `wl_id`(s) about to be
reconfigured or replaced — not just whether the ticker is flat. A resting order placed
under the OLD config (trail_buy_pct, running_low, etc.) left mid-flight when the node's
params change creates a real mismatch between what's actually resting at the broker and
what the node now believes its own config is. Found 2026-08-19: a 12-ticker promotion
pass's "no open positions" check only covered 9 of the 12 tickers on the first pass (the
other 4 -- AGQ/SOXL/KORU/DPST -- were checked separately after the gap was noticed), and
even then only checked `open_positions` (filled), missing that SOXL and DPST both had
real resting trailing-buy orders placed THAT SAME DAY in `pending_buys`. If a node has a
resting order, hold that specific node's flip until the order resolves (fills or is
cancelled/times out) rather than reconfiguring underneath it -- proceed with the rest of
a multi-ticker promotion pass in the meantime if the other nodes are clear.

**Extended 2026-08-22 (ground-truth kernel rebuild plan, adversarial review) for
strategy-version sunsets, not just param reconfigs**: when a promotion replaces a node's
underlying strategy/kernel version entirely (e.g. a v5/v5.1 node sunsetting to a v6 pick,
not just a parameter tweak on the same version), also check `watch_list` itself for the
ticker -- confirm the old node is correctly archived (not left `state='live'` alongside
the new one, no duplicate/orphaned rows for the same ticker), in addition to the
open_positions/pending_buys check above. Orders, positions, and watch_list rows are all
interrelated state that can drift out of sync specifically at a strategy-version
transition, not just a config-param change.

**Extended 2026-09-13 (real 7-ticker promotion pass) — sizing continuity check, do this
for EVERY promotion that changes the node's `version` and/or `strategy`, not just the
open-position/order check above**: `signals_helpers._last_sale_recovery` sizes the next
real buy off the node's own trade_log history, matched by `(ticker, strategy, version,
window, account)` — a promotion that changes `version` (near-universal, since it's tied
to the sweep campaign) or `strategy` (e.g. TrailingBoth→TrailingExit) means the NEW node's
lookup will never match the OLD node's real trade_log rows, so the very next buy silently
falls back to the flat `starting_notional` instead of compounding off the ticker's real
last-sold proceeds — exactly the "reset to an idealized size" behavior `_last_sale_recovery`
exists to avoid. Query the OLD node's real last **closed** trade (`SELECT exit_price,
shares FROM trade_log WHERE ticker=? AND exit_price IS NOT NULL ORDER BY id DESC LIMIT 1`
— note the `exit_price IS NOT NULL` filter: the most recent row can be a still-open
position with NULL exit fields, which looks like "no trades" if you don't filter it out,
found live 2026-09-13 for DPST/HIBL/KORU) and compare its real proceeds
(`exit_price * shares`) against the flat `starting_notional` floor. If they differ
meaningfully, decide explicitly whether to carry the real number forward via
`signals_db.set_starting_notional_override_once(wl_id, value)` (auto-clears itself after
the next real fill, normal compounding resumes automatically) or accept the reset —
don't let it default silently either way. **Sanity-check the number before using it**:
a proceeds figure landing suspiciously far below the ticker's normal sizing (found live
2026-09-13: HIBL $1,003.15 and KORU $2,194.20, both far under their real ~$10k target
sizing, one of them landing on the exact date of a real stock split) may reflect a partial
fill, a split-distorted price/share-count, or genuinely stale history — worth a second
look before trusting it as "the real current capital," not just taking the query's
output at face value.

## 18. Automation-scope check (REQUIRED ACTION, do this at the moment of live promotion)
Confirm the ticker is actually in `.env`'s `SCHWAB_AUTOMATION_TICKERS`
(`schwab_safety.AUTOMATION_ENABLED_TICKERS` at runtime) — `state='live'` alone does NOT
mean automated order placement works; a ticker missing from this list still gets real
Slack signal alerts but every automated BUY/SELL attempt is blocked
(`automation_blockers_other_than_node` returns `"{ticker} not in automation pilot scope
(manual-only)"`), silently downgrading a "live, automated" promotion to "live, manual-only"
with no error anywhere. **Real, recurring gap-shape — 3+ confirmed instances**: CURE/TMF/
ERX (2026-08-16), the FAS-missing-from-list bug, and OILU (2026-08-25, found live the same
day it was promoted alongside ETHU — ETHU was added to the list, OILU wasn't, same
promotion batch). Add the ticker to `.env` and **restart the daemon** — the list is read
from the environment once at import time, so an `.env` edit alone does not take effect on
a running process.

## Methodology notes (not standalone checks, but keep in mind while running the above)
- **Compare same node, not best-of-grid**, when checking whether a kernel/logic fix
  changed a ticker's numbers — re-optimizing across the whole grid after a fix confounds
  "did the fix help" with "did we find a different sweet spot."
- **Stop-loss width changes**: judge by total compounded return across all trades, not
  whether one specific losing trade would have survived a wider stop — a wider stop can
  look better on the trade that prompted the question while being worse in aggregate
  (more capital tied up, bigger average loss).
- **Already investigated and rejected**: Hurst exponent / ADF stationarity as entry
  filters (2026-06-28/29) — thorough research, concluded not actionable (lag problem,
  weak/inconsistent signal). Don't re-litigate without new information.

## When to run this
- Checks 1-13: before flipping any ticker `research`→`live`.
- **Checks 14-15: at the moment of promotion itself, not before** — the flip and the
  baseline seed (14) / sponsor identification (15) should happen together, same session,
  so a live node is never left unprotected on either front.
- **Check 16 (wash sale): whenever a promotion moves a ticker's account across the
  taxable/tax-advantaged boundary** — check before the flip, ticker-wide across accounts.
- **Check 17 (open positions/orders): immediately before flipping each specific node** —
  re-check right before, not earlier in the session, since a resting order can appear
  between when the promotion list was decided and when the flip actually runs.
- Whenever a live ticker's live behavior seems to be diverging from backtest expectations
  (the AGQ momentum discussion, 2026-07-12, is what prompted writing this down).
- Not needed on every session — this is a promotion/investigation gate, not a routine poll.
