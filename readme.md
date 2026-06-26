# Trading Alpha Engine

A z-score mean reversion system for leveraged ETFs, built in three layers: data collection, parameter optimization, and live trade execution.

---

## Layer 1 — Data Collection

`data_collector.py` runs as a background daemon, polling `data_manager.py` every 5 minutes to fetch and cache hourly OHLCV data for the full ticker universe via yfinance. Data is stored as CSV files in `cache/` (one per ticker). SPY is always included as the benchmark.

```bash
python data_collector.py        # runs continuously
python data_collector.py --once # single fetch and exit
```

---

## Layer 2 — Parameter Optimization

The core of the system. `run_optimization_sweep.py` searches for robust trading parameter sets by brute-forcing the full combination space of take profit %, stop loss %, and max hold time across one or more tickers and strategy variants.

Each combination (a "node") is evaluated by `strategy_optimizer.py`, which runs a full backtest simulation and returns alpha vs SPY. Results are cached in SQLite (`cache/trading_universe.db`) so nodes are never re-evaluated.

The search evolved through several approaches before settling on full brute force:
- Early versions tried smart grid search and generational refinement around alpha peaks
- The goal was to find "winning islands" — regions of the parameter space where many neighboring nodes all produce positive alpha, not just a single isolated peak
- Floating point precision issues with fine-mesh adjustments made partial search unreliable
- Full brute force (up to ~18k nodes per ticker) proved more reliable and runs overnight

Results are visualized as heatmaps in `logs/`.

```bash
python run_optimization_sweep.py
```

Config is set via `app.py` (Streamlit UI) or by editing `config.json` directly.

---

## Layer 3 — Live Trading Engine (Planned)

`trading_engine.py` is a placeholder. The intent is to take optimized parameter sets from Layer 2, apply them to live intraday signals, and track open positions across sessions.

Manual state updates will be needed (e.g. logging fills when returning home after market hours). This layer is not yet implemented.

---

## Streamlit UI

```bash
streamlit run app.py
```

Configure tickers, hyperparameters, and strategy variants. Launch and terminate optimization sweeps from the browser. Live sweep progress is read from `active_phase_grid.json`.

---

## Setup

```bash
pip install -r requirements.txt
```
