# Backlog

## [backtest] Open, raised 2026-08-03, evolved same session (paired-capital) — NEW strategy paradigm: paired-ticker capital allocation using a real matched leveraged inverse, explicitly NOT a v5 variant
Started as "hold idle capital in the inverse while primary is flat" (rejected — leveraged-decay blowup on long idle windows, `docs/research_log.md`'s 2026-08-03 entry), evolved through several framings the same session:
- **Idle-capital multiplier** (mutually-exclusive timing, filler only trades on its own signal during primary's idle windows): prototyped 3 variants — skip-only (`scripts/sim_v6_inverse_secondary_trade.py`), forced-exit-on-primary-reentry (`scripts/sim_constrained_inverse_pair.py`), aggressive-flip (open filler the instant primary exits regardless of filler's own signal state, `scripts/sim_paired_flip_strategy.py`, untested end-to-end yet — script had a bug fixed mid-session, needs a clean rerun). Real joint-capital results (`scripts/sim_v6_joint_capital.py`, merges primary's own trades + filler's non-overlapping trades into one chronological compounding curve): **AGQ-primary/ZSL-filler is the standout (1.56x multiplier on total compounded gain)**; GDXU/GDXD works both directions (1.09x/1.27x, reversed roles do better); NUGT/DUST fails either way (DUST has no cliff-safe node at any fixed_sl 1-3, win rates 0.5-14%); SOXL/SOXS fails both directions (0.51x/0.97x) — a couple of large SOXS losses land inside SOXL's idle windows and crush the compounded total (traced to a real ~11.6% weekend gap in the underlying; SOXL had zero position at the time, so the gain that would have offset it never happened).
- **Corrected framing (2026-08-03, later that session)**: the idle-capital-multiplier framing is NOT a hedge — primary/filler never overlap in time by construction, so a bad primary trade is never cushioned by a concurrent filler gain (confirmed directly via the SOXL/SOXS weekend-gap case above). User wants **additive/concurrent** allocation instead: both legs funded and held simultaneously (separate capital, not idle-capital reuse), sized close to balanced/50-50 rather than skewed (the "square maximizes area" intuition — two roughly-equal legs likely compound their joint product better than one dominant + one thin, though the actual optimal split ratio needs to be found empirically, not assumed).
- **KORU has no real live inverse** — the historical pair (KORZ, Direxion Daily South Korea Bear) is delisted, confirmed via yfinance (no price data, `possibly delisted`).
- **Not v5** — explicit user call: this needs its own version tag once built for real (name TBD).
**Next steps** (none started): (1) rebuild as a true concurrent-capital backtest (both legs open simultaneously, independently funded, sized as a fraction of total capital each) rather than any of the mutually-exclusive-timing variants above; (2) search for the capital-split ratio that maximizes joint compounded return, don't assume 50/50; (3) once a real candidate split+pair emerges, run the "island matchup" robustness check across both tickers' parameter neighborhoods simultaneously (not just a single best-cell pick) before trusting it; (4) separately, GDXD/DUST/ZSL/SOXS still only have one narrow sweep (or, for SOXS, an ad hoc non-cached grid search) — a proper resweep (wider `fixed_sl`, add `TrailingBothZScoreBreakout`) is still open if this concurrent framing ends up wanting a better filler node than what's on file today. Other real matched pairs in `config.json`'s universe with no v5 data at all: QID/QLD, YANG/YINN, TQQQ/SQQQ, FAS/FAZ, FNGU/FNGD, UCO/SCO, TMF/TMV.

## [backtest] Resolved 2026-08-03 — FFT-based cycle detection to inform z-score window selection: negative result, no significant periodicity found
Full writeup (hypothesis/method/result) in `docs/research_log.md`'s 2026-08-03 entry — this is a
research-narrative resolution, not an action record, so it lives there rather than being duplicated
here. Short version: `scripts/fft_cycle_analysis.py` (new) tested all 10 real v5 watchlist tickers'
hourly return series for a dominant FFT cycle, using a random-permutation null for significance (the
correct baseline — a raw peak from `scipy.fft` isn't itself evidence of periodicity for a series this
close to white noise). No ticker showed a significant peak (p=0.076-0.837, all above the 0.05 bar),
and no correlation with the empirically-chosen `window` (10 vs 20). The price-level series *did* show
"significant" ~350-480 bar peaks, but that's a known null-test artifact (linear detrending doesn't
remove multi-month drift, and a full-shuffle null destroys all autocorrelation, not just
periodicity) — not real cyclic structure, flagged as such rather than reported as a finding.
Distinct from the still-open, unscoped 2026-08-02 idea of FFT/wavelet live spectral filtering as its
own new strategy paradigm (see `docs/backlog_cache.md`) — that's a different, bigger question this
result doesn't touch. Also note: the original 2026-08-01 item bundled a second half ("or... feed
regime detection alongside the SPY-trend/VIX finding") that this experiment did not test — it only
checked for a single fixed dominant cycle, not rolling/time-varying spectral power as a regime
signal. That half is re-opened as its own line in `docs/backlog_cache.md` rather than treated as
closed by this result.

## [live-trading][security] Resolved 2026-08-02 — existing-position BUY guard closes the real double-buy gap confirmed 2026-07-24
Confirmed live 2026-07-24: two real resting `TRAILING_STOP` BUYs (GDXD 5sh, GDXU 3sh) left
`get_account_balance('soxl_ira')` completely unchanged before and after — Schwab doesn't reserve
buying power for a resting order. Consequence: `notional_cap` (checked per-order) and the real
cash-availability check (reads that same undecremented balance) can't by themselves stop a second
real BUY from being approved for a ticker the account already holds, once the first order has
*filled* (the existing resting-order dup guards, `_has_open_order`/`_has_open_buy_order_in_account`,
only cover the window before a fill — they check the live broker order book, which a filled order no
longer appears in). Design conversation started at a much heavier "cumulative same-day BUY notional"
approach (dynamic caps, cash-availability subtraction, realized-vs-approval-notional accounting) before
landing on the much simpler correct framing: this is a state question, not a math problem. Do we
already hold this ticker in this account? If so, block, unless it's the sanctioned top-up.

**Fix** (`schwab_safety.py` `check_order`, BUY branch, ~line 840): if
`signals_db.get_open_position_for_account(ticker, account)` returns a position, block the BUY with
`SafetyViolation`, unless `is_protective=True` (the `_reconcile_fill` top-up path completing that
same position's sizing). Reuses `_local_pos`, already fetched a few lines earlier for
`_log_pre_action_state_verification` — no new broker calls. Logs `buy_blocked_position_exists` to
`coverage_events`, registered in the Accountability Grid with `fake_venue_proof` from the new test.

**Tests**: `tests/test_fake_broker_buy_blocked_position_exists_scenario.py` (2 scenarios: genuine
2nd BUY blocked; `is_protective` top-up still allowed through despite the open position). Fixing this
also surfaced 5 pre-existing `test_schwab_safety.py` tests that were seeding a leftover open position
(`_seed_position()`, built to satisfy the SELL-side oversell guard) *before* an unrelated BUY call
meant to test cap/burst-cap logic in isolation — the new guard correctly caught those BUYs as if they
were real duplicates. Fixed by reordering each test's `_seed_position()` call to occur only right
before the SELL/SL leg that actually needs it, and adding a `_clear_position()` helper (raw DELETE,
not `close_position()`, to avoid tripping `same_day_block` for the cash-type `ira` account) for the one
test where a real position needed to exist for an earlier SELL and then NOT exist for a later BUY.
Full suite: 505 passed (was 496 pre-session).

## [live-trading][coverage] Resolved 2026-08-02 — TRAIL-exit reminder spam: routine "still resting, no action needed" alert now suppressed until it's genuinely been waiting too long
Prior state (already partially fixed by earlier 2026-08-01 sessions, see below): a TRAIL exit whose
automated trailing-sell order is confirmed still resting at the broker — completely normal, the order
will fill on its own — used to re-post a "SELL SIGNAL" alert every hourly bar close
(`notify_sell_signal`, `sell_alerted` dedups within a bar, not across bars) AND every
`EXIT_REMINDER_MINUTES` (15) via `check_exit_reminders`, for as long as the order stayed resting. The
2026-07-28 GDXU incident's wording/staleness bugs (hardcoded "Cancel Stop Loss order" text, a
`_current_price()` staleness gap) were already fixed as a side effect of separate 2026-08-01 work
(`_exit_order_resting`, built for the Skip-button fix) — investigated this session and found already
resolved, not by anyone deliberately closing this item. `_exit_pending_blocks` (the reminder-loop
message builder) already had a `reminder_num>=3` escalation distinguishing "routine, just waiting" from
"should have filled by now" wording (Opus review, 2026-08-01) — but it still POSTED on every reminder
cycle regardless, just with safer wording. User's ask, once the bug was explained plainly: don't ping at
all for expected behavior — the one useful notification is already the arm-time "trailing stop
activated" message; only ping again if something's actually stuck.

**Fix**: `notify_sell_signal` (`signals_notify.py` ~line 1532) now skips the initial Slack post
entirely when `reason == 'TRAIL' and resting is True` (still tracks `exit_pending` with
`channel/ts=None` so `check_own_sell_fills` keeps polling for the real fill and
`check_exit_reminders`'s elapsed-time gate still works). `check_exit_reminders`
(`signals_notify.py` ~line 1917) skips posting reminders #1 and #2 the same way (re-checking
`_exit_order_resting` fresh each cycle, not trusting a stale flag) but still advances
`reminder_count`/`last_reminder_at` so the existing `reminder_num>=3` escalation still fires on
schedule (~45 min) if the order genuinely hasn't filled by then. TP/SL/TIME reminders are untouched.

**Independent Opus review (cold, no session context) found 3 real defects in the first draft, all
fixed same session**, most severe first:
1. **HIGH**: `check_exit_reminders` only runs 9:00-16:00 (`active_signals._reminders_active`) —
   suppressing near that window's close, or outside it entirely, could leave a real open position
   with zero Slack visibility until reminders resume at 9:00 the next trading day (up to ~17.75h,
   not the intended ~45min-max escalation window). Fixed via new `_trail_alert_should_post_now`
   (checks time-of-day, fails toward posting if outside the window or within `3×EXIT_REMINDER_MINUTES`
   of its 16:00 cutoff), gating both the initial suppression in `notify_sell_signal` and the reminder
   suppression in `check_exit_reminders`.
2. **MEDIUM**: the new BUY guard (separate entry above) is ticker+account-keyed, not node-keyed — a
   2nd live node sharing a ticker+account would have its genuine first entry wrongly blocked. Latent
   today (no such pairing exists on the live watchlist), mirrors an already-documented `_node_id`
   ambiguity limitation in the same function — documented in a matching comment rather than
   engineering the deeper wl_id-threading fix (already a separate flagged follow-up), not fixed.
3. **LOW**: `notify_sell_signal` unconditionally overwrote `exit_pending` (resetting `reminder_count`
   to 0) every time it re-fired on a new bar close (`sell_alerted` dedups per-bar, not across bars) —
   could silently discard `check_exit_reminders`' escalation progress. Fixed: when an identical
   still-tracked TRAIL `exit_pending` already exists (same reason + order_id) and suppression applies,
   `notify_sell_signal` now leaves `trail_state` untouched entirely rather than rebuilding it.

A bug was introduced *while fixing finding #1* (the time-of-day gate condition in
`check_exit_reminders` was inverted — `and _trail_alert_should_post_now(now)` instead of
`and not ...`) and caught immediately by the new regression tests written for the fix, not by a
second review round — direct evidence for testing every review-driven fix rather than trusting it
by inspection alone.

**Tests**: `tests/test_trail_exit_reminder_suppression.py`, 6 tests (grew from 4 during the review
fix-up) — initial alert suppressed when confirmed resting; a TIME exit is NOT suppressed (sanity
check the suppression is TRAIL-only); reminders #1-#2 suppressed then #3 escalates and posts;
suppression doesn't mask a status change (order no longer confirmed resting posts immediately,
doesn't wait for #3); too-close-to-cutoff forces posting instead of suppressing; reminder_count is
preserved (not reset) across a bar re-fire. Full suite: 507 passed.

## [live-trading][coverage] Open, raised 2026-08-02 — `manual_buy_confirmation_account` has never fired for a real live BUY; the manual-confirmation Slack path is currently fully unexercised in production
Investigated as one of the 3 "suspicious" `wired-never-fired` Accountability Grid rows flagged 2026-08-01.
Traced every real (`is_dry_run_sim=0`) BUY fill on file (GDXU, ERY, LABD, RETL) via `coverage_events`:
all four resolved through `buy_fill_reconciled` (auto-fill-detection opt-in poll, or GDXU's dedicated
gap-resize test node's `gap_resize` path) — never through `handle_entry_price`/`handle_trail_buy_fill_
price`/`handle_manual_open` (the three Slack button handlers that log this scenario_key). DPST (id 136,
`soxl_ira`, the one node with `auto_fill_detection_enabled=False` that's actually `mode='live'`) has zero
rows in `trade_log`/`open_positions`/`pending_buys` — no signal has fired for it yet, so it's never had
the chance either. **HIBL/USD/YANG (ids 154/155/156) are additional real-money volunteers with auto-fill-
detection disabled, but are currently `mode='research'`** (bridged back pending the $5k funding settling,
see the 2026-08-01 (late) pilot-notional entry) — not live yet, so don't currently count.
**Real consequence, narrower than it first looked**: `_reconcile_buy_fill` (the automatic path) already
calls `_place_stop_loss_for_position` itself (`signals_notify.py:2258`) — so for any ticker on the
automatic path, SL placement is already exercised there, independent of whether `handle_entry_price`'s
own copy of that call has ever fired for real. The manual-confirmation path only matters for a ticker
*not* opted into auto-fill-detection, which per the user (2026-08-02) isn't the direction v5 is headed —
the goal is full automation with no manual Slack step at all, so this is really a "does the manual path
even need to keep existing" question, filed under **Slack interface improvements**, not a live coverage
risk to chase down. CLAUDE.md's "every real live BUY entry today still requires manual Slack confirmation"
line is stale for any auto-fill-detection-opted-in ticker and should be corrected next time that section
is touched.

## [live-trading][coverage] Open, raised 2026-08-02 — `fast_path_fill_reconciliation` has never fired; account-activity stream reconnects cleanly now, but its message-shape parsing is still unverified against a real fill
Investigated as one of the 3 "suspicious" `wired-never-fired` Accountability Grid rows flagged 2026-08-01.
`logs/active_signals.log` shows 4 stream disconnects: 2026-07-23 ~08:57 ET and 2026-07-30 ~17:08 ET were
both `could not locate runnable browser` (the known weekly Schwab-token-expiry pattern, confirmed by the
user — same cadence as the existing Sunday-reauth habit, self-resolves on reauth, not a real bug here).
2026-07-31 ~21:04 ET and 2026-08-01 ~21:04 ET were ordinary clean-ish disconnects (`no close frame
received or sent`, `received 1000 (OK); then sent 1000 (OK)`) — the stream is authenticating and
reconnecting fine now. **The real open question isn't connectivity, it's that `schwab_stream.
_parse_activity_message`'s ACCT_ACTIVITY field-shape parsing is explicitly self-documented in its own
module docstring as "unverified against a real fill event"** — no real Schwab fill message has ever been
confirmed to actually parse into a `FILL_QUEUE` event, let alone reach `drain_fill_queue`. Low urgency:
`check_auto_fills`'s 5-min poll fallback is unconditional and already does the reconciliation regardless,
so this is a latency-only gap today, not a correctness one.
**Diagnostic logging added 2026-08-02** (`schwab_stream._handle_activity_message`): every raw ACCT_ACTIVITY
message is now printed to `logs/active_signals.log` unconditionally, plus whether it parsed into a fill
event or not — self-diagnosing the next time a real fill happens on an auto-fill-detection-enabled ticker
(ERY/LABD/RETL today), no special trigger needed. Remove the logging once the shape's been confirmed
correct against a real fill. 3 stream tests still pass (`tests/test_*stream*`).

## [live-trading][security] Resolved 2026-08-01 (late) — `handle_entry_price` never auto-placed a protective stop for automation-scoped market-buy fills; found via a fake_broker test-quality audit, fixed with a new paired independent+contextual review pattern

**Found while fixing the 13 fake_broker test-quality gaps flagged (but never fixed) two sessions
earlier** (see this file's 2026-08-01 fake_broker-coverage entry) — `test_fake_broker_buy_button_
handlers_scenario.py`'s `test_manual_buy_confirmation_account_logs_coverage_and_places_stop` claimed
to prove `handle_entry_price` places a real STOP order, but made zero `fake_broker.orders` assertions
and ran on the dry_run `ira` account. Investigating led to a real production gap, not just a test gap:
`_place_stop_loss_for_position` (`signals_notify.py:617`) has exactly one call site in production code
before this fix — `handle_trail_buy_fill_price` (the trailing-buy "Filled" confirmation handler).
`handle_entry_price` (the market-buy "Executed" confirmation handler, used by `TrailingExitZScoreBreakout`
tickers — YANG, VOO, IVV, QQQ, IWM, DIA, XLF, JNUG, all in `SCHWAB_AUTOMATION_TICKERS`) never called it,
despite the identical `if ticker in AUTOMATION_ENABLED_TICKERS:` gate already existing on its sibling.
**Real severity, clarified during the session**: every real live BUY entry today — automation-scoped or
not — is still manually confirmed via Slack (only post-fill housekeeping is automated), so this isn't
legacy/bridge code; it's the actual live mechanism for every real market-buy fill right now. Also
confirmed the existing `missing_sl` reconciliation check (`signals_notify.py:574`) can't catch this
class of gap at all — it only fires when a stop was attempted and later found missing at the broker
(gated on `pos.get('sl_order_id')` being truthy), not when one was never attempted.

**Fixed**: added the identical gate + `_place_stop_loss_for_position(node, ticker)` call to
`handle_entry_price`, placed after the "Executed" `chat_update` confirmation (not before, see below),
wrapped in a bare `try/except` so nothing inside it can prevent that confirmation from being sent (the
real position is already open by that point — the user seeing confirmation matters more than a
silent-if-uncaught SL failure, which isn't silent anyway: it's covered by the function's own
UNPROTECTED alert on a caught failure). The identical wrap was retrofitted onto
`handle_trail_buy_fill_price`'s pre-existing call for parity, but that handler's message ordering was
left untouched (already battle-tested since 2026-07-24, no identified need to reorder it).

**New review pattern used this session, worth adopting going forward**: instead of a single independent
Opus review, ran an independent cold reviewer + a contextual reviewer (given the "why," not just the
diff) in parallel, then a rebuttal exchange between them. On the test-quality audit (13 files), this
caught something a single cold pass got wrong (a false-positive oversell-guard finding, conceded on
rebuttal after re-reading the actual `pytest.raises` assertion) and sharpened something it underscored
(coverage-registry gaming scored as a direct recurrence of an already-fixed 2026-07-26 false-green bug
class, not generic metric inflation). On this production diff, the contextual reviewer (given context
the cold one lacked — that `handle_entry_price` is also the manual catch-up/backdated-entry flow, see
`_reconcile_buy_fill`'s comment) caught a real ordering issue the cold reviewer missed entirely: a
catch-up entry confirmed days late could already be past its stop, triggering `_place_stop_loss_for_
position`'s self-correcting forced-market-SELL branch — which, before this fix, would have posted its
alert *before* the "Executed" confirmation, since the SL call ran first. Fixed by moving the SL-placement
call to after the confirmation message in `handle_entry_price` specifically.

**Also fixed in the same session** (the fake_broker test-quality audit's other 7 findings, now closed,
not just reviewed): `coverage_registry.py`'s `_scan_fake_venue_proof` now requires an actual `fake_broker`
pytest-fixture argument in a test signature, not just the substring "fake_broker" anywhere in the file
— 2 files were found gaming the old check by importing it for no functional reason; honest headline
dropped from a false 37/41 to a real 36/41. `tests/fake_broker.py`'s `get_account()` position calc now
nets filled SELL orders against BUYs (previously only summed BUYs, so a test simulating a real manual
sale had to bypass the fixture with a mock instead). An SL-tolerance test tightened to `abs=0.01` +
widened price split, mutation-tested against the real 2026-07-31 SL-anchor bug (now correctly fails on
revert). A `manual_sl_fallback_alert` test now places a real STOP first so its named "cancel-then-fail"
scenario actually happens. An `exit_arm_latency` test now asserts the real arm/exit decision, not just
that an event fired. Two reconciliation tests now use real fake_broker fills instead of mocking
`get_real_position` (the function under test). Full suite: 466 passed. `signals_invariants.py` clean.
`live_sim_harness.py` 7/7. Two independent Opus review rounds (cold + contextual) of the production diff
found no confirmed defects beyond the exception-safety/ordering issues already described and fixed above.

## [live-trading] Resolved 2026-08-01 — hold-time-forced exits reported as `TRAIL` instead of `TIME`; second Opus review of the same-session diff found 8 more real bugs, all fixed

**Found by the user directly, reviewing a real trade** (SH, closed 2026-07-31 via `exit_reason='TRAIL'`
with pnl -0.19% — barely moved, nowhere near a real 50%-wide trail-stop breach). `signals_compute.py`'s
`check_sell_condition` collapsed BOTH a genuine trail-stop breach AND hold-time expiring while armed
into `reason='TRAIL'` unconditionally (`if reason in ('WIN','LOSS'): reason='TRAIL'`) — the
`exit_forced_by_hold_time` flag (`strategies.py`) existed only to route the live *execution* mechanism
correctly (force-replace with a market sell instead of passively polling a resting order nowhere near
its trigger), never to change what got reported to the human. Confirmed via
`signals_blocks.py`'s Slack message builder: the `'TRAIL'` branch says "🟢 trailing stop triggered"
(actively wrong for a timeout), while the `else: # TIME` branch already said the correct thing
("🔶 TIME EXIT... Change Stop Loss → Market Close order").

**Fixed**: `signals_compute.py` now reports `'TIME'` (not `'TRAIL'`) when `exit_forced_by_hold_time` is
set. `signals_notify.py`'s `_attempt_automated_exit_sell` — every one of its ~6 order-routing checks
already tested `reason=='TRAIL' and hold_time_forced` together, never `hold_time_forced` alone — dropped
the now-redundant `reason=='TRAIL'` half, keeping `hold_time_forced` as the sole authoritative
discriminator (one check, the resting-order-reuse shortcut, deliberately kept `and not hold_time_forced`
as an explicit defensive guard rather than assuming mutual exclusivity, per `automation_principles.md`
#0 — this is what caught 3 test failures during implementation: the existing `tests/test_fake_broker_sh_scenario.py`
tests hand-construct `reason='TRAIL'` + `hold_time_forced=True` together to simulate the pre-fix
collapse, an "invalid" combination post-fix that the guard correctly still handles). `scripts/verify_live_parity.py`'s
backtest-parity classifier updated to recover WIN/LOSS-by-sign for the new `reason=='TIME' and
hold_time_forced` case too (previously only checked `reason=='TRAIL'`, would have silently
misclassified as `TWIN`/`TLOSS`, wrong for a post-activation exit). New direct test
(`test_check_sell_condition_reports_time_not_trail_for_hold_time_forced_exit`) proves the real
production function outputs `'TIME'`, not just the downstream routing. Zero backtest blast radius —
`backtester.py`/the numba kernel never reads `exit_forced_by_hold_time` or this reason string, this is
live-code-only. Full suite 465+ passed, `live_sim_harness.py` 7/7, `signals_invariants.py` clean.

**Second independent Opus review, same session** (per `session wrap`'s required 3+ rounds before fully
trusting a real diff) — reviewed the FIRST review's 8 fixes plus the unreviewed JNUG/JDST canary work
(see the entry below), found 6 more CONFIRMED + 4 PLAUSIBLE issues, all fixed:
1. JDST itself still had `max_hold_hours=2` (the same defect the headline fix corrected for its
   siblings) — never caught since JDST has no `MIRROR` counterpart. Fixed to 48h.
2. The JNUG/JDST "revert" (see below) was label-deep only — JNUG still carried VOO's entire
   `TrailingExitZScoreBreakout` config; `watch_list_audit` proved both were created 2026-07-29 as
   `TrailingBothZScoreBreakout`. Fixed via a new one-time script,
   `scripts/fix_jnug_jdst_pair_config.py`, copying JDST's real field-for-field config onto JNUG.
3-4. Stale `daily_plan` rows (pre-fix pollution under `2026-08-01`; post-commit-but-pre-JNUG-fix rows
   under `2026-08-03` still describing JNUG's old E-scenario role) — cleared/rebuilt.
5. `position_lock`'s `bad_results` fix (from the first review) was itself incomplete — excluded only
   `already_closed`, but `open_position` logs `result='acquired'` unconditionally on every call with
   zero contention required, so the first real live position open still flipped the branch
   verified-live off no real concurrency evidence. Fixed: `bad_results=['already_closed', 'acquired',
   'closed']`, leaving only `skipped_duplicate` (genuine dedup-under-contention evidence).
6. The met/deviation reconciliation fix (from the first review) still didn't sum once a scenario was
   snoozed (`status='skipped'`, uncounted). Fixed: now reports met/deviations/informational/snoozed,
   all four summing to the total.
7 (plausible). The "Canary" section reported every daily/informational scenario including non-canary
   control scenarios (e.g. `reconciliation_mismatch`), while the plan section below it filtered to
   `canary_`-prefixed keys only — two sections under one heading silently disagreeing on scope. Split:
   canary count matches the plan's scope; control scenarios get their own `[control]`-labeled lines.
8 (plausible). Canary `daily_plan` rows were write-only, never read back — exactly what let the JNUG
   staleness (finding 3 above) go undetected. Added a real check: today's canary plan row compared
   against the LIVE `scenario_expectations` text for the same ticker, flagged if they've diverged.
9 (plausible). `close_position` logged `result='closed'` BEFORE `log_trade_exit`/the `DELETE` ran — a
   raise in either would leave a `'closed'` event on record for a close that never happened. Reordered
   to log after both succeed.
10 (plausible). `opened_today` (in the live/paper activity loop) wasn't `is_dry_run_sim`-filtered unlike
   its two neighbors (`closed`/`still_open`) — could mask a real carried-in position's unplanned close
   as a routine "new entry today". Filtered to match.

## [live-trading] Resolved 2026-08-01 — canary `max_hold_hours` mirror gap (4 nodes silently misconfigured since 2026-07-29), plus the nightly EOD review/plan cycle and a fake-venue test-coverage push

**Context**: user asked for an evening review of live/canary/paper activity and a next-day plan,
after 5 prior sessions of this not sticking as a manual habit. Built as real code instead:
`signals_notify.build_eod_scenario_review`/`build_tomorrow_plan`, wired into `active_signals.py`'s
16:05 ET EOD slot (both the live-loop trigger and the startup catch-up path). Posts one Slack message
covering (1) a readiness headline (`verified/total (%)` from `scripts/coverage_registry.py`'s
live-computed per-branch status), (2) the canary scenario check, (3) real live + paper activity
today, (4) tomorrow's plan (new `daily_plan` table: canary rows copy `scenario_expectations`, live/
paper rows compute each open position's real SL/arm/trail/TIME triggers from its own config).

**Real bug found and fixed while investigating why 6 canary scenarios showed unexplained deviations
on 2026-07-31**: `scripts/mirror_canary_pair_config.py` (the script that gave SPXU/QID/TWM/SDOW/FAZ
the same hair-trigger design as their A-F counterparts, 2026-07-29) never included `max_hold_hours`
in its mirrored-fields list. SPXU/QID/TWM/SDOW silently carried the generic-config default (2h,
F-scenario's own value) instead of their real design value (48h) for 3 days -- harmless until the
2026-07-31 entry-abandon timeout started reusing `max_hold_hours` as its cancel threshold, at which
point it made SDOW's `canary_overnight_carry` scenario structurally unable to ever pass (a 2h
"resting order" cancel fires long before "overnight" can happen) and forced SPXU's
`canary_full_lifecycle` into a TIME exit instead of its designed TRAIL. Fixed: all 4 nodes corrected
to 48h; the mirror script patched to include `max_hold_hours` so this can't regress. All 6 of that
day's unexplained `coverage_deviations` rows explained (3 broad-market-uptrend, this bug for the
other 3, including DIA's own miss which was purely the former).

**Fake-venue (`tests/fake_broker.py`-driven) test-coverage push, same session**: went from 6/41 to
37/41 tracked branches (`scripts/coverage_registry.py`) having a real fake-broker regression test,
not just production-observed behavior. The remaining 3 are legitimate: `kernel_fill_parity` is
offline-only by design; `market_buy_placement`/`open_price_quality` both have real proof via other
tests (`tests/test_fake_broker_pinned_entry_scenario.py`) but use a different check mechanism
(`scenario_expectations`/`open_price_quality_log`, not `coverage_events`) that the registry's own
proof-scanner (`_EVENT_ASSERTED_RE`, regex-matches only `get_coverage_events(scenario_key=...)`)
structurally can't detect -- a known blind spot in the detector itself, not a real gap. Also closed:
`position_lock` (previously zero instrumentation at all, `check_mechanism='none'` -- deliberately
deferred 2026-07-28 as "not a side-channel log addition" -- added observational
`log_coverage_event` calls inside the already-locked block in `open_position`/`close_position`,
doesn't touch the lock's own acquire semantics; new concurrency test, 20 threads racing
open/close, proves the lock genuinely serializes) and a new end-to-end proof that a real Schwab
`REJECTED` order (confirmed live 2026-07-23 for naked-sell/oversell, but only via a
`schwab_client`-bypass test until now) is handled correctly through the actual production chain
(`_attempt_automated_sell` -> `schwab_client.place_trailing_sell` -> `OrderRejected` -> clean
fallback, no state corruption) -- `tests/test_fake_broker_order_rejected_scenario.py`.

**Readiness, for the record**: fake-venue test coverage (90%, 37/41) answers "would a regression get
caught," not "how close to trading material money" -- the separate, unchanged-by-this-session number
is real-world proof (44%, 18/41 `verified-live`), which only moves via the daemon actually running
long enough for each of the 14 `wired-never-fired` conditions to occur naturally, or a deliberately
staged live test. See `docs/backlog_cache.md`'s 2026-08-01 entries for what's still open (a TOCTOU
oversell-guard race, deprioritized given Schwab's own rejection is a confirmed real backstop; a
still-unconstructed production-path rejection test; the accumulation plan for the 14 branches).

## [live-trading][security] Resolved 2026-07-31 (session wrap, final review) — a 3rd independent review of the complete production diff found 3 more real issues, all fixed

**Context**: required by `docs/automation_principles.md` #12/CLAUDE.md's `session wrap` procedure --
one more independent Opus pass over the complete production diff (`active_signals.py`,
`paper_trading.py`, `schwab_client.py`, `schwab_safety.py`, `signals_helpers.py`,
`signals_notify.py`) before the session's first real commit. Re-verified all 6 of the prior review
round's fixes (all correct) and independently re-confirmed the oversell guard, `_check_position_exit`
re-fetch, and `running_low` clamp direction from scratch rather than trusting the earlier conclusions.
Found 3 new issues, all real, all fixed:

1. **MEDIUM: the `sl_order_id` writeback fix (prior review round, item 2) wasn't guarded against the
   new order id itself being `None`.** `extract_order_id` can legitimately return `None` on a real
   broker success (e.g. a missing Location header) -- both writeback call sites
   (`_attempt_automated_sell`, `_attempt_automated_exit_sell`) would then overwrite a real, valid
   `sl_order_id` with `NULL`. Consequence: the `missing_sl` reconciliation check (gated on
   `sl_order_id` truthy) goes silent, and a later hold-time-forced exit's `resting_order_id` resolves
   to `None`, falling through to a fresh `place_equity_sell` that the resting-order dup guard then
   rejects with **no alert at all** (both branches of the except block false) -- strictly worse than
   the pre-fix behavior, which at least kept the dead old id and produced a loud UNPROTECTED alert.
   Fixed with the same `is not None` guard `_place_stop_loss_for_position` already uses 600 lines
   away. 2 new regression tests (`test_schwab_automation.py`).

2. **LOW-MEDIUM: `check_entry_abandon` could cancel a real MARKET order `check_gap_resize` placed
   seconds earlier in the same poll iteration.** `check_gap_resize` converts a trailing-buy pending
   row into a MARKET order and writes the new order id back onto the same `pending_buys` row, but the
   node is still `_is_trailing_buy` and `check_entry_abandon` (which runs later in the same `run_loop`
   iteration) had no exclusion for a row gap-resize had just touched. Real failure shape: a daemon
   restart right as `bars_held` crosses `max_hold_hours` -- gap-resize correctly detects the cleared
   trigger and places a real pre-open market buy that can't fill before 9:30, `check_entry_abandon`
   then cancels that brand-new order in the same iteration and falsely posts "never bounced ... entry
   abandoned" when the trigger genuinely cleared moments earlier. Fixed: skips any row with
   `gap_resize_date == today` (the same persisted idempotency marker `check_gap_resize` already
   writes). 1 new regression test.

3. **LOW: `scripts/coverage_registry.py`'s `fast_path_fill_reconciliation` row had no `bad_results`,**
   but `drain_fill_queue`'s new opt-in gate (prior review round) logs two new non-proving results
   (`outside_automation_scope`, `auto_fill_detection_disabled`) that will be the *common* real outcome
   once this fires live (auto-fill detection is off by default) -- without `bad_results`,
   `compute_status` would render the row `verified-live` off events that only prove the gate fired,
   not that the real poll-reconfirm path ran. Added those two plus `stream_event_not_yet_confirmed_filled`.

**3 observations documented (docstring notes added, not code changes)**: a real account with
`order_placed=False, order_id=None` is indistinguishable from "user placed it manually but never
tapped the confirm button" -- `order_placed` is this system's only local signal for that, no fix
possible without a real broker order-book check this function deliberately doesn't do; the
`raced_fill` branch deliberately bypasses the `auto_fill_detection_enabled` opt-in gate (a real,
order-id-exact-confirmed fill must never be silently dropped, unlike `drain_fill_queue`'s ambiguous
ticker+account match); `bars_held < node['max_hold_hours']` now uses `.get(...) or float('inf')`
matching the paper twin's defensive pattern, closing a theoretical `None`-crashes-the-whole-loop gap.

Full suite: 414 passed (was 411 before this round's fixes/tests). `scripts/live_sim_harness.py`: 7/7.
`signals_invariants.py`: 2 known/accepted violations, unchanged.
`scripts/mutation_test_entry_abandon.py`: still 5/5.

## [live-trading][testing] Resolved 2026-07-31 (same day, 4th follow-up) — a real parametrized truth table + a hypothesis property test + full fake_broker use-case coverage (6 gaps closed, accountability matrix built)

**Context**: reviewing the 3rd follow-up's own summary against what was actually promised (the
original 4-item deterministic-testing proposal) surfaced a real gap: `check_entry_abandon`'s
`(dry_run, order_placed, order_id)` state space was covered by 8 separately-named scenario tests,
not the actual parametrized truth-table artifact discussed -- and a proposed `hypothesis` property
test for `paper_trading.update_paper_buys`' fill/abandon ordering (the bigger, harder-to-hand-
enumerate permutation space) had been described but never built. Both fixed same session, plus a
full fake_broker use-case audit the user specifically requested.

1. **Real parametrized truth table for `check_entry_abandon`.** `tests/test_entry_abandon_truth_table.py`
   (new): one `@pytest.mark.parametrize`-driven test enumerating every `(limits, order_placed,
   order_id)` cell mechanically, asserting the correct outcome (cancel/alert/clear) per cell in one
   place -- distinct from the scattered hand-named scenario tests, which cover most of the same
   cells but don't exist as a single systematic artifact.

2. **`hypothesis` property test for `paper_trading.update_paper_buys`.** New dependency
   (`hypothesis`, added to `requirements.txt`) -- `tests/test_paper_trading_properties.py` generates
   150 random `(price, running_low, trail_buy_pct, bars_held, max_hold_hours)` combinations per run
   and asserts the real invariant the historical ordering bug violated: a genuine bounce must always
   fill, even on the exact poll a position is also overdue (matches the kernel's real per-bar
   priority); fill and abandon are mutually exclusive outcomes for any single poll. Verified the test
   actually catches the bug it's built for by temporarily reintroducing the abandon-before-fill
   ordering and confirming the property test fails (found the counterexample
   price=51.0/running_low=50.0/trail_buy_pct=1.0/bars_held=1/max_hold_hours=1 -- a genuine 2%
   bounce on the exact bar the position also turns overdue), then cleanly restored.

3. **`scripts/fake_broker_coverage_matrix.py`** (new): an accountability matrix in the same spirit as
   `scripts/coverage_registry.py` -- never hand-typed opinion, re-derives from real test files every
   run. Enumerates every real broker-mutating USE CASE (11 total, one per genuinely distinct decision
   path, not one per `schwab_client` function -- several of those are called from 2-3 different
   scenarios each) and whether a `tests/test_fake_broker_*.py` scenario actually drives real
   production code through it. First run: **5/11 covered**. Two of the apparent "covers" were grep
   false positives caught by manual verification (documented inline in the script, since grep alone
   can't distinguish two branches sharing one entrypoint name): `test_fake_broker_sh_scenario.py`/
   `test_fake_broker_trail_exit_scenario.py` always seed an SL first, so the exit's fresh-placement
   fallback was never reached; `test_fake_broker_gap_resize_scenario.py` always seeds a resting
   order, so gap-resize's fresh-placement fallback was never reached either.

4. **All 6 gaps closed, 11/11 now covered**, each with a new fake_broker scenario test asserting on
   the fake broker's own resulting order state (not a mocked return value):
   - `tests/test_fake_broker_entry_scenario.py` -- trailing-buy entry (`_attempt_automated_buy` ->
     `place_trailing_buy`, the live-default strategy's own entry mechanism, previously zero coverage
     of any kind) and market-buy entry (`_attempt_automated_market_buy` -> `place_equity_buy`, plus
     its `_sync_confirm_and_protect` SL-placement path).
   - `tests/test_fake_broker_arm_scenario.py` -- the arm transition (`notify_trailing_activated` ->
     `_attempt_automated_sell`), previously zero coverage: replacing an existing resting SL with a
     real trailing-sell, and a fresh trailing-sell placement when there's no SL to replace. Confirms
     the 2026-07-31 `sl_order_id` writeback fix (3rd follow-up, item 2) works against real broker
     state, and clarified (via a wrong test assertion, fixed) that a fresh placement correctly
     tracks its order id in `trail_state.exit_order_id`, not `open_positions.sl_order_id` (nothing
     existed there to update).
   - `tests/test_fake_broker_exit_fresh_scenario.py` -- exit with no resting order to replace (fresh
     `place_equity_sell`), verified via a real immediate fill correctly closing the position.
   - `tests/test_fake_broker_gap_resize_scenario.py` (extended) -- gap-resize with no `order_id` on
     file (fresh `place_equity_buy`).

Full suite: 399 passed (was 392 after the 3rd follow-up). `scripts/live_sim_harness.py`: 7/7.
`signals_invariants.py`: 2 known/accepted violations, unchanged.
`scripts/fake_broker_coverage_matrix.py`: 11/11.

New `docs/heap.md` (per user request) -- ultra-short-term scratch capture, a corollary to
`backlog_cache.md` for mid-session "by the way" thoughts that shouldn't pollute the real backlog
until they're either built or dropped.

## [live-trading][testing] Resolved 2026-07-31 (same day, 3rd follow-up) — a second independent review found 6 more findings (fixed), then deterministic testing (mutation testing + fake_broker) built for check_entry_abandon, which itself found a real latent bug in the shared fake_broker.py fixture

**Context**: after the 2nd follow-up (below) closed 8 defects found by review, a second review round
verified those 8 fixes were correct (no new regressions) but found 6 further findings in the *new*
code the fixes added. All 6 fixed same session:

1. **MEDIUM**: three `check_entry_abandon` branches that leave the `pending_buys` row in place
   (`unrecognized_account`, `no_order_id_on_file`, `cancel_failed`) had no alert throttling --
   `bars_held` only grows once past `max_hold_hours`, so a real stuck position would re-alert on
   every single poll forever, burying real trade alerts. Fixed with a new
   `_ENTRY_ABANDON_ALERTED`/`_ENTRY_ABANDON_ALERT_COOLDOWN_SECS` (15 min), matching the existing
   `_RECONCILE_ALERTED` pattern this module already uses elsewhere for the identical reason.
2. **MEDIUM**: `entry_abandon_timeout` -- the first-ever production caller of a real broker
   `cancel_order` -- had no row in `scripts/coverage_registry.py`'s trade-flow accountability grid
   and no entry in `docs/live_test_coverage.md`. Both added.
3. **LOW**: `schwab_client.cancel_order`'s docstring still claimed "not currently called by any real
   path... none exists in this codebase yet" -- now false. Corrected.
4. **LOW**: the new alerts used bare `(account)` instead of the standing `mode_tag(account)`
   (LIVE/DRY-RUN) convention, and the terminal "abandoned" message unconditionally claimed "resting
   order cancelled" even on the dry_run path, where nothing was ever real. Fixed: all alerts now use
   `mode_tag`; a new `did_cancel` flag distinguishes a real confirmed cancel from every path that
   reaches the terminal branch with nothing real to cancel (dry_run, or no order ever placed).
5. **LOW**: the ambient/pinned-check cutoff (`(h0, m0+5)`) was hardcoded rather than derived from
   `_PINNED_ENTRY_TIMES` -- correct today, silently wrong if `_SIGNAL_WINDOWS`/`_PINNED_BAR_TIMES` is
   ever retimed independently. Fixed to derive the cutoff from the real pinned-time set.
6. **INFO**: `drain_fill_queue`'s new order_id-match node resolution compared across a JSON boundary
   with no type coercion -- `pending_buys.order_id` has INTEGER affinity, the stream side is raw
   JSON; a numeric-string `orderId` would silently fail to match (fails safe to the slow poll, but
   pointless latency). Fixed with explicit `int()` coercion on both sides.

**Then, per explicit user request, built two forms of deterministic testing** (an alternative to
relying on independent code review, which is non-deterministic -- two review passes can catch
different things) for `check_entry_abandon` specifically, since that's where the real-money bug was:

- **`scripts/mutation_test_entry_abandon.py`** (new): reverts one specific historical/current bug in
  `check_entry_abandon` at a time (temporarily rewriting `signals_notify.py`'s real source text),
  runs the exact regression test paired with that bug, asserts it fails (the mutant is "killed"),
  then restores the original text. 5 mutations covering the real-money no-order-id bug, the
  live-node-vs-pinned-snapshot bug, the unrecognized-account fail-closed guard, the FILLED-race
  reconciliation, and the unconfirmed-cancel fail-closed retry. **5/5 mutants killed** -- the test
  suite would have caught all five if they were reintroduced. Found one real test-coverage gap
  while scoping this (no existing test covered the `limits is None` branch at all) -- added
  `test_unrecognized_account_fails_closed_and_alerts` before including it as a mutation target.
  Distinct from `sim_chaos_monkey.py` (simulates a human missing real signals, a strategy-robustness
  question) -- this operates on the source code itself, a test-suite-quality question.
- **`tests/test_fake_broker_entry_abandon_scenario.py`** (new): drives `check_entry_abandon` against
  `tests/fake_broker.py`'s stateful simulated order book instead of a mocked function return value --
  asserts the *broker's own order state* actually changed (CANCELED), not just that `cancel_order`
  was called. **This immediately found a real, previously-undiscovered bug in the shared fake_broker.py
  fixture itself**: `FakeBroker.cancel_order`'s parameters were `(account_hash, order_id)`, but the
  real schwab-py client (and every real call site, `schwab_client.py:530`) calls it
  `cancel_order(order_id, account_hash)` -- order_id first. With the swapped signature, every prior
  call silently did nothing (looked up a hash string that's never a real dict key), and no test had
  ever caught it because no existing fake_broker test asserted on post-cancel broker state -- every
  other real order-placement path in this codebase was migrated to atomic `replace_order` before this
  session, so `cancel_order` had simply never been exercised through fake_broker until now. Fixed the
  parameter order; also added a missing `FakeBroker.get_order` method (needed by
  `schwab_client._confirm_order_status`'s real post-cancel poll, previously absent entirely -- any
  caller reaching it would silently get `None`/unconfirmed via a swallowed `AttributeError`). 2
  scenario tests: a real cancel that actually flips the broker's order status, and a real bounce-fill
  racing the cancel that gets reconciled into a real position instead of discarded.

Full suite: 392 passed (was 389 after the 6 findings, 386 before them). `scripts/live_sim_harness.py`:
7/7. `signals_invariants.py`: 2 known/accepted violations, unchanged.

## [live-trading][security] Resolved 2026-07-31 (same day, 2nd follow-up) — independent Opus trace review of the follow-up session below found 8 real defects in that work (1 real-money), all fixed

**Method**: rather than trust the follow-up session's own tests, a fresh Opus agent with no session context traced each of its 7 changes through their real call chains by hand (same style as the original audit two entries below), specifically hunting for composition bugs, races, and fail-open/fail-closed mistakes. Confirmed 8 defects; 4 other claims in the review brief were investigated and found NOT to be real issues (documented at the end). All 8 fixed same session, each with new/updated regression tests. Full suite: 386 passed (was 381). `scripts/live_sim_harness.py`: 7/7. `signals_invariants.py`: 2 known/accepted violations, unchanged.

1. **HIGH, real money: `check_entry_abandon` could orphan a real resting order while claiming it was cancelled.** The cancel branch required `order_id` truthy, but the manual "Trailing Buy Order Placed" Slack flow (`signals_handlers.handle_trail_buy_order_placed`) sets `pending_buys.order_placed=True` and never captures a broker order id (the user places it directly at Schwab, we never see the id returned) -- indistinguishable by `(order_placed, order_id)` alone from an automated dry_run placement, which also leaves `order_id=None`. For a REAL account's manual placement, the old code fell straight through to `db.clear_pending_buy_by_wl_id` + a "resting order cancelled" Slack message -- nothing was actually cancelled, and clearing the row also broke `handle_trail_buy_fill_price`'s stale-button guard, which needs that exact row to accept a later manual fill confirmation. Real failure shape: DPST (real, `soxl_ira`) BUY fires, automated placement declines (any of several reachable `SafetyViolation`s), user manually places + taps "Order Placed", 8 bars later the real still-resting order gets silently orphaned with zero local tracking and a false "cancelled" claim -- the exact leak this function exists to fix, now with the local record destroyed too. Fixed: distinguishes real-account-manual-placement (`not limits.dry_run and order_placed and not order_id` -- alert, leave the row untouched, require manual handling) from a dry_run account (safe to clear regardless, nothing real was ever placed) via the account's real `dry_run` flag, and fails closed (alert, don't touch) on an unrecognized account. 4 new tests.

2. **MEDIUM-HIGH: read the live `watch_list` node instead of the pinned pending-buy snapshot.** `check_entry_abandon` did `db.get_watch_list_node_by_id(wl_id)` for account/max_hold_hours, contradicting this module's own established convention (see the `_fresh_node` removal note: "a real resting order's account is a physical fact fixed at placement, not whatever watch_list says now") that every other `pending_buys` consumer (`check_gap_resize`, `check_auto_fills`, `_reconcile_buy_fill`) already follows. A later account edit on the node (a real, used pattern in this project) would silently retarget a real `cancel_order` call at the wrong account, or make a real non-dry_run order's cancel branch skip entirely if the node's account was edited to a dry_run one. Fixed: reads `pb['node']` (the signal-time snapshot already embedded in every pending_buys row) throughout, matching every sibling function. 1 new test (edits the live node's account after placement, confirms the cancel still targets the original account).

3. **MEDIUM: the ambient-scan exclusion (item 7 below) removed the only fallback when the pinned check itself didn't run.** `pinned_bar_alerted` is deliberately pre-seeded at daemon startup for any bar-time already past `now` (automation_principles.md #15), so a restart landing at e.g. 10:33 skips the 10:30 pinned entry check entirely for that day. The whole-window exclusion added by item 7 left an affected open_check+automation node with literally zero BUY coverage for the rest of that window (10:33-10:40), where before the fix the ambient scan would still have covered it at a degraded price. Same gap, smaller, when the pinned check's own price fetch fails all 3 retries. Fixed: `_ambient_buy_scan_nodes` now only excludes these nodes before the window's pinned moment (:25-:29); from :30 onward the ambient scan resumes as a fallback, since by then the pinned check has already had its one chance to win the dedup key first. 2 new tests (exclusion before :30, inclusion after).

4. **MEDIUM: paper's entry-abandon check ran before the bounce-fill check, inverting the kernel's real per-bar order.** The kernel (`backtester.py`'s `_simulate_trail_both`) checks the fill first and only falls through to the `wait_bars >= max_hours_to_hold` abandon on a bar that didn't fill; the first version of `paper_trading.update_paper_buys` checked abandon first, so a poll where price had already bounced past the trigger on the exact bar `max_hold_hours` was reached would abandon instead of fill -- paper P&L would then systematically drop marginal, latest-bounce entries. Fixed by reordering (fill check first, abandon only as a fallback) -- while fixing this, also found and fixed a second-order bug it exposed: the reordering initially let a stale/unavailable price (`_current_price`'s 90min staleness guard, common after a long enough wait) `continue` past the abandon check entirely via an early `if price is None: continue`, permanently un-abandoning an overdue position whenever fresh price data wasn't available. Now computes bars-held eligibility unconditionally (cached data only, doesn't need live price) before branching on price availability. 1 existing test's failure surfaced this during the fix itself.

5. **MEDIUM: the paper sizing "fix" (item 10 below) was less accurate than what it replaced, not more.** `buy_order_sizing`'s worst-case pad (`trail_buy_pct + pad_pct`) exists because a REAL order is sized before its fill price is known; paper already knows the fill price when it fills, and the real end state after `_reconcile_fill`'s post-fill top-up converges to `target_notional / fill_price` regardless of the initial pad -- so re-applying the pad at paper's fill time (which item 10 did) undersized paper positions relative to what a real position actually ends up holding, the opposite of the intended alignment. The ORIGINAL flat `starting_notional // price` formula (which item 10 replaced) was already the correct match to the real post-topup end state. Reverted to the flat formula, now with the (correct) rationale documented in place. Both of item 10's tests updated; 1 rewritten to make the real-end-state argument explicit instead of asserting parity with `buy_order_sizing`.

6. **LOW-MED: the paper abandon Slack alert could never fire.** `node` inside the abandon branch is the frozen `pending_buys.node_json` signal-time snapshot, and `paper_alert_verbose` isn't one of the fields captured in `_PENDING_BUY_NODE_KEYS` -- so `node.get('paper_alert_verbose')` was always `None`/falsy regardless of the real node's setting. The fill-alert path 40 lines below already re-reads the live node for exactly this reason (a 2026-07-26 Opus review fix for the identical bug shape); the new abandon branch reintroduced it. Fixed the same way (re-read via `db.get_watch_list_node_by_id`).

7. **LOW-MED, pre-existing (not introduced this session, fixed while touching the adjacent code): `start_paper_market_buy` still sizes off real `trade_log`.** Item 10's new `target_notional` override on `buy_order_sizing` was applied to the trailing-buy fill path but not the sibling market-buy path, which still calls `buy_order_sizing(node, sig)` with no override -- falling through to `_last_sale_recovery`'s real `trade_log` query. Paper fills never land in `trade_log` (`paper_trade_log` instead), so this is live-reachable today for the deliberate DPST live+research pairing (CLAUDE.md): if the paired nodes ever matched on `(ticker, strategy, version, window, account)`, the research node's paper sizing could pick up the live node's real trade proceeds. Fixed with the same `target_notional=starting_notional` override.

8. **LOW: `drain_fill_queue`'s new opt-in gate (item 5 below) resolved the node via a lookup its own neighboring comment says must never gate a real action.** The gate used `db.get_watch_list_node(ticker=ticker, account=account)` -- a fuzzy, ambiguity-prone lookup whose docstring already establishes (and this same function's `_reconcile_buy_fill` call, a few lines below, already respects) that it's "enrichment/logging, never a gate on a real action." Since `node_auto_fill_detection_enabled(None)` defaults closed, a fuzzy-match failure would silently drop the fast path for a genuinely opted-in node (fails closed to the slower `check_auto_fills` poll, so a latency regression rather than a safety hole, but a real self-contradiction). Fixed: resolves the node by exact `order_id` match against the real `pending_buys` row instead (the stream event already carries the real order_id). 1 new test (seeds a second node sharing the same ticker+account with detection OFF, proving the fix doesn't fall back to the wrong one).

**Investigated, found NOT to be real issues** (the review agent verified from real call-chain tracing, not assumption):
- The `shares >= 1` guard (item 6 below) doesn't fully prevent a 0-share BUY signal from entering the manual Slack pipeline -- true, but pre-existing (predates this session), not introduced by the guard itself.
- The oversell guard fix (item 3 below) -- traced every real non-test SELL-side `schwab_client` caller; all resolve `account` from the same `open_positions` row the guard's own lookup reads, so no real path is wrongly blocked.
- `sl_order_id` writeback (item 2 below) -- a single-column UPDATE with no read-modify-write, no clobber race against the Slack-handler thread.
- `_check_position_exit`'s re-fetch (item 8 below) -- `resolve_at_bar_close`/`sell_alerted` key off in-memory state and `pos['id']`, unaffected by the re-fetch.

## [live-trading][security] Resolved 2026-07-31 (same day, follow-up session) — 8 of the 13 items left open by the exit/arm/entry audit above, now fixed

**Scope**: closed the prioritized open list from the audit below, one at a time, each with its own
regression test and a full-suite run before moving to the next. Full suite: 381 passed (was 362 at
the start of the audit below, 371 after the [HIGHEST] item). `scripts/live_sim_harness.py`: 7/7.
`signals_invariants.py`: 2 known/accepted violations (SH's `max_hold_hours` snapshot drift, ERY's
`fixed_sl` snapshot drift from the widening the prior session — both position-row-vs-live-node-config
drift, the same accepted pattern as before), unchanged.

1. **[HIGHEST] Live entry-abandon timeout, built.** New `signals_notify.check_entry_abandon()`
   (wired into `active_signals.run_loop` alongside the other pending-buy trackers) mirrors the
   backtest kernel's `wait_bars >= max_hours_to_hold` (`backtester.py`'s `_simulate_trail_buy`/
   `_simulate_trail_both`): once `compute._bars_held(signal_time) >= node['max_hold_hours']`, cancels
   the real resting order (`schwab_client.cancel_order`) and clears the `pending_buys` row -- no
   trade recorded, matching the kernel's cancel-and-forget semantics. Handles a real race (the cancel
   lands the same instant a genuine bounce-fill does) by reconciling the fill instead of abandoning
   it, and fails closed (leaves the row, retries next poll) on an unconfirmed cancel. A parallel
   branch inside `paper_trading.update_paper_buys()` covers paper rows (no real order to cancel, just
   clears the row) -- paper trailing buys previously waited forever too, silently missing the
   "gives up on entry" outcome the kernel models. 9 new tests (`tests/test_entry_abandon.py`).

2. **`sl_order_id` staleness after any atomic replace, fixed.** Only the hold-time-forced TRAIL case
   (via `trail_state.exit_order_id`) was refreshed after the 2026-07-31 audit above; the
   `open_positions.sl_order_id` DB column itself was never updated after ANY replace --
   `_attempt_automated_sell` (arm-time SL-to-trailing-sell swap) and `_attempt_automated_exit_sell`
   (TP/SL/TIME exit's SL-to-market-sell swap) both now call `db.set_sl_order_id_by_position` with the
   new order id whenever the just-replaced order was the one tracked as `sl_order_id`. Without this,
   a later reader of `pos['sl_order_id']` (a re-arm attempt, the TRAIL+hold_time_forced SL fallback,
   the manual "cancel the existing stop-loss" reminder text) could reference a dead order id. 2 new
   tests (`tests/test_schwab_automation.py`).

3. **Oversell guard's fail-open branch, fixed after dedicated investigation.** The prior session's
   fix attempt broke ~14 tests and was reverted rather than same-night-patched. A background
   investigation agent confirmed the dominant cause: those tests are guard-isolation tests
   (`tests/test_schwab_safety.py` and others) that exercise OTHER `check_order` guards -- kill switch,
   cash check, signal window, duplicate-order, notional cap, hard ceiling, daily/burst caps -- via a
   SELL call with no `open_positions` row ever seeded, since seeding one was never their point. Every
   real production SELL call site was confirmed to resolve a position via `get_open_position_by_wl_id`/
   `get_position_by_id` *before* ever reaching `schwab_client`, with the same `account` value flowing
   straight through to `check_order`'s own separate `get_open_position_for_account` lookup -- so a
   real position should always be found there. Verified against the real `trading_live.db`: zero
   `open_positions` rows with a NULL/empty `account` (the one plausible legacy-data gap) before
   shipping. Fixed: `check_order`'s SELL branch now raises `SafetyViolation` outright when no local
   position is found (`if pos is None: raise ...`), instead of silently skipping the quantity bound.
   All 14 affected tests fixed by seeding a real position (`_seed_position()` helper,
   `tests/test_schwab_safety.py`) rather than by loosening the new guard -- matches
   `automation_principles.md` #6 (fix the guard/test, don't poke a bypass hole). 1 new dedicated
   regression test (`test_sell_blocked_when_no_local_position_on_file`) proves the fail-closed
   behavior itself, seeding nothing at all.

4. **`running_low` extended-hours contamination, bounded.** `update_real_pending_buys_running_low`
   deliberately uses `schwab_client.get_current_price`'s extended-hours-preferring quote (needed to
   track a genuine overnight/pre-market fall ahead of `check_gap_resize`'s pre-open check -- confirmed
   by `tests/test_fake_broker_gap_resize_scenario.py`'s existing Phase 1 test, which this fix had to
   preserve exactly), but a single thin/anomalous extended-hours print could previously crater
   `running_low` permanently (it's `min(...)`, never self-corrects). New
   `_MAX_RUNNING_LOW_DROP_PCT = 20.0` caps a single poll's drop; a genuine large real move still
   reaches its true low within a couple of polls (each capped step compounds), so real gap coverage
   is preserved. New test proves both the bound and that a reverting bad print doesn't get undone
   (the capped floor sticks, `min()` never rises) -- `tests/test_fake_broker_gap_resize_scenario.py`.

5. **`drain_fill_queue` opt-in gate bypass, fixed.** The fast websocket fill-detection path
   auto-reconciled any real fill unconditionally, bypassing `auto_fill_detection_enabled`/
   `node_auto_fill_detection_enabled` -- the exact opt-in gate the slower `check_auto_fills` poll
   already respected. Now checks both before calling `_reconcile_buy_fill`, logging
   `auto_fill_detection_disabled`/`outside_automation_scope` coverage events on skip. 3 existing
   `tests/test_part3_gap_resize.py` tests updated to explicitly opt in (`_enable_auto_fill_detection()`
   helper); 1 new test proves the gate itself (fill queued, nothing reconciled, pending row untouched).

6. **`shares >= 1` guard on the real BUY path, added.** `_attempt_automated_buy`/
   `_attempt_automated_market_buy` now check `sizing['shares'] < 1` before ever calling
   `schwab_client.place_trailing_buy`/`place_equity_buy` -- previously a too-small notional/price
   combo reached the broker call, got rejected, and both dinged the node circuit breaker's
   `order_failures` streak and posted a "blocked"-shaped alert that misleadingly implied a safety
   guard fired rather than "nothing to actually buy". New `automated_buy_execution`/
   `shares_too_small` coverage event; 1 new test (`tests/test_schwab_automation.py`).

7. **Ambient-scan-pre-empts-pinned-check race, fixed.** `_SIGNAL_WINDOWS` (10:25-10:40/15:25-15:40)
   starts 5 minutes before the pinned 10:30/15:30 check (`_scan_pinned_entry`) -- an ambient poll
   landing in that `:25-:29` gap could fire a BUY on the ambient scan's degraded yfinance price
   before the more accurate pinned check (Schwab's true session quote) ran, permanently winning the
   shared `buy_alerted` dedup key for that node/bar. New `active_signals._ambient_buy_scan_nodes()`
   excludes exactly the population `_scan_pinned_entry` exclusively owns (`entry_timing=='open_check'
   and ticker in AUTOMATION_ENABLED_TICKERS`) from the main ambient scan -- matches
   `_scan_pinned_entry`'s own docstring, which already establishes these nodes should never fall
   through to an ambient price, including on a pinned-fetch failure. 3 new tests
   (`tests/test_part4_entry_trigger.py`) confirm the exclusion and that non-open_check/non-automation
   nodes are unaffected (still ambient-covered, since they have no pinned alternative).

8. **Theoretical stale-snapshot race in the ambient exit loop, closed defensively.** `active_signals.
   _check_position_exit` (the per-position closure inside `run_loop`'s ambient exit-check loop) called
   `check_sell_condition` against the once-per-poll-cycle `open_positions` snapshot, not a fresh
   re-fetch -- the same stale-snapshot-then-clobber pattern already fixed elsewhere in the audit
   below, here "not practically reachable" only because of call ordering, not an explicit re-fetch.
   Now re-fetches via `db.get_position_by_id(pos['id'])` at the top, returning early (nothing to
   check) if the position was closed concurrently since the snapshot was taken.

**Deliberately not built, confirmed intentional design (not a bug):** BUY-shaped `check_order`
guards (kill switch, automation pause) also blocking SELL -- asked directly, user's answer: "i prefer
an off switch for everything - what if the algo is going rogue and tries to trade a non algo node?"
A genuine full-stop against a malfunctioning algo, including its exits, was judged more important
than exempting SELL. See `[[project_kill_switch_blocks_everything]]` memory.

**Attempted, reverted (not shipped):** live bounce-fill tracking's bar-Close-instead-of-Low/High bias
(`update_dry_run_buys`/`paper_trading.update_paper_buys`) -- switching to the cached bar's Low/High
broke 8 existing tests that all control simulated price via a `_current_price` monkeypatch, since
those tests would then need synthetic-CSV-level control instead. A bigger rewrite than this
documented low-severity (dry-run/paper simulation accuracy only, no real broker exposure) bias
warrants -- reverted cleanly, left open.

**Deliberately scoped out, not attempted this session:** the dry-run-account false "auto-placed"
circuit-breaker claim (conditional on a specific precondition, not confirmed currently live) and the
1-3% `fixed_sl` sizing research question (not a code defect).

## [live-trading][security] Resolved 2026-07-31 — full exit/arm/entry execution-path audit: 9 real bugs found and fixed (one real-money, one live-incident-during-the-session), 13 more confirmed and left open

**Scope and method**: rather than a diff review, this was a series of manual execution-path
walkthroughs (Opus agents given the real call chains and told to trace them by hand, not scan for
generic issues) across three areas in sequence: exit/sell logic, the arming transition (TP-threshold
clears → trailing-stop), and buy/entry logic. Exit got three separate passes (kernel-parity
decision-logic, order-placement execution-correctness, then a fresh context-free re-walk with no
prior-session context) plus an integration re-check specifically asking whether the individual fixes
composed correctly together. Every fix below was independently Opus-reviewed (several twice) before
landing; a final session-wrap review of the full combined diff found zero further issues and verified
the two real live-account writes made directly (not through the reviewed code path) against actual
broker state.

**Fixed — exit/sell side:**
1. **The original SH stuck-exit bug, fixed for real.** `notify_sell_signal`'s two call sites
   (`active_signals.py`) now re-fetch the position fresh (`db.get_position_by_id`) before acting,
   instead of using the pre-`check_sell_condition` snapshot — closes the root cause of the
   `exit_forced_by_hold_time` flag never surviving to reach the code meant to act on it.
2. **`_current_price`'s staleness guard** (`signals_compute.py`) changed from a same-calendar-day
   check to a straight bar-age check (`_STALE_PRICE_MAX_AGE = 90min`) — the date-only version missed
   the actual GDXU "current $81.92" overnight incident shape (a same-day-but-hours-old bar still
   passes a date check). The resulting off-hours alert-noise increase was then fixed by gating the
   alert to real trading hours (`9:35am–4pm`, not 9:00 — data isn't fresh until ~9:35 given real bar
   timing, found by review).
3. **`check_exit_reminders`/`check_trailing_reminders`** (`signals_notify.py`) had the identical
   stale-snapshot-then-clobber bug as the original SH incident, just via a different trigger (a
   15-minute reminder cycle instead of the exit-check loop) — both now re-fetch fresh before
   building their `trail_state` write, with `continue`-on-None hardening.
4. **`check_live_state_reconciliation`** had the same bug plus a second gap: it only re-checked
   openness via `wl_id` (skipping legacy `wl_id`-less rows) and then discarded the fresh row anyway,
   still comparing against the stale one. Now uses `db.get_position_by_id` and actually uses what it
   fetches — this also closed a real gap for `gap_resize`'s `shares` mutation, which the old
   `wl_id`-openness-only check couldn't see.
5. **Mid-bar `peak` contamination** (`strategies.py`, all 3 trailing-exit classes) — a mid-bar poll
   was unconditionally rolling the trailing `peak` forward, corrupting the "peak confirmed through
   the prior bar" invariant the 2026-07-20 gap-through-trigger fix depends on. Fixed by only
   persisting `peak` at real bar-close; verified directly against `backtester.py`'s actual kernel
   loop (peak only advances once per bar, after that bar's own gap check) — zero backtest blast
   radius by construction (the flag is live-code-only state, never fed to numba).
6. **`resting_order_id` missing an SL fallback** (`_attempt_automated_exit_sell`) — a third
   precondition of the SH bug family: if the arm-time trailing-sell placement itself failed (broker
   exception, paused automation), `exit_order_id` was never set, so the hold-time-forced branch
   resolved to `None` and self-blocked forever against the still-live original SL. Now falls back to
   `pos['sl_order_id']`.
7. **`exit_order_id` never refreshed after a force-replace** — fix #6's own first draft introduced a
   NEW instance of the clobber pattern (the write was itself built from a stale snapshot); caught in
   the same review cycle, fixed with an additional re-fetch. Separately, reusing `exit_order_id` as
   both "the order to replace" and "proof a replace happened" made the `still_unreplaced_trail_order`
   reuse-guard permanently true once that field started being refreshed — a composition bug caught by
   the dedicated integration re-check, fixed with a separate `hold_time_replaced` flag instead of an
   equality comparison.

**Fixed — arming:**
8. **Reconciliation blind spot** — arming (`trail_state['trailing']=True`) is persisted before the
   real order placement runs as a separate later step; a crash in that window left `trailing=True`
   with `order_placed` unset, which fell through both of `check_live_state_reconciliation`'s mismatch
   branches, permanently invisible. Fixed with a new `armed_order_never_confirmed` check — but the
   first version false-positived on the *normal* awaiting-manual-confirmation state (any node where
   `_attempt_automated_sell` legitimately declines), caught by review, narrowed to gate on
   `last_reminder_at` being absent (the field `notify_trailing_activated` sets unconditionally,
   auto-placed or not — its absence is the real "never ran at all" signature).
9. **Manual arm Slack alert missing share count, trail %, and a cancel-existing-stop instruction** —
   the alert asked a human to place a trailing order but gave neither of its two defining parameters,
   and never mentioned cancelling the resting protective SL first (mandatory on the automated path,
   to avoid two orders resting on the same shares).

**Fixed — buy/entry side (found via the exit walkthrough, since `_place_stop_loss_for_position` is
shared code, but affects every real fill):**
10. **Real-money bug: broker stop-loss anchored to the wrong price.** `_place_stop_loss_for_position`
    anchored the REAL Schwab stop order to `signal_price` (the original z-score trigger), while
    `strategies.py`'s own SL check always uses `entry_price` (the real fill) — correct only for
    market-buy strategies where the two coincide. For the trailing-buy strategies (the live default,
    all 10 v5 nodes, the real-money DPST node), the two diverge whenever price kept falling before
    the bounce — which could place the real stop ABOVE market, either rejected outright (zero broker
    protection) or triggering immediately. Fixed to anchor to `pos['entry_price']`, matching
    `strategies.py` exactly for every strategy, not just the ones where it happened to not matter.
    Directly contradicted CLAUDE.md's own claimed invariant ("stop set at the algo's exact fixed_sl
    price, no padding") — that line is now actually true.
11. **Hold-time origin** — `_bars_held` counts from `pos['signal_time']`, but the bounce-fill wait for
    the trailing-buy strategies was silently eating into that budget (the kernel's real basis is the
    FILL bar, with the wait tracked separately in `wait_bars` and explicitly excluded from `held`).
    Fixed across all 4 real/paper/dry-run trailing-buy fill paths to anchor `signal_time` to the real
    fill moment, not the stale original signal time — deliberately NOT applied to the manual-catch-up
    backdating flow (`handle_entry_price`), which is a genuinely different, intentional use of the
    same mechanism.
12. **SL placement retry + market-sell fallback** — built live, during a real incident: LABD's stop
    placement was REJECTED by Schwab ("stop price must be below the bid for sell stop orders")
    because its fill had been reconciled hours late (a stale `pending_buys` row processed only once
    the daemon restarted) and the market had already crossed the tight 0.3% target by placement time.
    Old code had zero retry — any failure immediately alerted UNPROTECTED and gave up. New logic
    re-checks the real current price on each retry; if the market has already crossed the target
    (no honest way to claim a fill at the stale theoretical `stop_price`), exits via a real MARKET
    sell instead — deliberately mirroring the backtest kernel's own gap-through-trigger SL logic
    (verified directly against `backtester.py`: fill at the real price when already gapped through,
    not the theoretical stop). If not yet breached, retries the same resting STOP (transient
    rejection). Relies on the existing `_has_open_sell_order` duplicate guard rather than a separate
    pre-check for the "already protected" case, per explicit user direction ("self-correcting").
    First version of this fix had its own real bug (caught by review, before it could bite twice):
    the market-sell fallback placed a real order but recorded nothing, so nothing could ever detect
    its fill — confirmed live against LABD's actual order before the fix landed. Fixed to record
    `exit_pending` and alert.

**Real live-account actions taken directly tonight (not through a reviewed code path, verified
against broker state after):**
- **ERY**: `fixed_sl` widened from 0.3% to 50% (matching SH's "wide/inert by design" pattern — ERY is
  meant to test a genuine TRAIL breach, and a 0.3% SL sitting at the same tightness as `arm_sell_pct`
  risked pre-empting the test before arming could ever happen). The real resting stop order was
  replaced via `schwab_client.replace_order_with_stop_loss` (pre-existing reusable infra, built
  2026-07-29 for exactly this "manually re-price a resting SL" case, last used for RETL) from $10.55
  to $5.29; DB's `sl_order_id` synced to the new order id.
- **LABD**: hit the real incident described in #12 above; its real market-sell order (already placed
  live before the `exit_pending` fix landed) was manually backfilled into `trail_state` so the
  daemon's existing, unchanged fill-detection code (`check_own_sell_fills`) can find and close it
  normally once it fills at market open.
- Both actions verified directly against live broker state (order id, price, status) in the
  session-wrap review, byte-for-byte matching what the reviewed code itself would have written.

**Confirmed but deliberately left open (13 items — see `docs/backlog_cache.md` for the trimmed
pointer list with priority/severity):** the oversell guard's fail-open branch on a missing local
position row (a first fix attempt broke 14 tests and was reverted — needs real investigation, not a
same-night patch); several BUY-shaped `schwab_safety.check_order` guards (kill switch, ticker/node
automation pause) that also block SELL by design today, worth a deliberate policy decision before the
node circuit breaker (currently monitor-only) ever becomes blocking; a dry-run-account false
"auto-placed" claim that can trip the node circuit breaker (conditional on a specific precondition,
not confirmed currently live); a theoretical (not practically reachable, protected by ordering rather
than freshness) stale-snapshot race in the ambient exit loop; the hold-time-origin question for
*manual* (not automated) trailing-buy fills was deliberately left as-is per the existing documented
catch-up-entry rationale; and 6 buy/entry-side findings from the dedicated entry walkthrough, worst
being **no live equivalent of the kernel's entry-abandon timeout** — a trailing-buy that never
bounces rests as a real GTC order forever and silently blocks every other BUY in that account once
the reminder logic goes quiet (see `docs/backlog_cache.md` for the full list of 6).

**Separately, same session**: explained and cleared 4 unexplained `coverage_deviations`
(VOO/DIA/QQQ/IVV "no closed trade" on 2026-07-30) with real hourly-bar confirmation that the broad
market was up that day (mean-reversion long-side canaries correctly saw no dislocation, their true
inverse pairs SPXU/QID/SDOW correctly triggered) — matches a documented 2026-07-28 precedent, not a
pipeline bug. Removed JNUG's `pairs_with VOO` label in `scripts/list_canary_nodes.py` (JNUG is 2x
gold miners, no real price relationship to VOO — confirmed independently moving, +3.29% vs VOO's
+0.76% the same day; JNUG's real test purpose, the E-scenario TrailingExit mechanism, is unaffected).
Investigated a striking v5 paper-trading result (25/25 closed trades, 0 wins, mean -3.47%) —
independently verified all 25 SL breaches against raw historical bars from scratch (bypassing the
project's own code entirely) and found zero mismatches: every breach was real. Combined with the
already-verified kernel-parity of the SL logic, this closes the loop that the result reflects a real,
severe 3-day drawdown (SOXL alone: -8.55%/-2.53%/-14.50%), not a code defect — though it does surface
a legitimate open research question (is 1-3% SL correctly sized for 3x-leveraged names in a
high-volatility regime) worth a `docs/research_log.md` entry if pursued further.

Test suite: 362 passed (was 349 at session start). `signals_invariants.py`: 2 known/accepted
violations (SH's pre-existing `max_hold_hours` snapshot drift, ERY's new `fixed_sl` snapshot drift
from the widening above — both position-row snapshots vs. live node config, same accepted pattern).
`scripts/live_sim_harness.py`: 7/7. Both `verify_trailing_buy_resolution.py`/
`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` clean, no new mismatches.

## [live-trading][security] Open, raised 2026-07-30 (evening) — `enable_node_auto_fill_detection(node_id)`'s docstring claims a ticker-level side effect it doesn't perform

**Found while wiring LABD's node 152 for the scenario-1 real-fill test** (see the sibling 2026-07-30
evening entry below for the full test context). `schwab_safety.py`'s
`enable_node_auto_fill_detection(node_id)` docstring reads: *"also ensures the ticker-level flag is
on, since the real gate is an AND of both layers and this is the only UI entry point that sets
either."* The function body only ever writes `NODE_AUTO_FILL_DETECTION_PATH` — it takes no `ticker`
argument at all, so it structurally cannot set the ticker-level flag (`AUTO_FILL_DETECTION_PATH`,
toggled separately via `enable_auto_fill_detection(ticker)`). Confirmed live: calling
`enable_node_auto_fill_detection(152)` alone left `auto_fill_detection_enabled('LABD')` returning
`False` — had to call `enable_auto_fill_detection('LABD')` separately to actually satisfy
`check_auto_fills`'s real gate (`auto_fill_detection_enabled(ticker) AND
node_auto_fill_detection_enabled(wl_id)`).

**Checked same session — not a live bug**: `signals_handlers.py:539-540`'s real Slack handler
(`handle_enable_auto_fill_detection`) already calls both `schwab_safety.enable_auto_fill_detection(ticker)`
and `schwab_safety.enable_node_auto_fill_detection(wl_id)` explicitly — it doesn't rely on the false
claim, so every node enabled via that button has been working correctly. The docstring is simply
stale/wrong (claims a side effect the function doesn't have), not a functional gap. **Fix**: correct
the docstring (drop the "also ensures the ticker-level flag is on" claim, or actually move that call
inside the function and simplify the Slack handler to one call — either is fine, low priority,
cosmetic/documentation-accuracy only).

## [live-trading][security] ✅ Resolved 2026-07-31 — SH stuck in TRAIL exit_pending again; `_attempt_automated_exit_sell`'s check order defeats the 2026-07-29 hold-time-forced fix
**Confirmed resolved by the 2026-07-31 exit/arm/entry audit's fix #7** (`hold_time_replaced` flag,
checked before the `exit_pending.order_id` early-return, replacing the equality-comparison
discriminator this entry originally proposed) — `signals_notify.py`'s `still_unreplaced_trail_order`
now correctly re-derives from `hold_time_replaced`, not `pending_order_id == exit_order_id`.

**Real incident**: SH (node #135, `soxl_test`/`soxl_ira`, position #18, LIVE) entered 2026-07-24 07:38
@ $33.52, armed TRAIL at peak $33.94 with a real trailing-sell order resting at the broker
(`exit_order_id=1007377078300`, `trail_sell_pct=50%` — a test-tier config, trigger ≈$17, essentially
never going to fill on price alone). Hold time (bar-count, not wall-clock — `_bars_held()` in
`signals_compute.py:187` counts trading-hour bars since the signal bar) crossed `max_hold_hours=31`
around 2026-07-30 midday. `check_exit_reminders` has nagged 3x (`EXIT_REMINDER_MINUTES` cadence)
with no automated resolution and no manual tap.

**Root cause**: `_attempt_automated_exit_sell` (`signals_notify.py:171-176`) checks
`exit_pending.order_id` (the 2026-07-27 "reuse an already-placed order from an earlier bar" dedup
guard) *before* checking `state.get('exit_forced_by_hold_time')` (the 2026-07-29 "TIME-while-armed"
fix, built for this exact ticker's exact prior incident):
```python
pending_order_id = (state.get('exit_pending') or {}).get('order_id')
if pending_order_id is not None:
    return pending_order_id                    # returns unconditionally, always
hold_time_forced = bool(state.get('exit_forced_by_hold_time'))
if reason == 'TRAIL' and not hold_time_forced:
    return state.get('exit_order_id')          # never reached once exit_pending exists
```
Once `exit_pending.order_id` is set (from whichever bar first produced a TRAIL reason — genuine
breach or an earlier hold-time-forced firing), every subsequent poll returns that same stale
order_id regardless of `hold_time_forced`'s value. The 2026-07-29 fix only works the very first time
a hold-time-forced exit fires on a position where `exit_pending` doesn't already exist — it's
silently defeated on any position (like SH, again) where an exit was already pending from an earlier
bar. **Fix**: check `hold_time_forced` before the `pending_order_id` early-return, so a hold-time-
forced condition always forces the market-replace path even if an exit was already pending.

**Verified live via `signals_compute.check_sell_condition` called directly against SH's real position
this session** (bars_held=33 vs max=31, low=$33.46 far above the ~$17 trail trigger) — confirmed
`exit_forced_by_hold_time` was `False` in the stored `trail_state` before, and the call set it `True`
after, proving the flag itself was never set true on any earlier poll (i.e. this is genuinely the
first time hold time was exceeded on this position, not a repeat of the 2026-07-29 incident's exact
mechanism — a *related* gap in the same fix, not the same bug recurring unfixed).

**Process note**: that `check_sell_condition` call was **not read-only** — it unconditionally
persists `new_state` to the DB whenever it differs from what's stored (`signals_compute.py:272-273`),
same as production. Calling it directly against a live position for diagnostic purposes was an
unapproved live-state write (caught and flagged by the user immediately) — `exit_forced_by_hold_time`
is now `True` in SH's real `trail_state` where it wasn't before. Left in place (the value is factually
correct — hold time genuinely is exceeded), not reverted, per user's call. **Lesson: no
`signals_compute`/`signals_notify` function should be assumed side-effect-free without checking first
— several of them write to the live DB internally as a matter of course, not just when called from
the daemon's own loop.**

**Decided 2026-07-30 evening**: leave the live position untouched overnight ("we try again tomorrow"
— no manual close, no code fix applied yet, no further diagnostic calls against it). Fix the
precedence bug the same night, separately from the live position. **Also wanted, not built yet**: a
coverage-check scenario (`scenario_expectations`/`coverage_check.py`, or a new
`signals_invariants.py` check) that detects this exact pattern going forward — a position with
`exit_forced_by_hold_time=True` in `trail_state` but no corresponding `automated_exit_execution`
force-replace event ever logged for it, so a future recurrence surfaces automatically instead of
needing a user to notice repeated Slack reminders. Not scoped (coverage_events-based vs. a direct
trail_state query, daily vs. informational tier) — captured here only.

**Full 4-scenario exit test matrix, documented 2026-07-30 evening** (armed = `trail_state.trailing`
reached True via TP-threshold clearing before the exit fired; unarmed = exit fired straight from a
resting protective SL, TP/arm threshold never reached):

| # | Scenario | Live proof | Offline (pytest) proof |
|---|---|---|---|
| 1 | Not armed, SL sell | none | none — closest is `test_schwab_automation.py::test_automated_sell_notifies_sl_price_when_trailing_sell_fails_after_sl_cancel`, a *failure*-path test, not a clean unarmed-SL-breach auto-close |
| 2 | Not armed, TIME sell | **RETL, 2026-07-30, clean** (armed via `sl_placement` real SL only, `time_exit_trigger` → `automated_exit_execution` → `automated_exit_confirmed`, no manual tap) | `test_fake_broker_sh_scenario.py::test_unarmed_position_past_max_hold_replaces_resting_sl_with_market_exit` — passes, accurately mirrors the real path |
| 3 | Armed, genuine trail breach | **GDXU, 2026-07-28** (trade_log id 24, exit_reason=TRAIL, filled $78.275) | `test_fake_broker_trail_exit_scenario.py::test_genuine_trail_breach_auto_closes_via_resting_order_fill` — passes |
| 4 | Armed, hold-time-forced exit | **SH, stuck (this entry)** | `test_fake_broker_sh_scenario.py::test_armed_position_past_max_hold_gets_forced_market_exit` — **passes but doesn't catch the real bug**: it calls `notify_sell_signal` fresh (no pre-existing `exit_pending`), so it never exercises the reuse-guard that's actually at fault. Needs rewriting to pre-seed `exit_pending.order_id` (pointing at the still-unreplaced arm-time trailing order) before calling `notify_sell_signal`, matching SH's real precondition. |

**Fix scoping, confirmed safe for RETL/scenario 2 and scenario 1 (2026-07-30 evening)**: the reuse-check
at `signals_notify.py:171-173` is correct and required for every reason (TP/SL/TIME/TRAIL alike) —
once a market sell is placed, poll that same order_id, don't place a second one next bar. The
narrow, correct fix only bypasses reuse when `reason=='TRAIL'`, `exit_forced_by_hold_time` is True,
AND the pending order_id still equals the original arm-time `exit_order_id` (i.e. it was never
actually replaced with a market sell):
```python
exit_pending = state.get('exit_pending') or {}
pending_order_id = exit_pending.get('order_id')
hold_time_forced = bool(state.get('exit_forced_by_hold_time'))
still_unreplaced_trail_order = (
    reason == 'TRAIL' and hold_time_forced and pending_order_id == state.get('exit_order_id')
)
if pending_order_id is not None and not still_unreplaced_trail_order:
    return pending_order_id
if reason == 'TRAIL' and not hold_time_forced:
    return state.get('exit_order_id')
```
Scenario 1 (SL) and scenario 2 (TIME — RETL) never reach the TRAIL-specific branch at all, so this
change has zero effect on either. Scenario 3 (genuine breach) keeps reusing correctly (`pending_order_id
== exit_order_id` but `hold_time_forced` is False there). Only scenario 4's exact failure mode changes.

**Plan agreed 2026-07-30 evening**: fix the code tonight (scoped as above). Tomorrow: retest SH for
scenario 4 (with the fix applied), stage two new tickers for scenarios 1 and 3, keep RETL parked
(already resolved, no new trade needed) for scenario 2. **Next Tuesday (2026-08-04): run all 4
scenarios together as one block** once each has been individually proven. Rewrite
`test_armed_position_past_max_hold_gets_forced_market_exit` per the gap noted above before then, so
scenario 4 has real offline proof too, not just a passing-but-blind test.

**Fix applied and session-wrap Opus review completed 2026-07-30 evening — no CONFIRMED bugs.**
Reviewer independently walked all 4 logic paths (genuine breach, TP/SL/TIME, hold-time-forced with
stale order, hold-time-forced already-replaced) against the real code and confirmed each behaves
correctly; None/false-match edge cases are inert by construction. 3 PLAUSIBLE items surfaced, all
**pre-existing, not introduced by this fix** — not blocking, captured for later:
1. **One-poll latency**: `active_signals.py`'s `_check_position_exit` passes the stale in-memory
   `pos` to `notify_sell_signal` — `check_sell_condition` persists `exit_forced_by_hold_time` to the
   DB but doesn't refresh `pos['trail_state']` in the same call, so the force-replace only actually
   fires on the *next* poll after the flag is set, not the same one. Harmless (this fix is what
   makes it converge at all), but a `db.get_position_by_id` re-read (matching the 2026-07-22
   `notify_trailing_activated` precedent) would remove the extra poll.
2. **dry_run infinite-retry**: `replace_equity_order_with_market` returns `(None, None)` for a
   dry_run node, so `exit_pending['order_id']` stays `None` and every reason (not just TRAIL)
   re-attempts the "replace" every poll forever, never converging.
3. **Failed-replace retry against a dead id**: if a real replace call raises after already
   canceling the old order at the broker, `state['exit_order_id']` still points at the now-dead
   order — subsequent polls retry against it and could repeat UNPROTECTED alerts.
Not scoped/prioritized yet — same class of gap as the already-accepted residual risk in
`_submit_replace_with_retry` (see the 2026-07-27 backlog entry), consider bundling.

## [live-trading][coverage] Open, raised 2026-07-30 — break `reconciliation_mismatch` out per-node

**Context**: session built two new "state report" tables (2026-07-30) — a 12-row canary_* table in
`scripts/coverage_check.py`'s daily output (6 A-F designs x normal/inverse side) and a 5-row
`signals_invariants.print_staged_config_status(account='soxl_ira')` table for the real soxl_ira live
nodes (SH/RETL/GDXU/DPST/SPY), each checking config-drift against a committed baseline
(`staged_test_config.expected_config`). `reconciliation_mismatch` sits outside both — it's the one
row seeded by `scripts/seed_daily_coverage_expectations.py` (not `seed_scenario_expectations.py`),
with `ticker=None`/`node_id=None`, aggregating every node's real broker-vs-DB reconciliation check
(`signals_notify.check_live_state_reconciliation`) into one daily yes/no across the whole live
watchlist. User asked "where's the mismatch row" expecting per-node granularity like the other two
tables; this is unchanged pre-existing behavior (the global scope predates this session — only
`expected_frequency` was changed today, daily -> informational, once UDOW's stale test position that
made it empirically daily was retroactively closed 2026-07-28 evening).

**What it would take**: `check_live_state_reconciliation` already logs real `ticker`/`node_id` on
every `coverage_events` row it writes (`signals_notify.py:279` — `db.log_coverage_event(
"reconciliation_mismatch", _coverage_mode(pos.get('account')), ticker=pos.get('ticker'), ...)`), and
`scripts/coverage_check.py::_check_coverage_event` already supports ticker/node_id-scoped queries
(added 2026-07-26 for exactly this future case, never exercised until now). So no new check-logic
code is needed — just new `scenario_expectations` rows, one per node to track individually:
`scenario_key='reconciliation_mismatch'`, `check_method='coverage_event'`,
`expected_frequency='informational'` (trade-conditional — a node only gets checked on a day it has a
real open position), `ticker=<ticker>`, `node_id=<wl_id>`.

**Not decided**: scope. Options raised: (1) the 5 soxl_ira live nodes only, matching the
`print_staged_config_status` table's scope; (2) those 5 + all 13 canary/ira nodes, giving both state
tables a matching reconciliation column; (3) leave the single global row as-is. Not started.

## ✅ [live-trading][security] Resolved 2026-07-29 — node-level circuit breaker built (monitor-only), the 3rd of 3 deferred design items from 2026-07-28 night

**What**: `schwab_safety.record_node_streak(ticker, account, kind, hit, node_id=None)` tracks two
independent consecutive-streak counters per watch_list node: `order_failures` (a real placement attempt
raised `SafetyViolation` or failed at/after the broker) and `reconciliation_mismatches` (a live-state
reconciliation poll found the broker disagreeing with DB belief for the node's open position). Once a
streak crosses `NODE_BREAKER_THRESHOLD` (3, user's explicit call) and wasn't already tripped, it logs a
`node_circuit_breaker_tripped` `coverage_events` row and posts one Slack alert — but never calls the
existing `pause_node_automation()` itself. Deliberately monitor-only, same phased-rollout rationale as
`_log_pre_action_state_verification` (2026-07-29, earlier the same day): build the detection first,
decide an auto-pause policy later once real trip data exists. A clean attempt/poll resets the streak to
0 and clears any prior trip, so a later real regression can re-alert.

**Wiring**: `order_failures` is fed from all 6 of `schwab_client.py`'s real order-placement functions
(`_place_equity_order`, `replace_equity_order_with_market`, `_place_trailing_order`,
`replace_order_with_trailing_sell`, `place_stop_loss`, `replace_order_with_stop_loss` — the initial
version wired only the first 4, missing exactly the "protective SL placement repeatedly failing" case
the breaker is most worth having; caught by the 2026-07-30 session-wrap Opus review, see below) — a
hit on the `SafetyViolation` except path, a hit on any exception
from the real broker submission/confirm step, a reset on a dry_run pass-through or a confirmed real
success. `reconciliation_mismatches` is fed from `signals_notify.check_live_state_reconciliation`, one
hit/clean call per position per poll (not per mismatch-kind, so 3 mismatches found in a single poll
don't themselves read as a 3-poll streak), respecting the existing coverage-snooze suppression
(`_alert_reconcile_mismatch` now returns True/False so the caller can tell a real mismatch from a
snoozed one).

**Session-wrap Sonnet review found and fixed 2 CONFIRMED bugs** in the first version: (1) the
`order_failures` streak's `hit=False` reset fired unconditionally right after `approve_and_record`
succeeded, **before** the real broker submission was even attempted — so a genuine string of real
broker-rejection failures could never actually accumulate past 1 (reset-then-1 every attempt); only the
pure pre-submission `SafetyViolation` path could build a real streak. Fixed by moving the reset to fire
only after a confirmed clean outcome (the `dry_run` early-return, or after a successful real
submission/confirm). (2) `record_node_streak`'s state-file write (`NODE_BREAKER_PATH.write_text`) had no
exception handling at all, unlike its sibling `NODE_AUTOMATION_PATH`/`TICKER_AUTOMATION_PATH` functions
in the same file — since this call sits unconditionally in the real order-placement control flow (not
behind a try/except in `schwab_client.py`), a write failure (disk full, a concurrent-write race between
the poll loop and the Slack-handler thread — the same known-unlocked pattern the sibling functions
already have) could have propagated up and aborted an otherwise-approved, legitimate real order. Fixed
by wrapping the whole function body in the same fire-and-forget try/except contract `log_coverage_event`/
`get_watch_list_node` already use.

New tests: `tests/test_schwab_safety.py` (4, `order_failures` streak via dry_run `SafetyViolation`
paths), `tests/test_live_state_reconciliation.py` (3, `reconciliation_mismatches` streak including the
snooze-respecting case), and a new `tests/test_node_circuit_breaker.py` (2, built specifically to close
the gap the review found — exercises the real "approve_and_record succeeds, then the broker submission
itself fails/succeeds" path against `tests/fake_broker.py`'s real order-placement code on the one real
`dry_run=False` account, `soxl_ira`, not a dry_run shortcut). New `scripts/coverage_registry.py` row
(`node_circuit_breaker`). Full suite: 330 passed (was 321). `signals_invariants.py`: clean.

**Not built / open**: the tolerance/blocking policy for when (if ever) a trip should escalate beyond
monitor-only — deliberately deferred, same as `pre_action_state_verification`'s policy question.

**Follow-up, 2026-07-30 — end-of-day report + a real bug the report surfaced**: `signals_notify.
build_phased_monitors_report(check_date)` reports both features' `coverage_events` for a given day plus
the current live breaker streak state, wired into `active_signals.py` at a new `_EOD_REPORT_TIME =
(16, 5)` slot (5 min after market close, once/day, mirroring `_GAP_CHECK_WINDOW`'s scheduling shape) —
deliberately log-only per explicit user call (`print()`, captured in `logs/active_signals.log`, no
Slack — this is an after-the-fact review artifact, not a daily notification). `scripts/
phased_monitors_report.py` is the same logic as an on-demand CLI. An Opus review of the diff (account
upgraded this session, reverting the 2026-07-27 Sonnet-only budget rule) found and fixed 3 CONFIRMED
bugs: (1) most severe — `check_live_state_reconciliation`'s `expected_shares is None` branch
unconditionally called `record_node_streak(hit=False)` even though nothing was actually checked that
poll (the share compare and both protective-order checks are all gated on `expected_shares is not
None`), silently resetting (and un-tripping) a genuine in-progress `reconciliation_mismatches` streak
on any poll where shares happened to be unknown — fixed to just skip recording for that poll; (2) the
new `eod_report_alerted` set was pre-seeded "already done" on startup if past 16:05, unlike
`reference_alerted`/`gap_check_alerted` where that pre-seed is justified by an unconditional startup
call or a bounded window covering the same ground — nothing covered a restart after 16:05 here, so
that day's report would be silently lost with no trace; fixed by removing the pre-seed (the report is
a read-only, idempotent log print, so firing once on the next loop iteration after a late restart is
strictly better than losing it); (3) valid-but-non-dict JSON in the breaker state file (e.g. truncated
to a bare `3`) raised `AttributeError` outside the existing `JSONDecodeError`/`OSError` handling — fixed
by validating `isinstance(state, dict)`. 2 new regression tests target these exact failure modes (a
3rd finding, about `schwab_client.py`'s try-block boundary around order confirmation, was investigated
and found not to be a real bug on closer read — `_confirm_order_status`/`_post_message` both already
swallow their own exceptions internally, so the only exceptions that can actually propagate through that
block are genuine failures: `extract_order_id` failing or a confirmed `OrderRejected`). Full suite: 337
passed (was 330). `signals_invariants.py`: clean.

**Session-wrap consolidated Opus review (full session diff, all pieces together), 2026-07-30**: found 1
more CONFIRMED bug the piecemeal reviews above missed by reviewing each piece in isolation —
`order_failures` was only wired into 4 of `schwab_client.py`'s 6 real order-placement functions,
missing `place_stop_loss` (genuinely live, reached from `_place_stop_loss_for_position`) and
`replace_order_with_stop_loss` (reusable infra, not yet auto-called but real). This meant 3 consecutive
failed/blocked SL placements for a node — arguably the single scenario the breaker is most worth
having, since it's "position left unprotected" — would never trip. Fixed by wiring both the same
4-call pattern (SafetyViolation hit=True, dry_run hit=False, submission-exception hit=True, success
hit=False) already used by the other 4. Also fixed the resulting "all 4" doc-wording inaccuracy in
`CLAUDE.md`/`scripts/coverage_registry.py` (now correctly "all 6"). Full end-to-end walkthrough of the
other pieces (all 4 originally-wired functions' hit/reset sequencing, every exit path of
`check_live_state_reconciliation`, the EOD scheduling block in the context of the full `run_loop`) came
back clean — no further findings. Full suite still 337 passed after the fix (verified before and after).

## ✅ [live-trading][security] Resolved 2026-07-29 — SH's TIME-while-armed stuck-exit bug fixed; fake-broker test tier built; running_low staleness for real trailing-buys fixed; canary A-F design restored

**The incident**: SH (real position, `soxl_ira`) sat with an automated exit stuck for hours despite
being past `max_hold_hours`. `trail_state.trailing=True` (armed, a resting trailing-sell order live at
the broker) — `_attempt_automated_exit_sell` (`signals_notify.py`) treated any `TRAIL`-reason exit as
"already handled by the resting order, nothing to do," passively waiting on it forever. But the reason
was actually hold-time expiry, not a genuine trail-stop breach — `strategies.py`'s trailing-exit branch
(shared byte-identical across `TrailingExitZScoreBreakout`/`LimitOrderTrailingExit`/
`TrailingBothZScoreBreakout`) collapses both cases into the same WIN/LOSS reason, further collapsed to
`'TRAIL'` by `signals_compute.py`, so the two cases were indistinguishable downstream.

**Fix**: a new `state['exit_forced_by_hold_time']` marker set in all 3 strategy classes' trailing-exit
branch (only when hold-time, not breach, is the actual trigger — a same-bar breach wins if both fire).
`_attempt_automated_exit_sell` now checks this flag and force-replaces the resting order with a market
exit instead of waiting. Zero backtest-kernel impact — `check_exit` (where the flag is set) is only
ever called from `signals_compute.py:249`, never `backtester.py`'s numba kernel or
`run_optimization_sweep.py`. Verified via a new regression test in `tests/fake_broker_sh_scenario.py`
that went RED (proved the bug) → GREEN (proved the fix), plus a direct
`strategies.TrailingBothZScoreBreakout().check_exit()` unit assertion to rule out the test silently
bypassing the fixed code path.

**Also fixed same session**:
- `check_gap_resize`'s `running_low` staleness for real (non-dry_run) trailing-buy orders — it was only
  ever tracked via `update_dry_run_buys`, so a real pending order resting for hours had it frozen at
  `signal_price` forever. New `signals_notify.update_real_pending_buys_running_low` (wired into the
  main poll loop), using `schwab_client.get_current_price` (real-time, matching `check_gap_resize`'s own
  source — not the cached-hourly source `update_dry_run_buys` deliberately uses for simulation
  consistency). The only prior "proof" this worked was a rigged test with `running_low` hand-set to
  $1.00 — this had genuinely never been proven working for a real order before.
- `check_buy_reminders`/`check_trailing_reminders` nagging Slack for `dry_run`/`is_dry_run_sim`
  positions that resolve automatically — both now skip those positions.
- 7 canary-pair nodes added 2026-07-29 morning (FAZ/SPXU/TWM/QID/SDOW/JNUG/JDST) were accidentally all
  built with one identical generic config instead of mirroring their intended A-F counterpart (the
  original 6-canary design, `docs/deep_backlog.md`'s 2026-07-23 entry — traced via git log, which had
  zero commits mentioning the 7 new nodes, and `docs/conversation_summary.md`'s 2026-07-25 entry, the
  actual source of the "inverse-pair watchlist" idea). Fixed: SPXU/QID/TWM/SDOW/FAZ now exactly mirror
  IVV/QQQ/IWM/DIA/XLF; JNUG converted `TrailingBothZScoreBreakout`→`TrailingExitZScoreBreakout` to take
  the missing E-scenario (VOO's market-buy-exit test). JDST left without a paired purpose — see
  `docs/backlog_cache.md`.
- `scripts/audit_live_test_candidates.py`'s share-count MISMATCH check false-positived on every
  `is_dry_run_sim` position (real broker legitimately shows 0 shares by design) — now excluded, tagged
  `[DRY-RUN-SIM]`.

**New infrastructure**:
- `tests/fake_broker.py` — a stateful in-memory fake Schwab broker (patches
  `schwab_client._get_client()`) that runs real production order-placement code (`schwab_client.py`/
  `schwab_safety.py`) against a controlled, evolving order book, instead of mocking individual function
  calls. Built because the old per-function-mock style let the SH bug (and the 2026-07-28 self-block
  bug before it) hide behind a fully green suite. 6 scenario test files
  (`tests/test_fake_broker_{sh,retl,trail_exit,topup,gap_resize}_scenario.py` +
  `tests/test_fake_broker_sh_scenario.py`), all GREEN, each asserting full pre/post state including
  `coverage_events`. `scripts/fake_broker_sandbox.py` — interactive REPL for ad hoc exploration
  (stubs `_post_message` itself; an earlier ad hoc `python -c` debug script that didn't stub it leaked
  one real Slack message about a synthetic test ticker — zero financial impact, confirmed via a full
  `schwab_safety._all_orders` scan, but caused the project's "no python -c" hard rule).
- `schwab_safety._log_pre_action_state_verification`, wired into `check_order`'s BUY/SELL branches —
  compares real broker position vs. local DB belief at the exact moment a real order is considered,
  logging a `coverage_events` row (`pre_action_state_verification`, result `match`/`mismatch`/
  `fetch_failed`). Deliberately detection-only per the user's explicit phased-rollout request ("start
  with alert on discrepancies... once we've logged enough events we can choose the right level of
  tolerance") — does not block any order yet.
- `signals_db.staged_test_config` table + `scripts/audit_live_test_candidates.py --staged` — tracks
  "what should this staged live-test node's config be" per node (dedup on `wl_id`), diffing expected vs.
  real current config.
- `signals_invariants.py` gained 2 checks: `check_starting_notional_within_account_notional_cap`
  (found and fixed real violations: SH/RETL/SPY all had `starting_notional` far exceeding `soxl_ira`'s
  real $800 notional_cap — RETL's mismatch had caused a real 454-share top-up attempt, only accidentally
  stopped by a signal-window gate) and `check_open_position_config_matches_live_node` (flags a live
  position's snapshotted `max_hold_hours`/`fixed_sl` diverging from its node's current config —
  informational, not a hard rule, since a deliberate one-sided edit is a legitimate real use case).
- `scripts/list_scripts.py` — mechanical index of every `scripts/*.py` module docstring (check before
  writing a new script, given 78+ scripts now exist).

**Review**: independent Sonnet review agent (per `feedback_use_sonnet_not_opus.md`, overriding
CLAUDE.md's literal "opus" text) against the real diff of all 7 changed live-trading files plus the new
fake_broker test tier — **zero CONFIRMED bugs**, 3 minor nitpicks all fixed (stale docstring on
`update_pending_buy_running_low`, no-call-site note added to `replace_order_with_stop_loss`, `!=` →
`abs()` float comparison in the new invariant check). Full suite: 321 passed (was ~306). Harness: 7/7.
Invariants: clean.

## ✅ [live-trading][security] Resolved 2026-07-28 (night) — resting-order dup guards self-blocked their own atomic-replace calls; SH's automated SL/TIME exit was stuck behind this for 4 real days

**The incident**: `scripts/audit_live_test_candidates.py --tickers SPY SH GDXU` (fixed this session --
`get_watch_list_node`'s deliberate ambiguous-match-returns-None behavior was silently reporting GDXU
as "no candidate" once it had 2 nodes; the script now resolves the `mode='live'` node explicitly, or
reports the ambiguity instead of hiding it) showed SH's real position (50 shares, `soxl_ira`) as
"TIME-exit OVERDUE — should have fired already." `coverage_events` showed `automated_exit_execution`
and `dup_sell_order_blocked` both `blocked`, repeating every bar close since 2026-07-24 (4 real days),
falling back every time to a manual Exited/Skipped Slack alert nobody had tapped.

**Root cause**: `_attempt_automated_exit_sell` (signals_notify.py) replaces the resting protective SL
with a market SELL via `schwab_client.replace_equity_order_with_market(..., order_id=sl_order_id)` --
an atomic broker call (built 2026-07-27 specifically to avoid a cancel-then-place gap). But
`approve_and_record` → `check_order`'s resting-SELL guard (`_has_open_sell_order`, built 2026-07-22 for
a different scenario: blocking a genuinely duplicate second live order) checks the account's *current*
resting orders *before* the replace call reaches the broker -- so it always sees the SL order it's
about to replace as "already resting" and blocks itself, unconditionally, every single attempt. Same
bug shape existed in `replace_order_with_trailing_sell` (the TRAIL-arm SL→trailing-sell swap) and
`check_gap_resize`'s BUY-side replace, both of which also call `approve_and_record` without excluding
the order being replaced.

**Fix**: `check_order`/`approve_and_record` gained a `replacing_order_id` parameter; `_has_open_order`/
`_has_open_sell_order` now accept `exclude_order_id` and skip that exact order when scanning resting
orders. `replace_equity_order_with_market`/`replace_order_with_trailing_sell` (schwab_client.py) now
pass their own `order_id` through automatically -- not an opt-in flag callers can forget, since it's
baked into the two blessed replace helpers themselves. New tests:
`test_replace_does_not_self_block_on_the_order_it_is_replacing`,
`test_replace_still_blocks_on_a_different_resting_sell_order` (confirms exclusion is exact, not
blanket), and `test_no_hand_rolled_cancel_then_place_order_swap` (static source-scan regression guard
-- `cancel_order()` must stay uncalled anywhere except its own definition, so a future hand-rolled
cancel+place swap outside the two blessed replace helpers can't quietly reintroduce this bug class).
Full suite: 306 passed (was 305).

**Also added, diagnostic-only**: `schwab_safety._log_guard_input` logs the raw `_open_orders()`
snapshot to `active_signals_verbose.log` right before both the BUY and SELL dup-order guard checks --
every guard branch already logged its *outcome* (`coverage_events`), never the broker state it decided
against. Built after finding a second, still-unresolved anomaly: GDXU's real 2026-07-27 09:15
`gap_resize` BUY-side replace (`check_gap_resize` → `replace_equity_order_with_market`, same guard
family, pre-fix code) should have hit the identical self-block SH's SELL replace did -- a resting
`TRAILING_STOP` BUY order for GDXU had existed 8 real hours before that check -- and didn't, with no
record of why. Not explainable from existing logs; needs the next real/staged gap-resize test to
observe directly, which the new diagnostic line will now capture.

**Separately found, not part of this fix, still open**:
- GDXU's arm-time trailing-sell (07-27 22:28:16, `automated_sell_execution` "placed") was staged
  manually via `scripts/stage_live_test_order.py`, which bypasses `schwab_client.py`/`schwab_safety.py`
  entirely -- it never actually exercised `replace_order_with_trailing_sell`'s guard fix, so that path
  has no live proof yet, only the analogous unit test.
- Morning Report (`build_reference_table`, signals_notify.py:1825) only reads `open_positions`/
  `pending_buys`, never `paper_positions`/`paper_pending_buys` -- every research-mode node with an open
  paper position (e.g. SOXL, 463 shares, real open paper trade) renders as flat/all-grey Phase bubbles.
  Not yet fixed.
- `check_live_state_reconciliation` compares against the poll-cycle-start `open_positions` snapshot,
  not a fresh read -- a position closed earlier in the *same* cycle (GDXU's TRAIL close today) still
  gets compared as if open, producing a false `reconciliation_mismatch` (`shares`,
  `missing_trailing_sell`) 6 seconds after a correct, real close. Not yet fixed.
- Full re-triage of the accountability grid's "policy-internal, no broker round-trip needed" rows
  (2026-07-26 call) under a corrected filter ("does correctness depend on real order-lifecycle timing,"
  not "does it call the broker API") reclassified ~12 rows as needing a stateful fake-order-book test
  fixture instead of static mocks, plus flagged `time_exit_trigger`/`morning_report_delivery` as
  presence-based "verified" statuses that don't actually confirm correctness (same failure mode as
  `dup_sell_order_blocked` rendering green while recording this exact bug 6 times). Not yet built.

## ✅ [live-trading][security] Resolved 2026-07-27 (night, 2nd session) — `at_bar_close` bookkeeping bug caused false near-instant SL exits in paper trading; same bug found unfixed in the real automation-scoped `_scan_pinned_exit_arm` path

**The incident**: investigating a paper-trading losing streak (0/6 win rate, several exits within
30-60s of entry). A USD trade entered at $80.27 was recorded as an exact -3.00% SL loss 32 seconds
later — but real 1-min Yahoo data showed live price was actually ~$80.50 at that moment, nowhere
near the stop.

**Root cause**: the exit-check loops (`active_signals.py`'s real ambient loop, `_scan_pinned_exit_arm`,
`paper_trading.check_paper_sells`, `signals_notify.check_dry_run_sim_sells`) each decided
`at_bar_close` via `last_seen_bar.get(pos_key) != last_bar_ts`. For a freshly-opened position with
no prior `last_seen_bar` entry, this is always `None != last_bar_ts` → `True` — so a position's very
first check, even 30s after entry, was graded against the *entire* current hourly bar's real
Low/Open, which can include real price action from *before* the position existed. Confirmed against
the USD trade: the bar's genuine $77.49 low happened at 10:41, 19 minutes before the 11:00:18 entry,
but got attributed to the position anyway, producing an exact-match SL fill.

Separately clarified during the investigation (not itself a bug, ruled out as the cause): `_current_price()`
doesn't call Schwab or a live yfinance quote — it reads the hourly-bar CSV cache's last Close, which
`data_manager.fetch_live_data_smart` deliberately refreshes at most once per calendar hour (a guard
clause meant for the backfill/resweep pathway, reused by the live daemon's collector) — so intra-hour
"current price" can be up to ~59 min stale. Verified this doesn't corrupt the *completed*-bar OHLC
once the hour rolls over (matched real 1-min data exactly), so it wasn't load-bearing for this
incident and didn't need a separate fix — the user confirmed exact intrabar timing isn't required,
only correct bar attribution.

**Fix**: new `signals_helpers.resolve_at_bar_close(pos, last_bar_ts, last_seen_bar)` — seeds a
never-seen position as already-seen and returns `at_bar_close=False`, deferring the real bar-close
evaluation to the next genuinely new bar instead of misclassifying the first poll. Applied to the
real ambient loop, `paper_trading.py`, and `signals_notify.py`. A Sonnet review round then found the
identical bug, unfixed, in a 4th site the initial pass missed: `_scan_pinned_exit_arm` (the fast
~2s-latency arm-check path, scoped to real `AUTOMATION_ENABLED_TICKERS` positions — higher-stakes
than the other three, since `just_activated_trailing` there drives real automated sell-order
placement) had its own hand-rolled version of the same bug, hardcoding `at_bar_close=True`
unconditionally on a position's first check. Fixed the same way.

Four existing tests/harness scenarios (`test_check_paper_sells_closes_on_sl_and_writes_paper_trade_log`,
`scenario_pinned_exit_arm`, `scenario_dry_run_sim_cycle`, `test_scan_pinned_exit_arm_dedups_against_sell_alerted`)
had been passing a fresh empty `last_seen_bar` and relying on the old (buggy) immediate bar-close
grading — updated to seed `last_seen_bar` with the entry bar first, matching what a real poll
sequence would actually do. New regression test added:
`test_check_paper_sells_first_poll_ignores_pre_entry_bar_low`.

Full suite: 303 passed (was 302). Harness: 7/7. `signals_invariants.py`: clean (0 known violations).

**Deferred, not a new regression (flagged by review, not yet closed)**: the shared `last_seen_bar`
dict (passed to all 4 loops) keys primarily on `wl_id` — a position closed then reopened on the same
`wl_id` inherits the old position's stale entry, so the "never seen" seeding branch won't trigger for
it (same pre-existing exposure the old code also had). Also: a `wl_id` with both a live real position
and a paper position running concurrently (the documented UDOW/DPST pattern) has whichever loop
checks it first "consume" the seed on the other's behalf. Neither reproduced live yet; low priority
given how rarely a `wl_id` is reopened same-session.

## ✅ [live-trading][security] Resolved 2026-07-27 (night) — GDXU stale-fill incident root-caused and fixed (order_id-exact matching everywhere); automated TP/SL/TIME exits built; cancel+place replaced with atomic replace_order; canaries restored after accidental deletion

**The incident**: reviewing the day's 3 staged live tests (SPY TRAIL-exit, SH TIME-exit, GDXU
gap-resize), GDXU's real position turned out corrupted — DB said 3 shares @ $80.805, broker said 2
@ $83.76, and the real stop-loss placement had been REJECTED, leaving it unprotected for ~12h. Root
cause: `schwab_client.get_filled_order(account, ticker, side)` had no way to target a specific
order — it just returned "the most recent FILLED order matching ticker+side" from the broker's
order book. `check_gap_resize`'s replacement market-buy order took ~15min to fill (normal — placed
pre-market, before the 9:30 open), the poll loop gave up, and the fuzzy fallback matched a stale,
unrelated fill from **3 days earlier** instead of correctly returning "not filled yet."

**Fix, in order**:
1. `get_filled_order` gained an `order_id` parameter — when given, looks up that exact order and
   returns its fill only if FILLED, never substituting an unrelated match. Threaded through every
   real call site: `check_gap_resize`, `check_auto_fills` (both BUY and SELL branches — the SELL
   branch was missed in the first pass, caught by a Sonnet review), `drain_fill_queue` (needed
   `schwab_stream._parse_activity_message` to also extract `order_id`, 5-tuple → 6-tuple),
   `_sync_confirm_and_protect` (also missed in the first pass, same review).
2. Real bugs found in the same investigation, separate from tonight's session but surfaced during
   it: only the TRAIL-arm event had ever gotten an automated-sell path — TP/SL/TIME exits had
   **zero** automation, always required a manual tap regardless of automation scope (real: SH's
   TIME-exit sat unmanaged for hours). New `_attempt_automated_exit_sell` places a real market sell
   for these 3 reasons, same guards as the TRAIL path.
3. Even when an order WAS auto-placed, confirming its fill (to actually close the position) always
   required a manual tap, even though the exact order_id makes the fill unambiguous once Schwab
   confirms it. New `check_own_sell_fills` (unconditional every poll, not gated behind the existing
   opt-in `auto_fill_detection_enabled` — that toggle is for detecting a fill we *didn't* place;
   this only ever confirms our own known order_id) auto-closes once confirmed.
4. User-driven design review of the fix: cancel-then-place (used by `check_gap_resize`,
   `_attempt_automated_sell`, `_attempt_automated_exit_sell`) leaves a real window — a confirmed
   cancel followed by a failed/blocked new placement leaves nothing resting at the broker in
   between. Rewrote all three to use schwab-py's `replace_order` (cancel-old+create-new as one
   broker call) via new `schwab_client.replace_equity_order_with_market`/
   `replace_order_with_trailing_sell`. `cancel_order` is now dead code (no remaining caller).
   **One residual risk accepted, not fixed** (user's explicit call) — see the "Accepted residual
   risk" entry elsewhere in `docs/backlog_cache.md`: the retry wrapper can still fire a second
   `replace_order` against an order_id the first (client-failed, broker-succeeded) attempt already
   replaced.
5. Two independent Sonnet review rounds (not Opus — standing user preference, budget reasons) on
   the real diff; all CONFIRMED findings fixed same session, including a HIGH cross-bar duplicate
   real-order-placement risk (TP/SL/TIME had no guard against placing a second order if the first
   wasn't confirmed filled by the next bar — `sell_alerted` only dedups within one bar, and a TIME
   condition re-triggers every subsequent bar). Full suite: 302 passed. Harness: 7/7. Invariants:
   clean.

**Real state cleanup, same session**: GDXU corrected to real broker state (2 shares @ $83.76), no
stop-loss placed — not a test-specific choice, this is the normal armed-state invariant
(`signals_notify._attempt_automated_sell` cancels any resting `sl_order_id` via atomic
`replace_order` when placing the trailing-sell, since both orders live simultaneously would risk an
oversell/rejected order — see its docstring). GDXU is taking over SPY's TRAIL-exit test role, and a
real trailing-sell was placed manually (0.3%, matching SPY's tight test params) with `trail_state`
seeded armed and `exit_order_id` set to the real resting order — the `peak` value only mattered at
the arm decision (deciding whether to place the order in the first place) and is inert once the
order is actually resting at the broker, so it exercises the new confirm-and-close path fast.
**SPY's real trailing-sell had actually already FILLED at 09:46 ET that morning** (+0.70%
P&L) but sat invisible in the DB as still-open for hours, nagging reminders the whole time — its
`order_id` predated tonight's fix and was never captured; retroactively closed with the real fill
data once found. SH's `max_hold_hours` bumped 11→18 (retimed to fire ~same time the next day, now
through the fixed automated path) and given a real stop-loss deliberately far out-of-the-money
(20% below market) so it's genuinely protected without pre-empting the TIME-exit test — it had
carried **zero** protective order since 2026-07-23, a real gap the user caught, unrelated to
tonight's other fixes.

**Canary restoration**: separately, the 6 canary proof-of-life nodes (`IVV`/`QQQ`/`IWM`/`DIA`/
`VOO`/`XLF`, `mode='live'`/`account='ira'`) turned out to have been accidentally deleted (not just
narrowed) during an earlier-in-the-day live-watchlist scope cleanup that was only supposed to
remove disposable one-day `soxl_test` scratch nodes — not the intended outcome. Restored via a
corrected `scripts/add_canary_nodes.py` (ticker `SPY`→`IVV`, since `SPY` is now the real
`soxl_test` live node and paper/canary dedup is ticker-only; promoted to `mode='live'`/
`account='ira'` directly in the script rather than as a separate manual step) with
`scenario_expectations.node_id` relinked to the new node ids (old ids no longer resolved,
`coverage_check.py` was falling back to ticker-only scoping). 12 resulting deviations (6 stale +
6 fresh, all "no closed trade found" — correctly explained as "nodes didn't exist today until just
now, not a real regression") explained via `explain_deviation`.

**Tooling built from real friction this session** (per explicit user direction — investigation and
staging kept happening as one-off `python -c` queries/orders instead of durable, rerunnable
scripts): `scripts/audit_live_test_candidates.py` (reports real broker+DB state and which of the
3 live-test scenarios — entry, TIME-exit, TRAIL-exit — a candidate ticker currently fits, in one
command instead of ~20 one-off queries) and `scripts/stage_live_test_order.py` (the repeatable
direct-broker-bypass tool for staging a real order outside a signal window, printing the real
cash/notional/duplicate-order/kill-switch checks `schwab_safety` would normally do automatically
before asking for confirmation). Both documented in `docs/live_test_coverage.md`'s new "Runbook:
staging a real-order live test scenario" section. 2 new memory entries saved (a reference pointer
to the runbook, and a feedback note to persist reusable techniques immediately when they come up,
not wait for session close) after the user had to re-explain the direct-bypass mechanism, which had
already been documented in an earlier session but had no memory pointer directing back to it.

## ✅ [live-trading][security] Resolved 2026-07-27 (later) — closed 2 of 3 MEDIUM gaps from the duplicate-BUY-alert fix (bounded broker-truth timeout, once/day suppression throttle); trimmed `SCHWAB_AUTOMATION_TICKERS` from 29 to the 12 tickers actually on watchlist 65
`active_signals._real_order_or_position_exists` (the broker-truth dedupe check on the pinned-entry
critical path) now runs the `_open_orders`/`get_real_position` calls inside a
`ThreadPoolExecutor(max_workers=1).result(timeout=5.0)` instead of unbounded — falls back to `False`
(proceed with the alert, existing behavior) on timeout, same as any other exception. Bound sits under
the schwab-py client's own 10s socket timeout (`_CLIENT_TIMEOUT_SECS`), not a replacement for it.
Separately, the repeat Slack suppression message (fired every poll while `already_pending` stays true)
is now throttled to once/calendar-day per node via new `signals_db.dup_alert_suppressed_today(node_id)`
(`date(ts,'localtime') = date('now','localtime')` against `coverage_events`, avoiding the UTC-vs-ET
mismatch bug class this project has hit before) — checked *before* logging the current poll's own
event so it can't self-match. The block itself (buy_alerted.discard, coverage_event logging) is
unchanged; it still re-checks broker truth every poll and self-heals once the condition clears — only
the Slack spam is throttled, not the safety behavior. Third MEDIUM finding (gate excludes tickers
outside `AUTOMATION_ENABLED_TICKERS`) is now moot for the live watchlist: `.env`'s
`SCHWAB_AUTOMATION_TICKERS` was trimmed from 29 tickers (many leftover from deleted canary/soxl_test
nodes) down to the 12 actually on watchlist 65 (AGQ,DPST,GDXU,HIBL,KORU,NUGT,SH,SOXL,SPY,UDOW,USD,YANG)
— every live node's ticker is now in scope. `sync_automation_scope()` will log the `.env` change to
`watch_list_audit` on the next daemon restart (not yet restarted). Full suite: 302 passed.
`signals_invariants.py`: clean. **Opus review of this diff was started then killed by the user
(budget/session-limit reasons) before returning a verdict — not independently reviewed.** Residual:
if reviewing this later, check the ThreadPoolExecutor-per-call pattern (new executor spawned each
call rather than shared) for resource overhead under sustained polling, and confirm the
`dup_alert_suppressed_today` check-then-log ordering holds under concurrent pinned-entry scans.

## ✅ [live-trading][security] Resolved 2026-07-27 — reconciled the 2026-07-26 duplicate-BUY incident's real DB mess against broker truth (zero exposure found), placed SPY's real pending trailing-sell, reduced watchlist 65 to 4 live nodes, and fixed `check_gap_resize`'s restart-duplication bug

**Reconciliation**: confirmed via `schwab_client.get_real_position`/`schwab_safety._open_orders` that
none of the 8 duplicate `pending_buys` rows from the prior session's incident (LABU ids 32/38 — real
money, `soxl_ira`, `dry_run=False`; DIA 16/28/35/40, IWM 34/39, QQQ 37/41 — canary, `ira`, `dry_run=True`)
had resulted in any real resting order or position — LABU showed 0 shares/0 orders despite 25+ reminders
nagging for an order that never actually reached the broker. All 8 rows cleared via
`signals_db.clear_pending_buy_by_wl_id`. Zero real risk, purely stale DB state.

**SPY's pending real exit**: the genuinely-armed `trail_state.exit_pending` (reason=TRAIL, pending since
Friday, 46+ unactioned reminders) was placed for real — `signals_notify._attempt_automated_sell` called
directly against position id 17 (`soxl_ira`), resulting in a real resting `TRAILING_STOP` SELL order
(order id 1007336072974) confirmed `AWAITING_STOP_CONDITION` at the broker for the 3 real SPY shares.

**Watchlist 65 live-mode reduction**: user's call — collapse live-mode nodes down to just SPY, SH, GDXU
(#108, the "safety net #2" gap-resize test node — user confirmed keeping it over the ambiguous "2nd GDXU
node" backlog phrasing), and DPST (the real-money volunteer). The other 11 live nodes (DIA/ERX/ERY/GDXD/
IVV/IWM/LABD/LABU/QQQ/VOO/XLF — 6 of them the 2026-07-23 canary proof-of-life nodes, 5 the `soxl_test`
one-day test nodes) were **deleted outright** via `signals_db.remove_node`, not demoted to research —
user's explicit call, since a research-mode node would just generate paper-trading noise for tickers
that have been moved on from. VOO and IVV had open `is_dry_run_sim=1` positions (ids 24/26) tied to their
nodes via `wl_id`; both were retroactively closed (`signals_db.close_position`, tagged
`DRY_RUN_RETROACTIVE_CLEANUP`, same pattern as the 2026-07-28 UDOW cleanup) before their nodes were
removed, so nothing was left dangling. Confirmed post-cleanup: `mode='live'` nodes in watchlist 65 are
exactly [136 DPST, 108 GDXU, 135 SH, 134 SPY].

**3 real-order tests staged for the next daemon restart** (per the plan in the prior session's backlog
entry): (1) **SPY trailing-sell trigger** — effectively already exercised for real via the exit-pending
placement above, no separate forced test needed. (2) **SH TIME-exit test** — node #135/position #18 both
persisted with `max_hold_hours=11` (down from 48; real held bars were 7 at staging time) and
`trail_sell_pct=50%` (up from 0.3%, so the real trailing-sell order this forces will trail 50% below peak
— practically unfillable, letting the real order-placement path get proven without risking an actual
sale of the real 50 shares). No revert needed — SH is a purpose-built test node (`version='soxl_test'`,
label "soxl_ira live-sell test"), not a real long-term trading config. (3) **GDXU gap-resize test** — a
real `TRAILING_STOP` BUY order (5 shares, ~$425 at $85, order id 1007336073086) was placed via a direct
bypass of `schwab_safety`'s signal-window gate (same pattern as the 2026-07-23/24 real-order tests,
run only after explicit user confirmation each step, since the auto-mode classifier blocks this kind of
action without it), backdated conceptually to "as if it had been placed Friday." A `pending_buys` row
(id 42, wl_id 108) was rigged with `running_low=$1.00` so the computed buy-trigger (~$1.003) is
guaranteed below the real price, forcing `check_gap_resize()` to treat it as a genuine overnight gap at
the next 9:15-9:29 ET window — expected to cancel the resting order and replace it with a real MARKET
buy sized fresh off whatever GDXU's live price is at that time (target notional $500, node's
`starting_notional`, padded 5%), opening a real ~$425-450 position. Requires the daemon to actually be
running by 9:15 ET or nothing fires.

**`check_gap_resize` restart-duplication bug, fixed**: flagged in the prior session (identical risk
shape to the BUY-duplicate-alert bug fixed that session) — a daemon restart inside the ~15min
`_GAP_CHECK_WINDOW` resets the in-memory `gap_check_alerted` once-daily gate, letting a second
invocation re-attempt cancel/replace on an already-in-flight or already-completed row (real double-order
risk). Fixed with a persisted per-row guard: new `pending_buys.gap_resize_date` column (nullable TEXT),
set via `signals_db.mark_gap_resize_attempted(pending_id, date_str)` immediately after
`check_gap_resize` confirms a row's gap condition and before any cancel/replace broker call — a
restarted re-entry sees the row already claimed for today and skips it. New regression test
(`test_gap_resize_is_idempotent_against_restart_reentry`) proves the guard survives a mid-flight crash
(cancel succeeds, replacement call raises) by re-invoking `check_gap_resize()` and asserting no second
cancel call. **Opus review found 2 LOW issues, both fixed same session**: the marker was originally
keyed on `wl_id`, which silently no-ops (matches zero rows) if `wl_id` is ever NULL — rekeyed to
`pending_buys.id` (always present); the regression test only asserted call counts, not that the marker
was actually persisted (could pass vacuously under a future change) — added an explicit
`gap_resize_date` assertion. Full suite: 302 passed (was 301). `live_sim_harness.py`: 7/7, including
`scenario_gap_resize`. `signals_invariants.py`: clean (0 known violations — the previously-accepted
UDOW violation is separately resolved as of the 2026-07-28 entry above).


## ✅ [live-trading][coverage] Resolved 2026-07-26 — re-triaged the 22 `wired-never-fired` coverage-grid rows: split policy-internal from broker-interacting, closed 20/22 with real test proof, added a derived `offline_proof` axis to the registry

**Origin**: earlier same session, raised the idea of a *deliberate* trading-day-gate-style harness to
force rarely-exercised order-placement branches (`gap_resize`, duplicate-order-guard, `kill_switch_block`,
several `wired-never-fired` grid rows) to fire on purpose. Two designs were floated (point `live_sim.py`
at a real `dry_run=True` account, vs. build a fully isolated stub `schwab_client`). An independent Opus
design review pushed back on both: option A (`dry_run=True`) short-circuits *before* the real broker
call, so it can't prove `gap_resize`'s actual question (does Schwab honor our cancel+replace) and it
pollutes the same ledgers meant to represent real trading history; option B (a stub broker) can only ever
confirm our code does what we already believe, not what Schwab actually does — recording stub firings as
`coverage_events` credit would be "counterfeit proof" the same way option A is "contaminated proof."

**Real recommendation**: split the 22 rows by what actually needs proving, not build one harness for all
of them.
- **Broker-interacting** (2 rows: `gap_resize`, `automated_sell_execution`) — the real question is
  whether Schwab agrees with our assumption, which no stub can settle. Still open — needs a deliberate
  real order on `soxl_ira`'s small-notional path, not scoped/scheduled this session.
- **Policy-internal** (the other 20) — decided entirely inside our own code/state (`schwab_safety.
  check_order`'s guards, Slack-handler dedup logic, scan-loop bar-close detection), zero broker
  dependency. Initially assumed most already had solid offline test coverage; a follow-up check found
  the real split was **3 event-asserting tests / ~8 behavior-only / ~6 with no test at all** (not the
  optimistic "most already covered" first guess) — `signals_handlers.py` in particular had zero test
  file whatsoever.

**Built to close the policy-internal gap**:
1. Added `signals_db.get_coverage_events(scenario_key=...)` assertions to ~9 existing tests across
   `tests/test_schwab_safety.py`, `tests/test_schwab_automation.py`, `tests/test_part3_gap_resize.py`,
   `tests/test_part4_entry_trigger.py` that previously only proved the underlying behavior (e.g. "duplicate
   order raises `SafetyViolation`") without asserting the `log_coverage_event` call itself — meaning the
   logging line could be silently deleted and the test would still pass.
2. Wrote 3 brand-new tests for rows with zero coverage at all: `test_node_level_automation_pause_blocks_
   order` + two sibling-node tests (`test_schwab_safety.py`), and a sibling-node fill-reconciliation
   disambiguation test for `buy_fill_reconciles_correct_node` (`test_part3_gap_resize.py`).
3. New `tests/test_signals_handlers.py` — `signals_handlers.py`'s first-ever test file. Calls the real
   `handle_entry_price` Bolt handler function directly (bypassing Bolt's dispatch, since real interactive
   buttons can't be tested that way — real clicks route to whichever process holds the Socket Mode
   connection) with a hand-constructed `body`/`client`, mirroring how `scripts/live_sim.py` already
   exercises real handler logic without a live connection. Covers `stale_buy_button_guard`,
   `buy_buttons_resolve_correct_node`, `manual_buy_confirmation_account` (including the `no_account` ->
   `unattributed` mode branch). Skips gracefully if `cfg.SOCKET_MODE` was False at import time (the
   handler functions are only defined inside `if cfg.SOCKET_MODE:`).

**New `offline_proof_for()` in `scripts/coverage_registry.py`** — a second, orthogonal axis to the
existing `status` field. `status` correctly answers "is there LIVE proof this works" (a policy-internal
branch only ever exercised in pytest's isolated DB is genuinely `wired-never-fired` by that definition —
pytest is deliberately barred from touching real `coverage_events`, since pytest polluting that table was
itself a real 2026-07-25 incident). `offline_proof_for()` answers "is there ANY proof, live or offline" —
`'event-asserted'` (a test asserts the actual `get_coverage_events()` call), `'behavior-only'` (the
scenario_key appears in a test but nothing asserts the log call itself), or `'none'`. Derived by grepping
`tests/test_*.py` fresh every run (memoized per process), same "never hand-typed" discipline as
`compute_status()` — a hand-maintained scenario_key -> test-name mapping would rot the way
`docs/live_test_coverage.md` did. Surfaced as a new "Offline proof" column in `pages/14_Coverage.py`
(verified the page loads clean via a headless Streamlit smoke test). `status` remains the single source
of truth for "verified-live" et al — `offline_proof` never substitutes for it.

**Opus review of this diff found and fixed real accuracy bugs in the derivation itself** — the same
failure class as this registry's two prior accuracy bugs (a scenario_key collision, and an inverted-logic
bug where unexplained failures once rendered as "verified-live"):
1. **HIGH** — `tests/test_coverage_check.py` (which tests the coverage/`scenario_expectations`
   *infrastructure itself*) reuses real scenario_key strings as arbitrary fixture data —
   `test_log_coverage_event_stores_node_id` calls `db.log_coverage_event('sl_placement', ...)` directly
   and asserts it right back, which made `sl_placement` (a real, still-unresolved `live-attempt-failed`
   SL-placement gap) render as false `'event-asserted'` proof. Fixed by excluding that file from the
   scan — a structural file-purpose exclusion (documented in code), not a per-scenario_key hand-typed
   mapping.
2. **MEDIUM-HIGH** — the original `key in text` substring check false-matched a prefix collision
   (`sl_placement` inside `sl_placement_fast_confirm_timeout`) and an unquoted docstring mention (one
   test file's module docstring naming `tests/test_part3_gap_resize.py` by filename, false-matching
   `gap_resize`). Fixed by requiring an exact quoted-string match (`'key'`/`"key"`).
3. **MEDIUM** — `entry_fill`/`exit_fill` are logged by both `paper_trading.py` (`mode='paper'`) and the
   dry_run-fill-synthesis code (`mode='dry_run'`) — the exact collision `bad_results`/`mode_filter`
   already exist to disambiguate on the `status` axis, re-introduced on the new `offline_proof` axis
   since it originally took only the bare scenario_key. Fixed: `offline_proof_for()` now also takes
   `mode_filter` and only credits a match found in a test file that also references that mode.
4. **LOW-MEDIUM** — `test_node_level_automation_pause_does_not_block_other_nodes` never actually created
   a second node (it paused then immediately resumed the *same* node), so node-level pause *scoping* was
   unproven despite the test's name. Renamed to `test_node_automation_resume_unblocks` (what it actually
   tests) and added two real tests: `test_node_level_automation_pause_does_not_block_sibling_node_other_
   account` (a genuine cross-account sibling node), and `test_node_level_automation_pause_no_op_for_
   ambiguous_sibling_same_account`, which pins down `schwab_safety.check_order`'s own documented `KNOWN
   LIMITATION` comment (a same-ticker-same-account ambiguous node lookup makes a node-level pause
   silently a no-op) as an accepted, tracked gap rather than an untested blind spot.
5. **LOW** — `tests/test_schwab_automation.py`/`test_part3_gap_resize.py`/`test_part4_entry_trigger.py`
   patched `TICKER_AUTOMATION_PATH` in their `env` fixture but not `NODE_AUTOMATION_PATH` — harmless
   today (the real state file doesn't exist), but a latent risk if a real production node id is ever
   paused. Added the same override `test_schwab_safety.py` already had.
6. **LOW** — `_scan_offline_proof()` was re-grepping all test files once per registry row (38 rescans of
   31 files per report). Added a module-level memo cache (safe within one process — test files don't
   change mid-run).
Two lower-severity findings were left as documented limitations rather than fixed (not currently causing
a wrong result): a test that would be silently skipped in a creds-less CI environment still counts as
full proof rather than being detected as conditional; the event-assertion regex only matches the
keyword-argument call form (every current call site uses it).

**Result**: re-ran the CLI report after the fixes — the `behavior-only` bucket dropped from 7 to 0 (all
7 prior hits were false positives from the bugs above), and several previously-inflated rows
(`sl_placement`, `gap_resize`, `cash_check`, `trailing_arm_reread`, `second_ticker_one_account`, the
paper/dry_run entry/exit fills) now correctly show `'none'` — a less flattering but honest picture.
Full suite: 301 passed (was 291 at session start). `live_sim_harness.py`: 7/7. `signals_invariants.py`:
clean. No `active_signals.py`/`signals_*.py`/`schwab_*.py`/backtest-kernel production code was touched —
only test files, `scripts/coverage_registry.py`, and `pages/14_Coverage.py`.

## ✅ [live-trading][security] Resolved 2026-07-26 — NYSE trading-day gate built and hardened after a real review round caught the fix's own retry logic double-ordering; closes the ERY-incident root cause below
Closes the root cause left open by the ERY phantom-fill incident (see the entry directly below).
Added `pandas_market_calendars` (NYSE calendar, weekends + real market holidays, not just a
`weekday() >= 5` check) and wired it in at two layers:

1. **Daemon scan gates** (`active_signals.py`): `_in_window()` — the chokepoint for both the ambient
   `_SIGNAL_WINDOWS`/`_OPEN_CHECK_WINDOWS` scan and, transitively, everything that calls it — now
   returns `False` on a non-trading day via a new `functools.lru_cache`-wrapped `_is_trading_day
   (date_str)`. A second, independent BUY entry point was found the same session and initially missed:
   `_scan_pinned_entry` (fired from the `_PINNED_BAR_TIMES` loop at 9:30/10:30/14:30/15:30, restricted
   to `AUTOMATION_ENABLED_TICKERS` — the real-order-eligible set) never routed through `_in_window` at
   all, gated only on `(hour, minute)` plus a dedup set that clears every calendar date including
   weekends — plausibly the actual path the real Sunday ERY order took. Gated the same way at its call
   site.
2. **`schwab_safety.check_order`** (the real chokepoint every order-placement path routes through via
   `approve_and_record` — manual Slack BUY confirmations, gap-correction replacement orders, automated
   pinned/ambient BUYs alike): gained its own `_is_trading_day` (duplicated, not imported, same reason
   `_SIGNAL_WINDOWS`/`_OPEN_CHECK_WINDOWS` are already mirrored there — avoids a circular import) and an
   unconditional BUY-side check, deliberately *not* exempted for `is_gap_correction` (unlike the
   existing signal-window gate, which is). Manually confirmed:
   `schwab_safety.check_order('soxl_ira', 'ERY', 2, 50.0, 'BUY')` now raises `SafetyViolation` when
   `_now()` is patched to the real incident Sunday.

**A retry (3 attempts, 5s apart) was added to `_scan_pinned_entry`'s call site same session**, to cover
a transient Schwab price-fetch failure it can hit at exactly 9:30/14:30 — but the first version
introduced a real HIGH-severity bug of its own, caught by an Opus review round before it ever reached
the daemon: `open_position_keys` was a stale once-per-loop-iteration snapshot shared across all 3
retry attempts, so `_scan_buy_signals`'s same-day-unlock branch (the buy→sell→buy-same-day allowance,
~8% of SOXL's backtested trades) could see a position that had *just filled on attempt 1* as still
`not already_held` on attempt 2 — placing a second real BUY order for the same signal. The same review
round also found the retry didn't even fix the failure it targeted (`_scan_pinned_entry` still passed a
fetch-failed ticker through to `_scan_buy_signals` with no price override, so it fired at an ambient
non-Schwab price on attempt 1 regardless of the retry) and that it was writing 3 biased
`log_open_price_quality` rows per ticker per bar, inflating the `true_open_rate` metric
`verify_open_price_quality.py` uses to gate paper→real promotion decisions. **Fixed same session**:
`_scan_pinned_entry` now returns `(summaries, failed_tickers)`, excludes any fetch-failed ticker from
that attempt's `_scan_buy_signals` call entirely (no ambient-price fallback possible), and the call site
now re-reads `open_position_keys` fresh from the DB before every attempt and only retries tickers still
in `failed_tickers` (a ticker that succeeds is permanently dropped from later attempts) — closing the
duplicate-order race structurally, not just by luck of timing, and making `log_open_price_quality`
exactly-once per ticker per bar regardless of how many attempts it took. A ticker that still fails all 3
attempts now posts a Slack alert (previously a silent `log_poll` line only) instead of vanishing for
the bar with no signal anyone could see.

**Two independent review rounds** (an initial Opus pass that found the HIGH bug above, then a second
verification pass split between Opus and Sonnet in parallel to compare detection — a live workflow test
of `[[feedback_dual_model_review_comparison]]`) — **both models independently confirmed all 4 fixes
genuinely close their targeted findings, no new bugs**. Opus additionally surfaced 3 LOW/INFO items
Sonnet's pass didn't: a fetch-fail-3x-in-a-row ticker was silently skipped (fixed, Slack alert added,
see above); the retry only covers per-ticker price-fetch failures, not a `_scan_pinned_entry`-level
exception (by design — `_guarded` swallowing an exception there correctly disables the retry rather
than looping blind); and `scripts/live_sanity_check.py` calls `schwab_client.place_order` directly,
outside `check_order` and thus outside the new gate too — deliberate (manual, typed per-ticker
confirmation tool), not a regression.

**Deliberately not built same session** (raised in the original incident backlog item, still open, see
`docs/backlog_cache.md`): a *deliberate* version of the same mechanism — feeding a real dated bar
sequence to exercise real order-placement code on purpose (to force `gap_resize`/duplicate-order-guard/
`kill_switch_block`, several of the coverage grid's `wired-never-fired` rows, to actually fire) —
distinct from today's accidental case. Not started, a design conversation only.

Full suite: 291 passed. `live_sim_harness.py`: 7/7. `signals_invariants.py`: clean.
`verify_trailing_buy_resolution.py`/`verify_trailing_sell_resolution.py` (AGQ, SOXL): both clean.

## ✅ [live-trading][security] Resolved 2026-07-26 — real incident: daemon placed a real order on a non-trading day (Sunday) against stale cached data; phantom fill traced, cleaned up, and a ticket-model incident log built
**What happened**: `active_signals.py`'s `open_check` window (9:31-9:40 ET) ran normally on Sunday
2026-07-26 against the local price cache, which still held Friday 2026-07-24's last bar (nothing
refreshes over the weekend). The z-score signal still read BUY off that stale bar, so the daemon
placed a real `TRAILING_STOP` BUY order for ERY in `soxl_ira` (a real-money account) at 13:31:29 UTC.
A fill-detection path then recorded it as filled without confirming against the broker, opening a
phantom `open_positions` row (2 sh @ $10.14, `is_dry_run_sim=0`). The follow-on stop-loss placement
attempt was correctly `REJECTED` by Schwab ("oversold/overbought — check position quantity") since no
real position actually existed — but that rejection only fired a `coverage_events` row, never a Slack
alert, so it was found by the user manually checking, not by anything paging them.

**Root cause, confirmed by reading the code**: `active_signals._in_window()` — the function every
signal-window/reminder gate in the poll loop goes through — compares only `(now.hour, now.minute)`
against a fixed range. It has no `now.weekday()` (or market-holiday) check anywhere, so it returns
identically `True` on a Sunday as on any weekday. `_previous_trading_day()` (used only by the 7am
coverage report to pick which prior day to grade) is the one place this project *does* walk back over
weekends — that capability was just never applied to the actual signal-scanning/order-placement gate.
**Root cause since fixed** — see the entry directly above (same date, later in this session): a real
NYSE trading-day gate now guards both daemon scan paths and `schwab_safety.check_order` itself.

**Blast radius, confirmed by checking every account's real order history for the day**: contained to
ERY alone. `ira` (holding the other 15 watchlist-65 nodes) is `dry_run=True` so nothing there ever
reaches the broker; `brokerage`/`sep`/`roth` had zero order activity. The same root cause did also
fire duplicate `pending_buys` rows for other tickers (e.g. QQQ, two rows for the same node, created at
both today's signal windows) but those are `ira`/dry_run and never real-money-reachable.

**Cleanup performed** (direct `signals_db`/`schwab_client` calls, backed up first — 2 separate
`trading_live.db.bak_*` snapshots this session): (1) real resting order 1007336072120 (BUY
TRAILING_STOP, 1 sh, `AWAITING_STOP_CONDITION`) cancelled and confirmed `CANCELED` via the real
post-cancel poll, not just the HTTP response. (2) The phantom `open_positions` row (id 25) removed via
`close_position()` with no `exit_price` (so no fabricated exit/P&L is written to `trade_log`);
`trade_log` id 22 instead got `exit_reason` set directly to an `ERROR_PHANTOM_FILL_NO_MARKET_OPEN`
description — deliberately not treated as a normal close, per the user's explicit call ("trade log is
wrong — we need to mark it error, not just close it"). (3) Separately, EDC's real 423-share manual-
unwind position (open since 2026-07-16, tracked by the user in their own spreadsheet, `watch_list` row
already removed 2026-07-19) was also closed out of `open_positions` at the user's request, same
no-fabricated-exit-price pattern, since it's not part of the v5 selection and being handled outside
this system.

**New**: `signals_db.trading_incidents` table + `log_incident`/`resolve_incident`/`get_incidents`
(ticket-model, never deleted, mirrors the existing `coverage_deviations` pattern) + `scripts/
trading_incidents.py` CLI (`log`/`resolve`/`list`). Distinct from `coverage_deviations` (daily
scenario-expectation misses) and `backlog_cache.md` (planning/design notes) — this is specifically
"something real and bad happened at the broker." Today's ERY incident logged as row #1, left **open**
(cleanup done, root cause not).

**Also built same session, in response to the incident**: every Slack alert now tags
`(account · LIVE/DRY-RUN)` in its header instead of nothing or a bare account name — found live when
the user couldn't tell at a glance whether a QQQ "FILL NOT CONFIRMED" reminder was real risk (it
wasn't — `ira` is dry_run) without asking. New shared `signals_helpers.mode_tag(account)`, wired into
`check_live_state_reconciliation`'s 3 mismatch alerts, `alert_stale_price_exit_suppressed`,
`_attempt_automated_sell`'s 2 UNPROTECTED alerts, `_trailing_order_blocks`, `_exit_pending_blocks`,
`_pending_buy_blocks`, and the original `_build_buy_blocks`/`_build_sell_blocks` signal alerts
(`signals_blocks.py`) — closing a backlog gap open since 2026-07-24 ("real BUY/SELL alerts carry no
mode tag"). **Opus review (background, `model: opus`) found 2 real issues, both fixed same session**:
(1) `_exit_pending_blocks` — the "EXIT NOT CONFIRMED" alert, the most urgent one by its own docstring
("real capital sitting unmanaged") — declared the `account` local but never actually added the tag to
its header text, a half-applied edit; now fixed. (2) `mode_tag`'s unknown/`None`-account fallback
defaulted to the reassuring `DRY-RUN` label — the wrong failure direction for a real-money position
with a NULL account (e.g. EDC's own case before cleanup, or any of the 18 `mode='live'` nodes with
`account IS NULL` still sitting in the archived watchlist 57); changed to a deliberately alarming
`UNKNOWN`, plus an `or 'unmapped'` display guard added to the 4 sites that were missing it (3 of them
would otherwise have rendered a literal `None`). Full suite: 291 passed. `live_sim_harness.py`: 7/7.
`signals_invariants.py`: clean.

## ✅ [live-trading][security] Resolved 2026-07-26 — `auto_fill_detection_enabled` was ticker-only-keyed, a gap the 2026-07-25/26 wl_id refactor was supposed to close but missed
**Found while investigating why a real SPY position sat unconfirmed for 2 days** (a deliberate
standing `soxl_ira` test position, not a bug) and discussing why exit confirmation isn't
automated — surfaced that `check_auto_fills`'s real auto-fill-detection path
(`schwab_safety.auto_fill_detection_enabled`, off by default, toggled via a Slack button) was
keyed purely on the ticker string, with no account/node scoping at all. Enabling it "for SPY"
would have silently applied to *any* node with that ticker in *any* account — not just the one
you actually trust. This is exactly the same bug class the wl_id refactor (`docs/deep_backlog.md`'s
2026-07-25/26 entry) fixed for `ticker_automation_enabled`/`pause_ticker_automation` via an
additive `node_automation_enabled` AND-gate, but `auto_fill_detection_enabled` itself was never
touched by that refactor — it slipped through.

**Fix**: added `schwab_safety.node_auto_fill_detection_enabled(node_id)` /
`enable_node_auto_fill_detection` / `disable_node_auto_fill_detection` — mirrors the
`node_automation_enabled` additive-layer pattern, but with the **default direction inverted**:
defaults False (including `node_id=None`), since this flag *grants* extra automation trust
rather than *restricting* it — missing node identity must not silently grant it, the opposite
fail-direction from the pause mechanism's fail-open default. Real gate is now
`auto_fill_detection_enabled(ticker) AND node_auto_fill_detection_enabled(wl_id)` at both
`check_auto_fills` call sites (buy-side via `node['id']`, sell-side via `pos['wl_id']`). The
Slack enable/disable buttons (`signals_notify._ticker_block`) now carry `{ticker, wl_id}` JSON
instead of a bare ticker string; `signals_handlers.py`'s two handlers parse it and toggle the
node-scoped flag (enable also sets the ticker-level flag; disable only clears the node-level
flag, so a sibling node's separate enablement survives). 2 new regression tests prove the exact
leak is closed (sibling node stays unaffected; `None` node_id defaults closed).

**Opus review same session found 4 issues, fixed the 2 real ones**: (1) CONFIRMED, fixed —
every already-posted reference report has old buttons carrying a bare ticker string, not JSON;
reports stay clickable indefinitely, so the new handlers' unguarded `json.loads` would have
crashed silently on any stale tap. Added a try/except in both handlers posting "stale button,
resend the report" rather than falling back to ticker-only enable (which would reintroduce the
leak). (2) CONFIRMED, cleaned up — a `_pos.wl_id` fallback in `_ticker_block` was dead code
(every row is built from a real watchlist node, so `wl_id` is always resolvable there directly).
(3) Documented, not fixed — a position whose `watch_list` node was later deleted (`wl_id=NULL`,
real example: EDC id=15, the 2026-07-19 manual-unwind row) can never get this feature enabled,
since it renders no reference-table row at all; not live-reachable today (EDC isn't in
`SCHWAB_AUTOMATION_TICKERS`) and fails in the safe direction. (4) Flagged, deferred by user
("maybe later") — disabling the ticker-level flag now has no UI caller, so there's no more
one-tap "kill it for every node on this ticker"; disabling N enabled sibling nodes now takes N
taps. Full suite: 291 passed (was 289). `live_sim_harness.py`: 7/7. `signals_invariants.py`:
clean.

## ✅ [live-trading][coverage] Resolved 2026-07-26 — `live_sim_harness.py` gained `scenario_dry_run_sim_cycle`, closing a coverage gap in the dry_run fill-synthesis testing itself
**Problem, found while deciding how to verify the previous session's dry_run fill-synthesis
work over a weekend (markets closed, no real signal windows to observe)**: neither
`scripts/live_sim.py` (the manual REPL) nor `tests/test_dry_run_sim.py` (12 unit tests, added
2026-07-26) drove `update_dry_run_buys`/`_fill_dry_run_buy`/`check_dry_run_sim_sells` end-to-end
through the real `active_signals.py`/`signals_notify.py` orchestration — the exact kind of gap
`scripts/live_sim_harness.py` exists to close for every other live-trading code path (see the
2026-07-23 entry below), but this new path had no harness scenario at all.

**Fix**: added `scenario_dry_run_sim_cycle` — creates a `mode='live'`/`account='ira'`
(dry_run=True) `TrailingBothZScoreBreakout` node, seeds a `pending_buys` row, patches
`signals_compute._current_price` to trigger a bounce-fill BUY through the real
`update_dry_run_buys`, asserts the synthesized `open_positions` row is tagged
`is_dry_run_sim=1` and the pending-buy row cleared, then patches `active_signals._load_cache`
with a hand-built OHLC bar that breaches `fixed_sl` and calls the real
`check_dry_run_sim_sells`, asserting the position closes with `exit_reason='SL'` and the
closed `trade_log` row carries `is_dry_run_sim=1`. Harness now 7/7 scenarios (~2s). Full suite:
244 passed, unchanged.

Updated `docs/live_test_coverage.md`'s two dry_run-fill-synthesis rows to list the new harness
scenario alongside the existing unit tests, and `docs/automation_principles.md` #11's scenario
count (6 → 7).

## ✅ [live-trading][coverage] Resolved 2026-07-26 — dry_run fill synthesis: a dry_run account's trailing/market-buy order now closes the loop against real price data
**Problem**: `coverage_deviations` showed real, unexplained "no closed trade found" rows for the 6
canary tickers (specifically XLF/VOO/IWM/QQQ, ids 7-10, dated 2026-07-24, still unexplained as of
this session) — traced to a real design gap, not a bug in the checker: a `dry_run=True` account's
real broker order-placement call short-circuits *before* the actual API call
(`schwab_client._place_trailing_order`/`_place_equity_order` both return `(None, None)` and post a
`"[DRY RUN] would ..."` Slack message), so the `pending_buys` row `notify_buy_signal` created never
gets a real fill confirmation — it just sits forever, `open_positions`/`trade_log` never gets a row,
and the daily coverage check correctly (if unhelpfully) flags it as a deviation every single day.
This is the same open question flagged 2026-07-24 evening ("how does dry_run test completion at
all") — the user's own candidate design from that session (stage positions as if they'd filled, the
same way paper trading does) is what got built.

**Design decision**: write to the REAL `open_positions`/`trade_log` tables (not `paper_trading.py`'s
separate `paper_positions`/`paper_trade_log`), tagged with a new `is_dry_run_sim` column — user's
explicit call, so `coverage_check.py`'s existing `trade_log`-reading logic needs no changes at all.
Schema: `is_dry_run_sim` added to `open_positions`/`trade_log` (mirrored onto `paper_positions`/
`paper_trade_log` purely so `open_position()`'s shared INSERT statement works unchanged against
either table pair — a paper position is never also dry-run-sim). `running_low` added to
`pending_buys` (mirrors `paper_pending_buys.running_low`), used only for dry_run trailing-buy
bounce tracking.

**Implementation** (`signals_notify.py`): `update_dry_run_buys()` iterates all `pending_buys`,
skips any node whose account isn't `dry_run=True` (or whose mode isn't `'live'`), then either
tracks running_low/synthesizes a bounce-fill (trailing-buy nodes, same math as
`paper_trading.update_paper_buys`) or fills immediately (market-buy-eligible nodes, same reasoning
as `paper_trading.start_paper_market_buy`) — calling `open_position(..., is_dry_run_sim=True)` and
posting a `[DRY RUN] would have filled ...` Slack message. `check_dry_run_sim_sells()` mirrors
`paper_trading.check_paper_sells()`: once `check_sell_condition` fires an exit reason on an
`is_dry_run_sim` position, closes it immediately rather than routing through `notify_sell_signal`'s
Slack-button-wait flow (correct only for a real order that might actually get a human tap). Both
wired into `active_signals.py`'s `run_loop` at the same cadence as their paper-trading equivalents;
the normal per-position exit-check loop skips `is_dry_run_sim` positions (routed to the new function
instead), as does `check_live_state_reconciliation` (no real broker state exists to compare
against for a synthetic position, by design).

**Opus review (session-wrap convention, since this touches `active_signals.py`/`signals_notify.py`/
`signals_db.py`) found 6 CONFIRMED + 2 PLAUSIBLE issues, all fixed same session**:
1. **Most severe** — `active_signals._scan_pinned_exit_arm` (a separate, earlier-firing exit-check
   loop) was missed by the original `is_dry_run_sim` skip guard. It shares the `last_seen_bar` dict
   with `check_dry_run_sim_sells`, so it would have consumed the bar-close marker out from under the
   new function (silently downgrading it to the mid-bar/continuous branch, bypassing the real
   intrabar Low/High gap-through-trigger logic) — and it would have fired real
   `notify_trailing_activated`/`notify_sell_signal` Slack flows (arm + interactive SELL button) for a
   position with no real order behind it at all. Fixed: added the same skip guard there too.
2. This also meant `check_trailing_reminders`/`check_exit_reminders` (which look mostly like no-ops
   for a sim position, since `trail_state`'s `trailing`/`exit_pending` fields are normally never set
   for one) were only safe *because* of finding #1's absence — once `notify_trailing_activated`/
   `notify_sell_signal` fired via that gap, they'd start nagging every 15 minutes forever. Resolved
   by fixing #1, no separate change needed.
3. `_fill_dry_run_buy` never checked `open_position()`'s own return value (`False` on a duplicate/
   already-open node) — its docstring explicitly warns every caller must check this "since a silent
   skip must not be reported as a fill." A duplicate fill attempt (e.g. a stale leftover
   `pending_buys` row) would have re-posted a false `[DRY RUN] would have filled` Slack message and a
   false `coverage_events` row every poll, forever. Fixed: checks `opened`, bails cleanly on `False`.
4. A `pending_buys` row with a falsy `wl_id` (legacy/pre-migration, or any future edge case) would
   never actually clear (`clear_pending_buy_by_wl_id`/`update_pending_buy_running_low` both key on
   `wl_id`, and SQL `NULL = ?` never matches) — combined with #3's missing check, this would re-fire
   a synthetic fill attempt and a false alert every poll indefinitely. Fixed: fails closed, skips any
   `wl_id`-less row entirely rather than trusting the frozen `node_json` snapshot fallback.
5. Simulated dry-run fills were contaminating two real safety/sizing code paths with no filter:
   `signals_db.closed_today()` (the same-day-rebuy cash-settlement warning/`SafetyViolation`) and
   `signals_helpers._last_sale_recovery()` (real order sizing off the last closed trade's proceeds).
   A simulated exit could have suppressed a real order or sized a real order off fake proceeds —
   currently latent (the one `dry_run=False` account, `soxl_ira`, is `account_type="margin"`, which
   `closed_today` doesn't even branch on; no shared account currently mixes sim and real trade
   history), but a real hazard the moment either changes. Fixed: both queries now filter
   `is_dry_run_sim=0`.
6. Synthetic positions were completely indistinguishable from real ones in every human-facing
   surface (`build_reference_table`/`_ticker_block`'s Held branch, `cmd_positions`,
   `scripts/open_positions_status.py`) — the exact class of ambiguity the existing 🧪CANARY/
   `(research)` tagging convention (2026-07-23) was built to prevent. Fixed: added a `🧪DRY-RUN-SIM`
   tag to the Held branch, suppressed the "Manually Close" button for a sim position (no real
   position to close), added an `is_dry_run_sim`/Sim column to both CLI tools.
7. (Plausible) No `node['mode'] == 'live'` gate on `update_dry_run_buys` — not currently reachable
   (a `research`-mode node's BUY never creates a `pending_buys` row today), but added defensively so
   a future BUY-routing change can't silently open a real `open_positions` row alongside a node's own
   `paper_positions` row for the same `wl_id`.
8. (Plausible, cosmetic) `check_gap_resize` was still reading `pending['signal_price']` as its
   running-low reference instead of the new `running_low` column — synced for consistency (no
   material behavior change pre-fix, since overnight polling never moves `running_low` anyway).

**Verification**: 12 new tests in `tests/test_dry_run_sim.py`, added incrementally as each Opus
finding was fixed — specifically target double-fill prevention, the market-buy-eligible immediate-
fill branch, the `wl_id`-less fail-closed skip, `closed_today`/`_last_sale_recovery` exclusion, and
the `_scan_pinned_exit_arm` skip guard (not just the original happy-path fill/close tests). Full
suite: 244 passed (was 238 pre-session). `scripts/live_sim_harness.py`: 6/6. `signals_invariants.py`:
unchanged, 1 known accepted violation (UDOW). `verify_trailing_buy_resolution.py`/
`verify_trailing_sell_resolution.py --tickers AGQ,SOXL`: both clean, no new mismatches (this diff
never touched `strategies.py`/`backtester.py`, run per the pre-commit checklist's
`active_signals.py`-changed trigger anyway).

**Not yet observed live** — this is new daemon logic, not yet run against a real restart. Next
natural checkpoint: the canaries' `coverage_deviations` rows should stop reappearing once the daemon
restarts with this change; watch `coverage_matrix.py`/`coverage_check.py` output for a real
`entry_fill`/`exit_fill` `mode=dry_run` row to confirm the synthesis actually fires live, not just in
tests.

## ✅ [live-trading][coverage] Resolved 2026-07-27 — `coverage_deviations` rows are now permanent record ("ticket model"), never deleted
Per user's explicit framing: an exception/deviation is an artifact of something that happened, like a
Jira ticket — once created, it should never vanish, only ever be explained. `clear_deviation_if_resolved`
(`signals_db.py`) changed from `DELETE` to auto-`UPDATE` (`reason='Auto-resolved: ...'`,
`reason_by='system'`) for a same-day unexplained deviation that a later re-check finds met — the row
stays, tagged as system-resolved rather than human-explained. Also cleared the 4 stale unexplained
XLF/VOO/IWM/QQQ rows (ids 7-10, open since 2026-07-24, root-caused by the dry_run fill synthesis gap
already fixed 2026-07-26) by explaining them directly.
**Regression found+fixed same session** (session-wrap Opus review): the auto-resolve change alone would
have let a genuine NEW same-day deviation silently inherit the stale system reason and vanish from
`get_deviations(unexplained_only=True)` — `record_deviation` now clears a `reason_by='system'` reason
(never a human one) whenever it refreshes an existing row, so a system auto-resolution yields to real
new evidence instead of masking it. New regression test
(`test_record_deviation_clears_system_reason_on_new_deviation`).
## ✅ [live-trading][security] Resolved 2026-07-25 — second Opus review round on the daily_order_cap change, 2 findings
Full-diff review of the final `schwab_safety.py` state (increment + check both BUY-only, `soxl_ira`
cap 3→100), after the first round's SELL-blocked-by-BUY-exhausted-cap bug was already fixed.
Mutation-tested the new regression test (`test_sell_not_blocked_by_buy_exhausted_daily_cap`) by
reverting the guard — confirmed it actually fails without the fix, not passing coincidentally.
Two findings:
1. **CONFIRMED, fixed same session**: `is_protective`'s docstrings (`check_order`/
   `approve_and_record`) still advertised it as covering "a stop-loss placement or post-fill
   top-up" — stale now that SELL (including stop-loss placement) is unconditionally exempt from
   `daily_order_cap` regardless of `is_protective`. The flag is dead on that path now, only
   meaningful for the BUY-side top-up. Docstrings corrected to say so explicitly.
2. **PLAUSIBLE, not fixed, user's known tradeoff**: bumping `soxl_ira`'s cap 3→100 with
   `notional_cap` unchanged at $800 removes the de-facto cumulative same-day BUY-notional bound
   the low cap was incidentally providing (~$2.4k/day → ~$80k/day). Nothing else in the codebase
   reads the counter for a second purpose (checked — `STATE_PATH` is read only in `check_order`,
   written only in `approve_and_record`), so this isn't a hidden second-order bug, just the
   explicit consequence of the bump the user already asked for. Ties into the existing
   still-open "max cumulative BUY notional per ticker per day" backlog item.
Full suite: 232 passed. `scripts/live_sim_harness.py`: 6/6 scenarios passed.
## ✅ [live-trading][coverage] Resolved 2026-07-25 — pytest was polluting the real coverage_events table with fake "daemon_section_exception" rows
Found while cross-checking `docs/live_test_coverage.md` against real `coverage_matrix.py` output
for the Monday live-test replan: `daemon_section_exception` showed 360 real-looking rows
(detail=`boom`), which turned out to be pytest fault-injection noise, not real daemon failures.
Root cause: 5 of 6 test functions in `tests/test_run_loop_fault_tolerance.py` called
`active_signals._guarded` with a real raising exception but never requested the file's own
`isolated_db` fixture — `signals_db.log_coverage_event` is fire-and-forget (never raises into the
caller), so each test passed while silently writing a real row into
`cache/live/trading_live.db`'s `coverage_events` table. Only one test out of six had opted into
isolation. Fixed: made `isolated_db` `autouse=True` for the whole file. Backed up
`trading_live.db` (`cache/live/trading_live.db.bak_20260725_114349_pre_coverage_events_test_pollution_cleanup`)
then deleted the 360 polluted rows (`scenario_key='daemon_section_exception' AND detail='boom'`).
Checked `tests/test_coverage_check.py` (the only other file touching `log_coverage_event`) — every
one of its 32 tests already explicitly requests `isolated_db`, no gap there. Full suite: 232 passed.
## ✅ [live-trading] Resolved 2026-07-23 — two logging/observability gaps found while chasing the above, both fixed
Found because the above incident was genuinely undiagnosable in real time — worth fixing on its
own merits, not just as a side effect:
1. **`logs/active_signals.log` never flushed.** `human_fh = open(HUMAN_LOG_PATH, "a")`
   (`active_signals.py`) had no explicit `.flush()` anywhere, unlike the verbose log — since it's
   not a tty, Python fully block-buffers it, so console output (including any `[slack error]`
   line) could sit invisible on disk for the buffer's lifetime. Confirmed live: the file's mtime
   was frozen for 10+ minutes while the daemon was demonstrably still looping (heartbeat proved
   it). Fixed: `open(HUMAN_LOG_PATH, "a", buffering=1)` (line-buffered). Takes effect on next
   restart — done, daemon already restarted with the fix live.
2. **`slack_message_log` recorded intent, not delivery.** `db.log_slack_message(mode, text)` was
   called *before* the real `chat_postMessage`/webhook attempt in `signals_blocks._post_message`
   — a row's existence was wrongly read as proof of a successful send mid-incident (a real mistake
   made in this session, not just a hypothetical risk). Fixed: new nullable `error` column
   (migration applied to the live DB, backed up first as
   `trading_live.db.bak_20260722_233127_pre_slack_log_migration`), and the log call moved to
   *after* the attempt, populated with the caught exception/HTTP-status string (`None` = no error
   caught). `_post_message` also now returns `(channel, ts)` reliably from a single code path.
   `send_reference_report` and `active_signals.py`'s two call sites (startup + scheduled) now
   print/capture that return value instead of discarding it, so a future incident has a real
   `ts` to check via `chat.getPermalink` instead of nothing.
Both root-caused entirely by inspection/live testing, no unit tests added yet for either (the
flush behavior and the error-column plumbing are both straightforward enough that a live restart
was the real verification — see `docs/live_test_coverage.md` if this should get a synthetic test
later). Full suite: 181 passed throughout.
## ✅ [live-trading][security] Resolved 2026-07-21 — account cash/buying-power check built: `get_account_balance` + `check_order` wiring, quantity-aware, fail-closed
Full design context (why this was needed) preserved below the line. Built this
session: `schwab_client.get_account_balance(account)` (`Client.get_account`,
parses `securitiesAccount.currentBalances.cashAvailableForTrading` — field
names follow Schwab's documented schema but are **unverified against a real
account response**, flagged for live confirmation, see `docs/live_test_coverage.md`).
Wired into `schwab_safety.check_order`'s single BUY chokepoint — every BUY-
placing path (`place_equity_buy`, `place_trailing_buy`, the top-up, the
gap-correction replacement) goes through it; confirmed by an independent Opus
review (2026-07-21) that no BUY path bypasses it. Requires
`cash_available >= notional + CASH_SAFETY_BUFFER` (`CASH_SAFETY_BUFFER = 200`,
a deliberate small flat cushion for per-order overage — fees/a quote-to-fill
price tick — instead of precise real-time accounting. Deliberately *not* a
restatement of the user's own much larger cash reserve habit (~$1,000 kept
in each account); `notional` is sized independently off `starting_notional`/
compounding logic, not off real cash, which is exactly why this check exists
at all — see `docs/automation_principles.md` #7a). Fails closed: any exception from
the balance fetch itself raises `SafetyViolation`, blocking the order rather
than letting it through unchecked. Four existing test files' fixtures
monkeypatch `get_account_balance` (large default) so existing BUY-path tests
don't hit a real API; 5 new dedicated tests in `test_schwab_safety.py`
(insufficient cash, buffer-on-top-of-notional, sufficient-cash passes,
balance-fetch-failure fails closed, SELL exempt). Full suite: 142 passed
(was 137).
**`CASH_SAFETY_BUFFER` was initially built as `1_000` — a bug, not the
intended design**: caught by the user immediately after review — the $1,000
was meant to be the user's own operational cash-reserve habit, not something
`check_order` enforces as a blocking requirement. Fixed same session:
`CASH_SAFETY_BUFFER` is `200` (a small per-order overage cushion, the only
thing actually enforced as a block), and a new, separate, **non-blocking**
`CASH_RESERVE_WATERMARK = 1_000` check posts an informational Slack warning
(💰 emoji) whenever `cash_available < CASH_RESERVE_WATERMARK` on a BUY check
— lets the user know to add cash before the reserve actually runs out,
without blocking the order. Deliberately checks the raw balance, not
"balance after this trade" (simpler, per user's own call — `automation_principles.md`
#7a). 2 new tests cover the warning firing and not firing. Full suite after
both fixes: 153 passed.
**Second real gap found by the Opus review and fixed same session**: Schwab
does not reserve buying power for a resting order (already known from the
existing same-ticker duplicate-order guard's own docstring), so **two
different live tickers sharing one account could each pass the cash check
against the same undecremented balance** — the flat $200 buffer alone
doesn't cover a second simultaneous BUY. Not reachable today (every live
ticker has its own account, per `_live_ticker_accounts`) but not structurally
enforced. Fixed with a real-order-book check (not a local heuristic, per
`automation_principles.md` #1): `schwab_safety._has_open_buy_order_in_account`
blocks a second concurrent BUY into an account that already has *any* other
ticker's resting BUY order, reusing the same `_open_orders()` fetch the
existing same-ticker guard (`_has_open_order`, refactored to share one API
call instead of two) already makes — no extra network cost. 3 new tests
cover this. Also tightened test hygiene as a side effect: the existing
same-ticker duplicate-order-book check was previously **hitting the real
Schwab API unmocked** in every BUY-path test (a pre-existing gap, not
introduced this session) — now mocked (`_open_orders` returns `[]` by
default) in all four fixtures, per `automation_principles.md` #8. Full
suite after this fix: 145 passed.
**Not yet fixed, two smaller residual findings from the same review, logged
as their own items below**: (1) the balance-fetch network call now happens
while `approve_and_record`'s cross-account file lock is held — a slow/hung
fetch would stall order processing for every account, not just the one being
checked; (2) `cashAvailableForTrading`'s exact field semantics (e.g. whether
it's margin-inclusive) are still unverified against a real account response.
**User's proposed empirical validation, still not built**: fund a real
(margin-enabled) account with a small amount (~$100), flip that one account's
`dry_run` to `False`, and place a single ~$100.50 order via a minimal
standalone script (bypassing `active_signals.py`/`signals_notify.py`/
`schwab_safety` entirely, calling `schwab_client.place_equity_buy` directly)
to observe Schwab's real behavior — this would be the first real (non-dry_run)
order this system has ever placed, so treat deliberately. Not scripted yet.
Related but distinct real-world note (not actionable code, confirm with
Schwab directly rather than assume): a short position from an oversell is
likely self-limiting in no-margin (IRA-type) accounts (order rejected, not
executed as a short), but could actually execute in the margin-enabled
account; unsure of Schwab's specific margin-call/PDT-flag policy for a brief
accidental short — don't assume benign without confirming.
Original framing (2026-07-21, before this fix): `schwab_client.py` had no
function at all to read an account's real cash balance, and
`schwab_safety.check_order` only enforced a fixed per-account `notional_cap`
(a static dollar ceiling), never actual available cash — so any BUY order
(including Part 3's top-up, which places a real order) exceeding real
available cash would either get rejected (no-margin accounts) or silently
draw on margin (margin-enabled accounts), with zero check on our side.
## ✅ [live-trading][security] Resolved 2026-07-21 — cash-balance network call held inside `approve_and_record`'s cross-account file lock
Found during the Opus review of the cash-balance check (resolved item
above). `check_order`'s cash-availability check (and the order-book fetch
for the duplicate-BUY guard) run while `approve_and_record` holds
`_open_locked()` — a global lock shared across every account. A slow or
hung Schwab call would stall order processing for every account's order,
not just the current one, for as long as schwab-py's 30s default httpx
timeout takes to fire. Fixed per the proportionate option flagged at the
time (`automation_principles.md` #7a): `schwab_client._get_client()` now
calls `client.set_timeout(_CLIENT_TIMEOUT_SECS)` (10.0s) once at client
creation, bounding every Schwab HTTP call — not just the two under the
lock — to 10s instead of 30s. Simpler than restructuring lock ordering,
same failure mode either way (fails closed once the call errors out).
New `tests/test_schwab_client.py::test_get_client_applies_short_timeout`.
Not fully closed: still bounded by 10s, not eliminated — restructuring the
lock so only the count/cap bookkeeping (not the network calls) runs under
it remains a further option if 10s is ever observed to matter in practice.
## ✅ [live-trading][security] Resolved 2026-07-21 — pre-existing test hygiene gap: some `schwab_safety` tests were silently hitting the real Schwab API
Found during the Opus review of the cash-balance check above. The same-ticker
duplicate-order-book check (`_has_open_order`, now refactored to share a
fetch with `_has_open_buy_order_in_account`) was being exercised in every
BUY-path test via a real, unmocked `get_orders_for_account` call against
whatever the actual `ira` account's real order book happened to contain at
test-run time. Ran the follow-up sweep this item asked for: every test file
that imports `schwab_safety`/`schwab_client`
(`test_schwab_safety.py`/`test_part3_gap_resize.py`/`test_part4_entry_trigger.py`/
`test_schwab_automation.py`/the new `test_schwab_client.py`) already mocks
`schwab_safety._open_orders` and `schwab_client.get_filled_order` in its
fixture — no stragglers found, nothing left to fix.
## ✅ [live-trading][security] Resolved 2026-07-21 — `active_signals.run_loop` fault tolerance built: per-section isolation + outer last-resort net
Full original context preserved below the line. Fixed this session, using
the granularity decision the item left open: **both** approaches, not one or
the other. A new `_guarded(section, fn, *args, **kwargs)` helper
(`active_signals.py`, just above `run_loop`) wraps every previously-unguarded
loop-body section (reference report, gap-resize check, window alerts,
pinned exit-arm/entry scans, the per-position exit-check loop — now also
per-position isolated via a `_check_position_exit` helper so one bad
position doesn't stop the rest — paper-sell checks, reminders, auto-fill
checks, fill-queue drain, paper-buy updates, the per-node limit-fill loop —
similarly per-node isolated — and both buy-signal scans): catches and logs
any exception, posts a rate-limited Slack alert (15min cooldown per section,
so a persistent failure doesn't spam every poll) rather than silently
swallowing it (`automation_principles.md` #4), and lets the loop continue to
its next section/iteration. On top of that, the whole loop-body block is
still wrapped in one outer try/except as a last-resort net, catching
anything that slips through an individual guard (e.g. a bug in the glue code
between sections) so the daemon can never die from an unhandled exception in
`run_loop` — logs and posts a 🔴 Slack alert, then proceeds to the next poll.
**Not yet done**: no fault-injection tests exist yet exercising any of these
paths (each guard's behavior is straightforward enough to reason about, but
per `automation_principles.md` #8, everything should be testable — add tests
that inject a raising stub into a couple of the wrapped sections and confirm
the loop survives and alerts, rather than trusting the wrapping by
inspection alone). Not yet observed live either — see
`docs/live_test_coverage.md`.
Original framing (2026-07-21, before this fix): found while discussing how to
chaos-test resilience to real-world failures (bar/data timeouts, real-time
price request timeouts, position/order-book request timeouts). Two spots
already isolated failures correctly (`_refresh`'s per-ticker timeout+catch,
`_scan_pinned_entry`'s per-ticker try/except) but everything else in
`run_loop`'s `while True:` body had no exception handling at all, and there
was no top-level try/except around the loop body either — a single
unexpected exception anywhere would propagate all the way up and kill the
entire daemon process, stopping monitoring/protection for every open
position on every ticker, not just skipping the one thing that failed.
## ✅ [live-trading][security] Resolved 2026-07-22 — `schwab_safety`'s duplicate-order guard now confirms against Schwab's real order book, not just a local pre-flight record
Full original context preserved below the line. `approve_and_record` still
writes the order into its local `recent_orders` dedup list before the real
`place_order` call happens, but `check_order`'s duplicate check now only
blocks a matching retry for a **real (non-dry_run) account** if a new
`_broker_confirms_order` cross-check finds a genuinely-accepted (WORKING/
QUEUED/FILLED, not CANCELED/EXPIRED/REJECTED/REPLACED) order for that exact
ticker/side/quantity in Schwab's real order book (`_all_orders`, a new
unfiltered sibling of `_open_orders`) — so a failed/rejected/errored prior
attempt no longer wrongly blocks a legitimate retry. Dry-run accounts keep
the old pure-local-heuristic behavior unchanged (nothing real to verify
against). 2 new tests in `test_schwab_safety.py`. Full suite after this fix
and the critical trail_state fix below: 172 passed (was 164).
Original framing (2026-07-21): found while fixing Part 3's top-up to place a
real broker order. `approve_and_record` writes the order into its local
`recent_orders` dedup list *before* the real `place_order` call happens
(`schwab_client._place_equity_order`) — if that broker call then fails/times
out/is rejected, the guard still believes it succeeded, so a legitimate
retry within `DUPLICATE_ORDER_WINDOW_SECS` (60s) was wrongly blocked as a
duplicate. Not urgent at the time — no real order had been placed yet
(`dry_run=True` everywhere, still true).
## ✅ [live-trading][security] Resolved 2026-07-22 — CRITICAL: trailing-arm state clobber caused re-arming and duplicate live trailing-sell orders (oversell risk); found by a full-stack Opus review
Found via a scoped independent Opus review of the whole automation stack
(`schwab_client.py`, `schwab_safety.py`, `active_signals.py`,
`signals_notify.py`, `signals_compute.py`, `signals_db.py`,
`paper_trading.py`), requested to look at seams between features each
already individually reviewed in prior sessions. `check_sell_condition`
(`signals_compute.py`) persists the newly-armed state (`trailing=True,
peak=P`) to the DB correctly, but `notify_trailing_activated`
(`signals_notify.py`) then merged its reminder fields onto the **stale**
in-memory `pos['trail_state']` (the pre-arm copy the caller passed in) and
overwrote the DB with it — silently losing `trailing`/`peak` right after
arming. On the next bar close the position looked unarmed again, `check_exit`
re-armed it, and (since a TrailingBoth position entered via trailing-buy has
no `sl_order_id` to cancel) `_attempt_automated_sell` placed a **second**
live trailing-sell order for the same shares — an oversell risk if both
filled. **Fixed**: new `signals_db.get_position_by_id(position_id)` (fresh
single-row lookup by primary key); `notify_trailing_activated` now re-reads
the position before merging, so it starts from the real just-armed state,
not the stale one. 1 new regression test.

**Two more findings from the same review, fixed alongside it, per explicit
user direction on scope**:
- SELL-side order placement had **no resting-order duplicate guard at all**
  (only BUY did) — this is what let the state-clobber bug above actually
  stack two live orders. Fixed: new `schwab_safety._has_open_sell_order`,
  wired into `check_order` as a same-ticker (not account-wide, unlike the
  BUY guard — an unrelated resting BUY must not block closing a position)
  SELL-side check. 2 new tests.
- `_attempt_automated_sell` cancels a resting stop-loss *before* confirming
  the trailing-sell placed; if placement then fails, the position is left
  with zero protection and no automated way to recover it. **User's explicit
  call**: don't restructure the cancel/place ordering (no real alternative
  that avoids some window of risk) — instead just make sure the user is
  told what to do. Now posts a 🚨 UNPROTECTED Slack alert with the SL price
  to manually re-place. 1 new test.

Also fixed as its own item (poll-loop/Slack-handler race, user: "yeah i
think loop poll double open/close needs a fix"): `open_position`/
`close_position` (`signals_db.py`) each did a SELECT-then-act with no lock
spanning both statements — the poll loop thread and the Socket Mode Slack
handler thread (same process, `active_signals.py` starts both) could each
pass their own existence check before either committed, risking a duplicate
open or a racing double-close silently overwriting `trade_log`'s exit
fields with the losing thread's values. Fixed with a plain
`threading.Lock()` (`signals_db._position_lock`) — single-process/
multi-thread daemon, not the cross-process concern `schwab_safety`'s file
lock guards. `close_position` now also returns `True`/`False` (was
implicit `None`) and is a safe no-op if the row is already gone. 3 new
tests.

**Not fixed, explicit user call**: kill switch / per-ticker pause also
blocks *protective* sell orders, not just new entries (found by the same
review) — this is existing, understood behavior (turning the algo off means
accepting the exposure), not a bug to patch.

Full suite: 172 passed (was 164). `verify_trailing_buy_resolution.py`/
`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` clean post-fixes.
Nothing here has been observed live — every account still `dry_run=True`.
## ✅ [live-trading][security] Resolved 2026-07-22 — live-state reconciliation check built: detection + text-only proposed remediation, never auto-executes
Related idea from the item above, split out once actually built. Originally
scoped as: does `open_positions.shares` match the broker's real position
size, and does a resting protective order (SL or trailing-sell, matching the
position's current phase) actually exist at the broker for that exact
quantity? **User explicitly flagged the danger**: this must stay detection/
alert-only (Slack notify on mismatch) — an auto-correcting version would be
a new automated-trading decision layer on top of the ones this project has
already found real bugs in, and a false-positive mismatch (legitimate
slippage, a deliberate manual override, a timing lag) would trigger a real,
wrong, automated trade to "fix" something that wasn't actually broken.
**Refined at the time**: a hybrid middle ground — on a detected mismatch,
compute and post a *proposed* remediation (e.g. "top up N shares" / "place
missing SL at $X") to Slack, rather than either silent auto-correction or a
bare alert with no suggested fix.

**Built 2026-07-22, scoped down to text-only** (confirmed with the user
before building: a clickable approve-button version would be a much bigger
build — new Slack handler, new execution path through `schwab_safety`,
needing its own dedicated safety review — deferred as a possible v2, not
built now). New `schwab_client.get_real_position(account, ticker)` (real
share quantity via `Client.get_account(fields=[Account.Fields.POSITIONS])`,
field names unverified against a real response, same caveat pattern as
`get_account_balance`). New `signals_notify.check_live_state_reconciliation`
(called every `run_loop` poll cycle via `_guarded`, automation-scope tickers
only — matches where the real order-placement risk actually is today):
compares broker-real shares vs. `open_positions.shares`, and whether the
expected resting order (SL pre-arm, trailing-sell post-arm) is actually
resting, via the existing `schwab_safety._open_orders` order-book fetch.
Posts a text-only Slack alert with the suggested fix on any mismatch —
never places an order itself. Broker treated as ground truth (automation_
principles.md #1); alerts rate-limited 15min per (position, mismatch-kind)
to avoid repeat-alert spam. 8 new tests (`tests/test_live_state_reconciliation.py`).
Full suite: 164 passed. `verify_trailing_buy_resolution.py`/
`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` clean (required
since `active_signals.py`/`signals_notify.py` changed).
Not yet done: no real mismatch has ever been observed live (every account
still `dry_run=True`) — tracked in `docs/live_test_coverage.md`.
## ✅ [backtest] Resolved 2026-07-18 — 5 tickers with a negative walk-forward fold (DPST, NUGT, RETL, UDOW, UVIX) sent to research, no per-ticker investigation
Follow-up from the walk-forward check (`docs/research_log.md` 2026-07-18 entry,
`logs/walk_forward_check.csv`). Each had exactly one mild negative-alpha fold out of 5
(DPST -11.9%, NUGT -7.2%, UDOW -3.6%, UVIX -4.0%, RETL -8.4%). **User decision: skip the
per-ticker regime investigation and just keep all 5 in research mode** rather than promoting
any of them further. NUGT/AGQ/GDXU/TQQQ/YANG were already `research`; RETL/UDOW/UVIX were
never in the live `watch_list` to begin with (screened candidates only). **DPST flipped
`live`→`research` this session** (`signals_db.set_node_mode(53, 'research')`, watchlist_id=9)
— it had been the leading candidate for the first taxable-brokerage-account ticker, but this
walk-forward flag plus its already-known thin trade count (chaos-monkey item, 2026-07-17)
was enough to deprioritize it without further digging. No live capital was at risk (DPST had
no open position at the time of the mode change).
## ✅ [backtest] Resolved 2026-07-18 — `signals_db.add_node`'s `fixed_sl` computation ignored the real per-node value for uses_fixed_sl strategies
`add_node` (`signals_db.py`) used to always compute `fixed_sl = _config_fixed_stop_loss()`
(reads `config.json`'s global `execution.fixed_stop_loss`) whenever
`strategies.uses_fixed_sl(strategy)` was true, regardless of what real per-node SL value the
caller actually wanted — no parameter existed to override it. Found 2026-07-18 promoting 19
tickers' v4 (SL=1%) nodes: every row came out with `fixed_sl=15.0` (the stale global default)
instead of the real `1.0`, silently wrong, no error; worked around that session by inserting
via direct SQL instead of `add_node`. **Fixed**: `add_node` gained a
`fixed_sl_override=None` parameter — when set, used instead of `_config_fixed_stop_loss()`.
`None` (the default) preserves old behavior for legacy v3.x callers.

## ✅ Backlog-hygiene pass, 2026-07-22 — five stale headings closed out, retroactively marked resolved
Found while doing a dedicated backlog-hygiene pass (flagged as a to-do at the end of the
prior session): these five `backlog_cache.md` headings described work that had actually
already been completed (sometimes by the item's own later progress notes, sometimes by a
separate later session), but the heading itself was never updated to say so — each one below
gets its own entry with the real resolution date and pointer.

## ✅ [live-trading][backtest] Resolved 2026-07-18 — v4 nodes (SL=1%, all 19 candidates) promoted into the real `watch_list` table, replacing v3.x SL=15% params
Heading previously read "High priority... not started, no `watch_list` rows changed yet" —
stale; its own later progress notes in the same entry already documented the real work: all
19 walk-forward-screened tickers' v4 winning nodes (`fixed_sl=1%`, pulled from
`backtest_cache` ranked by robust alpha) were inserted into watchlist 57 via direct SQL
(worked around the `add_node` `fixed_sl` bug, separately fixed the same session), each
annotated walk-forward-clean or flagged. All landed in `mode='research'`, not `mode='live'` —
"promoted into `watch_list`" meant the table, not live execution status; live/paused status is
tracked separately (see the watchlist-65 entry below).

## ✅ [live-trading] Superseded 2026-07-20 — watchlist 65 ("Live v5") is now the active watchlist, not watchlist 57 ("Live v4", GDXD+EDC only)
Heading previously described watchlist 57 (GDXD+EDC only) as "Active" — that was accurate as
of 2026-07-18 but is stale now. Watchlist 65 ("Live v5", 10 tickers: AGQ, DPST, GDXU, HIBL,
KORU, NUGT, SOXL, UDOW, USD, YANG) became the active watchlist 2026-07-20, selected from the
full 18-ticker v5 resweep by cliff-safe robust alpha (see the 2026-07-20 watchlist-65 entry
above). All 10 nodes are still `mode='research'` — nothing trades live yet. Watchlist 57's 18
v4 nodes are preserved, untouched, just no longer polled (same non-destructive
versioning pattern as every prior watchlist supersession). Always check `watchlists.is_active`
rather than trusting a hardcoded id anywhere in docs.

## ✅ [backtest] Resolved 2026-07-15/19/20 — trailing-buy fill logic kernel-correctness fix, executed in full across three sessions
Heading previously ended "Action needed: implement per the plan file. Not started" despite its
own later progress notes showing the opposite. Real timeline: kernel implemented 2026-07-15
(`_simulate_trail_both` computes possible/pessimistic/certain per node, island/cliff-safety
rank on `MIN` of the three, verified via `verify_v4_fill_bounds.py` across all 11 live nodes at
the time); `phase` column backfilled for the pre-tagging SOXL/KORU rows via
`scripts/backfill_v4_phase.py` (confirmed run clean, 0 rows left untagged — `docs/
conversation_summary.md` session ~12); gap-through-trigger entry-side fix 2026-07-19 and
exit-side fix 2026-07-20 (both in `CLAUDE.md`'s Key Files); full 18-ticker v4/v5 resweep under
the corrected kernel completed 2026-07-20. Nothing outstanding from the original plan remains.

## ✅ [backtest] Resolved 2026-07-17 — `entry_timing=open_check` live-actionable analog built
Heading previously said "Not started, not scoped beyond the shape above" — stale; the exact
proposed fix (a second poll window per signal time, ~9:31-9:40/14:31-14:40 alongside the
existing close-check windows, reusing `compute_buy_signal`, with `buy_alerted` dedup
preventing a double-fire) was built the same session as the GDXD promotion (`active_signals.
_OPEN_CHECK_WINDOWS`, `watch_list.entry_timing` column). Confirmed still live and in use today:
all 18 watchlist-65/57 v4 nodes use `entry_timing='open_check'` (`CLAUDE.md`'s Live Trading
section).

## ✅ [live-trading][security] Status corrected 2026-07-22 — one-account-per-ticker: infra built, rollout ongoing (not "not started")
Heading previously ended "Not started, no code changes yet" — stale; substantial
infrastructure now exists. `schwab_safety.ACCOUNTS` holds 4 account slots (brokerage/sep/
roth/ira) each with its own `AccountLimits` (`notional_cap`, `daily_order_cap`, `account_type`);
`watch_list.account` is a real per-node column; `_live_ticker_accounts()` derives the live
ticker→account mapping fresh from `mode='live'` rows (currently empty — no ticker is
`mode='live'` yet, so nothing is actually exercising per-ticker isolation live). The originally
floated "account nickname = ticker symbol" placeholder naming was not what shipped — actual
accounts are named by role (brokerage/sep/roth/ira), with `watch_list.account` doing the
ticker→account assignment instead. A 5th account (limited-margin IRA, funded 2026-07-22) is
queued to join this set once API token scope + compliance permission clear (see
`project_new_ira_account_status` memory) — rollout is incremental, not a single completed
migration, so this item stays open in spirit (tracking new-account onboarding) even though the
core mechanism is done.

## ✅ [live-trading][security] Resolved 2026-07-22 — `same_day_block` account-type-awareness
`schwab_safety.AccountLimits` gained `account_type` (`'cash'` or `'margin'` — regular and IRA
limited margin treated identically, since both lack the T+1 cash-settlement restriction the
same-day-rebuy check exists for). `ACCOUNTS`: brokerage is `'margin'`; sep/roth/ira are `'cash'`,
confirmed by the user. `check_order`'s same-day-rebuy check now only fires for `'cash'` accounts.
A blanket "real orders only in a confirmed margin account" gate was considered and explicitly
rejected mid-build (caused 2 real test failures before being reverted) — the user's account model
is one account per ticker, capital-capped and expanded by funding a new account rather than
liquidating an existing one (see `project_account_segregation_model` memory), so a hard
margin-only gate would've locked automation out of every existing account. 1 new test
(`test_same_day_rebuy_not_blocked_in_margin_account`). The new (5th) limited-margin IRA funded
2026-07-22 isn't in `ACCOUNTS` yet — blocked on Schwab API token scope + compliance trading
permission. Full suite: 177 passed.

## ✅ [backtest] Resolved 2026-07-22 — `auto_adjust`/split-guard reconciliation closed; data traceability (not full immutability) chosen as the design direction
Reconciled the open 2026-07-16 question (why did `data_manager.py`'s split-guard rescue still
fire for KORU if `yf.download()` already defaults to `auto_adjust=True`?): `auto_adjust=True`
only adjusts the window being fetched *right now* for corporate actions known as of today, not
rows already sitting in the local cache from a prior fetch — a new split therefore still produces
a scale cliff between stale-scale cached rows and new-scale freshly-fetched rows, which is exactly
what the split-guard detects and rescales the whole file to fix. Guard is real work, not dead
code. Since the rescale is one multiplicative factor across the whole series, downstream
%-based signals (z-score, SL/TP/arm, returns) are scale-invariant to it, so past `backtest_cache`
numbers should stay reproducible by re-running the same code. Corrected the stale in-code comment
at `data_manager.py:113-124` that had claimed the opposite (that yfinance's hourly interval isn't
retroactively split-adjusted at all). Full writeup: `docs/research_log.md`'s 2026-07-22 entry.
**Real, distinct gap raised in the same discussion, backlogged (medium priority, doesn't block
trading) rather than built**: no archived record of the exact cache-file data that fed any past
backtest — mutated in place on every split-guard rescale. User's explicit call: traceability (know
when/why/how data changed) over full immutable/versioned data linked to each `backtest_cache` row
— the latter would be real reproducibility but a much bigger lift (schema change across the whole
sweep engine, real storage growth), and isn't needed if the scale-invariance argument above holds.
Design agreed for later: a `data_mutation_log` table in `trading_universe.db` (via `db_cache.py`),
one row per split-guard rescale event with a `pre_mutation_snapshot` of the actual old data (not
just metadata about the change) — cheap since these events are rare. Not yet built.

## ✅ [live-trading][backtest] Resolved 2026-07-20 — watchlist 65 candidate testing complete; found+fixed a live/paper compliance gap and a `create_watchlist` id-burning bug
Full writeup in `docs/research_log.md` ("2026-07-20 (cont.) — Watchlist 65 candidate
testing: chaos-monkey, Stage 5 compliance gap found+fixed, watchlists.id gap
root-caused+fixed"). Short version: chaos-monkey execution-adherence extended to cover
`TrailingExitZScoreBreakout` and `open_check` (previously TB/close-only), repointed at
watchlist 65 — no node collapses even at 20% miss rate, USD's thin sample cleared. Stage
5 compliance recheck found `strategies.py::check_exit` (used by both real live positions
and paper trading) never received the 2026-07-20 exit-side gap-through-trigger kernel
fix — fixed by threading a real `open_price` through `check_sell_condition` from both
call sites and adding the Open-first gap check to `check_exit`. Verified via full
bar-by-bar parity against real historical trades (280 total across SOXL/AGQ, effectively
zero mismatches). Also found+fixed, while investigating the unexplained watchlist id
57->65 jump: `signals_db.create_watchlist`'s `INSERT OR IGNORE` silently burns an
AUTOINCREMENT id on any duplicate-name call even though no row is written (reproduced
directly) — fixed to check existence first. No node params changed; watchlist 65 is
candidate-tested and clear of blockers.

## ✅ [backtest] Resolved 2026-07-20 — last-window market-on-close vs trailing-buy comparison, MOC does not win
Full writeup in `docs/research_log.md` ("2026-07-20 — Last-window market-on-close vs
trailing-buy comparison"). Short version: for signals firing in the last daily window
(the 14:30 bar, no time left before the overnight gap), compared TB's real bounce-fill
against an MOC counterfactual entry at the same signal bar's Close, across all 4
`TrailingBothZScoreBreakout` tickers in the Live v5 watchlist (GDXU, HIBL, SOXL, USD) and
all 3 fill resolutions (possible/pessimistic/certain). TB won every ticker/resolution,
often by 5-20x on compounded return, driven by a fat right tail of large winners MOC's
earlier entry misses. Hand-verified 2 SOXL trades entry-to-exit against real OHLC bars —
every fill price traced exactly to a real Open/High/Low/Close. No 18-ticker backfill
run — the result was decisive enough on the current watchlist alone; treated as closed.
New tooling: `scripts/sim_close_vs_trail_buy.py` + `export_trades.
collect_last_window_comparisons`/`simulate_trail_both_signal_tracked`/
`_simulate_exit_from_entry`.

## ✅ Resolved 2026-07-20 — full 18-ticker v5 resweep completed; immediate-entry vs trailing-buy comparison; "Live v5" watchlist built

The interrupted resweep (started 2026-07-20, see prior entry below) was completed the same
day using the new per-ticker resumable queue tooling (`scripts/run_sweep_queue.sh` +
`scripts/campaign_config.py`) instead of the old batch `run_v4_full18_resweep.sh`/
`run_v5_full18_test.sh` scripts. Full writeup, including the immediate-entry
(`TrailingExitZScoreBreakout`) vs trailing-buy (`TrailingBothZScoreBreakout`) cliff-safety
comparison and two rounds of self-caught errors in that comparison (unfiltered-alpha
mistake, then a summary-sentence contradicting the very table it was drawn from), is in
`docs/research_log.md` ("2026-07-20 — Immediate-entry (TrailingExit) vs trailing-buy
(TrailingBoth) full 18-ticker v5 comparison").

New standing tool: `scripts/campaign_comparison_table.py` — prints the side-by-side
TB-vs-TE comparison table (best alpha, worst_neighbor/cliff status, full node params,
trades, win rate, liquidity) directly from `backtest_cache`, with a `--min-best` filter.
Built because this exact table got hand-rebuilt piece by piece several times in
conversation before being made a real script — use this going forward instead of
re-deriving it ad hoc.

**Outcome**: 10 tickers selected for a new "Live v5" watchlist (`watchlist_id=65`,
created+activated this session, superseding `watchlist_id=57` "Live v4"): AGQ, DPST,
GDXU, HIBL, KORU, NUGT, SOXL, UDOW, USD, YANG — split 4 `TrailingBothZScoreBreakout`
(GDXU/HIBL/SOXL/USD) and 6 `TrailingExitZScoreBreakout` (AGQ/DPST/KORU/NUGT/UDOW/YANG),
picked per-ticker by whichever strategy had the higher cliff-safe robust alpha at
`fixed_sl∈{1,2,3}`. TQQQ and LABU had a viable (cliff-safe) TE node but were dropped by
user call anyway. DUST/GDXD/NAIL/RETL/UVIX/ZSL excluded entirely — no cliff-safe node in
either strategy at this SL range. All 10 new nodes inserted via
`scripts/build_v5_watchlist.py` in `mode='research'` (not live, not automation-enabled) —
see the still-open candidate-testing item in `docs/backlog_cache.md` for what's needed
before any of these go further.

Also fixed while building the comparison table: `TrailingBothZScoreBreakout`'s swept
"stop_loss" grid axis is actually `trail_buy_pct` (real `stop_loss` column holds the
constant `fixed_sl`) — an ad hoc query that used the literal `stop_loss` column for the
cliff-neighborhood check produced numbers that disagreed with the real
`identify_full_mesh_candidates` log output until this was caught and fixed.

## ✅ [live-trading][security] Resolved 2026-07-21 — SELL-side automated-order attempt is now mode-gated, not just ticker-gated

Found 2026-07-19 while reviewing whether EDC (real open position, `research`-mode node) could
safely join the widened automation scope. `notify_trailing_activated` (`signals_notify.py`) is
called unconditionally from the real `open_positions` exit-check loop in `run_loop` and calls
`_attempt_automated_sell(pos, current_price)` with no check on the position's node `mode` —
only `_attempt_automated_sell`'s own `ticker not in AUTOMATION_ENABLED_TICKERS` gate stood
between a `research`-mode ticker's real open position and a real/dry-run automated sell
attempt. The BUY side didn't have this gap: `_scan_buy_signals` only reaches
`_attempt_automated_buy` when `node.get('mode')=='live'`. This is why EDC's node was removed
from `watch_list` rather than folded into the widened automation scope at the time — adding
its ticker to `AUTOMATION_ENABLED_TICKERS` while its real position's node was still `research`
would have exposed that position's exit to the automated-sell path.

**Fixed 2026-07-21**: `_attempt_automated_sell` now looks up the position's own `(ticker,
window)` node from `db.get_watchlist()` and only proceeds when a matching node exists with
`mode=='live'` — falling back to the manual flow (not KeyError) both when the mode doesn't
match and when no matching node exists at all (e.g. a since-removed node, mirroring EDC's
removal). Matches `automation_principles.md` #7 (new automation surfaces inherit the full
existing gating, not a subset). Two new tests in `tests/test_schwab_automation.py`:
`test_automated_sell_falls_back_when_node_mode_not_live`,
`test_automated_sell_falls_back_when_no_matching_node`. Full suite: 156 passed.

## [live-trading] Low priority, 2026-07-19 — paper-trading dedup is ticker-only, not `(ticker, window)`-aware

`paper_trading.start_paper_buy`'s dedup guard (`db.get_paper_pending_buy(ticker)` /
`db.get_open_position(ticker, paper=True)`) is a single-ticker lookup — unlike the real
`open_position()`, which dedups on `(ticker, window)` (`signals_db.py:737-738`, itself not
`node.id`-based, so two nodes sharing a `window` value would still collide there too). Fine
today since every automation-enabled ticker has exactly one node. Would need to become
`(ticker, window)`-aware before a ticker could ever run two nodes (e.g. a legacy v3.x node
alongside a new v4 node) through paper trading simultaneously — the second node's BUY
signal would otherwise be silently dropped as a false "already open" duplicate. Not
scoped, no ticker needs this today.

## Default rule: Slack-notify on every action-requiring state change (2026-07-07)

Established as a standing convention, not a one-off feature: any state transition in `active_signals.py` that requires the user to do something (place/cancel an order, confirm a fill, etc.) must have a corresponding Slack notification. Prompted by HIBL's arm-sell threshold crossing without an alert — but on investigation, the notification already exists (`notify_trailing_activated()`, fires on `just_activated_trailing` at `active_signals.py:1582-1583`) and today's miss was purely because the daemon was down the whole time since HIBL's buy signal (same root cause as the heartbeat/watchdog gap below), not a missing notification path.

Current coverage, for reference: buy signal (`notify_buy_signal`), sell signal/SL/TP/TIME/TRAIL (`notify_sell_signal`), trailing armed (`notify_trailing_activated`), limit fill (`notify_limit_filled`) all exist and are wired into `run_loop`. Action item going forward: any new state/strategy added to `active_signals.py` must include a matching Slack notification before being considered done — audit this against the rule at that time, don't assume coverage.

## Out-of-band heartbeat/watchdog for active_signals.py (2026-07-07)

Daemon crashed today mid-session (the `tickers`-table bug during the DB split) and there was no independent alert — Slack itself is posted *by* the daemon, so it can't reliably alert on its own death. Needs a separate standalone process: a lightweight watchdog (own process, cron or sleep-loop) that checks a heartbeat (daemon touches a file/DB row every poll cycle) and posts to Slack via its own independent webhook call if the heartbeat goes stale during market hours — so a bug in the main daemon can't take down its own alerting. Not started.

## ✅ Resolved 2026-07-12/13 — "What's close" proximity script

Covered by a different mechanism than originally scoped: the reference report's `build_reference_table` (`active_signals.py`) already shows per-ticker buy-trigger/arm-trigger distance (signed `Proximity` column) for every `mode='live'` node, and the 2026-07-12 "Resend Report" button lets the user trigger a fresh one on demand from Slack — no standalone script or slash command needed. Live-verified via multiple real sends.

## ✅ Resolved 2026-07-13 — Add account tracking (Brokerage/SEP/IRA/Roth) for portfolio performance

Built as originally scoped: `account` column added to both `open_positions` and `trade_log` (`trading_live.db`, migration in `ensure_tables()`, backup taken first — `cache/trading_live.db.bak_pre_account_migration_20260713`), threaded through `open_position()`/`log_trade_entry()` so the account is captured at execution time from the node's current `watch_list.account` value (survives later account reassignment, e.g. LABU's ira→roth move — old trades keep the account they were actually placed in). `pages/10_Open_Positions.py` shows an Account column. `pages/4_Portfolio.py` gained a new "Account Performance (live)" section: per-account realized trade count/win rate/compounded return from `trade_log`, plus open-position count/unrealized $ P&L from `open_positions` (current price via yfinance). Smoke-tested (HTTP 200, no traceback) and spot-checked against the real DB.

**Caveat**: only trades placed 2026-07-13 onward carry a real account value — the 4 positions open before the migration (AGQ/HIBL/EDC/SOXL) show as `unknown` since `account` was NULL at insert time; no historical backfill is possible (the value wasn't captured anywhere before now). The Portfolio page surfaces this as a caption when `unknown` rows exist.

## ✅ Done 2026-07-07 (evening, while user away) — Split trading_universe.db into live/research files; folded Watchlist Trade Pivot into Top Pivot; trail_pct rename still deferred

**DB split**: Built `cache/trading_live.db` as a **copy** of `watchlists`/`watch_list`/`open_positions`/`trade_log` (4/47/2/3 rows) — `cache/trading_universe.db` untouched, still holds `backtest_cache`/`hurst_cache`/`tickers` plus the original (now-redundant) copies of the live tables. Repointed `active_signals.py`'s `DB_PATH` to `trading_live.db`, added `RESEARCH_DB_PATH` for the one `hurst_cache` lookup it makes. Repointed `pages/10_Open_Positions.py`, and split `pages/4_Portfolio.py`/`pages/0_Top_Pivot.py`/`scripts/post_sweep_report.py` into dual-path (live table queries → `trading_live.db`, `backtest_cache` queries → `trading_universe.db`); `pages/0_Top_Pivot.py`'s watch_list/backtest_cache join now uses `ATTACH DATABASE` across the two files (verified working). `scripts/verify_live_parity.py` and `scripts/live_test.py` needed no changes (already DB-path-agnostic via monkeypatch / re-import).

**Correction to the original split plan**: `kv_cache` stays in the research file, not the live one — every `db_cache.py::refresh_*_cache()` function populates it from `backtest_cache` queries and it's consumed only by research-side Streamlit pages (pivot/cliff-grid/dropdown caches), not by `active_signals.py` at all.

**Not yet cut over**: the already-running `active_signals.py run` process (started 06:04 same day) still has the old code loaded and is reading/writing `trading_universe.db`'s original live tables — it was deliberately left untouched (user explicitly declined a restart mid-day). The code changes above only take effect on next restart. **Before trusting `trading_live.db` in production**: restart `active_signals.py`, confirm it comes up reading the new file, then drop the now-redundant `watchlists`/`watch_list`/`open_positions`/`trade_log` tables from `trading_universe.db` (not done — old tables currently still exist there too, in sync as of the split moment but will drift once the new daemon starts writing to `trading_live.db` only).

**Watchlist Trade Pivot fold**: absorbed `pages/12_Watchlist_Trade_Pivot.py`'s WIN/LOSS/TWIN/TLOSS/OPEN summary + drill-down into a new section at the bottom of `pages/0_Top_Pivot.py` (still reading from the `cache/watchlist_sweep.db` sandbox, unchanged) and deleted the standalone test page. Verified both pages return HTTP 200 with no tracebacks via a headless Streamlit smoke test.

**trail_pct → trail_sell_pct rename**: still deferred, now for a sharper reason than "wait for off-hours" — traced through `active_signals.py`'s `run_loop` and found `check_sell_condition` is called with no try/except around it (`active_signals.py:1522-1541`), so renaming the column out from under the running daemon would cause an uncaught `KeyError` on the very next poll cycle and kill the entire process (no monitoring on any open position until manually restarted). Do this only immediately before a planned restart, not mid-session.

## ✅ Resolved 2026-07-12 — `trail_pct`/`take_profit` rename propagation

All previously-listed files done: `pages/2_Node_Inspector.py`, `pages/3_Winners.py`, `pages/4_Portfolio.py`, `pages/10_Open_Positions.py` (`take_profit`→`axis_tp`, `trail_pct`→`trail_sell_pct` in SQL), `scripts/export_cliff_safety.py` (same rename, plus fixed a pre-existing `sl_label`/`sl_display` NameError left over from the original commit), `scripts/verify_live_parity.py` (node dict key `trail_pct`→`trail_sell_pct` to match `active_signals.py`'s expected key). `scripts/fill_trail_pct_gaps.py` needed no change — doesn't touch those columns. **Note**: other pages (`8_ADF_Filter.py`, `11_Universe_Scan.py`, `1_Spatial_Topology.py`, `7_Hurst_Filter.py`, `scripts/profile_dispatch.py`, `scripts/post_sweep_report.py`, `scripts/top_safe_nodes.py`) still query raw `take_profit` but were never in scope — they only look at non-v3.x strategies where the column is still populated; revisit only if they ever need to show `TrailingBothZScoreBreakout` rows. **Exception unchanged**: `cache/watchlist_sweep.db` is a separate, never-migrated snapshot DB.

## ✅ Mostly resolved 2026-07-13 — Live/backtest parity gap (trailing-buy/sell fill-timing)

`TrailingBothZScoreBreakout`'s trailing-buy "wait for bounce" entry has no live-orchestration implementation (hands off to a broker-side trailing-buy order) — `scripts/verify_live_parity.py` deliberately can't compare it. Instead of waiting on real broker fill data, built `scripts/verify_trailing_buy_resolution.py`: re-detects every recent signal's bounce-entry using yfinance 5-min bars (real intra-hour tracking) and diffs against what the hourly-bar kernel (`_simulate_trail_both`) would catch for the same signal. After fixing a cutoff-time bug 2026-07-13 (see below), result across all 11 watchlist-9 tickers (130/130 signals matched, last ~58d): mean price diff +0.19%. **SOXL is still the real outlier**: +1.81% mean fill-price penalty (individual signals up to +15.5%), driven by its `trail_buy_pct=1%` being far tighter than its own ~3.65% median intra-hour swing (ratio 3.65) — volatile enough to cross/re-cross the trigger within an hour, so 5-min tracking locks in an earlier/worse fill than the hourly kernel models. TQQQ shows smaller +0.84% drift; everything else is at/near parity (AGQ actually skewed favorable, -2.01%). Formalized as a repeatable procedure in `docs/watchlist_candidate_checklist.md` (checks 2/3).

Built the mirror-image check for the **exit** side 2026-07-13, `scripts/verify_trailing_sell_resolution.py`: same idea, but re-detects the peak/trail_stop crossing once trailing arms, using 5-min bars vs. the hourly kernel's trailing branch. Result: 21/21 exits matched, mean diff -0.17% — trailing-sell is already at parity across the whole watchlist (unlike entries, live trailing-sell is monitored continuously by `active_signals.py` itself, not handed off blind to a broker order, so this check mainly validates the *backtest's* hourly-bar exit modeling rather than a live-execution gap). LABU showed -4.6% on a single sample — not enough data to call a real outlier yet.

**Real bug found and fixed while building the sell-side script**: `max_hold_hours` counts hourly *bars* (~7/trading day), not calendar hours — the original buy-side script's cutoff-time math (`signal_time + timedelta(hours=max_hold_hours)`) was computing a cutoff days too early for any trade near its actual max-hold window, silently reporting fabricated "ran out of data" exits instead of real ones. Fixed in both scripts (now look up the real bar timestamp via `timestamps[entry_i + max_hold_hours]`). Rerunning the buy-side script after the fix confirmed the original SOXL finding wasn't an artifact of this bug — numbers moved only slightly (130/130 matched vs. 134/138 with the shorter truncated dataset before the fix).

**Still not fully closed** (residual, carried forward, no dedicated tracking item since — fold in if this area comes up again): both checks validate the *price* assumption is broadly sound (except SOXL entries); they don't validate the broker's trailing-buy order mechanics themselves (whether Schwab's own `TRAILING_STOP` trigger/fill logic matches the running-low model at all) — that piece still has no real-fill-time-vs-signal-time verification against actual Schwab fills.

## ✅ Resolved (db round-trip coverage built) / Dead (Task Scheduler piece dropped) 2026-07-13/14 — Test coverage & heartbeat watchdog

**Automated round-trip DB test** (`add_node`→`open_position`→`check_sell_condition`, no coverage existed as of 2026-07-13): built — `tests/test_db_roundtrip.py` and `tests/test_signal_and_notifications.py` now cover this path. Resolved.

**Heartbeat/Task Scheduler watchdog** (`scripts/check_heartbeat.py`, posts a Slack alert if `active_signals.py`'s heartbeat file goes stale — meant to catch the daemon going silent, e.g. host sleep/suspend, without relying on the daemon itself to notice its own death): scoped in full (Task Scheduler setup walkthrough, "run only when logged on" vs. password-store tradeoff, retry/missed-start behavior) then **deliberately dropped 2026-07-13** — for the failure modes it would catch (sleep/network/power), the user has no way to act on the alert remotely while at work, so the alert would be pure unactionable stress. Root cause (WSL sleep during market hours) fixed directly via a Windows power-plan change instead. `check_heartbeat.py` itself was left ~80% built and still works standalone if ever revisited — same-session fix wrapped `main()` so an unhandled crash in the check itself also posts a Slack alert, not just the two originally-expected stale/missing paths. See `docs/design.md`'s Heartbeat section for the current authoritative note.

## ✅ Resolved 2026-07-14 — trailing-buy re-entry timing after a same-day exit

No bug — same-day re-entry uses the exact same two daily signal windows as any other entry
(`_simulate_trail_both`'s new-signal detection has no calendar-day reset or first-entry-of-
day special-casing). Full experiment writeup in `docs/research_log.md` (2026-07-14 entry).

## ✅ Resolved 2026-07-15 — Phase 3 (full parameter mesh) adds no value over Phase 1/2/2.5

Phase 3 won 0/30 tagged SOXL+KORU v4 SL-sweep campaigns — Phase 1 or Phase 2 always held
the best robust-alpha node. `--max-phase` cap added to `run_optimization_sweep.py` (default
3, unchanged behavior) so future runs can skip it. Full experiment writeup in
`docs/research_log.md` (2026-07-15 entry).

## ✅ Resolved 2026-07-17 — same-day buy→sell block explored and deliberately not built

PDT rule eliminated by FINRA effective 2026-06-04 (Regulatory Notice 26-10); no broker or
regulatory reason for a same-day round-trip block remains. Real cost of blocking anyway is
high (GDXD retains only ~47% of edge if same-day exits are deferred to the next day).
Decision: proceed without a block. `schwab_safety.py`'s existing `same_day_block` (blocks
same-day *re-buy*, a different, unrelated direction) is untouched, still enforced live.
Full experiment writeup in `docs/research_log.md` (2026-07-17 entry).

## ✅ Done 2026-07-07 — Composite index `idx_bc_ticker_strategy_version` on `backtest_cache`

Added `CREATE INDEX idx_bc_ticker_strategy_version ON backtest_cache(ticker, strategy, version)` to both `cache/trading_universe.db` (86.2M rows, 430s to build) and the new `cache/watchlist_sweep.db` sandbox (34.7M rows, 49s). Found while an ad-hoc `ticker IN (...) AND strategy=? AND version LIKE 'v3.%'` query was scanning most of the table — `EXPLAIN QUERY PLAN` showed it falling back to the PK's autoindex (`strategy` is the PK's only usable leading column for that filter shape); none of the 4 existing secondary indexes had `ticker` paired with `strategy`/`version`. This index makes that exact filter shape (ticker-set + strategy + version-prefix) a pure index scan going forward.

## Watchlist-scoped trade-cache sandbox — new pattern, to formalize/absorb later (2026-07-07)

Built `cache/watchlist_sweep.db` as a disposable snapshot (not part of the live/research split below — a throwaway sandbox for prototyping) containing: a scoped `backtest_cache` subset (`TrailingBothZScoreBreakout`, v3.x, the 11 current watchlist tickers only — 34.7M rows), a copy of `watch_list` for `watchlist_id=9` ("Sweep v3 - Full"), and a new `trade_cache` table (one row per individual trade — entry/exit time/price, hours_held, Result WIN/LOSS/TWIN/TLOSS/OPEN, Return — keyed by full node identity) populated by running the actual kernel (`backtester.run_backtest_dispatch`) once per node rather than trusting `backtest_cache`'s aggregate `win_rate`/`win_twin_rate` columns (which can be stale — see finding below).

Built `pages/12_Watchlist_Trade_Pivot.py` as a **test page** against this sandbox: per-node WIN/LOSS/TWIN/TLOSS/OPEN breakdown + compounded return, with a drill-down into individual cached trades. User's explicit plan: test it standalone, then absorb into the existing `pages/0_Top_Pivot.py` later rather than keeping it as a separate permanent page.

**Related finding, not a new bug**: `backtest_cache.win_twin_rate` reads as `0.0` for any row computed before that column existed (added in commit `252b3bf`) — `run_optimization_sweep.py:66-72` explicitly documents old rows are never recomputed retroactively. SOXL's v3.18 rows are affected. The `trade_cache` sandbox sidesteps this entirely by computing fresh from the kernel instead of reading the cached aggregate.

**Open question**: should this sandbox pattern (scoped snapshot + trade_cache) become the permanent shape of the "watchlist" tier in the live/research split below, or stay a one-off testing tool? Not decided.

## ✅ Closed 2026-07-07 — SOXL/KORU live exit-strategy decision (overtaken by events)

Was an open question about whether to switch SOXL/KORU's live exit params to better-backtesting candidate configs (SOXL v3.35, KORU v3.34) — see prior analysis in git history if ever needed. Overtaken by the 2026-07-07 market swing: SOXL hit its real stop-loss and was closed out (-15.38%, logged in `trade_log`); KORU also breached its stop but the user chose to hold through it manually, working the position directly rather than via a config swap. No longer an open decision.

## Rename trail_pct → trail_sell_pct; also take_profit → trail_arm_pct for TrailingBoth; split buy/sell display columns (2026-07-06, updated 2026-07-06)

`trail_pct` (exit-side trailing %) and `trail_buy_pct` (entry-side bounce %) are asymmetrically named — the sell-side one kept its original name from `TrailingExitZScoreBreakout` (v1.8, back when there was only one trailing mechanism), while the buy-side one got a distinct name when `TrailingBothZScoreBreakout` added a second trailing axis. User wants `trail_pct` renamed to `trail_sell_pct` for symmetry/clarity, so buy-side and sell-side code/display never need to be parsed apart from a combined string.

**Added while walking through a full `TrailingBothZScoreBreakout` trade end-to-end (2026-07-06, investigating why SOXL v3.35 showed a 6837% alpha vs v3.18's 2894% — turned out to be real market data, a genuine +182% single trade during SOXL's documented April 2026 rally, not a bug)**: `take_profit` is also mis-named for this strategy — it doesn't exit the trade, it arms the trailing-sell mechanism once cleared (`check_exit` sets `state['trailing']=True` at that threshold, then rides `peak - trail_pct%` from there). Rename to `trail_arm_pct` for this strategy's semantics. Full sequence should read, in order: `trail_buy_pct` (bounce entry confirmation) → `trail_arm_pct` (threshold that arms the trailing sell, was `take_profit`) → `trail_sell_pct` (trailing-stop width, was `trail_pct`).

Decided explicitly **not** to physically reorder the DB columns to match this sequence — SQLite column order doesn't affect queries, only reordering would require a full rebuild of the 146M-row `backtest_cache` table (same risk class as the REINDEX incident earlier this session). Instead: rename only, and get the bounce→arm→trail-sell reading order via explicit column ordering in `SELECT` statements and the Cliff Safety CSV export, not the physical schema.

Scope (real column in 3 DB tables — **`open_positions` currently holds live KORU/SOXL positions**, so this must not be done carelessly mid-trade):
- DB: `ALTER TABLE ... RENAME COLUMN trail_pct TO trail_sell_pct` on `backtest_cache`, `watch_list`, `open_positions` (SQLite supports this natively, no full rebuild). Also consider renaming `take_profit` → `trail_arm_pct`, though note `take_profit` is a genuinely shared/generic axis column reused with real take-profit semantics by other strategies (`ZScoreBreakout`, `LimitExitZScoreBreakout`, etc.) — a straight column rename would mis-name it for those, so this likely needs to stay a per-strategy *display/label* rename rather than a DB column rename, unless a fuller audit says otherwise. **Back up the DB file first**, per established practice ([[feedback_backup_before_schema_migration]]).
- Code: rename the `trail_pct` param/column references across `strategies.py`, `backtester.py`, `run_optimization_sweep.py`, `active_signals.py`, `pages/0_Top_Pivot.py`, `pages/2_Node_Inspector.py`, `pages/3_Winners.py`, `pages/4_Portfolio.py`, `scripts/export_cliff_safety.py`, `scripts/fill_trail_pct_gaps.py`, `scripts/verify_live_parity.py`, `tests/test_TrailingBuyZScoreBreakout.py` — do NOT touch `trail_buy_pct` (no substring collision, but double check regex/replace scope).
- Also fix `scripts/export_cliff_safety.py`'s/`pages/0_Top_Pivot.py`'s combined `sl_display` ("Bounce % / Trail %" as one string) to output separate `trail_buy_pct`/`trail_sell_pct` numeric columns instead, ordered bounce→arm→trail-sell — this was the immediate trigger (Excel-facing CSV was mixing buy and sell values into one field).
- Verify KORU/SOXL still read back correctly from `open_positions` after the rename before considering it done.

Deferred until after live positions are settled or clearly off-hours — not done same-session as opening those two positions.

## ✅ Done (mostly) — Split trading_universe.db into separate live/research DB files (2026-07-06, cutover verified 2026-07-07)

`cache/trading_live.db` (watchlists, watch_list, open_positions, trade_log) and `cache/trading_universe.db` (backtest_cache, hurst_cache, tickers, kv_cache) now fully split, `active_signals.py` repointed and verified reading/writing the live file exclusively (confirmed via `ensure_tables()` + live position exit-check exercise). **Not yet done**: the 4 stale duplicate live tables (`watch_list`/`watchlists`/`open_positions`/`trade_log`) are still physically present in `trading_universe.db`, unused — a backup exists (`cache/trading_universe.db.bak_pre_table_drop_20260707`) but the actual `DROP TABLE` never ran. Safe to do once the `arm_sell_pct` migration (see rename item) finishes — don't run concurrent writes against the same file. Also cosmetic: the research file is still literally named `trading_universe.db`, not renamed to `trading_research.db`.

## Dead 2026-07-13 — Slack slash-command interaction for the live trading app (2026-07-06)

Originally scoped commands: `/positions` (check open positions), `/watchlist` (watchlist/buy-trigger status), `/status` (general daemon health). Superseded piecemeal by the button-based interaction pattern built across many sessions instead of a slash-command design: `/positions` ≈ `scripts/open_positions_status.py` + the manual open/close buttons (2026-07-12); `/watchlist` ≈ the reference report's Proximity column + on-demand resend button (2026-07-12, see the resolved proximity-script entry above). `/status` has no direct equivalent — resending the reference report and getting silence is a de-facto down-detector, but it's passive (relies on the user thinking to check) rather than a proactive alert. That gap is really the same problem as the still-open "Out-of-band heartbeat/watchdog" item above, not a slash command — closing this out as dead rather than carrying a stale command-design item forward.

## Split active_signals.py into modules (2026-07-06)

`active_signals.py` (1680+ lines) has grown to hold everything: DB connection/schema, watchlist CRUD, positions CRUD, signal computation, chart generation, Slack messaging, and the `run_loop` daemon + CLI, all in one file. Split in two passes, lowest-risk first:

1. **Pass 1 — watchlist + positions**: pull `get_watchlists`/`get_active_watchlist_id`/`create_watchlist`/`delete_watchlist`/`set_active_watchlist`/`get_watchlist`/`add_node`/`remove_node`/`set_node_mode`/`label_node` into `watchlist.py`, and `get_open_positions`/`update_position_trail_state`/`open_position`/`close_position`/`log_trade_entry`/`log_trade_exit` into `positions.py`. Both are pure DB CRUD, no coupling to signal logic — safest to extract first.
2. **Pass 2 — db + notify**: pull `_conn`/`ensure_tables` into `db.py` (shared base everything else imports), and the Slack/chart layer (`_post_message`, `_build_buy_blocks`, `_build_sell_blocks`, `notify_buy_signal`/`notify_sell_signal`/`notify_trailing_activated`/`notify_limit_fill`, `_chart_buy`/`_chart_sell`/`_upload_chart`) into `notify.py`.

Leaves `compute_buy_signal`/`check_sell_condition`/`run_loop`/CLI in `active_signals.py` (or a renamed daemon module) as the actual trading-logic core. Not started — deferred, live trading week takes priority.

## ✅ Done — Live-test the watchlist (2026-07-07)

Sweep 3 / "Sweep v3 - Full" watchlist is live and has been traded through a real market swing (SOXL stopped out, KORU held through a breach, HIBL entered and armed its trailing sell) — the priority this note flagged is complete.

**Milestone marker**: once the axis-schema cleanup (2026-07-05 — `strategies.py` class attributes + `validate_axis_values`, consolidating the 5 duplicated `_resolve_axis_columns`/`uses_fixed_sl` copies) and this live-test session both land, that's the end of `docs/operational_limits.md`'s Phase 1 (Manual Execution). Revisit then whether to relabel/split phases (e.g. current work retroactively "Phase 0" bootstrapping vs. a "Phase 1" that starts once Sweep 3 is the confirmed live watchlist) and define what Phase 2 actually covers (automation — see [[project_execution_automation_plan]]).

## Slack TEST MODE marker for manual notify_* testing (2026-07-05)

Found while manually testing `notify_buy_signal`/`notify_sell_signal` message rendering for SOXL/TQQQ: `SOCKET_MODE` is driven by real `.env` Slack credentials (bot/app token + `#trading` channel), so any ad-hoc script that calls a `notify_*` function posts to the real live channel with no indication it's a test — indistinguishable from an actual signal, including live Executed/Skipped buttons wired to real `open_position`/`close_position`. Add a `TEST_MODE` env var (or explicit param threaded through `_post_message`) that prefixes messages with something like "🧪 TEST MODE — " and swaps the action buttons for inert ones (or a plain-text context block) so manual testing can't be mistaken for — or accidentally trigger — a real trade.

## Automated round-trip test for active_signals.py (2026-07-05)

No test coverage exists for `active_signals.py`'s DB layer — `tests/` only exercises strategy kernels via fake dicts, never `add_node`/`open_position`/`check_sell_condition`. Found while quick-manually-testing the fixed_sl/trail_pct round-trip for a real v3.x trailing node (SOXL v3.18) and realizing there's no repeatable way to catch this axis-mapping bug class (already hit twice: the Portfolio page `load_watchlist_metrics` bug and the `dispatch_parallel_grid` `uses_fixed_sl` regression). Plan: pytest test monkeypatching `active_signals.DB_PATH` to a temp sqlite file, calling `ensure_tables()`, then driving `add_node()` with real v3.x trailing values → read back `watch_list` row → `open_position()` → read back `open_positions` row → `check_sell_condition()` with synthetic prices, asserting `trail_pct`/`fixed_sl`/`trail_buy_pct` survive each hop unmangled.

## 2026-07-05 (night) — win_twin_rate metric added; trail_pct sparse-then-fill extension; ALL53 ticker shorthand

- **Added `win_twin_rate` column to `backtest_cache`** (`run_optimization_sweep.py::init_idempotent_db`, simple `ALTER TABLE`, no PK rebuild needed): the existing `win_rate` only counts `Result=='WIN'` exactly, silently excluding profitable `TIME`-exit trades (`TWIN`) — found while investigating why a KORU node showed a 21% win rate despite yielding about the same alpha as a 71%-win-rate node (turned out 71% of its trades were actually profitable, just via `TWIN`, not counted). `win_twin_rate = (WIN+TWIN)/trades` is the real "did this trade make money" rate; old rows keep `win_twin_rate=0` (not recomputed retroactively — would require re-simulating all historical trades). Threaded through `dispatch_parallel_grid`'s cache-hit path, live-compute path, both INSERT statements, and displayed in `pages/0_Top_Pivot.py`'s Cliff Safety table alongside the original `win_rate` (kept, not replaced).
- **Extended `scripts/run_v3_backfill_sweep.sh`** with every single-percent trail_pct version 8-30% (`version = trail_pct% + 20` — same formula the existing 1-7% versions already followed, v3.21=1%...v3.27=7%) after finding `TrailingExitZScoreBreakout`'s (v3.18) full 1-30% sweep did much better at wide trail_pct (9-24%) than `TrailingBoth`'s tested 1-7% range. Sparse set (9/12/15/18/21/24/27/30% = v3.29/32/35/38/41/44/47/50) planned to run first; all in-between single-percent versions are wired and ready with no further script edits needed.
- **Built `scripts/fill_trail_pct_gaps.py`**: reads whatever sparse trail_pct data exists per ticker, finds each ticker's best value so far, and prints (doesn't execute) the `run_v3_backfill_sweep.sh` commands to backfill its immediate ±1% neighbors — mirrors the sweep engine's own coarse-then-island refinement, applied to the trail_pct axis. Already found (before any sparse run) that EDC/HIBL/SOXL's best trail_pct sits at the edge of the already-dense 1-7% range (7%), meaning 8% (v3.28) is worth checking once actually run.
- **Added an `ALL53` ticker-arg shorthand** to `run_v3_backfill_sweep.sh` (`./scripts/run_v3_backfill_sweep.sh <version> ALL53`) expanding to the full 53-ticker universe (same list as `run_v2_backfill_sweep.sh`), for running the v3.x strategy family against all 53 tickers instead of just Sweep 3's 11.

## ✅ Done 2026-07-05 — `backtest_cache` schema migration: real named columns + trail_pct as a true 4th axis (v3.x)

Built the "real named columns" option (see `docs/design.md`'s "v3.x reparameterization" section for full detail): `backtest_cache` schema rebuilt (60,364,303 rows verified carried over unchanged, no data migration needed — v1.x/v2.x rows keep their old overloaded meaning untouched), `stop_loss` now always means real SL going forward, `trail_buy_pct`/`trail_pct` are real columns. Went further than the minimal fix: `trail_pct` is now a genuine swept 4th grid axis for `TrailingBothZScoreBreakout` (`hyperparameters.trail_pcts`), replacing the old one-full-backfill-per-value pattern (v2.13-v2.17). Also fixed a pre-existing bug found along the way: Node Inspector/Portfolio only ever dispatched to `run_backtest_v17`-or-`run_backtest`, silently wrong for all 4 trailing strategies — now share one dispatch function (`backtester.py::run_backtest_dispatch`) with the sweep engine. `watch_list`/`open_positions` got a matching `trail_buy_pct` column; `add_node()` accepts real values for v3.x, falls back to legacy behavior for old nodes. v3.x backfill scope resorted and simplified same day (see `docs/design.md` "Version Changelog") — single combined tp/sl grid everywhere, TrailingBoth's trail_pct broken into per-value versions v3.21-27 instead of swept as a 4th axis in one run, restricted to Sweep 3's 11 tickers.

## ✅ Done 2026-07-05 — Fixed `add_node()`'s legacy fallback mis-mapping trail_buy_pct/trail_pct for `TrailingBothZScoreBreakout`

Found while asking "do any current watchlist winners sit in the 1/2/4/5 sl range" — the legacy fallback in `active_signals.py::add_node()` (added during the v3.x migration) always assumed the overloaded `stop_loss` value meant `trail_pct`, which is only true for `TrailingExitZScoreBreakout`/`LimitOrderTrailingExit`. For `TrailingBothZScoreBreakout` it actually means `trail_buy_pct`, with `trail_pct` a separate static per-version constant (v2.13=1%...v2.17=5%) that lived in `config.execution.trail_pct` at backfill time and can't be recovered from the row itself. All 8 Sweep 3 `TrailingBothZScoreBreakout` watch_list rows (AGQ/DPST/EDC/GDXU/HIBL/KORU/UVIX/YANG) had this backwards (`trail_buy_pct=0`, `trail_pct`=the real bounce value) — fixed in code (dispatches by strategy now, mirrors `run_optimization_sweep.py::_resolve_axis_columns`) and backfilled the 8 rows directly (backup at `cache/watch_list_backup_pre_trailfix_20260705.json`). No open position was affected (`open_positions` was empty throughout), but `check_sell_condition` would have used the wrong `trail_pct` for the first live trade on any of these 8 tickers. Also fixed `pages/0_Top_Pivot.py`'s "Cliff Safety" table, which displayed the raw overloaded `stop_loss` column with no indication of what it meant per strategy — now resolves and labels the real axis (SL / Bounce% / Trail% / Bounce%+Trail%) per row, and the neighbor-radius cliff query filters on the correct real column for v3.x rows instead of always `stop_loss`.

**Follow-up not done this session**: `pages/0_Top_Pivot.py`'s "Watchlist — Alpha by Strategy" section only queries `watchlist_id=1` and joins `backtest_cache`/`watch_list` on raw `b.stop_loss = w.stop_loss` — fine for that watchlist's real-SL strategies, but would break if it's ever extended to Sweep 3 (`watchlist_id=5`) or any future v3.x trailing-strategy node without the same axis-aware join fix.

## ✅ Done — Watchlist Repick (2026-07-04 → concluded 2026-07-07, Sweep 3/"Sweep v3 - Full" is live)

Was reviewing v2.x sweep results to repick the live watchlist (AGQ/EDC/FAS/HIBL). Entry/exit shorthand for reference: **Close** = bar-close confirmed entry, **Limit** = intrabar touch entry, **Trail** = trailing-buy bounce entry; **Fixed** = market exit at TP/SL, **Trail** = trailing-stop exit, **Limit** = limit-order exit. Existing versions: Close/Fixed=v1.5/2.5, Close/Trail=v1.8/2.8, Limit/Fixed=v1.7/2.7, Close/Limit=v2.12, Trail/Fixed=v1.9/2.9, Trail/Trail=v1.10/2.10. Concluded with the current v3.x, 10-ticker Sweep 3 watchlist now live. The "Research:" ideas below this point (post-loss cooldown, gap risk, seasonal patterns, etc.) are separate standalone research questions, still open.

Todo:
- **✅ Resolved 2026-07-05 — giving up on v2.4 (`TrendFilteredZScore`, 50-day SMA trend filter) for now**: didn't produce substantive signals. Not pursuing further.
- **✅ Resolved 2026-07-05 — giving up on limit-based order entry/exit variants for now**: v2.7 (`LimitOrderZScoreBreakout`) root-caused as structurally underperforming (see High Priority section), and unbuilt variants (Limit/Limit, Trail/Limit, v1.7-2, v1.7-3) aren't giving better signals than the close/trailing-based strategies already in place — same verdict as Hurst/ADF. Not pursuing further.
- **✅ Resolved 2026-07-05 — varying trailing-buy-entry % / trailing-sell-exit % already covered by v3.21-27**: `trail_buy_pct` is swept via the `sl` grid axis (combined grid incl. 1/2/4/5 low-end points) and `trail_pct` is swept 1-7% across the seven v3.21-27 versions (currently running backfill). No separate work needed.
- Revisit the design review list (`docs/code_review_findings.md`).

Research:
- **9:30am bar inclusion**: redoing charts/backtests to use the 9:30am bar (currently first bar is 10:30) is a big effort — changes every trading day from 7 bars to 8, requiring recalculation of max-hold-hours-to-bar-count conversions throughout `backtester.py`/sweep grids, plus re-fetching/re-caching hourly data with the new boundary. Flagged as research, not a quick todo.
- **Post-loss cooldown / trade freeze per ticker (2026-07-04)**: empirically checked the live watchlist's (AGQ/EDC/FAS/HIBL, v1.5 params) actual backtest trades — a large majority of losing exits are followed by a same-ticker re-entry within days (many same-day, in the next signal window), often at a *lower* price than the exit, not a recovered one. Mechanism: a stop-loss exit means price kept moving away from the entry, which against a lagging rolling SMA/std often makes the z-score deviation *larger*, so the same signal re-triggers almost immediately — the strategy has no memory of just having been stopped out. Proposed fix: block `check_signal` from returning BUY for N bars (e.g. 7) after that ticker's last exit. Caveat: this only delays re-entry, it doesn't prevent it if the ticker is still oversold once the freeze lifts — needs an actual kernel change (`_simulate`/`strategies.py`, new cooldown state) plus a backfill comparison against current results to know if it actually helps, not just a parameter tweak. (Side benefit: fewer close-in-time same-ticker round trips also reduces wash-sale frequency, though that's now less relevant since live testing is planned in IRA/Roth accounts — see `docs/operational_limits.md`.)
- Ways to avoid big losses, or strings of consecutive losses.
- Gap risk (overnight/weekend gaps through stop levels).
- Seasonal trade patterns.
- Tickers unrelated to a given ticker but highly correlated to its down-signals (cross-ticker signal).
- Split analysis into two halves — look for candidates whose edge holds in both halves (out-of-sample consistency) rather than just aggregate alpha.
- 3-month rolling window analysis — walk-forward validation instead of one static backtest window.
- Does higher liquidity correlate with the pattern holding up better?
- Why are some 3x leverage funds consistently more profitable (better alpha/win rate) than others across strategies/versions? Look for a common factor (underlying volatility/beta, decay from daily rebalancing, sector, liquidity, borrow cost) that predicts which leveraged ETFs are good candidates before running a full sweep on them.
- **KORU trail_pct=6-7% extension (2026-07-05)**: v2.13-17 (`TrailingBothZScoreBreakout`, trail_pct 1-5%) shows KORU's best node at 5% has 38 trades / 71% win rate, while 4 of the other 5 trail_pct values are stuck at exactly 25% win rate — likely a small-sample artifact (too few trades to mean anything at tight trail_pcts, not a real "gets worse" trend), not proof the pattern improves monotonically. Hypothesis: a tight trailing stop exits almost immediately on normal chop, so low trail_pct values barely get real trades at all; a wider stop gives the position room to actually ride the reversion, plausibly explaining both the trade-count jump and the win-rate jump together. Worth a single-ticker (KORU) rerun with `hyperparameters.trail_pcts=[1,2,3,4,5,6,7]` (now a real swept axis as of the v3.x reparameterization, see `docs/design.md`) to see whether 6-7% keeps improving, plateaus, or gives back — check trade counts at each value before trusting any win-rate comparison. Not built, one-off research item.

- **✅ Resolved 2026-07-04 — liquidity threshold (1% of 10-day avg $ volume) is fine as-is for lump-sum entry**: checked all four watchlist tickers' actual signal-window (10:25-10:40, 15:25-15:40) hourly $ volume against a $50k order, worst case HIBL's afternoon window at ~9.2% participation. Ran a square-root market-impact estimate (`impact ≈ k·σ·√participation`, using each ticker's own intrabar range as the volatility proxy) — worst-case estimated slippage ~0.4% (HIBL), everything else <0.3%. Negligible against 8-29% TP/SL bands and trade alpha in the tens-to-hundreds of percent. Tightening to 0.5% ADV would needlessly cut EDC/HIBL-type candidates for a cost that isn't real. No threshold change needed.

## High Priority

- **Trailing-buy entry fill assumes an optimistic (unproven) intrabar bar-path — needs a rerun of `_simulate_trail_buy`/`_simulate_trail_both` (v1.9/v1.10) with corrected fill logic (found 2026-07-10)**: `backtester.py`'s trailing-buy "waiting" loop (`_simulate_trail_both:602-616`, mirrored in `_simulate_trail_buy`) always updates `running_low` from the current bar's Low *before* checking whether the same bar's High clears the bounce trigger — i.e. it silently assumes the Low always happens before the High within every hourly bar, which is the best case for this strategy (lower running_low ⇒ easier/lower trigger to clear) and isn't knowable from OHLC bars alone. Built a side, read-only analysis in `scripts/export_trades.py` (`simulate_trail_both_ohlc_aware`, kept separate from the live kernel — nothing in `backtester.py`/`run_optimization_sweep.py` was touched) that resolves each bar's fill as **CERTAIN** wherever possible instead of guessing: (1) High clears the trigger from *prior* bars' already-confirmed low alone — certain regardless of this bar's own order; (2) after folding in this bar's own Low, the bar's **Close** itself clears the new trigger — certain, since Close is always chronologically last in the bar. Only when neither holds (a wick touches the trigger but Close pulls back before the bar ends, with no later bar ever confirming it) does it fall back to an Open/Close-direction heuristic — for SOXL that was just 1 of 61 entries (1.6%), so this is a solvable problem, not a fundamental blind spot.
  Result on SOXL (57-trade backtest, `TrailingBothZScoreBreakout`, current live watch_list params): 51/57 comparable trade entries shifted price/timing, and final compounded equity on $50k dropped from **$3.55M (7007% return) under the current optimistic kernel to $1.85M (3591%) under the certain-tiered logic** — a large, one-directional overstatement, not noise.
  **Action needed**: port the certain-tiered fill logic from `simulate_trail_both_ohlc_aware` back into the real numba kernels (`_simulate_trail_buy`, `_simulate_trail_both`) and rerun the full sweep for every `TrailingBuyZScoreBreakout`/`TrailingBothZScoreBreakout` node — this is the strategy family behind **all 11 tickers on the live watchlist (watchlist_id=9)**, so every live alpha/return number currently on file is inflated by an unquantified amount until this is redone. Related to (but a more precise, actionable version of) the existing "Live/backtest parity gap" P0 #3 note in `docs/backlog_cache.md` about the trailing-buy state machine having no live implementation to verify against — this finding is about the *backtest's own* fill assumption being optimistic, independent of live-parity. Not started.

- **✅ Look-ahead bias in every backtest's entry signal — fixed and tested**: Discovered 2026-07-03 — daily SMA/std indicators included the current day's own close, so intraday entry signals were scored against future information. Fixed in `backtester.py:24` (`prep_inputs`) to look up the *previous* day's indicator row, matching `active_signals.compute_buy_signal`'s live `today` cutoff. Verified via `scripts/verify_live_parity.py` (clean MATCH). Corrected data lives under the **v2.x** version namespace (v2.4-2.10 = same strategy mapping as v1.x); v1.x data left untouched for before/after comparison. Full details/quantified impact in git history if ever needed again.

- **Review P0 live-trading fixes one at a time**: 2026-07-03 follow-up session implemented fixes for code_review_findings.md P0 #1 (TIME exit wall-clock bug), #2 (fixed_sl/trail_pct round-trip), #4 (signal-window exact-minute bug), #5 (sell_alerted never cleared), #6 (app.py config corruption) — self-verified only (unit-level smoke tests + one real backfill), not yet walked through by the user. See "Fixed in follow-up session (2026-07-03, continued)" at the bottom of `docs/code_review_findings.md` for what changed and why. `active_signals.py` needs a restart once reviewed/accepted (live process won't pick up changes otherwise).

- **Manually test fixed_sl/trail_pct round-trip before promoting Sweep 3 (v3.18/v3.21-27) live**: P0 #2 fix (2026-07-03) adds `fixed_sl`/`trail_pct` columns and wires them through `add_node` → Slack BUY-button JSON → `open_position` → `check_sell_condition`, but this is currently **untested against a real trailing-strategy live position** — the current watchlist (AGQ/EDC/FAS/HIBL) is all v1.5, which doesn't exercise this code path at all. Superseded from v1.8/v1.9/v1.10 by v3.18 (`TrailingExitZScoreBreakout`)/v3.21-27 (`TrailingBothZScoreBreakout`) — needs thorough manual verification (add a node, confirm `watch_list`/`open_positions` get correct `fixed_sl`/`trail_pct` values, click through the actual Slack BUY button, verify `check_sell_condition` reads the right stop-loss) before trusting it with real money. **Planned as the focus of the next full session** — live-trading the watchlist end to end.

- **Dispatch overhead optimization**: Profiled 2026-07-03 (see `docs/dispatch_telemetry_results.md`) — the profiling script never actually measured DB-insert time; "88% result collection" is mostly just parallel kernel compute (~90% parallel efficiency), not IPC/pickling overhead. Batched the `backtest_cache` INSERT into `executemany()` (chunk size later bumped to 5000, running fine in production). No pre-change baseline was captured, so a before/after speed comparison isn't possible at this point — not worth chasing further unless a new perf question comes up.

- **Checkpoint 1 ticker-scoping bug — silently drops top candidates**: ✅ Fixed 2026-07-02. `identify_island_candidates()` (`run_optimization_sweep.py:350-360`) queried the entire historical `backtest_cache` for a version/strategy with no ticker filter, but `bh_cache` (gates Phase 2/3) is only built for the *current* run's `target_tickers`. Any run with a narrower ticker list than what's already cached from a prior broader run silently dropped legitimate top candidates at "No B&H data, skipping Phase 2" — no error, no warning surfaced anywhere except a log line. Confirmed it had happened across v1.6 (52x), v1.7 (27x), and v1.8 (3x) historically — including `MULL`, `VRTL`, `WULX`, `NBIZ`, `SMST`, the v1.6 top-5 alpha performers at the time. Fix: added `allowed_tickers` param to scope the Checkpoint 1 query to `ticker IN current tickers list`.

- **v1.9/v1.10 trailing buy strategies**: ✅ Built 2026-07-03. `TrailingBuyZScoreBreakout` (v1.9): after z-score signal, tracks running low and enters when price bounces `trail_buy_pct`% above it; fixed TP/SL exit. `TrailingBothZScoreBreakout` (v1.10): same trailing entry + trailing exit once TP activated (trail_pct=3% hardcoded). `sl` sweep axis → `trail_buy_pct` for both. Smoke test: AGQ/SOXL v1.10 beats v1.9; v1.8 beats v1.10 on AGQ but v1.10 beats v1.8 on SOXL — suggests ticker-dependent optimal. Sweep queued overnight.

- **Limit-order model variants (v1.7-2, v1.7-3) — ✅ abandoned 2026-07-05**: see Watchlist Repick section — giving up on limit-based order variants, not giving better signals.

- **Session cache two-file design**: Work account uses two files — one for session close handover, one (conversation_cache?) for top-10 session history loaded into conversation context. Need to check work account to reconstruct the design and port it here.

- **v1.6 coarse grid sweep**: ✅ Done. Step-3 [3,6,...,30] coarse + 3-island ±4 fine mesh + full mesh for cliff-safe top-10. Three-phase sweep engine built (`run_optimization_sweep.py`). v1.6 completed: 358 tickers coarse, 30 island mesh, 1 full mesh (WULX — only cliff-safe index/other candidate). SMST full mesh running separately.

- **Phase 2.5 bug — only sweeps best node's (w,z)**: Phase 2.5 runs a ±CLIFF_RADIUS TP/SL ±7h hold sweep around the true best node, but only for that node's (w, z) combo. Should sweep all 3 island centers across all (w, z) combos so cliff check has complete neighborhood data for every candidate.

- **Sweep run registry**: Add `sweep_runs` table to DB — one row per sweep execution with `run_id`, `version`, `timestamp`, `config_json` snapshot, `notes`, `phase_reached`. Lets you record why each version was run and reconstruct config if needed. Wire into sweep engine to auto-insert on start/finish. Concrete case for why this matters (2026-07-02): discovered v1.6 only partially copied v1.5.1's EDC/FAS data (AGQ fully copied — 72k/72k rows match exactly; EDC/FAS only 8k/72k copied) with no record anywhere of why, or that a copy even happened — took manual SQL archaeology to reconstruct. A run registry would have made this a one-row lookup instead.

- **Cliff check improvements**: Current `CLIFF_RADIUS=2`, `AND trades > 0` excludes NO_TRADES nodes. Consider: (1) include NO_TRADES as alpha=0 so cliff detection catches edges where signal disappears; (2) widen radius to 3 for coarse-only data where ±2 may miss real neighbors. v1.5 cliff check: 25/340 tickers safe — VRTL, WULX, CIFG, GEVX, CRDU are top safe candidates.

- **v1.7 limit order entry model**: ✅ Built. `LimitOrderZScoreBreakout` — fill on `Low <= lower_band` intrabar at `lower_band` price; intrabar stop loss checks `Low <= stop_price`; TP checks `Close >= tp_price` at bar close. New `_simulate_limit` Numba kernel + `run_backtest_v17`. Grid: w=[10,20], z=[1.0,1.5,2.0], TP/SL=[3,6,...,30], Hold=[7,14,...,140].

- **v1.8 trailing exit**: ✅ Sweep wired. `TrailingExitZScoreBreakout` — close-based entry (v1.5 style), trailing stop once TP% cleared. `trail_pct` replaces `stop_losses` in sweep grid ([2–10%]), `fixed_stop_loss=15` in config execution. `run_backtest_v18` dispatched in sweep engine. Config set to v1.8, ready to run. Pending: run sweep, review results.

- **v2.7 (`LimitOrderZScoreBreakout`) structurally underperforms v2.5/2.6 and v2.8/2.9/2.10 — root-caused 2026-07-04, not a bug**: Post-sweep review (`docs/post_sweep_report.md`) showed v2.7 averaging **negative** alpha across the full backfill (-61.5 avg vs +9 to +34 for other versions) and losing on every per-ticker best-node comparison checked (AGQ/EDC/FAS/HIBL/TQQQ/SOXL). Two structural causes, not overfitting or a data issue:
  - **Looser entry trigger**: `check_signal` requires only `Low <= band` (`strategies.py`, any wick counts) vs. `ZScoreBreakout`'s `Close <= band` (a confirmed close through the threshold). Many v2.7 entries are noise wicks that immediately revert — reflected in some sweep nodes' very low win rates (3–16%) despite high total return (a few outlier winners carrying the average).
  - **Capped fill price**: entry always fills at exactly `lower_band` (`backtester.py` `_simulate_limit`), so on days where price genuinely gaps/crashes well past the band, v2.7 gives up the deeper, more-extreme entry that `ZScoreBreakout` would have gotten from its Close-based fill.
  - Combined with the **fixed TP/SL exit**, v2.7 also caps its rare winners at a fixed % — on assets with fat-tailed moves (many backtest trades run 500–2000%+), this is the dominant driver of v2.7 losing to v2.8/2.9/2.10 (trailing exit / bounce-confirmation entry).
  - **Considered and rejected as a fix**: adding an entry "buffer" (`Low <= band × (1 - buffer%)`) — this is mathematically equivalent to raising `z_score_threshold`, a knob the sweep already searches (z=1.0/1.5/2.0); it doesn't add a new signal-quality filter, just requires a deeper (still potentially noisy) wick. A real noise filter needs something orthogonal to price depth (time/persistence confirmation, or a regime/trend filter) — but persistence confirmation is just `TrailingBuyZScoreBreakout`'s bounce-wait mechanism restated, and a regime filter is `TrendFilteredZScore` — i.e. there's no clean fix that doesn't collapse into an existing strategy variant. Concluded the entry noise is a real, unfixable-in-isolation cost of the touch-based entry model.

- **`verify_live_parity.py` reported a false MISMATCH for `LimitOrderZScoreBreakout` — fixed 2026-07-04, harness bug not a real live/backtest gap**: `replay()` checked the entry signal against bar Close instead of Low, unlike the kernel (`_simulate_limit`, which correctly uses Low). This made it look like live trading couldn't achieve the backtest's entries. Turned out backwards: production `active_signals.py`'s `notify_limit_fill` intrabar-fill-detection loop polls **all day**, every `POLL_SECS` (300s), unconditional on the `target_hours`/buy-window gate — so live genuinely does monitor continuously for limit-entry nodes, closely matching (in fact exceeding) the kernel's two-signal-window Low check. Fixed by making `replay()` use bar Low (not Close) as the price fed to `compute_buy_signal` for `LimitOrderZScoreBreakout` entries. Verified: TQQQ and HIBL nodes that previously reported MISMATCH now report clean MATCH.
  - **Bug found and fixed along the way**: `verify_live_parity.py`'s `replay()` mislabeled every trailing-stop-triggered exit as TWIN/TLOSS instead of WIN/LOSS, because `active_signals.check_sell_condition` (`active_signals.py:623-624`) collapses the strategy's `WIN`/`LOSS` reason into a generic `'TRAIL'` string for Slack messaging, and `replay()`'s label reconstruction didn't recognize `'TRAIL'` as a WIN/LOSS case. Fixed by treating `'TRAIL'` as WIN/LOSS-by-return-sign, distinct from `'TIME'` (which stays TWIN/TLOSS). Verified: AGQ v1.8 case that previously showed the labeling quirk now reports clean MATCH.

- **v1.9/v1.10 live execution — low priority, not sure we care**: since v1.x data predates the look-ahead bias fix, v1.9/v1.10 aren't trustworthy to trade as-is (would need a v2.9/v2.10-equivalent bias-corrected backfill first, which is already queued). Live-execution wiring itself is design-solved (Schwab trailing-stop-buy order, see `code_review_findings.md` finding #3 / `operational_limits.md`) — no state machine needed, just convert the staged order to a trailing-stop-buy at bar close. If `_STRATEGY_LABELS` entries for `TrailingBuyZScoreBreakout`/`TrailingBothZScoreBreakout` are a quick add, do it; otherwise don't prioritize.

- **Trade log UI**: DB table and schema exist (`trade_log` in `trading_universe.db`). Pending: Socket Mode modal to record entry/exit from Slack interactions.

- **Screener → sweep**: Re-export leveraged ETF screener with Underlying Index + Total Assets columns, re-import, then use Screener page to select candidates and add to config.json for sweep. Current import (Results 7.csv) is missing those columns so underlier classification is incomplete.

## Visualization Pages (Streamlit)

- **Island view (Portfolio page)**: Click a watchlist node → show its neighborhood (±2 TP/SL, ±1 day hold — ~50 nodes total) with the selected node highlighted in the center. Visual version of the existing cliff/island safety check.

- **Open Positions page**: ✅ Built (`pages/10_Open_Positions.py`) — entry/signal price, drift %, current price, P&L %, TP/SL prices, hours held/left.

- **Universe Scan page** (`pages/11_Universe_Scan.py`): ✅ Built — coarse alpha ranking, liquidity (max notional), underlier type, TOP_IDX/TOP_STK/LOW_LIQ/REFINE flags, neighborhood safety score. Pending: (1) switch safety score to worst-neighbor min (currently count of positive neighbors); (2) color-code green/yellow/red; (3) fine mesh trigger button for top-25 only.

- **Two-phase UX rethink**: The current pages reflect two distinct workflows that aren't made explicit: (1) **Discovery** — sweep → Winners → find candidate tickers/nodes; (2) **Optimization** — Spatial Topology + Node Inspector → refine a candidate into a tradeable config. Consider shared "active ticker" context across Topology and Node Inspector, or restructuring so the two optimization pages feel like sub-views of a single ticker analysis flow.

- **Trade chart page**: ✅ Built as Node Inspector (`pages/2_Node_Inspector.py`) — price + bands at z=2.0/2.5/3.0, trade markers, optional Hurst/ADF (opt-in).

- **Topology page — collapsible controls**: Pickers and dropdowns consume too much vertical space. Add collapse/expand toggle to maximize chart real estate. — Medium

- **Topology page — node selection rework**: Bottom section for picking and researching nodes is hard to use. Needs rework — easier node selection, clearer node details, path to launch Node Inspector from selected node. — Medium

- **SPY trend / VIX level as entry filter**: Next research direction after ruling out Hurst/ADF. Hypothesis: avoid entries when SPY is in a downtrend (price < 200d SMA) or VIX is elevated (VIX > 25). Macro regime signals rather than ticker-level — may address the lag problem that killed Hurst/ADF.

- **Is the look-ahead bias itself an accidental (contrarian) selection signal? Worth a deliberate, non-cheating test.** Raised 2026-07-03 alongside the look-ahead bias finding above. Mechanism, traced precisely: including day D's own close in the rolling window both pulls the SMA down *and* inflates the STD (a large move that day increases that day's contribution to variance). Since `lower_band = SMA - z*STD`, both effects push the band *further down* — i.e. the bias makes entry *harder*, not easier, specifically on days when the move is already large. This matches the measured direction: the biased kernel produced *fewer* trades than the corrected replay (AGQ: 18 vs 31), not more. So the bias isn't just noise — it behaves like an implicit filter: "was today's drop large relative to the volatility that same drop just created?" That's a real (if backwards-arrived-at) selection criterion, and it's an open question whether it survives being reformulated honestly — e.g. gating entry on same-day realized intraday range/volatility (which *is* knowable in real time, unlike tomorrow's close-to-close std) instead of cheating with future daily closes. Mean-reversion caveat (the user's point): if the ticker is genuinely mean-reverting, next-day sigma should come back down after the spike, so a filter built on "elevated same-day vol" might just be selecting for noise-driven overshoots rather than a durable edge — not guaranteed to hold, worth testing rather than assuming either way. Contrarian in the sense that "fix the bug and the strategy gets worse" would be a surprising result — but the trade-count/alpha data so far (bias removed, alpha still positive for all 4 watchlist tickers) suggests the effect isn't load-bearing, just inflating magnitude. Test idea: build a same-day realized-vol-gated variant (no future information) and compare its alpha/trade-count against both the biased kernel and the corrected replay.

## Medium Priority

- **Long-running process consistency check**: Currently manually restarting `active_signals.py` periodically for confidence that it hasn't drifted into a bad state. Goal: stop needing manual restarts. Two options discussed: (1) run a second process that restarts fresh every morning alongside the long-running one, diff their signals to build confidence the long-running one hasn't drifted; (2) make the long-running process itself controllable via the Slack socket (already Socket Mode) so it can take a "revalidate/restart" command without killing the OS process. Not urgent — manual restart is fine for now — but risk is forgetting to restart it one day, so don't let this drop.

- **Half-day trading sessions not handled**: `_SIGNAL_WINDOWS` (10:25-10:40, 15:25-15:40 ET) assumes a full session. On early-close days (day before Thanksgiving, Christmas Eve, etc.) the market closes ~1pm, so the 15:25 window never happens — a TIME exit that would've fired then just doesn't trigger until the next real session. Low priority, but a real gap in `_in_buy_window`/exit-check timing.

- **Parameter selection workflow**: After enough sweeps, need a way to review results across tickers and select parameter sets to trade — currently manual via logs/heatmaps.
- **`trading_engine.py` cleanup**: Either retrofit or replace with Layer 3 implementation; currently points at legacy files.

## Low Priority / Ideas

- **Dropped index `idx_bc_version_ticker_z_return`** (2026-07-03): `backtest_cache(version, ticker, z_score_threshold, strategy_return DESC)`, removed from `init_idempotent_db` (`run_optimization_sweep.py`) — checked every page query against `backtest_cache` via `EXPLAIN QUERY PLAN` and found none matching this shape (real point lookups hit the PK directly; nearby queries partition/order by `alpha_vs_spy`, not `strategy_return`). Was pure insert-time overhead, especially costly in Phase 3's full-mesh insert volume. If a future query needs it, re-add the `CREATE INDEX` line — the two other extra indexes (`idx_bc_ticker`, `idx_bc_version_return`) were verified in-use and kept.

- **Node version-change reminder in Slack alerts**: When a live watchlist node's version/params change day-over-day (e.g. AGQ v1.5→v1.6 swap on 2026-07-01), flag it explicitly in the alert instead of relying on the version tag alone. Low priority now — version tag already shown, gap was just not connecting it in the moment. Revisit if watchlist grows large enough that version swaps become frequent/hard to track manually.

- **Alternative trading windows**: Explore hourly bar closes beyond 9:30 and 14:30 (e.g. 11:30, 12:30, 1:30) — requires expanding `target_hours` and re-sweeping.
- **Chaos monkey / floor alpha**: Re-run backtest with worst-case execution — entry at highest price in N bars after signal, exit at lowest price in N bars after exit signal. Produces `floor_alpha` metric. Nodes with positive floor alpha are robust to real-world execution delays. Store alongside `alpha_vs_spy` in DB and surface in Winners page.
- **Alpha robustness — drop top N trades**: Re-run backtest dropping the top 3 best-performing trades and recalculate alpha. Tests whether alpha is structural or lucky.
- **Automated exits**: Exits (TP/SL/TIME) are deterministic and the primary candidate for brokerage API automation. Manual exit of multiple simultaneous positions is operationally risky. Entries stay manual.
- **Broader ticker universe**: `results.csv` (999 rows, mixed leveraged/non-leveraged) has liquidity data for non-leveraged ETFs. Import and sweep to increase signal frequency.
- **Half-Life of Reversion**: Fit an Ornstein-Uhlenbeck process per ticker to estimate mean-reversion speed. Use to inform `max_hold_hours` sweep range per ticker. One offline computation per ticker, surface in Screener.
- **Hurst/ADF as entry filter**: ✅ Researched (`pages/7_Hurst_Filter.py`, `pages/8_ADF_Filter.py`). Verdict: not actionable on current dataset — see `docs/research.md`. Revisit for v1.6 if dataset changes.
- **Advanced indicators**: Dataset size allows pre-computing Bollinger Bands, ATR, MACD etc. instantly via TA-Lib or pandas-ta.
- **Basic ML experimentation**: Dataset small enough to train Random Forests or XGBoost on CPU in seconds.
- **Multi-ticker signal dashboard**: View current z-score signals across the full ticker universe in one place.
- **Position sizing model**: Layer 3 will eventually need a position sizing model.

## [live-trading][tax] Deprioritized 2026-07-18 (raised 2026-07-17) — wash-sale/tax analysis needed before promoting any ticker into the taxable brokerage account
Distinct from the resolved cross-account (taxable-loss → IRA-repurchase) wash-sale item — this strategy re-enters losing tickers within 30 days as completely normal, routine behavior (not a one-time event), so any ticker live-traded in the taxable brokerage account will generate wash sales continuously, indefinitely. User's explicit call: **fine with this** — a wash sale only defers the loss into replacement-share cost basis, doesn't destroy it, and repeatedly sitting out 30 days after every loss to avoid it would likely cost more in missed re-entries than the deferral costs (matches the general "missed signals aren't free but aren't catastrophic either" theme from the chaos-monkey findings). Also discussed: splitting the brokerage account's capital, with part parked as a static buy-and-hold SPY sleeve alongside the actively-traded portion — orthogonal, low-risk, no design work needed.
**LABU floated as a candidate** for the first ticker promoted into the brokerage account, not decided.
**Year-end wrinkle flagged by user, not yet verified**: a wash sale is "just a deferral" in general, but if the 30-day window straddles the calendar year boundary (loss sold in December, replacement bought in the following January), the disallowed loss can't be claimed on that tax year's return at all — it rolls into the replacement shares' basis and isn't usable until eventually sold clean, effectively pushing the deduction a full tax year later rather than just 30 days. Relevant to year-end tax planning (offsetting current-year gains with current-year losses) specifically. Recalled from memory, not independently verified against current IRS rules yet — worth confirming precisely (which side of Dec 31 triggers it, exact mechanics) as part of the real analysis below rather than trusting an offhand recollection.
**Deprioritized 2026-07-18**: pushed behind the train/test split and v4-promotion work — no taxable-account promotion is imminent, so the tax analysis isn't blocking anything active right now. Revisit when a taxable-account promotion actually becomes near-term.
**Action needed**: user wants a real wash-sale/tax analysis done before promoting anything into the brokerage account (not scoped yet — e.g. quantifying deferred-vs-recognized loss timing impact across a tax year, and specifically verifying the year-end-straddle mechanic above). Not started.

## [live-trading][security] Phase 4 (deferred to cloud-infrastructure planning), 2026-07-18 — move order-placement/mutating Schwab calls behind a separate proxy this session can't write to
Raised by the user: their work architecture uses an API-proxy pattern (a separate service/codebase that holds real credentials and does the actual outbound call, so the calling app only ever sends intent) — nothing Schwab-specific exists yet, would need to be rebuilt from scratch in a separate folder/codebase here. Goal is two-fold, roughly equal weight: (1) safety boundary — a bug or bad edit made in *this* repo/session can never itself place/cancel/modify a real order, since the code with real trading authority would live somewhere this session doesn't write to; (2) credential isolation — the Schwab OAuth token/secrets would live only with the proxy, not on this machine/repo at all.
**Scope discussed**: only the write/mutating calls move (`place_trailing_buy`, `place_trailing_sell`, and any future cancel/modify-order calls) — read-only calls (`get_account`, `get_filled_order`, quotes) stay local in `schwab_client.py` since they can't themselves cause a trade. This server would call out to the proxy for anything that changes broker state; the proxy is the only thing holding a live, order-capable Schwab session.
**Not scoped at all yet**: what the proxy-side code actually looks like (that part is deliberately meant to live outside what an agent session writes), the request/response contract between this repo and the proxy, how the proxy would authenticate/hold the Schwab token, and how `schwab_safety.py`'s existing guardrails (allowlist, dry_run, kill switch, signal-window gate, duplicate-order window) would split across the local/proxy boundary — plausibly some checks stay local (fast pre-flight rejection) and some get re-validated proxy-side (defense in depth) but not decided.
**Deferred 2026-07-18**: user tagged this "phase 4" — deliberately pushed out until cloud infrastructure is actually being considered (the proxy is naturally a separately-hosted service, so the two decisions are linked). Not a priority for the current train/test-split + v4-promotion phase.
**Action needed**: design session, not started. Logged as backlog only per explicit 2026-07-17 instruction — no code changes, no client stubs, nothing wired.

## [live-trading][security] Phase 4 (deferred to cloud-infrastructure planning), 2026-07-18 — one brokerage account per live ticker, for blast-radius containment against a rogue algorithm
User plans to split into **one brokerage account per ticker** on the live watchlist. Originally raised as a PDT-avoidance idea (see the now-resolved same-day-block backlog item — that rationale turned out moot since the PDT rule itself was eliminated 2026-06-04). **Restated with a real, independent rationale**: capping how much capital a bug or malfunctioning algorithm on one ticker can ever touch. With separate accounts, a rogue order on ticker A structurally cannot reach ticker B's capital — no code enforcement needed, the account boundary itself is the guarantee.
**Directly relevant to an existing known gap**: the 2026-07-16 GDXD-promotion finding that Schwab does not reserve/check buying power against a resting order at placement time, combined with the 2026-07-17 finding that the new FINRA intraday-margin framework (replacing the old PDT rules, effective 2026-06-04) is a deficit-and-cure model, not a hard fill-time block — i.e. there's real reason to believe multiple simultaneously-resting orders across tickers *could* collectively commit more than available capital before anything stops them. That gap was previously flagged as needing an "aggregate-across-tickers cash exposure" guard (unbuilt). **One-account-per-ticker makes that guard structurally unnecessary** rather than something to build — each account's own balance is a hard ceiling on that ticker's exposure by construction.
**Complements, doesn't replace, the API-proxy idea above**: the proxy limits *what code can act* (safety boundary + credential isolation); account-splitting limits *how much money any single ticker's logic can ever reach* (blast-radius containment). Different layers of the same defense-in-depth goal.
**Real operational cost is small, confirmed by reading the code (2026-07-17)**: these are all IRA accounts, so there's no per-trade taxable event or capital-gains paperwork to multiply (just N annual 5498/1099-R-type forms, minor). And `schwab_client.py:29-59` already uses a single `schwab_auth.get_client()` OAuth session for the whole Schwab login, resolving every nickname (`brokerage`/`sep`/`roth`/`ira` today) to an account hash via one `get_account_numbers()` call plus a `SCHWAB_ACCOUNT_<NAME>` env-var suffix match — since all these accounts are linked under one login, scaling `NICKNAMES` from 4 to a dozen-plus needs zero new logins/tokens, just more list entries and env vars, plus updating whatever mapping decides which nickname a given ticker's trades route through.
**Deferred 2026-07-18, un-deferred same day**: briefly tagged "phase 4" alongside the API-proxy item, but the user reprioritized it as the active focus later the same session (after resolving the walk-forward negative-fold follow-up by sending DPST/NUGT/RETL/UDOW/UVIX to research instead of investigating further) — account-per-ticker setup is now the next real thread of work, not deferred infra planning. API-proxy item stays deferred on its own.
**Action needed**: design session on account-splitting scope (how many accounts, which nicknames, `NICKNAMES`/`SCHWAB_ACCOUNT_<NAME>` mapping, which tickers get their own account first). Not started. No code changes yet.

## ✅ [live-trading] Resolved 2026-07-18 — GDXD paper-trading layer built
`dry_run=True` (the state of every real Schwab account today) only posts a Slack "[DRY RUN]
would place..." message and produces zero simulated fill/P&L — no way to see whether the
automation engine (`schwab_client`/`schwab_safety`, wired 2026-07-17) actually catches signals
reliably before any ticker is flipped back to real execution. Built `paper_trading.py`
(new module) to close that gap for `schwab_safety.AUTOMATION_ENABLED_TICKERS` tickers
(currently `{"GDXD"}`) while they stay `research` mode:
- `start_paper_buy(node, sig)` — called from `active_signals._scan_buy_signals` on a BUY
  signal for a research-mode, automation-enabled ticker running a trailing-buy strategy
  (`db._is_trailing_buy(node)`). Inserts a `paper_pending_buys` row (`running_low` seeded at
  signal price); deduped against an existing paper position or pending row.
- `update_paper_buys()` — called every poll, unconditionally, same "not gated to a window"
  reasoning as `check_auto_fills` (a real trailing buy can fill any time after the signal).
  Tracks `running_low = min(running_low, current_price)`; once
  `current_price >= running_low * (1 + trail_buy_pct/100)`, sizes
  `shares = int(starting_notional // current_price)` — fully deployed at the real discovered
  fill price, the "correct" sizing approach identified in the trailing-buy capital-sizing
  backlog item (paper trading learns the true fill price directly instead of needing the
  live worst-case-conservative formula) — and opens a `paper_positions` row.
- `check_paper_sells(last_seen_bar, paper_sell_alerted, load_cache)` — mirrors the real
  `open_positions` exit-check block in `active_signals.run_loop` exactly (same `at_bar_close`
  detection, shares the `last_seen_bar` dict — safe since a ticker is never simultaneously
  `live` and `research`), calling `signals_compute.check_sell_condition(..., paper=True)`, the
  same exit state machine real positions use, writing to `paper_positions`/`paper_trade_log`
  instead. Uses its own dedup set (`paper_sell_alerted`) since paper position ids are
  independent of real `open_positions` ids.
- `signals_db.py`: new `paper_positions`/`paper_trade_log` tables, schema-identical mirrors of
  `open_positions`/`trade_log`. Existing CRUD (`get_open_positions`, `open_position`,
  `close_position`, `log_trade_entry`, `log_trade_exit`, `update_position_trail_state`) took a
  `paper=False` param rather than being duplicated (`_pos_tables(paper)` picks the table pair).
  New `paper_pending_buys` table — lighter than real `pending_buys` (no reminder machinery,
  since a paper fill is auto-detected every poll, never confirmed by a human click).
- `signals_compute.check_sell_condition` gained a `paper=False` param: threads to
  `db.update_position_trail_state(..., paper=paper)`, and skips the interactive
  "Apply Correction" corp-action Slack block when `paper=True` (that button's handler assumes
  a real `open_positions` id) — falls back to a plain freeze/print warning. Accepted gap since
  this is scoring infrastructure, not real capital.
**Deliberate deviation from the original framing** ("let `AUTOMATION_ENABLED_TICKERS` act
through the existing `_attempt_automated_buy`/`_attempt_automated_sell` path"): investigating
that path (`signals_notify.py`) found it would write real `pending_buys` rows that nothing
ever marks Filled (no human clicks the button for a research ticker, and auto-fill-detection
is opt-in/off) — those rows would sit forever and `check_buy_reminders` would nag every 15
minutes about a ticker that was never actually live. Built a fully separate simulation instead
— never calls `schwab_client`/`schwab_safety` at all, independent of `dry_run`, keeps real
live-trading state (reminders, `open_positions`, `_attempt_automated_sell`) completely
untouched by research-mode paper activity.
**Known limitation**: fills are sampled at `POLL_SECS` cadence, not tick-perfect against a
real broker's continuously-live `TRAILING_STOP` price — close enough to score signal-catching
reliability and get directionally-real fill data, not a tick-perfect replay.
`scripts/paper_trading_status.py` added (prints pending/open/closed paper state, matching
`scripts/open_positions_status.py`'s convention).
**Verified**: full `pytest tests/` suite, 92 passed (was 86 — 6 new tests in
`tests/test_paper_trading.py` covering pending-buy dedup, running-low tracking, bounce-fill
sizing, SL exit + `paper_trade_log` write, and that the real `open_positions`/`pending_buys`
tables are untouched by the paper flow). Confirmed against the real `trading_live.db`: real
`open_positions`(1)/`trade_log`(11) row counts unchanged after creating the new empty paper
tables via `scripts/paper_trading_status.py`.

## ✅ [backtest] Resolved 2026-07-18 — `signals_db.add_node`'s `fixed_sl` computation ignored the real per-node value for uses_fixed_sl strategies
`add_node` used to always compute `fixed_sl = _config_fixed_stop_loss()` (reads
`config.json`'s global `execution.fixed_stop_loss`) whenever `strategies.uses_fixed_sl(strategy)`
was true, regardless of what real per-node SL value the caller actually wanted. Found
2026-07-18 promoting 19 tickers' v4 (SL=1%) nodes: every row came out with `fixed_sl=15.0`
instead of the real `1.0`, silently wrong, no error — worked around that session via direct
SQL inserts. **Fixed**: `add_node` gained a `fixed_sl_override=None` parameter — when set,
used instead of `_config_fixed_stop_loss()`; `None` (the default) preserves the old
config-read behavior for legacy v3.x callers, so no existing call site needed to change.

## ✅ [live-trading] Resolved 2026-07-23 — canary watchlist nodes (A-F) built, sizing bug fixed, all live
Designed 2026-07-22, built 2026-07-23. `scripts/add_canary_nodes.py` adds six synthetic
`watch_list` nodes (SPY/QQQ/IWM/DIA/VOO/XLF — liquid, cached, unused by any real node) to the
active watchlist, all `mode='research'`, `version='canary'`, with deliberately extreme parameters
so each should complete its expected lifecycle every trading day:
- **A** (SPY, `TrailingBothZScoreBreakout`, `entry_timing='close'`): full happy path — ambient
  entry → bounce-fill → arm → trailing-sell, all same day. `fixed_sl=30` (unreachable),
  `arm_sell_pct`/`trail_buy_pct`/`trail_sell_pct`=0.1 (hair-trigger).
- **B** (QQQ, same shape): early-SL path — bounce-fill → immediate SL. `arm_sell_pct=10`
  (unreachable), `fixed_sl=0.1` (hair-trigger).
- **C** (IWM, same as A but `entry_timing='open_check'`): exercises `_scan_pinned_entry` +
  `open_price_quality_log` — the pinned intrabar-open scan, distinct from A's bar-close-only path.
- **D** (DIA, `trail_buy_pct=5.0`): large enough that the bounce-fill is unlikely to complete
  same day — a pending trailing-buy carried overnight into the next session's open is a direct
  daily regression check for the 2026-07-22 stale-cache fix (`_current_price`'s market-open
  staleness guard).
- **E** (VOO, `strategy='TrailingExitZScoreBreakout'`, not TrailingBoth): `signals_db.
  _is_trailing_buy` routes on the strategy's axis schema (`sl_axis` class attribute), not the
  `trail_buy_pct` value — only a real TrailingExit node reaches `paper_trading.
  start_paper_market_buy` (immediate market buy, no `pending_buys` row) instead of the
  bounce-fill path A-D exercise. A TrailingBoth node with `trail_buy_pct=0` would NOT have
  tested this (an earlier version of the plan got this wrong).
- **F** (XLF, `take_profit=50`/`fixed_sl_override=50`, `max_hold_hours=2`): arm and SL both
  practically unreachable, tiny hold cap — the only exit path left open is TIME.
**Sizing bug fixed**: `starting_notional=500` (the original A/B draft) sizes to
`int(500 // price) == 0` shares at SPY/QQQ's real price (~$700+) — both would have silently
never filled. Caught by independent Opus review before deployment; raised to `10000` for all six.
**Accepted limitation, not closed**: `_scan_pinned_exit_arm` only ever reads real (non-paper)
`open_positions` — no canary, however designed, can exercise it. Deferred to the planned
`live_sim.py` harness extension (`docs/backlog_cache.md`), which can call it directly against
synthetic positions instead.
**Verified**: all 6 present in `watch_list` (watchlist_id=65, 16 nodes total: 10 real v5 +
6 canary) via direct query. Deploying them is what surfaced the reference-report bugs — see
`docs/research_log.md`'s 2026-07-23 entry and `docs/backlog_cache.md`'s resolved entries for
that investigation.

## ✅ [live-trading] Resolved 2026-07-23 — `scripts/live_sim_harness.py` built: non-interactive coverage harness for `active_signals.py`/`signals_*.py`
Decided 2026-07-22 (see the entry directly above this one), built 2026-07-23. Six scenarios,
each calling the real orchestration function directly against an isolated sim DB
(`TRADING_DB_PATH` override, same mechanism `scripts/live_sim.py` already used) — full run takes
~2s: `scenario_pinned_entry_trailing_buy` (`_scan_pinned_entry`, the real open-check trailing-buy
entry path), `scenario_pinned_exit_arm` (`_scan_pinned_exit_arm` arming + `notify_trailing_
activated` persisting both `trailing` and `peak`, regression coverage for the 2026-07-22
stale-clobber bug), `scenario_reconcile_fill_topup` (`signals_notify._reconcile_fill` with a
deliberately forced shortfall), `scenario_gap_resize` (`signals_notify.check_gap_resize`,
asserting the actual replacement order's ticker/shares/price via a `wraps=` spy on
`place_equity_buy`, not just the Slack text), `scenario_time_exit` (`check_sell_condition`'s TIME
trigger via bars-held, not wall-clock hours), and `scenario_ambient_market_buy_entry` (the second
real entry path — an automated market buy via `_scan_buy_signals` → `notify_buy_signal` →
`_attempt_automated_market_buy` → synchronous fast-confirm → `_reconcile_buy_fill` → SL
placement). Real z-score math runs unmodified against a real synthetic CSV written to
`cache/research/` (same convention as `tests/conftest.py`'s `make_synthetic_csv`) — only the
`schwab_client` broker-network boundary is stubbed via `unittest.mock.patch.object`, since the
harness's job is verifying wiring (dedup, mode-gating, arm/re-arm state, sizing math, Slack
content), not re-verifying signal math (`backtest-change-rollout`'s job).

**Found and fixed along the way** (real bugs, not hypothetical):
1. `signals_db.get_open_position()` (singular ticker lookup) never coerced `trail_state` from
   `None` to `{}` the way `get_open_positions()`/`get_position_by_id()` already do — calling
   `check_sell_condition` against its result raised `TypeError: 'NoneType' object is not
   iterable` inside `strategies.py`'s `check_exit`. A pre-existing inconsistency (a stale comment
   in `tests/test_part4_entry_trigger.py` even documented it as expected), not previously hit
   because nothing in production called `check_sell_condition` against this function's raw
   result. Fixed to match its siblings; full suite unaffected (184 passed).
2. **Safety incident, found and remediated the same session**: `schwab_safety.py` has several
   real, hardcoded (`Path(__file__).parent / ...`) state files — order counts (`STATE_PATH`),
   kill switch, ticker-automation pause, auto-fill-detection toggle, automation-scope — none
   gated by `TRADING_DB_PATH` the way the DB is. An early version of this harness (before this
   was known) placed real dry-run BUY attempts that wrote straight into the real
   `cache/live/schwab_order_counts.json` across repeated debug runs, driving the real `ira`
   account's `daily_order_cap` counter to its actual limit (10/10) before it was caught (the
   harness's own scenarios started silently failing cross-contamination, which is what surfaced
   it). The file was reset (confirmed safe: its `recent_orders` contents were entirely the
   harness's own synthetic `ZHARN*` tickers, all counts are self-expiring/date-keyed, and the
   real kill switch — engaged since 2026-07-16 — meant no real order could have gone through
   regardless). **Structural fix, not just a harness workaround**: added `SCHWAB_STATE_DIR` env
   var to `schwab_safety.py` (mirrors `TRADING_DB_PATH` exactly — `_STATE_DIR` computed once at
   import time, all five real paths derive from it), so the harness now sets
   `SCHWAB_STATE_DIR` to a fresh `tempfile.mkdtemp()` before importing any project module,
   isolating all five files at once (kill-switch-engaged reads as `False` for a nonexistent
   file, no separate stub needed). This is the durable fix — any future test/sim script gets the
   same isolation automatically, not just this one harness.
3. Independent Opus review (requested mid-build) verified the `SCHWAB_STATE_DIR` remediation was
   complete (no other real file/OAuth-token path reachable given `dry_run=True` short-circuits
   before any real client call) and flagged two of the six scenarios' assertions as weaker than
   the regression they claimed to guard — both tightened: `scenario_pinned_exit_arm` now checks
   `peak` survived alongside `trailing` (the original clobber bug dropped both), and
   `scenario_gap_resize` now spies on the real `place_equity_buy` call args instead of asserting
   only the Slack message text.

Full suite: 184 passed (was 181; +3 from the stale-price-exit-alert item below, +0 net from this
item's `get_open_position` fix). Not yet adopted as a required step in any workflow (e.g.
`feature wrap`/`session wrap`) or documented in `docs/automation_principles.md` — still just a
tool that exists, per the original 2026-07-22 decision's "document as standing convention" being
left undone.

## ✅ [live-trading] Resolved 2026-07-23 — Slack alert for the stale-price-guard silent-suppression gap (2026-07-22 HIBL incident's residual finding)
The one gap the 2026-07-22 independent Opus review flagged as not-yet-fixed: when
`signals_compute._current_price()` returns `(None, None)` (its market-open staleness guard, or a
genuine same-day data-refresh failure), `active_signals._check_position_exit`'s mid-bar branch
silently `return`s — no SL/trailing-stop/TIME check runs against that real position for that
poll, previously with only a `log_poll` trace line, no Slack alert. Fixed:
`signals_notify.alert_stale_price_exit_suppressed(pos)`, rate-limited 15min per position id (same
cooldown pattern as `_alert_reconcile_mismatch`/`_guarded`'s per-section cooldown), wired into
`_check_position_exit` at the `cp is None` branch. 3 new tests
(`tests/test_stale_price_exit_alert.py`): fires with ticker/account/function name in the message,
rate-limited on a second call, and keyed per-position (not shared across positions). Full suite:
184 passed (was 181).

## ✅ [live-trading] Resolved 2026-07-23 — coverage_events fully wired; stale "~13 remaining" note corrected; `daemon_section_exception` added
Originally built 2026-07-22 (`signals_db.coverage_events` table + `log_coverage_event()`/
`get_coverage_events()`, `scripts/coverage_matrix.py` pivot view — see that session's entry
above) with 7 real control sites wired. A follow-up backlog note at the time listed ~13
scenarios as "not yet wired": SL placement (sync/async), top-up/`_reconcile_fill`, trailing-arm
re-read in `notify_trailing_activated`, `drain_fill_queue` fast-path, daemon-survives-exception,
open-price-quality, and (flagged as possibly-already-covered) second-live-ticker-BUY-blocked.
**2026-07-23 audit**: grepped every real `log_coverage_event(` call site in the codebase and
found the note was stale — `sl_placement` (+ `sl_placement_fast_confirm_timeout`), `top_up`,
`trailing_arm_state_reread`, `gap_resize`, `fast_path_fill_reconciliation`,
`reconciliation_mismatch`/`reconciliation_fetch_failed`, `cash_check`, `same_day_block`, and all
5 dup-order guards were already wired (in `signals_notify.py`/`schwab_safety.py`), evidently
done in a later session without this backlog entry ever being updated to reflect it.
`open_price_quality` was never meant to live in `coverage_events` in the first place — it already
has its own dedicated `open_price_quality_log` table (Part 4 Deliverable 2), a separate design
choice, not a gap.
**The one real gap found**: `active_signals._guarded()` (wraps every `run_loop` section in
try/except so one section's exception can't crash the daemon, automation_principles.md #3/#4)
caught and alerted on exceptions but never logged whether the daemon actually survived one — no
queryable record of which sections fail, how often, or when. Fixed: new `daemon_section_exception`
scenario key logged inside `_guarded`'s except block, `mode` via `signals_notify._coverage_mode(None)`
(safe fallback to `"dry_run"` since `_guarded` wraps whole daemon-loop sections, not one
account), `result=<section name>`, `detail=<exception text>`. Logs on every caught exception
(not cooldown-gated like the paired Slack alert) — an independent Opus review confirmed this is
an accepted non-issue given the table's diagnostic (count + most-recent) purpose, not a
correctness bug. New test `test_guarded_logs_coverage_event_on_failure`
(`tests/test_run_loop_fault_tolerance.py`), using the same `signals_config.DB_PATH` monkeypatch
isolation pattern as `test_db_roundtrip.py`. Full suite: 185 passed (was 184).
Prompted by discussing whether `coverage_events` captures enough state to support the kind of
manual cross-check the user did with the HIBL trailing-buy CSV a week prior — concluded
`detail` is free-text (real numbers like `shares=`/`price=`/`required=$`/`available=$` are
present, but not structured columns, and expected-vs-actual isn't stored side by side), so it
supports spot-checking a row but not query-level aggregation or automated verification yet.
Structured `detail` (human-readable short text + JSON, per the user's stated preference) was
discussed and deliberately deferred, not built this session.
`coverage_events` is now considered fully wired against every real control site identified so
far — treat as closed, not a standing backlog item; revisit only if a new control site is added.

## [live-trading] Resolved 2026-07-23 — Schwab OAuth `interactive=True` default hung the daemon (main client + account-activity stream), both fixed
Found live: the daemon printed schwab-py's "Press ENTER to open the browser" banner at startup
(stale/expired 7-day OAuth token), then a second real stall — heartbeat frozen for 2+ minutes,
two processes parked in `futex_do_wait` — traced to `schwab_client.py::_get_client()` and
`schwab_stream.py::_run_stream_once()` (the account-activity fast-path stream thread, started
unconditionally at daemon startup) both calling `schwab_auth.get_client()` with the default
`interactive=True`, contra `schwab_auth.py`'s own documented intent that unattended contexts
should use `interactive=False` so a stale token surfaces as an error, not a blocking prompt.
Fixed: both call sites now pass `interactive=False` explicitly (`_get_client(interactive: bool =
False)`, default flips; all 13 existing no-arg callers unaffected).
**Real nuance found empirically, not just from docs**: schwab-py's `easy_client()` does NOT skip
the login-flow attempt when `interactive=False` and the cached token is missing/stale — it still
calls `client_from_login_flow(..., interactive=interactive)`, which still calls `webbrowser.get()`
and raises a real exception (`could not locate runnable browser`, confirmed in this headless WSL
session) rather than blocking on `input()`. So the fix converts a silent hang into a clean raised
exception — it does not prevent the attempt itself, and `schwab_stream.run_stream_forever`'s
existing capped-backoff reconnect loop (5-60s, never gives up) will keep retrying-and-failing
indefinitely on a stale token.
**Second real gap found by an Opus review of this exact diff**: that reconnect loop's exception
handler `_post_message`s a real Slack alert to the live trading channel on every retry, not just
console/log — a persistently stale token would alert the real channel every ≤60s indefinitely,
risking burial of real trade alerts and Slack rate-limiting. Fixed: new `_ALERT_COOLDOWN_SECS =
900` (15 min, matches `active_signals._SECTION_ALERT_COOLDOWN_SECS`'s existing convention) gates
the Slack post specifically; console/log output stays unthrottled (cheap, local only). Considered
and declined making the alert itself back off exponentially (mirroring the connection retry's own
backoff) — user's call: connection-retry backoff bounds server load, alert-cooldown bounds human
attention, no reason to conflate the two into one mechanism; flat cooldown matches the one
existing precedent in this codebase.
2 existing tests updated for the new `interactive` kwarg / cooldown-gated alert count
(`test_schwab_client.py::test_get_client_applies_short_timeout`,
`test_schwab_stream.py::test_reconnect_backoff_increases_and_caps`). Full suite: 185 passed.
**Not yet fixed** (real, still open): nothing actually prevents the stream thread from retrying
forever against a token that can never succeed without a human completing the browser login —
the fail-fast fix only converts hang-forever into fail-and-retry-forever. A real fix would check
token file validity/freshness before ever calling `easy_client` and stop retrying (or fall back to
a single one-time warning) once it's structurally clear no reconnect can succeed headless. See
`docs/backlog_cache.md`.

## [live-trading][security] Live-fire dry-run test state left in place 2026-07-23 — needs manual revert
Session included a deliberate real-daemon dry-run exercise (not a harness/sim): kill switch
disengaged, all 16 `watchlist_id=65` nodes (10 real v5 + 6 canary) flipped to `mode='live'`,
UDOW additionally given `account='ira'` and a fake open position seeded directly into the real
`open_positions` table (740 sh @ $67.55, no real order behind it) so a SELL trigger had something
to fire against. `dry_run=True` on all 4 accounts throughout — confirmed (via Opus review
precedent + direct code read of `schwab_safety.check_order`'s ordering) that no real order could
reach Schwab regardless of kill-switch/mode state. Daemon was started/stopped several times during
the session and is **currently not running**. None of this was reverted before session end.
**Action needed next session**: revert all 16 nodes back to `mode='research'`, re-engage the kill
switch, delete UDOW's fake `open_positions` row (and any `trade_log`/`coverage_events` rows it
generated), and drop the `account='ira'` assignment. Real Schwab OAuth login (browser-based) also
still not done — needed before the cash-balance check can pass for any real BUY-side dry-run test.

## ✅ Resolved 2026-07-24 evening — seven live-test-day bugs fixed as one batch, all "compass"-adjacent (coverage/reliability of the automated flow), plus 3 new standing architectural conventions
Follow-on session to the 2026-07-24 real soxl_ira live test day (see the coverage-system entry
above and `docs/research_log.md`) — user explicitly asked to fix "almost everything" found that
day except the dry_run-completion design question (deliberately deferred, see
`docs/backlog_cache.md`'s open entry on that). All seven below were found live earlier the same
day; this entry is where they were actually fixed. Full suite 207 passed (was 195), plus
`scripts/live_sim_harness.py` 6/6, plus an independent Opus review of the real diff (which caught
two real regressions in this same batch, both fixed before commit — see the last two items).

1. **`add_node`'s NULL-unsafe dedup** (`signals_db.py`): `INSERT OR IGNORE` relied on the
   `watch_list` UNIQUE constraint, which SQLite never matches on `NULL == NULL` —
   `TrailingBothZScoreBreakout` nodes always store `take_profit=NULL`, so every rerun silently
   duplicated them (15 real duplicate rows found live that morning, plus a *fresh* batch of 5 more
   found still recurring that evening — the bug was still live, not just historical). Fixed with an
   explicit check-then-skip query (`COALESCE` to treat NULL as equal to NULL) instead of trusting
   the constraint alone. Cleaned up both batches of real duplicate rows via `signals_db.remove_node`
   (not raw SQL — the permission classifier correctly blocked a direct `DELETE` against the live DB;
   the app's own supported deletion path was the right tool anyway). Backed up
   `trading_live.db` first. New regression test confirms 3x `add_node` calls for the same
   TrailingBoth config yields exactly 1 row.
2. **`daily_order_cap` starving protective actions** (`schwab_safety.py`): counted SL-placement and
   post-fill top-up attempts against the same pool as new entries — confirmed live (LABU) that
   unrelated earlier entries exhausting the cap left a brand-new fill with no automated stop.
   Added `is_protective` param to `check_order`/`approve_and_record`, exempting *only* the
   `daily_order_cap` check (every other guard — cash, notional, dup-order, kill switch, burst cap —
   still applies). Wired into `place_stop_loss` (always) and `_reconcile_fill`'s top-up call.
   Opus review traced every call site and confirmed the bypass is exactly this narrow.
3. **`notional_cap` blocking automated SELLs** (`schwab_safety.py`): a real armed SPY trailing-sell
   was permanently blocked because its notional exceeded soxl_ira's $800 BUY-sizing cap — a real
   position that grows past the cap could never have its exit automated. Fixed: `notional_cap` now
   BUY-only; `HARD_ORDER_CEILING` ($100k) still applies to both sides as an absolute backstop.
4. **`TrailingBothZScoreBreakout` fills never got an automated stop-loss** (`signals_notify.py`,
   `signals_handlers.py`): `_place_stop_loss_for_position` was gated to exclude every trailing-buy
   node, both in the auto-fill-detection path (`_reconcile_buy_fill`) and — a deeper gap than the
   original backlog entry described — the manual "Filled" Slack button path
   (`handle_trail_buy_fill_price`) never called it at all, automated or not. Fixed both. Opus
   review traced both racing paths (auto-fill poll/websocket vs. human clicking Filled) and
   confirmed no double-SL-placement risk — whichever path loses the race returns early before
   reaching the SL call. Shipped with tests now (live verification still planned for Monday
   2026-07-27 per the original plan — this only removed the code-readiness blocker on that).
5. **`last_seen_bar` restart-unsafe init** (`active_signals.py`): started as an empty dict on every
   daemon restart, so `last_seen_bar.get(ticker)` returning `None` trivially `!= last_bar_ts` on the
   very first poll after ANY restart — confirmed live (a restart at 11:14 ET triggered SPY's arm/TP
   check at 11:21 ET, not a real bar close or pinned exit-arm time). Fixed via new
   `_seed_last_seen_bar(open_positions)`, called at `run_loop` startup, seeding each open position's
   ticker from its real current cached bar. Opus review confirmed this can't wrongly *skip* a
   genuinely-new bar's evaluation (the loop's own data refresh runs before the first check). The
   other three same-shaped trackers (`sell_alerted`/`window_alerted`/`limit_fill_alerted`) were
   scoped down and left as an explicit residual item, not fixed — see `docs/backlog_cache.md`; they
   have no dedicated DB column to reconstruct "already alerted today" from the way the clock-keyed
   trackers (`reference_alerted` etc.) do, so a rushed fix risked a new bug with no way to verify it
   against real market timing that day.
6. **`buy_alerted`'s once-per-day lockout** (`active_signals.py`): blocked a real, quantified ~8% of
   SOXL's backtested trades (buy→sell→buy same day) since the ticker's one alert fired before the
   position even closed. Fixed: the lock now clears for a `(ticker, strategy, window)` key once
   `closed_today(ticker)` is true, no position is open, **and no order is still resting**
   (`pending_buys` check) — that last condition was missing from the first pass and caught by Opus
   review: closed_today + no open position is *also* true for the entire window between "order
   placed" and "position opens on Filled confirmation," not just after a genuine close, so without
   it the unlock would have re-fired (re-notified, and on an automated path, re-placed a real
   order) on every single poll while a real re-entry order was still resting at the broker. Fixed
   before commit; new regression test simulates repeated polls with a resting pending buy and
   confirms zero re-fires.
7. **`_trailing_buy_status`'s stale cache-derived trigger** (`signals_notify.py`): re-derived
   `running_low`/trigger from the hourly cache's first bar since signal_time, instead of the real
   `pending['signal_price']` anchor `check_gap_resize` already uses for the real order's trigger —
   confirmed live (GDXU: real trigger $79.665×1.003=$79.90, cache-derived trigger $81.14, wrongly
   returned `met=False` and silently suppressed a reminder for an order that had already filled).
   Fixed: anchor `running_low` to `pending['signal_price']`, still track further real dips via the
   hourly cache going forward. **Opus review caught a regression in the first pass**: the no-cache
   fallback returned `met=False` instead of `met=None` — `check_buy_reminders` treats `False` as
   "confirmed not met, skip nagging" but `None` as "unknown, nag anyway," so the first-pass fix
   would have silently suppressed reminders for every ticker with no cache at all, the *exact*
   failure mode this fix exists to close, just via a different trigger. Fixed before commit
   (returns `None` with the real trigger still populated for display).

**New standing conventions, `docs/automation_principles.md` #13/#14/#15** (added same session, so
future code doesn't reintroduce these three bug shapes):
- #13: never rely on a UNIQUE constraint over a nullable column for dedup (this exact bug hit twice
  in one day — `add_node`'s `take_profit`, then `scenario_expectations`/`coverage_deviations`'s
  `ticker` from the prior session).
- #14: re-examine a safety guard's purpose before a new order type/code path routes through it
  (both `notional_cap` and `daily_order_cap` were built BUY-entry-centric and silently inherited by
  SELL/protective paths with the opposite risk profile).
- #15: any in-memory dedup/tracking set in `run_loop` must be smart-initialized at startup from
  real persisted/derivable state, not left empty (`last_seen_bar` vs. the already-correct
  `reference_alerted`/`gap_check_alerted`/`pinned_bar_alerted` pattern).

**User's framing, start of session**: explicitly asked whether these were "architectural bugs —
slop we created carelessly," not just isolated one-offs. Answer given and recorded: yes, in the
sense of missing standing conventions (the three patterns above), not careless individual mistakes
— each bug was a reasonable decision in isolation that didn't get re-examined when a new caller
started relying on it. The three new principles are the actual fix for the pattern, not just the
seven point fixes for each instance.

**Second Opus review round, same evening — 4 more findings (2 CONFIRMED, 2 PLAUSIBLE), all
fixed or triaged before commit**: the reviewer's full findings list was pulled after its prose
summary (correctly) only elaborated on items 6 and 7 above. The other four, not mentioned in the
prose:
- **CONFIRMED — `add_node`'s dedup key omitted `arm_sell_pct`** (`signals_db.py`): the fix in item
  1 above mirrored the *original* UNIQUE key exactly (`watchlist_id`/`ticker`/`strategy`/`version`/
  `window`/`take_profit`/`stop_loss`/`max_hold_hours`), which never included `arm_sell_pct` —
  but for `TrailingBothZScoreBreakout`, `take_profit` is always NULL and `arm_sell_pct` is the
  real distinguishing value. Once the NULL-matching fix started actually enforcing the rest of the
  key, two genuinely different nodes (same `take_profit=NULL`, different `arm_sell_pct`) would have
  silently collapsed to one — a new silent-drop bug introduced while fixing the old
  silent-duplicate one. Fixed: added `arm_sell_pct`/`trail_buy_pct`/`trail_sell_pct` (COALESCE'd)
  to the explicit dedup check. New regression test confirms two nodes differing only in
  `arm_sell_pct` both persist.
- **CONFIRMED — `buy_alerted`'s same-day unlock never fired for paper nodes** (`active_signals.py`,
  `signals_db.py`): `closed_today()` queried the real `trade_log` only — a paper-trading node's real
  exit only ever lands in `paper_trade_log`, so the unlock in item 6 above could never trigger for
  a paper node, silently keeping the very same-day-rebuy edge this fix exists to recover invisible
  in paper results. Fixed: `closed_today` gained a `paper=False` param (matching the `_pos_tables`
  convention used everywhere else); the unlock now passes `paper=True` for any non-`live`-mode
  node. Also generalized the pending-buy check from item 6 to union `get_paper_pending_buys()`
  alongside the real `get_pending_buys()`, since paper trailing-buy nodes have the identical
  pending-order-resting window (`paper_trading.start_paper_buy`).
- **PLAUSIBLE, addressed — SELL now bounded only by the $100k `HARD_ORDER_CEILING`**
  (`schwab_safety.py`): removing `notional_cap` from the SELL side (item 3 above) also removed the
  only real bound on SELL quantity our own code enforced — oversell protection had always relied
  entirely on Schwab's own rejection, `notional_cap` only coincidentally caught it too. Not a
  regression this session created outright (the underlying gap predates this diff), but directly
  exposed by it. Fixed with a more principled replacement: SELL quantity is now checked against
  the real `open_positions.shares` on file for the ticker (1.1‰ float tolerance), raising
  `SafetyViolation` before ever reaching the broker as a would-be short — narrower than
  `notional_cap` ever was (can't false-positive-block a legitimate large exit) but actually targets
  the real risk (our own inflated share-count bugs, e.g. an unconfirmed top-up).
- **PLAUSIBLE, not currently reachable, backlogged not coded** — the new manual-"Filled" SL call
  (item 4 above, `handle_trail_buy_fill_price`) gates only on `AUTOMATION_ENABLED_TICKERS`, not on
  `node.mode == 'live'`, unlike the BUY-side routing in `_scan_buy_signals`. Traced whether this is
  live-reachable today: the real `pending_buys` table (which is what makes the "Filled" button
  exist at all) is only ever populated from `notify_buy_signal`
  (`signals_notify.py:410`), which `_scan_buy_signals` only calls for `mode=='live'` nodes — a
  research-mode ticker's BUY routes to `paper_trading.start_paper_buy` instead, which has no
  "Filled" button at all. So this gap has no live path to it today; only a future change to that
  routing (e.g. the "Monday mode-scoping" backlog item, or the "run both real+paper regardless of
  mode" idea already on file) could make it reachable. Left as a defense-in-depth note on that
  future work rather than coded now — see `docs/backlog_cache.md`.

Full suite after this second round: 210 passed (was 195 at session start).

## ✅ [live-trading] Resolved 2026-07-25 — coverage-system "compass" v2 complete: node_id/mode/strategy_type identity, six-round Opus review chain, Streamlit dashboard, Slack-callable report
Spanned two sessions (2026-07-24 late night start through 2026-07-25). Real ask: not "add a
scenario_expectations row per known bug" but capture *all* intended behavior across paper/dry_run/
live, so a deviation with no reason is always the actionable finding (automation_principles.md #16's
"no unexplained failure" contract) — and along the way, close two architectural gaps the user flagged
directly: ticker alone isn't a real node identity key, and some safety controls weren't scoped at the
right level.

**Identity migration**: `scenario_expectations`/`coverage_deviations`/`coverage_events` gained
nullable `node_id` (FK `watch_list.id`) + `mode` (paper/dry_run/live), replacing ticker-only identity
(SQLite `UNIQUE` over nullable columns can't dedup — same bug class as `add_node`'s `take_profit=NULL`
failure). `add_scenario_expectation`/`record_deviation` rewritten with explicit COALESCE-based
check-then-upsert (automation_principles.md #13). New `signals_db.get_watch_list_node`/
`get_watch_list_node_by_id` (fail-open by design — pure observability, must never raise into live
control flow).

**Six independent Opus review rounds** on the growing diff (real money at stake) found and fixed,
with rising-then-falling severity: watchlist-scoping bugs in node resolution; `_PENDING_BUY_NODE_KEYS`
missing `'id'` (silently made ~14 of ~18 wired call sites dead code); a stale `pending_buys` account
snapshot (IVV, cleared per user decision); **the most severe finding** — `signals_blocks.py`'s
BUY-alert button and the Reference Report's "Manually Open" button both omitted `account`/`id` from
their node whitelist, so every manual BUY confirmation (the primary way live positions get opened)
could write `open_positions.account=NULL`, invisible to live-state reconciliation — fixed both
locations, a real pre-existing bug independent of the migration that this audit chain happened to
surface; a diverged same-day-buy-warning check misfiring on the live `soxl_ira` margin account; a
dead-code `_fresh_node` helper removed after its own fix made it a no-op; a fail-open regression in
an earlier round's own fix. Full suite 224 passed, `live_sim_harness.py` 6/6.

**Streamlit dashboard + strategy_type axis (2026-07-25)**: `pages/14_Coverage.py` ("Coverage
Compass") — unexplained deviations at top with an inline Explain action, today's per-scenario
pass/fail, the scenario x mode coverage matrix, per-scenario drill-down into raw events. Direct
`sqlite3` reads (not `signals_db`) to avoid constructing a Slack Bolt `App()` inside the Streamlit
process. `coverage_events` gained a nullable `strategy_type` column, auto-derived by
`log_coverage_event` from `node_id`'s real `watch_list.strategy` — no call site needed to change.
`scripts/coverage_matrix.py` gained a `--strategy` filter.

**Slack-callable report (piece #7)**: `signals_notify.send_coverage_report()` runs `scripts/
coverage_check.py`'s real check live and posts a compact summary, wired to a "🧭 Coverage Report"
button on the Morning Report. A required Opus review (automation_principles.md #12) found 2
CONFIRMED + 2 PLAUSIBLE, all fixed: an on-demand weekend tap manufactured permanent false-positive
deviations (a "trade closed today" expectation is trivially false when the market never opened —
confirmed live, 6 real Saturday rows in `trading_live.db`, cleaned up) — fixed with a weekday gate
inside `run_check` itself, covering both the CLI and the Slack caller; the Slack report re-queried
`coverage_deviations` after running the check, so a scenario that deviated earlier and became met
later still rendered UNEXPLAINED, contradicting the check that had just passed it — fixed by having
`run_check` return structured results (and a new `clear_deviation_if_resolved` delete a stale same-
day deviation once met), with the Slack report rendering from that live return value; an unknown
`check_method` silently rendered ✓, now shown as "not checked"; no exception handling meant a
failure would silently no-op the button, now posts a visible failure message.

Final state: full suite 228 passed (was 195 at the start of this arc), `live_sim_harness.py` 6/6.
All 7 pieces of the original 2026-07-24 coverage-system reframe (dashboard, 3rd axis, structured
expected-vs-actual, drill-down, no-unexplained-failure contract, Slack report) are done.

**Session-close Opus review (2026-07-25), covering the full session diff including `signals_db.py`'s
changes not yet independently reviewed** — 2 CONFIRMED + 1 PLAUSIBLE, all fixed: (1) CONFIRMED —
`scenario_key` alone is not a unique key (two active `scenario_expectations` rows can legitimately
share one `scenario_key`, disambiguated by `node_id`/`mode`, e.g. the same designed scenario run
against two nodes on purpose), but `send_coverage_report` and `pages/14_Coverage.py` both keyed their
deviation-lookup dicts by `scenario_key` alone — explaining one node's deviation would silently mask
a different node's still-unexplained one. Fixed: `coverage_check.run_check`'s result dicts now carry
`node_id`/`mode`, and both callers key by the composite `(scenario_key, ticker, node_id, mode)`. Live
data checked: the 6 pre-migration `node_id=NULL` canary rows are already `active=0` (not double-
counting today), so this wasn't yet live-triggering, but the code path was a real bug. (2) CONFIRMED
— `pages/14_Coverage.py`'s "Today's Scenarios" table inferred "✓ met" from "no deviation row today"
alone, without checking whether `coverage_check.run_check` actually evaluates that scenario at all —
an `occasional`/`regression-only`-frequency row or an unrecognized `check_method` would render green
having never been checked. Fixed: the page now imports `scripts.coverage_check.CHECKERS` and renders
"— not daily-checked" / "? unknown check_method" instead of assuming met for those cases. (3)
PLAUSIBLE — `clear_deviation_if_resolved` deleted unconditionally, including a row a human had
already explained; if the scenario later became met on a same-day re-check, the explanation and the
record that anything had deviated at all would be silently destroyed. Fixed: added `AND reason IS
NULL` to the delete, matching `record_deviation`'s existing "never clobber a reason" rule. 3 new
regression tests. Full suite 230 passed (was 228), `live_sim_harness.py` 6/6.

## ✅ wl_id-keyed refactor — implemented, reviewed (2 Opus rounds), landed 2026-07-25/26
Resolves the systemic ticker-only-keying gap scoped over the prior two sessions (see the
2026-07-25/26 design-conversation entry above, now historical) — everything in the live-trading
stack that assumed "one ticker == one active `watch_list` node" is rekeyed onto the `watch_list`
row's own PK (`id`, called `wl_id` in code/comments), across 20+ sites in `signals_db.py`,
`active_signals.py`, `paper_trading.py`, `signals_helpers.py`, `signals_handlers.py`,
`signals_notify.py`, `schwab_safety.py`.

**Schema migration** (`signals_db.ensure_tables()`): new `wl_id` column on `open_positions`,
`paper_positions`, `pending_buys`, `paper_pending_buys`. Backfill: `pending_buys`/
`paper_pending_buys` deterministically from `node_json['id']` (already embedded via
`_PENDING_BUY_NODE_KEYS`); `open_positions`/`paper_positions` best-effort matched against current
`watch_list` on `(ticker, strategy, version, window, account)`, left `NULL` on an ambiguous match
(confirmed real on `cache/live/trading_live.db`: EDC's real 423-share legacy position, id=15,
matches 2 candidate rows across 2 superseded watchlists and is correctly left `NULL` rather than
guessing). Applied to the real live DB this session (daemon confirmed not running first; backed up
to `cache/live/trading_live.db.bak_wl_id_migration_20260725_130800` beforehand).

**Key fixes on the real (non-paper) order path**: `schwab_safety._live_ticker_accounts()` changed
from `{ticker: account}` to `{ticker: set-of-accounts}` (this is what actually unblocks the
motivating SOXL-in-two-accounts design — the old single-value map hard-rejected a second live
node's real orders as "assigned to the wrong account"); `open_position()`'s duplicate-position dedup
moved from `(ticker, window)` to `wl_id` (with a `(wl_id IS NULL AND ticker=? AND window=?)`
fallback clause so legacy/unbackfillable rows keep the old protection); all 6 BUY-side Slack button
handlers now clear/mark `pending_buys` rows by `wl_id` instead of ticker, matching the SELL-side
`position_id` pattern that was already correct; `_reconcile_buy_fill` gained a `wl_id` disambiguation
hint (bails + alerts rather than guessing when it doesn't match); `_place_stop_loss_for_position`
now resolves the position by `wl_id` instead of ticker (was sizing/anchoring the stop off whichever
position had the latest `entry_time`, regardless of node); `check_order`'s oversell guard now
resolves the position by `(ticker, account)` via new `get_open_position_for_account` instead of
ticker alone.

**Additive, not built out further**: node-level automation pause (`schwab_safety.
pause_node_automation`/`resume_node_automation`/`node_automation_enabled`) alongside the existing
ticker-level pause, AND-gated (`ticker_automation_enabled(ticker) AND node_automation_enabled(wl_id)`)
— no Slack button wired yet, console/script-only. The per-node `dry_run` override idea (backlogged
2026-07-26, see below) remains gated on this landing and being observed correct live first.

**Known, deliberately-not-fixed residuals** (documented in code, not silently dropped):
- `check_order`'s `node_automation_enabled` gate still derives `_node_id` via a ticker+account fuzzy
  `get_watch_list_node()` lookup (returns `None` on ambiguity → pause silently no-ops for 2 nodes
  sharing both ticker AND account) — full plumbing of a real `wl_id` through `schwab_client`'s 6
  order-placement functions was scoped as its own follow-up, not done this session.
- `_last_sale_recovery`'s new narrowing to `(ticker, strategy, version, window, account)` is a real
  live sizing-behavior change: most v5 nodes' `trade_log` history is under a different `version`
  (v4/v3.x) so they now fall back to `starting_notional` instead of compounding off the last
  recycle. Confirmed benign (no node has a `NULL starting_notional`), but stated explicitly since
  it changes real position sizing.
- Legacy Slack messages predating the `node['id']` payload field (added 2026-07-25, commit
  `3accd4e`) would `KeyError` if clicked — confirmed no currently-outstanding `pending_buys` row
  lacks it; self-expiring, not fixed defensively.
- `_has_open_order`/`_has_open_buy_order_in_account`/`_has_open_sell_order` (schwab_safety.py)
  deliberately stay ticker(+account)-keyed — permanent policy constraint, the real broker order book
  has no `wl_id` concept; two live nodes on the same ticker in the *same* account still cannot both
  hold resting orders.
- Per-ticker pause/kill-switch stays ticker-scoped by explicit user decision (a pause is meant to be
  a blunt instrument); a proposed 3rd, strategy-wide pause level was explicitly rejected as
  over-engineering ("I'd probably just kill the full program").

**Verification**: full suite 232 passed throughout (was 232 pre-refactor too — pure rekeying, no
new test surface beyond fixing tests that hardcoded the old ticket-keyed shapes),
`live_sim_harness.py` 6/6, `verify_trailing_buy_resolution.py --tickers AGQ,SOXL` and
`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` both clean (expected — this refactor is
identity-keying only, doesn't touch signal/fill computation). Two independent Opus review rounds:
first round (against the initial implementation) found 11 issues, 2 HIGH on the real order path —
9 fixed same session, 2 left as documented limitations (above). Second round (against the fixed
diff, explicitly re-verifying the first round's fixes and sweeping fresh) confirmed all 9 fixes
landed correctly, then found 4 more real issues (1 HIGH: the oversell-guard ticker-only lookup
above; 1 MEDIUM-HIGH: `drain_fill_queue` had started passing its own fuzzy ticker+account node-id
guess as a hard disambiguation gate into `_reconcile_buy_fill`, which could block reconciliation of
a real confirmed fill — reverted to logging-only; 2 MEDIUM: `_pos_key`'s `last_seen_bar`-collision
fix wasn't applied to `paper_trading.py`'s copy of the same lookup, and `open_position_keys`'s
NULL-`wl_id` fallback was dropped in the exact same way `open_position()`'s SQL dedup already
needed one) — all 4 fixed and re-verified (full suite + harness + parity scripts green again).

**Not done this session** (deliberately deferred, per the original scoping): creating the second
SOXL `soxl_ira` node — planned as the next step once this refactor is observed correct in the field.

Also fixed opportunistically while verifying: `scripts/setup_2026_07_24_soxl_ira_live_test.py`'s
top-level code (real DB writes) was unconditionally executing on every bare `pytest` invocation
because its filename ends in `_test.py`, matching pytest's default discovery glob — wrapped in
`if __name__ == "__main__":`. Pre-existing hazard, unrelated to this refactor, found only because
the schema migration briefly broke it mid-run (caught before any real data was written past the
existing idempotent `add_node` calls).

## ✅ Resolved 2026-07-26 — Opus review of the paper_alert_verbose/account-dedup diff: 1 HIGH, 2 lower findings, all fixed
Background Opus review of the prior session's `paper_trading.py`/`signals_db.py` diff (paper-alert
suppression + the `account`-inclusive `watch_list` dedup, see the entry above) came back with:
1. **HIGH, fixed**: the new `account`-inclusive dedup broke idempotency of two live-setup scripts
   (`scripts/add_labu_backup_node.py`, `scripts/setup_2026_07_24_soxl_ira_live_test.py`) that called
   `add_node(...)` without `account`, then patched it in via a raw `UPDATE watch_list SET account=...`
   afterward. A rerun's dedup check now compares `account=NULL` against the real stored account,
   never matches, and inserts a second live node with its own `wl_id` — reproduced live against a
   copy of `trading_live.db` (91→92 rows, a duplicate LABU node). This is the exact failure mode
   behind the two `pre_dup_node_cleanup` backups already on file from 2026-07-24. Fixed: both
   scripts now pass `account="soxl_ira"` directly into `add_node`, `_set_account` helpers deleted,
   stale "rerun is a harmless no-op" docstring claims corrected. `scripts/live_sim_harness.py` had
   the identical pattern but was already safe (wipes its sim DB first) — left as-is.
2. **MEDIUM (forward hazard), fixed**: the `watch_list` account-rebuild migration
   (`signals_db.ensure_tables()`) copied columns via a hardcoded 24-column list that ran *after* the
   ALTER ladder — on any DB that hadn't been rebuilt yet, a future new column added by the ALTER
   ladder within the same `ensure_tables()` call would be silently dropped by the rebuild's fixed
   list. Fixed: column list now derived from `PRAGMA table_info(watch_list)` at runtime. Verified
   against a synthetic pre-migration DB (old schema, no `account` in `UNIQUE`, ALTER ladder + rebuild
   both firing in one call) — row survived with all 24 columns intact.
3. **LOW, fixed**: `update_paper_buys`'s fill alert read `paper_alert_verbose` off the frozen node
   snapshot captured in `pending_buys.node` at signal time, not the live `watch_list` row — flipping
   the flag on after a paper buy signal fired but before it filled wouldn't produce the "just turned
   verbosity on" alert you'd most want. Fixed: re-reads the live node via
   `get_watch_list_node_by_id` before checking the flag. Zero live impact today (0 rows in
   `paper_pending_buys` at review time).
Two PLAUSIBLE-but-not-CONFIRMED items left open, no live impact: the same rebuild's `UNIQUE(`
string-match schema check could false-positive on a hand-modified DB with no `UNIQUE` clause at all
(needs a hand-crafted DB to trigger); the rebuild has no explicit transaction wrapping (matches the
pre-existing 2026-07-18 `watchlist_id` migration's shape, not a new pattern). Full suite: 232
passed. `live_sim_harness.py`: 6/6 scenarios passed.

## ✅ Resolved 2026-07-26 — session-wrap Opus review of the above fix: 1 CONFIRMED (fixed), 1 LOW (no action)
Mandatory review of this session's own diff (`signals_db.py`/`paper_trading.py`, the 3 fixes above)
before closing. Findings:
1. **MEDIUM, CONFIRMED, fixed**: the `watch_list_new` rebuild table had no `DROP TABLE IF EXISTS`
   before its `CREATE TABLE` — reproduced live: if the `INSERT`/rebuild ever fails partway (e.g. a
   future ALTER-added column not yet reflected in the hardcoded `CREATE TABLE watch_list_new`
   definition), the orphaned table survives the failed transaction, and every subsequent
   `ensure_tables()` call (i.e. every daemon startup) dies at the `CREATE TABLE` with "table already
   exists" — a total outage, not a degraded mode, requiring manual `DROP TABLE watch_list_new` to
   recover. Real data was never at risk (the failing INSERT/DROP/RENAME transaction rolls back
   cleanly). Fixed: one `c.execute("DROP TABLE IF EXISTS watch_list_new")` before the executescript.
2. **LOW, no action**: `paper_trading.py`'s live-node re-read for the verbose-alert gate returns
   `None` (silently suppressing the alert) if the `watch_list` row is deleted between signal and
   fill — paper-only, alerts-only, no trade/P&L impact, flagged as an intentional-vs-accidental
   question rather than a defect.
Also confirmed by this review, worth keeping: the `paper_alert_verbose` bug fixed earlier this
session was actually worse than "stale" — the key was never in `_PENDING_BUY_NODE_KEYS`
(`signals_db.py`), so the frozen snapshot never carried it at all and the alert **could never fire**
regardless of flag state, not just on a narrow timing window. And the executescript→3-separate-
`execute()` change actually *improved* transaction atomicity (verified via a direct `in_transaction`
probe) rather than introducing a new crash window, since the old `executescript()` form auto-
committed the `DROP`/`RENAME` as separate transactions.
Full suite: 232 passed. `live_sim_harness.py`: 6/6 scenarios passed.

## ✅ Resolved 2026-07-26 — "run both" real+paper restructure dropped; the two-node pattern already covers it
Item 2 of the 2026-07-24 "paper trading fully dormant" Monday follow-up list (restructure the
BUY-routing if/elif so a node runs both the real/dry_run alert path and paper trading regardless of
`mode`) was dropped after verifying the wl_id refactor already makes a simpler alternative safe: two
separate `watch_list` nodes for the same ticker (one `research`, one `live`) — the exact DPST
pairing added last session — give continuous paper coverage and live trading simultaneously with no
code change. Verified before dropping: `paper_trading.py`'s dedup is `wl_id`-keyed, not
ticker-keyed, and `active_signals.py`'s `open_position_keys` (line 530-531) already merges real +
paper positions, so the two nodes don't collide. Item 3 (`account=None` gap on 15/24 `mode='live'`
nodes) remains open, still targeted for Monday 2026-07-27.

## ✅ Resolved 2026-07-26 — Morning Report / signal-window Slack alerts hit the 50-block limit again (25 nodes, up from 16 at the 2026-07-22 fix); replaced per-row block-shrinking with real chunking
The 2026-07-22 fix (collapsing 3 `actions` blocks/row into 1) bought headroom, not a permanent fix
— the watchlist grew from 16 to 25 nodes and the Morning Report hit Slack's `invalid_blocks` 50-block
cap again (confirmed in `logs/active_signals.log`). Rather than shrinking per-row block count again
(a fix with an expiration date), built `signals_blocks._post_chunked(text, fixed_blocks, units,
max_blocks=50)`: greedily packs per-row block groups into <=50-block chunks, posts the first chunk
normally and any overflow chunks as Slack thread replies (new `thread_ts`/`reply_broadcast` params on
`_post_message`) so the report still reads as one thing and overflow still triggers a mobile
notification. `signals_notify.send_reference_report` and `_send_window_alert` (the 10:25/15:25 ET
HIGH ALERT) both rewritten to build a list of row-units and call `_post_chunked` instead of
concatenating one flat `blocks` list.
**Session-wrap Opus review of the real diff (`signals_blocks.py`/`signals_notify.py`) found 4
CONFIRMED issues, all fixed same session**: (1) most severe — a chunk-2+ Slack post failure was
invisible to `log_coverage_event("morning_report_delivery", ...)`, which read only the first chunk's
`(channel, ts)` and would log "sent" even if the Buy Candidates section silently never posted; fixed
by having `_post_chunked` track delivery across every chunk and return `ts=None` if any chunk failed,
so `channel and ts` truthy-checks at call sites correctly read partial delivery as not-fully-sent. (2)
`_send_window_alert` (the signal-window HIGH ALERT, arguably more load-bearing than the morning
report — no daily backstop if it silently fails) was never converted and had the identical unbounded
50-block bug left in place; converted to `_post_chunked` too. (3) overflow thread replies were
mobile-notification-silent (no `reply_broadcast`) — fixed by adding `reply_broadcast=True` on every
chunk after the first, consistent with the project's standing "every alert must render cleanly on
mobile" convention. (4) test gap — the original tests only exercised `_post_chunked` with synthetic
block lists, nothing proved real `_ticker_block` output actually chunks correctly at scale; added
`test_send_reference_report_actually_chunks_a_large_watchlist` (60 synthetic rows through the real
`build_reference_table`→`_ticker_block`→`_post_chunked` path). 2 more regression tests target
the partial-failure detection itself. Also separately: my own test-script invocations while verifying
this fix live wrote 5 real `morning_report_delivery` rows into `cache/live/trading_live.db`'s
`coverage_events` table (not real daemon/button-triggered events) — same pollution class as the
2026-07-25 pytest incident; backed up the DB and deleted the 5 rows after confirming with the user
this wasn't the sticky `coverage_deviations` ticket model (raw event log, not a deviation record).
Full suite: 289 passed (was 277 at session start). `live_sim_harness.py`: 7/7. `signals_invariants.py`:
0 known violations (UDOW's stale test position was retroactively closed last session).

## ✅ Resolved 2026-07-26 — Trade-Flow Accountability Grid redesigned to show per-mode (Paper/Dry-run/Live) status independently, not one collapsed overall status
`compute_status()` (`scripts/coverage_registry.py`) picks one overall status per scenario in
priority order (live > dry_run > paper) and returns as soon as any mode has real events — so a
scenario with genuine live evidence *and* zero paper evidence rendered as fully green
`verified-live`, with no way to eyeball which modes actually have proof (raised directly by the
user reviewing the grid). New `compute_mode_statuses(row)` computes each of paper/dry_run/live
independently: for `coverage_events`-mechanism rows, buckets by real mode with the same
good/bad-results logic as `compute_status`; for a row scoped via `mode_filter` (e.g.
`dry_run_buy_synthesis`, which disambiguates a `scenario_key` shared with `paper_trading.py`), the
other two modes report `not-applicable` rather than a false gap; for `scenario_expectations`-
mechanism rows, queries `coverage_deviations` (which carries its own `mode` column) per mode instead
of across all modes at once; `offline_only`/`open_price_quality_log`/`none` mechanisms (not
mode-scoped by nature) mirror the same status across all three columns. `pages/14_Coverage.py`'s
main grid gained 3 new independently-colored columns (Paper/Dry-run/Live, each its own cell
background via a new `_heat_row_and_mode_cells` styling function) plus a compact `P/D/L` one-glance
column (e.g. `P🟩 D⬜ L🟥`), while keeping the existing whole-row-colored `Status` column for the
existing sort order. Confirmed live: `dry_run_buy_synthesis` now shows real `verified` dry_run
evidence (2 real events, 2026-07-26) — closes the "zero real firings ever" gap flagged at the start
of this same session. Also fixed same session: the Streamlit process itself (running since 2026-07-25)
was serving stale pre-2026-07-28 `coverage_registry.py` logic (Streamlit's file-watcher doesn't
reliably reload changes in imported-but-not-page modules) — a browser hard-refresh alone didn't fix
it, needed a process restart. 5 new regression tests in `tests/test_coverage_check.py`. Full suite:
289 passed (combined with the chunking fix above, same session).

## ✅ [live-trading][security] Resolved (retroactively confirmed 2026-08-01) — 2026-07-23 night soxl_ira live-order testing findings, all closed within days
Real orders placed directly against `soxl_ira` (bypassing `schwab_safety` via raw `schwab_client`/
`schwab.orders` calls, same pattern as `scripts/live_sanity_check.py`) to sanity-check real Schwab
behavior before the 2026-07-24 live test day. Findings, most-important first:
1. **Fixed same night**: `schwab_client.get_account_balance` read `cashAvailableForTrading`, which
   doesn't exist on a MARGIN-type account (confirmed against both `soxl_ira` and `ira` — both report
   `type: MARGIN` from Schwab's real API). Correct field: `availableFunds`. Fixed with a fallback.
   Fail-closed bug (blocked every real BUY via `KeyError` → `SafetyViolation`), not a silent bypass.
2. **Fixed 2026-07-24, commit `fda9b2a`** ("Fix real order async-confirmation gap"): order
   placement/cancellation are asynchronous — an oversized BUY returned HTTP 201 (accepted) but a
   follow-up `get_order` ~0.3-0.7s later showed the real verdict, `REJECTED`. Production code used to
   post a success message right after the HTTP response with no follow-up check. Fixed:
   `schwab_client._post_order_confirmation` (line 162) now polls real status via
   `_confirm_order_status`, raises `OrderRejected` on a confirmed bad status
   (`_ORDER_TERMINAL_BAD_STATUSES = {REJECTED, CANCELED, EXPIRED}`), posts an immediate fill message
   on `FILLED`, wired into every real placement/replace call site. Confirmed via 2 Opus review rounds
   at the time.
3. **Informational, not a bug**: contradicted the stated rationale for `_has_open_buy_order_in_account`
   (built 2026-07-22 on the assumption "Schwab doesn't reserve buying power for a resting order") — a
   real same-ticker resting BUY test showed Schwab *did* hold the first order's funds. The guard
   itself is still valid defense-in-depth; only its justification comment was inaccurate. Cross-ticker
   case was never separately tested, but is moot now — this guard has been exercised routinely by
   real live trading since (GDXU, SPY, DPST, SH, LABD, etc.).
4. All other clean/expected results (naked_sell rejected, oversell rejected, stacked-order boundary
   correct, unfilled BUY correctly excluded from sellable-share count, real `TRAILING_STOP`/`STOP`
   order shapes confirmed) — no action needed.
5. The deprecated `schwab-py` float-to-`set_stop_price` warning flagged this session is also already
   fixed — `schwab_client.py:601`/`655` pass `f"{stop_price:.2f}"` (string), not a raw float.
**"Biggest remaining gap" from this entry** (no real order had yet gone through the actual
production/Slack workflow, only direct-bypass API calls) is moot as of 2026-08-01 — the daemon has
since placed and confirmed many real orders through the full production path (GDXU gap-resize
2026-07-27, SPY trailing-sell fill 2026-07-27, DPST as the standing live-money volunteer, LABD's
rejected-stop fallback 2026-07-31, the `handle_entry_price` SL fix 2026-08-01, etc.). `notional_cap`
Test A (soxl_ira set to $1, expect `SafetyViolation`) was superseded by this broader real usage and
was never separately logged as run — not worth retroactively testing now that the guard chain has
extensive real-world exercise behind it.

## ✅ [live-trading] Resolved 2026-08-01 (evening) — SL Slack alert falls back to a generic "should have auto-filled" guess; split into distinct known/automation-pending/dry-run/manual paths
Grew out of a real SPY false-positive (07-24): `signals_blocks._build_sell_blocks`'s SL branch had
one fallback string ("Check account — Stop Loss order should have auto-filled @ $X") for every
position lacking `broker_stop_price`, regardless of whether an automated stop was ever supposed to
exist there. User's framing: this is really two (turned out to be four) different kinds of position
wearing the same alert path, and needed real code paths, not a query patch.
**Root cause found while scoping**: `signals_db.set_broker_stop_price` existed but was never called
anywhere in production — `broker_stop_price` was permanently `None` on every position, automated or
manual, making the "known, trust it" branch entirely theoretical.
**Design**: new `signals_helpers.stop_status(pos)` returns one of 4 states —
- `'known'`: `broker_stop_price` on file (our own code placed it and recorded it) — trustworthy.
- `'automation-pending'`: ticker in `AUTOMATION_ENABLED_TICKERS`, real (non-dry_run) account, no
  price on file — a real anomaly, actionable/urgent alert (placement may have failed).
- `'dry-run'`: the account is `dry_run` — `schwab_client.place_stop_loss` short-circuits and never
  places anything for these, so `broker_stop_price` can structurally never be recorded regardless of
  scope. Not an anomaly, must not render as one.
- `'manual'`: real account, position never automation-scoped — no automated stop was ever supposed
  to exist, alert says "verify yourself," not "should have auto-filled."
New `signals_db.set_broker_stop_price_by_position(position_id, price)` (position-id-scoped, mirrors
`set_sl_order_id_by_position` — the legacy ticker-keyed version would have misattributed the price
across nodes sharing a ticker, same bug class already fixed elsewhere via the wl_id refactor), wired
into the two success branches of `signals_notify._place_stop_loss_for_position`. `_build_sell_blocks`
and `_exit_pending_blocks` (the 15-min stalled-exit reminder) both branch on the 4 states instead of
the old binary fallback.
**Deliberately out of scope, per user's explicit call**: polling the broker's real order book to
detect a manually-placed stop and back-fill `broker_stop_price` from it. Reasoning: self-healing off
inferred/polled broker state risks silently masking a real problem — same failure class as the
already-fixed `coverage_deviations` auto-resolve bug that hid a genuine new failure. Only recording
what the automated code itself directly placed was judged safe; a manually-managed position is
expected to permanently show `'manual'`, which is correct, not a gap. **If a future broker-polling
feature is ever built**: any unexpected state change (a stop appears at the broker that we didn't
place) should always alert distinctly rather than silently reconcile — same "ticket" model, not
silent self-heal (user's framing: "either we're hacked or I took an action" — both cases are worth
surfacing, neither should be swallowed).
**Independent Opus review of the initial implementation found 3 issues, all fixed same session**:
1. **MED-HIGH**: `stop_status` initially ignored `dry_run` entirely — since `place_stop_loss` never
   really places anything for a dry-run account, every manually-confirmed position in the 4 of 5
   dry-run accounts would have permanently rendered a false "may be a placement failure" alarm. Fixed
   by adding the dedicated `'dry-run'` branch (checks `schwab_safety.ACCOUNTS[account].dry_run`).
2. **MED**: the `if sl_order_id is not None:` guard in `_place_stop_loss_for_position` was also
   gating the new `broker_stop_price` write — but that guard exists for a narrower, different reason
   specific to `sl_order_id` (order-id extraction can legitimately fail on a real success). Since
   placement success is already confirmed by that point, gating `broker_stop_price` on it too meant a
   genuinely-protected real position could still render the false alarm. Fixed: `broker_stop_price`
   now writes unconditionally on confirmed success (still separately gated on `not _dry_run_account`,
   added while fixing this, since the write would otherwise also fire for a dry-run no-op that
   returns cleanly with no exception).
3. **LOW-MED**: one new test (`test_manual_wins_even_with_stale_zero_price`) used an out-of-scope
   ticker and passed vacuously without exercising the falsy-`0.0`-price branch it claimed to test.
   Rewritten (`test_zero_price_is_not_trusted_as_known`) to use an in-scope ticker/real account so it
   actually exercises the check; 3 more tests added for the new `dry-run` branch and an unrecognized-
   account fallback.
Review also verified as genuinely safe (not gaps): the stale-`broker_stop_price`-after-arm concern is
real in principle but unreachable — `strategies.py`'s trailing branch early-returns before the SL
check once armed, so neither alert site's SL branch can be entered post-arm; the reference-table
display already independently guards via `trail_state.get('order_placed')`. The untouched
`signals_charts.py:143` display site is safe to leave as-is and is actually improved by this change
(it was drawing the wrong SL line for `uses_fixed_sl` strategies before, since its own fallback used
`stop_loss` not `fixed_sl` — a populated `broker_stop_price` now draws the real one).
7 new tests (`tests/test_signals_helpers_stop_status.py`, 7; 1 new assertion in
`tests/test_fake_broker_retl_scenario.py`). Full suite: 473 passed (was 466). `signals_invariants.py`:
clean, all live nodes match committed baseline.


# Bulk migration from backlog_cache.md, 2026-08-01 (evening)
The entries below were moved here verbatim from backlog_cache.md during a cleanup pass (that file had grown to 141 entries / 1728 lines, violating its own 1-2-line-pointer convention). Order preserved from backlog_cache.md (was roughly newest-first); not re-interleaved with the rest of this file's chronology -- search by header/date/ticker instead of assuming position in the file means anything here.

## [backtest][live-trading] Open, raised 2026-08-01 — formalize/write down the pattern separating manual-fill execution reality from automated/backtest-assumed fills
Currently scattered across `docs/operational_limits.md`'s Phase 1/2 marker and `sim_chaos_monkey.py`; not yet scoped where it should actually live.

## ✅ [backtest] Resolved 2026-08-01 (compounding-drag item) / Open (bear-market + regime items) — independent Opus challenge of the 2026-08-01 research tangent, then a redo + user correction on the drag finding
Full detail: `docs/research_log.md`'s five 2026-08-01 correction entries (bottom of file). Summary:
(1) **Compounding-drag — resolved.** Round 1 (Opus) flagged the original "no drag" sim as an
algebraic identity run against the wrong ($50k research-mode) nodes. Round 2 reran it against the
actual $60-200 pilot nodes (HIBL/USD/YANG) and found real, severe drag there (71-87% of trades
skipped for lack of an affordable share). **Per user correction**: that pilot-scale result doesn't
reopen the real question, since nobody deploys real capital at $200 notional — the identity is
actually a *proof* that no drag exists at realistic ($50k-scale) notional, not a null result;
`CLAUDE.md`'s Live Trading section corrected back to state this plainly. The real, narrower finding
that survives: the 3 pilot nodes' tiny notional may be undermining their own purpose (generating real
coverage-test order flow) by skipping most trades outright — open, see below. **Explicitly rejected**:
converting the `backtest_cache` retention policy's "one reference sample" into a pinned regression
test — user's call: unlikely to recur, and the diligent-manual-review process (per-strategy walkthrough)
is what actually found this bug in the first place, not a test that would have needed the bug to already
exist to write.
(2) **Time-reversal bear-market proxy — retracted, still open.** Reversing bars makes the strategy
buy local tops by construction, tells us nothing about bear-market survival. **No valid bear-market
test currently exists** (see 2026-07-18's daily-bar-variant idea and 2026-08-01's 1-min-recording
idea, neither executed — real intraday 2020/2022 data would need a paid vendor).
(3) **SPY-trend/VIX regime correlation — retracted, still open.** 669 trades across 10 correlated
tickers entering on the same ~3 real historical episodes is effective n≈3; re-slicing those 3
episodes at 3 SMA windows is 1 finding examined 3 ways, not 3 confirmations — the same
multiple-comparisons trap already named in this log's 2026-07-22 entry, reproduced without being
caught at the time.
(4) Gap/no-dip entry-quality finding softened from "no consistent pattern" to "underpowered at
n=3-4" — no action needed, already reflects reality.
**Action needed**: decide whether to bump HIBL/USD/YANG's `starting_notional` so the pilot nodes
actually generate the real order flow they were built for (currently skipping most signal windows);
decide whether/how to pursue real bear-market data. Regime/reversal findings just revert to
"unknown," not "wrong direction" — no forced action there.

## [live-trading][coverage] Open, raised 2026-07-30 — should canary scenario_expectations tests appear on the Trade-Flow Accountability Grid?
User pushback on a claim from a prior session: the canary_* scenarios (`scenario_expectations`/
`coverage_check.py`) exercise real code blocks (`_scan_pinned_entry`, `_attempt_automated_sell`,
etc.) same as any other trade-flow branch, so why don't they show up in
`scripts/coverage_registry.py`'s Trade-Flow Accountability Grid (`pages/14_Coverage.py`)? Two
coverage systems currently exist side by side (the grid's all-time `compute_status()`/
`offline_proof_for()` vs. the canary/reconciliation daily expected-vs-actual system) with no
established mapping between them. Not investigated this session, just captured per user's explicit
"park this in the backlog."

## [live-trading] Superseded 2026-08-01 — GDXD's old $5k-pilot-node role has no live successor plan; DPST now fills the "small real-money live volunteer" slot instead
GDXD's `v4` node was deleted 2026-07-20 after the kernel gap-fix flipped its backtest to CLIFF
(-37.8% alpha) — not restorable as-is, and no longer the open question (was previously listed here
as "missing, no direction yet"). DPST (`mode='live'`, `soxl_ira`, `starting_notional=800`, added
2026-07-25/26) is the actual current small-notional live-money ticker. GDXD itself remains only in
the `soxl_test` regression group, not the main watchlist — not treated as an open gap anymore.

## [live-trading][coverage] Open, raised 2026-07-28 — 2026-07-27's coverage check showed all 6 `canary_*` scenarios failing (IVV/QQQ/IWM/DIA/VOO/XLF); confirmed via `audit_live_test_candidates.py` this is a real no-entry day, not a pipeline bug (all 6 flat, none near their entry trigger). Two follow-ups: (1) all 6 still `expected_frequency='daily'` in `scenario_expectations` — same overly-optimistic assumption already fixed for 12 other grid rows 2026-07-28 (later), likely to keep minting false deviations on any day the entry dislocation doesn't happen; (2) each of the 6 `canary_*` rows appears **twice** in `scenario_expectations` (duplicate-seed/dedup gap, not yet investigated for double-counting impact on `coverage_check.py`'s output).
**Checked trade_log history for all 6 (2026-07-28)**: IWM/DIA/XLF have **never entered even once**
since being added 2026-07-23 — zero rows ever. VOO/IVV each entered once (2026-07-26) but never got a
natural exit — both were retroactively force-closed (`DRY_RUN_RETROACTIVE_CLEANUP`) during the
2026-07-27 watchlist cleanup, so `canary_market_buy_exit`/`canary_full_lifecycle` have never actually
been validated end-to-end, only interrupted. Only QQQ (`canary_early_sl`) has ever completed its
designed lifecycle naturally (entered and SL-exited 37s later, 2026-07-26) — the only one of the 6
canaries actually proven working as designed so far.
**Manually seeded 2026-07-28 08:21 ET**: `scripts/seed_canary_positions.py` directly inserted
`is_dry_run_sim=1` `open_positions` rows for IVV/IWM/DIA/VOO/XLF (1 share each, entered at real
current price) so their designed exit lifecycle can actually be exercised today instead of waiting on
a real z-score entry. QQQ left untouched (already proven). Expect today's coverage check for these 5
to reflect this manual seed, not an organic entry — don't misread it as a natural signal firing.

## [live-trading][coverage] Open, raised 2026-07-28 — two live-alert wording/staleness bugs found reviewing GDXU's TRAIL-exit test, not fixed yet
(1) `_build_sell_blocks` (`signals_blocks.py:278`/`290`) hardcodes "Cancel Stop Loss order — Sell
All (Market)" for TP/TRAIL alerts regardless of whether an SL order actually rests — for GDXU
(`trail_state.trailing=True`, `sl_order_id=None`, confirmed correct: SL genuinely was cancelled and
replaced by the resting trailing-sell) this reads as an instruction to cancel an order that doesn't
exist. Engine state itself is correct; only the alert text is stale (predates automated exits).
(2) `_current_price()` (`signals_compute.py:41`)'s staleness guard only fires for the specific
market-open-transition case (weekday, past 9:30am, cached row predates today) — overnight/after-hours
it silently replays the last cached hourly close as if live, which is what produced GDXU's "current
$81.92" overnight alert. SH didn't exhibit this only because its exit (TIME) is bar-count-based, not
price-dependent — unrelated immunity, not evidence the bug isn't real.
(3) **Bigger than wording**: for a TRAIL reason on an automation-scoped position, `notify_sell_signal`
falls through to the manual SELL-alert/Exited-Skipped-buttons path whenever its own 15s fill-confirm
poll (`_GAP_FILL_POLL_ATTEMPTS`x`_GAP_FILL_POLL_INTERVAL_SECS`) doesn't return a fill — which is the
*normal* case for a resting trailing-stop (can take far longer than 15s to actually trigger). There is
no manual action available once the trailing-sell order is already resting (`check_own_sell_fills`
polls it and auto-closes on fill) — the alert asks for action that doesn't exist, and `sell_alerted`
keys on bar-close so it'll keep re-firing every subsequent bar until it's actually filled. **User's
refinement**: don't just suppress this unconditionally — there's a real edge case worth keeping an
alert for (trailing-sell order resting a long time / expected to have triggered by now but the
position is still open, e.g. broker-side issue). Real fix should distinguish "routine still-waiting,
nothing wrong, no action" (suppress) from "should have filled by now and hasn't" (keep an informational
alert, phrased like "trail sell triggered but position still open?" — not the current "cancel SL/sell
all manually" action framing, since there's still no manual action to take, just a flag that something
may be off and worth a human look).

## [live-trading][security] Accepted residual risk, 2026-07-27 — `_submit_replace_with_retry`'s retry can fire a second `replace_order` against an already-replaced order_id
Built same session: `check_gap_resize`, `_attempt_automated_sell`, and `_attempt_automated_exit_sell`
were rewritten to use schwab-py's `replace_order` (atomic cancel-old+create-new) instead of a
separate `cancel_order` + `place_*` two-step, closing a real failure window the user identified
(a confirmed cancel followed by a failed/blocked new placement left nothing resting at the broker
in between). A second-round Sonnet review of that rewrite found a real residual gap in the fix
itself: `replace_order` targets one specific `order_id` (unlike a fresh placement) -- if attempt 1's
request actually lands at the broker (old order canceled, new one created) but the client-side
response handling then raises (timeout, malformed response after a real success), the retry loop
fires a SECOND `replace_order` against an order_id that's already dead. That call fails cleanly, so
the caller's final exception looks identical to "nothing happened at all" -- but a real, untracked
new order may already be resting. Every caller's UNPROTECTED/manual-fallback Slack messaging
currently asserts more certainty than this actually establishes (e.g. "place a stop-loss manually,
this position is unprotected" when an untracked order may already be live). **Accepted, not fixed,
by explicit user call** (narrow window: client-side failure right after a real broker success,
layered on top of an already-rare retry case) -- documented in `_submit_replace_with_retry`'s
docstring. Real fix, if revisited: before retrying, check the target order_id's live status at the
broker; if it's already gone (not "still resting"), stop retrying and raise a distinct "ambiguous,
check broker directly" signal instead of the current confident UNPROTECTED alert.

## [live-trading] Partially resolved 2026-07-27 (later) — `.env`'s `SCHWAB_AUTOMATION_TICKERS` trimmed from 29 to the 12 tickers on watchlist 65. Research universe/`trading_universe.db` scope not touched — still open if that broader trim is wanted.


## ✅ [live-trading][coverage] Resolved 2026-07-28 (evening) — coverage snoozes built (time-bounded acknowledgment for a known noisy scenario); UDOW's stale test position retroactively cleaned up
Grew out of reviewing `reconciliation_mismatch`'s 1753 real events, all attributable to UDOW's
deliberately-seeded stale test position (the one known accepted `signals_invariants.py`
violation) — noise on top of an already-understood issue, not a new finding each poll. New
`signals_db.coverage_snoozes` table + `snooze_coverage()`/`is_snoozed()`/`get_active_snoozes()`,
wired into `signals_notify._alert_reconcile_mismatch`: suppresses both the Slack alert and the
`coverage_events` log while an active snooze matches (scoped via nullable `ticker`/`account`/
`node_id`/`kind`), auto-expiring rather than silencing forever. New `scripts/snooze_coverage.py`
CLI. `scripts/coverage_check.py`'s daily `run_check` now skips (not deviates) a snoozed scenario,
since `reconciliation_mismatch` is the sole `DAILY_EXPECTED_IDS` row and would otherwise mint a
false ticket every day it's snoozed.
**Two Opus review rounds found and fixed 4 CONFIRMED bugs**: UTC-vs-local-time mismatch in the
expiry comparison (same trap class as the 2026-07-28 daily-report fix); the daily-check skip logic
itself (first version would still have deviated); **a snooze had no `kind` scope**, so a bare
ticker snooze (the documented UDOW use case) would have also silenced `missing_sl`/
`missing_trailing_sell` — a materially more severe "position may be unprotected at the broker"
alert class, not just the share-count drift being acknowledged — fixed via a new nullable `kind`
column; and the CLI never called `db.ensure_tables()`, so it would crash against a DB predating
this feature. Full detail: `docs/live_test_coverage.md`'s 2026-07-28 (evening) entry. Full suite:
278 passed (was 269). Harness: 7/7.
**Separately, same session: UDOW's real `open_positions` row (id 16, `ira`, opened 2026-07-23,
predates the dry-run-fill-synthesis feature) was backed up then retroactively closed** — tagged
`is_dry_run_sim=1` on both `open_positions`/`trade_log`, closed via `signals_db.close_position()`
at a real current price, `exit_reason='DRY_RUN_RETROACTIVE_CLEANUP'`. `signals_invariants.py`'s
previously-accepted UDOW violation is now genuinely resolved (confirmed clean), not just accepted.
UDOW's `research`-mode node is unblocked to run purely through paper trading going forward.

## ✅ [live-trading][coverage] Resolved 2026-07-28 (later) — daily coverage report ("like pytest, but with the market") now runs inside the live daemon at 7am, checking the previous trading day
Grew out of a conversation mapping every Trade-Flow Accountability Grid row to which canary/live
node should exercise it Monday, once the daemon (down since before this session) restarts. Landed
on a design where a real regression becomes a sticky ticket that keeps re-alerting until a human
explains it or a later genuine pass auto-resolves it — reusing the existing
`scenario_expectations`/`coverage_deviations` contract (already built 2026-07-24, already audited)
rather than inventing a parallel system. New `coverage_event` check method
(`scripts/coverage_check.py::_check_coverage_event`) extends that contract from the 6 canary
`trade_lifecycle` scenarios to the grid's `coverage_events`-backed rows. `active_signals.py`'s
existing 7:00am `_REFERENCE_TIMES` slot now also calls `send_coverage_report(previous_trading_day)`
— a real go/no-go gate before the trading day starts, not a same-day check (which would be
checking a day that hasn't traded yet).
**Two Opus review rounds (first-pass + session-wrap) found and fixed 4 CONFIRMED bugs total**: (1)
UTC-vs-ET date mismatch in the new checker (`date(ts)` vs a local-computed check_date, offsetting
the window ~4-5h — confirmed against 212 real misdated `coverage_events` rows), fixed via
`date(ts, 'localtime')`; (2) a daemon restart any time after 7am (the normal case, since this
project restarts after most edits) would silently skip that day's check forever — no ticket, no
alert, indistinguishable from a clean day in a sticky-ticket model — fixed with an unconditional
startup call mirroring the existing reference-report pattern; (3) the initial seed marked all 14
grid `coverage_events` rows `expected_frequency='daily'`, but replaying real history showed 12 are
trade-conditional (near-zero all-time events) and would have minted 11-14 tickets every single
day, burying the real signal — fixed by trimming; (4) round 2 caught that `cash_check`, one of the
two rows round-1's fix kept as daily, was itself trade-conditional (fired 1 of 3 real days) —
demoted too, leaving only `live_state_reconciliation_mismatch` (itself caveated: depends on UDOW's
known accepted stale-position violation persisting) as the sole daily row; also fixed
`_check_coverage_event` missing ticker/node_id scoping (same bug class as an earlier
`_check_trade_lifecycle` fix), zero live impact yet but defensive before any per-ticker scenario is
seeded. Full detail: `docs/live_test_coverage.md`'s 2026-07-28 (later) entry. Full suite: 269
passed (was 257). Harness: 7/7. `signals_invariants.py`: 1 known accepted violation (UDOW),
unchanged.
**Deliberately deferred/dropped during the same conversation, not built**: `two_nodes_same_ticker_diff_accounts`
test setup (would have needed either repurposing DPST's dedicated paper-vs-real comparison node or
a not-yet-built per-node `dry_run` override — user judged it disproportionate effort for a
fail-open guard at current scale, and noted SOXL will likely end up in >1 account organically
soon anyway, which will exercise it for real).

## ✅ [live-trading][coverage] Resolved 2026-07-28 — closed 12 of 13 remaining `not-instrumented` rows in the accountability grid (38 rows, down from 39 — one dead row removed)
User noticed the grid still had 13 `not-instrumented` rows after the 2026-07-27 evening widening and
asked to close them. 10 got real new `log_coverage_event` instrumentation: `automated_sell_mode_skip`/
`manual_sl_fallback_alert` (`signals_notify._attempt_automated_sell`), `exit_arm_latency`
(`active_signals._scan_pinned_exit_arm`), `node_level_automation_pause`/
`two_nodes_same_ticker_diff_accounts` (`schwab_safety.check_order`), `stale_buy_button_guard`/
`buy_buttons_resolve_correct_node`/`manual_buy_confirmation_account` (the 3 BUY-confirmation Slack
handlers), `buy_fill_reconciles_correct_node` (`signals_notify._reconcile_buy_fill`'s wl_id
disambiguation). 1 was a free row (`oversell_guard_correct_position` → already-logged
`sell_exceeds_position_blocked`, built 2026-07-24, never surfaced). 1 (`open_price_quality`) was
wired to its own pre-existing `open_price_quality_log` table (92 real rows since 2026-07-22) via a
new `compute_status` mechanism instead of duplicating into `coverage_events` — now `verified-live`.
Removed `live_state_reconciliation_design`, a dead row explicitly superseded by the already-built
`live_state_reconciliation_mismatch`. **`position_lock` deliberately left uninstrumented** (user's
call after being asked directly) — proving lock contention passively would require changing
`open_position`/`close_position`'s actual acquire pattern, a real behavior change to
live-trading-critical dedup code, not a side-channel log like the other 12. Session-wrap Opus
review of the diff (`active_signals.py`/`schwab_safety.py`/`signals_handlers.py`/`signals_notify.py`)
found zero confirmed bugs across all 13 new call sites (scope, None-safety, control-flow order,
`bad_results` semantics all checked out); one nit fixed same session —
`manual_buy_confirmation_account`'s `no_account` rows now log `mode="unattributed"` (matching
`gap_resize`'s existing precedent) instead of `_coverage_mode`'s misleading `"dry_run"` fallback.
Full suite: 257 passed (unchanged count, new assertions not yet added — see residual below).
Harness: 7/7. `signals_invariants.py`: 1 known accepted violation (UDOW), unchanged.
`verify_trailing_buy_resolution.py`/`verify_trailing_sell_resolution.py` (AGQ,SOXL) both clean.
**Residual, not done this session**: none of the 10 new call sites got a dedicated regression test
asserting the coverage event itself (same gap already noted for several 2026-07-27 evening sites) —
low priority, the sites are exercised implicitly by existing tests for their surrounding function.

## ✅ [live-trading][coverage] Resolved 2026-07-27 evening — widened the accountability grid from 32 to 39 rows, closing 5 real execution-logic gaps (not just guard logic)
User reviewed the grid built earlier the same day and flagged it as guard-heavy and thin on "did the
actual trade-flow step execute correctly." 2 rows added for free: `paper_entry_fill`/`paper_exit_fill`
— `paper_trading.py` already logs `entry_fill`/`exit_fill` under `mode='paper'` (built 2026-07-18),
it just never had a registry row surfacing it, despite paper trading being the *only* live validation
for all 10 real v5 watchlist tickers today. 5 more needed real new `log_coverage_event` instrumentation,
none of which existed before this session: `kill_switch_block` (does the kill switch actually block a
real order when engaged — `schwab_safety.check_order`), `automated_sell_execution` (does
`_attempt_automated_sell` actually place a real order, not just correctly guard/block —
`signals_notify.py`), `time_exit_trigger` (does a real position's TIME-based exit fire the SELL alert
— `notify_sell_signal`), `buy_fill_reconciled` (does a real detected fill open the position with
correct shares/price, distinct from the existing node-identity-disambiguation row — `_reconcile_buy_fill`),
`morning_report_delivery` (does the report actually post to Slack, not just get built — this exact
report silently posted with zero rows for weeks once already, 2026-07-23, with nothing tracking
delivery). 5 new regression tests (`test_schwab_safety.py`, `test_schwab_automation.py` x3,
`test_part3_gap_resize.py`, new `test_reference_report_coverage.py`). Full suite: 257 passed (was
250). `live_sim_harness.py`: 7/7. `signals_invariants.py`: 1 known accepted violation (UDOW),
unchanged. **Session-wrap Opus review of the real diff (`schwab_safety.py`/`signals_notify.py`)
found zero CONFIRMED bugs** — all 5 new log sites traced as genuinely non-load-bearing (kill-switch
fail-closed intact, `_mode` scoping correct across every early return, no new None-format risk,
`_post_message`'s `(channel, ts)` shape preserved on every path). One real nit found and fixed same
session: `send_reference_report` was logging via `_coverage_mode(None)`, which always falls back to
`"dry_run"` — this would have made `morning_report_delivery` permanently unable to render as
verified-live even after a real successful post; changed to log `mode="live"` unconditionally since
the report isn't scoped to any one account's dry_run flag. Full detail: `docs/live_test_coverage.md`'s
2026-07-27 evening entry.

## [live-trading][coverage] Open, raised 2026-07-27 — Trade-Flow Test Accountability Grid (`pages/14_Coverage.py`, backed by `scripts/coverage_registry.py`) needs filters + direct links to underlying tests
Built 2026-07-27: a 32-row registry of real trade-flow logic branches with status computed live
from `coverage_events`/`coverage_deviations` (never hand-typed) — see `docs/conversation_summary.md`'s
2026-07-27 entry for the full build/rationale (why two competing coverage systems already existed,
why Opus review alone isn't sufficient accountability). Real result at build time: 25 of 32 branches
(not-instrumented + wired-never-fired + attempt-failed) have zero live proof of working.
**Not yet built, raised same session**: the grid is a flat 32-row table with no way to narrow it down
or jump to the actual evidence:
1. **Filters** — by status (e.g. show only `not-instrumented`/`wired-never-fired`, the real gaps),
   by `check_mechanism`, or by free-text search over `code_path`/`scenario`.
2. **Links to underlying tests** — each row's `offline_coverage` field is free text naming a test file/
   function (e.g. `test_schwab_safety.py`) or a script (`live_sim_harness.py::scenario_x`), but there's
   no clickable/copyable path to actually go run or open it. Should resolve to something actionable —
   at minimum the real file path, ideally a button that runs the specific pytest node id or harness
   scenario and shows pass/fail inline.
Not scoped: exact filter UI (Streamlit multiselect/selectbox), whether test-links resolve to file paths
only or something more interactive. Registry itself (`scripts/coverage_registry.py`) is stable and
correct as of this session (250 tests passing) — this item is purely about surfacing/navigating it
better, not fixing its computation logic. **Its computation logic did need real fixing, twice, before
reaching that state**: two accuracy bugs caught during initial build (a `scenario_key` collision
between `paper_trading.py` and the dry_run-sim code sharing `entry_fill`/`exit_fill`; a blocked-vs-
succeeded conflation for `sl_placement`/`top_up`), then a required session-wrap Opus review of the
same-session `signals_db.py` change found 3 more CONFIRMED bugs, most severe being **inverted logic**:
the `scenario_expectations` check-mechanism branch fed unexplained-deviation rows (failures) into the
same good/bad bucketing as `coverage_events`, so an unexplained (currently failing) scenario rendered
green "verified-live" and a clean day with zero rows rendered red — backwards for an accountability
tool. Also found: the same-session `clear_deviation_if_resolved` DELETE→UPDATE change (see the
sticky-deviation entry below) let a genuine new same-day deviation inherit a stale system-authored
"auto-resolved" reason and vanish from the unexplained-deviations report; and `bad_results` only
listed one of several real failure result strings per scenario (`sl_placement`/`top_up`/`gap_resize`
each emit 2-4 distinct failure results, not one). All 3 fixed same session, 6 new regression tests
added, full suite 250 passed (was 244), harness 7/7, `signals_invariants.py` clean (1 known accepted
UDOW violation, unchanged). This is exactly the failure mode the grid was built to guard against in
the first place — a real, live example of why the user wanted evidence-based accountability instead
of trusting AI review alone.
3. **Row grouping** — raised same session, after adding heatmap row-coloring (`STATUS_COLOR`/`_heat_row`
   in `pages/14_Coverage.py`): the 32 rows currently render as one flat list sorted worst-status-first,
   with no grouping by feature area (entry/exit/guards/reconciliation/multi-node) or by which real module
   implements them (`schwab_safety.py` vs `signals_notify.py` vs `active_signals.py`). A logical grouping
   would make the heatmap easier to scan for "which whole area is weak" rather than reading 32
   individual rows. Not scoped: grouping key (by `code_path`'s module, by a new manually-tagged
   `feature_area` field on each `REGISTRY` row, or by `check_mechanism`), whether groups are collapsible
   sections or just a sort/header break within the same table.

## ✅ [live-trading][security] Resolved 2026-07-26 — `signals_invariants.py` built: startup + pre-commit config-invariant checks, 4 checks live. Full detail: `CLAUDE.md`'s Key Files entry.
Mitigates the Opus round-6 "stale-pending-buys guard" finding (still open, still needs live-Slack
testing before the handler itself is fixed) by guarding the config-state precondition instead —
plus 3 more checks from already-known backlog gaps (research-mode ticker still automation-scoped
with a real open position; `mode='live'` node with `account=None`; `brokerage.dry_run` flipping
False while the leverage-inclusive-cash gap is open). 1 known violation currently fires (UDOW's
seeded 2026-07-23 test position) — accepted, not a new bug, safe only because `ira` stays
`dry_run=True`.
Session-wrap Opus review of the diff found 2 CONFIRMED issues, both fixed same session: (1)
MEDIUM-HIGH — the new `run_loop` startup block was unguarded (outside any try/except, before
`_guarded`'s own per-section isolation kicks in), so a transient DB lock or a `log_slack_message`
exception could have prevented the live daemon from starting at all; fixed by routing through
`_guarded("invariants", ...)` and wrapping the `_post_message` call in its own try/except. (2)
MEDIUM — the research-mode-with-open-position check used a ticker-only `get_open_position` lookup,
which would false-positive on the deliberate DPST/GDXU live+research node pairs whenever the
*live* node held the real position; fixed to use `get_open_position_by_wl_id(node['id'])`. Also
noted, not acted on: the UDOW violation re-alerts on every daemon restart with no cooldown/dedup —
acceptable per user's call (it's a known, accepted condition), revisit if it becomes noisy.
`scripts/live_sim_harness.py`: 6/6 scenarios passed, both before and after the fixes.

## [live-trading][security] Idea, raised 2026-07-26, explicitly gated on the wl_id refactor above landing and being observed correct first — per-node `dry_run` override, additive/OR-logic only, never replacing the account-level flag
Grew directly out of the wl_id-refactor design conversation above: once `wl_id`
gives real per-node identity, a node-level `dry_run` parameter becomes
possible where it wasn't safely before. **Deliberately additive, not a
replacement** for the existing account-level `dry_run` (`schwab_safety.
ACCOUNTS[account].dry_run`) — a node's flag can only make things *more*
conservative, never less:
`real_order_allowed = (account.dry_run == False) AND (node.dry_run != True)`
A node-level `dry_run=True` can force a specific node into simulation even
inside an otherwise-live account (e.g. keep a new/unproven node dry even once
its account goes real); it can never force real execution in a `dry_run=True`
account. This bounds the failure mode of a wl_id-resolution bug to "wrongly
forces a node into dry_run," never "wrongly executes a real order" — much
lower stakes than a naive node-replaces-account design, which was rejected
specifically because it would have multiplied the blast radius of the exact
class of ticker-vs-node bug the wl_id refactor above exists to fix.
**Explicitly not to be built alongside or immediately after the wl_id
refactor** — needs the refactor landed and observed correct in the field
first, since this idea's safety case depends entirely on `wl_id` being
correctly threaded through real order placement already.

## [live-trading][compliance] Open, raised 2026-07-25 (3rd time this has come up in conversation, not previously backlogged) — FINRA eliminated the PDT rule/$25k threshold in 2026; re-evaluate daily_order_cap's purpose and confirm Schwab's rollout status
Came up while discussing whether `daily_order_cap` (currently per-*account*, not per-ticker —
`schwab_safety.py:643`/`714`) should exempt SELL orders from *incrementing* the counter (currently
only `is_protective` SL/top-up orders are exempted from being *blocked* by it, not from counting
toward it — a real, separate gap from the already-fixed `is_protective` bypass, found this session,
not yet fixed or scoped). That question was originally framed as a PDT-compliance concern (does
Schwab/FINRA care how many same-day round trips — buy+sell same ticker same day — we do), which
prompted a real web-research check since the user suspected FINRA had changed something here and
this exact topic had already come up twice before without ever getting checked or backlogged.
**Confirmed via research (WebSearch, 2026-07-25)**: FINRA Rule 4210 was amended, SEC-approved
2026-04-14, effective 2026-06-04 (`Regulatory Notice 26-10`) — **eliminates the Pattern Day Trader
designation and the $25,000 minimum equity requirement entirely**, replacing the old "4+ day trades
in 5 rolling business days locks a sub-$25k account out" rule with a risk-based intraday-margin
framework (equity requirements now scale with actual intraday exposure; accounts with as little as
$2,000 can day trade without frequency restrictions under the new rule). **Caveat, not yet
confirmed**: brokers have until 2027-10-20 to fully implement — unconfirmed whether Schwab has
actually rolled this out on our specific accounts as of today (~7 weeks post-effective-date).
**Practical implications, not yet acted on**:
1. If Schwab has already implemented this, the original PDT round-trip-counting concern that started
   this whole discussion may be moot for our accounts — worth confirming directly with Schwab (account
   PDT-flag status) rather than assuming either way.
2. `daily_order_cap`'s design purpose should be revisited regardless of PDT: it was never actually a
   day-trade counter (it counts raw orders, not buy+sell round trips, and it's per-account not
   per-ticker — see the still-undecided per-ticker-vs-per-account item below) — it's a bug-safety
   backstop against a runaway/repeat-order bug, a distinct concern from PDT compliance either way.
3. **The SELL-increment gap found this session is still open regardless of the PDT question** — SELL
   orders (including protective ones) currently still consume the same shared daily counter as BUYs
   (`approve_and_record`'s `today[account] += 1` runs unconditionally for every order/side). Candidate
   fix, following the same precedent as `notional_cap` being made BUY-only (2026-07-24): stop SELL
   orders from incrementing `daily_order_cap` at all, not just exempt protective ones from the check.
   Not yet built — user was mid-decision on this when the PDT tangent came up.
**Action needed**: (a) confirm Schwab's real PDT-framework rollout status on the actual live accounts,
(b) decide the SELL-increment fix independent of that, (c) decide the actual bumped `daily_order_cap`
number for `soxl_ira` (was mid-discussion, not resolved this session either).

## [backtest][live-trading] Research idea, raised 2026-07-25 — monthly universe rescreen (recurring cadence) + a possible "v6" momentum-exhaustion-bounce strategy variant
Two related but distinct ideas from the same discussion:
1. **Recurring monthly rescreen, not a one-time backlog item**: rerun the same kind of full-universe
   resweep/screen used to originally find v5 candidates on a monthly cadence, watching for tickers
   trending positively that aren't on the current watchlist yet. User's framing: this is literally how
   AGQ was found the first time, before its later results soured (`[[project_watchlist_selection_rationale]]`)
   — a standing process, not a single research task. Not scoped: which script/cadence mechanism (cron?
   manual monthly reminder?), what "trending positively" threshold triggers a closer look, whether this
   folds into the existing per-ticker resumable sweep tooling (`scripts/run_sweep_queue.sh`,
   `scripts/campaign_comparison_table.py`) or needs its own lighter-weight screen.
2. **Possible "v6" idea, momentum-exhaustion bounce plays**: distinct from the current z-score
   mean-reversion-on-dislocation strategy family. Observation: a ticker that's had a lot of momentum,
   as it starts losing steam, often gets a technical-analysis-driven bounce (people keep trying to buy
   the dip/breakout continuation even as the real trend is rolling over) — a different signal shape
   than the existing "dislocation from a stable mean" setup. **Core risk-management idea**: if the big
   down days (the real trend continuation, not the bounce) can be avoided via a tight stop (~1%), the
   small bounce attempts might be clippable for alpha even in a name whose longer trend is turning
   negative. Not designed: what defines "losing steam" (momentum indicator, volume pattern, RSI/MACD
   divergence?), how this differs mechanically from the existing z-score entry logic, backtest period
   selection (same overfitting concern already flagged for the "v6" idle-capital-parking idea —
   momentum-exhaustion regimes are likely just as period-dependent). **Not yet named/versioned for
   real** — "v6" is already tentatively claimed by the idle-capital-parking research idea (see the
   2026-07-22 resolved/paused entry below); if both ideas proceed, one will need a different version
   tag. Pure research idea, not scoped or started.

## [live-trading][security] Latent, found by Opus review round 6, 2026-07-25 -- stale-pending-buys guard could silently discard a real manual fill confirmation if a ticker is ever outside SCHWAB_AUTOMATION_TICKERS
Full detail in the round-6 entry above. `handle_entry_price`/`handle_trail_buy_fill_price` (added
round 4, `signals_handlers.py`) both check `any(p['ticker'] == ticker for p in db.get_pending_buys())`
before opening a position, on the assumption that every rendered Executed/Filled button implies a real
pending_buys row exists. That assumption breaks for a live-mode `TrailingExitZScoreBreakout` ticker
NOT in `SCHWAB_AUTOMATION_TICKERS` (`notify_buy_signal` only calls `db.add_pending_buy` when
`trailing_buy or market_buy_eligible`, and `market_buy_eligible` requires automation-scope
membership) -- such a ticker still renders a normal "Executed" button (per `_build_buy_blocks`'s
non-interactive `else` branch), but tapping it would hit the guard and be silently discarded as
"already resolved," with no position opened and no protective stop placed for a real fill. **Not
currently reachable** -- all 9 live TrailingExit tickers are confirmed in `.env`'s automation scope as
of 2026-07-25. **Action needed before this can bite**: either (a) fall back to
`db.get_open_position(ticker)` when no pending_buys row is found, treating "already open" as the only
real "already resolved" signal, or (b) always create a pending_buys row for every alert that renders
an Executed button, regardless of automation-scope membership. Needs live-Slack testing before
shipping either fix -- this session couldn't test the button/modal/handler chain end-to-end.

## ✅ [live-trading][coverage] Resolved 2026-07-26 — dry_run fill synthesis built (see the 2026-07-26 resolved entry above), closing this gap: dry_run does NOT auto-complete without it, which is exactly why the synthesis exists.
`_check_trade_lifecycle` (`scripts/coverage_check.py`) reads `trade_log`/`pending_buys` via
`signals_db.get_closed_trades_for_ticker_on_date`/`get_pending_buys_for_ticker_on_date` — correct
tables given all 6 canaries are currently `mode='live'`, `account='ira'` (not `research`/paper, an
initial reviewer premise that was checked against the live DB and found wrong). But for the 5
TrailingBoth canaries (IVV/QQQ/IWM/DIA/XLF — not VOO, which is `TrailingExitZScoreBreakout`), a real
trade only lands in `trade_log` after the manual "Trailing Buy Order Placed" → "Filled" Slack button
sequence, or via whatever automated fill-reconciliation path exists (`drain_fill_queue` et al.) — **not
yet confirmed whether `dry_run=True` ever auto-completes that sequence without a human tap**. If it
doesn't, the daily coverage check could show a "deviation" (no closed trade) for those 5 canaries most
days even when nothing is actually wrong — a false-positive-generating design gap in the very system
built to eliminate false "something looked off" ambiguity. **Action needed**: trace whether dry_run
auto-produces a fill event for TrailingBoth orders (check `signals_notify.py`'s fill-reconciliation path
against a dry_run account) before trusting `coverage_check.py`'s daily output for these 5 tickers.
Not investigated further same session — user deliberately deferred, heading out.
**User's framing, next session (2026-07-24 evening)**: called this "an important idea" in its own
right, not just a coverage-check caveat — this is really about **how dry_run tests completion** at
all, a question the user had already been independently thinking about (on the train the day before,
re: "how dry_run was going to handle no positions"). A candidate design raised: handle it the same way
paper trading does — **stage some positions as if they'd filled** (rather than requiring dry_run to
wait on a real fill event that may never come from a broker it never actually calls). Not the only
option discussed, just the one floated — pick this up as the first thing next session, per user's
explicit sequencing call.

## [live-trading][coverage] Minor, found 2026-07-24 ~evening via Opus review — record_deviation doesn't refresh expected_outcome on rerun
`signals_db.record_deviation`'s `ON CONFLICT` `UPDATE SET` refreshes `actual_summary`/`ts` but not
`expected_outcome` — if a `scenario_expectations` row's `expected_outcome` text is edited and the same
`(check_date, scenario_key, ticker)` deviation is re-recorded same day, the deviation row keeps the
stale `expected_outcome` value. Low-risk (informational field only, doesn't affect the met/not-met
pass/fail logic or any real trading decision) — backlogged, not fixed same session per user's call.

## [live-trading] Design note, raised 2026-07-24 ~16:20 ET — paper trading and dry_run test genuinely different things, not redundant with each other
`dry_run=True` runs the real order-placement code path (`schwab_safety.check_order` and every real
guard — cash check, `notional_cap`, `daily_order_cap`, duplicate-order guards, automation scope) all
the way to the real broker call, then stops — proves "would this order be correctly placed/blocked,"
never simulates a fill or tracks P&L. Paper trading (`paper_trading.py`) bypasses
`schwab_client`/`schwab_safety` entirely — never exercises the real guard code at all — and instead
simulates the full fill-to-exit lifecycle (bounce-fill timing, arm/trail/SL state machine, realized
P&L) against real price data. **Confirmed by today's actual results**: every single bug found today
(`notional_cap` blocking SELL, `daily_order_cap` starving SL placement, the `brokerage` account-hash
crash, the `add_node` dedup bug) came from dry_run/live activity going through the real guard code —
paper trading, even running, would never have caught any of them. Conversely dry_run never validates
whether the strategy's actual exit logic behaves realistically against real price movement, since it
never simulates a fill at all. **Conclusion**: complementary, not redundant — dry_run validates the
safety/guard layer, paper trading validates the strategy/execution-realism layer. Reinforces why
paper trading being fully dormant (see the entry below) is a real, separate gap, not something
today's dry_run-driven bug hunt already covers.

## [live-trading] Idea, raised 2026-07-24 ~15:05 ET — open a new margin account strictly dedicated to one real production ticker; keep soxl_ira as the standing multi-ticker test account
Today's `daily_order_cap` exhaustion (see entry below) traces to a real account-role mismatch:
`soxl_ira` ran 4 different tickers (GDXD/GDXU/ERY/LABU) through it today for setup convenience, which
doesn't match the real one-account-per-ticker model (`[[project_account_segregation_model]]`) —
`daily_order_cap=3` was sized for one ticker's real daily order volume, not four tickers sharing a
quota. **User's plan**: keep `soxl_ira` as a standing multi-ticker live-test account (not meant to
carry real production trading), and open a **new, separate margin account strictly dedicated to one
ticker** for actual real production trading, matching the intended model properly. Not scoped/actioned
yet — a real account-opening + `schwab_safety.ACCOUNTS` addition to do later, not today.
**Alternative/complementary feature idea, same discussion**: instead of (or alongside) the new
dedicated account, make `daily_order_cap` per-**ticker** rather than per-**account** — explicitly not
framed as a bug fix, but as a real feature needed *if* multi-ticker-per-account usage (like today's
`soxl_ira` test setup) continues rather than being fully replaced by one-account-per-ticker. A
per-ticker cap would let each ticker draw from its own quota, so one ticker's entries/exits/SL-
placements can't starve another's, without requiring a new account per ticker. Not scoped (schema
change to `AccountLimits`, how `check_order`/`schwab_order_counts.json` would need to key by
ticker+account instead of just account) — a real alternative design, worth weighing against the
new-account plan above rather than assuming one supersedes the other.
**User's leaning, same discussion**: if the real discipline is strict one-ticker-per-account
isolation (blast-radius containment, per `[[project_account_segregation_model]]`), then deliberately
blending tickers into one account's "swimlane" — even to solve the order-cap starvation problem —
undermines the reason that discipline exists in the first place. Leaning toward the new-dedicated-
account plan as the real fix, and treating the per-ticker-cap feature as a lower-priority fallback
only worth building if the team ever deliberately chooses to relax single-ticker-per-account
isolation, not as a way to avoid opening new accounts.

## [live-trading][tax] Idea, raised 2026-07-24 ~15:10 ET — run SOXL in both `roth` and `soxl_ira`, but `roth` needs to become limited-margin first
Grew out of the same-day-re-entry discussion (SOXL: ~29% of real historical trades involve a same-day
re-buy — see the `buy_alerted` day-lockout entry above) plus the account-segregation planning above.
`roth` is currently `account_type="cash"` (`schwab_safety.py:141`), so `same_day_block` would
structurally prevent it from ever capturing that same-day-re-entry slice of SOXL's edge — only a
margin/limited-margin account (like `soxl_ira`, `account_type="margin"`) can. **User's plan**: open a
**new Roth IRA with limited margin** (the existing `roth` account is a plain cash IRA) before SOXL
could meaningfully run there alongside `soxl_ira`/a future dedicated SOXL account. Both existing
`roth`/`soxl_ira` accounts are IRA-type, so no wash-sale cross-account concern between them (per the
already-resolved 2026-07-07 finding that IRA-realized losses never trigger wash-sale disallowance).
Not scoped/actioned — real account-opening step for later, tied to the broader account-segregation
plan above rather than a separate initiative.

## [live-trading] Open, raised 2026-07-24 ~10:55 ET — retry `check_gap_resize`'s cancel+replace test properly pre-market on a future day, not mid-day
Today's GDXD/GDXU gap-resize test (docs/live_test_plan_2026-07-24.md) missed its actual goal — neither
ticker had genuinely gapped past its trigger overnight, so `check_gap_resize` correctly did nothing
and the real cancel+replace-with-MARKET code path remained unexercised. Considered manufacturing the
scenario mid-day (place a real trailing-buy now, seed `pending_buys.signal_price` artificially low so
the current price already "clears" the trigger, then call `check_gap_resize()` directly) — **rejected
by the user**: `check_gap_resize` is specifically a pre-market/before-open mechanic (correcting a
resting order based on real overnight price movement before the 9:30 open), and forcing it mid-day
wouldn't test that same thing — the underlying mechanics (cancel-order confirmation, market-buy
placement) are already separately proven via today's other real tests anyway, so a mid-day manufactured
version would add nothing new. **Real action for next time**: retry this properly on a future pre-market
morning — same setup pattern as today (seed a resting trailing-buy + `pending_buys` before market open),
but this time pick a ticker already showing real overnight gap movement (per the KORU 8.26% gap lesson
from today) instead of picking blind and hoping.

## [live-trading] Open, found 2026-07-24 ~10:35 ET — real BUY/SELL Slack alerts carry no canary tag, unlike the Reference Report or paper-trading's console tag
`notify_buy_signal`/`_build_buy_blocks` (`signals_notify.py:359`/`signals_blocks.py:91`, the actual
per-signal alert with buttons) have zero `version=='canary'` awareness — no tag, no visual distinction
from a genuine live candidate signal. Contrast with two places that got this right: (1) the Reference
Report table (`signals_notify.py:1235`) tags canary rows 🧪CANARY, a 2026-07-23 fix specifically
because canary rows becoming visible without a tag was flagged as unsafe; (2) the paper-trading print
path (`active_signals.py:203`) explicitly labels `(paper-trading)`. The per-signal live alert — the
one that actually posts to Slack with real buttons in real time — has neither. **Demonstrated live
today**: IVV's canary BUY signal (10:34 ET, after today's SPY-canary-swap) posted as a plain
"BUY SIGNAL — IVV $742.21 z=-1.23" message, visually identical to a real candidate alert, no
indication anywhere in the message that it's a canary. Action needed: add the same 🧪CANARY-style tag
to `notify_buy_signal`'s console print and `_build_buy_blocks`' Slack message (and check the SELL
alert path, `_build_sell_blocks`, for the same gap). Not fixed same day — found mid test day, low
urgency since canary real-trade buttons are already suppressed separately, but a real "is this real?"
ambiguity risk during any future live session with canaries mixed into the same channel.
**User's broader ask, raised same discussion, taxonomy corrected after discussion**: don't just fix
canary tagging specifically — tag *every* message with its real mode as a standing policy, regardless
of which channel it lands in. **There are only 3 real modes: Live / Dry-run / Paper — Canary is not a
4th mode, it's a deliberately-extreme parameterization of Paper trading** (same underlying mechanism —
`paper_trading.py`'s simulation — just absurd thresholds for fast proof-of-life, per the original
canary design). This reframes the "paper trading fully dormant" entry above too: today's canaries
running as `mode='live'` isn't a separate, unrelated gap — it's the *same* 2026-07-23 mode-flip
collision also breaking canary's intended design specifically, since canaries were meant to run as
paper trades (with extreme params), not live-mode nodes. Rationale for universal tagging: the
channel-routing idea (separate entry above, "split paper-trading/dry-run/live notifications into
separate channels") is the primary fix for noise/ambiguity, but if a message ever gets posted to the
wrong channel (misconfigured routing, a future code path that forgets to route correctly, etc.), a
universal mode tag on the message itself is a defense-in-depth backstop — self-identifying regardless
of where it ends up, rather than trusting channel placement alone. Fold into the same design
conversation as the channel-routing item and the Monday mode-scoping decision; every `_post_message`
call site would need this tag threaded through alongside whatever destination-routing logic gets built.

## ✅ [live-trading] Resolved (confirmed stale 2026-08-01) — paper trading is currently fully dormant system-wide (side effect of the 2026-07-23 all-mode='live' decision, not a deliberate choice); revisit Monday
**Confirmed resolved via later work, not this session**: the 2026-07-25 mode-flip back to `research` for the v5 watchlist nodes restored paper trading. Verified live 2026-08-01: 32 closed paper trades on record (most recent close 2026-07-31), 6 currently open paper positions (YANG/UDOW/KORU/SOXL/USD/HIBL, entered 2026-07-29 through 2026-07-31) — genuinely active, not dormant. Original finding below, for context.
`active_signals.py:199-203`'s BUY-signal routing is a strict if/elif: `mode='live'` → real/dry_run
alert path (`notify_buy_signal`); `mode!='live'` (i.e. `research`) + in `AUTOMATION_ENABLED_TICKERS`
→ `paper_trading.start_paper_buy`. Since every node in the watchlist has been `mode='live'` since the
2026-07-23 "revert live-fire dry-run test state... confirmed intentional" decision (see the superseded
entry below), the paper-trading branch is currently unreachable for every ticker, not just today's
soxl_ira test scope — a system-wide side effect the user hadn't realized until asking today "why am I
not getting anything in paper trading."
**Root cause is two good decisions from different sessions silently canceling each other out**, not a
single mistake: `docs/conversation_summary.md:2214` (~2026-07-18) records the *original* decision —
"Research mode now doubles as the target state for automation-engine dry-run ('paper trading')" —
i.e. keep everything in `mode='research'` as the simplified default, with paper trading as the
standing dry-run layer. The 2026-07-23 decision to flip all 16 nodes to `mode='live'` (justified on
its own terms — safe because `dry_run=True` protects every account except `soxl_ira`) never got
reconciled against that earlier research-mode-as-default decision, so nobody noticed it would also
kill paper trading system-wide as an unintended side effect — **including the 6 canary nodes**, which
per their original design are meant to run as a deliberately-extreme *parameterization* of paper
trading (same mechanism, absurd thresholds for fast proof-of-life), not as a separate concept. There
are only 3 real modes — Live / Dry-run / Paper — canary is not a 4th; today's canaries running as
`mode='live'` is the same root-cause collision misfiring on canary's design specifically, not a
separate gap (see the "tag every message with its mode" entry below for the corrected taxonomy).
Follow-ups user wants to revisit **Monday (2026-07-27)**, not this week:
1. Decide whether some/all research-mode-eligible tickers should go back to `mode='research'` now
   that today's live test day is done, to restore paper-trading coverage.
2. **Real feature idea, bigger than a quick fix**: restructure the if/elif into "do both" — run the
   real/dry_run alert path AND a parallel paper-trading simulation for the same node regardless of
   `mode`, so paper trading runs continuously as a standing regression signal (ties into the
   2026-07-24 "permanent canary tickers in ira" idea above) instead of being mutually exclusive with
   live/dry_run alerting. Would also need `paper_trading.py`'s dedup (currently ticker-only) to
   handle a ticker being tracked both ways simultaneously without colliding.
3. **Compounding gap found same investigation**: 15 of the 24 `mode='live'` nodes (all v5 tickers +
   4 of 6 canaries) have `account=None` — never assigned a real account at all. `check_order` requires
   a real account before it can even evaluate `dry_run` vs `live`, so these fail-closed as `BLOCKED
   ... unknown account 'None'` (confirmed live: the HIBL/IWM "BLOCKED TRAILING BUY" messages this
   morning) instead of producing a useful dry_run simulation. Net effect: with today's `mode='live'`-
   everywhere state, almost nothing produces an informative dry_run walkthrough at all — only UDOW
   has a real account (`ira`), and it's dedup-blocked from new BUYs by its existing position. Fold
   into the Monday mode-scoping decision above — deciding `mode='research'` vs `'live'` per ticker
   should go hand-in-hand with deciding whether it needs a real account assigned.
4. **Process note**: this whole chain (two colliding decisions + the account-mapping gap) slipped
   past the 2026-07-23 session-wrap Opus review, even though that review covered the very session
   where the mode='live' flip happened. Not a failure of that review specifically — a diff-scoped
   review checks the day's actual changes, not a 7-session-old decision it was never shown alongside
   the new one. Worth remembering as a class of gap that kind of review structurally can't catch:
   cross-session decision reconciliation, not single-diff correctness.

**Item 1 resolved 2026-07-25, ahead of the planned Monday date**: all 10 v5-version nodes (watchlist
65, excluding the 6 canary + `soxl_test` nodes) flipped back `mode='live'` → `research` via a direct
`watch_list` update (backfilled into `watch_list_audit`) — restores continuous paper-trading coverage
on the actual live tickers/strategy, matching the original research-mode-as-default design. Canaries
deliberately left as-is; user's read is they don't need coverage diversity the way the real watchlist
does (already non-overlapping ticker set, distinct proof-of-life purpose).

**Item 2 (run-both restructure) dropped, 2026-07-26**: the wl_id refactor already makes the
two-node pattern (one `research` node for continuous paper coverage, a second `live` node in the
real account, same ticker/strategy/params — exactly the DPST pairing added last session) a safe,
zero-code-change way to get both simultaneously. Verified before dropping: `paper_trading.py`'s
dedup is wl_id-keyed, not ticker-keyed (already fixed by the refactor), and `open_position_keys`
(`active_signals.py:530-531`) already merges real + paper positions — so two nodes for one ticker
don't collide. Restructuring the if/elif to run both paths off one node would have duplicated what
adding a second node already gives you for free; not worth building.

**Item 3 (`account=None` gap) reconfirmed resolved, corrected 2026-07-26**: re-checked directly
against the live DB — `get_watchlist()` (`signals_db.py:733`, filters to the active watchlist only,
`watchlist_id=65`, which is all `active_signals.py` ever polls) shows 0 of the current 15
`mode='live'` nodes have `account=None` today. Initially mischaracterized this as "moot because the
affected nodes are gone" — corrected: it was a real, deliberate fix, not an accident of the mode
flips. `watch_list_audit` (ids 168-182, all `2026-07-24 14:15:06`) shows all 16 nodes that had
`account=None` that day (10 v5 + 6 canary) were explicitly backfilled `account None -> ira` via
direct `edit_node` updates — an operational patch applied after the gap was found live, same day,
not a code-level guard. **Residual, not yet closed**: `add_node` still has no validation preventing
a *future* `mode='live'` call from omitting `account` again — the fix closed the specific instances,
not the class of bug. Low priority (hasn't recurred since; every live-node-creation script since has
passed `account` explicitly) — worth a cheap guard (warn or reject `add_node(mode='live',
account=None)`) if it's ever worth the effort, not urgent enough to block anything.

## [live-trading] Far-backlog, raised 2026-07-25, deprioritized same day — v5 watchlist skews long-only, consider adding inverse counterparts
All 10 v5 tickers are one-directional leveraged longs (SOXL, GDXU, NUGT, HIBL, KORU, DPST, UDOW, USD,
AGQ) except YANG (the only inverse ticker) — the whole book loses together in a broad leveraged-long
selloff, unlike the old v4 set which had some hedge-like pairing (EDC/AGQ as SOXL/KORU hedges, per
`[[project_watchlist_selection_rationale]]`). That portfolio-balance logic didn't carry over into the
v5 selection, which picked per-ticker on cliff-safe robust alpha only.
**Original motivation, clarified same day**: the real point of pairing isn't just directional balance
in the abstract — it's two concrete operational goals once manual live trading resumes: (1) a genuine
hedge (a down move in one leg is offset by the paired inverse leg), and (2) **guaranteed fill
activity** — pairing a long with its inverse means at least one leg of the pair is likely to be
signaling/filling at any given time, addressing the same sparse-signal problem that motivated flipping
v5 back to research mode this session (too few live orders to spread tests/validation across).
**User's call, same day**: pushed to far-backlog, not near-term. Two reasons: (1) the mean-reversion
strategy itself already makes money in both up and down periods (z-score breakout on a dislocation,
not a directional bet), so long-only isn't the raw exposure risk it looks like at first glance — a
50/50 long/short allocation would be a seismic reallocation shift, not a quick tweak; (2) there isn't
a good backtest period to properly optimize/validate a bear-pair selection without overfitting to
whatever the sample period happened to contain (same class of problem as the "v6" idle-capital-parking
work's overfitting issue, `[[project ... v6 parking]]`/see the resolved 2026-07-22 entry). Worth
researching eventually, but needs a real design conversation (allocation split, which counterparts,
how to validate without overfitting) before scoping, not just grafting existing v4 inverse nodes on.

## [live-trading][security] Elevated priority 2026-07-24 morning — real evidence that neither notional_cap nor the cash check would catch a same-account double-buy
Confirmed empirically today, not just theoretical: placing two real resting `TRAILING_STOP` BUY orders
(GDXD 5sh, GDXU 3sh) left `get_account_balance('soxl_ira')` completely unchanged ($1,110.43 before and
after) — contradicts the assumption in `_has_open_buy_order_in_account`'s own docstring that Schwab
reserves buying power for a resting order (that finding was based on a bounded-price order the night
before, not a `TRAILING_STOP`). Practical consequence, walked through in detail this morning: in a
same-ticker double-buy scenario (e.g. `check_gap_resize` placing a replacement order while the original
somehow also still resolves for real — see the `cancel_order` confirmation fix above), **neither**
`notional_cap` (checked per-order, not cumulative) **nor** the real cash-availability check (sees the
undecremented balance, since the resting/just-filled original doesn't seem to move it) would catch it —
both individual orders are small enough to independently pass. This is the same structural gap already
flagged 2026-07-22 for the *multi-ticker-sharing-one-account* case
(`_has_open_buy_order_in_account`'s docstring), now confirmed to apply to the *same-ticker* double-buy
case too, and confirmed with real data instead of just reasoning about it. **User's call: "not good
enough"** — this needs an actual fix, not just a documented gap, before scaling beyond today's
single-ticker-per-scenario test. Candidate fix (not designed yet): a per-ticker cumulative same-day BUY
notional cap (ties into the already-backlogged 2026-07-22 "max cumulative BUY notional per ticker per
day" item, which was scoped for a different but related runaway-repeat-buy scenario — worth designing
both together).
**Existing partial mitigation, confirmed insufficient**: `signals_helpers._last_sale_recovery` already
sizes each order off the ticker's last-closed-trade proceeds (or `starting_notional` if none yet) — a
real soft ceiling against any *one* order being wildly oversized, used by both `buy_order_sizing` and
`check_gap_resize`'s replacement sizing. But it's per-order, not cross-order: for a ticker with no trade
history (like today's GDXD/GDXU), it falls back to the same `starting_notional` target for *any* order
attempt, so two independent, individually-reasonable-sized orders for the same ticker (the double-buy
case) would each pass it and still double real exposure together. Doesn't close the gap.

## [live-trading] Idea, raised 2026-07-24 morning — permanent canary tickers in the dormant `ira` account as a standing delayed-regression test
Insight from today's real `soxl_ira` test day: the canary-node pattern (deliberately inert
proof-of-life tickers, absurd thresholds, real Slack alerts but suppressed real-trade buttons) is
genuinely valuable beyond one-off testing. Since the existing `ira` account isn't supposed to carry
real trading activity anyway (manual live trading paused since 2026-07-18), it could permanently host
a small canary set — a standing, always-on regression signal that the full pipeline (signal
detection, Slack alerts, order-construction code paths) still works, without needing a dedicated test
day each time. Not scoped yet — which tickers, how many, whether `dry_run` stays True forever for
this account or gets a narrower always-real canary lane. Design conversation for later, not
blocking today's test plan.

## ✅ [live-trading] Resolved 2026-07-26, confirmed stale 2026-08-01 — Morning Report block-limit failure regressed (23 nodes now), fix via ticker-scope reduction not another patch
**Resolved via a different path than the user's original call below**: not a ticker-list trim, but real chunking + threading (`signals_blocks._post_chunked`, wired into `send_reference_report` and the signal-window alert path) — confirmed present in the code 2026-08-01. This handles an arbitrary node count robustly regardless of watchlist size, so the underlying failure mode (`invalid_blocks`) can't recur even without trimming the list. Original finding below, for context.
The 2026-07-23 fix (collapsing each row's actions blocks) worked for 16 nodes; adding 7 more
`soxl_test` nodes this morning (SPY/SH/ERX/ERY/LABD/GDXD/GDXU) pushed the reference table back over
Slack's 50-block cap — `invalid_blocks` again, report failed to send entirely at today's restart.
**User's call**: don't patch the block count again — the real fix is trimming the live/canary ticker
list down to the realistic ~3-4 tickers actually trading at once, which naturally keeps the report
under the limit. Revisit once the `soxl_test`/canary sprawl is cleaned up post-test-day, not before.

## [live-trading] Open, raised 2026-07-24 morning — signal/reminder alerts don't show dry_run status, only real order-placement messages do
Found live: a stale (pre-restart) daemon fired a real `SELL SIGNAL — STOP LOSS` alert for the
newly-seeded SPY test position and sat "Waiting for Slack response (Exited/Skipped)" — required a
manual real-order-book/position check (`schwab_safety._open_orders`, `schwab_client.get_real_position`)
to confirm nothing was actually placed, purely because the alert itself gave no indication of the
account's `dry_run` state. `[DRY RUN]` is already prefixed on the real order-placement messages
(`_place_equity_order`/`_place_trailing_order`/`place_stop_loss` in `schwab_client.py`), but the
earlier signal/reminder alerts (`notify_buy_signal`, `notify_sell_signal`, trailing-buy/reminder
messages in `signals_notify.py`) fire before any placement attempt and say nothing about it.
**Action needed**: surface the account's real `dry_run` flag (or "🧪 TEST" style tag for a
non-production node/account) directly on these earlier alerts too, not just the placement
confirmation — cheap, avoids exactly this kind of "is this real?" ambiguity during real trading days.

## [live-trading] Open, raised 2026-07-24 morning, CORRECTED 2026-07-24 ~10:15 ET — split paper-trading/dry-run/live notifications into separate channels (or otherwise reduce chattiness)
User's real complaint: the single trading Slack channel is getting noisy alongside today's real
`soxl_ira` dry_run=False test activity and the stale-daemon SPY/SH signals from this morning — hard
to tell what's actually real at a glance (compounds the dry_run-visibility gap above). **Correction**:
this entry originally assumed "10 real v5 + 6 canary nodes, all research-mode" were the noise source
via paper trading "running continuously" — that assumption is wrong. Confirmed today (see the
"paper trading fully dormant" entry above) that every node in the watchlist is actually `mode='live'`,
not `research`, so paper trading isn't running at all right now — the actual noise source today is
real/dry_run alerts across many simultaneously-live nodes (soxl_test + v5 + canary, all `mode='live'`),
not a paper-trading/live mix. The channel-routing need still stands (still hard to tell what's real
at a glance across dry_run vs. live vs. eventually-paper messages), but the framing of *why* was
wrong. **Action needed**: design a channel-routing scheme (e.g. separate channels/webhooks per mode,
or a consistent filterable tag prefix per message) so the real trading channel isn't drowned out —
still needs a design conversation, not a quick fix, since every `_post_message` call site would need
a mode-aware destination. Revisit once the Monday paper-trading-mode decision (see entry above) is
made, since that'll add a third real message source back into the mix.
**Further refined 2026-07-24 ~16:15 ET**: today's chattiness is a **shakeout-phase** artifact, not
the permanent target state — many tickers/canaries/tests all live at once, actively being debugged.
Once the real watchlist narrows to its actual production size (1-3 tickers genuinely live-trading),
the **live channel specifically** should get quiet/focused again — real trading shouldn't be noisy.
Other channels (paper/canary/ongoing-test activity) can stay as chatty as needed, since that's where
exploratory shakeout work belongs. So the channel-routing design should explicitly account for this
trajectory (loud-now/quiet-later on the live channel specifically) rather than just splitting by mode
as a static end-state — ties directly into the coverage-system reframe above, which is itself meant
to be the shakeout-phase tool that eventually lets the live channel go quiet with confidence.

## [live-trading][security] Open, raised by session-wrap Opus review 2026-07-23 night — `availableFunds` is leverage-inclusive for a real margin account, unverified whether that's safe for `brokerage`
`schwab_client.get_account_balance`'s new fallback (`cashAvailableForTrading` → `availableFunds`,
see the balance-bug fix below) is confirmed correct/fail-closed for `soxl_ira` (a *limited-margin*
IRA — no leverage, `availableFunds ≈ real cash`). Flagged, not fixed: for the real taxable
**`brokerage`** account (`schwab_safety.py:139`, genuine Reg-T margin), `availableFunds` reflects
cash plus loan value of held marginable positions minus margin requirement — i.e. it can exceed
settled cash. `check_order`'s cash-availability check is meant to be a conservative real-cash gate;
against `brokerage` it could currently pass a BUY partly funded by borrowing rather than settled
cash. Bounded today by `dry_run=True` + `notional_cap`/`CASH_SAFETY_BUFFER`, so not urgent — but
should be resolved (e.g. prefer a settled-cash-specific field for `account_type=="margin"`) before
ever flipping `brokerage` to real trading. Also flagged: `scripts/live_sanity_check.py`'s own copy of
the same fallback logic (line 67) is duplicated inline rather than calling `get_account_balance` —
justified (the script fetches balance+positions in one call, deliberately bypasses
`schwab_client`/`schwab_safety`), but a real future drift risk if the field-name fallback ever needs
to change again. A shared small helper (e.g. `_extract_available_cash(balances)`) would remove the
duplication cheaply.

## [live-trading] Open, planning in progress 2026-07-23 night — Friday 2026-07-24 real-account test plan on the new limited-margin IRA
Superseding the "revert live-fire state" framing below: user confirmed 2026-07-23 night that
`mode='live'` on all 16 `watchlist_id=65` nodes and the disengaged kill switch are **intentional**,
not leftover test state — safe because every account in `schwab_safety.ACCOUNTS`
(brokerage/sep/roth/ira) stays `dry_run=True`. The fake UDOW position (740 sh @ $67.55,
`account='ira'`, armed `trailing=True/peak=$69.57`) is also being left in place — that account
isn't part of tomorrow's plan and stays dry_run=True, so no real order can result from it.
**Real plan for tomorrow**: the user manually pre-staged two *real* positions in the new
limited-margin IRA today (outside our system) — SPY and SH (inverse S&P 500, -1x) — specifically
so arm/trailing-sell logic can be exercised against real live positions. Wants to test "as much as
possible" given the account only holds ~$5k. **Not yet done, blocking everything else**: the new
IRA account has **not been reauthenticated/reconnected** to the Schwab API yet — user hasn't
confirmed whether its token scope + compliance trading permission have actually cleared (last
known status 2026-07-22: still blocked on both). Given `scripts/schwab_oauth_setup.py` to run
themselves in their own terminal tonight; it'll show how many accounts are linked, which is the way
to confirm whether the new IRA is visible yet. **Still needed before Friday, once reauthed confirms
the account is visible**: (1) add the new IRA to `schwab_safety.ACCOUNTS` (account_type='margin' per
the existing "limited margin IRA = no same-day cash-settlement restriction" convention, dry_run
decision TBD), (2) real account-number suffix, (3) seed `open_positions` rows for the real SPY/SH
positions (real share counts/entry prices, pulled from the account or user-provided) with a node
config appropriate for exercising arm/trailing-sell, since neither ticker's existing watchlist-65
node fits as-is (SPY's is a deliberately-inert `canary` node; SH isn't on the watchlist at all),
(4) decide dry_run scope precisely — leaning only the new IRA goes `dry_run=False`, nothing else.
**Not fully planned yet** — user wants to think through this further before anything is built;
treat as a design conversation to finish, not a go-ahead to implement.

## [live-trading] Open, raised 2026-07-23 night — Schwab auth flow is "clunky", no cleaner unattended path designed yet
User's real complaint: when the 7-day token goes stale, there's no interactive capability inside
`active_signals.py` to complete the OAuth login (correctly, by design — headless daemon can't
answer a browser prompt) — the only path is running `scripts/schwab_oauth_setup.py` manually in a
separate terminal, and since `schwab_client._get_client()` caches the client in a module-level
global (`_client`, set once, never invalidated), even a successful manual reauth doesn't take
effect until the daemon process is **restarted** — an easy thing to forget mid-trading-day. Not
scoped yet: whether the fix is a Slack alert reminding to restart after any reauth, a way to force
`_client` to reload without a full daemon restart, a pre-market token-expiry check/warning, or
something else. **Action needed**: design conversation before building anything.

## ✅ [live-trading][security] Resolved 2026-07-22 — stale-cache race at market open (HIBL paper trade entered and SL'd in 31 seconds); `_current_price()` now rejects cache older than today at market open. Full writeup: `docs/research_log.md`'s 2026-07-22 "HIBL paper trade" entry.


## ✅ [live-trading][security] Resolved 2026-07-23 — Morning Report silently rendered empty for weeks (mode filter), then broke outright once fixed (Slack block-limit); both fixed. Full incident writeup: `docs/research_log.md`'s 2026-07-23 entry.


## [backtest] Open, paused 2026-07-22 — "v6" idle-capital parking idea; inconclusive, downturn-specific follow-up queued
Full detail in `docs/research_log.md`'s 2026-07-22 entry. Short version, in the order the
analysis actually evolved this session:
1. **First pass (restricted to EOD-exit gaps only, 62 windows across all 10 watchlist-65
   tickers)**: no vehicle showed a robust edge. SPY itself lost money (-5.4% compounded);
   the "top 30" leaderboard (TSLQ, SARK, WEBS, BERZ, SQQQ, ...) reversed sign completely in an
   out-of-sample 50/50 split check — clean overfitting, not a real effect.
2. **Broadened to every exit→next-entry gap, not just EOD ones** (776 windows) — the EOD-only
   restriction was itself the flaw (idle capital exists between any two trades, not just
   overnight ones). Re-scanned; had to exclude the 10 source tickers from the candidate pool
   (scoring e.g. KORU against KORU's own gap windows measures "should we not have exited," a
   different question, not the parking one — this had been silently inflating the first
   broadened run's top result to a nonsensical +68,113%).
3. **Corrected broadened result**: SPY-long is now sign-consistent positive across a 50/50
   split (+65.9%/+123.2%, both halves positive) — better than the first pass, but still ~50%
   win rate, so the compounded number is carried by a few big windows, not a consistent
   per-window edge. Short/inverse SPY (SH/SDS/SPXU/SPXS) all lost consistently in both
   halves — the mirror image, as expected.
4. **Not yet run**: check whether SPY-long's apparent edge is really just "the market went up
   most of this period" by scoring separately during real identified SPY drawdown episodes
   (`>=5%` from trailing peak) vs. everywhere else — 11 episodes identified in the 2023-08 to
   2026-07 window, notably 2023-09-21→2023-10-11 (-7.9%), 2023-10-18→2023-11-06 (-10.3%),
   2024-08-02→2024-08-13 (-8.4%), 2025-03-06→2025-05-12 (-19.0%, the big one), 2026-03-19→
   2026-04-08 (-9.1%). If the "hedge" thesis (our exits cluster around bad market days) is
   real, SPY-long should do *worse* and inverse should do *better* specifically inside these
   windows than outside them — not yet checked.
**Action needed**: run the downturn-episode-specific scoring (candidates already narrowed to
SPY/QQQ/SH/SDS/SPXU/SPXS/SQQQ/QID/BIL from this session's discussion) before drawing any
further conclusion. Scripts ready: `scripts/sim_v6_parking_vehicle_sweep.py` (windows already
cached in `output/v6_gap_windows.csv` with the broadened 776-window definition),
`scripts/sim_v6_split_check.py --split 50-50`/`70-30`/`5fold` (supports arbitrary fold configs).

## ✅ [live-trading][security] Planned for Friday (2026-07-24 WFH day), confirmed run 2026-07-23, closed out 2026-08-02 — real-account sanity tests: oversized BUY + naked SELL across several tickers, on the new limited-margin IRA (soxl_ira) only
**Closed 2026-08-02**: this was mistakenly reopened in `backlog_cache.md` as "never run" — it was in fact run for real 2026-07-23 (a day early), logged to `coverage_events` as `sanity_oversized_buy`/`sanity_naked_sell`, both confirmed real Schwab rejections. Also reconfirmed independently 2026-08-02: naked-sell is structurally guaranteed to reject by account mechanics alone (IRA disallows shorting entirely; a normal SELL in a margin account without shares also just rejects — shorting requires an explicit distinct order type this code never issues), so there was never a live-only unknown there to begin with.
Built this session (uncommitted): `scripts/live_sanity_check.py` — standalone, bypasses
`active_signals.py`/`signals_notify.py`/`schwab_safety` entirely (calls the schwab-py client
directly via account-suffix resolution, not nickname, since the new IRA isn't in
`schwab_safety.ACCOUNTS`/`NICKNAMES` yet) — same pattern as the manual $200k buying-power test
(2026-07-17). Two tests: `oversized_buy` (BUY 50x+ more shares than real cash affords, expect
Schwab rejection at placement) and `naked_sell` (SELL 1 share of a ticker held at 0, expect
rejection, not an accidental short — script hard-aborts if real shares are actually held).
Requires per-ticker typed confirmation (type the ticker again), never loops/retries, logs every
outcome to `coverage_events` (`scenario_key=sanity_oversized_buy`/`sanity_naked_sell`, `mode='live'`).
**User confirmed 2026-07-22**: only the new limited-margin IRA is going live (not
brokerage/sep/roth/ira yet) — but multiple tickers can be sanity-tested in sequence against it
since each test's notional/quantity is small/rejected by design. **Scope decision, same day**: only
run the two guaranteed-safe rejection tests for now; hold off on the deeper small-real-fill test
menu (tiny real BUY + SL placement + trailing-stop + `get_real_position` field check + sell, ~5
more tests, each closing a specific "unverified against a real account response" gap in
`docs/live_test_coverage.md`) until after seeing how the rejection tests land.
**Blocker**: user needs a WFH day to be present for this (can't run during market hours while at
work) — **planned for Friday 2026-07-24**. Also still needs: the new IRA's real account-number
suffix, and confirmation that its API token scope + compliance trading permission have actually
cleared (last known status 2026-07-22: still blocked on both) — check before Friday, not after
showing up to run the script.

## [live-trading] Idea, raised 2026-07-22, not designed — small (10-share) real EDC pilot position, held ~1 month to shake out issues before scaling
Distinct from the sanity-check tests above (those are one-shot, expect-rejection tests; this
would be a real, sustained, small live position). Not yet scoped: which account (the new limited-
margin IRA, presumably, matching "only going live on 1" above, but not confirmed for this
specifically), which node/strategy config (EDC's `watch_list` row was removed entirely 2026-07-19 —
would need a fresh node, not a revival of the old v3.27 one, since that's tied to the existing
manual 423-share position being unwound separately), and whether it runs through the real
automation path (`mode='live'`, in `AUTOMATION_ENABLED_TICKERS`, real Slack BUY/SL/trailing flow)
or is a one-off manually-placed position just monitored for drift. **Action needed**: design
session before building anything — account, node config, sizing override (10 shares fixed, not
the usual `starting_notional`-driven formula), and automation scope.

## [live-trading][tax] Open question, raised 2026-07-22 — which ticker (SOXL vs AGQ) goes to the taxable brokerage account first; wash-sale cross-account mechanic needs one more confirmation
Real comparison pulled this session (v5, `open_check`, `scripts/campaign_comparison_table.py
--tickers AGQ SOXL`): AGQ's winner is TE (`TrailingExitZScoreBreakout`, best alpha 1114.9%,
worst_neighbor 285.2, win rate 31.2%, 128 trades, no trailing-buy order to catch manually,
no dividends — cleaner tax treatment); SOXL's winner is TB (`TrailingBothZScoreBreakout`, best
alpha 1212.1%, worst_neighbor 153.9, win rate 9.9%, 151 trades, pays small dividends). **Real,
practical gap**: AGQ's liquidity (1% ADV ≈ $2.0M/day) is drastically thinner than SOXL's (≈
$136.0M/day) — a $50k AGQ order is a much bigger fraction of daily volume, closer to the
HIBL/EDC thin-liquidity fragmentation case than to SOXL. Leaning AGQ for the tax/win-rate/cliff-
safety reasons, capped by how large it can scale; not a final decision.
**Wash-sale mechanic reconfirmed this session, not fully resolved**: an IRA/SEP/Roth-realized loss
never triggers a wash sale at all (no recognized loss to disallow — resolved 2026-07-07). Only a
loss realized in a real taxable account matters, and it's direction-specific: rebuying in the
*same or another taxable account* just defers the loss (recoverable via basis); rebuying in an
*IRA-type account* within 30 days permanently disallows it (Rev. Rul. 2008-5). The one AGQ loss on
file (2026-07-07, in brokerage, clears 2026-08-06) is the safe/deferred case if AGQ is bought back
in brokerage itself — **not yet confirmed**: whether there's an AGQ loss in some *other taxable*
account (not sep/roth/ira, which don't count) that isn't in this file. **Action needed**: user to
confirm whether any such other-taxable-account AGQ loss exists before finalizing the brokerage
ticker decision.

## [live-trading][security] Not started, discussed at length 2026-07-22 — max cumulative BUY notional per ticker per day (backstop against a repeat-buy/runaway bug)
Raised while reviewing this session's order-submission retry mechanism: none
of the existing guards actually bound total same-day BUY exposure to a
single ticker. Walked through each one and confirmed the gap is real:
`notional_cap`/`HARD_ORDER_CEILING` are per-*order* only; `daily_order_cap`
counts orders per *account*, not per-ticker notional; the resting-order
guard (`_has_open_order`) offers no protection against repeated **market**
buys specifically, since a market order fills almost instantly — by the
time a second buy attempt fires, the first is already `FILLED` (terminal,
excluded from the open-orders check), so nothing stays resting for the
guard to catch; the duplicate-order guard only catches a retry within
`DUPLICATE_ORDER_WINDOW_SECS` (60s) and `DUPLICATE_ORDER_QUANTITY_TOLERANCE_PCT`
(5%) of the same quantity — a bug that re-sizes each attempt or fires more
than 60s apart slips past it entirely. The cash-balance check doesn't cover
it either: it bounds total *account* capital, not intended *trade size* —
an account can (and with compounding, is expected to) hold materially more
cash than one ticker's `starting_notional`, so a 4x-sized repeat-buy bug
could still pass the cash check every time.

**Design converged on, not yet built**: a flat multiplier on
`watch_list.starting_notional` (not a fixed dollar figure, so it scales per
ticker automatically) — leaning **1.1x** for the normal case. **Dynamic
case**: if a same-day sell already closed the position for this ticker
(this strategy always exits fully, so "the most recent same-day sell" is
unambiguous, no need to sum multiple), base the cap on that sell's real
notional (shares × exit price) instead of the static `starting_notional` —
tracks recycled capital rather than a plan-time number that may be stale
relative to actual account growth. Leaning **~1.0-1.05x** the sell notional
for that case specifically (not stacked on top of the normal 1.1x
allowance) — 1.0x exactly is workable but would occasionally trip on the
existing same-day sizing pad (`buy_order_sizing`'s ~1% `pad_pct`/
`market_pad_pct` overshoot) for a perfectly legitimate re-buy, requiring a
rare manual override; 1.05x absorbs that pad without ever blocking a normal
re-buy. **User wants to think about this further before committing to
a number or building it** — explicitly asked to backlog rather than
implement now. On breach: block + Slack alert (not also tripping the kill
switch — that idea was floated and dropped in favor of the simpler
per-order block).

## [live-trading] Low priority, idea raised 2026-07-21 — should `CASH_SAFETY_BUFFER` scale with order size instead of staying a flat $200?
Raised while fixing the buffer amount itself (see the resolved cash-check item
below — flipped from an accidental $1,000 hard requirement to the intended
$200 per-order overage cushion, with the user's real ~$1,000 reserve habit
kept separate and unenforced). Open question, not scoped: a flat $200 covers
fee/price-tick overage fine for a modest order, but may be too small (as a
fraction of the order) for a large notional, or unnecessarily conservative
for a tiny one. Possible shape: buffer as a percentage of `notional` with a
floor, or keyed off the ticker's typical bid-ask spread/volatility rather
than a single constant. Per `automation_principles.md` #7a (prefer simple
over precise when precision doesn't buy much), don't build this unless a
real case shows the flat $200 actually causing a problem — no evidence of
that yet, every account still `dry_run=True`.

## [live-trading] Implemented 2026-07-21, not yet live-tested — Entry Trigger/Fill/SL-Placement/Arm-latency automation for TrailingExitZScoreBreakout (Part 4)
Full design at `/home/pkim/.claude/plans/replicated-gliding-quasar.md` (not
git-tracked); implementation summary in `docs/design.md` (Layer 3, "Part 4"
entry). Automates the 6 `TrailingExitZScoreBreakout` watchlist-65 tickers'
(AGQ, DPST, KORU, NUGT, UDOW, YANG) previously fully-manual BUY flow: pinned
single-shot entry/exit-arm checks (`active_signals._PINNED_BAR_TIMES`,
`:30:02` each hourly bar boundary) replacing ambient 5-min polling,
`schwab_client.get_session_open_price()` (Schwab's real `openPrice`) for
detection-drift-free open-check entries, `_attempt_automated_market_buy`/
`notify_buy_signal`'s new branch for real market-order placement, a genuine
resting STOP order (`schwab_client.place_stop_loss`, new `open_positions.
sl_order_id` column) placed via a synchronous fast-confirm step immediately
after fill (real gap, not previously automated for any ticker — ~70-80% of
this strategy's trades exit via SL), and `_scan_pinned_exit_arm` collapsing
the trailing-sell arm-detection latency from up to 5 min to ~2s. Also found
and fixed while implementing: `schwab_safety._OPEN_CHECK_WINDOWS` was one
minute too late for the new pinned checks (`(9,31,...)`→`(9,30,...)`), and a
real `signals_compute.compute_buy_signal` bug where `prev_close` read the
unsliced `df_daily` (today's/latest close) instead of `df_daily_prior` — found
via the new backtest-replay verification script, see `docs/research_log.md`'s
2026-07-21 "Part 4 implementation" entry for the full writeup.
**A phase-by-phase scenario review the same day (continuing session) found and
fixed three more real bugs**, see `docs/design.md`'s Part 3 entry for full
detail: (1) the SL price was anchored to the real fill price instead of the
trigger/signal price, diverging from the backtest's stop formula whenever a
market order slipped; (2) the async fallback fill-detection paths
(`check_auto_fills`/`drain_fill_queue`/`check_gap_resize`) never actually
placed the SL if the synchronous fast-confirm path timed out, contradicting
the timeout alert's own claim; (3) Part 3's post-fill top-up never placed a
real broker order at all, only DB bookkeeping — fixed alongside a
`schwab_safety` duplicate-order-guard tightening (now quantity-aware, ±5%
tolerance) needed once the top-up started actually submitting orders.
Verified: full `pytest tests/` (137 passed, was 107 pre-session);
`scripts/verify_pinned_entry_vs_backtest.py` (Deliverable 1) — 5/6 tickers
100% clean, AGQ's one mismatch is a real, expected stock-split-day divergence
(see research log entry), not a defect; `verify_trailing_buy_resolution.py`/
`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` clean post-fixes.
**Not yet done**: Deliverable 2 (`scripts/verify_open_price_quality.py` +
`open_price_quality_log` table) needs real trading-day data to populate — the
daemon hasn't run live with this code yet; no real order tested end-to-end
(every account still `dry_run=True`); `dry_run=False` flip pending both.

## [live-trading] Implemented 2026-07-21, not yet live-tested — trailing-buy budget adherence (Part 3: padded sizing + overnight gap guard + post-fill top-up)
Full writeup in `docs/design.md` (Layer 3, "Part 3" entry) and `docs/research_log.md`'s
2026-07-21 design entry. Resolves both the "trailing-buy needs re-sizing" and
"gap-through-trigger fill optimism" backlog items with one unified design:
same-day sizing pad (`buy_order_sizing(node, sig, pad_pct=1.0)`), a pre-open gap
guard (`signals_notify.check_gap_resize`, fired once daily from `active_signals.
run_loop` at `_GAP_CHECK_WINDOW=(9,15,9,29)` — cancels+replaces a resting trailing
buy with a plain MARKET order only if the overnight gap already cleared the
trigger), and a post-fill top-up (`_reconcile_buy_fill`/`_reconcile_fill`, fed by
both the existing `check_auto_fills` poll and a new account-activity websocket,
`schwab_stream.py`). All 8 implementation tasks done; full `pytest tests/` green
(107 passed, 9 new), `verify_trailing_buy_resolution.py`/
`verify_trailing_sell_resolution.py --tickers AGQ,SOXL` clean, `pending_buys.order_id`
migration confirmed additive-only against a `trading_live.db` backup.
**Not yet done**: no real order has been tested end-to-end (every account is still
`dry_run=True`); `schwab_stream.py`'s account-activity payload parsing is unverified
against a real fill event — confirm both before trusting this beyond dry-run.
Also open: confirm with Schwab whether the limited-margin IRA falls under FINRA
Rule 4210's new intraday-margin cure mechanism (user will confirm directly, or a
test run can help — Schwab typically emails a notification for this); once
confirmed, rerun the pre-market-to-open drift check against Schwab's own quote
feed (not yfinance) to make sure the flat 5% gap-guard pad is still well-calibrated.

## ✅ [live-trading][backtest] Resolved 2026-07-21 — SOXL's watchlist-65 node stays TrailingBoth
Real comparison (v5, `open_check`, robust alpha): TB (sl2 tp30 trail_buy3.0 h70 w10 z1.0)
1212.1% best alpha, worst_neighbor 153.9; TE (sl2 tp26 trail_sell8.0 h119 w10 z1.0) 947.0%
best alpha, worst_neighbor 28.0. TE's edge was operational ease (no trailing-buy order to
catch manually) at the cost of ~22% alpha and a much thinner cliff margin. **User decision:
keep TB** — that operational-ease motivation is exactly what Part 3 (trailing-buy budget
adherence automation, above) resolves directly, so there's no reason to give up the extra
alpha. No watchlist/`signals_db` change made.

## [live-trading][tax] Active hold, set 2026-07-20 — don't buy GDXU/AGQ in any IRA-type account before their wash-sale clearance dates
Real taxable-brokerage-account losses found while discussing account mapping for
watchlist 65: GDXU lost in brokerage on 2026-07-06 (clears **2026-08-05**), AGQ lost
in brokerage on 2026-07-07 (clears **2026-08-06**). Per IRS Rev. Rul. 2008-5 (the
same mechanic resolved 2026-07-07 — see `project_wash_sale_holds.md`), buying either
ticker in any IRA-type account (ira/sep/roth, or the new limited-margin IRA) before
its clearance date permanently disallows that brokerage loss. Not the same situation
as the 2026-07-07 resolution (that was an IRA-internal loss, a non-issue) — this is a
real taxable-account loss, the actually-dangerous direction. Both tickers are fine to
trade in the brokerage account itself, or in IRA accounts after clearance. **Action
needed**: don't execute or recommend a GDXU/AGQ buy into any IRA-type account until
the respective date above.

## [backtest] Research idea, not started, 2026-07-20 — is overnight gap frequency/magnitude asymmetric (up-gap vs down-gap)?
Raised while confirming the gap-through-trigger fix is symmetric across the
trailing-buy trigger (adverse = up-gap) and the trailing-stop/SL trigger
(adverse = down-gap) -- it is (both fixed, 2026-07-19 entry-side / 2026-07-20
exit-side). Open question, not yet checked: do these leveraged/inverse ETFs
actually gap up vs. down with different frequency or magnitude? If so, the
trailing-buy side and trailing-stop side wouldn't be equally exposed to the
fill-optimism risk in practice even though the kernel treats them
symmetrically. **User: this should be checked across all three fill
resolutions (possible/pessimistic/certain), not just `possible` alone** --
same robust-alpha convention as everywhere else in the sweep engine, since
possible/pessimistic/certain can genuinely differ on when trailing activates
and where the peak sits, not just on the final number. Framed as
research/backlog, not scheduled.

## ✅ [live-trading] Resolved 2026-07-19 — `AUTOMATION_ENABLED_TICKERS` moved to `.env`, widened to all 18 v4 tickers; EDC's v3.27 node removed from `watch_list`. Full writeup: `docs/design.md` (Layer 3).


## ✅ [live-trading][security] Resolved 2026-07-17 — same-day buy→sell block explored and deliberately NOT built
Full writeup moved to `docs/research_log.md` (2026-07-17 entry). Short version: PDT rule
eliminated by FINRA 2026-06-04, no broker/regulatory reason for a block remains; real cost
of blocking anyway is high (GDXD retains only ~47% of edge if same-day exits are deferred).
Decision: proceed without a block. `schwab_safety.py`'s existing `same_day_block` (blocks
same-day *re-buy*, unrelated direction) is untouched, still enforced live.

## [live-trading][backtest] High priority (raised 2026-07-18) — dividend cash isn't credited into P&L/SL/arm tracking; material for DPST specifically
**2026-07-18: user confirmed this needs to happen** (along with the trailing-buy re-sizing item below) — no longer just a medium-priority idea.
Raised while investigating trailing-buy capital utilization. Checked real dividend history via `yfinance`: SOXL, DPST, EDC, HIBL, KORU, LABU, TQQQ, NUGT, YANG all pay small quarterly distributions (largest recent: HIBL $1.41/share, DPST $0.671/share — all well under ~2% of share price); AGQ, GDXD, GDXU pay none (commodity-linked structure, no dividend history on file).
**Confirmed via a real fetch (`auto_adjust=False` vs `auto_adjust=True` around DPST's 2026-06-23 ex-div date)**: `data_manager.py`'s `yf.download(auto_adjust=True)` (already relied on for the KORU split fix, session 15) only retroactively rescales *cached historical bars from before* an ex-div date (DPST 6/18 close: $122.94 raw → $122.29 once fetched after 6/23) — it does **not** touch live current price or a position's recorded `entry_price` in `open_positions`, which is correct: unlike a split, a dividend doesn't change what you actually paid, so `entry_price` should stay exactly as recorded. **No data-corruption bug found** — grepped the whole codebase for "dividend": zero hits, confirming there's also no dividend-crediting logic anywhere.
**The real gap**: the underlying market price genuinely drops by ~the dividend amount on the ex-date (true price action) — but the strategy only tracks price return (`current_price` vs `entry_price`), never adding back the real dividend cash actually received. So a position held across an ex-div date shows a P&L understated by roughly the dividend %, even though the account is economically whole (cash landed in settled cash). Same gap applies to SL/arm comparisons (`check_sell_condition`), which use the same unadjusted `entry_price` vs. live-price comparison.
**Material for DPST specifically**: last dividend was 0.51% of share price against `arm_sell_pct=1.0%` (over half its entire arm threshold) — a position near its arm trigger right around an ex-div date could show materially less gain than its true total return, delaying (or in a rare edge case, falsely triggering) arm/SL timing by a meaningful fraction of the threshold. HIBL/LABU (bigger $ dividends but wider 2%/21% arm thresholds) are less exposed proportionally; AGQ/GDXD/GDXU unaffected (no dividends).
**Action needed**: not scoped. Possible shape: track ex-div dates/amounts per held ticker (`yf.Ticker(t).dividends`) and add the per-share amount back into the live P&L/SL/arm comparison for any position that was open through the ex-date — needs design discussion on where this plugs into `check_sell_condition` without disturbing the existing corporate-action (split) freeze logic it sits next to.

## [backtest][live-trading] High priority, found+partially fixed 2026-07-19 — gap-through-trigger fill optimism: neither the backtest kernel nor live sizing ever modeled overnight gaps past trail_buy_pct
**Live-side design finalized 2026-07-21**: the Part 3 live infra referenced at the
bottom of this item is now fully designed — see the consolidated Part 3 entry at the
top of this file. Kernel fix (Part 1, below) remains resolved/unaffected.
Found while scoping the trailing-buy re-sizing item below. Empirically checked overnight-gap frequency (`.venv/bin/python scripts/sim_gap_policy.py` output, or the one-off `overnight_gap_check.py` scratch script) against every active v4 watchlist ticker's real `trail_buy_pct`: **a next-day gap exceeding the trigger happens on 19-44% of all trading days**, most tickers 30-40% — not a rare tail event, routine. Two independent places assumed this never happens:
1. **Backtest kernel** (`backtester.py::_simulate_trail_both`/`_simulate_trail_buy`): every trailing-buy fill used the theoretical `running_low × (1 + trail_buy_pct)` trigger price even when the bar's own `Open` had already proven it was blown through — silently overstating every v4 node's on-file return, a distinct fill-optimism source from the already-fixed Low/High-ordering one (closer in spirit to the deferred-SL gap bug fixed 2026-07-17, but on the entry side). **Fixed 2026-07-19**: all three resolutions now fill at the real `Open` once it's proven to have crossed the trigger, before falling through to the existing Low/High logic; `_simulate_trail_buy` gained a new `opens` parameter it previously lacked. Verified via a new synthetic-gap unit test (confirmed to fail pre-fix at the stale price, pass post-fix at the real Open) and byte-identical parity between the fixed kernel and the fixed `export_trades.simulate_trail_both_annotated` mirror on real SOXL/KORU/AGQ data. Full `pytest tests/` (94 passed) + required regression scripts (`verify_trailing_buy_resolution.py`/`verify_trailing_sell_resolution.py --tickers AGQ,SOXL`) both clean, only already-documented drift.
2. **Live worst-case sizing** (`signals_blocks.py`, 2026-07-17 fix): sizes off `signal_price × (1 + trail_buy_pct)`, which only bounds the fill under continuous intraday movement — a real `TRAILING_STOP` order left resting overnight becomes a market order the instant it triggers, so a gap can blow past that ceiling. No `TRAILING_STOP_LIMIT` order type exists at Schwab to cap this at the broker level (confirmed). **Not yet fixed** — this is Part 3 of the plan below, not yet built.
**Not yet done**: resweep of the v4 campaigns (needed since the kernel fix changes `possible`/`pessimistic`/`certain`/robust-alpha for every trailing-buy/-both node — same treatment as the original fill-optimism backfill, via `scripts/run_v4_backfill_sweep.sh`/`run_backfill_queue.sh`).
**Policy simulation built+run 2026-07-19**: `scripts/sim_gap_policy.py` + `export_trades.simulate_trail_both_gap_policy` (see `CLAUDE.md` Key Files) — decided, empirically, whether the live fix should always resize-and-enter on a gap or skip the trade above some gap-size threshold, matching the `sim_chaos_monkey.py` pattern rather than guessing. Result across all 18 v4 tickers (`output/gap_policy_summary.csv`): gap-through trades have consistently low win rates (8.5%-41.8%, most 10-30%, confirming they're genuinely worse setups) but skipping them moves total compounded return only modestly with no consistent direction per ticker — no threshold shows a clear universal edge, no ticker shows a dramatic blowup avoided by skipping. Leaning toward "always resize and enter" (simplest, matches the corrected kernel default) pending final confirmation.
**Action needed**: (1) resweep v4 campaigns with the fixed kernel; (2) build Part 3 (live infra: `schwab_client.cancel_order`, order-id capture, `pending_buys.order_id`, `signals_notify.check_gap_risk()`, a daily pre-open scheduled window in `active_signals.py`) — folds together with the trailing-buy re-sizing item's top-up design below, since both need the same cancel/resize plumbing. Full plan: `/home/pkim/.claude/plans/imperative-noodling-dream.md`.

## [live-trading] High priority, confirmed 2026-07-18 — trailing-buy order needs re-sizing as the trigger price moves, to actually use all budgeted capital
**Superseded 2026-07-21**: design finalized, folded into the consolidated Part 3 entry
at the top of this file (padded sizing + overnight gap guard + post-fill top-up) —
see that entry for the current plan, this one kept for historical context only.
**2026-07-18: user confirmed this needs to happen** (along with the dividend-tracking item above).
Raised by the user, separate from today's schwab_client wiring work. `signals_blocks._build_buy_blocks`/`signals_helpers.buy_order_sizing` size a trailing-buy order's *quantity* once, at signal time, off the worst-case bounce-trigger price (`price × (1 + trail_buy_pct/100)`, fixed 2026-07-17 to guarantee the order never costs more than `target_notional`). But a real Schwab `TRAILING_STOP` order's linked trigger price keeps moving as the broker's own running-low updates after the order is placed — if the real running low falls further before the bounce (the common case, same asymmetry already documented in the sizing-formula item below), the order fills at a lower price than the quantity was sized for, and the fixed share count now under-deploys the budgeted capital (real dollars left idle) rather than the over-deploy risk the worst-case formula was built to prevent.
**Confirmed real order type, 2026-07-17**: `schwab_client.place_trailing_buy`/`place_trailing_sell` build a real `TRAILING_STOP` order (`OrderType.TRAILING_STOP`, `schwab_client.py:124`) — not a market order. `place_equity_buy`/`place_equity_sell` (market) exist in the same file but nothing calls them for GDXD (`TrailingBothZScoreBreakout` always routes through the trailing functions), so the under-deployment gap described here is specifically about the trailing order's fixed quantity vs. its live-moving broker-side trigger.
**Second, smaller contributor, same direction**: `signals_helpers.buy_order_sizing` truncates to a whole share via `int(target_notional // (price * (1 + trail_buy_pct/100)))` — even at the exact worst-case fill price, integer rounding alone leaves up to ~1 share's worth of budgeted cash unused every trade. Worth folding into whichever fix below is chosen (e.g. a top-up order could also mop up the rounding remainder, not just the trigger-price gap).
**Proposed shape, decided 2026-07-19**: (b), a top-up order once the real fill price is known, chosen over (a) cancel/replace — reasoning: (a) would require polling/approximating the broker's own hidden running-low state (the same blind modeling that caused the gap-through-trigger bug below), plus new cancel/replace machinery and a cancel-vs-fill race risk; (b) only adds a plain market order on an existing, already-tested code path (`schwab_client.place_equity_buy`). Chosen despite (b)'s "no averaging" tradeoff (a top-up at a different price than the original fill needs a shares-weighted blended `entry_price`) since that's a contained, one-time write, not a standing failure surface.
**Real complication found while designing this, 2026-07-19**: chasing the top-up naively (buying at whatever price prevails when the fill is confirmed) risks buying into a further price run since the top-up has no worst-case ceiling of its own — this led to investigating how often price gaps at all, which surfaced a much bigger, separate bug (see the new "gap-through-trigger" item below): a real overnight gap past `trail_buy_pct` happens on 19-44% of trading days across the watchlist, not a rare edge case, and neither the backtest kernel nor the live worst-case sizing formula ever modeled it. That item is now the higher-priority blocker — **this item's live order-placement infra (Part 3: `schwab_client.cancel_order`, `pending_buys.order_id`, `signals_notify.check_gap_risk()`, a daily pre-open scheduled window) folds into and should be built alongside the gap-through-trigger fix's live-side piece**, not as a separate follow-on. See that item for current status.
**Action needed**: build Part 3 (live order-placement infra) — not started. Full plan in `/home/pkim/.claude/plans/imperative-noodling-dream.md`.

## [backtest][live-trading] High priority, 2026-07-16 — trailing-buy sizing formula spends money that isn't guaranteed to be there; backtest's compounding formula assumes it too
Found while manually reconstructing KORU's real live trailing-buy fills: `signals_blocks.py:97-98` computes `shares = target_notional // price`, where `price` is the *signal-time* price — but the order is a real trailing buy, which only fills once price bounces `trail_buy_pct`% above a running low that can only fall further after that. So the real fill price can be higher OR lower than the price used to size the order, and the resulting real dollar cost (`shares × actual_fill_price`) can exceed `target_notional` — i.e. the order can need more cash than was actually budgeted for. Real principle, stated by the user: **we can't spend money we don't have.**
**Backtest side**: the exact same unrealistic assumption is baked into every number in `backtest_cache` — `run_optimization_sweep.py:382`, `compounded = ((df_tr['Return'] + 1).prod() - 1) * 100`, assumes every trade can deploy an *exact* dollar notional with no share-count rounding and no sizing-price/fill-price mismatch. Reconstructed KORU's real 31-trade live-node history three ways: (1) naive compounding as currently computed — 17.4x; (2) capped at affordable shares when the real fill price makes the full requested quantity too expensive, but leaving cash idle when the fill is cheaper than expected — 16.4x; (3) correct version, sized directly off the real (already-known-after-the-fact) fill price so every trade is always fully deployed with zero shortfall and near-zero idle cash — 17.0x, the honest number. Same check on AGQ (37 trades, 5% trail_buy_pct, the second-highest on the watchlist after KORU's 12%): 20.1x (fixed) vs 19.8x (naive) — both real, measured examples show only a few percent divergence in practice (overshoot and undershoot trades roughly offset each other over enough trades), not the runaway compounding a naive "always overshoots by the full trail_buy_pct%" worst-case would suggest — but this is only two tickers' worth of real data, not proof it always washes out elsewhere.
**Scope not started**: fixing `_summarize_trades`'s compounding formula to properly walk trades with real share-count rounding (shares = floor(equity/entry_price), fully deployed, no shortfall) would change what `strategy_return`/`alpha_vs_spy` mean for every future run — making all of `backtest_cache`'s existing v3.x/v4 history incomparable to anything computed after the fix, a real backfill decision, not a quick patch. **Live side fix, principle confirmed but not implemented**: since `entry_price` genuinely isn't knowable at order-placement time (broker determines the real fill), the live formula can't just switch to `entry_price` the way the backtest fix can — needs to size *conservatively* for the worst case instead: `shares = target_notional // (price × (1 + trail_buy_pct))`, guaranteeing the worst-case fill (no further drop in running_low) never costs more than `target_notional`. Not yet implemented in `signals_blocks.py`.
**Action plan — all 5 items done, 2026-07-17:**
1. **Done.** Live sizing formula fixed: `signals_blocks.py:96-107` now sizes trailing-buy orders as `shares = target_notional // (price × (1 + trail_buy_pct/100))` when `db._is_trailing_buy(node)`, guaranteeing the worst-case fill never exceeds `target_notional`. Non-trailing-buy strategies unchanged. Verified with a standalone test (`trail_buy_pct=12%`, $50k target, $100 price: old formula → 500 shares/$56k worst-case cost; new formula → 446 shares/$49,952 worst-case cost).
2. **Done.** GDXD ran through the full checklist (see the entry above) — real flags found (macro trend, fill-drift ratio 2.51, late-window win-rate decline, and critically **check 9: only 7.2% of robust alpha survives the real same-day-block constraint**) but accepted given the deliberately small $5k pilot size. Checklist itself extended with checks 9 (same-day-block sensitivity) and 10 (same-day-collision 70/30 stability split) in `docs/watchlist_candidate_checklist.md`.
   **Blocker found and fixed**: GDXD's only campaigns on file used `entry_timing=open_check`, which had no live-actionable equivalent — the earlier-poll-window design from the `entry_timing=open_check` backlog item (below) was actually built this session: `active_signals._OPEN_CHECK_WINDOWS = [(9,31,9,40),(14,31,14,40)]`, `signals_db.watch_list.entry_timing` column (default `'close'`), `_scan_buy_signals()` shared helper. Verified: an open_check node fires once from the early window and doesn't double-fire at the later close window (existing `buy_alerted` dedup handles it for free); a close-only node is untouched by the early window.
3. **Done.** `schwab_safety.AUTOMATION_ENABLED_TICKERS` swapped `{"KORU"}` → `{"GDXD"}` (KORU was flat, no handoff risk either way). `schwab_safety._SIGNAL_WINDOWS` widened to include `_OPEN_CHECK_WINDOWS` so the automated BUY gate doesn't reject GDXD's real open-check-window orders. GDXD inserted into the real live `watch_list` (id=56): `mode='live'`, `account='ira'`, `entry_timing='open_check'`, `fixed_sl=1%` (a deliberate, user-confirmed first-of-its-kind divergence from the watchlist's flat 15% — every other ticker still runs the unswept global default, a still-open question), `trail_buy_pct=1%`, `arm_sell_pct=7%`, `trail_sell_pct=1%`, `max_hold_hours=7`, `window=20`. `dry_run=True` left untouched on the `ira` account — nothing places a real order yet, and separately, `schwab_client`/`schwab_safety` are still not called anywhere in `active_signals.py`'s actual loop (that wiring itself remains unbuilt).
   **Also added, beyond the original plan**: a per-ticker automation pause/resume Slack toggle (`schwab_safety.ticker_automation_enabled/pause_ticker_automation/resume_ticker_automation`, persisted to `cache/live/schwab_ticker_automation.json`, mirrors the existing global kill-switch pattern), with buttons on the reference report shown only for tickers in `AUTOMATION_ENABLED_TICKERS`. Requested mid-session once GDXD went live — lets automation be paused per-ticker from a phone without a code change.
4. **Done.** `_last_sale_recovery(ticker, starting_notional)` now requires the caller to pass `starting_notional` explicitly (raises `ValueError` if both trade history and `starting_notional` are missing) — no more hidden flat-$50k fallback. New `watch_list.starting_notional` column (`REAL NOT NULL DEFAULT 50000`, backfilled to 50000 for every existing row to preserve current behavior); GDXD's row set to `5000`. All 6 call sites (`signals_blocks.py`, `signals_notify.py` x2, `signals_handlers.py` x2) updated to pass `node.get('starting_notional')`; the two Slack-value round-tripped `node_fields` tuples (`signals_blocks.py`, `signals_notify.py`) extended to include it so it survives the button-click round trip.
5. **Done, with a real and more serious finding than the original hypothesis.** Live-tested directly against the real IRA account (real settled cash $271,662.09, confirmed via `client.get_account()` before testing — already well above both `HARD_ORDER_CEILING=$100k` and the IRA's own `$75k` cap, so no order within our own limits could ever test "beyond available cash" on its own). User placed a real $200k `TRAILING_STOP` buy order directly in Schwab's UI: **buying power was unaffected** — not reduced at all by the resting order. Re-tested with a limit order instead of a trailing-stop: **same result, buying power unaffected**. **Conclusion: Schwab does not reserve/check buying power against either order type at placement time** — whatever check exists (if any) only happens at actual fill/execution, not when a resting order is accepted. This is the opposite of the original hypothesis ("a cash account may already provide a hard backstop") — **no such backstop exists at placement time**. Both test orders were cancelled by the user afterward. **Real implication**: `schwab_safety`'s own per-order notional caps are the *only* protection that exists today — nothing on Schwab's side stops multiple legitimate-looking resting orders (e.g. several tickers' trailing buys, each individually under cap) from collectively committing far more than real available cash. Not an immediate risk at the current single-ticker $5k GDXD scope, but a real gap to close (e.g. an aggregate-resting-orders check, not just a per-order one) before meaningfully widening `AUTOMATION_ENABLED_TICKERS`. What happens when an order actually fires past available cash is still unknown — deliberately not tested (would require an uncontrolled real execution, not a placement-time check).

## ✅ [backtest] Resolved 2026-07-22 — split-guard/`auto_adjust` reconciliation closed; GDXD numbers verified clean; data traceability chosen over full immutability. Full writeup: `docs/research_log.md`'s 2026-07-22 entry.


## [backtest] Medium priority, designed-not-built 2026-07-22 — data mutation log (traceability, not full immutability/versioning) for historical price cache rescales
Raised while discussing the split-guard/`auto_adjust` reconciliation above. Real gap: no
archived record of the exact `cache/research/*_1h.csv` data that fed any given past
`backtest_cache` row — the file is mutated in place on every split-guard rescale (ordinary
incremental fetches only append new rows, never mutate old ones). Two shapes discussed: (a) full
immutable/versioned data with each `backtest_cache` row linked to a specific data version — real
byte-for-byte reproducibility, but a meaningfully bigger lift (schema change touching the whole
sweep engine, real storage growth); (b) a lightweight mutation-event log — cheap, no
reproducibility guarantee on its own. **User's decision**: (b) over (a) — traceability (know
when/why/how data changed) matters more than full immutability, since the split-guard's rescale
is scale-invariant to the %-based signals every strategy actually trades on, so re-running the
same code against today's cache *should* reproduce a past alpha number without needing the exact
old bytes.
**Design agreed, not yet built**: new `data_mutation_log` table in `trading_universe.db` (via
`db_cache.py`, the existing shared research DB) — one row per split-guard rescale event:
`ticker`, `factor`, `detected_at`, `overlap_bar_time`, `price_before`/`price_after`, `notes`, plus
a `pre_mutation_snapshot` column holding the full pre-rescale `df_local` (CSV-serialized) so the
actual old data is recoverable, not just the fact that it changed. Cheap since rescale events are
rare (a handful of real splits a year across the whole watchlist), captured only at the moment
`data_manager.py`'s existing rescale branch fires, not on every routine fetch.
**User's priority call**: medium — doesn't block getting trades out, no live urgency, but worth
doing before it's needed rather than after an incident. Full discussion: `docs/research_log.md`'s
2026-07-22 entry.

## [backtest] Idea, not scoped, 2026-07-15 — eventually delete v3.x once v4 is a confirmed superset
Floated as a future disk-relief idea, explicitly not something to act on yet. Real prerequisites before it's safe, raised and agreed in discussion: (1) v4 needs to actually be run for all 11 live-watchlist tickers, not just SOXL/KORU — the other 9 tickers' live `watch_list` config is currently backed entirely by v3.x data, so deleting v3 broadly now would leave them unsupported; (2) needs an explicit per-ticker check that each ticker's *currently live* winning node (the exact window/arm_sell_pct/trail_buy_pct/trail_sell_pct combo in `watch_list`) actually exists in v4's grid — v4's island search runs independently and isn't guaranteed to land on the identical node v3.x did, so "v4 covers v3" is a plausible but unverified claim, not a confirmed one. `possible`-value byte-matching was only spot-checked on one node per ticker last session (see v4 verification note), not proof of full grid coverage. Don't treat this as decided — revisit once both prerequisites are actually met.

## [backtest] Medium priority, 2026-07-15 (revised) — v4 sweep disk footprint: 11-ticker watchlist is fine, full 53-ticker universe is not
Real disk constraint found this session: WSL's own `df` free-space number (874GB) is misleading — it's against the vhdx's *nominal* max, not real disk. The vhdx is a dynamically-growing file on the Windows C: drive, which has only ~114GB actually free. Added `--max-phase` (see below) cuts each ticker's `backtest_cache` footprint to ~2.1GB (Phase1+2+2.5 only, no Phase3) × 20 campaigns. At that rate: **11-ticker live watchlist ≈ 23GB total — comfortably affordable.** **Full 53-ticker research universe ≈ 112GB — would consume essentially all remaining Windows-side headroom**, confirmed "tight" by the user. Decision: stay scoped to the 11-ticker live watchlist for this v4 SL-sweep (matches `run_v4_backfill_sweep.sh`'s existing documented scope). Extending to the full 53-ticker universe is a real future decision, not a default — would need either more disk (external drive, vhdx relocation — discussed 2026-07-15, not started) or further data reduction (downsample/summarize into `sl_sweep_summary`, drop raw rows post-summary) before attempting it.

## [live-trading] Mostly resolved 2026-07-15 — corporate-action (stock split) defense
KORU did an unannounced ~1-for-20 stock split effective pre-market 2026-07-15 (entry $460.976 → live price ~$23.44). Discovered because the daemon's live 1-min price fetch (`signals_compute.py:115`) picked it up immediately while `yfinance`'s slower endpoints (`fast_info`, hourly `history()`) and the `.splits`/`.actions` metadata all lagged and still showed the pre-split ~$481 level.
**Fixed same day**: `cache/research/KORU_1h.csv` rescaled (pre-split rows ÷20, confirmed exact via `yf.Ticker('KORU').splits`), original backed up to `KORU_1h.csv.pre_split_fix.bak`. `data_manager.fetch_live_data_smart`'s merge step now detects a likely split automatically (`signals_helpers.detect_price_discontinuity` — round-number ratio match against known factors, e.g. 2/3/5/10/20, not a bare magnitude threshold, since a 3x leveraged ETF can plausibly crash >66% in one real extreme day) and rescales the whole local cache before merging, so this specific corruption mode can't silently recur for any ticker.
**Corporate-action detection built and live**: `signals_helpers.detect_price_discontinuity` wired into both `compute_buy_signal` (freezes new-signal generation on a stale `prev_close` — self-heals once the CSV merge-guard above refreshes it) and `check_sell_condition` (freezes SL/arm checks on a stale `entry_price` — the exact false-SL mechanism this KORU incident exposed). The held-position case sends one Slack alert per detection (`cache/live/corporate_action_alerts.json` tracks "already alerted" so it doesn't spam every ~30s poll) with a proposed correction and an "Apply Correction" button — applying it directly fixes `entry_price` via `signals_db.correct_entry_price`, which is what clears the freeze (no separate frozen-flag to toggle).
**Real data corrected this session** (via Schwab's real `get_transactions` API, not guessed ratios): `trade_log` id=9 (KORU's since-closed position) — was showing a bogus -95.75% pnl_pct from comparing pre/post-split prices directly; real fills showed 112 shares → 2240 post-split, entry $23.0488/share, exit $19.5911/share weighted avg → corrected to **-15.00%**, a clean stop-loss exit at the real trigger, not a catastrophic loss. Also found and fixed a 1-share discrepancy in SOXL's `open_positions` (307 recorded vs. 308 real broker fills) the same way.
**Still open**: the research-sweep/live-daemon shared-cache structural gap (`run_optimization_sweep.py`/`data_manager.py` both read/write the same `cache/research/{ticker}_1h.csv`) is mitigated by the new split-guard but not truly decoupled — a live corporate action can still transiently affect an in-flight research run before the next merge cycle rescales it. Giving the research sweep its own snapshot, decoupled from the live feed, is still the real fix, not done.

## ✅ [live-trading] Resolved (was stale) 2026-07-15 — HIBL trailing-buy order
`pending_buys` id=4 from 2026-07-14 was already resolved by the time this was checked 2026-07-15 (a fresh position was opened and properly marked Filled — `open_positions` shows 482 shares @ $114.31, verified against real broker fills of 375+107). No action needed; the backlog note was outdated.

## [live-trading] In progress, wiring done 2026-07-17 — Schwab API automation, dry-run cutover not started
First real OAuth login completed against the IRA account (masked-suffix matching in `.env`, never a full account number). Real guardrails built into `schwab_safety.py` beyond the 2026-07-14 skeleton: ticker allowlist + account-consistency (sourced live from `watch_list`), duplicate-order window, same-day-re-buy block (real cash-account good-faith-violation risk — same-day-*sell* deliberately left unblocked, a soft employer preference not a broker rule), a BUY-only signal-window time gate (mirrors `active_signals._in_buy_window`; SELL isn't gated since exit checks run continuously), and `AUTOMATION_ENABLED_TICKERS = {"GDXD"}` (swapped from KORU 2026-07-17, see the GDXD-promotion item above). `schwab_client.py` gained `place_trailing_buy`/`place_trailing_sell` (real broker-native `TRAILING_STOP` orders) and, this session, `get_filled_order` (polls the order book for a real fill). Kill switch persists across daemon restarts with Slack "Stop Engine"/"Start Engine" buttons.
**Done 2026-07-17**: `schwab_client` is now actually wired into `active_signals.py`'s real BUY/SELL loop (`signals_notify.py`) — see `docs/design.md`'s 2026-07-17b addendum for the full breakdown. Automated placement (`notify_buy_signal`/`notify_trailing_activated` call `schwab_client.place_trailing_buy`/`place_trailing_sell` directly for GDXD, falling back to the manual button flow on any `SafetyViolation`) is live and exercisable today since every account is still `dry_run=True`. A separate, opt-in fill-detection capability (`check_auto_fills`, per-ticker toggle `schwab_safety.auto_fill_detection_enabled`, defaults **off**) auto-records a real fill instead of waiting for the Filled/Exited click, once turned on — held back by default since `get_filled_order`'s field parsing hasn't been confirmed against a real fill response yet. 9 new tests in `tests/test_schwab_automation.py`.
**Not done**: real (non-placeholder) notional/daily cap values in `schwab_safety.py:52-55` still need review. Dry-run cutover (flipping `dry_run=False` for `ira`) not started — next step is watching GDXD's automated placement through a real live signal window in dry-run first. Auto-fill-detection not yet enabled for GDXD. Aggregate-across-tickers cash exposure (flagged 2026-07-17, see the GDXD-promotion item above) still unguarded, relevant whenever `AUTOMATION_ENABLED_TICKERS` widens beyond one ticker.
**Blocked 2026-07-18**: dry-run cutover is on hold waiting on a limited margin account (not yet in place) — external dependency, not a code/design gap. Revisit once that account is available.

## [backtest] Medium priority, 2026-07-10 — same-bar arm/take-profit trigger not checked at entry
`simulate_trail_both_annotated`/`_simulate_trail_both` skip arm/TP/SL checks on the entry bar itself (fill sets `in_trade=True` then `continue`s, so trailing-arm logic starts evaluating the *next* bar). Checked across the 6 live tickers whether the entry bar's own High already cleared the arm threshold: SOXL 0/57, EDC 0/32, KORU 0/30, AGQ 2/36, LABU 1/38, but **HIBL 20/54 (37%)** — over a third of HIBL trades could arm the trailing-sell an hour earlier than the backtest credits. Direction of the return bias not yet determined — unprovable from hourly OHLC (same intrabar-order problem as the fill-timing item). Explicit user call: leave the kernel as-is since live trading has the same delayed-until-next-bar behavior — fixing backtest without fixing live would create a live/backtest divergence. Not started.

## Backlogged, 2026-07-13 — fill-price/drift accuracy (scoped down)
Fills often don't land exactly at the expected trigger, and the current typed-price-entry flow (Executed button → manual price entry) doesn't do anything with that drift beyond logging it. **Scoped down 2026-07-13**: user is planning to hook the strategy up to a real broker API eventually, which makes the whole manual-execution phase (and its fill-drift error) temporary — not worth building a dashboard or feeding drift back into the strategy. If built at all, keep it to a simple large-drift warning (e.g. flag any single fill >3% off the expected trigger) — nothing more. See [[project_execution_automation_plan]].

## [backtest] Open question, 2026-07-09 (rescoped 2026-07-13, buffer question resolved 2026-07-14) — fixed_sl=15% itself still needs a real sweep
Originally framed as "is the Schwab catastrophic-stop +1% buffer the right size" — stop order placed at `(stop_loss + 1)%` below trigger (flat +1% buffer, hardcoded, `schwab_sl_pct = node['stop_loss'] + 1`), on the theory that ordinary intraday noise shouldn't trip the broker stop before the "real" Slack SELL signal fires. **Rescoped 2026-07-13**: `fixed_sl=15%` itself was also picked arbitrarily (flat across the whole watchlist, not derived) — tuning the +1% buffer on top of an unvalidated base number didn't make sense in isolation.
**Buffer question resolved 2026-07-14, AGQ live incident**: the premise behind the +1% buffer was wrong. The algo's own SL check (`strategies.py` `TrailingBothZScoreBreakout.check_exit`: `if ctx['low'] <= stop_price: return 'SL'`) already fires on intrabar low breach with no bar-close confirmation — it's mechanically identical to a real broker stop order, not a smoothed/confirmed "real signal" that the buffer needs to protect against noise for. There's no meaningfully separate signal for the buffer to guard. Concretely: AGQ's algo SL fired 2026-07-13 15:29:42 ($63.58 target), user missed/skipped the Slack alert (live trading is fully manual — no broker API automation yet, so the alert is just a notification, not an executed order), and the position rode down toward the broker's actual stop at $62.83 (the padded 16%) with no protection in between. Decision: **set broker stop-loss orders at the algo's exact fixed_sl price going forward (no +1% padding)** — this both matches backtest behavior exactly and removes dependence on catching/acting on the Slack alert in time. `open_positions.broker_stop_price` column added this session to track the real broker order price per position for future reference/reminder-message context.
**Still open**: is 15% `fixed_sl` itself justified? Sweep fixed_sl across a range per ticker, see where compounded return/alpha actually peaks/plateaus vs. the current flat 15%, and whether it should be flat across the watchlist or per-ticker like `trail_buy_pct` already is. **Folded into the v4 kernel-correctness plan 2026-07-14** — see the fill-optimism item above, `/home/pkim/.claude/plans/rustling-bubbling-hennessy.md`. Not started.
**Progress 2026-07-16, SOXL review**: `robust_alpha` (`MIN(possible,pessimistic,certain)`) declines consistently as `stop_loss` loosens across the full 1-30% grid on every ticker checked so far (SOXL/KORU/EDC/GDXU) — SL=1% best (27,673% on SOXL's winning node), SL=30% worst. `STOP_LOSSES`/`DENSE_SLS` capped at 9% in `run_v4_backfill_sweep.sh`/`run_backfill_queue.sh` going forward — no value above 9% has come close to the low-SL region, not worth the compute. **Caveat, not yet resolved**: SOXL's SL=1% winning node's edge is undercut by real fill-drift — `scripts/verify_trailing_buy_resolution.py` shows SOXL's entry fills drift a mean **+1.81%** from the hourly-kernel assumption (intrahour-range/trail_buy_pct ratio 3.47, the worst on the watchlist), nearly double a 1%-wide stop's whole margin — plus the `entry_timing=open_check` live-actionability gap (separate item above) and the general execution-adherence question (chaos-monkey item above). The declining-SL *trend* is trusted; the specific SL=1% magnitude is not, pending those three caveats.

## [backtest] High priority, 2026-07-16 — need a proper delayed-entry simulation for the same-day-re-buy constraint, not just a trade-list filter
While reviewing SOXL SL=1%, tried a quick same-day-re-buy-blocked simulation (mirrors the real `schwab_safety.py` cash-account rule) by post-hoc filtering the already-generated trade list: any entry falling on the same calendar day as a prior exit was simply dropped. Result was a real, large effect (`possible` resolution: 176 trades/27,738% baseline → 105 trades/4,787% blocked, roughly matching current live SL=15%'s number) — but the method is wrong. **Dropping a blocked trade assumes the opportunity just vanishes and capital sits idle; in reality the setup would still be checked (and likely taken) the next eligible day, just later and at a different price.** A dropped-trade filter and a delayed-entry simulation are not the same thing and can diverge a lot, especially for a strategy this trade-frequent.
**What's actually needed**: a bar-by-bar Python-level replay (not achievable as a flag on the existing `_simulate_trail_both` numba kernel, which has no same-day-block state) that walks the two daily signal windows, reuses the strategy's existing entry-check logic, and — while blocked — keeps re-evaluating the entry condition on subsequent days instead of discarding it outright. Entry price/time change under delay, which can cascade (a delayed entry may exit differently, shifting when the *next* block would apply), so this can't be approximated by simple filtering.
**Not started.** Relevant to the same-day-re-buy question (which affects any tight-SL, high-frequency node, not just SOXL SL=1%) and adjacent to the execution-adherence/chaos-monkey item above, though distinct: chaos-monkey models *missed* signals, this models a *known, already-enforced* real constraint that predictably reshapes the trade sequence rather than randomly perturbing it.
**Fixed** (commit `cf96c56`, "Drop stale +1% SL buffer..."): the `+ 1` padding removed from all `schwab_sl_pct` call sites in `signals_notify.py`; `pos.get('broker_stop_price')` now checked first for held positions where a real tracked value exists.

## ✅ [backtest] Resolved 2026-07-14 — trailing-buy re-entry timing after a same-day exit
Full writeup moved to `docs/research_log.md` (2026-07-14 entry). Short version: no bug —
same-day re-entry uses the exact same two daily signal windows as any other entry, no
special-casing needed.

## [execution] Research question, 2026-07-15 — when would TWAP/VWAP order execution become worth considering
Raised as a forward-looking research question, not tied to a current incident. All live sizing is currently a single-shot market/trailing order at $50k-ish notional per trade (see [[project_execution_automation_plan]] — full API automation is planned but not yet built). Worth scoping once real API automation is underway: at what position size / ticker liquidity / order-notional-vs-avg-volume ratio does slicing an entry or exit via TWAP or VWAP start to reduce market-impact or slippage cost meaningfully vs. the current single fill? Relevant existing context: `run_optimization_sweep.py:893`/`pages/11_Universe_Scan.py:99` already compute a `max_notional = avg_vol_10d * last_price * 0.01` liquidity cap per ticker (1% of daily dollar volume) for candidate screening — the same ratio is probably the right starting lens for a TWAP/VWAP threshold. Not scoped, no design started — likely blocked on the API automation work landing first, since TWAP/VWAP only matters once orders are placed programmatically rather than manually through Schwab's UI.
**First real evidence, 2026-07-15**: user's HIBL buy filled in ~20 separate pieces at the broker — HIBL's 1% ADV liquidity cap is only ~$90k (EDC ~$113k, similarly thin), so a $50k order is already a meaningful fraction of the day's volume and fragments naturally. SOXL/TQQQ (~$60M-$136M caps) are nowhere close to this by comparison — confirms HIBL/EDC are the tickers where slicing would matter first, if it ever does.

## Live trading — open items
- **Watchlist size** (originally 2026-07-07: "cut watchlist from 6 to 3 if unwieldy" — stale, superseded, watchlist is now 11 tickers). **Rescoped 2026-07-13**: still a real open question, but reframed. Current constraint is deliberately human — user is balancing diversification across accounts and keeping single tickers isolated to cash-vs-margin accounts, which is manual-execution capacity, not a statistical one (the candidate-checklist work already confirmed several of these are "safe islands" on their own merits — this isn't a quality gate, it's a bandwidth one). Explicit ordering from the user: **don't debate watchlist size until (1) the remaining `[backtest]`-tagged items are resolved (9:30-bar re-entry timing, fill-price optimism/drift) and (2) the API automation question is settled**, since API automation is what actually unlocks capacity for more tickers — sizing the watchlist before that is premature.

## Live-trading reliability
- **Default rule**: every action-requiring state change in `active_signals.py` must have a Slack notification — audit this against any new strategy/state added going forward.
- **Race condition: cache CSV reads vs. the daemon's own background refresh** — found 2026-07-13 when "🔄 Resend Report" silently did nothing. Root cause confirmed in `logs/active_signals_verbose.log`: the daemon's main loop refreshes every watchlist/open-position ticker's `cache/{ticker}_1h.csv` roughly every 30s via `fetch_live_data_smart` (non-atomic write-in-place, no lock), and a concurrent Slack button handler's `_load_cache()` read landed mid-write on GDXU's file at the exact same second, hitting it truncated/empty → `pandas.errors.EmptyDataError` → handler crashed, logged, no error surfaced to Slack. Same click again worked (pure timing fluke). Any button click has a small window to hit whichever ticker happens to be mid-refresh. **Fix**: make `data_manager.py`'s cache write atomic (write to temp file, then rename) so readers never see a truncated file. Not started.

## Reference — backup/storage policy (2026-07-07, current; weekly removed 2026-07-18)
- `trading_live.db`: hourly cron backup, keep 30 days (`cache/live_backups/`).
- `trading_universe.db` (research, regenerable): daily rotating single-file backup only (`trading_universe_daily.db.bak`). **Weekly backup permanently removed from crontab 2026-07-18** (had been commented out since 2026-07-15 to free disk headroom for the SL sweep, `trading_universe_weekly.db.bak` 46GB deleted at the time) — user decided not to bring it back rather than just leaving it disabled.

## Deferred, lower priority
- **Train/test (e.g. 70/30) split for backtest date range** — ✅ Resolved 2026-07-18, see the walk-forward item near the top of this file and `docs/research_log.md`'s 2026-07-18 entry.

## [backtest] Idea, not scoped, 2026-07-18 — daily-bar strategy variant, for much longer backtest history / real bear-market regime coverage
Raised while discussing today's walk-forward results: the current strategy's cached
hourly data only goes back ~3 years (2023-07, or ~1 year for GDXU/NUGT/USD) — no real
adverse-regime coverage (no 2020 COVID crash, no 2022 bear market) — which is the deeper
issue behind any overfitting/thin-sample concern, not just something a train/test split on
the existing window can fully address. `yfinance` **daily** bars have no such limit:
SOXL back to 2010-03-11 (inception), AGQ back to 2008-12-04 (inception), SPY back to
1993-01-29 — 10x+ more history and real regime diversity if usable.
**Real constraint, not a quick extension of the existing strategy**: the live strategy's
trailing-buy/trailing-sell mechanics (running-low tracking, arm-then-trail state,
intrabar Low/High checks) genuinely need hourly granularity — a daily-bar version would be
a different strategy variant (new strategy class, new backtest kernel path), not a
longer-history version of the same `TrailingBothZScoreBreakout` node. Wouldn't directly
validate what's currently live; would need its own design/build/validation cycle.
**Action needed**: not scoped, not started — logged as a real idea worth a future focused
session, not urgent.

## ✅ [live-trading] Open question, 2026-08-01, closed 2026-08-02 — is a production-path oversell/rejection test even constructible?
**Closed 2026-08-02**: no, and it doesn't need to be. User confirmed the underlying mechanism — a naked-sell can't reach Schwab as a real short in either account type (IRA disallows shorting outright; margin/brokerage requires an explicit distinct short-sell order type this code never issues), matching this entry's own reasoning below that our own guards would catch it first anyway. Not a live-only unknown, structurally settled.
Real Schwab rejection of a naked-sell/oversized-buy was confirmed live 2026-07-23 (bypass calls,
see that entry above), and the production path's own handling of a real `OrderRejected` (falls back
to manual, clean state, correct alert) is now confirmed via `tests/test_fake_broker_order_rejected_scenario.py`
(2026-08-01, fake-broker-driven, not a live order). What's still untested: whether either of
`live_sanity_check.py`'s two designed rejection scenarios (naked_sell, oversized_buy) would ever
actually *reach* Schwab if routed through the real production wrappers (`schwab_client.place_equity_sell`/
`place_equity_buy`) instead of the raw bypass -- both scenarios look like exactly what our own
`schwab_safety.check_order` guards (oversell-position check, cash check) are built to block *before*
ever reaching the broker, so a production-path version of these specific tests would likely just
re-prove our own guards fire (already covered by fake-broker tests), not reach Schwab's real check at
all. Reaching Schwab's real rejection *through* the production path needs a scenario our own guards
wouldn't already catch -- narrow by design, no concrete one identified yet. Not urgent; revisit if a
real candidate scenario comes to mind.

## [backtest] Idea, not scoped, 2026-08-01 — FFT-based cycle detection to inform z-score window selection / regime structure
Raised in passing (someone mentioned FFT to the user, flagged as a "background topic," not urgent).
Hypothesis: decomposing a ticker's price series into dominant cycle frequencies might explain why some
tickers respond better than others to the current `window` sweep (10/20, chosen empirically, not derived
from any actual cyclical structure), and/or could feed regime detection alongside the 2026-08-01 SPY-trend/
VIX finding (`docs/research_log.md`) — both are approaches to characterizing market structure beyond raw
z-score reversion. Not scoped: which library/method (scipy FFT, wavelet transform), which tickers, or
whether this feeds window selection directly vs. a new regime signal. Purely a research idea for now.

## [data] Idea, not scoped, 2026-08-01 — start recording our own 1-minute bars now, so a future "we need historical data" request never hits an expired retention window again
Direct motivation: tonight's real-bear-market-coverage investigation found `yfinance` hourly data is
capped at ~2 years back from fetch time (why the cache tops out ~2023-07 even today), and Schwab's own
`get_price_history` intraday retention is *worse* — empirically tested live, cuts off between 250-300
days back (~8-10 months), not the ~2 years assumed. Both windows have already permanently rolled past
2020/2022 — no vendor we currently have access to can backfill those periods at real intraday
resolution; a real historical vendor (Massive/Polygon $29-79/mo, Databento pay-per-use) is the only path
for the past, discussed same session (`docs/research_log.md`, not yet acted on).
**The idea**: if we'd been recording 1-minute bars ourselves since inception, this specific problem
(needing data from a period that's already outside every free/current vendor's rolling window) would
never recur for anything that happens *from now on* — including whatever the next real bear market
turns out to be. Also finer-grained than the current hourly cache, which could reduce/eliminate the
possible/pessimistic/certain fill-ambiguity the kernel currently has to guess around for trailing-buy/
trailing-sell resolution.
**Scope, per user (2026-08-01)**: the ~53 tickers that could realistically go into the strategy (the
real candidate universe), not the full ~1,448-ticker research universe used for broad scans like the v6
idle-capital-parking sweep.
Not scoped: exact 53-ticker list (needs pulling from wherever candidate screening currently tracks it),
storage growth/retention policy over years, whether this hooks into the existing `data_collector.py`/
`data_manager.py` pipeline or is a separate always-on process, and whether Schwab's or another source's
minute feed is the right one to record from continuously.

## [backtest][data] Resolved 2026-08-02 — prune `backtest_cache` to island-only, uniformly, for every ticker/version
Full reasoning: `docs/conversation_summary.md`'s 2026-08-01 (evening) entry. General, repeatable
policy, not a v4-specific one-off: **a stored backtest result inherits the trust level of the code
that produced it — if that code is later found buggy, the stored output is disposable, and the only
things worth keeping are (1) the ability to recompute with current code (always intact regardless,
depends on `backtester.py` + raw price CSVs, never on `backtest_cache` rows) and (2) one reference
sample documenting the bug's before/after magnitude** (e.g. GDXD's v4 +1442.2%→v5 -37.8% CLIFF, or
SOXL's fragile 27,673%-robust_alpha v4 node vs. its v5 possible/certain range of 794.2%-5,772.6%).
Applies symmetrically to any future version (if a bug were ever found in the island-search/cliff-
safety logic itself, v5 would become exactly as disposable as v4 is now). Also: `cache/research/` is
already documented (`CLAUDE.md` Runtime Artifacts) as "regenerable research data", explicitly
distinct from `cache/live/` (the real trade record) — this whole DB was always meant to be disposable
scratch space; only `cache/live/` carries the never-delete weight, and stays architecturally separate
from `cache/research/` on purpose (so a heavy in-progress sweep never contends with the live daemon's
DB performance) — unaffected by this pruning either way.
**Final scope (went through several drafts same session before landing here — "full grid top 10" →
"full grid top10/island top20/top-node universe" → this)**: **no full-grid tier at all, for anyone.**
An island (~50 nodes: best `robust_alpha` node ± 2 nearest distinct `take_profit`/`stop_loss` values,
±24h `max_hold_hours`) for all 53 candidate tickers combined is only ~1-5 MB — negligible next to the
65 GB full DB (167,502,634 rows) — so gating island-keeping behind any ticker-count cutoff was pure
unnecessary complexity. Full grids only bought the ability to explore *beyond* the island's
neighborhood, which is realistically "re-run a fresh sweep" territory anyway (always possible
regardless of what's kept, per the recompute point above) — islands are already the same granularity
the project's own cliff-safety checking uses as its bar for "is this node robust." Applies uniformly:
watchlist tickers and candidates alike, all versions alike.
**Real numbers found querying this before finalizing**: 167,502,634 total rows, 53 distinct tickers,
65 GB DB size; per-watchlist-ticker row counts range ~4.5M (UDOW) to ~19.8M (SOXL) — confirms
`backtest_cache` is genuinely too large to query normally (basic aggregate queries and even
`PRAGMA integrity_check` routinely time out at 2+ minutes), independent of the disk-space argument —
a database this unwieldy to query isn't serving its purpose regardless of space.
**Execution status, 2026-08-01 evening**: `scripts/prune_backtest_cache.py` built (three modes:
`--dry-run` report-only, `--build` writes a fresh `trading_universe_pruned.db` leaving the original
fully untouched, `--swap` moves the original aside with a timestamp — never deletes it outright — and
renames the pruned DB into place). A full pre-prune snapshot was set aside first, permanently, outside
the daily backup rotation: `cache/research/permanent_archive/trading_universe_pre_prune_20260801.db`
(the redundant duplicate from an initial `cp`-not-`mv` mistake was cleaned up; only one 65GB archive
copy exists). User's stated intent for that archive: keep it a few months as a safety net, probably
delete it once confidence builds that it's genuinely never needed again — unless a new strategy
variant (e.g. a "v6") makes the old comparison data relevant again.
**Completed 2026-08-02**: `--dry-run` confirmed 493,720 of 167,502,634 rows to keep (0.2948%). `--build`
took ~37 min (recomputes the same keep-set from scratch, same slow-table cost as the dry-run) and wrote
a clean `trading_universe_pruned.db` — `PRAGMA integrity_check` ok, all 53 tickers present, spot-checked
SOXL's per-strategy/version alpha values against the pre-prune numbers documented above (sane, non-zero).
`--swap` moved the original 65GB DB aside to `cache/research/trading_universe.db.pre_prune_20260802_
013426` (not deleted, per the script's design) and put the pruned 256MB DB in its place as `trading_
universe.db`. The separate permanent archive copy (`cache/research/permanent_archive/trading_universe_
pre_prune_20260801.db`, full 65GB) is the one the user intends to keep for months as the real safety net
— the `.pre_prune_20260802_013426` swap-aside copy is redundant with it and can be deleted once the
pruned DB has been used for a while without issue.

## [live-trading][security] Resolved 2026-08-02 — both gaps deferred from the TP/TRAIL alert-wording fix closed: broker_stop_price clearing, and Skip no longer abandons a real resting order
Both found by the same Opus review that caught the alert-wording fixes; deliberately deferred at the
time (touches `_attempt_automated_sell`/`_attempt_automated_exit_sell`, which carry ~15 documented
regressions in their own comments — judged too risky to modify further without dedicated scoping).
**(1) `broker_stop_price` staleness — found already resolved on investigation, backlog was stale.**
Turned out to have been fixed the same day (2026-08-01, commit `9b2f22f`) by the session-wrap Opus
review's own HIGH finding — `set_broker_stop_price_by_position` now accepts `None` and is called at
both replace sites (`signals_notify.py:171`, `:343`) right after a real replace succeeds. This backlog
item just hadn't been updated to reflect that; no code change needed here, only doc cleanup.
**(2) `handle_sell_skipped` (`signals_handlers.py`) now cancels a real resting FRESH exit order before
dropping local tracking**, instead of just popping `exit_pending` (and its `order_id`) with nothing left
polling it. **The first version of this fix (committed to working tree, never landed) unconditionally
cancelled whatever order `exit_pending.order_id` pointed at — a paired independent+contextual Opus
review (2026-08-02) confirmed this was itself a HIGH real-money regression**: for a genuine TRAIL breach
(not hold-time-forced), `_attempt_automated_exit_sell` reuses the SAME order placed at the earlier arm
event (`signals_notify.py:251-252`) — the position's ONLY live protection, since arming already replaced
the stop-loss with it (confirmed by reading `_attempt_automated_exit_sell`/`notify_trailing_activated`
directly, not just taking the review's word for it). Cancelling it on Skip would leave the position with
zero broker protection while the alert simultaneously says "no action needed." The review also caught:
`exit_order_id` left stale after a cancel (next bar's forced-exit attempt would try to replace an
already-dead order and fail — the exact pre-existing risk `_attempt_automated_exit_sell`'s own comments
already flagged, reached through this new door); a confirmed `FILLED` status mishandled two ways (via a
race during the cancel call, and via the order already being FILLED before Skip was even tapped) with
`exit_pending` dropped either way, silently disarming `check_own_sell_fills`' polling reconciliation; and
`exit_pending` popped unconditionally even on a failed/unconfirmed cancel, recreating the exact "live
order with nothing polling it" bug the fix was meant to close, just on the unhappy path.

**Rewritten, correctly scoped:** cancellation now only applies to a genuinely FRESH exit order (TP/SL/
TIME, or a hold-time-forced TRAIL replace) — a standing arm-time trailing-sell (genuine TRAIL breach) is
left untouched, Skip only clears that alert's own reminder tracking. `exit_pending` is only dropped on a
confirmed `CANCELED`; a confirmed/discovered `FILLED` leaves it in place so `check_own_sell_fills`'
existing polling reconciles the real close with the real fill price instead of this handler guessing; an
unconfirmed/failed cancel also leaves it in place. A successful hold-time-forced cancel additionally
clears `exit_order_id`/`hold_time_replaced` so the next forced-exit attempt doesn't retry a dead order.
`pos` is re-fetched immediately before the write (the stale-snapshot clobber class this region has been
bitten by repeatedly — `_exit_order_resting`'s broker round-trip can take seconds). New
`tests/test_fake_broker_sell_skipped_scenario.py` (4 scenarios, zero prior coverage existed for this
handler) pins all four branches, including the TRAIL-protection-must-not-cancel case as the primary
regression guard. Full suite: 499 passed, no regressions.

**User's expectation, 2026-08-02**: this whole Skip/Exited manual-confirmation flow may be redone as
part of a broader Slack-interface rework once v5 moves toward no manual step at all (see the
`manual_buy_confirmation_account` entry above) — landed anyway since it's a live real-money gap today
and the redesign isn't scheduled.

## ✅ [live-trading][coverage] Resolved 2026-08-01 (evening) — GDXU TRAIL-exit alert wording bug closed, 2 review rounds
Original finding (2026-07-28): `_build_sell_blocks`'s TP/TRAIL branches hardcoded "Cancel Stop Loss
order — Sell All (Market)" regardless of whether an SL order actually rested — for a genuinely armed
position (SL already replaced by a resting trailing-sell, per `_attempt_automated_sell`), this reads
as an instruction to cancel an order that doesn't exist. Confirmed still real: `notify_sell_signal`
was rewritten 2026-07-31/08-01 to attempt an automated exit for every SELL reason before ever posting
a manual alert, falling through to `_build_sell_blocks` only when a short (~15s) fill-confirm poll
doesn't return a result — the *normal* case for a resting trailing-stop, not evidence of a problem.
The other sub-issue from the original finding (`_current_price()`'s overnight staleness guard) was
already fixed 2026-07-31 (widened to a straight age check) — confirmed via code read, no action needed.
**Fix (round 1)**: `_build_sell_blocks`/`_exit_pending_blocks` branch on whether an automated exit
order is confirmed resting instead of always showing the manual-cancel instruction.
**Opus review round 1 found 3 real gaps, all fixed**:
1. **HIGH**: the first version trusted a stored `order_id`'s mere presence as proof of a resting
   order — indistinguishable from a REJECTED/CANCELED one (real failure mode, see LABD's rejected
   stop, 2026-07-31), and the reassuring text removed the only warning that case had. Fixed: new
   `schwab_client.get_order_status(account, order_id)` (single non-retrying real status check,
   deliberately not reusing `_confirm_order_status`'s 4x0.5s retry loop built for right-after-
   placement use) + `signals_notify._exit_order_resting(pos, reason, order_id)`, a tri-state
   (True/False/None) check that fails toward the cautious message on both False and None.
2. **MEDIUM**: TIME reason was left on the old "Change Stop Loss → Market Close" text, contradicting
   its own now-reason-agnostic 15-min reminder. Fixed after confirming `_attempt_automated_exit_sell`
   really does route TIME through the identical market-sell path as TP (traced end to end).
3. **MEDIUM**: the TRAIL fix's first version missed its own target case (`exit_order_id` legitimately
   None on a real automated placement success) via a `trail_state['order_placed']` fallback.
Also escalated `_exit_pending_blocks`'s wording after `reminder_num >= 3` even when confirmed resting
("should have filled by now, worth a look") — the original 2026-07-28 design intent (distinguish
"routine still-waiting" from "should have filled by now"), not just wording.
**Opus review round 2 (verification pass on the round-1 fixes) found 2 more real gaps, both fixed**:
1. `_exit_order_resting` gated on `schwab_client._ORDER_TERMINAL_BAD_STATUSES` (3 statuses: REJECTED/
   CANCELED/EXPIRED, built for a different question) instead of `schwab_safety._OPEN_ORDER_STATUSES_
   EXCLUDED` (5 statuses, the set the resting-order duplicate guard already uses for "is this
   genuinely still open") — would have reported "confirmed resting" for an order that had actually
   FILLED or been REPLACED. Fixed by switching to the correct 5-status set.
2. The `trail_state['order_placed']` fallback for TRAIL was itself unsafe — `order_placed` is ALSO set
   by `signals_handlers.handle_trail_order_placed`, a manual Slack button tap with no broker
   verification, and there's no stored provenance distinguishing which path set it. Trusting it would
   have reintroduced the exact "trust an unverified flag instead of the broker" pattern this whole fix
   exists to eliminate, through a different door. **Removed rather than patched under time pressure**
   — the narrow sub-case it targeted (unextractable order id on a real automated TRAIL placement)
   reverts to the pre-2026-08-01 cautious text; not a new regression, same as before this session.
**Deferred, not fixed this session** (see the paired backlog entry): `broker_stop_price` never cleared
after a replace (now more load-bearing since `stop_status()` reads it directly); `handle_sell_skipped`
abandons tracking of a real resting order without cancelling it. Both touch
`_attempt_automated_sell`/`_attempt_automated_exit_sell`, judged too risky to modify further in the
same session given their documented regression history.
19 new tests (`tests/test_exit_order_resting.py`, `tests/test_sell_blocks_automated_exit_wording.py`).
Full suite: 493 passed (was 479). `signals_invariants.py`: clean.

## [live-trading][coverage] Partially resolved 2026-08-01 — the 07-27 canary_* duplicate-row concern was already inert; cleaned up
Investigated after noticing 6 `canary_*` `scenario_expectations` rows each had a duplicate. Root cause,
confirmed via git history: the table gained a `mode` column in commit `3accd4e` (2026-07-25, "Migrate
coverage system to real node_id identity"). The original 6 rows (seeded 2026-07-24) had `mode=None`;
the migration re-seeded the same `(scenario_key, node_id, ticker)` combos with `mode='live'` explicitly
set. Since `add_scenario_expectation`'s dedup key is `(scenario_key, node_id, ticker, mode)`, the
differing `mode` meant "not a match" — new rows got inserted instead of updating the old ones in place.
**Verified NOT a live bug**: the old `mode=None` rows were already `active=0` (mechanism unclear — no
current code sets `active=0` for this table, likely a manual DB edit during the same 07-25 session, not
committed as a script) and `get_scenario_expectations()` defaults to `active_only=True`; both
`coverage_check.py` and `pages/14_Coverage.py` explicitly filter `WHERE active=1` too — confirmed via
source read, not inference. So the duplicates never double-counted or affected `coverage_check.py`'s
output. Cleaned up anyway: backed up `cache/live/trading_live.db` first (daemon confirmed running but
this table sees no concurrent writes), deleted the 7 `active=0` rows directly. Full suite (coverage/
scenario-tagged subset): 126 passed, unaffected (isolated test DBs).
**Still open**: whether `canary_early_sl`/`canary_full_lifecycle`/`canary_market_buy_exit`/
`canary_overnight_carry` (QQQ/IVV/VOO/DIA) should also move to `expected_frequency='informational'`
like IWM/XLF did 2026-07-30 (the original 07-27 finding's other half) — not investigated this pass.

## [live-trading][coverage] Resolved 2026-08-01 — reconciliation_mismatch broken out per-node (20 rows, was 1 global)
Raised 2026-07-30: the global `reconciliation_mismatch` scenario_expectations row (ticker=None,
node_id=None) sat outside the two other per-node "state report" tables built the same session (the
12-row canary_* table, the 5-row soxl_ira staged-config table) — user expected the same granularity.
Infrastructure already existed and needed no code change: `check_live_state_reconciliation` already
logs `ticker`/`node_id` on every real `coverage_events` row, and `_check_coverage_event`
(`scripts/coverage_check.py`) already scoped its query by `scenario['ticker']`/`node_id` when set —
built defensively 2026-07-30, before any per-node scenario existed, specifically for this future case.
New `scripts/seed_reconciliation_mismatch_per_node.py`: deactivates the old global row (kept, not
deleted, same precedent as the 07-25 migration's duplicate-row handling), seeds one row per real
`mode='live'` node (20 total: 13 `ira` canary/mirror + 7 `soxl_ira`), `expected_frequency='informational'`
(same tier as before — a single node's reconciliation status is trade-conditional). Backed up
`cache/live/trading_live.db` first (daemon confirmed running, this table sees no concurrent writes).
**Found and fixed a real regression while validating**: `signals_notify.build_eod_scenario_review`'s
"other_results" (non-canary control scenarios) section printed one unconditional line per result — fine
for a 1-row global scenario, but would have posted 20 lines to the EOD Slack report every single day
for what's routine informational status. Fixed: grouped by `scenario_key`, one summary line
("X met / Y deviation(s) / Z informational / W snoozed of N") per key, matching the canary section's own
pattern immediately above it — individual per-ticker lines now only appear for a genuine ticket-eligible
deviation (a real problem), not for the routine case. Verified against real 2026-07-31 data (via
`coverage_check.py --date 2026-07-31`): correctly informational, zero false tickets. Full suite (coverage-
tagged subset): 146 passed.

## [live-trading][security] Resolved 2026-08-01 (evening) — real incident: an ad hoc test call posted a real Slack message; SIM_MODE flipped to a fail-safe default
While testing the reconciliation_mismatch fix above, `signals_notify.build_eod_scenario_review('2026-07-31')`
was called directly in a Python one-liner without setting `SIM_MODE=1` first — it returned a real
`(channel, ts)` pair, confirming a real, unprefixed message posted to the live Slack channel (content:
a coverage/readiness report, not a trade action — no order was placed). Root cause: `signals_config.
SIM_MODE` defaulted OFF (`os.environ.get("SIM_MODE") == "1"`), an opt-in-safety design that fails open —
any ad hoc script/test invocation that forgets to export it posts for real.
**Fix, user-directed**: flipped the default to fail-safe (`os.environ.get("SIM_MODE", "1") != "0"` — ON
unless explicitly set to `"0"`), matching the fail-closed pattern already used everywhere else in this
codebase (kill switch, oversell guard, node circuit breaker). Since `SIM_MODE`/`INTERACTIVE`/the Bolt
app singleton are computed at `signals_config` import time, the real daemon can't set this itself after
importing — `active_signals.py`'s own entrypoint now runs `os.environ.setdefault('SIM_MODE', '0')`
as literally its first executable line (before `import sys`, well before `signals_config` is reachable
via any import chain), so the existing real launch command (`python active_signals.py run` / bare
`python active_signals.py`) needs no change at all and no env var to remember.
**Defense in depth**: new `signals_invariants.check_sim_mode_off_for_real_daemon()` — deliberately NOT
added to `CHECKS`/`run_all()`, since that function also runs standalone via the pre-commit checklist
(`.venv/bin/python signals_invariants.py`), where `SIM_MODE=1` is the correct, expected default and
would false-positive on every routine pre-commit run. Instead called directly, once, from
`run_loop()`'s own startup (non-blocking, alerted not fatal, matching the existing invariants-check
pattern immediately below it) — catches the one real remaining failure mode, `SIM_MODE=1` already
exported in the shell before launch (`setdefault` respects an existing value by design and won't
override it), which would otherwise silently turn the real daemon into a no-op simulator: every alert
misleadingly prefixed 🧪 SIM MODE, every interactive button (Executed/Filled/Order Placed/Exited/
Skipped) disabled.
Verified all 4 real scenarios directly: ad hoc script alone → SIM ON; `active_signals` imported → SIM
OFF; `SIM_MODE=1` pre-exported then `active_signals` imported → stays ON (setdefault correctly
respects it); `signals_invariants.py` run standalone → no false-positive SIM_MODE violation.
**Not fixed / open question**: whether to post a Slack follow-up in the live channel clarifying the
earlier accidental EOD-report post was a test artifact — deferred to the user's call, not urgent (no
trade action, just a coverage report).

## ✅ [live-trading][security] Resolved 2026-08-01 (session-wrap review) — final whole-diff Opus review of all 5 session pieces together found 2 real cross-piece bugs, both fixed
Required by `session wrap` (CLAUDE.md): once individual pieces are reviewed and fixed, the FULL combined
diff still needs one end-to-end pass, since a fix in one piece can be undermined by another piece added
later in the same session. Findings:
1. **HIGH — piece 1 (SL alert `stop_status`) activated a dead code path that was never given the
   staleness handling it needed.** `broker_stop_price` had zero production writers before this session
   (confirmed: the legacy `set_broker_stop_price` was dead code) — so `_build_sell_blocks`/
   `_exit_pending_blocks`'s `'known'`/`if bsp:` branches were dead in production too. Making the field
   live (piece 1's whole point) meant it also needed to be *cleared* wherever the real stop it describes
   gets replaced — which happens routinely: `_attempt_automated_sell`'s arm-time SL→trailing-sell
   replace, and `_attempt_automated_exit_sell`'s TP/SL/TIME market-sell replace. Left uncleared,
   `stop_status()` reported `'known'` off a dead price — a real SL alert firing after either replace
   would have falsely said "broker stop on file, no action needed" for a position actually protected
   only by an unconfirmed resting order, a behavior *regression* from the pre-diff cautious "check
   account" text. Fixed: `db.set_broker_stop_price_by_position` now accepts `None` to clear; wired into
   both replace sites (`signals_notify.py`, the arm-time success path and the TP/SL/TIME replace
   success path, gated on `resting_order_id == pos.get('sl_order_id')` so only a genuine SL replace
   clears it, not a trailing-sell-to-trailing-sell no-op). 2 new regression tests
   (`tests/test_broker_stop_price_cleared_on_replace.py`, via `fake_broker`) prove the field actually
   clears after both real replace paths, not just that the code compiles.
2. **MEDIUM — piece 4's `os.environ.setdefault('SIM_MODE', '0')` sat at module-import scope**, so any
   `import active_signals` (the file's own documented library-reuse pattern — 11+ scripts do exactly
   this) silently disabled the new fail-safe default, reproducing the exact incident the change existed
   to prevent, just through the library-import path instead of a bare script. Verified empirically
   before and after the fix. Fixed: gated behind `if __name__ == '__main__':` (Python sets `__name__`
   before any of a module's top-level code runs, imports included, so this is safe to check at the very
   top of the file, before any imports) — only a real direct invocation (`python active_signals.py
   [run|list|add|remove|positions]`) now opts back into real posting; a library import stays SIM-ON by
   default. Re-verified both directions empirically (library import → stays True; direct execution via
   `runpy.run_path(..., run_name='__main__')`, the same mechanism as `python active_signals.py` → False).
Also checked and confirmed clean (no fix needed): piece 5's `is_protective` exemption doesn't interact
badly with the trading-day gate (separate, unconditional, unaffected) or with SIM_MODE/dry-run (the
window gate is side/flag-driven, doesn't branch on either); `tests/conftest.py`'s new
`cfg.INTERACTIVE = True` forcing doesn't mask any of piece 2's tests (every consumer reads `cfg.
INTERACTIVE` via the same patched module object, and piece 2's own tests explicitly override it back to
False per-test where needed); piece 3's `other_by_key` grouping composes correctly with the rest.
2 cosmetic-only findings (a stale docstring claim in a test file contradicting its own sibling test, a
redundant no-op patch loop in conftest) — not fixed, zero behavior impact, noted for whoever touches
that code next.
Full suite: 495 passed (was 466 at session start). `signals_invariants.py`: clean.
`live_sim_harness.py`: 7/7. `verify_trailing_buy_resolution.py`/`verify_trailing_sell_resolution.py
--tickers AGQ,SOXL`: both clean, matching expected drift.

## [live-trading][coverage] Open, raised 2026-08-01 — 14 Accountability Grid rows still wired-never-fired; 3 flagged suspicious, not yet investigated
Found while investigating the `top_up`/signal-window bug above (via `scripts/coverage_registry.py`
directly, not a diff review — same technique that found that real bug). Current real count: 41 rows,
18 verified-live, 1 `live-attempt-failed` (`top_up`, now fixed above), 14 `wired-never-fired`. Split
into two buckets:
- **Legitimately rare, not obviously a bug**: `kill_switch_block` (switch never engaged live),
  `node_level_automation_pause`/`two_nodes_same_ticker_diff_accounts`/`oversell_guard_correct_position`/
  `buy_buttons_resolve_correct_node`/`buy_fill_reconciles_correct_node` (all need 2+ nodes sharing one
  ticker concurrently — may not match current watchlist topology), `stale_buy_button_guard` (needs an
  actual duplicate button tap), `dup_order_no_false_block` (needs a legitimate top-up colliding with
  the dup-guard tolerance window), `market_buy_placement`/`pinned_entry_trigger` (only human-explained
  historical deviations exist), `position_lock` (deliberately left uninstrumented, user's call,
  2026-07-28).
- **Suspicious — should plausibly have fired by now given real trading volume, not yet checked**:
  `automated_sell_mode_skip`, `fast_path_fill_reconciliation` (websocket fast-path fill
  reconciliation), `manual_buy_confirmation_account` (manual BUY confirmations happen constantly).
  These are the natural next place to look for another `top_up`-shaped latent bug — same technique
  (check `db.get_coverage_events(scenario_key=...)` for real firings, trace why zero exist if the
  real-world trigger condition should be common) not yet applied to these 3.
**Action needed**: run the same investigation technique against these 3 specifically before assuming
they're just rare — `top_up` looked identical to these until checked directly.

## ✅ [live-trading][security] Resolved 2026-08-01 — real live-trading bug found via the Accountability Grid, not a diff review: post-fill top-up BUYs blocked 100% of the time outside signal windows
Found by running `scripts/coverage_registry.py` directly to answer "what's still uncovered" — showed
`top_up` as `live-attempt-failed`: fired for real 3 times, failed every single time
(`db_update_failed_after_real_order`, `failed_unexpectedly`, `blocked`, `overspent_no_corrective_sell`).
Traced the real events (`db.get_coverage_events(scenario_key='top_up')`): 2 of 3 were rejected for
`"BUY outside signal windows"` at 18:57 and 00:19 — hours after any signal window, exactly when a
delayed trailing-buy fill would land.
**Root cause**: `schwab_safety.check_order`'s signal-window gate (`schwab_safety.py:950`) only exempted
`is_gap_correction`, not `is_protective`. The post-fill top-up BUY (`signals_notify._reconcile_fill`,
`is_protective=True`) passes `is_gap_correction` through from its triggering fill — but that only
covers the ONE narrow case where the fill itself came from `check_gap_resize`. The general case (any
normal trailing-buy fill landing outside the narrow windows, which is routine — a resting order can
fill hours after the signal fired) was still gated. The code's own docstring shows the author was aware
of the narrow case but missed the general one. A top-up isn't a fresh signal-driven entry — it's
completing an already-approved one, same reasoning already applied to `is_gap_correction`.
**Fix**: `check_order`'s window-gate condition widened to `not is_gap_correction and not is_protective`.
Checked every real `is_protective=True` BUY-side call site (only one exists — the top-up itself; the
other two are SELL-side, where the window gate never applied anyway) to confirm no unintended scope
widening. Regression test added (`tests/test_schwab_safety.py::test_protective_buy_bypasses_signal_
window_gate`) confirming a non-protective BUY still gets blocked outside the window while a protective
one doesn't.
**Why the test suite never caught this**: nobody had ever written a test for `is_protective` against
the signal-window gate specifically — only the daily-cap exemption
(`test_protective_top_up_bypasses_exhausted_daily_cap`) was tested. The bug was invisible to "suite is
green" because the specific combination (top-up + outside-window) was never exercised. Found instead by
checking real broker outcomes via the Accountability Grid, not by reading the diff or trusting test
coverage — the exact reason the Grid was built.
Full suite: 494 passed (subsequently 495 after the session-wrap review's 2 more tests).
