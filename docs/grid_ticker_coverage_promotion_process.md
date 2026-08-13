# Accountability Grid — Ticker Coverage Promotion Process

The periodic exercise of mapping every Trade-Flow Test Accountability Grid row
(`scripts/coverage_registry.py`) to a real designated tester, closing real gaps with new tickers
only where genuinely needed, and retiring staged nodes whose purpose is already proven. Distinct
from `docs/new_mechanism_promotion_standard.md` (designing a brand-new mechanism) and
`docs/watchlist_candidate_checklist.md` (promoting a ticker/parameter combo for real trading
alpha) — this process is about *test coverage*, not alpha or new mechanisms. First run 2026-08-13
(late), captured after finding this needs to recur as the Grid grows (a concurrent session added
6 new rows mid-cycle the same week).

## Testing philosophy: evidence tiers and the two kinds of test plan

Captured 2026-08-14 after finding the existing model (every scenario's test plan = a detuned
ticker/node, canary or live) doesn't fit a real class of Grid rows: `schwab_safety.check_order`'s
guard-*rejection* paths (`unknown_account_block`, `hard_order_ceiling_block`,
`buy_signal_window_block`, etc.). No steady-state node config produces these from ordinary
trading — a well-behaved node, by design, should never trip them. Forcing one to happen is a
different kind of test plan than detuning a ticker's exit parameters.

**Evidence tiers, highest to lowest** (see `scripts/coverage_proof_matrix.py`'s module docstring
for the authoritative definitions): LIVE (real capital produced it) > CANARY (a real
`coverage_events` row with `mode='dry_run'` exists, from *any* source) > PAPER > SIMULATOR (a
`tests/test_fake_broker_*.py` test proves the code, zero real firing) > UNIT-TEST (plain mock,
no fake broker) > NONE. Always prefer the highest tier reachable — canary before live before
simulation — but the ceiling for a given row is capped by which test-plan kind actually applies
to it, not by ambition.

**Test-plan kind 1 — ticker-based (the default, existing model).** A `watch_list` node, detuned
in canary (dry_run) or live, sits in the normal poll loop and organically trips the scenario as
part of ordinary signal-driven trading. Tracked by `scripts/coverage_designated_tester.py`. Use
this whenever the scenario is something a real ticker's price action can produce (an SL exit, an
add-on trigger, a drought entry).

**Test-plan kind 2 — script-based (new, for guard-rejection rows).** A named script deliberately
calls `schwab_safety.check_order`/`approve_and_record` once, on demand, against a real dry_run
account with an out-of-bounds parameter (oversized notional, off-hours timestamp, wrong account) —
no node involved, no waiting. This still lands at CANARY tier on the proof matrix (the tier check
only cares that a real `mode='dry_run'` event was logged, not whether a node or a script produced
it) — it's a different mechanism for reaching the same tier, not a lesser one. `scripts/
coverage_designated_tester.py` doesn't track this kind yet (only node-based testers) — a
script-owned row currently misreports as `-- none designated --`, which reads as an unowned gap
even when it isn't. Extending the tracker to record a script-based test plan is backlogged (see
`docs/backlog_cache.md`), not yet built.

**When SIMULATOR is the accepted ceiling, not a gap.** Some rows genuinely aren't worth forcing to
CANARY even via a script — same shape as the existing `not-prod-required` rows in
`scripts/coverage_registry.py`. Accept SIMULATOR as final when the cost of forcing evidence
exceeds the value of the extra tier: e.g. `unknown_account_block` (only provable by deliberately
calling code wrong — proves the guard exists, nothing about real state), `global_burst_cap_block`
(forcing it means 12 real orders across all accounts within 60s, which would throttle actual
concurrent order placement during the test window), `account_disabled_block` (disabling an
account blocks every node on it, including real live ones sharing that account). This is a
judgment call, not a formula — mark it explicitly (a `structural_note`/`not-prod-required` entry
in the registry, with the reason), don't just leave it silently unaddressed.

## When to run this
Whenever `scripts/coverage_harness_breakdown.py` reports `unclassified` rows (the Grid grew since
`BEST_HARNESS` was last updated), or when a backlog review flags a `wired-never-fired` live-side
row with no designated tester, or on a rough monthly/quarterly cadence alongside the weekend-cleanup
skill.

## The steps

1. **Classify every unclassified Grid row as `live` / `canary` / structurally impossible.**
   Run `scripts/coverage_harness_breakdown.py --list` first — any `unclassified` bucket is new
   rows needing a `BEST_HARNESS` entry in `coverage_registry.py`. Test: does `check_order` (or
   whichever code path the row exercises) run identically for a `dry_run` node, with only the
   final broker submission skipped? If yes → `canary` (no real capital needed, ever). If it
   inherently requires a real fill/real broker response (an SL actually filling, a real add-on
   buying-power check against real account data) → `live`. A row that's real but only reachable
   by luck/timing coincidence or a human Slack click gets the harness label PLUS a
   `not_prod_required_note` (demoted, not un-classified) rather than left in limbo.
   **Caveat found 2026-08-13**: don't assume a `check_order` guard fires from ordinary
   traffic just because dry_run nodes exercise the same code path — some guards (e.g. a
   trading-day/signal-window block) are pre-empted by the daemon's own scan gate and need
   deliberate staging (an inflated `starting_notional_override`, a tightened cap) even though
   they're `canary`-harness, not organically observable.

2. **For every `live`-harness, non-demoted row, run `compute_status()` fresh — don't trust a
   memory snapshot or an old backlog note.** `scripts/coverage_registry.py`'s own status can flip
   from `wired-never-fired` to `verified-live` between sessions purely from organic trading
   activity or an unrelated Grid-computation bug fix landing. The **real, current gap** is only
   the rows still `wired-never-fired` (or `structural-gap`) after this fresh check — treat any
   older plan's gap list as a hypothesis to re-verify, not a fact.

3. **Before proposing a new ticker, check whether an existing node already covers the gap.**
   Run `scripts/coverage_designated_tester.py` — a row can show `wired-never-fired` while already
   having a real designated tester waiting on an organic trigger (no forcing mechanism, just
   patience). Proposing a brand-new ticker for a row that already has a live tester just adds
   real-capital footprint and order-rate-limit contention for a speed/certainty tradeoff, not a
   coverage gap — call this out explicitly as "accelerate an existing tester" vs. "close a true
   vacuum," and let the review step (5) weigh in on which is worth it. Real lesson from 2026-08-13:
   a 4-ticker plan (DRIP/SOLT/CURE/TMF) was built to close a 22-row gap; by the time it was
   revisited a fresh `compute_status()` pass showed 2 of those 4 tickers' entire purpose (5 + 1
   rows) had already been organically proven by unrelated live activity in the interim — building
   them anyway would have been pure waste.

4. **Apply the test-ticker selection screen fresh, every time — never reuse a prior session's
   vetting verdict as-is.** Four criteria (see `[[project_capital_scaling_test_ticker_pool]]`
   memory for the canonical list): not in the current `watch_list`; not the user's tracked
   "broad 6" or an inverse/leveraged fund sharing their underlying index; not a genuine,
   currently-selected real trading candidate (see note below); K1-clean for a tax-advantaged
   account. **Open methodological question found 2026-08-13, not yet resolved**: the "candidate
   report" check (criterion 3) needs a precise definition of what counts as "on the report" —
   `candidate_full_review_*.csv` is a broad ~176-ticker screening scan (`status` is
   SAFE/CLIFF, `pick` is almost always null), not a filtered "these are our real picks" list, so
   a literal "does this ticker appear as any row" check flags nearly everything, including
   tickers a prior vetting pass explicitly kept (DRIP, SOLT, TMF, CURE all literally appear as
   rows). The intent seems closer to "not the sector's actual chosen/selected representative"
   (e.g. GUSH is the real Oil&Gas E&P pick; DRIP, same sector inverse, was kept). Resolve this
   ambiguity explicitly with the user before trusting a "passes criterion 3" verdict — don't
   silently pick either reading.

5. **Draft the plan, then get an independent Opus review before touching anything real.** Spawn
   a fresh Opus agent (no session context) with the draft plan and read access to the actual repo
   — instruct it to verify claims against real code/DB, not just accept the plan's own framing.
   This has caught real errors twice already: a factual claim about which rows already had a
   designated tester, and a claim about which guard rows can fire organically. Don't skip this
   step even under time pressure — it's cheap (one agent call) relative to creating a real-capital
   node on a wrong premise.

6. **Execute only with explicit per-batch user confirmation.** `.claude/skills/live-test-node-setup`
   has a hard rule: never create or modify a live node unprompted, even at small notional. This
   process produces a *proposal*; the actual `add_node`/state-flip calls happen through that
   skill's staging procedure (label, `staged_test_config` row with `revert_to` values, DB backup,
   `signals_invariants.py` baseline seed) only after the user says go.

7. **Retire by pausing, not deleting.** A staged node whose designated-tester purpose is fully
   `verified-live` gets flipped `state='paper'` (or similar), keeping its `watch_list` row and
   `staged_test_config` history intact as a rerunnable regression fixture for the next time
   related code changes — per the live-test-node-setup skill's own staleness-check section. Prune
   only the specific stale `staged_test_config` *role* row if the node itself still serves another
   live purpose (e.g. RETL keeps running for its `drought_handoff`/`addon` roles while its
   superseded `time_exit_via_sl` role gets dropped).

## Tooling built for this (2026-08-13)
- `scripts/coverage_harness_breakdown.py` — BEST_HARNESS x compute_status() crosstab, logged/diffed
- `scripts/coverage_designated_tester.py` — which node is assigned to prove each row
- `scripts/coverage_proof_matrix.py` — LIVE/CANARY/PAPER/SIMULATOR tier per row
- `scripts/capital_scaling_gate.py` — narrower gate specifically for "ready to scale notional"
- `scripts/audit_live_test_candidates.py --staged` — staleness check on existing staged nodes
