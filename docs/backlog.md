# Backlog

## High Priority

- **Layer 3 — Live Trading Engine**: Build `live_trading.py` (or similar) to apply optimized parameters to live signals, track open positions across sessions, and handle manual end-of-day state updates

## Visualization Pages (Streamlit)

- **Trade chart page**: New Streamlit page showing hourly price, SMA/Bollinger bands, buy/sell/exit markers, and alpha scorecard for a selected ticker and parameter set. Should be launchable from Node Inspector. (3D topology and node inspector pages already exist in `pages/`)

## Medium Priority

- **Parameter selection workflow**: After enough sweeps, need a way to review results across tickers and select parameter sets to trade — currently manual via logs/heatmaps
- **`trading_engine.py` cleanup**: Either retrofit or replace with Layer 3 implementation; currently points at legacy files

## Low Priority / Ideas

- **Advanced indicators**: Dataset size allows pre-computing Bollinger Bands, ATR, MACD etc. instantly via TA-Lib or pandas-ta (compiled C under the hood) — no hardware constraints
- **Basic ML experimentation**: Dataset small enough to train Random Forests or XGBoost on CPU in seconds if we want to explore signal prediction
- **Multi-ticker signal dashboard**: View current z-score signals across the full ticker universe in one place
- **Position sizing**: Layer 3 will eventually need a position sizing model
