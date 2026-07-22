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
