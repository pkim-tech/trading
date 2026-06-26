# Algorithmic Trading Bot Controller

A modular, production-ready quantitative trading engine designed for algorithmic development, fast offline simulation, and smart live market backfilling. The system uses a customized, bulletproof local caching mechanism to prevent API rate-limiting while providing high-fidelity historical data workflows.

---

## 🛠 System Architecture & Structural Framework

The ecosystem is built on a decoupled, separation-of-concerns framework across four operational vectors:

1. **`data_manager.py` (The Data Intake Engine):** Governs data ingestion. Features a hybrid synthetic data generator for development and a **Hardened Incremental Backfiller** for live profiles. It reads local disk boundaries, fetches an overlapping buffer to absorb non-trading anomalies (weekends/holidays), and performs lookback deduplication using vector-optimized Pandas routines (`keep='last'`).
2. **`strategy.py` (The Mathematical Core):** Houses the pricing metrics. Computes rolling Simple Moving Averages (SMA) and Standard Deviation channels over custom periods, calculating dynamic Z-Scores to output market signals (`BUY`, `SELL`, `HOLD`).
3. **`main.py` (The Orchestrator):** Manages initialization, processes environment CLI parameters, locks simulation timelines, handles exception states, and drives the execution tracker frame-by-frame.
4. **`test_strategy.py` (The Verification Layer):** An isolated, deterministic unit testing harness designed to validate strategy calculations and edge cases against mock matrices before exposing the core engine to live markets.

---

## 🚀 Environment Installation & Dependencies

Ensure your execution environment is provisioned with Python 3.8+ and the following scientific computing and data-access frameworks:

```bash
pip install pandas numpy yfinance
