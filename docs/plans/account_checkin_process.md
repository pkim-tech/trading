# Account check-in: catch-up review (not a code change)

## NORTH STAR (user's explicit framing, 2026-08-14 evening — governs everything below)

**"Trades must equal backtest" is the single most important theme.** If real execution
diverges from what the backtest kernel would have done for the same real price action, that
is a terrible mistake somewhere — not an anomaly to explain away, not something to shrug off
as "close enough." This is the actual point of the test harness/tight-state-control priority
discussed tonight: the harness exists specifically to catch this divergence, and today's
same-bar re-entry bug (RETL doing something the kernel structurally cannot do) is a live
example of exactly the failure mode this principle exists to prevent.

**Why this is THE priority, not just a priority**: days of real work (sweeps, robust-alpha
ranking, cliff-safety, walk-forward validation, the whole candidate-selection pipeline) go into
picking a node's config. All of that investment is worthless if live execution doesn't
faithfully carry out the plan that was so carefully derived — a divergence here doesn't just
cost today's trade, it invalidates the reason the node was trusted with real capital at all.
Execution fidelity is what makes the upstream research work mean anything in practice.

Practical implications for this plan:
  - **Part 2 sub-part 2** (today's activity vs. backtest parity) and **Part 3** (the whole
    proof/coverage integrity check, especially sub-parts 2-3, `paper_vs_backtest_reconcile.py`
    and `verify_live_parity.py`) are not optional nice-to-haves — they are the direct,
    concrete instrument of this North Star. If either ever shows a real mismatch, that's a
    stop-everything finding, same severity class as today's same-bar cooldown bug.
  - This principle is bigger than this one plan — it belongs in `docs/automation_principles.md`
    (queued documentation follow-up, not done tonight — see below) as a standing engineering
    rule, not just something checked in this one report.

---

## FINALIZED STRUCTURE (superseding the draft below — kept for context)

**Part 1 — Log warning scan** (stands alone): grep `logs/active_signals.log` for `⚠️`
(excluding `schwab_stream` lines), scoped to today's 9:30–16:00 ET trading-hours window. If
empty: report "OK, no warnings today." If not: list each for review.

**Part 2 — State of the real live tickers** (notional > $5,000 only — AGQ/ETHU in
`brokerage`, DPST/NUGT/SOXL/SOXS in `ira`, DFEN/GDXU/KORU in `roth`; excludes test-live nodes
like CURE/ERX and all canary/paper/dry_run nodes), with these sub-parts:

  1. **State reconciliation** — does local DB state (open_positions, pending orders) match
     the real broker state (actual open orders/positions at Schwab) for these tickers/accounts.
  2. **Today's transaction log / activity vs. backtest parity** — every real trade/order event
     today for these tickers (including any add-on leg activity, folded in here rather than
     separate), checked against what the backtest would have predicted for the same real price
     action.
  3. **Real portfolio state** — current holdings/cash/value per account (`brokerage`/`ira`/`roth`).
  4. **Margin/buying-power exposure** — current real margin headroom, called out specifically
     because of today's real `addon_buying_power_check` reservation-math alert on `brokerage`
     ($20k cash-vs-buying-power gap) — don't just report position state, report actual usable
     margin too.
  5. **Today's P/L by ticker vs. benchmarks** — each real ticker's daily P/L vs. SPY and TQQQ's
     daily performance.
  6. **Per-ticker performance since 8/12** — 3m/6m/12m/YTD/1yr/2yr/all-time, real data only
     (`trade_log`, no simulation/backtest reconstruction). **Resolved**: any window with no
     real history yet renders as `-`, plus a **"since inception"** row (real performance over
     whatever short window each ticker/account has actually been live) as the one number that
     matters right now.
  7. **Portfolio-level performance**, same time windows + "since inception" row as #6, blended
     across the real account.

---



## Context
User got home from work and wants to "check in on the account." Through discussion, this
isn't a live-status check (daemon-up-right-now doesn't matter post-close, resting stops are
broker-side and daemon-independent) — it's a **retrospective catch-up**: the user wasn't
watching Slack during the trading day, so the real question is "what happened today, real
capital, that I might have missed while away." This is a read-only review, not an
implementation task — no code changes involved.

## Scope (read-only, all via existing scripts/DB queries, no code written)

1. **Real open positions** — across `brokerage`/`ira`/`roth`/`soxl_ira`, current state and
   protection status (does each have a real resting SL/trail order at the broker right now).

2. **Retrospective scan of today's trading-hours window (9:30–4:00 ET)** in
   `logs/active_signals.log` for any error/failure/retry lines — surfaces anything that broke
   intraday that the user wasn't present to see live.

3. **`trading_incidents` table** — anything logged today (the real incident-ticket mechanism
   already used earlier tonight for the RETL/daemon-outage investigation).

4. **Real order activity today** — fills, rejections, any real (non-canary/non-paper) order
   placements — a plain "what actually happened today" summary across real accounts.

5. **Unexplained coverage deviations touching real (non-canary) nodes specifically** — the
   canary-only "didn't cross threshold" misses aren't relevant here; only ones tied to real
   capital matter for this check.

## Liquidity/market-impact signal (added during discussion, refines the "trim" backlog item)

Original framing (1% of ADV in dollars) checked against real data — SOXL alone is ~$76-136M
depending on snapshot, two orders of magnitude above real current account size, so a static
ADV-percentage threshold is not a binding constraint yet and won't tell us anything useful.

**Real mechanism instead**: use each real trade's already-tracked `entry_drift_pct`/
`exit_drift_pct` (`trade_log`) — both BUY and SELL fills, not just entries — as direct
empirical evidence of execution slippage/market impact. Rather than a flat percentage picked
in the abstract, the right threshold is self-calibrating: compare today's fills against that
specific ticker's own historical drift distribution, since normal drift varies by ticker
liquidity (a thin name may normally run higher spread-driven drift than SOXL).

**Placeholder threshold, provisional**: 0.5% adverse drift (either direction, BUY or SELL) as
the flag point, to be refined once real per-ticker historical drift distributions are actually
pulled and looked at — not committed as a final number.

**Alert-only, not automatic** (user's explicit call, accepting the real cost — "if we lose 1
set of trades it is what it is") — same detection-only-decide-later pattern already used
elsewhere in this project (`_log_pre_action_state_verification`, `record_node_streak`, both
monitor-only, never auto-block/auto-adjust). This check surfaces "today's drift looked
abnormal for this ticker" as a flag for a human to review — it does NOT automatically override
the next BUY's notional or take any sizing action on its own. Whether that means trimming,
capping, or it was a one-off with no real meaning is a human judgment call, not something to
automate.

**Escalation threshold**: 2 max abnormal-drift alerts per ticker per day — beyond that, treat
it as a real, not one-off, signal (exact escalation action not yet defined — still just more
prominent flagging, not automatic action, per the above).

**Operational answer to the timing-risk concern** (raised earlier — does alert-only miss the
window before the next real signal fires): user's real plan is to manually reset/adjust the
node's notional when they get home from work, **even if a position is already open** at that
point — not contingent on catching it before the next signal. So the "missed window" risk is
accepted and handled by a deliberate later manual step, not by needing same-day real-time
intervention.

This is still the real trigger condition informing the "trim" backlog item's design (when
should a human even think about overriding next-buy notional), just not wired to act on its
own. Separately, still flagged as a longer-term open question (not for tonight): a hard
max-notional cap per node, since at large enough scale (~$2M+ in a name like SOXL) no sizing
formula alone solves real market-impact risk — trim only throttles the next buy, it doesn't
cap how large a node can compound to in the first place.

## Cash movement (in/out) — new Part 2 sub-item, refines the 2026-08-12 tax-cycle decision

Connects directly to the already-decided annual tax cycle (`backlog_resolved_recent.md`,
2026-08-12: reserve 55-60% of profits, year 1 is a wash since it also funds year 2's safe-harbor
reserve) — this adds the actual cash-movement mechanics that decision didn't specify.

**Core metric needed**: "necessary cash to fulfill position requirements" — the real minimum
cash that must stay in the account to support open positions/margin requirements (reuses
existing real margin-calc logic, e.g. `schwab_client.get_leveraged_buying_power`/margin
requirement checks in `schwab_safety.py`, not something new to build). Everything above that
minimum is the real candidate for sweeping out.

**OUT flow** (mostly tax-driven, in the margin/taxable account — `brokerage`): happens ~once a
year, near year-end, done properly so it captures essentially all real profit for that year.
Amount depends on year:
  - **Year 1 of profitability**: 100% of profit swept out (covers tax + safe-harbor estimates
    for year 2 — matches the existing "year 1 is a wash" decision).
  - **Year 2 and beyond**: 60% swept out (tax + estimates + "a little pocket change" reserve),
    40% stays reinvested.
  - **A losing year**: nothing swept out — no profit to reserve against.

**IN flow** (expected rare, "fingers crossed"): after real tax liability is actually
calculated/known (not just estimated), any reserved-but-unneeded cash can be moved back in as
new capital. This is a genuinely new piece, not covered by the 2026-08-12 decision.

**Report requirement**: user wants all of these numbers surfaced in the account check-in —
necessary cash for position requirements, current excess/sweepable cash, cumulative
reserved-out this year, projected year-end reserve. **Explicitly text-only for now** — user's
long-term goal is a proper web dashboard ("a better Schwab app"), but that's future scope, not
part of this plan.

## Part 3 — Proof/coverage integrity check (real reason: Part 1's log grep doesn't catch
## everything — today's RETL same-bar re-entry bug was never an error/warning, just
## unexpected/undesired behavior a log scan alone would never surface)

**Sub-part 1 — Coverage/Grid trend, code-change-aware.** Not just a point-in-time snapshot
(target: as close to 100% verified-live as possible) but a **historical trend** — improving or
aging out over time. Key question per row: when was the last major code change touching this
path? If none recently, stale-looking proof is fine (nothing to re-prove). If there was a
change, proof from *before* that change shouldn't count toward current confidence. Directly
reuses tonight's `coverage_proof_matrix.py`/`coverage_regression_watch.py` (already built) and
is the same underlying idea as the revived "snooze until code change" backlog item — just
framed here as a report metric (trend line) rather than a snooze mechanism.

**Sub-part 2 — Reconfirm paper-trading execution against backtest.** Reuses
`scripts/paper_vs_backtest_reconcile.py` (already exists) — reconciles real paper-trading
activity against a fresh backtest replay, flags direction mismatches/trade-count gaps.

**Sub-part 3 — Reconfirm live execution against backtest.** Reuses `scripts/verify_live_parity.py`
(already exists) — compares the real live-orchestration code (`compute_buy_signal`/
`check_sell_condition`) against the Numba kernel bar-by-bar. Deeper than sub-part 2: checks
whether the live code path itself still agrees with the kernel's decisions, not just whether
real outcomes matched — catches silent drift between the two codebases after either changes.

## Part 4 — Are we ready for the next day? Do we need to make any adjustments?

Forward-looking readiness, confirmed as its own part (not folded into Part 3's proof-integrity
check). Concretely:
  - For each real open position: its actual next trigger (arm level, SL price, trail-stop
    level) and how far current price sits from it.
  - For each real flat ticker: how close it is to a fresh entry signal.
  - Operational readiness: does the daemon need a restart before tomorrow's 9:30 open, is a
    Schwab token reauth due soon (per the user's Sunday-reauth cadence), anything else that
    would block normal operation tomorrow.
  - Any adjustments flagged as needed from Parts 1-3 that haven't been acted on yet (e.g. a
    drift alert from Part 2 still awaiting the user's manual notional reset when they got
    home, an unexplained coverage deviation still needing `--explain`).

**Explicitly out of scope for this plan** (real idea, but belongs to a different process):
portfolio-level concentration/correlation risk across real tickers (sector/leverage overlap,
e.g. SOXL/SOXS or NUGT/JNUG/GDXU clustering) — user's call: this belongs in portfolio
construction / quarterly adjustment review, not the daily account check-in. Connects to the
existing "quarterly node reviews/rebalance" idea raised earlier tonight (see backlog).

## Explicitly excluded
- Daemon current-liveness check (doesn't matter post-close on its own)
- Backlog review, recovery-race research, candidate-pool discussion — separate project work,
  not "the account"

## Execution
Run directly once plan mode exits — existing tools cover all 5 points
(`open_positions_status.py`/direct DB queries, `logs/active_signals.log` grep for the
9:30–16:00 window, `trading_incidents` table query, real broker order-activity query via
`schwab_client`, `coverage_check.py` filtered to non-canary tickers). No new scripts needed.
