# Backlog

## High Priority

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

- **Advanced indicators**: Dataset size allows pre-computing Bollinger Bands, ATR, MACD etc. instantly via TA-Lib or pandas-ta (compiled C under the hood) — no hardware constraints
- **Basic ML experimentation**: Dataset small enough to train Random Forests or XGBoost on CPU in seconds if we want to explore signal prediction
- **Multi-ticker signal dashboard**: View current z-score signals across the full ticker universe in one place
- **Position sizing**: Layer 3 will eventually need a position sizing model
