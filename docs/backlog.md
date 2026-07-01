# Backlog

## High Priority

- **v1.6 coarse grid sweep**: Re-run full universe with TP/SL at every-3 integers `[3,6,9,...,30]` (6000 nodes/ticker/threshold vs 54k). Goal: validate that islands found at coarse resolution match v1.5 fine-grid islands. If confirmed, adopt coarse grid as default for new thresholds. Discuss grid before implementing.

- **v1.6 limit order entry model**: Add `use_limit_fill` toggle to backtester. Fill condition: `Low <= lower_band` during the target bar (price touched the limit intrabar). Fill price: `lower_band` exactly. Current v1.5 uses `Close <= lower_band` as both signal and entry price. Limit model catches more trades (intrabar touches that close back above lower_band) at a better price. Requires passing lows array to Numba kernel. **Architecture decision**: implement as a separate strategy class (`LimitOrderZScoreBreakout`), not an inherited override. New strategy gets its own sweep version (v1.6).

- **Trade log UI**: DB table and schema exist (`trade_log` in `trading_universe.db`). Pending: Socket Mode modal to record entry/exit from Slack interactions.

- **Screener → sweep**: Re-export leveraged ETF screener with Underlying Index + Total Assets columns, re-import, then use Screener page to select candidates and add to config.json for sweep. Current import (Results 7.csv) is missing those columns so underlier classification is incomplete.

## Visualization Pages (Streamlit)

- **Open Positions page**: ✅ Built (`pages/10_Open_Positions.py`) — entry/signal price, drift %, current price, P&L %, TP/SL prices, hours held/left.

- **Two-phase UX rethink**: The current pages reflect two distinct workflows that aren't made explicit: (1) **Discovery** — sweep → Winners → find candidate tickers/nodes; (2) **Optimization** — Spatial Topology + Node Inspector → refine a candidate into a tradeable config. Consider shared "active ticker" context across Topology and Node Inspector, or restructuring so the two optimization pages feel like sub-views of a single ticker analysis flow.

- **Trade chart page**: ✅ Built as Node Inspector (`pages/2_Node_Inspector.py`) — price + bands at z=2.0/2.5/3.0, trade markers, optional Hurst/ADF (opt-in).

- **Topology page — collapsible controls**: Pickers and dropdowns consume too much vertical space. Add collapse/expand toggle to maximize chart real estate. — Medium

- **Topology page — node selection rework**: Bottom section for picking and researching nodes is hard to use. Needs rework — easier node selection, clearer node details, path to launch Node Inspector from selected node. — Medium

- **SPY trend / VIX level as entry filter**: Next research direction after ruling out Hurst/ADF. Hypothesis: avoid entries when SPY is in a downtrend (price < 200d SMA) or VIX is elevated (VIX > 25). Macro regime signals rather than ticker-level — may address the lag problem that killed Hurst/ADF.

## Medium Priority

- **Parameter selection workflow**: After enough sweeps, need a way to review results across tickers and select parameter sets to trade — currently manual via logs/heatmaps.
- **`trading_engine.py` cleanup**: Either retrofit or replace with Layer 3 implementation; currently points at legacy files.

## Low Priority / Ideas

- **Alternative trading windows**: Explore hourly bar closes beyond 9:30 and 14:30 (e.g. 11:30, 12:30, 1:30) — requires expanding `target_hours` and re-sweeping.
- **Chaos monkey / floor alpha**: Re-run backtest with worst-case execution — entry at highest price in N bars after signal, exit at lowest price in N bars after exit signal. Produces `floor_alpha` metric. Nodes with positive floor alpha are robust to real-world execution delays. Store alongside `alpha_vs_spy` in DB and surface in Winners page.
- **Alpha robustness — drop top N trades**: Re-run backtest dropping the top 3 best-performing trades and recalculate alpha. Tests whether alpha is structural or lucky.
- **Automated exits**: Exits (TP/SL/TIME) are deterministic and the primary candidate for brokerage API automation. Manual exit of multiple simultaneous positions is operationally risky. Entries stay manual.
- **Broader ticker universe**: `results.csv` (999 rows, mixed leveraged/non-leveraged) has liquidity data for non-leveraged ETFs. Import and sweep to increase signal frequency.
- **Half-Life of Reversion**: Fit an Ornstein-Uhlenbeck process per ticker to estimate mean-reversion speed. Use to inform `max_hold_hours` sweep range per ticker. One offline computation per ticker, surface in Screener.
- **Hurst/ADF as entry filter**: ✅ Researched (`pages/7_Hurst_Filter.py`, `pages/8_ADF_Filter.py`). Verdict: not actionable on current dataset — see `docs/research.md`. Revisit for v1.6 if dataset changes.
- **Advanced indicators**: Dataset size allows pre-computing Bollinger Bands, ATR, MACD etc. instantly via TA-Lib or pandas-ta.
- **Basic ML experimentation**: Dataset small enough to train Random Forests or XGBoost on CPU in seconds.
- **Multi-ticker signal dashboard**: View current z-score signals across the full ticker universe in one place.
- **Position sizing model**: Layer 3 will eventually need a position sizing model.
