# Trading Alpha Engine

## Session Commands
- `go` — session start: read the last ~60 lines of `docs/session_cache.md` and give a brief summary of where we left off and what's next.
- `session close` — session end: append a session summary to `docs/session_cache.md`. No commits, no tests.
- `feature wrap` — mid-session feature complete: update relevant docs, run pre-commit checklist (`docs/pre_commit_checklist.md`), and commit. Does not trigger session close.
- `session wrap` — feature wrap followed by session close.

## Project Overview
A z-score mean reversion backtesting and optimization system targeting leveraged ETFs. Runs parallel grid sweeps over take profit, stop loss, and hold time parameters to find optimal strategy configurations with positive alpha vs SPY. Now in live trading phase with manual execution via Schwab.

## Live Trading — Current State
- **Watchlist**: AGQ (w=10 TP=19 SL=8 hold=133h), EDC (w=10 TP=17 SL=17 hold=112h), FAS (w=10 TP=25 SL=10 hold=133h), HIBL (w=10 TP=29 SL=21 hold=126h) — all z=2.0 ZScoreBreakout v1.5
- **Signal windows**: 10:25–10:40 AM ET and 15:25–15:40 PM ET (matches backtest target_hours=(9,14))
- **Execution workflow**: Stage limit order pre-market at absurd low price → Slack fires at bar close if price confirmed below lower_band → edit order to market and submit (~5 seconds). 🔶 in morning report = set phone alarm for 10:28 and 15:28.
- **Stop loss**: Schwab stop order at lower_band × (1 - (SL%+1%)) — 1% buffer over backtest SL for intraday protection. Real exit is triggered by Slack SELL signal at bar close, not the Schwab stop (which is catastrophic insurance only).
- **Position sizing**: $50k notional per trade. Share count shown in BUY Slack message.
- **Entry price**: Real-time via `yfinance fast_info.last_price` at signal check time.

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
