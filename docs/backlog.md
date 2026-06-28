# Backlog

## High Priority

- **Remove FAS from watchlist**: Hurst=0.595 (full) / 0.574 (6mo), ADF non-stationary (p=0.36), z=3.0 sweep zero positive alpha across 54k nodes, z=2.5 best node only 1.43× B&H. Structurally momentum, not mean-reverting. The v1.4 559% return node is valid but untrustworthy given the Hurst profile.

- **v1.6 coarse grid sweep**: Re-run full universe with TP/SL at every-3 integers `[3,6,9,...,30]` (6000 nodes/ticker/threshold vs 54k). Goal: validate that islands found at coarse resolution match v1.5 fine-grid islands. If confirmed, adopt coarse grid as default for new thresholds. Discuss grid before implementing.

- **Hurst + ADF screener columns**: Compute Hurst exponent and ADF p-value per ticker (one-time, offline, on daily prices). Add as columns to `tickers` table and surface in Screener page as a quality gate before sweeping. `statsmodels` already installed. Rolling Hurst (30d window) now live on Node Inspector page.
- **Portfolio backtest page**: Replay all watchlist nodes simultaneously over the same timeline. Show concurrent positions, capital utilization, max simultaneous positions. Naturally handles same-ticker and cross-ticker comparison — distinct from Node Inspector (single-node deep dive) and Spatial Topology (island finding).

- **Position sizing in Slack BUY signal**: Include suggested max notional in the BUY Slack message (e.g. "Max size: $12k @ 1% of avg daily vol"). `avg_vol_10d` and `last_price` are already in the screener DB — look up by ticker at signal time.

- **Trade log**: New DB table to record each executed trade — signal price, execution price, exit price, drift on entry/exit. Triggered from Socket Mode modal submissions.
- **Screener → sweep**: Re-export leveraged ETF screener with Underlying Index + Total Assets columns, re-import, then use Screener page to select candidates and add to config.json for sweep. Current import (Results 7.csv) is missing those columns so underlier classification is incomplete.
- **Run sweep on leveraged universe**: ~130 leveraged ETFs with data at 2x/3x. At 20 min/ticker with current grid → ~45 hours. Consider coarsening TP/SL grid to reduce to ~3 days.

## Visualization Pages (Streamlit)

- **Trade chart page**: ✅ Built as Node Inspector (pages/2_Node_Inspector.py) — price + bands at z=2.0/2.5/3.0, trade markers, rolling Hurst, optional ADF, H-filter slider.
- **Topology page — collapsible controls**: Pickers and dropdowns consume too much vertical space on the Spatial Topology page. Add a collapse/expand toggle so the control panel can be hidden to maximize chart real estate. Also consider renaming the page to something shorter (e.g. "Topology" or "Map"). — Medium
- **Topology page — node selection rework**: The bottom section for picking and researching nodes is hard to use. Needs a full rework — easier node selection, clearer display of selected node details, and a path to launch the trade chart from a selected node. — Medium


## Medium Priority

- **Parameter selection workflow**: After enough sweeps, need a way to review results across tickers and select parameter sets to trade — currently manual via logs/heatmaps
- **`trading_engine.py` cleanup**: Either retrofit or replace with Layer 3 implementation; currently points at legacy files

## Low Priority / Ideas

- **Chaos monkey / floor alpha**: For each node, re-run backtest with worst-case execution — entry at highest price in up to N bars after signal, exit at lowest price in up to N bars after exit signal (model: 1-day delay and 2-week delay). Also model missed TIME_EXIT: exit N bars late at worst price in that window. Produces a `floor_alpha` metric. Nodes with positive floor alpha are robust to real-world execution delays (meetings, swimming pool, travel). Store alongside `alpha_vs_spy` in DB and surface in Winners page.
- **Alpha robustness — drop top N trades**: Re-run backtest dropping the top 3 best-performing trades and recalculate alpha. Tests whether alpha is structural or lucky. Complements floor alpha.
- **Automated exits**: Exits (TP/SL/TIME) are deterministic and the primary candidate for brokerage API automation. Manual exit of 8 simultaneous positions in 30 minutes is operationally risky. Entries stay manual (require sizing judgment).
- **Portfolio backtest page**: Replay all watched nodes simultaneously over the same time period. Show concurrent open positions over time (chart), max simultaneous positions, average utilization, capital requirements. Answers "how much capital do I actually need?"
- **Broader ticker universe**: `results.csv` (999 rows, mixed leveraged/non-leveraged) has liquidity data for non-leveraged ETFs. Import and sweep to increase signal frequency. Current universe is leveraged-only.
- **Half-Life of Reversion**: Fit an Ornstein-Uhlenbeck process per ticker to estimate how many hours prices take to revert to mean. Use to inform `max_hold_hours` sweep range per ticker (sweep around the half-life rather than the same grid for all tickers). One offline computation per ticker, surface in Screener. Complements Hurst and ADF.
- **ADF test (stationarity filter)**: One-time per-ticker computation. Augmented Dickey-Fuller test confirms whether a price series is stationary (genuinely mean-reverting) vs. random walk. Simple pass/fail screener column. Pair with Hurst for a two-signal quality gate.
- **Regime transition stress test (synthetic)**: Generate a single synthetic price series that transitions through Hurst regimes over time (e.g. mean-reverting → trending → random → back). Run backtester against it and visualize equity curve + open/close markers through the transition. Goal: confirm the strategy doesn't get caught entering trades as H spikes, and that the H filter cuts exposure at the right moment. Critical gap — all real data is bull market only.
- **H threshold slider (real ticker)**: For a selected ticker, compute rolling Hurst on real price history and let user drag an H cutoff threshold. Show how trades, return, and win rate change as the filter tightens/loosens. Practical calibration tool for setting live trading filter. Pair with regime transition stress test.
- **Rolling Hurst Exponent filter**: Compute rolling Hurst (512-bar window ≈ 6 months of trading hours, DFA method) per ticker offline. Surface as a screener column and use as a signal quality gate — H < 0.45 confirms genuine mean-reversion regime, H > 0.55 signals trending (suppress entries). Rolling window intentional: captures recent regime behavior, not a 2-year average that masks recent shifts. Motivated by bear/bull regime risk: leveraged ETFs in sustained sector trends look like dip-buying opportunities but aren't. See session discussion 2026-06-27.
- **Advanced indicators**: Dataset size allows pre-computing Bollinger Bands, ATR, MACD etc. instantly via TA-Lib or pandas-ta (compiled C under the hood) — no hardware constraints
- **Basic ML experimentation**: Dataset small enough to train Random Forests or XGBoost on CPU in seconds if we want to explore signal prediction
- **Multi-ticker signal dashboard**: View current z-score signals across the full ticker universe in one place
- **Position sizing**: Layer 3 will eventually need a position sizing model
