# Real-Order Execution for Drought-Overlay Entry and Margin Add-On Leg — Implementation Plan

Planning only, produced by an Opus planning pass 2026-08-1x (no code written). Structured per
`docs/new_mechanism_promotion_standard.md`. Pointer from `docs/design.md`'s "Real-order execution
edit plan" addendum.

---

## Part 0 — Research findings that shape the plan (verified by direct read)

These are the load-bearing facts. A future session should not re-derive them.

### 0.1 The real entry flow, both variants

`signals_notify.notify_buy_signal` (`signals_notify.py:1286-1412`) is the single real entry dispatcher. At line 1319 it branches on `db._is_trailing_buy(node)` (`signals_db.py:1564`, which reads `strategies.resolve_axis_columns(node['strategy'])[0] == 'trail_buy_pct'`):

- **Trailing-buy** (`TrailingBothZScoreBreakout`): `buy_order_sizing` → `_attempt_automated_buy` (`signals_notify.py:32`) → `schwab_client.place_trailing_buy` → `db.add_pending_buy(...)` + `mark_pending_buy_placed_by_wl_id`. Fill arrives later, either via the Slack "Filled" button (`signals_handlers.handle_trail_buy_fill_price`, `signals_handlers.py:128-215`) or automatically via `_reconcile_buy_fill` (`signals_notify.py:2250`). Both call `db.open_position(...)` then `_place_stop_loss_for_position(node, ticker)`.
- **Market-buy** (`TrailingExitZScoreBreakout`, e.g. DPST id=136): `_attempt_automated_market_buy` (`signals_notify.py:603`) → `schwab_client.place_equity_buy` → `add_pending_buy` → `_sync_confirm_and_protect(ticker, node, order_id)` (`signals_notify.py:821`), which synchronously polls for the fill and routes into `_reconcile_buy_fill`. The 2026-08-01 fix is the `if ticker in AUTOMATION_ENABLED_TICKERS: _place_stop_loss_for_position(...)` block at `signals_handlers.py:326-338`, mirroring the identical block at `signals_handlers.py:187-200`.

**Conclusion for drought entry**: it must mirror whichever branch the node's own strategy selects, not a fixed choice — drought reuses the same node's strategy/params, so the entry order shape must too. Route drought entry through the SAME `notify_buy_signal`-shaped dispatcher, not a parallel one.

### 0.2 The real exit flow already covers a drought position with zero new code

`get_open_positions()` has no `position_source` filter, and `_attempt_automated_sell` / `_attempt_automated_exit_sell` / `check_sell_condition` / `_place_stop_loss_for_position` all key on `pos['wl_id']` / `pos['ticker']`. A `position_source='drought_overlay'` row is structurally identical to a core row (`signals_db.open_drought_overlay_position`, `signals_db.py:3078-3127`, is a thin wrapper over `open_position`). **SL / TRAIL / TIME exits for a real drought position need no new code.** Only HANDOFF is new. Major scoping win — state this explicitly in the design doc entry.

### 0.3 Only ONE real account can execute either mechanism

`schwab_safety.ACCOUNTS` (`schwab_safety.py:157-172`):

| account | dry_run | account_type | notional_cap |
|---|---|---|---|
| brokerage | True | margin | 10,000 |
| sep | True | cash | 10,000 |
| roth | True | cash | 50,000 |
| ira | True | cash | 75,000 |
| **soxl_ira** | **False** | **margin** | **3,000** |

`soxl_ira` is the only account that is both margin-capable and live. `brokerage` is margin but dry-run. Every other account is cash and structurally cannot margin-borrow. All `mode='live'` nodes on `soxl_ira` are `entry_timing='open_check'` (ids 134,135,136,143,152,153,154,155,156). The 17 staged overlay-test nodes (167-183) are all `mode='research'` on account `ira`.

**Conclusion**: add-on's real path must hard-refuse a non-`margin` `account_type` in `schwab_safety` itself, not rely on Schwab rejecting it.

### 0.4 The `check_order` double-buy guard, and what it actually protects against

`schwab_safety.check_order:868` — `if _local_pos and not is_protective: raise SafetyViolation(...)`, logging `buy_blocked_position_exists`.

Incident it protects against (`deep_backlog.md:354-360,4448-4474`): confirmed live 2026-07-24, two real resting `TRAILING_STOP` BUYs (GDXD 5sh, GDXU 3sh) left `get_account_balance` completely unchanged — Schwab does not decrement buying power for a resting order. `notional_cap` and the cash check (reading that same undecremented balance) can both pass for two independent orders competing for the same dollars. The 2026-08-02 guard closes the post-fill half; `_has_open_order`/`_has_open_buy_order_in_account` cover the pre-fill half.

**Two guards will block a real add-on BUY, not one** — the single most important finding here:

1. `check_order:868` — the existing-position guard (the obvious one).
2. `check_order:876` — `_has_open_order(orders, ticker, ...)`. Its docstring (`:666-681`): "True if any resting order in `orders` is for this ticker, regardless of side." At the exact moment add-on triggers (core just armed), the core position ALWAYS has a resting protective SELL at the broker — its `sl_order_id` STOP or the arm-time `TRAILING_STOP` SELL `_attempt_automated_sell` just placed. So `_has_open_order` blocks the add-on BUY 100% of the time, guaranteed by construction. Not hypothetical — same failure shape as the 2026-07-28 SH self-block (`deep_backlog.md:1484`) and the 2026-08-01 top-up signal-window block (100% real-world failure rate before the fix). A plan that only exempts guard #1 reproduces that failure.
3. **Signal-window BUY gate** (`check_order:998`). An arm event fires whenever price crosses `arm_pct` — almost never inside a signal window. Third guaranteed block.
4. **Cash check** (`check_order:1032`). `schwab_client.get_account_balance` (`:695-718`) reads `cashAvailableForTrading`/`availableFunds`, never `buyingPower`. An add-on sized at 100% of an already-deployed position is, by definition, borrowing — `availableFunds` will frequently be below `notional + CASH_SAFETY_BUFFER`. The check will refuse the entire point of the mechanism.

### 0.5 `pending_buys` has no `position_source`

Schema at `signals_db.py:655-690`. `add_pending_buy` persists only `_PENDING_BUY_NODE_KEYS`. Every fill consumer (`_reconcile_buy_fill:2338`, `handle_trail_buy_fill_price:169`, `handle_entry_price:283`) calls `db.open_position(node, ...)` with the default `position_source='core'`. A real drought entry filling through this path would silently land as `position_source='core'`, destroying the discriminator. `pending_buys` needs the discriminator + drought config snapshot. One row per wl_id remains correct (drought/core mutually exclusive per wl_id, enforced by `open_position`'s dedup) — no parallel table needed.

### 0.6 `_scan_buy_signals` will burn core's daily alert slot during a real HANDOFF

`active_signals.py:366` — `already_held` check; `:385-388` sets `buy_alerted.add(alert_key)` BEFORE that check, and the `already_held` branch never discards it (unlike `already_pending` at `:403`). In paper, HANDOFF completes synchronously so this never bites. In real mode a HANDOFF spans polls (needs a confirmed real SELL fill) — core's signal would hit `already_held`, burn the day's alert slot, and never re-fire. Must be fixed as part of the wiring.

### 0.7 `_last_sale_recovery` does not filter `position_source`

`signals_helpers.py:217-247` — queries `trade_log` by `(ticker, strategy, version, window, account)` ORDER BY exit_time DESC LIMIT 1, ignoring `position_source`. Once real drought trades exist, a drought exit's proceeds would silently size the next core entry and vice versa. No-op today; fix now while free.

### 0.8 `closed_today` / same-day re-buy interacts with HANDOFF

`check_order:830` blocks a same-day BUY for a cash account; `:838` skips for margin. A HANDOFF closes drought and re-enters core same-day, same ticker — a hard block on a cash account. Real drought is only coherent on a margin account (`soxl_ira`), same conclusion as add-on, arrived at independently.

### 0.9 Where paper is wired today (wire the real path alongside, not instead of)

- `active_signals.py:988` — HANDOFF inside the pinned-bar block.
- `active_signals.py:1214` — HANDOFF in the ambient/open-check loop; `open_position_keys` refreshed at `:1216-1218`.
- `active_signals.py:1250` — drought ENTRY, after the core scans.
- `paper_trading.py:1428` — `check_paper_addon_trigger` from `check_paper_sells` on `just_activated_trailing`.
- `paper_trading.py:1455` — `close_paper_addon_leg_if_open` right after `db.close_position`, deliberately after the core exit's own coverage event + alert.

Both `check_paper_drought_entry`/`check_paper_drought_handoff` hard-return for `mode=='live'` — those guards stay. New real functions are siblings gated on `mode=='live'`, so a research node is unaffected.

### 0.10 `addon_legs` schema (real table exists, insufficient as-is)

`signals_db.py:993-1011`: `id, wl_id, parent_position_id, parent_trade_log_id, ticker, account, shares, entry_price, entry_time, status, exit_price, exit_time, exit_reason, pnl_pct, is_dry_run_sim`. Missing: `entry_order_id`, `exit_order_id`, `sl_order_id`/`broker_stop_price` (if D3 → stop), a placed-vs-filled status. `status` is `CHECK (status IN ('open','closed'))` — add a separate nullable `entry_status` column rather than widening that CHECK, to avoid a rebuild on a live table.

---

## Part 1 — Decisions requiring the user's sign-off before any code is written

**D1. Add-on's `check_order` exemption shape.** Recommend a new, narrow `is_addon_leg` flag — NOT `is_protective` (which also bypasses `daily_order_cap`, semantically wrong for a risk-*adding* order). See Part 3 for the non-abusable design.

**D2. Cash/margin check basis for the add-on order.** Existing cash check refuses nearly every real add-on. Options: (a) new `schwab_client.get_account_buying_power` reading `buyingPower`, gated to the `is_addon_leg` path, with a new `ADDON_BUYING_POWER_HEADROOM_MULT` (recommend 2.0 — never consume more than half available buying power); (b) don't special-case it, accept add-on only fires when idle cash happens to exist (diverges from the validated backtest). Recommend (a); field name unverified against a real Schwab response.

**D3. Does the real add-on leg get its own broker-side protective stop?** (a) no stop, lockstep-only (matches the validated model exactly, but is the only real position in this system with zero broker-side protection); (b) stop at the parent's stop price, accepting an occasional early exit the model didn't validate. Recommend (b) — an unstopped margin position is a bigger error than an early leg exit. Needs a real decision.

**D4. Real drought sizing basis.** Paper uses `starting_notional // price`. Confirm against `scripts/stacked_model/drought.py::generate_drought_trades` what compounding basis the validated research actually used before picking live's formula — filter `_last_sale_recovery` to `position_source='core'` either way (0.7).

**D5. `soxl_ira`'s `notional_cap` is $3,000.** Add-on doubles exposure on a node already at 2,500 (HIBL/YANG) — individually under cap, combined position is 5,000, unchecked by any existing guard. Decide whether add-on needs a new combined-exposure ceiling.

**D6. Which node hosts the first staged real test** (Part 12).

---

## Part 2 — Phase 1: Real DB schema

All edits in `signals_db.py`, inside `ensure_tables()`, following the existing idempotent `PRAGMA table_info` + `ALTER TABLE` pattern.

### 2.1 `pending_buys` — carry the drought discriminator through the fill

Add: `position_source TEXT NOT NULL DEFAULT 'core' CHECK (...)`, `drought_confirm_days INTEGER`, `drought_vol_gate REAL`, `drought_gap_start TEXT`, `drought_vol_pctile REAL`. (If SQLite can't add a CHECK via `ALTER TABLE ADD COLUMN` in this version, enforce in `add_pending_buy` instead, matching how `open_positions.position_source` got its constraint at table-creation time.)

- `add_pending_buy` — add the new kwargs, persist them; existing call sites unaffected (defaults preserve current behavior).
- `get_pending_buys` — already `SELECT *`, no edit needed.
- New: `get_drought_pending_buy(wl_id)` — mirrors `get_pending_buy_by_wl_id` + `AND position_source='drought_overlay'`.

### 2.2 `addon_legs` — real-execution columns

Add to `addon_legs` only (not `paper_addon_legs` — asymmetry documents the real/paper difference): `entry_order_id INTEGER`, `exit_order_id INTEGER`, `sl_order_id INTEGER` (if D3→stop), `broker_stop_price REAL` (if D3→stop), `entry_status TEXT DEFAULT 'filled' CHECK (entry_status IN ('placed','filled','abandoned'))`.

- `open_addon_leg` — add `entry_order_id=None, entry_status='filled'` kwargs; paper call site unchanged (default keeps old behavior).
- New: `set_addon_leg_entry_filled(leg_id, entry_price, entry_status='filled')`.
- New: `set_addon_leg_exit_order_id(leg_id, order_id)`.
- New: `get_open_addon_leg_by_wl_id(wl_id, paper=False)`.
- `close_addon_leg` — no signature change. Its `_ADDON_MARGIN_COST_FLAT_PCT=0.04` haircut is a modelled cost; for a real leg the true margin interest is knowable — log it as a reconciliation input, don't change the modelled value (parity with the validated model matters more than precision here).

### 2.3 `_last_sale_recovery` position_source filter

`signals_helpers.py:236-241` — add `AND position_source='core'` + a `position_source` param defaulting to `'core'`. No-op against today's real data (zero non-core rows in `trade_log`) — verify with a row count before/after.

### 2.4 Migration verification

Per `docs/design.md`'s existing convention: verify idempotent against a fresh DB and a COPY of `trading_live.db`. Never migrate the real live DB in-session — lands automatically on the next `ensure_tables()` call at the user's daemon restart.

---

## Part 3 — Phase 2: `schwab_safety` / `schwab_client` changes

Highest-risk file in the repo. Every change is additive, gated behind an explicit new flag that defaults off.

### 3.1 `schwab_safety.check_order` — new `is_addon_leg` parameter

Signature (`:747-750`) gains `is_addon_leg: bool = False`; `approve_and_record` (`:1133-1136`) threads it through.

**The flag is a request to run stricter checks, not a trust token.** When `is_addon_leg=True`, run these preconditions BEFORE any exemption, verified against DB/broker, never against the caller's claim:

1. `limits.account_type == 'margin'` else `raise SafetyViolation`. Log `addon_non_margin_account_blocked`. The hard refusal — permits only `brokerage` (dry-run) and `soxl_ira` (live) today.
2. Local position exists AND `position_source == 'core'` (inverse of the normal guard) — an add-on against a drought position is out of scope, already enforced paper-side.
3. `(trail_state or {}).get('trailing') is True` — parent must genuinely be armed. This is what makes the exemption non-abusable: only reachable in a state `notify_trailing_activated` produces.
4. `get_open_addon_leg_by_parent(...) is None` — one leg per parent, ever.
5. `quantity == int(local_pos['shares'])` — EXACT equality, not `<=`. Caps total exposure at exactly 2x by construction. Log `addon_size_mismatch_blocked` on failure.

Only after all five pass:

- **Exemption A** — skip the existing-position guard: `if _local_pos and not is_protective and not is_addon_leg:`.
- **Exemption B** — skip ONLY the SELL side of `_has_open_order`. Add `_has_open_buy_order_for_ticker(orders, ticker, exclude_order_id=None)` (mirrors `_has_open_sell_order`'s shape for the BUY side), use it in place of `_has_open_order` when `is_addon_leg`. Preserves the full 2026-07-24 protection (specifically about two resting BUYs) while unblocking the one shape guaranteed present. Same same-side-only reasoning already precedented by `_has_open_sell_order`'s own docstring.
- **Exemption C** — skip the signal-window gate (`and not is_addon_leg`). Trading-day gate stays unconditional (already unconditional even for `is_gap_correction`).
- **Exemption D** — cash check, per D2: on `is_addon_leg`, call `get_account_buying_power` instead of `get_account_balance`, require `buying_power >= notional * ADDON_BUYING_POWER_HEADROOM_MULT`, fail closed on fetch error exactly as today, log via the same `cash_check` key with a distinguishing detail (or a new `addon_buying_power_check` key).

**Not exempted, deliberately**: kill switch, account allowlist, `_live_ticker_accounts`, `node_automation_enabled`, `AUTOMATION_ENABLED_TICKERS`, `_has_open_buy_order_in_account` (a resting BUY for a DIFFERENT ticker in this account still blocks), trading-day gate, `HARD_ORDER_CEILING`, `notional_cap`, `daily_order_cap`, burst cap, duplicate-order window. Document this list in the docstring, matching `is_protective`'s own precedent.

### 3.2 `schwab_client` changes

- `_place_equity_order` — add `is_addon_leg: bool = False`, pass through; include `"ADD-ON "` in the Slack label.
- `place_equity_buy` — add `is_addon_leg` passthrough.
- `place_trailing_buy`/`_place_trailing_order` — no change (add-on is always a market order, must execute at the arm instant to match the model's sizing).
- New `get_account_buying_power(account) -> float` — mirrors `get_account_balance` exactly, reading `buyingPower`, raise on missing, same "unverified against a real response" caveat.
- `cancel_order` — no change needed, already returns `(response, confirmed_status)`.
- `replace_equity_order_with_market` — no change needed, already handles the SELL side with `replacing_order_id`.

### 3.3 `signals_invariants.py`

New startup invariant: no node may have `addon_enabled=1` AND `mode='live'` AND `account_type != 'margin'`. Same for `drought_overlay_enabled=1 AND mode='live' AND account_type=='cash'` (0.8). Fail loud at daemon start, not at first arm event.

---

## Part 4 — Phase 3: Real drought-overlay entry

New code in `signals_notify.py` (order orchestration + Slack), called from `active_signals.py`. `paper_trading.py` is NOT modified for the real path — its own docstring states it never calls `schwab_client`/`schwab_safety`, and that invariant should hold.

### 4.1 Extract the drought decision from the paper action

Refactor `check_paper_drought_entry` (`paper_trading.py:148-249`): extract the eligibility decision (last core exit → checkpoint-bar count → once-per-gap dedup → vol gate → price resolution, `:186-236`) into a new pure function `evaluate_drought_entry(node, paper: bool) -> dict | None`, returning `{'price','shares','confirm_days','vol_gate','vol_pctile','gap_start'}` or `None`. `check_paper_drought_entry` becomes a thin caller. Both tracks then share ONE eligibility implementation — makes promotion-standard item 3's "the SAME state machine real code will use" literal rather than asserted.

Adjustments, parameterized on `paper`: the `mode=='live'` early-return moves to the callers; existence checks use `get_open_position_by_wl_id(wl_id, paper=paper)` / `get_pending_buy_by_wl_id` for real; `_drought_trade_exists_for_gap`/`_last_core_exit_time` need a `paper` flag to read the right tables. Sizing per D4.

### 4.2 New `signals_notify.notify_drought_buy_signal(node, decision)`

Modelled on `notify_buy_signal:1286`, reusing its helpers:

```
sizing = buy_order_sizing(node, sig_like, target_notional=<per D4>)
if db._is_trailing_buy(node): auto_placed, order_id = _attempt_automated_buy(node, sizing)
else:                         auto_placed, order_id = _attempt_automated_market_buy(node, sizing)
db.add_pending_buy(node, sig_like, channel, ts, order_id=order_id, position_source='drought_overlay', ...)
if auto_placed and trailing_buy: db.mark_pending_buy_placed_by_wl_id(node['id'])
elif auto_placed:                _sync_confirm_and_protect(ticker, node, order_id)
```

Drought entry mirrors BOTH core mechanisms, dispatching on `db._is_trailing_buy(node)` exactly as core does — because drought reuses the node's own strategy, the entry order shape must follow the node's own `entry_timing`/strategy. For the current staged candidates (SOXL, `TrailingBothZScoreBreakout`) that's the trailing-buy path. Do not hardcode it.

No separate Slack block builder needed (buttons resolve by `node['id']`, drought/core mutually exclusive per wl_id) — but the message TEXT must say DROUGHT so a human tapping "Filled" knows what they're confirming.

### 4.3 Thread `position_source` through every fill consumer

Three sites call `db.open_position` from a `pending_buys` row — each must dispatch on `pending['position_source']`:

1. `signals_notify._reconcile_buy_fill:2338` — call `db.open_drought_overlay_position(...)` instead of `open_position` when `position_source=='drought_overlay'`. The subsequent top-up + `_place_stop_loss_for_position` work unchanged (position_source-agnostic — verify by reading, don't assume).
2. `signals_handlers.handle_trail_buy_fill_price:169` — same dispatch. Primary real path for a trailing-buy node.
3. `signals_handlers.handle_entry_price:283` — same dispatch.

Extract the dispatch into one shared helper (e.g. `signals_db.open_position_from_pending(...)`) so the three sites can't drift — this is exactly the drift pattern that produced the take_profit/trail_buy_pct column-overload bug found 3x in this codebase's history.

### 4.4 New `signals_notify.check_drought_entry(node)`

```
def check_drought_entry(node):
    if node.get('mode') != 'live': return
    if not node.get('drought_overlay_enabled'): return
    if node['ticker'] not in schwab_safety.AUTOMATION_ENABLED_TICKERS: return
    if not schwab_safety.node_automation_enabled(node['id']): return
    if node.get('daily_sync_halted_at'): return
    decision = paper_trading.evaluate_drought_entry(node, paper=False)
    if decision is None: return
    notify_drought_buy_signal(node, decision)
    db.log_coverage_event("drought_entry", _coverage_mode(node.get('account')), ...)
```

Exact mode-symmetry with `check_paper_drought_entry` (which returns early on `mode=='live'`).

### 4.5 Entry-abandon for a real drought trailing-buy

`check_entry_abandon` (`:960-1148`) already handles cancel/raced-fill/unconfirmed-cancel generically over `get_pending_buys()`. A drought row flows through unchanged EXCEPT the raced-fill branch calls `_reconcile_buy_fill`, which after 4.3 correctly opens a drought row — verify, likely zero edit. Add `position_source` to the `entry_abandon_timeout` detail. **Important**: an abandoned drought entry must not consume the once-per-gap allowance — confirm `_drought_trade_exists_for_gap` reads `trade_log`/`open_positions` (never written by an abandoned attempt), add a truth-table test.

---

## Part 5 — Phase 4: Real drought HANDOFF

The genuinely new race. Paper's HANDOFF is a synchronous DB write; real has three distinct states.

### 5.1 New `signals_notify.check_drought_handoff(node)`

```
def check_drought_handoff(node):
    if node.get('mode') != 'live': return
    if not node.get('drought_overlay_enabled'): return
    pending = db.get_drought_pending_buy(node['id'])
    pos     = db.get_drought_overlay_position(node['id'], paper=False)
    if pending is None and pos is None: return
    sig = compute_buy_signal(node)
    if sig is None or sig['signal'] != 'BUY': return   # copy VERBATIM from check_paper_drought_handoff:296 -- inline comment there documents the 2026-08-09 CRITICAL bug this guards against
    ...
```

### 5.2 Case A — drought entry order still resting, unfilled

Mirror `check_entry_abandon`'s cancel logic (`:1085-1122`) rather than writing fresh:

```
if not limits.dry_run and pending.get('order_placed') and not order_id:
    return   # manual placement, no id on file -- alert for manual cancel, do NOT clear the row (verbatim :1063-1083)
if order_id and not limits.dry_run:
    _, status = schwab_client.cancel_order(account, ticker, order_id)
    if status == 'FILLED':   # raced -- reconcile as a real drought fill, fall into Case B this same poll
    if status != 'CANCELED': # unconfirmed -- log, RETURN, retry next poll -- never discard real broker truth
db.clear_pending_buy_by_wl_id(node['id'])
db.log_coverage_event("drought_handoff", mode, result="cancelled_resting_entry", ...)
```

### 5.3 Case B — drought position filled and open

A real market SELL, `reason='HANDOFF'`. Strongly prefer calling `_attempt_automated_exit_sell` (`:175-374`) with the new reason rather than a parallel exit path — it already resolves the resting order id, uses the atomic `replace_equity_order_with_market`, repoints `sl_order_id`, clears `broker_stop_price`. `'HANDOFF'` falls through its generic TP/SL/TIME shape correctly (paper already writes this exit_reason). Verify no CHECK constraint rejects it.

**The DB row must not close until the fill is confirmed** — follow `notify_sell_signal`'s poll pattern (`:1505-1521`): only call `db.close_position(..., exit_reason='HANDOFF')` on a confirmed fill; otherwise persist `trail_state['exit_pending']` and let `check_own_sell_fills`/`check_auto_fills` close it on a later poll. This is the key structural difference from paper — document it in the function's docstring.

### 5.4 The ordering contract in real mode, and the alert-slot fix

Real contract: HANDOFF *initiates* the exit before core's scan runs; core's entry is expected on a LATER poll once the exit fill confirms. Requires the fix from 0.6 — in `_scan_buy_signals`'s `already_held` branch, discard the alert key when the blocker is a drought position with a HANDOFF in flight:

```
if already_held:
    _blocking = db.get_open_position_by_wl_id(node['id'])
    _handoff_in_flight = (_blocking and _blocking.get('position_source')=='drought_overlay'
                          and (_blocking.get('trail_state') or {}).get('exit_pending',{}).get('reason')=='HANDOFF')
    if _handoff_in_flight or db.get_drought_pending_buy(node['id']):
        buy_alerted.discard(alert_key)   # same reasoning as the already_pending branch at :403
```

Note this fix is necessary but not sufficient alone — while the exit SELL is resting, `_has_open_order` blocks core's BUY anyway, so the alert must survive until the poll AFTER the exit confirms. `buy_alerted` is run_loop-scoped (survives across polls within the day) and `entry_timing='open_check'` gives a second same-day window.

### 5.5 Wiring — three sites, exactly parallel to paper, inverse mode gate

At each existing HANDOFF/ENTRY call site (`active_signals.py:988,1214,1250`), add the real sibling under `if node.get('mode')=='live':` with the paper call kept in the `else`. Do NOT replace the paper calls — a research node's behavior is unchanged. `open_position_keys` refresh at `:1216-1218` must still run for both branches.

---

## Part 6 — Phase 5: Real add-on entry

### 6.1 New `signals_notify.check_addon_trigger_real(pos, current_price)`

Called from `notify_trailing_activated` at the very END, after `db.update_position_trail_state`, wrapped in try/except that never re-raises (precedented reasoning at `paper_trading.py:1443-1450` — a review found running the follow-on before the core event's own observability could lose the core arm's coverage event/alert entirely).

```
def check_addon_trigger_real(pos, current_price):
    node = db.get_watch_list_node_by_id(pos.get('wl_id'))
    if node is None or node.get('mode') != 'live': return
    if not node.get('addon_enabled'): return
    if pos.get('position_source') != 'core': return
    if db.get_open_addon_leg_by_parent(pos['id'], paper=False): return
    if pos['ticker'] not in schwab_safety.AUTOMATION_ENABLED_TICKERS: return
    if not schwab_safety.node_automation_enabled(pos.get('wl_id')): return
    limits = schwab_safety.ACCOUNTS.get(pos.get('account'))
    if limits is None or limits.account_type != 'margin':
        db.log_coverage_event("addon_entry_placement", ..., result="blocked_non_margin_account", ...)
        _post_message(f"add-on skipped -- '{pos.get('account')}' is not a margin account")
        return
    shares = int(pos['shares'])
    _, order_id = schwab_client.place_equity_buy(pos.get('account'), pos['ticker'], shares, current_price, is_addon_leg=True)
    leg_id = db.open_addon_leg(pos, shares=shares, entry_price=current_price, entry_time=datetime.now(),
                               paper=False, entry_order_id=order_id, entry_status='placed')
```

Double account-type check is deliberate: here for a clean Slack message + a named coverage event, and inside `check_order` as the hard non-bypassable refusal.

`is_dry_run_sim` parent: mirror `update_dry_run_buys`/`_fill_dry_run_buy`'s synthesis convention — `entry_status='filled'`, `entry_order_id=None`, no real order.

### 6.2 Fill confirmation for the add-on leg

Market order, fills near-immediately. Follow `_sync_confirm_and_protect`'s pattern: short synchronous `get_filled_order` poll, then `db.set_addon_leg_entry_filled(leg_id, fill_price)` + `log_coverage_event("addon_entry_fill", ...)`.

**Do not route through `_reconcile_buy_fill`** — that's `pending_buys`-driven and would try to open an `open_positions` row, exactly what the separate-table decision exists to prevent.

Per D3, if the leg gets its own stop: after confirmed fill, place via a new `_place_stop_loss_for_addon_leg(leg, node)` modelled on `_place_stop_loss_for_position` but anchored to the PARENT's `entry_price * (1 - sl_pct/100)` (the leg has no independent exit rule).

### 6.3 Truth-table / cross-mechanism interactions to enumerate explicitly

- `(core armed, drought open)` — unreachable by construction (dedup on wl_id), already asserted paper-side. Assert for real tables too.
- `(addon leg placed-not-filled, parent exits)` — must cancel the leg's resting BUY, never sell shares never bought. Genuinely new, no paper analogue.
- `(addon leg open, parent's exit SELL blocked/failed)` — both stay open, correct; must not be mistaken for desync.
- `(addon leg open, check_live_state_reconciliation runs)` — **required edit, not optional**: `signals_notify.check_live_state_reconciliation:448` compares real broker shares against `open_positions.shares`. With a real leg open, the broker holds 2x what `open_positions` says — will false-positive on every poll unless the check adds the leg's shares to the expected quantity.
- `(addon leg open, get_held_tickers/closed_today/top_up_position called)` — re-verify each still behaves correctly with a real leg row present.

### 6.4 New reconciliation sweep

`signals_notify.check_addon_leg_reconciliation(open_positions)`, called each poll alongside `check_own_sell_fills`/`check_auto_fills`: any `entry_status='placed'` leg past a timeout → poll/cancel/mark abandoned; any open leg whose parent no longer exists but whose parent's trade_log is closed → the lockstep close was missed, ALERT LOUDLY, do not auto-close at a guessed price (pure observation, matching `reconcile_daily_track_nodes`'s stance).

---

## Part 7 — Phase 6: Real add-on leg close (lockstep)

### 7.1 New `signals_notify.close_addon_leg_real_if_open(pos, exit_price, exit_reason, exit_time)`

Mirrors `close_paper_addon_leg_if_open` but places a real order first — cancel the leg's resting BUY if still `entry_status='placed'` (check the returned status; a race to FILLED falls through to a real SELL instead); otherwise replace/place the exit SELL, confirm the fill before calling `close_addon_leg`.

**Divergence to document**: paper closes the leg at the parent's EXACT exit price/reason. Real closes at the leg's own fill price (slippage will differ) — log both so reconciliation attributes the gap to slippage, not a logic bug.

### 7.2 Seven real call sites, not one

Unlike paper's single site, real exits fan out across `signals_notify.py:1513,1625,1948,2602` (`notify_sell_signal` auto + manual, `check_own_sell_fills`, `check_auto_fills`), `signals_handlers.py:489,590` (`handle_exit_price`, `handle_manual_close_price`), and `signals_notify.py:1274` (`check_dry_run_sim_sells`, synthesized only). Every one must call the lockstep close AFTER the core exit's own coverage event/alert, inside a never-re-raising try/except — extract ONE shared helper so seven call sites can't independently drift (a seven-way copy-paste is exactly how the 2026-08-01 "SL placed on one path but not the other" bug happened).

---

## Part 8 — Phase 7: Accountability Grid

The four existing rows (`drought_entry`, `drought_handoff`, `addon_entry_fill`, `addon_exit_fill`) carry no `mode` key and `coverage_check.py` only filters by mode when the scenario sets one — they're already mode-generic. **Do not duplicate them** — real events with `mode='live'`/`'dry_run'` flow into the same rows automatically. Update their `code_path` to name both implementations.

**Add new rows only for genuinely new control points with no paper analogue**: `drought_entry_placement`, `drought_handoff_cancel` (proves the new race is handled — without it the mechanism can't be answered for), `drought_handoff_exit_placement`, `addon_entry_placement`, `addon_double_buy_exemption` (records the five verified preconditions in `detail` every time the exemption fires — `bad_results=[]`, every firing is reviewable; the single most important new row, since it's the accountability record for the widened gate), `addon_exit_placement`, `addon_leg_reconciliation`, `drought_handoff_alert_slot_preserved`.

Extend `scripts/seed_daily_coverage_expectations.py` for the staged real-test node. Every new `log_coverage_event` uses `_coverage_mode(account)`, never a hardcoded string.

---

## Part 9 — Phase 8: `fake_broker` scenario tests

`tests/fake_broker.py` needs no structural extension (no new asset class) except: `FakeBroker.set_buying_power(account, bp)` + include `buyingPower` in `get_account`'s payload, for D2.

**New scenario files** (matching the existing 20 `test_fake_broker_*_scenario.py` naming):

1. `test_fake_broker_drought_entry_scenario.py` — trailing-buy AND market-buy node variants place the right real order shape; `pending_buys` carries `position_source='drought_overlay'`; fill opens a `drought_overlay` row, not `core`; a real STOP rests after. **Regression assertion**: an identical `mode='research'` node places NO broker order.
2. `test_fake_broker_drought_handoff_scenario.py` — the new race, all cases: resting-unfilled → cancel confirmed; resting-unfilled → cancel races to FILLED → falls through to Case B; resting-unfilled → cancel unconfirmed → row untouched, core still blocked; filled-open → replacing market SELL via `replace_order_with_stop_loss`, stays open until confirmed fill, closes `'HANDOFF'`. **Alert-slot assertion**: with HANDOFF in flight, `alert_key` is NOT left in `buy_alerted`.
3. `test_fake_broker_addon_entry_scenario.py` — happy path on `soxl_ira` (real MARKET BUY for exactly `pos['shares']` despite the resting protective SELL — **this is the whole point of the mechanism, without this assertion it silently never fires**; a second resting BUY still blocks); non-margin hard refusal on `ira`/`roth`/`sep`; five separate non-abuse assertions (no core position / not armed / leg exists / size mismatch / drought position); non-exempted guards still fire (kill switch, notional_cap, ceiling, daily cap, burst cap, non-trading-day, different-ticker BUY block); buying-power gate.
4. `test_fake_broker_addon_lockstep_exit_scenario.py` — parent SL exit closes the leg in lockstep with the parent's reason; leg still `'placed'` when parent exits → cancelled, never sold, `'ABANDONED'`; cancel races to FILLED → real SELL; leg never independently exits on its own notional move; `check_live_state_reconciliation` shows no false mismatch with a leg open.
5. `test_fake_broker_overlay_truth_table_scenario.py` — real-table versions of the paper truth table (`tests/test_overlay_paper_trading.py:539-670`).

Extend `scripts/fake_broker_coverage_matrix.py` for the new scenarios.

---

## Part 10 — Phase 9: Reconciliation

Extend `paper_trading.reconcile_overlay_nodes`/`_reconcile_drought`/`_reconcile_addon` to also accept `mode='live'` nodes, comparing real `trade_log`/`addon_legs` against the backtest mirror, writing to `overlay_reconciliation_log`. Pure observation, never auto-corrects/auto-halts — same stance as `reconcile_daily_track_nodes`. New real-only divergence dimensions: entry slippage, HANDOFF exit price vs. signal price, real margin cost vs. the flat 0.04pp model.

---

## Part 11 — Phase 10: Review gate (non-negotiable)

Touches `active_signals.py`, `signals_notify.py`, `signals_handlers.py`, `signals_db.py`, `schwab_safety.py`, `schwab_client.py`, `signals_invariants.py` — **requires the paired Opus review (independent-cold + contextual) before it ships**, per this project's standing convention. Direct the reviews at minimum: the `is_addon_leg` exemption surface (can any exemption reach a path the five preconditions don't constrain?); the `_has_open_order`→`_has_open_buy_order_for_ticker` swap (does it weaken the 2026-07-24 protection anywhere reachable?); every `cancel_order` status branch in the HANDOFF cancel logic; whether any of the seven `close_position` call sites is missing the lockstep close; whether `position_source` can ever thread incorrectly.

Then, per the promotion standard, **expect a dedicated edge-cases-on-edge-cases hardening pass after staged testing** — budget for it; this project's history (9 bugs → 8 more → 8 more, 2026-07-31) says this is the norm for a change this size, not a sign something went wrong.

---

## Part 12 — Phase 11: The staged real-order test (describe only — the user executes)

Per `docs/design.md`'s "Staged real-order test protocol" section, one mechanism at a time, never batched.

**Test 1 — drought entry.** Requires a `mode='live'`, `drought_overlay_enabled=1` node on `soxl_ira` (only coherent account) — none exists today, needs creating or opting an existing `soxl_ira` node in. Flat immediately before staging (verify, don't assume). Route through the real production path, not a bypass. Small notional, well under the $3,000 cap. **Organic signal only** — a drought entry needs real checkpoint bars to elapse; forcing it would invalidate the test. Tag as staged in `coverage_events.detail`.

**Test 2 — HANDOFF.** Do not stage separately — let it occur organically on the next core signal after Test 1. This is the one that exercises the new cancel race.

**Test 3 — add-on entry.** Only after 1-2 are clean. Requires `addon_enabled=1` on a live `soxl_ira` node. Trigger (core arming) is organic and reasonably frequent. Watch for: `addon_double_buy_exemption` firing with all five preconditions recorded; the order NOT blocked by `_has_open_order`; `check_live_state_reconciliation` not alarming on the 2x broker position.

**Test 4 — add-on lockstep exit.** Follows Test 3 automatically on the parent's own exit.

**Prerequisite for all four**: run the entire mechanism in `dry_run=True` on `brokerage` (margin-typed, dry-run) for a full cycle first — exercises every `check_order` guard with `place_equity_buy` short-circuiting before real submission. A free rehearsal layer the account table already provides.

**The user runs and confirms every one of these** — no agent step in this plan flips a node to `mode='live'`, sets `dry_run=False`, or fires a real order. Cleanup and `deep_backlog.md`/`research_log.md` documentation likewise sit with the user.

**Confirmed out of scope**: skim-and-reserve (alert-only, never places an automated order). All 17 staged paper nodes (167-183) untouched.

---

## Execution order summary

| Phase | Deliverable | Blocks |
|---|---|---|
| 0 | User answers D1-D6 | everything |
| 1 | Schema: `pending_buys` discriminator, `addon_legs` order-id columns, `_last_sale_recovery` filter | 3,4,5,6 |
| 2 | `schwab_safety.is_addon_leg` + `_has_open_buy_order_for_ticker` + `schwab_client` passthrough + invariants | 6,7 |
| 3 | Drought entry: `evaluate_drought_entry` extract, `notify_drought_buy_signal`, `check_drought_entry`, 3-site fill dispatch | 4 |
| 4 | Drought HANDOFF: `check_drought_handoff`, alert-slot fix, 3-site wiring | — |
| 5 | Add-on entry: `check_addon_trigger_real`, fill confirm, reconciliation sweep, `check_live_state_reconciliation` patch | 6 |
| 6 | Add-on lockstep exit: `close_addon_leg_real_if_open` + 7 call sites | — |
| 7 | Coverage registry rows + expectation seeding | — |
| 8 | 5 new fake_broker scenario test files + fixture `buyingPower` support | review |
| 9 | Reconciliation extended to live mode | — |
| 10 | **Paired Opus review (independent-cold + contextual)** | staged test |
| 11 | Staged real-order test — **user executes** | — |
| 12 | Hardening pass (expected, budgeted) | — |

### Critical files
- `schwab_safety.py` (`check_order:747-1130`, `_has_open_order:666`, `ACCOUNTS:157`)
- `signals_notify.py` (`notify_buy_signal:1286`, `_attempt_automated_exit_sell:175`, `notify_trailing_activated:1709`, `_reconcile_buy_fill:2250`, `check_entry_abandon:960`)
- `signals_db.py` (`pending_buys` schema `:655`, `addon_legs` schema `:993`, `open_position:2785`, `open_drought_overlay_position:3078`, `open_addon_leg:3153`)
- `active_signals.py` (`_scan_buy_signals:329`, HANDOFF/ENTRY wiring `:988,1214,1250`)
- `paper_trading.py` (`check_paper_drought_entry:148`, `check_paper_drought_handoff:251`, `check_paper_addon_trigger:1105`, `close_paper_addon_leg_if_open:1143`)
- `signals_handlers.py` (`handle_trail_buy_fill_price:128`, `handle_entry_price:236`, `handle_exit_price:474`)
- `tests/fake_broker.py`, `scripts/coverage_registry.py:475-545`
