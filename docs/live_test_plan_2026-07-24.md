# Live Test Plan — 2026-07-24 (soxl_ira real-money test day)

Master accounting doc for every real action taken against `soxl_ira` (the only account going
`dry_run=False` today) — one row per action, filled in as it actually happens, not reconstructed
afterward. To be reviewed together this weekend and again mid-day (~noon/12:00) per the user's
request — the goal is enough real data to determine whether paper trading / dry-run / live trading
actually worked as expected, not just whether an order was accepted.

## Scope
- Account: `soxl_ira` (suffix 931), `dry_run=False` for this account only. All other accounts
  (`brokerage`/`sep`/`roth`/`ira`) stay `dry_run=True`, untouched.
- Real pre-staged positions already in the account before today: 3 SPY, 50 SH.
- `.env` `SCHWAB_AUTOMATION_TICKERS` widened today to add ERX, ERY, LABD, SH, GDXU (SPY/GDXD already
  present).

## FINAL STATUS — end of day (16:02 ET, daemon shut down)
**soxl_ira real state at close:**
- Final cash: **$1,110.46** (started $1,110.43 — net day P&L ≈ **+$0.03**, essentially flat: GDXD/GDXU
  round trip -$1.87, LABU +$1.44, ERY +$0.47)
- **ERY**: CLOSED. Full round trip: auto-detected BUY fill $10.1375 (10:47 ET, zero manual clicks) →
  real Market-on-Close SELL @ $10.37 (16:00:00 ET exactly, first MOC order the user has ever placed)
  → +2.3%, ~$0.47 gain. Clean, fully-reconciled test of the automated-entry path end to end.
- **SPY**: left OPEN through Monday (3 sh, entry $738.18, armed & tracking peak $743.57+, automated
  sell permanently blocked by `notional_cap` — real bug filed, see below).
- **SH**: left OPEN through Monday (50 sh, entry $33.52, hit real fixed-SL, never armed — real bug/gap
  filed re: no automated protection for manually-seeded positions).
- **LABD**: never triggered all day (wrong-sign z) — no position, no test exercised.
- **ERX**: never triggered all day (wrong-sign z) — no position, no test exercised.
- **GDXD/GDXU**: fully closed mid-morning (see below), net -$1.87.
- **LABU**: fully closed mid-afternoon (see below), net +$1.44.

## Earlier status detail (kept for the record)
- **ERY** (entry detail): real filled position (2 sh @ $10.1375, auto-detected/auto-reconciled ~10:47
  ET — first fully-automated real BUY→fill→position of the day, zero manual clicks). No real broker
  stop (`TrailingBoth` gating gap, see backlog).
- **SPY**: ARMED for real ~11:21 ET (`arm_sell_pct` lowered to 0.1% earlier), peak $743.57+, real
  price ran up well past entry intraday — but the **automated trailing-sell is permanently blocked**:
  `notional_cap=$800` applies to the SELL side too, and SPY's real position (~$2,227) exceeds it, so
  `_attempt_automated_sell` gets a real `SafetyViolation` every time (confirmed in Slack log, 11:21
  ET). **Test result: clean fail** — the automated-exit leg of "Real SELL exercise (arm/trail
  automated...)" doesn't work for a position this size while the cap stays low. Real bug filed
  (`notional_cap` shouldn't gate SELL orders the same way as BUYs). Position remains open/unprotected
  by an automated order — user's call to leave as-is, not urgent to fix same day. Note: the exact
  11:21 arm timing was a separate side effect of the 11:14 daemon restart (`last_seen_bar` reset), not
  a natural bar-close — also backlogged, real bug found+filed. **Deliberately left open through
  Monday** (user's call ~12:25 ET) — both because the automated-sell path is a dead end today
  (notional_cap) and to keep a real position on hand in case more trades/tests are wanted Monday,
  rather than needing to re-stage a fresh position from scratch.
- **SH**: hit its real fixed-SL (never armed — price moved past both arm and SL in one move; the
  strategy checks fixed-SL before arm). **Deliberately left open/unresolved for Monday's `TrailingBoth`
  SL-automation test** (user's call ~11:32 ET — small position, low stakes, useful carryover rather
  than closing out a real SL trigger we're not acting on today anyway; same "keep it available for
  more Monday trades" rationale as SPY above).
- Unresolved Slack reminders: keep clicking **Skip** on SPY-exit/SH-exit reminders (not testing manual
  SL today) and on **LABU** (sizing-bug artifact, no real order exists to confirm).
- **GDXD/GDXU: fully closed.** Both filled for real at the open (GDXD 5sh@$47.39, GDXU 3sh@$80.805),
  manually sold out (GDXD@$47.3508, GDXU@$80.25) to recycle capital — net combined loss ~$1.87.
  Gap-resize (`check_gap_resize`) ran correctly at 9:15-9:29 and did nothing, since neither had
  actually gapped past its trigger — the cancel+replace code path itself was **not exercised** today.
- **ERX**: never triggered (z stayed positive all morning) — no order, no test exercised for this leg.
- **LABU**: 9:30:02 attempt crashed on a sizing bug (`starting_notional=$22` → 0 shares, fixed to
  $500). **Retried successfully at the 14:30:02 pinned check** — real BUY fired, filled cleanly
  (1 sh @ $245.1434, first real `open_check` fill of the day). **But the actual test goal — proving
  `TrailingExit`'s automatic SL placement works — still failed**: both the SL placement and a
  top-up-buy attempt got blocked by `daily_order_cap=3`, already exhausted by the day's 3 earlier real
  BUYs (GDXD/GDXU/ERY). Real bug filed. **Clean fail on the intended test, for a third distinct
  reason** (not the `TrailingBoth` gate, not `notional_cap` — this time `daily_order_cap`). User's
  call: manually close out LABU rather than place a real stop, since the daily quota's already spent.
- **Account-mapping backfill done ~10:15-10:40 ET** (separate from the soxl_ira test, applies to the
  other 15 `mode='live'` nodes that had `account=None`): all mapped to `ira` except canary SPY, which
  was swapped to ticker **IVV** on account **brokerage** to avoid a real ticker/account collision with
  the real `soxl_ira` SPY node. IVV fired a real (dry_run) BUY signal at 10:34 ET.
- Several real bugs found and backlogged today (not fixed same-day) — see "Bugs found today" below.

## Bugs found today (all in docs/backlog_cache.md, not fixed same-day unless noted)
1. **`add_node`'s NULL-based dedup silently fails for `TrailingBoth` nodes** — caused 15 real
   duplicate `soxl_test` watch_list rows this morning (SPY/SH/ERX/ERY/GDXD ×4 each). **Fixed today**:
   duplicates removed (kept earliest id per ticker), root cause (NULL != NULL in the UNIQUE
   constraint) backlogged for a real code fix later.
2. **Same-ticker cumulative BUY notional cap gap** — elevated priority, real evidence from today
   (resting `TRAILING_STOP` orders didn't move `availableFunds`). Not built.
3. **SL alert falls back to a generic guess instead of querying the real broker order book** — found
   on SPY's SL alert. Broader ask: split into automated-position vs. manual-position alert paths.
4. **`TrailingBoth` fills never get a real automated broker-side stop-loss placed** (only
   `TrailingExit`/market-buy fills do) — affects ERX/ERY/GDXD/GDXU. Plan: small real test Monday.
5. **`check_buy_reminders`' fill-reminder suppression uses a stale/wrong hourly-cache trigger
   estimate** — confirmed on GDXU, silently suppressed a reminder for an order that had already
   filled for real.
6. **Paper trading is fully dormant system-wide** — side effect of the 2026-07-23 all-`mode='live'`
   decision colliding with an earlier (~2026-07-18) "research mode = paper trading" decision. Also
   silently broke canary's intended design (canary = paper trading with extreme params, not a 4th
   mode). Revisit Monday.
7. **15 of 24 `mode='live'` nodes had no account assigned at all** (`account=None`), fail-closed as
   BLOCKED instead of producing a useful dry_run message. **Fixed today** (backfilled to `ira`,
   canary SPY moved to `brokerage`/IVV to avoid a collision).
8. **`_sync_confirm_and_protect` fires a false "UNPROTECTED" alarm for dry_run market-buys** — it
   unconditionally polls the real broker for a fill confirmation even when nothing was actually
   placed (dry_run). Demonstrated live on VOO. Not fixed.
9. **Real BUY/SELL Slack alerts carry no canary tag** — unlike the Reference Report or paper
   trading's console tag. Demonstrated live on IVV. Broader ask: tag every message with its real
   mode (Live/Dry-run/Paper) as a standing policy, not just canary.

## Code fixes made today (see git diff for exact changes, Opus-reviewed)
1. **Async order placement/cancellation verification** (`schwab_client.py`) — every real
   placement/cancel now polls the real status (4 attempts/0.5s) before posting to Slack, instead of
   trusting the initial HTTP response. A confirmed REJECTED/CANCELED/EXPIRED now raises
   `schwab_client.OrderRejected`, caught by every real call site (falls back to manual /
   `db.clear_pending_buy`, matching the existing SafetyViolation-handling pattern) so a rejected
   order never gets marked as a successful placement.
2. `_confirm_order_status` retries through all 4 attempts on a transient poll error instead of
   giving up on the first one (Opus review finding).
3. `place_stop_loss` now passes the stop price as a string, not a float (schwab-py deprecation fix).
4. `check_gap_resize` gained an `except schwab_client.OrderRejected` branch (previously only caught
   `SafetyViolation`) — clears the phantom `pending_buys` row instead of leaving it to nag forever.
5. **Second Opus review (session wrap) found a real gap in the cancel-confirmation fix itself**:
   `cancel_order` confirmed the real status but callers (`check_gap_resize`, `_attempt_automated_sell`)
   proceeded as if the cancel succeeded regardless. Fixed: `cancel_order` now returns
   `(response, confirmed_status)`; both callers abort (don't place a replacement/new order) unless
   `confirmed_status == 'CANCELED'`. Two distinct real consequences this closes: (a) `check_gap_resize`
   — a genuine **double-buy** risk (both the original trailing-buy and the replacement market order could
   fill for real money) if the cancel silently didn't take effect; (b) `_attempt_automated_sell` — **not**
   an actual oversell (confirmed empirically 2026-07-23 night that Schwab rejects a real oversell attempt,
   e.g. SPY 4-vs-3-held), just a rejected/wasted order attempt (now cleanly caught as `OrderRejected`
   anyway) that would've left the position without its intended new protection — and even that has an
   independent backstop: `signals_notify.check_live_state_reconciliation` runs every poll cycle and
   specifically detects an open position missing its expected resting SL/trailing-sell order, alerting
   with a proposed fix. Corrected here after the
   user caught the overstated "oversell risk" framing. 4 existing tests updated for the new return
   signature. Daemon restarted again (pid 671527, 08:20:41) to pick this up before the 9:15 window.

## Nodes built today (`scripts/setup_2026_07_24_soxl_ira_live_test.py`, watch_list_id=65, version='soxl_test', all mode='live', account='soxl_ira')
| id | Ticker | Strategy | Window | Entry timing | Purpose |
|---|---|---|---|---|---|
| 102 | SPY | TrailingBoth | 99 | close | Real SELL exercise (arm/trail automated, SL manual-confirm) on the existing 3-share position |
| 103 | SH | TrailingBoth | 99 | close | Same, on the existing 50-share position (opposite direction hedge) |
| 104 | ERX | TrailingBoth | 20 | close | Real BUY signal + top-up test (trailing-buy path) |
| 105 | ERY | TrailingBoth | 20 | close | Same, opposite-direction pair to ERX |
| 106 | LABD | TrailingExit | 20 | open_check | Real market-buy path test (`_attempt_automated_market_buy`), pinned check fires 9:30:02 |
| 107 | GDXD | TrailingBoth | 20 | close | Gap-resize (Part 3 branch B) test ticker #1 |
| 108 | GDXU | TrailingBoth | 20 | close | Gap-resize test ticker #2 (safety net, opposite direction) |
| 127 | LABU | TrailingExit | 20 | open_check | Added 08:42 ET as a parallel backup to LABD for the same market-buy path test — mirrors LABD's node config exactly (same strategy/window/entry_timing/mode/account), so whichever of the two actually fires exercises `_attempt_automated_market_buy`. `z_score_threshold` lowered 1.5→0.5 on both LABD/LABU ~08:58 ET to raise the odds of a real fire today (LABD's z was +0.99 — wrong sign, won't fire regardless of threshold; LABU's z was -1.08, now past the 0.5 threshold, BUY confirmed as of last check) |

SPY/SH `open_positions` seeded (`entry_price`/`signal_price` = current market price at seed time as a
placeholder, not the user's real cost basis, which isn't recorded in this system) — 3 sh @ $738.18,
50 sh @ $33.52.

`auto_fill_detection` enabled for ERX and ERY (default off) so a real fill gets recorded automatically
via `check_auto_fills`'s poll, without needing a manual "Filled" click.

## Real orders placed today (pre-market, bypassing schwab_safety's signal-window gate — same
deliberate-bypass pattern as 2026-07-23 night's testing, since this is a manually-constructed
overnight-gap scenario, not an organic signal)
| Ticker | Shares | Type | signal_price (yesterday's close, used as running-low reference) | Real trigger | Order ID | Notional |
|---|---|---|---|---|---|---|
| GDXD | 5 | TRAILING_STOP BUY, trail=0.3% | $47.99 | $48.13 | 1007317964232 | ~$232 |
| GDXU | 3 | TRAILING_STOP BUY, trail=0.3% | $79.665 | $79.90 | 1007317964233 | ~$239 |

Both confirmed real and resting (`AWAITING_STOP_CONDITION`) via the real order book. Originally sized
at 10/6 shares (~$943 combined) but halved after confirming real cash is $1,110.43 and that amount
would've left too little for ERX/ERY/LABD/Test-A later — capital budgeting matters since resting
orders may reserve funds (see note below).

**Observation, not yet explained**: `get_account_balance('soxl_ira')` still reported $1,110.43
unchanged immediately after both trailing-buy orders went resting — contrasts with 2026-07-23 night's
finding that Schwab reserves cash for a resting order. Leading theory (per the PLUG boundary-search
finding that night, $1.82-$10.55 buffer, not the full notional): the reservation may be a small buffer
specific to bounded-price orders, not the full notional, and/or may not apply the same way to
`TRAILING_STOP` orders (unbounded execution price). Not confirmed — flagged for follow-up, not
resolved today.

## Sequencing / run of show — with actual outcomes
| Time (ET) | Ticker(s) | Planned action | Actual outcome |
|---|---|---|---|
| Pre-market | GDXD, GDXU | Real trailing-buy orders placed + `pending_buys` seeded | ✅ Done ~08:10 |
| Pre-market | — | Daemon restarted (pid 669431, 08:07) | ✅ Confirmed current |
| 9:15–9:29 | GDXD, GDXU | `check_gap_resize` cancel+replace if gapped | ✅ Ran correctly, did nothing (neither had gapped) — cancel+replace path itself NOT exercised |
| ~9:30 (check-in) | GDXD, GDXU | Sell out if filled / cancel if still resting | Both **filled for real** at the open (unplanned — the ordinary trailing-buy mechanism, not the gap-resize path). GDXD 5sh@$47.39, GDXU 3sh@$80.805. Manually sold: GDXD@$47.3508, GDXU@$80.25. Net loss ~$1.87. Reconciled directly via `db.open_position`/`close_position` (no button — `auto_fill_detection` wasn't on for these two) |
| 9:30:02 (pinned) | LABD, LABU | Real BUY check off session Open | LABD stayed HOLD (wrong-sign z, as expected). LABU fired BUY (z=-1.09) but **automated placement crashed** — sizing bug (`starting_notional=$22` → 0 shares for a ~$255 stock). No real order exists; manual fallback also showed 0 shares, unusable. Skipped. |
| 9:30 onward (ambient) | SPY, SH | Real SELL monitoring, arm/trail automated | Still open, unarmed as of 10:46 ET — SPY needs +0.3% (~$740.40) to arm, hasn't gotten there. SL alerts (#1-7 so far) being Skipped per user's call — not testing manual SL path today, only arm/trail matters |
| 9:31–9:40 | LABD, LABU | Ambient fallback | Same outcome as pinned check — no fire |
| 10:25–10:40 | ERX, ERY | Real BUY signal, automated placement + auto-fill detection | ERX never triggered (z stayed positive). ERY fired (z=-1.82), real trailing-buy order placed (2sh, ~$20), still resting `AWAITING_STOP_CONDITION` as of 10:46 ET, not filled |
| 10:25–10:40 | ERX or ERY | Top-up test (3b), manual | **Not yet attempted** — blocked on ERY actually filling first |
| 10:25–10:40 | 3rd ticker | Second-ticker-BUY guard test | **Not yet attempted** |
| 10:25–10:40 or 15:25–15:40 | ERX/ERY node | Test A: `notional_cap=$1` block-then-restore | **Not yet attempted** |
| Stretch goal | ERX, ERY | Gap behavior at 10:30 bar boundary | Nothing to observe — ERY never gapped, still organically resting |
| 15:25–15:40 | any unresolved | Backup/catch-up window | |
| End of day | all | Restore `notional_cap=$800`, final cash reconciliation, backfill `coverage_events`, update this doc | |

## Known gaps / open items (not blocking, tracked separately in backlog_cache.md)
- Morning Report `invalid_blocks` failure (23 nodes now exceeds Slack's 50-block cap) — user's call:
  fix via ticker-scope reduction later, not another patch today.
- Cash-reservation-for-trailing-orders question (above) — not resolved.
- Idea: permanent canary tickers in the dormant `ira` account as a standing regression test —
  design conversation for later.
- Gap-resize test's `signal_price` used yesterday's cached close as the running-low reference
  (deliberate, to genuinely test the overnight-gap scenario rather than a same-day one).
- **Cancel+replace path not actually exercised today**: `check_gap_resize` ran correctly at 9:15-9:29
  (confirmed via `pending_buys` — both GDXD/GDXU still show their original `order_id`, no cancel
  attempt made) but neither ticker had genuinely gapped past its trigger by real price, so it correctly
  did nothing. The real cancel-and-replace-with-MARKET code branch remains untested this cycle —
  needs a future day where a real gap-through actually occurs.
