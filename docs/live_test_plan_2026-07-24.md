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
   anyway) that would've left the position without its intended new protection. Corrected here after the
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

## Sequencing / run of show
| Time (ET) | Ticker(s) | Action | Notes |
|---|---|---|---|
| Pre-market | GDXD, GDXU | Real trailing-buy orders placed + `pending_buys` seeded off yesterday's close | Done ~08:10 |
| Pre-market | — | Daemon restarted (pid 669431, 08:07) | Confirmed current vs. all source edits |
| 9:15–9:29 | GDXD, GDXU | `check_gap_resize` runs automatically — cancel+replace with MARKET if either cleared its trigger | Neither had cleared as of ~08:04 (GDXD actually gapped down slightly, GDXU ~flat) |
| ~9:30 (check-in) | GDXD, GDXU | If filled: sell out to recycle capital. If still resting: cancel to release any reservation | Manual step, capital-recycling before the busier 10:25 window |
| 9:30:02 (pinned) | LABD | Real BUY check off session Open — fully automated if it fires (market buy + fill-confirm + real STOP) | Was HOLD (z=+0.87) as of last check |
| 9:30 onward (ambient) | SPY, SH | Real SELL monitoring — arm/trail-sell automated; SL is manual Exited/Skipped only (no resting broker protection since these were manually seeded, not bought through the automated flow) | SPY SL condition already flagged once pre-restart (harmless, not resolved) |
| 9:31–9:40 | LABD | Ambient fallback if the pinned check missed | |
| 10:25–10:40 | ERX, ERY | Real BUY signal (ERY showed BUY, z=-1.57, as of ~08:00) — fully automated placement + auto-fill-detected fill | |
| 10:25–10:40 | ERX or ERY | Top-up test (3b) — **requires manual action**: place a real 1-share BUY + call `_reconcile_fill` directly, since Schwab won't naturally underfill a small resting order | Node `starting_notional` already sized for ~2 shares to create the shortfall |
| 10:25–10:40 | 3rd ticker | Second-ticker-BUY guard test — opportunistic, or manual | |
| 10:25–10:40 or 15:25–15:40 | ERX/ERY node | Test A: temporarily `notional_cap=$1`, confirm block, then retry at `$800` restored (item 4b: confirm the retry isn't wrongly treated as a duplicate) | **Requires manual action** |
| Stretch goal | ERX, ERY | Check gap behavior specifically at the 10:30 bar boundary | Not scoped in detail yet |
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
