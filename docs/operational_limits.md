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

### Entry Execution Approach
- **Order type**: Limit order staged pre-market at an absurd low price (will not fill accidentally). At 10:30 AM or 3:30 PM ET, if Slack fires a BUY signal, edit the limit price to current market and submit.
- **Signal check windows**: 10:25–10:40 AM ET (9:30 bar close) and 15:25–15:40 PM ET (14:30 bar close). `active_signals.py` only evaluates buy/sell signals within these windows.
- **Data source**: Real-time spot price via `yfinance fast_info.last_price` at signal check time. Hourly cached data used only for indicator computation (SMA, Std).
- **Do not use overnight limit orders at lower_band**: Open-fill analysis showed entering at the 9:30 open (before the intrabar decline) is consistently worse than the 10:30 close. A staged limit order edited at signal time is the correct approach.

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
