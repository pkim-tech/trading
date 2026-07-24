# Automation Engineering Principles

Standing rules for building the live-trading automation engine (`active_signals.py` and
everything it calls). Distinct from `operational_limits.md` (position-sizing/exposure rules
for the human trader) — these are engineering rules for the code itself, written because the
same classes of bug keep recurring across sessions. Apply these by default; don't re-litigate
per feature.

## 0. Default to absurdly defensive
This is the umbrella rule the rest of this document operationalizes: assume every external
call (broker API, price feed, cache) can fail, time out, or lie; assume every piece of state
(shares held, cash available, an order's real status) can be stale or wrong; assume any code
path not under test is broken until proven otherwise. When a choice is between "handle this
edge case" and "it probably won't happen," handle it — this is real money, and nearly every
bug found so far (phantom shares, unplaced SL, stale cache, ungated automation) was exactly
the edge case that "probably wouldn't happen."

## 1. Reconfirm real state before acting — never trust a local/cached record as ground truth
Local bookkeeping (`open_positions.shares`, the duplicate-order dedup list, a cached
`backtest_cache` row) can silently drift from what the broker/data source actually holds.
Before a state-changing action, prefer a live check over an assumed one where the cost of
being wrong is real money.
**Why**: the phantom top-up-shares bug (DB updated, no real order ever placed), the duplicate-
order guard trusting its own pre-flight record before the broker call confirms anything, and
two separate cache-trusts-without-reverifying bugs in the sweep engine (`run_phase1_coarse`
row-count check, the since-removed campaign-level skip check) all stemmed from this.

## 2. Fail closed on financial/safety checks
If a safety check itself can't complete (a balance fetch fails, a timeout, an API error),
block the action — don't fall through to allowing it. An unchecked order is exactly the risk
being prevented.
**Why**: the scoped-not-yet-built cash-balance check is designed this way explicitly; the
alternative (fail-open) turns a missing-data problem into a silent safety-bypass.

## 3. Isolate failures per-unit — one ticker/position's exception must not kill the daemon
Every section of `run_loop` that touches a specific ticker or position should catch and log
locally, not let an exception propagate to the outer loop.
**Why**: `_refresh` and `_scan_pinned_entry` already do this correctly (per-ticker try/except,
skip and continue); most of the rest of `run_loop` doesn't, meaning one bad tick/API hiccup
anywhere can crash monitoring for every open position — a total-outage risk, worse than any
single wrong-state bug found so far.

## 4. Every state-changing or degraded-mode event gets a visible notification
Don't let a caught exception, a skipped check, or a fallback path resolve silently — if a
human would want to know, post to Slack, don't just log.
**Why**: matches the existing reminder-loop pattern (BUY-placed nagging, TRAILING ACTIVATED);
called out explicitly as a requirement for whatever fault-tolerance wrapping gets built next.
**Corollary**: this applies even when nothing is wrong enough to block. A trend worth flagging
before it becomes a hard failure (e.g. an account's cash running low) deserves a non-blocking
warning, not silence until the day it finally trips a real limit. The cash-balance check's
`CASH_RESERVE_WATERMARK` warning is exactly this — informational, doesn't gate the order, just
surfaces "you'll want to add cash soon" before it's actually a problem.

## 5. Detected mismatches get a proposed fix for human approval, never silent auto-correction
When code detects that live state and expected state disagree (broker position vs.
`open_positions`, missing protective order, etc.), the response is: alert, and optionally
propose the specific remediation — not execute it automatically.
**Why**: explicit user call — a false-positive mismatch triggering an automated "fix" trade is
worse than the silent-bug risk it would replace. This is the agreed design for the live-state
reconciliation idea.

## 6. Don't add a bypass flag to route around a safety check — tighten the check itself
If a legitimate action is getting incorrectly blocked by a guard, fix the guard's fingerprint/
logic so it correctly distinguishes the legitimate case, rather than adding an `is_x`-style
escape hatch.
**Why**: direct user pushback on this exact pattern ("we're just poking holes through the
protection mechanisms") when fixing the duplicate-order guard's false-positive on the top-up
order — solved by adding a quantity-match requirement, not a bypass.

## 7. New automation surfaces inherit the full existing gating, not a subset
When a new code path is added to an existing gated system (mode-gating, ticker-gating,
scope-gating), it must be checked against every gate that already applies to sibling paths —
don't let it slip through with only some of them.
**Why**: BUY-side automation is gated by both ticker membership and node `mode`; SELL-side
automation was only ever gated by ticker — an inconsistency found, not yet fixed, that lets a
`research`-mode ticker's real position get routed through automated-sell.

## 7a. Prefer a cheap static buffer over precise accounting, when the buffer is cheap
Defensive doesn't mean maximally precise — where a small, fixed margin of safety removes an
entire class of edge case, take it instead of building exact real-time accounting. A buffer is
simpler, has fewer failure modes of its own, and is easier to reason about under stress.
**Why**: user call on the cash-balance check — rather than computing exact available cash net
of every pending/resting order in real time, require only a small fixed per-order cushion
(`CASH_SAFETY_BUFFER=200`, covering fees/a quote-to-fill price tick) on top of the notional.
This is deliberately not the user's real safety margin — the user separately keeps a much
larger cash reserve (~$1,000) sitting in each account as their own operational habit, so
`cash_available` already carries that headroom most of the time; the code doesn't need to
re-enforce it. The exact-accounting alternative (modeling every pending order precisely) costs
real complexity (and its own new bug surface) for no benefit the reserve habit doesn't already
cover.

## 8. Everything must be testable
Every automation code path needs a real test — unit test, synthetic-scenario test, or an
offline replay script — not just manual reasoning or a one-off manual check. If a path can't
be exercised without a live account, that itself is a design smell worth fixing (see
`scripts/live_sim.py`'s isolated-DB approach as the pattern for testing the Slack-lifecycle
side without touching the real daemon).
**Why**: every real bug caught this project (SL price basis, missing fallback SL, phantom
top-up shares, the exit-side gap-through-trigger bug, etc.) was found by a test or an offline
replay script, not by inspection alone.

## 9. Every live behavior must be provably aligned with the backtest kernel it's implementing
When live code implements a decision the backtest kernel already models (entry trigger,
signal computation, fill resolution), build a replay/parity script that runs the real backtest
logic against real historical data and asserts the live code reproduces the same decision —
don't just eyeball the two implementations for equivalence.
**Why**: this is exactly what `verify_pinned_entry_vs_backtest.py`, `verify_trailing_buy_
resolution.py`, and `verify_trailing_sell_resolution.py` do, and each has caught real
divergence (the `prev_close` bug, documented fill-resolution drift) that manual review missed.

## 10. Maintain a live-test coverage ledger, separate from code and backlog
Keep a standing record — `docs/live_test_coverage.md` — of every scenario/code-path that needs
to be exercised against the real live daemon (not just an offline replay), and mark it done
only once actually observed succeeding live, with a date. A test passing in isolation is not
the same claim as "this scenario has actually run correctly in production."
**Why**: this project has repeatedly relied on `dry_run=True` + unit tests as a stand-in for
live validation (Deliverable 2's `open_price_quality_log`, the cash-balance check, the whole
Part 3/Part 4 automation surface) with no single place tracking which scenarios still lack a
real live observation — easy to lose track of across sessions without one.

## 11. Run the live-sim coverage harness before closing a session that touched live-trading code
Whenever `active_signals.py` or any `signals_*.py`/`schwab_*.py` module changed during a
session, run `python scripts/live_sim_harness.py` (all 6 scenarios, ~2s) and confirm every
scenario passes before that session's `session wrap`/`session close`, in addition to the unit
suite and the kernel-parity verify scripts (#9).
**Why**: the harness exercises real orchestration functions (`_scan_pinned_entry`,
`_scan_pinned_exit_arm`, `_reconcile_fill`, `check_gap_resize`, TIME-exit, ambient market-buy)
end-to-end against synthetic data — a layer unit tests don't cover and the real daemon can't
safely be used to test. Built 2026-07-23; found a real `get_open_position` bug and a real state-
file pollution incident (`SCHWAB_STATE_DIR`) in its first build session, so it has already
demonstrated value beyond what the unit suite alone catches.

## 12. Independent Opus review before closing a session that touched live-trading code or the backtest kernel
`session wrap` now spawns a fresh Opus review agent (no context from the session's own reasoning)
against the real diff whenever `active_signals.py`, any `signals_*.py`/`schwab_*.py` module, or
the backtest kernel (`backtester.py`, `strategies.py`, `run_optimization_sweep.py`) changed —
resolve any CONFIRMED finding before committing.
**Why**: this project's most serious bugs (the 2026-07-22 trailing-arm state clobber causing
duplicate live sell orders; the 2026-07-19/20 trailing-buy/trailing-stop gap-through-trigger
kernel bugs) were caught by an ad hoc independent Opus review requested mid-session, not by the
author's own inspection or the unit suite. A kernel mistake is just as load-bearing as a
live-trading one — paper trading, dry run, and eventual live execution all inherit whatever the
kernel got wrong, so a resweep built on a silent kernel bug produces confidently wrong numbers
same as a live daemon bug produces a wrong order. Making the review a standing, required step
(rather than something raised occasionally, per session_cache's 2026-07-22/23 notes) closes that
gap instead of relying on remembering to ask for it.

## 13. Never rely on a UNIQUE constraint over a nullable column for dedup
SQLite (and most SQL engines) never treat `NULL == NULL` as a match for uniqueness purposes, so
`INSERT OR IGNORE`/`ON CONFLICT` silently stops deduping the moment any column in the UNIQUE key
can be NULL. Either normalize the nullable column to a real sentinel value before the constraint
sees it, or do an explicit check-then-skip (query for an equivalent row first, using `COALESCE`
to treat NULL as equal to NULL, then only insert if nothing matched) instead of trusting the
constraint alone.
**Why**: this exact bug hit the codebase twice in one day (2026-07-24) — `add_node`'s
`take_profit=NULL` for `TrailingBothZScoreBreakout` nodes (15 real duplicate live `watch_list`
rows on `soxl_ira`), then `add_scenario_expectation`/`record_deviation`'s `ticker=NULL` for
control-site scenarios. Two independent authors hit the identical shape without recognizing it as
a pattern until the second occurrence — worth checking any future nullable-column-in-a-UNIQUE-key
design against this before it becomes a third incident.

## 14. Re-examine a safety guard's purpose before a new order type or code path routes through it
A limit built for one purpose (e.g. bounding new risk-adding exposure on a BUY) does not
automatically make sense for every order type that later starts calling through the same
chokepoint. When a new caller (a SELL, a protective follow-on action, a top-up) is wired into an
existing guard, explicitly ask whether the guard's original rationale still applies — don't let it
silently inherit a limit designed around a different scenario.
**Why**: `notional_cap` (sized for new-BUY risk) permanently dead-ended a real automated
trailing-SELL once a position grew past it, since a SELL closes exposure rather than adding it;
`daily_order_cap` (sized for entry-order volume) starved a stop-loss placement and a top-up-buy
purely on order-count bookkeeping, leaving a genuine fresh fill unprotected. Both found live
2026-07-24, both fixed by asking "does this guard's purpose apply to this order type" rather than
by loosening the limit itself.

## 15. Any in-memory dedup/tracking set in `run_loop` must be smart-initialized at startup, not left empty
A plain `set()`/`dict()` seeded empty at daemon startup means the very first poll after *any*
restart trivially treats "no record at all" as "this just happened for the first time" — even
when the real state (a bar already closed, an alert already sent) hasn't actually changed.
Initialize from real persisted/derivable state instead: either the clock (if the tracker is keyed
on a fixed daily time slot, like `reference_alerted`/`gap_check_alerted`/`pinned_bar_alerted`) or
the real current data (if it's keyed on a moving value like a bar timestamp, like
`last_seen_bar`, seeded from each open position's real current bar).
**Why**: `last_seen_bar` starting empty caused a restart to force a spurious off-schedule
bar-close evaluation on every open position, confirmed live 2026-07-24 (a restart at 11:14 ET
triggered SPY's arm/TP check at 11:21 ET, not a real bar close or pinned exit-arm time) — same
underlying restart-safety gap `reference_alerted` and friends were already deliberately built to
avoid, just not applied consistently to every tracker of that shape.
