import json
import time
import argparse
import logging
from pathlib import Path

# Try to look inside your data_manager module for live network calls
try:
    from data_manager import fetch_live_data_smart
except ImportError:
    # Fail-safe mock if you are testing scripts independently without data_manager present
    def fetch_live_data_smart(ticker):
        import yfinance as yf
        df = yf.download(ticker, period="1y", interval="1h", multi_level_index=False)
        df.to_csv(Path("./cache") / f"{ticker}_1h.csv")

# --- GLOBAL CONFIGURATIONS ---
TICKERS_FILE  = Path("./tickers.json")
LOOP_INTERVAL = 300  # 5 minutes in seconds


def load_tickers() -> list[str]:
    if not TICKERS_FILE.exists():
        raise FileNotFoundError(f"{TICKERS_FILE} not found — create it with a JSON array of ticker symbols")
    return json.loads(TICKERS_FILE.read_text())

# --- RUNTIME DIRECTORY SETUP ---
LOG_DIR = Path("./logs")
LOG_DIR.mkdir(exist_ok=True)
CACHE_DIR = Path("./cache")
CACHE_DIR.mkdir(exist_ok=True)

# FORCE RESET: Clear out any conflicting configurations from imports to ensure file writing works
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

# Set up explicit professional root logger format structure
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# File Handler (Saves permanently to disk file storage)
file_handler = logging.FileHandler(LOG_DIR / "data_collector.log", mode='a', encoding='utf-8')
file_handler.setFormatter(log_formatter)
root_logger.addHandler(file_handler)

# Stream Handler (Displays live colored output directly in your active terminal window)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(log_formatter)
root_logger.addHandler(stream_handler)


def parse_runtime_arguments():
    """Allows turning off the background loop mode if you want a quick one-shot manual download."""
    parser = argparse.ArgumentParser(description="Live Portfolio Data Collector Daemon")
    parser.add_argument("--once", action="store_true", help="Run the collection loop exactly once and exit immediately.")
    return parser.parse_args()


def start_batch_collector():
    args = parse_runtime_arguments()
    
    portfolio_tickers = load_tickers()
    if "SPY" not in portfolio_tickers:
        portfolio_tickers.append("SPY")
        
    logging.info("🚀 Production Background Data Collector Initialized.")
    logging.info(f"📋 Tracking {len(portfolio_tickers)} tokens: {', '.join(portfolio_tickers)}")
    logging.info(f"⏱️ Heartbeat loop interval set to {LOOP_INTERVAL // 60} minutes.")
    logging.info("---------------------------------------------------------")
    
    while True:
        logging.info("By Volatility Target: Starting synchronized portfolio updates...")
        start_time = time.time()
        
        # Loop through all portfolio tokens sequentially in a fast concurrent burst
        for ticker in portfolio_tickers:
            logging.info(f"🔄 Syncing {ticker}...")
            try:
                # Triggers your custom network / cache tracking math
                fetch_live_data_smart(ticker)
                logging.info(f"    ✅ {ticker} successfully updated and cached.")
            except Exception as e:
                # Captures network drops, API issues, or proxy timeouts without crashing the engine
                logging.warning(f"    ⚠️ Could not update {ticker} during this slice: {e}")
                
        elapsed_time = time.time() - start_time
        logging.info(f"💾 Batch portfolio sync completed in {elapsed_time:.2f} seconds.")
        
        # Break out immediately if user specified single-execution tracking flags
        if args.once:
            logging.info("🏁 Single-execution run completed (--once flag active). Terminating framework.")
            break
            
        logging.info(f"⏳ Sleeping for {LOOP_INTERVAL // 60} minutes...\n")
        time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    try:
        start_batch_collector()
    except KeyboardInterrupt:
        logging.info("🛑 Data Collector stopped manually via KeyboardInterrupt exit command.")
    except Exception as e:
        logging.critical(f"🚨 Data Collector loop suffered an unhandled exit error: {e}")