# Live Test Coverage Ledger

Standing record of every scenario/code-path in the live-trading automation engine that needs
to be exercised against the *real* live daemon (not just an offline replay or unit test) —
see `automation_principles.md` #10. A row only moves to Verified once actually observed
succeeding in a real live run, with a date and what was observed. Passing in isolation
(unit test, replay script) is a prerequisite, not a substitute — track that separately in the
"Offline coverage" column.

Status values: **Not started** (no live observation, may not even be built) / **Pending**
(built, `dry_run=True`, waiting for a live window to exercise it) / **Verified** (observed
live, date + note).

**2026-07-22: most rows below now also have a queryable backing** — `signals_db.coverage_events`
(new table) is logged at ~18 real control sites across `schwab_safety.check_order`,
`signals_notify.py`, and `paper_trading.py` (`scenario_key`, `mode` ∈ paper/dry_run/live, ticker,
result, detail, timestamp). `scripts/coverage_matrix.py` pivots it (rows=scenario_key,
columns=paper/dry_run/live) so "has X actually fired, when, with what result" is answerable by
query instead of by re-reading this file's prose. This doesn't replace the table below (which
also tracks scenarios that aren't logging-instrumented yet, e.g. open-price-quality) — treat
`coverage_matrix.py` as the live/authoritative answer for any row it does cover, and update this
file's Status column to Verified once a query confirms a real (non-paper, ideally non-dry_run)
observation, same as before.

| Scenario | Code path | Offline coverage | Status | Notes |
|---|---|---|---|---|
| Pinned entry trigger fires at the right bar/price | `active_signals._scan_pinned_entry` | `scripts/verify_pinned_entry_vs_backtest.py` (5/6 tickers clean, AGQ's 1 mismatch explained) | Pending | Needs a live trading day with the daemon actually running this code |
| Real market-order BUY placement + fill confirm | `_attempt_automated_market_buy`, `_sync_confirm_and_protect` | Unit tests only (`test_part4_entry_trigger.py`) | Not started | No real (non-dry_run) order ever placed by this system |
| SL placed at signal price after fill (sync path) | `_place_stop_loss_for_position` via sync confirm | Unit test (SL anchors to `signal_price`) | Not started | Depends on real BUY placement above |
| SL placed via async fallback (timeout path) | `check_auto_fills`/`drain_fill_queue`/`check_gap_resize` fill poll | Unit test only | Not started | This exact path had the "SL never placed" bug found/fixed 2026-07-21 — high value to actually observe live |
| Post-fill top-up places a real order | `_reconcile_fill` | Unit test (`test_part3_gap_resize.py`) | Not started | Real-money risk if untested live: this is the path with the phantom-shares bug fixed this session |
| Overnight gap-resize (cancel trailing buy, replace w/ market) | `signals_notify.check_gap_resize`, `_GAP_CHECK_WINDOW` | Unit tests only | Not started | Needs a real overnight gap past `trail_buy_pct` while daemon is live |
| Exit-arm latency scan (pinned) | `_scan_pinned_exit_arm` | None beyond code review | Not started | |
| Trailing-buy/-stop fill resolution parity vs backtest kernel | kernel + live sizing | `verify_trailing_buy_resolution.py`/`verify_trailing_sell_resolution.py` | N/A (offline-only by design) | Rerun after any kernel/signals_notify/schwab_safety change — already a standing habit, not a live-daemon test |
| Open-price quality (real open vs. what live code captured) | `_scan_pinned_entry` logging → `open_price_quality_log` | `scripts/verify_open_price_quality.py` (script exists, no data yet) | Pending | Deliverable 2 — needs real trading-day data, can't be backfilled |
| Cash-balance check blocks an order correctly | `schwab_client.get_account_balance` + `schwab_safety.check_order` | 8 unit tests (`test_schwab_safety.py`), Opus-reviewed 2026-07-21 | Pending | Built this session; `cashAvailableForTrading` field name still unverified against a real account response — first real (dry_run or live) BUY signal will exercise the fetch even in dry_run, worth checking the raw response then |
| Second-live-ticker-in-one-account BUY correctly blocked | `schwab_safety._has_open_buy_order_in_account` | 3 unit tests (`test_schwab_safety.py`) | Not started | Not reachable today (one ticker per account) — will matter once the one-account-per-ticker rollout or any account-sharing change lands |
| Daemon survives an unhandled exception mid-loop | `active_signals._guarded` + outer try/except in `run_loop` | 6 unit tests (`test_run_loop_fault_tolerance.py`) exercise `_guarded` directly | Not started | Built this session; still no test that runs a real (or simulated) `run_loop` iteration end-to-end with a failing section — only the isolated helper is tested |
| Duplicate-order guard doesn't false-block a legitimate top-up | `schwab_safety` quantity-aware guard | Unit tests (`test_schwab_safety.py`) | Not started | Only exercised in unit tests so far, not against real order timing |
| Live-state reconciliation (position/order-book mismatch detection) | not built | N/A | Not started | Still just a design idea in backlog |
| Automated sell correctly skipped for a non-live-mode node's position | `signals_notify._attempt_automated_sell` mode check | 2 new unit tests (`test_schwab_automation.py`) | Not started | Built this session (2026-07-21); no ticker has ever hit this exact scenario live (today's automation-scope tickers only run mode='live' nodes) |
| Live-state reconciliation detects and alerts on a real mismatch | `signals_notify.check_live_state_reconciliation`, `schwab_client.get_real_position` | 8 unit tests (`test_live_state_reconciliation.py`) | Not started | Built 2026-07-22; no real mismatch has ever been observed (every account `dry_run=True`); `get_real_position`'s `longQuantity`/`positions` field names also unverified against a real account response |
| Trailing-arm state survives `notify_trailing_activated` without re-arming on the next bar | `signals_notify.notify_trailing_activated` (re-reads via `signals_db.get_position_by_id`) | 1 unit test (`test_schwab_automation.py`) | Not started | Fixed 2026-07-22 (Opus review, critical): a stale-state overwrite was clobbering `trailing`/`peak` right after arming, causing re-arming and a second live trailing-sell order for the same shares. Never observed live either way — only ever ran in dry_run/unit-test form |
| Second live SELL order for the same ticker correctly blocked | `schwab_safety._has_open_sell_order` (SELL-side resting-order guard) | 2 unit tests (`test_schwab_safety.py`) | Not started | Built 2026-07-22 as the structural fix that prevents the trail_state bug above (or anything else) from stacking two real exit orders |
| Manual SL-price fallback alert fires correctly when trailing-sell placement fails post-SL-cancel | `signals_notify._attempt_automated_sell` | 1 unit test (`test_schwab_automation.py`) | Not started | Built 2026-07-22; deliberately no auto-recovery here (user's call) — needs a real failed placement to confirm the alert text/price are actually useful in practice |
| Poll loop and Slack handler can't double-open/double-close the same position | `signals_db._position_lock` around `open_position`/`close_position` | 3 unit tests (`test_db_roundtrip.py`) | Not started | Fixed 2026-07-22 (Opus review); race window is narrow and never observed causing a real duplicate, but the lock is now structurally in place |
| Duplicate-order retry after a real rejected/failed order isn't wrongly blocked | `schwab_safety._broker_confirms_order` | 2 unit tests (`test_schwab_safety.py`) | Not started | Fixed 2026-07-22 — closes the "bigger structural change" backlog item; needs a real rejected order to confirm the retry path in practice |
| Fast-path (websocket) fill reconciliation doesn't act on a partial/in-flight execution | `signals_notify.drain_fill_queue` (re-confirms via `get_filled_order` poll instead of trusting the raw stream message) | 3 unit tests (`test_part3_gap_resize.py`) | Not started | Fixed 2026-07-22 — the stream message's own `filledQuantity` may represent one partial execution of a still-filling order (unverified cumulative-vs-incremental semantics); needs a real multi-execution fill to confirm the poll-reconfirm path in practice |
| `same_day_block` skips correctly for margin accounts, still blocks cash accounts | `schwab_safety.check_order` (`AccountLimits.account_type`) | 2 unit tests (`test_schwab_safety.py`) | Not started | Built 2026-07-22; no real same-day re-buy has ever been attempted live in either account type |

## How to update this
- New automation feature → add a row when it's built (even if `Status: Not started`).
- A scenario moves to **Verified** only after a real live occurrence is confirmed (Slack log,
  trade_log row, or direct observation) — cite the date and what was seen.
- Don't delete a row once something regresses it back to unverified — note the regression
  instead, so history isn't lost.
