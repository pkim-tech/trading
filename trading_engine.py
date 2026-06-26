import sys
import argparse
import logging
import pandas as pd
import numpy as np
from pathlib import Path

# Import our data tools safely from data_manager
from data_manager import generate_mock_signal_data

# --- CONFIGURATION ---
CACHE_DIR = Path("./cache")
ENTRY_THRESHOLD = 2.0

def parse_arguments():
    """Configures and parses command-line arguments for the engine."""
    parser = argparse.ArgumentParser(description="Algorithmic Trading Bot Controller")
    parser.add_argument(
        "--mode", 
        choices=["BACKTEST", "LIVE", "TEST_BUY", "TEST_SELL"], 
        default="BACKTEST", 
        help="Execution mode (default: BACKTEST)"
    )
    parser.add_argument(
        "--ticker", 
        default="AAPL", 
        help="Stock ticker symbol (default: AAPL)"
    )
    parser.add_argument(
        "--window", 
        type=int,
        default=20, 
        help="SMA lookback window size (default: 20)"
    )
    return parser.parse_args()


def process_indicators(df_daily, window_size):
    """Calculates SMA and Standard Deviation dynamically on daily bars using available columns."""
    df_daily = df_daily.copy()
    
    # 🟢 Dynamically identify the correct closing column
    close_col = 'Adj Close' if 'Adj Close' in df_daily.columns else 'Close'
    
    df_daily['SMA'] = df_daily[close_col].rolling(window=window_size).mean()
    df_daily['Std'] = df_daily[close_col].rolling(window=window_size).std()
    return df_daily


def check_signal(current_price, sma, std):
    """Compares current hourly price to daily metrics and returns a Z-Score signal."""
    if pd.isna(sma) or pd.isna(std) or std == 0:
        return "HOLD"
        
    z_score = (current_price - sma) / std
    
    if z_score <= -ENTRY_THRESHOLD:
        return "BUY"
    elif z_score >= ENTRY_THRESHOLD:
        return "SELL"
    return "HOLD"


def run_system():
    args = parse_arguments()
    logging.info(f"🎬 Initializing System in [{args.mode}] mode for {args.ticker}...")

    # 1. ROUTE TO DATA SOURCE (Mock Testing vs. Real Local Cache)
    if args.mode in ["TEST_BUY", "TEST_SELL"]:
        logging.info(f"🧪 Injecting controlled mock data from data_manager...")
        target = "BUY" if args.mode == "TEST_BUY" else "SELL"
        df_hourly = generate_mock_signal_data(target_signal=target)
        
        df_daily = df_hourly.resample('D').last().dropna(subset=['Adj Close'])
        df_daily['Adj Close'] = df_daily['Adj Close'] + np.random.normal(0, 0.1, len(df_daily))
        if target == "BUY":
            df_daily.iloc[-1, df_daily.columns.get_loc('Adj Close')] = 50.0
        else:
            df_daily.iloc[-1, df_daily.columns.get_loc('Adj Close')] = 150.0
            
        df_daily = process_indicators(df_daily, args.window)
    else:
        cache_path = CACHE_DIR / f"{args.ticker}_1h.csv"
        if not cache_path.exists():
            logging.error(f"❌ Cache file missing for {args.ticker} at {cache_path}. Run data_collector.py first!")
            sys.exit(1)
            
        try:
            df_hourly = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            if df_hourly.empty:
                logging.error(f"❌ Cache file for {args.ticker} is empty.")
                sys.exit(1)
                
            df_daily = df_hourly.resample('D').last().dropna(subset=['Adj Close'])
            df_daily = process_indicators(df_daily, args.window)
        except Exception as e:
            logging.error(f"❌ Error loading cache data: {e}")
            sys.exit(1)

    # 2. OUTPUT TRACKING BOUNDARIES
    start_date = df_hourly.index.min().strftime('%Y-%m-%d')
    end_date = df_hourly.index.max().strftime('%Y-%m-%d')
    logging.info(f"📅 Simulation Window Locked: {start_date} to {end_date} ({len(df_daily)} daily bars available)")

    # =========================================================================
    # 3. STRATEGY SIMULATION EXECUTION (Modularized Link)
    # =========================================================================
    from strategy_optimizer import run_backtest_simulation
    
    # This fires our multi-hour check system (9:30 AM and 2:30 PM bars)
    trades = run_backtest_simulation(df_hourly, df_daily, args.ticker, args.mode, target_hours=(9, 14))

    # =========================================================================
    # 4. RUN SUMMARY REPORT
    # =========================================================================
    print("\n" + "="*50)
    print(f"📊 MODULAR RUN SUMMARY FOR {args.ticker} ({args.mode})")
    print("="*50)
    if not trades:
        print(" No completed liquidation trades recorded in this window.")
    else:
        df_tr = pd.DataFrame(trades)
        wins = len(df_tr[df_tr['Result'] == 'WIN'])
        losses = len(df_tr[df_tr['Result'] == 'LOSS'])
        avg_ret = df_tr['Return'].mean()
        
        print(f"🔹 Total Trades Completed: {len(df_tr)}")
        print(f"🟢 Wins (Take Profit):     {wins}")
        print(f"🔴 Losses (Stop Loss):    {losses}")
        print(f"📈 Average Return/Trade:  {avg_ret*100:+.2f}%")
    print("="*50 + "\n")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_system()