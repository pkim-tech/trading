# Trading Alpha Engine

## Session Commands
- `go` — session start: read `docs/handover.md` (compact current state) and give a brief summary of where we left off and what's next. Fall back to `docs/session_cache.md` only if handover.md is missing or unclear.
- `session close` — session end: append a handover summary to `docs/session_cache.md` AND overwrite `docs/handover.md` with the current compact state. No commits, no tests.
- `feature wrap` — mid-session feature complete: update relevant docs, run pre-commit checklist (`docs/pre_commit_checklist.md`), and commit. Does not trigger session close.
- `session wrap` — feature wrap followed by session close.

## Project Overview
A z-score mean reversion backtesting and optimization system targeting leveraged ETFs (TQQQ, SOXL, AGQ, KORU, etc.). Runs parallel grid sweeps over take profit, stop loss, and hold time parameters to find optimal strategy configurations with positive alpha vs SPY.

## Key Files
- `app.py` — Streamlit UI for configuring and launching optimization sweeps
- `run_optimization_sweep.py` — main parallel optimization engine (brute force + generational refinement)
- `backtester.py` — core backtest simulation logic (`run_backtest`)
- `strategies.py` — strategy classes: `ZScoreBreakout`, `TrendFilteredZScore`
- `data_collector.py` — fetches and caches hourly price data
- `data_manager.py` — data fetching and cache management
- `config.json` — runtime config: tickers, hyperparameters, strategy selection
- `pages/` — Streamlit multipage app views (Spatial Topology, Node Inspector)

## Runtime Artifacts (not committed)
- `cache/` — hourly CSV data per ticker + SQLite results DB (`trading_universe.db`)
- `logs/` — optimization output PNGs, CSVs, text reports
- `output/` — archived/legacy files
- `active_phase_grid.json` — live progress state written during sweep runs
- `current_test.json` — temp telemetry file, deleted on sweep exit

## How to Run
```bash
# Install dependencies
pip install -r requirements.txt

# Launch Streamlit UI
streamlit run app.py

# Or run optimization sweep directly
python run_optimization_sweep.py
```

## Architecture Notes
- Optimization uses `ProcessPoolExecutor` with up to 10 workers
- Results cached in SQLite to avoid re-running completed nodes
- Multi-generation refinement: macro grid scan → frontier detection → fine mesh around top performers
- `active_phase_grid.json` is written by the sweep and read by the Streamlit UI for live progress display
