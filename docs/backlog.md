# Backlog

## High Priority

- **v1.6 coarse grid sweep**: Re-run full universe with TP/SL at every-3 integers `[3,6,9,...,30]` (6000 nodes/ticker/threshold vs 54k). Goal: validate that islands found at coarse resolution match v1.5 fine-grid islands. If confirmed, adopt coarse grid as default for new thresholds. Discuss grid before implementing.

- **v1.6 limit order entry model**: Add `use_limit_fill` toggle to backtester. Fill condition: `Low <= lower_band` during the target bar (price touched the limit intrabar). Fill price: `lower_band` exactly. Current v1.5 uses `Close <= lower_band` as both signal and entry price. Limit model catches more trades (intrabar touches that close back above lower_band) at a better price. Requires passing lows array to Numba kernel. This matches the real execution approach (limit order staged pre-market, edited at signal time). Return impact unknown — needs full re-sweep. **Architecture decision**: implement as a separate strategy class (`LimitOrderZScoreBreakout`), not an inherited override — execution price is fundamental to the P&L chain and changing it via inheritance is fragile. Reuse band/signal calculation logic via shared utility. New strategy gets its own sweep version (v1.6).

- **Hurst + ADF screener columns**: Hook into data download — compute on download and store in `tickers` table. Not a single scalar (regime-dependent); pending decision on right aggregation. Rolling per-ticker series already computed in Node Inspector; Hurst/ADF at signal time now sent in Slack BUY message.
- **Portfolio backtest page**: ✅ Built (`pages/4_Portfolio.py`). Gantt timeline + SPY overlay + concurrent positions panel, all shared x-axis. Hurst/ADF sliders filter trades by regime at entry. Summary metrics + per-node table with unfiltered vs filtered return comparison.

- **Position sizing in Slack BUY signal**: ✅ BUY message now shows max notional and max shares at 1% of avg daily vol.

- **Trade log**: New DB table to record each executed trade — signal price, execution price, exit price, drift on entry/exit. Triggered from Socket Mode modal submissions.
- **Screener → sweep**: Re-export leveraged ETF screener with Underlying Index + Total Assets columns, re-import, then use Screener page to select candidates and add to config.json for sweep. Current import (Results 7.csv) is missing those columns so underlier classification is incomplete.
- **Run sweep on leveraged universe**: ~130 leveraged ETFs with data at 2x/3x. At 20 min/ticker with current grid → ~45 hours. Consider coarsening TP/SL grid to reduce to ~3 days.

## Visualization Pages (Streamlit)

- **Two-phase UX rethink**: The current pages reflect two distinct workflows that aren't made explicit: (1) **Discovery** — sweep → Winners → find candidate tickers/nodes; (2) **Optimization** — Spatial Topology + Node Inspector + Hurst filter → refine a candidate into a tradeable config. Spatial Topology and Node Inspector are ticker-centric views (ticker is the subject, node is a detail) while Winners is node-centric. Navigation feels like peers but they're different phases. Consider: shared "active ticker" context carried across Topology and Node Inspector, clearer phase separation in the sidebar, or restructuring so the two optimization pages feel like sub-views of a single ticker analysis flow.

- **Trade chart page**: ✅ Built as Node Inspector (pages/2_Node_Inspector.py) — price + bands at z=2.0/2.5/3.0, trade markers, rolling Hurst, optional ADF, H-filter slider.
- **Topology page — collapsible controls**: Pickers and dropdowns consume too much vertical space on the Spatial Topology page. Add a collapse/expand toggle so the control panel can be hidden to maximize chart real estate. Also consider renaming the page to something shorter (e.g. "Topology" or "Map"). — Medium
- **Topology page — node selection rework**: The bottom section for picking and researching nodes is hard to use. Needs a full rework — easier node selection, clearer display of selected node details, and a path to launch the trade chart from a selected node. — Medium


- **SPY trend / VIX level as entry filter**: Next research direction after ruling out Hurst/ADF. Hypothesis: avoid entries when SPY is in a downtrend (e.g. price < 200d SMA) or VIX is elevated (e.g. VIX > 25). Both are macro regime signals rather than ticker-level, which may address the lag problem that killed Hurst/ADF.

## Medium Priority

- **Parameter selection workflow**: After enough sweeps, need a way to review results across tickers and select parameter sets to trade — currently manual via logs/heatmaps
- **`trading_engine.py` cleanup**: Either retrofit or replace with Layer 3 implementation; currently points at legacy files

## Low Priority / Ideas

- **Alternative trading windows**: Backtest currently enters only on the 9:30 bar close (10:30 AM) and 14:30 bar close (3:30 PM). Explore whether other hourly bar closes (e.g. 11:30, 12:30, 1:30) improve signal frequency or return — requires expanding `target_hours` in backtester and re-sweeping affected tickers.

- **Chaos monkey / floor alpha**: For each node, re-run backtest with worst-case execution — entry at highest price in up to N bars after signal, exit at lowest price in up to N bars after exit signal (model: 1-day delay and 2-week delay). Also model missed TIME_EXIT: exit N bars late at worst price in that window. Produces a `floor_alpha` metric. Nodes with positive floor alpha are robust to real-world execution delays (meetings, swimming pool, travel). Store alongside `alpha_vs_spy` in DB and surface in Winners page.
- **Alpha robustness — drop top N trades**: Re-run backtest dropping the top 3 best-performing trades and recalculate alpha. Tests whether alpha is structural or lucky. Complements floor alpha.
- **Automated exits**: Exits (TP/SL/TIME) are deterministic and the primary candidate for brokerage API automation. Manual exit of 8 simultaneous positions in 30 minutes is operationally risky. Entries stay manual (require sizing judgment).
- **Portfolio backtest page**: Replay all watched nodes simultaneously over the same time period. Show concurrent open positions over time (chart), max simultaneous positions, average utilization, capital requirements. Answers "how much capital do I actually need?"
- **Broader ticker universe**: `results.csv` (999 rows, mixed leveraged/non-leveraged) has liquidity data for non-leveraged ETFs. Import and sweep to increase signal frequency. Current universe is leveraged-only.
- **Half-Life of Reversion**: Fit an Ornstein-Uhlenbeck process per ticker to estimate how many hours prices take to revert to mean. Use to inform `max_hold_hours` sweep range per ticker (sweep around the half-life rather than the same grid for all tickers). One offline computation per ticker, surface in Screener. Complements Hurst and ADF.
- **ADF test (stationarity filter)**: ✅ Built as `pages/8_ADF_Filter.py`. Verdict: not actionable as entry filter — see `docs/research.md`.
- **Regime transition stress test (synthetic)**: Generate a single synthetic price series that transitions through Hurst regimes over time (e.g. mean-reverting → trending → random → back). Run backtester against it and visualize equity curve + open/close markers through the transition. Goal: confirm the strategy doesn't get caught entering trades as H spikes, and that the H filter cuts exposure at the right moment. Critical gap — all real data is bull market only.
- **H threshold slider (real ticker)**: ✅ Built as `pages/7_Hurst_Filter.py`. Verdict: not actionable as entry filter — see `docs/research.md`.
- **Rolling Hurst Exponent filter**: Compute rolling Hurst (512-bar window ≈ 6 months of trading hours, DFA method) per ticker offline. Surface as a screener column and use as a signal quality gate — H < 0.45 confirms genuine mean-reversion regime, H > 0.55 signals trending (suppress entries). Rolling window intentional: captures recent regime behavior, not a 2-year average that masks recent shifts. Motivated by bear/bull regime risk: leveraged ETFs in sustained sector trends look like dip-buying opportunities but aren't. See session discussion 2026-06-27.
- **Advanced indicators**: Dataset size allows pre-computing Bollinger Bands, ATR, MACD etc. instantly via TA-Lib or pandas-ta (compiled C under the hood) — no hardware constraints
- **Basic ML experimentation**: Dataset small enough to train Random Forests or XGBoost on CPU in seconds if we want to explore signal prediction
- **Multi-ticker signal dashboard**: View current z-score signals across the full ticker universe in one place
- **Position sizing**: Layer 3 will eventually need a position sizing model
