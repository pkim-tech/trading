# Backlog

## High Priority

- **Layer 3 — Live Trading Engine**: Build `live_trading.py` (or similar) to apply optimized parameters to live signals, track open positions across sessions, and handle manual end-of-day state updates
- **Node Performance — Numba/L3 Cache**: Each node/sim currently takes ~4 seconds. Gemini recommended isolating the core backtest loop, converting pandas columns to raw numpy arrays before the loop, and adding Numba's `@njit` decorator (`pip install numba`). Dataset is tiny (~35MB even at 500 tickers x 10 years hourly) so GPU is overkill — CPU L3 cache is the right target. Expected to drop execution time dramatically.
- **Lost Nodes Bug**: Several thousand nodes go missing during sweep runs — rerunning the sweep doesn't recover them. Suspected cause: a hard loss floor in the simulator that silently drops nodes instead of recording them. Need to investigate `strategy_optimizer.py` for any filtering/threshold logic that discards results rather than recording them.
- **3D Visualizer — Planned Nodes**: When the app was split into pages, the visualization that showed all planned nodes in blue on the 3D graph was lost. Need to restore this in `pages/1_Spatial_Topology.py`.

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
