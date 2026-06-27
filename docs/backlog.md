# Backlog

## High Priority

- **Position sizing in Slack BUY signal**: Include suggested max notional in the BUY Slack message (e.g. "Max size: $12k @ 1% of avg daily vol"). `avg_vol_10d` and `last_price` are already in the screener DB — look up by ticker at signal time.

- **Trade log**: New DB table to record each executed trade — signal price, execution price, exit price, drift on entry/exit. Triggered from Socket Mode modal submissions.
- **Screener → sweep**: Re-export leveraged ETF screener with Underlying Index + Total Assets columns, re-import, then use Screener page to select candidates and add to config.json for sweep. Current import (Results 7.csv) is missing those columns so underlier classification is incomplete.
- **Run sweep on leveraged universe**: ~130 leveraged ETFs with data at 2x/3x. At 20 min/ticker with current grid → ~45 hours. Consider coarsening TP/SL grid to reduce to ~3 days.

## Visualization Pages (Streamlit)

- **Trade chart page**: New Streamlit page showing hourly price, SMA/Bollinger bands, buy/sell/exit markers, and alpha scorecard for a selected ticker and parameter set. Should be launchable from Node Inspector. (3D topology and node inspector pages already exist in `pages/`)
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
- **Advanced indicators**: Dataset size allows pre-computing Bollinger Bands, ATR, MACD etc. instantly via TA-Lib or pandas-ta (compiled C under the hood) — no hardware constraints
- **Basic ML experimentation**: Dataset small enough to train Random Forests or XGBoost on CPU in seconds if we want to explore signal prediction
- **Multi-ticker signal dashboard**: View current z-score signals across the full ticker universe in one place
- **Position sizing**: Layer 3 will eventually need a position sizing model
