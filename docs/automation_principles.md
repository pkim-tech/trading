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
session, run `python scripts/live_sim_harness.py` (all 7 scenarios, ~2s) and confirm every
scenario passes before that session's `session wrap`/`session close`, in addition to the unit
suite and the kernel-parity verify scripts (#9).
**Why**: the harness exercises real orchestration functions (`_scan_pinned_entry`,
`_scan_pinned_exit_arm`, `_reconcile_fill`, `check_gap_resize`, TIME-exit, ambient market-buy,
dry_run fill synthesis)
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

## 16. Every new DB table gets a Streamlit reference page
When a new table is added (anywhere — `trading_live.db`, `trading_universe.db`, `watchlist_sweep.db`),
build or extend a `pages/` view for it in the same session, not later. Data that only exists behind an
ad hoc script (`scripts/foo_status.py`) or raw SQL means the user has to know to ask for it and wait on
a tool call — a live reference page means they can just look. This applies even to tables built as
internal plumbing (e.g. a coverage/audit table), not just user-facing ones.
**Why**: user's explicit standing instruction, 2026-07-24 late night, raised while building the
`scenario_expectations`/`coverage_deviations`/`node_id` migration — "I need the reference pages,"
i.e. a page is the actual deliverable, not an optional nice-to-have layered on after the schema work.

## 17. Independent code review is non-deterministic — pair it with a deterministic test for state-machine-shaped bugs
Two independent review passes over the same diff can each catch things the other misses (confirmed
directly, 2026-07-31: a first review found a HIGH real-money bug a second, later review round didn't
re-flag, and vice versa for smaller findings). For a bug whose root cause is a missed branch in a
small state space (a handful of boolean/enum fields combining into a decision), review alone is a
probabilistic net, not a proof. Where the state space is small enough to enumerate by hand, write a
real parametrized truth-table test (one test, every cell, the expected outcome asserted per cell) —
not scattered individually-named scenario tests that happen to cover most of the same cells, which
reads as coverage but isn't a systematic artifact. Where the space is too large to hand-enumerate,
use property-based testing (`hypothesis`) asserting the real invariant instead of individual examples.
To verify a test actually catches what it claims to, temporarily reintroduce the historical bug
(mutation testing) and confirm the test goes red before trusting it.
**Why**: `check_entry_abandon`'s real-money bug (an order could be silently orphaned while the code
claimed it was cancelled) was a missed cell in a 3-axis, 8-cell state space — found by review, but
the regression tests written for it were hand-named scenarios, not the actual truth table, until a
later gap-check caught the shortfall. `scripts/mutation_test_entry_abandon.py` and
`tests/test_entry_abandon_truth_table.py`/`tests/test_paper_trading_properties.py` are the reusable
patterns this principle generalizes from.

## 18. A shared test fixture needs its own coverage audit — "the fixture exists" isn't "the fixture is exercised"
A stateful test double (`tests/fake_broker.py`) can have a real bug in a code path no existing test
has ever actually reached — passing tests prove nothing about a method nothing calls. Before trusting
a shared fixture as a safety net for a class of bugs, build an evidence-derived matrix (grep the real
test files fresh every run, the same "never hand-typed" discipline as `coverage_registry.py`) of every
real use case the fixture is meant to cover, and confirm each one is genuinely reached — not just that
the surrounding orchestration function is called by name. A single entrypoint can hide 2+ genuinely
different branches (a replace-path and a fresh-placement fallback sharing one function); grep-only
detection will false-positive on the untested branch, so a real "was this specific branch reached"
check requires reading the test, not just matching the function name.
**Why**: `fake_broker.py`'s `cancel_order` had its arguments in the wrong order relative to the real
schwab-py client, making every call to it a silent no-op — undiscovered for the fixture's whole
lifetime because no existing test had ever called `cancel_order` (every other real order-placement
path had migrated to atomic `replace_order` first). `scripts/fake_broker_coverage_matrix.py` (built
2026-07-31) is the reusable pattern: found 6 of 11 real broker-mutating use cases uncovered, 2 of
those grep false positives caught only by manually reading the test and confirming which branch it
actually reaches.

## 19. A new automated order-placement scenario gets a fake_broker test in the same session it's built, not later
When a new production code path is added that mutates a real broker order (a new `schwab_client`
call site, or a new reachable use case through an existing one — e.g. a new exit reason, a new
guard-fallback branch), add it to `scripts/fake_broker_coverage_matrix.py`'s `USE_CASES` table and
write its fake_broker scenario test then, not as a later batch-reconciliation audit. The matrix is
only a real accountability tool if it's kept current at the point of change — otherwise the exact
gap it was built to close (real order-mutating code with no fixture-driven proof it behaves
correctly) silently reopens every time the codebase grows.
**Why**: user's explicit instruction, 2026-07-31 evening, given directly after confirming the
matrix-x-truth-table exhaustive fake_broker plan for that weekend — without this, the same "11 use
cases existed in production, only some were ever covered" gap `scripts/fake_broker_coverage_matrix.py`
was built to close would just reopen gradually as new scenarios get added.
