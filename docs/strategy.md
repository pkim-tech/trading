# Strategy Reference

## Core Strategy — ZScoreBreakout

Z-score mean reversion on leveraged ETFs. Buy when price drops significantly below its rolling mean; exit at a fixed take profit, stop loss, or max hold time.

### Entry Signal
- Compute rolling SMA and std dev over the last `window` **daily** closes (prior closed days only — today's intraday bar is excluded)
- Z-score = `(current_price - SMA) / std`
- BUY when z-score ≤ -2.0

### Exit Conditions
1. **Take Profit** — hourly close ≥ entry × (1 + tp%)
2. **Stop Loss** — hourly close ≤ entry × (1 - sl%)
3. **Time Exit** — position still open after `max_hold_hours`

### Parameter Space
- `window` — rolling lookback in days (e.g. 10, 20, 30, 40)
- `take_profit` — TP % (e.g. 1–30%)
- `stop_loss` — SL % (e.g. 1–30%)
- `max_hold_hours` — max hold duration in hours (e.g. 7–140h in 7h steps)

### Why Leveraged ETFs
Leveraged ETFs exhibit volatility decay and mean-reverting behavior around their daily reset mechanism. Sharp intraday or multi-day drawdowns tend to recover, making z-score entries historically profitable. Regular ETFs and stocks have different return profiles; this strategy has not been validated on them.

---

## Optimization

The sweep searches for **alpha islands** — broad, contiguous regions of the parameter space where many neighboring nodes all produce positive alpha vs SPY. A single isolated peak is fragile; a plateau is robust.

Alpha = strategy compounded return − SPY buy-and-hold return over the same period.

Nodes with 0 trades are cached as NO_TRADES and excluded from results — they represent parameter combinations where the z-score threshold was never crossed given the ticker's volatility profile.

---

## Live Trading Assumptions

- **Signal timing**: BUY/SELL signals are checked on hourly bar close during market hours. Signals can only fire during market hours (yfinance hourly data is market hours only).
- **Execution**: Manual — Slack notification → human confirms entry/exit price via modal.
- **No brokerage integration**: Position state is tracked in `open_positions` DB table, not via broker API.
- **One position per node**: Duplicate positions for the same `(ticker, window)` are blocked.

---

## Edge Cases & Decisions

### Outside Market Hours
Not applicable — hourly data is market hours only. Signals cannot fire overnight.

### Stacked Signals
Two or more BUY signals firing in the same poll cycle (different tickers or different nodes on the same ticker). No priority rule defined yet. Capital allocation is manual.

### Missed Entry Window
If the BUY signal is missed (e.g. user in meetings), entry could be delayed by hours or up to a day. Worst-case floor alpha simulation planned: re-run backtest with entry at highest price in up to N bars after signal, exit at lowest price in up to N bars after exit signal. See backlog.

### Early Manual Exit
User exits a position before TP/SL/TIME fires. Logged as `MANUAL` exit reason in `trade_log`.

### Missed Exit Signal
Position stays open past `max_hold_hours` because the exit signal was missed. Not currently handled — would require a separate staleness check in the poll loop.

### Position Sizing
No automated sizing model. Suggested max notional = 1% of `avg_vol_10d × last_price` — included in Slack BUY message (planned, see backlog). Intent is to stay well under daily liquidity to avoid moving the market.

### Execution Drift
Entry/exit prices will differ from signal prices due to timing. Drift is tracked in `trade_log` (`entry_drift_pct`, `exit_drift_pct`).

---

### Simultaneous Exits
Multiple positions hitting TP/SL/TIME in the same window. Manually confirming 8 exits in 30 minutes is operationally risky. Exits are better candidates for automation than entries — entry sizing requires judgment, exit conditions are deterministic. This is the primary motivation for eventual brokerage API integration.

---

## What We Have Not Validated

- Strategy performance on non-leveraged ETFs or individual stocks
- Performance with brokerage execution latency / real fills
- Behavior during market dislocations (COVID crash, flash crashes)
- Multi-position capital allocation rules
