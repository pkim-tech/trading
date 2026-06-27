# Trading Alpha Engine

A z-score mean reversion system for leveraged ETFs, built in three layers: data collection, parameter optimization, and live trade execution.

---

## Layer 1 — Data Collection

`data_collector.py` fetches and caches hourly OHLCV data for the full ticker universe via yfinance. Data is stored as CSV files in `cache/` (one per ticker). SPY is always appended as the benchmark.

The ticker universe is defined in `tickers.json` — a plain JSON array of symbols. Edit this file to add or remove tickers; both `data_collector.py` and the optimization sweep read from it.

```bash
python data_collector.py        # runs continuously (every 5 min)
python data_collector.py --once # single fetch and exit
```

A cron job runs `--once` daily at 8 AM to keep all tickers fresh. Output is logged to `logs/data_collector_daily.log`.

---

## Layer 2 — Parameter Optimization

The core of the system. `run_optimization_sweep.py` searches for robust trading parameter sets by brute-forcing the full combination space of take profit %, stop loss %, and max hold time across one or more tickers and strategy variants.

Each combination (a "node") is evaluated by `backtester.py`, which runs a full backtest simulation and returns alpha vs SPY. Results are cached in SQLite (`cache/trading_universe.db`) so nodes are never re-evaluated.

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

## Layer 3 — Active Signals

`active_signals.py` monitors the watch list and fires BUY/SELL alerts to Slack (and console) when entry/exit conditions are met. It fetches fresh price data for each watched ticker at the start of every poll cycle — no separate data collector process needed.

```bash
python active_signals.py                        # monitor all watched tickers
python active_signals.py --ticker AGQ,TQQQ      # limit to specific tickers
```

**Watch list management:**

```bash
python active_signals.py list       # show watched nodes
python active_signals.py add        # add a node interactively
python active_signals.py remove     # remove a node
python active_signals.py positions  # show open positions
```

**Slack — Socket Mode (interactive):** set `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, and `SLACK_CHANNEL` in `.env`. BUY/SELL alerts include Executed/Skipped buttons. Clicking opens a modal to enter execution price, which opens or closes a position in the DB. A price chart (30-day price + SMA/bands + signal marker) is uploaded to the channel on each signal.

**Slack — Webhook fallback:** set `SLACK_WEBHOOK_URL` in `.env`. Fire-and-forget, no interactive buttons.

**Console only:** works without any Slack config — blocks on stdin and prompts for execution price.

Poll interval defaults to 300s and is controlled by `SIGNAL_POLL_SECS` in `.env`.

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
