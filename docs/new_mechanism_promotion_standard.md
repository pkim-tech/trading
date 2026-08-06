# New Live-Trading Mechanism Promotion Standard

Distinct from `docs/watchlist_candidate_checklist.md` (promoting a *ticker/parameter combination*
within an already-live mechanism) — this is the checklist for promoting a genuinely **new
mechanism** (a new kind of position, order type, or state machine this system has never run live
before) toward real capital. Built 2026-08-07, extracted directly from the drought-overlay/add-on/
put-hedge live-automation design after the same session's design review had to catch two real,
already-known-pattern gaps (paper/live-vs-backtest reconciliation, the daily-track/live-track split)
that should have been applied on the first pass, not reminded into the design after the fact. This
document exists specifically so that doesn't need to happen again — every item below is a pattern
this project has already established and validated for core strategy; the point of this checklist is
to make applying them to the *next* new mechanism automatic, not something dependent on recalling
the right prior session.

**When to use this**: any time a new mechanism is being designed — a new position type, a new order
type/asset class, a new automated decision the daemon makes that it's never made before. If you're
just adding a new ticker to an existing, already-live mechanism, use
`watchlist_candidate_checklist.md` instead.

## The checklist

1. **Real DB schema first.** New position/state needs real tables or columns before anything else
   can be built on top of them. Prefer extending an existing table with a discriminator column
   (`position_source`, `paper_role`, etc. — this project's established pattern) over a parallel table
   when the new thing is structurally similar to something that exists; use a genuinely new table
   only when the asset class itself is different (e.g. options vs. equity shares).

2. **`tests/fake_broker.py` extension for any new order type.** If the mechanism places a kind of
   order this project has never placed before (a new asset class, a new order shape), fake_broker
   needs to simulate it before anything downstream can be tested against a controlled, evolving order
   book instead of one-off mocks.

3. **Paper-trading simulation, using the SAME state machine real code will use.** Every mechanism
   that eventually touches real capital in this project has been paper-tested first — this is not
   optional. **Explicitly include a daily-track (`paper_role='daily_sync'`, prices off the last
   closed bar's Close) variant alongside the normal live-tick-priced paper node**, mirroring core
   strategy's own 2026-08-05 "Two-track paper trading" design — without this split, nothing downstream
   can separate a genuine logic bug from ordinary price-source noise between the backtest's discrete
   pricing assumption and live's continuous tick pricing. **This item is the one most likely to be
   silently skipped** — it was missed on the first draft of the 2026-08-07 drought/add-on/put-hedge
   design and had to be added after the fact. Ask explicitly: "does this need a daily-track split,"
   don't assume a single paper node is enough just because it simulates the mechanism at all.

4. **Paper/live-vs-backtest reconciliation.** A nightly job that replays whatever pure-Python
   backtest-kernel mirror function generated the original research finding against real accumulated
   data, and compares its implied result to what live/paper actually recorded — mirrors
   `paper_trading.reconcile_daily_track_nodes` exactly. **Deliberately pure observation**: never
   auto-corrects, never auto-halts, logs divergence for a human to look at. If a real backtest-kernel
   mirror function already exists (it usually will, from whatever research validated the mechanism in
   the first place), reuse it directly rather than re-deriving reconciliation logic from scratch.

5. **Trade-Flow Accountability Grid** (`scripts/coverage_registry.py`) — add scenario rows for every
   new control point the mechanism introduces. A mechanism the grid doesn't know about can't be
   answered for "is this proven live," which defeats the grid's whole purpose as this project's
   single non-opinion-dependent source of truth on live coverage.

6. **Truth-table permutation coverage for cross-mechanism interactions.** The riskiest edge cases in
   a system with multiple simultaneously-active mechanisms are the interactions BETWEEN them, not
   any one mechanism in isolation — explicitly enumerate the real state-space (what can this
   mechanism's state be, combined with what every other active mechanism's state can be) rather than
   spot-checking a few cases that come to mind. Pay particular attention to: same-tick/same-bar race
   conditions between two mechanisms' triggers, order-of-operations questions when two mechanisms
   need to act on the same underlying position, and whether an existing guard (e.g. a same-ticker
   double-buy guard) was written assuming only one mechanism could ever trigger it.

7. **Staged real-order live testing**, matching this project's existing "Staged real-order test
   protocol" pattern (`docs/design.md`) — small real notional, organic real signals only, never a
   forced/faked trigger, one new order type/mechanism at a time, not a batch.

8. **Expect an edge-cases-on-edge-cases hardening pass.** This project's own track record (the
   2026-07-31 exit/arm/entry audit found 9 real bugs, a same-day follow-up found 8 more, an
   independent review of *that* found 8 more) says a dedicated audit pass after staged testing is the
   norm for a change of this size, not a sign something went wrong in the earlier steps.

## Why this exists

Every one of the 8 items above was already a validated, working pattern in this codebase before
2026-08-07 — none of it is new invention. The gap that prompted this document wasn't missing
knowledge, it was that a first-pass design for a genuinely new mechanism didn't automatically apply
patterns already proven for a different mechanism (core strategy), and needed two rounds of direct
prompting to catch what should have been checked against a list like this one from the start.
