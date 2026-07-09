# Operational Limits

Rules imposed on live trading based on the current phase of the system. These exist because the system is partially manual — limits prevent overexposure while execution, monitoring, and automation are still being built out.

---

## Phase 1 — Manual Execution (Current)

**Characteristics**: Slack signals, manual entry/exit confirmation, no brokerage API, no automated exits.

### Risk Management — First Principle
**Never risk capital that would matter if lost.** Position sizes must be small enough that a complete loss on any single trade, or a simultaneous loss across all open positions, does not materially impact retirement or financial security. Everything else in this document flows from this.

### Position Limits
- **Max simultaneous open positions**: TBD — set based on portfolio backtest analysis (how often do stacked signals occur?)
- **Max notional per trade**: 1% of ticker's avg daily dollar volume (surfaced in Slack BUY message)
- **One node per ticker**: Only one watch list entry per ticker until portfolio-level behavior is validated

### Entry/Exit Action Table — By Strategy

Each strategy has different execution mechanics. Check the strategy name shown in the Slack message against this table before acting — do not assume all signals work like v1.5/v1.6.

General notes that apply everywhere:
- **Signal check windows**: 10:25–10:40 AM ET (9:30 bar close) and 15:25–15:40 PM ET (14:30 bar close), for anything marked "bar-close" below.
- **Data source**: Real-time spot price via `yfinance fast_info.last_price` at signal check time. Hourly cached data used only for indicator computation (SMA, Std).
- **Do not use overnight limit orders at lower_band for bar-close strategies**: Open-fill analysis showed entering at the 9:30 open (before the intrabar decline) is consistently worse than the 10:30 close. A staged limit order edited at signal time is the correct approach.
- **No use for stop-limit orders anywhere in this workflow**: all exits (fixed floor stop, armed trailing stop) must guarantee a fill — a stop-limit risks no fill at all during a fast/gappy move on a leveraged ETF, which defeats the point of a stop at exactly the moment it's needed. The one place a stop-limit could plausibly help (capping slippage on a v1.9/v1.10 trailing-buy entry) isn't offered as a combo order at Schwab (confirmed: no trailing-stop-limit-to-buy). Use plain stop (market) throughout.

| # | Strategy | Signal | Timing | Slack message | Required action |
|---|----------|--------|--------|----------------|------------------|
| 1 | `ZScoreBreakout` (v1.5/v1.6) | BUY | bar-close (signal window) | 🟢 BUY — Market — price/shares + 🔴 stop-loss price | Edit pre-staged absurd-low limit → market, submit within ~5 sec. Then place the Schwab stop at the shown price. |
| 2 | `ZScoreBreakout` (v1.5/v1.6) | SELL | bar-close (signal window) | TP: "Cancel Stop Loss — Sell All (Market)". SL: "Check account — stop should have auto-filled". TIME: "Change Stop Loss → Market Close order" | TP → sell now. SL → just verify the resting Schwab stop caught it. TIME → convert to a market-close (EOD) order. |
| 3 | `TrendFilteredZScore` (v1.4) | BUY | bar-close (signal window) | Same as row 1 | Same as row 1 — mechanically identical to ZScoreBreakout, just gated by the extra 50d trend filter. |
| 4 | `TrendFilteredZScore` (v1.4) | SELL | bar-close (signal window) | Same as row 2 | Same as row 2. |
| 5 | `LimitOrderZScoreBreakout` (v1.7) | BUY | all day, continuous (intrabar Low touch) | ✅ "LIMIT FILLED" — price/shares + 🔴 stop price | **No entry action** — this is a real resting limit order at the computed trigger price, not a placeholder; it already filled on its own. Just place the Schwab stop at the shown price. |
| 6 | `LimitOrderZScoreBreakout` (v1.7) | SELL | SL continuous (intrabar); TP/TIME bar-close (signal window) | Same TP/SL/TIME messages as row 2 | Same actions as row 2. SL is a backup confirmation — the real protection is the resting Schwab stop, which should already have fired. |
| 7 | `TrailingExitZScoreBreakout` (v1.8) | BUY | bar-close (signal window) | Same as row 1 | Same as row 1. |
| 8 | `TrailingExitZScoreBreakout` (v1.8) | SELL | SL + trailing-stop continuous (intrabar); TP-activation & TIME bar-close | 🎯 "TRAILING ACTIVATED" (no action, informational) fires once when TP clears. Final exit: 🟢 "TRAILING STOP" or 🔴 "STOP LOSS" or 🔶 "TIME EXIT" | TRAILING ACTIVATED → no action, just confirms state changed. Final exit messages → same actions as row 2 (TRAIL behaves like TP: cancel stop, sell now). |

**Table above predates `TrailingBothZScoreBreakout` (v1.10) going live** — see the dedicated lifecycle table below; it's the strategy 100% of the current live watchlist actually uses (trailing buy *and* trailing sell), not covered by rows 1-8.

### `TrailingBothZScoreBreakout` (v1.10) Full Lifecycle — 100% of live watchlist (2026-07-09)

Reviewed end-to-end 2026-07-09 after the TP/SL-label and signal-window-alert fixes. Reconciles the code's actual behavior against the manual Schwab workflow, state by state.

| # | State | What fires | Frequency | Code | Status |
|---|---|---|---|---|---|
| 1 | Above z trigger — holding | Reference table shown in startup report + signal-window pings | Startup, 7am, each window | `build_reference_table` (`active_signals.py:1503`) | OK |
| 2 | Below z trigger, in window → BUY trailing order alert | `notify_buy_signal` → `_build_buy_blocks`: trigger price, shares, notional (~$50k), max-vol cap | Bar-close, in-window only | `active_signals.py:1213`, `:914` | **Gap: no account shown** unless the same-day-buy-warning branch happens to fire — otherwise the alert never states which account (brokerage/ira/sep) to place the order in |
| 3 | Trailing buy pending → "should have filled by now" reminder | Doesn't exist. "Executed" button lets you self-report a fill any time, but nothing nags like the sell-side reminder does | — | — | **Gap** — matches the open backlog item "Trailing-buy fill confirmation" (2026-07-07), never built |
| 4 | Holding, waiting for arm/profit level | Silent, shown only in periodic table | — | — | OK by design |
| 5a | Profit level reached → notify to place trailing sell | `notify_trailing_activated` fires once, detected | **Bar-close only**, not continuous | `strategies.py:294` (gated behind `at_bar_close`) | Not literally "any time" — up to ~1 poll-cycle-after-bar-close lag (bars close hourly) |
| 6a | Trailing sell pending, order not yet placed | Reminder every 15 min until "Order Placed" clicked; goes silent once placed | Every 15 min while unplaced | `check_trailing_reminders` (`active_signals.py:1411`) | OK — matches "no new notifications once placed" |
| 7a | Trailing sell hit → notify expected sell | `notify_sell_signal(..., 'TRAIL', ...)` | Continuous, every poll (currently 30s), using polled price as proxy for intrabar low/high | `strategies.py:280-288`, `active_signals.py:691` | Fires promptly, but timing is bounded by poll interval, not true tick data — some drift vs. the broker's own continuous trailing engine is inherent, not fully fixable without a streaming feed |
| 5b | SL hit | `notify_sell_signal(..., 'SL', ...)` | Continuous, every poll — no bar-close gate | `strategies.py:290` | OK — any time, as expected |
| 5c | Max hold hours hit | `notify_sell_signal(..., 'TIME', ...)` | **Bar-close only** | `strategies.py:298` (same `at_bar_close` gate as 5a) | Not "any time" — up to ~1h lag past the actual max-hold crossing, intentional (mirrors backtest kernel's hourly-bar-based max-hold check; changing to continuous would break backtest parity) |

**Open items from this review**: (1) add account to the BUY alert — small fix, not yet done; (2) trailing-buy fill reminder — real feature, not yet scoped/built.

### Execution Limits
- **Do not enter if you cannot monitor**: If unavailable for the next 2h, skip the signal
- **Exit within one trading day of exit signal**: If you miss the exit signal, close at next morning's open
- **No entries in the last 30 minutes of trading**: Insufficient time to react to same-day TP/SL
- **No early manual exits**: Trust TP/SL/TIME — overriding the system emotionally undermines the backtest alpha. Early exit is only justified for operational reasons (e.g. simultaneous exit overload), not price anxiety.
- **Position sizes must not risk retirement capital**: Keep notional small enough that a full loss on any single position is acceptable. This removes the emotional pressure to exit early.

### Monitoring Limits
- **Max watch list size**: TBD — constrained by how many Slack notifications you can realistically act on per day
- **No new entries when 3+ positions are open**: Until portfolio backtest validates stacked position behavior
- **Close all positions and pause watch list before travel**: If connectivity is uncertain (vacation, cruise, international travel), close open positions and remove tickers from watch list before leaving. Do not rely on Slack being reachable.

### Data Integrity Limits
- **Stock splits can silently corrupt cached price data**: `data_manager.py::fetch_live_data_smart`'s incremental fetch only re-adjusts *overlapping* rows on each update (full split-adjusted history is only pulled fresh on initial bootstrap). If a split lands after a ticker's initial bootstrap, older cached rows can stay at the pre-split price scale while newer rows come in post-split-adjusted — producing a fake single-bar price jump that can dominate a compounded backtest return. Found and fixed for **UVIX** (1-for-20 reverse split, 2026-07-01 — fake +1889% trade inflated one node's alpha to +4400%) and **NBIZ** (2026-06-03 split, but the bad tick turned out to be baked into yfinance's own historical data — a full cache rebuild did not fix it, so NBIZ was blacklisted: removed from `tickers.json` and `watch_list` instead).
- **Not yet built — split-hold safeguard**: run `scripts/check_stock_splits.py` (queries yfinance's authoritative `Ticker.splits`, flags any split landing inside a ticker's cached date range) at the start of each trading day, scoped to watchlist/`open_positions` tickers. Any ticker flagged should be pulled from live signal checks until its cache is rebuilt and verified clean. **If a position is open across a split date**, the entry price/share count must be manually reconciled against the actual broker position (which auto-adjusts on the split) before trusting any Slack exit signal — the cached-data math and the real brokerage position can silently diverge.

---

## Account Type — IRA / Roth IRA (Planned Live Test)

Plan is to test strategies live in IRA/Roth IRA accounts first, not a taxable brokerage account.

- **No margin**: IRAs are cash accounts. Not a constraint on strategy design — the 3x leverage in AGQ/SOXL/TQQQ/etc. is embedded in the fund itself, not achieved via account margin. Shares are bought outright.
- **T+1 settlement — cannot sell and buy same day with the same cash**: a cash account cannot reuse unsettled sale proceeds for a new purchase same-day; doing so risks a good-faith violation. This only matters when *different tickers* compete for the same account's cash on the same day (e.g. one position's exit funding a different position's entry). Ask Schwab whether the IRA has "limited margin" enabled — it doesn't allow borrowing/leverage, just removes the settlement-violation risk for same-day reuse of proceeds.
- **Mitigation — 3 separate IRA-type accounts, 1 position each**: removes the cross-ticker cash-contention problem entirely, since no account's cash is ever competing between two different tickers' signals. (A single account's own position exiting and its *same* ticker re-signaling later the same day would still hit the settlement wait, but that's a narrow edge case.)
- **Wash sale rule is moot inside an IRA/Roth**: neither account type reports per-trade gains/losses for tax purposes, so there's nothing to disallow. The one real trap — a taxable-account loss permanently disallowed because the same security is repurchased in an IRA within 30 days — only applies if a ticker is traded in *both* a taxable account and an IRA. **Rule: do not trade any ticker in a taxable brokerage account that is also live in one of the 3 IRA accounts** (or vice versa).
- **UBTI/K-1 risk — check before funding**: AGQ (ProShares Ultra Silver) is commodity-futures-based; funds structured this way can generate Unrelated Business Taxable Income, which can trigger tax even inside an IRA, and may issue a K-1 instead of a 1099. Verify with Schwab/fund prospectus before trading AGQ in an IRA. The other watchlist tickers (SOXL, TQQQ, FAS, EDC, HIBL, GDXU) are standard equity-index '40 Act funds and don't have this issue.

---

## Phase 2 — Automated Exits (Planned)

Exits (TP/SL/TIME) submitted automatically via brokerage API. Entries remain manual.

- Removes the simultaneous-exit operational risk
- Increases safe max simultaneous positions
- Requires brokerage API integration (Alpaca, IBKR, etc.)

---

## Phase 3 — Semi-Automated (Future)

Automated exits + optional automated entries for high-confidence nodes (positive floor alpha, broad alpha island, low execution drift historically).

---

## Open Questions

- What is the right max simultaneous positions for Phase 1? (needs portfolio backtest)
- What is total capital allocation to this strategy vs other uses?
- What is the per-trade notional target? (affects how many positions are needed to deploy capital)
